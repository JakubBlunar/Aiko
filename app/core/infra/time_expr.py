"""Parse relative-time expressions in user text into a concrete window.

K-time2: the memory extractor resolves relative phrases to absolute
``event_time`` at *write* time, but nothing resolves them at *query* time —
so "what did I tell you yesterday about the dashboard?" runs a pure
semantic search and can answer off the wrong day. This module turns a
user's relative-time phrase into a concrete ``(start, end)`` window
against the single ``timephrase`` now-anchor, so :mod:`rag_retriever` can
bias retrieval toward memories/messages actually recorded in that window.

Read-only, deterministic, no LLM. The window is resolved in the anchor's
own timezone (UTC by default, matching how ``created_at`` is stored); the
retrieval bonus is a soft boost, not a hard filter, so a small timezone
skew on a day boundary only shifts the nudge slightly rather than dropping
a correct hit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.infra import timephrase

__all__ = ["TimeWindow", "parse_time_window"]


# Directions: a query looking backward ("yesterday") vs forward
# ("tomorrow") vs a fuzzy "around then" span. Only ``past`` windows arm
# the empty-window tonal guard — we never want to tell the user "I have
# nothing from today" for a throwaway "how are you today".
DIR_PAST = "past"
DIR_FUTURE = "future"


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "couple": 2,
    "few": 3, "several": 4,
}


@dataclass(frozen=True)
class TimeWindow:
    """An inclusive ``[start, end]`` instant range a query refers to.

    ``label`` echoes the matched phrase ("yesterday", "last week") for the
    tonal guard. ``direction`` is :data:`DIR_PAST` / :data:`DIR_FUTURE`.
    ``guardable`` is True only when an empty window should make Aiko say
    she has nothing from then (clearly retrospective phrases).
    """

    start: datetime
    end: datetime
    label: str
    direction: str
    guardable: bool = False

    def contains(self, when: datetime | None) -> bool:
        if when is None:
            return False
        aware = timephrase.to_aware(when)
        if aware is None:
            return False
        return self.start <= aware <= self.end


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def _span(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Normalise so start <= end (defensive)."""
    if start <= end:
        return start, end
    return end, start


def _month_window(now: datetime, year: int, month: int) -> tuple[datetime, datetime]:
    start = now.replace(
        year=year, month=month, day=1,
        hour=0, minute=0, second=0, microsecond=0,
    )
    # First day of the following month, minus a microsecond.
    if month == 12:
        nxt = start.replace(year=year + 1, month=1)
    else:
        nxt = start.replace(month=month + 1)
    return start, nxt - timedelta(microseconds=1)


