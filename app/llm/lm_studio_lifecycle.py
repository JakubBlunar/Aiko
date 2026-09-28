"""LM Studio native model loading and readiness supervision.

Chat remains on LM Studio's OpenAI-compatible ``/v1`` API. This module uses
the native ``/api/v0/models`` and ``/api/v1/models/load`` endpoints only for
explicit, observable prewarm. Startup never waits for these calls.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests


log = logging.getLogger("app.llm.lm_studio")

_MAX_ATTEMPTS = 3
_STATUS_READ_TIMEOUT_SECONDS = 30.0
_READY_RECHECK_SECONDS = 300.0
_FAILURE_RETRY_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class LmStudioModelSpec:
    provider_id: str
    base_url: str
    api_key: str
    model: str
    context_length: int | None
    roles: tuple[str, ...]
    connect_timeout_seconds: float = 5.0
    load_timeout_seconds: float = 900.0
    required: bool = False

    @property
    def key(self) -> tuple[str, str, int | None]:
        return (self.provider_id, self.model, self.context_length)


def _native_base_url(base_url: str) -> str:
    root = (base_url or "").strip().rstrip("/")
    if root.lower().endswith("/v1"):
        root = root[:-3].rstrip("/")
    return root


class LmStudioLifecycle:
    """Reconcile configured LM Studio models on one background thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._desired: dict[tuple[str, str, int | None], LmStudioModelSpec] = {}
        self._records: dict[tuple[str, str, int | None], dict[str, Any]] = {}
        self._revision = 0
        self._thread: threading.Thread | None = None

    def reconcile(
        self, specs: list[LmStudioModelSpec], *, force: bool = False,
    ) -> None:
        """Replace desired models and start a non-blocking reconciliation."""
        if self._stop.is_set():
            return
        deduped: dict[tuple[str, str, int | None], LmStudioModelSpec] = {}
        for spec in specs:
            if not spec.provider_id or not spec.model or not spec.base_url:
                continue
            previous = deduped.get(spec.key)
            if previous is not None:
                spec = LmStudioModelSpec(
                    provider_id=spec.provider_id,
                    base_url=spec.base_url,
                    api_key=spec.api_key,
                    model=spec.model,
                    context_length=spec.context_length,
                    roles=tuple(sorted(set(previous.roles + spec.roles))),
                    connect_timeout_seconds=spec.connect_timeout_seconds,
                    load_timeout_seconds=spec.load_timeout_seconds,
                    required=previous.required or spec.required,
                )
            deduped[spec.key] = spec
        with self._condition:
            previous_desired = self._desired
            self._desired = deduped
            self._revision += 1
            for key, spec in deduped.items():
                current = self._records.get(key)
                if (
                    force
                    or previous_desired.get(key) != spec
                    or current is None
                    or current.get("status") not in {
                    "checking", "loading", "ready"
                    }
                ):
                    self._records[key] = self._record(spec, "queued")
            for key in list(self._records):
                if key not in deduped:
                    del self._records[key]
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run,
                    name="lm-studio-prewarm",
                    daemon=True,
                )
                self._thread.start()
            self._condition.notify_all()

    def status(self) -> dict[str, Any]:
        with self._lock:
            rows = [dict(row) for row in self._records.values()]
            rows.sort(key=lambda row: (row["provider_id"], row["model"]))
            return {
                "running": bool(self._thread and self._thread.is_alive()),
                "models": rows,
            }

    def provider_status(self, provider_id: str) -> dict[str, Any]:
        rows = [
            row for row in self.status()["models"]
            if row["provider_id"] == provider_id
        ]
        if not rows:
            return {"status": "disabled", "models": []}
        states = {row["status"] for row in rows}
        if states == {"ready"}:
            status = "ready"
        elif "loading" in states or "checking" in states or "queued" in states:
            status = "loading"
        elif "ready" in states:
            status = "degraded"
        elif "unreachable" in states:
            status = "unreachable"
        else:
            status = "failed"
        return {"status": status, "models": rows}

    def before_inference(self, provider_id: str, model: str) -> None:
        """Wait for an in-flight explicit load; fail fast during backoff."""
        with self._condition:
            keys = [
                key for key, spec in self._desired.items()
                if spec.provider_id == provider_id and spec.model == model
            ]
            if not keys:
                return
            deadline = time.monotonic() + max(
                self._desired[key].load_timeout_seconds for key in keys
            )
            while any(
                self._records.get(key, {}).get("status")
                in {"queued", "checking", "loading"}
                for key in keys
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._stop.is_set():
                    raise TimeoutError(f"LM Studio model {model!r} is still loading")
                self._condition.wait(timeout=remaining)
            row = next((self._records.get(key) for key in keys), None)
            if row and row.get("status") in {"failed", "unreachable"}:
                retry_at = float(row.get("retry_at_monotonic", 0.0) or 0.0)
                required = any(self._desired[key].required for key in keys)
                if (
                    row.get("status") == "failed"
                    or required
                    or time.monotonic() < retry_at
                ):
                    raise RuntimeError(
                        f"LM Studio model {model!r} unavailable: "
                        f"{row.get('error') or row.get('status')}"
                    )

    def stop(self, timeout: float = 1.5) -> None:
        self._stop.set()
        with self._condition:
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, float(timeout)))

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                revision = self._revision
                specs = list(self._desired.values())
            for spec in specs:
                if self._stop.is_set():
                    break
                self._ensure_loaded(spec, revision)
            with self._condition:
                if self._stop.is_set() or not self._desired:
                    self._thread = None
                    self._condition.notify_all()
                    return
                if revision != self._revision:
                    continue
                has_failure = any(
                    row.get("status") in {"failed", "unreachable"}
                    and bool(row.get("retryable"))
                    for row in self._records.values()
                )
                self._condition.wait(
                    timeout=(
                        _FAILURE_RETRY_SECONDS
                        if has_failure
                        else _READY_RECHECK_SECONDS
                    )
                )

    def _ensure_loaded(self, spec: LmStudioModelSpec, revision: int) -> None:
        root = _native_base_url(spec.base_url)
        headers = {"Content-Type": "application/json"}
        if spec.api_key:
            headers["Authorization"] = f"Bearer {spec.api_key}"
        last_error = ""
        unreachable = False
        retryable = False
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            self._set(spec, revision, "checking", attempt=attempt, error="")
            try:
                response = requests.get(
                    f"{root}/api/v1/models",
                    headers=headers,
                    timeout=(
                        spec.connect_timeout_seconds,
                        _STATUS_READ_TIMEOUT_SECONDS,
                    ),
                )
                if response.status_code == 404:
                    response = requests.get(
                        f"{root}/api/v0/models",
                        headers=headers,
                        timeout=(
                            spec.connect_timeout_seconds,
                            _STATUS_READ_TIMEOUT_SECONDS,
                        ),
                    )
                response.raise_for_status()
                loaded = self._loaded_model(response.json(), spec.model)
                if loaded is not None:
                    effective_context = self._context_from(loaded)
                    if (
                        spec.context_length
                        and effective_context
                        and effective_context != spec.context_length
                    ):
                        raise RuntimeError(
                            "LM Studio loaded context "
                            f"{effective_context}, expected {spec.context_length}"
                        )
                    self._set(
                        spec,
                        revision,
                        "ready",
                        instance_id=str(loaded.get("instance_id") or spec.model),
                        effective_context_length=effective_context,
                        load_time_seconds=0.0,
                    )
                    return
                self._set(spec, revision, "loading", attempt=attempt)
                payload: dict[str, Any] = {
                    "model": spec.model,
                    "echo_load_config": True,
                }
                if spec.context_length:
                    payload["context_length"] = int(spec.context_length)
                response = requests.post(
                    f"{root}/api/v1/models/load",
                    headers=headers,
                    json=payload,
                    timeout=(
                        spec.connect_timeout_seconds,
                        spec.load_timeout_seconds,
                    ),
                )
                response.raise_for_status()
                body = response.json() if response.content else {}
                load_config = body.get("load_config") if isinstance(body, dict) else {}
                effective_context = self._context_from(
                    load_config if isinstance(load_config, dict) else {}
                )
                if (
                    spec.context_length
                    and effective_context
                    and effective_context != spec.context_length
                ):
                    raise RuntimeError(
                        "LM Studio loaded context "
                        f"{effective_context}, expected {spec.context_length}"
                    )
                self._set(
                    spec,
                    revision,
                    "ready",
                    instance_id=str(
                        body.get("instance_id") or spec.model
                        if isinstance(body, dict) else spec.model
                    ),
                    effective_context_length=effective_context,
                    load_time_seconds=float(
                        body.get("load_time_seconds", 0.0) or 0.0
                        if isinstance(body, dict) else 0.0
                    ),
                )
                return
            except requests.RequestException as exc:
                last_error = str(exc)[:300]
                unreachable = True
                response = getattr(exc, "response", None)
                status = int(getattr(response, "status_code", 0) or 0)
                retryable = not status or status >= 500 or status in {408, 429}
                unreachable = not status
                if status and status < 500 and status not in {408, 429}:
                    break
            except (RuntimeError, TypeError, ValueError) as exc:
                last_error = str(exc)[:300]
                break
            if attempt < _MAX_ATTEMPTS and self._stop.wait(2 ** (attempt - 1)):
                return
        self._set(
            spec,
            revision,
            "unreachable" if unreachable else "failed",
            error=last_error or "LM Studio prewarm failed",
            retryable=retryable,
            retry_at_monotonic=(
                time.monotonic() + _FAILURE_RETRY_SECONDS if retryable else 0.0
            ),
        )

    @staticmethod
    def _loaded_model(body: Any, model: str) -> dict[str, Any] | None:
        models = body.get("models") if isinstance(body, dict) else None
        if isinstance(models, list):
            for item in models:
                if not isinstance(item, dict):
                    continue
                aliases = {
                    str(item.get("key", "")).strip(),
                    str(item.get("selected_variant", "")).strip(),
                }
                variants = item.get("variants")
                if isinstance(variants, list):
                    aliases.update(str(value).strip() for value in variants)
                if model not in aliases:
                    continue
                instances = item.get("loaded_instances")
                if not isinstance(instances, list) or not instances:
                    return None
                instance = instances[0]
                if not isinstance(instance, dict):
                    return None
                config = instance.get("config")
                loaded = dict(config) if isinstance(config, dict) else {}
                loaded["instance_id"] = str(instance.get("id", "") or model)
                return loaded
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            if str(item.get("id", "")).strip() != model:
                continue
            if str(item.get("state", "")).strip().lower() == "loaded":
                return item
        return None

    @staticmethod
    def _context_from(payload: dict[str, Any]) -> int | None:
        for key in ("context_length", "loaded_context_length"):
            try:
                value = int(payload.get(key, 0) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return None

    @staticmethod
    def _record(spec: LmStudioModelSpec, status: str) -> dict[str, Any]:
        return {
            "provider_id": spec.provider_id,
            "model": spec.model,
            "context_length": spec.context_length,
            "roles": list(spec.roles),
            "required": spec.required,
            "status": status,
            "attempt": 0,
            "error": "",
            "instance_id": "",
            "effective_context_length": None,
            "load_time_seconds": None,
            "updated_at_monotonic": time.monotonic(),
            "retry_at_monotonic": 0.0,
            "retryable": False,
        }

    def _set(
        self,
        spec: LmStudioModelSpec,
        revision: int,
        status: str,
        **updates: Any,
    ) -> None:
        with self._condition:
            if self._revision != revision or self._desired.get(spec.key) != spec:
                return
            row = self._records.setdefault(spec.key, self._record(spec, status))
            row.update(updates)
            row["status"] = status
            row["updated_at_monotonic"] = time.monotonic()
            self._condition.notify_all()
        log.info(
            "lm-studio prewarm: provider=%s model=%s status=%s attempt=%s",
            spec.provider_id,
            spec.model,
            status,
            row.get("attempt", 0),
        )


__all__ = ["LmStudioLifecycle", "LmStudioModelSpec"]
