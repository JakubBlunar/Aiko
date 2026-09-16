"""In-memory Live urge store: merge, compete, park, expire, withdraw."""
from __future__ import annotations

import uuid
from typing import Iterable

from app.core.infra import timephrase
from app.core.live.urge import (
    ACTIVE_STATES,
    EXPRESSIVE_KINDS,
    TERMINAL_STATES,
    URGE_RANK,
    LiveNotice,
    LiveUrge,
)


DEFAULT_TTL_MS = {
    "yield_floor": 8_000,
    "acknowledge_user": 8_000,
    "remain_present": 30_000,
    "share_delight": 20_000,
    "ask_about_result": 45_000,
    "comfort": 20_000,
    "wait": 15_000,
}

_NOTICE_TO_URGE = {
    "user_typing": ("acknowledge_user", "user", "user_typing"),
    "user_speech": ("yield_floor", "user", "user_speech"),
    "user_meaning": ("acknowledge_user", "user", "user_meaning"),
    "silence": ("remain_present", "presence", "silence_wake"),
    "wait_expired": ("remain_present", "presence", "wait_expired"),
    "sleep": ("remain_present", "sleep", "sleep"),
    "shared_commitment": ("share_delight", "shared_activity", "shared_activity"),
    "user_focus": ("remain_present", "coding", "world_truth_coding"),
    "focus_started": ("remain_present", "coding", "focus_started"),
    "focus_boundary": ("remain_present", "presence", "focus_boundary"),
    "returned_to_machine": ("remain_present", "machine", "returned_to_machine"),
    "app_category_changed": ("remain_present", "presence", "app_category_changed"),
    "shared_activity_resumed": (
        "share_delight", "shared_activity", "shared_activity_resumed",
    ),
    "session": ("remain_present", "session", "session_changed"),
    "cue": ("ask_about_result", "", "cue_pool"),
    "nudge": ("remain_present", "proactive", "prepared_nudge"),
    "affect_changed": ("remain_present", "presence", "affect_changed"),
    "vitality_changed": ("remain_present", "presence", "vitality_changed"),
}


