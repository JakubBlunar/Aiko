"""Tests for K85e -- the pursuit lean block and the ``share`` want.

Two outlets for the same thing. The block is the lull permission slip
(one per conversation, shared with K81's taste lean); the want is the
slow-burn version that reaches K53 instead. Both exist because the
lull gate is narrow by design and a pursuit that only ever surfaced
during a stall would never be something she just brings up.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.session.inner_life_part3 import InnerLifePart3Mixin


class _FakeConcept:
    def __init__(self, kind: str, label: str, confidence: float = 0.8) -> None:
        self.kind = kind
        self.label = label
        self.confidence = confidence
        self.subject = "aiko"


class _FakeView:
    enabled = True

    def __init__(self, rows: list[_FakeConcept]) -> None:
        self._rows = rows
        self.asked: list[str] = []

    def core(
        self,
        *,
        subject: str | None = None,
        kind: str | None = None,
        min_confidence: float = 0.0,
        limit: int | None = None,
    ) -> list[_FakeConcept]:
        self.asked.append(str(kind))
        out = [
            r for r in self._rows
            if r.kind == kind and r.confidence >= min_confidence
        ]
        return out[:limit] if limit is not None else out


class _Host(InnerLifePart3Mixin):
    """The slice of ``SessionController`` the two lean blocks touch."""

    def __init__(
        self,
        view: _FakeView,
        *,
        enabled: bool = True,
        lull: float | None = 0.05,
        conduct: list[dict] | None = None,
        closeness: float = 0.8,
    ) -> None:
        self._view = view
        self._taste_lean_fired = False
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                taste_steer_enabled=True,
                taste_steer_widen_enabled=True,
                pursuit_lean_enabled=enabled,
                surfacing_conduct_enabled=conduct is not None,
                appetite_min_axes=0.15,
            ),
            assistant=SimpleNamespace(user_display_name="Jacob"),
        )
        self._memory_settings = SimpleNamespace(
            stagnation_mild_threshold=0.18,
            taste_steer_min_confidence=0.6,
        )
        self._user_id = "jacob"
        self._conduct = conduct or []
        self._chat_db = SimpleNamespace(kv_get=lambda _k: None)
        # A lull is a *low* mean distance -- the conversation circling --
        # measured against the band K18 publishes after calibration.
        self._topic_stagnation_detector = SimpleNamespace(
            last_mean=lull, mild_threshold=0.20,
        )
        self._relationship_axes_store = SimpleNamespace(
            get=lambda _uid: SimpleNamespace(
                closeness=closeness, comfort=closeness,
            ),
        )

    @property
    def user_display_name(self) -> str:
        return "Jacob"


def _render(host: _Host, which: str = "pursuit") -> str:
    import app.core.concepts.concept_view as cv
    import app.core.concepts.surfacing_conduct as sc

    view_original = cv.concept_view_from
    conduct_original = sc.load_conduct_snapshot
    cv.concept_view_from = lambda _h: host._view  # type: ignore[assignment]
    sc.load_conduct_snapshot = lambda _g: host._conduct  # type: ignore
    try:
        if which == "pursuit":
            return host._render_pursuit_lean_block()
        return host._render_taste_lean_block()
    finally:
        cv.concept_view_from = view_original  # type: ignore[assignment]
        sc.load_conduct_snapshot = conduct_original  # type: ignore


class PursuitLeanTests(unittest.TestCase):
    def test_an_active_pursuit_renders_the_slip(self) -> None:
        view = _FakeView([_FakeConcept("pursuit", "keeping a herb garden")])
        block = _render(_Host(view))
        self.assertIn("Something you've been up to", block)
        self.assertIn("keeping a herb garden", block)
        self.assertEqual(view.asked, ["pursuit"])

    def test_the_copy_asks_for_a_concrete_thing_not_a_topic(self) -> None:
        # The failure mode here is a hobbyhorse -- a quiet turn turned
        # into a monologue -- so the slip has to be an offer that can be
        # dropped, not a subject to steer onto.
        block = _render(
            _Host(_FakeView([_FakeConcept("pursuit", "keeping a herb garden")]))
        )
        self.assertIn("small concrete thing", block)
        self.assertIn("let it drop", block)
        self.assertNotIn("?", block.split("\n", 1)[1].split(".")[0])

    def test_no_pursuit_is_silence(self) -> None:
        view = _FakeView([_FakeConcept("taste", "long arguments about maps")])
        self.assertEqual(_render(_Host(view)), "")

    def test_a_candidate_grade_confidence_does_not_speak(self) -> None:
        view = _FakeView([
            _FakeConcept("pursuit", "keeping a herb garden", confidence=0.3),
        ])
        self.assertEqual(_render(_Host(view)), "")

    def test_the_switch_silences_it(self) -> None:
        view = _FakeView([_FakeConcept("pursuit", "keeping a herb garden")])
        self.assertEqual(_render(_Host(view, enabled=False)), "")


class GateTests(unittest.TestCase):
    """The pacing gate is shared with K81, so it is tested once here."""

    def _view(self) -> _FakeView:
        return _FakeView([_FakeConcept("pursuit", "keeping a herb garden")])

    def test_a_live_conversation_is_not_a_lull(self) -> None:
        self.assertEqual(_render(_Host(self._view(), lull=0.9)), "")

    def test_a_cold_detector_reads_as_no_fire(self) -> None:
        self.assertEqual(_render(_Host(self._view(), lull=None)), "")

    def test_a_cold_bond_reads_as_no_fire(self) -> None:
        self.assertEqual(_render(_Host(self._view(), closeness=0.02)), "")

    def test_an_l42_fixation_finding_suppresses_it(self) -> None:
        # A pursuit surfacing while she is already fixating is the
        # hobbyhorse the L42 counterweight exists to catch.
        host = _Host(self._view(), conduct=[{"shape": "fixation"}])
        self.assertEqual(_render(host), "")

    def test_a_benign_conduct_finding_does_not(self) -> None:
        host = _Host(self._view(), conduct=[{"shape": "breadth"}])
        self.assertNotEqual(_render(host), "")


class SharedLatchTests(unittest.TestCase):
    """One permission slip with two sources, not two slips."""

    def _both(self) -> _FakeView:
        return _FakeView([
            _FakeConcept("pursuit", "keeping a herb garden"),
            _FakeConcept("taste", "long arguments about map design"),
        ])

    def test_a_pursuit_closes_the_door_on_the_taste_lean(self) -> None:
        host = _Host(self._both())
        self.assertIn("herb garden", _render(host))
        self.assertEqual(_render(host, "taste"), "")

    def test_a_taste_closes_the_door_on_the_pursuit_lean(self) -> None:
        host = _Host(self._both())
        self.assertIn("map design", _render(host, "taste"))
        self.assertEqual(_render(host), "")

    def test_it_fires_at_most_once_per_conversation(self) -> None:
        host = _Host(self._both())
        self.assertNotEqual(_render(host), "")
        self.assertEqual(_render(host), "")


class OrderTests(unittest.TestCase):
    def test_pursuit_precedes_taste_in_the_tier_ladder(self) -> None:
        from app.core.session.prompt_assembler import _PROMPT_BLOCK_TIERS

        t6 = _PROMPT_BLOCK_TIERS["T6_detectors"]
        self.assertLess(
            t6.index("pursuit_lean_block"), t6.index("taste_lean_block"),
        )

    def test_the_block_has_a_handling_header(self) -> None:
        from app.core.session.prompt_support import HANDLING_SECTIONS

        self.assertIn("pursuit_lean_block", HANDLING_SECTIONS)


if __name__ == "__main__":
    unittest.main()
