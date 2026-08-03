"""P44 — prompt-cache prefix divergence measurement.

Three layers, tested separately:

* :func:`diagnose_divergence` against a five-block synthetic ladder, so
  the interesting cases are readable instead of buried in a 30 KB prompt.
* the real assembler, to prove the snapshot survives a round trip and
  that a byte-identical turn reports no divergence at all.
* the JSONL sink, whose entire job is to keep these records OUT of
  ``app.log``.
"""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from app.core.infra.crash_logging import (
    configure_logging_full,
    configure_prompt_cache_log,
    get_prompt_cache_log_path,
)
from app.core.session.chat_turn_mixin import _estimate_scale
from app.core.session.prompt_assembler import block_hash_table
from app.core.session.prompt_prefix_telemetry import (
    PrefixSnapshot,
    diagnose_divergence,
    emit_prefix_record,
    message_digest,
    prompt_cache_sink_enabled,
)
from app.llm.chat_client import ChatUsage
from app.llm.openai_compatible_client import _fill_wall_clock_eval_duration

from tests.test_prompt_assembler import _TempDb, _make_assembler


# A tiny stand-in for the real 106-block ladder. Stable at the front,
# volatile at the back, exactly like the thing it models.
_LADDER = ("persona", "relationship", "summary", "affect", "mood_hint")
_TIER_OF = {
    "persona": "T0",
    "relationship": "T1",
    "summary": "T2",
    "affect": "T5",
    "mood_hint": "T6",
}
_CHARS = {
    "persona": 1000,
    "relationship": 200,
    "summary": 300,
    "affect": 100,
    "mood_hint": 50,
}


def _snapshot(**overrides: object) -> PrefixSnapshot:
    """A snapshot of the synthetic ladder, with named blocks overridden.

    Each block's digest is derived from its name unless the caller
    supplies replacement text, so "block X changed" reads as
    ``_snapshot(mood_hint="something else")``.
    """
    texts = {name: f"{name}-v1" for name in _LADDER}
    history = overrides.pop("history", ("m1", "m2", "m3"))
    for name, text in overrides.items():
        texts[name] = str(text)
    return PrefixSnapshot(
        block_hashes={n: message_digest(t) for n, t in texts.items()},
        block_chars=dict(_CHARS),
        history_hashes=tuple(message_digest(h) for h in history),
        history_chars=sum(len(h) for h in history),
        sys_chars=sum(_CHARS.values()),
    )


def _diagnose(prev, current):
    return diagnose_divergence(
        prev, current, ladder=_LADDER, tier_of=_TIER_OF,
    )


class DiagnoseDivergenceTests(unittest.TestCase):
    """The pure function, against the synthetic ladder."""

    def test_first_turn_has_nothing_to_compare(self) -> None:
        result = _diagnose(None, _snapshot())
        self.assertTrue(result.first_turn)
        self.assertIsNone(result.diverged)
        # Cold turns must not pollute the mean lost_chars in the report.
        self.assertEqual(result.lost_chars, 0)

    def test_identical_prompt_reports_no_divergence(self) -> None:
        result = _diagnose(_snapshot(), _snapshot())
        self.assertIsNone(result.diverged)
        self.assertIsNone(result.tier)
        self.assertEqual(result.changed, 0)
        self.assertEqual(result.lost_chars, 0)
        self.assertEqual(result.lost_pct, 0.0)

    def test_single_volatile_block_reports_block_and_cost(self) -> None:
        result = _diagnose(_snapshot(), _snapshot(mood_hint="turn 2 mood"))
        self.assertEqual(result.diverged, "mood_hint")
        self.assertEqual(result.tier, "T6")
        self.assertEqual(result.changed, 1)
        # Last in the ladder, so only its own chars are past the break.
        self.assertEqual(result.lost_chars, _CHARS["mood_hint"])
        self.assertEqual(result.lost_pct, round(100.0 * 50 / 1650, 1))

    def test_mid_ladder_break_costs_everything_after_it(self) -> None:
        result = _diagnose(_snapshot(), _snapshot(summary="recompacted"))
        self.assertEqual(result.diverged, "summary")
        self.assertEqual(
            result.lost_chars,
            _CHARS["summary"] + _CHARS["affect"] + _CHARS["mood_hint"],
        )

    def test_two_changes_report_the_earliest(self) -> None:
        # The whole premise of prefix caching: a T5 change makes the T6
        # change free, because everything after T5 was already lost.
        result = _diagnose(
            _snapshot(),
            _snapshot(affect="new affect", mood_hint="new mood"),
        )
        self.assertEqual(result.diverged, "affect")
        self.assertEqual(result.tier, "T5")
        self.assertEqual(result.changed, 2)
        self.assertEqual(result.changed_by_tier, {"T5": 1, "T6": 1})
        self.assertEqual(
            result.lost_chars, _CHARS["affect"] + _CHARS["mood_hint"],
        )

    def test_a_stable_block_changing_is_the_expensive_case(self) -> None:
        result = _diagnose(_snapshot(), _snapshot(persona="edited persona"))
        self.assertEqual(result.diverged, "persona")
        self.assertEqual(result.tier, "T0")
        self.assertEqual(result.lost_chars, sum(_CHARS.values()))
        self.assertEqual(result.lost_pct, 100.0)

    def test_a_block_appearing_counts_as_a_change(self) -> None:
        # Empty blocks are not appended, so a block switching on shifts
        # everything after it -- it must not read as "unchanged".
        prev = _snapshot()
        current = _snapshot()
        thinned = dict(prev.block_hashes)
        thinned.pop("affect")
        prev = PrefixSnapshot(
            block_hashes=thinned,
            block_chars=prev.block_chars,
            history_hashes=prev.history_hashes,
            history_chars=prev.history_chars,
            sys_chars=prev.sys_chars,
        )
        result = _diagnose(prev, current)
        self.assertEqual(result.diverged, "affect")