class LiveUrgeStore:
    """Bounded ephemeral candidates. Nothing here executes."""

    def __init__(self, *, max_active: int = 8) -> None:
        self._max_active = max(1, int(max_active))
        self._urges: dict[str, LiveUrge] = {}
        self._blocked: dict[str, str] = {}
        self._last_notices: tuple[LiveNotice, ...] = ()

    def active(self) -> tuple[LiveUrge, ...]:
        return tuple(
            urge for urge in self._urges.values() if urge.state in ACTIVE_STATES
        )

    def all_urges(self) -> tuple[LiveUrge, ...]:
        return tuple(self._urges.values())

    def last_notices(self) -> tuple[LiveNotice, ...]:
        return self._last_notices

    def expire_due(self, now_mono_ms: float) -> int:
        expired = 0
        for urge_id, urge in list(self._urges.items()):
            if urge.state not in ACTIVE_STATES:
                continue
            if urge.is_expired(now_mono_ms):
                self._urges[urge_id] = self._with_state(urge, "expired")
                self._blocked[urge.repetition_key] = urge.evidence_key()
                expired += 1
        return expired

    def withdraw_expressive(self, *, reason: str = "constraint") -> int:
        withdrawn = 0
        for urge_id, urge in list(self._urges.items()):
            if urge.state not in ACTIVE_STATES:
                continue
            if urge.kind in EXPRESSIVE_KINDS:
                self._urges[urge_id] = self._with_state(urge, "withdrawn")
                withdrawn += 1
        return withdrawn

    def ingest_notices(
        self,
        notices: Iterable[LiveNotice],
        *,
        now_mono_ms: float,
        suppress_expressive: bool = False,
    ) -> tuple[LiveUrge, ...]:
        self._last_notices = tuple(notices)
        created: list[LiveUrge] = []
        for notice in notices:
            if notice.kind == "sleep" or suppress_expressive:
                self.withdraw_expressive(reason=notice.reason_code)
            mapping = _NOTICE_TO_URGE.get(notice.kind)
            if mapping is None:
                continue
            kind, subject, repetition = mapping
            subject = notice.subject or subject
            if suppress_expressive and kind in EXPRESSIVE_KINDS:
                continue
            if kind in EXPRESSIVE_KINDS and notice.kind == "sleep":
                continue
            urge = self._propose(
                kind=kind,
                subject=subject,
                source=notice.source,
                source_ids=notice.source_ids or (notice.notice_id,),
                repetition_key=f"{repetition}:{subject}",
                now_mono_ms=now_mono_ms,
            )
            if urge is not None:
                created.append(urge)
        self._compete()
        return tuple(created)

    def propose(
        self,
        *,
        kind: str,
        subject: str,
        source: str,
        source_ids: tuple[str, ...],
        repetition_key: str,
        now_mono_ms: float,
        cue_id: int | None = None,
        ttl_ms: int | None = None,
    ) -> LiveUrge | None:
        return self._propose(
            kind=kind,
            subject=subject,
            source=source,
            source_ids=source_ids,
            repetition_key=repetition_key,
            now_mono_ms=now_mono_ms,
            cue_id=cue_id,
            ttl_ms=ttl_ms,
        )

    def park(self, urge_id: str) -> LiveUrge | None:
        urge = self._urges.get(urge_id)
        if urge is None or urge.state not in ACTIVE_STATES:
            return None
        parked = self._with_state(urge, "parked")
        self._urges[urge_id] = parked
        return parked

    def _propose(
        self,
        *,
        kind: str,
        subject: str,
        source: str,
        source_ids: tuple[str, ...],
        repetition_key: str,
        now_mono_ms: float,
        cue_id: int | None = None,
        ttl_ms: int | None = None,
    ) -> LiveUrge | None:
        evidence = "|".join(str(item) for item in source_ids)
        blocked = self._blocked.get(repetition_key)
        if blocked is not None and blocked == evidence:
            return None
        for existing in self._urges.values():
            if existing.repetition_key != repetition_key:
                continue
            if existing.state in ACTIVE_STATES:
                return self._merge(existing, now_mono_ms=now_mono_ms, cue_id=cue_id)
            if (
                existing.state in TERMINAL_STATES
                and existing.evidence_key() == evidence
            ):
                return None
        ttl = int(ttl_ms if ttl_ms is not None else DEFAULT_TTL_MS.get(kind, 15_000))
        urge = LiveUrge(
            urge_id=uuid.uuid4().hex[:16],
            kind=kind,
            subject=subject,
            created_at=timephrase.utcnow().isoformat(timespec="seconds"),
            created_monotonic_ms=float(now_mono_ms),
            expires_after_ms=ttl,
            source=source,
            source_ids=source_ids,
            state="candidate",
            repetition_key=repetition_key,
            cue_id=cue_id,
            salience_inputs={"transition_strength": 0.6},
        )
        self._urges[urge.urge_id] = urge
        self._trim()
        return urge

    def _merge(
        self,
        existing: LiveUrge,
        *,
        now_mono_ms: float,
        cue_id: int | None,
    ) -> LiveUrge:
        merged = LiveUrge(
            urge_id=existing.urge_id,
            kind=existing.kind,
            subject=existing.subject,
            created_at=existing.created_at,
            created_monotonic_ms=existing.created_monotonic_ms,
            expires_after_ms=existing.expires_after_ms,
            source=existing.source,
            source_ids=existing.source_ids,
            concept_ids=existing.concept_ids,
            salience_inputs=dict(existing.salience_inputs),
            state=existing.state,
            repetition_key=existing.repetition_key,
            cue_id=existing.cue_id if cue_id is None else cue_id,
        )
        self._urges[merged.urge_id] = merged
        return merged

    def _compete(self) -> None:
        by_subject: dict[str, list[LiveUrge]] = {}
        for urge in self.active():
            by_subject.setdefault(urge.subject, []).append(urge)
        for group in by_subject.values():
            if len(group) < 2:
                continue
            ranked = sorted(
                group,
                key=lambda item: URGE_RANK.get(item.kind, 0),
                reverse=True,
            )
            winner = ranked[0]
            for loser in ranked[1:]:
                if loser.kind == winner.kind:
                    continue
                parked = self._with_state(loser, "parked")
                self._urges[loser.urge_id] = parked

    def recompete(self, *, rank_bonus: dict[str, int] | None = None) -> None:
        """Re-run subject competition with optional kind rank bonuses."""
        bonus = rank_bonus or {}
        by_subject: dict[str, list[LiveUrge]] = {}
        for urge in self.active():
            by_subject.setdefault(urge.subject, []).append(urge)
        for group in by_subject.values():
            if len(group) < 2:
                continue
            ranked = sorted(
                group,
                key=lambda item: (
                    URGE_RANK.get(item.kind, 0) + int(bonus.get(item.kind, 0))
                ),
                reverse=True,
            )
            winner = ranked[0]
            for loser in ranked[1:]:
                if loser.kind == winner.kind:
                    continue
                self._urges[loser.urge_id] = self._with_state(loser, "parked")

    def _trim(self) -> None:
        active = list(self.active())
        extra = len(active) - self._max_active
        if extra <= 0:
            return
        ranked = sorted(
            active,
            key=lambda item: URGE_RANK.get(item.kind, 0),
        )
        for urge in ranked[:extra]:
            self._urges[urge.urge_id] = self._with_state(urge, "withdrawn")

    @staticmethod
    def _with_state(urge: LiveUrge, state: str) -> LiveUrge:
        return LiveUrge(
            urge_id=urge.urge_id,
            kind=urge.kind,
            subject=urge.subject,
            created_at=urge.created_at,
            created_monotonic_ms=urge.created_monotonic_ms,
            expires_after_ms=urge.expires_after_ms,
            source=urge.source,
            source_ids=urge.source_ids,
            concept_ids=urge.concept_ids,
            salience_inputs=dict(urge.salience_inputs),
            state=state,
            repetition_key=urge.repetition_key,
            cue_id=urge.cue_id,
        )
