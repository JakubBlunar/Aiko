"""Generic runtime/settings ownership. Bundle-specific fields and rules stay in bundles."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
import logging
import threading
from typing import Any, Callable
import uuid

from app.plugins.loader import discover_plugins, PluginStub
from app.plugins.runtime import activate_plugin, ActivatedPlugin
from app.plugins.settings_contract import PluginSettings
from app.plugins.settings_validation import activation_problem, validate_settings

log = logging.getLogger("app.plugins.manager")


class PluginConflict(ValueError):
    pass


class PluginValidation(ValueError):
    def __init__(self, errors):
        self.errors = errors
        super().__init__(errors[0]["message"] if errors else "Invalid plugin settings")


def _merge(base, patch):
    result = deepcopy(base)
    for key, value in patch.items():
        result[key] = (
            _merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else deepcopy(value)
        )
    return result


@dataclass
class _Plugin:
    stub: PluginStub
    phase: str = "disabled"
    error: str = ""
    generation: str = ""
    applied_revision: str | None = None
    active: ActivatedPlugin | None = None
    activation_lock: Any = field(default_factory=threading.Lock)
    install_lock: Any = field(default_factory=threading.RLock)


class PluginManager:
    def __init__(
        self,
        *,
        roots: list[str],
        entries: Callable[[], dict],
        master: Callable[[], bool],
        persist: Callable[[str | None, dict], None],
        transport: Any,
        publish: Callable[[], None],
        deps_root=None,
        activate=activate_plugin,
    ):
        self._roots, self._entries, self._master = roots, entries, master
        self._persist, self._transport, self._publish = persist, transport, publish
        self._deps_root, self._activate = deps_root, activate
        self._lock = threading.RLock()
        self._closed = False
        self._listeners: list[Callable] = []
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="plugin-activate")
        self._lifecycle = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plugin-dispose")
        self._plugins = {stub.id: _Plugin(stub) for stub in self._discover()}

    def _discover(self):
        return discover_plugins(self._roots, entries=self._entries())

    def start(self):
        for pid, record in list(self._plugins.items()):
            if record.stub.enabled:
                self._schedule(pid)

    def add_listener(self, callback):
        with self._lock:
            self._listeners.append(callback)

    def transport_changed(self):
        """Notify settings clients after connection/catalogue changes."""
        self._changed("")

    def _changed(self, pid):
        for callback in tuple(self._listeners):
            try:
                callback({"plugin_id": pid})
            except Exception:
                log.debug("plugin state listener failed", exc_info=True)

    def _revision(self, record):
        return hashlib.sha256(
            json.dumps(
                [record.stub.enabled, record.stub.config, bool(self._master())],
                sort_keys=True,
                ensure_ascii=False,
            ).encode()
        ).hexdigest()

    def snapshot(self):
        with self._lock:
            statuses = {row["id"]: row for row in self._transport.server_status()}
            rows = []
            for pid, record in self._plugins.items():
                spec = record.stub.settings or PluginSettings()
                transport = statuses.get(pid)
                phase, error = record.phase, record.error
                if record.active and record.active.server is not None:
                    status = (transport or {}).get("status")
                    phase = (
                        "ready"
                        if status == "connected" and (transport or {}).get("available", True)
                        else ("error" if status == "failed" else "starting")
                    )
                    error = (transport or {}).get("error") or error
                if not self._master():
                    phase = "blocked"
                    error = "The plugin system is disabled"
                access = []
                public_config = spec.public_config(record.stub.config)
                for declaration in spec.ui.get("resources", []):
                    values = public_config.get(declaration.get("field"), [])
                    for value in values if isinstance(values, list) else []:
                        access.append(
                            {
                                "name": value.get(declaration.get("name_field", "name"), "")
                                if isinstance(value, dict)
                                else "",
                                "path": value.get(declaration.get("path_field", "root"), "")
                                if isinstance(value, dict)
                                else value,
                                "mode": declaration.get("mode", "access"),
                            }
                        )
                        if isinstance(value, dict) and declaration.get("scope_field"):
                            access[-1]["scope"] = value.get(declaration["scope_field"], [])
                access = [item for item in access if isinstance(item["path"], str) and item["path"]]
                for item in access:
                    item["name"] = str(item["name"]) if item["name"] is not None else ""
                    if "scope" in item:
                        item["scope"] = (
                            [value for value in item["scope"] if isinstance(value, str)]
                            if isinstance(item["scope"], list)
                            else []
                        )
                revision = self._revision(record)
                rows.append(
                    {
                        "id": pid,
                        "name": record.stub.name,
                        "enabled": record.stub.enabled,
                        "phase": phase,
                        "error": error,
                        "revision": revision,
                        "applied_revision": record.applied_revision,
                        "generation": record.generation,
                        "schema": spec.schema,
                        "ui": spec.ui,
                        "config": public_config,
                        "access": access,
                        "tool_count": (transport or {}).get("tool_count", 0)
                        if phase == "ready"
                        else 0,
                        "warnings": record.stub.warnings,
                    }
                )
            return {"enabled": bool(self._master()), "plugins": rows}

    def update(self, pid, *, expected_revision, config=None, enabled=None, reset=False):
        with self._lock:
            if self._closed:
                raise ValueError("Plugin runtime is stopping")
            record = self._plugins.get(pid)
            if record is None:
                raise KeyError(pid)
            if expected_revision != self._revision(record):
                raise PluginConflict("Plugin settings changed; refresh before applying this draft")
            if enabled is not None and type(enabled) is not bool:
                raise ValueError("enabled must be a boolean")
            old = self._entries().get(pid, {})
            entry = deepcopy(old)
            if enabled is not None:
                entry["enabled"] = enabled
            if reset:
                entry["config"] = {}
            elif config is not None:
                if not isinstance(config, dict) or len(json.dumps(config)) > 65536:
                    raise ValueError("Provide a bounded plugin configuration object")
                spec = record.stub.settings or PluginSettings()
                unknown = set(config) - set(spec.schema.get("properties", {}))
                if unknown:
                    raise ValueError("Only settings exposed by this plugin may be edited")
                effective = spec.merge_config(record.stub.config, config)
                errors = validate_settings(spec, effective)
                if errors:
                    raise PluginValidation(errors)
                # Persist just the edited top-level fields, plus omitted secret values
                # carried from their existing resource rows.
                entry["config"] = _merge(
                    entry.get("config", {}), {key: effective[key] for key in config}
                )
            self._persist(pid, entry)
            fresh = next(stub for stub in self._discover() if stub.id == pid)
            record.stub = fresh
            prepared = self._invalidate(pid)
        self._schedule(pid, prepared)
        return next(row for row in self.snapshot()["plugins"] if row["id"] == pid)

    def set_master(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("enabled must be a boolean")
        with self._lock:
            if self._closed:
                raise ValueError("Plugin runtime is stopping")
            self._persist(None, {"enabled": enabled})
            prepared = [(pid, self._invalidate(pid)) for pid in self._plugins]
        for pid, change in prepared:
            self._schedule(pid, change)
        return self.snapshot()

    def retry(self, pid, revision):
        return self.update(pid, expected_revision=revision)

    def _current(self, pid, generation):
        return not self._closed and self._plugins[pid].generation == generation

    def _invalidate(self, pid):
        # Called under the settings lock, so observers cannot see a new desired
        # configuration while the old generation is still admitted.
        record = self._plugins[pid]
        old, record.active = record.active, None
        generation = uuid.uuid4().hex
        record.generation = generation
        record.applied_revision = None
        record.error = ""
        record.phase = "applying"
        self._transport.block_server(pid)
        return record, old, generation, record.stub

    def _schedule(self, pid, prepared=None):
        with self._lock:
            record, old, generation, stub = prepared or self._invalidate(pid)
        self._publish()
        self._changed(pid)

        def dispose():
            try:
                with record.install_lock:
                    if self._current(pid, generation):
                        self._transport.replace_server(pid, None)
            except Exception as exc:
                self._fail(pid, generation, str(exc))
                return
            finally:
                if old:
                    self._dispose(old)
            with self._lock:
                if not self._current(pid, generation):
                    return
                if not stub.enabled or not self._master():
                    record.phase = "disabled"
                    self._changed(pid)
                    return
            with self._lock:
                if self._current(pid, generation):
                    self._workers.submit(activate)

        def activate():
            with record.activation_lock:
                if not self._current(pid, generation):
                    return
                if stub.settings_error:
                    self._fail(pid, generation, stub.settings_error)
                    return
                problem = activation_problem(stub.settings or PluginSettings(), stub.config)
                if problem:
                    self._fail(pid, generation, problem, phase="needs_configuration")
                    return

                def stage(value):
                    with self._lock:
                        if self._current(pid, generation):
                            record.phase = value
                    self._changed(pid)

                plugin = None
                try:
                    plugin = self._activate(
                        stub,
                        deps_root=self._deps_root,
                        on_stage=stage,
                        cancelled=lambda: not self._current(pid, generation),
                    )
                    with self._lock:
                        current = self._current(pid, generation)
                    if not current:
                        self._dispose(plugin)
                        return
                    if plugin.status != "active":
                        self._dispose(plugin)
                        self._fail(
                            pid,
                            generation,
                            plugin.reason,
                            phase=(
                                "needs_configuration" if plugin.status == "gated_out" else "error"
                            ),
                        )
                        return
                    with record.install_lock:
                        if not self._current(pid, generation):
                            self._dispose(plugin)
                            return
                        if plugin.server is not None:
                            self._transport.replace_server(
                                pid, plugin.server, generation=generation, expose=False
                            )
                        with self._lock:
                            current = self._current(pid, generation)
                            if current:
                                record.active = plugin
                                record.phase = "starting" if plugin.server is not None else "ready"
                                record.applied_revision = self._revision(record)
                        if not current:
                            self._transport.block_server(pid)
                            self._transport.replace_server(pid, None)
                            self._dispose(plugin)
                            return
                    self._publish()
                    with self._lock:
                        if self._current(pid, generation) and plugin.server is not None:
                            self._transport.release_server(pid, generation)
                    self._changed(pid)
                except Exception as exc:
                    if plugin is not None:
                        self._dispose(plugin)
                    self._fail(pid, generation, str(exc))

        with self._lock:
            if self._closed:
                if old:
                    self._dispose(old)
            else:
                self._lifecycle.submit(dispose)

    def _fail(self, pid, generation, error, *, phase="error"):
        with self._lock:
            if not self._current(pid, generation):
                return
            record = self._plugins[pid]
            record.active = None
            record.phase, record.error = phase, str(error)[:500]
        self._transport.block_server(pid)
        try:
            self._publish()
        except Exception:
            log.warning("Plugin error publication failed", exc_info=True)
        self._changed(pid)

        def retire_failed_service():
            with record.install_lock:
                if self._current(pid, generation):
                    try:
                        self._transport.replace_server(pid, None)
                    except Exception:
                        log.warning("Failed plugin transport cleanup", exc_info=True)

        with self._lock:
            if not self._closed:
                self._lifecycle.submit(retire_failed_service)

    @staticmethod
    def _dispose(plugin):
        callbacks, plugin.cleanups = plugin.cleanups, []
        for callback in reversed(callbacks):
            try:
                callback()
            except Exception:
                log.warning("Plugin cleanup failed", exc_info=True)

    def active_plugins(self):
        with self._lock:
            return [
                (pid, record.generation, record.active)
                for pid, record in self._plugins.items()
                if record.active is not None and self._master()
            ]

    def activations(self):
        """Compatibility debug view includes disabled/gated bundles as well."""
        with self._lock:
            return [
                record.active
                or ActivatedPlugin(
                    id=pid,
                    name=record.stub.name,
                    root=record.stub.root,
                    enabled=record.stub.enabled,
                    status={"error": "invalid", "needs_configuration": "gated_out"}.get(
                        record.phase, record.phase
                    ),
                    reason=record.error,
                    warnings=list(record.stub.warnings),
                )
                for pid, record in self._plugins.items()
            ]

    def guard(self, pid, generation):
        with self._lock:
            return (
                self._current(pid, generation)
                and self._master()
                and self._plugins[pid].active is not None
            )

    def close(self):
        with self._lock:
            self._closed = True
            active = [record.active for record in self._plugins.values() if record.active]
            for record in self._plugins.values():
                record.active = None
        for plugin in active:
            self._transport.block_server(plugin.id)
            self._dispose(plugin)
        self._workers.shutdown(wait=False, cancel_futures=True)
        self._lifecycle.shutdown(wait=False, cancel_futures=False)
