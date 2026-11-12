# Fast brain tools: quality and missing reads

Reviewed 6 November 2026 at `4efe8d23`. This is an evaluation and proposed
implementation order; production behavior has not changed in this review.

Implemented subsequently: [changes and validation](../../docs/fast-brain-tool-improvements.md).
The reproductions below describe the audited baseline; the reusable probe now
checks the repaired contracts.

## Scope and evidence

The live read-only `list_agent_tools` diagnostic reported 34 registered tools:
seven recall/archive/diary readers, one diary writer, four goal tools, three
workflow controls, eleven world tools, two weather tools, time, activity, local
calendar, reminders, web search and the calculator plugin. Registration is not
proof that the gate exposes a tool on the right turn or that the model uses it.
The gate snapshot had no turns yet after restart; this audit does not estimate
live selection or answer accuracy.

Inspected the registry, schemas, dispatch, gate/router, two-pass execution,
retrieval facades and the stores/readers behind the tool families. Ran 521 focused
tests and 15 subtests successfully across tools, schemas, routing, tool execution,
recall, diary/archive, goals/workflows, world, weather/search, calendar/reminders
and self-history, including calculator/plugin and on-demand activity coverage.
Timing markers were excluded. These existing tests mostly pin
current contracts; their passing does not resolve the gaps below.

The reusable [offline probe](../../scripts/evaluate_fast_tool_contracts.py) exercised
18 deliberately selected cases: 15 flagged a contract/capability gap, three were
positive controls. These are targeted reproductions, not an error rate or 15
independent defects. It uses synthetic objects and calls no model, external
service or live writer. The standalone probe needed local async/socket access
for retrieval imports; its restricted-sandbox attempt was interrupted, then the
same probe completed with that access. Lint passed. The affected-test selector
found no static test imports of this new script; the direct smoke run and the
explicit integration selection above supply the execution coverage.

A read-only SQLite snapshot contained 5,518 memories, with 297 content strings
longer than the fast tools' 280-character prefix, including five fact/preference/
event/relationship rows. This counts potential clipping exposure, not rows whose
meaning was actually changed. Subject/date loss also affects short results.

## 1. Preserve qualified recall results and make their sources inspectable

**High priority.** `RecallTool.run` emits only `source`, ranking `score` and the
first 280 characters. `RecallTopicTool.run` emits only `score` and that prefix.
The retriever already carries source identity, dates, temporal framing, subject,
observation mode/domain, inferred-versus-stated provenance and associative scope;
the fast wrappers discard these. Improvements to normal prompt recall therefore
do not automatically improve tool-returned recall.

In the probe, a 688-character Aiko virtual-event record ended with an explicit
qualification that nothing happened physically. Both tools returned exactly the
first 280 characters and lost that qualification along with subject and dates.

`recall_concept` is stronger: it returns the subject, applicability, validity,
source IDs/revisions and uncertainty. Its general evidence projection still
shortens source text to 280 characters and its rationale to 500, without marking
those individual strings as incomplete. Its `all_evidence` schema promises every
supporting memory, while the implementation deliberately caps excerpts at 16.
The bundle's traversal truncation flag is not a character-truncation flag.
The tool also selects one nearest concept across all subjects, with no explicit
subject filter or concept-ID lookup. Similar wording about Aiko and the user can
compete; returning the subject helps detect a mismatch but cannot ask for the
other one directly. Extend this existing read with explicit subject/ID selection.

**Improve:** reuse the authoritative qualified record representation for both
fast recall tools. Keep recorded time distinct from event time; retain subject,
mode/domain, provenance, current/retracted status, source IDs and expansion scope.
Describe the ranking score as relevance, not confidence. Budget whole records or
return an explicitly incomplete navigational preview with a direct full-record
read; do not silently present an arbitrary prefix as a complete memory.

**Missing capability:** a bounded read-only `inspect_memory(memory_id)` (name is
proposed), returning the whole stored memory and its qualified/source receipt,
with original-message context when available. `read_diary(get)` deliberately
rejects ordinary fact IDs. Archive `message` mode reads original messages, not
the memory record or its interpretation. Keep these different authorities clear.
Source inspection should not rewrite the memory or manufacture a new observation.

Acceptance: a late exception survives; Aiko's imagined activity cannot become a
user's physical event; a memory ID returned by recall can be inspected; supplied
source-message IDs can be opened; incomplete coverage is explicit.

Sources: [fast recall](../../app/llm/tools/builtins.py),
[retrieval](../../app/core/rag/rag_retriever.py),
[evidence projection](../../app/core/concepts/evidence_projection.py),
[diary reader](../../app/llm/tools/diary.py),
[archive reader](../../app/llm/tools/conversation_history.py).

## 2. Topic recall needs source status, coverage and honest empty outcomes

**High priority.** Topic recall picks one nearest cluster, sorts members by query
similarity and returns a capped list. It does not return cluster/member totals,
whether more rows were omitted, alternative clusters, a continuation cursor or
source-state qualifications. Its schema says it pulls up everything about a topic.

The probe inserted a synthetic memory marked superseded and inaccurate. Topic
recall returned its text without indicating either status. Historical sources can
be useful, but they must not look like currently supported observations. Reuse
the existing source-revision/eligibility policy, and distinguish historical reads
from assertions about what is still supported.

