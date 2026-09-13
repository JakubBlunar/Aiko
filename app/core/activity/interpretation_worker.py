"""LLM-lane Level-2 activity interpretation (C6 phase 4).

Triggered by Level-1's change signal (a newer rollup watermark), not a
clock. Follows H25's shape: local worker model, ``LlmPriorityGate`` via
the maintenance client, off the turn path, and must be able to return
nothing. A reading without a confidence is dropped.

Allowlisted titles may enter the *prompt*. They never persist here, never
enter Live 4B, and a title that leaks into the reading is treated as
nothing. No MemoryStore, no cue, no UIA.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from app.core.activity.aggregation_worker import KV_ROLLUP, KV_SESSION_ID
from app.core.infra import timephrase
from app.core.proactive.idle_worker import WorkSignal

if TYPE_CHECKING:
    from app.core.activity.store import ActivityStore
    from app.llm.chat_client import ChatClient

log = logging.getLogger("app.activity.interpretation_worker")

_INTERVAL_SECONDS = 90.0
_SATURATION_SESSIONS = 8
_MIN_CONFIDENCE = 0.6
_MAX_READING_CHARS = 200
_MAX_SESSIONS = 12
_MAX_TOKENS = 160
_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)

KV_INTERP_SESSION_ID = "activity.interpretation.last_session_id"
KV_INTERP = "activity.interpretation.json"

KINDS = frozenset({"coding", "media", "idle", "other"})

INTERPRETATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reading": {"type": "string"},
        "kind": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["reading", "confidence"],
}

_SYSTEM_PROMPT = (
    "You interpret coarse desktop activity for Aiko. You see app names, "
    "durations, idle/lock spans, and window titles only for allowlisted apps. "
    "Titles may still be sensitive — never copy a title, filename, URL, or "
    "document name into the reading.\n"
    "Return ONE JSON object and nothing else:\n"
    '{"reading": "short present-tense observation or empty string", '
    '"kind": "coding|media|idle|other|", "confidence": 0.0-1.0}\n'
    "If the evidence is thin, ambiguous, or you might be wrong (gaming vs "
    "a tutorial, browsing vs working), return "
    '{"reading": "", "kind": "", "confidence": 0.0}. '
    "Never invent a project, document, person, or emotion. "
    + timephrase.LIVE_STATE_TIME_RULE
)


def load_activity_interpretation(
    kv_get: Callable[[str], str | None] | None,
) -> dict[str, Any] | None:
    """Privacy-safe kv blob. Never raises; never returns a title."""
    if kv_get is None:
        return None
    try:
        raw = kv_get(KV_INTERP)
    except Exception:
        return None
    if not raw:
        return None
    try:
        blob = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    blob.pop("title", None)
    return blob


def parse_interpretation_payload(content: Any) -> dict[str, Any] | None:
    """Extract ``reading`` / ``kind`` / ``confidence``. None if unparseable."""
    blob: Any = content
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return None
        match = _JSON_OBJECT_RE.search(text)
        if match is not None:
            text = match.group(0)
        try:
            blob = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(blob, dict):
        return None
    if "confidence" not in blob:
        return None
    try:
        confidence = float(blob["confidence"])
    except (TypeError, ValueError):
        return None
    kind = str(blob.get("kind") or "").strip().lower()
    if kind not in KINDS:
        kind = ""
    reading = " ".join(str(blob.get("reading") or "").split())
    return {
        "reading": reading[:_MAX_READING_CHARS],
        "kind": kind,
        "confidence": max(0.0, min(1.0, confidence)),
    }


def decide_interpretation(
    parsed: Mapping[str, Any] | None,
    *,
    titles: Sequence[str] = (),
    min_confidence: float = _MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Drop a reading that lacks confidence, is weak, or echoes a title."""
    if parsed is None:
        return _nothing("invalid")
    try:
        confidence = float(parsed["confidence"])
    except (KeyError, TypeError, ValueError):
        return _nothing("invalid")
    reading = str(parsed.get("reading") or "").strip()
    kind = str(parsed.get("kind") or "").strip().lower()
    if kind not in KINDS:
        kind = ""
    if not reading:
        return _nothing("empty", confidence=confidence)
    if confidence < float(min_confidence):
        return _nothing("low_confidence", confidence=confidence, kind=kind)
    lowered = reading.lower()
    for title in titles:
        token = str(title or "").strip()
        if token and token.lower() in lowered:
            return _nothing("title_leak", confidence=confidence, kind=kind)
    return {
        "reading": reading[:_MAX_READING_CHARS],
        "kind": kind,
        "confidence": confidence,
        "nothing": False,
        "reason": "ok",
    }


def format_interpretation_prompt(
    rollup: Mapping[str, Any],
    sessions: Sequence[Mapping[str, Any]] | None,
    *,
    now: datetime | None = None,
) -> str:
    """Age-tagged rollup + sessions. Titles stay in this prompt only."""
    clock = now if now is not None else timephrase.utcnow()
    lines = [
        timephrase.today_anchor(clock),
        "",
        "Level-1 rollup:",
        "app=%s session_count=%s idle_span_seconds=%s lock_span_seconds=%s"
        % (
            str(rollup.get("app") or "-"),
            int(rollup.get("session_count") or 0),
            int(rollup.get("idle_span_seconds") or 0),
            int(rollup.get("lock_span_seconds") or 0),
        ),
        "",
        "Recent sessions (newest first):",
    ]
    count = 0
    for row in sessions or ():
        if not isinstance(row, Mapping):
            continue
        lines.append(_session_line(row, clock))
        count += 1
        if count >= _MAX_SESSIONS:
            break
    if count == 0:
        lines.append("- none")
    return "\n".join(lines)


