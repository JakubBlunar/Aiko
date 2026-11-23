# Output correctness for Aiko's local agentic framework

Reviewed the checkpointed workflow at `2a6fb0c0` on 23 November 2026. The design target is a companion that can research, compare, prepare and carry out requests with one small local model. The saved workflow route points to `baytout3/qwen3.5-uncensored:9b` with 65,536 tokens. An exact 8B checkpoint would need its own measurements.

## Where the current contract breaks

| Priority | Observed behavior | Proposed change and acceptance case |
| --- | --- | --- |
| 1. Evidence can come from the request itself | Native web search saves the query, hits and generated summary together. The reviewer can quote any substring of the serialized result. In an isolated production review, a scripted approval cited the query and obtained a certificate for opening time and accessibility. The only returned snippet said “See map.” Criterion records require a boolean and index. One quote for a whole sentence can cover several factual clauses. | Give each usable observation an ID and a field of origin. For native search, expose each hit's title, URL and snippet individually. Keep the query and execution summary available for diagnostics. Require a source field for each factual criterion and each independent material claim. In the fixture above, both criteria should stay unresolved. A source with only the hours should produce a useful partial answer. |
| 2. A read receipt can establish submission | Successful MCP operations, including reads, receive an acknowledged receipt. The planner's submitted branch accepts any acknowledged, completed child of the task. An isolated workflow with only a read of a blank form finished as submitted. | Bind a submission receipt to the intended effect, resource and operation. A read of a booking form can establish its contents. A provider acknowledgement for the requested booking can establish submission. A matching confirmation can establish the booking. Check all three labels against the same request. |
| 3. A verification read can be blocked | The standalone skill loop fingerprints the operation name, arguments and configuration generation for the entire job. A scripted read A, read B, read A ended as blocked with two completed children. A read of the same resource after editing it, or a fresh snapshot after interacting with a page, has the same fingerprint. Plugin procedures use a separate step path. | Track why another read is needed. Allow a bounded fresh observation after a relevant state change, scheduled wait or stale target. Count identical observations without progress toward a loop limit. Keep the existing handling for an interrupted mutation with an unknown outcome. Exercise both standalone and procedure paths. |
| 4. Work discussion can contradict a verified report | The main reporter checks sources. Work discussion sees a 4,000-character root result and up to eight recent children with 1,500 characters each. Saved model notes share the frame. A nonempty discussion reply can be persisted without a source check. Planner-authored milestone and reply text also enters the work log before spoken-report review. | Let discussion retrieve bounded passages from owned saved results. Render execution status directly and answer factual follow-ups from source fields under the same rules used for final answers. Test a fact on page two of the ninth most recent child, with a plausible wrong answer in the planner notes. |

The first three rows were exercised with scripted inference and fictional data. The fourth follows from the current output paths. [Completion review](../../app/core/tasks/workflow/harness_results.py), [review validation](../../app/core/tasks/workflow/review_contract.py), [native search](../../app/core/tasks/handlers/web_search.py), [MCP receipts](../../app/core/tasks/handlers/mcp_tool.py), [workflow actions](../../app/core/tasks/workflow/harness_mixin.py), [work discussion](../../app/core/tasks/workflow/work_discussion.py).

## What the installed worker actually did

Ran the production completion reviewer in a standalone process using fictional in-memory child results and the installed 9B workflow model on loopback. Each request used a 65,536-token context, reasoning enabled, seed 71 and temperature zero. The proposed answer and search query both said that Orchid Hall opens at ten and has step-free access. Only the returned snippet changed.

| Returned snippet | Expected verdict | Actual verdict | Prompt tokens | Generated tokens |
| --- | --- | --- | ---: | ---: |
| Both facts about Orchid Hall | Support | Supported | 749 | 4,642 |
| Directions to a venue map | Reject | Unsupported | 745 | 4,694 |
| Both facts about Iris Hall | Reject | Unsupported | 740 | 3,967 |

The 9B reviewer distinguished these three cases. The scripted reviewer showed that the host would admit the query-only claim if the model made the wrong judgment. These observations answer different questions. Across three calls, provider usage counted 13,303 generated tokens. This single fixture and seed exercises completion review only. The planner, spoken report, ordinary conversation and an exact 8B model still need end-to-end evaluation. Separately, 55 existing correctness and report-grounding checks and three temporary diagnostic checks passed with the managed test runner.

## How new capabilities should fit

