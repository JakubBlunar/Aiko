"""Unit tests for the demand-driven scheduling primitives (P36).

Covers the pure math in ``app.core.proactive.idle_worker`` and the
contention classifier in ``app.core.proactive.llm_contention``. The
scheduler wiring is exercised separately in
``tests/test_idle_worker_scheduler.py``.
"""
from __future__ import annotations

import unittest

from app.core.infra.settings import (
    LLM_ROLE_MAIN_CHAT,
    LLM_ROLE_WORKER_DEFAULT,
    LlmProvider,
    LlmRoute,
    LlmSettings,
)
from app.core.proactive.idle_worker import (
    LANE_COMPUTE,
    LANE_LLM,
    WorkSignal,
    classify_depth,
    compute_staleness,
    compute_urgency,
    derive_min_interval_s,
    evaluate_admission,
    pressure_from_count,
    pressure_from_deficit,
)
from app.core.proactive.llm_contention import (
    CONTENTION_NONE,
    CONTENTION_QUEUEING,
    CONTENTION_SWAPPING,
    classify_contention,
    llm_lane_multiplier,
)


class WorkSignalTests(unittest.TestCase):
    def test_pressure_is_clamped(self) -> None:
        self.assertEqual(WorkSignal(pressure=5.0).pressure, 1.0)
        self.assertEqual(WorkSignal(pressure=-2.0).pressure, 0.0)
        self.assertAlmostEqual(WorkSignal(pressure=0.42).pressure, 0.42)

    def test_lane_follows_needs_llm(self) -> None:
        self.assertEqual(WorkSignal(pressure=1.0).lane, LANE_COMPUTE)
        self.assertEqual(
            WorkSignal(pressure=1.0, needs_llm=True).lane, LANE_LLM,
        )


class PressureShapeTests(unittest.TestCase):
    """The two directions a worker can want to run."""

    def test_backlog_pressure_rises_with_the_pile(self) -> None:
        self.assertEqual(pressure_from_count(0, saturation=4), 0.0)
        self.assertEqual(pressure_from_count(1, saturation=4), 0.5)
        self.assertEqual(pressure_from_count(4, saturation=4), 1.0)
        self.assertEqual(pressure_from_count(99, saturation=4), 1.0)

    def test_full_shelf_is_zero_pressure(self) -> None:
        # The whole point: a cue worker with stock is never admitted.
        self.assertEqual(pressure_from_deficit(3, want=3), 0.0)
        self.assertEqual(pressure_from_deficit(9, want=3), 0.0)

    def test_empty_shelf_is_maximum_pressure(self) -> None:
        self.assertEqual(pressure_from_deficit(0, want=3), 1.0)
        self.assertEqual(pressure_from_deficit(0, want=1), 1.0)

    def test_one_short_already_clears_the_default_threshold(self) -> None:
        # Being caught empty when the conversation opens a seam costs
        # more than an extra restock, so the curve is eager on purpose.
        self.assertGreaterEqual(pressure_from_deficit(5, want=6), 0.5)
        self.assertGreaterEqual(pressure_from_deficit(2, want=3), 0.5)

    def test_deficit_is_linear_in_the_shortfall(self) -> None:
        self.assertEqual(pressure_from_deficit(2, want=8), 0.75)
        self.assertEqual(pressure_from_deficit(4, want=8), 0.5)

    def test_degenerate_inputs_do_not_explode(self) -> None:
        self.assertEqual(pressure_from_deficit(-4, want=2), 1.0)
        self.assertEqual(pressure_from_deficit(0, want=0), 1.0)
        self.assertEqual(pressure_from_deficit(1, want=0), 0.0)

    def test_the_two_helpers_are_genuine_inverses_at_the_ends(self) -> None:
        self.assertEqual(pressure_from_count(0, saturation=3), 0.0)
        self.assertEqual(pressure_from_deficit(3, want=3), 0.0)
        self.assertEqual(pressure_from_count(3, saturation=3), 1.0)
        self.assertEqual(pressure_from_deficit(0, want=3), 1.0)


class MinIntervalTests(unittest.TestCase):
    def test_ratio_scales_with_interval(self) -> None:
        # The property that lets one ratio serve intervals spanning
        # three orders of magnitude.
        self.assertEqual(
            derive_min_interval_s(30.0, wake_seconds=15.0, ratio=0.1), 15.0,
        )
        self.assertEqual(
            derive_min_interval_s(1800.0, wake_seconds=15.0, ratio=0.1), 180.0,
        )
        self.assertEqual(
            derive_min_interval_s(86400.0, wake_seconds=15.0, ratio=0.1),
            8640.0,
        )

    def test_wake_seconds_is_the_floor(self) -> None:
        # Nothing can run more often than one tick, so a tiny interval
        # must not produce a sub-tick floor.
        self.assertEqual(
            derive_min_interval_s(10.0, wake_seconds=30.0, ratio=0.1), 30.0,
        )


