"""The public surface ``app/web/`` is allowed to use on :class:`SessionController`.

The REST and WebSocket layers grew by reaching through the controller into its
private attributes -- 67 such reaches when this mixin was written, and nothing
noticed, because the web tests build sessions out of ``MagicMock``, which
answers any attribute name at all. Renaming a controller attribute was a
runtime surprise waiting for whichever route happened to run first.

This mixin is the answer to "what may a route call?". Two rules follow from it:

* A route talks to the controller through names declared here. New needs get a
  method here, not a new reach; ``tests/test_private_reach_guard.py`` keeps
  ``app/web/`` at zero private reaches so that stays true.
* Anything that has to be *synced* after a settings change belongs here rather
  than at the call site. The settings PATCH route used to know that toggling
  earcons means also poking ``_earcons``, that a proactive cooldown means
  ``_proactive.update_runtime``, and so on -- four subsystems it had no
  business holding handles to. Those live in the ``set_*`` methods below, so
  the knowledge sits next to the state instead of in a route body.

Deliberately *not* here: ``settings`` hands back the live ``AppSettings``, which
callers still mutate in place. That is the existing contract of the PATCH route
and untangling it is a separate job; what this buys today is that the attribute
can be renamed, and that every route reaching for configuration is visible as
one declared property instead of 33 scattered accesses.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.infra.settings import persist_user_overrides

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.core.infra.settings import AppSettings
    from app.core.tasks.task_events import TaskEventStore
    from app.core.tasks.task_inputs import TaskInputStore
    from app.core.tasks.task_orchestrator import TaskOrchestrator
    from app.core.tasks.task_store import TaskStore


log = logging.getLogger("app.session.web_facade")


class WorkerUnavailable(RuntimeError):
    """An on-demand worker was asked to run while it is not wired up.

    Distinct from a worker that ran and failed: routes answer 503 for this and
    500 for anything else, and collapsing the two would report a disabled
    feature as a server fault.
    """


@dataclass(frozen=True, slots=True)
class TaskHandles:
    """The task stores the REST layer needs, bundled so routes take one hop.

    Every handle is optional because ``agent.tasks_enabled=False`` installs a
    disabled stub: ``store`` and ``orchestrator`` become ``None`` and the event
    and input stores are never created at all. Routes check ``enabled`` once
    and answer with an empty, ``enabled: false`` payload.
    """

    store: "TaskStore | None"
    orchestrator: "TaskOrchestrator | None"
    event_store: "TaskEventStore | None"
    input_store: "TaskInputStore | None"

    @property
    def enabled(self) -> bool:
        return self.store is not None


class WebFacadeMixin:
    """Public accessors and settings-with-sync operations for the web layer."""

    # ── configuration ────────────────────────────────────────────────

    @property
    def settings(self) -> "AppSettings":
        """The live settings object. Mutations take effect immediately.

        Fields backed by a running subsystem need that subsystem told as well;
        use the ``set_*`` methods below for those rather than assigning here.
        """
        return self._settings  # type: ignore[attr-defined]

    @property
    def user_id(self) -> str:
        """The configured user id, never blank.

        REST rows and background task rows must agree on this or a task
        created by one path becomes invisible to the other. Stripped to match
        how ``__init__`` normalises it, so the two cannot disagree.
        """
        return str(getattr(self, "_user_id", "") or "").strip() or "default"

    @property
    def missing_chat_model(self) -> str:
        """The configured local chat model if it is not installed, else "".

        Non-empty means the next turn would 404; the onboarding flow offers to
        pull it. Read via ``getattr`` because it is set during LLM wiring, and
        a client can connect before that has run.
        """
        return str(getattr(self, "_missing_chat_model", "") or "")

    # ── settings that need a live subsystem told ─────────────────────

    def set_earcons_enabled(self, enabled: bool) -> None:
        """Toggle earcons, sync the player, and persist the override."""
        enabled = bool(enabled)
        self._settings.audio.earcons_enabled = enabled  # type: ignore[attr-defined]
        earcons = getattr(self, "_earcons", None)
        if earcons is not None:
            try:
                earcons.enabled = enabled
            except Exception:
                log.debug("earcons enable toggle failed", exc_info=True)
        try:
            persist_user_overrides({"audio": {"earcons_enabled": enabled}})
        except Exception:
            log.debug("persist earcons override failed", exc_info=True)

    def set_proactive_runtime(
        self,
        *,
        cooldown_seconds: float | None = None,
        cooldown_seconds_typed: float | None = None,
    ) -> None:
        """Push new proactive cooldowns into settings and the running director.

        The director caches its cooldowns, so a settings-only write would not
        take effect until the next restart.
        """
        settings_agent = self._settings.agent  # type: ignore[attr-defined]
        runtime: dict[str, float] = {}
        if cooldown_seconds is not None:
            settings_agent.proactive_cooldown_seconds = float(cooldown_seconds)
            runtime["cooldown_seconds"] = float(cooldown_seconds)
        if cooldown_seconds_typed is not None:
            settings_agent.proactive_cooldown_seconds_typed = float(
                cooldown_seconds_typed,
            )
            runtime["cooldown_seconds_typed"] = float(cooldown_seconds_typed)
        if not runtime:
            return
        proactive = getattr(self, "_proactive", None)
        if proactive is None:
            return
        try:
            proactive.update_runtime(**runtime)
        except Exception:
            log.debug("proactive update_runtime failed", exc_info=True)

    def set_shared_moments_runtime(
        self,
        *,
        min_turn_gap: int | None = None,
        cooldown_seconds: float | None = None,
    ) -> None:
        """Update shared-moment pacing in settings and the live detector.

        The detector is absent when shared moments are disabled, in which case
        the settings write still lands so enabling it later picks up the value.
        """
        settings_agent = self._settings.agent  # type: ignore[attr-defined]
        runtime: dict[str, Any] = {}
        if min_turn_gap is not None:
            settings_agent.shared_moments_min_turn_gap = int(min_turn_gap)
            runtime["min_turn_gap"] = int(min_turn_gap)
        if cooldown_seconds is not None:
            settings_agent.shared_moments_cooldown_seconds = float(cooldown_seconds)
            runtime["cooldown_seconds"] = float(cooldown_seconds)
        if not runtime:
            return
        detector = getattr(self, "_moment_detector", None)
        if detector is None:
            return
        try:
            detector.update_runtime(**runtime)
        except Exception:
            log.debug("moment detector update_runtime failed", exc_info=True)

    def set_grounding_line_mode(self, mode: str) -> None:
        """Set the grounding-line mode and tell the prompt assembler.

        The assembler decides per build whether to emit the line, so it has to
        hear about the change or the next prompt keeps the old mode.
        """
        self._settings.agent.grounding_line_mode = mode  # type: ignore[attr-defined]
        assembler = getattr(self, "_prompt_assembler", None)
        if assembler is None:
            return
        try:
            assembler.set_grounding_line_mode(mode)
        except Exception:
            log.debug("set_grounding_line_mode failed", exc_info=True)

    # ── history ──────────────────────────────────────────────────────

    def list_sessions(self) -> list[dict[str, Any]]:
        """Every stored session, for the session switcher."""
        return self._chat_db.list_sessions()  # type: ignore[attr-defined]

    def get_session_messages(
        self,
        session_id: str,
        *,
        limit: int = 200,
        before_id: int | None = None,
    ) -> list[Any]:
        """Messages for a session, oldest-first.

        Without ``before_id`` this is the most recent ``limit`` rows (the
        initial-load contract); with it, up to ``limit`` rows immediately older
        than that id, which is how the chat UI pages backwards.
        """
        if before_id is not None:
            return self._chat_db.get_messages_before(  # type: ignore[attr-defined]
                session_id, before_id=int(before_id), limit=limit,
            )
        return self._chat_db.get_messages(  # type: ignore[attr-defined]
            session_id, limit=limit,
        )

    def delete_session(self, session_id: str) -> None:
        """Delete a stored session and its messages."""
        self._chat_db.delete_session(session_id)  # type: ignore[attr-defined]

    # ── tasks ────────────────────────────────────────────────────────

    @property
    def tasks(self) -> TaskHandles:
        """The task stores, or a bundle of ``None`` when tasks are disabled."""
        return TaskHandles(
            store=getattr(self, "_task_store", None),
            orchestrator=getattr(self, "_task_orchestrator", None),
            event_store=getattr(self, "_task_event_store", None),
            input_store=getattr(self, "_task_input_store", None),
        )

    # ── turn plumbing ────────────────────────────────────────────────

    def notify_user_message(
        self, speaker: str, text: str, message_id: int | None = None,
    ) -> None:
        """Announce a chat message to every message listener.

        Drives the chat transcript for both typed and voice input, and for
        Aiko's own replies.
        """
        self._notify_message(speaker, text, message_id)  # type: ignore[attr-defined]

    def request_turn_stop(self) -> None:
        """Ask the in-flight turn to stop generating at its next checkpoint."""
        runner = getattr(self, "_turn_runner", None)
        if runner is None:
            return
        runner.request_stop()

    def weather_public_snapshot(self) -> dict[str, Any]:
        """Weather state with API keys masked, safe to send to a browser."""
        return self._weather_public_snapshot()  # type: ignore[attr-defined]

    # ── on-demand workers ────────────────────────────────────────────

    def run_curiosity_seed_worker_now(self) -> dict[str, Any]:
        """Run one curiosity-seed pass now, bypassing the idle-window gate."""
        return self._run_worker_now("_curiosity_seed_worker", "curiosity seed")

    def run_goal_worker_now(self) -> dict[str, Any]:
        """Run one goal pass now.

        Skips the idle gate but not the worker's own rate limiter, so calling
        this repeatedly cannot exceed the configured per-period caps.
        """
        return self._run_worker_now("_goal_worker", "goal")

    def run_concept_synthesis_worker_now(self) -> dict[str, Any]:
        """Run one concept-synthesis pass now, forced.

        ``force`` matters for the manual trigger: a normal pass short-circuits
        when no source memory changed, which looks broken to someone who just
        pressed the button.
        """
        return self._run_worker_now("_concept_synthesis_worker", "concept synthesis", force=True)

    def _run_worker_now(
        self, attr: str, label: str, **kwargs: Any,
    ) -> dict[str, Any]:
        worker = getattr(self, attr, None)
        if worker is None:
            raise WorkerUnavailable(f"{label} worker unavailable")
        return worker.run(**kwargs) or {}