Start with one end-to-end companion request. For example, “Compare the two workshops I mentioned, then remind me the evening before the cheaper one.” The workflow needs the two referenced items from chat, comparable prices and currencies, the date and timezone, and an acknowledgement for the user's requested reminder. Each can fail independently. Aiko should still return the comparison if the reminder needs clarification.

1. **Bind the request.** Save the referenced messages or attachments with ownership, revision and the user's exact constraints. When Aiko asks a clarifying question, carry the question and answer together on resume. The current handoff retains the active utterance, and the question and subsequent guidance are stored as separate events. A bare “the second one” is an expensive ambiguity to pass to the worker. [Workflow start](../../app/llm/tools/workflow_tools.py), [input resumption](../../app/core/tasks/workflow/goal_workflow_handler.py).
2. **Make the result of each capability explicit.** Extend the existing plugin evidence contract so a capability can return observed fields, source, subject, observation time and coverage. Use host checks for IDs, amounts, units, timezones and persisted values. Keep semantic review for interpretation. Retain proposals or drafts as authored deliverables with checkable constraints. “Suggested schedule”, “saved reminder” and “notification delivered” need distinct evidence. [Plugin contract](../../app/plugins/sdk.py), [existing text-note adapter](../../plugins/filesystem/aiko_files/evidence.py).
3. **Reuse the companion's services.** A first multi-step action adapter could call the existing user-scoped reminder service and verify the saved reminder ID and due time. Reuse its settings and existing action policy. The workflow fast-tool bridge currently shares reads. The service already has separately observable scheduling and delivery. [Fast-tool bridge](../../app/core/tasks/workflow/fast_tools.py), [reminder behavior](../../docs/tools.md#local-reminders-d1).

## Spending the 64K window

Aiko already has whole-schema discovery, recoverable result pages, typed plugin procedures, sequential profiles and a shared inference gate. Keep those. The planner normally targets up to 12,288 input tokens, with reasoning headroom up to 8,192 per call at this window. Reservations describe capacity. Measure actual prompt and generated tokens per completed task. [Task budgeting](../../docs/task-context-budget.md).

- **Constrain the next decision.** The transport supports JSON Schema. The planner and verifier currently request generic JSON. Pilot a small schema per phase with the disclosed actions and eligible evidence IDs. Return a plan or notes only when they change. Include a discovery path for capabilities outside the current menu. Benchmark format repairs separately from wrong decisions in valid JSON. [Planner](../../app/core/tasks/workflow/workflow_planner.py), [Ollama transport](../../app/llm/ollama_client.py).
- **Make simple answers cheaper.** For an exact saved value or effect, render the checked fact once with Aiko's existing wording conventions. For comparisons, let the model explain already validated rows. Reserve longer reasoning for conflicting sources, ambiguous intent and synthesis. A/B test this against today's reasoning-enabled calls, including conversation wait time on the shared model. [Local reporting](../../app/core/tasks/work_reports.py).
- **Use Hermes selectively.** Its on-demand skills and recoverable long tool results already have Aiko counterparts. A useful next transfer is retaining a precise recovery pointer when an excerpt is evicted, including in follow-up discussion. Pair each selected action with its verification operation in discovery so a small model can see how to finish. Prefer the existing plugin procedures for predictable call sequences. [Hermes skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills), [Hermes context management](https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching), [earlier local comparison](hermes-harness-review-2026-11-08.md).

## Acceptance

Use paired fictional requests with the same wording, model, context and decoding settings. Change only which evidence exists. Include a full answer and a partial one, similar venue names, different currencies or dates, a search miss, an acknowledged submission, a changed page and an ambiguous reference resolved mid-task. Score the final chat message and a factual follow-up as well as the saved objective outcome. Count false success, correct completion, unnecessary abstention, wrong-subject facts and repeated effects separately. Record inference calls, prompt/output tokens and elapsed time. A system that always declines will pass the false-success check and fail the positive controls.

Start implementation with the field-level evidence boundary, submission receipts and legitimate re-reads. Evaluate that baseline before changing reasoning budgets or adding a second domain adapter. Ordinary-use acceptance remains in the [workflow backlog](../plans/agent-workflows.md).

## Implementation and next experiment

The first pass addressed the three reproduced gaps. The shared review validator now checks the hit field and the supporting claims for each satisfied criterion. Submission requires the receipt of a concrete declared write. Verification reads can run again under the ordinary allowance and no-progress limits. Corrected decisions keep the review feedback even in the 4K fallback pack. Source-current search excerpts go straight to the report outbox with attribution.

