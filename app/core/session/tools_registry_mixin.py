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
            from app.llm.tools.builtins import GetTimeTool, RecallTool
            if getattr(tools_cfg, "get_time", True):
                registry.register(GetTimeTool())
            if getattr(tools_cfg, "recall", True) and getattr(self, "_rag_retriever", None) is not None:
                registry.register(RecallTool(self._rag_retriever))
            # Synchronous exact-arithmetic tool. No external deps, no
            # store — safe to register whenever the switch is on so Aiko
            # never has to guess a number.
            if getattr(tools_cfg, "calculate", True):
                try:
                    from app.llm.tools.calc import CalculateTool

                    registry.register(CalculateTool())
                except Exception:
                    log.warning(
                        "calculate tool failed to register", exc_info=True
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
            # Chunk 10: filesystem task tools — ``start_file_search``
            # and ``cancel_file_task``. Gated on ``tools.file_tasks``
            # (default True) and skipped silently when the task
            # subsystem itself is off (``agent.tasks_enabled=False``
            # leaves ``_task_orchestrator`` as ``None``).
            if (
                getattr(tools_cfg, "file_tasks", True)
                and getattr(self, "_task_orchestrator", None) is not None
            ):
                try:
                    from app.llm.tools.file_tasks import build_file_task_tools

                    for tool in build_file_task_tools(self):
                        registry.register(tool)
                except Exception:
                    log.warning(
                        "file task tools failed to register", exc_info=True
                    )
            # Nested goal workflows — ``start_workflow`` / ``check_my_work``
            # / ``cancel_work``. The brain-facing control surface for the
            # background ``GoalWorkflowHandler`` (multi-step goals: search →
            # read → summarise). Distinct from the fast file lane above:
            # the file tools fold a single op into the turn, the workflow
            # tools kick off a planned chain that reports asynchronously.
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
        except Exception:
            log.warning("tool registry build failed", exc_info=True)
        self._tool_registry = registry
        if hasattr(self, "_turn_runner"):
            self._turn_runner.set_tool_registry(registry)
        log.info("tool registry rebuilt: %s", registry.names())
