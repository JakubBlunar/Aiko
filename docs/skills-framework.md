# Skills, tool families and capability disclosure

Aiko has two execution lanes. This guide owns how capabilities reach the model;
[the agent harness](agent-harness.md) owns what happens after work starts.
There is no shared `Skill`/`BrainSkill` class hierarchy: brain families and
`WorkflowSkillRegistry` are the existing implementations.

| Term | Current representation | Execution |
| --- | --- | --- |
| Brain tool | `Tool` registered in the per-turn `ToolRegistry` | Returns inline during a conversation tool pass |
| Brain family | `_TOOL_FAMILY` plus routing patterns | Selects which fast-tool schemas a turn receives |
| Workflow skill | `WorkflowSkill`: name, description, full input contract, group, effect, generation and spawn function | Creates an owned child task; terminal/control actions are handled by the harness |
| Task | SQLite execution row with handler/state/result and parent identity | Orchestrator runs and records the invocation |
| Profile | General, reader, researcher, browser, writer or reviewer | Selects work instructions and admissible operation types |
| Procedure/playbook | Plugin-owned bounded calls or optional instructions | Uses the same disclosed contracts and dispatch checks |

External MCP operations are worker-only. Read-only brain tools can be explicitly
shared into the workflow catalogue as saved child tasks; the reverse does not
expose filesystem/browser mutations inline in conversation. Synchronous search
and other existing fast tools retain their documented conversation path.

## Brain-lane progressive disclosure

[`tool_pass_gate.py`](../app/core/session/tool_pass_gate.py) first decides whether
a tool pass is needed, then `select_active_tool_names` chooses its schema subset.
`agent.skill_router_enabled` defaults to true. For a specific family signal, the
subset contains matching registered tools plus the configured core families
(`agent.brain_core_skills`, default `time`, `recall`, `world`). Core membership
exposes schemas when a pass runs; it does not force a pass on every message.

Continuity and conservative fallback reasons widen to all registered tools:
forced passes, finished work, active tasks, tool follow-up, unknown tool families
and generic requests. A closed acknowledgement can skip a follow-up pass.
The dedicated `premise_recall` decision selects only recall tools. Family/pattern
metadata supplied by an SDK fast tool joins the same routing boundary.

Registration still obeys settings, availability and ownership. Narrowing selects
among registered tools; it does not enable disabled capabilities. Add a new core
tool to `_TOOL_FAMILY` and describe what/when/how it runs in its schema, rather
than appending conditional tool instructions to the always-on persona.

## Checkpointed workflow disclosure

The current harness uses [`profiles.catalogue`](../app/core/tasks/workflow/profiles.py)
and lexical [discovery ranking](../app/core/tasks/workflow/discovery.py), not the
legacy group router as its primary menu. A planning step selects up to six
complete operation schemas under the dynamic [context budget](task-context-budget.md).
Profile, task relevance, explicit discovery and pins determine the candidate set.
A schema that cannot fit is omitted whole, never shortened into a different
contract. A manifest advertises omitted capabilities for discovery.

`discover {query,page}` searches/pages the catalogue; `discover {names:[...]}`
pins up to six exact contracts. An undisclosed registered name causes discovery
and a fresh decision, not execution of hidden arguments. Input schema, effect,
current generation, ownership and approval policy are checked before dispatch.
A name in a manifest is not authorization.

Mandatory plugin operation guidance accompanies the selected capability. Optional
`read_playbook` guidance loads on demand by current plugin generation. A
`run_procedure` definition must also survive that decision's budget selection.
See [plugin procedures](agent-harness.md#plugin-procedures-and-playbooks) for the
SDK syntax and restart/admission contract.

## Legacy worker group router

`agent.workflow_skill_router_enabled` defaults to false. It applies to the older
non-checkpointed planning path's `_planner_skills`, not the harness's bounded
`catalogue` selection. The keyword classifier in
[`workflow_skill_router.py`](../app/core/tasks/workflow/workflow_skill_router.py)
returns one matching available group or the full menu on zero/multiple matches.
Built-in keyword groups include web/vision; MCP groups require a server-name
match. Older `files` keywords do not supply built-in file operations.

### Worker-router default-on gate

Keep this legacy narrowing opt-in. A goal spanning web research and a filesystem
plugin can match only `web` and hide needed MCP tools, despite the full-menu
fallback for multiple *detected* groups. Enabling the flag is not required to
use bounded discovery in checkpointed work. Any broader router change belongs
in the [workflow backlog](../backlog/plans/agent-workflows.md) with mixed-goal
coverage and an honest missing-capability result.

## Ownership and verification

Brain schema selection belongs to `tool_pass_gate`; worker capability
registration belongs to
[`skill_registry.py`](../app/core/tasks/workflow/skill_registry.py) and the
[MCP bridge](mcp-clients.md). The host owns allowance, persistence and approvals;
plugins own their operation semantics and optional browser projections.

Focused routing tests cover family selection, conservative fallbacks and plugin
metadata; workflow harness/operation tests cover complete schemas, discovery,
child ownership and stale generations. Model selection quality and real browser
success require separate ordinary-use evidence.
