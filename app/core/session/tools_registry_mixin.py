"""Agent tool-registry mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
per-turn agent tool registry rebuild + accessors. State ownership stays
on ``SessionController.__init__``.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for any moved method must patch
``app.core.session.tools_registry_mixin.<symbol>`` instead."""
from __future__ import annotations

import logging


log = logging.getLogger("app.session")


class ToolsRegistryMixin:
    """rebuild_tool_registry + tool_registry / available_tool_names."""

    @property
    def tool_registry(self):
        return getattr(self, "_tool_registry", None)

    def available_tool_names(self) -> list[str]:
        registry = getattr(self, "_tool_registry", None)
        if registry is None:
            return []
        try:
            return registry.names()
        except Exception:
            return []

    def rebuild_tool_registry(self) -> None:
        """Rebuild the tool registry after settings change.

        Reads the current ``settings.tools`` block, constructs a fresh
        registry, and hands it to the active :class:`TurnRunner`.
        """
        try:
            from app.llm.tools import build_default_registry, ToolRegistry
        except Exception:
            log.warning("tool registry import failed", exc_info=True)
            self._tool_registry = None
            if hasattr(self, "_turn_runner"):
                self._turn_runner.set_tool_registry(None)
            return

        tools_cfg = getattr(self._settings, "tools", None)
        if tools_cfg is None or not getattr(tools_cfg, "enabled", True):
            self._tool_registry = ToolRegistry()
            self._turn_runner.set_tool_registry(self._tool_registry)
            return

        registry = ToolRegistry()
        try:
            from app.llm.tools.builtins import GetTimeTool, RecallTool, RecallTopicTool
            if getattr(tools_cfg, "get_time", True):
                registry.register(GetTimeTool())
            if getattr(tools_cfg, "recall", True) and getattr(self, "_rag_retriever", None) is not None:
                registry.register(RecallTool(self._rag_retriever))
            # F10d cluster-scoped recall. Gated by its own switch but needs
            # the same retriever; only useful once the topic graph is wired
            # (the tool returns an empty result otherwise, so it is safe to
            # register regardless).
            if getattr(tools_cfg, "recall_topic", True) and getattr(self, "_rag_retriever", None) is not None:
                registry.register(RecallTopicTool(self._rag_retriever))
            # ``calculate`` moved to the bundled ``calculator`` plugin
            # (see ``plugins/calculator/``) — it now registers via the
            # ToolPlugin SDK fast-tool path below, not as a core builtin.
            # H11 weather tools (get_weather / get_forecast). Synchronous
            # single-GET tools — safe on the brain lane. Independent of the
            # passive ambient feed (agent.weather_sync_enabled): the tools
            # answer on-demand "what's the forecast?" even with the overlay
            # off. They geocode arbitrary place names at call time.
            if getattr(tools_cfg, "weather", True):
                try:
                    from app.llm.tools.weather import build_weather_tools

                    for tool in build_weather_tools(self):
                        registry.register(tool)
                except Exception:
                    log.warning(
                        "weather tools failed to register", exc_info=True
                    )
            # web_search is intentionally NOT a brain builtin anymore.
            # A DuckDuckGo round-trip is too slow for the fast
            # conversational lane, so it now lives only as a background
            # workflow skill (``WorkflowSkillRegistry`` -> ``web_search``
            # task handler). ``tools.web_search`` still gates whether the
            # workflow offers the skill to its planner. The fact-checker
            # and curiosity workers keep their own private WebSearchTool
            # instances — those are background workers, not the brain.
            if (
                getattr(tools_cfg, "world", True)
                and getattr(self, "_world_store", None) is not None
            ):
                try:
                    from app.llm.tools.world import build_world_tools

                    for tool in build_world_tools(self):
                        registry.register(tool)
                except Exception:
                    log.warning("world tools failed to register", exc_info=True)
            # K1: goal tools (list_goals / add_goal / update_goal_progress
            # / archive_goal). Gated on ``tools.goals`` (default True)
            # and skipped silently when the goal store didn't wire
            # (no embedder / memory disabled).
            if (
                getattr(tools_cfg, "goals", True)
                and getattr(self, "_goal_store", None) is not None
            ):
                try:
                    from app.llm.tools.goals import build_goal_tools

                    for tool in build_goal_tools(self):
                        registry.register(tool)
                except Exception:
                    log.warning("goal tools failed to register", exc_info=True)
            # Nested goal workflows — ``start_workflow`` / ``check_my_work``
            # / ``cancel_work``. The brain-facing control surface for the
            # background ``GoalWorkflowHandler`` (multi-step goals: search →
            # read → summarise) — the workflow tools kick off a planned chain
            # that reports asynchronously; file work (and any MCP tool) runs
            # in that background lane, never as a fast brain tool.
            # Gated on ``tools.workflow`` AND a live orchestrator AND the
            # handler actually being registered (``agent.workflow_enabled``).
            if getattr(tools_cfg, "workflow", True) and (
                getattr(self, "_task_orchestrator", None) is not None
            ):
                try:
                    from app.core.tasks.handler_names import (
                        HANDLER_GOAL_WORKFLOW,
                    )
                    from app.llm.tools.workflow_tools import (
                        build_workflow_tools,
                    )

                    if (
                        self._task_orchestrator.handler_for(
                            HANDLER_GOAL_WORKFLOW
                        )
                        is not None
                    ):
                        for tool in build_workflow_tools(self):
                            registry.register(tool)
                except Exception:
                    log.warning(
                        "workflow tools failed to register", exc_info=True
                    )
            # Brain-lane fast tools contributed by code plugins (SDK
            # ``register_fast_tool``). Registered last; their P14 gate
            # families / patterns are pushed to the TurnRunner below.
            for spec in getattr(self, "_plugin_fast_tools", []) or []:
                try:
                    from app.llm.tools.plugin_tool import PluginFastTool

                    registry.register(PluginFastTool(spec))
                except Exception:
                    log.warning(
                        "plugin fast tool failed to register: %s",
                        getattr(spec, "name", "?"),
                        exc_info=True,
                    )
        except Exception:
            log.warning("tool registry build failed", exc_info=True)
        self._tool_registry = registry
        if hasattr(self, "_turn_runner"):
            self._turn_runner.set_tool_registry(registry)
            self._turn_runner.set_plugin_tool_gate(
                getattr(self, "_plugin_tool_families", {}),
                getattr(self, "_plugin_family_patterns", {}),
            )
        log.info("tool registry rebuilt: %s", registry.names())
