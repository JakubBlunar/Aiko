# Immersion polish

Small additions that compound. The bottom "Other ideas considered"
block from the legacy backlog file has been folded into this section
— the second-scene idea graduates to H5, and the rest become a
"Minor polish" subsection at the bottom.

---

## H1. Conversation-arc surfacing via tag

Shipped — see [`shipped.md`](shipped.md) "H1 + K4. Conversation-arc
self-tag + dialogue-act tagging (schema v13)".

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

## H3. Mood drift narrator

Read-only periodic check on `affect_state` history and
`relationship_axes`. If Jacob's mood has been low for 3+ sessions or
Aiko's axes have drifted notably in a single direction (e.g.
`closeness` has been climbing for two weeks), surface a small
reflective note for Aiko to acknowledge gently — never mechanically
("you seem to be in a better place lately, I've noticed"). Key
files: [`app/core/affect/affect_state.py`](../../app/core/affect/affect_state.py),
[`app/core/relationship/relationship_axes.py`](../../app/core/relationship/relationship_axes.py),
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
inner-life providers.

---

## H4. Document-recall recency boost

Documents Jacob uploaded in the last 7 days get a `+0.05` retrieval
score in [`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py)
so newly-added knowledge surfaces preferentially without crowding
out long-term anchors. Cheap to ship; gives uploaded docs a chance
to feel "current" before fading into the long-term pool.

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
[`app/llm/tools/world_tools.py`](../../app/llm/tools/world_tools.py),
[`web/src/components/WorldTab.tsx`](../../web/src/components/WorldTab.tsx).
Out of scope for v1 because a single cozy room + garden already
covers the cookie use case; pick this up if the scene switch becomes
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

## Minor polish

These were in the bottom "Other ideas considered" of the legacy
backlog. None of them are urgent; folded here so they don't get
forgotten.

- **Second TTS provider behind `TtsEngine`.** Pocket-TTS is the only
  implemented backend. Adding e.g. Piper, Coqui, or an OpenAI-compatible
  cloud voice would let users pick a different timbre / language without
  swapping the whole pipeline. The `TtsEngine` protocol in
  [`app/tts/base.py`](../../app/tts/base.py) is the extension point.
- **SSML prosody for emotional speech.** _Shipped_ — see the
  "Aiko expressive speech (Pocket-TTS prosody overlay)" entry in
  [`shipped.md`](shipped.md). Pocket-TTS still doesn't accept SSML
  natively, so the rollout instead wired the dormant knobs
  (`tts_length_scale`, ambient volume gain, runtime temperature),
  added real timed pauses, introduced a per-sentence
  `[[prosody:whisper|soft|slow|fast|firm]]` markup family, expanded
  the earcon palette (chuckle / soft_sigh / sharp_gasp / breath /
  mm) with auto-sprinkle on sad openers, and widened the speed
  clamp to ±12% with per-reaction sub-caps. All CPU, no new model.
- **Barge-in enabled by default for Live mode.** Currently
  `audio.barge_in_enabled: false` in [`config/default.json`](../../config/default.json).
  The plumbing is there in [`app/core/session/live_session.py`](../../app/core/session/live_session.py);
  flip the flag and validate against the existing
  `barge_in_min_speech_seconds` floor. **Do P25 first** (client-side
  audio flush, see [`perf.md`](perf.md#p25-client-keeps-playing-scheduled-audio-after-server-side-tts-stop)) —
  server-side barge-in without the client flush still talks over
  the user for up to a few seconds of already-scheduled audio.
