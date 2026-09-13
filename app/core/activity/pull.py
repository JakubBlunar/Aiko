"""C7 live activity pull — last session plus an optional fresh sample.

The tool waits a bounded window for a collector ``snapshot()`` that
travelled the same ingest + redact path. Timeout or no desktop returns
the last stored session, not an error. Titles are whatever ingest
already allowed; UIA is not stubbed here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from app.core.activity.envelope import ActivityEnvelope
from app.core.infra import timephrase


PULL_TIMEOUT_SECONDS = 0.25


def format_activity_view(
    *,
    session_row: Mapping[str, Any] | None = None,
    live: ActivityEnvelope | None = None,
    fresh: bool = False,
    enabled: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Privacy-safe tool payload. No surface id. No UIA tree."""
    clock = now if now is not None else timephrase.utcnow()
    app = ""
    title = ""
    source = ""
    stamp = ""
    if live is not None:
        app = str(live.subject.app or "").strip()
        title = str(live.subject.title or "").strip()
        source = str(live.source or "").strip()
        stamp = str(live.at or "").strip()
    elif session_row:
        app = str(session_row.get("app") or "").strip()
        title = str(session_row.get("title") or "").strip()
        source = str(session_row.get("source") or "").strip()
        stamp = str(
            session_row.get("ended_at") or session_row.get("started_at") or ""
        ).strip()
    age_s = _age_seconds(stamp, clock)
    note = _note(fresh=fresh, enabled=enabled, has_sample=bool(app or source))
    return {
        "fresh": bool(fresh),
        "enabled": bool(enabled),
        "app": app or None,
        "title": title or None,
        "source": source or None,
        "as_of_seconds": age_s,
        "note": note,
    }


def pick_last_session(sessions: list[Mapping[str, Any]] | None) -> dict[str, Any] | None:
    """Prefer the newest foreground session; otherwise the newest row."""
    rows = [row for row in (sessions or ()) if isinstance(row, Mapping)]
    for row in rows:
        if str(row.get("source") or "") == "foreground":
            return dict(row)
    if rows:
        return dict(rows[0])
    return None


def _age_seconds(stamp: str, now: datetime) -> float | None:
    if not stamp:
        return None
    text = stamp.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (now - dt).total_seconds()
    return max(0.0, round(delta, 1))


def _note(*, fresh: bool, enabled: bool, has_sample: bool) -> str:
    if not enabled:
        return "activity awareness is off; last stored session if any"
    if fresh:
        return "live sample"
    if has_sample:
        return "last stored session; collector timed out or no desktop"
    return "no recent desktop activity"
