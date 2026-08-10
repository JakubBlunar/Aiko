"""L28: the goals block leads with aspirations, floors on K1 goal rows.

L28 gated "goals meet aspirations" on L14, and when aspirations shipped
the gate lifted without anyone noticing -- ``app/core/goals/`` had no
concept reference at all. These tests cover the resulting block: which
source leads, that neither source alone silences it, and that the two
stay distinguishable, because an aspiration is who she is *becoming* and
a goal is an actionable to-do.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.session.inner_life_part1 import InnerLifePart1Mixin


class _FakeConcept:
    def __init__(
        self,
        label: str,
        *,
        kind: str = "aspiration",
        subject: str = "aiko",
        confidence: float = 0.8,
    ) -> None:
        self.label = label
        self.kind = kind
        self.subject = subject
        self.confidence = confidence
        self.status = "active"
        self.concept_id = abs(hash(label)) % 10_000


class _FakeView:
    def __init__(
        self,
        rows: list[_FakeConcept] | None = None,
        *,
        enabled: bool = True,
        raises: bool = False,
    ) -> None:
        self._rows = list(rows or [])
        self.enabled = enabled
        self._raises = raises
        self.consumers: list[str] = []

    def for_consumer(self, consumer, *, subject=None):
        self.consumers.append(str(consumer))
        if self._raises:
            raise RuntimeError("store is gone")
        return list(self._rows)


class _FakeGoal:
    def __init__(self, id_: int, content: str, metadata: dict | None = None):
        self.id = id_
        self.content = content
        self.metadata = metadata or {}


class _FakeGoalStore:
    def __init__(self, goals: list[_FakeGoal] | None = None, *, raises=False):
        self._goals = list(goals or [])
        self._raises = raises

    def list_active(self) -> list[_FakeGoal]:
        if self._raises:
            raise RuntimeError("db is gone")
        return list(self._goals)


class _Host(InnerLifePart1Mixin):
    """The slice of ``SessionController`` the goals block touches."""

    def __init__(
        self,
        *,
        goals: _FakeGoalStore | None = None,
        view: _FakeView | None = None,
        enabled: bool = True,
        max_rendered: int = 3,
    ) -> None:
        self._goal_store = goals
        self._view = view
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                goals_enabled=enabled,
                goals_max_rendered=max_rendered,
            ),
        )

    @property
    def user_display_name(self) -> str:
        return "Jacob"


def _render(host: _Host) -> str:
    import app.core.concepts.concept_view as cv

    original = cv.concept_view_from
    cv.concept_view_from = lambda _h: host._view  # type: ignore[assignment]
    try:
        return host._render_goals_block()
    finally:
        cv.concept_view_from = original  # type: ignore[assignment]


_GOALS = [_FakeGoal(1, "finish the greenhouse shelf")]


class AspirationLeadTests(unittest.TestCase):
    def test_aspirations_lead_the_goal_rows(self) -> None:
        block = _render(_Host(
            goals=_FakeGoalStore(_GOALS),
            view=_FakeView([_FakeConcept("someone who finishes things")]),
        ))
        self.assertLess(
            block.index("someone who finishes things"),
            block.index("greenhouse shelf"),
        )

    def test_an_aspiration_is_not_dressed_up_as_a_to_do(self) -> None:
        # Both arrive as bullets under one header, so without the prefix
        # the prompt cannot tell a direction from a chore -- and the whole
        # distinction the concept kind draws would be lost in rendering.
        block = _render(_Host(
            goals=_FakeGoalStore(_GOALS),
            view=_FakeView([_FakeConcept("someone who finishes things")]),
        ))
        self.assertIn("- becoming: someone who finishes things", block)
        self.assertIn("- finish the greenhouse shelf", block)

    def test_aspirations_alone_still_render(self) -> None:
        # The point of leading with them: the block used to return early on
        # an empty goal list, so a rich aspiration set said nothing.
        block = _render(_Host(
            goals=_FakeGoalStore([]),
            view=_FakeView([_FakeConcept("someone who finishes things")]),
        ))
        self.assertIn("someone who finishes things", block)
        self.assertIn("these are her own", block)

    def test_goals_alone_render_exactly_as_before(self) -> None:
        block = _render(_Host(goals=_FakeGoalStore(_GOALS), view=_FakeView([])))
        self.assertIn("- finish the greenhouse shelf", block)
        self.assertNotIn("becoming", block)

    def test_neither_source_is_silence(self) -> None:
        self.assertEqual(
            _render(_Host(goals=_FakeGoalStore([]), view=_FakeView([]))), "",
        )

    def test_aspirations_cannot_crowd_out_the_goals(self) -> None:
        block = _render(_Host(
            goals=_FakeGoalStore(_GOALS),
            view=_FakeView([
                _FakeConcept(f"aspiration number {i}") for i in range(6)
            ]),
        ))
        self.assertEqual(block.count("- becoming:"), 2)
        self.assertIn("greenhouse shelf", block)

    def test_a_repeated_label_contributes_one_line(self) -> None:
        block = _render(_Host(
            goals=_FakeGoalStore(_GOALS),
            view=_FakeView([
                _FakeConcept("someone who finishes things"),
                _FakeConcept("Someone who finishes things"),
            ]),
        ))
        self.assertEqual(block.count("becoming:"), 1)

    def test_it_reads_the_declared_diet(self) -> None:
        view = _FakeView([_FakeConcept("someone who finishes things")])
        _render(_Host(goals=_FakeGoalStore(_GOALS), view=view))
        self.assertEqual(view.consumers, ["goals_block"])


class DegradationTests(unittest.TestCase):
    def test_a_cold_view_leaves_the_block_as_it_was(self) -> None:
        block = _render(_Host(
            goals=_FakeGoalStore(_GOALS),
            view=_FakeView(
                [_FakeConcept("someone who finishes things")], enabled=False,
            ),
        ))
        self.assertIn("greenhouse shelf", block)
        self.assertNotIn("becoming", block)

    def test_a_broken_view_leaves_the_block_as_it_was(self) -> None:
        block = _render(_Host(
            goals=_FakeGoalStore(_GOALS), view=_FakeView(raises=True),
        ))
        self.assertIn("greenhouse shelf", block)

    def test_a_broken_goal_store_still_renders_aspirations(self) -> None:
        # Each source floors the other; a failure on one side is not a
        # reason for the block to disappear.
        block = _render(_Host(
            goals=_FakeGoalStore(raises=True),
            view=_FakeView([_FakeConcept("someone who finishes things")]),
        ))
        self.assertIn("someone who finishes things", block)

    def test_no_goal_store_at_all_still_renders_aspirations(self) -> None:
        block = _render(_Host(
            goals=None,
            view=_FakeView([_FakeConcept("someone who finishes things")]),
        ))
        self.assertIn("someone who finishes things", block)

    def test_the_feature_switch_silences_both_sources(self) -> None:
        self.assertEqual(
            _render(_Host(
                goals=_FakeGoalStore(_GOALS),
                view=_FakeView([_FakeConcept("someone who finishes things")]),
                enabled=False,
            )),
            "",
        )


if __name__ == "__main__":
    unittest.main()
