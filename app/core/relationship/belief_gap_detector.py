"""Belief applicability maintenance and compatibility gap rendering.

Aiko's AffectState describes Aiko, not the user's topic-specific feelings.
Lexical overlap/antonyms likewise do not verify a scoped interpretation.
The post-turn hook ages records; source-linked revisions happen in the worker
and explicit corrections in the review UI. Neither creates a forced question.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.core.relationship.belief_store import (
    BeliefStore,
    KIND_MOOD,
)
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.affect.affect_state import AffectState


log = logging.getLogger("app.belief_gap_detector")


# Default thresholds. Overridable via ``belief_settings`` to keep
# tests cheap and let the user dial the gap sensitivity in config.
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
    return timephrase.utcnow()


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
        evidence_message_id: int | None = None,
    ) -> list[BeliefGap]:
        """Age records without treating affect or lexical similarity as evidence.

        Deprecated signal arguments are accepted for caller compatibility and
        intentionally ignored. A model of Aiko's emotion cannot verify the user.
        """
        self._sweep_stale(user_id=user_id)
        return []

    # ── stale sweep ──────────────────────────────────────────────────

    def _sweep_stale(self, *, user_id: str) -> None:
        days = float(
            getattr(
                self._belief_settings,
                "belief_stale_after_days",
                _DEFAULT_STALE_AFTER_DAYS,
            )
        )
        # Structured applicability still expires when legacy aging is disabled.
        cutoff = (self._clock() - timedelta(days=days if days > 0 else 365000)).isoformat()
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
