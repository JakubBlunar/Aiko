"""Tests for ``app.core.affect.mood_shell``.

Covers the K5 mood shell tilt: band classification, dominant-axis
selection (threshold + canonical-order tie-breaking), rule lookup
priority (specific (band, axis) before fallback (band, None)), the
"no shell" zones (neutral-mid affect, cold-start no-affect, disabled
flag), and the rendered ``Tone shell: ...`` line.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass

from app.core.affect.mood_shell import (
    MoodShell,
    derive_mood_shell,
    render_mood_shell_block,
)


@dataclass
class _AffectStub:
    valence: float = 0.0
    arousal: float = 0.4


@dataclass
class _AxesStub:
    closeness: float = 0.0
    humor: float = 0.0
    trust: float = 0.0
    comfort: float = 0.0


class BandClassificationTests(unittest.TestCase):
    def test_neutral_mid_returns_none(self) -> None:
        # The neutral-valence + mid-arousal cell is the "no shell" zone.
        shell = derive_mood_shell(
            affect=_AffectStub(valence=0.0, arousal=0.4),
            axes=None,
        )
        self.assertIsNone(shell)

    def test_no_affect_returns_none(self) -> None:
        shell = derive_mood_shell(affect=None, axes=None)
        self.assertIsNone(shell)

    def test_disabled_returns_none(self) -> None:
        shell = derive_mood_shell(
            affect=_AffectStub(valence=0.8, arousal=0.8),
            axes=_AxesStub(humor=0.9),
            enabled=False,
        )
        self.assertIsNone(shell)


class DominantAxisTests(unittest.TestCase):
    def test_below_threshold_axes_are_ignored(self) -> None:
        # All axes below the 0.5 threshold → fallback rule fires.
        shell = derive_mood_shell(
            affect=_AffectStub(valence=0.5, arousal=0.5),
            axes=_AxesStub(closeness=0.3, humor=0.3),
        )
        self.assertIsNotNone(shell)
        # Fallback rule for pos_mid carries no axis contributor.
        assert shell is not None
        self.assertTrue(
            all("axis=" not in c for c in shell.contributors)
        )

    def test_largest_absolute_axis_wins(self) -> None:
        # humor=0.55, closeness=0.85 → closeness dominates.
        shell = derive_mood_shell(
            affect=_AffectStub(valence=0.5, arousal=0.5),
            axes=_AxesStub(closeness=0.85, humor=0.55),
        )
        assert shell is not None
        self.assertEqual(shell.tilt, "affectionate_steady")
        self.assertTrue(
            any("axis=closeness" in c for c in shell.contributors)
        )

    def test_require_axis_short_circuits_when_no_axis_crosses(
        self,
    ) -> None:
        shell = derive_mood_shell(
            affect=_AffectStub(valence=0.6, arousal=0.5),
            axes=None,
            require_axis=True,
        )
        self.assertIsNone(shell)


class TiltRuleLookupTests(unittest.TestCase):
    def test_pos_high_humor_is_playful_easy(self) -> None:
        shell = derive_mood_shell(
            affect=_AffectStub(valence=0.7, arousal=0.8),
            axes=_AxesStub(humor=0.7),
        )
        assert shell is not None
        self.assertEqual(shell.tilt, "playful_easy")
        self.assertIn("laughing", shell.line)

    def test_pos_mid_closeness_is_affectionate_steady(self) -> None:
        shell = derive_mood_shell(
            affect=_AffectStub(valence=0.5, arousal=0.45),
            axes=_AxesStub(closeness=0.7),
        )
        assert shell is not None
        self.assertEqual(shell.tilt, "affectionate_steady")
        self.assertIn("affectionate", shell.line)

    def test_neg_high_falls_back_to_anchor_steady(self) -> None:
        shell = derive_mood_shell(
            affect=_AffectStub(valence=-0.7, arousal=0.8),
            axes=None,
        )
        assert shell is not None
        self.assertEqual(shell.tilt, "anchor_steady")

    def test_neg_mid_comfort_is_soft_repair(self) -> None:
        shell = derive_mood_shell(
            affect=_AffectStub(valence=-0.5, arousal=0.45),
            axes=_AxesStub(comfort=0.6),
        )
        assert shell is not None
        self.assertEqual(shell.tilt, "soft_repair")

    def test_neu_high_with_humor_is_alert_playful(self) -> None:
        shell = derive_mood_shell(
            affect=_AffectStub(valence=0.0, arousal=0.75),
            axes=_AxesStub(humor=0.6),
        )
        assert shell is not None
        self.assertEqual(shell.tilt, "alert_playful")

    def test_neu_low_falls_back_to_steady_quiet(self) -> None:
        shell = derive_mood_shell(
            affect=_AffectStub(valence=0.0, arousal=0.2),
            axes=None,
        )
        assert shell is not None
        self.assertEqual(shell.tilt, "steady_quiet")


class RenderingTests(unittest.TestCase):
    def test_render_prefixes_with_tone_shell(self) -> None:
        shell = MoodShell(
            tilt="affectionate_steady",
            line="Lean affectionate and unhurried; let warmth show.",
            contributors=[],
        )
        rendered = render_mood_shell_block(shell)
        self.assertTrue(rendered.startswith("Tone shell:"))
        self.assertIn("affectionate", rendered)

    def test_render_none_returns_empty_string(self) -> None:
        self.assertEqual(render_mood_shell_block(None), "")

    def test_render_empty_line_returns_empty_string(self) -> None:
        shell = MoodShell(tilt="x", line="   ", contributors=[])
        self.assertEqual(render_mood_shell_block(shell), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
