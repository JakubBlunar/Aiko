from contextlib import closing

import pytest

from app.core.infra.chat_database import ChatDatabase
from app.core.world import idle_creations, writing_practice
from app.core.world.world_store import WorldStore
from scripts.writing_practice_replay import (
    EXPLAIN, FRESH, IMAGE, LATER, REVIEW, REVISION, seed_work,
)


@pytest.mark.parametrize("later", ["", LATER, LATER + EXPLAIN])
def test_practice_reads_original_revised_and_separate_endings(tmp_path, later):
    path = tmp_path / "work.db"
    db = ChatDatabase(path)
    with closing(db._get_conn()), closing(WorldStore(path)) as store:
        assert "don't have" in writing_practice.practice_answer(REVIEW, store.list_items).reply
        receipt = seed_work(store, later)
        answer = writing_practice.practice_answer(REVIEW, store.list_items).reply
        assert REVISION.split(". ")[1] in answer and "absence" in answer
        if later:
            assert IMAGE in answer
            assert ("kept writing after" in answer) == bool(EXPLAIN in later)
            item = store.get_item(receipt["item_id"])
            row = idle_creations.revisions(item)[0]
            for change in ({"author": "user"}, {"text": "User supplied replacement ending."}):
                store.update_item(item.id, state={**item.state, "revisions": [{**row, **change}]})
                assert IMAGE not in writing_practice.practice_answer(REVIEW, store.list_items).reply
        else:
            assert "don't have a separate" in answer
        assert writing_practice.practice_answer(FRESH, store.list_items).guidance
        assert not writing_practice.practice_answer(FRESH + " Explain the ending.",
                                                    store.list_items).guidance
        assert not writing_practice.practice_answer("Hello!", None).guidance
