"""Worker LLM client mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
worker/maintenance/workflow client construction, the worker-model
cascade, ``set_chat_model``, and context-window resolution. The chat
client factory ``_build_chat_client`` and ``list_chat_models`` stay in
the controller / llm-settings group (they touch the secret/route
machinery and are patched by tests there). State ownership stays on
``SessionController.__init__``.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for any moved method must patch
``app.core.session.llm_clients_mixin.<symbol>`` instead."""
from __future__ import annotations

import logging
from typing import Any
from app.llm.llm_gate import CONVERSATION_WORKER
from collections.abc import Callable
from app.llm.chat_client import ChatClient
from app.llm.llm_gate import GatedChatClient
from app.core.infra.settings import LLM_ROLE_WORKER_DEFAULT
from app.core.infra.settings import LLM_ROLE_WORKFLOW
from app.llm.llm_gate import LlmPriorityGate
from app.llm.llm_gate import MAINTENANCE_WORKER
from app.llm.ollama_client import OllamaClient
from app.llm.llm_gate import TASK
from app.llm.factory import build_client_for_route
from dataclasses import replace
from app.llm.llm_gate import tier_from_name


log = logging.getLogger("app.session")


class LlmClientsMixin:
    """Worker/workflow client build + worker-model cascade + set_chat_model."""

    @staticmethod
    def _apply_model_to_worker(worker: Any, model: str) -> bool:
        """Push ``model`` onto one worker via whatever knob it exposes.

        Tries ``update_runtime(model=...)``, then ``update_model(...)``,
        then a direct ``_model`` assignment. Returns True if any path
        landed. All failures are swallowed -- a single odd worker must
        not break the cascade.
        """
        if worker is None:
            return False
        fn = getattr(worker, "update_runtime", None)
        if callable(fn):
            try:
                fn(model=model)
                return True
            except TypeError:
                try:
                    fn(model)
                    return True
                except Exception:
                    pass
            except Exception:
                pass
        fn = getattr(worker, "update_model", None)
        if callable(fn):
            try:
                fn(model)
                return True
            except Exception:
                pass
        if hasattr(worker, "_model"):
            try:
                worker._model = model  # type: ignore[attr-defined]
                return True
            except Exception:
                pass
        return False

    def _build_worker_runtime_updaters(self) -> list[Callable[[str], None]]:
        """Build the declarative cascade list once (lazy)."""
        updaters: list[Callable[[str], None]] = []
        for attr in self._WORKER_MODEL_CONSUMERS:
            def _upd(model: str, _attr: str = attr) -> None:
                self._apply_model_to_worker(getattr(self, _attr, None), model)

            updaters.append(_upd)
        return updaters

    def _cascade_worker_model(self, worker_model: str) -> None:
        """Apply ``worker_model`` to every registered worker consumer."""
        if getattr(self, "_worker_runtime_updaters", None) is None:
            self._worker_runtime_updaters = self._build_worker_runtime_updaters()
        for upd in self._worker_runtime_updaters:
            try:
                upd(worker_model)
            except Exception:
                log.debug("worker runtime model cascade failed", exc_info=True)

    def _worker_route_model_ctx(self) -> tuple[str, int | None]:
        """Resolve the background-worker model + context window (P13).

        The ``worker_default`` route is the source of truth: when its
        ``model`` / ``context_window`` are set they win, falling back to
        the legacy ``ollama.chat_model`` / ``ollama.context_window`` so
        an un-customised install behaves exactly as before. Used at both
        worker-client construction sites (``__init__`` + 
        ``reconfigure_chat_llm``) so a route edit (which previously only
        persisted the catalogue) actually retargets the workers.
        """
        legacy_model = (self._settings.ollama.chat_model or "").strip()
        legacy_ctx = getattr(self._settings.ollama, "context_window", None)
        try:
            route = self._settings.llm.routes.get(LLM_ROLE_WORKER_DEFAULT)
        except Exception:
            route = None
        model = legacy_model
        ctx = legacy_ctx
        if route is not None:
            route_model = (getattr(route, "model", "") or "").strip()
            if route_model:
                model = route_model
            if getattr(route, "context_window", None):
                ctx = route.context_window
        return (model or "llama3.1:8b"), ctx

    def _build_worker_ollama_client(self, keep_alive: str) -> "ChatClient":
        """Construct a dedicated local-Ollama worker client honouring the
        worker route's context window (P13). The model is passed per-call
        by each worker via ``_effective_worker_model``; we still seed the
        client's default ``chat_model`` + ``num_ctx`` from the route so a
        worker that omits an explicit model/num_ctx inherits the right
        size.
        """
        worker_model, worker_ctx = self._worker_route_model_ctx()
        base = self._settings.ollama
        worker_settings = base
        if (worker_ctx is not None and worker_ctx != base.context_window) or (
            worker_model and worker_model != (base.chat_model or "").strip()
        ):
            worker_settings = replace(
                base,
                context_window=worker_ctx if worker_ctx is not None else base.context_window,
                chat_model=worker_model or base.chat_model,
            )
        return OllamaClient(
            worker_settings,
            base_url=base.base_url,
            keep_alive=keep_alive,
        )

    def _install_worker_clients(self, raw_worker_client: ChatClient) -> None:
        """Wrap the raw worker client in the priority gate (Phase 6).

        Builds ONE :class:`LlmPriorityGate` around the underlying worker
        client and exposes three shared-gate proxy views:

        * ``self._worker_client`` (+ the ``self._ollama`` alias) at
          ``CONVERSATION_WORKER`` — the ~24 existing per-turn /
          speaking-window sites keep using it unchanged.
        * ``self._maintenance_client`` at ``MAINTENANCE_WORKER`` — for
          idle-scheduler workers (decay, promotion, conflict, …).
        * ``self._workflow_client`` at ``TASK`` — injected into the
          ``GoalWorkflowHandler``.

        Per-call acquire (inside the proxy) means the workflow daemon
        releases the gate while waiting on its children — no priority
        inversion. When the gate is disabled the proxies are
        pass-through (``gate=None``).
        """
        agent = self._settings.agent
        gate_enabled = bool(getattr(agent, "worker_llm_gate_enabled", True))
        max_conc = max(1, int(getattr(agent, "worker_llm_max_concurrency", 1)))
        overrides = dict(getattr(agent, "worker_llm_priority_overrides", {}) or {})
        self._worker_client_inner = raw_worker_client
        gate = (
            LlmPriorityGate(max_concurrency=max_conc, name="worker")
            if gate_enabled
            else None
        )
        self._worker_llm_gate = gate
        conv_prio = tier_from_name(overrides.get("conversation", ""), CONVERSATION_WORKER)
        maint_prio = tier_from_name(overrides.get("maintenance", ""), MAINTENANCE_WORKER)
        task_prio = tier_from_name(overrides.get("task", ""), TASK)
        # On reconfigure, mutate the existing proxy objects in place so the
        # ~24 worker references already holding them follow the new
        # topology; on first build, create them.
        existing_worker = getattr(self, "_worker_client", None)
        if isinstance(existing_worker, GatedChatClient):
            existing_worker.retarget(raw_worker_client, gate, conv_prio)
        else:
            self._worker_client = GatedChatClient(
                raw_worker_client, gate, conv_prio, name="conversation"
            )
        existing_maint = getattr(self, "_maintenance_client", None)
        if isinstance(existing_maint, GatedChatClient):
            existing_maint.retarget(raw_worker_client, gate, maint_prio)
        else:
            self._maintenance_client = GatedChatClient(
                raw_worker_client, gate, maint_prio, name="maintenance"
            )
        self._ollama = self._worker_client  # back-compat alias
        existing_workflow = getattr(self, "_workflow_client", None)
        new_workflow = self._build_workflow_client(gate, task_prio)
        if isinstance(existing_workflow, GatedChatClient) and isinstance(
            new_workflow, GatedChatClient
        ):
            existing_workflow.retarget(
                new_workflow._inner, new_workflow._gate, task_prio
            )
        else:
            self._workflow_client = new_workflow
        log.info(
            "worker-llm gate: enabled=%s max_concurrency=%d conv=%d maint=%d task=%d",
            gate_enabled,
            max_conc,
            conv_prio,
            maint_prio,
            task_prio,
        )

    def _build_workflow_client(
        self, worker_gate: "LlmPriorityGate | None", task_priority: int
    ) -> ChatClient:
        """Resolve the ``workflow`` route into a gated client.

        Default case: the workflow route mirrors ``worker_default`` so it
        resolves to the SAME underlying worker client — share the worker
        gate at ``TASK`` priority (one Ollama instance, no extra VRAM).

        Divergent case: the user repointed ``workflow`` at a different
        provider. Resolve a dedicated client via the cache; a *remote*
        provider has its own compute so it gets NO gate (it must not
        inherit the local model's concurrency=1), while a divergent
        *local* Ollama route still shares the worker gate.
        """
        try:
            route = self._settings.llm.routes.get(LLM_ROLE_WORKFLOW)
            worker_route = self._settings.llm.routes.get(LLM_ROLE_WORKER_DEFAULT)
        except Exception:
            route = None
            worker_route = None
        mirrors_worker = (
            route is None
            or worker_route is None
            or (
                route.provider_id == worker_route.provider_id
                and (route.model or "") == (worker_route.model or "")
                and route.context_window == worker_route.context_window
            )
        )
        if mirrors_worker:
            return GatedChatClient(
                self._worker_client_inner, worker_gate, task_priority, name="task"
            )
        try:
            client = build_client_for_route(
                self._client_cache, route=route, settings=self._settings.llm
            )
            provider = self._find_llm_provider(route.provider_id)
            is_local = (
                provider is not None
                and (provider.kind or "").strip().lower() == "ollama"
            )
            gate = worker_gate if is_local else None
            log.info(
                "workflow client: divergent route provider=%s model=%s local=%s",
                route.provider_id,
                route.model,
                is_local,
            )
            return GatedChatClient(client, gate, task_priority, name="task")
        except Exception:
            log.warning(
                "workflow client: route resolution failed, sharing worker client",
                exc_info=True,
            )
            return GatedChatClient(
                self._worker_client_inner, worker_gate, task_priority, name="task"
            )

    def _resolve_context_window(
        self, override: int | None, model: str,
    ) -> tuple[int, str]:
        """Pick the context window and record the source.

        Order of preference:
        1. Explicit config override (``chat_llm.context_window`` /
           ``ollama.context_window``).
        2. Active client's ``get_context_length(model)`` — Ollama's
           ``/api/show`` for local models, the static lookup table
           in ``OpenAICompatibleClient`` for known cloud models.
        3. Hardcoded ``8192`` last-resort fallback.
        """
        if override:
            try:
                value = int(override)
                if value > 0:
                    return value, "config"
            except (TypeError, ValueError):
                pass
        try:
            detected = self._chat_client.get_context_length(model)
        except Exception:
            detected = None
        if detected and detected > 0:
            return int(detected), "client"
        return 8192, "fallback"

    def set_chat_model(self, model_name: str) -> None:
        normalized = (model_name or "").strip()
        if not normalized:
            return
        # Write the new model to the field that actually owns it:
        # ``ollama.chat_model`` for the pure-Ollama setup, and
        # ``chat_llm.model`` for the remote / OpenAI-compatible
        # setup. Cross-writing both (the pre-PR2 behaviour) used to
        # overwrite the WORKER model name on every chat-model change
        # — when chat moved to ``gpt-5-mini``, ``ollama.chat_model``
        # also became ``gpt-5-mini``, and on next boot the
        # background workers tried to hit local Ollama with the
        # remote model name (HTTP 404).
        if (self._chat_provider or "ollama").strip().lower() == "ollama":
            self._settings.ollama.chat_model = normalized
        else:
            self._settings.chat_llm.model = normalized
        self._effective_chat_model = normalized
        # The worker model only follows the chat model when the
        # worker client IS the chat client (pure-Ollama OR
        # ``workers_use_local=False``). When workers run on a
        # separate local Ollama instance, the worker model stays
        # pinned to whatever ``ollama.chat_model`` was at startup —
        # it's a different model on a different backend.
        if self._worker_client_inner is self._chat_client:
            self._effective_worker_model = normalized
        # Re-resolve the context window for the new model. Honour the explicit
        # config override if any; otherwise re-query /api/show.
        chat_llm = self._settings.chat_llm
        ctx_override = chat_llm.context_window or getattr(
            self._settings.ollama, "context_window", None,
        )
        self._context_window, self._context_source = self._resolve_context_window(
            ctx_override, normalized,
        )
        self._turn_runner.update_runtime(
            model=normalized, context_window=self._context_window,
        )
        # Cascade the WORKER model (not the chat model) to every active
        # worker instance via the declarative registry (P13b — replaces
        # the old hand-coded 3-worker block that left ~12 workers on the
        # stale model until restart). The proactive director is on the
        # chat path so it gets the chat model.
        worker_model = self._effective_worker_model
        self._cascade_worker_model(worker_model)
        self._proactive.update_runtime(model=normalized)
