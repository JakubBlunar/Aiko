"""Small original writings with immutable, inspectable saved revisions.

These are creative texts, never observations, research findings or user facts.
The item is the durable output; the away journal only points to its revision.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
from typing import Any
from uuid import uuid4

MAX_REVISIONS = 8
DAILY_WRITES = 3
TEXT_LIMIT = 1400
FORMS = frozenset({"poem", "short_scene", "character_sketch", "wordplay"})


@dataclass(frozen=True)
class CreationDraft:
    title: str
    form: str
    text: str
    item_id: int | None = None
    base_revision: int = 0
    expected_updated_at: str | None = None


def revisions(item: Any) -> list[dict[str, Any]]:
    state = getattr(item, "state", None) or {}
    if state.get("creation_schema") != 1:
        return []
    rows = state.get("revisions")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def writes_on_day(items: list[Any], now: datetime) -> int:
    day = now.astimezone().date()
    total = 0
    for item in items:
        for row in revisions(item):
            try:
                at = datetime.fromisoformat(row["at"])
                if at.tzinfo is not None and at.astimezone().date() == day:
                    total += 1
            except (KeyError, TypeError, ValueError):
                continue
    return total


def parse_draft(raw: Any, visible_items: list[Any]) -> CreationDraft | None:
    if not isinstance(raw, dict):
        return None
    title, form, text = raw.get("title"), raw.get("form"), raw.get("text")
    if not all(isinstance(value, str) for value in (title, form, text)):
        return None
    title, form, text = title.strip(), form.strip(), text.strip()
    if (not 2 <= len(title) <= 100 or form not in FORMS or not 40 <= len(text) <= TEXT_LIMIT
            or "[[" in title + text or "<|" in title + text):
        return None
    item_id, base = raw.get("item_id"), raw.get("base_revision", 0)
    if item_id is None:
        if type(base) is not int or base != 0:
            return None
        return CreationDraft(title, form, text)
    if type(item_id) is not int or type(base) is not int:
        return None
    item = next((i for i in visible_items if i.id == item_id), None)
    rows = revisions(item)
    if not rows or len(rows) >= MAX_REVISIONS or base != len(rows):
        return None
    return CreationDraft(title, form, text, item_id, base, item.updated_at)


def save_draft(store: Any, draft: CreationDraft, *, location_id: int | None,
               now: datetime) -> dict[str, Any] | None:
    items = store.list_items()
    if parse_draft(asdict(draft), items) is None:
        return None
    if draft.item_id is not None and draft.expected_updated_at is None:
        return None
    if writes_on_day(items, now) >= DAILY_WRITES:
        return None
    fingerprint = " ".join(draft.text.split()).casefold()
    if any(" ".join(str(row.get("text") or "").split()).casefold() == fingerprint
           for item in items if item.id != draft.item_id for row in revisions(item)):
        return None
    if draft.item_id is not None:
        item = store.get_item(draft.item_id)
        existing = revisions(item)
        if not existing:
            return None
        latest = existing[-1]
        if (" ".join(str(latest.get("text") or "").split()).casefold() == fingerprint
                and latest.get("title") == draft.title and latest.get("form") == draft.form):
            return None
    row = {"revision": draft.base_revision + 1, "at": now.isoformat(),
           "title": draft.title, "form": draft.form, "text": draft.text,
           "text_sha256": hashlib.sha256(draft.text.encode()).hexdigest(),
           "mode": "creative_fiction", "author": "aiko"}
    if draft.item_id is None:
        result = store.add_item(
            name=draft.title, slug="aiko_writing_" + uuid4().hex,
            kind="other", location_id=location_id,
            description=f"Aiko's original {draft.form.replace('_', ' ')}; saved writing.",
            state={"creation_schema": 1, "revisions": [row]},
        )
        if result is None:
            return None
        item = result[0]
    else:
        item = store.get_item(draft.item_id)
        prior = revisions(item)
        if len(prior) != draft.base_revision or not prior:
            return None
        item = store.update_item(
            item.id, name=draft.title,
            description=f"Aiko's original {draft.form.replace('_', ' ')}; saved writing.",
            state={**item.state, "revisions": [*prior, row]},
            expected_updated_at=draft.expected_updated_at,
        )
        if item is None:
            return None
    return {"effect": "save_creation", "item_id": item.id, "item": item.name,
            "revision": row["revision"], "text_sha256": row["text_sha256"],
            "mode": "creative_fiction", "at": row["at"]}


def prompt_context(items: list[Any]) -> str:
    works = [(item, revisions(item)) for item in items if revisions(item)]
    works.sort(key=lambda pair: pair[1][-1].get("at", ""), reverse=True)
    lines = []
    for item, rows in works[:4]:
        latest = rows[-1]
        lines.append(
            f"item_id={item.id}; title={latest.get('title')!r}; form={latest.get('form')}; "
            f"revision={len(rows)}/{MAX_REVISIONS}; saved_at={latest.get('at')}; "
            f"creative text (not life evidence): {str(latest.get('text') or '')[:800]}"
        )
    return "\n".join(lines) or "No saved original writings here yet."


def inspect_revision(item: Any, revision: Any = None, offset: Any = 0) -> dict[str, Any]:
    rows = revisions(item)
    number = len(rows) if revision is None else revision
    if type(number) is not int or not 1 <= number <= len(rows):
        raise ValueError(f"revision must be an integer 1-{len(rows)}")
    row = rows[number - 1]
    text = str(row.get("text") or "")
    if type(offset) is not int or not 0 <= offset <= len(text):
        raise ValueError("offset must be a valid character position")
    return {**row, "text": text[offset:offset + 1000], "offset": offset,
            "next_offset": offset + 1000 if offset + 1000 < len(text) else None,
            "total_characters": len(text), "revision_count": len(rows),
            "source_status": (
                "saved_text_matches" if row.get("text_sha256") == hashlib.sha256(
                    text.encode()).hexdigest() else "text_changed_or_untracked"
            ),
            "interpretation": "Original creative fiction, not an autobiographical claim."}
