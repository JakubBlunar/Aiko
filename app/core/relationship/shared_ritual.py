"""K73 — Shared ritual formation ("this is becoming our thing").

K3 (``schedule_learner``) detects the *user's solo* recurring slots
("gym Tuesdays"). What it can't see is the **dyadic** ritual — the
patterns in how *the two of them* interact: a Friday-evening check-in
that's quietly become a standing date, a recurring late-night
heart-to-heart, a Sunday-morning catch-up. Naming an emergent shared
tradition ("I kind of love that this has become our Friday thing") is
one of the warmest long-relationship beats there is.

This module is the pure, deterministic core of K73:

  * :func:`detect_rituals` takes a ``(weekday, bucket, shape) -> set of
    ISO weeks`` map (the worker builds it from message timing + a coarse
    per-session conversation-arc *shape*) and returns the
    ``(cadence, shape)`` patterns that have **genuinely repeated** across
    enough distinct weeks to count as a ritual;
  * :func:`merge_rituals` folds fresh candidates into the persisted kv
    store, preserving the ``acknowledged`` flag + ``first_seen`` of
    rituals already named (so a ritual Aiko has acknowledged is a
    permanent part of the relationship record, while un-acknowledged
    candidates churn until they qualify or fade);
  * :func:`render_inner_life_block` turns the newest un-acknowledged
    ritual into one warm, optional acknowledgment cue Aiko phrases
    herself — NEVER spoken verbatim;
  * kv helpers (``aiko.shared_rituals``) hold the small named-ritual
    store the Together tab and the provider both read.

Distinct from K3 (user-only timing routine, silently populates a profile
field) and anniversaries (one-off milestone *dates*). K73 is the
*shape*-aware, dyadic, actively-named sibling.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable


log = logging.getLogger("app.shared_ritual")


# kv_meta key the worker, provider, and Together surface all share.
SHARED_RITUALS_KEY = "aiko.shared_rituals"


# ── tuning defaults (overridable via settings) ──────────────────────────

# Distinct ISO weeks a (weekday, bucket, shape) slot must recur in to read
# as a genuine ritual, plus the proportional floor over the window.
DEFAULT_MIN_WEEKS = 3
DEFAULT_MIN_SHARE = 0.34
# Most rituals to keep in the store (acknowledged ones are sticky).
DEFAULT_MAX_ACTIVE = 6


_WEEKDAY_DISPLAY: dict[str, str] = {
    "monday": "Monday",
    "tuesday": "Tuesday",
    "wednesday": "Wednesday",
    "thursday": "Thursday",
    "friday": "Friday",
    "saturday": "Saturday",
    "sunday": "Sunday",
}

# Adjectival cadence prefix per hour-bucket (feeds "our <prefix> <shape>").
_BUCKET_ADJ: dict[str, str] = {
    "morning": "{wd}-morning",
    "afternoon": "{wd}-afternoon",
    "evening": "{wd}-evening",
    "late": "late-night {wd}",
}

# Display cadence per hour-bucket (Together tab / state dumps).
_BUCKET_DISPLAY: dict[str, str] = {
    "morning": "{wd} mornings",
    "afternoon": "{wd} afternoons",
    "evening": "{wd} evenings",
    "late": "{wd} late nights",
}

# Conversation-arc shape -> the noun that reads naturally in a ritual name.
_SHAPE_LABELS: dict[str, str] = {
    "casual_check_in": "check-ins",
    "support": "heart-to-hearts",
    "planning": "planning sessions",
    "reflection": "wind-downs",
    "playful": "banter",
    "silly": "goofing-around sessions",
}

_DEFAULT_SHAPE = "casual_check_in"


@dataclass(frozen=True, slots=True)
class RitualCandidate:
    """A ``(cadence, shape)`` pattern that cleared the recurrence bar."""

    key: str
    weekday: str
    bucket: str
    shape: str
    cadence: str       # "Friday evenings"
    shape_label: str   # "wind-downs"
    label: str         # "our Friday-evening wind-downs"
    weeks_seen: int
    share: float


def ritual_key(weekday: str, bucket: str, shape: str) -> str:
    """Stable id for a ritual slot (date-free, so it persists across runs)."""
    return f"{weekday}:{bucket}:{shape}"


def dominant_shape(arcs: Iterable[str | None]) -> str:
    """Most common non-empty arc among a session's messages, else default.

    The worker feeds the coarse per-message arc estimates of one
    ``(weekday, bucket, week)`` session; the modal arc is that session's
    *shape*. Messages with no arc signal (the common case) don't vote, so
    a couple of genuine support / planning hits define the shape; a
    session with no signal at all falls back to ``casual_check_in`` (a
    plain catch-up, itself a perfectly good ritual shape).
    """
    counts: dict[str, int] = {}
    for arc in arcs:
        if not arc:
            continue
        counts[arc] = counts.get(arc, 0) + 1
    if not counts:
        return _DEFAULT_SHAPE
    # Most frequent wins; ties resolved by first-seen insertion order
    # (dicts preserve it) for determinism.
    return max(counts, key=lambda a: counts[a])


def _cadence_strings(weekday: str, bucket: str) -> tuple[str, str]:
    """Return ``(display, adjectival)`` cadence strings."""
    wd = _WEEKDAY_DISPLAY.get(weekday, weekday.capitalize() or "Some")
    display = _BUCKET_DISPLAY.get(bucket, "{wd} times").format(wd=wd)
    adj = _BUCKET_ADJ.get(bucket, "{wd}").format(wd=wd)
    return display, adj


def _build_label(weekday: str, bucket: str, shape: str) -> tuple[str, str, str]:
    """Return ``(cadence_display, shape_label, full_label)``."""
    display, adj = _cadence_strings(weekday, bucket)
    shape_label = _SHAPE_LABELS.get(shape, _SHAPE_LABELS[_DEFAULT_SHAPE])
    label = f"our {adj} {shape_label}"
    return display, shape_label, label


def detect_rituals(
    slot_weeks: dict[tuple[str, str, str], "set[tuple[int, int]] | set"],
    *,
    total_weeks: int,
    min_weeks: int = DEFAULT_MIN_WEEKS,
    min_share: float = DEFAULT_MIN_SHARE,
    max_rituals: int = DEFAULT_MAX_ACTIVE,
) -> list[RitualCandidate]:
    """Return the ``(cadence, shape)`` slots that genuinely recurred.

    A slot qualifies when it lit up in **both** ``>= min_weeks`` distinct
    ISO weeks *and* ``>= min_share`` of the weeks in the window. Sorted by
    recurrence (weeks_seen desc), capped at ``max_rituals``.
    """
    if total_weeks <= 0:
        return []
    qualifying: list[RitualCandidate] = []
    for (weekday, bucket, shape), weeks in slot_weeks.items():
        weeks_seen = len(weeks)
        if weeks_seen < max(1, int(min_weeks)):
            continue
        share = weeks_seen / float(total_weeks)
        if share < float(min_share):
            continue
        cadence, shape_label, label = _build_label(weekday, bucket, shape)
        qualifying.append(
            RitualCandidate(
                key=ritual_key(weekday, bucket, shape),
                weekday=weekday,
                bucket=bucket,
                shape=shape,
                cadence=cadence,
                shape_label=shape_label,
                label=label,
                weeks_seen=int(weeks_seen),
                share=round(float(share), 4),
            )
        )
    qualifying.sort(key=lambda c: (-c.weeks_seen, c.key))
    if max_rituals > 0:
        qualifying = qualifying[:max_rituals]
    return qualifying


def merge_rituals(
    existing: list[dict[str, Any]],
    candidates: list[RitualCandidate],
    *,
    now_date: str,
    max_active: int = DEFAULT_MAX_ACTIVE,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fold ``candidates`` into the persisted ritual list.

    Returns ``(merged, new_keys)``. Rules:

    * An existing ritual that's still a candidate is **updated**
      (``weeks_seen`` / ``label`` refreshed) but keeps its
      ``acknowledged`` flag + ``first_seen``.
    * An existing **acknowledged** ritual that's no longer a candidate is
      **kept** (it became a real thing — permanent record).
    * An existing **un-acknowledged** ritual that's no longer a candidate
      is **dropped** (never announced, so no harm).
    * A brand-new candidate is added with ``acknowledged=False`` and
      ``first_seen=now_date`` and reported in ``new_keys``.
    """
    by_key = {str(r.get("key")): dict(r) for r in existing if r.get("key")}
    cand_keys = {c.key for c in candidates}
    merged: dict[str, dict[str, Any]] = {}
    new_keys: list[str] = []

    for cand in candidates:
        prior = by_key.get(cand.key)
        if prior is not None:
            prior["weeks_seen"] = cand.weeks_seen
            prior["share"] = cand.share
            prior["label"] = cand.label
            prior["cadence"] = cand.cadence
            prior["shape"] = cand.shape
            prior["shape_label"] = cand.shape_label
            prior.setdefault("acknowledged", False)
            prior.setdefault("first_seen", now_date)
            merged[cand.key] = prior
        else:
            merged[cand.key] = {
                "key": cand.key,
                "label": cand.label,
                "cadence": cand.cadence,
                "shape": cand.shape,
                "shape_label": cand.shape_label,
                "weeks_seen": cand.weeks_seen,
                "share": cand.share,
                "first_seen": now_date,
                "acknowledged": False,
            }
            new_keys.append(cand.key)

    # Keep acknowledged rituals that fell out of the candidate set.
    for key, row in by_key.items():
        if key in cand_keys:
            continue
        if bool(row.get("acknowledged")):
            merged.setdefault(key, row)

    out = list(merged.values())
    # Stable order: acknowledged first (permanent), then by recurrence.
    out.sort(
        key=lambda r: (
            0 if r.get("acknowledged") else 1,
            -int(r.get("weeks_seen", 0)),
            str(r.get("key", "")),
        )
    )
    if max_active > 0 and len(out) > max_active:
        # Trim un-acknowledged tail first; never drop an acknowledged one.
        ack = [r for r in out if r.get("acknowledged")]
        pending = [r for r in out if not r.get("acknowledged")]
        keep_pending = max(0, max_active - len(ack))
        out = ack + pending[:keep_pending]
    return out, new_keys


