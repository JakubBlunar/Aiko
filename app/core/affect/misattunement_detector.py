"""Subtle misattunement detection (K23).

Per-turn detector that fires ``mild_disengagement`` when {user_name}
goes short or pivots topics right after a substantial Aiko reply.
Sits in the gap between K17 (semantic repair, fires on explicit
"that's not what I meant" regex hits) and K14 (multi-turn engagement
aggregate that needs warmup and smooths abrupt single-turn shifts).

Two trigger paths, single result band:

* ``shrink`` -- Aiko's last reply was substantial (``>=
  shrink_min_prev_words`` words) and the current user message is very
  short (``<= shrink_max_user_words`` words). A one-word reply right
  after a 60-word answer reads as "you went quiet on me".
* ``pivot`` -- K6 :class:`NoveltyDetector` flagged the current user
  message as ``strong_novelty`` (a sharp topic shift) AND the message
  is short (``<= pivot_max_user_words``). A short pivot away without
  acknowledging Aiko's last point reads as "you didn't engage with
  what I said".

Either trigger fires the same cue ("pull back, lighter, drop the
agenda, no apologies"); strong-vs-mild banding is intentionally not
modelled in the MVP -- the cooldown gate keeps the cue rare enough
that a single voicing is sufficient.

Cooldown lives on :class:`SessionController` (``_misattunement_cooldown``);
the provider decrements it each call regardless of trigger state and
arms it to :data:`AgentSettings.misattunement_cooldown_turns` whenever
``detect()`` returns a hit. Per-turn nature means the cue lands on
the SAME turn that's about to reply to the disengaging message --
pulling back IS the next reply, not the one after. That's why this
detector is provider-time rather than post-turn-stash.

Inputs are scalar -- no tables, no LLM, no embedder. K6's
``last_distance`` / ``last_band`` (already computed earlier in the
provider chain) supply the topic-continuity signal at zero extra
cost.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass


log = logging.getLogger("app.misattunement_detector")


# Default trigger thresholds. Exported so settings can mirror them
# verbatim and tests can assert against the same numbers.
DEFAULT_SHRINK_MIN_PREV_WORDS: int = 30
DEFAULT_SHRINK_MAX_USER_WORDS: int = 8
DEFAULT_PIVOT_MAX_USER_WORDS: int = 8
DEFAULT_PIVOT_BAND: str = "strong_novelty"


# ── Result type ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MisattunementResult:
    """One per-turn misattunement signal.

    ``band`` is single-valued (``mild_disengagement``) in the MVP;
    the field exists for symmetry with the K17 / K8 sibling detectors
    and to allow adding a ``strong_disengagement`` band later without
    churning the provider's call shape. ``trigger`` is the diagnostic
    label that landed the cue -- exposed for the MCP debug tool and
    the per-fire INFO log line, not used in render.
    """

    band: str
    trigger: str  # "shrink" | "pivot"
    prev_aiko_words: int
    this_user_words: int
    novelty_distance: float | None


# ── Public API ───────────────────────────────────────────────────────────


def detect(
    *,
    prev_aiko_words: int | None,
    this_user_words: int,
    novelty_band: str | None,
    novelty_distance: float | None,
    cooldown_remaining: int,
    shrink_min_prev_words: int = DEFAULT_SHRINK_MIN_PREV_WORDS,
    shrink_max_user_words: int = DEFAULT_SHRINK_MAX_USER_WORDS,
    pivot_band: str = DEFAULT_PIVOT_BAND,
    pivot_max_user_words: int = DEFAULT_PIVOT_MAX_USER_WORDS,
) -> MisattunementResult | None:
    """Classify the current turn and return a :class:`MisattunementResult`
    when it fits one of the two trigger paths, or ``None`` otherwise.

    Defensive against missing inputs:

    * ``cooldown_remaining > 0`` short-circuits to ``None`` -- the
      previous cue is still in flight and a re-fire would stack
      "pull back" cues across consecutive turns.
    * ``prev_aiko_words`` of ``None`` (no prior assistant turn,
      cold-start session) disables the shrink trigger; the pivot
      trigger can still fire if K6 supplies a band.
    * ``this_user_words`` of ``0`` is treated as "no input to score"
      and returns ``None`` for both triggers.

    Trigger checks are short-circuited in shrink-first order so an
    eligible shrink hit doesn't get classified as a pivot when both
    apply. The render text doesn't depend on the trigger label, so
    this is a diagnostic-only ordering.
    """
    if cooldown_remaining is not None and int(cooldown_remaining) > 0:
        return None
    user_words = int(this_user_words or 0)
    if user_words <= 0:
        return None

    # Shrink trigger -- substantial prior Aiko reply + very short user reply.
    if prev_aiko_words is not None:
        prev = int(prev_aiko_words)
        if (
            prev >= int(shrink_min_prev_words)
            and user_words <= int(shrink_max_user_words)
        ):
            return MisattunementResult(
                band="mild_disengagement",
                trigger="shrink",
                prev_aiko_words=prev,
                this_user_words=user_words,
                novelty_distance=(
                    float(novelty_distance)
                    if novelty_distance is not None
                    else None
                ),
            )

    # Pivot trigger -- K6 flagged a sharp topic shift AND user went short.
    band_norm = (novelty_band or "").strip().lower()
    if (
        band_norm == (pivot_band or "").strip().lower()
        and band_norm  # don't match empty-on-empty
        and user_words <= int(pivot_max_user_words)
    ):
        return MisattunementResult(
            band="mild_disengagement",
            trigger="pivot",
            prev_aiko_words=int(prev_aiko_words) if prev_aiko_words is not None else 0,
            this_user_words=user_words,
            novelty_distance=(
                float(novelty_distance)
                if novelty_distance is not None
                else None
            ),
        )

    return None


def render_inner_life_block(
    result: MisattunementResult,
    *,
    user_display_name: str = "the user",
) -> str:
    """Render ``result`` into a system-prompt-ready block.

    Single voicing -- the cue is always "pull back, lighter, drop
    the agenda, no apologies." Mirrors K17's "don't apology-spiral"
    rail because the most common LLM failure mode on a short-reply
    signal is to over-correct with "are you ok? did I say something
    wrong?" which is itself disengaging.
    """
    head = (
        f"Heads-up: {user_display_name} just gave a short reply after "
        "your last full answer"
    )
    body = (
        "He may be drifting or just busy -- either way, pull back this "
        "turn. Shorter reply, lighter weight, drop the agenda. One "
        "focused observation or one small offer is plenty. Don't ask "
        "\"are you ok?\", don't apologise, don't camp on it -- just "
        "lighten the load and let him come back to you."
    )
    return f"{head}.\n{body}"
