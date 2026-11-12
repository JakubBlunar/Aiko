# Companion patterns

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

The historical K1-K107 status catalogue is now in [the shipped
catalogue](shipped/pattern-status-history.md). K97-K99 and K102/K103 implementation is archived;
their named behavioral checks remain in
[T5](testing.md#t5-llm-behavioural-eval-suite-separate-track-not-a-ci-gate).

## K12. Calendar-linked anticipation

**Status: Partial.**

**Implemented / available:** Opt-in one-off calendar storage/tool and staged approaching-event cues
are implemented.

**Remaining:** UI, recurrence/all-day events, cancelled-event restoration, sync/alarms, automatic
extraction and real-model anticipation checks remain.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k12).

**Source check:** [calendar.py](../../app/core/goals/calendar.py),
[calendar_anticipation.py](../../app/core/proactive/calendar_anticipation.py),
[test_calendar.py](../../tests/test_calendar.py),
[rag_retriever.py](../../app/core/rag/rag_retriever.py),
[follow_up_worker.py](../../app/core/proactive/follow_up_worker.py).

## K19. Cold-start companion onboarding

**Status: Open implementation.**

**Implemented / available:** First-run onboarding collects a name; relationship/profile storage exists.

**Remaining:** Implement a short conversational onboarding arc with explicit preferences/boundaries
and evaluated cold-start exploration; shared-turn engagement is not cue-level approval.

**Source check:** [FirstRunOnboarding.tsx](../../web/src/features/onboarding/FirstRunOnboarding.tsx).

## K33. Cozy mode — persistent register softening

**Status: Open implementation.**

**Implemented / available:** Soft/slow prosody, circadian context and intimacy controls exist; the
cozy library is a different feature.

**Remaining:** Add explicit persistent cozy-register state, controls and bounded auto-entry/exit
across prompt/cadence/UI.

**Source check:** [settings.py](../../app/core/infra/settings.py),
[cadence.py](../../app/core/voice/cadence.py).

## K40. Comfortable silence — don't always fill space

**Status: Partial.**

**Implemented / available:** Explicit completion can get a brief acknowledgement while optional cues
defer without consumption.

**Remaining:** Evaluate inferred comfortable silence separately; brevity, elapsed silence or low
engagement do not establish completion. Preserve owed repairs/results.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k40).

**Source check:** [stance.py](../../app/core/conversation/stance.py),
[judgment_shadow.py](../../app/core/conversation/judgment_shadow.py).

## K41. Same-reply mid-stream self-correction (embedding variant)

**Status: Open implementation.**

**Implemented / available:** Next-turn correction and inline correction markup exist.

**Remaining:** Add a validated stream-time contradiction check and bounded abort/continuation splice
across transcript/TTS; measure cost/detection first.

**Source check:** [turn_runner.py](../../app/core/session/turn_runner.py),
[conflict_heuristics.py](../../app/core/memory/conflict_heuristics.py),
[self_correction_detector.py](../../app/core/conversation/self_correction_detector.py).

## K42. Multi-bubble reply bursts — texting rhythm

**Status: Open implementation.**

**Implemented / available:** The normal turn streams/persists one main assistant message.

**Remaining:** Implement opt-in bounded bursts with shared turn identity, delayed-bubble
cancellation and delivery/memory attribution.

**Source check:** [turn_runner.py](../../app/core/session/turn_runner.py),
[session_controller.py](../../app/core/session/session_controller.py),
[store.ts](../../web/src/store.ts),
[useAssistantSocket.ts](../../web/src/hooks/useAssistantSocket.ts).

<a id="k49-messiness-permission--typed-imperfection"></a>

## K49. Rare earned self-edit cues

**Status: Partial.**

**Implemented / available:** Speech-texture permission, spoken filler controls and correction
parsing/strike-through/earcon plumbing exist.

**Remaining:** Evaluate a rare earned self-edit cue. Do not manufacture mistakes or claim markup is
missing; over-polish detection was deliberately deferred after disfluency overuse.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k49).

**Source check:** [session_text_utils.py](../../app/core/session/session_text_utils.py),
[prompt_support.py](../../app/core/session/prompt_support.py),
[cadence.py](../../app/core/voice/cadence.py),
[aiko_style_tracker.py](../../app/core/persona/aiko_style_tracker.py).

