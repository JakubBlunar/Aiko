"""Aiko style-pattern tracker (response-variability anti-rut layer).

Watches Aiko's *own* recent assistant turns and emits a soft "Heads-up"
inner-life cue when she ruts on a single opener, a question-end pattern,
or runaway length. Sibling architecture to :mod:`app.core.conversation.novelty_detector`
(K6) and :mod:`app.core.conversation.topic_stagnation` (K18); the persona file's
"Style patterns I'm in" section pairs with the cues this module emits.

Design choices (kept deliberately close to K6/K18):

- **Pure rolling-window detector**. No embedder, no rag_store, no
  user_id. Per-turn cost is a deque append + a few counter scans.
- **Banded output**: ``opener_rut`` > ``anaphoric_opener`` >
  ``question_saturation`` > ``length_sprawl`` (priority order). Only one
  band fires per turn so the cue stays a single short line.
- **Per-band cooldown**. Each band has its own cooldown counter so an
  active opener-rut nudge doesn't suppress a later question-saturation
  cue, but the same band won't re-fire on consecutive turns.
- **Warmup gate**. Need at least ``warmup_min`` (default 6) recorded
  turns before any cue is allowed; the deque fills silently before
  that.
- **Settings-driven thresholds**. Tunable via ``AgentSettings.style_tracker_*``
  so calibration can move without code changes.

K88 added the ``anaphoric_opener`` band: too many replies in a row whose
first real clause hangs off his sentence ("Then...", "Exactly.", "That
makes sense"). It belongs here rather than in the persona sheet for a
structural reason -- persona line 30's standing DON'T PARROT has been in
force the entire time this was the dominant pattern, and a standing rule
is evaluated one turn at a time, so it cannot tell the first warm "Then
those pokes are reserved for you" from the fifth in a row. Only a window
can. The detector itself lives in :mod:`app.core.persona.anaphora`,
shared with the K90 report so the cue and the measurement can't drift.

The tracker is constructed on :class:`SessionController` start-up
(when ``agent.style_tracker_enabled``), fed by the post-turn mixin
after meta-tag stripping, and surfaced as the ``style_pattern``
inner-life provider on the prompt assembler.
"""
from __future__ import annotations

import collections
import logging
import re
from dataclasses import dataclass
from typing import Any

from app.core.persona.anaphora import SENT_SPLIT_RE, is_anaphoric_opener


log = logging.getLogger("app.aiko_style_tracker")


# Module-level defaults so tests can instantiate without a settings stub.
# ``SessionController`` passes ``AgentSettings`` in production and the
# tracker reads the configured values via ``getattr``.
_DEFAULT_WINDOW = 12
_DEFAULT_WARMUP_MIN = 6
_DEFAULT_OPENER_COUNT_THRESHOLD = 4
_DEFAULT_OPENER_TOPK_SHARE = 0.60
_DEFAULT_QUESTION_RATE_THRESHOLD = 0.75
_DEFAULT_AVG_QUESTIONS_THRESHOLD = 1.5
_DEFAULT_LENGTH_AVG_THRESHOLD = 50.0
# K88. Both gates have to clear, which is what makes this a rate
# detector rather than a ban: four in a twelve-turn window, and at least
# a third of the window. Measured against 1894 real turns, the standing
# rate is 18% and a 4/12 window occurs 17% of the time -- in line with
# the opener-rut band's 23% at its own default, and rare enough that the
# occasional warm "Then those pokes are reserved for you" never trips
# it. The count floor keeps a short warmup window from firing on 1-of-2.
_DEFAULT_ANAPHORIC_COUNT_THRESHOLD = 4
_DEFAULT_ANAPHORIC_RATE_THRESHOLD = 0.33
_DEFAULT_CUE_COOLDOWN_TURNS = 5
# Minimum word count to count as a "real" turn for tracking purposes.
# A single-word "yeah." reply or a pure stage-direction earcon shouldn't
# push the window forward; they're reactions, not measurable replies,
# and would otherwise drag every average toward zero.
_MIN_TURN_WORDS = 2


