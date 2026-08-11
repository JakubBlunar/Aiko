"""Topic-stagnation detector (K18 personality backlog).

Sibling to :mod:`app.core.conversation.novelty_detector` (K6). Where K6 fires when
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
- **Self-calibrating thresholds**. What counts as "circling" is a
  question about *this* conversation against its own history, not an
  absolute cosine distance, and the absolute version does not survive
  contact with a different embedding model. The shipped constants
  (mild 0.18, strong 0.10) turned out to sit below the *minimum*
  distance this install has ever produced: 52 consecutive readings
  spanning 0.310-0.422, every one of them ``band=silent``. K18 could
  not fire, and neither could anything downstream of it — the
  dormant-interest re-opener needs a lull to land on and had never
  rendered once in 378 attempts. So the bands are now percentiles of
  the install's own rolling baseline (persisted in ``kv_meta``), with
  the constants kept as the cold-start fallback until enough turns
  have been measured to have a distribution at all.

The detector is constructed on :class:`SessionController` start-up
(when ``agent.topic_stagnation_enabled``) and registered as the
``stagnation`` inner-life provider on the prompt assembler. It is
called per-turn from the assembler's ``assemble_with_budget``,
right after the K6 ``novelty`` provider so we know whether novelty
just fired this turn.
"""
from __future__ import annotations

import collections
import json
import logging
import statistics
from dataclasses import dataclass
from typing import Any, Callable


log = logging.getLogger("app.topic_stagnation")


# Module-level defaults so tests can instantiate without a settings
# stub. ``SessionController`` passes ``MemorySettings`` in production
# and the detector reads the configured values via ``getattr``.
_DEFAULT_WINDOW = 6
_DEFAULT_MILD_THRESHOLD = 0.18
_DEFAULT_STRONG_THRESHOLD = 0.10
_DEFAULT_COOLDOWN_TURNS = 4
_DEFAULT_POST_NOVELTY_SUPPRESSION_TURNS = 3

