"""Tests for the K81 taste-lean block and its K85a widened read.

The block reads only ``subject="aiko"`` concepts, so the interesting
behaviour is which of them count as hers: two taste rows exist on the
live store, while a hundred value / aspiration / identity rows do -- and
three quarters of those name the user, which makes them useless for
breaking a lull with something of her own.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.concepts.own_subject import is_bond_scoped, own_subjects
from app.core.session.inner_life_part3 import InnerLifePart3Mixin


class BondScopeTests(unittest.TestCase):
    def test_naming_the_user_is_bond_scoped(self) -> None:
        self.assertTrue(
            is_bond_scoped(
                "I value grounding our closeness in Jacob's world", "Jacob",
            )
        )

    def test_second_person_is_bond_scoped(self) -> None:
        self.assertTrue(is_bond_scoped("I like how you explain things"))

    def test_first_person_plural_is_bond_scoped(self) -> None:
        self.assertTrue(is_bond_scoped("I value our shared rituals"))

    def test_her_own_position_survives(self) -> None:
        self.assertFalse(
            is_bond_scoped(
                "I'm growing into someone who finds joy in quiet, domestic "
                "observation and creative expression.",
                "Jacob",
            )
        )

    def test_her_own_third_person_is_not_filtered(self) -> None:
        # "her" shows up in her own self-descriptions; filtering on it
        # would throw away the most self-directed material there is.
        self.assertFalse(
            is_bond_scoped("anchors her attention in the quiet details")
        )

    def test_the_name_must_be_a_whole_word(self) -> None:
        self.assertFalse(is_bond_scoped("I enjoy jacobean drama", "Jacob"))

    def test_own_subjects_keeps_order(self) -> None:
        rows = [
            SimpleNamespace(label="I value our rituals"),
            SimpleNamespace(label="I value slow mornings"),
            SimpleNamespace(label="I value quiet rooms"),
        ]
        kept = own_subjects(rows, "Jacob")
        self.assertEqual(
            [r.label for r in kept],
            ["I value slow mornings", "I value quiet rooms"],
        )


class _FakeConcept:
    def __init__(self, kind: str, label: str, confidence: float = 0.8) -> None:
        self.kind = kind
        self.label = label
        self.confidence = confidence
        self.subject = "aiko"


class _FakeView:
    """Stands in for ``ConceptView``, serving rows by kind."""

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
    """The slice of ``SessionController`` the lean block touches."""

    def __init__(self, view: _FakeView, *, widen: bool = True) -> None:
        self._view = view
        self._taste_lean_fired = False
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                taste_steer_enabled=True,
                taste_steer_widen_enabled=widen,
                surfacing_conduct_enabled=False,
                appetite_min_axes=0.15,
            ),
            assistant=SimpleNamespace(user_display_name="Jacob"),
        )
        self._memory_settings = SimpleNamespace(
            stagnation_mild_threshold=0.18,
            taste_steer_min_confidence=0.6,
        )
        self._user_id = "jacob"
        self._chat_db = None
        # A lull is a *low* mean distance -- the conversation circling.
        self._topic_stagnation_detector = SimpleNamespace(
            last_mean=0.10, mild_threshold=0.20,
        )
        self._relationship_axes_store = SimpleNamespace(
            get=lambda _uid: SimpleNamespace(closeness=0.8, comfort=0.8),
        )

    @property
    def user_display_name(self) -> str:
        return "Jacob"


def _render(host: _Host) -> str:
    import app.core.concepts.concept_view as cv

    original = cv.concept_view_from
    cv.concept_view_from = lambda _h: host._view  # type: ignore[assignment]
    try:
        return host._render_taste_lean_block()
    finally:
        cv.concept_view_from = original  # type: ignore[assignment]


class WidenedReadTests(unittest.TestCase):
    def test_a_taste_still_wins_and_reads_as_a_topic(self) -> None:
        view = _FakeView([
            _FakeConcept("taste", "long arguments about map design"),
            _FakeConcept("aspiration", "I'm growing into someone patient"),
        ])
        block = _render(_Host(view))
        self.assertIn("Leaning toward what you love", block)
        self.assertIn("map design", block)
        self.assertIn("steer gently toward it", block)

    def test_an_aspiration_backs_a_missing_taste(self) -> None:
        view = _FakeView([
            _FakeConcept(
                "aspiration",
                "I'm growing into someone who finds joy in quiet, domestic "
                "observation.",
            ),
        ])
        block = _render(_Host(view))
        self.assertIn("Something of yours to put on the table", block)
        self.assertIn("quiet, domestic observation", block)
        # A value is not a topic: asking her to steer onto one produces
        # a lecture, so the copy asks her to state it instead.
        self.assertNotIn("steer gently toward it", block)

    def test_bond_scoped_rows_are_not_hers_to_lean_on(self) -> None:
        view = _FakeView([
            _FakeConcept("aspiration", "I'm growing closer to Jacob"),
            _FakeConcept("value", "I value our shared rituals"),
            _FakeConcept("identity", "I am the one who waits for him"),
        ])
        self.assertEqual(_render(_Host(view)), "")

    def test_the_kinds_are_tried_in_order(self) -> None:
        view = _FakeView([
            _FakeConcept("value", "I value slow mornings"),
            _FakeConcept("identity", "I am a slow reader"),
        ])
        block = _render(_Host(view))
        self.assertIn("slow mornings", block)
        self.assertEqual(view.asked[:3], ["taste", "aspiration", "value"])

    def test_the_switch_restores_the_taste_only_read(self) -> None:
        view = _FakeView([_FakeConcept("value", "I value slow mornings")])
        self.assertEqual(_render(_Host(view, widen=False)), "")

    def test_the_confidence_bar_still_applies(self) -> None:
        view = _FakeView([
            _FakeConcept("value", "I value slow mornings", confidence=0.4),
        ])
        self.assertEqual(_render(_Host(view)), "")

    def test_it_fires_at_most_once_per_conversation(self) -> None:
        view = _FakeView([_FakeConcept("value", "I value slow mornings")])
        host = _Host(view)
        self.assertNotEqual(_render(host), "")
        self.assertEqual(_render(host), "")


if __name__ == "__main__":
    unittest.main()
