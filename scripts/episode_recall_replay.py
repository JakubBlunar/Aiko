"""Compare one-slot ranked recall with a receipt-first selector on fictional episodes."""

import argparse
from contextlib import closing
from dataclasses import dataclass
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
from app.core.memory.learned_strategies import words
from app.core.memory.memory_origin import memory_origin
from app.core.memory.memory_relations import admit_relation, incoming_relations, reference_revision
from app.core.memory.memory_store import MemoryStore
from app.core.memory.observation_receipts import admit_observation, longitudinal_sources_current
from app.core.memory.reflective_evidence import eligible
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


@dataclass(frozen=True)
class Episode:
    memory_id: int
    message_ids: tuple[int, ...]
    text: str


def _format_receipt(receipt, now):
    return timephrase.format_transcript([{
        "role": receipt["role"], "content": receipt["quote"], "created_at": receipt["source_at"],
    }], now)


def _packet(memory, observation, store, now):
    sources = observation["sources"]
    action, outcome = observation.get("action"), observation.get("outcome")
    fields = [observation["context"], action, outcome, *observation.get("exceptions", [])]
    if any(not isinstance(field, dict) or field not in sources or field.get("role") != "user"
           or field.get("session_id") != memory.source_session for field in fields):
        return None
    corrections, truncated = incoming_relations(memory, store)
    if truncated:
        return None
    for row in corrections:
        relation = row["receipt"]
        if relation["relation"] == "correction":
            if not row["current"] or not relation["report"]["session_id"].startswith(f"{_OWNER}:"):
                return None
            outcome = relation["report"]
            break
    fields = [observation["context"], action, outcome, *observation.get("exceptions", [])]
    text = "\n".join(dict.fromkeys(_format_receipt(field, now) for field in fields))
    if len(text) > _CONTEXT_LIMIT:
        return None
    message_ids = tuple(sorted({item["message_id"] for item in [*sources, outcome]}))
    return Episode(memory.id, message_ids, text)


def select_episode(question, candidates, store, now):
    """Select only a unique literal match among eligible user report receipts."""
    query_words = words(question)
    matches = []
    for memory in candidates:
        observation = (memory.metadata or {}).get("observation")
        origin = memory_origin(memory)
        if (not isinstance(observation, dict) or observation.get("subject") != "user"
                or origin.subject != "user" or origin.mode != "observation"
                or origin.domain != "conversation"
                or not (memory.source_session or "").startswith(f"{_OWNER}:")):
            continue
        context = observation.get("context") or {}
        if context.get("context_only") or context.get("role") != "user":
            continue
        overlap = len(query_words.intersection(words(context.get("quote", ""))))
        if overlap >= 2:
            matches.append((overlap, memory, observation))
    if not matches:
        return None, "no_match"
    best = max(item[0] for item in matches)
    episodes = {}
    for overlap, memory, observation in matches:
        if overlap == best:
            if not eligible(memory) or not longitudinal_sources_current(memory, store):
                return None, "source_unavailable"
            packet = _packet(memory, observation, store, now)
            if packet is None:
                return None, "source_unavailable"
            episodes.setdefault(packet.message_ids, packet)
    if len(episodes) > 1:
        return None, "ambiguous"
    return next(iter(episodes.values())), "matched"


def seed_episodes(db, store):
    vector = np.array([1.0, 0.0], dtype=np.float32)
    episodes = {}
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
        mid = db.add_message(session, "user", content)
        row = db.get_message_row(mid)
        observation = admit_observation({
            facet: {"message_id": mid, "quote": quote} for facet, quote in (
                ("context", context), ("action", action), ("outcome", outcome),
            )
        }, [row], session_key=session, subject="user")
        episodes[name] = store.add(
            content, "event", vector, source_session=session, source_message_id=mid,
            metadata={"observation": observation}, temporal_type="past_event",
            provenance="stated", skip_dedupe=True,
        )

    prior = episodes["purifier"]
    session = f"{_OWNER}:later"
    correction = "Correction, the bedroom air purifier still clicks after I reseated the filter."
    mid = db.add_message(session, "user", correction)
    relation = admit_relation({
        "prior_memory_id": prior.id, "relation": "correction",
        "report": {"message_id": mid, "quote": correction},
    }, [db.get_message_row(mid)], {prior.id: {"revision": reference_revision(prior)}}, store,
        session_key=session, subject="user")
    store.add(correction, "fact", vector, source_session=session, source_message_id=mid,
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