# ── self-calibration ────────────────────────────────────────────────
#
# The baseline is a rolling record of window means, so the bands are
# read off the same quantity they are compared against.
KV_BASELINE = "topic_stagnation.mean_baseline"
# Enough readings to have a shape worth trusting; below this the
# configured constants stand. At roughly one measured turn a minute of
# conversation this is a couple of active days.
_ADAPTIVE_MIN_SAMPLES = 60
_BASELINE_CAP = 400
# Which slice of her own history counts as a lull. Deliberately tight:
# "we have been circling this" should describe a genuinely quiet
# stretch, not the calmer half of every conversation.
_MILD_PERCENTILE = 0.15
_STRONG_PERCENTILE = 0.05
# Writing the baseline on every measured turn would put a SQLite write
# on the prompt-assembly path for no benefit; a lost tail of a few
# samples changes no percentile.
_PERSIST_EVERY = 10


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. No interpolation: the baseline is a
    sample of real readings and a real reading is the honest bar."""
    if not sorted_values:
        raise ValueError("empty baseline")
    index = int(len(sorted_values) * fraction)
    return float(sorted_values[min(index, len(sorted_values) - 1)])


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
        kv_get: "Callable[[str], str | None] | None" = None,
        kv_set: "Callable[[str, str], None] | None" = None,
    ) -> None:
        self._memory_settings = memory_settings
        window = max(2, int(self._setting("stagnation_window", _DEFAULT_WINDOW)))
        self._distance_history: collections.deque[float] = collections.deque(
            maxlen=window,
        )
        self._cooldown_remaining = 0
        self._post_novelty_suppression = 0
        # K54 (topic appetite) consumes this: the most recent rolling
        # mean computed over a *full* window. Deliberately NOT reset
        # per turn — a short "ok" reply skips the K6 measurement, and
        # that's exactly the turn K54 needs the standing lull reading
        # for. ``None`` until the window first fills.
        self.last_mean: float | None = None
        # The bands actually in force. Consumers that gate on "is this a
        # lull" (K67's dormant-interest re-opener) must read these rather
        # than the configured constant, or they are testing against a bar
        # the detector itself has stopped using.
        self.mild_threshold: float = float(
            self._setting("stagnation_mild_threshold", _DEFAULT_MILD_THRESHOLD)
        )
        self.strong_threshold: float = float(
            self._setting(
                "stagnation_strong_threshold", _DEFAULT_STRONG_THRESHOLD,
            )
        )
        self.adaptive: bool = False
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._baseline: collections.deque[float] = collections.deque(
            maxlen=_BASELINE_CAP,
        )
        self._unpersisted = 0
        self._load_baseline()
        self._refresh_thresholds()

    # ── self-calibration ─────────────────────────────────────────────

    def _load_baseline(self) -> None:
        if self._kv_get is None:
            return
        try:
            raw = self._kv_get(KV_BASELINE)
            values = json.loads(raw) if raw else []
        except Exception:
            log.debug("topic-stagnation baseline read failed", exc_info=True)
            return
        if not isinstance(values, list):
            return
        for value in values:
            try:
                self._baseline.append(float(value))
            except (TypeError, ValueError):
                continue

    def _persist_baseline(self, *, force: bool = False) -> None:
        if self._kv_set is None or not self._baseline:
            return
        self._unpersisted += 1
        if not force and self._unpersisted < _PERSIST_EVERY:
            return
        self._unpersisted = 0
        try:
            self._kv_set(
                KV_BASELINE,
                json.dumps([round(v, 4) for v in self._baseline]),
            )
        except Exception:
            log.debug("topic-stagnation baseline write failed", exc_info=True)

    def _refresh_thresholds(self) -> None:
        """Recompute the bands from the baseline, or fall back to config.

        Percentiles of her own history rather than absolute distances:
        the shipped constants encode one embedding model's scale, and on
        a model whose distances run higher they silence the detector
        completely instead of merely making it strict.
        """
        configured_mild = float(
            self._setting("stagnation_mild_threshold", _DEFAULT_MILD_THRESHOLD)
        )
        configured_strong = float(
            self._setting(
                "stagnation_strong_threshold", _DEFAULT_STRONG_THRESHOLD,
            )
        )
        if len(self._baseline) < _ADAPTIVE_MIN_SAMPLES:
            self.adaptive = False
            self.mild_threshold = configured_mild
            self.strong_threshold = min(configured_strong, configured_mild)
            return
        ordered = sorted(self._baseline)
        self.adaptive = True
        self.mild_threshold = _percentile(ordered, _MILD_PERCENTILE)
        self.strong_threshold = min(
            _percentile(ordered, _STRONG_PERCENTILE), self.mild_threshold
        )

    def baseline_snapshot(self) -> dict[str, Any]:
        """What the detector is currently calibrated against (for MCP /
        debugging). Cheap, read-only."""
        ordered = sorted(self._baseline)
        return {
            "samples": len(ordered),
            "adaptive": self.adaptive,
            "min_samples_needed": _ADAPTIVE_MIN_SAMPLES,
            "mild_threshold": round(self.mild_threshold, 4),
            "strong_threshold": round(self.strong_threshold, 4),
            "observed_min": round(ordered[0], 4) if ordered else None,
            "observed_median": (
                round(statistics.median(ordered), 4) if ordered else None
            ),
            "observed_max": round(ordered[-1], 4) if ordered else None,
        }

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
        if len(self._distance_history) == self._distance_history.maxlen:
            # K54 hook: refresh the standing lull reading on every
            # measured turn, independent of cooldown / suppression.
            self.last_mean = float(
                statistics.fmean(self._distance_history)
            )
            # Feed the same reading to the baseline the bands are drawn
            # from, and do it here rather than after the gates below:
            # cooldown and suppression decide whether this turn may
            # *fire*, not whether it happened. Excluding suppressed turns
            # would bias the distribution toward the noisy ones.
            self._baseline.append(self.last_mean)
            self._refresh_thresholds()
            self._persist_baseline()
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

        # Both are kept as *upper* bounds (lower mean = more stagnant)
        # with strong <= mild; ``_refresh_thresholds`` already enforces
        # the ordering for both the adaptive and the configured path.
        mild = self.mild_threshold
        strong = self.strong_threshold

        band: str | None
        if mean_distance < strong:
            band = BAND_STRONG_LULL
        elif mean_distance < mild:
            band = BAND_MILD_LULL
        else:
            band = None

        log.info(
            "topic-stagnation: mean=%.3f band=%s window=%d "
            "mild=%.3f strong=%.3f adaptive=%s samples=%d",
            mean_distance,
            band or "silent",
            window_size,
            mild,
            strong,
            self.adaptive,
            len(self._baseline),
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


def lull_band(
    detector: "TopicStagnationDetector | None",
    memory_settings: Any | None = None,
) -> float:
    """The bar a standing-lull consumer must test ``last_mean`` against.

    The detector's *effective* mild band, not the configured constant.
    Since self-calibration the two are usually different numbers, and
    the constant is the one that cannot fire: it encodes one embedding
    model's distance scale and on this install sits below every reading
    ever taken.
    """
    band = getattr(detector, "mild_threshold", None)
    if band is not None:
        return float(band)
    return float(
        getattr(
            memory_settings,
            "stagnation_mild_threshold",
            _DEFAULT_MILD_THRESHOLD,
        )
    )


def in_standing_lull(
    detector: "TopicStagnationDetector | None",
    memory_settings: Any | None = None,
) -> bool:
    """Is the conversation currently circling? The one definition.

    Five prompt blocks wait for "a natural lull" and each used to spell
    the test out itself. Four spelled it wrong -- two inverted the
    comparison outright, and all four read the raw constant -- so
    K54 topic-appetite could never fire and the K81/K85e lean gate and
    the L17e reflection gate were open on exactly the turns they meant
    to sit out. The polarity is the easy thing to get backwards:
    ``last_mean`` is a *distance*, so a lull is a low reading, not a
    high one.

    ``None`` (the window has not filled) reads as "not a lull". Every
    consumer wants a positive reading before speaking up, so a cold
    signal must block rather than pass.
    """
    mean = getattr(detector, "last_mean", None)
    if mean is None:
        return False
    return float(mean) <= lull_band(detector, memory_settings)


# F10k: cap on how long a cluster label we'll splice into the cue
# (mirrors ``novelty_detector._MAX_TOPIC_LABEL_CHARS``).
_MAX_TOPIC_LABEL_CHARS = 48


def _clean_topic_label(label: str | None) -> str:
    s = (label or "").strip()
    if not s or "\n" in s or len(s) > _MAX_TOPIC_LABEL_CHARS:
        return ""
    return s


def render_inner_life_block(
    result: StagnationResult | None,
    *,
    user_display_name: str = "Jacob",
    topic_label: str = "",
) -> str:
    """Render the one-line inner-life signal for the given band.

    Two bands, two copies. ``mild_lull`` nudges Aiko to notice the
    rhythm and optionally take a soft pivot; ``strong_lull`` asks
    her to either deepen the thread on purpose or offer a real
    off-ramp. Returns ``""`` when ``result`` is ``None`` so the
    assembler can drop the block entirely.

    ``user_display_name`` is interpolated into the mild copy so a
    rename via onboarding / settings is reflected without a restart.

    F10k: when the K9 topic graph named the cluster the conversation
    has been looping on, ``topic_label`` adds a private "(the X
    thread)" context clause so the lull cue points at the actual
    topic instead of a vague "this". Internal context only — the
    persona block tells Aiko never to quote it.
    """
    if result is None:
        return ""
    name = (user_display_name or "").strip() or "Jacob"
    label = _clean_topic_label(topic_label)
    clause = f" (Context, don't quote: the {label} thread.)" if label else ""
    if result.band == BAND_STRONG_LULL:
        return (
            "Heads-up: this thread has been pretty looped for a while -- "
            "lean toward either deepening it on purpose or offering a "
            "real off-ramp, whichever fits the moment." + clause
        )
    if result.band == BAND_MILD_LULL:
        return (
            f"Heads-up: you've been circling the same topic with {name} "
            "for a bit -- a soft pivot's fine if one fits, otherwise just "
            "keep going." + clause
        )
    return ""


__all__ = [
    "BAND_MILD_LULL",
    "BAND_STRONG_LULL",
    "StagnationResult",
    "TopicStagnationDetector",
    "in_standing_lull",
    "lull_band",
    "render_inner_life_block",
]