## K50. Typed-mode delivery pacing — the missing "read → pause → type" beat

**Status: Open implementation.**

**Implemented / available:** Generation status and spoken cadence/prosody exist.

**Remaining:** Implement opt-in context-sensitive read/pause/type presentation and typed delivery
metadata; test responsiveness without forced delay on urgent practical replies.

**Source check:** [turn_runner.py](../../app/core/session/turn_runner.py),
[session_controller.py](../../app/core/session/session_controller.py),
[cadence.py](../../app/core/voice/cadence.py),
[ChatView.tsx](../../web/src/features/chat/ChatView.tsx).

## K62. Co-experience companion — follow a show/album/book with the user

**Status: Partial.**

**Implemented / available:** Explicit per-user show/book progress and cited discussion persist
across sessions/restarts with correction/stop/source validation.

**Remaining:** Canonical prefix-limited retrieval, albums, concurrent titles, implicit progress, UI
and real-model spoiler tests remain; IM3 owns the shared reader.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k62).

**Source check:** [media_thread.py](../../app/core/conversation/media_thread.py),
[response_text_service.py](../../app/core/services/response_text_service.py),
[prompt_assembler.py](../../app/core/session/prompt_assembler.py).

## K77. Candor gate — "can I be real with you?"

**Status: Partial.**

**Implemented / available:** Opt-in taste-only ask-first candor uses K29 trust/tenure/source gates
and a persisted cooldown.

**Remaining:** Evaluate generated consent and offer-versus-delivery behavior. Broader
concern/relationship candor needs evidence/calibration, which L44 does not yet supply.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k77).

**Source check:** [candor_gate.py](../../app/core/relationship/candor_gate.py),
[relationship_axes.py](../../app/core/relationship/relationship_axes.py).

## K79. Hesitation tell — typing latency as a signal

**Status: Open implementation.**

**Implemented / available:** Composing presence and engagement latency exist, without calibrated
hesitation inference.

**Remaining:** Define privacy-preserving compose timing/baselines and evaluate a rare leave-space
cue. Delay plus brevity cannot establish concealed feelings; timing need not transmit draft text.

**Source check:** [session_controller.py](../../app/core/session/session_controller.py),
[engagement_tracker.py](../../app/core/affect/engagement_tracker.py).

## K83. The right to decline — she always answers, and that's the tell

**Status: Partial.**

**Implemented / available:** The opt-in disclosure-only boundary pilot has narrow eligibility,
persistent cooldown and overridable next-turn permission.

**Remaining:** Evaluate generated adherence, alternatives and asking again. Broader emotional
requests, affect triggers and repair arc remain absent; distinguish chosen boundary from provider
refusal.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k83).

**Source check:** [decline_gate.py](../../app/core/relationship/decline_gate.py),
[concept_lifecycle.py](../../app/core/concepts/concept_lifecycle.py).

## K84. Calibrated jealousy — the risky one, written down honestly

**Status: Partial.**

**Implemented / available:** Explicitly invited opt-in comparison uses consent/context/cooldown
gates without durable jealousy episodes/axis penalties.

**Remaining:** Evaluate no-guilt/no-carryover and boundary behavior. Unsolicited/absence-based
jealousy is deliberately excluded, not planned missing work.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k84).

**Source check:** [calibrated_jealousy.py](../../app/core/affect/calibrated_jealousy.py),
[post_turn_mixin.py](../../app/core/session/post_turn_mixin.py).

## K92. Conversational stance — one decision per turn, not ten permission slips

**Status: Partial.**

**Implemented / available:** Stance phases 1-2 and staged cross-type selection cover thoughts,
continuations, drift, associations, seeds and calendar; final included claims commit.

**Remaining:** Convert eager legacy providers to non-mutating candidates; arbitrate essential
handling with content and evaluate final inclusion/real-route quality. Judgment shadow is not
activated; global arbitration is unfinished.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k92).

**Source check:** [chat_database.py](../../app/core/infra/chat_database.py),
[inner_life_part1.py](../../app/core/session/inner_life_part1.py),
[inner_life_part2.py](../../app/core/session/inner_life_part2.py),
[inner_life_part3.py](../../app/core/session/inner_life_part3.py),
[prompt_assembler.py](../../app/core/session/prompt_assembler.py),
[lead_follow_report.py](../../scripts/lead_follow_report.py).

