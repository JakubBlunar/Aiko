# Task capabilities, approvals & the `write_file` skill

This doc covers three things that ship together:

1. A **reusable capability + approval framework** every future
   destructive task can plug into.
2. The first consumer: a **`file_write`** capability + workflow skill.
3. A synchronous **`calculate`** tool (unrelated to approvals, but
   shipped in the same pass — exact arithmetic instead of guessed
   numbers).

The design goal is that adding the *next* destructive capability
(shell exec, http post, send email, …) is "declare a capability +
write a handler", with the approval prompt, the per-capability config,
and the UI surface all handled by shared code.

---

## 1. The reusable framework

### Capabilities — `app/core/tasks/capabilities.py`

A `TaskCapability` is a tiny frozen descriptor:

```python
TaskCapability(id="file_write", label="write to a file", destructive=True)
```

- `id` — stable identifier, also the override-map key and the session
  approve-all key.
- `label` — the phrase used in the approval prompt (`"I'd like to
  {label}: ..."`).
- `destructive` — whether actions warrant an approval gate. A
  non-destructive capability never asks.

Handlers register their capability at import time. The process-wide
registry (`register_capability` / `get_capability` / `all_capabilities`)
lets the settings layer + the MCP `get_approvals_state` tool enumerate
what exists without importing every handler.

### Approval gate — `app/core/tasks/approval.py`

Pure helpers, no orchestrator / settings / I/O:

- `resolve_approval(capability_id, *, mode, overrides, session_approved)`
  → `"auto"` (proceed) or `"ask"` (gate). Precedence: **session
  approve-all** (the `"all"` sentinel or the capability id in the set)
  → **per-capability override** → **global mode**.
- `build_request(capability, action_summary)` → the standard
  `TaskInputNeeded` with options `["approve", "approve all", "deny"]`.
- `parse_decision(answer)` → `APPROVE` / `APPROVE_ALL` / `DENY`. Exact
  option strings (the TaskStrip buttons) win first; free text falls
  through a small heuristic; **ambiguous / empty input is `DENY`**
  (fail safe — never perform a destructive action on an unclear
  answer).

### Policy storage

- **Persistent**: `agent.task_approval_mode` (`ask` | `auto`) +
  `agent.task_approval_overrides` (`{capability_id: mode}`).
- **Session-scoped**: `SessionController._approved_capabilities`, an
  in-memory set populated by an `approve all` decision. Never
  persisted — a blanket approval can't silently outlive the session.

The controller injects two callbacks into every destructive handler:
`_resolve_task_approval(capability_id) -> "auto"|"ask"` and
`_mark_capability_session_approved(capability_id)`.

### Handler integration pattern

```python
# start():
resolved = self._resolve(args)
if resolved.destructive and self._resolve_approval(CAP) == "ask":
    emit(build_request(get_capability(CAP), summary))
    return {"args": args, "phase": "awaiting_approval"}
return self._perform_and_complete(...)

# on_input():
decision = parse_decision(answer)
if decision == DENY:
    emit(TaskCompleted(result={"written": False, "declined": True}))
    return {...}
if decision == APPROVE_ALL:
    self._mark_session_approved(CAP)
return self._perform_and_complete(...)  # re-resolves + acts
```

### Workflows can pause for the user — `_wait_child`

For an interactive child handler to actually work inside a goal
workflow, `GoalWorkflowHandler._wait_child` waits **through** a child's
`awaiting_input` status instead of treating the wait timeout as a stall
and cancelling it. A genuine timeout (child still `running`, no
progress) still cancels; a parent cancellation while waiting cancels
the child and reports `cancelled`.

### UI-only, no spoken approval (for now)

Background children are spawned `notify_aiko=False`. The brain-loop
`task_input_needed` handler skips parking a chat cue for
`notify_aiko=False` tasks, so the approval prompt shows up **only** in
the chat-adjacent TaskStrip (clickable approve / approve all / deny) and
Aiko does not speak it. Foreground / Aiko-initiated awaiting-input
(e.g. the file_read multi-root disambiguation, `notify_aiko=True`) is
unchanged — she still asks naturally. A spoken-approval surface is a
future, opt-in addition (see the backlog).

---

## 2. `file_write`

- **Reachable only as a workflow skill** (`write_file`), never as a
  fast brain tool — keeps the brain lane light and centralises the
  destructive op behind the planner.
- **Master switch**: `agent.file_write.enabled` (default `false`).
- **Writable root required**: a `agent.task_file_allowed_roots` entry
  with `read_only: false`. With the switch on but no writable root the
  skill is not offered.
- **Ops**: `write` (create/overwrite), `append`, `replace`
  (find/replace in an existing file).
- **Destructive = modifies an existing file.** Creating a brand-new
  file is non-destructive (still root / extension / byte gated, just no
  approval). Overwrite / append-to-existing / replace ask first (unless
  the policy resolves to `auto`).
- **Safety**: writable-root gating, extension allow-list
  (`agent.file_write.allowed_extensions`), byte cap
  (`agent.file_write.max_bytes`), and **atomic write** (temp file +
  `os.replace`, so a crash mid-write never leaves a half-written file).

### Adding the next destructive capability

1. `register_capability(TaskCapability(id="shell_exec", label="run a
   command", destructive=True))`.
2. Write the handler; inject `resolve_approval` + `mark_session_approved`;
   reuse the `start` / `on_input` pattern above.
3. Add a workflow skill that spawns it `notify_aiko=False`.
4. (Optional) a nested resource-config dataclass like
   `FileWriteSettings`. The *approval* policy is already generic — no
   new approval code.

---

## 3. `calculate`

Synchronous, in-turn tool (`tools.calculate`, default on). Evaluates an
arithmetic expression through `app.core.calc.safe_eval` — an AST
whitelist (numeric literals, `+ - * / // % **`, unary signs,
parentheses, an allow-list of `math` functions + `abs`/`round`/`min`/
`max`, and `pi`/`e`/`tau`). No `eval`, no names, no attribute access,
no collections; exponentiation is bounded. The cure for "what's 18.5%
of 2,340?" being a hallucinated number.

---

## Debugging

- MCP `get_approvals_state` — global mode, overrides, the session
  approve-all set, and every registered capability with its
  `effective_mode` right now.
- `tail_logs(module_contains="file_write")` — `file_write: awaiting
  approval: ...`, `file_write: completed: ...`, `file_write: write
  declined by user`.
- `tail_logs(module_contains="workflow.handler")` — `workflow child
  awaiting input: ... (waiting through for the user's answer)`.
- `tail_logs(module_contains="calc")` — `calculate: expr=... result=...`.
