# Memory producers and grounding audit — 5 November 2026

The purpose of the concept layer is Aiko's grounded understanding of her
memories: what she knows about the user, herself, their relationship and the
world. The memory system must preserve what each source actually establishes.
Creative inner life, imagined activities and tentative interpretations are
legitimate contents; their presence does not establish that an external event
occurred or that the user holds a particular preference.

**Main finding:** the most consequential problem is loss of source meaning as
records cross producer, storage and concept boundaries. Prompt quality matters,
but actor, type of evidence, time and derivation history need to survive those
boundaries. Current data contains concrete counterexamples, not merely risks.

## Scope and method

- Reviewed the runtime write surface: **32 direct MemoryStore.add call sites in
  26 modules**, plus indirect producers using shared stores/writers. Enumeration
  combined text search, AST call inspection, constructor ownership and manual
  dismissal of unrelated `.add` calls on cue, hypothesis, moment and RAG stores.
- Reviewed content-changing and belief-changing paths: both consolidators,
  correction, fact-checking, conflict resolution, concept-driven memory revision,
  temporal maintenance, tier promotion and manual editing.
- Inspected all 5,436 rows for structural metadata and temporal/lineage coverage,
  selected 113 bounded examples across all 21 stored kinds, and followed the
  strongest counterexamples through their complete direct concept evidence.
  This is not a semantic grade of all 5,436 memories or a prevalence estimate.
- Read the running worker/clock snapshots after restart. The debug clock was
  disabled. Most producer run counters were zero in that new process; this says
  nothing about historical producer throughput or quality.
- All application/database inspection was read-only. No worker was forced, model
  evaluation called, memory rewritten, source deleted or historical repair made.

Private artifacts are under ignored `tmp/memory-producer-audit-2026-11-05/`:
`callsite-inventory.json`, `writer-calls.txt`, `corpus-summary.json`,
`memory-samples.json`, `direct-concept-feeds.json`, `case-studies.json`,
`lineage-coverage.json`, and `workers.json`. Personal source text belongs there,
not in tracked fixtures. Stock counts below refer to the captured snapshot.

## What the stored data establishes

| Observation | Count | Interpretation and limit |
| --- | ---: | --- |
| Memories | 5,436 | Months of history, not just the recent ordinary-use trial. |
| Long-term / archive / scratchpad | 3,869 / 973 / 594 | Storage/retention state; these are not truth grades. |
| Inferred / stated provenance | 4,424 / 1,012 | Inferred is the safe default; the label alone does not establish source support. |
| Direct source-message pointer | 805 | Some other records retain URLs, attachments or message IDs in metadata. Absence is not proof of fabrication. |
| At least one complete root under the existing lineage diagnostic | 1,510 | Root coverage, not entailment or independent support. Historical unknown roots must stay unknown. |
| Promise memories | 1,039 | 544 explicitly assistant-side, 447 user-side, 48 legacy/unknown. None has a direct source-message pointer in this snapshot. |
| Goal-progress notes | 120 | 119 marked `worker`, one `tool`; none has a direct message pointer. These are predominantly Aiko's reflections. |
| Shared moments | 445 | 289 detector-generated, 155 inline-tag, one manual. Only one retains `source_message_ids`. |
| Research records with the newer excerpt/attribution fields | 77/684 knowledge; 4/35 curiosity findings | Older records are not necessarily unsupported: knowledge records retain URLs. Their exact supporting excerpt/attribution cannot be recovered from the new fields. |
| Rows carrying consolidation metadata | 215 | Includes surviving and absorbed records; not 215 independently verified rewrites. |
| Consolidation-marked rows still labelled stated | 83 | A review cohort for provenance preservation; some may have unchanged fallback text. |

Relative wording also survives in recently written records: 12 goal-progress,
13 topic-digest, four diary, three event and three self memories since 28 October.
These are wording flags, not an error rate. Correct event fields can coexist
with bad stored wording. A separate check found no `past_event` whose event date
was still in the future and no timestamped `future_plan` already overdue at the
snapshot. Event dates later than original creation are often legitimate plans
subsequently reclassified as past events; they must not be counted as failures.

## Findings, ordered by consequence

### 1. Hypothetical and non-user material becomes evidence of user experience

