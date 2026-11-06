# Further worker quality review: 6 Nov 2026

This follows the [memory-boundary improvements](memory-boundary-improvements.md).
It reviews generation inside workers, beyond the six intake/recall boundaries
already implemented. The goal remains grounded understanding over memories,
with creative reflection kept available under its actual source meaning.

## Method and limits

Reviewed growth witness and its mood-sample inputs; background and post-sleep
diaries; evolution diary; knowledge-map reflection; idle knowledge and curiosity
research. Checked ordinary summary, thread resummary and introspection as
comparisons, and inspected the promotion/style-decay paths without treating
retention or elapsed-time decay as new testimony.

The app answered a read-only status request. SQLite was opened with `mode=ro`
and `PRAGMA query_only=ON`. Bounded stored examples were inspected; no live
worker was forced, model called, message sent or memory changed. Synthetic
probes exercised the actual pure renderers/parsers with generic inputs.
The snapshot is not a post-restart error-rate measurement or proof that every
current source path is active in the running process.

## 1. Growth witness changes whose state it is describing

**Highest priority: confirmed subject error.** `record_daily_sample()` reads
Aiko's affect and internal relationship axes. `GrowthWitnessWorker._evaluate()`
feeds those samples to `detect_growth()`. The renderer then describes the user
as lighter, steadier, more comfortable or more trusting. The drift threshold
and minimum sample count are real gates; they do not establish the user's state.

The optional goal detail compounds this: `_goal_detail()` reads Aiko's own
`GoalStore`, accepts any non-empty `last_progress_note`, and calls its title
"the work he's been putting into ...". A reflection or intention is sufficient;
neither user ownership nor a reported step is required. Six active goals with a
last progress note were present in the inspected snapshot. This does not mean
six incorrect cues were delivered.

Synthetic probes showed both transformations: rising affect samples produced
a cue about the user seeming lighter, and an Aiko goal with a reflective note
became work attributed to the user. The longer-window copy also compares the
user to when the relationship first began, although the detector only compares
parts of a rolling sample window. Surfacing does not pass the finding's date to
the prose renderer, so an older finding can be expressed as "lately".

**Improve:** describe these signals as Aiko's tentative experience, with the
actual comparison period. A claim about user growth needs its own dated user
observations. Keep Aiko goals on Aiko's side, and distinguish reflection from
reported action. A subject-specific offer should use the existing cue pool with
source receipt, freshness and consumption tracking rather than a kv watermark.

**Acceptance:** Aiko's mood can improve while the user reports difficulty,
without generating a claim that the user improved. An Aiko goal cannot become
the user's work. A rolling fortnight cannot establish change since first meeting.
An old unsurfaced finding cannot describe the present without a freshness check.

Sources: [mood sample writer](../app/core/affect/mood_drift_worker.py),
[growth worker](../app/core/proactive/growth_witness_worker.py),
[growth renderer](../app/core/relationship/growth_witness.py),
[goal ownership](../app/core/goals/goal_store.py).

## 2. Background diary needs one frozen source packet

Existing safeguards are useful: transcript lines retain actor/age labels;
earlier diary entries are explicitly subjective continuity; the prompt preserves
imagined-scene framing; output rejects relative deictics; journal metadata marks
background authorship and Aiko reflection. Unchanged-context gating already exists.

The remaining input budget can cut a message at its prefix. If the latest line
alone exceeds the budget, `build_recent_context()` keeps its beginning, including
the actor/time label, but can lose a final negation or qualification. A probe
retained the opening of a proposed plan while omitting its final statement that
it was never done and remained imaginary.

The fingerprint and transcript are also obtained through separate provider reads.
`_run()` captures a key, composes from text, and `_persist()` obtains the session
again after generation/embedding. It does not revalidate the captured message
revisions. A source edit or session switch during that work can therefore leave
an entry linked to a context key/session that does not describe its actual input.
This is a verified code path, not an observed live session-switch incident.

An adjacent path, thread resummary, has a genuine history-revision save guard
but still trims each message to 300 characters before composing its present-tense
thread note. That guard prevents writing against edited history; it does not
restore a qualification removed from the prompt. Include that input packing in
the same review rather than treating a revision check as sufficient source fidelity.

**Improve:** capture message IDs, session, recording dates, roles and revisions
once, and render whole qualified records from that packet. Keep earlier diary
entries as generation context, not additional observations. Recheck captured
rows before saving and retain their derivation receipt. New messages alone need
not invalidate an unchanged older source; edits and mismatched ownership do.
An oversized fragment must not be presented as complete testimony.

**Acceptance:** a final "only imagined" or "I did not do it" survives selection,
or the incomplete row is omitted. Switching sessions cannot relabel the entry's
source. Editing a captured message defers the derived write. Genuine subjective
reflection remains allowed without manufacturing user facts.

Sources: [diary worker](../app/core/proactive/diary_worker.py),
[provider wiring](../app/core/session/idle_workers_init_mixin.py),
[journal authorship contract](../app/core/memory/diary_journal.py),
[thread input packing](../app/core/proactive/thread_resummary_worker.py).

Post-sleep diary already keeps the episode ID and anchors its event time to the
episode ending. Its output's `[:500]` is another whole-output refusal candidate;
this is lower priority than the background diary's source-packet problem.

## 3. Research: offered sources are not selected support

Both research workers preserve search excerpts, retrieval time, selected indices
and attribution status. The primary URL already follows a selected index;
personal follow-up questions are excluded from the public research lane. These
protections should remain.

