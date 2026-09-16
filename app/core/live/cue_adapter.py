"""Project cue-pool and prepared-nudge candidates into LiveUrge records.

Peek only. Never ``take_pool_cue``, never consume a nudge, never speak.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from app.core.infra import timephrase
from app.core.live.labels import cap_live_subject
from app.core.live.urge import LiveUrge
from app.core.live.urge_store import LiveUrgeStore
from app.core.proactive.cue_accounting import policy_for


class CueUrgeAdapter:
    """Live's only projection of the cue pool / prepared-nudge shelf."""

    def __init__(
        self,
        *,
        pending_provider: Callable[[], Iterable[Any]] | None = None,
        nudge_provider: Callable[[], Any | None] | None = None,
    ) -> None:
        self._pending_provider = pending_provider
        self._nudge_provider = nudge_provider

    def project(
        self,
        store: LiveUrgeStore,
        *,
        now_mono_ms: float,
        suppress_expressive: bool = False,
    ) -> tuple[LiveUrge, ...]:
        if suppress_expressive:
            return ()
        created: list[LiveUrge] = []
        for row in self._pending_rows():
            if not self._eligible(row):
                continue
            cue_id = int(getattr(row, "id", 0) or 0)
            cue_type = str(getattr(row, "cue_type", "") or "cue")
            subject = (
                cap_live_subject(getattr(row, "subject", "") or "")
                or cap_live_subject(cue_type)
                or "cue"
            )
            urge = store.propose(
                kind="ask_about_result",
                subject=subject,
                source="cue_pool",
                source_ids=(f"cue:{cue_id}",),
                repetition_key=f"cue:{cue_id}",
                now_mono_ms=now_mono_ms,
                cue_id=cue_id,
            )
            if urge is not None:
                created.append(urge)
        nudge = self._nudge()
        if nudge is not None:
            source_id = str(getattr(nudge, "source_id", "") or "nudge")
            kind_label = str(getattr(nudge, "source_kind", "") or "proactive")
            subject = cap_live_subject(kind_label) or "proactive"
            urge = store.propose(
                kind="remain_present",
                subject=subject,
                source="proactive.nudge",
                source_ids=(f"nudge:{source_id}",),
                repetition_key=f"nudge:{source_id}",
                now_mono_ms=now_mono_ms,
            )
            if urge is not None:
                created.append(urge)
        return tuple(created)

    def _pending_rows(self) -> list[Any]:
        provider = self._pending_provider
        if provider is None:
            return []
        try:
            rows = list(provider() or ())
        except Exception:
            return []
        return rows[:8]

    def _nudge(self) -> Any | None:
        provider = self._nudge_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:
            return None

    @staticmethod
    def _eligible(row: Any) -> bool:
        cue_type = str(getattr(row, "cue_type", "") or "")
        policy = policy_for(cue_type)
        last = getattr(row, "last_surfaced_at", None)
        if policy is None or not last:
            return True
        cooldown_h = float(getattr(policy, "surface_cooldown_hours", 0.0) or 0.0)
        if cooldown_h <= 0:
            return True
        try:
            then = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return True
        age_h = (timephrase.utcnow() - then).total_seconds() / 3600.0
        return age_h >= cooldown_h
