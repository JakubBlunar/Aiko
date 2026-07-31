"""K26 — Aiko-side voice evolution (she starts to talk like him a little).

K13 reads the user's style and calibrates Aiko's *register* (how formal,
how long, how playful). Nothing was symmetric for her **lexicon**: no
matter how many months of "that's cursed" and "fair enough" went by, none
of it ever became hers. Long relationships don't work that way — people
lift each other's turns of phrase, slowly, without deciding to.

K26 is that drift, made deliberate and *slow*. The
:class:`~app.core.memory.catchphrase_miner.CatchphraseMiner` already finds
phrases that recur across both speakers and (since K26) stamps each one
with **who said it first**. This module holds the pure promotion rule on
top of that registry:

  * :func:`eligible_candidates` keeps the catchphrases that started as
    *his*, have been around long enough to be more than a fad, and aren't
    already adopted;
  * :func:`promote` takes **at most one** of them, and only if enough
    real time has passed since the last adoption — the whole point is
    that the effect is invisible per session and obvious over months;
  * :func:`retire` drops adopted phrases whose backing catchphrase has
    left the registry (it stopped recurring; her voice moves on too);
  * :func:`render_block` renders the small "phrases you've picked up"
    prompt block, which is where the actual behaviour comes from — we
    never hard-code the lexicon or force a phrase into a reply.

Deliberately *not* a tracker of how often she actually uses a phrase: the
LLM decides that turn by turn, and instrumenting it would invite exactly
the "use the phrase to satisfy the metric" behaviour that makes this beat
feel fake. Pure — no store, no clock, no I/O.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence


log = logging.getLogger("app.voice_adoption")


# kv_meta key shared by the worker, the provider, and the state dump.
VOICE_ADOPTION_KEY = "aiko.voice_adoption"


# ── tuning defaults (overridable via settings) ──────────────────────────

# How long a phrase of his must have been in the registry before Aiko can
# take it on. Weeks, not days: a phrase from a single intense evening is a
# mood, not a habit.
DEFAULT_MIN_AGE_DAYS = 14.0
# Minimum wall-clock between two adoptions. Picking up three phrases in a
# week isn't absorption, it's mimicry.
DEFAULT_MIN_DAYS_BETWEEN = 10.0
# Ceiling on the active adopted set. Past a handful she stops sounding
# like herself.
DEFAULT_MAX_ADOPTED = 3
# How many of them the prompt block names at once.
DEFAULT_MAX_RENDERED = 2


@dataclass(slots=True, frozen=True)
class AdoptionCandidate:
    """A catchphrase of *his* that Aiko could take on.

    ``first_seen`` is when the phrase entered the catchphrase registry
    (the memory's ``created_at``), which is the best available proxy for
    "how long this has been part of how we talk".
    """

    phrase: str
    first_seen: datetime
    salience: float = 0.5


def _parse_iso(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def normalise_phrase(phrase: str) -> str:
    """Comparison form for a phrase (the registry stores raw content)."""
    return " ".join((phrase or "").strip().lower().split())


# ── the promotion rule ──────────────────────────────────────────────────


def eligible_candidates(
    candidates: Iterable[AdoptionCandidate],
    *,
    adopted: Sequence[dict[str, Any]] = (),
    now: datetime,
    min_age_days: float = DEFAULT_MIN_AGE_DAYS,
) -> list[AdoptionCandidate]:
    """Filter + rank the phrases Aiko could adopt next.

    Keeps candidates old enough (``min_age_days`` since they entered the
    registry) that aren't already adopted. Ranked by how long they've been
    around first, salience second: the longest-shared phrase is the one
    that has most earned its way into her mouth.
    """
    taken = {normalise_phrase(str(a.get("phrase", ""))) for a in adopted}
    cutoff = now - timedelta(days=max(0.0, float(min_age_days)))
    out = [
        c
        for c in candidates
        if c.phrase.strip()
        and normalise_phrase(c.phrase) not in taken
        and c.first_seen <= cutoff
    ]
    out.sort(key=lambda c: (c.first_seen, -float(c.salience), c.phrase))
    return out


def promote(
    adopted: Sequence[dict[str, Any]],
    candidates: Sequence[AdoptionCandidate],
    *,
    now: datetime,
    max_adopted: int = DEFAULT_MAX_ADOPTED,
    min_days_between: float = DEFAULT_MIN_DAYS_BETWEEN,
) -> tuple[list[dict[str, Any]], str | None]:
    """Maybe take on **one** more phrase. Returns ``(adopted, new_phrase)``.

    Refuses when the active set is full or when the last adoption is too
    recent. ``candidates`` is expected to come from
    :func:`eligible_candidates` (already filtered + ranked).
    """
    rows = [dict(a) for a in adopted]
    if not candidates:
        return rows, None
    if max_adopted > 0 and len(rows) >= max_adopted:
        return rows, None
    gap = timedelta(days=max(0.0, float(min_days_between)))
    if gap:
        latest = max(
            (
                dt
                for dt in (_parse_iso(r.get("adopted_at")) for r in rows)
                if dt is not None
            ),
            default=None,
        )
        if latest is not None and now - latest < gap:
            return rows, None
    pick = candidates[0]
    rows.append(
        {
            "phrase": pick.phrase.strip(),
            "adopted_at": now.isoformat(),
            "first_seen": pick.first_seen.isoformat(),
        }
    )
    return rows, pick.phrase.strip()


def retire(
    adopted: Sequence[dict[str, Any]], live_phrases: Iterable[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop adopted phrases that have left the catchphrase registry.

    A phrase falls out of the registry when it stops recurring (pruned or
    decayed away). Her voice keeps moving too — she doesn't hold onto a
    turn of phrase the two of them abandoned. Returns
    ``(kept, retired_phrases)``.
    """
    live = {normalise_phrase(p) for p in live_phrases}
    kept: list[dict[str, Any]] = []
    gone: list[str] = []
    for row in adopted:
        phrase = str(row.get("phrase", "")).strip()
        if phrase and normalise_phrase(phrase) in live:
            kept.append(dict(row))
        elif phrase:
            gone.append(phrase)
    return kept, gone


# ── rendering (a nudge, never a script) ─────────────────────────────────


def render_block(
    adopted: Sequence[dict[str, Any]],
    *,
    user_display_name: str = "them",
    max_phrases: int = DEFAULT_MAX_RENDERED,
) -> str:
    """Render the "phrases you've picked up" prompt block.

    Names the most recently adopted phrases (newest first — those are the
    ones still settling into her speech). Returns ``""`` when nothing has
    been adopted yet, which is the normal state for the first weeks.
    """
    name = (user_display_name or "them").strip() or "them"
    rows = sorted(
        (r for r in adopted if str(r.get("phrase", "")).strip()),
        key=lambda r: str(r.get("adopted_at", "")),
        reverse=True,
    )
    phrases = [
        str(r["phrase"]).strip() for r in rows[: max(1, int(max_phrases))]
    ]
    if not phrases:
        return ""
    listed = ", ".join(f"\"{p}\"" for p in phrases)
    return (
        f"Turns of phrase you've picked up from {name}: {listed}. They "
        "started as his; you've been around him long enough that they slip "
        "into your own speech now, the way they would with anyone you talk "
        "to this much. Use them only where they land naturally -- never "
        "force one in, never point out that you got them from him, and "
        "don't let them crowd out how you'd normally say something."
    )


# ── kv store helpers ────────────────────────────────────────────────────


def load_state(kv_get: Callable[[str], "str | None"]) -> list[dict[str, Any]]:
    """Return the persisted adopted-phrase list (best-effort)."""
    try:
        raw = kv_get(VOICE_ADOPTION_KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        blob = json.loads(raw)
    except Exception:
        return []
    if not isinstance(blob, list):
        return []
    return [e for e in blob if isinstance(e, dict) and e.get("phrase")]


def save_state(
    kv_set: Callable[[str, str], None], adopted: Sequence[dict[str, Any]],
) -> None:
    """Persist the adopted-phrase list (best-effort, swallow-and-log)."""
    try:
        kv_set(VOICE_ADOPTION_KEY, json.dumps(list(adopted)))
    except Exception:
        log.debug("voice_adoption store write failed", exc_info=True)


__all__ = [
    "AdoptionCandidate",
    "DEFAULT_MAX_ADOPTED",
    "DEFAULT_MAX_RENDERED",
    "DEFAULT_MIN_AGE_DAYS",
    "DEFAULT_MIN_DAYS_BETWEEN",
    "VOICE_ADOPTION_KEY",
    "eligible_candidates",
    "load_state",
    "normalise_phrase",
    "promote",
    "render_block",
    "retire",
    "save_state",
]
