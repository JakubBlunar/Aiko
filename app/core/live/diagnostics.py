"""Privacy-safe Live diagnostic dumps. No titles, transcripts, or journal bodies."""
from __future__ import annotations

import hashlib
import json
import logging
import marshal
import threading
import uuid
from collections import deque
from copy import deepcopy
from typing import Any

from app.core.infra import timephrase


_PROCESS_INSTANCE = uuid.uuid4().hex
_TRACE_LOG = logging.getLogger("app.live")


def code_digest(*values: Any) -> str:
    digest = hashlib.sha256()
    for value in values:
        code = getattr(value, "__code__", None)
        digest.update(marshal.dumps(code) if code is not None else str(value).encode())
    return digest.hexdigest()


class LiveDecisionTrace:
    """Content-free decision tail; the rotating app log retains completed records."""

    def __init__(self, *, code_sha256: str, prompt_version: int) -> None:
        self.identity = {
            "process_instance": _PROCESS_INSTANCE,
            "controller_instance": uuid.uuid4().hex,
            "decision_code_sha256": code_sha256,
            "prompt_version": prompt_version,
            "trace_version": 1,
        }
        self._rows: deque[dict[str, Any]] = deque(maxlen=64)
        self._lock = threading.Lock()

    def begin(self, *, generation: int, trigger: str) -> dict[str, Any]:
        return {
            **self.identity,
            "decision_id": uuid.uuid4().hex,
            "started_at": timephrase.utcnow().isoformat(),
            "generation": generation,
            "trigger": trigger,
            "menu": [],
            "inference_submitted": False,
            "inference_completed": False,
            "intent": "",
            "selected_urge_id": "",
            "reason": "model_failure",
            "executed": False,
        }

    def finish(self, row: dict[str, Any]) -> None:
        row = {**row, "finished_at": timephrase.utcnow().isoformat()}
        with self._lock:
            self._rows.append(deepcopy(row))
        _TRACE_LOG.info("live decision: %s", json.dumps(row, separators=(",", ":")))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"identity": dict(self.identity), "decisions": deepcopy(list(self._rows))}


_DROP_KEYS = frozenset({
    "title",
    "text",
    "draft",
    "audio",
    "pcm",
    "wav",
    "pointer",
    "cursor",
    "transcript",
    "situation_summary",
    "raw_preview",
})


def sanitize_live_dump(value: Any) -> Any:
    """Strip private payload fields while keeping counts, intents, app names."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            token = str(key)
            if token in _DROP_KEYS:
                continue
            out[token] = sanitize_live_dump(item)
        return out
    if isinstance(value, (list, tuple)):
        return [sanitize_live_dump(item) for item in value]
    return value
