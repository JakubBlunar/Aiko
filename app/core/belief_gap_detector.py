"""Belief gap detector (K2 personality backlog).

Post-turn helper that compares Aiko's active mood beliefs against
the live :class:`app.core.affect_state.AffectState` and her active
opinion beliefs against the user's most recent message. Mismatches
produce :class:`BeliefGap` records the
``_belief_inner_life_provider`` renders into the next-turn prompt
so Aiko can either gently name the gap ("I had you pegged as
excited about Tokyo -- am I reading this wrong?") or silently
update her model.

Two detection passes, both pure functions over the store state:

1. **Mood gap** -- for every ``active`` mood belief younger than
   ``belief_recent_window_hours``, compare the stored
   ``valence`` / ``arousal`` against the live affect read. A gap
   fires when ``|val_pred - val_obs| > belief_gap_valence_threshold``
   OR ``|aro_pred - aro_obs| > belief_gap_arousal_threshold``
   OR the recomputed mood label flips into the opposing
   valence-band. ``status`` is bumped to ``contradicted`` and
   ``gap_seen_at`` stamped; the gap tuple is returned for the
   inner-life provider.

2. **Opinion gap** -- for every ``active`` opinion belief, run
   :func:`app.core.conflict_heuristics.classify_pair` against the
   most recent user message. A ``definite`` heuristic flips the
   belief to ``contradicted``; a strong content overlap with no
   contradiction signal nudges it to ``confirmed``.

Stale aging is a third pass run opportunistically: any active
belief whose ``last_checked_at`` (or ``observed_at`` when never
checked) is older than ``belief_stale_after_days`` is flipped to
``stale`` and dropped from future detector ticks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.core.belief_store import (
    Belief,
    BeliefStore,
    KIND_MOOD,
    KIND_OPINION,
    STATUS_ACTIVE,
)
from app.core.conflict_heuristics import (
    HEURISTIC_DEFINITE,
    classify_pair,
)

if TYPE_CHECKING:
    from app.core.affect_state import AffectState


log = logging.getLogger("app.belief_gap_detector")


# Default thresholds. Overridable via ``belief_settings`` to keep
# tests cheap and let the user dial the gap sensitivity in config.
_DEFAULT_VAL_THRESHOLD = 0.30
_DEFAULT_ARO_THRESHOLD = 0.25
_DEFAULT_RECENT_WINDOW_HOURS = 24
_DEFAULT_STALE_AFTER_DAYS = 90


@dataclass(slots=True, frozen=True)
class BeliefGap:
    """One detected mismatch the inner-life provider will render."""

    belief_id: int
    kind: str
    topic: str
    predicted_state: str
    confidence: float
    reason: str
    # Optional: the observed state we compared against. ``observed`` is
    # the mood label for mood gaps; ``None`` for opinion gaps because
    # the comparator there is a user message, not a snapshot of state.
    observed: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp))
    except ValueError:
        return None


def _valence_band(valence: float) -> str:
    """Coarse valence band used to detect "opposing" flips.

    Three bands: ``pos`` / ``neutral`` / ``neg``. We only call a
    label-flip gap when the predicted and observed valence sit in
    *opposing* bands (``pos`` vs ``neg``) -- crossing into neutral
    is too noisy to flag.
    """
    if valence >= 0.10:
        return "pos"
    if valence <= -0.10:
        return "neg"
    return "neutral"


class BeliefGapDetector:
    """Compare active beliefs against live signals and surface gaps."""

    def __init__(
        self,
        *,
        belief_store: BeliefStore,
        belief_settings: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self._belief_store = belief_store
        self._belief_settings = belief_settings
        self._clock = clock or _utcnow

    # ── public API ───────────────────────────────────────────────────

    def detect(
        self,
        *,
        user_id: str,
        affect: "AffectState | None" = None,
        recent_user_message: str | None = None,
    ) -> list[BeliefGap]:
        """Run both detector passes and return the surfaced gaps.

        ``affect`` may be ``None`` (e.g. on the first turn before the
        AffectStore has a row); we just skip the mood pass in that case.
        ``recent_user_message`` may be ``None`` for the same reason on
        the opinion pass.
        """
        gaps: list[BeliefGap] = []

        # 1. mark stale rows first so we don't compare against ancient
        # predictions on this tick.
        self._sweep_stale(user_id=user_id)

        # 2. mood pass.
        if affect is not None:
            gaps.extend(
                self._detect_mood_gaps(user_id=user_id, affect=affect)
            )

        # 3. opinion pass.
        text = (recent_user_message or "").strip()
        if text:
            gaps.extend(
                self._detect_opinion_gaps(
                    user_id=user_id, user_message=text,
                )
            )

        if gaps:
            log.info(
                "belief-gap-detector: gaps=%d (user=%s)",
                len(gaps),
                user_id,
            )
        else:
            log.debug(
                "belief-gap-detector: no gaps user=%s",
                user_id,
            )
        return gaps

    # ── mood pass ────────────────────────────────────────────────────

    def _detect_mood_gaps(
        self,
        *,
        user_id: str,
        affect: "AffectState",
    ) -> list[BeliefGap]:
        recent_hours = float(
            getattr(
                self._belief_settings,
                "belief_recent_window_hours",
                _DEFAULT_RECENT_WINDOW_HOURS,
            )
        )
        val_threshold = float(
            getattr(
                self._belief_settings,
                "belief_gap_valence_threshold",
                _DEFAULT_VAL_THRESHOLD,
            )
        )
        aro_threshold = float(
            getattr(
                self._belief_settings,
                "belief_gap_arousal_threshold",
                _DEFAULT_ARO_THRESHOLD,
            )
        )

        now = self._clock()
        since_iso = (now - timedelta(hours=recent_hours)).isoformat()
        beliefs = self._belief_store.list_active_for_gap_check(
            user_id=user_id, kind=KIND_MOOD, since_iso=since_iso,
        )
        gaps: list[BeliefGap] = []
        for b in beliefs:
            if b.valence is None:
                # Worker / tag only stored a textual label without
                # numeric coordinates; stamp_checked but skip the
                # numeric comparator.
                self._belief_store.stamp_checked(b.id, gap=False)
                continue
            val_diff = abs(float(b.valence) - float(affect.valence))
            aro_diff = (
                abs(float(b.arousal) - float(affect.arousal))
                if b.arousal is not None
                else 0.0
            )
            pred_band = _valence_band(float(b.valence))
            obs_band = _valence_band(float(affect.valence))
            band_flip = (
                pred_band != "neutral"
                and obs_band != "neutral"
                and pred_band != obs_band
            )
            if val_diff > val_threshold or aro_diff > aro_threshold or band_flip:
                reason = self._render_mood_reason(
                    b, affect=affect,
                    val_diff=val_diff, aro_diff=aro_diff,
                    band_flip=band_flip,
                )
                self._belief_store.mark_contradicted(b.id, stamp_gap=True)
                gaps.append(
                    BeliefGap(
                        belief_id=b.id,
                        kind=b.kind,
                        topic=b.topic,
                        predicted_state=b.predicted_state,
                        confidence=b.confidence,
                        reason=reason,
                        observed=str(affect.mood_label),
                    )
                )
                log.info(
                    "belief-gap MOOD: id=%s topic=%r predicted=%r vs "
                    "observed=%s (val_diff=%.2f aro_diff=%.2f flip=%s)",
                    b.id,
                    b.topic,
                    b.predicted_state,
                    affect.mood_label,
                    val_diff,
                    aro_diff,
                    band_flip,
                )
            else:
                self._belief_store.stamp_checked(b.id, gap=False)
        return gaps

    def _render_mood_reason(
        self,
        b: Belief,
        *,
        affect: "AffectState",
        val_diff: float,
        aro_diff: float,
        band_flip: bool,
    ) -> str:
        if band_flip:
            return (
                f"predicted {b.predicted_state} but mood actually reads "
                f"{affect.mood_label}"
            )
        if val_diff >= aro_diff:
            return (
                f"valence drifted {val_diff:.2f} from {b.predicted_state}; "
                f"now {affect.mood_label}"
            )
        return (
            f"arousal drifted {aro_diff:.2f}; {affect.mood_label} doesn't "
            f"match {b.predicted_state}"
        )

    # ── opinion pass ─────────────────────────────────────────────────

    def _detect_opinion_gaps(
        self,
        *,
        user_id: str,
        user_message: str,
    ) -> list[BeliefGap]:
        # Opinions have no time window: an old belief can still be
        # contradicted by a fresh statement. We still bound the list
        # to a sensible cap to keep the heuristic loop bounded.
        beliefs = self._belief_store.list_active(
            user_id=user_id, kind=KIND_OPINION, limit=200,
        )
        gaps: list[BeliefGap] = []
        for b in beliefs:
            # We compare the user's literal message against the
            # belief's "{topic} {predicted_state}" rendering. That
            # gives the heuristic enough lexical surface to catch a
            # direct negation ("rust isn't overhyped at all").
            belief_text = f"{b.topic} {b.predicted_state}"
            result = classify_pair(user_message, belief_text)
            if result.label == HEURISTIC_DEFINITE:
                reason = "user message contradicts prediction (" + ", ".join(
                    result.signals
                ) + ")"
                self._belief_store.mark_contradicted(b.id, stamp_gap=True)
                gaps.append(
                    BeliefGap(
                        belief_id=b.id,
                        kind=b.kind,
                        topic=b.topic,
                        predicted_state=b.predicted_state,
                        confidence=b.confidence,
                        reason=reason,
                        observed=None,
                    )
                )
                log.info(
                    "belief-gap OPINION: id=%s topic=%r predicted=%r "
                    "signals=%s",
                    b.id,
                    b.topic,
                    b.predicted_state,
                    result.signals,
                )
                continue
            # Cheap confirmation pass: if the message tokenises with a
            # strong overlap with the belief text and there's no
            # contradiction signal, nudge toward ``confirmed``.
            if self._strong_overlap(user_message, belief_text):
                self._belief_store.mark_confirmed(b.id)
                log.info(
                    "belief-gap OPINION confirmed: id=%s topic=%r",
                    b.id,
                    b.topic,
                )
            else:
                self._belief_store.stamp_checked(b.id, gap=False)
        return gaps

    @staticmethod
    def _strong_overlap(text_a: str, text_b: str) -> bool:
        """Jaccard >= 0.6 on lowercased word sets, ignoring stopwords."""
        stop = {
            "a", "an", "and", "as", "at", "be", "by", "for", "i", "in",
            "is", "it", "its", "of", "on", "or", "that", "the", "to",
            "was", "with", "you", "your", "he", "she", "they", "we",
        }
        toks_a = {
            t for t in (text_a or "").lower().split() if t and t not in stop
        }
        toks_b = {
            t for t in (text_b or "").lower().split() if t and t not in stop
        }
        if not toks_a or not toks_b:
            return False
        inter = len(toks_a & toks_b)
        union = len(toks_a | toks_b)
        if union == 0:
            return False
        return (inter / union) >= 0.6

    # ── stale sweep ──────────────────────────────────────────────────

    def _sweep_stale(self, *, user_id: str) -> None:
        days = float(
            getattr(
                self._belief_settings,
                "belief_stale_after_days",
                _DEFAULT_STALE_AFTER_DAYS,
            )
        )
        if days <= 0:
            return
        cutoff = (self._clock() - timedelta(days=days)).isoformat()
        try:
            n = self._belief_store.mark_stale_older_than(
                cutoff_iso=cutoff, user_id=user_id,
            )
        except Exception:
            log.debug("belief-gap-detector: stale sweep raised", exc_info=True)
            return
        if n:
            log.info(
                "belief-gap-detector: marked %d stale beliefs older than %s",
                n,
                cutoff,
            )


def render_inner_life_block(gaps: list[BeliefGap], *, max_lines: int = 2) -> str:
    """Render up to ``max_lines`` gap lines for the inner-life prompt.

    The block is the post-turn provider's contribution to the next
    turn's prompt: short, present-tense, third-person -- the same
    voice the other inner-life providers use. Mood gaps prefer
    "Jacob's mood reads X, not Y"; opinion gaps prefer "Jacob pushed
    back on your read that X is Y".
    """
    if not gaps:
        return ""
    lines: list[str] = []
    for g in gaps[:max_lines]:
        if g.kind == KIND_MOOD:
            if g.observed:
                lines.append(
                    f"You had Jacob pegged as {g.predicted_state} about "
                    f"{g.topic}; the room actually reads {g.observed}."
                )
            else:
                lines.append(
                    f"Your {g.predicted_state} read on {g.topic} isn't "
                    "matching the live affect."
                )
        else:
            lines.append(
                f"Jacob pushed back on your read that {g.topic} is "
                f"{g.predicted_state} -- update accordingly."
            )
    return "\n".join(lines)
