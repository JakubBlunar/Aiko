"""L5 concept surfacing: ``_render_concept_block`` render logic.

Covers confidence-scaled hedging, the item cap, the confidence floor,
{user_name} personalisation, and the silent paths (feature off, no
store, immature graph, nothing eligible).
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.session.inner_life_part1 import InnerLifePart1Mixin


class _Concept:
    def __init__(self, label: str, confidence: float, *, status="active",
                 subject="user", kind="identity") -> None:
        self.label = label
        self.confidence = confidence
        self.status = status
        self.subject = subject
        self.kind = kind


class _ConceptStore:
    def __init__(self, concepts) -> None:
        self._concepts = concepts

    def list_by(self, *, status=None, subject=None, kind=None, **_kw):
        return [
            c
            for c in self._concepts
            if (status is None or c.status == status)
            and (subject is None or c.subject == subject)
            and (kind is None or c.kind == kind)
        ]


class _Graph:
    def __init__(self, mature: bool = True) -> None:
        self._mature = mature

    def mature(self, *, min_clusters: int, min_members: int = 0) -> bool:
        return self._mature


class _Host(InnerLifePart1Mixin):
    """Minimal stand-in exposing just what ``_render_concept_block`` reads."""

    def __init__(self, *, concepts=None, graph=None, concepts_enabled=True,
                 block_enabled=True, max_items=3, min_conf=0.55,
                 min_clusters=6, user_name="Jacob") -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                concepts_enabled=concepts_enabled,
                concept_block_enabled=block_enabled,
            )
        )
        self._memory_settings = SimpleNamespace(
            concept_surface_max_items=max_items,
            concept_surface_min_confidence=min_conf,
            concept_min_clusters=min_clusters,
        )
        self._concept_store = (
            _ConceptStore(concepts) if concepts is not None else None
        )
        self._topic_graph = graph
        self._user_name = user_name

    @property
    def user_display_name(self) -> str:
        return self._user_name


class ConceptBlockRenderTests(unittest.TestCase):
    def test_silent_when_concepts_disabled(self) -> None:
        host = _Host(
            concepts=[_Concept("enjoys systems", 0.9)],
            graph=_Graph(True),
            concepts_enabled=False,
        )
        self.assertEqual(host._render_concept_block(), "")

    def test_silent_when_block_disabled(self) -> None:
        host = _Host(
            concepts=[_Concept("enjoys systems", 0.9)],
            graph=_Graph(True),
            block_enabled=False,
        )
        self.assertEqual(host._render_concept_block(), "")

    def test_silent_when_no_store(self) -> None:
        host = _Host(concepts=None, graph=_Graph(True))
        self.assertEqual(host._render_concept_block(), "")

    def test_silent_when_graph_immature(self) -> None:
        host = _Host(
            concepts=[_Concept("enjoys systems", 0.9)],
            graph=_Graph(False),
        )
        self.assertEqual(host._render_concept_block(), "")

    def test_silent_when_nothing_clears_confidence_floor(self) -> None:
        host = _Host(
            concepts=[_Concept("shaky guess", 0.4)],
            graph=_Graph(True),
            min_conf=0.55,
        )
        self.assertEqual(host._render_concept_block(), "")

    def test_only_active_user_identity_surfaced(self) -> None:
        concepts = [
            _Concept("dormant one", 0.9, status="dormant"),
            _Concept("about aiko", 0.9, subject="aiko"),
            _Concept("relationship thing", 0.9, subject="relationship"),
            _Concept("active user trait", 0.9),
        ]
        host = _Host(concepts=concepts, graph=_Graph(True))
        out = host._render_concept_block()
        self.assertIn("active user trait", out)
        self.assertNotIn("dormant one", out)
        self.assertNotIn("about aiko", out)
        self.assertNotIn("relationship thing", out)

    def test_personalised_and_hedged(self) -> None:
        host = _Host(
            concepts=[_Concept("enjoys understanding systems", 0.9)],
            graph=_Graph(True),
            user_name="Jacob",
        )
        out = host._render_concept_block()
        self.assertIn("Jacob", out)
        self.assertIn("You're fairly sure", out)
        # Framed as an impression to hold lightly, not a fact.
        self.assertIn("impressions", out.lower())

    def test_confidence_scaled_wording(self) -> None:
        host = _Host(
            concepts=[
                _Concept("high one", 0.85),
                _Concept("mid one", 0.7),
                _Concept("low one", 0.58),
            ],
            graph=_Graph(True),
            max_items=3,
        )
        out = host._render_concept_block()
        self.assertIn("You're fairly sure high one", out)
        self.assertIn("You have a sense that mid one", out)
        self.assertIn("You have a loose impression that low one", out)

    def test_capped_and_ordered_by_confidence(self) -> None:
        concepts = [
            _Concept("c-low", 0.6),
            _Concept("c-top", 0.95),
            _Concept("c-mid", 0.8),
            _Concept("c-extra", 0.7),
        ]
        host = _Host(concepts=concepts, graph=_Graph(True), max_items=2)
        out = host._render_concept_block()
        lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
        self.assertEqual(len(lines), 2)
        self.assertIn("c-top", lines[0])
        self.assertIn("c-mid", lines[1])

    def test_max_items_zero_silent(self) -> None:
        host = _Host(
            concepts=[_Concept("enjoys systems", 0.9)],
            graph=_Graph(True),
            max_items=0,
        )
        self.assertEqual(host._render_concept_block(), "")


class HedgeHelperTests(unittest.TestCase):
    def test_bands(self) -> None:
        f = InnerLifePart1Mixin._hedge_for_confidence
        self.assertEqual(f(0.9), "You're fairly sure")
        self.assertEqual(f(0.7), "You have a sense that")
        self.assertEqual(f(0.55), "You have a loose impression that")


if __name__ == "__main__":
    unittest.main()
