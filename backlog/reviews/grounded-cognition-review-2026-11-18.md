# Grounded memory, understanding and immersion — 18 November 2026

## What to improve first

Aiko needs memories rich enough to use in the right situation. A short memory can
carry rich evidence if it preserves who said what, the thing they meant, when and
where it applied, and whether they proposed, tried, corrected or reported an
outcome. The original evidence can stay linked and expand on demand. One ordinary
detail used accurately at the right time can do more for continuity than several
abstract descriptions of the user's personality.

The priority is to preserve meaning at capture, then carry it through topic
summaries, concepts and recall. The most concrete engineering fix in this review
is to make all digest readers enforce the same source and freshness checks.
The highest-value capture change is to retain a complete interaction agreement
or meaningful episode even when its information spans several turns.

## Scope and evidence

The installed SQLite database and its stored surfacing ledger were inspected in
read-only transactions. The 72-hour window ending the evening of 18 November UTC
contained 121 memory rows out of 5,906 stored. The spot checks below compare
selected original turns, memory rows, topic membership, positive concept evidence
and occasional downstream reply records. Sampling was diagnostic. The window can
span different deployed revisions, and there is no denominator of all exchanges
worth remembering. The analysis does not estimate a capture or recall success rate.

| Signal | Read-only observation | Interpretation |
| --- | --- | --- |
| Source receipts | All 19 memories classified as user observations in the 72-hour window had a source-message link. 29 extractor rows had claim-fidelity receipts. | Traceability is useful for review. Matching a quote does not establish the paraphrase or its modality. |
| Rich evidence | None of the 121 rows had an `observation` or `follow_through` receipt. The cohort included 28 Aiko reflections and 28 virtual activity records. | The optional fields are sparse here. It is still unknown how many clear context/action/outcome opportunities existed or which writer/configuration was deployed. The kinds serve different purposes. |
| Topics | 5,143 assignments across 99 topics. 41 topics contain both user and Aiko rows. Cluster 13 has 178 notes and one recorded observation group in its current evidence profile, with 168 notes of unknown lineage. | Row count represents material to navigate. The profile can expose the difference between many notes and much usable evidence. |
| Exposure | The 72-hour ledger has 1,393 memory selections and 2,300 concept selections across core, flex and activation. 599 memory selections are currently classified as Aiko declarations and 124 as user observations. | This is exposure in one conversational mix. Score actual use by reading the question and reply. Echo and selection counts do not show that an item helped or caused an error. |

No model was called, no test conversations were added, and no stored memory,
concept, topic assignment or runtime setting was changed. Case references use
local record IDs and paraphrase private dialogue.

## 1. Preserve the meaning of the original exchange

- **A useful positive.** Memory `11668` preserves both the completed replacement
  and the remaining intermittent noise. It leaves occurrence time unknown. This
  is the kind of compact evidence a later troubleshooting reply can use. When a
  report gives a result, the useful retained unit is the change and its qualified
  outcome together.
- **A fragmented agreement.** Messages `11305–11308` contain a reason the user
  needs a teasing signal, a response they approved, and Aiko's chosen refinement.
  `11679` keeps the refinement. It leaves out the user-side need and the precise
  scope of what they endorsed. A correct future memory should distinguish the
  user's need, the agreed cue and Aiko's own conditional practice. Later evidence
  that the cue actually helped would add an outcome. The original proposal would
  remain a proposal until the user accepted it. This case is also documented in
  the [17 November capture review](memory-capture-review-2026-11-17.md).
- **A sourced inference became an event.** Message `11473` contains a shared-scene
  gesture and an expression of worry. Memory `11770` adds the actor's purpose
  of resolving tension and is classified as a user observation. It is one viable
  recorded alternative for active value `7107`. The row has other support and had
  no recent surfacing record in this window. The concrete issue is that the quote
  supports the gesture and the expressed worry while the added purpose gained the
  same evidence standing. Preserve virtual shared participation, the actual
  emotion reported by its owner, and any inferred intention as distinct roles.
- **A quote can lose its object.** `11763` records six episodes of an unnamed
  show and an opinion on pacing. `11765` records something a character can do
  as an ongoing user observation. The saved texts have traceable sources, but
  neither record carried a topic assignment at inspection. The same storage
  convention also admitted `11755`, a fragment about knowing Aiko's dreams with
  unresolved speaker-relative wording. A relevant media exchange can enrich an
  identified viewing thread. An unnamed show, fictional claim or dueling
  pronouns needs a clear referent before it becomes reusable personal knowledge.

The writer already asks for durable needs, explicit corrections and limits on
routine gestures. It also supports optional evidence fields and a literal-source
fallback. The next step is to review high-value **opportunities** and measure
whether those fields and actor/scope distinctions survive the whole path. Adding
more fields to the prompt alone would leave omissions and misclassification
unmeasured. [Extractor](../../app/core/memory/memory_extractor.py),
[memory admission](../../app/core/memory/memory_admission.py),
[source roles](../../app/core/memory/memory_origin.py).

## 2. Let clusters organize recall and preserve each claim's authority

