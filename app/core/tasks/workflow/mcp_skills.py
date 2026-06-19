"""MCP → WorkflowSkill bridge.

Converts the tools discovered by :class:`ExternalMcpManager` into
:class:`WorkflowSkill` rows registered on the background-lane
:class:`WorkflowSkillRegistry`. The goal-workflow planner then picks them
up automatically through ``describe_for_planner()`` — zero planner-code
change, exactly as the registry's "MCP-pluggable" docstring anticipates.

Each MCP tool becomes a skill whose ``name`` is namespaced
``<server_id>__<tool_name>`` (so two servers can advertise a ``read_file``
without colliding), whose ``arg_schema`` is derived from the tool's JSON
Schema ``inputSchema``, and whose ``spawn`` starts a ``HANDLER_MCP_TOOL``
child task carrying ``{server_id, tool_name, tool_args}``.

MCP tools live in the background lane ONLY — they are never added to the
brain's fast :class:`ToolRegistry`.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.core.tasks.handler_names import HANDLER_MCP_TOOL
from app.core.tasks.task_handler import INITIATED_BY_BACKGROUND
from app.core.tasks.workflow.skill_registry import (
    SpawnContext,
    WorkflowSkill,
    WorkflowSkillRegistry,
)

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.mcp.client.manager import ExternalMcpManager, McpToolDescriptor


log = logging.getLogger("app.tasks.workflow.mcp_skills")

_TITLE_CAP = 64
_DESC_CAP = 600


def _arg_schema_from_input(input_schema: dict[str, Any]) -> dict[str, Any]:
    """Flatten a JSON-Schema ``inputSchema`` into the planner arg shape.

    The planner reads ``{arg: {type, description, required}}``; an MCP
    tool's ``inputSchema`` is a full JSON Schema object, so we project its
    ``properties`` and ``required`` list down to that shape.
    """
    if not isinstance(input_schema, dict):
        return {}
    props = input_schema.get("properties")
    required = set(input_schema.get("required") or [])
    out: dict[str, Any] = {}
    if isinstance(props, dict):
        for name, spec in props.items():
            spec = spec if isinstance(spec, dict) else {}
            out[str(name)] = {
                "type": spec.get("type", "string"),
                "description": str(spec.get("description", "") or ""),
                "required": name in required,
            }
    return out


def _make_spawn(server_id: str, tool_name: str):
    """Build the child-spawn function for one MCP tool."""

    def _spawn(args: dict[str, Any], ctx: SpawnContext) -> "int | None":
        return ctx.orchestrator.start_task(
            user_id=ctx.user_id,
            handler_name=HANDLER_MCP_TOOL,
            args={
                "server_id": server_id,
                "tool_name": tool_name,
                "tool_args": dict(args or {}),
            },
            title=f"workflow mcp: {server_id}/{tool_name}"[:_TITLE_CAP],
            initiated_by=INITIATED_BY_BACKGROUND,
            notify_aiko=False,
            visible_to_user=True,
            parent_task_id=ctx.parent_task_id,
        )

    return _spawn


def _skill_from_descriptor(desc: "McpToolDescriptor") -> WorkflowSkill:
    description = desc.description.strip() or f"MCP tool {desc.name}"
    description = f"[{desc.server_id}] {description}"[:_DESC_CAP]
    return WorkflowSkill(
        name=desc.qualified_name,
        description=description,
        arg_schema=_arg_schema_from_input(desc.input_schema),
        spawn=_make_spawn(desc.server_id, desc.name),
        # Per-server router group so a server's tools are narrowed in/out
        # together (and the catalogue can grow without bloating each plan).
        group=f"mcp:{desc.server_id}",
    )


def register_mcp_skills(
    skill_registry: WorkflowSkillRegistry,
    manager: "ExternalMcpManager",
) -> list[str]:
    """Register every discovered (allow-listed) MCP tool as a skill.

    Idempotent: re-registering a name overwrites the slot (the registry's
    documented convention), so this can be called again on reconnect /
    config change. Returns the list of registered skill names.

    The ``expose_tools`` per-server allow-list is already applied by the
    manager (it filters at ``list_tools`` time), so every descriptor
    returned here is meant to be exposed.
    """
    names: list[str] = []
    for desc in manager.list_available_tools():
        skill = _skill_from_descriptor(desc)
        skill_registry.register(skill)
        names.append(skill.name)
    log.info("mcp skills registered: count=%d names=%s", len(names), names)
    return names


__all__ = ["register_mcp_skills"]
