# Immersion polish

Small additions that compound. The world / idle-life / co-presence
items that have shipped (**H0, H1, H9, H11, H13–H22** + the SSML
prosody minor item) have been moved to
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
| H5  | Second scene / travel semantics               | ❌ open (deferred; H22 shipped the lightweight precursor) |
| H6  | Audible backchannels ("mm-hm")                | ❌ open |
| H7  | Listen while speaking (soften half-duplex)    | ❌ open |
| H8  | Topic mood-origin memory                      | ✅ shipped — [immersion.md](shipped/immersion.md#h8-topic-mood-origin-memory) |
| H9  | Aiko's diary                                  | ✅ shipped — [immersion.md](shipped/immersion.md#h9-aikos-diary--a-readable-window-into-her-inner-life) |
| H10 | Autonomous idle-life on the avatar            | ❌ open (no `IdleLifeChannel` yet — the data moves, the rig doesn't act it out) |
| H11 | Real-world co-location — weather + season     | ✅ shipped — [immersion.md](shipped/immersion.md#h11-real-world-co-location--weather--season-sync) |
| H12 | Aiko-initiated intentional gifts              | ❌ open |
| H13–H22 | Idle-life / world batch                   | ✅ shipped — [immersion.md](shipped/immersion.md) |
| H23 | Avatar shared-moment snapshot ("selfie")      | ❌ open (rig-dependent) |
| H24 | Occasion- / season-aware outfits              | ❌ open (rig-dependent) |
| H25 | Show-and-tell — share an image, she reacts    | ❌ open |
| H26 | Caught mid-something — busy when you arrive   | ❌ open |
| H27 | Co-presence mode — in the room, not talking   | ❌ open (depends on H10) |

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

## H5. Second scene / travel semantics

Today the world is exactly one room (plus the garden, which is
co-located with the room). A natural extension is a second scene
(a balcony, a coffee shop, a library) with travel semantics: Aiko
picks the scene appropriate to the conversation ("let's go grab
tea") and the prompt block flips. Would need a `scene_id` column
on `world_state`, a tool to switch scenes, and some thinking about
whether items move with her or stay in their scene. Key files:
[`app/core/world/world_store.py`](../../app/core/world/world_store.py),
[`app/llm/tools/world.py`](../../app/llm/tools/world.py),
[`web/src/features/settings/WorldTab.tsx`](../../web/src/features/settings/WorldTab.tsx).
Out of scope for v1 because a single cozy room + garden already
covers the cookie use case; **H22 (light outings)** shipped the
lightweight precursor. Pick this up if the scene switch becomes
narratively useful.

---

## H6. Audible backchannels — "mm-hm" while the user speaks

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

**Status: not yet built.** The data half (H13–H22) all shipped, so Aiko's
location / posture / activity now genuinely move in `world_state` — but the
Live2D rig still doesn't *act any of it out*; there's no `IdleLifeChannel`.

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

## H25. Show-and-tell — share an image, she reacts and remembers

**Motivation.** Vision settings exist, but the relationship is one-directional on
images: Aiko can't be *shown* things. Letting the user drop a photo into chat
("look at my new desk", "this is my dog") and having Aiko genuinely react to it —
and then **remember it** as a shared moment with the image attached — is one of
the strongest "she's actually here with me" beats available, and a huge chunk of
the path (a multimodal-capable provider via the LLM router, the chat image-bubble
type) is reachable. The reaction should route through the normal turn (so affect,
reactions, and a possible K57 episode all fire naturally off what she saw), and
the moment should land in the shared-moments timeline with a thumbnail so it can
be anniversaried and called back later. The hard parts: gating on a
vision-capable route, privacy posture (the image is the user's — store locally,
never auto-upload), and graceful degradation when the active model is text-only.
Pairs with the shared-moments / Together tab and H23 (her side of the camera).
Key files: an image-attachment path in
[`ChatView.tsx`](../../web/src/features/chat/ChatView.tsx) /
[`store.ts`](../../web/src/store.ts), a multimodal turn path through the
[`ChatClient`](../../app/llm/chat_client.py) router, a `shared_moment` write
carrying the local image ref, `agent.image_share_enabled` + a vision-capability
gate.

---

## H26. Caught mid-something — she was busy when you opened the app

**Motivation.** The away-activity machinery (H22 light outings, the away
journal, the garden-visit and hobby workers) answers "what did she do while you
were gone" as a *report* — a completed activity, narrated in past tense on the
first turn back. What it never does is catch her **in the middle of one**. The
difference is small in implementation and large in effect: "I re-potted the
basil this afternoon" is a status line, whereas "oh — hang on, let me put this
down" is a person with her own present tense, briefly interrupted by you
arriving. The second one implies a life that was running before you got here,
which is exactly what the whole world-simulation layer exists to establish and
currently undersells by only ever speaking in the perfect tense. The pieces are
all present: the world store knows her location and state, the activity workers
already model duration, and the reconnection / session-clock blocks already fire
on return — this is a framing change plus a notion of *in-progress* rather than
*completed* activity.

**Correction (12 Aug, read against the code).** "The activity workers already
model duration" is not true, and it is the part that decides the size of this
entry. `world_state` carries `location_id` / `posture` / `activity` /
`updated_at` and nothing else — there is no `started_at`, no expected end, no
way to ask "is she in the middle of this". K91's `beat_episode.py` chains beats
into an episode but the chain has no wall-clock span either; it is a planner
over beat *keys*. So the in-progress notion has to be **built**, not read: the
smallest honest version is a `started_at` + expected-duration pair written when
a beat begins, which is also exactly what the resume hook needs later. That
makes this a small feature rather than the pure re-framing the paragraph above
implies — still cheap, but not free. Best beats come from the interruption being slightly
inconvenient (hands full, something on the stove, halfway through a chapter) and
from her coming *back* to it later in the session or the next one, which is what
turns it from a greeting flourish into continuity. The risk is it becoming a
verbal tic on every single session start — it should be occasional, and it must
never delay her actually attending to him, since a companion who makes you wait
while she tidies up is worse than one who was simply pleased to see you. Pairs
with H10 (autonomous idle-life on the avatar), which would let the interruption
be *visible* rather than narrated. Key files: an in-progress activity state in
[`app/core/world/`](../../app/core/world/) alongside the existing away-activity
journal, the away-activity worker in
[`app/core/world/idle_activity_worker.py`](../../app/core/world/idle_activity_worker.py),
the reconnection cue path in
[`inner_life_part2.py`](../../app/core/session/inner_life_part2.py), plus a
resume hook so an interrupted activity can be picked up later.

---

## H27. Co-presence mode — in the room, not in conversation

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
- **Barge-in enabled by default for Live mode.** _Open._ Currently
  `audio.barge_in_enabled: false` in [`config/default.json`](../../config/default.json).
  The plumbing is there in [`app/core/session/live_session.py`](../../app/core/session/live_session.py);
  flip the flag and validate against the existing
  `barge_in_min_speech_seconds` floor. The prerequisite is **done**: P25
  shipped the client-side audio flush
  ([`shipped/perf.md`](shipped/perf.md#p25-client-audio-is-flushed-when-speech-is-cut-off)),
  so an interrupt is now actually silent instead of talking over the user
  for up to a few seconds of already-scheduled audio. That was
  deliberately kept out of the P25 change — flipping the default is an
  immersion decision, and it belongs here.
