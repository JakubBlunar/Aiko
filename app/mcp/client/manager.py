"""ExternalMcpManager -- connect to external MCP servers as a client.

Owns a dedicated asyncio event loop on a daemon thread (lifecycle modeled
on :class:`app.mcp.runner.McpServerRunner`). For each enabled server it
runs a long-lived *supervisor coroutine* that launches the child process
(stdio) or connects (sse), initializes the MCP session, lists the tools,
then holds the connection open until a restart/shutdown is requested,
reconnecting with backoff on failure.

Synchronous callers (task-handler worker threads) reach a live session via
:meth:`call_tool`, which marshals the coroutine onto the manager loop with
``asyncio.run_coroutine_threadsafe`` and blocks for the result.

Log hygiene: the JSON-RPC protocol runs over each child's private
stdin/stdout pipes, so it never touches our console / ``app.log``. The one
noise source is the child's *stderr* (server diagnostics + ``npx`` install
chatter), which ``stdio_client(errlog=...)`` would otherwise dump raw to
``sys.stderr``. We pass a line-buffered writer that forwards each line into
a per-server logger ``app.mcp.client.<id>`` at DEBUG -- silent at the
default INFO level, but grep-able via ``tail_logs(module_contains="mcp.client")``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.infra.settings import ExternalMcpServer


log = logging.getLogger("app.mcp.client")


# Status constants for a configured server's live connection.
STATUS_DISABLED = "disabled"
STATUS_CONNECTING = "connecting"
STATUS_CONNECTED = "connected"
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"


class McpToolError(RuntimeError):
    """Raised when a tool call can't be dispatched or fails."""


@dataclass(frozen=True, slots=True)
class McpToolDescriptor:
    """One tool advertised by a connected external MCP server."""

    server_id: str
    name: str
    description: str
    input_schema: dict[str, Any]

    @property
    def qualified_name(self) -> str:
        """Namespaced skill name: ``<server_id>__<tool_name>``."""
        return f"{self.server_id}__{self.name}"


@dataclass(slots=True)
class _ServerState:
    server: "ExternalMcpServer"
    status: str = STATUS_CONNECTING
    error: str = ""
    tools: list[McpToolDescriptor] = field(default_factory=list)
    session: Any = None  # mcp.ClientSession, touched only on the loop
    restart_event: Any = None  # asyncio.Event, created on the loop
    # Captured at connect time (schema-free fallback guidance source).
    # ``instructions`` is the ``initialize()`` result's server instructions
    # (many servers ship a short "how to use my tools" blurb here);
    # ``prompts`` is a best-effort ``list_prompts()`` snapshot.
    instructions: str = ""
    prompts: list[dict[str, Any]] = field(default_factory=list)


class _StderrPump:
    """Bridges a child process's stderr into a Python logger.

    The MCP stdio client hands ``errlog`` straight to the subprocess as
    its OS-level stderr (``stderr=errlog`` on ``anyio.open_process``), so
    it must be a real file object with a ``fileno()`` — a plain Python
    writable raises ``'... has no attribute fileno'``. We give the SDK the
    **write end** of an ``os.pipe()`` and drain the **read end** on a
    daemon thread, forwarding each complete line into ``logger`` at DEBUG.
    Net effect matches the AGENTS.md log contract: child stderr (server
    diagnostics + ``npx`` chatter) is silent at INFO, lands in ``app.log``,
    and is grep-able via ``tail_logs(module_contains="mcp.client")``.

    Our write end stays open for the session so the reader thread only
    hits EOF (and exits) when :meth:`close` is called at teardown.
    """

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger
        r_fd, w_fd = os.pipe()
        # Handed to the SDK as the child's stderr target (needs fileno()).
        self.writer = os.fdopen(
            w_fd, "w", buffering=1, encoding="utf-8", errors="replace"
        )
        self._reader = os.fdopen(
            r_fd, "r", encoding="utf-8", errors="replace"
        )
        self._thread = threading.Thread(
            target=self._drain, daemon=True, name="mcp-stderr-pump",
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            for line in self._reader:
                line = line.rstrip("\r\n")
                if line:
                    self._logger.debug("%s", line)
        except Exception:
            pass

    def close(self) -> None:
        # Close the write end so the reader hits EOF, then the read end.
        try:
            self.writer.close()
        except Exception:
            pass
        try:
            self._reader.close()
        except Exception:
            pass
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)


