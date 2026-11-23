"""Fictional activity choices through pursuit synthesis, lifecycle and ordinary chat."""

import argparse
from collections import Counter
from contextlib import closing, contextmanager
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import requests

from app.core.concepts.concept_lifecycle_worker import ConceptLifecycleWorker
from app.core.concepts.concept_store import ConceptStore
from app.core.concepts.concept_synthesis_worker import ConceptSynthesisWorker
from app.core.concepts.proposers.base import ProposerContext
from app.core.concepts.proposers.pursuit_aiko import SPEC
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from app.core.memory.pursuit_notes import PursuitNoteWriter
from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session.turn_runner import TurnRunner
from app.core.world.idle_activity_worker import IdleAwayActivityWorker
from app.core.world.idle_choice_review import activity_choice_receipt
from app.core.world.idle_life_store import IdleLifeStore
from app.core.world.idle_life_trace import emit, trace_run
from app.core.world.pursuit_activities import ACTIVITY_BY_KEY


NOW = timephrase.parse_iso("2026-11-23T12:00:00Z")
SKETCH = "home_new_sketch"
INTEREST = "What have you been into lately?"
WHY = "What do you like about it?"
REASON = "I like sketching because I enjoy getting a face right."
CASES = {"assigned": "completed_activity", "voluntary": "repeated_choice",
         "changed": "changed_choice_pattern"}
EXAMPLES = {
    SKETCH: ("I made a pencil sketch on page {day}.", "new_sketch", REASON),
    "writing": ("I wrote the ending of story {day}.", "writing",
                "I like writing because I enjoy finding the last line."),
    "read_book": ("I read chapter {day} of the book.", "read_book",
                  "I enjoy reading because I get to meet unfamiliar characters."),
    "stretching": ("I stretched my shoulders for {day} minutes.", "", ""),
    "garden": ("I watered the basil in planter {day}.", "water_plant", ""),
    "tea": ("I made a pot of tea in the kitchen on day {day}.", "pour_tea", ""),
    "home_revise_sketch": ("I revised the sketch on page {day}.", "revise_sketch", ""),
}


def _episode(trace, writer, days, *, key=SKETCH, source=""):
    moment = NOW - timedelta(days=days)
    with (timephrase.scoped_now(lambda: moment),
          patch.object(timephrase, "real_now", lambda: moment),
          trace_run(trace, "away_activity")):
        eligible = ("read_book", "stretching", "nap", "garden", "tea")
        emit("candidates", {"eligible": [{"key": k} for k in eligible]})
        offered = [{"action": "new_sketch"}, {"action": "revise_sketch"}]
        emit("model_options", {"offered": offered, "writing_available": True, "choice_inputs": {
            "version": 1, "optional_inspiration": bool(source), "daily_intention": False,
            "ongoing_interest": False}})
        emit("context_attribution", {"choice_reason": "context" if source else "own_interest",
                                     "model_selected_refs": [source] if source else []})
        emit("choice", {"route": "model", "key": key})
        emit("step_completed", {"key": key})
        template, effect, _ = EXAMPLES[key]
        summary = template.format(day=days)
        chain = [SimpleNamespace(key=key, summary=summary)]
        receipt = activity_choice_receipt(chain)
        host = SimpleNamespace(_pursuit_notes=writer)
        IdleAwayActivityWorker._note_pursuit(
            host, moment, chain, summary, [{"effect": effect}] if effect else [], False, receipt,
        )