<a id="k93-the-substance-floor--what-she-takes-to-the-floor-not-whether-she-takes-it"></a>

## K93. Expand substance ranking coverage and validation

**Status: Partial.**

**Implemented / available:** Ledger ranking and CuePolicy.substance type priors are read by staged
selection; the old unread-substance claim is false.

**Remaining:** Extend provider coverage and compare useful content at equal budgets. Coarse priors
are not semantic quality or causal engagement credit.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k93).

**Source check:** [cue_accounting.py](../../app/core/proactive/cue_accounting.py),
[cue_producer.py](../../app/core/proactive/cue_producer.py),
[wants_ledger.py](../../app/core/conversation/wants_ledger.py).

## K96. One shot per thought — in-turn deliberation

**Status: Partial.**

**Implemented / available:** Post-reply second_thought, concrete adapter checks and content-free
error categories exist.

**Remaining:** Diagnose deployed NONE/valid-thought behavior, then measure
admission/expression/answer and next-turn interference. In-turn effort selection and other
experiments remain unimplemented; adapter tests do not prove the live failure fixed.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k96).

**Source check:** [second_thought_worker.py](../../app/core/proactive/second_thought_worker.py),
[test_second_thought.py](../../tests/test_second_thought.py),
[chat_client.py](../../app/llm/chat_client.py),
[openai_compatible_client.py](../../app/llm/openai_compatible_client.py),
[turn_runner.py](../../app/core/session/turn_runner.py).

## K100. Evidence-bound memory admission

**Status: Partial.**

**Implemented / available:** Plain tags/batch extraction share source/speaker/literal-support
admission, correction lineage and retry checkpoints.

**Remaining:** Cover other writers; evaluate paraphrase entailment, useful retention and false
durable writes. Two guarded writers are not graph-wide evidence admission.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k100).

**Source check:** [turn_runner.py](../../app/core/session/turn_runner.py),
[memory_extractor.py](../../app/core/memory/memory_extractor.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Synthetic cases: user statement; assistant speculation about
the user; self-disclosure; explicit correction; dated temporary plan; duplicate
extraction; invented evidence ID; session switch; partial embed/write failure.
Measure correct attribution, useful retention, false durable writes and
correction lineage separately. Readable-empty extraction still advances;
retrying a partly applied batch neither loses accepted candidates nor duplicates
them. Replay against isolated state and compare with the current writers before
expanding to other workers. No extra synchronous LLM call on every chat turn.

</details>

## K101. Route an unresolved premise to a bounded next step

**Status: Partial.**

**Implemented / available:** Observer-signaled memory needs can trigger strict enabled-built-in
recall-only passes with freshness/topic/attempt guards.

**Remaining:** Evaluate decisions/latency and consider bounded ask/tool/wait choices beyond recall;
empty/disabled subsets stay closed.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k101).

**Source check:**
[conversation_situation.py](../../app/core/conversation/conversation_situation.py),
[tool_pass_gate.py](../../app/core/session/tool_pass_gate.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Compare synthetic multi-turn cases with identical budgets:
missing referent, evidence already available, fresh unresolved fact with no
keyword, explicit correction, changed subject, disabled capability, and a task
already complete. Check grounded answers and correct capability availability,
unnecessary calls/questions, latency and unauthorized actions. A quiet "not
enough evidence" result can be correct. Start in shadow mode on isolated
fixtures; require better decisions rather than a higher tool-call rate.

</details>

## K104. Cause-specific feelings and mixed emotions

**Status: Partial.**

**Implemented / available:** Cause-keyed episode updates and bounded mixed rendering are implemented.

**Remaining:** Semantic cause matching, generalized correction/resolution and paired conversational
evaluation remain; unrelated positive turns are not resolution evidence.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k104).