class StalenessAndUrgencyTests(unittest.TestCase):
    def test_staleness_clamps_to_unit_range(self) -> None:
        self.assertEqual(compute_staleness(0.0, 100.0), 0.0)
        self.assertEqual(compute_staleness(50.0, 100.0), 0.5)
        self.assertEqual(compute_staleness(500.0, 100.0), 1.0)

    def test_zero_heartbeat_reads_as_fully_stale(self) -> None:
        self.assertEqual(compute_staleness(1.0, 0.0), 1.0)

    def test_pressure_dominates_but_staleness_rescues(self) -> None:
        busy_and_fresh = compute_urgency(1.0, 0.0)
        idle_and_stale = compute_urgency(0.0, 1.0)
        self.assertGreater(busy_and_fresh, idle_and_stale)
        # A stale low-pressure worker still accumulates rank rather than
        # being permanently outranked by a noisier neighbour.
        self.assertGreater(idle_and_stale, 0.0)


class DepthTests(unittest.TestCase):
    def test_tiers_map_to_multipliers(self) -> None:
        self.assertEqual(classify_depth(10.0)[0], "just_left")
        self.assertEqual(classify_depth(10.0)[2], 1.0)
        self.assertEqual(classify_depth(600.0)[0], "away")
        self.assertEqual(classify_depth(600.0)[2], 3.0)
        self.assertEqual(classify_depth(2400.0)[0], "long_away")
        self.assertEqual(classify_depth(50000.0)[0], "overnight")
        self.assertEqual(classify_depth(50000.0)[2], 10.0)

    def test_max_multiplier_of_one_disables_scaling(self) -> None:
        for secs in (10.0, 600.0, 2400.0, 50000.0):
            self.assertEqual(
                classify_depth(secs, max_multiplier=1.0)[2], 1.0,
            )

    def test_tier_index_is_monotonic(self) -> None:
        indices = [classify_depth(s)[1] for s in (10, 600, 2400, 50000)]
        self.assertEqual(indices, [0, 1, 2, 3])


class AdmissionTests(unittest.TestCase):
    def _admit(self, **kw):
        base = dict(
            elapsed_s=100.0,
            heartbeat_s=300.0,
            min_interval_s=30.0,
            signal=WorkSignal(pressure=0.9),
            threshold=0.35,
        )
        base.update(kw)
        return evaluate_admission(**base)

    def test_no_signal_takes_the_legacy_path(self) -> None:
        verdict = self._admit(signal=None)
        self.assertTrue(verdict.admit)
        self.assertEqual(verdict.reason, "legacy")
        # Unmigrated workers are charged to the LLM lane because we
        # cannot know that they are cheap.
        self.assertEqual(verdict.lane, LANE_LLM)

    def test_never_run_worker_is_admitted(self) -> None:
        verdict = self._admit(elapsed_s=None)
        self.assertTrue(verdict.admit)
        self.assertEqual(verdict.reason, "first_run")

    def test_floor_blocks_even_at_full_pressure(self) -> None:
        verdict = self._admit(
            elapsed_s=5.0, signal=WorkSignal(pressure=1.0),
        )
        self.assertFalse(verdict.admit)
        self.assertEqual(verdict.reason, "floor")

    def test_zero_pressure_is_not_admitted_before_heartbeat(self) -> None:
        verdict = self._admit(signal=WorkSignal(pressure=0.0))
        self.assertFalse(verdict.admit)
        self.assertEqual(verdict.reason, "idle")

    def test_heartbeat_guarantees_liveness_despite_zero_pressure(self) -> None:
        # A broken probe must not starve its worker forever.
        verdict = self._admit(
            elapsed_s=400.0, signal=WorkSignal(pressure=0.0),
        )
        self.assertTrue(verdict.admit)
        self.assertEqual(verdict.reason, "heartbeat")

    def test_pressure_admits_long_before_the_heartbeat(self) -> None:
        # The whole point: 100s into a 300s heartbeat, real backlog runs.
        verdict = self._admit(
            elapsed_s=100.0, signal=WorkSignal(pressure=1.0),
        )
        self.assertTrue(verdict.admit)
        self.assertEqual(verdict.reason, "pressure")

    def test_low_pressure_below_threshold_waits(self) -> None:
        verdict = self._admit(
            elapsed_s=40.0,
            signal=WorkSignal(pressure=0.1),
            threshold=0.5,
        )
        self.assertFalse(verdict.admit)
        self.assertEqual(verdict.reason, "below_threshold")

    def test_lane_is_carried_from_the_signal(self) -> None:
        self.assertEqual(
            self._admit(signal=WorkSignal(pressure=0.9)).lane, LANE_COMPUTE,
        )
        self.assertEqual(
            self._admit(
                signal=WorkSignal(pressure=0.9, needs_llm=True),
            ).lane,
            LANE_LLM,
        )


