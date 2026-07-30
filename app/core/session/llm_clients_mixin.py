"""Worker LLM client mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
worker/maintenance/workflow client construction, the worker-model
cascade, ``set_chat_model``, and context-window resolution. The chat
model listing ``list_chat_models`` stays in
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
from app.core.infra.settings import find_provider
from app.core.infra.settings import LLM_ROLE_MAIN_CHAT
from app.core.infra.settings import LLM_ROLE_WORKER_DEFAULT
from app.core.infra.settings import LLM_ROLE_WORKFLOW
from app.core.infra.settings import LlmRoute
from app.core.infra.settings import local_ollama_provider
from app.core.infra.settings import transport_for_provider
from app.llm.llm_gate import LlmPriorityGate
from app.llm.llm_gate import MAINTENANCE_WORKER
from app.llm.ollama_client import OllamaClient
from app.llm.llm_gate import TASK
from app.llm.factory import build_client_for_route
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

    def _route_or_none(self, role: str) -> "LlmRoute | None":
        """Look up one role in the route table; ``None`` when unset."""
        try:
            return self._settings.llm.routes.get(role)
        except Exception:
            return None

    def _build_route_client(
        self, route: "LlmRoute | None", *, role: str,
    ) -> ChatClient:
        """Resolve a route into a cached :class:`ChatClient`.

        Falls back to a bare local-Ollama client when the role has no
        route or its provider is missing from the catalogue — a
        hand-edited config must not stop the app from booting into the
        settings drawer where the user can fix it.
        """
        if route is not None:
            try:
                return build_client_for_route(
                    self._client_cache, route=route, settings=self._settings.llm,
                )
            except Exception:
                log.warning(
                    "route %s: could not resolve provider %r; falling back to "
                    "local Ollama. Fix the route in Settings -> LLM.",
                    role,
                    getattr(route, "provider_id", ""),
                )
        else:
            log.warning("route %s: not configured; falling back to local Ollama.", role)
        provider = local_ollama_provider(self._settings.llm)
        return OllamaClient(
            transport_for_provider(provider, route=route),
            base_url=provider.base_url,
            keep_alive=provider.keep_alive,
        )

    @staticmethod
    def _routes_share_client(
        a: "LlmRoute | None", b: "LlmRoute | None",
    ) -> bool:
        """True when two routes can be served by one client instance.

        Same provider AND same context window: the client cache is keyed
        on the endpoint, so two routes on one provider already share an
        instance — but the transport carries a default ``num_ctx``, so
        routes that disagree on the window each need their own.
        """
        if a is None or b is None:
            return False
        return (
            a.provider_id == b.provider_id
            and a.context_window == b.context_window
        )

    def _worker_route_model_ctx(self) -> tuple[str, int | None]:
        """Resolve the background-worker model + context window.

        The ``worker_default`` route is the source of truth. Used at both
        worker-client construction sites (``__init__`` + ``update_route``)
        so a route edit actually retargets the workers instead of only
        persisting the catalogue.
        """
        route = self._route_or_none(LLM_ROLE_WORKER_DEFAULT)
        if route is None:
            return "llama3.1:8b", None
        return (route.model or "").strip() or "llama3.1:8b", route.context_window

    def _build_worker_client(self) -> "ChatClient":
        """Construct a dedicated worker client for the ``worker_default`` route.

        Only used when the worker route diverges from ``main_chat``
        (see :meth:`_routes_share_client`) — otherwise the two roles
        share one cached client.

        A local worker gets its own uncached :class:`OllamaClient`
        rather than the shared one: the model is passed per-call by each
        worker via ``_effective_worker_model``, but the transport's
        default ``num_ctx`` is what sizes the kv-cache on first load, so
        a worker route with a different context window can't reuse the
        chat client's transport. Remote providers carry no such
        per-route transport state, so those go through the cache.
        """
        route = self._route_or_none(LLM_ROLE_WORKER_DEFAULT)
        provider = (
            find_provider(self._settings.llm, route.provider_id)
            if route is not None
            else None
        ) or local_ollama_provider(self._settings.llm)
        if provider.kind != "ollama":
            return self._build_route_client(
                route, role=LLM_ROLE_WORKER_DEFAULT,
            )
        return OllamaClient(
            transport_for_provider(provider, route=route),
            base_url=provider.base_url,
            keep_alive=provider.keep_alive,
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
        1. The route's explicit ``context_window``.
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
        """Point the ``main_chat`` route at a different model.

        Only the model changes — the provider, context window and token
        budget on the route are left alone, so switching models inside
        one provider can't silently widen the window back to the
        model's advertised maximum.
        """
        normalized = (model_name or "").strip()
        if not normalized:
            return
        route = self._route_or_none(LLM_ROLE_MAIN_CHAT)
        if route is not None:
            route.model = normalized
        self._effective_chat_model = normalized
        # The worker model only follows the chat model when the worker
        # client IS the chat client. When workers run on a separate
        # Ollama instance their model stays pinned to the
        # ``worker_default`` route — it's a different model on a
        # different backend, and sending this name there would 404.
        if self._worker_client_inner is self._chat_client:
            self._effective_worker_model = normalized
        # Re-resolve the context window for the new model: keep the
        # route's explicit value if it has one, else re-query /api/show.
        self._context_window, self._context_source = self._resolve_context_window(
            route.context_window if route is not None else None, normalized,
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
