"""Bounded, content-free assembly receipts, joinable by assistant message ID.

This is diagnostic state, not cue supply or an outcome/truth label. Existing
cue, block, delivery and answer ledgers retain their own meanings.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from app.core.infra import timephrase

PREFIX = "diagnostic.surfacing."
PREFIX_END = PREFIX + "~"
MAX_TURNS = 128
MAX_ATTEMPTS = 2
log = logging.getLogger(__name__)


def _word(value):
    return str(value or "")[:160]


def _names(value):
    return [_word(item) for item in value[:160]] if isinstance(value, list) else []


def compact_trace(attempts):
    """Explicit field allowlist: never persist rendered text or provider messages."""
    result = []
    for attempt in attempts[-MAX_ATTEMPTS:]:
        choice = attempt.get("optional_choice") or {}
        handling = attempt.get("handling") or {}
        claims = attempt.get("claims") or []
        result.append({
            "attempt_id": _word(attempt.get("attempt_id")),
            "decision": {key: _word(attempt.get("decision", {}).get(key))
                         for key in ("stance", "reason")},
            "optional_choice": {
                "winner": _word(choice.get("winner")),
                "focused_block": _word(choice.get("focused_block")),
                "candidates": _names(choice.get("candidates")),
            },
            "declines": {str(key)[:160]: _word(value)
                         for key, value in list((attempt.get("declines") or {}).items())[:160]},
            "providers": {
                str(key)[:160]: {field: _word(value.get(field)) for field in ("state", "error")}
                for key, value in list((attempt.get("providers") or {}).items())[:160]
                if isinstance(value, Mapping)
            },
            "blocks": {str(key)[:160]: _word(value)
                       for key, value in list((attempt.get("blocks") or {}).items())[:160]},
            "handling": {"included": bool(handling.get("included")),
                         "bundles_omitted": _names(handling.get("bundles_omitted"))},
            "claims": [{
                "cue_id": int(claim["cue_id"]), "cue_type": _word(claim.get("cue_type")),
                "state": _word(claim.get("state")),
                "source_revision": _word(claim.get("source_revision")),
                "blocks": _names(claim.get("blocks")),
            } for claim in claims[:64]],
            "claims_omitted": max(0, len(claims) - 64),
        })
    return {"version": 1, "attempts": result,
            "attempts_omitted": max(0, len(attempts) - MAX_ATTEMPTS)}


def record_trace(db, assistant_message_id, attempts):
    """Replace a turn receipt atomically; retain the newest 128 message IDs.

    Separate keys avoid a read/modify/write race between concurrent sessions.
    All writes use the caller's database, including its test redirect. Failures
    cannot prevent a reply or roll back the already-recorded prompt blocks.
    """
    if not attempts or not assistant_message_id:
        return False
    try:
        if int(assistant_message_id) <= 0:
            return False
        payload = compact_trace(attempts)
        payload["assistant_message_id"] = int(assistant_message_id)
        conn = db._get_conn()
        with conn:
            conn.execute(
                "INSERT INTO kv_meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (f"{PREFIX}{int(assistant_message_id):020d}", json.dumps(payload),
                 timephrase.utcnow().isoformat()),
            )
            conn.execute(
                "DELETE FROM kv_meta WHERE key IN (SELECT key FROM kv_meta "
                "WHERE key >= ? AND key < ? ORDER BY key DESC LIMIT -1 OFFSET ?)",
                (PREFIX, PREFIX_END, MAX_TURNS),
            )
        return True
    except Exception:
        log.warning("surfacing trace insert failed", exc_info=True)
        return False
