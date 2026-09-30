"""Wall-clock delivery of persistent one-shot reminder notifications."""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from app.core.goals.reminders import ReminderStore


log = logging.getLogger("app.reminders")


class ReminderDispatcher:
    def __init__(
        self, store: ReminderStore, session_id: Callable[[], str],
        notify: Callable[[object], None], *, interval_seconds: float = 30.0,
    ) -> None:
        self.store = store
        self.session_id = session_id
        self.notify = notify
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_due(self) -> int:
        delivered = 0
        for _ in range(100):
            row = self.store.deliver_next(self.session_id())
            if row is None:
                break
            delivered += 1
            try:
                self.notify(row)
            except Exception:
                log.exception("Reminder notification broadcast failed; chat row is persisted")
        return delivered

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="reminder-dispatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:
                log.exception("Reminder delivery failed; pending rows will be retried")
            self._stop.wait(self.interval_seconds)
