"""Replay ordinary chat after a checked troubleshooting result and an unrelated detour."""
import argparse
from contextlib import closing
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.core.conversation.conversation_situation import ConversationSituationStore
from app.core.conversation.conversation_situation_worker import ConversationSituationWorker
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.session.conversation_situation_mixin import ConversationSituationMixin
from app.core.session.inner_life_part1 import InnerLifePart1Mixin
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from app.llm.chat_client import ChatUsage
from scripts.live_scenarios import require_local_model


QUESTION = "What causes my desk microphone clicks?"
CHECK = "Are the clicks audible in a local recording of the same input?"
RETURN = "Back to the desk microphone clicks. Which explanation fits the test better?"
CASES = {
    "present": ("yes", RETURN),
    "absent": ("no", RETURN),
    "wrong_input": ("unknown", RETURN),
    "correction": ("yes", "Back to the desk microphone clicks. Correction: I listened to the "
                   "wrong file. The actual local recording is clean. Which explanation fits?"),
    "other_device": ("yes", "Back to the laptop microphone clicks. What evidence do you "
                     "have about that microphone?"),
    "unrelated": ("yes", "Suggest one name for a bakery."),
}
RESULT = {"yes": "I hear the clicks in the local recording of the same input.",
          "no": "The local recording of the same input is clean.",
          "unknown": "The recording is clean, but it used a different input."}


class RecordedObserver:
    """Hold the prediction and extraction constant while varying the user report."""
    def chat_json(self, *_args, **_kwargs):
        return json.dumps(self.payload), ChatUsage(done_reason="stop")


class Host(ConversationSituationMixin, InnerLifePart1Mixin):
    session_key = "replay:chat"
    _user_id = "replay"
    _settings = SimpleNamespace(agent=SimpleNamespace())
    _memory_settings = SimpleNamespace()
    user_display_name = "Robin"


class MissingObservation(Exception):
    """The model observer did not establish the prerequisite for the next stage."""


def seed(db, case, *, observer_client=None, model="fixture"):
    host = Host()
    host._chat_db = db
    host._conversation_situation_store = store = ConversationSituationStore(db)
    observer = RecordedObserver()
    worker = ConversationSituationWorker(
        client=observer_client or observer, chat_db=db, store=store, model=model,
        world_snapshot_provider=dict, judgment_shadow_enabled=False,
    )
    session = host.session_key
    answer, _ = CASES[case]
    goal = db.add_message(session, "user", QUESTION)
    check = db.add_message(session, "assistant", "An input fault would click even in a local "
                           "recording of the same input. A call transport fault would leave "
                           "that recording clean if it bypasses the call. If yes, inspect the "
                           "input. If no, inspect the call path. " + CHECK)

    def note(text, mid):
        return {"text": text, "evidence_message_ids": [mid]}

    working = {
        "question": note(QUESTION, goal), "next_check": note(CHECK, check),
        "hypotheses": [note("An input-side fault", check), note("A call transport fault", check)],
        "decision_check": {"if_yes": "Inspect the input.", "if_no": "Inspect the call path."},
        "causal_check": {"expects_yes": 0, "assumptions": ["the same input was recorded",
                         "the recording bypasses call transport"], "result_applies": False},
    }
    observer.payload = {"operation": "replace", "summary": "Diagnosing desk microphone clicks.",
                        "evidence_message_ids": [goal, check], "working_set": working}
    def observe(field):
        state = worker.run(session)
        if not (state and state.working_set and getattr(state.working_set, field)):
            raise MissingObservation(field)
        return state.working_set

    observe("causal_check")
    report = db.add_message(session, "user", RESULT[answer])
    observer.payload["evidence_message_ids"].append(report)
    working["check_result"] = note(RESULT[answer], report)
    if answer != "unknown":
        working["decision_check"]["answer"] = answer
        working["causal_check"]["result_applies"] = True
    observe("check_result")
    for message in ("New topic. Let's talk about pottery.", "The pots have blue glaze."):
        mid = db.add_message(session, "user", message)
        db.add_message(session, "assistant", "That sounds lovely.")
        observer.payload = {"operation": "replace", "summary": "Discussing pottery.",
                            "evidence_message_ids": [mid], "working_set": None}
        observe("suspended")
    return host, report


def main():
    from scripts.companion_pattern_replay import BASE, ReplayModel

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=71)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--model-observer", action="store_true",
                        help="also perform extraction with the installed model")
    parser.add_argument("--case", choices=CASES)
    args = parser.parse_args()
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as session, TemporaryDirectory(dir=scratch, prefix="diagnosis-") as tmp:
        client = ReplayModel(session, args.model, args.seed)
        require_local_model(session, BASE, args.model)

        def chat_post(url, *, json, **kwargs):
            return client._post_structured_chat(url, json={
                **json, "options": {**json.get("options", {}), "seed": args.seed},
            }, **kwargs)

        persona = Path(tmp) / "persona.txt"
        persona.write_text("You are Aiko. Answer {user_name} briefly.", encoding="utf-8")
        moment = timephrase.parse_iso("2026-11-22T12:00:00Z")
        with (patch("app.llm.ollama_client.requests.post", chat_post),
              timephrase.scoped_now(lambda: moment)):
            for case in ([args.case] if args.case else CASES):
                _, question = CASES[case]
                db = ChatDatabase(Path(tmp) / f"{case}.db")
                with closing(db._get_conn()):
                    try:
                        host, _ = seed(db, case, observer_client=client if args.model_observer
                                       else None, model=args.model)
                    except MissingObservation as exc:
                        if not args.model_observer:
                            raise
                        print(json.dumps({"case": case, "model": args.model, "seed": args.seed,
                                          "observer": "model", "missing": str(exc),
                                          "last_observation": client.responses[-1]}), flush=True)
                        continue
                    prompt = PromptAssembler(db, persona_path=persona, recent_window=2,
                                             continuity_max_messages=0)
                    host._prompt_assembler = prompt
                    prompt.set_relevant_context_provider(host.build_relevant_context)
                    prompt.set_inner_life_providers(
                        conversation_situation=host._render_conversation_situation_block)
                    runner = TurnRunner(client, db, prompt, model=args.model, context_window=8192,
                                        max_tokens=400, temperature=0, filler_enabled=False)
                    result = runner.run(session_key=host.session_key, user_text=question)
                    print(json.dumps({"model": args.model, "seed": args.seed, "case": case,
                                      "observer": "model" if args.model_observer else "fixture",
                                      "finish": result.telemetry.concepts_surfaced.get("reason")
                                      if result.telemetry.decision_reply else
                                      client.last_usage.done_reason, "reply": result.text,
                                      "returned": "Check already performed" in
                                      result.telemetry.system_prompt}), flush=True)


if __name__ == "__main__":
    main()
