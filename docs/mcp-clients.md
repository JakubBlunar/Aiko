# External MCP-server clients (Phase 1)

Aiko has always been an MCP **server** (`app/mcp/server.py` exposes debug
tools over SSE). This adds the **client** half: the app launches / connects
to external MCP servers at boot, discovers their tools, and registers those
tools as **background-worker skills only**.

> **Lane rule.** MCP tools land **only** in the `WorkflowSkillRegistry`
> (the goal-workflow planner / background lane). They are **never** added to
> the brain's fast `ToolRegistry`. The brain keeps its existing custom file
> tools; the filesystem MCP server (when configured) runs *alongside* them,
> used only by background workers.

## Architecture

```
config: mcp_clients.servers[]
        │
        ▼
ExternalMcpManager  (daemon thread + asyncio loop)
  • per-server supervisor coroutine: stdio_client → ClientSession.initialize → list_tools
  • holds the session open; reconnects with backoff on drop
  • call_tool(...) via run_coroutine_threadsafe  (thread-safe for handler threads)
        │ discovered tools
        ▼
register_mcp_skills()  →  WorkflowSkillRegistry  (background lane only)
        │
        ▼
GoalWorkflowHandler planner (worker LLM) picks skills via describe_for_planner()
        │ spawn child task
        ▼
TaskOrchestrator → McpToolHandler.start() → manager.call_tool() → result → TaskCompleted
```

### Components

| File | Role |
|------|------|
| [`app/core/infra/settings.py`](../app/core/infra/settings.py) | `ExternalMcpServer` / `ExternalMcpSettings` dataclasses + parsers; `agent.mcp_clients_enabled`. |
| [`app/mcp/client/manager.py`](../app/mcp/client/manager.py) | `ExternalMcpManager` — owns the asyncio loop on a daemon thread, supervises each server, exposes `call_tool` / `list_available_tools` / `server_status` / `restart` / `stop`. |
| [`app/core/tasks/handlers/mcp_tool.py`](../app/core/tasks/handlers/mcp_tool.py) | `McpToolHandler` (`HANDLER_MCP_TOOL`) — one generic handler proxies every MCP tool call; flattens text content, emits `TaskCompleted` / `TaskFailed`. |
| [`app/core/tasks/workflow/mcp_skills.py`](../app/core/tasks/workflow/mcp_skills.py) | `register_mcp_skills()` — converts discovered tools into namespaced `WorkflowSkill`s whose `spawn` starts a `HANDLER_MCP_TOOL` child task. |
| [`app/core/session/task_orchestration_mixin.py`](../app/core/session/task_orchestration_mixin.py) | `_init_external_mcp()` builds + starts the manager after the workflow handler, registers the handler + skills; `_shutdown_task_orchestration()` stops it. |

## Lifecycle

1. **Boot** (`_init_external_mcp`, gated on `agent.mcp_clients_enabled` + a non-empty enabled server list, only reached when `agent.workflow_enabled`): build `ExternalMcpManager`, register `McpToolHandler`, `start()` the manager, and run `register_mcp_skills` once.
2. **Connect (async).** Each server's supervisor coroutine launches the child (stdio) / connects (sse), `initialize()`s, `list_tools()`, caches the catalogue, fires the **tools-changed callback** (which re-runs `register_mcp_skills`), then holds the session open. Because connection is async (an `npx` cold start can take seconds), the immediate boot-time registration usually finds 0 tools — the callback lands the real catalogue moments later, no restart needed.
3. **Call.** A handler worker thread calls `manager.call_tool(server_id, tool, args)`, which marshals the coroutine onto the manager loop with `run_coroutine_threadsafe` and blocks for the result.
4. **Reconnect.** On a dropped session the supervisor loops with capped exponential backoff (`status='failed'` in between).
5. **Shutdown.** `manager.stop()` wakes every supervisor, stops the loop, and tears down the async context managers — which terminate the child processes.

## Secrets

`env` values support `${ENV:NAME}` indirection, resolved from the process
environment at launch (`resolve_env` in the manager). So a token lives in an
env var, not in `config/user.json`:

