"""Dedicated Live heartbeat. Not idle-gated.

Idle workers skip a live voice session. This tick keeps the situation
frame, affect decay, and vitality clocks moving while Live posture is on.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger("app.live")

DEFAULT_INTERVAL_S = 1.0


class LiveHeartbeat:
    """Daemon loop that calls ``tick`` while Live posture is on."""

    def __init__(
        self,
        tick: Callable[[], None],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._tick = tick
        self._interval_s = max(0.2, float(interval_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._loop,
                name="live-heartbeat",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self, *, timeout: float = 1.5) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and threading.current_thread() is not thread:
            thread.join(timeout=timeout)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.debug("live heartbeat tick failed", exc_info=True)
            self._stop.wait(self._interval_s)
