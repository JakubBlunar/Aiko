"""Ingest a client envelope: parse, redact, persist. Optional and stale."""
from __future__ import annotations

import logging
from typing import Any

from app.core.activity.envelope import ActivityEnvelope, parse_envelope
from app.core.activity.handlers import redact
from app.core.activity.store import ActivityStore

log = logging.getLogger("app.activity.ingest")


def ingest_envelope(
    raw: Any,
    *,
    settings: Any,
    store: ActivityStore | None,
) -> ActivityEnvelope | None:
    """Redact-then-write. Unknown ``v`` / unknown ``source`` are dropped.

    Failures are debug-logged and swallowed so a hostile or malformed
    sample cannot stall a turn or the WebSocket loop.
    """
    try:
        envelope = parse_envelope(raw)
    except Exception:
        log.debug("activity envelope parse failed", exc_info=True)
        return None
    if envelope is None:
        return None
    try:
        redacted = redact(envelope, settings)
    except Exception:
        log.debug("activity redact failed source=%s", envelope.source, exc_info=True)
        return None
    if redacted is None:
        log.debug("activity envelope dropped source=%s", envelope.source)
        return None
    if store is None:
        return redacted
    try:
        store.add_event(redacted)
    except Exception:
        log.debug("activity persist failed", exc_info=True)
    return redacted