The common [proposer memory renderer](../../app/core/concepts/proposers/base.py)
normally supplies ID, shortened content and recording age. The recent aspiration
fix adds plan/kind status there, but it is not a general authority/actor contract.
The [synthesis worker](../../app/core/concepts/concept_synthesis_worker.py) classifies
cluster subjects by the majority share of `self`, `reflection` and `diary`.
Other kinds default toward the user side. Aiko goals/progress, pursuit notes,
drafts and public research therefore do not reliably retain their proper role.
Cosine fit is topical relatedness, not an assertion that the source can establish
the proposed personal claim.

Three inspected active concepts demonstrate the consequence:

- Concept 13090 describes a completed cooking consultation. Its direct evidence
  is public cooking material plus memory 10185, a hypothetical pre-written answer
  beginning with an explicit conditional question. It contains no direct record
  of that consultation occurring.
- Concept 13129 describes an experience of the user. All four direct sources
  are Aiko goal-progress reflections: 10478, 10728, 10755 and 10798.
- Concept 13123 describes the user configuring browser cookies. All three direct
  sources—6132, 6588 and 6589—are impersonal public facts about cookies, not
  observations of the user taking those steps.

Direct active user-concept support includes goal-progress notes for 25 concepts,
pre-thoughts for seven, open questions for 31, pursuit notes for 56 and knowledge
for 131. These cohorts overlap and are **not** automatic counts of wrong concepts.
A personal claim can have other valid support. The inspected examples establish
the failure mode without making that broader assertion.

The existing [L49 lineage model](../../app/core/concepts/concept_evidence_lineage.py)
already recognizes model reflections and external observations, but explicitly
runs in shadow mode. It does not enforce claim-subject eligibility or change
promotion. Build on that distinction rather than creating a second truth store.

**First fix:** carry source meaning into candidate selection, prompts and
admission. A draft/question can guide curiosity; a world fact can answer a world
question; neither alone establishes a personal occurrence. Aiko's own declarations
can ground her self-model without being relabelled as user testimony.

### 2. Memory kind is overloaded as subject, authority and storage purpose

The kind `self_tagged` normally means a remembered statement about the user, but
[RelationshipPulseWorker](../../app/core/relationship/relationship_pulse.py) also
writes Aiko's own relationship stance under that kind. Two first-person examples
exist among 202 stored self-tagged rows. At least one is direct evidence for active
user-boundary/style concepts. Most self-tagged rows are about the user, so a bulk
kind rename would be wrong.

174 self-tagged rows point to assistant messages. That is **not** itself a subject
error: many are Aiko's paraphrases of user statements. It does mean that author,
subject and evidence authority cannot safely be inferred from that pointer alone.

Promises already retain `promise_who`, and catchphrases retain first-speaker
`origin`; these are useful local contracts. They are not consistently exposed
to personal-concept synthesis. Goals and progress notes are Aiko-owned by their
worker contract, but no universal subject field enforces that interpretation.

**Fix:** distinguish author/source, claim subject and representation purpose.
Use the existing SQLite/metadata ownership rather than parallel stores. Preserve
user, Aiko, joint relationship and impersonal-world meanings separately.

### 3. Generating a reflection can look like observing progress

[GoalWorker](../../app/core/goals/goal_worker.py) reflects using a goal and its last
few generated reflections. Its reflection call has no dated transcript, current
date anchor or stored-text time rule. History is emitted as plain note bullets.
[GoalStore](../../app/core/goals/goal_store.py) writes these as long-term
`goal_progress` with confidence 0.8. This supports a history of Aiko's reflections,
not empirical progress in the user or completion of actions.

Recent notes contain projected actions and transient self-descriptions. Recording
them is appropriate for an inner life; treating repeated reflection as new
observations creates a self-confirmation loop. The goal-generation/reflection
contract needs to separate intention, reflection and recorded action/result.

**Fix:** retain the private voice and autonomy, age-tag the actual inputs, and
require a result/source receipt for claims of completed progress. An unchanged
goal should be allowed to produce no new progress entry. A reflection can still
be retained explicitly as a reflection.

### 4. Time safety is uneven, especially between source time and write time

