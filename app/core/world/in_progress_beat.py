"""H26 — an away beat that is still happening when the user comes back.

K36 beats are atomic and past tense. The worker picks something, writes
the *finished* state into the room, journals "re-potted the basil", and
on return Aiko reports it. Every beat is therefore something she has
already tidily completed, which is a strange way to live: nobody's
afternoon is a list of closed tasks. It also means opening the app never
interrupts anything, and never interrupting anything is what makes the
away-life read as a changelog rather than a life.

This module adds the missing notion: a beat with a **wall-clock span**.
When one is left open, the room shows her mid-activity, the journal stays
silent (there is nothing to report yet), and a return inside the window
catches her at it — "oh — hang on, let me put this down".

Three states, one kv key (``away_activity.in_progress``):

* **open** — started, not yet due to finish. A return now catches her.
* **interrupted** — a return caught her and she set it down. The beat is
  now an unfinished thread the worker can pick back up, which is what
  makes it a thread rather than a one-liner.
* **gone** — completed (either the window elapsed with nobody home, or
  she resumed and finished it). The summary lands in the journal at that
  point, so the existing K36 surfacing path is unchanged.

The two-phase shape is borrowed from :class:`GardenVisitWorker`, which
already parks a ``return_at`` watermark and pulls her back when it
passes. This is the same trick applied to the beat itself.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from typing import Any, Callable

log = logging.getLogger("app.world.in_progress")

IN_PROGRESS_KEY = "away_activity.in_progress"

# How long each kind of beat plausibly takes, as (min, max) minutes.
# These are not simulation — nothing counts pages — they only have to be
# long enough that catching her mid-way is believable and short enough
# that she is not "still making tea" three hours later.
_DURATIONS: dict[str, tuple[int, int]] = {
    "reading": (25, 70),
    "book": (25, 70),
    "tea": (8, 18),
    "snack": (8, 20),
    "cooking": (20, 50),
    "baking": (35, 80),
    "tidying": (15, 40),
    "cleaning": (15, 40),
    "plant": (10, 25),
    "garden": (15, 40),
    "pet": (10, 25),
    "music": (20, 50),
    "drawing": (25, 70),
    "writing": (25, 60),
    "game": (25, 70),
    "movie": (45, 100),
    "outing": (25, 60),
    "bath": (20, 40),
    "nap": (25, 60),
}
_DEFAULT_DURATION = (15, 45)


@dataclass(slots=True)
class InProgressBeat:
    """An away beat with a span, and where it is in that span."""

    key: str
    activity: str
    posture: str
    summary: str
    started_at: str
    expected_end_at: str
    # Set when a return caught her at it; the beat becomes a thread to
    # pick back up rather than something to report.
    interrupted_at: str = ""
    used_item_id: int | None = None

    @property
    def interrupted(self) -> bool:
        return bool(self.interrupted_at)

    def is_open_at(self, now: datetime) -> bool:
        """True while she would still plausibly be doing this."""
        if self.interrupted:
            return False
        end = parse_iso(self.expected_end_at)
        return end is not None and now < end

    def is_due_at(self, now: datetime) -> bool:
        """True once the window has elapsed and it should be closed out."""
        if self.interrupted:
            return True
        end = parse_iso(self.expected_end_at)
        return end is None or now >= end

    def minutes_in(self, now: datetime) -> int:
        started = parse_iso(self.started_at)
        if started is None:
            return 0
        return max(0, int((now - started).total_seconds() // 60))

    def minutes_left(self, now: datetime) -> int:
        end = parse_iso(self.expected_end_at)
        if end is None:
            return 0
        return max(0, int((end - now).total_seconds() // 60))


def parse_iso(value: Any) -> datetime | None:
    """Lenient ISO-8601 parse; ``None`` on anything unusable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def pick_duration_minutes(
    key: str, rng: random.Random | None = None,
) -> int:
    """Plausible length for a beat of this kind, in minutes."""
    picker = rng or random
    low, high = _DEFAULT_DURATION
    needle = (key or "").lower()
    for name, span in _DURATIONS.items():
        if name in needle:
            low, high = span
            break
    return picker.randint(low, high)


def build(
    *,
    key: str,
    activity: str,
    posture: str,
    summary: str,
    now: datetime,
    rng: random.Random | None = None,
    used_item_id: int | None = None,
) -> InProgressBeat:
    """Open a beat now, ending after a plausible span."""
    minutes = pick_duration_minutes(key, rng)
    return InProgressBeat(
        key=key,
        activity=activity,
        posture=posture,
        summary=summary,
        started_at=now.isoformat(timespec="seconds"),
        expected_end_at=(
            now + timedelta(minutes=minutes)
        ).isoformat(timespec="seconds"),
        used_item_id=int(used_item_id) if used_item_id is not None else None,
    )


def load(kv_get: Callable[[str], str | None]) -> InProgressBeat | None:
    """Read the open beat, or ``None`` when there isn't one."""
    try:
        raw = kv_get(IN_PROGRESS_KEY)
    except Exception:
        return None
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except Exception:
        return None
    if not isinstance(blob, dict):
        return None
    try:
        return InProgressBeat(
            key=str(blob.get("key") or ""),
            activity=str(blob.get("activity") or ""),
            posture=str(blob.get("posture") or ""),
            summary=str(blob.get("summary") or ""),
            started_at=str(blob.get("started_at") or ""),
            expected_end_at=str(blob.get("expected_end_at") or ""),
            interrupted_at=str(blob.get("interrupted_at") or ""),
            used_item_id=(
                int(blob["used_item_id"])
                if blob.get("used_item_id") is not None
                else None
            ),
        )
    except Exception:
        log.debug("in-progress beat decode failed", exc_info=True)
        return None


def save(kv_set: Callable[[str, str], None], beat: InProgressBeat) -> None:
    """Persist the open beat."""
    try:
        kv_set(IN_PROGRESS_KEY, json.dumps(asdict(beat)))
    except Exception:
        log.debug("in-progress beat write failed", exc_info=True)


def clear(kv_set: Callable[[str, str], None]) -> None:
    """Close the beat out — nothing is in progress any more."""
    try:
        kv_set(IN_PROGRESS_KEY, "")
    except Exception:
        log.debug("in-progress beat clear failed", exc_info=True)


def mark_interrupted(
    kv_set: Callable[[str, str], None],
    beat: InProgressBeat,
    now: datetime,
) -> InProgressBeat:
    """Record that a return caught her at it and she set it down."""
    beat.interrupted_at = now.isoformat(timespec="seconds")
    save(kv_set, beat)
    return beat


__all__ = [
    "IN_PROGRESS_KEY",
    "InProgressBeat",
    "build",
    "clear",
    "load",
    "mark_interrupted",
    "parse_iso",
    "pick_duration_minutes",
    "save",
]
