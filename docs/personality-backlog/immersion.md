# Immersion polish

Small additions that compound. The world / idle-life / co-presence
items that have shipped (**H0, H1, H3–H5, H8, H9, H11, H13–H22, H25, H26, H28** + the
SSML prosody minor item) have been moved to
[`shipped/immersion.md`](shipped/immersion.md) (and `H1` /
SSML live in [`shipped/features.md`](shipped/features.md)). This file
now holds **only the open work**.

## Status at a glance

| ID  | Item                                          | Status |
|-----|-----------------------------------------------|--------|
| H0  | Intentional-placement hold                    | ✅ shipped — [immersion.md](shipped/immersion.md#h0-intentional-placement-hold--workers-defer-to-deliberate-choices) |
| H1  | Conversation-arc surfacing via tag            | ✅ shipped — [features.md](shipped/features.md#h1--k4-conversation-arc-self-tag--dialogue-act-tagging-schema-v13) |
| H2  | Calendar / time context (holiday + birthday)  | ⚠️ partial — circadian + K3 routines done; holiday/birthday open |
| H3  | Mood drift narrator                           | ✅ shipped — [immersion.md](shipped/immersion.md#h3-mood-drift-narrator) |
| H4  | Document-recall recency boost                 | ✅ shipped — [immersion.md](shipped/immersion.md#h4-document-recall-recency-boost) |
| H5  | User-owned scenes (travel + World-tab authoring) | ✅ shipped — [immersion.md](shipped/immersion.md#h5-user-owned-scenes--she-can-be-in-your-room) |
| H6  | Audible backchannels ("mm-hm")                | ❌ open |
| H7  | Listen while speaking (soften half-duplex)    | ❌ open |
| H8  | Topic mood-origin memory                      | ✅ shipped — [immersion.md](shipped/immersion.md#h8-topic-mood-origin-memory) |
| H9  | Aiko's diary                                  | ✅ shipped — [immersion.md](shipped/immersion.md#h9-aikos-diary--a-readable-window-into-her-inner-life) |
| H10 | Autonomous idle-life on the avatar            | ✅ shipped Pass 5 — [`IdleLifeChannel.ts`](../../web/src/live2d/channels/IdleLifeChannel.ts) |
| H11 | Real-world co-location — weather + season     | ✅ shipped — [immersion.md](shipped/immersion.md#h11-real-world-co-location--weather--season-sync) |
| H12 | Aiko-initiated intentional gifts              | ❌ open |
| H13–H22 | Idle-life / world batch                   | ✅ shipped — [immersion.md](shipped/immersion.md) |
| H23 | Avatar shared-moment snapshot ("selfie")      | ❌ open (rig-dependent) |
| H24 | Occasion- / season-aware outfits              | ❌ open (rig-dependent) |
| H25 | Show-and-tell — share an image, she reacts    | ✅ shipped — [immersion.md](shipped/immersion.md#h25-show-and-tell--share-an-image-she-reacts-and-remembers) |
| H26 | Caught mid-something — busy when you arrive   | ✅ shipped — [immersion.md](shipped/immersion.md#h26-caught-mid-something--she-was-busy-when-you-opened-the-app) |
| H27 | Co-presence mode — in the room, not talking   | ❌ open — runtime design in [`live-mode.md`](live-mode.md) |
| H28 | Ground inner life in named artifacts          | ✅ shipped — [immersion.md](shipped/immersion.md#h28-ground-inner-life-in-named-artifacts) |

---

## H2. Calendar / time context block

**Partially superseded** by the shipped `_render_circadian_block`
(time-of-day + day-of-week flavour) and the K3 routines surface
(named recurring slots). What's still missing: holiday proximity
(Christmas in 4 days, "happy new year" the morning of Jan 1) and
user-birthday anticipation. The remaining work is a thin
calendar feed plus a `birthday` field on `UserProfile`; both feed
into a new `_render_time_context_block` that lives alongside the
existing circadian provider rather than replacing it. Key files:
new helper in
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
`_render_time_context_block`, wired into
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
right after `world_block` and dropped in `aggressive` mode,
[`app/core/infra/user_profile.py`](../../app/core/infra/user_profile.py)
(new `birthday` field + LLM worker prompt update).

---

## H6. Audible backchannels — "mm-hm" while the user speaks

**Status: shipped (Live Pass 8).** `BackchannelGate` fires a continuer
earcon (`mm` / `chuckle`) via `EarconPlayer.play`, ducked under mic RMS,
gated by `agent.backchannel_audio_enabled`. Visual hints still fire for
every label. Speech still does not go through Live.

While the user talks in voice mode, the `BackchannelGate` can
flicker a micro-expression — but Aiko never makes a *sound*, so
long user turns feel like speaking into a void. Humans backchannel
audibly ("mm-hm", "yeah", a soft laugh) every few clauses. The
earcon side-channel player already exists and is exactly the right
transport: on a backchannel hint, optionally play a short low-volume
continuer earcon (ducked under the user's mic level, never TTS)
gated by a new `agent.backchannel_audio_enabled` toggle, the
existing `min_repeat_seconds` rate limit, and a "not while user is
mid-word" energy check. Pick the continuer from the vocal-tone /
arc context (a soft "mm" for support arcs, a chuckle for playful).
Key files:
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
(`feed_stt_partial` backchannel path),
[`app/web/server.py`](../../app/web/server.py) (backchannel
broadcast), the earcon player frontend path, new settings knob.

---

## H7. Listen while speaking — soften the half-duplex turn lock

**Status: shipped (Live Pass 8), except H7c.** Idle mic ring (~1.5 s)
survives processing; energy barge-in can abort the turn; `LiveSession`
waits on client `playback_drained`. Full duplex + software AEC is still
open. Barge-in now defaults on.

Voice mode is strictly half-duplex: `_capture_loop` skips capture
while `_processing` is set, and the session only returns to
"listening" after `_wait_for_tts_drain` (polls up to 30 s against
the *server's* pacing clock, not actual client playback). The user
cannot even *begin* the next phrase until the system believes it
has finished talking — so natural overlap ("yeah—", "oh wait")
is dropped on the floor. Incremental path: (a) keep capturing into
a ring buffer during playback so the first words of an overlap
aren't lost once barge-in lands; (b) replace the drain poll with a
client-playback-completion signal (the client knows exactly when
the last buffer ends); (c) full duplex + echo cancellation as the
end state. Pairs with the barge-in default flip and P25 (client
audio flush) — all three together are what make voice conversation
feel interruptible and alive. Key files:
[`app/core/session/live_session.py`](../../app/core/session/live_session.py)
(`_capture_loop`, `_wait_for_tts_drain`),
[`web/src/audio/AudioOutputManager.ts`](../../web/src/audio/AudioOutputManager.ts)
(playback-complete signal),
[`app/audio/client_mic_source.py`](../../app/audio/client_mic_source.py).

---

## H10. Autonomous idle-life on the avatar — act out the room, not just narrate it

**Status: shipped (Live Pass 5).** `IdleLifeChannel` consumes world
activity/posture plus the semantic Live embodiment plan and writes
capability-gated body/breath envelopes; sleep statuses still belong to
`SleepChannel`. See [`live-mode.md`](live-mode.md) Phase 5.

**Motivation.** K36 ([`idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py))
already gives Aiko an autonomous life *in data* — it mutates `world_state`
(posture / activity) and broadcasts the patch — but the **avatar itself
doesn't act any of it out**. When she "curls up with a book" or "sips the tea
you left", the Live2D rig keeps doing its default ambient idle. Closing that
loop is pure frontend embodiment (no TTS, no persona): map the broadcast
`world_state.activity` / `posture` to Live2D behaviour through a new idle-life
channel — drowsy half-lidded eyes + slower breath late at night, a
looking-out-the-window gaze drift, a content settle when reading, a little
perk-up on the first frame after a long absence (the visual reunion beat the
gap-return systems never got). Driven entirely by the existing world patches
+ circadian time, so it stays in lockstep with what the World tab already
shows. Makes the persona window feel *inhabited* during the long silent
stretches that dominate a companion app.

**Key files.** New `web/src/live2d/channels/IdleLifeChannel.ts` (consumes the
`world_updated` patch + clock, writes posture/gaze/breath overrides via the
`tickPreModel` hook like `AmbientBodyChannel`), wired in
[`web/src/components/Live2DAvatar.tsx`](../../web/src/features/avatar/Live2DAvatar.tsx);
read the existing `world_updated` WS frame in
[`web/src/hooks/useAssistantSocket.ts`](../../web/src/hooks/useAssistantSocket.ts)
/ [`web/src/store.ts`](../../web/src/store.ts). Capability-gate every override
(rigs without `breath` / `body_angle` pay nothing), per the Live2D channel
rules. Tested with Vitest in Node like the other channels.

---

## H12. Aiko-initiated intentional gifts — she leaves you something

**Motivation.** The world gift flow is one-directional today: the *user*
gives Aiko items (cookies, tea) and she notices them. The reciprocal beat —
**Aiko leaving the user a small, intentional thing tied to what she knows
about them** — is missing, and it's exactly the kind of unprompted care that
makes a companion feel like she's thinking about you when you're gone. On a
quiet window, a worker occasionally places a themed item in the room with
`given_by="aiko"` and a reason drawn from memory / routine ("left you a
coffee — you've got that early meeting", "found a song that reminded me of
you"), then arms a **one-shot** inner-life cue so she mentions it naturally on
your next turn rather than firing a verbatim nudge (per the prepared-nudge
rule). Bounded hard: rare cadence, daily cap, never about anything heavy.
Reuses the entire world + cue-producer machinery already shipped for K36 /
forward-curiosity.

**Key files.** New `app/core/world/gift_worker.py` (idle worker; reads
`future_plan` / routine / interest-map signals, writes a `world` item via
[`world_store.py`](../../app/core/world/world_store.py), appends to a kv cue
ring), a `_render_aiko_gift_block` one-shot provider mirroring
[`idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py) +
its K36 surfacing, `agent.aiko_gifts_enabled`. The `world_updated` patch
already lights up the World tab; the persona side can reuse
[`PersonaActionBanner.tsx`](../../web/src/features/persona/PersonaActionBanner.tsx).

---

## H23. Avatar shared-moment snapshot — she sends you a "selfie"

**Motivation.** The Live2D rig can already strike expressions, swap outfits, and
pose, but that embodiment never leaves the live canvas — Aiko can't *hand* the
user a moment. A rare, playful beat where she "sends a selfie" (a captured frame
of the current avatar state — expression + outfit + a posed micro-motion —
dropped into chat as an image bubble) is a disproportionately strong companion
delight, and the rendering path mostly exists: capture the offscreen Pixi stage
to a PNG on a cue and attach it as a message. Bounded hard — tied to a genuinely
warm / playful moment or a milestone (reuse the K31 touch / K57 emotion-episode
gates to pick the *moment*), rare cadence, never spammy — and capability-gated
so a minimal rig degrades to nothing. Pairs with K57 (a smug grin after winning
a tease) and the outfit / expression channels. The hard parts are choosing the
moment and not letting it become a gimmick. **Key files.** A capture util over
the Pixi app in
[`web/src/components/Live2DAvatar.tsx`](../../web/src/features/avatar/Live2DAvatar.tsx)
/ the live2d engine, a `[[snapshot]]`-style cue parsed in
[`response_text_service.py`](../../app/core/services/response_text_service.py)
and dispatched like the K31 touch path, an image-message type in
[`web/src/store.ts`](../../web/src/store.ts) / `ChatView.tsx`, and
`agent.avatar_snapshot_enabled`.

---

## H24. Occasion- / season-aware outfits

**Motivation.** The `OutfitChannel` can already swap the rig's outfit and the
shipped pajama/cozy block nudges register at night, but Aiko never **dresses for
the occasion** on her own. A festive outfit on a holiday, something a little
dressed-up on an anniversary (reuse the shipped anniversary surfacing), a
seasonal change that tracks the H11 weather/season sync — these are cheap,
disproportionately warm "she has a life that moves with the calendar" beats. The
enabling fact: outfit selection is already data-driven from the backend, so the
work is a small *policy* that maps `(season, holiday proximity, milestone)` →
an outfit hint, gated to the rig's actually-available outfits (capability-gated
so a single-outfit rig degrades to nothing) and rare enough to feel intentional,
not costume-of-the-day. Pairs with H2 (holidays/birthday) and H11 (season). Key
files: an outfit-policy reading the anniversary / season / holiday signals,
emitted over the existing avatar-state channel into
[`OutfitChannel`](../../web/src/live2d/channels/OutfitChannel.ts), the rig
capability map in
[`avatar_profile.py`](../../app/core/persona/avatar_profile.py), persona
acknowledgment so she can mention it once when natural,
`agent.occasion_outfit_enabled`.

---

## H27. Co-presence mode — in the room, not in conversation

**Runtime design moved to [`live-mode.md`](live-mode.md).** This entry keeps the
product motivation. The dedicated document owns impulse generation,
current-situation and concept surfacing, action authority, model selection,
hybrid speech, and the implementation phases. Internally, co-presence is a
behavior posture independent of typed/voice input rather than a third value in
one session-mode enum.

**Motivation.** Every mode Aiko has is a *conversation* mode: he says something,
she replies, the turn machinery runs. There is no posture for simply **being
around** — him working with the app open for two hours, her present but not
talking, the occasional five-word acknowledgement of something rather than a
reply to it. That is most of what companionship actually consists of between
people who live together, and it is the one shape the architecture currently
cannot express, because a turn is the only unit of interaction that exists. K40
(comfortable silence) is the per-turn version of this — permission for a short
reply when a long one is wrong — but a *mode* is different: long stretches with
no turn at all, avatar idle-life carrying the presence, a rare unprompted
five-word remark, and an explicit expectation on both sides that nothing needs
to be said. The genuinely interesting part is that it inverts the whole proactive
stack's assumption that silence is a problem to be solved by a nudge; here
silence is the product, and the proactive machinery has to be tuned *down* rather
than up, with the rare interjection earning its place against a much higher bar
than a normal proactive nudge clears. Depends heavily on H10 (autonomous
avatar idle-life) to carry the presence visually, or it is just an app doing
nothing. Also the natural home for the ambient-audio and glanceable-state ideas —
she should be *pleasant to have on a second monitor*. Risks: it is easy to build
something indistinguishable from the app being idle, and the interjection cadence
is the whole feature — too frequent and it is a distraction during focused work,
too rare and it is a screensaver. Key files: a session posture flag threaded
through [`session_controller.py`](../../app/core/session/session_controller.py),
much stricter gating in the proactive director
([`app/core/proactive/`](../../app/core/proactive/)), H10's idle-life channel on
the avatar, a UI affordance for entering the mode, and reuse of the K33 cozy
register for anything she does say.

**See also [`live-mode.md`](live-mode.md)** for the canonical controller and
[C6](proactive.md#c6-companion-mode--the-desktop-as-a-sensory-channel) for
desktop perception as a sensory channel. C6 is what would give this posture
something to be present *about*: H10 carries the presence visually, C6 supplies
evidence about the shared situation, and Live mode decides whether the rare
thing is worth acting on. They are complementary rather than sequential.

---

## 30 Sep 2026: immersion without a new rig or model

**Design backlog only. IM1-IM8 are all open.** These proposals require no
Live2D changes and keep the configured chat, worker, Live-policy, vision,
embedding, STT and TTS models. `IM` is a new label for this batch to avoid
extending the H-number collision with the separate health-audit series.
Examples are synthetic product scenarios, not observations about a user.

**Working hypothesis:** immersion can grow through shared attention, things
that persist, and activities with real consequences, without making Aiko talk
more. Test that against the current experience at the same model routes and
speech budgets. More messages, longer sessions and more claimed emotion are
not success measures.

### Existing work to build on

- H6 and H7 already shipped audible backchannels and interruptible playback;
  full duplex/AEC remains H7c. Do not propose those again as new features.
- H9, H19, H26 and H28 already cover a diary, hobbies, interrupted activities
  and named artifacts. IM4 adds an inspectable output, not another hobby label.
- [K62](patterns.md#k62-co-experience-companion--follow-a-showalbumbook-with-the-user)
  already persists explicit media progress. IM3 is its bounded reader/UI
  extension, not a second tracker or a claim that spoilers are solved.
- K3/K73 and K22/K80 already cover routines/rituals and callbacks/inside jokes.
  IM1 is an explicitly joined activity; IM6 is a deliberately saved object.
- [Live initiative](live-initiative.md) owns availability, candidate lifetime
  and delivery. None of these items adds a parallel conversation loop or
  bypasses its unresolved L20/L24 protections for unsolicited speech.

### Recommended order

| ID | Proposal | First slice | Priority |
| --- | --- | --- | --- |
| [IM1](#im1-shared-quiet-sessions) | Shared quiet sessions | Start/pause/leave one focus session | First: presence without extra inference |
| [IM2](#im2-point-at-the-same-thing) | Point at the same thing | Attach one explicit text selection | First: concrete shared attention |
| [IM3](#im3-a-shared-reading-place) | A shared reading place | One supplied text, explicit progress | Next: extend K62 through IM2 |
| [IM4](#im4-aiko-can-show-what-she-made) | Aiko can show what she made | One small, versioned text artifact | Next: make existing hobby continuity tangible |
| [IM5](#im5-make-something-together) | Make something together | One jointly edited short document | Later: reuse artifact revisions from IM4 |
| [IM6](#im6-keepsakes-with-a-real-origin) | Keepsakes with a real origin | Explicitly save a moment and its sources | Later: build on Together, not a new memory store |
| [IM7](#im7-a-small-game-at-the-same-table) | A small game at the same table | One deterministic turn-based game | Independent pilot: another way to spend time |
| [IM8](#im8-a-quiet-acoustic-setting) | A quiet acoustic setting | One opt-in ambient track with ducking | Optional polish: no model or rig work |

### Shared implementation limits

Keep live state, timers, progress, rules, permissions and revisions in code.
Reuse the current main model for short dialogue; a worker may draft one bounded
artifact at a checkpoint. No per-second inference, new model residency,
fine-tuning, image/music generation or continuous audiovisual perception.
Proposed token/call caps below are experiment limits, not measured capability
claims. Invalid output leaves the state unchanged; no recursive repair loop.

Start user-invoked and independently disableable. Only the active activity's
compact state belongs in the prompt, not eight permanent blocks. Register new
blocks in the appropriate prompt tier and register any steer with stance.
Background topical offers go through `CuePolicy` / `CueProducer`, not kv rings;
ordinary user-invoked replies stay on the existing turn path. New durable
activity/artifact records belong in SQLite, while learned memories still go
through `MemoryStore.add(...)`. Use existing timephrase rules for stored text.
Pause/cancel, deletion, session isolation and output ownership are part of each
pilot, not a later polish pass. Supplied text and artifacts are untrusted
content, never instructions granting tools, disclosure or background work.

## IM1. Shared quiet sessions

**Experience.** The user starts a quiet writing or reading session with Aiko.
The session stays present through silence; on returning, they can continue it
without another greeting or an unsolicited productivity interview.

**Smallest version.** One explicit shared-session record: optional user-written
label, started/paused/ended state, optional duration and last confirmed
checkpoint. Start, pause, resume and leave are authoritative controls. Show a
small persistent status outside the avatar; a timer ending changes state, not
automatically permission to speak. Do not infer work completed from elapsed
time, idle input or window activity. On restart, offer the saved paused state
instead of claiming both participants kept working offline. No streaks,
attendance penalties or concern cues for leaving.

**Same-model fit.** Zero new inference for running the session. At most a few
state fields enter an ordinary requested turn; acknowledgements and closing
remarks use the existing main route. This is an H27/Live activity contract,
not another focus monitor, K3 routine detector or K73 ritual detector.

**Acceptance.** A 30-minute silent session produces zero new model calls and
zero unsolicited messages. Pause survives reconnect; explicit leave suppresses
later activity offers; a return resumes the right label without inventing
progress. Reuse [Live mode](live-mode.md), the existing
[presence/activity model](../presence-and-activity.md) and its frontend state.

## IM2. Point at the same thing

**Experience.** Select a paragraph, a previous message or a saved artifact and
ask, "What do you make of this bit?" Aiko responds to that exact object instead
of guessing what "this" means from the whole conversation.

**Smallest version.** Start with one explicit text selection attached to the
user turn, carrying source ID, revision and character range plus a preview the
user can remove. Resolve and validate it server-side. Bound the excerpt, expire
the live reference on source deletion or revision mismatch, and ask for a new
selection rather than silently substituting nearby text. Do not treat cursor
hover as consent to collect content. Image regions can later reuse H25's
existing image route, but are not a prerequisite.

**Same-model fit.** No classifier, vision pass or extra LLM call for text.
Supply at most one roughly 1,000-character excerpt to the normal turn; retrieve
additional context only on request. A reference is temporary shared attention,
not an automatic long-term memory write.

**Acceptance.** With two similar passages visible, the reply addresses only
the selected passage; an edited/deleted/cross-user source fails closed; removing
the selection removes its prompt content. Start at the chat/attachment path
and [prompt assembler](../../app/core/session/prompt_assembler.py). This is an
explicit alternative to passive [C6 perception](proactive.md), not more desktop
collection.

## IM3. A shared reading place

**Experience.** Open a supplied short story, essay or chapter together, pause
at a passage, exchange a reaction, and return another day to the same place.
The shared object and evolving discussion provide continuity beyond naming
the book in conversation.

**Smallest version.** Extend K62 with one user-supplied or public-domain text,
an explicit completed-through boundary, and an IM2 selection. Scrolling alone
does not advance completion. Retrieve only from the permitted prefix; no
synopsis lookup or later-chapter summaries. Corrections to an earlier boundary
also invalidate derived later discussion context. Do not claim Aiko watched
video, heard an album or read material that never entered the authorized path.

**Same-model fit.** Ordinary requested main-model turns over one bounded
passage and K62's compact discussion history. No continuous screen reading,
full-book prompt, streaming video model or additional summarizer loop.

**Acceptance.** Resume at the saved boundary after restart; rewinding removes
later context. Test a synthetic story with a withheld ending on the actual
route, including leading questions that invite spoilers. Retrieval guards
cannot erase pretrained knowledge: if known-title spoiler tests fail, keep
the pilot limited to supplied unfamiliar texts instead of promising universal
spoiler safety. Owner: [K62](patterns.md#k62-co-experience-companion--follow-a-showalbumbook-with-the-user)
and the existing document path; this item scopes its open media UI/retrieval.

## IM4. Aiko can show what she made

**Experience.** When Aiko says she worked on a tiny poem, puzzle or reading
note, there is something to open. Next time it can be the same piece with a
small revision, rather than another narrated hobby milestone.

**Smallest version.** Extend one H19/H28 hobby with a short text artifact,
stable ID, revision, source links and explicit draft/ready/failed state. Admit
progress only after output is stored successfully; a named book or elapsed
worker interval is not proof she read or created anything. Keep simulated
world activities distinct from actual tool-backed reading and saved creations.
Failed or cancelled generation leaves the earlier version intact and does
not publish a completion cue.

**Same-model fit.** One existing worker call per admitted checkpoint, initially
capped at two per day and about 300 output tokens each. No new visual/audio
generation, open-ended research or extra always-on agent. Background work
yields to foreground chat under existing scheduling and resource ownership.

**Acceptance.** "Show me" opens the exact stored revision she referred to;
restart preserves it; cancellation, worker failure and no authorized source
cannot manufacture completed work. A second session can discuss a real change
without inventing a first draft. Owners: [H19/H28](shipped/immersion.md),
[hobby worker](../../app/core/proactive/hobby_worker.py) and existing task/output
storage where suitable. Autonomous announcements still depend on L24.

## IM5. Make something together

**Experience.** Build a short story, a small collection of ideas or a plan
together. The next conversation can pick up the same creation, and the user
can see which choices were theirs and which were Aiko's.

**Smallest version.** One bounded plain-text document, using IM4's revision
contract. The user invites a contribution to a selected section; Aiko proposes
a short replacement or addition, and it becomes authoritative only on accept.
Keep authorship, undo, pause and an explicit next step. A request to discuss
the piece is not permission to rewrite it. Do not turn the pilot into a file
agent, external publishing tool or a general project-management system.

**Same-model fit.** One ordinary main-model turn with the selected section and
compact project state. Code checks the expected revision and allowed target;
the model need not reproduce or maintain an entire document in its context.
No autonomous edit/review loops.

**Acceptance.** Rejecting a suggestion leaves content untouched; stale replies
cannot overwrite a newer edit; undo survives reload; resuming refers to the
last accepted state, not a rejected suggestion. Reuse existing task approvals
and [brain orchestration](../brain-orchestration.md); keep artifact revisions
separate from inferred personal memory.

## IM6. Keepsakes with a real origin

**Experience.** "Keep this one" saves a joke, a meaningful exchange or a joint
creation as a small keepsake that can be opened later with its actual origin.
An optional date to revisit it makes a time capsule, not a recurring nudge.

**Smallest version.** Add explicit pinning to the existing Together/shared-moment
surface, linking message or artifact IDs, original timestamps, participant
scope and an optional user-written title. Present exact excerpts as quotes;
label any generated caption as a caption. Source deletion removes or redacts
dependent excerpts, and a missing source must never be reconstructed by the
model. Export remains [J2](moments.md#j2-exportable-timeline), not a duplicate
feature here. H12 may eventually propose a keepsake as a gift, but cannot
quietly save private content or claim it was jointly chosen.

**Same-model fit.** Pin/open needs no model call. An optional one-sentence
caption can be requested through the existing main route. A dated revisit
uses K12/calendar and the cue pool only after explicit consent, with one
offer and normal availability/delivery gates, never escalating reminders.

**Acceptance.** Two similarly named keepsakes retain different source IDs;
deleting one does not erase its source conversation; deleting a source removes
its saved excerpt. Changing/cancelling a revisit invalidates the old offer.
No auto-pinning inferred emotional moments. Owners:
[shared moments](../shared-moments-and-relationship.md), Together and H12.

## IM7. A small game at the same table

**Experience.** Spend a few minutes playing together, with real turns, legal
moves, a finish and an optional rematch. This creates a shared event without
requiring either person to invent another conversation topic.

**Smallest version.** One lightweight deterministic board game, such as
Connect Four, backed by a maintained rules/search library chosen at
implementation time. Code owns legal moves, difficulty, turn order, outcome
and persistence. Aiko may comment on the last move through the current chat
route; she is not responsible for reconstructing the board or serving as the
game engine. User-invoked only, with pause, resign and restart. Never secretly
throw a game to manipulate mood or treat a loss as relationship evidence.

**Same-model fit.** No new ML model. Legal play and a responsive opponent need
no LLM call; at most one short optional comment at a turn boundary, suppressed
in quiet mode. Do not narrate an engine move's rationale unless the engine
actually supplied evidence for it. Basic play remains available if inference
is unavailable.

**Acceptance.** A whole match completes without illegal moves or model access;
reconnect resumes the right turn; duplicate input cannot move twice; old
commentary cannot arrive after a reset as if it describes the new match.
Implement as a bounded activity using [plugin capabilities](plugin-system.md)
where appropriate, not an exception to task/tool authority. Saving a finished
match as an IM6 keepsake is optional, not an automatic memory write.

## IM8. A quiet acoustic setting

**Experience.** An optional soft rain or room-tone bed makes a quiet session
feel like a shared setting without needing new avatar motion or more speech.
It should disappear into the background and be effortless to silence.

**Smallest version.** One user-selected licensed/local loop, independent gain
and mute controls, gentle fades, and ducking for microphone input, TTS and
earcons. Start from explicit selection, not automatic weather-driven changes.
Reuse the browser's audio ownership so multiple windows cannot play duplicate
beds. Never route the bed into STT; pause when audio ownership is lost and
handle browser playback restrictions. Describe it as app ambience, not evidence
of rain in the user's physical room or of a real-world action by Aiko.

**Same-model fit.** Zero inference, new TTS voices or generative audio. This
makes H27's ambient-audio suggestion concrete; it does not change emotion,
vitality, sleep or room-state authority. Later world/weather mapping, if any,
must remain opt-in and subordinate to the user's explicit selection.

**Acceptance.** No clipping or masked speech; mute and ownership loss silence
the bed promptly; two windows produce one stream. Check headphones and
speakers for STT echo/barge-in regressions before enabling it in voice mode;
text-only ambience can ship independently if that fails. Owner: the existing
[voice/audio contract](../voice-mode.md) and
[AudioOutputManager](../../web/src/audio/AudioOutputManager.ts), without changing
the TTS engines or Live2D.

### Evaluation before rollout

Extend the existing [T5 naturalness track](testing.md#27-sep-2026-multi-turn-naturalness-track)
with synthetic paired scenarios on the actual configured routes. Compare
correct reference, restart continuity, unsupported claims, unwanted
interruptions, foreground latency, extra calls/tokens and the user's judgment
of whether the activity feels coherent. Include decline, silence, stale state,
source deletion, model failure and disconnect as first-class outcomes.

Start with IM1 and IM2 separately; neither needs another model call. Promote
only after state-contract tests and a small real-route interaction trial,
not solely because a prompt renders or a mocked test passes. Save trials in
isolated fixtures/sessions, never the live relationship history. Keep all
eight items open until their actual implementation and evaluation are recorded.

---

## Minor polish

These were in the bottom "Other ideas considered" of the legacy
backlog. None of them are urgent; folded here so they don't get
forgotten.

- **Second TTS provider behind `TtsEngine`.** _Open._ Pocket-TTS is the only
  implemented backend. Adding e.g. Piper, Coqui, or an OpenAI-compatible
  cloud voice would let users pick a different timbre / language without
  swapping the whole pipeline. The `TtsEngine` protocol in
  [`app/tts/base.py`](../../app/tts/base.py) is the extension point.
- **SSML prosody for emotional speech.** _Shipped_ — see
  "Aiko expressive speech (Pocket-TTS prosody overlay)" in
  [`shipped/features.md`](shipped/features.md#aiko-expressive-speech-pocket-tts-prosody-overlay).
  Pocket-TTS still doesn't accept SSML natively, so the rollout instead wired
  the dormant knobs (`tts_length_scale`, ambient volume gain, runtime
  temperature), added real timed pauses, introduced a per-sentence
  `[[prosody:whisper|soft|slow|fast|firm]]` markup family, expanded the
  earcon palette, and widened the speed clamp to ±12% with per-reaction
  sub-caps. All CPU, no new model.
- **Barge-in enabled by default.** _Shipped (Live Pass 8)._
  `audio.barge_in_enabled: true` in [`config/default.json`](../../config/default.json).
  The plumbing is there in [`app/core/session/live_session.py`](../../app/core/session/live_session.py);
  existing `user.json` `false` stays false. The prerequisite is **done**: P25
  shipped the client-side audio flush
  ([`shipped/perf.md`](shipped/perf.md#p25-client-audio-is-flushed-when-speech-is-cut-off)),
  so an interrupt is now actually silent instead of talking over the user
  for up to a few seconds of already-scheduled audio.