def resolve_env(env: dict[str, str]) -> dict[str, str]:
    """Resolve ``${ENV:NAME}`` indirection from the process environment.

    A value of ``"${ENV:GITHUB_TOKEN}"`` becomes ``os.environ["GITHUB_TOKEN"]``
    (empty string when unset). Plain values pass through unchanged. Lets a
    token live in an env var instead of in ``config/user.json``.
    """
    out: dict[str, str] = {}
    for key, value in (env or {}).items():
        if (
            isinstance(value, str)
            and value.startswith("${ENV:")
            and value.endswith("}")
        ):
            var = value[len("${ENV:") : -1].strip()
            out[key] = os.environ.get(var, "")
        else:
            out[key] = str(value)
    return out


class ExternalMcpManager:
    """Lifecycle owner for outbound MCP-server connections.

    Thread-safe public surface: :meth:`start`, :meth:`stop`,
    :meth:`restart`, :meth:`call_tool`, :meth:`list_available_tools`,
    :meth:`server_status`. Everything that touches a live ``ClientSession``
    is marshalled onto the internal loop.
    """

    def __init__(
        self,
        servers: list["ExternalMcpServer"],
        *,
        connect_timeout: float = 30.0,
        reconnect_min_seconds: float = 2.0,
        reconnect_max_seconds: float = 60.0,
    ) -> None:
        self._servers = list(servers or [])
        self._connect_timeout = max(1.0, float(connect_timeout))
        self._reconnect_min = max(0.5, float(reconnect_min_seconds))
        self._reconnect_max = max(self._reconnect_min, float(reconnect_max_seconds))
        self._states: dict[str, _ServerState] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._shutdown = False
        self._lock = threading.Lock()
        # Optional callback fired (on the loop thread) whenever a server's
        # tool catalogue changes, so the controller can re-register skills.
        self._on_tools_changed = None

    # ── lifecycle ────────────────────────────────────────────────────

    def set_tools_changed_callback(self, cb) -> None:  # noqa: ANN001
        """Register a callback invoked when any server's tools change.

        Called with no arguments. Fired from the loop thread, so the
        callback must be thread-safe / cheap (the controller schedules a
        debounced skill re-registration).
        """
        self._on_tools_changed = cb

    def start(self) -> None:
        """Spin up the loop thread and connect every enabled server."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="mcp-client-manager",
        )
        self._thread.start()
        # Wait briefly for the loop to come up so callers can immediately
        # schedule work; connection happens asynchronously after.
        self._ready.wait(timeout=5.0)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            for server in self._servers:
                state = _ServerState(server=server)
                self._states[server.id] = state
                if not server.enabled or not server.autostart:
                    state.status = STATUS_DISABLED
                    continue
                state.restart_event = asyncio.Event()
                loop.create_task(self._supervise(state))
            self._ready.set()
            loop.run_forever()
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    async def _supervise(self, state: _ServerState) -> None:
        """Per-server connect → list_tools → hold-open → reconnect loop."""
        server = state.server
        backoff = self._reconnect_min
        while not self._shutdown:
            state.status = STATUS_CONNECTING
            try:
                await self._connect_once(state)
                backoff = self._reconnect_min  # reset after a clean session
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                state.status = STATUS_FAILED
                state.error = str(exc)
                state.session = None
                log.warning(
                    "external-mcp connect failed: server=%s err=%s",
                    server.id, exc,
                )
            if self._shutdown:
                break
            # Reconnect with capped exponential backoff.
            await asyncio.sleep(backoff)
            backoff = min(self._reconnect_max, backoff * 2)
        state.status = STATUS_STOPPED
        state.session = None

    async def _connect_once(self, state: _ServerState) -> None:
        """Open one session and hold it until restart/shutdown."""
        from mcp import ClientSession

        server = state.server
        child_log = logging.getLogger(f"app.mcp.client.{server.id}")
        pump: _StderrPump | None = None
        if server.transport == "sse":
            from mcp.client.sse import sse_client

            ctx = sse_client(server.url)
        else:
            from mcp.client.stdio import (
                StdioServerParameters,
                get_default_environment,
                stdio_client,
            )

            child_env = {**get_default_environment(), **resolve_env(server.env)}
            params = StdioServerParameters(
                command=server.command,
                args=list(server.args),
                env=child_env,
            )
            # The SDK uses ``errlog`` as the child's raw stderr fd, so it
            # must have a ``fileno()``. Route it through a pipe → logger;
            # fall back to DEVNULL if the pipe can't be built (never spew).
            try:
                pump = _StderrPump(child_log)
                errlog = pump.writer
            except Exception:
                pump = None
                errlog = subprocess.DEVNULL  # type: ignore[assignment]
            ctx = stdio_client(params, errlog=errlog)

        try:
            async with ctx as streams:
                read_stream, write_stream = streams[0], streams[1]
                async with ClientSession(read_stream, write_stream) as session:
                    init_result = await asyncio.wait_for(
                        session.initialize(), timeout=self._connect_timeout,
                    )
                    await self._capture_guidance(state, session, init_result)
                    await self._refresh_tools(state, session)
                    state.session = session
                    state.status = STATUS_CONNECTED
                    state.error = ""
                    log.info(
                        "external-mcp connected: server=%s tools=%d",
                        server.id, len(state.tools),
                    )
                    if self._on_tools_changed is not None:
                        try:
                            self._on_tools_changed()
                        except Exception:
                            log.debug(
                                "tools-changed callback raised", exc_info=True
                            )
                    # Hold the session open until a restart/shutdown wakes us.
                    if state.restart_event is None:
                        state.restart_event = asyncio.Event()
                    await state.restart_event.wait()
                    state.restart_event.clear()
        finally:
            state.session = None
            if pump is not None:
                pump.close()

    async def _capture_guidance(
        self, state: _ServerState, session: Any, init_result: Any
    ) -> None:
        """Capture server ``instructions`` + ``list_prompts()`` (best-effort).

        These are the runtime-captured fallback guidance source for the
        planner: a plugin's ``SKILL.md`` wins, but a plain (non-plugin)
        MCP server that ships instructions still teaches the planner how
        to use its tools. Never raises — a server that doesn't implement
        prompts just yields an empty list.
        """
        try:
            state.instructions = str(
                getattr(init_result, "instructions", "") or ""
            ).strip()
        except Exception:
            state.instructions = ""
        prompts: list[dict[str, Any]] = []
        try:
            result = await session.list_prompts()
            for prompt in getattr(result, "prompts", []) or []:
                prompts.append(
                    {
                        "name": str(getattr(prompt, "name", "") or ""),
                        "description": str(
                            getattr(prompt, "description", "") or ""
                        ),
                    }
                )
        except Exception:
            prompts = []
        state.prompts = prompts

    async def _refresh_tools(self, state: _ServerState, session: Any) -> None:
        result = await session.list_tools()
        allow = set(state.server.expose_tools or ())
        deny = set(state.server.disabled_tools or ())
        descriptors: list[McpToolDescriptor] = []
        for tool in result.tools:
            if allow and tool.name not in allow:
                continue
            if tool.name in deny:
                continue
            schema = tool.inputSchema if isinstance(tool.inputSchema, dict) else {}
            descriptors.append(
                McpToolDescriptor(
                    server_id=state.server.id,
                    name=tool.name,
                    description=str(tool.description or ""),
                    input_schema=schema,
                )
            )
        state.tools = descriptors

    def stop(self) -> None:
        """Stop the loop, close every session, terminate child processes."""
        self._shutdown = True
        loop = self._loop
        if loop is not None and loop.is_running():
            # Wake every supervisor so its hold-open await returns, then
            # stop the loop. Closing the loop tears down the async context
            # managers (which kill the child processes).
            def _wake_all() -> None:
                for state in self._states.values():
                    if state.restart_event is not None:
                        state.restart_event.set()
                loop.call_later(0.2, loop.stop)

            try:
                loop.call_soon_threadsafe(_wake_all)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def restart(self, server_id: str) -> bool:
        """Force a reconnect of one server (re-reads tools)."""
        state = self._states.get(server_id)
        loop = self._loop
        if state is None or loop is None or state.restart_event is None:
            return False
        loop.call_soon_threadsafe(state.restart_event.set)
        return True

    # ── tool dispatch ────────────────────────────────────────────────

    def call_tool(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Any:
        """Call a tool on a connected server and return its ``CallToolResult``.

        Thread-safe: marshals the call onto the manager loop. Raises
        :class:`McpToolError` if the server is unknown / not connected, or
        the call times out / fails.
        """
        state = self._states.get(server_id)
        if state is None:
            raise McpToolError(f"unknown MCP server: {server_id!r}")
        loop = self._loop
        session = state.session
        if loop is None or session is None or state.status != STATUS_CONNECTED:
            raise McpToolError(
                f"MCP server {server_id!r} not connected (status={state.status})"
            )
        call_timeout = float(timeout if timeout else state.server.timeout_seconds)
        coro = session.call_tool(
            tool_name,
            arguments or {},
            read_timeout_seconds=timedelta(seconds=call_timeout),
        )
        try:
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            return fut.result(timeout=call_timeout + 5.0)
        except McpToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise McpToolError(
                f"MCP tool call failed: {server_id}/{tool_name}: {exc}"
            ) from exc

    # ── introspection (thread-safe reads) ────────────────────────────

    def list_available_tools(self) -> list[McpToolDescriptor]:
        """All tools across connected servers (namespaced, dedupe-safe)."""
        out: list[McpToolDescriptor] = []
        for state in self._states.values():
            if state.status == STATUS_CONNECTED:
                out.extend(state.tools)
        return out

    def server_instructions(self, server_id: str) -> str:
        """The captured ``initialize()`` instructions for a server (or "")."""
        state = self._states.get(server_id)
        return state.instructions if state is not None else ""

    def list_prompts(self, server_id: str) -> list[dict[str, Any]]:
        """The captured ``list_prompts()`` snapshot for a server (or [])."""
        state = self._states.get(server_id)
        return list(state.prompts) if state is not None else []

    def captured_group_guidance(self) -> dict[str, str]:
        """``{"mcp:<id>": guidance}`` from captured server instructions/prompts.

        The runtime-captured fallback guidance source (folded in below a
        plugin's ``SKILL.md`` by the wiring). Only connected servers that
        actually shipped instructions and/or prompts appear.
        """
        out: dict[str, str] = {}
        for state in self._states.values():
            if state.status != STATUS_CONNECTED:
                continue
            blocks: list[str] = []
            if state.instructions.strip():
                blocks.append(state.instructions.strip())
            if state.prompts:
                lines = [
                    f"- {p.get('name', '')}: {p.get('description', '')}".rstrip(
                        ": "
                    )
                    for p in state.prompts
                    if p.get("name")
                ]
                if lines:
                    blocks.append("Available prompts:\n" + "\n".join(lines))
            if blocks:
                out[f"mcp:{state.server.id}"] = "\n\n".join(blocks)
        return out

    def server_status(self) -> list[dict[str, Any]]:
        """Debug snapshot of every configured server."""
        snapshot: list[dict[str, Any]] = []
        for state in self._states.values():
            snapshot.append(
                {
                    "id": state.server.id,
                    "name": state.server.name,
                    "transport": state.server.transport,
                    "status": state.status,
                    "error": state.error,
                    "tool_count": len(state.tools),
                    "tools": [t.name for t in state.tools],
                    "has_instructions": bool(state.instructions.strip()),
                    "prompt_count": len(state.prompts),
                }
            )
        return snapshot


__all__ = [
    "ExternalMcpManager",
    "McpToolDescriptor",
    "McpToolError",
    "resolve_env",
    "STATUS_CONNECTED",
    "STATUS_CONNECTING",
    "STATUS_DISABLED",
    "STATUS_FAILED",
    "STATUS_STOPPED",
]
