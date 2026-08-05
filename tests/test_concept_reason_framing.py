"""L41 reason-conditioned phrasing tests.

Covers:
- ``InnerLifePart1Mixin._reason_framing`` maps each L35 reason to its voice,
  guards the ``settled`` frame on confidence, and falls back to the confidence
  hedge for ``None`` / unknown / the four unmapped reasons.
- ``_render_relevant_concepts`` renders each reason's framing, keeps the plain
  confidence hedge for unmapped reasons, and reverts to the hedge everywhere
  when the ``concept_reason_framing_enabled`` master switch is off.
- The load-bearing invariant: no raw reason token and no mechanism word ever
  reaches the rendered text.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.session.inner_life_part1 import InnerLifePart1Mixin


# Tokens that must never leak into a rendered concept line. The raw L35
# reason tokens (concept_surfacing.REASON_*) plus the mechanism nouns their
# debug labels use -- if any of these appears, Aiko is reading her own
# machinery, which is exactly what L41 must not do.
_FORBIDDEN_TOKENS = (
    "core_belief",
    "topic_match",
    "high_confidence",
    "recently_reinforced",
    "settled_belief",
    "association",
    "unresolved_contradiction",
    "recently_revived",
    "loosening_boundary",
    "newly_promoted",
    "recent_change",
    "contradiction",
    "surfaced",
    "surface_reason",
    "confidence",
    "revived",
    "topic",
)


def _host(*, framing_enabled: bool = True):
    class _Host(InnerLifePart1Mixin):
        def __init__(self) -> None:
            self._concept_store = None  # supporting labels resolve to []
            self._settings = SimpleNamespace(
                agent=SimpleNamespace(
                    concept_reason_framing_enabled=framing_enabled
                )
            )

        @property
        def user_display_name(self) -> str:
            return "Jacob"

    return _Host()


def _concept(cid: int, label: str, *, confidence: float = 0.9):
    return SimpleNamespace(
        concept_id=cid,
        label=label,
        confidence=confidence,
        plasticity=0.3,
        kind="identity",
        subject="user",
        last_reinforced_at=None,
    )


class ReasonFramingHelperTests(unittest.TestCase):
    def test_settled_frame_when_confident(self) -> None:
        out = InnerLifePart1Mixin._reason_framing("settled_belief", 0.9)
        self.assertEqual(out, "You've long since made your mind up that")

    def test_settled_frame_falls_back_when_low_confidence(self) -> None:
        # Stability and confidence are different axes: a stable-but-unsure
        # concept must not claim "made your mind up".
        out = InnerLifePart1Mixin._reason_framing("settled_belief", 0.4)
        self.assertEqual(
            out, InnerLifePart1Mixin._hedge_for_confidence(0.4)
        )

    def test_freshly_changed_family_shares_one_voice(self) -> None:
        expected = "Lately you've come around to feeling that"
        for reason in (
            "recent_change",
            "loosening_boundary",
            "newly_promoted",
            "recently_revived",
        ):
            self.assertEqual(
                InnerLifePart1Mixin._reason_framing(reason, 0.7), expected
            )

    def test_association_frame(self) -> None:
        out = InnerLifePart1Mixin._reason_framing("association", 0.5)
        self.assertEqual(out, "Something here nudges the sense that")

    def test_contradiction_frame_is_restrained(self) -> None:
        out = InnerLifePart1Mixin._reason_framing(
            "unresolved_contradiction", 0.7
        )
        self.assertEqual(out, "You haven't fully settled it, but you sense that")
        # Restrained means it names no mechanism.
        self.assertNotIn("contradiction", out)

    def test_unmapped_reasons_use_confidence_hedge(self) -> None:
        for reason in (
            "topic_match",
            "high_confidence",
            "recently_reinforced",
            "core_belief",
        ):
            self.assertEqual(
                InnerLifePart1Mixin._reason_framing(reason, 0.9),
                InnerLifePart1Mixin._hedge_for_confidence(0.9),
            )

    def test_none_and_unknown_use_confidence_hedge(self) -> None:
        self.assertEqual(
            InnerLifePart1Mixin._reason_framing(None, 0.6),
            InnerLifePart1Mixin._hedge_for_confidence(0.6),
        )
        self.assertEqual(
            InnerLifePart1Mixin._reason_framing("made_up_reason", 0.6),
            InnerLifePart1Mixin._hedge_for_confidence(0.6),
        )


class RenderFramingTests(unittest.TestCase):
    def _render(self, reason: str, *, confidence: float = 0.9, framing=True):
        host = _host(framing_enabled=framing)
        c = _concept(1, "Jacob values owning his data", confidence=confidence)
        text, _ = host._render_relevant_concepts(
            [c], score_components={1: {"reason": reason}}
        )
        return text

    def test_settled_reason_renders_settled_voice(self) -> None:
        text = self._render("settled_belief")
        self.assertIn("You've long since made your mind up that", text)
        self.assertNotIn("You're fairly sure", text)

    def test_association_reason_renders_primed_voice(self) -> None:
        text = self._render("association")
        self.assertIn("Something here nudges the sense that", text)

    def test_change_reason_renders_freshly_changed_voice(self) -> None:
        text = self._render("recent_change")
        self.assertIn("Lately you've come around to feeling that", text)

    def test_unmapped_reason_keeps_confidence_hedge(self) -> None:
        text = self._render("topic_match")
        self.assertIn("You're fairly sure", text)
        self.assertNotIn("made your mind up", text)

    def test_toggle_off_reverts_to_hedge_everywhere(self) -> None:
        text = self._render("settled_belief", framing=False)
        self.assertIn("You're fairly sure", text)
        self.assertNotIn("made your mind up", text)

    def test_missing_score_components_keeps_hedge(self) -> None:
        host = _host()
        c = _concept(1, "Jacob values owning his data")
        text, _ = host._render_relevant_concepts([c])  # no score_components
        self.assertIn("You're fairly sure", text)

    def test_trace_still_records_confidence_hedge(self) -> None:
        # The framing change must not disturb the debug trace's ``hedge``
        # field (telemetry is unchanged).
        host = _host()
        c = _concept(1, "Jacob values owning his data")
        _, trace = host._render_relevant_concepts(
            [c], score_components={1: {"reason": "settled_belief"}}
        )
        entry = trace["surfaced"][0]
        self.assertEqual(entry["hedge"], "You're fairly sure")
        self.assertEqual(entry["surface_reason"], "settled_belief")


class AntiNarrationTests(unittest.TestCase):
    """No framing, under any reason, may leak a reason token or a mechanism
    word into the rendered text."""

    def test_no_reason_or_mechanism_token_reaches_output(self) -> None:
        reasons = (
            "core_belief",
            "topic_match",
            "high_confidence",
            "recently_reinforced",
            "settled_belief",
            "association",
            "unresolved_contradiction",
            "recently_revived",
            "loosening_boundary",
            "newly_promoted",
            "recent_change",
        )
        host = _host()
        concepts = [
            _concept(i, f"Jacob has trait number {i}")
            for i, _ in enumerate(reasons, start=1)
        ]
        components = {
            i: {"reason": reason}
            for i, reason in enumerate(reasons, start=1)
        }
        text, _ = host._render_relevant_concepts(
            concepts, score_components=components
        )
        lowered = text.lower()
        for token in _FORBIDDEN_TOKENS:
            self.assertNotIn(token, lowered, f"leaked mechanism token: {token}")


if __name__ == "__main__":
    unittest.main()
