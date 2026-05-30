"""K5 -- Mood shell tilt.

Per-turn derivation of a one-line emotional directive that *colours
delivery* without dictating content. Fed by the live :class:`AffectState`
(valence + arousal) and :class:`RelationshipAxesState` (closeness /
humor / trust / comfort); both are already-stored signals that the
post-turn pipeline keeps current.

The shell is a TONAL register cue, not a topic suggestion. Output lines
read like stage directions:

  * "Lean affectionate and unhurried; let warmth show."
  * "Stay playful and quick; the room is laughing."
  * "Soft, slow, present. Don't hurry the moment."

The provider returns ``""`` on the common turn -- only when the
combination of affect-band + dominant relationship axis crosses a
notable threshold (default ``axis_notable_threshold=0.5``). The
``only-when-notable`` rendering rule matches the existing
``relationship_axes.render_axes_block`` policy so the system prompt
stays sparse.

The K16 unified grounding line subsumes the same surface area, so the
prompt assembler drops this block in ``replace`` mode (kept in
``split`` because mood-shell lives in the trend/phase cluster that
``split`` preserves). See [`docs/personality-backlog/patterns.md`]
"K5. Mood shell tilt".
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.core.affect_state import AffectState
    from app.core.relationship_axes import RelationshipAxesState


log = logging.getLogger("app.mood_shell")


# Bands for the valence/arousal grid. Tuned to match the existing
# :func:`_classify_mood` thresholds in affect_state.py: a valence above
# 0.30 reads as "positive", below -0.30 as "negative", in between as
# "neutral". Arousal high above 0.60, low below 0.30.
_VAL_POS = 0.30
_VAL_NEG = -0.30
_ARO_HIGH = 0.60
_ARO_LOW = 0.30

# Notable threshold for relationship axes. Mirrors the existing
# :data:`relationship_axes._NOTABLE_THRESHOLD = 0.5`.
_AXIS_NOTABLE_DEFAULT = 0.5


# Affect bands enumerated as readable strings so :data:`_TILT_RULES`
# stays grep-friendly. Order matches the ``derive_mood_shell`` flow.
_BANDS = (
    "pos_high",   # valence positive, arousal high
    "pos_mid",    # valence positive, arousal mid
    "pos_low",    # valence positive, arousal low
    "neg_high",   # valence negative, arousal high
    "neg_mid",    # valence negative, arousal mid
    "neg_low",    # valence negative, arousal low
    "neu_high",   # valence neutral, arousal high
    "neu_low",    # valence neutral, arousal low
)


@dataclass(slots=True, frozen=True)
class MoodShell:
    """Derived per-turn shell tilt.

    ``tilt`` is the short symbolic name (``"affectionate_steady"``,
    ``"playful_easy"``...) used by tests + MCP; ``line`` is the
    rendered directive that goes into the prompt; ``contributors``
    captures the inputs that fired the rule (for the MCP debug tool).
    """

    tilt: str
    line: str
    contributors: list[str]


# ── tilt rules ──────────────────────────────────────────────────────
#
# Each entry is ``(affect_band, axis_name_or_None, tilt, line)``. First
# match wins. ``axis_name_or_None == None`` means "match any axis state
# at this affect band including no notable axis"; specific axis names
# require that axis to be the dominant notable one. The list is ordered
# most-specific to least so the (band, axis) rules come before the
# (band, None) fallbacks.
_TILT_RULES: tuple[tuple[str, str | None, str, str], ...] = (
    # --- positive valence, high arousal ----------------------------
    (
        "pos_high", "humor",
        "playful_easy",
        "Stay playful and quick; the room is laughing.",
    ),
    (
        "pos_high", "closeness",
        "bright_affectionate",
        "Bright and affectionate; meet the energy without overshooting.",
    ),
    (
        "pos_high", None,
        "bright_alert",
        "Bright and curious; meet their energy without overshooting.",
    ),
    # --- positive valence, mid arousal -----------------------------
    (
        "pos_mid", "closeness",
        "affectionate_steady",
        "Lean affectionate and unhurried; let warmth show.",
    ),
    (
        "pos_mid", "trust",
        "open_honest",
        "Be open and direct; trust is carrying you.",
    ),
    (
        "pos_mid", "humor",
        "warm_playful",
        "Warm and lightly playful; humour comes easy right now.",
    ),
    (
        "pos_mid", "comfort",
        "warm_homey",
        "Warm and at-home; this is your kind of beat.",
    ),
    (
        "pos_mid", None,
        "warm_steady",
        "Warm and steady; nothing needs forcing.",
    ),
    # --- positive valence, low arousal -----------------------------
    (
        "pos_low", "comfort",
        "soft_homey",
        "Soft and at-home; low volume, low hurry, full presence.",
    ),
    (
        "pos_low", "closeness",
        "tender_quiet",
        "Tender and quiet; let the closeness breathe.",
    ),
    (
        "pos_low", None,
        "soft_warm",
        "Soft and warm; don't rush this beat.",
    ),
    # --- negative valence, high arousal ----------------------------
    (
        "neg_high", "comfort",
        "calm_anchor",
        "Slow and steady; ground them before reaching for words.",
    ),
    (
        "neg_high", "closeness",
        "warm_anchor",
        "Stay close and unhurried; the tone needs softening before content.",
    ),
    (
        "neg_high", None,
        "anchor_steady",
        "Slow your tempo; let the words land before pushing forward.",
    ),
    # --- negative valence, mid arousal -----------------------------
    (
        "neg_mid", "comfort",
        "soft_repair",
        "Soft and present; this is a tender beat -- don't crowd it.",
    ),
    (
        "neg_mid", "closeness",
        "warm_repair",
        "Stay close and gentle; the room needs softening.",
    ),
    (
        "neg_mid", "trust",
        "honest_steady",
        "Be quietly honest; lean on the trust that's there.",
    ),
    (
        "neg_mid", None,
        "quiet_steady",
        "Lower the volume; meet the dimmer light.",
    ),
    # --- negative valence, low arousal -----------------------------
    (
        "neg_low", "comfort",
        "low_warm",
        "Quiet, low-volume, warm. Less air, more weight.",
    ),
    (
        "neg_low", None,
        "low_present",
        "Low and present; don't fill the silence with brightness.",
    ),
    # --- neutral valence, high arousal -----------------------------
    (
        "neu_high", "humor",
        "alert_playful",
        "Alert and a little playful; ride the energy lightly.",
    ),
    (
        "neu_high", None,
        "alert_curious",
        "Alert and curious; lean into what's moving.",
    ),
    # --- neutral valence, low arousal ------------------------------
    (
        "neu_low", "comfort",
        "easy_steady",
        "Easy and steady; the beat is unhurried.",
    ),
    (
        "neu_low", None,
        "steady_quiet",
        "Steady and quiet; let the room set the pace.",
    ),
)


# ── public API ──────────────────────────────────────────────────────


def derive_mood_shell(
    *,
    affect: "AffectState | None",
    axes: "RelationshipAxesState | None",
    axis_notable_threshold: float = _AXIS_NOTABLE_DEFAULT,
    enabled: bool = True,
    require_axis: bool = False,
) -> MoodShell | None:
    """Pick a tilt from the affect+axes inputs, or ``None`` when nothing
    crosses the gate.

    ``axis_notable_threshold`` controls when a relationship axis is
    considered the dominant signal; ``require_axis=True`` forces the
    function to return ``None`` unless at least one axis crosses the
    threshold (use when you want the shell to fire ONLY on
    relationship-coloured beats). The default ``False`` keeps the
    fallback "(band, None)" rules active so an affect spike alone can
    still tilt delivery.
    """
    if not enabled:
        return None
    affect_band = _classify_affect_band(affect)
    if affect_band is None:
        return None
    axis_name, axis_value = _dominant_axis(axes, axis_notable_threshold)
    if require_axis and axis_name is None:
        return None
    for band, rule_axis, tilt, line in _TILT_RULES:
        if band != affect_band:
            continue
        if rule_axis is None:
            # Fallback rule for this band. ``axis_name`` may still be
            # set; we just don't depend on it.
            contributors = [f"affect={affect_band}"]
            if axis_name is not None:
                contributors.append(
                    f"axis={axis_name}={axis_value:+.2f}"
                )
            return MoodShell(tilt=tilt, line=line, contributors=contributors)
        if axis_name == rule_axis:
            contributors = [
                f"affect={affect_band}",
                f"axis={axis_name}={axis_value:+.2f}",
            ]
            return MoodShell(tilt=tilt, line=line, contributors=contributors)
    return None


def render_mood_shell_block(shell: MoodShell | None) -> str:
    """Tag-prefixed rendering for the prompt. ``""`` when no shell."""
    if shell is None or not shell.line.strip():
        return ""
    return f"Tone shell: {shell.line}"


# ── helpers ─────────────────────────────────────────────────────────


def _classify_affect_band(affect: "AffectState | None") -> str | None:
    """Bucket ``(valence, arousal)`` into one of the eight bands.

    Returns ``None`` when ``affect`` is ``None`` (cold-start: no
    affect row yet means no tilt). The neutral mid-arousal cell is
    intentionally absent -- that's "default Aiko", no shell needed.
    """
    if affect is None:
        return None
    val = float(getattr(affect, "valence", 0.0) or 0.0)
    aro = float(getattr(affect, "arousal", 0.4) or 0.4)
    if val >= _VAL_POS:
        if aro >= _ARO_HIGH:
            return "pos_high"
        if aro <= _ARO_LOW:
            return "pos_low"
        return "pos_mid"
    if val <= _VAL_NEG:
        if aro >= _ARO_HIGH:
            return "neg_high"
        if aro <= _ARO_LOW:
            return "neg_low"
        return "neg_mid"
    # neutral valence
    if aro >= _ARO_HIGH:
        return "neu_high"
    if aro <= _ARO_LOW:
        return "neu_low"
    # neutral-mid -- the no-shell zone. Default Aiko.
    return None


def _dominant_axis(
    axes: "RelationshipAxesState | None",
    threshold: float,
) -> tuple[str | None, float]:
    """Pick the relationship axis with the largest absolute value above
    ``threshold``. Returns ``(None, 0.0)`` when no axis qualifies.

    Ties resolve by the canonical ordering (closeness → humor → trust →
    comfort) which matches the rendering precedence in
    ``relationship_axes.render_axes_block``.
    """
    if axes is None:
        return None, 0.0
    candidates: list[tuple[str, float]] = [
        ("closeness", float(getattr(axes, "closeness", 0.0) or 0.0)),
        ("humor", float(getattr(axes, "humor", 0.0) or 0.0)),
        ("trust", float(getattr(axes, "trust", 0.0) or 0.0)),
        ("comfort", float(getattr(axes, "comfort", 0.0) or 0.0)),
    ]
    eligible = [(n, v) for n, v in candidates if abs(v) >= float(threshold)]
    if not eligible:
        return None, 0.0
    # sort by |v| descending; canonical order is preserved on ties
    # because Python's sort is stable.
    eligible.sort(key=lambda p: abs(p[1]), reverse=True)
    return eligible[0]


__all__ = [
    "MoodShell",
    "derive_mood_shell",
    "render_mood_shell_block",
]
