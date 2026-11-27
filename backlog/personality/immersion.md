# Immersion and shared activities

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

Immersion H numbers are distinct from health-audit H numbers. Use the existing models, speech owner
and shared TTS/ownership contract. Start user-invoked; pause/cancel/deletion and source authority
are part of each activity. More talking, time spent or inferred attachment are not success measures.

## H2. Calendar / time context block

**Status: Partial.**

**Implemented / available:** Circadian/routine context and K12's opt-in one-off calendar/anticipation exist.

**Remaining:** Add holiday proximity and explicit user-birthday anticipation through existing
owners; do not build a second calendar or circadian clock.

[Implemented slice](shipped/audit-2026-10-24.md#immersion-h2).

**Source check:** [session_controller.py](../../app/core/session/session_controller.py),
[prompt_assembler.py](../../app/core/session/prompt_assembler.py),
[user_profile.py](../../app/core/infra/user_profile.py).

## H7. Listen while speaking — soften the half-duplex turn lock

**Status: Partial.**

**Implemented / available:** Overlap mic ring, energy barge-in, client playback_drained and
default-on interruption exist.

**Remaining:** H7c full duplex/software AEC and actual microphone/playback validation remain.
Coordinate P24; the overlap ring is not full duplex.

[Implemented slice](shipped/audit-2026-10-24.md#immersion-h7).

**Source check:** [live_session.py](../../app/core/session/live_session.py),
[AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts),
[client_mic_source.py](../../app/audio/client_mic_source.py).

## H12. Aiko-initiated intentional gifts — she leaves you something

**Status: Open implementation.**

**Implemented / available:** User-to-Aiko gifts and named hobbies/artifacts exist.

**Remaining:** Implement intentional Aiko-to-user gifts with a real saved output/source, permission
and rare grounded delivery; a narrated gift is not an artifact.

**Source check:** [world_store.py](../../app/core/world/world_store.py),
[idle_activity_worker.py](../../app/core/world/idle_activity_worker.py),
[PersonaActionBanner.tsx](../../web/src/features/persona/PersonaActionBanner.tsx).

## H23. Avatar shared-moment snapshot — she sends you a "selfie"

**Status: Open implementation.**

**Implemented / available:** Live2D expressions/outfits render on canvas.

**Remaining:** Implement deliberate source-linked avatar snapshots, file lifecycle and user-visible
sending; do not claim unseen or generated real-world photos.

**Source check:** [Live2DAvatar.tsx](../../web/src/features/avatar/Live2DAvatar.tsx),
[response_text_service.py](../../app/core/services/response_text_service.py),
[store.ts](../../web/src/store.ts).

## H24. Occasion- / season-aware outfits

**Status: Open implementation.**

**Implemented / available:** Outfit controls and day/weather/world context exist.

**Remaining:** Add explicit occasion/season mapping with user override, ownership and no repeated
changes; do not confuse existing sleep/weather changes with occasion-aware selection.

**Source check:** [OutfitChannel.ts](../../web/src/live2d/channels/OutfitChannel.ts),
[avatar_profile.py](../../app/core/persona/avatar_profile.py).

## H27. Co-presence mode — in the room, not in conversation

**Status: Partial.**

**Implemented / available:** Two-axis Live posture, quiet/unprompted controls, nonverbal presence
and idle-life behavior are implemented.

**Remaining:** Evaluate coherent quiet co-presence and add explicit joined-activity state via IM1;
optional ambience is IM8. Live availability/delivery gaps stay L20/L24, not a second controller.

[Implemented slice](shipped/audit-2026-10-24.md#immersion-h27).

**Source check:** [session_controller.py](../../app/core/session/session_controller.py).

## IM1. Shared quiet sessions

**Status: Open implementation.**

**Implemented / available:** Live quiet posture and presence runtime exist.

**Remaining:** Implement explicit start/pause/resume/leave/checkpoint state with zero inference
during silence, restart-as-paused and no invented work or attendance reward.

**Source check:** [LiveModeToggle.tsx](../../web/src/features/chat/LiveModeToggle.tsx),
[frame.py](../../app/core/live/frame.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** A 30-minute silent session produces zero new model calls and
zero unsolicited messages. Pause survives reconnect; explicit leave suppresses
later activity offers; a return resumes the right label without inventing
progress. Reuse [Live mode](live-mode.md), the existing
[presence/activity model](../../docs/presence-and-activity.md) and its frontend state.

</details>

## IM2. Point at the same thing

**Status: Open implementation.**

**Implemented / available:** Message/image/document attachment and source lookup paths exist.

**Remaining:** Attach one explicit text selection with source/revision/range, removable preview,
bounded context and server-side scope/deletion/revision validation.

**Source check:** [prompt_assembler.py](../../app/core/session/prompt_assembler.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** With two similar passages visible, the reply addresses only
the selected passage; an edited/deleted/cross-user source fails closed; removing
the selection removes its prompt content. Start at the chat/attachment path
and [prompt assembler](../../app/core/session/prompt_assembler.py). This is an
explicit alternative to passive [C6 perception](proactive.md), not more desktop
collection.

</details>

## IM3. A shared reading place

**Status: Open implementation.**

**Implemented / available:** K62 persists explicit media progress; supplied-text/room reading and
library grounding now exist.

**Remaining:** Build a shared-reader UI and permitted-prefix retrieval with IM2 selection, rewind
invalidation and actual-route spoiler contrasts. Grounded idle reading alone is not shared reading.

**Source check:**
[idle_activity_candidates_mixin.py](../../app/core/world/idle_activity_candidates_mixin.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Resume at the saved boundary after restart; rewinding removes
later context. Test a synthetic story with a withheld ending on the actual
route, including leading questions that invite spoilers. Retrieval guards
cannot erase pretrained knowledge: if known-title spoiler tests fail, keep
the pilot limited to supplied unfamiliar texts instead of promising universal
spoiler safety. Owner: [K62](patterns.md#k62-co-experience-companion--follow-a-showalbumbook-with-the-user)
and the existing document path; this item scopes its open media UI/retrieval.

</details>

## IM4. Aiko can show what she made

**Status: Partial.**

**Implemented / available:** Saved research evidence, hobby progression from stored results,
read_diary and Diary source inspection now exist; named activity is no longer the only foundation.

2026-11-06: [saved original writing](../../docs/idle-life-quality-improvements.md) adds actual poems,
fictional scenes, character sketches and wordplay, retained revisions, stale-edit protection,
exact-revision `inspect_item` reads and a World-tab reader. Writing is optional and uses the
existing away-activity model choice; it does not turn fiction into autobiographical facts.
Restart and ordinary-use variety/surfacing checks remain pending.

**Remaining:** Generalize beyond completed short writing to draft/ready/failed artifacts,
cancellation preservation and explicit cross-session comparison. Validate ordinary-use exact-revision
discussion. Research notes and the short-writing slice are not the full creation contract.

[Implemented slice](shipped/audit-2026-10-24.md#immersion-im4).

**Source check:** [pursuit_research.py](../../app/core/proactive/pursuit_research.py),
[diary.py](../../app/llm/tools/diary.py),
[test_pursuit_research.py](../../tests/test_pursuit_research.py),
[test_read_diary_tool.py](../../tests/test_read_diary_tool.py),
[hobby_worker.py](../../app/core/proactive/hobby_worker.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** "Show me" opens the exact stored revision she referred to;
restart preserves it; cancellation, worker failure and no authorized source
cannot manufacture completed work. A second session can discuss a real change
without inventing a first draft. Owners: [H19/H28](shipped/immersion.md),
[hobby worker](../../app/core/proactive/hobby_worker.py) and existing task/output
storage where suitable. Autonomous announcements still depend on L24.

</details>

## IM5. Make something together

**Status: Open implementation.**

**Implemented / available:** Tasks/approvals and saved notes exist.

**Remaining:** Implement a jointly edited bounded document with proposed versus accepted revisions,
authorship, undo and stale-write protection; discussion does not authorize edits.

**Source check:** [approval.py](../../app/core/tasks/approval.py),
[task_store.py](../../app/core/tasks/task_store.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Rejecting a suggestion leaves content untouched; stale replies
cannot overwrite a newer edit; undo survives reload; resuming refers to the
last accepted state, not a rejected suggestion. Reuse existing task approvals
and [brain orchestration](../../docs/brain-orchestration.md); keep artifact revisions
separate from inferred personal memory.

</details>

## IM6. Keepsakes with a real origin

**Status: Open implementation.**

**Implemented / available:** Together/shared moments and source IDs exist.

**Remaining:** Add explicit pin/open/delete with exact origins, scoped excerpts and source-deletion
invalidation. Optional revisit needs consent/K12; export stays J2.

**Source check:** [shared_moments.py](../../app/core/relationship/shared_moments.py),
[test_web_server_together.py](../../tests/test_web_server_together.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** Two similarly named keepsakes retain different source IDs;
deleting one does not erase its source conversation; deleting a source removes
its saved excerpt. Changing/cancelling a revisit invalidates the old offer.
No auto-pinning inferred emotional moments. Owners:
[shared moments](../../docs/shared-moments-and-relationship.md), Together and H12.

</details>

## IM7. A small game at the same table

**Status: Open implementation.**

**Implemented / available:** The tool/plugin/task framework can host a bounded activity.

**Remaining:** Implement one deterministic legal-move game with pause/resign/reconnect/reset and
no-model play; commentary is optional and cannot invent engine reasoning.

**Source check:** [sdk.py](../../app/plugins/sdk.py),
[goal_workflow_handler.py](../../app/core/tasks/workflow/goal_workflow_handler.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** A whole match completes without illegal moves or model access;
reconnect resumes the right turn; duplicate input cannot move twice; old
commentary cannot arrive after a reset as if it describes the new match.
Implement as a bounded activity using [plugin capabilities](plugin-system.md)
where appropriate, not an exception to task/tool authority. Saving a finished
match as an IM6 keepsake is optional, not an automatic memory write.

</details>

## IM8. A quiet acoustic setting

**Status: Open implementation.**

**Implemented / available:** Audio ownership, TTS and earcons exist; ambient_noise is affect
variation, not an acoustic bed.

**Remaining:** Add an explicitly selected local/licensed loop with independent
gain/mute/fades/ducking, single-owner playback and STT echo checks. No inference is needed.

**Source check:** [AudioOutputManager.ts](../../web/src/audio/AudioOutputManager.ts).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** No clipping or masked speech; mute and ownership loss silence
the bed promptly; two windows produce one stream. Check headphones and
speakers for STT echo/barge-in regressions before enabling it in voice mode;
text-only ambience can ship independently if that fails. Owner: the existing
[voice/audio contract](../../docs/voice-mode.md) and
[AudioOutputManager](../../web/src/audio/AudioOutputManager.ts), without changing
the TTS engines or Live2D.

</details>

## IM9. Teach Aiko how to say a name

**Open.** Aiko spells a friend's name correctly in chat but says it wrong every time.
Let the user select the name in a reply, enter a spoken spelling and hear the whole
sentence with the current voice. Save the spelling for that user only if they like the
preview. Start with exact whole names and an editable list. A full name lets the user
narrow a correction when the same spelling has two pronunciations.

**First check.** Teach “Saoirse” using a sentence that also mentions “Saoirseville.”
Listen to both the preview and the next ordinary reply. Only the whole name should
sound different. Reopen the chat and copy the reply to check the original spelling.
Remove the entry and listen again. The shared speech preparation path is the place
to apply the correction. [Speech text preparation](../../app/core/session/session_text_utils.py)

## IM10. Keep my place while my hands are busy

**Open.** “Walk me through this recipe while I cook.” Start with a numbered list the
user pastes. Aiko reads one step and waits. “Again” repeats it, “back” revisits the
previous step and “next” advances. Show the current line and its number so the user
can also take over by tapping. A question like “Was that one teaspoon or two?” should
get an answer from the list while the user's place stays put.

**First check.** Use a four-step recipe with different amounts of the same ingredient.
At step two, ask about its quantity, repeat the line, then disconnect. On return the
card should still show step two and wait for input. Deliver the same “next” event
twice and check that Aiko reads step three once. [Voice ownership and playback](../../docs/voice-mode.md)

## IM11. Pick separately and reveal together

**Open.** “Which title would you put on our story? I have a favorite. You go first,
but hide it.” Give Aiko the same two or three titles the user sees. Have her commit
one choice and one concrete reason before the user picks, then reveal both side by
side. A disagreement gives them something to talk about. Agreement can be just as
interesting when the reasons differ.

**First check.** Offer “Last Tram” and “Night Window.” Once Aiko has picked, branch
the same round and choose a different title on each branch. Both reveals must show
her original title and reason verbatim. Editing a title starts a fresh round.

## IM12. Take a noticing walk together

**Open.** Before a walk, ask Aiko for one small thing to look for, such as “Find
something that makes a sound you usually tune out.” When the user returns, invite
one field note in their own words. Aiko can ask about the particular detail that
caught them. A paper map rattling behind glass gives them a different conversation
from a bicycle ticking as it coasts. A text exchange is enough for the first version.

**First check.** Try the same invitation with each of those two reports and with
“I stayed in.” In the first two, Aiko should respond to the sound the user described
and attribute the observation to them. In the third, let the outing end there.
Leaving the invitation unanswered should produce no check-in or completed walk.

## Moved out of the active queue

- **H6** — shipped: [record](shipped/audit-2026-10-24.md#immersion-h6).
- **H10** — shipped: [record](shipped/audit-2026-10-24.md#immersion-h10).
- **Minor polish** — second TTS backend (Chatterbox), expressive prosody and default-on barge-in are
  implemented; see [TTS registry](../../app/tts/registry.py), [parity
  regressions](../../tests/test_tts_pacing_parity.py) and
  [configuration](../../config/default.json).

<a id="30-sep-2026-immersion-without-a-new-rig-or-model"></a>

IM1-IM8 were the original no-new-model activity proposals. IM9-IM12 were added on 26 November 2026.
