"""Tests for K54 topic appetite — the pure gate walk, the
contribution-share helper, the render, the inner-life provider
plumbing (via a minimal mixin host stub), the K18 ``last_mean``
exposure, and the prompt-assembler slot wiring."""
from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.core.conversation import topic_appetite as tap
from app.core.conversation import wants_ledger as wl
from app.core.conversation.topic_stagnation import TopicStagnationDetector
from app.core.session.inner_life_providers_mixin import (
    InnerLifeProvidersMixin,
)


def _decide(**overrides) -> tap.AppetiteDecision:
    kwargs = dict(
        already_fired=False,
        arc="casual_check_in",
        closeness=0.6,
        comfort=0.6,
        lull_mean=0.08,
        short_reply_share=0.8,
        want_text="tell Jacob about the bees documentary",
        want_pressure=0.6,
        lull_threshold=0.18,
        short_share_threshold=0.6,
        min_want_pressure=0.35,
        min_axes=0.15,
        force=False,
    )
    kwargs.update(overrides)
    return tap.decide(**kwargs)


class ShortReplyShareTests(unittest.TestCase):
    def test_empty_is_none(self) -> None:
        self.assertIsNone(tap.compute_short_reply_share([]))

    def test_share(self) -> None:
        share = tap.compute_short_reply_share(
            [50, 50, 300, 50], short_chars=160,
        )
        self.assertEqual(share, 0.75)

    def test_all_long(self) -> None:
        self.assertEqual(
            tap.compute_short_reply_share([300, 400], short_chars=160),
            0.0,
        )


class DecideTests(unittest.TestCase):
    def test_fires_when_all_gates_pass(self) -> None:
        decision = _decide()
        self.assertTrue(decision.fire)
        self.assertEqual(decision.reason, "fire")

    def test_support_arc_blocks(self) -> None:
        self.assertEqual(_decide(arc="support").reason, "arc_blocked")

    def test_reflection_arc_blocks(self) -> None:
        self.assertEqual(_decide(arc="reflection").reason, "arc_blocked")

    def test_once_per_session(self) -> None:
        self.assertEqual(
            _decide(already_fired=True).reason, "already_fired",
        )

    def test_cold_axes_block(self) -> None:
        self.assertEqual(_decide(closeness=0.0).reason, "axes_cold")
        self.assertEqual(_decide(comfort=-0.5).reason, "axes_cold")
        # Missing axes read as 0.0 -> below the 0.15 floor.
        self.assertEqual(
            _decide(closeness=None, comfort=None).reason, "axes_cold",
        )

    def test_no_lull_blocks(self) -> None:
        self.assertEqual(_decide(lull_mean=None).reason, "no_lull")
        self.assertEqual(_decide(lull_mean=0.40).reason, "no_lull")

    def test_still_contributing_blocks(self) -> None:
        self.assertEqual(
            _decide(short_reply_share=0.2).reason, "still_contributing",
        )
        # Cold start (no replies measured) must read as contributing.
        self.assertEqual(
            _decide(short_reply_share=None).reason, "still_contributing",
        )

    def test_no_offer_blocks(self) -> None:
        self.assertEqual(_decide(want_text=None).reason, "no_offer")
        self.assertEqual(_decide(want_text="  ").reason, "no_offer")
        self.assertEqual(
            _decide(want_pressure=0.1).reason, "no_offer",
        )

    def test_force_bypasses_most_gates(self) -> None:
        decision = _decide(
            already_fired=True,
            closeness=-1.0,
            comfort=-1.0,
            lull_mean=None,
            short_reply_share=None,
            want_pressure=0.0,
            force=True,
        )
        self.assertTrue(decision.fire)

    def test_force_still_blocked_by_arc(self) -> None:
        self.assertEqual(
            _decide(arc="support", force=True).reason, "arc_blocked",
        )

    def test_force_still_needs_offer(self) -> None:
        self.assertEqual(
            _decide(want_text=None, force=True).reason, "no_offer",
        )


class RenderTests(unittest.TestCase):
    def test_copy(self) -> None:
        block = tap.render_block(
            "the bees documentary", user_display_name="Jacob",
        )
        self.assertIn("tapped out", block)
        self.assertIn("the bees documentary", block)
        self.assertIn("Jacob", block)
        self.assertIn("no sighing", block)

    def test_blank_offer_fallback(self) -> None:
        block = tap.render_block("", user_display_name="Jacob")
        self.assertIn("the thing you've been wanting", block)