BAND_OPENER_RUT = "opener_rut"
BAND_ANAPHORIC_OPENER = "anaphoric_opener"
BAND_QUESTION_SATURATION = "question_saturation"
BAND_LENGTH_SPRAWL = "length_sprawl"


# Sentence splitter: end-of-sentence punctuation runs. Used both for
# the sentence count and (indirectly) by the opener detector. Shared
# with the K88 detector in :mod:`app.core.persona.anaphora`, which owns
# the definition.
_SENT_SPLIT_RE = SENT_SPLIT_RE
# Strip leading non-word characters from the first word so quoted /
# parenthesised openers ("\"yeah", "(oh") still bucket onto the bare
# token.
_OPENER_STRIP_LEAD = re.compile(r"^[\W_]+")
# Strip trailing non-word characters so "yeah," and "yeah." both
# bucket as ``"yeah"``.
_OPENER_STRIP_TAIL = re.compile(r"[\W_]+$")


@dataclass(slots=True, frozen=True)
class _TurnFeatures:
    """One recorded assistant turn's normalised features."""

    opener: str
    word_count: int
    sentence_count: int
    question_count: int
    ends_with_question: bool
    anaphoric_opener: bool = False


@dataclass(slots=True, frozen=True)
class StyleRutResult:
    """One banded rut signal the inner-life provider may render.

    ``detail`` is a debug-friendly string surfaced in logs (e.g.
    ``"'yeah' x5/10"``); the persona-facing copy is generated by
    :func:`render_inner_life_block` from the ``band`` alone.
    """

    band: str
    detail: str
    window_size: int


