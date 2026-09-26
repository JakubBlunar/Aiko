"""In-memory Live urge store: merge, compete, park, expire, withdraw."""
from __future__ import annotations

import uuid
from dataclasses import replace
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
    "share_observation": 45_000,
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
            if (
                urge.state == "deferred"
                and now_mono_ms - urge.created_monotonic_ms > 15 * 60_000
            ):
                self._urges[urge_id] = self._with_state(urge, "expired")
                expired += 1
                continue
            if urge.state not in ACTIVE_STATES:
                continue
            if urge.is_expired(now_mono_ms):
                deferred = (
                    urge.source == "cue_pool" and urge.cue_id is not None
                    and not urge.opportunity_seen and not urge.reconsidered
                )
                self._urges[urge_id] = self._with_state(
                    urge, "deferred" if deferred else "expired",
                )
                self._blocked[urge.repetition_key] = urge.evidence_key()
                expired += 1
            self._trim()
        return expired

    def note_opportunity(self, urge_id: str) -> None:
        urge = self._urges.get(urge_id)
        if urge is not None and urge.state in ACTIVE_STATES:
            self._urges[urge_id] = replace(urge, opportunity_seen=True)

    def consume(self, urge_id: str) -> None:
        urge = self._urges.get(urge_id)
        if urge is not None:
            self._urges[urge_id] = self._with_state(urge, "consumed")
            self._blocked[urge.repetition_key] = urge.evidence_key()

    def reconsider(self, cue_id: int, *, now_mono_ms: float) -> None:
        for urge_id, urge in list(self._urges.items()):
            if urge.cue_id != cue_id or urge.state != "deferred":
                continue
            if now_mono_ms - urge.created_monotonic_ms > 15 * 60_000:
                self._urges[urge_id] = self._with_state(urge, "expired")
                continue
            self._urges[urge_id] = replace(
                urge, state="candidate", reconsidered=True,
                created_monotonic_ms=now_mono_ms,
            )
            self._blocked.pop(urge.repetition_key, None)
            self._trim()

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
        purpose: str = "",
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
            purpose=purpose,
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
        purpose: str = "",
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
            purpose=purpose,
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
        merged = replace(
            existing,
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
        deferred = sorted(
            (urge for urge in self._urges.values() if urge.state == "deferred"),
            key=lambda item: item.created_monotonic_ms,
        )
        for urge in deferred[:-self._max_active]:
            self._urges[urge.urge_id] = self._with_state(urge, "withdrawn")
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
        return replace(urge, state=state)
