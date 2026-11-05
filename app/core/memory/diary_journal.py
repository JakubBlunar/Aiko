"""Journal authorship and append-only, intentional diary writes."""
from __future__ import annotations

import hashlib

from app.core.infra import timephrase


def journal_metadata(source: str, *, revisits_entry_id: int | None = None) -> dict:
    chosen = source in {"tool", "inline_tag"}
    return {
        "journal": {"version": 1, "authorship": "chosen" if chosen else "background",
                    "source": source, "revisits_entry_id": revisits_entry_id},
        "memory_origin": {"version": 1, "subject": "aiko", "mode": "reflection",
                          "writer": "diary_" + source, "domain": "conversation"},
    }


def journal_authorship(metadata: dict) -> str:
    journal = metadata.get("journal") or {}
    if isinstance(journal, dict) and journal.get("version") == 1:
        authorship = journal.get("authorship")
        if isinstance(authorship, str) and authorship in {"chosen", "background"}:
            return authorship
    origin = metadata.get("memory_origin") or {}
    writer = origin.get("writer") if isinstance(origin, dict) else None
    if isinstance(writer, str) and writer in {
        "diary_worker", "post_sleep_diary_worker",
    }:
        return "background"
    return "unspecified"


def append_chosen_entry(store, embed, content, *, session_key, revisits_entry_id=None):
    """Return (saved row, created). Repeating the same dated write is idempotent."""
    if not isinstance(content, str) or not 16 <= len(content.strip()) <= 2000:
        raise ValueError("content must be 16-2000 characters of first-person journal prose")
    content = content.strip()
    if "[[" in content or "]]" in content:
        raise ValueError("write plain prose, without inline control tags")
    if timephrase.has_relative_deictic(content):
        raise ValueError("use concrete dates instead of relative time such as today or tomorrow; "
                         f"the local date is {timephrase.now().date().isoformat()}")
    now = timephrase.utcnow().isoformat()
    key = hashlib.sha256((session_key + "\n" + timephrase.now().date().isoformat()
                          + "\n" + content + "\n" + str(revisits_entry_id)).encode()).hexdigest()
    admission_key = "diary_tool:" + key

    def existing():
        return next((row for row in store.iter_by_kinds(["diary"])
                     if row.metadata.get("admission_key") == admission_key), None)

    saved = existing()
    if saved is not None:
        return saved, False
    metadata = journal_metadata("tool", revisits_entry_id=revisits_entry_id)
    metadata["admission_key"] = admission_key
    saved = store.add(
        content=content, kind="diary", embedding=embed(content),
        salience=0.6, source_session=session_key, metadata=metadata,
        tier="long_term", temporal_type="past_event", event_time=now, skip_dedupe=True,
    )
    if saved is None:
        saved = existing()
        if saved is not None:
            return saved, False
        raise RuntimeError("the diary entry was not saved")
    return saved, True


def diary_awareness(tool_names) -> str:
    if "read_diary" not in tool_names:
        return ""
    return (
        "Your saved diary exists across conversations and is readable by the user. "
        "It can contain chosen entries, background reflections, dreams and research notes, "
        "with dates and source details. Entries are personal perspectives from their "
        "recorded time, not proof of facts about the user or of a shared physical event. "
        "Earlier entries can coexist with a changed perspective. Keeping a diary is optional."
    )
