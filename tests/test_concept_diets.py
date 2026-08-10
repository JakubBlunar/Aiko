"""Worker concept diets, the role axis, and the core lane's openness reserve.

Three mechanisms, one shared purpose: stop a concept selection from
consisting entirely of the kinds that constrain Aiko. Ranking concepts on
strength alone converges on ``boundary`` (importance prior 0.9) and
``value`` (0.85), and a prompt built only from those can restate what she
already holds but never reach past it.

- the **role axis** (`anchor` / `guide` / `generative`) is the vocabulary,
- **diets** budget and balance what a worker reads,
- the **openness reserve** keeps a seat on the pinned core lane for a kind
  that is otherwise structurally ineligible for it.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.concepts.concept_diets import (
    CONCEPT_DIETS,
    ConceptDiet,
    DietTuning,
    diet_problems,
    diet_for,
    registry_problems,
    resolve_budget,
    tuning_from_host,
)
from app.core.concepts.concept_kinds import (
    CONCEPT_KINDS,
    ROLE_GENERATIVE,
    ROLES,
    get_kind,
    kinds_by_role,
)
from app.core.concepts.concept_view import ConceptView


def _c(cid, *, kind="identity", subject="user", confidence=0.7, label=None,
       status="active"):
    return SimpleNamespace(
        concept_id=cid,
        label=label if label is not None else f"concept {cid}",
        kind=kind,
        subject=subject,
        confidence=confidence,
        status=status,
        distinct_source_count=3,
    )


class _FakeStore:
    def __init__(self, concepts):
        self._concepts = {int(c.concept_id): c for c in concepts}

    def list_by(self, *, status=None, subject=None, kind=None, user_id=None):
        return [
            c for c in self._concepts.values()
            if (status is None or c.status == status)
            and (subject is None or c.subject == subject)
            and (kind is None or c.kind == kind)
        ]

    def get(self, cid):
        return self._concepts.get(int(cid))

    def edges_from(self, _node_type, _node_id):
        return []


def _view(concepts, **tuning):
    opts = {"context_window": 65536}
    opts.update(tuning)
    return ConceptView(_FakeStore(concepts), tuning=DietTuning(**opts))


def _kinds(concepts):
    out: dict[str, int] = {}
    for c in concepts:
        out[c.kind] = out.get(c.kind, 0) + 1
    return out


# ── the role axis ─────────────────────────────────────────────────────


class RoleAxisTests(unittest.TestCase):
    def test_every_registered_kind_carries_a_recognised_role(self) -> None:
        for name, kind in CONCEPT_KINDS.items():
            with self.subTest(kind=name):
                self.assertIn(kind.role, ROLES)

    def test_each_role_has_members(self) -> None:
        # A role nobody carries would make the balance mechanisms silently
        # inert -- the reserve would never fill and the floor never fire.
        for role in ROLES:
            with self.subTest(role=role):
                self.assertTrue(kinds_by_role(role))

    def test_kinds_by_role_is_sorted_and_total(self) -> None:
        seen = [k.name for role in ROLES for k in kinds_by_role(role)]
        self.assertEqual(sorted(seen), sorted(CONCEPT_KINDS))
        for role in ROLES:
            names = [k.name for k in kinds_by_role(role)]
            self.assertEqual(names, sorted(names))

    def test_an_unknown_role_is_empty_rather_than_an_error(self) -> None:
        self.assertEqual(kinds_by_role("rail"), [])
        self.assertEqual(kinds_by_role(""), [])

    def test_the_highest_stakes_kinds_are_the_guides(self) -> None:
        # The premise the whole pass rests on: rank on importance and the
        # top of the list is what constrains her. If this ever stops being
        # true the reserve and the floor are solving a problem that moved.
        ladder = sorted(
            CONCEPT_KINDS.values(), key=lambda k: -k.importance,
        )
        self.assertEqual(
            [k.name for k in ladder[:2]], ["boundary", "value"],
        )
        self.assertTrue(all(k.role == "guide" for k in ladder[:2]))


# ── registry invariants ───────────────────────────────────────────────


class DietRegistryTests(unittest.TestCase):
    def test_the_shipped_registry_is_healthy(self) -> None:
        self.assertEqual(registry_problems(), [])

    def test_every_declared_kind_is_registered(self) -> None:
        for name, diet in CONCEPT_DIETS.items():
            for kind in diet.kinds:
                with self.subTest(consumer=name, kind=kind):
                    self.assertIsNotNone(get_kind(kind))

    def test_a_guide_only_diet_is_rejected(self) -> None:
        problems = diet_problems(
            ConceptDiet(consumer="rails", kinds=("boundary", "value"))
        )
        self.assertTrue(
            any("generative" in p for p in problems), problems,
        )

    def test_a_guide_diet_with_a_generative_kind_passes(self) -> None:
        self.assertEqual(
            diet_problems(
                ConceptDiet(consumer="ok", kinds=("boundary", "taste"))
            ),
            [],
        )

    def test_a_generative_only_diet_needs_no_guide(self) -> None:
        # The invariant is one-directional on purpose. A worker that only
        # reads what could move is open by construction; requiring a rail
        # back would be the mechanism arguing against itself.
        self.assertEqual(
            diet_problems(ConceptDiet(consumer="open", kinds=("taste",))), [],
        )

    def test_malformed_diets_are_caught(self) -> None:
        self.assertTrue(
            diet_problems(ConceptDiet(consumer="empty", kinds=()))
        )
        self.assertTrue(
            diet_problems(ConceptDiet(consumer="bogus", kinds=("nope",)))
        )
        self.assertTrue(
            diet_problems(
                ConceptDiet(consumer="dupe", kinds=("taste", "taste"))
            )
        )
        self.assertTrue(
            diet_problems(
                ConceptDiet(consumer="w", kinds=("taste",), weight=0.0)
            )
        )

    def test_producers_have_no_diet(self) -> None:
        # The exclusion principle: feeding the concept producers the
        # existing concept set makes a self-confirming loop.
        for producer in (
            "concept_synthesis",
            "hypothesis_proposer",
            "memory_extractor",
        ):
            with self.subTest(producer=producer):
                self.assertIsNone(diet_for(producer))

    def test_an_unknown_consumer_is_none_not_an_error(self) -> None:
        self.assertIsNone(diet_for("nobody"))
        self.assertIsNone(diet_for(""))


# ── budget ────────────────────────────────────────────────────────────


class BudgetTests(unittest.TestCase):
    def _tuning(self, **kw):
        opts = {
            "context_window": 65536,
            "token_fraction": 0.06,
            "max_tokens": 600,
            "min_tokens": 150,
        }
        opts.update(kw)
        return DietTuning(**opts)

    def test_weight_scales_the_allowance(self) -> None:
        light = ConceptDiet(consumer="a", kinds=("taste",), weight=0.5)
        heavy = ConceptDiet(consumer="b", kinds=("taste",), weight=2.0)
        t = self._tuning()
        self.assertEqual(resolve_budget(light, t), 300)
        self.assertEqual(resolve_budget(heavy, t), 1200)

    def test_the_cap_binds_on_a_large_window(self) -> None:
        # 6% of 65536 is ~3900 tokens, which is more concepts than the
        # store is likely to hold -- the fraction alone would never bind.
        diet = ConceptDiet(consumer="a", kinds=("taste",))
        self.assertEqual(resolve_budget(diet, self._tuning()), 600)

    def test_the_fraction_binds_on_a_small_window(self) -> None:
        diet = ConceptDiet(consumer="a", kinds=("taste",))
        self.assertEqual(
            resolve_budget(diet, self._tuning(context_window=4096)), 245,
        )

    def test_the_floor_protects_a_tiny_window(self) -> None:
        diet = ConceptDiet(consumer="a", kinds=("taste",), weight=0.1)
        self.assertEqual(
            resolve_budget(diet, self._tuning(context_window=1024)), 150,
        )

    def test_an_unknown_window_falls_back_to_the_cap(self) -> None:
        # Not to zero: a view built without settings should still hand a
        # worker a usable handful rather than silently starving it.
        diet = ConceptDiet(consumer="a", kinds=("taste",))
        self.assertEqual(
            resolve_budget(diet, self._tuning(context_window=0)), 600,
        )

    def test_every_shipped_diet_can_afford_one_of_each_kind(self) -> None:
        # A diet whose budget cannot seat every kind it declares is a
        # silent lie: the round-robin would drop the last bucket every
        # single run, and it would look like the store was simply empty.
        t = self._tuning()
        nominal = 20  # a typical rendered concept line
        for name, diet in CONCEPT_DIETS.items():
            with self.subTest(consumer=name):
                self.assertGreaterEqual(
                    resolve_budget(diet, t), len(diet.kinds) * nominal,
                )

    def test_tuning_is_sized_off_the_worker_route(self) -> None:
        host = SimpleNamespace(
            _memory_settings=SimpleNamespace(
                concept_diet_token_fraction=0.06,
                concept_diet_max_tokens=600,
                concept_diet_min_tokens=150,
                concept_importance_enabled=True,
                concept_importance_strength=0.4,
            ),
            _context_window=200000,
            _worker_route_model_ctx=lambda: ("local", 8192),
        )
        self.assertEqual(tuning_from_host(host).context_window, 8192)

    def test_tuning_survives_a_half_built_host(self) -> None:
        tuning = tuning_from_host(SimpleNamespace())
        self.assertEqual(tuning.context_window, 0)
        self.assertEqual(tuning.max_tokens, 600)
        self.assertEqual(tuning.importance_strength, 0.0)

    def test_disabling_importance_switches_the_axis_off(self) -> None:
        host = SimpleNamespace(
            _memory_settings=SimpleNamespace(
                concept_importance_enabled=False,
                concept_importance_strength=0.4,
            ),
        )
        self.assertEqual(tuning_from_host(host).importance_strength, 0.0)


# ── the draw ──────────────────────────────────────────────────────────


class ForConsumerTests(unittest.TestCase):
    def _lopsided(self):
        """A store shaped like the real problem: many strong values, few
        weak generative rows."""
        rows = []
        cid = 0
        spec = (
            ("value", "user", 10, 0.95),
            ("identity", "user", 10, 0.93),
            ("affective", "user", 4, 0.70),
            ("taste", "user", 3, 0.62),
            ("aspiration", "user", 2, 0.66),
        )
        for kind, subject, count, conf in spec:
            for i in range(count):
                cid += 1
                rows.append(
                    _c(cid, kind=kind, subject=subject,
                       confidence=conf - i * 0.01)
                )
        return rows

    def test_a_consumer_without_a_diet_gets_nothing(self) -> None:
        self.assertEqual(_view(self._lopsided()).for_consumer("nobody"), [])

    def test_a_cold_layer_degrades_to_empty(self) -> None:
        view = ConceptView(None, tuning=DietTuning())
        self.assertEqual(view.for_consumer("belief_inference"), [])

    def test_the_draw_reaches_every_declared_kind(self) -> None:
        # The point of the whole exercise: 20 strong values and identities
        # do not get to spend the entire budget.
        out = _view(self._lopsided()).for_consumer("belief_inference")
        counts = _kinds(out)
        for kind in diet_for("belief_inference").kinds:
            with self.subTest(kind=kind):
                self.assertGreater(counts.get(kind, 0), 0)

    def test_a_tight_budget_trims_every_kind_evenly(self) -> None:
        # Round-robin during the draw is what makes this true. A global
        # sort by strength would spend the whole budget on values.
        out = _view(
            self._lopsided(), max_tokens=60, min_tokens=60,
        ).for_consumer("belief_inference")
        counts = _kinds(out)
        self.assertTrue(out)
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)

    def test_a_generative_kind_survives_the_tightest_budget(self) -> None:
        out = _view(
            self._lopsided(), max_tokens=40, min_tokens=40,
        ).for_consumer("belief_inference")
        roles = {get_kind(c.kind).role for c in out}
        self.assertIn(ROLE_GENERATIVE, roles)

    def test_the_budget_actually_binds(self) -> None:
        wide = _view(self._lopsided()).for_consumer("belief_inference")
        narrow = _view(
            self._lopsided(), max_tokens=100, min_tokens=100,
        ).for_consumer("belief_inference")
        self.assertLess(len(narrow), len(wide))

    def test_a_long_label_costs_only_its_own_slot(self) -> None:
        # Skip-and-continue rather than stop-on-first-miss: one verbose
        # concept should not truncate everything queued behind it.
        rows = [
            _c(1, kind="taste", label="x" * 400, confidence=0.9),
            _c(2, kind="taste", label="short one", confidence=0.8),
            _c(3, kind="taste", label="another short", confidence=0.7),
        ]
        out = _view(
            rows, max_tokens=40, min_tokens=40,
        ).for_consumer("forward_curiosity")
        self.assertEqual([c.concept_id for c in out], [2, 3])

    def test_the_subject_scope_is_honoured(self) -> None:
        rows = [
            _c(1, kind="pursuit", subject="aiko", confidence=0.9),
            _c(2, kind="pursuit", subject="user", confidence=0.95),
        ]
        out = _view(rows).for_consumer("wants_ledger")
        self.assertEqual([c.concept_id for c in out], [1])

    def test_an_explicit_subject_overrides_the_diet(self) -> None:
        rows = [
            _c(1, kind="pursuit", subject="aiko", confidence=0.9),
            _c(2, kind="pursuit", subject="user", confidence=0.95),
        ]
        out = _view(rows).for_consumer("wants_ledger", subject="user")
        self.assertEqual([c.concept_id for c in out], [2])

    def test_the_confidence_floor_is_honoured(self) -> None:
        rows = [
            _c(1, kind="taste", confidence=0.9),
            _c(2, kind="taste", confidence=0.3),
        ]
        out = _view(rows).for_consumer("forward_curiosity")
        self.assertEqual([c.concept_id for c in out], [1])

    def test_ordering_falls_back_to_confidence_without_importance(self) -> None:
        # importance_strength defaults to 0.0, so within a kind the order
        # is the pre-diet banded-confidence one.
        rows = [
            _c(1, kind="taste", confidence=0.60),
            _c(2, kind="taste", confidence=0.95),
            _c(3, kind="taste", confidence=0.75),
        ]
        out = _view(rows).for_consumer("forward_curiosity")
        self.assertEqual([c.concept_id for c in out], [2, 3, 1])

    def test_importance_reorders_within_a_kind(self) -> None:
        # With the axis on but no affect context, the scorer degrades to
        # the bare kind prior. That is constant inside a bucket, so the
        # order must be identical to the confidence-only one -- the
        # documented no-regression path.
        rows = [
            _c(1, kind="taste", confidence=0.60),
            _c(2, kind="taste", confidence=0.95),
            _c(3, kind="taste", confidence=0.75),
        ]
        out = _view(rows, importance_strength=0.4).for_consumer(
            "forward_curiosity"
        )
        self.assertEqual([c.concept_id for c in out], [2, 3, 1])

    def test_importance_does_not_let_a_guide_swamp_the_draw(self) -> None:
        # The reason the draw is round-robin and not a global ranked
        # prefix. Values out-prior tastes 0.85 to 0.30, so a global sort
        # on importance x confidence would return values only.
        rows = [
            _c(i, kind="value", subject="aiko", confidence=0.9)
            for i in range(1, 11)
        ] + [
            _c(20 + i, kind="taste", subject="aiko", confidence=0.5)
            for i in range(3)
        ]
        out = _view(rows, importance_strength=0.4).for_consumer("stance")
        self.assertGreater(_kinds(out).get("value", 0), 0)
        self.assertGreater(_kinds(out).get("taste", 0), 0)

    def test_the_draw_is_deterministic(self) -> None:
        rows = self._lopsided()
        first = _view(rows).for_consumer("belief_inference")
        second = _view(rows).for_consumer("belief_inference")
        self.assertEqual(
            [c.concept_id for c in first], [c.concept_id for c in second],
        )


# ── the openness reserve ──────────────────────────────────────────────


class OpennessReserveTests(unittest.TestCase):
    def _pinned(self):
        """A store where every core-lane-eligible kind is well stocked and
        strong, plus some weaker generative rows."""
        rows = []
        cid = 0
        spec = (
            ("identity", 6, 0.95),
            ("value", 6, 0.93),
            ("boundary", 6, 0.90),
            ("generalization", 6, 0.88),
            ("aspiration", 2, 0.70),
            ("taste", 2, 0.66),
        )
        for kind, count, conf in spec:
            for i in range(count):
                cid += 1
                rows.append(_c(cid, kind=kind, confidence=conf - i * 0.01))
        return rows

    def test_without_the_reserve_the_lane_is_all_rails_and_ground(self) -> None:
        # The state of things before this change, asserted so the reason
        # for the reserve is visible in the suite rather than only in a
        # comment.
        out = _view(self._pinned()).core_lane(limit=8)
        roles = {get_kind(c.kind).role for c in out}
        self.assertNotIn(ROLE_GENERATIVE, roles)

    def test_the_reserve_puts_a_generative_concept_on_the_lane(self) -> None:
        out = _view(self._pinned()).core_lane(
            limit=8, openness_slots=2, openness_min_confidence=0.5,
        )
        generative = [
            c for c in out if get_kind(c.kind).role == ROLE_GENERATIVE
        ]
        self.assertEqual(len(generative), 2)
        self.assertEqual(len(out), 8)

    def test_the_reserve_balances_across_generative_kinds(self) -> None:
        out = _view(self._pinned()).core_lane(limit=8, openness_slots=2)
        reserved = [
            c.kind for c in out if get_kind(c.kind).role == ROLE_GENERATIVE
        ]
        self.assertEqual(sorted(reserved), ["aspiration", "taste"])

    def test_a_weak_generative_concept_is_not_pinned(self) -> None:
        # Pinning a half-formed aspiration into every single turn is worse
        # than pinning nothing.
        rows = [
            _c(1, kind="identity", confidence=0.95),
            _c(2, kind="aspiration", confidence=0.30),
        ]
        out = _view(rows).core_lane(
            limit=4, openness_slots=2, openness_min_confidence=0.5,
        )
        self.assertEqual([c.concept_id for c in out], [1])

    def test_an_unfillable_reserve_wastes_no_pin(self) -> None:
        rows = self._pinned()
        without = _view(
            [c for c in rows if get_kind(c.kind).role != ROLE_GENERATIVE]
        ).core_lane(limit=8, openness_slots=2)
        self.assertEqual(len(without), 8)

    def test_zero_slots_reproduces_the_old_lane_exactly(self) -> None:
        rows = self._pinned()
        self.assertEqual(
            [c.concept_id for c in _view(rows).core_lane(limit=8)],
            [
                c.concept_id
                for c in _view(rows).core_lane(limit=8, openness_slots=0)
            ],
        )

    def test_the_reserve_never_exceeds_the_cap(self) -> None:
        out = _view(self._pinned()).core_lane(limit=1, openness_slots=3)
        self.assertEqual(len(out), 1)

    def test_the_lane_is_byte_stable_across_calls(self) -> None:
        # The core lane sits in a cache-prefix-sensitive tier, so a
        # reserved pick that moved between two identical calls would
        # invalidate the prompt cache behind it every turn.
        rows = self._pinned()
        runs = [
            [
                (c.concept_id, c.label)
                for c in _view(rows).core_lane(limit=8, openness_slots=2)
            ]
            for _ in range(5)
        ]
        self.assertEqual(len(set(map(tuple, runs))), 1)

    def test_confidence_drift_does_not_resequence_the_reserve(self) -> None:
        # Banding is what buys that stability: L3 nudges confidence on
        # every tick, and raw ordering would trade neighbours constantly.
        before = _view([
            _c(1, kind="aspiration", confidence=0.7000),
            _c(2, kind="aspiration", confidence=0.6995),
        ]).core_lane(limit=2, openness_slots=2)
        after = _view([
            _c(1, kind="aspiration", confidence=0.6996),
            _c(2, kind="aspiration", confidence=0.6999),
        ]).core_lane(limit=2, openness_slots=2)
        self.assertEqual(
            [c.concept_id for c in before], [c.concept_id for c in after],
        )


if __name__ == "__main__":
    unittest.main()