```json
"env": { "GITHUB_TOKEN": "${ENV:GITHUB_TOKEN}" }
```

Keychain-backed write-only secret storage + a Settings UI is deferred to
Phase 3.

## Log hygiene

The JSON-RPC protocol runs over each child's **private** stdin/stdout pipes
(the SDK owns them), so protocol traffic never reaches our console /
`app.log` / ring buffer. The one noise source is the child's **stderr**
(server diagnostics + `npx` install/progress chatter), which
`stdio_client(errlog=...)` would otherwise dump raw to `sys.stderr`. The
manager passes a line-buffered writer that forwards each line into a
per-server logger `app.mcp.client.<id>` at **DEBUG** — silent at the default
INFO level, lands in `app.log`, grep-able via
`tail_logs(module_contains="mcp.client")`. The manager itself logs lifecycle
lines under `app.mcp.client` (`external-mcp connected:`, `external-mcp
connect failed:`).

## MCP debug tools

On the embedded debug server (`app/mcp/server.py`):

- `list_external_mcp_servers()` — status + tool counts per configured server.
- `list_external_mcp_tools()` — every discovered tool (`server_id`, `name`, `qualified_name`, `description`, `input_schema`).
- `call_external_mcp_tool(server_id, tool, args_json)` — call a tool end-to-end, no task row, no Aiko. Fastest reachability check.
- `restart_external_mcp_server(server_id)` — force one server to reconnect (re-reads tools).
- `get_browser_perception_state()` — perception layer status (enabled / server_id / adapter / memory pages / last summary). See [browser-perception.md](browser-perception.md).
- `preview_browser_perception(raw_text, args_json)` — run the perception pipeline on a pasted snapshot, no live browser.

## Tool filtering: allow-list vs deny-list

Each server row supports two complementary filters (applied in
`ExternalMcpManager._refresh_tools`, before tools become skills):

- `expose_tools` — *allow-list*. When non-empty, ONLY these tool names register.
- `disabled_tools` — *deny-list*. Tool names to drop even if they pass the allow-list. Convenient for hiding a few unwanted tools without enumerating everything you keep.

```json
{ "id": "browser", "command": "npx", "args": ["-y", "real-browser-mcp"],
  "disabled_tools": ["browser_console", "browser_network", "browser_evaluate", "browser_handle_dialog"] }
```

## Filesystem MCP proof

Add to `config/user.json` (gitignored):

```json
{
  "agent": { "mcp_clients_enabled": true, "workflow_enabled": true },
  "mcp_clients": {
    "servers": [
      {
        "id": "filesystem",
        "name": "Filesystem",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/Users/you/Documents"]
      }
    ]
  }
}
```

Restart the app, then verify over the debug MCP:

1. `list_external_mcp_servers()` → the `filesystem` row shows `status: "connected"` and a non-zero `tool_count`.
2. `list_external_mcp_tools()` → tools like `filesystem__read_text_file`, `filesystem__list_directory`, … (namespaced).
3. `call_external_mcp_tool("filesystem", "list_directory", "{\"path\": \"C:/Users/you/Documents\"}")` → directory listing.
4. The tools are visible to the **background planner only** — a `start_workflow` run can pick `filesystem__*` skills; the brain's fast tool list is unchanged.

The custom file tools (`FileRead/Search/Write`) keep working alongside the
filesystem MCP server; retiring them is Phase 4.

## Browser MCP + perception layer

`real-browser-mcp` (`npx -y real-browser-mcp` + a Chrome extension) is just
another stdio server row. On top of it, the optional **browser perception
layer** reshapes the raw accessibility snapshot into a compact, ranked,
deduped, form-grouped, change-diffed page model before it reaches the
planner — and it's **server-agnostic** (swap the MCP server, keep the
optimizations). See [browser-perception.md](browser-perception.md) for the
full design, the adapter contract, and the swap runbook.

## Later phases (not built)

- **Settings UI + keychain.** A drawer panel + `/api/mcp/servers` CRUD + `mcp_servers_changed` WS, cloning the `llm.providers` pattern, plus keychain-backed secret env vars.
- **Retire the custom file tools** once the filesystem MCP server is validated.