class StagnationLastMeanTests(unittest.TestCase):
    """K54's read of the K18 detector: ``last_mean`` is a standing
    value refreshed on full-window measured turns and NOT reset by
    skipped (None-distance) turns."""

    def test_none_until_window_full(self) -> None:
        detector = TopicStagnationDetector(
            memory_settings=SimpleNamespace(stagnation_window=3),
        )
        detector.detect(0.1)
        detector.detect(0.1)
        self.assertIsNone(detector.last_mean)
        detector.detect(0.1)
        self.assertAlmostEqual(detector.last_mean, 0.1, places=6)

    def test_survives_unmeasured_turns(self) -> None:
        detector = TopicStagnationDetector(
            memory_settings=SimpleNamespace(stagnation_window=2),
        )
        detector.detect(0.2)
        detector.detect(0.4)
        self.assertAlmostEqual(detector.last_mean, 0.3, places=6)
        detector.detect(None)
        self.assertAlmostEqual(detector.last_mean, 0.3, places=6)

    def test_refreshed_during_cooldown(self) -> None:
        settings = SimpleNamespace(
            stagnation_window=2,
            stagnation_mild_threshold=0.5,
            stagnation_cooldown_turns=4,
        )
        detector = TopicStagnationDetector(memory_settings=settings)
        detector.detect(0.1)
        detector.detect(0.1)  # fires -> cooldown armed
        detector.detect(0.3)  # suppressed by cooldown, but measured
        self.assertAlmostEqual(detector.last_mean, 0.2, places=6)


# ── provider plumbing ───────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Row:
    role: str
    content: str


class _FakeChatDb:
    def __init__(self, rows: list[_Row], ledger_json: str | None) -> None:
        self._rows = rows
        self._ledger_json = ledger_json

    def get_messages(self, session_id: str, *, limit=None):  # noqa: ARG002
        if limit is None:
            return list(self._rows)
        return list(self._rows[-limit:])

    def kv_get(self, key: str):  # noqa: ARG002
        return self._ledger_json


class _FakeAxesStore:
    def __init__(self, closeness: float, comfort: float) -> None:
        self._c = closeness
        self._f = comfort

    def get(self, user_id: str):  # noqa: ARG002
        return SimpleNamespace(closeness=self._c, comfort=self._f)


class _FakeArcStore:
    def __init__(self, arc: str) -> None:
        self._arc = arc

    def get_or_default(self, user_id: str):  # noqa: ARG002
        return SimpleNamespace(arc=self._arc)


def _ledger_json(pressure: float = 0.6) -> str:
    state, added = wl.add_want(
        wl.LedgerState(),
        text="tell Jacob about the bees documentary",
        kind="share",
        source="seed",
        source_ref="seed:1",
        now=datetime.now(timezone.utc),
        initial_pressure=pressure,
    )
    assert added
    return wl.serialize(state)


class _Host(InnerLifeProvidersMixin):
    user_display_name = "Jacob"
    session_key = "s1"
    _user_id = "u1"

    def __init__(
        self,
        *,
        enabled: bool = True,
        arc: str = "casual_check_in",
        closeness: float = 0.6,
        comfort: float = 0.6,
        lull_mean: float | None = 0.08,
        rows: list[_Row] | None = None,
        ledger_json: str | None = None,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                topic_appetite_enabled=enabled,
                appetite_short_reply_chars=160,
                appetite_short_share_threshold=0.6,
                appetite_window=4,
                appetite_min_want_pressure=0.35,
                appetite_min_axes=0.15,
            ),
        )
        self._memory_settings = SimpleNamespace(
            stagnation_mild_threshold=0.18,
        )
        self._arc_store = _FakeArcStore(arc)
        self._relationship_axes_store = _FakeAxesStore(closeness, comfort)
        self._topic_stagnation_detector = SimpleNamespace(
            last_mean=lull_mean,
        )
        if rows is None:
            rows = [_Row("assistant", "mm, nice")] * 4
        if ledger_json is None:
            ledger_json = _ledger_json()
        self._chat_db = _FakeChatDb(rows, ledger_json)


