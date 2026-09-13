"""Live policy context: rails, situation concepts, modifiers, ledger.

Does not call a policy model, take cues, or write T3 habituation / L37.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable

from app.core.concepts.policy_selector import (
    PolicyConceptPick,
    select_bounded_concepts,
)
from app.core.infra import timephrase
from app.core.live.epochs import classify_epoch
from app.core.live.frame import LiveSituationFrame
from app.core.live.modifiers import (
    LiveBehaviorModifiers,
    apply_modifiers_to_urges,
    modifiers_from_situation,
)
from app.core.live.urge_store import LiveUrgeStore


LIVE_POLICY_TOKEN_BUDGET = 96
_LEDGER_CAP = 48


def situation_summary(frame: LiveSituationFrame) -> str:
    """Normalized present-tense situation line for embedding."""
    inferred = str(frame.shared.inferred.label or "unknown")
    sharing = str(frame.shared.sharing or "unknown")
    app = str(frame.shared.user_active_app or "none")
    os_idle = str(frame.shared.os_idle or "missing")
    session_s = int(frame.shared.session_duration_s or 0)
    attention = f"{frame.attention.target} {frame.attention.mode}".strip()
    sleep = str(frame.constraints.sleep_status or "awake")
    activity = str(frame.shared.world_activity or "idle")
    return (
        f"inferred {inferred}; sharing {sharing}; app {app}; "
        f"os_idle {os_idle}; session_s={session_s}; "
        f"attention {attention}; sleep {sleep}; activity {activity}"
    )


def render_behavior_rails(picks: tuple[PolicyConceptPick, ...]) -> tuple[str, ...]:
    """Short imperative guidance. Never dialogue to quote."""
    lines: list[str] = []
    for pick in picks:
        if pick.lane != "rail":
            continue
        label = pick.label[:72]
        if pick.kind == "boundary":
            line = f"Respect: {label}"
        elif pick.kind == "ritual":
            line = f"Shared ritual: {label}; let silence carry it."
        elif pick.kind == "value":
            line = f"Hold: {label}"
        elif pick.kind == "affective":
            line = f"Affect: {label}"
        else:
            line = label
        if line not in lines:
            lines.append(line)
        if len(lines) >= 4:
            break
    return tuple(lines)


@dataclass(frozen=True, slots=True)
class LivePolicyLedgerEntry:
    frame_generation: int
    concept_ids: tuple[int, ...]
    reasons: tuple[str, ...]
    scores: tuple[float, ...]
    modifiers: dict[str, Any]
    proposed_action: str = ""
    arbiter_result: str = ""
    completed_action: str = ""
    main_model_used: bool = False
    occurred_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "frame_generation": self.frame_generation,
            "concept_ids": list(self.concept_ids),
            "reasons": list(self.reasons),
            "scores": list(self.scores),
            "modifiers": dict(self.modifiers),
            "proposed_action": self.proposed_action,
            "arbiter_result": self.arbiter_result,
            "completed_action": self.completed_action,
            "main_model_used": self.main_model_used,
            "occurred_at": self.occurred_at,
        }


class LivePolicyContextRuntime:
    """Read concepts, stamp rails and modifiers, record a ledger row."""

    def __init__(
        self,
        *,
        view_provider: Callable[[], Any] | None = None,
        embedder_provider: Callable[[], Any] | None = None,
        token_budget_provider: Callable[[], int] | None = None,
    ) -> None:
        self._view_provider = view_provider
        self._embedder_provider = embedder_provider
        self._token_budget_provider = token_budget_provider
        self._last_picks: tuple[PolicyConceptPick, ...] = ()
        self._last_modifiers = LiveBehaviorModifiers()
        self._ledger: deque[LivePolicyLedgerEntry] = deque(maxlen=_LEDGER_CAP)

    @property
    def last_modifiers(self) -> LiveBehaviorModifiers:
        return self._last_modifiers

    @property
    def last_picks(self) -> tuple[PolicyConceptPick, ...]:
        return self._last_picks

    def apply(
        self,
        frame: LiveSituationFrame,
        *,
        urges: LiveUrgeStore | None = None,
        trigger_kind: str = "heartbeat",
        live_quiet: bool = False,
        min_gap_after_speech_ms: int = 0,
    ) -> LiveSituationFrame:
        epoch = classify_epoch(trigger_kind)
        refresh = epoch != "data_only" or not self._last_picks
        picks = self._last_picks
        if refresh:
            picks = self._select(frame, refresh_relevant=epoch != "data_only")
            self._last_picks = picks
        modifiers = modifiers_from_situation(
            frame, picks,
            live_quiet=live_quiet,
            min_gap_after_speech_ms=min_gap_after_speech_ms,
        )
        self._last_modifiers = modifiers
        if urges is not None:
            apply_modifiers_to_urges(urges, modifiers)
        self._append_ledger(frame, picks, modifiers)
        return self._stamp(frame, picks, modifiers)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "policy_concepts": [item.to_payload() for item in self._last_picks],
            "modifiers": self._last_modifiers.to_payload(),
            "policy_ledger": [
                item.to_payload() for item in list(self._ledger)[-8:]
            ],
        }

    def _select(
        self,
        frame: LiveSituationFrame,
        *,
        refresh_relevant: bool,
    ) -> tuple[PolicyConceptPick, ...]:
        view = self._view()
        rails: list[Any] = []
        relevant: list[tuple[Any, float]] = []
        if view is not None:
            try:
                rails = list(view.for_consumer("live_policy") or [])
            except Exception:
                rails = []
            if refresh_relevant:
                relevant = self._relevant(view, frame)
        return select_bounded_concepts(
            rails=rails,
            relevant=relevant,
            token_budget=self._token_budget(),
            habituation=1.0,
        )

    def _token_budget(self) -> int:
        provider = self._token_budget_provider
        if callable(provider):
            try:
                value = int(provider() or 0)
                if value > 0:
                    return value
            except Exception:
                pass
        return LIVE_POLICY_TOKEN_BUDGET

    def _relevant(self, view: Any, frame: LiveSituationFrame) -> list[tuple[Any, float]]:
        embedder = self._embedder()
        if embedder is None or not hasattr(view, "relevant"):
            return []
        try:
            vector = embedder.embed(situation_summary(frame))
        except Exception:
            return []
        if vector is None:
            return []
        try:
            pairs = view.relevant(vector, k=8, min_sim=0.15)
        except Exception:
            return []
        return list(pairs or [])

    def _view(self) -> Any:
        provider = self._view_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            return None

    def _embedder(self) -> Any:
        provider = self._embedder_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            return None

    def _append_ledger(
        self,
        frame: LiveSituationFrame,
        picks: tuple[PolicyConceptPick, ...],
        modifiers: LiveBehaviorModifiers,
    ) -> None:
        self._ledger.append(
            LivePolicyLedgerEntry(
                frame_generation=int(frame.generation),
                concept_ids=tuple(pick.concept_id for pick in picks),
                reasons=tuple(pick.reason for pick in picks),
                scores=tuple(pick.score for pick in picks),
                modifiers=modifiers.to_payload(),
                proposed_action="",
                occurred_at=timephrase.utcnow().isoformat(timespec="seconds"),
            )
        )

    def record_outcome(
        self,
        *,
        proposed_action: str,
        arbiter_result: str,
        main_model_used: bool = False,
        executed: bool = False,
    ) -> None:
        if not self._ledger:
            return
        last = self._ledger[-1]
        action = str(proposed_action or "")
        self._ledger[-1] = replace(
            last,
            proposed_action=action,
            arbiter_result=str(arbiter_result or ""),
            completed_action=action if executed else "",
            main_model_used=bool(main_model_used),
        )

    def _stamp(
        self,
        frame: LiveSituationFrame,
        picks: tuple[PolicyConceptPick, ...],
        modifiers: LiveBehaviorModifiers,
    ) -> LiveSituationFrame:
        rails = render_behavior_rails(picks)
        situation = tuple(
            pick.label for pick in picks if pick.lane == "situation"
        )
        prior = frame.continuity.situation_concepts
        continuity = replace(
            frame.continuity,
            behavior_rails=rails,
            situation_concepts=prior if prior else situation,
        )
        forbid = list(frame.constraints.speech_forbid_reasons)
        if modifiers.speech_budget == "forbidden" and "speech_budget" not in forbid:
            forbid.append("speech_budget")
        constraints = replace(
            frame.constraints,
            speech_budget=modifiers.speech_budget,
            questions_allowed=modifiers.questions_allowed,
            min_gap_after_speech_ms=modifiers.min_gap_after_speech_ms,
            max_reaction_intensity=modifiers.max_reaction_intensity,
            speech_forbid_reasons=tuple(forbid),
        )
        return replace(frame, continuity=continuity, constraints=constraints)