Both topic and concept retrieval also catch embedding errors and return the same
empty sentinel as an ordinary no-match. Their tools then report successful calls
with `no matching topic cluster` / `no matching concept`. The probe reproduced
both outcomes from an unavailable embedder. The model cannot honestly infer
"I don't have anything about that" from these results.

**Improve:** distinguish `no_match`, `unavailable`, `partial` and valid results at
the facade/tool boundary; report capped coverage and why records were withheld.
Correct the schema's completeness promises. Make broad-topic ambiguity visible
before treating one cluster as all of Aiko's knowledge.

## 3. Reachability does not cover some requests advertised by the schemas

**High priority.** With enabled core families, no prior tool continuity, no active
task and no additional information-need signal, the gate returned `no_signal` for:

- "Why do you think that about me?"
- "What have you figured out about me?"
- "What are you up to?"
- "Can you write a file with those notes?"

The first two are advertised concept-recall requests. The third is an exact
`check_my_work` description example. The last should reach the background
workflow control. A matching schema is irrelevant if the decision pass never
runs. Existing task/previous-tool/premise signals can rescue some real turns;
these probes isolate the missing text routes, not observed live misfires.

**Improve:** add bounded scenario-driven coverage for grounding questions and
direct work requests, then check the selected subset as well as the run/skip
decision. Preserve banter skips and the cost boundary; avoid a blanket always-on
tool pass. Keep conceptual grounding tied to a real referent/context where needed.

Positive control: information-need `premise_recall` already narrows to registered
read-only recall tools even with the router disabled; world/task mutations remain
excluded. Remember/weather requests also reached their families.

Sources: [gate/router](../../app/core/session/tool_pass_gate.py),
[runner](../../app/core/session/turn_runner.py).

## 4. Work status needs honest failures and completed-result inspection

**High priority for correctness.** `check_my_work` catches a failed
`list_running` read, substitutes `[]`, and returns a successful tool result.
The probe's failed read produced exactly the same JSON as a healthy empty store.
Its schema tells Aiko that an empty active list means she is not working.

It can also inspect only active rows and capability gaps. Once a task finishes or
fails, this tool cannot open its ID, inspect its result/failure, or tell whether
an earlier task succeeded. The underlying orchestrator already exposes `get`
and user-scoped history. A disappearing active task is not proof of completion.

**Improve:** surface unavailable/partial task reads. Extend `check_my_work` with
an optional task ID or add a small read-only `read_work` tool. Return the owning
user's task status, dates, last update, result/failure and pending question when
relevant, including terminal tasks. Do not automatically restart a completed or
failed task merely to find out what happened.

Sources: [workflow tools](../../app/llm/tools/workflow_tools.py),
[orchestrator reads](../../app/core/tasks/task_orchestrator.py).

## 5. Fix the time seam and obsolete capability instructions

`get_time` uses `timephrase.now()` with no explicit zone but calls
`datetime.now(ZoneInfo(...))` with one. Under the synthetic narrative clock,
the default read reported 2031 while `timezone=UTC` reported real November 2026.
Both forms should read the same narrative instant and convert its zone.

`look_around` tells Aiko to call `list_file_roots` for files. That tool is absent
from the live fast registry; file access belongs to background workflows. Remove
the obsolete reference and audit cross-tool names/inline-vs-background claims.

Sources: [time tool](../../app/llm/tools/builtins.py),
[world schema](../../app/llm/tools/world.py),
[live registry wiring](../../app/core/session/tools_registry_mixin.py).

## What is worth adding after the basic contracts

1. **Exact memory/source inspection** and **work-by-ID inspection**, as above.
   These close concrete dead ends in already-returned receipts.
2. **Focused present interpretations.** Current situation-specific beliefs are
   distinct from settled concepts and open hypotheses. The fast registry has no
   direct reader for them. A small topic/subject-scoped read could expose the
   interpretation, current applicability/expiry, cited observations, uncertainty
   and alternatives. Reuse BeliefStore's active quality/lifecycle read, not its
   unrestricted diagnostic rows. No new belief-writing tool is needed for this.
3. **Focused relationship/change history.** The fast self-history schema accepts
   only `aiko` and `user`; the history facade can walk other subjects. The separate
   evolution diary also has a facade but no fast reader. Extend an existing read
   with relationship scope and event/period drill-down, keeping subjective
   journalling separate from recorded changes of understanding. A query/concept
   filter would be more useful than an unqualified dump of every era or hunch.

Keep the execution budget in view. The runner allows two pre-stream decision
rounds, and some provider policies limit it to one. Archive overview -> exact
message can fit two; overview -> message list -> long-message continuation cannot
all fit. Prefer qualified bundles, direct source IDs and resumable pagination over
increasing every turn's round allowance. This is a design limit, not a new claim
that the existing provider restriction is wrong.

## Suggested implementation order

1. Repair recall result contracts, topic source status and absence/error semantics;
   add exact memory inspection using the existing stores and receipts.
2. Cover natural grounding/action requests through the gate and selected schemas.
3. Improve task status and add terminal-task/result inspection.
4. Fix the explicit-timezone seam and obsolete schema cross-references.
5. Add focused interpretation/relationship-history reads after those existing
   paths are dependable. Keep capability descriptions compact and lifetime gates
   explicit. A larger tool list by itself does not improve grounded answers.

Archive paging/source drill-down, diary authorship and save receipts, activity
freshness, world book-passage continuation and registry argument validation are
useful existing patterns to reuse. No live messages were sent, no workers were
forced, and no live memory/task/world records were rewritten by this review.
