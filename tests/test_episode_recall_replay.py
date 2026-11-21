"""The offline replay preserves a qualified and revision-bound episode."""

from contextlib import closing
from dataclasses import replace

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore
from scripts.episode_recall_replay import replay, seed_episodes, select_episode


def test_replay_preserves_partial_results_and_current_correction(tmp_path):
    path = tmp_path / "episode-recall.db"
    db = ChatDatabase(path)
    with closing(db._get_conn()), closing(MemoryStore(path)) as store:
        records = seed_episodes(db, store)
        calls = []

        def infer(system, prompt, schema):
            calls.append(prompt)
            assert "Today is " in prompt
            return {"reply": "Synthetic reply."}

        changes = db._get_conn().total_changes
        cases = replay(records, store, infer)
        for case in cases.values():
            assert case["arms"]["episode"]["memory_id"] == case["expected_id"]
        assert cases["partial_repair"]["arms"]["ranked"]["memory_id"] == records["desk"].id
        assert "tightened the mounting screws" in calls[1]
        assert "still hums on high" in calls[1]
        assert "still clicks" in calls[5]
        assert "That fixed the clicking" not in calls[5]
        assert len(cases["corrected_outcome"]["source_message_ids"]) == 2
        assert cases["ambiguous_object"]["selection_status"] == "ambiguous"
        assert "Several recorded episodes match" in calls[8]
        assert len(calls[6]) == len(calls[7]) == len(calls[8])
        for turn in (0, 2, 4, 9):
            assert len(calls[turn]) == len(calls[turn + 1])
        assert db._get_conn().total_changes == changes

        corrected = cases["corrected_outcome"]["source_message_ids"][-1]
        assert db.update_message_content(corrected, "I deleted my report about the purifier.")
        assert select_episode("Did the bedroom air purifier stop?", records.values(), store,
                              timephrase.utcnow()) == (None, "source_unavailable")


def test_episode_replay_rejects_foreign_virtual_ambiguous_and_changed_sources(tmp_path):
    path = tmp_path / "episode-scope.db"
    db = ChatDatabase(path)
    with closing(db._get_conn()), closing(MemoryStore(path)) as store:
        records = seed_episodes(db, store)
        now = timephrase.utcnow()
        extractor = records["extractor"]
        query = "What happened with the kitchen extractor fan?"
        virtual = replace(extractor, metadata={**extractor.metadata, "memory_origin": {
            **extractor.metadata["memory_origin"], "domain": "virtual",
        }})
        foreign = replace(extractor, source_session="someone-else:extractor")
        assert select_episode(query, [virtual, foreign], store, now) == (None, "no_match")
        assert select_episode("What did I do when the fan rattled?", records.values(),
                              store, now) == (None, "ambiguous")
        copies = [extractor, replace(extractor, id=999)]
        selected, status = select_episode(query, copies, store, now)
        assert status == "matched" and selected.memory_id == extractor.id
        assert db.update_message_content(extractor.source_message_id, "I removed that report.")
        assert select_episode(query, records.values(), store, now) == (None, "source_unavailable")