An intermediate 9B run exposed a failure that field IDs alone missed. The model approved the hours and accessibility claim by citing “See our venue map for directions.” The host now checks that search-backed answer parts reproduce complete returned fields. This preserves qualifying sentences. A synthesized answer can use a separately inspected underlying source. This deliberately raises the cost of paraphrasing from snippets alone.

| Fictional case | Final decision | Reported input / generated tokens |
| --- | --- | ---: |
| Matching venue, hours and access | Supported both criteria | 824 / 3,955 |
| The proposed claim appeared only in the search query | Rejected before inference | 0 / 0 |
| Iris Hall source offered as proof of the same facts about Orchid Hall | Rejected before inference | 0 / 0 |
| Hours established, access unknown | Certified the hours-only partial answer | 878 / 6,307 |
| Accurate quotation about Iris Hall offered in answer to the Orchid Hall request | Rejected in review | 814 / 5,129 |
| Accurate directions excerpt offered instead of hours and accessibility | Rejected in review | 814 / 5,190 |

The [replay](../../scripts/workflow_review_replay.py) uses the production verifier, in-memory tools' results, seed 71, temperature zero and 65,536 context tokens. Run it with `python -m scripts.workflow_review_replay --model <installed-local-model>`. These are six controlled review decisions. Tests separately exercised search discovery and repair, repeat reads, write receipts and report delivery. Ordinary conversation, an exact 8B checkpoint and full-request latency remain unmeasured.

**Next experiment.** Start from “Compare the two workshops I mentioned” with fictional messages, source pages and an independent expected comparison. Include same-name workshops in different cities, conflicting dates, a missing price and a precise follow-up about an early source. Carry the resolved message and source IDs from chat to the planner, and let work discussion retrieve cited passages from owned results. Measure wrong-subject answers, correct partials, unnecessary abstention, number of planning calls and tokens to delivered answer. The held-out relevance checks still consumed roughly 5,100 generated tokens each. This is where to compare a smaller structured review or deterministic ID, amount and timezone checks before tuning a global reasoning cap.

## Referenced workshop comparison

The fictional catalogue has three workshops with the same name. The user mentioned
Alderwick (EUR 25) and Fernford (EUR 40). Birch Quay (EUR 90) is a distractor.
The handoff model selects exact passages, and the host binds them to their
messages. The local workflow model can discover or read the fixture pages and
the saved work supports a source follow-up. Reproduce with
`python -m scripts.workshop_workflow_replay --model baytout3/qwen3.5-uncensored:9b`.
The replay uses disposable storage, loopback Ollama, a 65,536-token window,
temperature zero and seed 71. The handoff is a structured call using the start
tool schema. Ordinary chat tool choice and browser navigation need separate trials.

One run returned the correct prices and difference while the reviewer cited the
Alderwick page for both answer parts. Both pages existed. A source follow-up
could trace only one. The host now checks that written currency amounts repeated
from evidence into a comparison have quotations, and the review prompt permits
multiple claims for one sentence. The next run returned both source URLs and
the correct EUR 15 difference. It took four planning calls and two verification
calls. The latter generated 7,881 and 5,464 tokens from roughly 1,660 input
tokens each. The drawer’s source lookup uses zero inference calls.

In the delivery replay, the worker read both pages and finished in three planner
calls. The verifier generated 6,297 tokens. Report composition and review
generated another 1,426 and 5,592. The delivered text matched the saved finding,
and both the implicit chat-tool lookup and the drawer returned both pages.
The chat-tool invocation was explicit in the replay. The ordinary-chat gate
was tested separately with the same question. This leaves real chat tool choice,
final streamed wording and live-site reliability open.

Final drafts that repeat the current certified finding verbatim now reuse its
review. A source change during composition invalidates the draft. Measure how
often the narrator actually returns an identical draft before estimating savings.
Seeing identical delivered text alone cannot distinguish a matching draft from
a rejected rewrite that fell back to the original finding.

The report outbox now saves a rewrite's checked quotations with the draft in
one transaction. A fictional two-source test recovered the source of a sentence
added in narration after reopening the database. Changing that source made the
lookup unavailable. The test uses scripted inference to exercise the storage
and retrieval contract.

