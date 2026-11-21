"""A short visual acknowledgement of a confirmed desktop return."""
from __future__ import annotations

import math
import threading
import uuid

from app.core.activity.evidence import ActivityEvidence
from app.core.live.frame import SLEEP_SPEECH_FORBID, LiveSituationFrame
from app.core.live.main_wake import main_wake_floor_busy


RETURN_GLANCE_MS = 900


class LiveReturnGlance:
    """One pulse per observed away-to-active edge in a Live generation."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._scope: tuple[str, int] | None = None
        self._observed_ms = -1.0
        self._away = False
        self._id = ""
        self._until_ms = 0.0

    def observe(self, frame: LiveSituationFrame, evidence: ActivityEvidence | None) -> None:
        with self._lock:
            now = frame.monotonic_ms
            if now < self._observed_ms:
                return
            self._observed_ms = now
            scope = (frame.session_id, frame.mode_generation)
            if scope != self._scope:
                self._scope = scope
                self._away = False
                self._id = ""
                return

            if evidence is None or not evidence.present:
                self._away = False
                self._id = ""
                return
            if evidence.os_idle in {"idle", "locked"}:
                self._away = self._away or not evidence.stale
                self._id = ""
                return
            returning, self._away = self._away, False
            if (
                evidence.stale
                or evidence.source != "foreground"
                or evidence.os_idle != "active"
                or frame.constraints.dnd
                or frame.constraints.sleep_status in SLEEP_SPEECH_FORBID
                or main_wake_floor_busy(frame)
                or frame.interaction.playback_active
            ):
                self._id = ""
                return
            if returning:
                self._id = uuid.uuid4().hex
                self._until_ms = now + RETURN_GLANCE_MS

    def payload(self, now_ms: float, mode_generation: int) -> dict[str, str | int] | None:
        with self._lock:
            remaining = math.ceil(self._until_ms - now_ms)
            if (
                not self._id or remaining <= 0
                or self._scope is None or self._scope[1] != mode_generation
            ):
                return None
            return {"id": self._id, "remaining_ms": min(RETURN_GLANCE_MS, remaining)}

    def clear(self) -> None:
        with self._lock:
            self._scope = None
            self._away = False
            self._id = ""
