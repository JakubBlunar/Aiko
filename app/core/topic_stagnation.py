"""Topic-stagnation detector (K18 personality backlog).

Sibling to :mod:`app.core.novelty_detector` (K6). Where K6 fires when
a single user turn diverges *sharply* from the recent topic baseline,
K18 fires when the rolling distance to that baseline stays *low* for
a window of turns -- the conversation has been circling the same
ground for a while and Aiko may want to acknowledge the rhythm or
offer a soft pivot.

Design choices (kept deliberately close to K6 so the two cues feel
like a matched pair in the persona block):

- **Pure streak detector**. No embedder, no rag_store, no user_id.
  We consume the per-turn distance K6 already computes (exposed as
  ``NoveltyDetector.last_distance``) so we never re-embed the user
  message.
- **Banded output**, mirroring K6: ``mild_lull`` (mean distance below
  the mild threshold) and ``strong_lull`` (mean distance below the
  strong threshold). Names use "lull" rather than "stagnation" to
  read softer in logs and persona copy.
- **Cooldown between hits**, longer than K6's by default because
  lulls are by nature drawn-out; firing the same band on consecutive
  turns is almost never useful.
- **Post-novelty suppression**. Right after K6 fires, the centroid
  is in the middle of a topic shift -- distances will be weird for a
  few turns. We mute K18 for a configurable suppression window so
  the two detectors don't talk past each other.
- **Conservative defaults**. Thresholds are intentionally narrow
  (a 6-turn mean distance must drop below ~0.18 for the mild band,
  below ~0.10 for the strong band). Calibration is the kind of thing
  only live testing settles; the persona explicitly tells Aiko that
  *not* hearing the cue is also a signal.

The detector is constructed on :class:`SessionController` start-up
(when ``agent.topic_stagnation_enabled``) and registered as the
``stagnation`` inner-life provider on the prompt assembler. It is
called per-turn from the assembler's ``assemble_with_budget``,
right after the K6 ``novelty`` provider so we know whether novelty
just fired this turn.
"""
from __future__ import annotations

import collections
import logging
import statistics
from dataclasses import dataclass
from typing import Any


log = logging.getLogger("app.topic_stagnation")


# Module-level defaults so tests can instantiate without a settings
# stub. ``SessionController`` passes ``MemorySettings`` in production
# and the detector reads the configured values via ``getattr``.
_DEFAULT_WINDOW = 6
_DEFAULT_MILD_THRESHOLD = 0.18
_DEFAULT_STRONG_THRESHOLD = 0.10
_DEFAULT_COOLDOWN_TURNS = 4
_DEFAULT_POST_NOVELTY_SUPPRESSION_TURNS = 3


BAND_MILD_LULL = "mild_lull"
BAND_STRONG_LULL = "strong_lull"


@dataclass(slots=True, frozen=True)
class StagnationResult:
    """One banded stagnation signal the inner-life provider may render.

    ``mean_distance`` is the arithmetic mean of the last
    ``window_size`` distances (matched to ``stagnation_window``); it
    sits in ``[0.0, 2.0]`` in theory but practical values cluster in
    ``[0, 1.2]`` since :class:`Embedder` returns unit-norm vectors.
    Lower mean = more topical clustering = stronger stagnation.
    """

    band: str
    mean_distance: float
    window_size: int