def _llm(
    *,
    chat_provider: str = "local",
    worker_provider: str = "local",
    chat_model: str = "qwen3.5:9b",
    worker_model: str = "qwen3.5:9b",
    providers: list[LlmProvider] | None = None,
) -> LlmSettings:
    if providers is None:
        providers = [
            LlmProvider(
                id="local", name="Local", kind="ollama",
                base_url="http://127.0.0.1:11434",
            ),
        ]
    return LlmSettings(
        providers=providers,
        routes={
            LLM_ROLE_MAIN_CHAT: LlmRoute(
                provider_id=chat_provider, model=chat_model,
            ),
            LLM_ROLE_WORKER_DEFAULT: LlmRoute(
                provider_id=worker_provider, model=worker_model,
            ),
        },
    )


class ContentionTests(unittest.TestCase):
    def test_same_endpoint_same_model_is_queueing(self) -> None:
        self.assertEqual(classify_contention(_llm()), CONTENTION_QUEUEING)

    def test_same_endpoint_different_model_is_swapping(self) -> None:
        self.assertEqual(
            classify_contention(_llm(worker_model="qwen3.5:4b")),
            CONTENTION_SWAPPING,
        )

    def test_different_endpoints_is_none(self) -> None:
        providers = [
            LlmProvider(
                id="local", name="Local", kind="ollama",
                base_url="http://127.0.0.1:11434",
            ),
            LlmProvider(
                id="other", name="Other box", kind="ollama",
                base_url="http://192.168.1.50:11434",
            ),
        ]
        settings = _llm(worker_provider="other", providers=providers)
        self.assertEqual(classify_contention(settings), CONTENTION_NONE)

    def test_remote_worker_route_is_none(self) -> None:
        providers = [
            LlmProvider(
                id="local", name="Local", kind="ollama",
                base_url="http://127.0.0.1:11434",
            ),
            LlmProvider(
                id="cloud", name="Cloud", kind="openai_compatible",
                base_url="https://api.example.com/v1",
            ),
        ]
        settings = _llm(worker_provider="cloud", providers=providers)
        self.assertEqual(classify_contention(settings), CONTENTION_NONE)

    def test_localhost_and_loopback_ip_are_the_same_gpu(self) -> None:
        providers = [
            LlmProvider(
                id="local", name="Local", kind="ollama",
                base_url="http://127.0.0.1:11434",
            ),
            LlmProvider(
                id="alias", name="Alias", kind="ollama",
                base_url="http://localhost:11434/",
            ),
        ]
        settings = _llm(worker_provider="alias", providers=providers)
        # A cosmetic spelling difference must not read as split
        # backends and silently remove the protection.
        self.assertEqual(classify_contention(settings), CONTENTION_QUEUEING)

    def test_missing_worker_route_shares_the_chat_client(self) -> None:
        settings = _llm()
        del settings.routes[LLM_ROLE_WORKER_DEFAULT]
        self.assertEqual(classify_contention(settings), CONTENTION_QUEUEING)

    def test_unknown_provider_errs_strict(self) -> None:
        settings = _llm(worker_provider="does_not_exist")
        self.assertEqual(classify_contention(settings), CONTENTION_SWAPPING)

    def test_override_short_circuits_detection(self) -> None:
        settings = _llm(worker_model="qwen3.5:4b")  # would be swapping
        self.assertEqual(
            classify_contention(settings, override="none"), CONTENTION_NONE,
        )

    def test_unknown_override_falls_back_to_auto(self) -> None:
        self.assertEqual(
            classify_contention(_llm(), override="banana"),
            CONTENTION_QUEUEING,
        )


class LaneMultiplierTests(unittest.TestCase):
    def test_swapping_is_pinned_through_shallow_tiers(self) -> None:
        for tier_index, depth_mult in ((0, 1.0), (1, 3.0)):
            self.assertEqual(
                llm_lane_multiplier(
                    CONTENTION_SWAPPING,
                    tier_index=tier_index,
                    depth_multiplier=depth_mult,
                ),
                1.0,
            )

    def test_swapping_opens_from_long_away(self) -> None:
        self.assertEqual(
            llm_lane_multiplier(
                CONTENTION_SWAPPING, tier_index=2, depth_multiplier=6.0,
            ),
            6.0,
        )

    def test_split_backends_track_depth_at_every_tier(self) -> None:
        for grade in (CONTENTION_NONE, CONTENTION_QUEUEING):
            for tier_index, depth_mult in enumerate((1.0, 3.0, 6.0, 10.0)):
                self.assertEqual(
                    llm_lane_multiplier(
                        grade,
                        tier_index=tier_index,
                        depth_multiplier=depth_mult,
                    ),
                    depth_mult,
                )


if __name__ == "__main__":
    unittest.main()
