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

import logging
import threading
import time


log = logging.getLogger("app.session")


class ProactivePresenceMixin:
    """Proactive messages + typed-silence timer + presence/activity."""

    def build_startup_greeting(self) -> str:
        return "Welcome back. Audio is ready."

    def generate_proactive_message(self) -> str | None:
        # The new ProactiveDirector speaks directly via TTS. Returning ``None``
        # tells LiveWorker not to also queue something itself.
        self._proactive.notify_silence(self.session_key)
        return None

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
            self._proactive.notify_typed_silence(self.session_key)
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
        try:
            self._connected_clients = max(0, int(count))
        except (TypeError, ValueError):
            self._connected_clients = 0

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