def session_titles(sessions: Sequence[Mapping[str, Any]] | None) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for row in sessions or ():
        if not isinstance(row, Mapping):
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(title)
    return tuple(out)


class ActivityInterpretationWorker:
    """IdleWorker whose ``demand()`` is a newer Level-1 rollup watermark."""

    name = "activity_interpretation"

    def __init__(
        self,
        store: "ActivityStore",
        *,
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        interval_seconds: float = _INTERVAL_SECONDS,
        min_confidence: float = _MIN_CONFIDENCE,
    ) -> None:
        self._store = store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._ollama = ollama
        self._model = str(model or "").strip()
        self._interval = max(30.0, float(interval_seconds))
        self._min_confidence = float(min_confidence)

    @property
    def interval_seconds(self) -> float:
        return self._interval

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        del now, last_run_at
        return self._store is not None

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal | None:
        del now, last_run_at
        if self._store is None:
            return WorkSignal(pressure=0.0, reason="no activity store")
        if self._ollama is None or not self._model:
            return WorkSignal(pressure=0.0, reason="no worker model")
        rollup_id = self._int_kv(KV_SESSION_ID)
        if rollup_id <= 0:
            return WorkSignal(pressure=0.0, reason="no activity rollup")
        watermark = self._int_kv(KV_INTERP_SESSION_ID)
        if rollup_id <= watermark:
            return WorkSignal(pressure=0.0, reason="no new activity rollup")
        delta = rollup_id - watermark
        return WorkSignal(
            pressure=min(1.0, delta / float(_SATURATION_SESSIONS)),
            reason="%d new rolled-up sessions" % delta,
            needs_llm=True,
        )

    def run(self) -> dict[str, Any] | None:
        if self._store is None:
            return None
        if self._ollama is None or not self._model:
            return None
        rollup = self._load_rollup()
        if rollup is None:
            return None
        try:
            rollup_id = int(rollup.get("last_session_id") or 0)
        except (TypeError, ValueError):
            rollup_id = 0
        if rollup_id <= self._int_kv(KV_INTERP_SESSION_ID):
            return None
        try:
            sessions = self._store.recent_sessions(limit=_MAX_SESSIONS)
        except Exception:
            log.debug("activity interpret sessions failed", exc_info=True)
            sessions = []
        now = timephrase.utcnow()
        prompt = format_interpretation_prompt(rollup, sessions, now=now)
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0.2, "num_predict": _MAX_TOKENS},
                format_json=True,
                json_schema=INTERPRETATION_JSON_SCHEMA,
                think=False,
                surface="activity_interpret",
            )
        except Exception:
            log.debug("activity interpret llm failed", exc_info=True)
            return None
        decided = decide_interpretation(
            parse_interpretation_payload(content),
            titles=session_titles(sessions),
            min_confidence=self._min_confidence,
        )
        payload = {
            "last_session_id": rollup_id,
            "app": str(rollup.get("app") or "").strip(),
            "reading": decided["reading"] if not decided["nothing"] else "",
            "kind": decided["kind"] if not decided["nothing"] else "",
            "confidence": float(decided["confidence"]),
            "nothing": bool(decided["nothing"]),
            "reason": str(decided["reason"]),
            "computed_at": now.isoformat(timespec="seconds"),
        }
        payload.pop("title", None)
        try:
            self._kv_set(KV_INTERP, json.dumps(payload, ensure_ascii=False))
            self._kv_set(KV_INTERP_SESSION_ID, str(rollup_id))
        except Exception:
            log.debug("activity interpret persist failed", exc_info=True)
            return None
        log.info(
            "activity interpret: nothing=%s confidence=%.2f kind=%s app=%s "
            "reason=%s",
            payload["nothing"],
            payload["confidence"],
            payload["kind"] or "-",
            payload["app"] or "-",
            payload["reason"],
        )
        return payload

    def _load_rollup(self) -> dict[str, Any] | None:
        try:
            raw = self._kv_get(KV_ROLLUP)
        except Exception:
            return None
        if not raw:
            return None
        try:
            blob = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(blob, dict):
            return None
        blob.pop("title", None)
        return blob

    def _int_kv(self, key: str) -> int:
        try:
            raw = self._kv_get(key)
        except Exception:
            return 0
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0


def _session_line(row: Mapping[str, Any], now: datetime) -> str:
    started = str(row.get("started_at") or row.get("ended_at") or "")
    age = timephrase.age_prefix(started, now) or "unknown age"
    source = str(row.get("source") or "-").strip() or "-"
    app = str(row.get("app") or "-").strip() or "-"
    try:
        duration = max(0, int(float(row.get("duration_seconds") or 0)))
    except (TypeError, ValueError):
        duration = 0
    line = "- [%s] source=%s app=%s duration=%ss" % (age, source, app, duration)
    title = str(row.get("title") or "").strip()
    if title:
        line += " title=%s" % title
    return line


def _nothing(
    reason: str,
    *,
    confidence: float = 0.0,
    kind: str = "",
) -> dict[str, Any]:
    return {
        "reading": "",
        "kind": kind,
        "confidence": max(0.0, min(1.0, float(confidence))),
        "nothing": True,
        "reason": reason,
    }