class HistoryDivergenceTests(unittest.TestCase):
    """Separating a window slide from messages rewritten in place."""

    def test_unchanged_history_diverges_nowhere(self) -> None:
        result = _diagnose(_snapshot(), _snapshot())
        self.assertIsNone(result.history_diverged)
        self.assertEqual(result.history_slid, 0)

    def test_appending_a_message_keeps_the_prefix(self) -> None:
        result = _diagnose(
            _snapshot(history=("m1", "m2")),
            _snapshot(history=("m1", "m2", "m3")),
        )
        self.assertIsNone(result.history_diverged)
        self.assertEqual(result.history_slid, 0)
        self.assertEqual(result.history_msgs, 3)

    def test_a_slid_window_diverges_at_zero_but_is_explained(self) -> None:
        # The window dropped two messages off the front. Cache-wise the
        # history is lost from index 0, but a stable tail survives, so
        # this is a different (cheaper, self-healing) failure than churn.
        result = _diagnose(
            _snapshot(history=("m1", "m2", "m3", "m4")),
            _snapshot(history=("m3", "m4", "m5")),
        )
        self.assertEqual(result.history_diverged, 0)
        self.assertEqual(result.history_slid, 2)

    def test_rewritten_messages_are_not_explained_by_a_slide(self) -> None:
        # The K-time1 age prefixes re-stamp every retained message as the
        # clock ticks, so no shift lines the two lists up. history_slid
        # of -1 is the fingerprint the measurement exists to catch.
        result = _diagnose(
            _snapshot(history=("[2 min ago] a", "[1 min ago] b")),
            _snapshot(history=("[5 min ago] a", "[4 min ago] b", "c")),
        )
        self.assertEqual(result.history_diverged, 0)
        self.assertEqual(result.history_slid, -1)


class BlockHashTableTests(unittest.TestCase):
    def test_resolves_the_same_names_as_the_char_table(self) -> None:
        table = block_hash_table({
            "persona": "body",
            "mood_hint": "cheerful",
        })
        self.assertIn("persona", table)
        self.assertIn("mood_hint", table)

    def test_same_text_hashes_the_same_and_different_text_does_not(self) -> None:
        first = block_hash_table({"persona": "body"})
        same = block_hash_table({"persona": "body"})
        other = block_hash_table({"persona": "body "})
        self.assertEqual(first["persona"], same["persona"])
        self.assertNotEqual(first["persona"], other["persona"])

    def test_empty_and_absent_are_distinguishable(self) -> None:
        # An empty block still hashes; that is what lets "went empty"
        # register as a change rather than vanishing from the table.
        table = block_hash_table({"persona": ""})
        self.assertIn("persona", table)


