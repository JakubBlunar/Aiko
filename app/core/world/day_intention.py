"""K91 pass 4 — her day has something she meant to get to.

Beats were sampled independently: even with episodes, one afternoon bore
no relation to the next morning, so a day never added up to anything. A
person's quiet day usually has a small spine — the tomatoes need seeing
to, the book is nearly done — and the satisfying part is *closing* it.

Once per local day this module proposes one intention drawn from what her
world actually needs, plus her current hobby. It biases beat selection
toward the intention all day (a nudge, never a gate), and the beat that
finally satisfies it says so, which is what turns a stack of activities
into "I'd been meaning to finish that all day".

Pure and total: no I/O, no clock reads, no randomness outside the passed
``rng``. The worker owns persistence; :func:`load` / :func:`dump` only
convert.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.world import beat_detail


# kv_meta key the idle-activity and garden workers share.
DAY_INTENTION_KEY = "aiko.day_intention"

# How much the intention tilts the H18 weighted draw. A nudge: she can
# still spend the day doing something else, the way anyone does.
INTENT_BOOST = 1.8

# A book this close to the end is worth finishing.
_NEARLY_DONE_CHAPTERS = 4

# Ways to admit she'd been meaning to do the thing.
_CLOSING_FRAGMENTS: tuple[str, ...] = (
    "I'd been meaning to get to that all day",
    "which is what I'd promised myself this morning",
    "the one thing I actually meant to do today",
    "I'd had it in the back of my mind since morning",
)

# Small self-directed fallbacks for a day with no pressing need, so the
# spine exists even when nothing in the room is asking for attention.
_FALLBACK_INTENTIONS: tuple[tuple[str, str], ...] = (
    ("sort out my desk properly", "tidy_desk"),
    ("sit and watch the street for a while", "look_outside"),
    ("draw something properly instead of scribbling", "doodle"),
    ("spend a while just thinking", "wander"),
)


@dataclass(frozen=True, slots=True)
class DayIntention:
    """One small thing she meant to do today."""

    day: str
    text: str
    beat_key: str
    satisfied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "text": self.text,
            "beat_key": self.beat_key,
            "satisfied": bool(self.satisfied),
        }

    def satisfy(self) -> "DayIntention":
        return DayIntention(
            day=self.day,
            text=self.text,
            beat_key=self.beat_key,
            satisfied=True,
        )


def local_day(now: datetime) -> str:
    """The local calendar day, matching the worker's cap watermarks."""
    return now.astimezone().strftime("%Y-%m-%d")


def dump(intention: DayIntention) -> str:
    return json.dumps(intention.to_dict())


def load(raw: str | None) -> DayIntention | None:
    """Parse a stored intention, or ``None`` for missing/garbage."""
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    day = str(blob.get("day") or "").strip()
    text = str(blob.get("text") or "").strip()
    if not day or not text:
        return None
    return DayIntention(
        day=day,
        text=text,
        beat_key=str(blob.get("beat_key") or "").strip(),
        satisfied=bool(blob.get("satisfied")),
    )


def propose(
    items: list[Any],
    *,
    now: datetime,
    hobby: str | None = None,
    rng: random.Random,
) -> DayIntention:
    """Pick today's intention from what her world is actually asking for.

    Needs come first and in priority order — ripe produce spoils, a
    thirsty plant suffers, a nearly-finished book nags — with her hobby
    and then a small self-directed pool behind them, so there is always
    an intention even in a becalmed room.
    """
    for text, key in _needs(items, now=now):
        return DayIntention(day=local_day(now), text=text, beat_key=key)
    if hobby:
        return DayIntention(
            day=local_day(now),
            text="put a proper hour into " + hobby,
            beat_key="tidy_desk",
        )
    text, key = rng.choice(_FALLBACK_INTENTIONS)
    return DayIntention(day=local_day(now), text=text, beat_key=key)


def _needs(items: list[Any], *, now: datetime) -> list[tuple[str, str]]:
    """Candidate intentions the room is asking for, most pressing first."""
    needs: list[tuple[str, str]] = []

    ripe = [
        i
        for i in items
        if str(getattr(i, "kind", "")) == "plant"
        and str((getattr(i, "state", None) or {}).get("stage", "")).lower()
        == "mature"
    ]
    if ripe:
        needs.append(
            ("pick the " + str(ripe[0].name) + " before it goes over", "garden")
        )

    thirsty = beat_detail.thirstiest_plant(items, now=now)
    if thirsty is not None:
        needs.append(
            ("give the " + str(thirsty.name) + " a proper soak", "garden")
        )

    for item in items:
        state = getattr(item, "state", None)
        if not isinstance(state, dict):
            continue
        try:
            total = int(state.get("total", 0))
            progress = int(state.get("progress", 0))
        except (TypeError, ValueError):
            continue
        if total <= 0 or progress <= 0:
            continue
        if total - progress <= _NEARLY_DONE_CHAPTERS:
            title = str(state.get("title") or getattr(item, "name", "my book"))
            needs.append(("finish " + title, "read_book"))
            break

    return needs


def closing_fragment(rng: random.Random) -> str:
    """How she admits the thing she'd meant to do is now done."""
    return rng.choice(_CLOSING_FRAGMENTS)


def close_out(summary: str, rng: random.Random) -> str:
    """Append the "meant to do that" admission to a beat's clause."""
    base = (summary or "").strip().rstrip(".")
    if not base:
        return base
    return base + " — " + closing_fragment(rng)


__all__ = [
    "DAY_INTENTION_KEY",
    "INTENT_BOOST",
    "DayIntention",
    "local_day",
    "load",
    "dump",
    "propose",
    "closing_fragment",
    "close_out",
]
