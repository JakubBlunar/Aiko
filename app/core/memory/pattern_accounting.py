"""Content-free intake counters for the multi-day concept/pattern trial."""
from __future__ import annotations

import json
import logging

log = logging.getLogger("app.conversation_patterns")
PREFIX = "concept.pattern_intake:"


def intake_snapshot(db, owner):
    try:
        value = json.loads(db.kv_get(PREFIX + str(owner)) or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, AttributeError):
        return {}


def count_intake(db, owner, **increments):
    try:
        counts = intake_snapshot(db, owner)
        for name, amount in increments.items():
            counts[name] = int(counts.get(name, 0)) + int(amount)
        db.kv_set(PREFIX + str(owner), json.dumps(counts))
    except (ValueError, TypeError, AttributeError):
        log.debug("pattern intake accounting unavailable", exc_info=True)
