"""LLM settings / provider catalogue / secrets mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
provider catalogue + route CRUD, secret migration/hydration, and
LLM-settings persistence. State ownership stays on
``SessionController.__init__``.

:meth:`LlmSettingsMixin.update_route` is the single mutation path for
"which model serves this role": it validates the draft, persists the
catalogue, and — for ``main_chat`` / ``worker_default`` — rebuilds the
live clients and cascades the change to the TurnRunner, the proactive
director and every background worker.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for a moved method must patch
``app.core.session.llm_settings_mixin.<symbol>`` instead (notably
``persist_user_overrides`` and ``secret_store``)."""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from app.llm.chat_client import ChatClient
from app.llm.ollama_client import OllamaClient
from app.core.infra.settings import local_ollama_provider
from app.core.infra.settings import transport_for_provider
from app.core.infra.settings import LLM_ROLE_MAIN_CHAT
from app.core.infra.settings import LLM_ROLE_WORKER_DEFAULT
from app.core.infra.settings import LLM_ROLE_WORKFLOW
from app.core.infra.settings import LlmProvider
from app.core.infra.settings import LlmRoute
from app.core.infra.settings import llm_provider_to_dict
from app.core.infra.settings import llm_route_to_dict
from app.core.infra.settings import _norm_api_style
from app.core.infra.settings import persist_user_overrides
from app.core.infra import secret_store
from app.llm.factory import build_probe_client
import time
import uuid
from app.core.session.llm_presets import _PROVIDER_PRESETS


log = logging.getLogger("app.session")


