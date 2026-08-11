"""L45 self-tuning concept gates: the solver, the rails, and the two locks.

The feature's whole risk is that it writes thresholds by itself, so the tests
that matter most are the ones about what it *refuses* to write:

- an ``observe``-mode gate must never reach ``MemorySettings``, because those
  are the gates that mutate the concept store and move the distribution the
  next run measures;
- a value set in ``config/user.json`` must never be overridden, and no
  background path may edit that file.

Both are asserted against the real registry rather than a fixture, so adding a
gate cannot quietly opt out of them.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np

from app.core.concepts.gate_measure import populations, sample_pair_cosine, snapshot
from app.core.concepts.gate_tuner_worker import (
    LAST_RUN_KEY,
    ConceptGateTunerWorker,
)
from app.core.concepts.gate_tuning import (
    GATE_SPECS,
    MODE_APPLY,
    MODE_OBSERVE,
    OBJ_POOL_MULTIPLE,
    OBJ_SHARE_ABOVE,
    OBJ_UNDER_REACH,
    POP_ACTIVE_CONFIDENCE,
    POP_CANDIDATE_CONFIDENCE,
    POP_DORMANT_QUIET_DAYS,
    POP_OPENNESS_POOL,
    GateSpec,
    applied_settings,
    quantile,
    solve,
    solve_all,
    spec_for,
)
from app.core.infra.gate_tuning_store import (
    HISTORY_CAP,
    adopt_gate,
    append_population,
    apply_gates,
    build_document,
    load_gates,
    load_population,
    save_gates,
    tuned_values,
)

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)


def _spec(**kwargs) -> GateSpec:
    base = {
        "setting": "test_gate",
        "population": POP_ACTIVE_CONFIDENCE,
        "objective": OBJ_SHARE_ABOVE,
        "target": 0.5,
        "why": "because",
        "mode": MODE_APPLY,
        "min_samples": 1,
    }
    base.update(kwargs)
    return GateSpec(**base)


def _iso(now: datetime, days_ago: float) -> str:
    return (now - timedelta(days=days_ago)).isoformat()


def _c(cid, *, kind="identity", subject="user", confidence=0.7,
       status="active", created_at="2026-01-01T00:00:00+00:00", dim=0,
       last_reinforced_at=None):
    return SimpleNamespace(
        concept_id=cid,
        label=f"concept {cid}",
        kind=kind,
        subject=subject,
        confidence=confidence,
        status=status,
        created_at=created_at,
        last_reinforced_at=last_reinforced_at,
        distinct_source_count=3,
        embedding=(
            np.ones(dim, dtype=np.float32) * (0.1 * cid) if dim else
            np.zeros(0, dtype=np.float32)
        ),
    )


class QuantileTests(unittest.TestCase):
    def test_it_interpolates_between_neighbours(self) -> None:
        values = [0.0, 1.0]
        self.assertAlmostEqual(quantile(values, 0.5), 0.5)
        self.assertAlmostEqual(quantile(values, 0.25), 0.25)

    def test_the_ends_are_the_extremes(self) -> None:
        values = [0.2, 0.4, 0.9]
        self.assertAlmostEqual(quantile(values, 0.0), 0.2)
        self.assertAlmostEqual(quantile(values, 1.0), 0.9)

    def test_an_empty_run_is_zero_rather_than_an_error(self) -> None:
        self.assertEqual(quantile([], 0.5), 0.0)


class ShareAboveTests(unittest.TestCase):
    def test_the_bar_admits_the_requested_share(self) -> None:
        samples = [i / 100.0 for i in range(101)]
        solution = solve(
            _spec(target=0.3, max_step=1.0), samples, current=0.5,
        )
        self.assertAlmostEqual(solution.proposed, 0.7, places=2)
        admitted = sum(1 for s in samples if s >= solution.proposed)
        self.assertAlmostEqual(admitted / len(samples), 0.3, places=1)

    def test_a_tiny_target_lands_in_the_far_tail(self) -> None:
        samples = [i / 1000.0 for i in range(1000)]
        solution = solve(
            _spec(target=0.01, max_step=1.0, ceiling=1.0),
            samples,
            current=0.5,
        )
        self.assertGreater(solution.proposed, 0.98)


class PoolMultipleTests(unittest.TestCase):
    def test_the_bar_leaves_the_pool_at_the_requested_multiple(self) -> None:
        samples = [i / 100.0 for i in range(100)]
        solution = solve(
            _spec(
                objective=OBJ_POOL_MULTIPLE,
                target=3.0,
                pool_cap_setting="cap",
                max_step=1.0,
            ),
            samples,
            current=0.5,
            pool_cap=4,
        )
        eligible = sum(1 for s in samples if s >= solution.proposed)
        self.assertEqual(eligible, 12)

    def test_a_pool_too_small_to_satisfy_asks_for_the_lowest_value(self) -> None:
        samples = [0.6, 0.7, 0.8]
        solution = solve(
            _spec(
                objective=OBJ_POOL_MULTIPLE,
                target=5.0,
                pool_cap_setting="cap",
                max_step=1.0,
                floor=0.0,
            ),
            samples,
            current=0.75,
            pool_cap=4,
        )
        self.assertAlmostEqual(solution.proposed, 0.6)

    def test_an_unknown_cap_is_no_signal_and_holds_the_current_value(self) -> None:
        solution = solve(
            _spec(objective=OBJ_POOL_MULTIPLE, target=3.0),
            [0.1, 0.5, 0.9],
            current=0.64,
        )
        self.assertEqual(solution.clamped_by, "no_signal")
        self.assertAlmostEqual(solution.proposed, 0.64)
        self.assertFalse(solution.moved)


class UnderReachTests(unittest.TestCase):
    def test_the_bar_stays_under_what_the_population_reaches(self) -> None:
        """The taste failure, in one assertion.

        A floor above the observed maximum is a gate that can never open, and
        that is exactly what ``taste_min_affinity`` was for five weeks.
        """
        samples = [0.09, 0.15, 0.22, 0.31]
        solution = solve(
            _spec(objective=OBJ_UNDER_REACH, target=0.6, max_step=1.0,
                  floor=0.0),
            samples,
            current=0.5,
        )
        self.assertLess(solution.proposed, max(samples))
        self.assertAlmostEqual(solution.proposed, 0.186, places=3)


class RailTests(unittest.TestCase):
    def test_a_gate_walks_rather_than_jumps(self) -> None:
        solution = solve(
            _spec(target=1.0, max_step=0.02, floor=0.0),
            [0.1] * 50,
            current=0.6,
        )
        self.assertAlmostEqual(solution.proposed, 0.58)
        self.assertEqual(solution.clamped_by, "max_step")
        self.assertAlmostEqual(solution.raw, 0.1)

    def test_the_floor_wins_over_the_data(self) -> None:
        solution = solve(
            _spec(target=1.0, max_step=1.0, floor=0.4),
            [0.05] * 50,
            current=0.5,
        )
        self.assertAlmostEqual(solution.proposed, 0.4)
        self.assertEqual(solution.clamped_by, "floor")

    def test_the_ceiling_wins_over_the_data(self) -> None:
        solution = solve(
            _spec(target=0.0, max_step=1.0, ceiling=0.8),
            [0.99] * 50,
            current=0.5,
        )
        self.assertAlmostEqual(solution.proposed, 0.8)
        self.assertEqual(solution.clamped_by, "ceiling")

    def test_too_few_samples_holds_the_current_value(self) -> None:
        solution = solve(
            _spec(min_samples=40), [0.5] * 10, current=0.63,
        )
        self.assertEqual(solution.clamped_by, "warmup")
        self.assertAlmostEqual(solution.proposed, 0.63)
        self.assertIsNone(solution.raw)

    def test_a_failed_solve_never_falls_back_to_zero(self) -> None:
        """The dangerous default. A bar of 0.0 admits everything."""
        for samples in ([], [0.5] * 3):
            solution = solve(_spec(min_samples=40), samples, current=0.71)
            self.assertAlmostEqual(solution.proposed, 0.71)


class RegistryTests(unittest.TestCase):
    def test_gate_names_are_unique(self) -> None:
        names = [spec.setting for spec in GATE_SPECS]
        self.assertEqual(len(names), len(set(names)))

    def test_only_read_side_gates_are_cleared_to_apply(self) -> None:
        """The lifecycle gates write to the store; they must stay observed."""
        self.assertEqual(
            set(applied_settings()),
            {
                "context_budget_core_min_confidence",
                "concept_core_openness_min_confidence",
                "profile_concept_min_confidence",
            },
        )
        for name in (
            "concept_promote_min_confidence",
            "concept_dormant_confidence_floor",
            "concept_retire_confidence_floor",
            "taste_min_affinity",
        ):
            spec = spec_for(name)
            self.assertIsNotNone(spec, name)
            self.assertEqual(spec.mode, MODE_OBSERVE, name)
            self.assertFalse(spec.writable, name)

    def test_every_writable_gate_names_a_real_settings_field(self) -> None:
        from app.core.infra.memory_settings import MemorySettings

        settings = MemorySettings()
        for spec in GATE_SPECS:
            if spec.is_setting_field:
                self.assertTrue(
                    hasattr(settings, spec.setting),
                    f"{spec.setting} is not a MemorySettings field",
                )

    def test_the_rails_bracket_each_gate_default(self) -> None:
        """A default outside its own rails means the spec is nonsense."""
        from app.core.infra.memory_settings import MemorySettings

        settings = MemorySettings()
        for spec in GATE_SPECS:
            self.assertLess(spec.floor, spec.ceiling, spec.setting)
            if not spec.is_setting_field:
                continue
            default = float(getattr(settings, spec.setting))
            self.assertGreaterEqual(default, spec.floor, spec.setting)
            self.assertLessEqual(default, spec.ceiling, spec.setting)

    def test_pool_objectives_declare_the_cap_they_scale_against(self) -> None:
        for spec in GATE_SPECS:
            if spec.objective == OBJ_POOL_MULTIPLE:
                self.assertTrue(spec.pool_cap_setting, spec.setting)


class MeasurementTests(unittest.TestCase):
    def test_populations_split_by_status_and_lane(self) -> None:
        rows = [
            _c(1, confidence=0.9),
            _c(2, confidence=0.5, status="candidate"),
            _c(3, confidence=0.1, status="dormant"),
            _c(4, kind="aspiration", confidence=0.6),
            _c(5, kind="identity", subject="aiko", confidence=0.8),
        ]
        pops = populations(rows)
        self.assertEqual(pops[POP_ACTIVE_CONFIDENCE], [0.9, 0.6, 0.8])
        self.assertEqual(pops[POP_CANDIDATE_CONFIDENCE], [0.5])
        self.assertEqual(pops["faded_confidence"], [0.1])
        # aspiration is the generative kind here; the profile lane is
        # user-subject identity/value only, so #5 is excluded.
        self.assertEqual(pops[POP_OPENNESS_POOL], [0.6])
        self.assertEqual(pops["profile_pool_confidence"], [0.9])

    def test_an_unmeasurable_population_is_absent_not_empty(self) -> None:
        pops = populations([_c(1)])
        self.assertNotIn("cluster_engaged_rate", pops)
        self.assertNotIn("pair_cosine", pops)
        # No dormant rows at all, so no quiet-days distribution to report.
        self.assertNotIn(POP_DORMANT_QUIET_DAYS, pops)

    def test_dormant_quiet_days_measures_the_ttl_gate(self) -> None:
        """L46: the one population in days rather than in a score, feeding the
        observe-only ``concept_dormant_ttl_days`` gate. It has to read the pool
        the way ``_is_stale_dormant`` does -- ``last_reinforced_at`` when there
        is one, ``created_at`` for a row nothing ever reinforced -- or the
        solved value would be tuned against a distribution the gate never
        applies to."""
        rows = [
            _c(1, status="dormant", last_reinforced_at=_iso(NOW, 10)),
            _c(2, status="dormant", last_reinforced_at=_iso(NOW, 40)),
            # Never reinforced -> falls back to its creation stamp.
            _c(3, status="dormant", created_at=_iso(NOW, 5)),
            _c(4, status="active", last_reinforced_at=_iso(NOW, 90)),
        ]
        quiet = populations(rows, now=NOW)[POP_DORMANT_QUIET_DAYS]
        self.assertEqual([round(q) for q in quiet], [10, 40, 5])

    def test_the_dormant_ttl_gate_is_observed_never_applied(self) -> None:
        spec = spec_for("concept_dormant_ttl_days")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.mode, MODE_OBSERVE)

    def test_a_missing_population_is_skipped_rather_than_solved(self) -> None:
        solutions = solve_all(
            GATE_SPECS, {}, current={}, caps={},
        )
        self.assertEqual(solutions, {})

    def test_the_cosine_sample_is_bounded(self) -> None:
        rows = [_c(i, dim=4) for i in range(1, 30)]
        sample = sample_pair_cosine(rows, pairs=10)
        self.assertLessEqual(len(sample), 10)
        for value in sample:
            self.assertLessEqual(value, 1.0001)

    def test_rows_without_vectors_yield_no_pairs(self) -> None:
        self.assertEqual(sample_pair_cosine([_c(1), _c(2)], pairs=10), [])

    def test_the_snapshot_records_the_gap_rather_than_assuming_a_day(self) -> None:
        row = snapshot(
            [_c(1)],
            {},
            now=NOW,
            previous_at=NOW - timedelta(hours=37.5),
            event_counts={"promoted": 3},
        )
        self.assertAlmostEqual(row["hours_since_previous"], 37.5)
        self.assertEqual(row["events_since_previous"], {"promoted": 3})

    def test_a_first_snapshot_has_no_gap(self) -> None:
        row = snapshot([_c(1)], {}, now=NOW)
        self.assertIsNone(row["hours_since_previous"])


class DocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "concept_gates.json"
        self.addCleanup(self._tmp.cleanup)

    def _solutions(self):
        return solve_all(
            GATE_SPECS,
            {
                POP_ACTIVE_CONFIDENCE: [i / 100.0 for i in range(100)],
                POP_CANDIDATE_CONFIDENCE: [i / 100.0 for i in range(100)],
                "core_pool_confidence": [i / 100.0 for i in range(100)],
            },
            current={spec.setting: 0.5 for spec in GATE_SPECS},
            caps={"context_budget_core_cap": 2},
        )

    def test_it_round_trips_through_the_file(self) -> None:
        document = build_document(self._solutions(), now=NOW)
        save_gates(document, path=self.path)
        loaded = load_gates(path=self.path)
        self.assertEqual(loaded["gates"].keys(), document["gates"].keys())
        self.assertEqual(loaded["updated_at"], NOW.isoformat())

    def test_a_corrupt_file_reads_empty_instead_of_raising(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        self.assertEqual(load_gates(path=self.path)["gates"], {})

    def test_a_future_version_is_discarded(self) -> None:
        self.path.write_text(
            json.dumps({"version": 999, "gates": {"x": {"value": 0.9}}}),
            encoding="utf-8",
        )
        self.assertEqual(load_gates(path=self.path)["gates"], {})

    def test_only_applied_entries_are_offered_as_values(self) -> None:
        document = build_document(self._solutions(), now=NOW)
        values = tuned_values(document)
        for name in values:
            self.assertTrue(document["gates"][name]["applied"], name)
        self.assertNotIn("concept_promote_min_confidence", values)

    def test_history_records_a_move_once_and_stays_capped(self) -> None:
        document = None
        for step in range(HISTORY_CAP + 6):
            solutions = solve_all(
                [_spec(setting="context_budget_core_min_confidence",
                       population=POP_ACTIVE_CONFIDENCE,
                       target=0.5, max_step=1.0, floor=0.0, ceiling=1.0)],
                {POP_ACTIVE_CONFIDENCE: [0.4 + step / 100.0] * 50},
                current={"context_budget_core_min_confidence": 0.5},
            )
            document = build_document(
                solutions,
                now=NOW + timedelta(days=step),
                specs=[_spec(setting="context_budget_core_min_confidence")],
                previous=document,
            )
        history = document["gates"]["context_budget_core_min_confidence"][
            "history"
        ]
        self.assertEqual(len(history), HISTORY_CAP)

    def test_an_unchanged_value_does_not_grow_the_history(self) -> None:
        specs = [_spec(setting="context_budget_core_min_confidence")]
        solutions = solve_all(
            specs,
            {POP_ACTIVE_CONFIDENCE: [0.5] * 50},
            current={"context_budget_core_min_confidence": 0.5},
        )
        first = build_document(solutions, now=NOW, specs=specs)
        second = build_document(
            solutions, now=NOW + timedelta(days=1), specs=specs,
            previous=first,
        )
        entry = second["gates"]["context_budget_core_min_confidence"]
        self.assertEqual(len(entry["history"]), 1)

    def test_a_user_override_is_recorded_with_its_drift(self) -> None:
        specs = [
            _spec(
                setting="context_budget_core_min_confidence",
                target=0.5,
                max_step=1.0,
                floor=0.0,
                ceiling=1.0,
            )
        ]
        solutions = solve_all(
            specs,
            {POP_ACTIVE_CONFIDENCE: [0.8] * 50},
            current={"context_budget_core_min_confidence": 0.5},
        )
        document = build_document(
            solutions,
            now=NOW,
            specs=specs,
            user_overrides={"context_budget_core_min_confidence": 0.7},
        )
        entry = document["gates"]["context_budget_core_min_confidence"]
        self.assertFalse(entry["applied"])
        self.assertIn("user.json", entry["unapplied_because"])
        self.assertAlmostEqual(entry["user_value"], 0.7)
        self.assertAlmostEqual(entry["drift_from_user"], 0.1, places=3)


class PopulationFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "concept_population.jsonl"
        self.addCleanup(self._tmp.cleanup)

    def test_lines_append_and_read_back_oldest_first(self) -> None:
        for i in range(3):
            append_population({"at": f"day-{i}"}, path=self.path)
        rows = load_population(path=self.path)
        self.assertEqual([r["at"] for r in rows], ["day-0", "day-1", "day-2"])

    def test_the_file_is_trimmed_to_the_cap(self) -> None:
        for i in range(8):
            append_population({"at": i}, path=self.path, cap=3)
        rows = load_population(path=self.path)
        self.assertEqual([r["at"] for r in rows], [5, 6, 7])

    def test_a_junk_line_is_skipped_not_fatal(self) -> None:
        append_population({"at": "good"}, path=self.path)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{truncated\n")
        self.assertEqual(len(load_population(path=self.path)), 1)


class ApplyTests(unittest.TestCase):
    """The first lock: what may reach ``MemorySettings``."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.config = Path(self._tmp.name) / "user.json"
        self.config.write_text("{}", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def _document(self, gates: dict) -> dict:
        return {"version": 1, "updated_at": NOW.isoformat(), "gates": gates}

    def test_an_applied_read_gate_lands_on_the_settings(self) -> None:
        settings = SimpleNamespace(context_budget_core_min_confidence=0.75)
        applied = apply_gates(
            settings,
            self._document({
                "context_budget_core_min_confidence": {
                    "value": 0.62, "applied": True,
                },
            }),
            config_path=self.config,
        )
        self.assertEqual(applied, {"context_budget_core_min_confidence": 0.62})
        self.assertAlmostEqual(
            settings.context_budget_core_min_confidence, 0.62
        )

    def test_an_observe_gate_never_reaches_the_settings(self) -> None:
        """Even if the file claims it was applied.

        The file is on disk and hand-editable, so ``mode`` is re-checked
        against the registry at apply time rather than trusted from the
        document.
        """
        settings = SimpleNamespace(concept_promote_min_confidence=0.6)
        applied = apply_gates(
            settings,
            self._document({
                "concept_promote_min_confidence": {
                    "value": 0.2, "applied": True,
                },
            }),
            config_path=self.config,
        )
        self.assertEqual(applied, {})
        self.assertAlmostEqual(settings.concept_promote_min_confidence, 0.6)

    def test_every_observe_gate_in_the_registry_is_inert(self) -> None:
        gates = {
            spec.setting: {"value": 0.123, "applied": True}
            for spec in GATE_SPECS
            if spec.mode != MODE_APPLY
        }
        settings = SimpleNamespace(**{name: 0.9 for name in gates})
        self.assertEqual(
            apply_gates(settings, self._document(gates),
                        config_path=self.config),
            {},
        )
        for name in gates:
            self.assertAlmostEqual(getattr(settings, name), 0.9, msg=name)

    def test_a_user_override_wins_over_a_learned_value(self) -> None:
        self.config.write_text(
            json.dumps(
                {"memory": {"context_budget_core_min_confidence": 0.7}}
            ),
            encoding="utf-8",
        )
        settings = SimpleNamespace(context_budget_core_min_confidence=0.7)
        applied = apply_gates(
            settings,
            self._document({
                "context_budget_core_min_confidence": {
                    "value": 0.55, "applied": True,
                },
            }),
            config_path=self.config,
        )
        self.assertEqual(applied, {})
        self.assertAlmostEqual(settings.context_budget_core_min_confidence, 0.7)

    def test_an_unapplied_entry_is_skipped(self) -> None:
        settings = SimpleNamespace(context_budget_core_min_confidence=0.75)
        apply_gates(
            settings,
            self._document({
                "context_budget_core_min_confidence": {
                    "value": 0.4, "applied": False,
                },
            }),
            config_path=self.config,
        )
        self.assertAlmostEqual(
            settings.context_budget_core_min_confidence, 0.75
        )

    def test_a_synthetic_kind_floor_has_nowhere_to_land(self) -> None:
        settings = SimpleNamespace(**{"kind_floor.value.min_confidence": 0.72})
        self.assertEqual(
            apply_gates(
                settings,
                self._document({
                    "kind_floor.value.min_confidence": {
                        "value": 0.4, "applied": True,
                    },
                }),
                config_path=self.config,
            ),
            {},
        )

    def test_a_missing_settings_object_is_a_no_op(self) -> None:
        self.assertEqual(apply_gates(None, self._document({})), {})


class AdoptTests(unittest.TestCase):
    """The second lock: only an explicit command touches ``user.json``."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        root = Path(self._tmp.name)
        self.config = root / "user.json"
        self.gates = root / "concept_gates.json"
        self.config.write_text(
            json.dumps({
                "memory": {
                    "context_budget_core_min_confidence": 0.7,
                    "max_memories": 5000,
                },
                "avatar": {"scale": 1.2},
            }),
            encoding="utf-8",
        )
        self.addCleanup(self._tmp.cleanup)

    def test_adopting_seeds_the_gate_and_frees_the_key(self) -> None:
        result = adopt_gate(
            "context_budget_core_min_confidence",
            current_value=0.7,
            now=NOW,
            path=self.gates,
            config_path=self.config,
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["removed_from_user_json"])

        entry = load_gates(path=self.gates)["gates"][
            "context_budget_core_min_confidence"
        ]
        # Seeded from where the user left it, so behaviour does not jump at
        # the moment of handoff -- the step clamp walks from 0.7.
        self.assertAlmostEqual(entry["value"], 0.7)
        self.assertAlmostEqual(entry["seed_value"], 0.7)
        self.assertEqual(entry["seeded_from"], "config/user.json")
        self.assertTrue(entry["applied"])

    def test_adopting_leaves_the_users_other_settings_alone(self) -> None:
        adopt_gate(
            "context_budget_core_min_confidence",
            current_value=0.7,
            now=NOW,
            path=self.gates,
            config_path=self.config,
        )
        raw = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertNotIn(
            "context_budget_core_min_confidence", raw["memory"],
        )
        self.assertEqual(raw["memory"]["max_memories"], 5000)
        self.assertEqual(raw["avatar"], {"scale": 1.2})

    def test_adopting_a_key_the_user_never_set_is_refused(self) -> None:
        result = adopt_gate(
            "profile_concept_min_confidence",
            current_value=0.5,
            now=NOW,
            path=self.gates,
            config_path=self.config,
        )
        self.assertFalse(result["ok"])
        self.assertIn("not set in config/user.json", result["error"])

    def test_adopting_an_unknown_gate_is_refused(self) -> None:
        result = adopt_gate(
            "not_a_gate", current_value=0.5, path=self.gates,
            config_path=self.config,
        )
        self.assertFalse(result["ok"])

    def test_a_synthetic_gate_cannot_be_adopted(self) -> None:
        result = adopt_gate(
            "kind_floor.value.min_confidence",
            current_value=0.72,
            path=self.gates,
            config_path=self.config,
        )
        self.assertFalse(result["ok"])


class _KV:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key) or None

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


class _Store:
    def __init__(self, rows) -> None:
        self.rows = list(rows)
        self.calls = 0

    def list_by(self, **_kwargs):
        self.calls += 1
        return list(self.rows)


class SchedulingTests(unittest.TestCase):
    """Liveness on a machine that is switched off overnight."""

    def _worker(self, kv: _KV, **settings) -> ConceptGateTunerWorker:
        base = {
            "concept_gate_tuning_enabled": True,
            "concept_gate_tuning_heartbeat_seconds": 21600,
            "concept_gate_tuning_cadence_seconds": 86400,
            "concept_min_clusters": 1,
            "concept_min_history_days": 0.0,
        }
        base.update(settings)
        rows = [
            _c(i, created_at="2026-01-01T00:00:00+00:00") for i in range(1, 40)
        ]
        return ConceptGateTunerWorker(
            concept_store=_Store(rows),
            memory_settings=SimpleNamespace(**base),
            kv_get=kv.get,
            kv_set=kv.set,
        )

    def test_the_heartbeat_is_shorter_than_the_cadence(self) -> None:
        """The liveness fix, asserted.

        The scheduler admits an over-budget worker only once it is three of
        its own heartbeats overdue. A heartbeat equal to the daily cadence
        would make that a three-day wait on a machine that sleeps.
        """
        worker = self._worker(_KV())
        self.assertLess(worker.interval_seconds, worker.cadence_seconds)

    def test_a_never_run_tuner_is_due_immediately(self) -> None:
        worker = self._worker(_KV())
        self.assertIsNotNone(worker.demand(now=NOW, last_run_at=None))

    def test_it_asks_for_the_compute_lane(self) -> None:
        signal = self._worker(_KV()).demand(now=NOW, last_run_at=None)
        self.assertFalse(signal.needs_llm)

    def test_a_run_inside_the_cadence_is_invisible(self) -> None:
        kv = _KV()
        kv.set(LAST_RUN_KEY, (NOW - timedelta(hours=3)).isoformat())
        worker = self._worker(kv)
        self.assertIsNone(worker.demand(now=NOW, last_run_at=None))

    def test_an_overdue_run_catches_up_once_not_per_missed_day(self) -> None:
        kv = _KV()
        kv.set(LAST_RUN_KEY, (NOW - timedelta(days=9)).isoformat())
        worker = self._worker(kv)
        self.assertIsNotNone(worker.demand(now=NOW, last_run_at=None))
        with TemporaryDirectory() as tmp:
            self._redirect(tmp)
            self.assertIsNotNone(worker.run())
            # One completed run clears the backlog; nine missed days do not
            # queue nine runs.
            self.assertIsNone(worker.demand(now=NOW, last_run_at=None))
            self.assertIsNone(worker.run())

    def test_run_is_a_no_op_when_not_due(self) -> None:
        kv = _KV()
        kv.set(LAST_RUN_KEY, NOW.isoformat())
        worker = self._worker(kv)
        worker._concept_store.calls = 0
        self.assertIsNone(worker.run())
        self.assertEqual(worker._concept_store.calls, 0)

    def test_a_disabled_tuner_is_never_ready(self) -> None:
        worker = self._worker(_KV(), concept_gate_tuning_enabled=False)
        self.assertFalse(worker.is_ready(now=NOW, last_run_at=None))

    def test_a_young_graph_is_not_calibrated_against(self) -> None:
        worker = self._worker(_KV(), concept_min_clusters=50)
        self.assertFalse(worker.is_ready(now=NOW, last_run_at=None))

    def _redirect(self, tmp: str) -> None:
        """Point the tuning files at a temp dir for the duration of a test."""
        import os

        previous = os.environ.get("AIKO_TUNING_DIR")
        os.environ["AIKO_TUNING_DIR"] = tmp

        def restore() -> None:
            if previous is None:
                os.environ.pop("AIKO_TUNING_DIR", None)
            else:
                os.environ["AIKO_TUNING_DIR"] = previous

        self.addCleanup(restore)


class WorkerRunTests(unittest.TestCase):
    def setUp(self) -> None:
        import os

        self._tmp = TemporaryDirectory()
        previous = os.environ.get("AIKO_TUNING_DIR")
        os.environ["AIKO_TUNING_DIR"] = self._tmp.name

        def restore() -> None:
            if previous is None:
                os.environ.pop("AIKO_TUNING_DIR", None)
            else:
                os.environ["AIKO_TUNING_DIR"] = previous
            self._tmp.cleanup()

        self.addCleanup(restore)

    def _settings(self) -> SimpleNamespace:
        values = {
            "concept_gate_tuning_enabled": True,
            "concept_gate_tuning_heartbeat_seconds": 21600,
            "concept_gate_tuning_cadence_seconds": 86400,
            "concept_gate_tuning_cosine_pairs": 0,
            "concept_min_clusters": 1,
            "concept_min_history_days": 0.0,
            "context_budget_core_cap": 2,
            "concept_core_openness_slots": 2,
            "profile_concept_max_lines": 4,
        }
        for spec in GATE_SPECS:
            if spec.is_setting_field:
                values.setdefault(spec.setting, 0.5)
        return SimpleNamespace(**values)

    def _rows(self):
        rows = []
        for i in range(1, 200):
            rows.append(_c(
                i,
                kind="identity" if i % 2 else "value",
                confidence=0.3 + (i % 60) / 100.0,
                status="active" if i % 3 else "candidate",
            ))
        return rows

    def test_a_full_run_writes_both_files_and_stamps_the_cadence(self) -> None:
        kv = _KV()
        worker = ConceptGateTunerWorker(
            concept_store=_Store(self._rows()),
            memory_settings=self._settings(),
            kv_get=kv.get,
            kv_set=kv.set,
        )
        stats = worker.run()
        self.assertIsNotNone(stats)
        self.assertGreater(stats["solved"], 0)
        self.assertTrue(load_gates()["gates"])
        self.assertEqual(len(load_population()), 1)
        self.assertTrue(kv.get(LAST_RUN_KEY))

    def test_a_real_run_never_moves_an_observe_gate(self) -> None:
        """The end-to-end version of the first lock."""
        settings = self._settings()
        observed = {
            spec.setting: getattr(settings, spec.setting)
            for spec in GATE_SPECS
            if spec.mode != MODE_APPLY and spec.is_setting_field
        }
        kv = _KV()
        ConceptGateTunerWorker(
            concept_store=_Store(self._rows()),
            memory_settings=settings,
            kv_get=kv.get,
            kv_set=kv.set,
        ).run()
        for name, before in observed.items():
            self.assertAlmostEqual(
                getattr(settings, name), before, msg=name,
            )

    def test_the_second_run_records_the_gap_between_them(self) -> None:
        kv = _KV()
        worker = ConceptGateTunerWorker(
            concept_store=_Store(self._rows()),
            memory_settings=self._settings(),
            kv_get=kv.get,
            kv_set=kv.set,
        )
        worker.run()
        kv.set(LAST_RUN_KEY, (NOW - timedelta(days=400)).isoformat())
        worker.run()
        rows = load_population()
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["hours_since_previous"])
        self.assertIsNotNone(rows[1]["hours_since_previous"])

    def test_a_store_that_raises_does_not_stamp_the_cadence(self) -> None:
        class _Broken:
            def list_by(self, **_kwargs):
                raise RuntimeError("db gone")

        kv = _KV()
        worker = ConceptGateTunerWorker(
            concept_store=_Broken(),
            memory_settings=self._settings(),
            kv_get=kv.get,
            kv_set=kv.set,
        )
        self.assertIsNone(worker.run())
        self.assertIsNone(kv.get(LAST_RUN_KEY))


if __name__ == "__main__":
    unittest.main()
