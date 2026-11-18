from datetime import datetime, timezone

from app.core.conversation.exchange_recall import exchange_evidence
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.llm.token_utils import estimate_tokens


def test_correction_recalls_original_morning_exchange_across_sessions(tmp_path, monkeypatch):
    db = ChatDatabase(tmp_path / "exchange.db")
    now = datetime(2026, 11, 17, 19, 51).astimezone()
    monkeypatch.setattr(timephrase, "now", lambda: now)

    def add(session, role, content, hour, minute):
        sent_at = datetime(2026, 11, 12, hour, minute).astimezone()
        return db.execute_commit(
            "INSERT INTO messages (session_id, role, content, token_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session, role, content, 0, sent_at.astimezone(timezone.utc).isoformat()),
        )

    add("owner:morning", "user", "I have survived! Finally out.", 9, 29)
    add("owner:morning", "assistant", "You caught me mid-nap.", 9, 30)
    add("owner:morning", "user", "Sorry, I had to wake you up.", 9, 32)
    add("owner:morning", "user", "I wanted to say how the dentist went.", 9, 37)
    add("owner:morning", "user", "Managed to survive successfully.", 9, 53)
    add("owner:night", "user", "I worked late after the dentist.", 23, 50)
    add("other:morning", "user", "Private dentist details. I survived it.", 9, 28)
    correction = ("I am free today. Dentist was in Thursday, you forgot I even woke you up "
                  "to tell you how I managed to survive it.")
    db.add_message("owner:current", "user", correction)

    text, ids = exchange_evidence(
        db, correction, "owner:current", "User", budget=450, estimate_tokens=estimate_tokens,
    )
    assert ids
    assert "2026-11-12 09:29" in text and "I have survived!" in text
    assert "2026-11-12 09:32" in text and "wake you up" in text
    assert "Private dentist details" not in text
    assert correction not in text
    assert exchange_evidence(
        db, "I forgot to book the dentist", "owner:current", "User",
        budget=450, estimate_tokens=estimate_tokens,
    ) == ("", [])
