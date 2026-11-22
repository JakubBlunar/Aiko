"""Exercise ordinary prompt assembly and visible replies with a local Ollama model."""

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from app.core.conversation.clarification_reply import is_clarification_question
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.infra.settings import (
    LLM_ROLE_WORKER_DEFAULT,
    find_provider,
    load_settings,
    transport_for_provider,
)
from app.core.memory.memory_store import MemoryStore
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.session_text_utils import sanitize_assistant_text
from app.core.session.turn_runner import TurnRunner
from scripts.episode_recall_replay import seed_episodes, select_episode
from scripts.live_scenarios import LoopbackPolicyClient, require_local_model


@dataclass(frozen=True)
class Case:
    request: str
    question: str | None = None
    evidence: str = ""
    history: tuple[tuple[str, str], ...] = ()
    expected_words: tuple[str, ...] = ()
    forbidden_words: tuple[str, ...] = ()


def cases(episodes, store):
    unknown = "What did I do when the fan rattled?"
    known = "What happened when I worked on the kitchen extractor fan?"
    now = timephrase.utcnow()
    _episode, status = select_episode(unknown, episodes.values(), store, now)
    selected, known_status = select_episode(known, episodes.values(), store, now)
    if status != "ambiguous" or known_status != "matched":
        raise ValueError("Episode fixtures did not produce the expected evidence states")
    return {
        "episode_ambiguous": Case(
            unknown, "Which one do you mean?",
            "The evidence leaves multiple matches. Which fan the user means is unresolved.",
        ),
        "episode_resolved": Case(known, evidence=selected.text,
                                 expected_words=("screws", "hum"),
                                 forbidden_words=("motor", "probably", "because", "?")),
        "reference_without_history": Case(
            "What did we decide about that one?", "Which one do you mean?",
        ),
        "reference_in_current_chat": Case(
            "Can you give me feedback on it?", "Which one do you mean?",
            history=(("user", "I'm considering the orange logo and the gray logo."),
                     ("assistant", "Both are reasonable options.")),
        ),
        "goal_missing": Case(
            "I'd like help with my spreadsheet of weekly sales and item counts.",
            "What would you like to achieve?",
        ),
        "time_missing": Case(
            "Draft one sentence to Mira confirming my arrival time. "
            "It must include the exact time, which I haven't provided yet.",
            "What time should I use?",
        ),
        "followup_answered": Case(
            "I mean the office desk fan. Reply with just the room name.",
            history=(("user", "I mentioned the office desk fan and the kitchen extractor fan."),
                     ("assistant", "Which one do you mean?")),
            expected_words=("office",), forbidden_words=("?", "humming", "cooler"),
        ),
        "time_answered": Case(
            "It's 3 PM. Draft the one-sentence arrival text to Mira.",
            history=(("user", "Please draft a text to Mira confirming my arrival time."),
                     ("assistant", "What time should I use?")),
            expected_words=("3", "mira"), forbidden_words=("?",),
        ),
        "creative_request": Case("Give me one calm name for a coffee shop."),
        "answerable_with_choices": Case(
            "Give me one substitute for fresh basil in tomato pasta.",
            forbidden_words=("could you", "which pasta"),
        ),
    }


def replay(db, store, persona_path, client, route):
    results = {}
    for name, case in cases(seed_episodes(db, store), store).items():
        key = f"replay:clarification-{name}"
        for role, text in case.history:
            db.add_message(key, role, text)
        prompt = PromptAssembler(db, persona_path=persona_path, continuity_max_messages=0)
        if case.evidence:
            prompt.set_inner_life_providers(
                conversation_situation=lambda _, evidence=case.evidence: evidence,
            )
        runner = TurnRunner(
            client, db, prompt, model=route.model, context_window=route.context_window or 8192,
            max_tokens=170, temperature=0.0, filler_enabled=False,
        )
        visible = []
        result = runner.run(session_key=key, user_text=case.request, on_token=visible.append)
        saved = (db.get_message_row(result.assistant_message_id)
                 if result.assistant_message_id else None)
        expected = case.question
        if expected is None:
            answer = result.text.casefold()
            behavior_matches = (bool(answer) and not is_clarification_question(result.text)
                                and all(word in answer for word in case.expected_words)
                                and not any(word in answer for word in case.forbidden_words))
        else:
            behavior_matches = result.text == expected
        results[name] = {
            "expected_question": expected, "reply": result.text,
            "passes": bool(behavior_matches and saved and saved.content == result.text
                           and sanitize_assistant_text("".join(visible)) == result.text),
        }
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=101)
    args = parser.parse_args()
    settings = load_settings(read_only=True)
    route = settings.llm.routes.get(LLM_ROLE_WORKER_DEFAULT)
    provider = find_provider(settings.llm, route.provider_id) if route else None
    if provider is None or provider.kind != "ollama":
        parser.error("The configured worker_default route must use loopback Ollama")

    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with (requests.Session() as session,
          TemporaryDirectory(prefix="clarification-", dir=scratch) as temp):
        client = LoopbackPolicyClient(transport_for_provider(provider, route=route),
                                     provider.base_url, session)
        require_local_model(session, provider.base_url, route.model)

        def offline_post(url, *, json, **kwargs):
            payload = {**json, "options": {**json["options"], "seed": args.seed}}
            return client._post_structured_chat(url, json=payload, **kwargs)

        root = Path(temp)
        persona_path = root / "persona.txt"
        persona_path.write_text(
            "You are Aiko, a conversational companion. Speak naturally to {user_name}.\n",
            encoding="utf-8",
        )
        db_path = root / "replay.db"
        db = ChatDatabase(db_path)
        moment = datetime(2026, 11, 22, 12, tzinfo=timezone.utc)
        with (closing(db._get_conn()), closing(MemoryStore(db_path)) as store,
              timephrase.scoped_now(lambda: moment),
              patch("app.llm.ollama_client.requests.post", offline_post)):
            results = replay(db, store, persona_path, client, route)
    print(json.dumps({"model": route.model, "seed": args.seed, "cases": results}, indent=2))
    return 0 if all(case["passes"] for case in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