The relevant clocks are different: when a source message was said, when its event
occurred/is planned, and when a worker wrote or rewrote the memory. A current date
anchor alone cannot recover those distinctions after source timestamps are lost.

Examples of remaining gaps:

- Goal reflection history is undated; the worker has no time anchor/storage rule.
- Dream input callbacks/self-notes are reduced to strings before composition.
  Its output has the stored-text rule, but the source recording ages are lost.
- Away diary has a current anchor and a concrete-date output instruction, but
  `build_recent_context` strips message timestamps. It writes `durable` by default.
- Pre-thought question generation has a current anchor; drafting uses the persona
  message builder. The stored question/answer is still hypothetical and may keep
  relative wording. The generation-stage anchor does not date the hypothetical
  answer as an actual event.
- Shared-moment detector history is speaker-labelled but undated. Its `when`
  defaults to detection time, which can differ from the moment being summarized.
- Corrections, web fact-check rewrites and concept-to-memory arbitration use raw
  claim strings without sufficient source-age/current-time context.
- Current extraction has proper dated input, but a paraphrase can remove a time
  qualifier while leaving a temporary hope or state labelled `durable`. One
  recent source about hoping the following workday passes quickly became an
  undated durable fact. Lexical detection cannot repair omitted meaning.

The [store's add backstop](../../app/core/memory/memory_store.py) catches relative
wording and repairs some type/expiry inconsistencies. It does not rewrite the
words or verify semantic event time. The content-changing `update` path does not
run the same deictic/direction checks. Merge and rewrite paths therefore need
equivalent invariants, with an explicit way to preserve unknown event time.

**Promises need special treatment:** their deadline intentionally lives in
metadata, governed by commitment status, so an overdue promise remains visible.
Moving every deadline to ordinary event expiry would break that behavior.
Expose actor, statement time, deadline and open/fulfilled/dropped status to the
relevant consumers instead. The promise LLM parser currently returns no source
message ID even though persistence accepts one; all 1,039 stored pointers are
absent. Its existing age-tagged transcript and side validation are worth keeping.

### 5. Merging and rewriting can retain authority that no longer describes the text

Both consolidation paths preserve some grounding, but they have different risks:

- [MemoryConsolidator](../../app/core/memory/memory_consolidator.py) age-tags inputs
  and excludes `self`/`reflection`, but its prompt frames all notes as facts about
  the user and converts first-person content to third person. Other Aiko-owned
  kinds are not protected by those two exclusions. It rewrites the survivor,
  repoints concept edges and deletes victims. It explicitly increments use count.
- [MemoryConsolidationWorker](../../app/core/memory/memory_consolidation_worker.py)
  requires the same kind and a non-conflict heuristic. It retains absorbed rows
  in archive and source IDs, which is useful. It nevertheless promotes the
  primary to long-term, takes maximum input confidence and keeps the primary's
  old source/temporal/provenance fields for newly generated text. Its merge call
  lacks the full current-date/stored-text contract.
- `MemoryStore.update` exposes no provenance parameter. A model paraphrase or
  fact-check rewrite can therefore inherit a `stated` label and an original
  message pointer that do not establish its new wording. The 83 consolidation-
  marked stated rows identify a review cohort, not a proven count of wrong edits.
- In `MemoryStore.add`, the highest-cosine duplicate route is not universally
  partitioned by subject or representation purpose. A similar statement can be
  absorbed before source/time differences have been enforced. Existing duplicate
  observation receipts help preserve sources, but do not establish semantic
  interchangeability of the claims.

**Fix:** merge only compatible subject, modality and temporal scopes; preserve
original source identities and distinguish a synthesis from literal testimony.
Do not let performing a merge count as independent confirmation of its content.
Keep the conservative option to retain separate records when scope is uncertain.

### 6. Retrieval, promotion and conflict resolution do not certify truth

[MemoryPromotionWorker](../../app/core/memory/memory_promotion_worker.py) promotes on
age/use or revival score, not new testimony. Consolidation can also directly
promote. Consequently long-term should mean retained/useful, not independently
verified. Otherwise a generated note can be retrieved, reused and promoted while
remaining the same unsupported interpretation.

[MemoryConflictWorker](../../app/core/memory/memory_conflict_worker.py) can auto-resolve
based on confidence difference after conflict detection; confidence is partly a
producer default. Its raw-text arbitration lacks a full actor/time/evidence-role
comparison. Only two currently stored rows carry its auto-supersession marker;
there is no observed pinned auto-supersession in this snapshot. The concern is
the policy, not evidence of widespread historical damage.

[ConceptBeliefReviser](../../app/core/concepts/concept_belief_reviser.py) correctly
uses bounded arbitration, preserves pinned memories and can decide that the
memory is fine while the concept was wrong. Keep those safeguards. Its arbiter
still needs dated source authority so a generated counter-interpretation cannot
override an observation merely by sounding convincing. Six stored memories have
its superseded marker; those were not independently adjudicated in this audit.

## Producer-by-producer inventory

Defaults below are the current call-site request. The store can reclassify time
or coerce pin/tier settings. A missing direct pointer is not a missing URL or
attachment. Risks are source-contract findings, not measured producer error rates.

| Writer / owner | What it writes and what it establishes | Quality, time and subject assessment |
| --- | --- | --- |
| [MemoryExtractor](../../app/core/memory/memory_extractor.py) | User facts/events/preferences/relationship and Aiko self-notes; structured strategies/terms | Dated transcript, temporal fields, source admission and pending-source recheck are useful. Uncited inference remains admissible to scratchpad; whole-message literal equality is a very narrow stated category. Semantic time/actor omissions remain possible. |
| [TurnRunner inline remember](../../app/core/session/turn_runner.py) | `self_tagged` about user or `self` about Aiko | Uses admission and generated-turn guards; unresolved paraphrases are provisional. Deliberate Aiko self-notes get high initial confidence; durable default and kind-based subject need explicit scope. |
| [TurnRunner inline diary](../../app/core/session/turn_runner.py) | Aiko diary during conversation | Points to assistant turn and uses durable/long-term. Preserve as subjective journal of the exchange, not external observation. |
| [GoalStore](../../app/core/goals/goal_store.py) via [GoalWorker](../../app/core/goals/goal_worker.py) | Aiko goals and generated progress reflections | Two writes; high-confidence long-term defaults, undated recursive reflection inputs. Main actor/progress/time risk described above. |
| [CatchphraseMiner](../../app/core/memory/catchphrase_miner.py) | Recurring phrases and fast assistant-origin phrase births | Two writes; recurrence/both-speaker checks and first-speaker origin are useful. Exact utterance IDs are mostly not retained. A phrase's recurrence is not a broader personal belief. |
| [IdleFactChecker](../../app/core/memory/idle_fact_checker.py) | Confidence/content edits; resolved gap answer as `fact` | Web-verdict prompt and confidence bounds exist. Rewrites/answer rows need retained excerpts and dates; preserve testimony versus public verification and distinguish personal from world subject. |
| [KnowledgeGapStore/extractor](../../app/core/memory/knowledge_gap_extractor.py) | `knowledge_gap`, zero initial confidence, optional turn pointer | Correctly represents an unanswered question. Needs explicit non-assertion eligibility throughout concept consumption; resolving it must retain question-to-answer evidence. |
| [PromiseWorker](../../app/core/memory/promise_worker.py) | Actor-labelled commitments, status and optional deadline metadata | Dated transcript, unknown-side rejection, deadline/status semantics are good. Source ID is not populated by LLM parsing; paraphrased promise is not proof of performance. |
| [PursuitNoteWriter](../../app/core/memory/pursuit_notes.py) | Aiko virtual activities, hobby milestones/wrap-up | Source/topic/action metadata helps. Kind is not in generic Aiko-self routing; durable default and absent universal actor/domain can turn virtual state into user experience. |
| [UserCorrectionWorker](../../app/core/memory/user_correction_worker.py) | Replacement personal note, stated, high confidence | Explicit-correction intent and old-note linkage are good. Output is a model paraphrase without retained source message/quote or full time context; preserve the actual correction's time/scope. |
| [CuriosityWorker](../../app/core/proactive/curiosity_worker.py) | `open_question`, ask-user versus subject purpose | Purpose separation is useful. This is an intention to ask, not the user's answer; current-turn wording and topic cues are not durable personal evidence. |
| [DiaryWorker](../../app/core/proactive/diary_worker.py) | Aiko away diary | Current anchor/concrete-date instruction; undated recent dialogue and durable/long-term request. Subjective interpretation should retain its source window and record date. |
| [DreamWorker](../../app/core/proactive/dream_worker.py) | `[dream]` reflection, scratchpad | Prefix, sleep episode ID and storage-time rule are useful. Stringified source memories lose age; generated imagery must remain distinguishable from observed events. |
| [IdleCuriosityWorker](../../app/core/proactive/idle_curiosity_worker.py) | `curiosity_finding` from web excerpts | Current anchor, storage rule, cited excerpt metadata, personal-question exclusion. Source indices attribute a conclusion, not prove entailment; old rows lack retained excerpt fields. |
| [IdleKnowledgeWorker](../../app/core/proactive/idle_knowledge_worker.py) | Evergreen impersonal `knowledge`; also conversation-owned search write-back | Explicitly forbids personal claims; newer excerpt/citation and time rules are good. Research output must stay world knowledge; public facts are currently usable in false personal event arcs. |
| [KnowledgeMapReflectionWorker](../../app/core/proactive/knowledge_map_reflection_worker.py) | `[mindmap]` reflection, scratchpad | Current anchor, recency hints and storage rule. Interprets graph shape; not an independent observation confirming the graph's contents. |
| [PostSleepDiaryWorker](../../app/core/proactive/post_sleep_diary_worker.py) | Aiko diary from recorded sleep episode | Stronger reference path: concrete episode facts, age/date context, rejection of relative wording, `past_event` and episode end time. Keep subjective rendering distinct from additional physical facts. |
| [PreThoughtWorker](../../app/core/proactive/pre_thought_worker.py) | Conditional question and imagined answer, scratchpad, confidence 0.5 | Explicit conditional wrapper and dedicated kind exist. Current consumers nevertheless use it as an event source. Question-generation anchor does not validate the drafted answer or event occurrence. |
| [PursuitResearch](../../app/core/proactive/pursuit_research.py) | Impersonal research question; result-linked diary | Two writes; source existence, dated research result, research-memory ID and relative-word fallback are useful. Diary plus result are one derivation, not independent support. |
| [ReflectionWorker](../../app/core/proactive/reflection_worker.py) | Reflection, open questions, callbacks after a turn | Current anchor/storage rule and completed-turn receipt exist. Short first-person output and affect are interpretations; actual user/assistant text is supplied but not a per-item supporting quote. |
| [RelationshipPulseWorker](../../app/core/relationship/relationship_pulse.py) | Aiko stance under `self_tagged` | Uses age-tagged memories/current anchor/storage rule. Destination kind conflicts with user-anchor semantics. Preserve as Aiko's stance rather than reinterpret it as user testimony. |
| [SharedMomentsStore](../../app/core/relationship/shared_moments.py) / [detector](../../app/core/relationship/shared_moment_extractor.py) | Manual, inline-tag, detector and repair moments | Explicit source/vibe/when/confidence; manual pinning. Detector has no-invention/time rules but undated input and almost no retained message IDs in stock. Preserve shared virtual/interpersonal context and distinguish detection date from event date. |
| [TopicDigestWorker](../../app/core/conversation/topic_digest_worker.py) | Cluster summary, long-term, including refresh-in-place | Age-tags inputs and excludes digest recursion in graph clustering. Stored relative phrases and overinterpretation remain; refresh retains old record creation/temporal fields. Digest source IDs should remain derivation links, not extra observations. |
| [HypothesisResolveMixin](../../app/core/session/hypothesis_resolve_mixin.py) | World hypothesis answer and user-answer fact | Two writes; user-answer path retains observed event/quote/message. World path lacks equivalent root receipt. Confirmation of an answer must not promote all of the earlier guessed wording to fact. |
| [MemoryFacadeMixin](../../app/core/session/memory_facade_mixin.py) | Manual UI/API memory and manual gap-answer fact | Two writes; manual addition explicitly grants stated authority/high confidence. Gap-answer sibling lacks equivalent source metadata; editing must invalidate old receipts without pretending to preserve verbatim testimony. |
| [SpeakingWindowJobsMixin](../../app/core/session/speaking_window_jobs_mixin.py) | Dated relationship milestone callback | Deterministic tracker event, low salience and concrete date are good. Explicit event type/relationship subject would preserve that meaning downstream; legacy wording can remain an instruction. |
| [ImageMemory](../../app/core/vision/image_memory.py) | Caption/vision-derived event | Names both participants and retains attachment, source message and seen-at timestamp. Seen time is not necessarily when a depicted event occurred; image interpretation is not user testimony or proof of physical identity. |

### Indirect producers and neighboring stores

- [IdleActivityWorker](../../app/core/world/idle_activity_worker.py) and
  [GardenVisitWorker](../../app/core/world/garden_visit_worker.py) call PursuitNoteWriter
  when there are recorded world effects, completed intentions or meaningful
  activity chains. Preserve that virtual-world grounding and subject.
- [HobbyWorker](../../app/core/proactive/hobby_worker.py) calls the same writer for
  milestones/wrap-ups; result-backed research is distinguished from project ideas.
  Its research coordinator also writes questions and result-linked diaries.
- [Onboarding goal](../../app/core/goals/onboarding_goal.py), goal tools and
  [post-turn goal tags](../../app/core/session/post_turn_mixin.py) share GoalStore.
  Tags/tool invocations still need source actor/time, even though they are deliberate.
- Catchphrase births can also create a shared moment. Inline moment tags,
  manual moments and [conflict-repair bookkeeping](../../app/core/session/post_turn_helpers_mixin.py)
  share SharedMomentsStore. These are separate origins despite one storage helper.
- [SummaryWorker](../../app/core/proactive/summary_worker.py),
  [ArchiveSummaryWorker](../../app/core/proactive/archive_summary_worker.py), belief,
  affection/humor and introspection workers have their own summary/domain stores.
  They are not extra direct MemoryStore writers. Their prose can still feed
  memory-generation prompts; a summary or belief remains a derived input.
- CuePool, hypothesis, concept/event stores and the RAG mirror have `.add`
  methods but are not additional memory source-of-truth writers. External/plugin
  code that is not present in this checkout cannot be certified by static review.

## Recommended implementation sequence

1. **Preserve source identity and enforce allowed uses.** Extend existing memory
   admission/lineage with stable writer, subject, source authority, representation
   purpose and source references/revisions. Enforce it at memory-to-concept
   selection/admission as well as in prose. Start with the three reproduced
   draft/progress/world-fact counterexamples. Keep legitimate Aiko self-knowledge,
   joint memories and world knowledge available in their appropriate roles.
2. **Unify insert/update/merge invariants.** Compatible subject, modality and time
   scopes; source-preserving rewrites; derived outputs cannot inherit unearned
   verbatim authority or create independent support. Preserve original records
   and explicit historical uncertainty. Avoid automatic bulk relabelling.
3. **Fix the high-risk producer contracts.** Goal reflection/action separation;
   source-dated dream/diary/moment inputs; promise source IDs and status-aware
   consumer rendering; pulse self-subject; explicit hypothetical draft exclusion.
   Each slice should have a separate commit and its focused behavior tests.
4. **Validate the remaining producers by role.** Research excerpt support versus
   boilerplate/unsupported inference; images and virtual-world effects; literal
   corrections and personal/world conflicts. Build on current good receipt paths.
5. **Measure source-to-concept quality before lifecycle tuning.** Freeze synthetic
   contrasts with positive controls, then review fresh per-producer cohorts after
   activation. Measure source support, actor, event time, modality, omission and
   independent evidence separately from natural voice or memory usefulness.

Acceptance contrasts should include: an old user event recalled now; a genuine
changed preference versus different contexts; a future promise versus completion;
a hypothetical draft versus the later actual exchange; Aiko's goal reflection
versus a user report; public factual knowledge versus user action; a virtual-world
effect versus a physical-world event; paraphrase versus literal testimony;
same-event diary/digest/extraction versus independent observations; an edited or
deleted source; consolidation across incompatible actor/time scopes; and a user
correction that must outrank a generated interpretation. Positive controls must
retain Aiko's explicit own preferences and useful source-backed general knowledge.

This audit changes no producer behavior and does not certify or repair the
historical graph. Documentation/link/LF checks are appropriate here; no production
code changed and no full-suite or live model-quality evaluation was run.
