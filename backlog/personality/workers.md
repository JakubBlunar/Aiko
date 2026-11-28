# Background workers

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

## G5. Self-tuning cue cooldowns

**Status: Open implementation.**

**Implemented / available:** Reach/decline reports, demand scheduling, inventory and cooldowns exist.

**Remaining:** Validate an attributable usefulness signal and armed denominators before bounded
opt-in cooldown adaptation. Do not tune away topic mismatch or use copied turn engagement as item
reward.

**Source check:** [cue_accounting.py](../../app/core/proactive/cue_accounting.py),
[cue_decision_store.py](../../app/core/memory/cue_decision_store.py).

## G6. Per-provider decline attribution

**Status: Partial.**

**Implemented / available:** Structural and several provider-specific declines are named; bounded
assembly receipts distinguish provider/error states and final claims.

**Remaining:** Instrument remaining generic provider bailouts by failing family; preserve
supply-versus-gate and error-versus-silence distinctions. Accurate final inclusion precedes learned
selection.

[Implemented slice](shipped/audit-2026-10-24.md#workers-g6).

**Source check:** [surfacing_trace_store.py](../../app/core/memory/surfacing_trace_store.py),
[hypothesis_surface_report.py](../../scripts/hypothesis_surface_report.py).

<a id="g7-worker-prompts-have-no-input-token-accounting"></a>

## G7. Worker prompt-budget rollout

**Status: Partial.**

**Implemented / available:** Summary, memory extraction, belief inference, hypothesis proposals
and generated archive summaries use the shared whole-request packer. The thread-note worker
also uses it against the configured maintenance route. It reserves its 320-token response,
gives newest whole messages priority, and drops old notes or summaries if that allows more
recent messages to fit. It skips a request whose newest message cannot fit and repacks on
the next attempt if the route changes while queued.

**Remaining:** Roll out to other worker builders with their actual route/output limits and
worker-owned reduction rules. The helper and summary cursor repair are already implemented.

[Implemented slice](shipped/audit-2026-10-24.md#workers-g7).

**Source check:** [worker_prompt_budget.py](../../app/core/proactive/worker_prompt_budget.py),
[summary_worker.py](../../app/core/proactive/summary_worker.py),
[belief_worker.py](../../app/core/relationship/belief_worker.py),
[thread_resummary_worker.py](../../app/core/proactive/thread_resummary_worker.py),
[test_summary_worker.py](../../tests/test_summary_worker.py).

## G8. Refresh activity summaries when an existing session grows

**Implemented, ordinary-use check open.** The activity workers now notice
duration changes within the same session. A disposable SQLite regression fed
five-second samples for six minutes. The summary advanced from zero to 360
seconds in one-minute steps. Interpretation called the model twice, at the
start and at five minutes. A subsequent activity switch made both workers
eligible immediately. Check the timing during ordinary desktop collection
under scheduler load. [Activity guide](../../docs/presence-and-activity.md)

## G9. Let a worker remember a well-founded abstention

**Implemented, ordinary-use check open.** The proposer can remember an explicit
empty answer for six hours and reconsider when the offered material changes.
It asks what detail gave Aiko a hunch, what else could explain that detail, and
what answer would change her reading. A copied observation or an alternative
identical to the guess fails admission.
[Hypothesis admission](../../docs/hypothesis-cue-quality.md#hypothesis-admission)

The [local replay](../../scripts/hypothesis_proposal_replay.py) used fictional
records and the installed 9B model. The original worker turned a parcel pickup
into a theory about emotional connection. The revised worker left that errand
alone. It kept a guess about sign visibility when the choice was unexplained,
then abstained when the designer gave her reason. Both abstentions skipped the
next identical model request. Aiko replaying the pause before a chorus produced
a hunch about how the pause affected anticipation, including when the settled
sign story appeared alongside it. The proposed question about the sign still
assumed the designer had held the letters. Check that questions in ordinary
conversation preserve the user's actual observations.

In a follow-up, the worker drafted the source quote and check question before
writing out its explanation. Across six fictional inputs at each of seeds 71
and 79 on the same 9B model, the sign and sound-design questions asked what
drew Mira to her choice. Neither assumed a test or a motive. Both runs left the
explained choice and routine errand alone and kept Aiko's listening question
in the mixed case. The questions were sometimes in third person. These were
stored proposals; their eventual spoken phrasing still needs an ordinary-use check.

## Memory/time quality: ordinary-use follow-up

The [17 November memory-capture follow-up](../reviews/memory-capture-review-2026-11-17.md)
finds partial retention of a teasing signal but a missing user-side communication
need. Brain writing now has stable capability guidance and optional owner-quoted
notes. Both writers can retain qualified literal testimony when a paraphrase loses
a condition, and rejected extraction proposals no longer spend the write cap.
Fresh ordinary-use recall/omission checks remain open; source presence is not
semantic correctness and capture does not force concept promotion.

**Status: Fresh cohorts reviewed; meaning fidelity still open (17 Nov 2026).**
The [17 November continuity review](../reviews/companion-continuity-review-2026-11-17.md)
traces repeated diary prose and inherited unsupported claims into active concepts,
fixes age-dependent source identity, and adds bounded diary grounding and
project-purpose research checks. It also reviews 72 hours of independent activity
and makes new plan rejections diagnosable. Ordinary-use validation of these fixes,
historical repair, omissions and broader semantic calibration remain open.
The 15 November follow-up found
an uncited-admission bypass, fresh promise/clock/return interpretation failures,
and downstream concept overreach. Bounded fixes preserve positive cases;
diary novelty, research relevance, omissions and broader semantic fidelity remain
open. Historical rows were not repaired; the new code needs a backend restart.
The [ordinary-use review](../reviews/ordinary-use-review-2026-11-12.md) covers promises,
extraction, summaries, reflective output, research and downstream concepts.
Advice/offer/commitment, actor, condition and outcome interpretation are separate
from source presence. Bounded corrections passed regressions; synthetic worker
prose still contains semantic errors, so this is not a general quality pass.
The concrete review
checklist lives under [L22's post-restart memory cohort](concepts.md#l22-follow-up-post-restart-memory-quality-cohort-6-nov-2026).
Check fresh worker output against its actual sources after normal use, including
missed important memories, and keep historical issues separate. Do not count a
healthy startup or a sparse worker cohort as semantic quality validation.

**21 November diary review.** A read-only SQLite pass found 10 entries dated 17–21 November
(UTC). Eight attached a background generation packet. Their stored primary session
matched the packet, and all 90 message references still matched the saved revisions.
Each packet had a distinct context key. Entries 11840 and 11861 still repeat the same
reflection with close paraphrasing across different exchanges. The later entry brings
back an earlier simile absent from its fresh transcript, while the earlier diary entry
appears in its memory inputs. At that review the duplicate gate caught normalized
exact matches. This review covers provenance and one repetition example.

**22 November diary follow-up.** The background worker now compares a grounded
draft with the same eligible earlier entries used during composition. An accepted
new reflection cites a current message ID. A failed comparison can retry, and
editing a source during review prevents a stale write. [Worker contract](../../docs/worker-quality-improvements.md#background-diary-and-thread-inputs)

In disposable SQLite runs on local `baytout3/qwen3.5-uncensored:9b` (seeds 71
and 73), the composer supplied fixed fictional drafts and the model performed
the reviews. Before the change, both runs stored a paraphrase of a diary about
sorting tiles. The updated novelty check rejected it on both seeds with the
same single-message context and retained a new reflection on a cracked tile's
lower note. An invented agreement was rejected by the independent grounding
check. With an unrelated message added, grounding sometimes rejected the
paraphrase first. The changed-outcome draft still reached storage on both seeds.

Held-out paper-boat cases exposed a wording limit. Source review withheld the
repeat and a changed-outcome draft that described *seeing* the boat float,
although the transcript contained a user's report. A separately tested draft
that attributed the result to the user passed both reviews on both seeds.
Ordinary-use acceptance still needs newly written entries, missed good drafts
and inspection of any later concept that uses them. Historical diaries and
concepts retain their original records.

<a id="next-producer-quality-candidates-source-review-6-nov-2026"></a>

### Reflective producer quality (implemented 6 Nov 2026)

**Status: Implemented; ordinary-use validation waiting.**
The [12 Nov review](../reviews/ordinary-use-review-2026-11-12.md) supplies a bounded
fresh cohort; reported-step/outcome positives remain sparse, and semantic
interpretation checks remain open after the follow-up fixes.
[Implementation and limits](../../docs/reflective-producer-quality.md). Activation requires
another backend restart; do not score older rows as output from these changes.

- Goal reflections now receive bounded relevant source records and deliberate
  notes, distinguish reflection/intention/reported step/outcome, skip unchanged
  context, and retain input revisions. Novelty checks preserve changed conditions
  and negation. Existing generated notes do not become new observations.
- Shared moments now retain narrow clause citations and an explicit exchange,
  recalled-event or virtual scope. Unknown occurrence dates remain unknown;
  recording time alone cannot create an anniversary. Source edits defer writes,
  and deliberate revisions preserve old receipts as history.
- Dreams now receive whole qualified memory rows with input identities/revisions.
  Legacy text stays explicitly identity-unknown. Input edits defer output; derived
  context supplies no additional observation roots or confidence votes.

**Remaining:** Review usefulness and abstention in fresh normal use, including
missed meaningful goal changes, repetitive imagery, genuine outcome retention,
shared-moment source/time accuracy and preserved dream exceptions. The concrete
checklist remains under [L22](concepts.md#l22-follow-up-post-restart-memory-quality-cohort-6-nov-2026).
No worker was forced or live historical memory rewritten during implementation.

Source checks: [goal_reflection_evidence.py](../../app/core/goals/goal_reflection_evidence.py),
[goal_worker.py](../../app/core/goals/goal_worker.py),
[moment_evidence.py](../../app/core/relationship/moment_evidence.py),
[shared_moment_extractor.py](../../app/core/relationship/shared_moment_extractor.py),
[reflective_evidence.py](../../app/core/memory/reflective_evidence.py),
[dream_worker.py](../../app/core/proactive/dream_worker.py).

## Remaining memory boundaries (reviewed 6 Nov 2026)

**Status: Implemented; ordinary-use review pending.** The read-only
[boundary audit](../reviews/memory-boundary-audit-2026-11-06.md) found six areas now covered
by the [implementation record](../../docs/memory-boundary-improvements.md):

1. Preserve SQLite subject, source mode and virtual/external domain through
   direct and associative recall; kind-only headings currently lose them.
2. Keep hypothesis answer memories bounded to the supported reply and retain
   support receipts through world graduation, with honest answer/event timing.
3. Separate image sharing time from depicted time and retain caption-message
   provenance, subject and uncertainty.
4. Preserve manual gap-answer context and distinguish successful saving from
   deliberate dismissal or a failed write.
5. Retain virtual pursuit episode/research receipts and qualified time scope.
6. Retain catchphrase occurrences and first-observed authorship scope, plus both
   sides and verified timing for a newly shared joke.

The audit includes acceptance scenarios and historical examples. Those examples
are not a fresh post-restart quality measurement; L22's existing review remains
pending. A backend restart activates the changes. Historical memories were not
bulk rewritten; known source edits are checked when those receipts are consumed.

## Further worker quality review (6 Nov 2026)

**Status: Implemented; ordinary-use review pending after restart.** The
[worker audit](../reviews/worker-quality-audit-2026-11-06.md) identifies five additional
areas beyond the implemented memory intake/recall boundaries:

1. Growth witness: preserve ownership of affect, relationship signals and Aiko
   goals; do not assert user growth from Aiko's internal state.
2. Background diary: freeze/recheck one source packet and preserve whole message
   qualifications, session identity and subjective derivation; include thread-note
   message trimming in the input-packing work.
3. Research: separate offered excerpts from cited support, preserve whole facts,
   and recheck a question before attaching an answer/resolving it.
4. Evolution diary: retain relationship subjects and historical dates in the
   composition context, alongside the existing event links and period bounds.
5. Knowledge-map reflection: retain qualified concept/source records, distinguish
   recall activity from discussion/knowledge, and skip unchanged context.

Source checks, read-only snapshot evidence, synthetic reproductions and acceptance
scenarios are in the audit. The [implementation record](../../docs/worker-quality-improvements.md)
describes the source packets, attribution, historical diary and qualified map
context. Historical memories were not rewritten. L22's ordinary-use checkpoint
remains open; local structural regressions do not measure live-model prose quality.

After restart and fresh ordinary use, review new outputs for:

- Dated growth cues that stay about Aiko's own feelings and are consumed once.
- Diary entries that retain final qualifications and their original session,
  without retelling unchanged conversations.
- Research findings with selected excerpt support; inspect unattributed refusal
  frequency and verify edited questions stay unresolved by older answers.
- Evolution-diary entries about old changes that retain historical dates and
  relationship subjects.
- Knowledge-map reflections that preserve subject and conditional scope, and do
  not recur merely because old memories were recalled or a cooldown expired.

## G-CLEANUP. `consolidator_state.last_cluster_index` is not dead weight — do not drop it

**Status: Open implementation.**

**Implemented / available:** RelationshipPulse uses last_cluster_index as its last-pulse turn count.

**Remaining:** Move pulse state to a dedicated key or migrate both users to a correctly named field;
correct the misleading consolidator comment. Do not delete a column with a real reader.

**Source check:** [memory_consolidator.py](../../app/core/memory/memory_consolidator.py),
[relationship_pulse.py](../../app/core/relationship/relationship_pulse.py).

## Moved out of the active queue

- **G4** — shipped: [record](shipped/audit-2026-10-24.md#workers-g4).
