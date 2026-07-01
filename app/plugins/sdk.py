"""ToolPlugin SDK -- the public surface a plugin's ``entry.py`` codes against.

A plugin is a small Python package. Its ``entry.py`` defines::

    def define_plugin(api):
        api.require_binary("npx")
        root = api.require_config("root")
        api.register_mcp_server(command="npx", args=["-y", "server", root])
        api.register_skills("skills")
        api.register_tool_result_middleware(MyMiddleware())

The runtime ( :mod:`app.plugins.runtime` ) constructs a :class:`PluginApi`,
calls ``define_plugin(api)``, then reads back everything the plugin
registered. The app-side coupling (turning a server spec into an
``ExternalMcpServer``, loading ``SKILL.md`` files) lives in the runtime, NOT
here -- this module keeps a **dependency-light public surface** (stdlib only)
so it can later be extracted into a standalone ``aiko-plugin-sdk`` package for
third-party authors with zero behaviour change.

Trust model: there is no sandbox. A plugin's entrypoint runs in-process, so
"enabled == trusted". The loader only ever hands the runtime plugins that
already resolved to ``enabled=True`` from JSON alone (never importing code to
decide), so a disabled plugin's ``entry.py`` is never imported.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence, runtime_checkable


# ── tool-result middleware contract ───────────────────────────────────


@dataclass(frozen=True, slots=True)
class MiddlewareResult:
    """What a tool-result middleware returns to reshape a tool result.

    Structurally identical to ``app.core.browser.perception.PerceptionResult``
    so the generic ``BrowserPerception`` can be registered unchanged. The
    handler reads ``content`` (full block, capped) + ``summary`` (one line the
    planner sees); ``element_count`` is advisory (logged).
    """

    content: str
    summary: str
    element_count: int = 0


@runtime_checkable
class ToolResultMiddleware(Protocol):
    """Reshape a raw MCP tool result before the planner sees it.

    ``claims`` decides whether this middleware wants to touch a given
    ``(server_id, tool_name)``; ``transform`` returns a reshaped result or
    ``None`` to pass the raw result through unchanged. Both are duck-typed --
    a third-party middleware need not import this Protocol, only match the
    shape.
    """

    def claims(self, server_id: str, tool_name: str) -> bool: ...

    def transform(
        self,
        server_id: str,
        tool_name: str,
        raw_text: str,
        tool_args: dict[str, Any] | None = None,
    ) -> Any | None: ...


class _FilteredMiddleware:
    """Wrap a target middleware with an extra ``server_id`` / tool-name gate.

    Used when a plugin registers a middleware with an explicit ``server_id``
    / ``tool_names`` filter -- the filter is AND-ed with the target's own
    ``claims`` (if it has one). Exposes ``server_id`` so the wiring layer can
    dedupe against the legacy global ``browser_perception``.
    """

    def __init__(
        self,
        target: Any,
        *,
        server_id: str | None,
        tool_names: Sequence[str] | None,
    ) -> None:
        self._target = target
        self.server_id = server_id
        self._tool_names = (
            frozenset(str(t) for t in tool_names) if tool_names else None
        )

    def claims(self, server_id: str, tool_name: str) -> bool:
        if self.server_id is not None and server_id != self.server_id:
            return False
        if self._tool_names is not None and tool_name not in self._tool_names:
            return False
        target_claims = getattr(self._target, "claims", None)
        if callable(target_claims):
            return bool(target_claims(server_id, tool_name))
        return True

    def transform(
        self,
        server_id: str,
        tool_name: str,
        raw_text: str,
        tool_args: dict[str, Any] | None = None,
    ) -> Any | None:
        return self._target.transform(server_id, tool_name, raw_text, tool_args)


# ── gating ────────────────────────────────────────────────────────────


class PluginGatedError(Exception):
    """Raised by the ``require_*`` helpers to gate a plugin out cleanly.

    The runtime catches this and records the plugin as ``gated_out`` with
    the reason, instead of treating it as a hard failure.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ── collected registrations ───────────────────────────────────────────


