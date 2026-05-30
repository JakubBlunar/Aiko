"""Affect rupture-and-repair detection (K8).

Per-turn detector that fires when {user_name}'s mood drops sharply
*right after Aiko's last reply*. The signal is one-shot: the
post-turn flow stashes a :class:`RuptureResult` and the inner-life
provider consumes it on the very next turn so Aiko softens, checks
in once gently, and moves on without camping on the rupture.

The detector is *not* a generic "they're sad" cue (that already
lands in the affect / mood blocks). What's distinct here is the
*pre/post* delta on a single turn: their valence dropped by more
than ``threshold`` between right-before and right-after Aiko's
reply, and Aiko's reply was *not* an empathetic / gentle reaction
(those wouldn't be the cause of the dip; her own concern would
falsely trigger the cue).

Inputs are scalar -- no tables, no LLM, no embedder. The math is
``state.valence - affect_before.valence`` where both are taken
from the existing :class:`app.core.affect_state.AffectStore`
already snapshotted in :meth:`post_turn_mixin._post_turn_inner_life`.

Two architectural choices:

1.  Reaction filter: when Aiko's reply itself was already gentle /
    sad / concerned / calm, an observed valence drop is much more
    likely to be the user's pre-existing state surfacing rather
    than a beat that landed wrong. Skipping these reactions
    avoids a false-positive loop where Aiko apologises for being
    empathetic.
2.  Threshold floor (``rupture_valence_drop_threshold = 0.12``):
    the AffectUpdater uses ``_ALPHA = 0.35`` and per-reaction
    impulses around ±0.20, so a single-turn drop of ≥0.12 is a
    real shift in target, not residual smoothing noise.

The post-turn flow handles the one-shot consumption contract by
stashing the result on the ``SessionController``; the provider
clears the slot after a single render.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass


log = logging.getLogger("app.affect_rupture_detector")


# Default reactions where a post-turn valence drop is *expected*
# (Aiko was responding to existing bad news / a sad beat) and so a
# rupture cue would be a false positive. Used when no override is
# passed to ``detect``. Lowercased; stripped at call site.
DEFAULT_EXCLUDED_REACTIONS: frozenset[str] = frozenset({
    "concerned",
    "gentle",
    "sad",
    "calm",
    "thoughtful",
    "quiet",
})


# ── Result type ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RuptureResult:
    """One per-turn affect-rupture signal."""

    valence_drop: float  # positive number == magnitude of the drop
    prior_reaction: str  # what Aiko emitted last turn (cleaned)
    prior_valence: float  # affect_before.valence
    current_valence: float  # state.valence after apply_turn


# ── Public API ───────────────────────────────────────────────────────────


def detect(
    *,
    prior_valence: float | None,
    current_valence: float | None,
    prior_reaction: str | None,
    threshold: float = 0.12,
    excluded_reactions: Iterable[str] | None = None,
) -> RuptureResult | None:
    """Classify the current turn and return a :class:`RuptureResult`
    when {user_name}'s valence dropped by more than ``threshold``
    *and* Aiko's reaction wasn't one of the excluded (already-
    empathetic) reactions, or ``None`` otherwise.

    Defensive against missing inputs:

    * ``prior_valence`` or ``current_valence`` of ``None`` returns
      ``None`` (the snapshot didn't land for some reason -- the
      controller should not synthesise rupture from missing data).
    * ``threshold`` is clamped to a non-negative float; a zero or
      negative threshold disables the detector.
    """
    if prior_valence is None or current_valence is None:
        return None
    if threshold is None or float(threshold) <= 0.0:
        return None

    drop = float(prior_valence) - float(current_valence)
    if drop < float(threshold):
        return None

    cleaned_reaction = (prior_reaction or "").strip().lower()
    excluded = (
        frozenset(s.strip().lower() for s in excluded_reactions if s)
        if excluded_reactions is not None
        else DEFAULT_EXCLUDED_REACTIONS
    )
    if cleaned_reaction in excluded:
        return None

    return RuptureResult(
        valence_drop=round(drop, 4),
        prior_reaction=cleaned_reaction or "neutral",
        prior_valence=round(float(prior_valence), 4),
        current_valence=round(float(current_valence), 4),
    )


def render_inner_life_block(
    result: RuptureResult,
    *,
    user_display_name: str = "the user",
) -> str:
    """Render ``result`` into a system-prompt-ready block.

    Single voicing -- the rupture beat is always "soften, check in
    once, don't camp on it". Quotes the prior reaction so the LLM
    can see what tone Aiko had been in (a "playful" reaction
    landing wrong reads differently from a "neutral" one).
    """
    head = (
        f"Heads-up: {user_display_name}'s mood just dipped right after "
        "your last reply"
    )
    rxn = (result.prior_reaction or "").strip().lower()
    if rxn and rxn != "neutral":
        head = f"{head} (your last reaction was \"{rxn}\")"
    body = (
        "That beat may have landed wrong. Soften this turn and check "
        "in once gently before pushing forward -- a quiet \"hey, you "
        "good?\" or a small course-correction (\"that came out colder "
        "than I meant -- I just meant...\") fits. One repair beat, "
        "then move on; don't camp on the rupture or perform concern."
    )
    return f"{head}.\n{body}"
