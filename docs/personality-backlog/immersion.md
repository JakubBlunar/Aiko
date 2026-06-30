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
[`web/src/components/Live2DAvatar.tsx`](../../web/src/components/Live2DAvatar.tsx);
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
[`PersonaActionBanner.tsx`](../../web/src/components/PersonaActionBanner.tsx).

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
  `barge_in_min_speech_seconds` floor. **Do P25 first** (client-side
  audio flush, see [`perf.md`](perf.md#p25-client-keeps-playing-scheduled-audio-after-server-side-tts-stop)) —
  server-side barge-in without the client flush still talks over
  the user for up to a few seconds of already-scheduled audio.
