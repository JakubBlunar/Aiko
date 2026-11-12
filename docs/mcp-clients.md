# External MCP-server clients

Aiko exposes an [embedded debug MCP server](../rules/mcp-server.md) and consumes
external MCP servers through a separate client runtime. External tools execute
as background workflow children. They are not automatically added to the
conversation's fast-tool registry; explicit SDK fast tools use a different API.
Start with [the agent harness](agent-harness.md) for the complete work flow.

## Connection and dispatch

[`ExternalMcpManager`](../app/mcp/client/manager.py) owns a dedicated asyncio loop
on a daemon thread. Per-server supervisors connect, initialize the MCP session,
discover tools and maintain/reconnect the transport with backoff. Synchronous
handler threads call `call_tool`, which marshals onto that loop. Server startup
and dependency preparation do not run inline in a conversation turn.

[`register_mcp_skills`](../app/core/tasks/workflow/mcp_skills.py) bridges live
descriptors into `WorkflowSkillRegistry` as `<server_id>__<tool_name>` entries.
The tools-changed callback refreshes the catalogue after asynchronous connection;
workflow menus expose bounded complete schemas with discovery for omitted tools.
The namespace is a capability definition, not a running task.

The planner spawns a child through the existing orchestrator.
[`McpToolHandler`](../app/core/tasks/handlers/mcp_tool.py) validates and dispatches
one concrete operation through the manager. Plugin ownership/configuration
generations follow the descriptor, pending step, approval contract and dispatch.
Tools that disappeared or changed generation require a fresh plan. See
[operation approvals](task-approvals.md) for effect classification and waits.

## Plugins and runtime configuration

Bundled filesystem, browser and source-inspection tools are supplied by
[SDK plugins](plugins.md). [`PluginManager`](../app/plugins/manager.py) owns
selective activation, replacement/removal of registrations, cleanup and desired
versus applied revisions. Settings > Tools > Plugins can enable, disable or apply
configuration without restarting Aiko or unrelated services.

An access reduction revokes the old generation. Pending work cannot reinterpret
old relative paths under replacement roots, even when tool schemas are identical.
An operation already sent may finish; a returned acknowledgement does not undo
revocation or make an uncertain mutation safe to repeat. Current lifecycle and
failure semantics belong to [plugin settings](plugin-settings.md).

Legacy `mcp_clients.servers` configuration remains available for directly
configured servers, gated by `agent.mcp_clients_enabled`. Plugin setup uses
`api.register_mcp_server` rather than the retired declarative plugin manifest.
File read/search/write come from the filesystem plugin, not built-in file
handlers. The core allowed-root sandbox still serves vision/managed attachments;
it is not a second active file-operation catalogue.

## Results and observations

Original MCP text is retained as an owned SQLite result artifact when available.
The inline tool view is capped at 100,000 characters; artifact quotas are
2,000,000 UTF-8 bytes per item and 8,000,000 per job. Unsupported media, truncation
and storage failure remain explicit partiality. Paging saved text does not rerun
the tool or refresh browser targets.

The first claiming tool-result middleware can provide a compact rendered view
and an optional `planner_context` projection. The host attaches task/source/
configuration identity; browser interpretation belongs to the plugin. Old
observations are invalidated across resume/reconfiguration or intervening owner
operations. See [browser perception](browser-perception.md),
[task context](task-context-budget.md) and
[result storage](agent-harness.md#evidence-storage).

## Secrets and logs

Direct-server `env` supports `${ENV:NAME}` indirection from the host environment.
SDK plugins read configuration through `api.config` and environment values through
`api.env`; do not copy credentials into committed manifests. Provider API keys
use the separate provider-credential boundary. This transport guide does not
promise a generic secret editor for every plugin field.

MCP JSON-RPC uses private transport pipes/sessions. Child stderr is forwarded to
per-server `app.mcp.client.<id>` DEBUG logs instead of raw console chatter.
Manager lifecycle uses `app.mcp.client` (`external-mcp connected:` and
`external-mcp connect failed:`). Inspect metadata/status before enabling verbose
logs or making a remote call.

## Debug tools

- `list_external_mcp_servers` and `list_external_mcp_tools`: current connection
  status and exposed descriptors.
- `get_external_mcp_instructions`: captured runtime guidance.
- `get_browser_perception_state` and `preview_browser_perception`: inspect or
  preview perception without a production browser mutation.
- `restart_external_mcp_server`: reconnect one server; this changes runtime state.
- `call_external_mcp_tool`: direct debug invocation, outside task recording and
  the normal workflow approval/reporting path. Use only for explicitly intended
  operations; it is not the read-only status check.

Session wiring lives in
[`task_orchestration_mixin.py`](../app/core/session/task_orchestration_mixin.py)
and [`plugin_settings_mixin.py`](../app/core/session/plugin_settings_mixin.py).
The [platform backlog](../backlog/personality/plugin-system.md) owns proposed
extensions; it does not override these implemented contracts.
