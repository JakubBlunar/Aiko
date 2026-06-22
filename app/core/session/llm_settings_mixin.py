"""LLM settings / provider catalogue / secrets mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
provider catalogue + route CRUD, the chat_llm reconfigure path, the
legacy<->catalogue mirror, secret migration/hydration, and the
LLM-settings persistence. State ownership stays on
``SessionController.__init__``.

The chat-client factory ``_build_chat_client`` stays defined in the
controller (re-exported for ``server.py`` + tests); this module reaches
it through a thin lazy forwarder so monkeypatches of
``session_controller._build_chat_client`` are still honoured and there
is no import cycle.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for a moved method must patch
``app.core.session.llm_settings_mixin.<symbol>`` instead (notably
``persist_user_overrides`` and ``secret_store``)."""
from __future__ import annotations

import logging
from typing import Any
from app.llm.chat_client import ChatClient
from app.core.infra.settings import LLM_ROLE_MAIN_CHAT
from app.core.infra.settings import LLM_ROLE_WORKER_DEFAULT
from app.core.infra.settings import LLM_ROLE_WORKFLOW
from app.core.infra.settings import LlmProvider
from app.core.infra.settings import LlmRoute
from app.core.infra.settings import _urls_match
from app.core.infra.settings import persist_user_overrides
from app.core.infra import secret_store
import time
import uuid
from app.core.session.llm_presets import _PROVIDER_PRESETS


def _build_chat_client(*args, **kwargs):
    """Lazy forwarder to the controller's chat-client factory.

    Imported at call time so (a) there is no import cycle with
    ``session_controller`` and (b) test monkeypatches of
    ``session_controller._build_chat_client`` are honoured.
    """
    from app.core.session.session_controller import (
        _build_chat_client as _impl,
    )

    return _impl(*args, **kwargs)


log = logging.getLogger("app.session")


