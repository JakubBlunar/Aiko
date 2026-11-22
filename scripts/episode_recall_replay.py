"""Compare one-slot ranked recall with a receipt-first selector on fictional episodes."""

import argparse
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import requests

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import (
    LLM_ROLE_WORKER_DEFAULT,
    find_provider,
    load_settings,
    transport_for_provider,
)
from app.core.memory.episode_recall import select_episode as _select_episode
from app.core.memory.memory_relations import admit_relation, reference_revision
from app.core.memory.memory_store import MemoryStore
from app.core.memory.observation_receipts import admit_observation
from app.core.rag.rag_retriever import RagRetriever
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


_OWNER = "replay"
_CONTEXT_LIMIT = 480
_AMBIGUITY_NOTICE = (
    "Several recorded episodes match this reference. There is no established match. "
    "Ask which object the user means before recalling any action or result."
)
_SYSTEM = (
    "Answer the current user message conversationally. Treat quoted context as a report, "
    "including partial outcomes and corrections. Preserve what remains unresolved and who "
    "reported it. Never present a different object or scene as the one the user means. "
    "If the reference matters but cannot be resolved, ask one short question. "
    "For unrelated requests answer normally without bringing up prior episodes."
)
_ANSWER_SCHEMA = {
    "type": "object", "properties": {"reply": {"type": "string"}}, "required": ["reply"],
}
_CASES = {
    "partial_repair": ("What happened when I worked on the kitchen extractor fan?", "extractor"),
    "similar_object": ("What happened with my office desk fan?", "desk"),
    "corrected_outcome": ("Did my change fix the bedroom air purifier?", "purifier"),
    "ambiguous_object": ("What did I do when the fan rattled?", None),
    "unrelated": ("Could you suggest a simple design for a thank-you card?", None),
}


def select_episode(question, candidates, store, now):
    return _select_episode(question, candidates, store, now, owner=_OWNER)


def seed_episodes(db, store, *, embed=None):
    def embedding(text):
        return embed(text) if embed is not None else np.array([1.0, 0.0], dtype=np.float32)

    episodes = {}
    now = timephrase.utcnow()
    for name, context, action, outcome in (
        ("extractor", "When the kitchen extractor fan rattled on high",
         "I tightened the mounting screws", "That fixed the rattle but it still hums on high."),
        ("desk", "When the office desk fan rattled on high",
         "I cleaned the grille", "That fixed the rattle completely."),
        ("purifier", "When the bedroom air purifier clicked after a filter change",
         "I reseated the filter", "That fixed the clicking."),
    ):
        session = f"{_OWNER}:{name}"
        content = f"{context}, {action}. {outcome}"
        with timephrase.scoped_now(lambda: now - timedelta(days=5)):
            mid = db.add_message(session, "user", content)
        row = db.get_message_row(mid)
        observation = admit_observation({
            facet: {"message_id": mid, "quote": quote} for facet, quote in (
                ("context", context), ("action", action), ("outcome", outcome),
            )
        }, [row], session_key=session, subject="user")
        episodes[name] = store.add(
            content, "event", embedding(content), source_session=session, source_message_id=mid,
            metadata={"observation": observation}, temporal_type="past_event",
            provenance="stated", skip_dedupe=True,
        )

    prior = episodes["purifier"]
    session = f"{_OWNER}:later"
    correction = "Correction, the bedroom air purifier still clicks after I reseated the filter."
    with timephrase.scoped_now(lambda: now - timedelta(days=2)):
        mid = db.add_message(session, "user", correction)
    relation = admit_relation({
        "prior_memory_id": prior.id, "relation": "correction",
        "report": {"message_id": mid, "quote": correction},
    }, [db.get_message_row(mid)], {prior.id: {"revision": reference_revision(prior)}}, store,
        session_key=session, subject="user")
    store.add(correction, "fact", embedding(correction),
              source_session=session, source_message_id=mid,
              metadata={"contextual_relation": relation}, provenance="stated", skip_dedupe=True)
    return episodes


def replay(episodes, store, infer):
    now = timephrase.utcnow()
    results = {}
    for case, (question, expected) in _CASES.items():
        # Both arms receive the same candidates and one memory slot. The
        # leading neighbors simulate a close but misleading semantic match.
        ranked = list(episodes.values())
        if case in {"partial_repair", "ambiguous_object"}:
            ranked[0], ranked[1] = ranked[1], ranked[0]
        if case == "corrected_outcome":
            ranked.insert(0, ranked.pop())
        selected, status = select_episode(question, ranked, store, now)
        baseline = RagRetriever._memory_to_hit(ranked[0], score=1.0)
        evidence = {
            "ranked": (int(baseline.record.id), RagRetriever.format_block([baseline])),
            "episode": (selected.memory_id, selected.text) if selected else (None, ""),
        }
        if status == "ambiguous":
            evidence["episode_with_ambiguity"] = (None, _AMBIGUITY_NOTICE)
        arms = {}
        for arm, (memory_id, text) in evidence.items():
            if len(text) > _CONTEXT_LIMIT:
                raise ValueError("Fixture exceeds the shared context ceiling")
            prompt = (f"{timephrase.today_anchor(now)}\nPast context:\n"
                      f"{text.ljust(_CONTEXT_LIMIT)}\nCurrent user message: {question}")
            arms[arm] = {"memory_id": memory_id, **infer(_SYSTEM, prompt, _ANSWER_SCHEMA)}
        expected_id = episodes[expected].id if expected else None
        results[case] = {"expected_id": expected_id, "selection_status": status, "arms": arms,
                         "source_message_ids": list(selected.message_ids) if selected else []}
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=53)
    args = parser.parse_args()
    settings = load_settings(read_only=True)
    route = settings.llm.routes.get(LLM_ROLE_WORKER_DEFAULT)
    provider = find_provider(settings.llm, route.provider_id) if route else None
    if provider is None or provider.kind != "ollama":
        parser.error("The configured worker_default route must use local Ollama")
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as session, TemporaryDirectory(prefix="rs3-", dir=scratch) as trial:
        client = LoopbackPolicyClient(transport_for_provider(provider, route=route),
                                     provider.base_url, session)
        require_local_model(session, provider.base_url, route.model)

        def infer(system, prompt, schema):
            raw, usage = client.chat_json(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                model=route.model, json_schema=schema, timeout_seconds=120,
                options={"seed": args.seed, "temperature": 0, "num_predict": 200},
                surface="episode_recall_replay",
            )
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("reply"), str):
                raise ValueError("Invalid reply from the local model")
            return {"reply": payload["reply"], "prompt_tokens": usage.prompt_tokens}

        path = Path(trial) / "episodes.db"
        db = ChatDatabase(path)
        with closing(db._get_conn()), closing(MemoryStore(path)) as store:
            cases = replay(seed_episodes(db, store), store, infer)
    print(json.dumps({"model": route.model, "seed": args.seed, "cases": cases}, indent=2))


if __name__ == "__main__":
    main()