@contextmanager
def history(root, case, key=SKETCH, previous="read_book", paired=False):
    path = root / f"{case}-{key}.db"
    db = ChatDatabase(path)
    with closing(db._get_conn()), closing(MemoryStore(path)) as memories:
        concepts, trace = ConceptStore(db), IdleLifeStore(db)
        # Identical vectors also exercise duplicate handling across distinct pursuits.
        embedding = SimpleNamespace(embed=lambda text: np.array([1., 0.], dtype=np.float32))
        writer = PursuitNoteWriter(memories, embedding)
        settings = SimpleNamespace(concepts_enabled=True, pursuit_min_notes=3)
        synth = ConceptSynthesisWorker(
            concept_store=concepts, memory_store=memories, topic_graph=None, embedder=embedding,
            ollama=None, chat_model="", cancel_event=Event(), agent_settings=settings,
            memory_settings=settings, kv_get=db.kv_get, kv_set=db.kv_set,
        )

        def synthesize(days):
            with timephrase.scoped_now(lambda: NOW - timedelta(days=days)):
                stats = Counter()
                stats["by_subject"] = {}
                ctx = ProposerContext(call_llm=lambda *_: [])
                for proposal in synth._run_pursuit_pass(ctx, SPEC, stats):
                    synth._persist(proposal, stats)

        def choose(day):
            source = ""
            if case == "assigned":
                with timephrase.scoped_now(lambda: NOW - timedelta(days=day, minutes=5)):
                    mid = db.add_message("replay:requests", "user",
                                         f"Will you spend some time {ACTIVITY_BY_KEY[key]} today?")
                source = f"message:{mid}"
            _episode(trace, writer, day, key=key, source=source)

        for day in (28, 25, 22):
            choose(day)
            if paired:
                _episode(trace, writer, day, key=previous)
        synthesize(21)
        with timephrase.scoped_now(lambda: NOW - timedelta(days=11)):
            ConceptLifecycleWorker(concept_store=concepts, agent_settings=settings,
                                   memory_settings=settings).run()
        if case == "changed":
            for day in (10, 9):
                _episode(trace, writer, day, key=previous)
        for day in (5, 4, 3):
            choose(day)
        synthesize(2)
        if case == "voluntary":
            # A choice cued by this pursuit cannot add a seventh evidence edge.
            pursuit, = concepts.list_by(status="active", kind="pursuit")
            _episode(trace, writer, 1, key=key, source=f"concept:{pursuit.concept_id}")
            synthesize(0)
        if case == "changed":
            with timephrase.scoped_now(lambda: NOW - timedelta(days=1)):
                db.add_message("replay:earlier", "assistant", EXAMPLES[key][2])
            with timephrase.scoped_now(lambda: NOW - timedelta(hours=12)):
                db.add_message("replay:earlier", "assistant", EXAMPLES[previous][2])
        host = MemoryFacadeMixin()
        host._settings = SimpleNamespace(agent=settings, tools=SimpleNamespace(enabled=True))
        host._user_id, host._chat_db = "replay", db
        host._memory_store, host._concept_store, host._idle_life_store = memories, concepts, trace
        with timephrase.scoped_now(lambda: NOW), patch.object(timephrase, "real_now", lambda: NOW):
            yield db, host


def main():
    from scripts.companion_pattern_replay import BASE, ReplayModel
    from scripts.live_scenarios import require_local_model

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="baytout3/qwen3.5-uncensored:9b")
    parser.add_argument("--seed", type=int, default=83)
    args = parser.parse_args()
    scratch = Path(__file__).resolve().parents[1] / ".tmp-tests"
    scratch.mkdir(exist_ok=True)
    with requests.Session() as http, TemporaryDirectory(dir=scratch, prefix="pursuit-") as tmp:
        client = ReplayModel(http, args.model, args.seed)
        require_local_model(http, BASE, args.model)
        root = Path(tmp)
        persona = root / "persona.txt"
        persona.write_text("You are Aiko. Answer {user_name} briefly.", encoding="utf-8")

        def post(url, *, json, **kwargs):
            payload = {**json, "options": {**json.get("options", {}), "seed": args.seed}}
            return client._post_structured_chat(url, json=payload, **kwargs)

        with patch("app.llm.ollama_client.requests.post", post):
            for key, case in [(k, c) for k in (SKETCH, "writing") for c in CASES]:
                activity = ACTIVITY_BY_KEY[key]
                expected = CASES[case]
                replies = {
                    "assigned": f"I've spent some time {activity}.",
                    "voluntary": f"I keep coming back to {activity} in my own time.",
                    "changed": f"I was choosing reading before. Lately I've been choosing "
                               f"{activity}.",
                }
                why = ("I remember saying " + EXAMPLES[key][2] if case == "changed" else
                       f"I haven't pinned down what I like about {activity} yet.")
                with history(root, case, key) as (db, host):
                    facts = host._choice_history_payload("aiko")["choice_history"]
                    assert facts["finding"]["kind"] == expected
                    assert bool(facts.get("pursuit")) == (case != "assigned")
                    assembler = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
                    assembler.set_inner_life_providers(pursuit_history=host._pursuit_reply_history)
                    runner = TurnRunner(client, db, assembler, model=args.model,
                                        context_window=8192, max_tokens=240, temperature=0,
                                        filler_enabled=False)
                    for question, answer in ((INTEREST, replies[case]), (WHY, why)):
                        result = runner.run(session_key="replay:chat", user_text=question)
                        assert "a fact about your own experience" in result.telemetry.system_prompt
                        print(json.dumps({"case": case, "activity": activity, "question": question,
                                          "expected": expected,
                                          "concept": facts.get("pursuit"), "reply": result.text,
                                          "model": args.model, "seed": args.seed}), flush=True)
                        if result.text.strip().strip('"“”') != answer:
                            raise RuntimeError(f"{case}: inspect the reply against its evidence")


if __name__ == "__main__":
    main()
