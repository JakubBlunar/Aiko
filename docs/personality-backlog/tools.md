# Tools and developer diagnostics

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

D1 one-shot reminders are [implemented](shipped/tools.md#d1-local-one-shot-reminders--shipped);
recurrence and OS notifications remain open follow-ups, not part of the one-shot implementation.

## D8. Conversation archive recall with daily overviews

**Status: Partial.**

**Implemented / available:** User-scoped paged conversation_history, daily source outlines across
sessions, drill-down/coverage and transactional edit/deletion invalidation are implemented.

**Remaining:** Generated topic/event summaries with citation/coverage controls, real-model
overview-to-source scenarios and lookup/token benchmarks remain. Reuse the implemented dedicated
tool; the extend-recall-versus-new-tool decision is resolved.

[Implemented slice](shipped/audit-2026-10-24.md#tools-d8).

**Source check:** [archive.py](../../app/core/conversation/archive.py),
[conversation_history.py](../../app/llm/tools/conversation_history.py),
[daily_overview_worker.py](../../app/core/proactive/daily_overview_worker.py),
[test_conversation_archive.py](../../tests/test_conversation_archive.py),
[builtins.py](../../app/llm/tools/builtins.py),
[tool_pass_gate.py](../../app/core/session/tool_pass_gate.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance checks.** Synthetic conversations spanning sessions, local midnight
and daylight-saving changes resolve "last week" and "last month" correctly;
pagination neither skips nor repeats messages; context survives page boundaries;
cross-user rows never leak; broad recaps expose partial coverage; exact quotes
come from source rows rather than summaries. A fact omitted by memory extraction
is still retrievable. Empty dates, unavailable stores, missing summaries, retries,
late messages, corrections and deletions have explicit tested outcomes. Include
tool-gate/skill-router reach checks and real-model scenarios that choose overview
then drill-down without guessing missing dialogue. Measure lookup latency and
token cost before choosing page sizes or an overview-generation cadence.

</details>

## D-approval. Spoken / Aiko-voiced task approvals

**Status: Open implementation.**

**Implemented / available:** Task approvals park in awaiting_input with TaskStrip controls.

**Remaining:** Add opt-in conversational approval wording and unambiguous routing back to the
pending task, preserving one decision across voice/UI. Do not resurrect timed escalation without a
separate explicit policy.

**Source check:** [task_orchestration_mixin.py](../../app/core/session/task_orchestration_mixin.py),
[approval.py](../../app/core/tasks/approval.py),
[prompt_assembler.py](../../app/core/session/prompt_assembler.py).

## DT2. Relationship state inspector — one-shot consolidated snapshot

**Status: Open implementation.**

**Implemented / available:** Per-feature MCP relationship diagnostics exist.

**Remaining:** Implement one bounded consolidated snapshot and decide a read-only UI surface; do not
duplicate store ownership.

**Source check:** [self_state_tools.py](../../app/mcp/server_tools/self_state_tools.py),
[emotion_touch_tools.py](../../app/mcp/server_tools/emotion_touch_tools.py).

## DT3. Feature-flag catalog + "minimal mode" preset

**Status: Open implementation.**

**Implemented / available:** Agent settings expose feature flags individually.

**Remaining:** Add a catalog with current/default/purpose and a reversible minimal-mode preset with
explicit restore semantics.

**Source check:** [agent_settings.py](../../app/core/infra/agent_settings.py).

## DT4. Scenario / conversation replay harness

**Status: Open implementation.**

**Implemented / available:** DT1 clock and MCP messaging/response details exist; T5 has bounded
offline scenario runners.

**Remaining:** Implement shared live-app replay with isolated state, seeded scenarios and block
assertions; exact/model replay is not supplied by the short Live action tail.

**Source check:** [evaluate_social_reasoning.py](../../scripts/evaluate_social_reasoning.py),
[core_tools.py](../../app/mcp/server_tools/core_tools.py).

## DT5. `get_surfacing_outcomes` — did what she brought up land?

**Status: Partial.**

**Implemented / available:** Surfacing leaderboard/lane/echo views, cue outcomes and bounded
persisted assembly traces/offline hypothesis reporting exist.

**Remaining:** Expose a consolidated per-turn trace, explicit whole-store coverage and wider
inner-life read surface with denominators/shrunk estimates. Existing bounded receipts are a partial
trace, not full coverage.

[Implemented slice](shipped/audit-2026-10-24.md#tools-dt5).

**Source check:**
[surfacing_outcome_tools.py](../../app/mcp/server_tools/surfacing_outcome_tools.py),
[surfacing_trace_store.py](../../app/core/memory/surfacing_trace_store.py),
[hypothesis_surface_report.py](../../scripts/hypothesis_surface_report.py).

## D7. Anticipatory routine assistance — act on what she's learned

**Status: Open implementation.**

**Implemented / available:** K3 routines, tasks, approvals and activity intake exist.

**Remaining:** Join high-confidence learned slots to one bounded routine-specific offer/preparation,
with ignore/dismiss behavior and shared C6 intake; destructive work still needs normal approval.

**Source check:** [schedule_learner.py](../../app/core/infra/schedule_learner.py).

## D1-R. Reminder recurrence and OS notification delivery

**Status: open.**

**Implemented / available:** The local one-shot reminder tool/store/cue path is implemented.

**Remaining:** Add explicit recurring rules and OS delivery/cancellation/dedupe. Keep
recurrence/timezone policy and notification permissions separate from the one-shot tool.

**Source check:** [reminders.py](../../app/llm/tools/reminders.py), [tools.md](shipped/tools.md).