class ProviderTests(unittest.TestCase):
    def test_fires_once_then_latches(self) -> None:
        host = _Host()
        block = host._render_topic_appetite_block()
        self.assertIn("tapped out", block)
        self.assertIn("the bees documentary", block)
        self.assertTrue(host._topic_appetite_fired)
        self.assertEqual(host._render_topic_appetite_block(), "")

    def test_disabled_switch_silent(self) -> None:
        host = _Host(enabled=False)
        self.assertEqual(host._render_topic_appetite_block(), "")

    def test_no_lull_silent(self) -> None:
        host = _Host(lull_mean=None)
        self.assertEqual(host._render_topic_appetite_block(), "")
        host = _Host(lull_mean=0.5)
        self.assertEqual(host._render_topic_appetite_block(), "")

    def test_substantive_replies_silent(self) -> None:
        host = _Host(rows=[_Row("assistant", "x" * 400)] * 4)
        self.assertEqual(host._render_topic_appetite_block(), "")

    def test_too_few_replies_silent(self) -> None:
        # Window is 4; only 2 assistant rows -> cold start, no fire.
        host = _Host(rows=[_Row("assistant", "mm")] * 2)
        self.assertEqual(host._render_topic_appetite_block(), "")

    def test_cold_axes_silent(self) -> None:
        host = _Host(closeness=0.0, comfort=0.0)
        self.assertEqual(host._render_topic_appetite_block(), "")

    def test_empty_ledger_silent(self) -> None:
        host = _Host(ledger_json=wl.serialize(wl.LedgerState()))
        self.assertEqual(host._render_topic_appetite_block(), "")

    def test_low_pressure_silent(self) -> None:
        host = _Host(ledger_json=_ledger_json(pressure=0.1))
        self.assertEqual(host._render_topic_appetite_block(), "")

    def test_support_arc_silent(self) -> None:
        host = _Host(arc="support")
        self.assertEqual(host._render_topic_appetite_block(), "")

    def test_force_bypasses_gates(self) -> None:
        host = _Host(
            lull_mean=None,
            closeness=-1.0,
            comfort=-1.0,
            rows=[_Row("assistant", "x" * 400)] * 4,
        )
        host._topic_appetite_force_next = True
        block = host._render_topic_appetite_block()
        self.assertIn("tapped out", block)
        # The force flag is one-shot.
        self.assertFalse(host._topic_appetite_force_next)

    def test_user_rows_ignored_in_share(self) -> None:
        # Plenty of short user rows must not count toward the share.
        rows = [_Row("user", "ok")] * 10 + [_Row("assistant", "mm")] * 4
        host = _Host(rows=rows)
        self.assertIn(
            "tapped out", host._render_topic_appetite_block(),
        )


class TopicAppetiteProviderSlotTests(unittest.TestCase):
    """K54 block lands in the system prompt directly under the K52
    wants block, and IS dropped under ``aggressive=True`` (same
    posture as wants/curiosity — it's a permission slip)."""

    _CUE = "Honest read: this topic has been circling for a while"

    def _assemble(self, *, aggressive: bool = False, **providers):
        from app.core.infra.chat_database import ChatDatabase
        from app.core.session.prompt_assembler import PromptAssembler

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = ChatDatabase(Path(tmp.name) / "chat.db")
        self.addCleanup(lambda: db._get_conn().close())
        persona = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        )
        persona.write("P")
        persona.close()
        assembler = PromptAssembler(
            db, persona_path=Path(persona.name), recent_window=20,
        )
        db.add_message(
            session_id="a1", role="user", content="hi", token_count=2,
        )
        assembler.set_inner_life_providers(**providers)
        messages, _ = assembler.assemble_with_budget(
            "a1", "hello there",
            context_window=4096, response_budget=256,
            aggressive=aggressive,
        )
        return messages[0]["content"]

    def test_block_lands_in_system_prompt(self) -> None:
        content = self._assemble(topic_appetite=lambda: self._CUE)
        self.assertIn(self._CUE, content)

    def test_sits_after_wants(self) -> None:
        wants_cue = "Things you've been wanting from a conversation"
        content = self._assemble(
            wants=lambda: wants_cue,
            topic_appetite=lambda: self._CUE,
        )
        self.assertLess(
            content.index(wants_cue), content.index(self._CUE),
        )

    def test_dropped_under_aggressive(self) -> None:
        content = self._assemble(
            topic_appetite=lambda: self._CUE, aggressive=True,
        )
        self.assertNotIn(self._CUE, content)


if __name__ == "__main__":
    unittest.main()
