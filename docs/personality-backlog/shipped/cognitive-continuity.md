# Cognitive continuity implementation

## K62. Explicit shared-media pilot (29 Sep 2026)

One session-local media thread retains a title, episode/chapter completion
boundary and up to four discussion exchanges by message ID. The main reply
can revisit an earlier interpretation in light of new user evidence without
another worker or model call. This is contextual continuity, not a proactive
offer: it adds no stance candidate, cue type or new pressure to speak.

Progress comes from bounded explicit English declarations, for example
`I finished chapter 4 of Glass Harbor` or `I started North Station ep 4 tonight`.
Finished/watched/read sets the completed boundary; started/watching/reading
keeps it at the preceding part. A bare numbered update can refer to the one
active thread. A lower correction discards later discussion references;
switching works clears the old references. `Stop tracking this book` (or
`this show`) clears the references and retains a stop watermark against old
updates. No implicit viewing, season mapping, canonical plot lookup, album
tracking or automatic progress detection is included.

Post-turn persistence validates message speaker, session and ordering in
SQLite. The T6 `media_context_block` reads that state and projects the live
declaration without writing, including in preview/aggressive assembly.
On later turns it requires the title to be raised explicitly, with stricter
matching for short ambiguous titles. Transcript excerpts are bounded and
age-tagged, distinguish user evidence from Aiko's revisable proposals, and
do not certify viewing, attention or comprehension. No separate copies of
raw conversation are stored in the thread metadata.

The prompt restricts discussion to user-supplied details within the progress
boundary, with no later hints or outside summaries. This is **not a proven
spoiler guarantee**: the main model still has its own knowledge and ordinary
history/retrieval. Model-level spoiler/adversarial tests and measured opinion
continuity remain prerequisites to broader media retrieval or promotion.

Owners: [media_thread.py](../../../app/core/conversation/media_thread.py),
[conversation_situation_mixin.py](../../../app/core/session/conversation_situation_mixin.py)
and the existing post-turn/prompt pipeline. Tests use isolated SQLite and
cover source/session validation, correction, stop/retry, work switches,
discussion lineage and non-mutating assembly.

## K14/K23. Answer-aware brevity (29 Sep 2026)

The shared turn-shape reader recognizes bounded completion, acknowledgement,
playful backchannels and yes/no answers to a preceding closed question.
K23 does not interpret those as withdrawal. K14 keeps negative typed-length
deviations diagnostic-only: a short typed reply cannot reduce closeness or
be labelled abandoned from length alone. Supported brief replies also protect
voice engagement from negative timing/length scores without awarding closeness.
Absence accounting and baseline updates remain intact.

Post-turn context reads messages strictly before the current user-message ID,
not the assistant reply just generated. Missing context falls back safely.
Ambiguous brevity is not proof of success or distress; K23's fallback cue asks
for proportional length without inventing a relationship problem. Coverage:
engagement, misattunement detector and provider regressions. No live quality
or false-positive rate is claimed from these synthetic cases.

## K18/K40. Progress and explicit endings (29 Sep 2026)

K18 now distinguishes explicit progress reports from low topical distance.
Reported findings, narrowed options and working results suppress that turn's
lull without discarding its baseline sample or spending a new cooldown.
The shared standing-lull reader and K54 agree; the progress flag resets even
when the next turn has no embedding. This is a bounded lexical pilot, not a
general semantic progress measure or a new K97 observer field.

Explicit completion selects FOLLOW plus K92's existing HOLD axis, with a
few-word acknowledgement permitted. Optional stance providers are guarded
at registration against attempt-local completion, before one-shot state or
pooled cues can be consumed. Preview and aggressive reassembly share that
rule; later turns retain their offers. Repairs, task results, attachments
and delivery evidence remain available. A fresh question or mixed request
does not match the strict completion reader. Silence is not completion.

Tests cover same-topic progress/repetition, turn-local freshness, stance,
and real prompt assembly with non-consumption and owed-content controls.
This ships K40's explicit-completion slice only; inferred comfortable silence,
co-presence and measured conversational naturalness remain open.

## Live L18. Cue purpose and exact handoff

Shipped 27 Sep 2026. Live projection assigns server-owned purpose to known
share/continuation sources; remaining registered cues conservatively remain
asks and unknown types cannot wake speech. A neutral `share_observation`
urge preserves non-question content through the question gate. The numbered
menu carries purpose, not cue bodies or titles, and the model cannot change
it through proposal arguments. Parking, merging, and expiry preserve it.

Main-turn rendering resolves the selected ID through `CueStore.available`
and `take_pool_cue(cue_id=...)`; it cannot substitute a sibling, spend a
consumed/expired cue, or bypass type cadence. Dispatch rechecks speech
permission, quiet/DND, and source availability. No speech budget increased.

Owners: [cue_adapter.py](../../../app/core/live/cue_adapter.py),
[main_wake.py](../../../app/core/live/main_wake.py), and
[live_mode_mixin.py](../../../app/core/session/live_mode_mixin.py).
Regression coverage: [test_live_mode_pass27.py](../../../tests/test_live_mode_pass27.py)
and [test_cue_pool_consumption.py](../../../tests/test_cue_pool_consumption.py).
Human evaluation of grounded contributions versus interruption remains T5;
this is a tested delivery contract, not evidence of greater naturalness.