class AssemblerRoundTripTests(unittest.TestCase):
    """The snapshot, driven through the real assembler."""

    def _assemble(self, assembler, session: str = "p44"):
        _messages, telemetry = assembler.assemble_with_budget(
            session, "hello",
            context_window=8192,
            response_budget=256,
        )
        return telemetry

    def test_first_turn_then_identical_turn_reports_no_divergence(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="Steady persona.")
            db.add_message(
                session_id="p44", role="user", content="hi", token_count=2,
            )
            first = self._assemble(assembler)
            # Nothing to compare against on turn one.
            self.assertEqual(first.prefix_diverged, "")
            second = self._assemble(assembler)
            # Same providers, same history, same persona: byte-identical,
            # which is the state the whole ladder exists to produce.
            self.assertEqual(second.prefix_diverged, "")
            self.assertEqual(second.prefix_changed, 0)
            self.assertEqual(second.prefix_lost_chars, 0)

    def test_toggling_one_t6_block_names_that_block(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="Steady persona.")
            db.add_message(
                session_id="p44", role="user", content="hi", token_count=2,
            )
            self._assemble(assembler)
            assembler.set_inner_life_providers(
                novelty=lambda _t: "Heads-up: something new to say.",
            )
            telemetry = self._assemble(assembler)
            self.assertEqual(telemetry.prefix_diverged, "novelty_block")
            self.assertEqual(telemetry.prefix_tier, "T6_detectors")
            self.assertGreater(telemetry.prefix_lost_chars, 0)

    def test_an_earlier_tier_wins_over_a_later_one(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="Steady persona.")
            db.add_message(
                session_id="p44", role="user", content="hi", token_count=2,
            )
            self._assemble(assembler)
            # ``vitality`` rather than ``affect``: affect_block is served
            # from the _StaticSlices cache, so a provider wired after the
            # first assembly would not show up until the cache key moves.
            # Vitality is deliberately excluded from that cache.
            assembler.set_inner_life_providers(
                vitality=lambda: "Body: bright and rested.",
                novelty=lambda _t: "Heads-up: something new to say.",
            )
            telemetry = self._assemble(assembler)
            # Both changed, but only the earlier one is reported: past
            # the T5 break the T6 change costs nothing extra.
            self.assertEqual(telemetry.prefix_diverged, "vitality_block")
            self.assertEqual(telemetry.prefix_tier, "T5_affect_style")
            self.assertEqual(telemetry.prefix_changed, 2)

    def test_sessions_do_not_compare_against_each_other(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="Steady persona.")
            for session in ("a", "b"):
                db.add_message(
                    session_id=session, role="user",
                    content="hi", token_count=2,
                )
            self._assemble(assembler, "a")
            telemetry = self._assemble(assembler, "b")
            # Session b's first turn, even though a already ran.
            self.assertEqual(telemetry.prefix_diverged, "")

    def test_snapshot_store_stays_bounded(self) -> None:
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="Steady persona.")
            for index in range(20):
                session = f"s{index}"
                db.add_message(
                    session_id=session, role="user",
                    content="hi", token_count=2,
                )
                self._assemble(assembler, session)
            self.assertLessEqual(len(assembler._prefix_snapshots), 8)


