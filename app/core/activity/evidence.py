"""Privacy-safe C6 evidence for Live and K72.

Reads the activity store and returns app-name-only fields. Never titles,
never an LLM. Store failures become missing evidence so callers cannot
stall.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any, Mapping, Sequence

from app.core.infra import timephrase


log = logging.getLogger("app.activity.evidence")

STALE_AFTER_SECONDS = 120.0
CODING_CONFIDENCE = 0.7
UNKNOWN_APP_CONFIDENCE = 0.35
CODING_CONFIDENCE_FLOOR = 0.5

CODING_MARKERS = (
    "cursor",
    "code",
    "vscode",
    "visual studio",
    "pycharm",
    "intellij",
    "terminal",
    "notepad",
    "vim",
    "emacs",
)
MEDIA_MARKERS = (
    "youtube",
    "netflix",
    "vlc",
    "mpv",
    "anime",
    "plex",
    "spotify",
    "twitch",
)

_OS_IDLE_VALUES = frozenset({"active", "idle", "locked", "missing"})


@dataclass(frozen=True, slots=True)
class ActivityEvidence:
    """Aggregator output. ``conflicts`` stay empty; the assembler records them."""

    app: str = ""
    source: str = ""
    duration_seconds: int = 0
    os_idle: str = "missing"
    stale: bool = True
    confidence: float = 0.0
    present: bool = False
    conflicts: tuple[str, ...] = ()
    session_count: int = 0
    idle_span_seconds: int = 0
    lock_span_seconds: int = 0

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("title", None)
        return payload


def compute_activity_rollup(
    sessions: Sequence[Mapping[str, Any]] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Level-1 rollup. App names and durations only — never titles."""
    clock = now if now is not None else timephrase.utcnow()
    last_session_id = 0
    session_count = 0
    idle_span = 0
    lock_span = 0
    app = ""
    if not sessions:
        return {
            "last_session_id": 0,
            "app": "",
            "session_count": 0,
            "idle_span_seconds": 0,
            "lock_span_seconds": 0,
            "computed_at": clock.isoformat(timespec="seconds"),
        }
    for row in sessions:
        if not isinstance(row, Mapping):
            continue
        try:
            last_session_id = max(last_session_id, int(row.get("id") or 0))
        except (TypeError, ValueError):
            pass
        source = str(row.get("source") or "").strip().lower()
        try:
            duration = max(0, int(float(row.get("duration_seconds") or 0)))
        except (TypeError, ValueError):
            duration = 0
        session_count += 1
        if source == "idle":
            idle_span += duration
        elif source == "lock":
            lock_span += duration
        elif source == "foreground" and not app:
            app = str(row.get("app") or "").strip()
    return {
        "last_session_id": last_session_id,
        "app": app,
        "session_count": session_count,
        "idle_span_seconds": idle_span,
        "lock_span_seconds": lock_span,
        "computed_at": clock.isoformat(timespec="seconds"),
    }


MISSING_EVIDENCE = ActivityEvidence()


def classify_app(app: str) -> str:
    """Same coding/media markers Live's assembler uses."""
    lowered = str(app or "").strip().lower()
    if not lowered:
        return ""
    if any(marker in lowered for marker in CODING_MARKERS):
        return "coding"
    if any(marker in lowered for marker in MEDIA_MARKERS):
        return "media"
    return "other"


def evidence_from_store(
    store: Any | None,
    *,
    now: datetime | None = None,
) -> ActivityEvidence:
    """Read ``recent_sessions`` + ``last_event``. Never raises."""
    if store is None:
        return MISSING_EVIDENCE
    clock = now if now is not None else timephrase.utcnow()
    try:
        last = store.last_event()
        sessions = store.recent_sessions(limit=24)
    except Exception:
        log.debug("activity evidence store read failed", exc_info=True)
        return MISSING_EVIDENCE
    try:
        return _from_rows(last, sessions, now=clock)
    except Exception:
        log.debug("activity evidence compute failed", exc_info=True)
        return MISSING_EVIDENCE


def late_night_dates_from_sessions(
    sessions: Sequence[Mapping[str, Any]] | None,
    *,
    now: datetime,
    window_days: int,
    start_hour: int,
    end_hour: int,
) -> list[str]:
    """Local-tz dates whose foreground sessions overlap the small-hours window.

    Failures yield an empty list. Titles are ignored.
    """
    if not sessions:
        return []
    try:
        cutoff = now - timedelta(days=max(1, int(window_days)))
        dates: set[str] = set()
        for row in sessions:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("source") or "") != "foreground":
                continue
            started = _parse_at(row.get("started_at"))
            ended = _parse_at(row.get("ended_at")) or started
            if started is None:
                continue
            if ended is not None and ended < cutoff and started < cutoff:
                continue
            for date_key in _small_hours_dates(
                started, ended, start_hour=start_hour, end_hour=end_hour,
            ):
                dates.add(date_key)
        return sorted(dates)
    except Exception:
        log.debug("activity late-night dates failed", exc_info=True)
        return []