Mixed topics can be useful. A shared experience, Aiko's reflection and related
outside reading can belong near one another while serving different questions.
The current evidence profile and role-balanced digest inputs already expose much
of this distinction. The gap is at the read boundary.

- Digest `11790` stores one relationship-history claim and three world-knowledge
  claims with individual scopes and source IDs. RAG can turn the digest into a
  single paragraph in the topic expansion. The reader already has the metadata
  needed to use the history when asked about a shared memory and the research
  when asked a general question. The stored research is reported here as a source
  category. Its scientific truth was outside this review.
- Digest `11795` sets a specific date and adds family and arrival details to a
  claim citing `11593`. That cited memory records a visit and late activity,
  but carries no occurrence time and does not contain those added details. A
  structured scope and an offered source ID still need claim-level meaning
  checks before the digest becomes a reliable compression.
- The ordinary temporal filter recognizes `summary_claims.valid_until`.
  Cluster expansion uses `_lookup_cluster_digest`, which checks that a digest
  row exists and is the right kind. It then appends the digest before doing the
  receipt checks for raw siblings. A small synthetic probe executing the two
  source functions returned an expired digest from cluster lookup while the
  ordinary temporal filter rejected it. This is a specific path discrepancy.
  The real digests above had no selected memory rows in the 72-hour ledger, so
  their observed data quality and this reproducible read gap are separate from
  a demonstrated bad answer.

Use the existing claim receipts to select relevant clauses and verify their
sources at read time. A stale or inapplicable digest should leave room for
valid raw siblings. Repeated notes, their topic summary and concepts derived
from them should remain one traceable family when selecting context. Keep
current topic membership while testing this change. Cluster splits need evidence
of retrieval or comprehension failures after authority-aware reading.
[Digest construction](../../app/core/conversation/topic_digest_worker.py),
[topic evidence profile](../../app/core/conversation/topic_evidence.py),
[RAG retrieval and rendering](../../app/core/rag/rag_retriever.py).

## 3. Check what the concept actually adds

The partial teasing note `11679` survives in live support for identity `13695`,
value `13696` and narrative `13704`. The identity calls the signal reliable, while
the note establishes only Aiko's choice. The broader value and narrative cite
adjacent material about play, affection and viewing habits. Those rows can be
relevant context without establishing a general claim about how the signal works.
Value `13696` had five core selections and narrative `13704` two flex selections.
One reply marked as an echo for the value concerned a different immediate topic
in the same conversational register. The echo supports neither successful recall
of the signal nor proof that the value caused that response.

The earlier diary finding remains operationally relevant. Active `13690` and
`13691` still each have `11669` as their only viable recorded support. The prior
review found that the diary added an agreement absent from its source packet.
Together these concepts had 45 recorded selections in the 72-hour window. The
evidence revision being current explains why the dependency gate alone can pass
them. New diary admission checks and the depth-2 safeguards do not retroactively
establish better evidence for those historical claims.

Review a small group of frequently surfaced claims in order of consequence.
Compare the actual premise and original source with the label. Ask which wording
adds a reasonable common principle and which adds a motive, reliability, scope
or outcome with no basis. Use L49 to expose repeated roots and L52 to withdraw
unsupported current use while preserving history. An evidence review can retain
a narrower claim when supported. Avoid widening depth or changing confidence
thresholds to improve how this cohort looks.
[Earlier diary trace](companion-continuity-review-2026-11-17.md#1-source-to-memory-to-later-understanding),
[depth-2 backlog](../personality/concepts.md#l46-validate-useful-depth-2-abstractions).

## 4. Test the experience in a conversation

Use paired cases with the same memory count, prompt budget and reply model. One
case should need the particular memory. The contrast should change a condition,
speaker, date, topic or outcome. Keep the comparison usable with isolated
synthetic data while the ordinary-use cohort grows.

| Situation | What a useful reply should do | Contrast to check |
| --- | --- | --- |
| A signal agreed during a shared scene | Use the agreed cue when it fits, without narrating its storage | A new gesture was proposed but never accepted |
| Resuming a show | Recover the correct title and episode position and avoid spoilers | A similar scene belongs to a different show |
| Returning to a repair | Recall what was tried, what improved and what still fails | The same repair was only proposed, or the outcome was later corrected |
| Revisiting an earlier interaction | Retain the virtual or conversation framing and the actual feeling the person reported | The same imagery came from a diary, fictional quote or outside reading |
| An ambiguous callback | Ask one narrow question or leave the reference tentative | A strong recent anchor already resolves it |

Label each case independently for capture, grounding, selection and final use.
Count important omissions, unsupported claims, missing qualifiers, wrong
actor/domain, stale-source leakage, needless questions and unnecessary callbacks.
Preserve cases where it is natural to say nothing about memory at all. For
ordinary use, review whether the detail helped the turn rather than whether it
was mentioned. Keep source IDs and local revisions in the private evaluation;
use fictional examples for tracked fixtures.

The implementation queue is linked from the
[personality backlog](../personality/index.md#grounded-memory-and-cognition).