class TopicStagnationDetector:
    """Detect sustained low divergence in the K6 distance stream.

    Owns a small ring of recent distances (no vectors, no embeddings)
    plus two pieces of state: a hit cooldown so the same band doesn't
    re-fire on consecutive turns, and a post-novelty suppression
    counter that keeps K18 quiet for a few turns after K6 fires (so
    a fresh topic shift doesn't immediately register as "we've been
    on this for a while").

    Not thread-safe by itself; the caller (``PromptAssembler``) is
    expected to invoke ``detect`` on the turn thread, after the K6
    novelty provider has run.
    """

    def __init__(
        self,
        *,
        memory_settings: Any | None = None,
    ) -> None:
        self._memory_settings = memory_settings
        window = max(2, int(self._setting("stagnation_window", _DEFAULT_WINDOW)))
        self._distance_history: collections.deque[float] = collections.deque(
            maxlen=window,
        )
        self._cooldown_remaining = 0
        self._post_novelty_suppression = 0

    # ── public API ───────────────────────────────────────────────────

    def detect(
        self,
        distance: float | None,
        *,
        novelty_just_fired: bool = False,
    ) -> StagnationResult | None:
        """Score the rolling distance window for a stagnation hit.

        ``distance`` is the per-turn cosine-distance K6 just computed
        (or ``None`` when K6 didn't actually measure -- short text,
        warmup, embed failure). ``novelty_just_fired`` tells us
        whether K6 emitted a banded result for this turn so we can
        arm the post-novelty suppression window.

        Returns a :class:`StagnationResult` when the rolling mean
        crosses one of the configured bands, ``None`` otherwise
        (silent turn, warmup, cooldown, or active post-novelty
        suppression).
        """
        # Step 1: arm post-novelty suppression *before* we touch the
        # history. We still record this turn's distance so the window
        # keeps moving; we just won't fire while suppression is hot.
        if novelty_just_fired:
            suppression = max(
                0,
                int(
                    self._setting(
                        "stagnation_post_novelty_suppression_turns",
                        _DEFAULT_POST_NOVELTY_SUPPRESSION_TURNS,
                    )
                ),
            )
            self._post_novelty_suppression = suppression
            log.debug(
                "topic-stagnation: novelty fired; arming suppression=%d",
                suppression,
            )

        # Step 2: bail without touching anything when K6 didn't even
        # measure (short text / warmup / embed failure). Appending
        # would be wrong -- we'd inject a non-measurement into the
        # streak and risk dragging the mean.
        if distance is None:
            log.debug("topic-stagnation: skip (distance is None)")
            return None

        # Step 3: record and tick down counters every measured turn.
        # Order matters: append first so the window evolves even when
        # we end up suppressed below.
        self._distance_history.append(float(distance))
        if self._post_novelty_suppression > 0:
            self._post_novelty_suppression -= 1
            log.debug(
                "topic-stagnation: post-novelty suppressed remaining=%d",
                self._post_novelty_suppression,
            )
            return None
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            log.debug(
                "topic-stagnation: cooldown remaining=%d",
                self._cooldown_remaining,
            )
            return None

        # Step 4: only score once the window is genuinely full. A
        # half-filled deque underweights the early-conversation case
        # where there hasn't been time to circle anything yet.
        if len(self._distance_history) < self._distance_history.maxlen:  # type: ignore[arg-type]
            log.debug(
                "topic-stagnation: warmup (history=%d need=%d)",
                len(self._distance_history),
                self._distance_history.maxlen,
            )
            return None

        mean_distance = float(statistics.fmean(self._distance_history))
        window_size = len(self._distance_history)

        mild = float(
            self._setting("stagnation_mild_threshold", _DEFAULT_MILD_THRESHOLD)
        )
        strong = float(
            self._setting(
                "stagnation_strong_threshold", _DEFAULT_STRONG_THRESHOLD,
            )
        )
        # Defensive ordering: stagnation thresholds are *upper* bounds
        # (lower mean = more stagnant), so ``strong`` should be <=
        # ``mild``. If a misconfigured strong>mild slipped through,
        # collapse to a single-threshold behaviour using the tighter
        # value so we don't over-fire.
        if strong > mild:
            strong = mild

        band: str | None
        if mean_distance < strong:
            band = BAND_STRONG_LULL
        elif mean_distance < mild:
            band = BAND_MILD_LULL
        else:
            band = None

        log.info(
            "topic-stagnation: mean=%.3f band=%s window=%d",
            mean_distance,
            band or "silent",
            window_size,
        )

        if band is None:
            return None

        cooldown = max(
            0,
            int(self._setting("stagnation_cooldown_turns", _DEFAULT_COOLDOWN_TURNS)),
        )
        self._cooldown_remaining = cooldown
        return StagnationResult(
            band=band,
            mean_distance=mean_distance,
            window_size=window_size,
        )

    # ── internals ────────────────────────────────────────────────────

    def _setting(self, name: str, default: Any) -> Any:
        return getattr(self._memory_settings, name, default)


def render_inner_life_block(
    result: StagnationResult | None,
    *,
    user_display_name: str = "Jacob",
) -> str:
    """Render the one-line inner-life signal for the given band.

    Two bands, two copies. ``mild_lull`` nudges Aiko to notice the
    rhythm and optionally take a soft pivot; ``strong_lull`` asks
    her to either deepen the thread on purpose or offer a real
    off-ramp. Returns ``""`` when ``result`` is ``None`` so the
    assembler can drop the block entirely.

    ``user_display_name`` is interpolated into the mild copy so a
    rename via onboarding / settings is reflected without a restart.
    """
    if result is None:
        return ""
    name = (user_display_name or "").strip() or "Jacob"
    if result.band == BAND_STRONG_LULL:
        return (
            "Heads-up: this thread has been pretty looped for a while -- "
            "lean toward either deepening it on purpose or offering a "
            "real off-ramp, whichever fits the moment."
        )
    if result.band == BAND_MILD_LULL:
        return (
            f"Heads-up: you've been circling the same topic with {name} "
            "for a bit -- a soft pivot's fine if one fits, otherwise just "
            "keep going."
        )
    return ""


__all__ = [
    "BAND_MILD_LULL",
    "BAND_STRONG_LULL",
    "StagnationResult",
    "TopicStagnationDetector",
    "render_inner_life_block",
]