class PromptCacheSinkTests(unittest.TestCase):
    """The sink's one job: stay out of ``app.log``."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        configure_prompt_cache_log(enabled=False)
        logging.getLogger("app").handlers.clear()
        try:
            self._tmp.cleanup()
        except Exception:
            pass

    def test_disabled_by_default_writes_nothing(self) -> None:
        configure_prompt_cache_log(enabled=False)
        self.assertFalse(prompt_cache_sink_enabled())
        self.assertIsNone(get_prompt_cache_log_path())
        emit_prefix_record({"diverged": "mood_hint"})

    def test_records_land_in_their_own_file_as_json(self) -> None:
        target = self.tmp / "pc.jsonl"
        configure_prompt_cache_log(enabled=True, path=str(target))
        self.assertTrue(prompt_cache_sink_enabled())
        emit_prefix_record({"diverged": "mood_hint", "lost_chars": 4120})
        for handler in logging.getLogger("app.promptcache").handlers:
            handler.flush()
        line = target.read_text(encoding="utf-8").strip()
        # Whole line parses: no LOG_FORMAT preamble wrapping the JSON.
        payload = json.loads(line)
        self.assertEqual(payload["diverged"], "mood_hint")
        self.assertEqual(payload["lost_chars"], 4120)
        self.assertIn("ts", payload)

    def test_nothing_leaks_into_the_main_log(self) -> None:
        app_log = self.tmp / "app.log"
        configure_logging_full(
            level_name="INFO",
            file_enabled=True,
            file_path=str(app_log),
            prompt_cache_log_enabled=True,
            prompt_cache_log_path=str(self.tmp / "pc.jsonl"),
        )
        emit_prefix_record({"diverged": "a_very_distinctive_block"})
        logging.getLogger("app.demo").info("an ordinary line")
        for name in ("app", "app.promptcache"):
            for handler in logging.getLogger(name).handlers:
                handler.flush()
        contents = app_log.read_text(encoding="utf-8")
        # Without propagate=False the record would be here too, which is
        # the entire reason the separate file exists.
        self.assertNotIn("a_very_distinctive_block", contents)
        self.assertIn("an ordinary line", contents)


class WallClockEvalDurationTests(unittest.TestCase):
    """tok/s on providers that report no generation timer."""

    def test_derives_eval_duration_and_flags_it(self) -> None:
        usage = ChatUsage(completion_tokens=50)
        _fill_wall_clock_eval_duration(usage, 1000.0, 3000.0)
        self.assertEqual(usage.eval_duration_ms, 2000.0)
        self.assertTrue(usage.eval_duration_estimated)
        # The point of the exercise: a real number instead of 0.
        self.assertEqual(usage.tokens_per_second, 25.0)

    def test_a_provider_reported_duration_is_left_alone(self) -> None:
        usage = ChatUsage(completion_tokens=50, eval_duration_ms=1234.0)
        _fill_wall_clock_eval_duration(usage, 100.0, 5000.0)
        self.assertEqual(usage.eval_duration_ms, 1234.0)
        self.assertFalse(usage.eval_duration_estimated)

    def test_no_first_token_means_no_guess(self) -> None:
        usage = ChatUsage(completion_tokens=0)
        _fill_wall_clock_eval_duration(usage, None, 5000.0)
        self.assertEqual(usage.eval_duration_ms, 0.0)
        self.assertFalse(usage.eval_duration_estimated)

    def test_merge_ors_the_flag_rather_than_summing_it(self) -> None:
        measured = ChatUsage(completion_tokens=10, eval_duration_ms=100.0)
        guessed = ChatUsage(
            completion_tokens=10,
            eval_duration_ms=200.0,
            eval_duration_estimated=True,
        )
        merged = measured.merge(guessed)
        self.assertIs(merged.eval_duration_estimated, True)
        self.assertEqual(merged.eval_duration_ms, 300.0)
        clean = measured.merge(ChatUsage(completion_tokens=5))
        self.assertIs(clean.eval_duration_estimated, False)


class BreakdownScaleTests(unittest.TestCase):
    """Rescaling estimated rows onto the provider's real token count."""

    def test_rows_sum_to_the_providers_count(self) -> None:
        system, history, user = 12000, 2500, 480
        estimate = system + history + user
        actual = 15409
        scale = _estimate_scale(estimate=estimate, actual=actual)
        total = round(system * scale) + round(history * scale) + round(user * scale)
        self.assertAlmostEqual(total, actual, delta=2)

    def test_a_wild_estimate_is_not_corrected(self) -> None:
        # Beyond the sane band, "correcting" would be inventing numbers;
        # better to leave the discrepancy visible.
        self.assertEqual(_estimate_scale(estimate=1000, actual=10000), 1.0)
        self.assertEqual(_estimate_scale(estimate=10000, actual=1000), 1.0)

    def test_missing_either_side_is_a_no_op(self) -> None:
        self.assertEqual(_estimate_scale(estimate=0, actual=15000), 1.0)
        self.assertEqual(_estimate_scale(estimate=15000, actual=0), 1.0)

    def test_telemetry_keeps_the_raw_estimate(self) -> None:
        # The scaling happens in the metrics dict, never on the
        # telemetry object -- otherwise the JSONL record's
        # est_error_pct would be zero by construction.
        with _TempDb() as db:
            assembler = _make_assembler(db, persona_text="Steady persona.")
            db.add_message(
                session_id="p44", role="user", content="hi", token_count=2,
            )
            _messages, telemetry = assembler.assemble_with_budget(
                "p44", "hello", context_window=8192, response_budget=256,
            )
            self.assertEqual(
                telemetry.prompt_tokens_estimate,
                telemetry.system_tokens
                + telemetry.history_tokens
                + telemetry.user_tokens,
            )


if __name__ == "__main__":
    unittest.main()