## Live L17. One later opening

Shipped Pass 32. A cue-pool urge that expires without a server-admissible
speech opening becomes deferred, not immediately forgotten. Only a real
busy-to-open typing/playback transition or focus-boundary notice can offer
one reconsideration. Heartbeats and unchanged silence cannot. Deferred
entries are capped at the active-store limit and age out after fifteen
minutes. Reconsideration requires the source to remain in the available
pool; exact handoff still checks relevance, cadence and source state.

An urge that already had an opening, was enqueued, was withdrawn, or already
used its reconsideration does not rearm. Enqueue consumes only the temporary
urge, not the durable cue. Queue dispatch now compares mode generations in
the same domain and rejects actions queued before intervening user intent.
Speech permission, quiet, sleep, question limits and budgets remain gates.

Coverage: [test_live_mode_pass3.py](../../../tests/test_live_mode_pass3.py)
and [test_live_mode_pass10.py](../../../tests/test_live_mode_pass10.py), plus
the Live/cue regression slice. Real-world interruption and missed-opening
rates remain a T5 evaluation task, not a claim established by these tests.

## K97. Evidence-linked working understanding

The existing conversation-situation observer now carries one open,
user-evidenced question, up to three reported facts, one tentative
interpretation and one unresolved premise. Notes are capped at 160
characters and cite validated transcript IDs. Each refresh replaces the
set; missing/cleared output removes it rather than preserving unsupported
interpretations. SQLite stores it inside the existing situation JSON.

The prompt distinguishes reports, hypotheses and unknowns, gives the latest
user message precedence, and does not turn this understanding into an
offer to speak. It uses the existing situation block/tier, not a new steer.
Non-spatial questions need no room lease; actual world conflicts still
suppress the reading. Old-source writes, intervening input and changed
session/mode tokens cannot overwrite newer state. The observer retains its
post-reply cadence; its output cap increases from 240 to 480 tokens.

Coverage: [test_conversation_situation.py](../../../tests/test_conversation_situation.py)
and the prompt-cache regression tests. The paired referent/correction/
unknown-answer evaluation remains open: structural tests do not establish
that an extra working set beats the existing summary and history.

## K98. Answer-earned interest continuation

The situation observer may propose one observation after closing an explicit
shared comparison/project or Aiko-pursuit question. The old owned-thread
retirement is unchanged. Production requires new, substantive user-answer
evidence and topical overlap with the proposed change; acknowledgements,
common explicit declines and unsupported proposals do not earn a successor.
An all-state source lookup prevents another successor from the same question.

`CueProducer` owns publication as `interest_continuation`: one stocked cue,
one surfacing, one-hour expiry and cadence. The cue owner enforces session,
topic and quiet checks even for an exact Live claim. A pivot or explicit
decline expires the offer; newer observer evidence withdraws an older offer.
It has its own T6 block and CALLBACK stance entry. Conditional handling
allows a new observation or silence, never a repeated ask or activity claim.
No additional worker or ask-pressure budget was introduced.

Coverage: conversation-situation and owned-thread tests, cue accounting and
consumption, persona hoisting, stance and prompt-cache ordering. Naturalness
and semantic progress still need the paired K98/T5 behavior evaluation; the
substance and decline checks are conservative English-language guards, not
a general proof of understanding an answer.

## K99. Recent delivery provenance

`DeliveryLedger` keeps four recent responses, each with at most 32 identified
audio clips. The turn runner supplies response identity, and the TTS queue
captures it per text item before asynchronous playback. Optional binary
audio-start tokens preserve legacy playback. Owner-, scope- and
generation-checked receipts distinguish completed clips, interrupted clips
and unknown delivery. Server send completion alone is never playback proof.

Completed visible message bubbles may send a separately identified text
presentation receipt. Existing explicit reactions count as acknowledgement,
not comprehension or agreement. A non-steering T6 block exposes bounded
recent evidence and cautions against treating unplayed/generated/private
material as discussed. It deliberately has no stance offer. No raw audio or
new persistent message archive is stored.

See [voice-mode.md](../../voice-mode.md#delivery-evidence-k99) for wire format,
limits and compatibility. Tests cover interrupted key clips, complete clips,
wrong/stale owners and generations, duplicate receipts, queued identity,
text-only delivery and missing telemetry. Browser audio accounting has
behavior tests; visible-bubble wiring follows the repository's source-test
convention. Actual hardware audibility and T5 continuity benefit remain open.

## Verification

All five implementation phases have focused regression coverage and passed
`npm run lint`. Final backend run: 11,894 passed, 8 skipped, 1,269 subtests
passed, with the unchanged pre-existing private-reach guard failure
(471 MCP private accesses against budget 466). Frontend: 820 tests passed.
No production messages, forced cues or live settings changes were used to
establish these results. The original T5 behavior comparisons remain open.

**29 Sep 2026 judgment/media follow-up verification:** final backend suite
12,041 passed, 8 skipped, 1,304 subtests passed; the sole failure remains the
unchanged MCP private-reach guard (471 against 466). Python lint, frontend
typecheck and backlog links pass. Validation used synthetic/isolated state,
not live conversations; no runtime settings, model routes or live data changed.
