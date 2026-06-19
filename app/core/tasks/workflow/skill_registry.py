"""WorkflowSkillRegistry — the goal-workflow capability catalogue.

A :class:`WorkflowSkill` is one thing a goal workflow's planner can
*do*: search files, read a file, search the web, or ``finish``. Each
skill carries

* ``name`` — the planner-facing identifier (also the registry key).
* ``description`` — one line the planner reads to decide when to use it.
* ``arg_schema`` — a small ``{arg: {type, description, required}}`` map
  the planner fills in (and the handler validates) for the child spawn.
* ``spawn`` — a child-spawn function ``(args, ctx) -> child_task_id``
  that creates the actual background task under the parent. ``None``
  for terminal skills (``finish``) that the handler loop consumes
  directly instead of spawning a child.

Why a registry separate from the brain's :class:`ToolRegistry`?

* **Different lane.** Brain tools must be fast (they run inside the
  conversational turn). Workflow skills run in the background, so they
  can be slow + heavy (web search, and later browser-MCP, code
  execution, …). Keeping the two catalogues apart is what lets us move
  ``web_search`` OFF the brain and onto the workflow without it
  leaking back into the fast lane.
* **Child-spawn, not direct-call.** A workflow skill *spawns a task*
  (so it gets a row, heartbeat, cancel path, event log, and shows up
  in the task tree under its parent), whereas a brain tool returns a
  string inline.
* **MCP-pluggable.** ``register`` is the extension point: an MCP
  server advertising a capability registers a skill whose ``spawn``
  dispatches to that server, and the planner picks it up with zero
  planner-code changes.

The registry is deliberately tiny and pure — no I/O, no settings
reads, no LLM. The handler builds it once at boot (via
:func:`build_builtin_skill_registry`) and hands it to the planner.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.tasks.handler_names import (
    HANDLER_FILE_READ,
    HANDLER_FILE_SEARCH,
    HANDLER_FILE_WRITE,
    HANDLER_VISION_DESCRIBE,
    HANDLER_WEB_SEARCH,
)
from app.core.tasks.task_handler import INITIATED_BY_BACKGROUND


log = logging.getLogger("app.tasks.workflow.skills")


# Terminal "skill" the planner picks to stop the loop. Not spawnable —
# the handler consumes it directly. Kept in the registry so the
# planner's allowed-action set is uniformly derived from one place.
WORKFLOW_SKILL_FINISH = "finish"

# Built-in spawnable skill names (stable identifiers).
WORKFLOW_SKILL_SEARCH_FILES = "search_files"
WORKFLOW_SKILL_READ_FILE = "read_file"
WORKFLOW_SKILL_WEB_SEARCH = "web_search"
WORKFLOW_SKILL_WRITE_FILE = "write_file"
WORKFLOW_SKILL_DESCRIBE_IMAGE = "describe_image"


@dataclass(frozen=True, slots=True)
class SpawnContext:
    """Everything a skill's ``spawn`` function needs to create a child.

    Built fresh by the :class:`GoalWorkflowHandler` for each planned
    step. ``orchestrator`` is the live :class:`TaskOrchestrator`;
    ``user_id`` stamps the child row for the per-user cap; the child is
    parented at ``parent_task_id`` so it lands in the workflow's task
    tree and is cascade-cancelled with the parent.
    """

    orchestrator: Any
    user_id: str
    parent_task_id: int


# A child-spawn function. Returns the new child ``task_id`` or ``None``
# when the spawn was rejected (per-user cap, missing handler, …). MUST
# NOT raise — the handler treats ``None`` as a soft failure and records
# it on the blackboard.
SkillSpawnFn = Callable[[dict[str, Any], SpawnContext], "int | None"]


@dataclass(frozen=True, slots=True)
class WorkflowSkill:
    """One capability the planner can invoke."""

    name: str
    description: str
    arg_schema: dict[str, Any] = field(default_factory=dict)
    spawn: SkillSpawnFn | None = None
    terminal: bool = False
    # Router group: the unit the worker-lane skill router narrows to
    # (``files`` / ``web`` / ``vision`` / a per-MCP-server label). Empty
    # means "uncategorised" — such skills are never hidden by the router.
    group: str = ""

    @property
    def spawnable(self) -> bool:
        """True when this skill spawns a child task (vs. ``finish``)."""
        return self.spawn is not None and not self.terminal


class WorkflowSkillRegistry:
    """Name → :class:`WorkflowSkill` map with planner-render + spawn helpers.

    Re-registering a name overwrites — same convention as
    :class:`TaskOrchestrator.register_handler`, so a hot-reload or an
    MCP server reconnecting cleanly replaces its slot.
    """

    def __init__(self) -> None:
        self._skills: dict[str, WorkflowSkill] = {}

    def register(self, skill: WorkflowSkill) -> None:
        name = str(getattr(skill, "name", "") or "").strip()
        if not name:
            raise ValueError("skill must have a non-empty 'name'")
        self._skills[name] = skill
        log.debug(
            "workflow skill registered: name=%s spawnable=%s total=%d",
            name,
            skill.spawnable,
            len(self._skills),
        )

    def get(self, name: str) -> WorkflowSkill | None:
        return self._skills.get(str(name))

    def names(self) -> list[str]:
        """All registered skill names, sorted for stable prompts."""
        return sorted(self._skills.keys())

    def spawnable_names(self) -> list[str]:
        """Names of skills that spawn a child (excludes ``finish``)."""
        return sorted(n for n, s in self._skills.items() if s.spawnable)

    def describe_for_planner(
        self, groups: "set[str] | None" = None
    ) -> list[dict[str, Any]]:
        """Structured catalogue the planner renders into its prompt.

        One entry per skill: ``{name, description, args, terminal, group}``
        where ``args`` is the skill's arg-schema. Pure data — the
        planner module decides how to format it (JSON block, bullet
        list, …).

        When ``groups`` is given (worker-lane skill router narrowing),
        only skills whose ``group`` is in the set are included — except
        terminal skills (``finish``) and uncategorised skills (empty
        ``group``), which are ALWAYS included so the planner can always
        stop and a future untagged skill is never silently hidden.
        ``groups=None`` (the default) returns the full catalogue.
        """
        out: list[dict[str, Any]] = []
        for name in self.names():
            skill = self._skills[name]
            if (
                groups is not None
                and not skill.terminal
                and skill.group
                and skill.group not in groups
            ):
                continue
            out.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "args": dict(skill.arg_schema or {}),
                    "terminal": bool(skill.terminal),
                    "group": skill.group,
                }
            )
        return out

    def groups(self) -> set[str]:
        """Non-empty router groups present in the registry."""
        return {s.group for s in self._skills.values() if s.group}

    def spawn_child(
        self, name: str, args: dict[str, Any], ctx: SpawnContext
    ) -> "int | None":
        """Spawn the child task for skill ``name``.

        Returns the child ``task_id`` or ``None`` when the skill is
        unknown, terminal, or the spawn was rejected. Never raises — a
        spawn function that throws is caught here and downgraded to
        ``None`` so a single bad skill can't crash the workflow loop.
        """
        skill = self._skills.get(str(name))
        if skill is None:
            log.warning("workflow spawn: unknown skill name=%s", name)
            return None
        if skill.spawn is None or skill.terminal:
            log.warning(
                "workflow spawn: skill is not spawnable name=%s", name
            )
            return None
        try:
            return skill.spawn(dict(args or {}), ctx)
        except Exception:
            log.exception("workflow spawn raised: skill=%s", name)
            return None


# ── built-in skill spawn functions ───────────────────────────────────


def _spawn_file_search(args: dict[str, Any], ctx: SpawnContext) -> "int | None":
    """Spawn a ``file_search`` child.

    Args (all optional except when ``only_new`` is false, where a
    non-empty ``query`` is required by the handler):

    * ``query`` — filename substring.
    * ``root_label`` — scope to one configured root.
    * ``max_results`` — cap (1-500).
    * ``case_sensitive`` — default false.
    * ``only_new`` — surface only new/modified files vs. the per-root
      snapshot baseline.
    """
    query = str(args.get("query", "") or "").strip()
    root_label = str(args.get("root_label", "") or "").strip()
    try:
        max_results = max(1, min(500, int(args.get("max_results", 50))))
    except (TypeError, ValueError):
        max_results = 50
    case_sensitive = bool(args.get("case_sensitive", False))
    only_new = bool(args.get("only_new", False))
    title = f"workflow search: {query[:48]}" if query else "workflow search: new files"
    return ctx.orchestrator.start_task(
        user_id=ctx.user_id,
        handler_name=HANDLER_FILE_SEARCH,
        args={
            "query": query,
            "root_label": root_label,
            "max_results": max_results,
            "case_sensitive": case_sensitive,
            "only_new": only_new,
        },
        title=title,
        initiated_by=INITIATED_BY_BACKGROUND,
        # Children don't notify Aiko directly — the parent workflow
        # aggregates and reports once at the end.
        notify_aiko=False,
        visible_to_user=True,
        parent_task_id=ctx.parent_task_id,
    )


def _spawn_file_read(args: dict[str, Any], ctx: SpawnContext) -> "int | None":
    """Spawn a ``file_read`` child.

    Args:

    * ``path`` (required) — label-prefixed or bare path.
    * ``max_bytes`` (optional) — soft cap; handler clamps to ceiling.
    """
    path = str(args.get("path", "") or "").strip()
    if not path:
        log.warning("workflow read_file: empty path, skipping spawn")
        return None
    child_args: dict[str, Any] = {"path": path}
    try:
        max_bytes = int(args.get("max_bytes", 0) or 0)
    except (TypeError, ValueError):
        max_bytes = 0
    if max_bytes > 0:
        child_args["max_bytes"] = max_bytes
    return ctx.orchestrator.start_task(
        user_id=ctx.user_id,
        handler_name=HANDLER_FILE_READ,
        args=child_args,
        title=f"workflow read: {path[:64]}",
        initiated_by=INITIATED_BY_BACKGROUND,
        notify_aiko=False,
        visible_to_user=True,
        parent_task_id=ctx.parent_task_id,
    )


def _spawn_write_file(args: dict[str, Any], ctx: SpawnContext) -> "int | None":
    """Spawn a ``file_write`` child.

    Args:

    * ``path`` (required) — label-prefixed or bare path inside a
      writable root.
    * ``op`` (optional) — ``write`` (default) / ``append`` / ``replace``.
    * ``content`` — text for ``write`` / ``append``.
    * ``find`` / ``replace`` — for the ``replace`` op.

    The destructive-write approval gate lives inside the
    :class:`FileWriteHandler` (it emits the approval ``TaskInputNeeded``
    and the user answers it in the TaskStrip), so the spawn itself just
    forwards the args. The child is ``notify_aiko=False`` so Aiko
    doesn't narrate the approval out loud — the strip is the surface.
    """
    path = str(args.get("path", "") or "").strip()
    if not path:
        log.warning("workflow write_file: empty path, skipping spawn")
        return None
    op = str(args.get("op", "write") or "write").strip().lower()
    child_args: dict[str, Any] = {"path": path, "op": op}
    if "content" in args:
        child_args["content"] = str(args.get("content") or "")
    if "find" in args:
        child_args["find"] = str(args.get("find") or "")
    if "replace" in args:
        child_args["replace"] = str(args.get("replace") or "")
    return ctx.orchestrator.start_task(
        user_id=ctx.user_id,
        handler_name=HANDLER_FILE_WRITE,
        args=child_args,
        title=f"workflow write: {path[:64]}",
        initiated_by=INITIATED_BY_BACKGROUND,
        notify_aiko=False,
        visible_to_user=True,
        parent_task_id=ctx.parent_task_id,
    )


def _spawn_web_search(args: dict[str, Any], ctx: SpawnContext) -> "int | None":
    """Spawn a ``web_search`` child.

    Args:

    * ``query`` (required) — search query.
    * ``max_results`` (optional, 1-10) — result cap.
    """
    query = str(args.get("query", "") or "").strip()
    if not query:
        log.warning("workflow web_search: empty query, skipping spawn")
        return None
    try:
        max_results = max(1, min(10, int(args.get("max_results", 5))))
    except (TypeError, ValueError):
        max_results = 5
    return ctx.orchestrator.start_task(
        user_id=ctx.user_id,
        handler_name=HANDLER_WEB_SEARCH,
        args={"query": query, "max_results": max_results},
        title=f"workflow web search: {query[:48]}",
        initiated_by=INITIATED_BY_BACKGROUND,
        notify_aiko=False,
        visible_to_user=True,
        parent_task_id=ctx.parent_task_id,
    )


def _spawn_describe_image(args: dict[str, Any], ctx: SpawnContext) -> "int | None":
    """Spawn a ``vision_describe`` child.

    Args:

    * ``path`` (required) — label-prefixed or bare path to an image
      inside a configured root (incl. the managed ``Attachments`` root).
    * ``question`` (optional) — what to focus on / ask about the image.

    The vision call reuses the already-loaded local worker model (no
    second model); the handler validates the image + runs the call.
    """
    path = str(args.get("path", "") or "").strip()
    if not path:
        log.warning("workflow describe_image: empty path, skipping spawn")
        return None
    child_args: dict[str, Any] = {"path": path}
    question = str(args.get("question", "") or args.get("prompt", "") or "").strip()
    if question:
        child_args["question"] = question
    return ctx.orchestrator.start_task(
        user_id=ctx.user_id,
        handler_name=HANDLER_VISION_DESCRIBE,
        args=child_args,
        title=f"workflow describe image: {path[:56]}",
        initiated_by=INITIATED_BY_BACKGROUND,
        notify_aiko=False,
        visible_to_user=True,
        parent_task_id=ctx.parent_task_id,
    )


# ── built-in skill definitions ───────────────────────────────────────


def _file_search_skill() -> WorkflowSkill:
    return WorkflowSkill(
        name=WORKFLOW_SKILL_SEARCH_FILES,
        description=(
            "Search the user's configured file roots for files whose "
            "filename contains a substring. Use only_new=true to find "
            "files that are new or changed since the last scan."
        ),
        arg_schema={
            "query": {
                "type": "string",
                "description": "Filename substring to match (basename only).",
                "required": False,
            },
            "root_label": {
                "type": "string",
                "description": "Optional: scope to one configured root.",
                "required": False,
            },
            "only_new": {
                "type": "boolean",
                "description": (
                    "Optional: only return files new/modified since the "
                    "last scan of each root."
                ),
                "required": False,
            },
            "max_results": {
                "type": "integer",
                "description": "Optional cap on matches (1-500, default 50).",
                "required": False,
            },
        },
        spawn=_spawn_file_search,
        group="files",
    )


def _file_read_skill() -> WorkflowSkill:
    return WorkflowSkill(
        name=WORKFLOW_SKILL_READ_FILE,
        description=(
            "Read the text content of one file from the configured roots. "
            "Pass a label-prefixed path ('Documents:notes/q4.md') or a "
            "bare path. Use after search_files to inspect a match."
        ),
        arg_schema={
            "path": {
                "type": "string",
                "description": "Path to read (label-prefixed or bare).",
                "required": True,
            },
            "max_bytes": {
                "type": "integer",
                "description": "Optional soft cap on bytes to read.",
                "required": False,
            },
        },
        spawn=_spawn_file_read,
        group="files",
    )


def _web_search_skill() -> WorkflowSkill:
    return WorkflowSkill(
        name=WORKFLOW_SKILL_WEB_SEARCH,
        description=(
            "Search the public web (DuckDuckGo) for current information — "
            "news, prices, recent releases, facts that change over time. "
            "Slow (seconds), which is why it's a background workflow skill "
            "and not a fast conversational tool."
        ),
        arg_schema={
            "query": {
                "type": "string",
                "description": "The search query.",
                "required": True,
            },
            "max_results": {
                "type": "integer",
                "description": "Optional result cap (1-10, default 5).",
                "required": False,
            },
        },
        spawn=_spawn_web_search,
        group="web",
    )


def _write_file_skill() -> WorkflowSkill:
    return WorkflowSkill(
        name=WORKFLOW_SKILL_WRITE_FILE,
        description=(
            "Create, overwrite, append to, or find/replace text in a file "
            "inside one of the user's WRITABLE roots. Use op='write' to "
            "create or replace a whole file, op='append' to add to the "
            "end, op='replace' to swap one piece of text for another. "
            "Overwriting / appending-to / editing an EXISTING file asks "
            "the user to approve first (they confirm in the task strip); "
            "creating a new file does not. Prefix the path with a root "
            "label when more than one writable root exists "
            "('Notes:todo.md')."
        ),
        arg_schema={
            "path": {
                "type": "string",
                "description": (
                    "Target path (label-prefixed or bare) inside a "
                    "writable root."
                ),
                "required": True,
            },
            "op": {
                "type": "string",
                "description": (
                    "One of 'write' (create/overwrite), 'append', "
                    "'replace'. Default 'write'."
                ),
                "required": False,
            },
            "content": {
                "type": "string",
                "description": "Text to write or append (write/append ops).",
                "required": False,
            },
            "find": {
                "type": "string",
                "description": "Text to search for (replace op).",
                "required": False,
            },
            "replace": {
                "type": "string",
                "description": "Replacement text (replace op).",
                "required": False,
            },
        },
        spawn=_spawn_write_file,
        group="files",
    )


def _describe_image_skill() -> WorkflowSkill:
    return WorkflowSkill(
        name=WORKFLOW_SKILL_DESCRIBE_IMAGE,
        description=(
            "Look at an IMAGE file and describe what's in it, using local "
            "vision. Pass a label-prefixed path ('Attachments:photo.png' "
            "or 'Documents:screenshot.png') or a bare path. Use this for "
            "any 'what's in this picture / screenshot / photo' request, "
            "or when the user attached an image. Optionally pass a "
            "'question' to focus on something specific. Slow (it runs a "
            "vision model), which is why it's a background workflow skill."
        ),
        arg_schema={
            "path": {
                "type": "string",
                "description": (
                    "Path to the image (label-prefixed or bare)."
                ),
                "required": True,
            },
            "question": {
                "type": "string",
                "description": (
                    "Optional: what to focus on or ask about the image."
                ),
                "required": False,
            },
        },
        spawn=_spawn_describe_image,
        group="vision",
    )


def _finish_skill() -> WorkflowSkill:
    return WorkflowSkill(
        name=WORKFLOW_SKILL_FINISH,
        description=(
            "Stop the workflow and report back. Pick this when you have "
            "enough information to answer the goal, when no further step "
            "would help, or when you've hit a dead end."
        ),
        arg_schema={
            "findings": {
                "type": "string",
                "description": (
                    "A concise summary of what you found, to report to "
                    "the user."
                ),
                "required": False,
            },
            "outcome": {
                "type": "string",
                "description": (
                    "One of: 'success', 'partial', 'nothing_found'."
                ),
                "required": False,
            },
        },
        terminal=True,
    )


def build_builtin_skill_registry(
    *,
    web_search_enabled: bool = True,
    file_skills_enabled: bool = True,
    file_write_enabled: bool = False,
    vision_enabled: bool = False,
) -> WorkflowSkillRegistry:
    """Construct the default registry: file search/read + web + finish.

    ``web_search_enabled`` mirrors ``tools.web_search`` so a user who
    disabled web search doesn't get the skill offered to the planner.
    ``file_skills_enabled`` mirrors ``agent.builtin_file_skills_enabled``
    — when ``False`` the built-in ``file_search`` / ``read_file`` /
    ``write_file`` skills are NOT registered, so a user who handles files
    exclusively through a filesystem MCP server gets a single path
    convention and no built-in-vs-MCP overlap for the planner to confuse.
    ``file_write_enabled`` mirrors ``agent.file_write.enabled`` — the
    destructive ``write_file`` skill is only offered when the master
    switch is on (and a writable root exists, which the handler
    enforces at run time), and only when ``file_skills_enabled`` is on.
    ``vision_enabled`` mirrors ``agent.vision.enabled`` — the
    ``describe_image`` skill is only offered when vision is on (and an
    active root exists). The ``finish`` terminal skill is always present
    — a workflow must always be able to stop.

    Callers (the handler / mixin) layer MCP-provided skills on top via
    :meth:`WorkflowSkillRegistry.register` after this returns.
    """
    registry = WorkflowSkillRegistry()
    if file_skills_enabled:
        registry.register(_file_search_skill())
        registry.register(_file_read_skill())
        if file_write_enabled:
            registry.register(_write_file_skill())
    if web_search_enabled:
        registry.register(_web_search_skill())
    if vision_enabled:
        registry.register(_describe_image_skill())
    registry.register(_finish_skill())
    log.info(
        "workflow skill registry built: skills=%s",
        registry.names(),
    )
    return registry


__all__ = [
    "WORKFLOW_SKILL_FINISH",
    "WORKFLOW_SKILL_SEARCH_FILES",
    "WORKFLOW_SKILL_READ_FILE",
    "WORKFLOW_SKILL_WEB_SEARCH",
    "WORKFLOW_SKILL_WRITE_FILE",
    "WORKFLOW_SKILL_DESCRIBE_IMAGE",
    "SpawnContext",
    "SkillSpawnFn",
    "WorkflowSkill",
    "WorkflowSkillRegistry",
    "build_builtin_skill_registry",
]
