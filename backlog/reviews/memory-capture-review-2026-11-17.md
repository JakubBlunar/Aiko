# Brain memory capture and missed interaction agreements, 17 November 2026

## Request and inspected evidence

The user asked to improve deliberate brain memories and the ordinary extractor,
using an earlier teasing-signal discussion as the concrete missing-memory case.
The preceding companion-quality work was committed separately as `5538e321`.

Read-only MCP status and `mode=ro` SQLite queries were used. No personal dialogue
was sent to a probe, no live worker was forced and no historical memory or model
setting was edited. Examples below describe source roles and record IDs rather
than copying private dialogue. All model experiments used fictional exchanges and
disposable databases. This pass requires a backend restart to load its new grammar
and capability instructions together.

Messages 11305-11308 establish a communication need, a proposed signal, the user's
confirmation that a poke/playful gesture helps identify teasing, and Aiko's chosen
ear-flick refinement plus a proximity-qualified poke. Memory 11679 is a partial
self memory about the ear flick, linked to 11308. It does not retain the user's
need or the full poke qualification. The exchange was therefore partially captured,
not wholly absent from memory. It should not become a claim that teasing always
works, that the user accepted every later refinement or that a gesture was executed.

At inspection, four memories since 10 November identify `admission.writer=inline`;
all are user `self_tagged` notes with empty source-ID arrays. There are no inline
self notes in that cohort. This is evidence of sparse deliberate writing and weak
traceability, not proof that every omission came from a broken writer. Simplified
baseline probes can already elicit useful notes with the old prompt.

## Changes

### Make the existing brain writing capability concrete

A configuration-stable T0 block explains which details should affect future
conversations, including communication needs, boundaries, corrections, signals and
chosen interaction practices. It names no new conversation topic and has no stance
offer. It works independently of the user-editable persona and appears only when
memory storage and embedding are available. No new tool pass or per-turn model
call was added.

An optional JSON body inside the existing remember tags includes content, a literal
source quote and optional temporal type. User and Aiko notes use different tag
forms and different owner sources. The server binds quotations to the same-session
recent window, including the newly completed Aiko reply. This fixes the previous
harvester's inability to cite the very reply in which Aiko made her choice.

Quoted user paraphrases remain inferred/scratchpad notes. Plain legacy notes still
work. A deliberate note is not automatic agreement, observed success or permission
to relax concept thresholds. The consumer can use its source receipts normally;
tests establish communication-style eligibility without forcing a concept.

The capture implementation is a small memory-owned module rather than more code
inside `TurnRunner`. Two admitted notes may be attempted per reply. Malformed JSON,
foreign/missing assistant replies, wrong-owner quotes and invented citations cannot
silently fall back to a raw JSON memory. Sources are rechecked after embedding.

### Preserve qualified information instead of merely rejecting a bad paraphrase

A fictional setup-format probe exposed a valid quote rejected because the proposed
paraphrase omitted a literal condition marker. Both writers now discard that
interpretation and can retain an attributed, literal qualified source sentence.
This does not accept the incomplete paraphrase or weaken `admit_memory` checks.

The fallback is bounded, keeps literal whitespace and message receipts, and cannot
inherit proposed outcomes, relations, supersession or event-time claims. It is
unavailable for invalid/missing testimony or other rejection reasons. Relative
stored wording remains prohibited. The stored marker explains why a literal
source note appears instead of the model's preferred phrasing.

The extractor's cap formerly sliced proposals before admission. Several rejected
proposals could hide a later valid memory. The cap now applies to admitted records,
within the existing generation/output budget. No promotion/dedupe threshold changed.

Its base guidance also prioritizes substantive agreements over incidental activity
and distinguishes ordinary preferences from formal pattern schemas. One person's
need and the other's response keep separate ownership and qualifications.

### Keep private notes private

Remember-tag parsing is shared by capture, output cleanup and self-disclosure
accounting. Quoted JSON can contain single brackets and newlines without leaking
to streaming display, speech or saved chat. Existing malformed single-bracket
cleanup stays compatible. Vulnerability cost classifies the chosen note rather
than a deeper supporting quote or JSON syntax. Failed or incomplete assistant
turns still cannot learn. Content-free logs distinguish saves, citation/fidelity
refusals and changed sources.

## Bounded model evidence and limits

The configured main model is `deepseek-v4-1-flash-uncensored`; the worker model is
`baytout3/qwen3.5-uncensored:9b`. Clients were built in a separate process from a
read-only configuration parse, without migration or configuration writes.

The initial comparison used four fictional exchanges: a teasing signal, numbered
setup steps, casual poking/laughter and an unaccepted wink suggestion. Each ran
with old/new guidance in the brain and extractor lanes: 16 calls total. The brain
probe used a smaller 1024-token cap; extraction used its 4096-token answer budget
with reasoning and the configured context window. Findings:

- Both old brain positive cases already produced notes. New teasing notes added
  actual user and current-assistant citations. This does not establish a measured
  increase in spontaneous capture frequency in ordinary conversations.
- The new setup note initially lost admission through a condition-word mismatch.
  Replaying that same frozen output after the literal-source fallback saves the
  qualified, attributed source without another model call. The frozen teasing
  notes remain source-backed; casual output remains unrecorded.
- One old worker casual-case call timed out. One new brain unaccepted-case call
  truncated to an empty answer. Neither counts as correct abstention.

Four new brain calls then used the configured 1564-token cap, followed by two final
worker cases: six more calls, 22 attempts overall. The setup note saves a qualified
source; casual brain output emits no memory; the wink case keeps uncertainty and
an optional experiment rather than inventing acceptance. The teasing brain reply
truncated at the configured cap, so that attempt cannot count as capture success.
No production budget or reasoning setting was changed to hide these failures.

The final worker exchange explicitly includes the user's conditional gesture need
and Aiko's proximity-qualified response. It retains both, including their conditions,
rather than only the decorative ear detail. It also retains the earlier uncertainty
and offer, so this is a successful recall control, not proof of concise selection.
The final casual worker case still saves two routine gesture/laughter events despite
the stronger importance guidance. Selectivity and model-level meaning remain open.

Frozen-output replay uses four of the already-generated brain replies and adds no
model calls; the empty truncated reply stays an unavailable outcome. These are
diagnostic examples, not a general naturalness, reliability or latency score.

## Validation and next ordinary-use check

The broad affected-source graph selects 458 modules through shared text, prompt and
admission imports. A representative integration set covers real persistence,
notifications, source ownership/edit/delete, final-turn eligibility, quoted-note
privacy, malformed brackets, disclosure pacing, prompt-cache placement, extractor
watermarks/caps and downstream source eligibility. The representative set passes
775 tests plus 7776 subtests; the final affected selection passes 26 tests, overlapping
that set. The last three owned-reply guard contrasts are included in the final
selection. `npm run lint`, whitespace and LF checks pass. The full suite was not run.
Managed test scratch is cleaned after each run; the separate probe directory is
removed after its processes finish.

After restart, review new deliberate notes for frequency and useful coverage,
including corrections, communication needs and reciprocal agreements. Count
admission losses and missing memories as well as false claims. Inspect the writer's
source receipts and then actual later recall/concept use; saved rows alone do not
establish those outcomes. Historical memory 11679 and the older uncited tags were
not silently rewritten or retroactively certified. A source-aware repair of a
specific historical miss is separate from enabling future capture.
