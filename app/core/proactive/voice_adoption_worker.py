"""K26 — voice-adoption worker (silent producer).

Once a day, during a quiet window, this worker looks at the catchphrase
registry, asks which of those phrases started as *his*, and — rarely —
lets Aiko take one on as her own. The pure promotion rule lives in
:mod:`app.core.relationship.voice_adoption`; this worker is just the
plumbing: read the registry, resolve provenance, retire what's gone,
persist. It never speaks. The only visible effect is the small prompt
block the provider renders, weeks later.

**Provenance** comes from ``metadata.origin``, stamped by the miner at
write time (K26) and by the K80 fast path. Rows written before that
existed carry no origin, so an optional ``origin_resolver`` can look the
phrase up in the message history (who said it first). Without a resolver
those legacy rows are simply skipped — never adopted on a guess, since
Aiko adopting a phrase that was hers all along is the one failure mode
that reads as broken.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.infra import timephrase
from app.core.proactive.idle_worker import default_is_ready
from app.core.relationship import voice_adoption as _va


if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.memory.memory_store import MemoryStore


log = logging.getLogger("app.voice_adoption_worker")


def _utcnow() -> datetime:
    return timephrase.utcnow()


class VoiceAdoptionWorker:
    """IdleWorker that slowly folds the user's phrases into Aiko's voice."""

    name = "voice_adoption"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        memory_store: "MemoryStore | None",
        enabled_provider: Callable[[], bool] | None = None,
        origin_resolver: Callable[[str], str | None] | None = None,
        interval_seconds: float = 86400.0,
        min_age_days: float = _va.DEFAULT_MIN_AGE_DAYS,
        min_days_between: float = _va.DEFAULT_MIN_DAYS_BETWEEN,
        max_adopted: int = _va.DEFAULT_MAX_ADOPTED,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._memory = memory_store
        self._enabled_provider = enabled_provider
        self._origin_resolver = origin_resolver
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._min_age_days = max(0.0, float(min_age_days))
        self._min_days_between = max(0.0, float(min_days_between))
        self._max_adopted = max(1, int(max_adopted))
        self._clock = clock or _utcnow
        self._force_next = False

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        if not self._enabled():
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"adopted": None, "disabled": True}
        if self._memory is None:
            return {"adopted": None, "no_store": True}

        now = self._clock()
        # A forced run drops the two *time* gates (age + spacing) so a
        # month-long mechanic stays testable; it never drops the
        # provenance gate or the ceiling.
        forced = self._force_next
        self._force_next = False

        rows = self._catchphrase_rows()
        live = [str(r.get("phrase", "")) for r in rows]
        state = _va.load_state(self._chat_db.kv_get)
        state, retired = _va.retire(state, live)

        candidates = [
            _va.AdoptionCandidate(
                phrase=str(r["phrase"]),
                first_seen=r["first_seen"],
                salience=float(r.get("salience", 0.5) or 0.5),
            )
            for r in rows
            if r.get("origin") == "user"
        ]
        eligible = _va.eligible_candidates(
            candidates,
            adopted=state,
            now=now,
            min_age_days=0.0 if forced else self._min_age_days,
        )
        state, new_phrase = _va.promote(
            state,
            eligible,
            now=now,
            max_adopted=self._max_adopted,
            min_days_between=0.0 if forced else self._min_days_between,
        )
        if new_phrase or retired:
            _va.save_state(self._chat_db.kv_set, state)
        if new_phrase:
            log.info(
                "voice adoption: picked up %r (his phrase; %d adopted now)",
                new_phrase, len(state),
            )
        if retired:
            log.info("voice adoption: dropped %s", retired)
        return {
            "adopted": new_phrase,
            "retired": retired,
            "active": len(state),
            "candidates": len(candidates),
            "eligible": len(eligible),
        }

    # ── helpers ──────────────────────────────────────────────────────

    def _catchphrase_rows(self) -> list[dict[str, Any]]:
        """Registry rows with provenance + age resolved."""
        store = self._memory
        if store is None:
            return []
        try:
            mems = store.list_recent(limit=128, kind="catchphrase")
        except Exception:
            log.debug("catchphrase read failed", exc_info=True)
            return []
        out: list[dict[str, Any]] = []
        for mem in mems:
            phrase = (getattr(mem, "content", "") or "").strip()
            if not phrase:
                continue
            first_seen = self._created_at(mem)
            if first_seen is None:
                continue
            out.append(
                {
                    "phrase": phrase,
                    "first_seen": first_seen,
                    "salience": getattr(mem, "salience", 0.5),
                    "origin": self._origin_of(mem, phrase),
                }
            )
        return out

    def _origin_of(self, mem: Any, phrase: str) -> str | None:
        meta = getattr(mem, "metadata", None) or {}
        origin = meta.get("origin") if isinstance(meta, dict) else None
        if origin in ("user", "assistant"):
            return str(origin)
        if self._origin_resolver is None:
            return None
        try:
            resolved = self._origin_resolver(phrase)
        except Exception:
            log.debug("origin resolve failed", exc_info=True)
            return None
        return resolved if resolved in ("user", "assistant") else None

    @staticmethod
    def _created_at(mem: Any) -> datetime | None:
        raw = getattr(mem, "created_at", None)
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        if not raw:
            return None
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def force_next(self) -> None:
        """Arm a one-shot bypass of the age + spacing gates."""
        self._force_next = True
