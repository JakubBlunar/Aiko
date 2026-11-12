# Task operation approvals

This is the approval contract used by the [agent harness](agent-harness.md).
File operations are supplied by the filesystem MCP plugin; there is no current
built-in `file_write` handler or `write_file` workflow skill. Plugin resource
configuration and operation approvals are separate checks.

## Policy and answers

[`approval.py`](../app/core/tasks/approval.py) resolves policy in this order:

1. Session approve-all (`all` or the specific approval ID).
2. `agent.task_approval_overrides` for that ID.
3. Global `agent.task_approval_mode` (`ask` or `auto`).

The session approve-all set is in memory and is not persisted. Runtime settings
changes use the session's persisted settings path; approving an operation does
not change plugin roots or grant a new account/resource scope.

`build_request` emits a `TaskInputNeeded` with `approve`, `approve all` and `deny`
options. Exact options are accepted first; free text uses a conservative parser.
Empty or ambiguous input denies. The input store keeps the question and answer,
and the child releases its execution invocation while awaiting a decision.

The TaskStrip and Work drawer expose pending approval inputs. Answer through
`POST /api/tasks/{id}/answer` or the corresponding task-input tool. A discussion
or guidance message does not substitute for that answer. The generic brain
`task_input_needed` handler is UI-only; harness-authored work questions have their
own reporting path and do not imply spoken generic-operation approvals.

## External MCP operation boundary

[`mcp_operation_gate.py`](../app/core/tasks/workflow/mcp_operation_gate.py) prepares
the concrete operation from the manager's live exposed descriptor. It validates
arguments against the complete JSON schema and rejects a stale configuration
generation before approval or dispatch.

Effect classification in
[`operation_contract.py`](../app/core/tasks/workflow/operation_contract.py) treats
the known filesystem read operations as `read` and its other operations as
`write`. Other servers use the descriptor effect derived from their tool
contract/annotations; absent knowledge defaults to `unknown`.

- `read` operations proceed without the mutation approval question.
- `write` and `unknown` operations resolve the host approval policy.
- The base capability is `mcp:<server_id>:<tool_name>`; when present, the
  configuration generation qualifies the approval ID. A session approval from
  one generation cannot authorize the same tool against replacement resources.

The approval preview includes concrete arguments with common credential fields
redacted. Saved approval state includes the operation signature: server, tool,
schema, effect and generation. On answer, the handler prepares the current
contract again and refuses a changed signature. Cancellation/admission is checked
before RPC dispatch, and the manager checks the requested generation at its
own boundary. A matching JSON schema alone is insufficient after a roots/account
change.

[`McpToolHandler`](../app/core/tasks/handlers/mcp_tool.py) owns this start/answer/
dispatch flow for every external operation. Its receipt records effect, tool,
argument digest and acknowledgement; acknowledgement is not read-back proof.
A timeout or failure after sending a write/unknown operation retains uncertainty
rather than authorizing a blind repeat. Cancellation cannot roll back a remote
effect already sent.

Procedures apply the same checks to each underlying child operation. Neither a
procedure nor a saved learned method bypasses current policy. Resource scope is
enforced by the plugin/server configuration; this approval system is not an OS
sandbox or a complete job-specific grant system.

## Adding capabilities

Native handlers can use the descriptors in
[`capabilities.py`](../app/core/tasks/capabilities.py), `resolve_approval`,
`build_request` and `parse_decision`, with the controller's resolve/session-mark
callbacks. Their concrete resource validation and revalidation still belong at
the dispatch boundary. External MCP tools already share the generic handler;
adding one does not require a second approval implementation.

The calculator is a synchronous SDK fast tool from `plugins/calculator/`. It is
not an approval consumer; its registration and runtime toggle follow the
[plugin lifecycle](plugin-settings.md).

## Diagnostics

`get_approvals_state` reports global policy, overrides, session approvals and
registered capabilities. Inspect the child task's pending input and events for
an actual wait. `app.tasks.mcp_tool` logs operation completion/failure;
`app.mcp.client` logs server lifecycle. Use disposable resources when testing a
mutation or uncertain outcome. For runtime configuration revocation see
[plugin settings](plugin-settings.md).