@dataclass(slots=True)
class _InlineSkill:
    name: str
    description: str
    body: str


@dataclass(slots=True)
class _FastToolSpec:
    """A brain-lane fast tool a plugin contributes.

    ``handler`` is the plugin's own callable, invoked **synchronously** on
    the conversational turn thread (it must be quick — it blocks the reply).
    ``parameters`` is a JSON-Schema object describing the call arguments.

    ``family`` + ``gate_patterns`` feed the P14 tool-pass gate / brain skill
    router (see :mod:`app.core.session.tool_pass_gate`): a tool with a family
    keeps the gate's skip/narrow optimization; a family-less tool degrades
    the gate to always-run (safe, just un-optimized).
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[[dict[str, Any]], str]
    family: str | None = None
    gate_patterns: tuple[str, ...] = ()


# ── the api handed to define_plugin ───────────────────────────────────


class PluginApi:
    """The object a plugin's ``define_plugin(api)`` registers against.

    Constructed per plugin by the runtime. The plugin reads ``config`` /
    ``env`` and calls the ``register_*`` / ``require_*`` methods; the runtime
    then reads back ``server_spec`` / ``skill_dirs`` / ``inline_skills`` /
    ``middlewares``.
    """

    def __init__(
        self,
        *,
        plugin_id: str,
        plugin_root: Path,
        config: dict[str, Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._plugin_id = plugin_id
        self._plugin_root = plugin_root
        self._config = dict(config or {})
        self._log = logger or logging.getLogger(f"app.plugins.{plugin_id}")
        # Collected registrations (read back by the runtime).
        self._server_spec: dict[str, Any] | None = None
        self._skill_dirs: list[str] = []
        self._inline_skills: list[_InlineSkill] = []
        self._middlewares: list[Any] = []
        self._fast_tools: list[_FastToolSpec] = []

    # ── accessors ────────────────────────────────────────────────────

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @property
    def plugin_root(self) -> Path:
        return self._plugin_root

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    @property
    def logger(self) -> logging.Logger:
        return self._log

    def env(self, name: str, default: str | None = None) -> str | None:
        """Read a process environment variable (secrets stay out of config)."""
        val = os.environ.get(name)
        return val if val is not None else default

    # ── gating helpers (replace declarative ``requires``) ─────────────

    def require_binary(self, name: str) -> str:
        """Gate the plugin out unless ``name`` is on PATH. Returns its path."""
        path = shutil.which(name)
        if not path:
            raise PluginGatedError(f"missing binary {name} on PATH")
        return path

    def require_config(self, key: str) -> Any:
        """Gate the plugin out unless ``config[key]`` is truthy. Returns it."""
        val = self._config.get(key)
        if val is None or (isinstance(val, str) and not val.strip()):
            raise PluginGatedError(f"missing config key {key}")
        return val

    def require_env(self, name: str) -> str:
        """Gate the plugin out unless env var ``name`` is set. Returns it."""
        val = os.environ.get(name)
        if not val or not val.strip():
            raise PluginGatedError(f"missing env var {name}")
        return val

    # ── capability registration ──────────────────────────────────────

    def register_mcp_server(
        self,
        *,
        transport: str = "stdio",
        command: str | None = None,
        args: Sequence[str] | None = None,
        env: dict[str, str] | None = None,
        url: str | None = None,
        autostart: bool = True,
        timeout_seconds: float = 30.0,
        expose_tools: Sequence[str] | None = None,
        disabled_tools: Sequence[str] | None = None,
        name: str | None = None,
    ) -> None:
        """Register the MCP server this plugin wraps (id = plugin id).

        Stores a plain spec dict; the runtime turns it into an
        ``ExternalMcpServer`` (that coupling stays app-side so this SDK is
        extractable). Calling twice overwrites the previous spec.
        """
        if self._server_spec is not None:
            self._log.warning(
                "plugin %s registered a second MCP server; overwriting",
                self._plugin_id,
            )
        self._server_spec = {
            "id": self._plugin_id,
            "name": name or self._plugin_id,
            "transport": transport,
            "command": command or "",
            "args": list(args or []),
            "env": dict(env or {}),
            "url": url or "",
            "autostart": bool(autostart),
            "timeout_seconds": timeout_seconds,
            "expose_tools": list(expose_tools or []),
            "disabled_tools": list(disabled_tools or []),
        }

    def register_skills(self, *dirs: str) -> None:
        """Register sub-dir(s) holding ``SKILL.md`` planner guidance.

        Defaults to ``"skills"`` when called with no args. The runtime loads
        the files (relative to the plugin root) and composes the plugin's
        ``mcp:<id>`` group guidance.
        """
        names = [str(d).strip() for d in dirs if str(d).strip()] or ["skills"]
        for name in names:
            if name not in self._skill_dirs:
                self._skill_dirs.append(name)

    def register_skill(self, name: str, description: str, body: str) -> None:
        """Register one inline skill (guidance) without a ``SKILL.md`` file."""
        self._inline_skills.append(
            _InlineSkill(
                name=str(name or "").strip(),
                description=str(description or "").strip(),
                body=str(body or "").strip(),
            )
        )

    def register_tool_result_middleware(
        self,
        middleware: Any,
        *,
        server_id: str | None = None,
        tool_names: Sequence[str] | None = None,
    ) -> None:
        """Register a tool-result middleware (see :class:`ToolResultMiddleware`).

        With no ``server_id`` / ``tool_names`` the middleware's own
        ``claims`` decides everything. Passing either wraps it in an extra
        AND-gate (handy for a middleware that has no ``claims`` of its own).
        """
        if server_id is not None or tool_names is not None:
            middleware = _FilteredMiddleware(
                middleware, server_id=server_id, tool_names=tool_names
            )
        self._middlewares.append(middleware)

    def register_fast_tool(
        self,
        *,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler: Callable[[dict[str, Any]], str],
        family: str | None = None,
        gate_patterns: Sequence[str] | None = None,
    ) -> None:
        """Register a brain-lane fast tool (synchronous, called inline).

        The chat model calls it in-turn like a builtin; ``handler(args)``
        runs on the turn thread and must return a ``str`` (or raise) — keep
        it quick, it blocks the reply. Call this any number of times to ship
        a whole family of tools from one plugin.

        ``family`` + ``gate_patterns`` are optional but recommended: they
        wire the tool into the P14 tool-pass gate / brain skill router so
        the gate can skip / narrow when the turn has no matching signal. A
        tool with no ``family`` still works, but forces the gate to always
        run (``reason="unknown_tool"``).
        """
        tool_name = str(name or "").strip()
        if not tool_name:
            self._log.warning(
                "plugin %s register_fast_tool: empty name, ignored",
                self._plugin_id,
            )
            return
        if not callable(handler):
            self._log.warning(
                "plugin %s register_fast_tool %s: handler not callable, ignored",
                self._plugin_id,
                tool_name,
            )
            return
        patterns = tuple(
            str(p).strip() for p in (gate_patterns or ()) if str(p).strip()
        )
        fam = str(family).strip() if family else None
        self._fast_tools.append(
            _FastToolSpec(
                name=tool_name,
                description=str(description or "").strip(),
                parameters=dict(parameters or {}),
                handler=handler,
                family=fam or None,
                gate_patterns=patterns,
            )
        )

    # ── read-back for the runtime ─────────────────────────────────────

    @property
    def server_spec(self) -> dict[str, Any] | None:
        return self._server_spec

    @property
    def skill_dirs(self) -> list[str]:
        return list(self._skill_dirs)

    @property
    def inline_skills(self) -> list[_InlineSkill]:
        return list(self._inline_skills)

    @property
    def middlewares(self) -> list[Any]:
        return list(self._middlewares)

    @property
    def fast_tools(self) -> list[_FastToolSpec]:
        return list(self._fast_tools)


__all__ = [
    "MiddlewareResult",
    "ToolResultMiddleware",
    "PluginApi",
    "PluginGatedError",
]