Two isolated runs of the workshop replay used the same local 9B model at 64K.
The three-skill catalogue used 2,330 input tokens on the first planning call.
With `--large-catalogue` the fixture registered 512 extra stellar-coordinate
tools and the first planning call used 2,227 tokens. The larger catalogue
activates relevance filtering. Both runs delivered the correct comparison and
both sources. The larger run dispatched zero distractors. The small run
reworded the report and retrieved both page quotations behind the new wording.
The large run repeated the certified finding and skipped the narration review.
These trials exercise catalogue growth with lexically distinct distractors.

### Switching capabilities after reading a source

The [multi-step replay](../../scripts/workflow_switch_replay.py) starts a reader
workflow with the same request and six similar pickup-note tools in each case.
Only the latest personal note identifies the next register. Both fictional
registers accept the same identifier and return different locations, and each
register has a current, archive and preview lookup. A third note explicitly
says it has no reference. The oracle checks the actual calls and arguments, the
verified finding, the delivered report and its saved quotations. Run it with
`python -m scripts.workflow_switch_replay --model <installed-local-model> --case all`.

Before this change, reading the note left the planner looking at the same six
note tools. The model recovered through explicit discovery on both positive
cases. A first revision exposed the right register after the read, then lost it
when the model inspected the note. The host correctly rejected a request for
the newly hidden operation and exposed it for a retry. Keeping the latest read
available across inspections addresses that extra round trip. The negative case
tests whether an absent reference leads to a speculative lookup.

One baseline and one final run per fixture used the configured local 9B model,
65,536-token context, seed 71 and temperature zero. Counts below are baseline
→ final. Total calls and token counts include verification, report composition
and any report review through delivery.

| Note content | Planner calls | Total model calls | Input tokens | Generated tokens |
| --- | ---: | ---: | ---: | ---: |
| Waybill reference | 6 → 4 | 8 → 6 | 17,934 → 11,695 | 8,944 → 7,630 |
| Claimcheck reference | 5 → 4 | 7 → 6 | 14,151 → 11,682 | 9,326 → 6,578 |
| No reference | 3 → 3 | 6 → 5 | 9,399 → 8,613 | 11,624 → 8,318 |

All six runs delivered the expected answer with zero incorrect tool
dispatches. Both final positive runs inspected the note once, used the current
lookup directly and retained quotes from both sources. The first planner input
was 2,020 tokens in each run. The no-reference planner performed the same
actions before and after. Its final run skipped a separate report review, so
the difference in that row cannot be credited to routing. Its report correctly
quoted the missing reference but went on to say “in the entry,” which leaves
the distinction between the note and a linked record less clear than it could be.

### Indirect and ambiguous references

The `references` group in the [same replay](../../scripts/workflow_switch_replay.py)
keeps the pickup request and identifier fixed. In the indirect case the note
describes the paper a carrier signed for freight. Only the waybill description
covers that purpose. In two independent ambiguous cases the note lists both
registers, each containing the same number, and leaves the selection blank.
The replay answers a focused question on the same task, choosing the waybill in
one run and the claimcheck in the other. It checks calls before the question,
the resumed lookup, the delivered location and saved source quotations.
A fourth control mentions both registers but explicitly assigns the claimcheck
to the pickup. Its expected path is a single current claimcheck lookup after
the note, without a question. A fifth, held-out note calls the waybill “the
document accompanying the shipment.” None of those content words appears in
the waybill tool name, group or description. It has the same expected lookup
and location as the carrier-and-freight case.

At `009e416b` the indirect case succeeded after five planner calls. The model
inferred the right tool name from the carrier and freight wording, but the
host first had to expose the missing schema. Both ambiguous runs reached their
nine-turn limit after the note read and eight inspections. Neither asked the
user or delivered a location. In an intermediate version the model saw both
groups and reported both locations with the selection still unresolved. A
successful evidence review of those locations did not establish user intent.

The final selection step can extract a named target with a quotation, ask the
user, or search for more evidence. Only the subsequent ordinary planning step
can call a registered operation. On the explicit-selection control the model
quoted “The signed instruction assigns the claimcheck to this pickup.” The
next menu contained the three claimcheck editions. The runner checked the
current lookup, the absence of a question and the saved quotes for both children.
With the indirect carrier-and-freight wording, two distinctive description
terms put the waybill editions directly into the second menu. Planner calls
fell from five to three and total input tokens from 15,331 to 9,714. Total
generated tokens rose from 11,015 to 12,605 across planning, verification and
reporting in this pair.

