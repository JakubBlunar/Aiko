"""K85b — durable notes about what Aiko did in her own time.

Her inner life produced plenty of material and then threw all of it
away. The away-activities journal is an eight-entry ring; the hobby is a
single ``kv_meta`` blob that ``_rotate_hobby`` overwrites with
``progress=0`` the moment a thread finishes; the idle-seed ring holds
six. Nothing she did on her own outlived the week, so nothing could be
mined into a lasting sense of what she is *into* -- which is the whole
reason the ``taste`` concept kind has two rows in it and the K81 lean
block almost never fires.

A ``pursuit_note`` is one line of that material, kept. It is written
when something happened worth having an angle on:

  * a **hobby milestone** or a **wrap-up** -- the two moments the hobby
    worker already stops to think about, and the two the rotation used
    to discard, and
  * a **substantive away beat** -- one that changed her room, ran as a
    multi-beat episode, or closed the day's intention. Not "looked out
    the window": a beat that left a trace is a beat there is something
    to say about.

Notes land on the ``long_term`` tier because the point is outliving the
ring they came from, and go through normal dedupe: watering the same
lettuce every day should read as one recurring thread, not thirty rows.

This module is the single write path so the metadata shape stays
uniform for the ``pursuit`` proposer that reads it (K85c).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.memory.memory_store import MemoryStore


log = logging.getLogger("app.pursuit_notes")


PURSUIT_NOTE_KIND = "pursuit_note"

# Sources, so the proposer can weight a milestone above a beat without
# re-parsing the text.
SOURCE_HOBBY_MILESTONE = "hobby_milestone"
SOURCE_HOBBY_WRAPUP = "hobby_wrapup"
SOURCE_AWAY_BEAT = "away_beat"

_MIN_CHARS = 12
_MAX_CHARS = 400


class _Embedder(Protocol):
    def embed(self, text: str) -> Any:
        ...


class PursuitNoteWriter:
    """The one write path for ``pursuit_note`` rows.

    Handed to the producers as a single optional collaborator rather
    than a store plus an embedder, so a worker that has no memory layer
    wired simply doesn't get one and never has to check two things.
    """

    def __init__(
        self,
        memory_store: "MemoryStore",
        embedder: _Embedder,
        *,
        salience: float = 0.55,
    ) -> None:
        self._store = memory_store
        self._embedder = embedder
        self._salience = max(0.0, min(1.0, float(salience)))

    def write(
        self,
        content: str,
        *,
        source: str,
        topic: str = "",
        at: datetime | None = None,
        extra: dict[str, Any] | None = None,
    ) -> int | None:
        """Write one note. Returns its id, or ``None`` if it didn't land.

        ``None`` covers both failure and dedupe-into-an-existing-row,
        which callers treat the same way: there is nothing new to do.
        """
        text = (content or "").strip()
        if len(text) < _MIN_CHARS:
            return None
        text = text[:_MAX_CHARS]
        stamp = (at or timephrase.utcnow()).isoformat(timespec="seconds")
        metadata: dict[str, Any] = {
            "source": str(source or "")[:40],
            "noted_at": stamp,
        }
        if topic:
            metadata["topic"] = str(topic)[:120]
        if extra:
            metadata.update(extra)
        try:
            embedding = self._embedder.embed(text)
        except Exception:
            log.debug("pursuit note embed failed", exc_info=True)
            return None
        try:
            mem = self._store.add(
                content=text,
                kind=PURSUIT_NOTE_KIND,
                embedding=embedding,
                salience=self._salience,
                tier="long_term",
                metadata=metadata,
                provenance="inferred",
            )
        except Exception:
            log.debug("pursuit note write failed", exc_info=True)
            return None
        if mem is None:
            log.debug("pursuit note deduped: source=%s", source)
            return None
        log.info(
            "pursuit note: id=%s source=%s topic=%s",
            mem.id, source, topic[:40],
        )
        return int(mem.id)


__all__ = [
    "PURSUIT_NOTE_KIND",
    "SOURCE_AWAY_BEAT",
    "SOURCE_HOBBY_MILESTONE",
    "SOURCE_HOBBY_WRAPUP",
    "PursuitNoteWriter",
]
