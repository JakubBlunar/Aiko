"""C6 Level-3 companion intake — one pooled ``companion_activity`` cue.

Perception (Level-2 kv) and intake stay layered: this worker never calls
the interpreter. A confident, non-idle reading becomes one private T6
note Aiko may phrase herself. Idle / empty / ``nothing`` readings do not
publish. The same ``kind`` + ``app`` signature does not re-draft.

This is **not** a gap cue and is **not** in ``GAP_CUE_ORDER``. A queued
``cue_pool`` row is the whole arming signal (audit shape 13). No
``MemoryStore``. Live still peeks the pool; it never ``take_pool_cue``.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Mapping

from app.core.activity.interpretation_worker import (
    KINDS,
    load_activity_interpretation,
)
from app.core.proactive.cue_producer import CueProducer, StoreProvider
from app.core.proactive.idle_worker import WorkSignal


log = logging.getLogger("app.activity.companion_cue_worker")

_INTERVAL_SECONDS = 90.0
_KV_LAST_SIGNATURE = "companion_activity.last_signature"
_SKIP_KINDS = frozenset({"", "idle"})


def companion_activity_signature(kind: str, app: str) -> str:
    """Date-free identity of the current observation."""
    return "companion_activity:%s:%s" % (
        str(kind or "").strip().lower(),
        str(app or "").strip().lower(),
    )


def companion_activity_subject(kind: str, app: str) -> str:
    """Lexical match tokens — app name + kind, never a window title."""
    app_name = str(app or "").strip()
    kind_name = str(kind or "").strip().lower()
    if app_name and kind_name:
        return "%s %s" % (app_name, kind_name)
    return app_name or kind_name or "desktop activity"


def render_companion_activity_cue(
    *,
    user_name: str,
    reading: str,
    kind: str = "",
    app: str = "",
) -> str:
    """Private T6 steer. Never a screen narration, never a title."""
    del kind, app
    name = str(user_name or "").strip() or "them"
    observation = " ".join(str(reading or "").split())
    if not observation:
        return ""
    return (
        "Something you've quietly clocked about %s: %s. "
        "If a warm, natural moment opens, you can notice it ONCE in passing "
        "-- a small awareness that he's there doing that, never a narration "
        "of his screen, never a status report, never a title or filename. "
        "Phrase it yourself. If it doesn't fit, drop it completely."
    ) % (name, observation)


def decide_companion_activity(
    interp: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """None when there is nothing worth noticing."""
    if not interp:
        return None
    if interp.get("nothing"):
        return None
    reading = " ".join(str(interp.get("reading") or "").split())
    if not reading:
        return None
    kind = str(interp.get("kind") or "").strip().lower()
    if kind not in KINDS or kind in _SKIP_KINDS:
        return None
    try:
        confidence = float(interp.get("confidence") or 0.0)
    except (TypeError, ValueError):
        return None
    if confidence < 0.6:
        return None
    app = str(interp.get("app") or "").strip()
    return {
        "reading": reading,
        "kind": kind,
        "app": app,
        "signature": companion_activity_signature(kind, app),
        "subject": companion_activity_subject(kind, app),
    }


class CompanionActivityWorker:
    """IdleWorker that drafts one current-state companion cue from Level-2."""

    name = "companion_activity"

    def __init__(
        self,
        *,
        kv_get: Callable[[str], str | None] | None = None,
        kv_set: Callable[[str, str], None] | None = None,
        enabled_provider: Callable[[], bool] | None = None,
        cue_store_provider: StoreProvider | None = None,
        user_name_provider: Callable[[], str] | None = None,
        interval_seconds: float = _INTERVAL_SECONDS,
    ) -> None:
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._enabled_provider = enabled_provider
        self._cues = CueProducer("companion_activity", cue_store_provider)
        self._user_name_provider = user_name_provider
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._force_next = False

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        del now, last_run_at
        return self._enabled()

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal | None:
        """Pressure from a new reading, not from an empty shelf.

        A current-state observation has to replace a stale pending row
        when the signature changes, so inventory fullness is not a veto.
        The same signature does not re-draft (unless ``force_next``).
        """
        del now, last_run_at
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        if self._cues.store() is None:
            return WorkSignal(pressure=0.0, reason="no cue store")
        decided = decide_companion_activity(
            load_activity_interpretation(self._kv_get),
        )
        if decided is None:
            return WorkSignal(pressure=0.0, reason="no companion reading")
        if not self._force_next:
            last = self._kv_get_safe(_KV_LAST_SIGNATURE)
            if last and last == decided["signature"]:
                return WorkSignal(pressure=0.0, reason="same signature")
        return WorkSignal(
            pressure=1.0,
            reason=decided["signature"],
            needs_llm=False,
        )

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"drafted": 0, "disabled": True}

        forced = self._force_next
        self._force_next = False

        decided = decide_companion_activity(
            load_activity_interpretation(self._kv_get),
        )
        if decided is None:
            return {"drafted": 0, "no_reading": True}

        if not forced:
            last = self._kv_get_safe(_KV_LAST_SIGNATURE)
            if last and last == decided["signature"]:
                return {"drafted": 0, "same_signature": decided["signature"]}

        text = render_companion_activity_cue(
            user_name=self._user_name(),
            reading=decided["reading"],
            kind=decided["kind"],
            app=decided["app"],
        )
        subject = decided["subject"]
        if not text or not subject:
            return {"drafted": 0, "unrenderable": True}

        self._retire_live()
        cue_id = self._cues.publish(
            subject,
            text,
            payload={
                "kind": decided["kind"],
                "app": decided["app"],
                "signature": decided["signature"],
            },
        )
        if not cue_id:
            return {"drafted": 0, "publish_failed": True}

        self._kv_set_safe(_KV_LAST_SIGNATURE, decided["signature"])
        log.info(
            "companion-activity drafted: kind=%s app=%s cue=%s",
            decided["kind"],
            decided["app"] or "-",
            cue_id,
        )
        return {
            "drafted": 1,
            "kind": decided["kind"],
            "app": decided["app"],
            "signature": decided["signature"],
            "cue_id": cue_id,
        }

    def force_next(self) -> None:
        """Arm a one-shot bypass of the signature gate."""
        self._force_next = True

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def _user_name(self) -> str:
        if self._user_name_provider is None:
            return "them"
        try:
            return str(self._user_name_provider() or "them")
        except Exception:
            return "them"

    def _kv_get_safe(self, key: str) -> str | None:
        if self._kv_get is None:
            return None
        try:
            value = self._kv_get(key)
        except Exception:
            return None
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _kv_set_safe(self, key: str, value: str) -> None:
        if self._kv_set is None:
            return
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug("companion-activity kv_set failed", exc_info=True)

    def _retire_live(self) -> None:
        """Current-state: a newer reading replaces any pending row."""
        store = self._cues.store()
        if store is None:
            return
        try:
            for row in store.pending(self._cues.cue_type, limit=20):
                store.supersede(row.id, evidence="newer activity reading")
        except Exception:
            log.debug("companion-activity retire failed", exc_info=True)