The held-out shipment wording left the initial follow-up menu unchanged. The
model inferred the waybill name, the host exposed its schema, and the model
supplied the correct identifier on its next decision. It delivered Copper
Alcove with quotes from the note and waybill. This recovery took five planner
calls, eight model calls overall and 15,197 input tokens.

Both ambiguous runs paused after reading only the note. The first answer
selected the waybill and the second selected the claimcheck. The same task
then read only the chosen current record. Both delivered reports attributed
the selection to the user and retained the note and register quotations. The
waybill branch used five planner calls and 13,920 total input tokens. The
claimcheck branch used eight planner calls and 22,071 total input tokens,
including repeated inspections of the same lookup. One question used the
third person (“does the user mean”) even though the choices were clear.

Each of those two target-choice prompts used 586 input tokens. They generated
4,384 and 2,605 tokens respectively. A next experiment can vary the reasoning
allowance for this decision while measuring unnecessary questions, incorrect
selections and time to a verified answer. The five fictional cases exercise
workflow start through report delivery on the configured local 9B model at
65,536 context, temperature zero and seed 71. Ordinary-chat workflow selection,
an exact 8B checkpoint and follow-up after a document or schedule change remain open.

## How much reasoning does a target choice need?

The paired replay holds the model (`baytout3/qwen3.5-uncensored:9b`), context
(65,536), seed (71), temperature (zero), date and answer allowance (512) fixed.
Five pickup prompts cover an assignment, a blank selection, a correction with
reversed candidate order, conflicting records and an explicit request for both.
The three held-out survey prompts change the names, wording and order.
The oracle checks the selected source and decisive quotation, both options in
a clarification, or both catalogue terms in a comparison.

| First choice | Original prompt, pickup | Revised prompt, pickup | Revised prompt, survey |
| --- | --- | --- | --- |
| Thinking disabled | 4/5 | 5/5 | 3/3 |
| Thinking enabled, 0 extra tokens | 1/5 | 5/5 | 3/3 |
| Thinking enabled, 1,024 extra | 3/5 | 5/5 | Unmeasured |
| Thinking enabled, 4,096 extra | Unmeasured | 5/5 | Unmeasured |
| Thinking enabled, 8,192 extra | 4/5 | 5/5 | Unmeasured |

On the original prompt, three choices used the entire 512-token allowance
without returning a decision. Even the largest allowance missed the requested
comparison. The revised prompt asks about the scope of the user's request first.
Across the eight cases it used 267 generated tokens with thinking disabled,
versus 2,653 with thinking enabled and zero extra tokens. The individual direct
answers took 24–39 tokens. With thinking disabled, 39 answer tokens was the
lowest tested cap at which all eight returned correct JSON with a normal stop.
At 38 the disagreement question still parsed but hit the length limit. These
totals cover the target choice only. The runtime keeps the normal answer
allowance so longer candidate names and supporting quotations have room.

With the full workflow context the direct pass once selected the waybill and
quoted two sentences that explicitly assigned the claimcheck. The old substring
check accepted the citation, and verification accepted the resulting wrong
location. The new fast pass requires a single complete statement naming just
the selected candidate. When it saw the same response (56 tokens), it withheld
the lookup. The bounded reasoned retry (171 tokens) cited the assignment,
selected the claimcheck and delivered Pearl Rotunda. A repeated unsupported
target and citation on retry yields a partial result. This check establishes
where the words came from. Whether they designate the right source still
depends on the model.

Both ambiguous workflow branches asked with 35 generated tokens, after reading
only the note. Each used the user's reply to look up the chosen register. In a
repeat of the waybill branch, the planner inspected already-read results five
times and paused after verification, without delivering a report. An isolated
rerun delivered Copper Alcove in four planner calls. Both clarification branches
delivered the right locations but omitted that the user supplied the choice.
One explicit-selection report phrased the pickup note as the item to collect.
The location and source assertions in this replay do not check those details.
Report attribution and repeat inspection remain separate follow-ups.

Reproduce the paired runs with
`python -m scripts.workflow_choice_budget_replay --model baytout3/qwen3.5-uncensored:9b --headroom off 0 1024 4096 8192`.
Add `--holdout` for the survey cases or `--answer-tokens` to vary the shared
answer allowance. The full workflow replay uses `scripts.workflow_switch_replay`
with the same model and `--case resolved`, `--case ambiguous-waybill` or
`--case ambiguous-claimcheck`.
