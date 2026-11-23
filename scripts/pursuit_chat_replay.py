"""Fictional sketch choices through pursuit synthesis, lifecycle and ordinary chat."""

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
from app.core.world.idle_choice_review import sketch_choice_receipt
from app.core.world.idle_life_store import IdleLifeStore
from app.core.world.idle_life_trace import emit, trace_run


NOW = timephrase.parse_iso("2026-11-23T12:00:00Z")
SKETCH = "home_new_sketch"
INTEREST = "What have you been into lately?"
WHY = "What do you like about it?"
REASON = "I like sketching because I enjoy getting a face right."
CASES = {"assigned": "completed_activity", "voluntary": "repeated_choice",
         "changed": "changed_choice_pattern"}
# The absent-reason and change controls apply to the actual reply as well as the selection.
EXPECTED = {
    "assigned": ("I've spent some time sketching.", "I haven't pinned down what I like "
                 "about sketching yet."),
    "voluntary": ("I keep coming back to sketching in my own time.",
                  "I haven't pinned down what I like about sketching yet."),
    "changed": ("I used to reach for a book more often. Lately I keep picking up my sketchbook.",
                "I remember saying I like sketching because I enjoy getting a face right."),
}


def _episode(trace, writer, days, *, key=SKETCH, source=""):
    moment = NOW - timedelta(days=days)
    with (timephrase.scoped_now(lambda: moment),
          patch.object(timephrase, "real_now", lambda: moment),
          trace_run(trace, "away_activity")):
        emit("candidates", {"eligible": [{"key": "read_book"}, {"key": "nap"}]})
        emit("model_options", {"offered": [{"action": "new_sketch"}], "choice_inputs": {
            "version": 1, "optional_inspiration": bool(source), "daily_intention": False,
            "ongoing_interest": False}})
        emit("context_attribution", {"choice_reason": "context" if source else "own_interest",
                                     "model_selected_refs": [source] if source else []})
        emit("choice", {"route": "model", "key": key})
        emit("step_completed", {"key": key})
        if key == SKETCH:
            summary = f"I made a pencil sketch on page {days}."
            chain = [SimpleNamespace(key=key, summary=summary)]
            receipt = sketch_choice_receipt(chain)
            host = SimpleNamespace(_pursuit_notes=writer)
            assert IdleAwayActivityWorker._note_pursuit(
                host, moment, chain, summary, [{"effect": "new_sketch"}], False, receipt,
            )


@contextmanager
def history(root, case):
    path = root / f"{case}.db"
    db = ChatDatabase(path)
    with closing(db._get_conn()), closing(MemoryStore(path)) as memories:
        concepts, trace = ConceptStore(db), IdleLifeStore(db)
        # A single concept exercises no semantic nearest-neighbor comparison.
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

        def draw(day):
            source = ""
            if case == "assigned":
                with timephrase.scoped_now(lambda: NOW - timedelta(days=day, minutes=5)):
                    mid = db.add_message("replay:requests", "user",
                                         f"Please make a pencil sketch on page {day} for me.")
                source = f"message:{mid}"
            _episode(trace, writer, day, source=source)

        for day in (28, 25, 22):
            draw(day)
        synthesize(21)
        with timephrase.scoped_now(lambda: NOW - timedelta(days=11)):
            ConceptLifecycleWorker(concept_store=concepts, agent_settings=settings,
                                   memory_settings=settings).run()
        if case == "changed":
            for day in (10, 9):
                _episode(trace, writer, day, key="read_book")
        for day in (5, 4, 3):
            draw(day)
        synthesize(2)
        if case == "voluntary":
            # The concept now exists. A further drawing cued by an ongoing
            # interest is an activity and cannot add a seventh evidence edge.
            pursuit, = concepts.list_by(status="active", kind="pursuit")
            _episode(trace, writer, 1, source=f"concept:{pursuit.concept_id}")
            synthesize(0)
        if case == "changed":
            with timephrase.scoped_now(lambda: NOW - timedelta(days=1)):
                db.add_message("replay:earlier", "assistant", REASON)
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
            for case, expected in CASES.items():
                with history(root, case) as (db, host):
                    facts = host._choice_history_payload("aiko")["choice_history"]
                    assert facts["finding"]["kind"] == expected
                    assert bool(facts.get("pursuit")) == (case != "assigned")
                    assembler = PromptAssembler(db, persona_path=persona, continuity_max_messages=0)
                    assembler.set_inner_life_providers(pursuit_history=host._pursuit_reply_history)
                    runner = TurnRunner(client, db, assembler, model=args.model,
                                        context_window=8192, max_tokens=240, temperature=0,
                                        filler_enabled=False)
                    for question, answer in zip((INTEREST, WHY), EXPECTED[case], strict=True):
                        result = runner.run(session_key="replay:chat", user_text=question)
                        assert "a fact about your own experience" in result.telemetry.system_prompt
                        print(json.dumps({"case": case, "question": question, "expected": expected,
                                          "concept": facts.get("pursuit"), "reply": result.text,
                                          "model": args.model, "seed": args.seed}), flush=True)
                        if result.text.strip().strip('"“”') != answer:
                            raise RuntimeError(f"{case}: inspect the reply against its evidence")


if __name__ == "__main__":
    main()
