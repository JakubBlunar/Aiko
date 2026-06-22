"""Anniversary matching for shared moments.

Pure functions that take a list of :class:`SharedMomentRow` and a
"today" datetime and return the single best anniversary match, plus a
ready-to-paste prompt block. Kept module-local (no DB, no LLM) so it's
easy to unit-test.

Cadence: 1-month, 3-month, 6-month, 1-year and per-year-after windows,
each matched within a ±1 calendar-day tolerance. The largest matching
window wins so a moment from 1y3mo ago doesn't keep firing the
"3 months ago" badge — we surface the 1y match instead.

Rate-limiting: a moment whose ``metadata.last_anniversaried_at`` is
within the last 6 hours is skipped so the same anniversary doesn't fire
on every turn during a conversation.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from app.core.relationship.shared_moments import SharedMomentRow


# Anniversary windows in days. Negative entries are deltas from today;
# the matcher considers each in turn and stops at the first match. Order
# matters: the longest window is checked first so a "1 year ago" beats a
# coincidental "1 month ago" overlap for the same moment.
_WINDOW_DAYS_DESC: tuple[tuple[int, str], ...] = (
    (365 * 5, "five years ago today"),
    (365 * 4, "four years ago today"),
    (365 * 3, "three years ago today"),
    (365 * 2, "two years ago today"),
    (365, "a year ago today"),
    (180, "six months ago today"),
    (90, "three months ago today"),
    (30, "a month ago today"),
)

# Tolerance in days for matching the window. Spans the day before and
# after to be lenient with timezones and "it's nearly the same date".
_TOLERANCE_DAYS = 1.0

# How long after surfacing a moment do we skip it. Six hours means a
# multi-turn conversation only surfaces the same anniversary once.
_RATE_LIMIT_SECONDS = 6 * 3600.0

# J6: vibes never surfaced as an anniversary. A "repair" moment marks a
# resolved disagreement; "happy anniversary of our fight" is exactly the
# grievance-ledger tone the feature must avoid. Repairs still ride normal
# RAG recall when the topic genuinely resurfaces.
_ANNIVERSARY_EXCLUDED_VIBES: frozenset[str] = frozenset({"repair"})


@dataclass(slots=True)
class AnniversaryMatch:
    """One picked anniversary, ready to render or post-stamp."""

    moment_id: int
    summary: str
    vibe: str
    days_ago: int
    window_label: str  # "a year ago today"
    when_iso: str


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def pick_anniversary(
    moments: Iterable["SharedMomentRow"],
    *,
    now: datetime,
    tolerance_days: float = _TOLERANCE_DAYS,
    rate_limit_seconds: float = _RATE_LIMIT_SECONDS,
) -> AnniversaryMatch | None:
    """Pick the single best anniversary match for ``now``.

    Strategy:
      * For each (window, label), find moments whose ``when`` was that
        many days ago (within ``tolerance_days``).
      * Among matches, prefer pinned > newer ``when`` > highest salience.
        Rows whose ``last_anniversaried_at`` is within ``rate_limit_seconds``
        are skipped.
      * Return the first window with any match; longer windows are
        checked first.
    """
    candidates_by_window: dict[int, list["SharedMomentRow"]] = {}
    for moment in moments:
        if str(getattr(moment, "vibe", "")) in _ANNIVERSARY_EXCLUDED_VIBES:
            continue
        when = _parse_iso(moment.when)
        if when is None:
            continue
        days_ago = (now - when).total_seconds() / 86400.0
        if days_ago < 0:
            continue
        for window, _label in _WINDOW_DAYS_DESC:
            if abs(days_ago - window) <= tolerance_days:
                candidates_by_window.setdefault(window, []).append(moment)
                break

    if not candidates_by_window:
        return None

    last_stamp_cutoff = now - timedelta(seconds=rate_limit_seconds)

    for window, label in _WINDOW_DAYS_DESC:
        bucket = candidates_by_window.get(window) or []
        fresh: list["SharedMomentRow"] = []
        for moment in bucket:
            stamped = _parse_iso(moment.last_anniversaried_at)
            if stamped is not None and stamped >= last_stamp_cutoff:
                continue
            fresh.append(moment)
        if not fresh:
            continue
        # Pinned first, newer-when next, highest-salience last. Python's
        # sort is stable so chaining gives composite ordering without the
        # awkward "reverse a string" tricks.
        fresh.sort(key=lambda r: -r.salience)
        fresh.sort(key=lambda r: r.when, reverse=True)
        fresh.sort(key=lambda r: 0 if r.pinned else 1)
        chosen = fresh[0]
        when_dt = _parse_iso(chosen.when)
        days_ago = (
            int((now - when_dt).total_seconds() // 86400) if when_dt is not None else window
        )
        return AnniversaryMatch(
            moment_id=int(chosen.id),
            summary=str(chosen.summary),
            vibe=str(chosen.vibe),
            days_ago=days_ago,
            window_label=label,
            when_iso=chosen.when,
        )
    return None


def render_anniversary_block(match: AnniversaryMatch | None) -> str:
    """Format the match as the inner-life prompt block."""
    if match is None:
        return ""
    return (
        f"On your mind today — {match.window_label}: {match.summary}. "
        "Acknowledge naturally if it fits the conversation — never force "
        "a 'remember when' if it doesn't."
    )
