"""J7 — gift / promise-kept signals must reach the moment scheduler.

Regression guard for the ordering bug where the relationship-axes updater
cleared ``_last_turn_gift_received`` / ``_last_turn_promise_kept`` before
``_maybe_schedule_moment_llm_job`` read them off ``self``, so giving Aiko a
gift or keeping a promise could never seed a shared moment. The fix passes
the signals in as explicit kwargs; these tests pin that contract.
"""
from __future__ import annotations

import unittest
from typing import Any

from app.core.session.speaking_window_jobs_mixin import SpeakingWindowJobsMixin


class _StubDetector:
    def __init__(self) -> None:
        self.should_run_kwargs: dict[str, Any] | None = None

    def should_run_llm(self, **kwargs: Any) -> bool:
        self.should_run_kwargs = kwargs
        # Block the actual job submission — we only care about the signals.
        return False


class _StubScheduler:
    def __init__(self) -> None:
        self.submitted: list[Any] = []

    def submit(self, job: Any) -> None:
        self.submitted.append(job)


class _Host(SpeakingWindowJobsMixin):
    """Minimal host exposing only what the scheduler method touches."""

    def __init__(self) -> None:
        self._moment_detector = _StubDetector()
        self._scheduler = _StubScheduler()
        self.session_key = "user::sess"
        self._last_turn_gift_received = False
        self._last_turn_promise_kept = False

    class _DB:
        def get_messages(self, *_a: Any, **_k: Any) -> list[Any]:
            return []

    _chat_db = _DB()


class MomentSignalForwardingTests(unittest.TestCase):
    def _run(self, **kwargs: Any) -> dict[str, Any]:
        host = _Host()
        host._maybe_schedule_moment_llm_job(
            user_text="hi",
            assistant_text="hey",
            raw_assistant_text="hey",
            milestone=None,
            **kwargs,
        )
        assert host._moment_detector.should_run_kwargs is not None
        return host._moment_detector.should_run_kwargs

    def test_gift_signal_forwarded(self) -> None:
        seen = self._run(gift_signal=True)
        self.assertTrue(seen["gift_signal"])
        self.assertFalse(seen["promise_kept_signal"])

    def test_promise_signal_forwarded(self) -> None:
        seen = self._run(promise_kept_signal=True)
        self.assertTrue(seen["promise_kept_signal"])
        self.assertFalse(seen["gift_signal"])

    def test_defaults_false_when_not_passed(self) -> None:
        seen = self._run()
        self.assertFalse(seen["gift_signal"])
        self.assertFalse(seen["promise_kept_signal"])

    def test_does_not_read_instance_flags(self) -> None:
        # Even if the (already-cleared) instance flags are False, an
        # explicit True kwarg must win — proving the scheduler no longer
        # depends on the instance-flag clearing order.
        host = _Host()
        host._last_turn_gift_received = False
        host._maybe_schedule_moment_llm_job(
            user_text="hi",
            assistant_text="hey",
            raw_assistant_text="hey",
            milestone=None,
            gift_signal=True,
        )
        assert host._moment_detector.should_run_kwargs is not None
        self.assertTrue(host._moment_detector.should_run_kwargs["gift_signal"])


if __name__ == "__main__":
    unittest.main()