**Source check:** [emotion_episodes.py](../../app/core/affect/emotion_episodes.py),
[test_emotion_episodes.py](../../tests/test_emotion_episodes.py),
[expression_mask.py](../../app/core/affect/expression_mask.py),
[inner_life_part3.py](../../app/core/session/inner_life_part3.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** An unrelated compliment does not certify resolution of a
different issue; relevant clarification can resolve it. Repeated evidence does
not amplify an episode. Mixed feelings produce one proportionate response,
not a recitation of mood labels or oscillating faces. Acknowledged/resolved
issues stay resolved. Normal goodbyes, work, other friendships and declined
romance do not generate new grievance episodes. Silence can remain appropriate.

</details>

## K105. Expression follows communicative intent

**Status: Partial.**

**Implemented / available:** Pause-only expressive beats and subtle mood mist use existing shared
speech/avatar controls.

**Remaining:** Run semantic-selection/listening and interruption/timing comparisons including
neutral controls; general intent-to-expression selection and new laughter synthesis are not
implemented.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k105).

**Source check:** [cadence.py](../../app/core/voice/cadence.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[ExpressionChannel.ts](../../web/src/live2d/channels/ExpressionChannel.ts).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** In a blinded listening comparison, delivery fits the intended
meaning without reducing intelligibility or worsening first-audio latency.
Paired sincere/teasing lines remain distinguishable without canned laughter.
Interrupted or cancelled chunks cannot fire a late grin, laugh or pause. Text
mode remains natural without invented spoken fillers, unsupported rigs degrade
gracefully, and two clients do not both perform a playback-linked beat. Tags
remain absent from stored/displayed text. Silence and neutral delivery are
valid controls, not failures to be made more expressive.

</details>

## K106. Shared shorthand with negotiated meaning

**Status: Partial; live validation remains (28 Oct 2026).**

**Implemented / available:** Explicitly adopted definitions are stored through MemoryStore by the
existing extraction call. Source receipts, owner/session/activity/relationship scope, expiry,
corrections and retirement gate reference resolution in the existing T3 memory budget. Proposals
and unclear replies cannot establish adoption. Current-source checks prevent stale vector recall
from bypassing the contract. Intake counters and inclusion traces support the live trial.

**Remaining:** Evaluate natural adoption/paraphrases, referent accuracy, corrections and unwanted
callbacks on the current model. The conservative literal matcher is not general semantic agreement.

**Source check:** [shared_shorthand.py](../../app/core/memory/shared_shorthand.py),
[test_shared_shorthand.py](../../tests/test_shared_shorthand.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Reuse an agreed term without explaining its origin every time;
use its meaning to resolve a later reference. A corrected name replaces the
active reading without losing source history. Identical words in another task
do not import the old referent. Declined names stay unused; ambiguous meanings
can remain unresolved. Deleting the underlying source invalidates unsupported
derived entries. Measure referent accuracy and unwanted callbacks, not phrase
reuse frequency.

</details>

## K107. Provider outcomes are not character evidence

**Status: Partial.**

**Implemented / available:** Explicit failure/refusal/truncation/cancellation provenance,
stream/UTF-8/private-thinking guards and selected delayed-writer checks exist.

**Remaining:** Establish origin for ordinary text-only refusal, audit downstream promotion and
evaluate route swaps/partial audio/cancellation/tool effects; streamed audio cannot be retracted and
tool replay is not authorized.

[Implemented slice](shipped/audit-2026-10-24.md#patterns-k107).

**Source check:** [chat_client.py](../../app/llm/chat_client.py),
[turn_runner.py](../../app/core/session/turn_runner.py),
[chat_turn_mixin.py](../../app/core/session/chat_turn_mixin.py),
[post_turn_mixin.py](../../app/core/session/post_turn_mixin.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Synthetic provider refusal/error and no-answer budget exhaustion
leave durable character boundaries and trust unchanged. A real user stop still
takes effect immediately. Interrupted output is not treated as a completed or
heard exchange, and retrying cannot duplicate tool side effects. Unknown origin
stays unknown; a supported explicit character preference can still be learned
through the existing evidence rules. Switching routes preserves existing
preferences rather than relearning them from the new provider's defaults.

</details>

## Moved out of the active queue

- **K78** — shipped: [record](shipped/audit-2026-10-24.md#patterns-k78).
- **K97** — shipped: [record](shipped/audit-2026-10-24.md#patterns-k97).
- **K98** — shipped: [record](shipped/audit-2026-10-24.md#patterns-k98).
- **K99** — shipped: [record](shipped/audit-2026-10-24.md#patterns-k99).
- **K102** — shipped: [record](shipped/audit-2026-10-24.md#patterns-k102).
- **K103** — shipped: [record](shipped/audit-2026-10-24.md#patterns-k103).
