from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")


def register(mcp, session: "SessionController") -> None:
    @mcp.resource("assistant://history")
    def get_history() -> str:
        """Recent conversation messages (most recent 40)."""
        try:
            rows = session._chat_db.get_messages(session.session_key, limit=40)
            entries = [
                {"role": r.role, "content": r.content[:500], "created_at": str(r.created_at)}
                for r in rows
            ]
            return json.dumps(entries, indent=2, default=str)
        except Exception as exc:
            return f"Error reading history: {exc}"

    @mcp.resource("assistant://config")
    def get_config() -> str:
        """Current assistant configuration snapshot."""
        s = session._settings
        info = {
            "model": session.effective_chat_model,
            "base_url": s.ollama.base_url,
            "temperature": s.ollama.temperature,
            "context_window": session.context_window_size,
            "tts_provider": s.tts.provider,
            "tts_voice": s.tts.voice,
            "tts_enabled": s.tts.enabled,
            "stt_model": s.stt.model,
            "stt_language": s.stt.language,
            "mcp_server_port": s.mcp_server.port,
        }
        return json.dumps(info, indent=2, default=str)

    # ── Brain-orchestration task debug surface (chunk 9) ─────────────
    #
    # These tools let Cursor / VSCode MCP clients drive the
    # filesystem reference handler end-to-end. Useful both as a
    # smoke test for the queue path and as a real-world demo of
    # the "Aiko does something in the background, surfaces the
    # result on the next turn" flow described in
    # ``docs/brain-orchestration.md``.

    @mcp.tool()
    def list_file_roots() -> str:
        """List configured filesystem roots + their validation status.

        Mirrors ``agent.task_file_allowed_roots`` after boot-time
        validation. Each entry reports ``label``, ``path`` (the
        normalised absolute form), ``active`` (False if the path
        missing / not a directory / had an invalid label),
        ``reason`` (stable short string when inactive), and
        ``warnings`` (e.g. ``"sensitive_directory"``).
        """
        from app.core.tasks.sandbox import FileTaskRoot, validate_roots

        roots_raw = getattr(
            session._settings.agent, "task_file_allowed_roots", ()
        ) or ()
        roots: list[FileTaskRoot] = []
        for entry in roots_raw:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label", "")).strip()
            path = str(entry.get("path", "")).strip()
            if not label or not path:
                continue
            roots.append(
                FileTaskRoot(
                    label=label,
                    path=path,
                    read_only=bool(entry.get("read_only", True)),
                )
            )
        verdicts = validate_roots(roots)
        out = [
            {
                "label": vr.root.label,
                "path": vr.abs_path,
                "active": vr.active,
                "reason": vr.reason,
                "warnings": list(vr.warnings),
                "read_only": vr.root.read_only,
            }
            for vr in verdicts
        ]
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def start_file_search(
        query: str, root_label: str = "", max_results: int = 50
    ) -> str:
        """Start an asynchronous file search task and return its id.

        The search runs on the orchestrator's worker pool — this
        call returns immediately with ``{"task_id": N}``. Results
        land as a ``task_result`` cue on the brain queue and
        surface in Aiko's next turn (or escalate to a proactive
        turn after the silence window).

        ``root_label`` scopes to a single configured root (empty =
        all active roots). ``max_results`` caps the returned match
        list; the handler stops walking once the cap is hit and
        flags ``truncated=true`` on the result.
        """
        if session._task_orchestrator is None:
            return json.dumps(
                {"error": "task subsystem disabled (agent.tasks_enabled=False)"}
            )
        user_id = str(getattr(session, "_user_id", "default"))
        title = f"file search: {query[:60]}"
        if root_label:
            title += f" (in {root_label})"
        task_id = session._task_orchestrator.start_task(
            user_id=user_id,
            handler_name="file_search",
            args={
                "query": query,
                "root_label": root_label,
                "max_results": int(max_results),
            },
            title=title,
            initiated_by="system",  # MCP path is admin-initiated
        )
        if task_id is None:
            return json.dumps({"error": "task_spawn_rejected"})
        return json.dumps({"task_id": task_id, "handler": "file_search"})

    @mcp.tool()
    def start_file_read(path: str, max_bytes: int = 0) -> str:
        """Start an asynchronous file read task and return its id.

        Reads a text file from one of the configured file roots
        (``agent.task_file_allowed_roots``). Path can be label-
        prefixed (``"Documents:notes/q4.md"``) or bare
        (``"notes/q4.md"``); a bare path that matches in multiple
        roots transitions the task to ``awaiting_input`` rather than
        guessing — surface the candidate list via
        :func:`list_active_tasks` and resolve with
        :func:`answer_file_task`.

        ``max_bytes`` of 0 (or omitted) uses the configured ceiling
        ``agent.task_file_read_max_bytes`` (default 256 KiB).
        """
        if session._task_orchestrator is None:
            return json.dumps(
                {"error": "task subsystem disabled (agent.tasks_enabled=False)"}
            )
        user_id = str(getattr(session, "_user_id", "default"))
        args: dict[str, Any] = {"path": path}
        if max_bytes and int(max_bytes) > 0:
            args["max_bytes"] = int(max_bytes)
        task_id = session._task_orchestrator.start_task(
            user_id=user_id,
            handler_name="file_read",
            args=args,
            title=f"file read: {path[:80]}",
            initiated_by="system",
        )
        if task_id is None:
            return json.dumps({"error": "task_spawn_rejected"})
        return json.dumps({"task_id": task_id, "handler": "file_read"})

    @mcp.tool()
    def answer_file_task(task_id: int, answer: str) -> str:
        """Resolve an ``awaiting_input`` file task with the user's answer.

        Used to disambiguate a bare-path read whose path matched in
        multiple roots. ``answer`` should be one of the candidate
        strings the handler emitted (typically
        ``"<label>:<relative_path>"`` from
        ``state.input_request.options``).

        Returns ``{"answered": true, "task_id": N}`` when the
        orchestrator accepted the answer; ``answered=false`` means
        the task wasn't actually waiting, was unknown, or had
        already terminated. A bad answer text may still flip the
        task to another ``awaiting_input`` (the handler decides) —
        check :func:`list_active_tasks` to verify the new status.
        """
        if session._task_orchestrator is None:
            return json.dumps(
                {"error": "task subsystem disabled (agent.tasks_enabled=False)"}
            )
        try:
            ok = session._task_orchestrator.answer(int(task_id), str(answer))
        except Exception as exc:
            return json.dumps({"error": f"answer failed: {exc}"})
        return json.dumps({"answered": bool(ok), "task_id": int(task_id)})

    @mcp.tool()
    def list_active_tasks() -> str:
        """Return JSON of every task currently ``running`` or ``awaiting_input``.

        Reads :meth:`TaskOrchestrator.list_running` for all users
        (no per-user filter — debug-only). Each entry includes
        ``id``, ``handler_name``, ``title``, ``status``,
        ``progress``, ``last_message``, ``user_id``, ``initiated_by``,
        and ``created_at``.
        """
        if session._task_orchestrator is None:
            return json.dumps([])
        try:
            rows = session._task_orchestrator.list_running(user_id=None)
        except Exception as exc:
            return json.dumps({"error": f"list_running failed: {exc}"})
        out = []
        for row in rows:
            entry: dict[str, Any] = {
                "id": row.id,
                "handler_name": row.handler_name,
                "title": row.title,
                "status": row.status,
                "progress": row.progress,
                "last_message": row.last_message,
                "user_id": row.user_id,
                "initiated_by": row.initiated_by,
                "created_at": row.created_at,
            }
            # Surface input_request so the MCP user can see what
            # answer the task is waiting for (chunk 12 file_read +
            # any future awaiting-input handler benefits).
            if row.input_request is not None:
                entry["input_request"] = row.input_request
            out.append(entry)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def cancel_task(task_id: int) -> str:
        """Cancel an active task by id.

        Returns ``{"cancelled": true, "task_id": N}`` on success,
        ``{"error": ...}`` when the task subsystem is disabled or
        the row isn't in an active state. Cancellation is a row
        transition + ``handler.cancel`` callback; the handler is
        expected to release external resources but may take a
        moment to notice (the orchestrator does NOT block).
        """
        if session._task_orchestrator is None:
            return json.dumps(
                {"error": "task subsystem disabled (agent.tasks_enabled=False)"}
            )
        try:
            ok = session._task_orchestrator.cancel(int(task_id))
        except Exception as exc:
            return json.dumps({"error": f"cancel failed: {exc}"})
        return json.dumps({"cancelled": bool(ok), "task_id": int(task_id)})

    @mcp.tool()
    def get_workflow_state(task_id: int) -> str:
        """Deep snapshot of one nested goal workflow + its children.

        For the ``goal_workflow`` parent task ``task_id``: returns the
        parent row (status / phase / progress / last_message / result)
        plus every child task it spawned (file_search / file_read /
        web_search / …) with each child's status + result. First stop
        when "the workflow finished but the answer looks wrong" — you
        can see exactly which sub-step produced which finding, and
        whether any child failed or got cancelled. Returns
        ``{"error": ...}`` when the task subsystem is off or the id
        isn't a known task.
        """
        orch = getattr(session, "_task_orchestrator", None)
        if orch is None:
            return json.dumps(
                {"error": "task subsystem disabled (agent.tasks_enabled=False)"}
            )
        try:
            parent = orch.get(int(task_id))
        except Exception as exc:
            return json.dumps({"error": f"get failed: {exc}"})
        if parent is None:
            return json.dumps({"error": f"no task with id {task_id}"})

        def _row(row: Any) -> dict[str, Any]:
            return {
                "id": row.id,
                "handler_name": row.handler_name,
                "title": row.title,
                "status": row.status,
                "phase": getattr(row, "phase", None),
                "progress": row.progress,
                "last_message": row.last_message,
                "parent_task_id": getattr(row, "parent_task_id", None),
                "result": row.result,
                "error": row.error,
            }

        children: list[dict[str, Any]] = []
        try:
            store = getattr(orch, "_store", None)
            if store is not None:
                children = [
                    _row(c) for c in store.list_children(int(task_id))
                ]
        except Exception as exc:
            children = [{"error": f"list_children failed: {exc}"}]
        payload = {
            "parent": _row(parent),
            "children": children,
            "child_count": len(children),
        }
        return json.dumps(payload, indent=2, default=str)

    @mcp.tool()
    def list_capability_gaps() -> str:
        """Things a goal workflow recently could NOT do (missing skills).

        Returns the bounded ring of capability gaps recorded by the
        :class:`GoalWorkflowHandler` whenever its planner declared a
        ``missing_capability`` — i.e. the goal needed a skill that
        isn't registered yet (send email, open a web page, run code,
        …). Each entry is ``{capability, goal}``. This is the
        backing data for Aiko's "I don't know how to do that yet"
        honesty + a roadmap signal for which skills to build next.
        Empty list when nothing has been blocked.
        """
        fn = getattr(session, "workflow_capability_gaps", None)
        if not callable(fn):
            return json.dumps([])
        try:
            return json.dumps(list(fn()), indent=2, default=str)
        except Exception as exc:
            return json.dumps({"error": f"capability gaps read failed: {exc}"})

    @mcp.tool()
    def get_approvals_state() -> str:
        """Dump the task-approval policy + every registered capability.

        Returns a JSON dict: the global ``mode`` (ask|auto), the
        per-capability ``overrides`` map, the in-memory
        ``session_approved`` set (capabilities the user clicked
        "approve all" on this session), and a ``capabilities`` list —
        each ``{id, label, destructive, effective_mode}`` so you can
        see at a glance what would gate vs. proceed right now. First
        stop for "did my file_write override take?" / "is approve-all
        still active?". Destructive task handlers (file_write today)
        read the same ``effective_mode`` before acting.
        """
        fn = getattr(session, "approvals_state", None)
        if not callable(fn):
            return json.dumps(
                {"error": "approvals state unavailable (tasks disabled?)"}
            )
        try:
            return json.dumps(fn(), indent=2, default=str)
        except Exception as exc:
            return json.dumps({"error": f"approvals state read failed: {exc}"})

    @mcp.tool()
    def get_vision_state() -> str:
        """Snapshot the local-vision (describe_image) capability.

        Returns ``enabled`` (``agent.vision.enabled``), the effective
        model the vision call would use (the ``agent.vision.model``
        override, or the worker model when empty), the runtime type of
        the worker client (must be ``OllamaClient`` for image
        passthrough), whether the ``describe_image`` skill is currently
        registered, the resource caps, and the active file roots images
        can be resolved from. First stop for "why won't Aiko look at the
        image?" — confirm enabled + an OllamaClient worker + an active
        root.
        """
        agent = getattr(session._settings, "agent", None)
        vision_cfg = getattr(agent, "vision", None)
        worker_client = getattr(session, "_worker_client_inner", None)
        worker_type = type(worker_client).__name__ if worker_client else None
        override = str(getattr(vision_cfg, "model", "") or "").strip()
        effective_model = override or str(
            getattr(session, "_effective_worker_model", "") or ""
        )
        skill_registered = False
        try:
            reg = getattr(session, "_workflow_skill_registry", None)
            if reg is not None:
                skill_registered = "describe_image" in set(reg.names())
        except Exception:
            skill_registered = False
        from app.core.tasks.sandbox import FileTaskRoot, validate_roots

        roots_raw = getattr(agent, "task_file_allowed_roots", ()) or ()
        roots: list[FileTaskRoot] = []
        for entry in roots_raw:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label", "")).strip()
            path = str(entry.get("path", "")).strip()
            if not label or not path:
                continue
            roots.append(
                FileTaskRoot(
                    label=label,
                    path=path,
                    read_only=bool(entry.get("read_only", True)),
                )
            )
        active = [vr.root.label for vr in validate_roots(roots) if vr.active]
        payload = {
            "enabled": bool(getattr(vision_cfg, "enabled", False)),
            "model_override": override,
            "effective_model": effective_model,
            "worker_client_type": worker_type,
            "worker_is_ollama": worker_type == "OllamaClient",
            "skill_registered": skill_registered,
            "max_bytes": int(getattr(vision_cfg, "max_bytes", 0) or 0),
            "timeout_seconds": int(getattr(vision_cfg, "timeout_seconds", 0) or 0),
            "allowed_extensions": list(
                getattr(vision_cfg, "allowed_extensions", ()) or ()
            ),
            "active_roots": active,
        }
        return json.dumps(payload, indent=2, default=str)

    @mcp.tool()
    def describe_image_now(path: str, question: str = "") -> str:
        """Describe an image synchronously, bypassing the planner.

        Runs the ``vision_describe`` handler directly (no workflow, no
        background queue) and blocks for the result — the fastest way to
        verify the vision path end-to-end. ``path`` is label-prefixed
        (``"Documents:photo.png"``) or bare; ``question`` optionally
        focuses the description. Returns the handler's result dict
        (``description`` / ``summary`` / ``model`` / …) or an
        ``{"error": ...}`` describing why it failed (vision disabled, no
        active root, non-multimodal worker model, oversize, …).
        """
        orch = getattr(session, "_task_orchestrator", None)
        if orch is None:
            return json.dumps(
                {"error": "task subsystem disabled (agent.tasks_enabled=False)"}
            )
        handler = None
        try:
            handler = orch.handler_for("vision_describe")
        except Exception:
            handler = None
        if handler is None:
            return json.dumps(
                {
                    "error": (
                        "vision_describe handler not registered "
                        "(agent.vision.enabled=False or no active root?)"
                    )
                }
            )
        captured: dict[str, Any] = {}

        def _emit(event: Any) -> None:
            name = type(event).__name__
            if name == "TaskCompleted":
                captured["result"] = getattr(event, "result", None)
            elif name == "TaskFailed":
                captured["error"] = getattr(event, "error", "failed")
            elif name == "TaskInputNeeded":
                captured["awaiting_input"] = getattr(event, "prompt", "")
                captured["options"] = list(getattr(event, "options", ()) or ())

        args: dict[str, Any] = {"path": path}
        if question.strip():
            args["question"] = question.strip()
        try:
            handler.start(args, _emit)
        except Exception as exc:
            return json.dumps({"error": f"vision call raised: {exc}"})
        if "result" in captured:
            return json.dumps(captured["result"], indent=2, default=str)
        return json.dumps(captured or {"error": "no result emitted"}, indent=2, default=str)

    @mcp.tool()
    def list_external_mcp_servers() -> str:
        """Status of every configured EXTERNAL MCP server (client side).

        These are the servers the app connects OUT to (filesystem,
        browser, …) to consume their tools — distinct from this debug
        server. Returns one entry per configured server:
        ``{id, name, transport, status, error, tool_count, tools}``.
        ``status`` is one of ``connecting`` / ``connected`` / ``failed``
        / ``disabled`` / ``stopped``. Empty list when the manager isn't
        running (master switch off or no servers configured).
        """
        manager = getattr(session, "_external_mcp_manager", None)
        if manager is None:
            return json.dumps(
                {"enabled": False, "reason": "no external MCP manager running"}
            )
        try:
            return json.dumps(manager.server_status(), indent=2, default=str)
        except Exception as exc:
            return json.dumps({"error": f"status read failed: {exc}"})

    @mcp.tool()
    def list_external_mcp_tools() -> str:
        """Every tool discovered across connected external MCP servers.

        Returns a JSON list of
        ``{server_id, name, qualified_name, description, input_schema}``.
        ``qualified_name`` (``<server_id>__<tool_name>``) is the skill
        name the background-workflow planner sees. Use this to confirm a
        server's tools auto-registered into the background lane.
        """
        manager = getattr(session, "_external_mcp_manager", None)
        if manager is None:
            return json.dumps(
                {"enabled": False, "reason": "no external MCP manager running"}
            )
        try:
            tools = [
                {
                    "server_id": t.server_id,
                    "name": t.name,
                    "qualified_name": t.qualified_name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in manager.list_available_tools()
            ]
            return json.dumps(tools, indent=2, default=str)
        except Exception as exc:
            return json.dumps({"error": f"tool list failed: {exc}"})

    @mcp.tool()
    def call_external_mcp_tool(
        server_id: str, tool: str, args_json: str = "{}"
    ) -> str:
        """Call an external MCP tool end-to-end (no task row, no Aiko).

        The fastest way to verify a server is reachable and a tool
        works: dispatches straight through the manager, bypassing the
        task orchestrator. ``args_json`` is a JSON object of the tool's
        arguments. Returns the flattened text content + an ``is_error``
        flag, or an error string.
        """
        manager = getattr(session, "_external_mcp_manager", None)
        if manager is None:
            return json.dumps(
                {"enabled": False, "reason": "no external MCP manager running"}
            )
        try:
            tool_args = json.loads(args_json or "{}")
            if not isinstance(tool_args, dict):
                return json.dumps({"error": "args_json must be a JSON object"})
        except Exception as exc:
            return json.dumps({"error": f"args_json parse failed: {exc}"})
        try:
            from app.core.tasks.handlers.mcp_tool import _flatten_content

            result = manager.call_tool(server_id, tool, tool_args)
            text, non_text = _flatten_content(result)
            return json.dumps(
                {
                    "server_id": server_id,
                    "tool": tool,
                    "is_error": bool(getattr(result, "isError", False)),
                    "non_text_blocks": non_text,
                    "content": text,
                },
                indent=2,
                default=str,
            )
        except Exception as exc:
            return json.dumps({"error": f"call failed: {exc}"})

    @mcp.tool()
    def restart_external_mcp_server(server_id: str) -> str:
        """Force one external MCP server to reconnect (re-reads tools).

        Use after editing a server's config or when it dropped. Returns
        ``{restarted: bool}``; ``false`` means the id is unknown or the
        manager isn't running.
        """
        manager = getattr(session, "_external_mcp_manager", None)
        if manager is None:
            return json.dumps({"restarted": False, "reason": "manager not running"})
        try:
            return json.dumps({"restarted": bool(manager.restart(server_id))})
        except Exception as exc:
            return json.dumps({"error": f"restart failed: {exc}"})

    @mcp.tool()
    def get_browser_perception_state() -> str:
        """Snapshot of the browser perception layer (the snapshot middleware).

        Returns ``{enabled, server_id, snapshot_tools, adapter,
        max_ranked_elements, memory_pages, transform_count, last_summary}``
        when the layer is configured, else ``{enabled: false}``. Use this
        to confirm the perception layer is active and which MCP server /
        adapter it's wrapping before debugging a browse workflow.
        """
        perception = getattr(session, "_browser_perception", None)
        if perception is None:
            return json.dumps(
                {"enabled": False, "reason": "browser perception not configured"}
            )
        try:
            return json.dumps(perception.debug_state(), indent=2, default=str)
        except Exception as exc:
            return json.dumps({"error": f"state read failed: {exc}"})

    @mcp.tool()
    def preview_browser_perception(raw_text: str, args_json: str = "{}") -> str:
        """Run the perception pipeline on a pasted snapshot (no live browser).

        Feeds ``raw_text`` (a raw accessibility-tree dump) through the
        configured adapter + dedup/group/rank/diff pipeline and returns
        the reshaped ``{content, summary, element_count}`` — or
        ``{parsed: false}`` when the adapter can't parse it (the raw text
        would pass through unchanged in production). ``args_json`` is the
        optional tool-args object (e.g. ``{"url": "..."}``) used for the
        page key/title. The fastest way to validate the adapter against a
        real snapshot format without driving Chrome.
        """
        perception = getattr(session, "_browser_perception", None)
        if perception is None:
            return json.dumps(
                {"enabled": False, "reason": "browser perception not configured"}
            )
        try:
            tool_args = json.loads(args_json or "{}")
            if not isinstance(tool_args, dict):
                return json.dumps({"error": "args_json must be a JSON object"})
        except Exception as exc:
            return json.dumps({"error": f"args_json parse failed: {exc}"})
        try:
            tool = next(iter(perception.snapshot_tools), "")
            result = perception.transform(
                perception.server_id, tool, raw_text, tool_args
            )
            if result is None:
                return json.dumps({"parsed": False, "reason": "adapter passthrough"})
            return json.dumps(
                {
                    "parsed": True,
                    "element_count": result.element_count,
                    "summary": result.summary,
                    "content": result.content,
                },
                indent=2,
                default=str,
            )
        except Exception as exc:
            return json.dumps({"error": f"preview failed: {exc}"})

    log.info("MCP server created (lean v1)")