def long_focus_dates_from_sessions(
    sessions: Sequence[Mapping[str, Any]] | None,
    *,
    now: datetime,
    window_days: int,
    min_seconds: int,
    night_start_hour: int,
    night_end_hour: int,
) -> list[str]:
    """Local-tz dates whose coding foreground time (outside small hours)
    clears ``min_seconds``.

    Failures yield an empty list. Titles are ignored. Media / idle / lock
    do not count. Small-hours overlap is subtracted so C9 late-nights and
    daytime long-focus do not double-count the same sitting.
    """
    if not sessions:
        return []
    try:
        cutoff = now - timedelta(days=max(1, int(window_days)))
        threshold = max(1, int(min_seconds))
        per_day: dict[str, int] = {}
        for row in sessions:
            if not isinstance(row, Mapping):
                continue
            if str(row.get("source") or "") != "foreground":
                continue
            if classify_app(str(row.get("app") or "")) != "coding":
                continue
            started = _parse_at(row.get("started_at"))
            if started is None:
                continue
            ended = _parse_at(row.get("ended_at"))
            if ended is None or ended <= started:
                try:
                    extra = max(0, int(float(row.get("duration_seconds") or 0)))
                except (TypeError, ValueError):
                    extra = 0
                ended = started + timedelta(seconds=extra)
            if ended < cutoff and started < cutoff:
                continue
            for date_key, seconds in _daytime_coding_seconds(
                started,
                ended,
                night_start_hour=night_start_hour,
                night_end_hour=night_end_hour,
            ):
                per_day[date_key] = per_day.get(date_key, 0) + seconds
        return sorted(
            day for day, seconds in per_day.items() if seconds >= threshold
        )
    except Exception:
        log.debug("activity long-focus dates failed", exc_info=True)
        return []


def _from_rows(
    last: Mapping[str, Any] | None,
    sessions: Sequence[Mapping[str, Any]] | None,
    *,
    now: datetime,
) -> ActivityEvidence:
    if not last:
        return MISSING_EVIDENCE
    source = str(last.get("source") or "").strip().lower()
    if source not in {"foreground", "idle", "lock"}:
        source = ""
    last_at = _parse_at(last.get("at"))
    stale = last_at is None or _age_seconds(now, last_at) > STALE_AFTER_SECONDS
    os_idle = _os_idle_from_source(source)
    fg = _latest_foreground_session(sessions)
    app = str(last.get("app") or "").strip()
    if not app and fg is not None:
        app = str(fg.get("app") or "").strip()
    duration = 0
    if fg is not None:
        try:
            duration = max(0, int(float(fg.get("duration_seconds") or 0)))
        except (TypeError, ValueError):
            duration = 0
    confidence = 0.0
    if not stale:
        kind = classify_app(app)
        if kind == "coding":
            confidence = CODING_CONFIDENCE
        elif app:
            confidence = UNKNOWN_APP_CONFIDENCE
    rollup = compute_activity_rollup(sessions, now=now)
    return ActivityEvidence(
        app=app,
        source=source,
        duration_seconds=duration,
        os_idle=os_idle if os_idle in _OS_IDLE_VALUES else "missing",
        stale=stale,
        confidence=confidence,
        present=True,
        session_count=int(rollup.get("session_count") or 0),
        idle_span_seconds=int(rollup.get("idle_span_seconds") or 0),
        lock_span_seconds=int(rollup.get("lock_span_seconds") or 0),
    )


def _os_idle_from_source(source: str) -> str:
    if source == "lock":
        return "locked"
    if source == "idle":
        return "idle"
    if source == "foreground":
        return "active"
    return "missing"


def _latest_foreground_session(
    sessions: Sequence[Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    if not sessions:
        return None
    for row in sessions:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("source") or "") == "foreground":
            return row
    return None


def _small_hours_dates(
    started: datetime,
    ended: datetime | None,
    *,
    start_hour: int,
    end_hour: int,
) -> list[str]:
    local_start = started.astimezone()
    local_end = (ended or started).astimezone()
    if local_end < local_start:
        local_end = local_start
    dates: list[str] = []
    day = local_start.date()
    last_day = local_end.date()
    tz = local_start.tzinfo
    while day <= last_day:
        window_start = datetime.combine(
            day, dt_time(hour=int(start_hour)), tzinfo=tz,
        )
        window_end = datetime.combine(
            day, dt_time(hour=int(end_hour)), tzinfo=tz,
        )
        if local_start < window_end and local_end >= window_start:
            dates.append(day.isoformat())
        day += timedelta(days=1)
    return dates


def _daytime_coding_seconds(
    started: datetime,
    ended: datetime,
    *,
    night_start_hour: int,
    night_end_hour: int,
) -> list[tuple[str, int]]:
    """Seconds of ``[started, ended)`` on each local date, minus small hours."""
    local_start = started.astimezone()
    local_end = ended.astimezone()
    if local_end <= local_start:
        return []
    out: list[tuple[str, int]] = []
    tz = local_start.tzinfo
    day = local_start.date()
    last_day = local_end.date()
    while day <= last_day:
        day_start = datetime.combine(day, dt_time(), tzinfo=tz)
        day_end = day_start + timedelta(days=1)
        overlap_start = max(local_start, day_start)
        overlap_end = min(local_end, day_end)
        total = _overlap_seconds(overlap_start, overlap_end, day_start, day_end)
        night_start = datetime.combine(
            day, dt_time(hour=int(night_start_hour)), tzinfo=tz,
        )
        night_end = datetime.combine(
            day, dt_time(hour=int(night_end_hour)), tzinfo=tz,
        )
        night = _overlap_seconds(
            overlap_start, overlap_end, night_start, night_end,
        )
        daytime = max(0, total - night)
        if daytime > 0:
            out.append((day.isoformat(), daytime))
        day += timedelta(days=1)
    return out


def _overlap_seconds(
    start: datetime,
    end: datetime,
    win_start: datetime,
    win_end: datetime,
) -> int:
    lo = max(start, win_start)
    hi = min(end, win_end)
    if hi <= lo:
        return 0
    return int((hi - lo).total_seconds())


def _age_seconds(now: datetime, then: datetime) -> float:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (now - then).total_seconds())


def _parse_at(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
