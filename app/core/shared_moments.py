"""Shared-moments store.

Thin wrapper around :class:`MemoryStore` that adds kind-aware CRUD with
structured ``(when, what, vibe)`` metadata. Every row is stored as a
``shared_moment`` memory; this module is just an ergonomic facade so the
REST layer, anniversary provider, and "Together" UI tab don't have to
shuffle JSON metadata in and out of the generic memory API.

Persistence shape on the ``memories`` row:

    kind     = "shared_moment"
    content  = "Shared moment (<vibe>): <summary>"
    metadata = {
        "vibe": str,
        "what": str,            # short summary, same as content body
        "when": str,            # ISO8601 — when the moment happened
        "source_message_ids": [int, ...]?,
        "source": "tag" | "llm" | "manual",
        "confidence": float,
        "last_anniversaried_at": str?,  # set by the anniversary provider
    }
    pinned   = 1 when source == "manual" (user click on a chat message)

The class is intentionally stateless aside from the underlying
:class:`MemoryStore`; callers hold the only reference.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable

from app.core.shared_moment_extractor import (
    SharedMomentCandidate,
    normalise_vibe,
)

if TYPE_CHECKING:
    from app.core.memory_store import Memory, MemoryStore
    from app.llm.embedder import Embedder


log = logging.getLogger("app.shared_moments")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class SharedMomentRow:
    """Typed view of a ``shared_moment`` memory row."""

    id: int
    summary: str
    vibe: str
    when: str  # ISO8601
    created_at: str
    salience: float
    pinned: bool
    source: str
    confidence: float
    source_message_ids: list[int]
    last_anniversaried_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "vibe": self.vibe,
            "when": self.when,
            "created_at": self.created_at,
            "salience": float(self.salience),
            "pinned": bool(self.pinned),
            "source": self.source,
            "confidence": float(self.confidence),
            "source_message_ids": list(self.source_message_ids),
            "last_anniversaried_at": self.last_anniversaried_at,
        }


def _row_from_memory(mem: "Memory") -> SharedMomentRow:
    meta = mem.metadata or {}
    when = str(meta.get("when") or mem.created_at)
    source_ids = meta.get("source_message_ids") or []
    if not isinstance(source_ids, list):
        source_ids = []
    return SharedMomentRow(
        id=int(mem.id),
        summary=str(meta.get("what") or _strip_summary_prefix(mem.content)),
        vibe=normalise_vibe(meta.get("vibe")),
        when=when,
        created_at=mem.created_at,
        salience=float(mem.salience),
        pinned=bool(mem.pinned),
        source=str(meta.get("source") or "manual"),
        confidence=float(meta.get("confidence", 0.6)),
        source_message_ids=[int(i) for i in source_ids if isinstance(i, (int, float))],
        last_anniversaried_at=meta.get("last_anniversaried_at"),
    )


_CONTENT_PREFIX = "Shared moment ("


def _strip_summary_prefix(content: str) -> str:
    """Best-effort recovery of the summary when ``metadata.what`` is missing.

    Manually-created rows or migrated rows without ``what`` fall back to
    parsing the content. Robust to the closing ``)`` and colon.
    """
    text = str(content or "")
    if not text.startswith(_CONTENT_PREFIX):
        return text
    closing = text.find(")", len(_CONTENT_PREFIX))
    if closing < 0:
        return text
    rest = text[closing + 1 :].lstrip(": ").strip()
    return rest or text


def _format_content(summary: str, vibe: str) -> str:
    return f"{_CONTENT_PREFIX}{vibe}): {summary.strip()}"


class SharedMomentsStore:
    """Kind-aware CRUD for ``shared_moment`` rows."""

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        embedder: "Embedder | None",
    ) -> None:
        self._memory_store = memory_store
        self._embedder = embedder

    # ── writes ──

    def add(
        self,
        *,
        summary: str,
        vibe: str,
        when: str | None = None,
        source: str = "manual",
        confidence: float = 0.7,
        source_message_ids: Iterable[int] | None = None,
        source_session: str | None = None,
        source_message_id: int | None = None,
        salience: float | None = None,
        pinned: bool | None = None,
    ) -> SharedMomentRow | None:
        """Persist a new shared moment. Returns the typed row or ``None``."""
        cleaned_summary = (summary or "").strip()
        if len(cleaned_summary) < 4:
            return None
        normalised_vibe = normalise_vibe(vibe)
        content = _format_content(cleaned_summary, normalised_vibe)
        when_iso = str(when).strip() if when else _now_iso()
        # Validate ISO; fall back to "now" if anything weird arrives.
        try:
            datetime.fromisoformat(when_iso.replace("Z", "+00:00"))
        except ValueError:
            log.debug("invalid 'when' iso for shared moment, falling back to now")
            when_iso = _now_iso()

        embedder = self._embedder
        if embedder is None:
            log.debug("no embedder available; cannot persist shared moment")
            return None
        try:
            emb = embedder.embed(content)
        except Exception:
            log.debug("shared moment embed failed", exc_info=True)
            return None

        # Salience defaults vary by source. Manual + tag are user/persona
        # curated and start higher; LLM-detected moments start moderate so
        # they can decay if they don't get used.
        if salience is None:
            if source == "manual":
                salience = 0.85
            elif source == "tag":
                salience = 0.75
            elif normalised_vibe in {"milestone", "gift", "vulnerable"}:
                salience = 0.7
            else:
                salience = 0.6
        if pinned is None:
            pinned = source == "manual"

        meta: dict[str, Any] = {
            "vibe": normalised_vibe,
            "what": cleaned_summary,
            "when": when_iso,
            "source": source,
            "confidence": float(confidence),
        }
        ids_list = [int(i) for i in (source_message_ids or []) if i is not None]
        if ids_list:
            meta["source_message_ids"] = ids_list

        mem = self._memory_store.add(
            content=content,
            kind="shared_moment",
            embedding=emb,
            salience=float(salience),
            source_session=source_session,
            source_message_id=source_message_id,
            metadata=meta,
            pinned=bool(pinned),
            # Manual/tag writes intentionally bypass dedupe so a near-similar
            # earlier moment doesn't silently absorb the new one. The LLM
            # detector still goes through normal dedupe because it can fire
            # multiple times in a single warm conversation.
            skip_dedupe=source in {"manual", "tag"},
        )
        if mem is None:
            return None
        return _row_from_memory(mem)

    def add_from_candidate(
        self,
        candidate: SharedMomentCandidate,
        *,
        source_session: str | None = None,
        source_message_id: int | None = None,
    ) -> SharedMomentRow | None:
        return self.add(
            summary=candidate.summary,
            vibe=candidate.vibe,
            when=candidate.when,
            source=candidate.source,
            confidence=candidate.confidence,
            source_message_ids=candidate.source_message_ids,
            source_session=source_session,
            source_message_id=source_message_id,
        )

    def update(
        self,
        moment_id: int,
        *,
        summary: str | None = None,
        vibe: str | None = None,
        when: str | None = None,
        pinned: bool | None = None,
        salience: float | None = None,
    ) -> SharedMomentRow | None:
        existing = self._memory_store.get(int(moment_id))
        if existing is None or existing.kind != "shared_moment":
            return None
        meta = dict(existing.metadata or {})
        new_summary = summary.strip() if summary is not None else str(meta.get("what") or _strip_summary_prefix(existing.content))
        if len(new_summary) < 4:
            return None
        new_vibe = normalise_vibe(vibe) if vibe is not None else normalise_vibe(meta.get("vibe"))
        if when is not None:
            try:
                datetime.fromisoformat(str(when).replace("Z", "+00:00"))
                meta["when"] = str(when)
            except ValueError:
                pass
        meta["what"] = new_summary
        meta["vibe"] = new_vibe
        new_content = _format_content(new_summary, new_vibe)
        embedding = None
        if summary is not None and self._embedder is not None:
            try:
                embedding = self._embedder.embed(new_content)
            except Exception:
                log.debug("shared moment re-embed failed", exc_info=True)
        updated = self._memory_store.update(
            int(moment_id),
            content=new_content,
            salience=salience,
            embedding=embedding,
            metadata=meta,
        )
        if updated is None:
            return None
        if pinned is not None:
            self._memory_store.set_pinned(int(moment_id), bool(pinned))
            refreshed = self._memory_store.get(int(moment_id))
            if refreshed is not None:
                updated = refreshed
        return _row_from_memory(updated)

    def stamp_anniversary(self, moment_id: int) -> None:
        """Record that the anniversary block surfaced this moment now."""
        existing = self._memory_store.get(int(moment_id))
        if existing is None or existing.kind != "shared_moment":
            return
        self._memory_store.update(
            int(moment_id),
            metadata={"last_anniversaried_at": _now_iso()},
            metadata_merge=True,
        )

    def delete(self, moment_id: int) -> bool:
        existing = self._memory_store.get(int(moment_id))
        if existing is None or existing.kind != "shared_moment":
            return False
        return self._memory_store.delete(int(moment_id))

    # ── reads ──

    def get(self, moment_id: int) -> SharedMomentRow | None:
        mem = self._memory_store.get(int(moment_id))
        if mem is None or mem.kind != "shared_moment":
            return None
        return _row_from_memory(mem)

    def list(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
        vibe: str | None = None,
    ) -> tuple[list[SharedMomentRow], int]:
        """Paginated list ordered by ``when`` descending. Returns (rows, total)."""
        rows = [
            _row_from_memory(m)
            for m in self._memory_store.iter_by_kind("shared_moment")
        ]
        if vibe:
            vibe_norm = normalise_vibe(vibe)
            rows = [r for r in rows if r.vibe == vibe_norm]
        total = len(rows)
        # Newest first. Tie-break on id so the order is stable.
        rows.sort(key=lambda r: (r.when, r.id), reverse=True)
        start = max(0, int(offset))
        stop = start + max(1, int(limit))
        return rows[start:stop], total

    def count(self) -> int:
        return self._memory_store.count_memories(kind="shared_moment")

    def iter_all(self) -> list[SharedMomentRow]:
        return [
            _row_from_memory(m)
            for m in self._memory_store.iter_by_kind("shared_moment")
        ]
