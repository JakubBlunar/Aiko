from contextlib import closing

from app.core.conversation.shared_retrospective import check_retrospective
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from scripts.longitudinal_reasoning_replay import (
    CHOIR, HIGHLIGHT, NOW, PRINTER, QUESTION, seed_rows,
)


def test_original_user_evidence_is_period_and_owner_scoped(tmp_path):
    db = ChatDatabase(tmp_path / "fiction.db")
    with closing(db._get_conn()), timephrase.scoped_now(lambda: NOW):
        seed_rows(db, "owner", PRINTER * 2 + CHOIR + [HIGHLIGHT, (0, "user", "secret in May"),
                  (5, "assistant", "The biggest thing for you was skydiving.")])
        seed_rows(db, "other", [(6, "user", "My biggest secret.")])
        db.add_message("owner:noremember", "user", "Biggest secret in private.")
        current = db.add_message("owner:now", "user", "Secret current question.")
        result = check_retrospective(QUESTION, db, "owner", "owner:now", current).guidance
        assert '[12, "printer"]' in result
        assert "choir" in result and HIGHLIGHT[2] in result
        assert "secret" not in result.lower() and "skydiving" not in result
        for key in ("owner:noremember", "other:now", "unowned"):
            assert "(none)" in check_retrospective(QUESTION, db, "owner", key, current).guidance
        other = check_retrospective("What stands out in my painting?", None, "", "", None)
        assert not other.guidance