class LlmSettingsMixin:
    """Provider catalogue + routes + chat_llm reconfigure + secrets."""

    def reconfigure_chat_llm(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Rebuild ``self._chat_client`` from a partial ``chat_llm`` patch.

        ``payload`` is a dict subset of :class:`ChatLlmSettings` fields
        (any combination of ``provider``, ``provider_preset``,
        ``model``, ``base_url``, ``api_key``, ``api_key_env``,
        ``max_tokens``, ``temperature``, ``context_window``,
        ``keep_alive``, ``workers_use_local``, ``extra_headers``).

        Side effects:
        1. The in-memory :class:`ChatLlmSettings` is mutated in place.
        2. ``persist_user_overrides({"chat_llm": ...})`` writes the
           new values to ``user.json`` so the change survives a restart.
        3. ``self._chat_client`` is rebuilt via :func:`_build_chat_client`.
        4. ``self._worker_client`` (and the ``self._ollama`` alias) is
           rebound: if the new provider is non-Ollama and
           ``workers_use_local`` is True, a fresh local Ollama client
           is created; otherwise the worker client points at the
           chat client.
        5. TurnRunner + ProactiveDirector are pointed at the new
           client; the model + context window cache are reset.
        6. Worker clients **are not** swapped on existing worker
           instances — the rename in this method is best-effort
           against new turns only. A restart is required to flip
           background workers between Ollama and a remote provider.
           Documented in the "Restart required" notice on the UI.

        Returns the masked snapshot (with ``has_api_key`` instead of
        the raw key) so the REST caller can echo it back to the
        client.
        """
        chat_llm = self._settings.chat_llm

        def _set(field: str, value: Any) -> None:
            if hasattr(chat_llm, field):
                setattr(chat_llm, field, value)

        if "provider" in payload:
            raw = str(payload["provider"] or "").strip().lower()
            if raw in {"ollama", "openai_compatible"}:
                _set("provider", raw)
        if "provider_preset" in payload:
            _set("provider_preset", str(payload["provider_preset"] or "").strip().lower())
        if "model" in payload:
            _set("model", str(payload["model"] or "").strip())
        if "base_url" in payload:
            _set("base_url", str(payload["base_url"] or "").strip())
        if "api_key" in payload:
            # Empty string is a valid value here — it means "clear the
            # stored key". Don't strip-then-falsy-collapse.
            _set("api_key", str(payload["api_key"] or "").strip())
        if "api_key_env" in payload:
            _set("api_key_env", str(payload["api_key_env"] or "").strip())
        if "max_tokens" in payload:
            try:
                _set("max_tokens", max(0, int(payload["max_tokens"])))
            except (TypeError, ValueError):
                pass
        if "temperature" in payload:
            try:
                _set("temperature", float(payload["temperature"]))
            except (TypeError, ValueError):
                pass
        if "context_window" in payload:
            raw = payload["context_window"]
            try:
                _set(
                    "context_window",
                    int(raw) if raw not in (None, "", 0) else None,
                )
            except (TypeError, ValueError):
                pass
        if "keep_alive" in payload:
            _set("keep_alive", str(payload["keep_alive"] or "").strip() or "30m")
        if "workers_use_local" in payload:
            _set("workers_use_local", bool(payload["workers_use_local"]))
        if "reasoning_effort" in payload:
            _set(
                "reasoning_effort",
                str(payload["reasoning_effort"] or "").strip().lower(),
            )
        if "extra_headers" in payload:
            raw_headers = payload.get("extra_headers") or {}
            if isinstance(raw_headers, dict):
                _set("extra_headers", {
                    str(k).strip(): str(v).strip()
                    for k, v in raw_headers.items()
                    if str(k).strip() and v is not None
                })

        # Persist. The API key is routed into the OS keychain via
        # ``store_or_passthrough`` (-> "" on disk) when a backend exists;
        # otherwise it falls back to plaintext so the key isn't lost.
        try:
            persist_user_overrides({"chat_llm": {
                "provider": chat_llm.provider,
                "provider_preset": chat_llm.provider_preset,
                "model": chat_llm.model,
                "base_url": chat_llm.base_url,
                "api_key": secret_store.store_or_passthrough(
                    self._chat_llm_secret_account(), chat_llm.api_key
                ),
                "api_key_env": chat_llm.api_key_env,
                "max_tokens": chat_llm.max_tokens,
                "temperature": chat_llm.temperature,
                "context_window": chat_llm.context_window,
                "keep_alive": chat_llm.keep_alive,
                "workers_use_local": chat_llm.workers_use_local,
                "reasoning_effort": getattr(chat_llm, "reasoning_effort", ""),
                "extra_headers": dict(chat_llm.extra_headers or {}),
            }})
        except Exception:
            log.warning("persist chat_llm overrides failed", exc_info=True)

        # Rebuild clients.
        self._chat_client = _build_chat_client(
            chat_llm=chat_llm,
            ollama_settings=self._settings.ollama,
            role="chat",
        )
        if (
            (chat_llm.provider or "ollama").strip().lower() != "ollama"
            and bool(chat_llm.workers_use_local)
        ):
            raw_worker_client: ChatClient = self._build_worker_ollama_client(
                chat_llm.keep_alive
            )
        else:
            raw_worker_client = self._chat_client
        # Phase 6: rebuild the gate + proxies in place. Existing worker
        # references hold the proxy objects, which forward to whatever the
        # gate wraps, so this transparently retargets every worker.
        self._install_worker_clients(raw_worker_client)
        self._chat_provider = (chat_llm.provider or "ollama").strip().lower()

        # Recompute the worker model based on the new client topology:
        # when the worker client is a separate local Ollama, the
        # worker model comes from the ``worker_default`` route (P13),
        # falling back to ``ollama.chat_model``; otherwise it tracks the
        # chat model. ``set_chat_model`` below picks this up via
        # ``self._effective_worker_model``.
        if self._worker_client_inner is self._chat_client:
            self._effective_worker_model = (
                chat_llm.model.strip()
                or self._settings.ollama.chat_model.strip()
                or "llama3.1:8b"
            )
        else:
            self._effective_worker_model, _ = self._worker_route_model_ctx()

        # Re-resolve model + context window. ``set_chat_model`` does
        # the right cascade (TurnRunner / ProactiveDirector / workers).
        new_model = (
            chat_llm.model.strip()
            or self._settings.ollama.chat_model.strip()
            or "llama3.1:8b"
        )
        # Drop the model-listing cache so the next /api/models lands fresh.
        self._models_cache = None
        # Point TurnRunner + ProactiveDirector at the new client.
        # ``set_chat_model`` below cascades the model/context update.
        try:
            self._turn_runner.update_runtime(client=self._chat_client)
        except Exception:
            log.debug("turn_runner update_runtime(client=) failed", exc_info=True)
        try:
            self._proactive.update_runtime(client=self._chat_client)
        except Exception:
            log.debug("proactive update_runtime(client=) failed", exc_info=True)
        self.set_chat_model(new_model)
        log.info(
            "chat_llm reconfigured: provider=%s model=%s base_url=%s "
            "workers_use_local=%s has_api_key=%s",
            chat_llm.provider,
            self._effective_chat_model,
            chat_llm.base_url or "(default)",
            "1" if chat_llm.workers_use_local else "0",
            "1" if (chat_llm.api_key or "").strip() else "0",
        )
        # PR 2: mirror the just-applied legacy state back into the
        # catalogue so ``llm.routes.main_chat`` stays in sync. Cheap
        # (mutates in-memory dataclasses), idempotent, and lets the
        # new REST surface read either ``chat_llm`` or the catalogue
        # interchangeably.
        try:
            self._sync_llm_routes_from_legacy()
            self._persist_llm_settings()
        except Exception:
            log.debug("sync llm.routes from legacy failed", exc_info=True)
        return self._chat_llm_public_snapshot()

    def _chat_llm_public_snapshot(self) -> dict[str, Any]:
        """Return a serialisable view of ``chat_llm`` with the API key masked.

        Used by ``GET /api/settings`` and the response to PATCH /
        PUT credentials. The raw key is replaced by a boolean
        ``has_api_key`` flag; the UI shows ``••••••••`` when true and
        empty when false.
        """
        cfg = self._settings.chat_llm
        return {
            "provider": cfg.provider,
            "provider_preset": cfg.provider_preset,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "has_api_key": bool((cfg.api_key or "").strip()),
            "api_key_env": cfg.api_key_env,
            "max_tokens": int(cfg.max_tokens),
            "temperature": (
                float(cfg.temperature) if cfg.temperature is not None else None
            ),
            "context_window": cfg.context_window,
            "keep_alive": cfg.keep_alive,
            "workers_use_local": bool(cfg.workers_use_local),
            "reasoning_effort": getattr(cfg, "reasoning_effort", "") or "",
            "extra_headers": dict(cfg.extra_headers or {}),
        }

    def _mask_provider(self, provider: LlmProvider) -> dict[str, Any]:
        """Return a JSON-serialisable view of ``provider`` with the
        ``api_key`` masked behind a ``has_api_key`` flag."""
        return {
            "id": provider.id,
            "name": provider.name,
            "kind": provider.kind,
            "base_url": provider.base_url,
            "has_api_key": bool((provider.api_key or "").strip()),
            "api_key_env": provider.api_key_env,
            "extra_headers": dict(provider.extra_headers or {}),
            "timeout_seconds": int(provider.timeout_seconds or 300),
            "keep_alive": provider.keep_alive,
            "reasoning_effort": getattr(provider, "reasoning_effort", "") or "",
        }

    def list_providers(self) -> list[dict[str, Any]]:
        """Return the catalogue with credentials masked."""
        return [self._mask_provider(p) for p in self._settings.llm.providers]

    def list_routes(self) -> dict[str, dict[str, Any]]:
        """Return the role-assignment table."""
        out: dict[str, dict[str, Any]] = {}
        for role, route in self._settings.llm.routes.items():
            out[role] = {
                "provider_id": route.provider_id,
                "model": route.model,
                "context_window": route.context_window,
                "max_tokens": int(route.max_tokens or 512),
                "temperature": route.temperature,
                "reasoning_effort": getattr(route, "reasoning_effort", "") or "",
            }
        return out

    def _find_llm_provider(self, provider_id: str) -> LlmProvider | None:
        for entry in self._settings.llm.providers:
            if entry.id == provider_id:
                return entry
        return None

    def _generate_provider_id(self, template_id: str | None) -> str:
        """Pick a unique id for a new provider.

        Uses ``template_id`` as a seed when supplied; appends a suffix
        when the natural id is already taken so two "openai" entries
        can coexist (e.g. a "personal" key and a "team" key).
        """
        base = (template_id or "custom").strip().lower()
        existing = {p.id for p in self._settings.llm.providers}
        if base not in existing:
            return base
        for i in range(2, 100):
            candidate = f"{base}_{i}"
            if candidate not in existing:
                return candidate
        return f"{base}_{uuid.uuid4().hex[:8]}"

    def add_provider(
        self,
        *,
        template_id: str | None = None,
        draft: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a new provider to the catalogue.

        ``template_id`` (optional) seeds the entry from a row of
        :func:`_PROVIDER_PRESETS` (``"openai"``, ``"gemini"``, …). The
        ``draft`` dict can override any field. Returns the masked
        snapshot of the inserted entry.
        """
        seed: dict[str, Any] = {}
        if template_id:
            for preset in _PROVIDER_PRESETS:
                if preset.get("id") == template_id:
                    seed = {
                        "kind": preset.get("provider", "ollama"),
                        "name": preset.get("label", template_id),
                        "base_url": preset.get("base_url", ""),
                        "api_key_env": preset.get("env_hint", ""),
                    }
                    break
        payload = dict(draft or {})
        for k, v in seed.items():
            payload.setdefault(k, v)
        # Translate the legacy "provider" key (used in presets) to the
        # new "kind" field.
        if "kind" not in payload and "provider" in payload:
            payload["kind"] = payload.pop("provider")
        provider_id = (
            str(payload.get("id", "") or "").strip()
            or self._generate_provider_id(template_id)
        )
        kind = str(payload.get("kind", "ollama") or "ollama").strip().lower()
        if kind not in {"ollama", "openai_compatible"}:
            kind = "ollama"
        name = str(payload.get("name", "") or "").strip() or provider_id
        base_url = str(payload.get("base_url", "") or "").strip()
        api_key = str(payload.get("api_key", "") or "").strip()
        api_key_env = str(payload.get("api_key_env", "") or "").strip()
        headers_raw = payload.get("extra_headers") or {}
        if isinstance(headers_raw, dict):
            extra_headers = {
                str(k).strip(): str(v).strip()
                for k, v in headers_raw.items()
                if str(k).strip() and v is not None
            }
        else:
            extra_headers = {}
        try:
            timeout = max(1, int(payload.get("timeout_seconds", 300)))
        except (TypeError, ValueError):
            timeout = 300
        keep_alive = str(payload.get("keep_alive", "30m") or "30m").strip() or "30m"
        reasoning_effort = str(
            payload.get("reasoning_effort", "") or ""
        ).strip().lower()
        new_provider = LlmProvider(
            id=provider_id,
            name=name,
            kind=kind,
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            extra_headers=extra_headers,
            timeout_seconds=timeout,
            keep_alive=keep_alive,
            reasoning_effort=reasoning_effort,
        )
        if self._find_llm_provider(provider_id) is not None:
            raise ValueError(
                f"provider id {provider_id!r} already exists; "
                "edit the existing entry or pick a different id"
            )
        self._settings.llm.providers.append(new_provider)
        self._persist_llm_settings()
        log.info(
            "llm: added provider id=%s kind=%s base_url=%s",
            new_provider.id,
            new_provider.kind,
            new_provider.base_url,
        )
        return self._mask_provider(new_provider)

    def update_provider(
        self,
        provider_id: str,
        draft: dict[str, Any],
    ) -> dict[str, Any]:
        """Edit non-credential fields on an existing provider.

        Use :meth:`update_provider_credentials` for the api_key /
        api_key_env path (separate to keep credentials out of logs).
        """
        provider = self._find_llm_provider(provider_id)
        if provider is None:
            raise KeyError(f"unknown provider id={provider_id!r}")
        if "name" in draft:
            provider.name = str(draft["name"] or "").strip() or provider.name
        if "kind" in draft:
            kind = str(draft["kind"] or "").strip().lower()
            if kind in {"ollama", "openai_compatible"}:
                provider.kind = kind
        if "base_url" in draft:
            provider.base_url = str(draft["base_url"] or "").strip()
        if "extra_headers" in draft:
            raw_headers = draft.get("extra_headers") or {}
            if isinstance(raw_headers, dict):
                provider.extra_headers = {
                    str(k).strip(): str(v).strip()
                    for k, v in raw_headers.items()
                    if str(k).strip() and v is not None
                }
        if "timeout_seconds" in draft:
            try:
                provider.timeout_seconds = max(1, int(draft["timeout_seconds"]))
            except (TypeError, ValueError):
                pass
        if "keep_alive" in draft:
            provider.keep_alive = (
                str(draft["keep_alive"] or "").strip() or "30m"
            )
        if "reasoning_effort" in draft:
            provider.reasoning_effort = str(
                draft["reasoning_effort"] or ""
            ).strip().lower()
        # Anything changed -> drop the cached client so future
        # ``cache.get`` rebuilds with the new fields.
        self._client_cache.invalidate(provider_id)
        # If the main_chat route still points at this provider, mirror
        # the changes back to the legacy ``chat_llm`` block so the
        # active session reflects them.
        main_route = self._settings.llm.routes.get(LLM_ROLE_MAIN_CHAT)
        if main_route is not None and main_route.provider_id == provider_id:
            self._mirror_route_to_chat_llm(provider, main_route)
            # Rebuild the active chat client so the next turn picks up
            # the new base_url / extra_headers immediately.
            self._chat_client = _build_chat_client(
                chat_llm=self._settings.chat_llm,
                ollama_settings=self._settings.ollama,
                role="chat",
            )
            try:
                self._turn_runner.update_runtime(client=self._chat_client)
                self._proactive.update_runtime(client=self._chat_client)
            except Exception:
                log.debug("update_runtime(client=) after provider edit failed", exc_info=True)
        self._persist_llm_settings()
        log.info("llm: updated provider id=%s", provider_id)
        return self._mask_provider(provider)

    def update_provider_credentials(
        self,
        provider_id: str,
        creds: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace the api_key / api_key_env on an existing provider."""
        provider = self._find_llm_provider(provider_id)
        if provider is None:
            raise KeyError(f"unknown provider id={provider_id!r}")
        if "api_key" in creds:
            provider.api_key = str(creds["api_key"] or "").strip()
        if "api_key_env" in creds:
            provider.api_key_env = str(creds["api_key_env"] or "").strip()
        # Credentials changed -> invalidate the cached client so the
        # next get() rebuilds with the new bearer header.
        self._client_cache.invalidate(provider_id)
        # If main_chat references this provider, mirror to chat_llm and
        # rebuild the in-flight chat client.
        main_route = self._settings.llm.routes.get(LLM_ROLE_MAIN_CHAT)
        if main_route is not None and main_route.provider_id == provider_id:
            self._settings.chat_llm.api_key = provider.api_key
            self._settings.chat_llm.api_key_env = provider.api_key_env
            self._chat_client = _build_chat_client(
                chat_llm=self._settings.chat_llm,
                ollama_settings=self._settings.ollama,
                role="chat",
            )
            try:
                self._turn_runner.update_runtime(client=self._chat_client)
                self._proactive.update_runtime(client=self._chat_client)
            except Exception:
                log.debug("update_runtime(client=) after credentials edit failed", exc_info=True)
        self._persist_llm_settings()
        log.info(
            "llm: updated credentials provider=%s has_api_key=%s",
            provider_id,
            "1" if (provider.api_key or "").strip() else "0",
        )
        return self._mask_provider(provider)

    def remove_provider(self, provider_id: str) -> None:
        """Delete a provider. Fails with ``ValueError`` when any route
        still references it (the UI catches the 409 and asks the user
        to retarget the route first)."""
        if self._find_llm_provider(provider_id) is None:
            raise KeyError(f"unknown provider id={provider_id!r}")
        referenced_by = [
            role
            for role, route in self._settings.llm.routes.items()
            if route.provider_id == provider_id
        ]
        if referenced_by:
            raise ValueError(
                f"provider id={provider_id!r} is still referenced by "
                f"route(s) {sorted(referenced_by)!r}; retarget them first"
            )
        self._settings.llm.providers = [
            p for p in self._settings.llm.providers
            if p.id != provider_id
        ]
        self._client_cache.invalidate(provider_id)
        self._persist_llm_settings()
        log.info("llm: removed provider id=%s", provider_id)

    def update_route(
        self,
        role: str,
        draft: dict[str, Any],
    ) -> dict[str, Any]:
        """Set ``llm.routes[role]`` from a partial draft.

        For ``main_chat`` this is the catalogue-aware equivalent of
        :meth:`reconfigure_chat_llm`: it mutates the route, mirrors
        the matching fields back to the legacy ``chat_llm`` block,
        and rebuilds the chat client + cascades to TurnRunner /
        ProactiveDirector / SummaryWorker via ``set_chat_model``. For
        ``worker_default`` the route + cache update is recorded but
        the in-flight workers still read from the legacy
        ``ollama`` + ``chat_llm.workers_use_local`` config — Phase 3
        will swap that.
        """
        role_name = (role or "").strip()
        if not role_name:
            raise ValueError("role must be a non-empty string")
        current = self._settings.llm.routes.get(role_name)
        if current is None:
            # Allow creation of new roles (Phase 3 prep).
            current = LlmRoute(provider_id="", model="")
        if "provider_id" in draft:
            current.provider_id = str(draft["provider_id"] or "").strip()
        if "model" in draft:
            current.model = str(draft["model"] or "").strip()
        if "context_window" in draft:
            raw = draft["context_window"]
            try:
                current.context_window = (
                    int(raw) if raw not in (None, "", 0) else None
                )
            except (TypeError, ValueError):
                current.context_window = None
        if "max_tokens" in draft:
            try:
                current.max_tokens = max(0, int(draft["max_tokens"] or 0)) or 512
            except (TypeError, ValueError):
                pass
        if "temperature" in draft:
            raw = draft["temperature"]
            try:
                current.temperature = (
                    float(raw) if raw not in (None, "") else None
                )
            except (TypeError, ValueError):
                current.temperature = None
        if "reasoning_effort" in draft:
            current.reasoning_effort = str(
                draft["reasoning_effort"] or ""
            ).strip().lower()
        provider = self._find_llm_provider(current.provider_id)
        if provider is None:
            raise KeyError(
                f"route {role_name!r} references unknown "
                f"provider_id={current.provider_id!r}"
            )
        self._settings.llm.routes[role_name] = current
        if role_name == LLM_ROLE_MAIN_CHAT:
            # Mirror to legacy chat_llm + rebuild client (uses the
            # existing reconfigure_chat_llm path so all the cascades
            # fire correctly).
            chat_payload = self._route_to_chat_llm_payload(provider, current)
            self.reconfigure_chat_llm(chat_payload)
        else:
            # Non-chat role: persist the catalogue snapshot. Workers
            # don't pick up the new client mid-flight; restart required.
            self._persist_llm_settings()
        log.info(
            "llm: updated route %s -> provider=%s model=%s context=%s",
            role_name,
            current.provider_id,
            current.model,
            current.context_window,
        )
        return {
            "provider_id": current.provider_id,
            "model": current.model,
            "context_window": current.context_window,
            "max_tokens": int(current.max_tokens or 512),
            "temperature": current.temperature,
            "reasoning_effort": getattr(current, "reasoning_effort", "") or "",
        }

    def test_provider(
        self,
        provider_id: str,
        *,
        override_model: str | None = None,
        override_context_window: int | None = None,
    ) -> dict[str, Any]:
        """Run a one-token probe chat against ``provider``.

        Returns the same shape as the existing
        ``POST /api/llm/test-connection`` response so the UI can
        reuse the same banner. The probe is built from the provider's
        own credentials (never touches the saved key on a different
        entry). ``override_model`` lets the caller test a model id
        the user is typing in the combobox before committing to save.
        """
        provider = self._find_llm_provider(provider_id)
        if provider is None:
            raise KeyError(f"unknown provider id={provider_id!r}")
        # Borrow the existing test-connection plumbing. We synthesise
        # a one-off ``ChatLlmSettings`` instance from the provider +
        # the overrides so the test path stays identical to the
        # legacy ``POST /api/llm/test-connection``.
        from app.core.infra.settings import ChatLlmSettings

        candidate_model = (override_model or "").strip()
        if not candidate_model:
            main_route = self._settings.llm.routes.get(LLM_ROLE_MAIN_CHAT)
            if main_route is not None and main_route.provider_id == provider_id:
                candidate_model = main_route.model
        probe_settings = ChatLlmSettings(
            provider=provider.kind,
            model=candidate_model,
            base_url=provider.base_url,
            api_key=provider.api_key,
            api_key_env=provider.api_key_env,
            context_window=override_context_window,
            extra_headers=dict(provider.extra_headers or {}),
            max_tokens=8,  # enough for a one-token probe
            keep_alive=provider.keep_alive,
            reasoning_effort=getattr(provider, "reasoning_effort", "") or "",
        )
        start = time.time()
        try:
            probe = _build_chat_client(
                chat_llm=probe_settings,
                ollama_settings=self._settings.ollama,
                role="test",
            )
            try:
                resp = probe.chat(
                    [{"role": "user", "content": "Reply 'ok'."}],
                    model=candidate_model,
                    options={"num_predict": 4, "temperature": 0},
                )
            finally:
                close = getattr(probe, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            latency_ms = int((time.time() - start) * 1000)
            usage = getattr(resp, "usage", None)
            completion_tokens = int(
                getattr(usage, "completion_tokens", 0) or 0
            )
            return {
                "success": True,
                "latency_ms": latency_ms,
                "completion_tokens": completion_tokens,
                "model": candidate_model,
            }
        except Exception as exc:
            return {
                "success": False,
                "error_code": exc.__class__.__name__,
                "error_message": str(exc) or "Provider rejected the request.",
                "model": candidate_model,
            }

    def client_cache_stats(self) -> dict[str, Any]:
        """Diagnostic snapshot of the shared client cache."""
        return self._client_cache.stats()

    def _mirror_route_to_chat_llm(
        self, provider: LlmProvider, route: LlmRoute,
    ) -> None:
        """Write a (provider, route) pair into the legacy ``chat_llm`` block.

        Keeps the legacy block in sync with the new catalogue so
        downstream code that still reads ``chat_llm.*`` (and external
        scripts) keeps working unchanged.
        """
        cfg = self._settings.chat_llm
        cfg.provider = provider.kind
        cfg.model = route.model
        cfg.base_url = provider.base_url
        cfg.api_key = provider.api_key
        cfg.api_key_env = provider.api_key_env
        cfg.extra_headers = dict(provider.extra_headers or {})
        cfg.keep_alive = provider.keep_alive or "30m"
        cfg.context_window = route.context_window
        cfg.max_tokens = int(route.max_tokens or 512)
        cfg.temperature = route.temperature
        cfg.reasoning_effort = (
            (getattr(route, "reasoning_effort", "") or "").strip()
            or (getattr(provider, "reasoning_effort", "") or "").strip()
        )
        # Set the UI hint to the provider id when it matches a known
        # preset (purely cosmetic — used to highlight the card).
        cfg.provider_preset = (
            provider.id if provider.id in {p["id"] for p in _PROVIDER_PRESETS} else ""
        )

    def _route_to_chat_llm_payload(
        self, provider: LlmProvider, route: LlmRoute,
    ) -> dict[str, Any]:
        """Translate a (provider, route) pair into a ``reconfigure_chat_llm``
        payload so we can reuse all the legacy cascade plumbing."""
        return {
            "provider": provider.kind,
            "provider_preset": (
                provider.id if provider.id in {p["id"] for p in _PROVIDER_PRESETS} else ""
            ),
            "model": route.model,
            "base_url": provider.base_url,
            "api_key": provider.api_key,
            "api_key_env": provider.api_key_env,
            "max_tokens": int(route.max_tokens or 512),
            "temperature": route.temperature,
            "context_window": route.context_window,
            "keep_alive": provider.keep_alive,
            "reasoning_effort": (
                (getattr(route, "reasoning_effort", "") or "").strip()
                or (getattr(provider, "reasoning_effort", "") or "").strip()
            ),
            "extra_headers": dict(provider.extra_headers or {}),
            # ``workers_use_local`` lives outside the catalogue for
            # now (a per-role concern that the new ``routes`` table
            # supersedes); keep the existing value to avoid
            # accidentally flipping it.
            "workers_use_local": bool(self._settings.chat_llm.workers_use_local),
        }

    def _sync_llm_routes_from_legacy(self) -> None:
        """Mirror ``chat_llm`` + ``ollama`` back into ``llm.routes``.

        Called at the end of :meth:`reconfigure_chat_llm` so a legacy
        PATCH against ``/api/settings`` (e.g. from an old client) still
        leaves ``llm.routes.main_chat`` consistent with the new state.
        Also runs at end of ``__init__`` so a fresh boot lands with
        the two snapshots in sync even when the migration produced a
        slightly stale shape.
        """
        chat_llm = self._settings.chat_llm
        ollama = self._settings.ollama
        # Make sure a local_ollama provider exists (it must — the
        # migration synthesises one, but a hand-edited user.json
        # could have removed it).
        local_provider = self._find_llm_provider("local_ollama")
        if local_provider is None:
            local_provider = LlmProvider(
                id="local_ollama",
                name="Local Ollama",
                kind="ollama",
                base_url=(ollama.base_url or "").strip() or "http://127.0.0.1:11434",
                api_key="",
                api_key_env="",
                extra_headers={},
                timeout_seconds=int(getattr(ollama, "timeout", 300)) or 300,
                keep_alive="30m",
            )
            self._settings.llm.providers.append(local_provider)
        else:
            # Keep base_url + timeout in sync with the legacy block.
            local_provider.base_url = (ollama.base_url or "").strip() or local_provider.base_url
            local_provider.timeout_seconds = int(getattr(ollama, "timeout", 300)) or local_provider.timeout_seconds
        # Resolve which provider main_chat points at.
        provider_id_for_chat = "local_ollama"
        if (chat_llm.provider or "").strip().lower() != "ollama" or (
            chat_llm.base_url and not _urls_match(chat_llm.base_url, local_provider.base_url)
        ):
            # Find or create a separate provider entry that matches
            # the legacy chat_llm block.
            preset_id = (chat_llm.provider_preset or "").strip().lower()
            target_id = preset_id or "chat_migrated"
            if target_id == "local_ollama":
                target_id = "chat_migrated"
            existing = self._find_llm_provider(target_id)
            if existing is None:
                kind = (chat_llm.provider or "openai_compatible").strip().lower()
                if kind not in {"ollama", "openai_compatible"}:
                    kind = "openai_compatible"
                existing = LlmProvider(
                    id=target_id,
                    name=preset_id.title() if preset_id else "Chat provider",
                    kind=kind,
                    base_url=(chat_llm.base_url or "").strip(),
                    api_key=chat_llm.api_key or "",
                    api_key_env=chat_llm.api_key_env or "",
                    extra_headers=dict(chat_llm.extra_headers or {}),
                    timeout_seconds=int(getattr(ollama, "timeout", 300)) or 300,
                    keep_alive=chat_llm.keep_alive or "30m",
                )
                self._settings.llm.providers.append(existing)
            else:
                kind = (chat_llm.provider or existing.kind).strip().lower()
                if kind in {"ollama", "openai_compatible"}:
                    existing.kind = kind
                existing.base_url = (chat_llm.base_url or existing.base_url).strip()
                existing.api_key = chat_llm.api_key or existing.api_key
                existing.api_key_env = chat_llm.api_key_env or existing.api_key_env
                existing.extra_headers = dict(chat_llm.extra_headers or existing.extra_headers or {})
                existing.keep_alive = chat_llm.keep_alive or existing.keep_alive
            provider_id_for_chat = target_id
        self._settings.llm.routes[LLM_ROLE_MAIN_CHAT] = LlmRoute(
            provider_id=provider_id_for_chat,
            model=(chat_llm.model or "").strip(),
            context_window=chat_llm.context_window,
            max_tokens=int(chat_llm.max_tokens or 512),
            temperature=chat_llm.temperature,
            reasoning_effort=(
                getattr(chat_llm, "reasoning_effort", "") or ""
            ).strip(),
        )
        # P13: the worker route is the source of truth for the worker
        # model + context. A chat-provider reconfigure must NOT clobber a
        # hand-edited worker route, so preserve an existing route's
        # model/context/budget; only seed from legacy ``ollama.*`` when
        # the route is absent or un-customised.
        existing_worker = self._settings.llm.routes.get(LLM_ROLE_WORKER_DEFAULT)
        worker_model = (ollama.chat_model or "").strip()
        worker_ctx = ollama.context_window
        worker_max_tokens = 512
        worker_temp = None
        if existing_worker is not None:
            if (getattr(existing_worker, "model", "") or "").strip():
                worker_model = existing_worker.model
            if getattr(existing_worker, "context_window", None):
                worker_ctx = existing_worker.context_window
            worker_max_tokens = int(getattr(existing_worker, "max_tokens", 512) or 512)
            worker_temp = getattr(existing_worker, "temperature", None)
        self._settings.llm.routes[LLM_ROLE_WORKER_DEFAULT] = LlmRoute(
            provider_id="local_ollama",
            model=worker_model,
            context_window=worker_ctx,
            max_tokens=worker_max_tokens,
            temperature=worker_temp,
        )
        # Preserve a customised workflow route; otherwise mirror the
        # worker route so it shares the same cached client (no VRAM).
        existing_workflow = self._settings.llm.routes.get(LLM_ROLE_WORKFLOW)
        if existing_workflow is None or not (
            (getattr(existing_workflow, "model", "") or "").strip()
        ):
            self._settings.llm.routes[LLM_ROLE_WORKFLOW] = LlmRoute(
                provider_id="local_ollama",
                model=worker_model,
                context_window=worker_ctx,
                max_tokens=worker_max_tokens,
                temperature=worker_temp,
            )

    def _chat_llm_secret_account(self) -> str:
        """Keychain account the legacy ``chat_llm`` key is filed under.

        ``chat_llm`` mirrors whatever provider ``main_chat`` points at,
        so we bind its secret to that provider's account to avoid a
        second, drift-prone copy. Falls back to a dedicated account when
        no ``main_chat`` route exists (degenerate hand-edited config).
        """
        main_route = self._settings.llm.routes.get(LLM_ROLE_MAIN_CHAT)
        if main_route is not None and (main_route.provider_id or "").strip():
            return secret_store.provider_account(main_route.provider_id)
        return secret_store.CHAT_LLM_ACCOUNT

    def _init_secret_storage(self) -> None:
        """Hydrate keys from the keychain + migrate plaintext off disk.

        Best-effort and fully guarded: any failure leaves credentials
        exactly as they were loaded from config. Inert under pytest.
        """
        if secret_store.running_under_test():
            return
        try:
            self._migrate_and_hydrate_secrets()
        except Exception:
            log.warning(
                "secret-store init failed; leaving credentials as-is",
                exc_info=True,
            )

    def _migrate_and_hydrate_secrets(self) -> None:
        moved = False
        # Catalogue providers: plaintext on disk -> keychain (migrate);
        # blank on disk -> pull from keychain into memory (hydrate).
        for provider in self._settings.llm.providers:
            account = secret_store.provider_account(provider.id)
            plaintext = (provider.api_key or "").strip()
            if plaintext:
                if secret_store.set_secret(account, plaintext):
                    moved = True
            else:
                hydrated = secret_store.get_secret(account)
                if hydrated:
                    provider.api_key = hydrated
        # Legacy chat_llm block, bound to its main_chat provider account.
        chat_llm = self._settings.chat_llm
        chat_account = self._chat_llm_secret_account()
        plaintext = (chat_llm.api_key or "").strip()
        if plaintext:
            if secret_store.set_secret(chat_account, plaintext):
                moved = True
        else:
            hydrated = secret_store.get_secret(chat_account)
            if hydrated:
                chat_llm.api_key = hydrated
        if not moved:
            return
        # We successfully stashed at least one plaintext key -> rewrite
        # ``user.json`` with the keys blanked. ``_persist_llm_settings``
        # routes provider keys through ``store_or_passthrough`` (-> "");
        # the focused merge blanks the legacy ``chat_llm.api_key``.
        try:
            self._persist_llm_settings()
        except Exception:
            log.warning(
                "secret-store: blanking provider keys on disk failed",
                exc_info=True,
            )
        try:
            persist_user_overrides({"chat_llm": {"api_key": ""}})
        except Exception:
            log.warning(
                "secret-store: blanking chat_llm key on disk failed",
                exc_info=True,
            )
        log.info(
            "secret-store: moved plaintext API key(s) from user.json into "
            "the OS keychain (backend=%s)",
            secret_store.backend_name(),
        )

    def _persist_llm_settings(self) -> None:
        """Write the catalogue + routes to ``user.json``.

        Mirrors :func:`persist_user_overrides` for the ``chat_llm``
        block. API keys are routed through
        :func:`secret_store.store_or_passthrough` — when an OS keychain
        backend is available the secret is stashed there and ``""`` is
        written to disk; when no backend exists the key falls back to
        plaintext in ``user.json`` (gitignored, fs-permission-guarded)
        so a key is never silently lost. Under pytest the passthrough is
        inert, preserving the historical plaintext-config behaviour.
        """
        providers_payload: list[dict[str, Any]] = []
        for p in self._settings.llm.providers:
            providers_payload.append({
                "id": p.id,
                "name": p.name,
                "kind": p.kind,
                "base_url": p.base_url,
                "api_key": secret_store.store_or_passthrough(
                    secret_store.provider_account(p.id), p.api_key
                ),
                "api_key_env": p.api_key_env,
                "extra_headers": dict(p.extra_headers or {}),
                "timeout_seconds": int(p.timeout_seconds or 300),
                "keep_alive": p.keep_alive,
            })
        routes_payload: dict[str, dict[str, Any]] = {}
        for role, r in self._settings.llm.routes.items():
            routes_payload[role] = {
                "provider_id": r.provider_id,
                "model": r.model,
                "context_window": r.context_window,
                "max_tokens": int(r.max_tokens or 512),
                "temperature": r.temperature,
            }
        try:
            persist_user_overrides({
                "llm": {
                    "providers": providers_payload,
                    "routes": routes_payload,
                },
            })
        except Exception:
            log.warning("persist llm overrides failed", exc_info=True)

    @staticmethod
    def provider_presets() -> list[dict[str, Any]]:
        """Return the curated preset catalogue.

        Static method — the catalogue is process-wide. Exposed via
        ``GET /api/llm/presets``.
        """
        return [dict(p) for p in _PROVIDER_PRESETS]

    def list_chat_models(
        self,
        *,
        refresh: bool = False,
        provider: str | None = None,
    ) -> list[str]:
        """Return the model identifiers visible to the active chat client.

        ``provider`` (optional) lets the UI preview a non-active
        provider's model list without committing to it — used by the
        ChatProviderSection drawer to populate the model dropdown the
        instant a user picks a different preset. When None, returns the
        cached / fresh list from ``self._chat_client``.

        Best-effort: the underlying ``list_models`` returns ``[]`` on
        failure, and we always prepend the currently configured model
        so the dropdown shows a working selection even when the
        provider's listing endpoint is down.
        """
        # Provider preview: build a throwaway client with the requested
        # provider, no api_key (the listing endpoint is usually
        # open). This is intentionally lossy — auth-gated providers
        # will just return [] and the UI falls back to a free-text
        # input. The throwaway never touches the real client state.
        if provider:
            target = provider.strip().lower()
            if target and target != (self._chat_provider or "ollama"):
                try:
                    from app.core.infra.settings import ChatLlmSettings

                    probe = _build_chat_client(
                        chat_llm=ChatLlmSettings(provider=target),
                        ollama_settings=self._settings.ollama,
                        role="probe",
                    )
                    return probe.list_models()
                except Exception:
                    log.debug(
                        "list_chat_models provider preview failed: %s",
                        target, exc_info=True,
                    )
                    return []
        now = time.monotonic()
        if not refresh and self._models_cache is not None and (now - self._models_cache_time) < self._cache_ttl:
            return list(self._models_cache)
        try:
            models = self._chat_client.list_models()
        except Exception:
            models = []
        current = self.chat_model
        if current and current not in models:
            models.insert(0, current)
        self._models_cache = list(models)
        self._models_cache_time = now
        return models
