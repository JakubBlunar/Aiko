"""K20 -- Metacognitive calibration store.

Tracks Jacob's calibration signal toward Aiko's claims as a per-user
``CalibrationState``: a global trust scalar plus a small bounded ring
of topic slots (each carrying a centroid + score + timestamp +
signal_count). The state is the *write side* of K20; the read side
lives in :mod:`app.core.calibration_detector` (regex + cosine
detection, decay, and the inner-life cue renderer).

Persistence shape mirrors K13's ``user_style_signal``: one JSON blob
per user keyed by ``user_id`` so we can extend the payload shape
without a column migration. The blob is hand-encoded (no float32
arrays) because SQLite can't round-trip numpy buffers efficiently and
the centroid list is small (default cap = 8 slots * embedder DIM).

K20 deliberately does NOT touch RAG retrieval scores -- F3
(``memory.confidence`` + ``(uncertain)`` suffix) already owns the
per-memory accuracy lane. K20 is the *per-user / per-topic register
tilt* on top of it.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np


log = logging.getLogger("app.calibration_store")


# Module-level defaults so tests can construct without a settings stub
# and so the store can return a sensible baseline when no row exists
# yet for the user. Production code uses the values from
# :class:`app.core.settings.MemorySettings`.
_DEFAULT_BASELINE = 0.80
_DEFAULT_MAX_TOPIC_SLOTS = 8


# ── Data types ──────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class TopicSlot:
    """One bounded topic slot: a centroid embedding + the running
    calibration score for memories that fall near it.

    ``centroid`` is a unit-norm float vector with the same DIM as the
    embedder. The slot is "near" an assistant_text vector when their
    cosine similarity exceeds the configured merge threshold.
    """

    centroid: np.ndarray
    score: float
    last_signal_at: datetime
    signal_count: int


@dataclass(slots=True, frozen=True)
class CalibrationState:
    """Per-user calibration snapshot.

    Returned by :meth:`CalibrationStore.get`, consumed by
    :func:`app.core.calibration_detector.apply_signal` /
    :func:`app.core.calibration_detector.decay` /
    :func:`app.core.calibration_detector.render_inner_life_block`.
    Frozen so updates always create a fresh instance -- the store's
    ``upsert`` is the only place state mutates.
    """

    global_score: float
    last_updated_at: datetime | None
    topics: tuple[TopicSlot, ...]


def baseline_state(*, baseline: float = _DEFAULT_BASELINE) -> CalibrationState:
    """Return the cold-start state used when no row exists for the
    user. ``baseline`` is the trust-anchored default the global score
    decays toward."""
    return CalibrationState(
        global_score=float(baseline),
        last_updated_at=None,
        topics=tuple(),
    )


# ── JSON round-trip helpers ─────────────────────────────────────────


def _state_to_json(state: CalibrationState) -> str:
    payload: dict[str, Any] = {
        "global_score": float(state.global_score),
        "last_updated_at": (
            state.last_updated_at.isoformat()
            if state.last_updated_at is not None
            else None
        ),
        "topics": [
            {
                "centroid": [float(x) for x in slot.centroid.tolist()],
                "score": float(slot.score),
                "last_signal_at": slot.last_signal_at.isoformat(),
                "signal_count": int(slot.signal_count),
            }
            for slot in state.topics
        ],
    }
    return json.dumps(payload, separators=(",", ":"))


def _state_from_json(
    raw: str | None,
    *,
    baseline: float,
) -> CalibrationState:
    """Parse a persisted blob best-effort; fall back to baseline on
    any malformed input. Never raises -- a broken DB row should not
    bring the post-turn pipeline down."""
    if not raw:
        return baseline_state(baseline=baseline)
    try:
        data = json.loads(raw)
    except Exception:
        log.debug("calibration_store: malformed JSON", exc_info=True)
        return baseline_state(baseline=baseline)
    if not isinstance(data, dict):
        return baseline_state(baseline=baseline)

    global_score = _coerce_float(
        data.get("global_score"), default=baseline, lo=0.0, hi=1.0,
    )
    last_updated_at = _coerce_dt(data.get("last_updated_at"))
    raw_topics = data.get("topics") or []
    if not isinstance(raw_topics, list):
        raw_topics = []
    topics: list[TopicSlot] = []
    for row in raw_topics:
        if not isinstance(row, dict):
            continue
        centroid_raw = row.get("centroid") or []
        if not isinstance(centroid_raw, list) or not centroid_raw:
            continue
        try:
            centroid = np.asarray(
                [float(x) for x in centroid_raw], dtype=np.float32,
            )
        except (TypeError, ValueError):
            continue
        norm = float(np.linalg.norm(centroid))
        if norm <= 0.0:
            continue
        if abs(norm - 1.0) > 1e-3:
            centroid = centroid / norm
        slot_score = _coerce_float(
            row.get("score"), default=baseline, lo=0.0, hi=1.0,
        )
        last_signal = _coerce_dt(row.get("last_signal_at"))
        if last_signal is None:
            continue
        try:
            signal_count = max(0, int(row.get("signal_count", 0)))
        except (TypeError, ValueError):
            signal_count = 0
        topics.append(
            TopicSlot(
                centroid=centroid,
                score=slot_score,
                last_signal_at=last_signal,
                signal_count=signal_count,
            )
        )
    return CalibrationState(
        global_score=global_score,
        last_updated_at=last_updated_at,
        topics=tuple(topics),
    )


def _coerce_float(
    value: Any, *, default: float, lo: float, hi: float,
) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return float(default)
    return max(lo, min(hi, f))


def _coerce_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Store ───────────────────────────────────────────────────────────


class CalibrationStore:
    """SQLite read / UPSERT for the ``user_calibration_state`` table.

    Mirrors :class:`app.core.style_signal.StyleSignalStore`: a tiny
    adapter around ``ChatDatabase`` that round-trips a JSON blob
    keyed by ``user_id``. The blob shape is encoded by
    :func:`_state_to_json` / :func:`_state_from_json` so future
    additions to ``CalibrationState`` don't need a column migration.

    All methods swallow per-call exceptions and log at DEBUG -- a
    broken row must not crash the post-turn pipeline. ``get`` returns
    a baseline state on any failure so the detector can proceed.
    """

    def __init__(self, db: Any, *, baseline: float = _DEFAULT_BASELINE) -> None:
        self._db = db
        self._baseline = float(baseline)

    # ── public API ────────────────────────────────────────────────

    def get(self, user_id: str) -> CalibrationState:
        if not user_id:
            return baseline_state(baseline=self._baseline)
        try:
            row = self._db.execute_fetchone(
                "SELECT state_json FROM user_calibration_state "
                "WHERE user_id = ?",
                (user_id,),
            )
        except sqlite3.Error:
            log.debug("calibration_store: get failed", exc_info=True)
            return baseline_state(baseline=self._baseline)
        if row is None:
            return baseline_state(baseline=self._baseline)
        return _state_from_json(row[0], baseline=self._baseline)

    def upsert(self, user_id: str, state: CalibrationState) -> None:
        if not user_id:
            return
        try:
            blob = _state_to_json(state)
            self._db.execute_commit(
                "INSERT INTO user_calibration_state "
                "(user_id, state_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "state_json = excluded.state_json, "
                "updated_at = excluded.updated_at",
                (
                    user_id,
                    blob,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        except sqlite3.Error:
            log.debug("calibration_store: upsert failed", exc_info=True)
        except Exception:
            log.debug(
                "calibration_store: upsert encode failed", exc_info=True,
            )

    def reset(self, user_id: str) -> None:
        """Delete the per-user row. ``get`` then returns the baseline
        state. Useful for the MCP debug surface and for testing."""
        if not user_id:
            return
        try:
            self._db.execute_commit(
                "DELETE FROM user_calibration_state WHERE user_id = ?",
                (user_id,),
            )
        except sqlite3.Error:
            log.debug("calibration_store: reset failed", exc_info=True)


__all__ = [
    "CalibrationState",
    "CalibrationStore",
    "TopicSlot",
    "baseline_state",
]
