from types import SimpleNamespace

from app.core.infra.chat_database import ChatDatabase
from app.core.live.timing_preference import defer_casual_while_coding, observe_timing_instruction


TEACH = "Remember to save casual topics for breaks while I’m coding."
FORGET = "Forget my coding-time preference."


def coding_frame(*, stale=False):
    return SimpleNamespace(
        shared=SimpleNamespace(
            os_idle="active",
            inferred=SimpleNamespace(label="user_coding", confidence=0.7),
        ),
        constraints=SimpleNamespace(stale_sources=("c6",) if stale else ()),
    )


def test_timing_uses_current_user_evidence_from_the_isolated_database(tmp_path):
    path = tmp_path / "chat.db"
    db = ChatDatabase(path)
    unsolicited = db.add_message("first", "assistant", TEACH)
    quoted = db.add_message("first", "user", f"Example phrase: {TEACH}")
    for message_id in (unsolicited, quoted):
        observe_timing_instruction(db, "owner", db.get_message_row(message_id))
    assert not defer_casual_while_coding(db, "owner", coding_frame())

    taught = db.add_message("first", "user", TEACH)
    observe_timing_instruction(db, "owner", db.get_message_row(taught))
    db._get_conn().close()
    reopened = ChatDatabase(path)
    try:
        assert defer_casual_while_coding(reopened, "owner", coding_frame())
        assert not defer_casual_while_coding(reopened, "other-owner", coding_frame())
        assert not defer_casual_while_coding(reopened, "owner", coding_frame(stale=True))
        assert reopened.delete_message_rows("first", [taught]) == [taught]
        assert not defer_casual_while_coding(reopened, "owner", coding_frame())
    finally:
        reopened._get_conn().close()


def test_forget_wins_over_delayed_capture_of_the_older_instruction(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    try:
        taught = db.add_message("first", "user", TEACH)
        forgot = db.add_message("second", "user", FORGET)
        observe_timing_instruction(db, "owner", db.get_message_row(forgot))
        observe_timing_instruction(db, "owner", db.get_message_row(taught))
        assert not defer_casual_while_coding(db, "owner", coding_frame())
    finally:
        db._get_conn().close()