However, `research_evidence.evidence_metadata()` puts all offered excerpt URLs in
`source_urls`. `_memory_lineage()` treats those URLs as support roots, without
using `supporting_source_indices` or `evidence_support`. A probe selecting only
the second of two excerpts still returned both URLs as complete source lineage.
The unattributed path likewise retains the offered URL set.

The inspected DB had 73 rows with one selected index and multiple listed URLs,
including memories 9635, 9994 and 9614. That count describes metadata shape, not
73 false facts. The current independence report is a shadow diagnostic; this
finding does not establish an automatic confidence increase. It does establish
incorrect attribution of offered results as support, which can also distort
overlap/independence grouping between findings.

There is a second deterministic loss: `_parse_facts()` slices facts longer than
240 characters. A synthetic 378-character fact lost its final exception and
became a 233-character generalization. The resulting text is what gets embedded
and stored. Confidence thresholds do not recover that qualification.

Idle curiosity also retains the question object through search/distillation,
then writes a finding and marks its ID resolved without rechecking the question's
revision/purpose. An edited question can receive an answer to its earlier text.

**Improve:** separate offered excerpts from per-finding cited support and make
lineage consume the latter. Unknown attribution stays unknown. Keep literal
support spans and bounded source identity; a citation is not semantic entailment.
Refuse/regenerate whole overlong facts. Freeze/recheck the original question
before attaching an answer or resolving it. Retrieval time remains distinct
from publication time and from proof that a changing fact is still current.

**Acceptance:** selecting source B creates no support root for unrelated A;
missing indices do not make all results supporting evidence. A final exception
cannot disappear at the length cap. Editing/deleting a question during research
does not resolve a different question or create a falsely linked answer.

Sources: [research receipts](../app/core/memory/research_evidence.py),
[knowledge worker](../app/core/proactive/idle_knowledge_worker.py),
[curiosity worker](../app/core/proactive/idle_curiosity_worker.py),
[lineage reader](../app/core/concepts/concept_evidence_lineage.py).

## 4. Evolution diary loses subject and historical time before composition

This is a separate append-only change log, not the ordinary memory diary.
Its entries already retain event IDs, concept IDs and period bounds. Ordered
pagination, diversity selection and watermark behavior preserve useful history.
Keep that store and purpose intact.

`render_learning_brief()` nevertheless maps only `subject=aiko` to "about myself";
every other subject becomes "about them". A relationship event therefore receives
a user-directed subject hint. A synthetic relationship event reproduced that
exact output. The renderer omits each event's date, while `_compose()` adds
neither a today anchor nor the stored-text time rule. The store accepts the
generated prose without a relative-deictic check.

Historical backlog makes this consequential: inspected entry 995 links 12 events
and has an Aug-to-Oct period under a Nov creation stamp. Period metadata is
present, but the composer did not see those dates through the learning brief.
The entry uses present-understanding language. This does not prove its claims
are false or that it was produced by the newest running code; it demonstrates
why historical event periods must survive into the composition context.

**Improve:** preserve user/Aiko/relationship subjects explicitly and age-tag each
recorded change. Include period bounds, today anchor and the stored-text rule.
Describe what changed at that historical time. A claim about what is held now
needs current support; a later withdrawal must not erase the earlier historical
change. Retain the existing click-through event links.

**Acceptance:** relationship change cannot silently become a user trait; a
backfilled August change cannot acquire a November occurrence date. Historical
and current understanding remain distinct even when later evidence reverses it.

Source: [evolution diary worker](../app/core/concepts/evolution_diary_worker.py).

## 5. Knowledge-map reflection flattens useful concept context

This worker already uses `ConceptView` and a registered kind diet. It has a today
anchor and stored-text time rule; its outputs are classified as Aiko reflections.
The issue is not absence of concepts.

`_territory_labels()` reduces concepts to strings, and `_rich_line()` presents
those labels as "you believe" alongside cluster size and recency. Formal subject,
concept identity, validity scope and support revisions do not reach the composer
or resulting memory. Stored map reflections retain a source name and reflection
time, without the concept/source packet that generated the interpretation.

Cluster activity uses memory creation/last-use times. "Hot this week" can reflect
recall activity; it does not by itself establish fresh user discussion or verified
knowledge. A timer can also trigger another reflection without checking whether
the meaningful source context changed.

The snapshot contains 48 map reflections, including 11222, 11101 and 10919. These
counts are not a repetition rate. They make this a worthwhile provenance and
usefulness review rather than a purely hypothetical unused feature.

**Improve:** provide bounded, qualified concept records with subject and source
scope; distinguish conversation, recall activity and recorded knowledge coverage.
Retain source revisions and skip unchanged meaningful context. Treat derived
reflections as reflection history, not independent factual coverage or new
observations that justify another reflection about the same territory.

**Acceptance:** an Aiko taste and user preference remain distinguishable in the
same topic. Recalling an old note does not become a new user interest. Unchanged
evidence does not mint another paraphrase merely because a cooldown elapsed.

Sources: [map worker](../app/core/proactive/knowledge_map_reflection_worker.py),
[cluster activity](../app/core/conversation/topic_graph.py).

## Suggested delivery order

Fix growth-witness ownership first, then the background diary's source packet.
Research attribution/whole-fact admission follows, then evolution-diary historical
scope and knowledge-map context. Reuse the existing source receipt and qualified
record helpers rather than introduce a second memory store or blanket concept
injection into every worker.

Ordinary summary and thread resummary already age-tag transcript inputs and
guard persistence against history revision changes. Introspection already frames
contradicted beliefs as provisional hypotheses with alternatives and dates.
The next changes should bring the remaining workers toward those contracts,
while preserving deliberate journalling and creative inner life.
