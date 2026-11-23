"""Replay ordinary chat after a checked troubleshooting result and an unrelated detour."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import requests

from app.core.conversation.conversation_situation import (
    ConversationSituationStore, _extract_json_object,
)
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


def microphone_reply_fits(reply, *, reported):
    """Score the two labeled speech acts in the ordinary microphone replay."""
    text = reply.casefold().replace("**", "").replace("’", "'")
    if reported:
        cause = r"(?:an? |the )?input(?:-side)? (?:fault|issue|problem)"
        favored = re.search(
            rf"\b(?:{cause} (?:fits? (?:the .*? )?better|(?:is |seems )?more (?:likely|plausible))"
            rf"|(?:points? (?:to|towards?)|suggests?|favou?rs?) {cause})\b", text,
        )
        return bool(favored and "click" in text and "record" in text
                    and not re.search(r"\b(?:definitely|proves?|confirms?|ruled out)\b", text))
    invitation = re.search(r"\b(?:if|whether|try|let me know|listen|check)\b", text)
    assertion = re.search(r"\b(?:you(?:'ve| have) (?:heard|confirmed|found)|"
                          r"(?:since|given that|because) (?:the )?clicks? |"
                          r"(?:is|are) (?:definitely|confirmed)|the culprit is)\b", text)
    for sentence in re.split(r"(?<=[.?!])\s+", text):
        if sentence.endswith("?") or re.search(r"\b(?:if|whether|would)\b", sentence):
            continue
        if re.search(r"\b(?:clicks? (?:persist|are audible|show up)|"
                     r"(?:input|transport) fault fits better|(?:that|this) (?:confirms?|proves?)|"
                     r"recording (?:is clean|contains clicks))\b", sentence):
            return False
    return bool("record" in text and (invitation or "?" in text) and not assertion)

# Gold opportunities fixed before running the observer. The press owner proposes
# the test. The fan explanation supplies no discriminator or predicted outcomes.
EXAMPLES = {
    "microphone": (QUESTION, CHECK, [
        ("user", QUESTION),
        ("assistant", "An input fault would click even in a local recording of the same input. "
         "A call transport fault would leave that recording clean if it bypasses the call. "
         "If yes, inspect the input. If no, inspect the call path. " + CHECK),
    ]),
    "press": ("Why are my press prints streaky?", "Does a fresh plate also leave streaks?", [
        ("user", "Why are my press prints streaky?"),
        ("assistant", "We could distinguish a plate defect from a roller defect."),
        ("user", "Does a fresh plate also leave streaks?"),
        ("assistant", "A damaged plate would leave the fresh print clean. A faulty roller would "
         "leave streaks even with the fresh plate. If yes, inspect the roller. "
         "If no, inspect the original plate."),
    ]),
    "fan": ("Why does the loft fan hum?", None, [
        ("user", "Why does the loft fan hum?"),
        ("assistant", "A loose grille might explain the sound."),
    ]),
}


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
    goal, check = [db.add_message(session, role, content)
                   for role, content in EXAMPLES["microphone"][2]]

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
    parser.add_argument("--capture-only", action="store_true",
                        help="inspect independent initial captures, including attribution controls")
    parser.add_argument("--ordinary", action="store_true",
                        help="generate every assistant reply and observe each completed turn")
    scenario = parser.add_mutually_exclusive_group()
    scenario.add_argument("--two-problems", action="store_true",
                          help="switch between two tested problems after correcting the first")
    scenario.add_argument("--microphone", action="store_true",
                          help="score the proposed microphone test and its reported result")
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
            if args.ordinary or args.two_problems or args.microphone:
                db = ChatDatabase(Path(tmp) / "ordinary.db")
                with closing(db._get_conn()):
                    host = Host()
                    host._chat_db = db
                    host._conversation_situation_store = store = ConversationSituationStore(db)
                    prompt = PromptAssembler(db, persona_path=persona, recent_window=2,
                                             continuity_max_messages=0)
                    host._prompt_assembler = prompt
                    prompt.set_relevant_context_provider(host.build_relevant_context)
                    prompt.set_inner_life_providers(
                        conversation_situation=host._render_conversation_situation_block)
                    runner = TurnRunner(client, db, prompt, model=args.model, context_window=8192,
                                        max_tokens=300, temperature=0, filler_enabled=False)
                    worker = ConversationSituationWorker(
                        client=client, chat_db=db, store=store, model=args.model,
                        world_snapshot_provider=dict, judgment_shadow_enabled=False,
                    )
                    goal, check, dialogue = EXAMPLES["press"]
                    recap = "Back to the press prints. Which explanation fits better?"
                    turns = [
                        (goal, None), (dialogue[-1][1] + " " + check, None),
                        ("I tested the fresh plate. It also leaves streaks.", "yes"),
                        ("New topic. I'm glazing a blue bowl in pottery class.", "yes"),
                        (recap, "yes"), ("Back to the press prints. Correction: I mixed up the "
                                         "plates. The fresh plate actually prints clean. "
                                         "Which cause fits now?", "no"), (recap, "no"),
                    ]
                    mic_proposal = EXAMPLES["microphone"][2][1][1]
                    if args.microphone:
                        turns = [(QUESTION, None), (mic_proposal, None), (RESULT["yes"], "yes")]
                    elif args.two_problems:
                        mic_recap = "Back to the desk microphone clicks. Which cause fits better?"
                        correction = turns[-2]
                        turns = [
                            *turns[:3], correction,
                            (QUESTION, None), (mic_proposal, None),
                            (RESULT["yes"], "yes"), (mic_recap, "yes"), (recap, "no"),
                            (mic_recap, "yes"),
                        ]
                    failures = []
                    result_sources = {}
                    for index, (message, expected) in enumerate(turns):
                        result = runner.run(session_key=host.session_key, user_text=message)
                        if (args.two_problems and index == 3) or message == RESULT["yes"]:
                            row = db.get_message_row(result.outcome.user_message_id)
                            assert row.role == "user" and row.content == message
                            source = "mic" if message == RESULT["yes"] else "press"
                            result_sources[source] = (row.id,)
                        state = worker.run(host.session_key)
                        notes = state.working_set if state else None
                        observed = _extract_json_object(client.responses[-1][0]) or {}
                        decision = notes.decision_check if notes else None
                        answer = decision.answer if decision else None
                        wording_pass = None
                        if message in (mic_proposal, RESULT["yes"]):
                            reported = message == RESULT["yes"]
                            wording_pass = microphone_reply_fits(result.text, reported=reported)
                            receipt = (notes.check_result.evidence_message_ids
                                       if notes and notes.check_result else ())
                            if (not wording_pass or not notes or not notes.causal_check
                                    or notes.causal_check.result_applies != reported
                                    or receipt != (result_sources["mic"] if reported else ())):
                                failures.append(index + 1)
                        initial_goal = index == 0 or (args.two_problems and index == 4)
                        if answer != expected or not (initial_goal or
                                                      (notes and notes.causal_check)):
                            failures.append(index + 1)
                        if args.two_problems and message in (mic_recap, recap):
                            mic = message == mic_recap
                            label = "input fault" if mic else "damaged plate"
                            report = RESULT["yes"] if mic else "fresh plate actually prints clean"
                            other = "plate" if mic else "microphone"
                            reply = result.text.lower()
                            if (not result.telemetry.decision_reply or label not in reply
                                    or report.lower() not in reply or other in reply):
                                failures.append(index + 1)
                            active = "mic" if mic else "press"
                            parked = state.parked_comparison if state else None
                            if (not notes or not notes.check_result or not parked
                                    or notes.check_result.evidence_message_ids !=
                                    result_sources[active] or
                                    parked.working_set.check_result.evidence_message_ids !=
                                    result_sources["press" if mic else "mic"]):
                                failures.append(index + 1)
                        elif message == recap:
                            favored = "faulty roller" if expected == "yes" else "damaged plate"
                            if (not result.telemetry.decision_reply
                                    or favored not in result.text.lower()):
                                failures.append(index + 1)
                        print(json.dumps({
                            "user": message, "reply": result.text,
                            "goal": notes.question.text if notes else None,
                            "check": notes.next_check.text if notes and notes.next_check else None,
                            "answer": answer,
                            "reported": notes.check_result.text
                            if notes and notes.check_result else None,
                            "verdict": observed.get("diagnostic_verdict"),
                            "operation": observed.get("operation"),
                            "wording_pass": wording_pass,
                            "returned": "Check already performed" in result.telemetry.system_prompt,
                        }), flush=True)
                    if failures:
                        raise SystemExit(f"Failed replay turns: {sorted(set(failures))}")
                return
            if args.capture_only:
                failed = []
                for name, (goal, check, dialogue) in EXAMPLES.items():
                    db = ChatDatabase(Path(tmp) / f"{name}.db")
                    with closing(db._get_conn()):
                        ids = [db.add_message(Host.session_key, role, content)
                               for role, content in dialogue]
                        worker = ConversationSituationWorker(
                            client=client, chat_db=db, store=ConversationSituationStore(db),
                            model=args.model, world_snapshot_provider=dict,
                            judgment_shadow_enabled=False,
                        )
                        state = worker.run(Host.session_key)
                        notes = state.working_set if state else None
                        captured = bool(notes and notes.causal_check)
                        passed = (not captured if not check else bool(
                            captured and notes.question.text == goal
                            and notes.next_check.text == check
                            and notes.question.evidence_message_ids == (ids[0],)
                            and notes.next_check.evidence_message_ids ==
                            (ids[1 if name == "microphone" else 2],)
                            and notes.causal_check.expects_yes == (0 if name == "microphone" else 1)
                        ))
                        if not passed:
                            failed.append(name)
                        observed = _extract_json_object(client.responses[-1][0]) or {}
                        print(json.dumps({"example": name, "pass": passed, "captured": captured,
                                          "verdict": observed.get("diagnostic_verdict"),
                                          "goal": str(notes.question) if notes else None,
                                          "test": str(notes.next_check) if notes else None,
                                          "finish": client.responses[-1][1]}), flush=True)
                if failed:
                    raise SystemExit(f"Failed diagnostic attribution: {failed}")
                return
            missing = []
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
                        missing.append(case)
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
            if missing:
                raise SystemExit(f"Missing diagnostic observations: {missing}")


if __name__ == "__main__":
    main()