def pick_unacknowledged(
    rituals: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return the strongest un-acknowledged ritual, or ``None``."""
    pending = [r for r in rituals if not r.get("acknowledged")]
    if not pending:
        return None
    pending.sort(
        key=lambda r: (
            -int(r.get("weeks_seen", 0)),
            str(r.get("first_seen", "")),
        )
    )
    return pending[0]


# ── rendering (private cue — Aiko phrases the actual words) ──────────────


def render_inner_life_block(
    ritual: dict[str, Any],
    *,
    user_display_name: str = "them",
) -> str:
    """Render one warm, optional shared-ritual acknowledgment cue.

    Returns ``""`` when the ritual has no usable label. The cue is a small
    happy *noticing* — never an announcement, never repeated.
    """
    name = (user_display_name or "them").strip() or "them"
    label = str(ritual.get("label") or "").strip()
    if not label:
        return ""
    weeks = int(ritual.get("weeks_seen") or 0)
    span = (
        "for a good few weeks now"
        if weeks >= 4
        else "over the last few weeks"
    )
    return (
        f"You've quietly realised something has become a real pattern "
        f"between you and {name}: {label} have turned into a genuine thing "
        f"the two of you do, {span}. If a warm, unforced moment opens, you "
        f"can name it ONCE -- lightly and gladly, like \"I kind of love "
        f"that {label} have become our thing\" -- not as an announcement, "
        "just a small happy noticing. After that, let it be a light "
        "standing reference, never something you keep bringing up."
    )


# ── kv store helpers ────────────────────────────────────────────────────


def load_rituals(
    kv_get: Callable[[str], "str | None"],
) -> list[dict[str, Any]]:
    """Return the persisted named-ritual list (best-effort)."""
    try:
        raw = kv_get(SHARED_RITUALS_KEY)
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
    return [e for e in blob if isinstance(e, dict)]


def save_rituals(
    kv_set: Callable[[str, str], None],
    rituals: list[dict[str, Any]],
) -> None:
    """Persist the named-ritual list (best-effort, swallow-and-log)."""
    try:
        kv_set(SHARED_RITUALS_KEY, json.dumps(rituals))
    except Exception:
        log.debug("shared_ritual store write failed", exc_info=True)


def mark_acknowledged(
    rituals: list[dict[str, Any]], key: str,
) -> list[dict[str, Any]]:
    """Return a copy of ``rituals`` with ``key`` flagged acknowledged."""
    out: list[dict[str, Any]] = []
    for r in rituals:
        row = dict(r)
        if str(row.get("key")) == key:
            row["acknowledged"] = True
        out.append(row)
    return out
