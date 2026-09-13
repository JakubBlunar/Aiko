"""Activity-awareness collection pipeline (C6 phases 1-5).

Desktop sources push versioned envelopes over the existing WebSocket.
This package redacts, stores, sessionizes, rollups, and interprets them.
Live and K72 read privacy-safe evidence from the same store (app name,
idle/lock, duration, session counts) — not titles. Level-2 readings live
in kv. Level-3 companion intake is a pooled ``companion_activity`` cue
(TurnRunner only; Live peeks, never takes).
"""
from __future__ import annotations

from app.core.activity.companion_cue_worker import (
    CompanionActivityWorker,
    decide_companion_activity,
)
from app.core.activity.envelope import ActivityEnvelope, parse_envelope
from app.core.activity.evidence import (
    ActivityEvidence,
    compute_activity_rollup,
    evidence_from_store,
    long_focus_dates_from_sessions,
)
from app.core.activity.handlers import KNOWN_SOURCES, redact
from app.core.activity.interpretation_worker import (
    load_activity_interpretation,
)
from app.core.activity.pull import format_activity_view, pick_last_session
from app.core.activity.store import ActivityStore


__all__ = [
    "ActivityEnvelope",
    "ActivityEvidence",
    "ActivityStore",
    "CompanionActivityWorker",
    "KNOWN_SOURCES",
    "compute_activity_rollup",
    "decide_companion_activity",
    "evidence_from_store",
    "format_activity_view",
    "load_activity_interpretation",
    "long_focus_dates_from_sessions",
    "parse_envelope",
    "pick_last_session",
    "redact",
]