class AikoStylePatternTracker:
    """Watch Aiko's recent turns for opener / question / length ruts.

    Owns a small ring of per-turn features (no vectors, no LLM) plus
    three independent cooldown counters -- one per band so each rut
    can cool independently. Not thread-safe; the post-turn pipeline
    calls :meth:`record_turn` on the turn thread and the assembler
    calls :meth:`detect` on that same thread.
    """

    def __init__(self, *, agent_settings: Any | None = None) -> None:
        self._agent_settings = agent_settings
        window = max(
            2,
            int(self._setting("style_tracker_window", _DEFAULT_WINDOW)),
        )
        self._window: collections.deque[_TurnFeatures] = collections.deque(
            maxlen=window,
        )
        # Per-band cooldown counters so an opener-rut nudge doesn't
        # mask a later question-saturation cue, but the same band
        # won't re-fire on every turn while the rut persists.
        self._cooldowns: dict[str, int] = {
            BAND_OPENER_RUT: 0,
            BAND_ANAPHORIC_OPENER: 0,
            BAND_QUESTION_SATURATION: 0,
            BAND_LENGTH_SPRAWL: 0,
        }

    # ── public API ────────────────────────────────────────────────────

    def record_turn(self, assistant_text: str) -> None:
        """Capture features from one *post-strip* assistant reply.

        Caller is expected to have stripped meta tags (e.g. via
        :func:`strip_all_meta_tags`) so we measure the spoken text,
        not raw model output. Empty / very short / blank turns are
        silently ignored so a single-word "yeah." reply doesn't push
        a non-measurement into the window.
        """
        text = (assistant_text or "").strip()
        if not text:
            return
        features = _extract_features(text)
        if features.word_count < _MIN_TURN_WORDS:
            return
        self._window.append(features)
        log.debug(
            "aiko-style-tracker: recorded turn opener=%r words=%d "
            "sentences=%d questions=%d ends_q=%s anaphoric=%s window=%d",
            features.opener,
            features.word_count,
            features.sentence_count,
            features.question_count,
            features.ends_with_question,
            features.anaphoric_opener,
            len(self._window),
        )

    def detect(self) -> StyleRutResult | None:
        """Score the rolling window for the highest-priority rut.

        Returns ``None`` (silent) on warmup, when every cooldown is
        active, or when no signal trips. Cooldowns tick down by one
        regardless so the counter always advances over time.
        """
        # Tick cooldowns first so non-firing turns still progress them.
        # Using ``list`` so we can mutate the dict in-place.
        for band in list(self._cooldowns):
            if self._cooldowns[band] > 0:
                self._cooldowns[band] -= 1

        warmup = max(
            2,
            int(self._setting("style_tracker_warmup", _DEFAULT_WARMUP_MIN)),
        )
        if len(self._window) < warmup:
            log.debug(
                "aiko-style-tracker: warmup (window=%d need=%d)",
                len(self._window),
                warmup,
            )
            return None

        cooldown_turns = max(
            0,
            int(
                self._setting(
                    "style_tracker_cue_cooldown_turns",
                    _DEFAULT_CUE_COOLDOWN_TURNS,
                )
            ),
        )

        # Priority order: opener rut > anaphoric opener > question >
        # length. Each band is independently cooldown-gated so a hot
        # question-saturation cooldown can't mute an opener-rut nudge
        # that just appeared.
        #
        # The opener rut outranks K88's band because it is the narrower
        # ask on a turn where both fire: "you have opened five replies
        # with the same word" names one thing to change, where "your
        # sentences keep hanging off mine" needs her to find something
        # of her own first. The anaphoric band gets the next window --
        # they cool independently, and the rut is the rarer signal.
        opener_result = self._evaluate_opener_rut()
        if (
            opener_result is not None
            and self._cooldowns[BAND_OPENER_RUT] == 0
        ):
            self._cooldowns[BAND_OPENER_RUT] = cooldown_turns
            log.info(
                "aiko-style-tracker: %s detail=%s window=%d",
                opener_result.band,
                opener_result.detail,
                opener_result.window_size,
            )
            return opener_result

        anaphoric_result = self._evaluate_anaphoric_opener()
        if (
            anaphoric_result is not None
            and self._cooldowns[BAND_ANAPHORIC_OPENER] == 0
        ):
            self._cooldowns[BAND_ANAPHORIC_OPENER] = cooldown_turns
            log.info(
                "aiko-style-tracker: %s detail=%s window=%d",
                anaphoric_result.band,
                anaphoric_result.detail,
                anaphoric_result.window_size,
            )
            return anaphoric_result

        question_result = self._evaluate_question_saturation()
        if (
            question_result is not None
            and self._cooldowns[BAND_QUESTION_SATURATION] == 0
        ):
            self._cooldowns[BAND_QUESTION_SATURATION] = cooldown_turns
            log.info(
                "aiko-style-tracker: %s detail=%s window=%d",
                question_result.band,
                question_result.detail,
                question_result.window_size,
            )
            return question_result

        length_result = self._evaluate_length_sprawl()
        if (
            length_result is not None
            and self._cooldowns[BAND_LENGTH_SPRAWL] == 0
        ):
            self._cooldowns[BAND_LENGTH_SPRAWL] = cooldown_turns
            log.info(
                "aiko-style-tracker: %s detail=%s window=%d",
                length_result.band,
                length_result.detail,
                length_result.window_size,
            )
            return length_result

        return None

    # ── introspection (used by tests / mcp tools) ─────────────────────

    def window_size(self) -> int:
        return len(self._window)

    def last_turn_anaphoric(self) -> bool:
        """Did her most recent recorded reply open on a clause of his?

        K94 reads this to decide whether to ask for the reply's shape.
        Read-only on purpose: :meth:`detect` ticks the per-band cooldowns
        as a side effect, so a caller that only wants the feature must
        not go through it or it would silently consume K88's budget.

        Sourced here rather than recomputed at the call site because this
        window holds the *stripped* reply text the user actually heard,
        fed post-turn after meta-tag removal. Re-deriving the flag from
        raw model output would measure a different string than the one
        K88's band and the K90 report measure, which is precisely the
        drift :mod:`app.core.persona.anaphora` exists to prevent.
        """
        if not self._window:
            return False
        return bool(self._window[-1].anaphoric_opener)

    # ── internals ─────────────────────────────────────────────────────

    def _setting(self, name: str, default: Any) -> Any:
        return getattr(self._agent_settings, name, default)

    def _evaluate_opener_rut(self) -> StyleRutResult | None:
        """Same opener used too often, OR top-2 share >= threshold."""
        openers = [f.opener for f in self._window if f.opener]
        if not openers:
            return None
        counts = collections.Counter(openers)
        most_common = counts.most_common(2)
        if not most_common:
            return None
        top_opener, top_count = most_common[0]
        count_threshold = max(
            2,
            int(
                self._setting(
                    "style_tracker_opener_count_threshold",
                    _DEFAULT_OPENER_COUNT_THRESHOLD,
                )
            ),
        )
        share_threshold = float(
            self._setting(
                "style_tracker_opener_topk_share",
                _DEFAULT_OPENER_TOPK_SHARE,
            )
        )
        top2_count = top_count + (
            most_common[1][1] if len(most_common) > 1 else 0
        )
        top2_share = top2_count / float(len(openers))
        triggered_count = top_count >= count_threshold
        triggered_share = top2_share >= share_threshold
        if not (triggered_count or triggered_share):
            return None
        if triggered_count:
            detail = f"{top_opener!r} x{top_count}/{len(openers)}"
        else:
            top2 = " ".join(f"{w!r}" for w, _ in most_common[:2])
            detail = f"top2={top2} share={top2_share:.0%}"
        return StyleRutResult(
            band=BAND_OPENER_RUT,
            detail=detail,
            window_size=len(self._window),
        )

    def _evaluate_anaphoric_opener(self) -> StyleRutResult | None:
        """K88: too many replies opening on a clause that needs his.

        A **rate**, deliberately, and never a ban. The persona's
        standing "DON'T PARROT" line has been in place the whole time
        this pattern has been the dominant one, and it fails for a
        structural reason: a standing rule is evaluated per turn, so it
        cannot distinguish the one warm "Then those pokes are reserved
        for you" from the fifth in a row. Only a window can, which is
        why this belongs here rather than in the persona sheet.

        Both gates must clear -- an absolute count and a share of the
        window -- so neither a long calm window nor a two-turn warmup
        can trip it alone.
        """
        if not self._window:
            return None
        hits = sum(1 for f in self._window if f.anaphoric_opener)
        rate = hits / float(len(self._window))
        count_threshold = max(
            2,
            int(
                self._setting(
                    "style_tracker_anaphoric_count_threshold",
                    _DEFAULT_ANAPHORIC_COUNT_THRESHOLD,
                )
            ),
        )
        rate_threshold = float(
            self._setting(
                "style_tracker_anaphoric_rate_threshold",
                _DEFAULT_ANAPHORIC_RATE_THRESHOLD,
            )
        )
        if hits < count_threshold or rate < rate_threshold:
            return None
        return StyleRutResult(
            band=BAND_ANAPHORIC_OPENER,
            detail=f"anaphoric={hits}/{len(self._window)} ({rate:.0%})",
            window_size=len(self._window),
        )

    def _evaluate_question_saturation(self) -> StyleRutResult | None:
        """Question-end rate too high OR avg questions/turn too high."""
        if not self._window:
            return None
        ends_q = sum(1 for f in self._window if f.ends_with_question)
        question_rate = ends_q / float(len(self._window))
        avg_q = sum(f.question_count for f in self._window) / float(
            len(self._window),
        )
        rate_threshold = float(
            self._setting(
                "style_tracker_question_rate_threshold",
                _DEFAULT_QUESTION_RATE_THRESHOLD,
            )
        )
        avg_threshold = float(
            self._setting(
                "style_tracker_avg_questions_threshold",
                _DEFAULT_AVG_QUESTIONS_THRESHOLD,
            )
        )
        triggered_rate = question_rate >= rate_threshold
        triggered_avg = avg_q >= avg_threshold
        if not (triggered_rate or triggered_avg):
            return None
        if triggered_rate:
            detail = (
                f"end_rate={question_rate:.0%} "
                f"({ends_q}/{len(self._window)})"
            )
        else:
            detail = f"avg_q={avg_q:.2f}"
        return StyleRutResult(
            band=BAND_QUESTION_SATURATION,
            detail=detail,
            window_size=len(self._window),
        )

    def _evaluate_length_sprawl(self) -> StyleRutResult | None:
        """Average word count over the window above threshold."""
        if not self._window:
            return None
        avg_words = sum(f.word_count for f in self._window) / float(
            len(self._window),
        )
        threshold = float(
            self._setting(
                "style_tracker_length_avg_threshold",
                _DEFAULT_LENGTH_AVG_THRESHOLD,
            )
        )
        if avg_words < threshold:
            return None
        return StyleRutResult(
            band=BAND_LENGTH_SPRAWL,
            detail=f"avg_words={avg_words:.1f}",
            window_size=len(self._window),
        )


