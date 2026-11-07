"""Thin session integration for the generic plugin runtime."""

from __future__ import annotations

from dataclasses import replace
import logging
import threading
from types import SimpleNamespace

log = logging.getLogger("app.session.plugins")


class PluginSettingsMixin:
    @property
    def plugins(self):
        return getattr(self, "_plugin_runtime", None)

    def _plugin_entries(self):
        return {
            pid: {"enabled": entry.enabled, "config": dict(entry.config)}
            for pid, entry in self._settings.plugins.entries.items()
        }

    def _persist_plugin_setting(self, pid, value):
        from app.core.infra.settings import PluginEntry, replace_user_override

        if pid is None:
            replace_user_override(("plugins", "enabled"), value["enabled"])
            self._settings.plugins.enabled = value["enabled"]
        else:
            replace_user_override(("plugins", "entries", pid), value)
            self._settings.plugins.entries[pid] = PluginEntry(**value)

    def _start_plugin_runtime(self, agent, skill_registry):
        from app.core.tasks.handlers.mcp_tool import McpToolHandler
        from app.core.tasks.workflow.mcp_skills import register_mcp_skills
        from app.mcp.client.manager import ExternalMcpManager
        from app.plugins.loader import default_plugin_roots, discover_plugins
        from app.plugins.manager import PluginManager

        self._plugin_publish_lock = threading.RLock()
        self._plugin_runtime = None
        self._external_mcp_manager = None
        self._plugin_skill_registry = skill_registry
        self._reset_plugin_registrations()
        roots = default_plugin_roots() + list(self._settings.plugins.paths)
        # Discovered plugin ids reserve ownership even while disabled: a legacy
        # same-id server must never bypass a plugin's disable switch.
        ids = {stub.id for stub in discover_plugins(roots, entries=self._plugin_entries())}
        permitted = bool(getattr(agent, "mcp_clients_enabled", True))
        servers = (
            [
                server
                for server in self._settings.mcp_clients.servers
                if server.enabled and server.id not in ids
            ]
            if permitted
            else []
        )
        self._legacy_mcp_servers = {server.id: server for server in servers}
        self._active_mcp_servers = dict(self._legacy_mcp_servers)
        manager = ExternalMcpManager(servers)
        self._external_mcp_manager = manager

        def catalogue_changed():
            register_mcp_skills(skill_registry, manager)
            if self.plugins is not None:
                self.plugins.transport_changed()

        manager.set_tools_changed_callback(catalogue_changed)
        orchestrator = getattr(self, "_task_orchestrator", None)
        if orchestrator is not None:
            orchestrator.register_handler(
                McpToolHandler(
                    manager=manager,
                    middlewares_provider=lambda: self._plugin_middlewares,
                    resolve_approval=self._resolve_task_approval,
                    mark_session_approved=self._mark_capability_session_approved,
                    can_dispatch=orchestrator.claim_operation_dispatch,
                )
            )
        manager.start()
        self._plugin_runtime = PluginManager(
            roots=roots,
            entries=self._plugin_entries,
            master=lambda: self._settings.plugins.enabled and permitted,
            persist=self._persist_plugin_setting,
            transport=manager,
            publish=self._publish_plugins,
        )
        self._plugin_runtime.start()

    def _reset_plugin_registrations(self):
        self._loaded_plugins = []
        self._mcp_plugin_guidance = {}
        self._plugin_middlewares = []
        self._plugin_fast_tools = []
        self._plugin_tool_families = {}
        self._plugin_family_patterns = {}
        self._plugin_family_pattern_strs = {}
        self._browser_perception = None

    def _publish_plugins(self):
        runtime = self.plugins
        if runtime is None:
            return
        with self._plugin_publish_lock:
            loaded, guidance, middlewares, fast_tools = [], {}, [], []
            collection = SimpleNamespace(
                _plugin_fast_tools=[],
                _plugin_tool_families={},
                _plugin_family_patterns={},
                _plugin_family_pattern_strs={},
            )
            servers = dict(self._legacy_mcp_servers)
            for pid, generation, plugin in runtime.active_plugins():
                loaded.append(plugin)
                guidance.update(plugin.group_guidance)
                middlewares.extend(plugin.middlewares)
                for spec in plugin.fast_tools:

                    def guarded(*args, _pid=pid, _gen=generation, _fn=spec.handler, **kwargs):
                        if not runtime.guard(_pid, _gen):
                            raise RuntimeError(
                                "Plugin configuration changed; request this tool again"
                            )
                        result = _fn(*args, **kwargs)
                        if not runtime.guard(_pid, _gen):
                            raise RuntimeError("Plugin configuration changed during this operation")
                        return result

                    fast_tools.append(replace(spec, handler=guarded))
                if plugin.server is not None:
                    servers[pid] = plugin.server
            self._collect_plugin_fast_tools(fast_tools, target=collection)
            # Build privately before swapping complete lists: a concurrent tool
            # dispatch must never capture an empty/half-built middleware chain.
            self._loaded_plugins = loaded
            self._mcp_plugin_guidance = guidance
            self._plugin_middlewares = middlewares
            self._plugin_fast_tools = collection._plugin_fast_tools
            self._plugin_tool_families = collection._plugin_tool_families
            self._plugin_family_patterns = collection._plugin_family_patterns
            self._plugin_family_pattern_strs = collection._plugin_family_pattern_strs
            self._active_mcp_servers = servers
            self._browser_perception = next(
                (mw for mw in self._plugin_middlewares if hasattr(mw, "debug_state")), None
            )
            if getattr(self, "_turn_runner", None) is not None:
                self.rebuild_tool_registry()

    def reload_plugin_guidance(self):
        """Reapply bundle definitions through the same managed lifecycle."""
        runtime = self.plugins
        if runtime is None:
            return {"enabled": False, "reason": "plugin runtime unavailable"}
        snapshot = runtime.snapshot()
        for row in snapshot["plugins"]:
            if row["enabled"]:
                runtime.retry(row["id"], row["revision"])
        return runtime.snapshot()
