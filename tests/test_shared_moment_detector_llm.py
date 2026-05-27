"""Track-2 ``MomentDetector`` gating logic.

These tests use a mock Ollama client so we can exercise the
cadence/cooldown/signal gates without making a real LLM call.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.core.shared_moment_extractor import MomentDetector


def _make(min_turn_gap: int = 2, cooldown: float = 0.0) -> tuple[MomentDetector, MagicMock]:
    ollama = MagicMock()
    persisted: list = []
    det = MomentDetector(
        ollama=ollama,
        model="test-model",
        persist_callback=persisted.append,
        min_turn_gap=min_turn_gap,
        cooldown_seconds=cooldown,
    )
    det._persisted = persisted  # type: ignore[attr-defined]
    return det, ollama


class TestGating(unittest.TestCase):
    def test_no_signal_blocks_run(self) -> None:
        det, ollama = _make(min_turn_gap=1)
        det.notify_user_turn()
        det.notify_user_turn()
        self.assertFalse(
            det.should_run_llm(
                reaction_signal=False,
                milestone_signal=False,
                gift_signal=False,
                promise_kept_signal=False,
                now_monotonic=10.0,
            )
        )

    def test_cadence_blocks_until_min_turn_gap(self) -> None:
        det, _ = _make(min_turn_gap=3)
        det.notify_user_turn()
        # Only one turn since boot — below min_turn_gap=3.
        self.assertFalse(
            det.should_run_llm(
                reaction_signal=True,
                milestone_signal=False,
                gift_signal=False,
                promise_kept_signal=False,
                now_monotonic=5.0,
            )
        )
        det.notify_user_turn()
        det.notify_user_turn()
        self.assertTrue(
            det.should_run_llm(
                reaction_signal=True,
                milestone_signal=False,
                gift_signal=False,
                promise_kept_signal=False,
                now_monotonic=5.0,
            )
        )

    def test_cooldown_blocks_back_to_back_runs(self) -> None:
        det, ollama = _make(min_turn_gap=1, cooldown=120.0)
        ollama.chat.return_value = (
            '{"moment": {"summary": "we laughed about cookies", "vibe": "playful"}}'
        )
        det.notify_user_turn()
        det.notify_user_turn()
        result1 = det.maybe_run_llm(
            history_provider=lambda: [("user", "ahah okay"), ("assistant", "ha")],
            now_monotonic=10.0,
            reaction_signal=True,
            milestone_signal=False,
            gift_signal=False,
            promise_kept_signal=False,
        )
        self.assertIsNotNone(result1)
        # Cooldown not elapsed -> second call must be blocked, even with signal.
        det.notify_user_turn()
        det.notify_user_turn()
        self.assertFalse(
            det.should_run_llm(
                reaction_signal=True,
                milestone_signal=False,
                gift_signal=False,
                promise_kept_signal=False,
                now_monotonic=15.0,
            )
        )
        # After cooldown elapses it should be allowed again.
        self.assertTrue(
            det.should_run_llm(
                reaction_signal=True,
                milestone_signal=False,
                gift_signal=False,
                promise_kept_signal=False,
                now_monotonic=200.0,
            )
        )

    def test_maybe_run_llm_persists_on_success(self) -> None:
        det, ollama = _make(min_turn_gap=1)
        ollama.chat.return_value = (
            '{"moment": {"summary": "we laughed about the cookie jar", "vibe": "playful"}}'
        )
        det.notify_user_turn()
        det.notify_user_turn()
        result = det.maybe_run_llm(
            history_provider=lambda: [("user", "hahaha"), ("assistant", "okay okay")],
            now_monotonic=0.0,
            reaction_signal=True,
            milestone_signal=False,
            gift_signal=False,
            promise_kept_signal=False,
        )
        self.assertIsNotNone(result)
        self.assertEqual(det._persisted, [result])  # type: ignore[attr-defined]
        self.assertEqual(result.source, "llm")
        # ``when`` is stamped automatically by the detector.
        self.assertTrue(result.when)

        stats = det.stats()
        self.assertEqual(stats["llm_scheduled"], 1)
        self.assertEqual(stats["llm_completed"], 1)
        self.assertEqual(stats["llm_persisted"], 1)

    def test_maybe_run_llm_returns_null_payload(self) -> None:
        det, ollama = _make(min_turn_gap=1)
        ollama.chat.return_value = '{"moment": null}'
        det.notify_user_turn()
        det.notify_user_turn()
        result = det.maybe_run_llm(
            history_provider=lambda: [("user", "what's 2+2"), ("assistant", "four")],
            now_monotonic=0.0,
            reaction_signal=True,
            milestone_signal=False,
            gift_signal=False,
            promise_kept_signal=False,
        )
        self.assertIsNone(result)
        stats = det.stats()
        self.assertEqual(stats["llm_returned_null"], 1)
        self.assertEqual(stats["llm_persisted"], 0)

    def test_no_signal_increments_skip_counter(self) -> None:
        det, ollama = _make(min_turn_gap=1)
        det.notify_user_turn()
        det.notify_user_turn()
        result = det.maybe_run_llm(
            history_provider=lambda: [("user", "yo"), ("assistant", "yo back")],
            now_monotonic=0.0,
            reaction_signal=False,
            milestone_signal=False,
            gift_signal=False,
            promise_kept_signal=False,
        )
        self.assertIsNone(result)
        ollama.chat.assert_not_called()
        self.assertEqual(det.stats()["llm_skipped_no_signal"], 1)


if __name__ == "__main__":
    unittest.main()