def _extract_features(text: str) -> _TurnFeatures:
    """Pure feature extractor (called from :meth:`record_turn`)."""
    cleaned = (text or "").strip()
    words = cleaned.split()
    word_count = len(words)
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(cleaned) if s.strip()]
    sentence_count = len(sentences) or (1 if cleaned else 0)
    # Total '?' count is a safe proxy for question count: handles both
    # stacked questions ("Did you? Or did you?") and questions without
    # trailing punctuation in the middle of a reply.
    question_count = cleaned.count("?")
    # Ends-with-question: trim trailing whitespace, quotes, brackets so
    # a closing quote after a question still counts.
    tail = cleaned.rstrip().rstrip(")\"'»”] ")
    ends_with_question = tail.endswith("?")
    if words:
        first = words[0]
        first = _OPENER_STRIP_LEAD.sub("", first)
        first = _OPENER_STRIP_TAIL.sub("", first)
        opener = first.lower()
    else:
        opener = ""
    return _TurnFeatures(
        opener=opener,
        word_count=word_count,
        sentence_count=sentence_count,
        question_count=question_count,
        ends_with_question=ends_with_question,
        anaphoric_opener=is_anaphoric_opener(cleaned),
    )


def render_inner_life_block(result: StyleRutResult | None) -> str:
    """Render the one-line inner-life cue for the given band.

    Three bands, three copies. Returns ``""`` when ``result`` is
    ``None`` so the assembler can drop the block entirely.
    """
    if result is None:
        return ""
    if result.band == BAND_OPENER_RUT:
        return (
            "Heads-up: your last few replies have all opened with the "
            "same word. Try a different entry this turn -- or skip the "
            "opener and lead with the substance."
        )
    if result.band == BAND_ANAPHORIC_OPENER:
        return (
            "Heads-up: your last several replies have all opened by "
            "reaching back into his sentence -- \"Then...\", "
            "\"Exactly.\", \"That makes sense\". Open this one on "
            "something of your own instead: a thing you did, noticed, "
            "or think, before you touch what he said."
        )
    if result.band == BAND_QUESTION_SATURATION:
        return (
            "Heads-up: your last several replies all ended on a "
            "question. Drop a thought, observation, or opinion of your "
            "own this turn instead of asking back."
        )
    if result.band == BAND_LENGTH_SPRAWL:
        return (
            "Heads-up: your replies have been running long. Keep this "
            "one to one or two crisp sentences."
        )
    return ""


__all__ = [
    "BAND_ANAPHORIC_OPENER",
    "BAND_LENGTH_SPRAWL",
    "BAND_OPENER_RUT",
    "BAND_QUESTION_SATURATION",
    "AikoStylePatternTracker",
    "StyleRutResult",
    "render_inner_life_block",
]