class LlmSettingsMixin:
    """Provider catalogue + route CRUD + secrets."""

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
            "api_style": getattr(provider, "api_style", "auto") or "auto",
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
                        "api_style": preset.get("api_style", "auto"),
                        "reasoning_effort": preset.get(
                            "default_reasoning_effort", "",
                        ),
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
        api_style = _norm_api_style(payload.get("api_style"))
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
            api_style=api_style,
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
        if "api_style" in draft:
            provider.api_style = _norm_api_style(draft["api_style"])
        # Anything changed -> drop the cached client so future
        # ``cache.get`` rebuilds with the new fields.
        self._client_cache.invalidate(provider_id)
        self._persist_llm_settings()
        # Rebuild whatever this provider serves so the next turn picks
        # up the new base_url / headers / timeout immediately.
        if self._provider_is_live(provider_id):
            self._rebuild_llm_clients()
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
        self._persist_llm_settings()
        if self._provider_is_live(provider_id):
            self._rebuild_llm_clients()
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

        The single mutation path for role assignments. Unspecified
        keys keep their current value, so a caller that only wants to
        change the model can't accidentally reset the context window.

        After validating the draft against the catalogue the route is
        persisted, and the live topology is rebuilt when the role has
        running clients behind it: ``main_chat`` re-points the
        TurnRunner and proactive director, ``worker_default`` re-points
        the ~24 background workers, and ``workflow`` re-resolves the
        nested-workflow client. Editing a role therefore takes effect
        immediately instead of at the next restart.
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
        self._persist_llm_settings()
        if role_name in {
            LLM_ROLE_MAIN_CHAT, LLM_ROLE_WORKER_DEFAULT, LLM_ROLE_WORKFLOW,
        }:
            self._rebuild_llm_clients()
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

    def _provider_is_live(self, provider_id: str) -> bool:
        """True when any route currently resolves to ``provider_id``."""
        return any(
            route.provider_id == provider_id
            for route in self._settings.llm.routes.values()
        )

    def _rebuild_llm_clients(self) -> None:
        """Re-derive every live client from the current route table.

        Mirrors the construction order in ``SessionController.__init__``
        so a route edit lands in exactly the state a restart would
        produce. The gated worker proxies are mutated in place by
        ``_install_worker_clients``, so the ~24 worker instances already
        holding a reference follow the new topology without being
        rebuilt themselves.
        """
        chat_route = self._route_or_none(LLM_ROLE_MAIN_CHAT)
        worker_route = self._route_or_none(LLM_ROLE_WORKER_DEFAULT)
        self._chat_client = self._build_route_client(
            chat_route, role=LLM_ROLE_MAIN_CHAT,
        )
        if worker_route is None or self._routes_share_client(
            chat_route, worker_route,
        ):
            raw_worker_client: ChatClient = self._chat_client
        else:
            raw_worker_client = self._build_worker_client()
        self._install_worker_clients(raw_worker_client)
        provider = (
            self._find_llm_provider(chat_route.provider_id)
            if chat_route is not None
            else None
        )
        self._chat_provider = provider.kind if provider is not None else "ollama"
        if self._worker_client_inner is self._chat_client:
            self._effective_worker_model = (
                chat_route.model if chat_route else ""
            ).strip() or "llama3.1:8b"
        else:
            self._effective_worker_model, _ = self._worker_route_model_ctx()
        # Drop the model-listing cache so the next /api/models is fresh.
        self._models_cache = None
        for target, label in (
            (getattr(self, "_turn_runner", None), "turn_runner"),
            (getattr(self, "_proactive", None), "proactive"),
        ):
            update = getattr(target, "update_runtime", None)
            if not callable(update):
                continue
            try:
                update(client=self._chat_client)
            except Exception:
                log.debug("%s update_runtime(client=) failed", label, exc_info=True)
        # Cascades the model + context window to the turn runner, the
        # proactive director and every registered worker.
        self.set_chat_model(
            (chat_route.model if chat_route else "").strip() or "llama3.1:8b",
        )
        self._refresh_missing_chat_model()
        log.info(
            "llm clients rebuilt: chat=%s/%s workers=%s shared=%s",
            self._chat_provider,
            self._effective_chat_model,
            self._effective_worker_model,
            "1" if self._worker_client_inner is self._chat_client else "0",
        )

    def _refresh_missing_chat_model(self) -> None:
        """Re-evaluate the boot-time "chat model isn't installed" flag.

        ``prewarm_runtime`` sets it once at boot; a route edit can both
        fix it (the user picked something they already have) and cause
        it (they typed a tag that was never pulled). Without this the
        onboarding banner would keep naming the model they just moved
        away from. Best-effort: an unreachable provider leaves the flag
        as-is rather than inventing a verdict.
        """
        if (self._chat_provider or "ollama") != "ollama":
            self._missing_chat_model = ""
            return
        model = (self._effective_chat_model or "").strip()
        if not model or model.endswith("-cloud") or model.endswith(":cloud"):
            self._missing_chat_model = ""
            return
        try:
            installed = self._chat_client.list_models()
        except Exception:
            log.debug("missing-model refresh: list_models failed", exc_info=True)
            return
        self._missing_chat_model = "" if model in installed else model

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
        candidate_model = (override_model or "").strip()
        if not candidate_model:
            main_route = self._settings.llm.routes.get(LLM_ROLE_MAIN_CHAT)
            if main_route is not None and main_route.provider_id == provider_id:
                candidate_model = main_route.model
        start = time.time()
        try:
            # Uncached on purpose: the drawer tests credentials the user
            # has not saved yet, and caching a client built from a wrong
            # key would poison the entry the live routes share.
            probe = build_probe_client(provider, model=candidate_model)
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
        moved = self._adopt_retired_chat_llm_secret()
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
        if not moved:
            return
        # We successfully stashed at least one plaintext key -> rewrite
        # ``user.json`` with the keys blanked. ``_persist_llm_settings``
        # routes provider keys through ``store_or_passthrough`` (-> "").
        try:
            self._persist_llm_settings()
        except Exception:
            log.warning(
                "secret-store: blanking provider keys on disk failed",
                exc_info=True,
            )
        log.info(
            "secret-store: moved plaintext API key(s) from user.json into "
            "the OS keychain (backend=%s)",
            secret_store.backend_name(),
        )

    def _adopt_retired_chat_llm_secret(self) -> bool:
        """Re-file the retired ``chat_llm`` keychain entry, if any.

        Before the config consolidation the main chat key could live
        under its own keychain account. The provider catalogue is now
        the only holder of credentials, so move that secret onto the
        account of whichever provider ``main_chat`` points at — without
        it, an upgrading user whose key was already in the keychain
        would silently lose it. Returns True when a key was adopted, so
        the caller re-persists the (blanked) config.
        """
        legacy = secret_store.get_secret(secret_store.CHAT_LLM_ACCOUNT)
        if not legacy:
            return False
        route = self._settings.llm.routes.get(LLM_ROLE_MAIN_CHAT)
        provider = (
            self._find_llm_provider(route.provider_id)
            if route is not None
            else None
        )
        if provider is None:
            return False
        if not (provider.api_key or "").strip():
            provider.api_key = legacy
        secret_store.set_secret(
            secret_store.provider_account(provider.id), provider.api_key,
        )
        secret_store.delete_secret(secret_store.CHAT_LLM_ACCOUNT)
        log.info(
            "secret-store: adopted the retired chat_llm key onto provider %s",
            provider.id,
        )
        return True

    def _persist_llm_settings(self) -> None:
        """Write the catalogue + routes + embedding to ``user.json``.

        API keys are routed through
        :func:`secret_store.store_or_passthrough` — when an OS keychain
        backend is available the secret is stashed there and ``""`` is
        written to disk; when no backend exists the key falls back to
        plaintext in ``user.json`` (gitignored, fs-permission-guarded)
        so a key is never silently lost. Under pytest the passthrough is
        inert, preserving the historical plaintext-config behaviour.
        """
        llm = self._settings.llm
        providers_payload = [
            llm_provider_to_dict(
                p,
                api_key=secret_store.store_or_passthrough(
                    secret_store.provider_account(p.id), p.api_key,
                ),
            )
            for p in llm.providers
        ]
        routes_payload = {
            role: llm_route_to_dict(route) for role, route in llm.routes.items()
        }
        try:
            persist_user_overrides({
                "llm": {
                    "providers": providers_payload,
                    "routes": routes_payload,
                    "embedding": {
                        "provider_id": llm.embedding.provider_id,
                        "model": llm.embedding.model,
                        "num_ctx": llm.embedding.num_ctx,
                        "num_gpu": llm.embedding.num_gpu,
                    },
                },
            })
        except Exception:
            log.warning("persist llm overrides failed", exc_info=True)

    # ── Model installation (Ollama pull) ────────────────────────────

    def required_models(self) -> dict[str, Any]:
        """Report which locally-hosted models the current config needs.

        Drives the first-run model step: unlike
        :meth:`list_chat_models` (which prepends the configured model so
        the drawer's dropdown always has a selection) this reports the
        provider's *actual* inventory, so "configured but not pulled
        yet" is visible rather than papered over.

        Only Ollama routes are reported — a hosted provider has nothing
        to download. ``installed`` is the raw tag list of the local
        provider; ``[]`` when it can't be reached.
        """
        llm = self._settings.llm
        local = local_ollama_provider(llm)
        installed: list[str] = []
        reachable = False
        if local is not None:
            try:
                installed = build_probe_client(local).list_models()
                reachable = True
            except Exception:
                log.debug(
                    "required_models: provider %s unreachable",
                    local.id, exc_info=True,
                )
        have = set(installed)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()

        def _add(role: str, model: str, provider_id: str) -> None:
            name = (model or "").strip()
            if not name or name in seen:
                return
            provider = self._find_llm_provider(provider_id)
            if provider is None or provider.kind != "ollama":
                return
            seen.add(name)
            rows.append({
                "role": role,
                "model": name,
                "provider_id": provider.id,
                "installed": name in have,
            })

        for role in (
            LLM_ROLE_MAIN_CHAT, LLM_ROLE_WORKER_DEFAULT, LLM_ROLE_WORKFLOW,
        ):
            route = llm.routes.get(role)
            if route is not None:
                _add(role, route.model, route.provider_id)
        _add("embedding", llm.embedding.model, llm.embedding.provider_id)
        return {
            "provider_id": local.id if local is not None else "",
            "base_url": local.base_url if local is not None else "",
            "reachable": reachable,
            "installed": installed,
            "required": rows,
        }

    def validate_pull_target(
        self, model: str, *, provider_id: str = "",
    ) -> LlmProvider:
        """Resolve + check the provider a pull would target.

        Split out of :meth:`pull_model` so the REST layer can reject a
        bad request synchronously with a 4xx instead of accepting it and
        reporting the failure asynchronously. Raises ``ValueError`` for
        a blank model or a provider that hosts its own models, and
        ``KeyError`` for an unknown provider id.
        """
        if not (model or "").strip():
            raise ValueError("model must be a non-empty string")
        target_id = (provider_id or "").strip()
        provider = (
            self._find_llm_provider(target_id)
            if target_id
            else local_ollama_provider(self._settings.llm)
        )
        if provider is None:
            raise KeyError(f"unknown provider id={target_id!r}")
        if provider.kind != "ollama":
            raise ValueError(
                f"provider {provider.id!r} is {provider.kind}, which hosts its "
                "own models — nothing to pull",
            )
        return provider

    def pull_model(
        self,
        model: str,
        *,
        provider_id: str = "",
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Download ``model`` onto an Ollama provider, reporting progress.

        Blocking — a multi-GB pull takes minutes, so the REST layer runs
        this on a worker thread. ``on_progress`` receives dicts shaped
        like the ``model_pull_progress`` WS event so the caller can
        forward them straight to the browser.
        """
        name = (model or "").strip()
        provider = self.validate_pull_target(name, provider_id=provider_id)
        client = OllamaClient(
            transport_for_provider(provider),
            base_url=provider.base_url,
        )

        def _emit(status: str, completed: int, total: int) -> None:
            if on_progress is None:
                return
            on_progress({
                "model": name,
                "status": status,
                "completed": completed,
                "total": total,
                "percent": (
                    round(completed * 100.0 / total, 1) if total > 0 else None
                ),
            })

        try:
            client.pull_model(name, on_progress=_emit)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        # The model list and the "model missing" flag are both stale now.
        self._models_cache = None
        if name == self._effective_chat_model:
            self._missing_chat_model = ""

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

        ``provider`` (optional) is a catalogue provider *id*: it lets the
        settings drawer and the first-run model picker preview another
        provider's model list without committing to it. When None,
        returns the cached / fresh list from ``self._chat_client``.

        Best-effort: the underlying ``list_models`` returns ``[]`` on
        failure, and we always prepend the currently configured model
        so the dropdown shows a working selection even when the
        provider's listing endpoint is down.
        """
        target_id = (provider or "").strip()
        main_route = self._settings.llm.routes.get(LLM_ROLE_MAIN_CHAT)
        active_id = main_route.provider_id if main_route is not None else ""
        if target_id and target_id != active_id:
            entry = self._find_llm_provider(target_id)
            if entry is None:
                return []
            try:
                return build_probe_client(entry).list_models()
            except Exception:
                log.debug(
                    "list_chat_models: preview of provider %s failed",
                    target_id, exc_info=True,
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
