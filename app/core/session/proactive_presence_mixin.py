"""Proactive + presence mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
proactive-message surface (startup greeting, proactive generation, live
voice-session flag), the typed-silence timer machinery, and the
user-presence / active-app signals. State ownership stays on
``SessionController.__init__``.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for any moved method must patch
``app.core.session.proactive_presence_mixin.<symbol>`` instead."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any


log = logging.getLogger("app.session")


class ProactivePresenceMixin:
    """Proactive messages + typed-silence timer + presence/activity."""

    @property
    def reminder_store(self):
        return self._reminder_dispatcher.store

    def _on_reminder_delivered(self, message) -> None:
        self._notify_message("Assistant (proactive)", message.content, message.id)
        if self._live_voice_session_active and not self._turn_in_progress:
            self.speak_text(message.content)

    def build_startup_greeting(self) -> str:
        return "Welcome back. Audio is ready."

    def generate_proactive_message(self) -> str | None:
        # Silence wakes BrainLoop. The director only speaks after the
        # free-to-speak gate, and never while Live posture is on.
        if self._sleeping_now():
            return None
        self._enqueue_silence_proactive("voice_silence")
        return None

    def _sleeping_now(self) -> bool:
        store = getattr(self, "_sleep_store", None)
        if store is None:
            return False
        try:
            return store.get_state().status == "asleep"
        except Exception:
            return False

    def set_live_voice_session_active(self, active: bool) -> None:
        was_active = self._live_voice_session_active
        self._live_voice_session_active = bool(active)
        self._state.session_type = "live" if active else "chat"
        # Voice mode dominates: drop any pending typed timer so a
        # stale typed nudge can't fire while the user is on the mic.
        # When voice mode ends we don't auto-arm — typing is required
        # to get back into "we just had a typed turn" state.
        if active and not was_active:
            self._disarm_typed_silence_timer()
        if bool(active) != bool(was_active):
            bump = getattr(self, "bump_live_mode_generation", None)
            if callable(bump):
                bump("voice_session")

    def _is_typed_proactive_eligible(self) -> bool:
        """Predicate handed to :class:`ProactiveDirector`.

        Folds *all* gating concerns into one boolean so the director
        never has to know about settings, live mode, or presence.
        Voice mode dominance lives here: when the user is on the mic
        the typed path is forcefully disabled regardless of presence
        signals (which are typed-mode only — see ``set_user_present``).

        The presence gate is conditional on
        ``agent.proactive_typed_when_away``: with it ``False`` (the
        default) hidden / blurred windows silence the timer; with it
        ``True`` the timer fires regardless. The flag exists so users
        who want Aiko to chime in even when they've alt-tabbed away
        can opt in without having to disable the proactive subsystem
        entirely.
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "proactive_typed_enabled", True)):
            return False
        if self._sleeping_now():
            return False
        if self._live_voice_session_active:
            return False
        if self._turn_in_progress:
            return False
        if not self._user_present and not bool(
            getattr(agent, "proactive_typed_when_away", False)
        ):
            return False
        # K14: skip the typed nudge when the last turn read as
        # ``"abandoned"`` (steep latency *and* curt message). The
        # absence-curiosity inner-life cue on the *next* user turn
        # handles this case more gracefully than a proactive ping
        # would; firing here would compound the "Aiko is talking past
        # me" signal. Cleared by the next non-abandoned scoring.
        if bool(getattr(agent, "engagement_proactive_gate", True)):
            if getattr(self, "_last_engagement_label", "neutral") == "abandoned":
                return False
        return True

    def _vitality_scale_silence(self, budget: float) -> float:
        """K68: scale a proactive silence window by current body-energy.

        Returns ``budget * (1 + factor * (1 - 2*energy))`` so a tired Aiko
        (low energy) waits longer before initiating and a lit-up one
        initiates sooner. ``vitality_proactive_factor=0`` or the feature
        disabled -> the budget is returned unchanged. Best-effort.
        """
        try:
            agent = self._settings.agent
            if not bool(getattr(agent, "vitality_enabled", True)):
                return budget
            factor = float(
                getattr(self._memory_settings, "vitality_proactive_factor", 0.4)
            )
            if factor <= 0.0:
                return budget
            snap = self.vitality_snapshot()
            energy = snap.get("energy") if isinstance(snap, dict) else None
            if energy is None:
                return budget
            e = max(0.0, min(1.0, float(energy)))
            mult = 1.0 + factor * (1.0 - 2.0 * e)
            return max(1.0, budget * mult)
        except Exception:
            log.debug("vitality silence scale raised", exc_info=True)
            return budget

    def _arm_typed_silence_timer(self) -> None:
        """Schedule a one-shot fire after ``proactive_silence_seconds_typed``.

        Cancels any in-flight timer so we don't race two of them past
        the cooldown gate inside ``ProactiveDirector``. Stores both the
        wall-clock (monotonic) arm time and the budget so a presence
        flip can re-arm with the remaining budget instead of starting
        a fresh full window.
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "proactive_typed_enabled", True)):
            return
        if self._sleeping_now():
            return
        budget = float(getattr(agent, "proactive_silence_seconds_typed", 240.0))
        if budget <= 0.0:
            return
        # K68: a tired Aiko initiates less (stretch the silence window),
        # a lit-up Aiko initiates sooner (shrink it). Energy 0 -> longer,
        # energy 1 -> shorter, energy 0.5 -> unchanged.
        budget = self._vitality_scale_silence(budget)
        if budget <= 0.0:
            return
        with self._typed_silence_lock:
            if self._typed_silence_timer is not None:
                try:
                    self._typed_silence_timer.cancel()
                except Exception:
                    log.debug("typed timer cancel raised", exc_info=True)
            timer = threading.Timer(budget, self._on_typed_silence_fire)
            timer.name = "typed-silence-timer"
            timer.daemon = True
            self._typed_silence_timer = timer
            self._typed_silence_armed_at = time.monotonic()
            self._typed_silence_armed_budget = budget
            timer.start()

    def _disarm_typed_silence_timer(self) -> None:
        """Cancel + clear the current typed-silence timer (no fire)."""
        with self._typed_silence_lock:
            if self._typed_silence_timer is not None:
                try:
                    self._typed_silence_timer.cancel()
                except Exception:
                    log.debug("typed timer cancel raised", exc_info=True)
            self._typed_silence_timer = None
            self._typed_silence_armed_at = None
            self._typed_silence_armed_budget = None

    def _on_typed_silence_fire(self) -> None:
        """Timer body: hand off to the director if we're still eligible.

        Re-checked under ``_is_typed_proactive_eligible`` rather than
        trusting the moment we armed. The director enforces its own
        cooldown and inflight guards, so this is purely "should we
        even ask?".
        """
        with self._typed_silence_lock:
            self._typed_silence_timer = None
            self._typed_silence_armed_at = None
            self._typed_silence_armed_budget = None
        try:
            if self._sleeping_now():
                return
            self._enqueue_silence_proactive("typed_silence")
        except Exception:
            log.debug("notify_typed_silence raised", exc_info=True)

    def set_user_present(self, present: bool) -> None:
        """Public: client-side presence change (tab visibility / window focus).

        Three-state semantics:
        - True after False: re-arm with the remaining silence budget
          if a typed turn is still "owed" a fire (i.e. we had armed a
          timer that got cancelled by the False flip).
        - False after True: cancel the pending timer; if it had been
          running a while, remember the elapsed so the next True flip
          re-arms with what's left.
        - Same value as before: no-op (idempotent — a debounced UI
          may legitimately resend the same value).

        Voice mode does NOT call this path. The voice-mode
        ``LiveSession._maybe_proactive`` continues to fire on its own
        45 s threshold; users wearing the mic may legitimately be
        away from the screen but still present in conversation.
        """
        new_value = bool(present)
        with self._typed_silence_lock:
            if self._user_present == new_value:
                return
            self._user_present = new_value
            armed_at = self._typed_silence_armed_at
            armed_budget = self._typed_silence_armed_budget
            timer = self._typed_silence_timer
        if not new_value:
            if timer is not None:
                # Snapshot how much budget had elapsed so the next
                # True flip re-arms with the remainder rather than
                # giving the user a fresh 4-min grace every alt-tab.
                if armed_at is not None and armed_budget is not None:
                    elapsed = time.monotonic() - armed_at
                    remaining = max(0.0, armed_budget - elapsed)
                else:
                    remaining = 0.0
                with self._typed_silence_lock:
                    if self._typed_silence_timer is not None:
                        try:
                            self._typed_silence_timer.cancel()
                        except Exception:
                            log.debug("typed timer cancel raised", exc_info=True)
                    self._typed_silence_timer = None
                    self._typed_silence_armed_at = None
                    # Stash the remaining budget under the same field
                    # so a subsequent True flip can re-arm with it.
                    self._typed_silence_armed_budget = remaining
            return
        # Flipped to present. If a timer is already running, leave it
        # alone (it was armed before we ever went away). If we have a
        # leftover ``_typed_silence_armed_budget`` from the away leg,
        # re-arm with that budget so the user gets the same total
        # quiet window they would have had if they hadn't alt-tabbed.
        with self._typed_silence_lock:
            if self._typed_silence_timer is not None:
                return
            remaining = self._typed_silence_armed_budget
            self._typed_silence_armed_budget = None
        if remaining is None or remaining <= 0.0:
            return
        agent = self._settings.agent
        if not bool(getattr(agent, "proactive_typed_enabled", True)):
            return
        with self._typed_silence_lock:
            timer = threading.Timer(
                float(remaining), self._on_typed_silence_fire,
            )
            timer.name = "typed-silence-timer"
            timer.daemon = True
            self._typed_silence_timer = timer
            self._typed_silence_armed_at = time.monotonic()
            self._typed_silence_armed_budget = float(remaining)
            timer.start()

    def set_connected_clients(self, count: int) -> None:
        """Public: record how many UI websocket clients are attached.

        Called by the web layer on every connect / disconnect. The
        diary worker (H9) reads :meth:`is_user_away` (derived from this
        count) so it only writes "while you were away" entries when no
        window is open — when a client is connected, Aiko uses the live
        ``[[diary:...]]`` tag instead. Coerced to a non-negative int.
        """
        previous = int(getattr(self, "_connected_clients", 0) or 0)
        try:
            self._connected_clients = max(0, int(count))
        except (TypeError, ValueError):
            self._connected_clients = 0
        if previous <= 0 and int(self._connected_clients) >= 1:
            bump = getattr(self, "bump_live_mode_generation", None)
            if callable(bump):
                bump("reconnect")

    def is_user_away(self) -> bool:
        """``True`` when no UI websocket client is currently connected.

        Stronger than ``not self._user_present`` (tab visibility): a
        backgrounded PWA stays connected-but-hidden, which is *not*
        away. The diary worker gates on this so it never double-writes
        with the live tag path while a window is open.
        """
        return int(getattr(self, "_connected_clients", 0)) <= 0

    def set_user_active_app(self, app: str | None) -> None:
        """Public: update the foreground app the user is in.

        Server-side privacy gate: when ``activity_awareness_enabled``
        is ``False`` the value is silently dropped. This means a
        buggy or rogue client emitting ``user_activity`` events while
        the user has disabled the feature in settings cannot leak
        which apps the user is in.

        Empty string / blank coerces to ``None`` (no block in
        prompt) so a client that wants to clear the cached value
        without disabling the feature can send ``""``.
        """
        if not bool(getattr(self._settings.agent, "activity_awareness_enabled", False)):
            self._user_active_app = None
            return
        if app is None:
            self._user_active_app = None
            return
        cleaned = str(app).strip()
        self._user_active_app = cleaned or None

    def ingest_activity_envelope(self, envelope: dict[str, Any] | None) -> None:
        """Store a collector envelope. Sibling of ``set_user_active_app``.

        Does **not** call ``_touch_user_activity`` — coding-not-chatting
        must look idle to the scheduler. Toggle off drops the envelope
        and clears the live app-name cache. Titles never land in
        ``_user_active_app`` (prompt stays app-name only). Idle and lock
        envelopes clear the live app cache and publish a coalesced Live
        impulse; they do not early-return before that publish.
        """
        if not bool(getattr(self._settings.agent, "activity_awareness_enabled", False)):
            self._user_active_app = None
            return
        from app.core.activity.ingest import ingest_envelope

        store = getattr(self, "_activity_store", None)
        try:
            redacted = ingest_envelope(
                envelope, settings=self._settings, store=store,
            )
        except Exception:
            log.debug("ingest_activity_envelope failed", exc_info=True)
            return
        if redacted is None:
            return
        self._complete_activity_pull(redacted)
        if redacted.source == "foreground":
            self.set_user_active_app(redacted.subject.app)
            kind = "activity.session_changed"
        elif redacted.source in {"idle", "lock"}:
            self.set_user_active_app(None)
            kind = f"activity.{redacted.source}"
        else:
            return
        publish = getattr(self, "publish_live_impulse", None)
        if not callable(publish):
            return
        try:
            publish(
                kind=kind,
                source="activity.ingest",
                coalesce_key=kind,
                privacy="local_state",
                priority="attention",
                ttl_ms=30_000,
                payload={
                    "source": redacted.source,
                    "app": str(redacted.subject.app or ""),
                },
            )
        except Exception:
            log.debug("activity live impulse failed", exc_info=True)

    def activity_timeline_snapshot(self, *, limit: int = 20) -> dict[str, Any]:
        """Debug dump of the C6 collection store (MCP + tests)."""
        from app.core.activity.handlers import KNOWN_SOURCES

        agent = getattr(self._settings, "agent", None)
        memory = getattr(self._settings, "memory", None)
        store = getattr(self, "_activity_store", None)
        last = store.last_event() if store is not None else None
        sessions = (
            store.recent_sessions(limit=limit) if store is not None else []
        )
        counts = store.counts() if store is not None else {}
        try:
            keep_days = max(0, int(getattr(memory, "activity_keep_days", 30)))
        except (TypeError, ValueError):
            keep_days = 30
        interpretation = None
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is not None:
            try:
                from app.core.activity.interpretation_worker import (
                    load_activity_interpretation,
                )

                interpretation = load_activity_interpretation(chat_db.kv_get)
            except Exception:
                interpretation = None
        return {
            "enabled": bool(
                getattr(agent, "activity_awareness_enabled", False),
            ),
            "title_allowlist": list(
                getattr(agent, "activity_title_allowlist", None) or [],
            ),
            "keep_days": keep_days,
            "registered_sources": list(KNOWN_SOURCES),
            "last_event": last,
            "recent_sessions": sessions,
            "event_count": counts.get("events", 0),
            "session_count": counts.get("sessions", 0),
            "oldest_event_at": counts.get("oldest_event_at"),
            "interpretation": interpretation,
        }

    def add_activity_request_listener(
        self, callback: Callable[[str], None],
    ) -> None:
        listeners = getattr(self, "_activity_request_listeners", None)
        if listeners is None:
            listeners = []
            self._activity_request_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def pull_activity_for_tool(self) -> str:
        """C7 ``get_activity`` body. Never raises."""
        try:
            view = self.pull_activity()
        except Exception:
            log.debug("pull_activity failed", exc_info=True)
            view = {
                "fresh": False,
                "enabled": False,
                "app": None,
                "title": None,
                "source": None,
                "as_of_seconds": None,
                "note": "no recent desktop activity",
            }
        return json.dumps(view, ensure_ascii=False)

    def pull_activity(self, *, timeout_s: float | None = None) -> dict[str, Any]:
        """Bounded live pull; last stored session on timeout."""
        from app.core.activity.pull import (
            PULL_TIMEOUT_SECONDS,
            format_activity_view,
            pick_last_session,
        )

        enabled = bool(
            getattr(self._settings.agent, "activity_awareness_enabled", False)
        )
        store = getattr(self, "_activity_store", None)
        sessions = []
        if store is not None:
            try:
                sessions = store.recent_sessions(limit=8)
            except Exception:
                log.debug("activity pull sessions failed", exc_info=True)
                sessions = []
        fallback = pick_last_session(sessions)
        if not enabled:
            return format_activity_view(
                session_row=fallback, fresh=False, enabled=False,
            )
        wait_s = (
            PULL_TIMEOUT_SECONDS if timeout_s is None else max(0.0, float(timeout_s))
        )
        live = self._request_activity_snapshot(timeout_s=wait_s)
        if live is not None:
            return format_activity_view(
                session_row=fallback, live=live, fresh=True, enabled=True,
            )
        return format_activity_view(
            session_row=fallback, fresh=False, enabled=True,
        )

    def _request_activity_snapshot(
        self, *, timeout_s: float,
    ) -> Any:
        from app.core.activity.envelope import ActivityEnvelope

        request_id = uuid.uuid4().hex[:12]
        event = threading.Event()
        bucket: list[ActivityEnvelope] = []
        lock = self._activity_pull_lock()
        waiters = self._activity_pull_waiters()
        with lock:
            waiters[request_id] = (event, bucket)
        self._notify_activity_request(request_id)
        listeners = getattr(self, "_activity_request_listeners", None) or []
        # No WS listener (tests, browser-only) → do not block the turn.
        if timeout_s > 0 and listeners:
            event.wait(timeout_s)
        with lock:
            waiters.pop(request_id, None)
        for env in bucket:
            if env.source == "foreground":
                return env
        return bucket[0] if bucket else None

    def _notify_activity_request(self, request_id: str) -> None:
        listeners = getattr(self, "_activity_request_listeners", None) or []
        for listener in list(listeners):
            try:
                listener(request_id)
            except Exception:
                log.debug("activity request listener raised", exc_info=True)

    def _complete_activity_pull(self, envelope: Any) -> None:
        request_id = str(getattr(envelope, "request_id", None) or "").strip()
        if not request_id:
            extras = getattr(envelope, "extras", None) or {}
            if isinstance(extras, dict):
                request_id = str(extras.get("request_id") or "").strip()
        if not request_id:
            return
        lock = self._activity_pull_lock()
        waiters = self._activity_pull_waiters()
        with lock:
            waiter = waiters.get(request_id)
            if waiter is None:
                return
            event, bucket = waiter
            bucket.append(envelope)
            if getattr(envelope, "source", "") == "foreground":
                event.set()

    def _activity_pull_waiters(self) -> dict[str, Any]:
        waits = getattr(self, "_activity_pulls", None)
        if waits is None:
            self._activity_pulls = {}
            waits = self._activity_pulls
        return waits

    def _activity_pull_lock(self) -> threading.Lock:
        lock = getattr(self, "_activity_pull_mutex", None)
        if lock is None:
            lock = threading.Lock()
            self._activity_pull_mutex = lock
        return lock

