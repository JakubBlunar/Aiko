from contextlib import closing
from copy import deepcopy
from dataclasses import replace

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from scripts.concept_analogy_replay import evidence_context, replay, seed_episodes


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
