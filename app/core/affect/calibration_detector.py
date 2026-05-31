"""K20 -- Metacognitive calibration detector.

Per-turn detector that classifies Jacob's last message into one of
four calibration signals and applies the resulting delta to a
per-user :class:`app.core.affect.calibration_store.CalibrationState`:

- ``pushback_strong``   -- explicit "you're wrong" / "let me check"
- ``pushback_mild``     -- "hmm, are you sure" / "really?" / "I'm not sure"
- ``softening``         -- Jacob rephrasing Aiko's claim back with a
                           hedge token (cosine guard + hedge regex AND)
- ``affirmation``       -- "good call" / "you're right" / "nice catch"

The detector is *write-only* to the calibration state. The read side
-- "should Aiko hedge this turn?" -- lives in
:func:`render_inner_life_block`, called by the inner-life provider on
the *next* turn.

Posture: verbal hedging only. K20 deliberately does NOT touch RAG
retrieval scores. F3 (``memory.confidence`` + ``(uncertain)``
suffix) already owns the per-memory accuracy lane; K20 is the
*per-user / per-topic register tilt* on top of it.

Design choices baked in here:

- **Stateless module, not a class.** All state lives on the
  :class:`CalibrationState` snapshot passed in. Same posture as the
  K8 / K17 / K22 detectors.
- **Regex-first detection.** Strong / mild / affirmation are pure
  regex (microseconds). Softening adds an embedding cosine guard
  *plus* a hedge-token regex -- bare cosine fires false positives
  on plain topic continuation, so the AND gate is the whole point.
- **Allocation, not clustering.** Topic slots merge on cosine >=
  ``topic_merge_threshold`` else allocate a new slot. On overflow
  we evict the slot whose ``abs(score - baseline)`` is smallest AND
  whose ``last_signal_at`` is oldest (composite key). No K-means;
  K9 will replace this with proper clusters when it ships.
- **Lazy decay.** Exponential drift toward the baseline based on
  elapsed time since ``last_updated_at``. Called once on every read
  (the inner-life provider) and once on every write (right before
  ``apply_signal``) so the delta lands on a current snapshot rather
  than a stale one.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import numpy as np

from app.core.affect.calibration_store import CalibrationState, TopicSlot


log = logging.getLogger("app.calibration_detector")


# ── Regex bands ──────────────────────────────────────────────────────


# Strong pushback: explicit "you're wrong" / "let me check" / "that's
# not right". Each pattern is anchored to word boundaries so partial
# matches inside larger tokens don't fire (e.g. "wrong-headed" stays
# silent).
_PUSHBACK_STRONG: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\bare\s+you\s+sure\b(?:[^.?!\n]{0,40}\b(?:that'?s|thats|about\s+that|right|correct)\b)?",
        re.I,
    ),
    re.compile(r"\bactually[, ]+(?:it'?s|that'?s|the|no)\b", re.I),
    re.compile(r"\b(?:wait|hold\s+on)[, ]+(?:no|that'?s\s+not)\b", re.I),
    re.compile(r"\bi\s+think\s+you(?:'?re|\s+are)\s+wrong\b", re.I),
    re.compile(
        r"\b(?:that'?s|thats|this\s+is|it'?s|its)\s+(?:not\s+right|incorrect|wrong|not\s+correct)\b",
        re.I,
    ),
    re.compile(
        r"\blet\s+me\s+(?:double[- ]?check|verify|fact[- ]?check|look\s+that\s+up|check\s+that)\b",
        re.I,
    ),
    re.compile(
        r"\bi'?ll\s+have\s+to\s+(?:double[- ]?check|verify|look\s+that\s+up)\b",
        re.I,
    ),
    re.compile(r"\bthat\s+(?:doesn'?t|does\s+not)\s+sound\s+right\b", re.I),
)


# Mild pushback: softer doubt. Aiko's authority is shaky but Jacob
# isn't explicitly correcting her.
_PUSHBACK_MILD: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:hmm|hm|mmm),?\s+(?:are\s+you\s+sure|really)\b", re.I),
    re.compile(r"(?:^|[\s\.])(?:really|seriously)\?+", re.I),
    re.compile(r"\bi'?m\s+not\s+(?:so\s+)?sure\s+(?:about|that)\b", re.I),
    re.compile(r"\bis\s+that\s+(?:right|accurate|correct|true)\??", re.I),
    re.compile(r"\bdoesn'?t\s+that\s+(?:contradict|sound\s+off)\b", re.I),
)


# Affirmation: pulls the calibration back up toward baseline.
_AFFIRM: tuple[re.Pattern[str], ...] = (
    re.compile(r"\byou(?:'?re|\s+are)\s+(?:totally\s+|completely\s+)?right\b", re.I),
    re.compile(r"\bgood\s+(?:call|catch|point|one)\b", re.I),
    re.compile(r"\bnice\s+catch\b", re.I),
    re.compile(r"\byeah[, ]+(?:that'?s|exactly|right|correct)\b", re.I),
    re.compile(r"\b(?:exactly|that'?s\s+it)\b", re.I),
    re.compile(r"\b(?:huh|oh)[,!. ]+(?:you'?re|you\s+are)\s+right\b", re.I),
)


# Hedge tokens for the softening AND-guard. Pure cosine alone fires
# on plain topic continuation (Jacob keeps talking about the same
# subject); the hedge token is the disambiguator that means "I'm
# rephrasing what you said with doubt baked in".
_HEDGE_TOKEN: re.Pattern[str] = re.compile(
    r"\b(?:right\?+|are\s+you\s+sure|you\s+mean|is\s+that\s+right|"
    r"so\s+you(?:'?re|\s+are)\s+saying|so\s+(?:basically|essentially)|"
    r"huh\?+|wait\??)\b",
    re.I,
)


SignalKind = Literal[
    "pushback_strong",
    "pushback_mild",
    "softening",
    "affirmation",
]


# Per-band deltas applied to global + matched topic slot. Negative
# deltas drop trust; positive nudges it back up. Magnitudes are
# deliberately small so one false positive doesn't crater the
# calibration -- the decay path is the real recovery channel.
_BAND_DELTAS: dict[SignalKind, float] = {
    "pushback_strong": -0.10,
    "pushback_mild": -0.05,
    "softening": -0.07,
    "affirmation": +0.04,
}


@dataclass(frozen=True, slots=True)
class CalibrationSignal:
    """One classified user-turn signal. Returned by :func:`detect`,
    consumed by :func:`apply_signal`."""

    kind: SignalKind
    delta: float
    trigger_excerpt: str


# ── Detection ────────────────────────────────────────────────────────


def detect(
    *,
    user_text: str,
    user_vec: np.ndarray | None = None,
    prior_assistant_vec: np.ndarray | None = None,
    softening_cosine_threshold: float = 0.70,
) -> CalibrationSignal | None:
    """Classify ``user_text`` into a calibration signal or ``None``.

    Priority order (first match wins, terminating evaluation):

    1. ``pushback_strong`` -- explicit "you're wrong" / "let me check"
    2. ``pushback_mild``   -- softer doubt
    3. ``softening``       -- cosine guard + hedge-token AND
    4. ``affirmation``     -- "you're right" / "good call"

    Pushback beats affirmation when both regex families match the
    same message (rare, but a string like "you're right, but are you
    sure about the date?" reads as net pushback).

    Defensive against empty/short input: messages under 4 chars after
    strip never fire (rhetorical "huh?" is its own short clause).
    """
    cleaned = (user_text or "").strip()
    if len(cleaned) < 4:
        return None

    # Strong pushback
    for pattern in _PUSHBACK_STRONG:
        match = pattern.search(cleaned)
        if match is not None:
            return CalibrationSignal(
                kind="pushback_strong",
                delta=_BAND_DELTAS["pushback_strong"],
                trigger_excerpt=_trim_excerpt(match.group(0)),
            )

    # Mild pushback
    for pattern in _PUSHBACK_MILD:
        match = pattern.search(cleaned)
        if match is not None:
            return CalibrationSignal(
                kind="pushback_mild",
                delta=_BAND_DELTAS["pushback_mild"],
                trigger_excerpt=_trim_excerpt(match.group(0)),
            )

    # Softening: hedge-token AND high cosine to the prior assistant
    # message. Both must hold -- bare cosine fires on topic
    # continuation, bare hedge token would double-count with the
    # mild-pushback patterns above.
    hedge_match = _HEDGE_TOKEN.search(cleaned)
    if (
        hedge_match is not None
        and user_vec is not None
        and prior_assistant_vec is not None
        and getattr(user_vec, "size", 0) > 0
        and getattr(prior_assistant_vec, "size", 0) > 0
    ):
        try:
            sim = _cosine(user_vec, prior_assistant_vec)
        except Exception:
            sim = -1.0
        if sim >= float(softening_cosine_threshold):
            return CalibrationSignal(
                kind="softening",
                delta=_BAND_DELTAS["softening"],
                trigger_excerpt=_trim_excerpt(hedge_match.group(0)),
            )

    # Affirmation
    for pattern in _AFFIRM:
        match = pattern.search(cleaned)
        if match is not None:
            return CalibrationSignal(
                kind="affirmation",
                delta=_BAND_DELTAS["affirmation"],
                trigger_excerpt=_trim_excerpt(match.group(0)),
            )

    return None


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    av = np.asarray(a, dtype=np.float32)
    bv = np.asarray(b, dtype=np.float32)
    if av.size == 0 or bv.size == 0 or av.shape != bv.shape:
        return -1.0
    na = float(np.linalg.norm(av))
    nb = float(np.linalg.norm(bv))
    if na <= 0.0 or nb <= 0.0:
        return -1.0
    return float((av * bv).sum()) / (na * nb)


def _trim_excerpt(text: str, *, limit: int = 60) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "..."


# ── State updates ───────────────────────────────────────────────────


def apply_signal(
    state: CalibrationState,
    *,
    signal: CalibrationSignal,
    assistant_vec: np.ndarray | None,
    now: datetime | None = None,
    topic_merge_threshold: float = 0.78,
    max_topic_slots: int = 8,
    baseline: float = 0.80,
) -> CalibrationState:
    """Return a new state with the signal applied.

    - ``global_score`` += ``signal.delta`` clamped to ``[0, 1]``
    - If ``assistant_vec`` is provided, locate or allocate a topic
      slot whose centroid has cosine >= ``topic_merge_threshold``
      with the assistant vector; apply the delta to its score
      (clamped) and update its centroid via an exponential running
      mean.
    - On overflow (>= ``max_topic_slots``), evict the slot whose
      score is closest to ``baseline`` AND whose ``last_signal_at``
      is oldest (the slot with the weakest signal that hasn't moved
      recently).
    - ``last_updated_at`` is bumped to ``now``.
    """
    now_ts = now or datetime.now(timezone.utc)
    new_global = _clamp(state.global_score + signal.delta, 0.0, 1.0)

    new_topics: list[TopicSlot] = list(state.topics)
    if assistant_vec is not None and getattr(assistant_vec, "size", 0) > 0:
        try:
            av = np.asarray(assistant_vec, dtype=np.float32)
            norm = float(np.linalg.norm(av))
            if norm > 0.0:
                if abs(norm - 1.0) > 1e-3:
                    av = av / norm
                # Find matching slot
                best_idx = -1
                best_sim = -1.0
                for idx, slot in enumerate(new_topics):
                    try:
                        sim = float((slot.centroid * av).sum())
                    except Exception:
                        continue
                    if sim > best_sim:
                        best_sim = sim
                        best_idx = idx

                if best_idx >= 0 and best_sim >= float(topic_merge_threshold):
                    # Merge: exponential moving average on the centroid
                    # (alpha=0.30 -- a hit nudges the centroid toward
                    # the new vector but doesn't replace it).
                    existing = new_topics[best_idx]
                    alpha = 0.30
                    merged_centroid = (1.0 - alpha) * existing.centroid + alpha * av
                    cnorm = float(np.linalg.norm(merged_centroid))
                    if cnorm > 0.0:
                        merged_centroid = merged_centroid / cnorm
                    new_topics[best_idx] = TopicSlot(
                        centroid=merged_centroid.astype(np.float32),
                        score=_clamp(
                            existing.score + signal.delta, 0.0, 1.0,
                        ),
                        last_signal_at=now_ts,
                        signal_count=existing.signal_count + 1,
                    )
                else:
                    # Allocate a fresh slot starting at baseline so a
                    # single signal moves it visibly (baseline + delta)
                    # without pinning it to the extreme.
                    new_slot = TopicSlot(
                        centroid=av.astype(np.float32),
                        score=_clamp(baseline + signal.delta, 0.0, 1.0),
                        last_signal_at=now_ts,
                        signal_count=1,
                    )
                    if len(new_topics) >= int(max_topic_slots):
                        evict_idx = _pick_eviction(
                            new_topics, baseline=baseline,
                        )
                        if evict_idx >= 0:
                            new_topics.pop(evict_idx)
                    new_topics.append(new_slot)
        except Exception:
            log.debug(
                "calibration-detector: apply_signal topic raised",
                exc_info=True,
            )

    log.info(
        "calibration: kind=%s delta=%+.2f global=%.2f->%.2f topics=%d",
        signal.kind,
        signal.delta,
        state.global_score,
        new_global,
        len(new_topics),
    )

    return CalibrationState(
        global_score=new_global,
        last_updated_at=now_ts,
        topics=tuple(new_topics),
    )


def _pick_eviction(
    topics: list[TopicSlot], *, baseline: float,
) -> int:
    """Return the index of the slot whose score is closest to
    ``baseline`` AND whose ``last_signal_at`` is oldest (composite
    key: smaller distance from baseline wins; ties broken by older
    timestamp). Returns -1 on an empty list."""
    if not topics:
        return -1
    best_idx = 0
    best_key: tuple[float, datetime] | None = None
    for idx, slot in enumerate(topics):
        distance = abs(slot.score - float(baseline))
        key = (distance, slot.last_signal_at)
        if best_key is None or key < best_key:
            best_key = key
            best_idx = idx
    return best_idx


def decay(
    state: CalibrationState,
    *,
    now: datetime | None = None,
    half_life_days: float = 5.0,
    baseline: float = 0.80,
    topic_half_life_multiplier: float = 1.6,
) -> CalibrationState:
    """Apply exponential decay toward ``baseline`` based on elapsed
    time since ``last_updated_at``. Idempotent when no time has
    passed; safe to call on a brand-new state (``last_updated_at is
    None`` -> no-op return of the input).

    Topic slots decay slower than the global score
    (``topic_half_life_multiplier`` lengthens the half-life) -- a
    learned topic stance should outlive a general bad day.
    """
    if state.last_updated_at is None:
        return state
    if float(half_life_days) <= 0.0:
        return state

    now_ts = now or datetime.now(timezone.utc)
    elapsed_seconds = (now_ts - state.last_updated_at).total_seconds()
    if elapsed_seconds <= 0:
        return state
    elapsed_days = elapsed_seconds / 86400.0

    global_frac = math.exp(
        -math.log(2.0) * elapsed_days / float(half_life_days)
    )
    topic_half = float(half_life_days) * float(topic_half_life_multiplier)
    topic_frac = math.exp(-math.log(2.0) * elapsed_days / topic_half)

    new_global = (
        float(baseline) + (state.global_score - float(baseline)) * global_frac
    )
    new_global = _clamp(new_global, 0.0, 1.0)

    new_topics: list[TopicSlot] = []
    for slot in state.topics:
        new_score = (
            float(baseline) + (slot.score - float(baseline)) * topic_frac
        )
        new_topics.append(
            TopicSlot(
                centroid=slot.centroid,
                score=_clamp(new_score, 0.0, 1.0),
                last_signal_at=slot.last_signal_at,
                signal_count=slot.signal_count,
            )
        )

    return CalibrationState(
        global_score=new_global,
        last_updated_at=now_ts,
        topics=tuple(new_topics),
    )


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, float(value)))


# ── Render ──────────────────────────────────────────────────────────


def render_inner_life_block(
    state: CalibrationState,
    *,
    user_display_name: str = "the user",
    global_threshold: float = 0.55,
    topic_threshold: float = 0.50,
) -> str | None:
    """Render a one-line directive when ``state`` warrants it.

    Returns ``None`` (silent) unless ``global_score < global_threshold``
    OR a topic slot's score is below ``topic_threshold``. When both
    fire, the **topic-specific** cue wins -- it carries more
    actionable hedging guidance than a generic global note.

    Topic-specific cue uses a generic descriptor ("your claims around
    this topic") because we don't yet have cluster labels; the
    descriptor is intentionally vague so Aiko fills in the specifics
    from the conversation context.
    """
    name = (user_display_name or "").strip() or "the user"

    # Pick the lowest-scoring topic slot below the topic threshold
    # (if any). We don't render a slot that's still trending up
    # toward baseline.
    low_topic: TopicSlot | None = None
    for slot in state.topics:
        if slot.score >= float(topic_threshold):
            continue
        if low_topic is None or slot.score < low_topic.score:
            low_topic = slot

    if low_topic is not None:
        return (
            f"Heads-up: {name} has been pushing back on your claims around "
            f"this topic lately (your accuracy reads tentative there). "
            "Lead the next claim with a hedge -- \"I think...\", \"if I'm "
            "remembering right...\" -- rather than the conclusion."
        )

    if state.global_score < float(global_threshold):
        return (
            f"Heads-up: {name} has been double-checking you a lot lately. "
            "Treat your own claims as drafts -- soften the confident "
            "phrasing this turn and offer to verify if the topic is one "
            "you're not solid on."
        )

    return None


__all__ = [
    "CalibrationSignal",
    "SignalKind",
    "apply_signal",
    "decay",
    "detect",
    "render_inner_life_block",
]
