"""Activity-awareness collection pipeline (C6 phases 1-2).

Desktop sources push versioned envelopes over the existing WebSocket.
This package redacts, stores, and sessionizes them. Interpretation,
cues, memories, UIA, and a live tool-pass pull are later consumers of
the same store — they are not stubbed here.
"""
from __future__ import annotations

from app.core.activity.envelope import ActivityEnvelope, parse_envelope
from app.core.activity.handlers import KNOWN_SOURCES, redact
from app.core.activity.store import ActivityStore


__all__ = [
    "ActivityEnvelope",
    "ActivityStore",
    "KNOWN_SOURCES",
    "parse_envelope",
    "redact",
]
