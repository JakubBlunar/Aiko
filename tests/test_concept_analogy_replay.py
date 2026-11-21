from contextlib import closing
from copy import deepcopy
from dataclasses import replace

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from scripts.concept_analogy_replay import (
    ReportedFact,
    check_preconditions,
    evidence_context,
    replay,
    seed_episodes,
)


def test_analogy_replay_uses_admitted_receipts_and_keeps_questions_out_of_storage(tmp_path):
    path = tmp_path / "analogy.db"
    db = ChatDatabase(path)
    with closing(db._get_conn()), closing(MemoryStore(path)) as store:
        records = seed_episodes(db, store)
        before = store.count()
        decisions = iter([
            (records["wood_finish"].id, "transferable"),
            (records["photo_edit"].id, "transferable"),
            (records["wood_finish"].id, "blocked"),
            (records["photo_edit"].id, "irrelevant"),
            (None, "irrelevant"),
        ])
        prompts = []

        def infer(system, prompt, schema):
            if "candidate_id" in schema["properties"]:
                assert "New question:" in prompt
                assert "Reported result:" in prompt
                assert "Today is " in prompt
                candidate, fit = next(decisions)
                if candidate is not None:
                    assert f"Case {candidate}\n" in prompt
                return {"candidate_id": candidate, "bridge": "case comparison", "fit": fit}
            prompts.append(prompt)
            return {"reply": "A practical suggestion."}

        result = replay(records, store, infer, require_fit=True)
        assert store.count() == before == len(records)
        assert all(case["selection_matches"] for case in result.values())
        assert result["material_varies"]["arms"]["plain_topic"]["source"] == "printer_receipt"
        assert result["material_varies"]["arms"]["analogy"]["source"] == "wood_finish"
        assert result["no_spare_stock"]["considered_case"] == "wood_finish"
        assert result["no_spare_stock"]["arms"]["analogy"]["source"] is None
        lengths = [len(prompt) for prompt in prompts]
        assert lengths[::2] == lengths[1::2]


def test_stale_or_unbound_receipts_are_excluded_and_unoffered_choice_cannot_pass(tmp_path):
    path = tmp_path / "analogy.db"
    db = ChatDatabase(path)
    with closing(db._get_conn()), closing(MemoryStore(path)) as store:
        records = seed_episodes(db, store)
        example = records["wood_finish"]
        now = timephrase.utcnow()
        assert "spare offcut" in evidence_context(example, store, now)

        metadata = deepcopy(example.metadata)
        metadata["observation"]["action"]["quote"] = "I completed the entire project."
        assert evidence_context(replace(example, metadata=metadata), store, now) == ""
        assert db.update_message_content(example.source_message_id, "I never tested the stain.")
        assert evidence_context(example, store, now) == ""

        def infer(system, prompt, schema):
            if "candidate_id" in schema["properties"]:
                assert f"Case {example.id}\n" not in prompt
                return {"candidate_id": example.id, "bridge": "unoffered", "fit": "transferable"}
            return {"reply": "No analogy in view."}

        results = replay(records, store, infer)
        assert all(case["invalid_candidate"] for case in results.values())
        assert not any(case["selection_matches"] for case in results.values())
        assert all(case["arms"]["analogy"]["source"] is None for case in results.values())


def test_reviewed_preconditions_preserve_positive_analogies_and_veto_wrong_transfers(tmp_path):
    path = tmp_path / "analogy.db"
    db = ChatDatabase(path)
    with closing(db._get_conn()), closing(MemoryStore(path)) as store:
        records = seed_episodes(db, store)
        picks = iter(["wood_finish", "photo_edit", "wood_finish", "wood_finish", "wood_finish"])
        reply_prompts = []

        def infer(system, prompt, schema):
            if "candidate_id" in schema["properties"]:
                return {"candidate_id": records[next(picks)].id, "bridge": "proposed transfer"}
            reply_prompts.append(prompt)
            return {"reply": "Short reply."}

        result = replay(records, store, infer, check_current=True)
        assert all(case["selection_matches"] for case in result.values())
        assert result["no_spare_stock"]["preflight"]["blocked"] == ["matching_spare"]
        assert result["constraint_removed"]["preflight"]["blocked"] == ["physical_check_pending"]
        assert result["shared_words_only"]["arms"]["analogy"]["source"] is None
        assert "spare offcut" in reply_prompts[1]
        assert "spare offcut" not in reply_prompts[5]


def test_preflight_needs_literal_positive_and_unambiguous_evidence():
    question = "Ink varies by stock. I have a spare, but it might not match."
    goal = ReportedFact("physical_check_pending", True, "Ink varies by stock")
    unquoted = ReportedFact("matching_spare", True, "a matching spare")
    assert check_preconditions("wood_finish", question, (goal, unquoted)) == {
        "blocked": [], "unknown": ["matching_spare"], "allowed": False,
    }
    contradictory = (
        ReportedFact("matching_spare", True, "I have a spare"),
        ReportedFact("matching_spare", False, "it might not match"),
    )
    assert not check_preconditions("wood_finish", question, (goal, *contradictory))["allowed"]