def _parse_count(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        try:
            return max(1, int(token))
        except ValueError:
            return None
    return _NUMBER_WORDS.get(token)


def parse_time_window(
    text: str, now: datetime | None = None,
) -> TimeWindow | None:
    """Return the first relative-time window referenced by ``text``.

    ``now`` defaults to the live :func:`timephrase.now`. Returns ``None``
    when no recognised relative-time phrase is present. Only the first
    match is returned — queries almost always carry a single time anchor.
    """
    if not text:
        return None
    anchor = timephrase.to_aware(now) if now is not None else timephrase.now()
    if anchor is None:
        return None
    s = text.lower()

    # ── explicit "N <unit> ago" and "last/past N <units>" ────────────
    m = re.search(
        r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|"
        r"couple|few|several)\s+(day|days|week|weeks|month|months)\s+ago\b",
        s,
    )
    if m:
        n = _parse_count(m.group(1)) or 1
        unit = m.group(2)
        if unit.startswith("day"):
            target = anchor - timedelta(days=n)
            start, end = _day_bounds(target)
        elif unit.startswith("week"):
            target = anchor - timedelta(weeks=n)
            start, _ = _day_bounds(target - timedelta(days=target.weekday()))
            end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        else:  # months
            ym = anchor.year * 12 + (anchor.month - 1) - n
            start, end = _month_window(anchor, ym // 12, ym % 12 + 1)
        return TimeWindow(start, end, m.group(0).strip(), DIR_PAST, guardable=True)

    m = re.search(
        r"\b(?:last|past|recent)\s+(\d+|a|an|one|two|three|four|five|six|"
        r"seven|eight|nine|ten|couple|few|several)?\s*(day|days|week|weeks|"
        r"month|months)\b",
        s,
    )
    if m and m.group(2):
        n = _parse_count(m.group(1) or "1") or 1
        unit = m.group(2)
        if unit.startswith("day"):
            start, _ = _day_bounds(anchor - timedelta(days=n))
            _, end = _day_bounds(anchor)
            return TimeWindow(start, end, m.group(0).strip(), DIR_PAST, guardable=True)
        if unit.startswith("week"):
            # "last week" (singular, n defaulted to 1) → previous ISO week.
            this_mon, _ = _day_bounds(anchor - timedelta(days=anchor.weekday()))
            if (m.group(1) is None) and unit == "week":
                start = this_mon - timedelta(weeks=1)
                end = this_mon - timedelta(microseconds=1)
            else:
                start, _ = _day_bounds(anchor - timedelta(weeks=n))
                _, end = _day_bounds(anchor)
            return TimeWindow(start, end, m.group(0).strip(), DIR_PAST, guardable=True)
        # months
        if (m.group(1) is None) and unit == "month":
            ym = anchor.year * 12 + (anchor.month - 1) - 1
            start, end = _month_window(anchor, ym // 12, ym % 12 + 1)
        else:
            ym = anchor.year * 12 + (anchor.month - 1) - n
            start, _ = _month_window(anchor, ym // 12, ym % 12 + 1)
            _, end = _month_window(anchor, anchor.year, anchor.month)
        return TimeWindow(start, end, m.group(0).strip(), DIR_PAST, guardable=True)

    # ── single-word day anchors ──────────────────────────────────────
    if re.search(r"\byesterday\b", s):
        start, end = _day_bounds(anchor - timedelta(days=1))
        return TimeWindow(start, end, "yesterday", DIR_PAST, guardable=True)
    if re.search(r"\b(?:last night|last evening)\b", s):
        y = anchor - timedelta(days=1)
        start = y.replace(hour=18, minute=0, second=0, microsecond=0)
        _, end = _day_bounds(y)
        return TimeWindow(start, end, "last night", DIR_PAST, guardable=True)
    if re.search(r"\btomorrow\b", s):
        start, end = _day_bounds(anchor + timedelta(days=1))
        return TimeWindow(start, end, "tomorrow", DIR_FUTURE)
    if re.search(r"\bthis morning\b", s):
        base, _ = _day_bounds(anchor)
        return TimeWindow(
            base.replace(hour=5), base.replace(hour=11, minute=59, second=59),
            "this morning", DIR_PAST,
        )
    if re.search(r"\bthis afternoon\b", s):
        base, _ = _day_bounds(anchor)
        return TimeWindow(
            base.replace(hour=12), base.replace(hour=16, minute=59, second=59),
            "this afternoon", DIR_PAST,
        )
    if re.search(r"\b(?:this evening|tonight)\b", s):
        base, _ = _day_bounds(anchor)
        return TimeWindow(
            base.replace(hour=17), base.replace(hour=23, minute=59, second=59),
            "this evening", DIR_PAST,
        )
    if re.search(r"\btoday\b", s):
        start, end = _day_bounds(anchor)
        # Not guardable: "today" is common chit-chat, not a recall request.
        return TimeWindow(start, end, "today", DIR_PAST)

    # ── week / month spans ───────────────────────────────────────────
    if re.search(r"\bnext week\b", s):
        this_mon, _ = _day_bounds(anchor - timedelta(days=anchor.weekday()))
        start = this_mon + timedelta(weeks=1)
        end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
        return TimeWindow(start, end, "next week", DIR_FUTURE)
    if re.search(r"\bthis week\b", s):
        this_mon, _ = _day_bounds(anchor - timedelta(days=anchor.weekday()))
        _, end = _day_bounds(anchor)
        return TimeWindow(this_mon, end, "this week", DIR_PAST)
    if re.search(r"\bthis month\b", s):
        start, _ = _month_window(anchor, anchor.year, anchor.month)
        _, end = _day_bounds(anchor)
        return TimeWindow(start, end, "this month", DIR_PAST)

    # ── "on <weekday>" → most recent past occurrence ─────────────────
    m = re.search(
        r"\b(?:on|last)\s+(monday|tuesday|wednesday|thursday|friday|"
        r"saturday|sunday)\b",
        s,
    )
    if m:
        target_wd = _WEEKDAYS[m.group(1)]
        delta = (anchor.weekday() - target_wd) % 7
        delta = delta or 7  # "on Monday" said on a Monday → last Monday
        start, end = _day_bounds(anchor - timedelta(days=delta))
        return TimeWindow(start, end, m.group(0).strip(), DIR_PAST, guardable=True)

    # ── "back in <month>" / "in <month>" → that month ────────────────
    m = re.search(
        r"\b(?:back in|in)\s+(january|february|march|april|may|june|july|"
        r"august|september|october|november|december)\b",
        s,
    )
    if m:
        month = _MONTHS[m.group(1)]
        # Pick the most recent past occurrence: this year if the month
        # has already happened, otherwise last year.
        year = anchor.year if month <= anchor.month else anchor.year - 1
        start, end = _month_window(anchor, year, month)
        return TimeWindow(start, end, m.group(0).strip(), DIR_PAST, guardable=True)

    return None
