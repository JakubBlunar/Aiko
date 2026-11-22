# Tools and developer diagnostics

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

D1 one-shot reminders are [implemented](shipped/tools.md#d1-local-one-shot-reminders--shipped);
recurrence and OS notifications remain open follow-ups, not part of the one-shot implementation.

## Fast brain tool quality review (6 Nov 2026)

**Status: Implemented; ordinary-use review pending after restart.** The
[fast-tool audit](../reviews/fast-brain-tools-audit-2026-11-06.md) checks the live 34-tool
inventory, return contracts, gate/router reachability and source inspection gaps.
An offline synthetic probe reproduces lost recall qualifications, unmarked
withdrawn topic records, failures reported as empty results, missing natural
request routes, the explicit-timezone clock bypass and an obsolete tool reference.

The [implementation record](../../docs/fast-brain-tool-improvements.md) covers qualified
recall and `inspect_memory`, topic coverage/error status, natural request routing,
owned task-result inspection, clock/schema cleanup, `recall_beliefs` and focused
relationship/evolution-history reads. Existing stores and execution-round limits
remain authoritative. Structural checks do not establish live-model accuracy.

After restart, review whether natural grounding questions select the right
subject, omitted text is inspected before asserting its meaning, finished work is
reported from its saved result, and current interpretations remain tentative.
Historical understanding must retain its period rather than imply the user or
relationship changed. Older belief quotes without full-message revisions remain
explicitly unverified beyond the quoted span; no historical rows were rewritten.

## D8. Conversation archive recall with daily overviews

**Status: Partial.**

**Implemented / available:** User-scoped paged conversation_history, daily source outlines across
sessions, drill-down/coverage and transactional edit/deletion invalidation are implemented.
The [28 October slice](../../docs/conversation-archive.md#archive-review-and-generated-summaries-28-october-2026)
adds the Archive review UI, explicit durable idle-time summary requests, typed source-cited
discussion bullets and original-message drill-down, with bounded context and source invalidation.

**Remaining:** Real-model recap faithfulness and overview-to-source scenarios, end-to-end
latency/token measurements, semantic topic search, cross-day correction links and an explicit cost
policy for automatic historical generation. Reuse the implemented dedicated tool; the
extend-recall-versus-new-tool decision is resolved.

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

## D9. Bookmark a past exchange

**Status: Open.** Give the user a way to save a specific exchange and find it
again by its original date. Aiko's life and relationship with the user follow
one timeline. Returning to something they said about piano last week is a
conversation happening now, with the intervening days and later decisions
still part of her context.

**First slice.** Save one user-owned message anchor with an optional short
label. Open a dated preview through existing history while staying in the
current chat. Bookmark creation and viewing are UI actions. A future “bring
this up with Aiko” action would need a separate paired test and would use the
existing source-backed history path to cite the exchange as past dialogue
in a new turn. Where a choice or plan has since changed, the later evidence
still determines what is current. [Session continuity](../../app/core/session/session_continuity.py), [history contract](../../docs/conversation-archive.md).

**Acceptance.** Bookmark a discussion about a plan, change the plan in a
later chat, then open the bookmark after restart. The preview shows the
original exchange and its original date. Check that opening it leaves the
active chat, current time, world state and return cues untouched. An altered
or deleted source shows its status when opened. Before considering a chat
action, test that Aiko can discuss the earlier plan while honoring the later
change and remembering the conversation in between.

## D10. Show what changed since a particular conversation

**Status: Partial.** Aiko's history tool can compare later reports on a
literal topic against an exact earlier exchange or resolve it from a unique
current source link. It returns original-message
IDs, separate report and event times, and bounded coverage. Current
follow-through and correction receipts supply links to earlier statements.
Fixtures cover an unrelated similar report, repeated wording, later memory
extraction, edited or deleted sources, and a window ending before the latest
chat. The isolated 9B chat replay resolved the topic-only purifier request and
attributed the later correction to the user on seeds 53 and 54.
[Read contract](../../docs/conversation-archive.md#changes-since-an-exchange), [regressions](../../tests/test_conversation_archive.py).

**Remaining:** Check broader natural references in ordinary chat, whether the
model asks when several exchanges fit, and whether it preserves the reported
versus event dates in its answer. Literal matching
can miss paraphrases and unmarked reports. Measure those misses before trying
semantic retrieval or adding more categories of change.

## D11. Rehearse a conversation in a disposable draft

**Status: Open.** Let the user try different ways of saying something with
Aiko before it becomes part of their conversation. For example, the user can
give her an unsent message and ask how it might land, or ask her to play the
other side for one exchange. The result is visibly a rehearsal and can be
copied out or discarded.

Ordinary reply retry removes newer transcript rows before regenerating. The
global `remember_history` setting selects the history key for ordinary turns.
A draft needs its own evidence boundary so fictional dialogue cannot influence
what Aiko learns about the user. [Retry path](../../app/core/session/message_history.py), [ordinary turn](../../app/core/session/chat_turn_mixin.py), [history setting](../../docs/configuration.md).

**First slice.** Use a text-only preview action with user-supplied draft and
optional selected context. Run a single response on the normal chat provider
through a path that returns draft text without committing a chat turn. The
preview has no turn tags, tool calls, memory indexing, cue consumption or
post-turn producers. If the user later sends edited text as an ordinary
message, only that explicit message becomes part of the conversation. Keep
simulated roles and responses identifiable in the preview.

**Acceptance.** Rehearse a fictional disagreement and generate two variants.
Neither creates a transcript row, a shared event, a belief update, a reminder,
or an outstanding repair. Closing, cancelling or retrying a draft preserves
the original chat. The user can copy a variant without the app sending it.

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
