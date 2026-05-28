# Immersion polish

Small additions that compound. The bottom "Other ideas considered"
block from the legacy backlog file has been folded into this section
— the second-scene idea graduates to H5, and the rest become a
"Minor polish" subsection at the bottom.

---

## H1. Conversation-arc surfacing via tag

[`app/core/conversation_arc.py`](../../app/core/conversation_arc.py)
already infers arcs internally; expose them as `[[arc:vulnerable]]` /
`[[arc:silly]]` / `[[arc:focused]]` self-tags Aiko can emit, stored on
the relevant `messages` row. Useful for the Together-tab timeline
(filter by arc), retrieval scoring (arc-matched memories score
slightly higher when the current arc matches), and post-hoc analysis
of what kind of conversations were most common. Key files: persona
file, [`app/core/conversation_arc.py`](../../app/core/conversation_arc.py),
[`app/core/rag_retriever.py`](../../app/core/rag_retriever.py).

---

## H2. Calendar / time context block

A small inner-life provider that summarises "what's true right now"
— time of day (morning / afternoon / evening / late), day of week,
season, holiday proximity (Christmas in 4 days, Jacob's birthday
next week). Lets Aiko say "Sunday morning vibes" naturally without
calling `get_time` every turn. Pairs nicely with the shipped
schedule learner (G2) — once she knows Jacob's usual hours, she can
comment when he's online unusually early or late. Key files: new
helper in [`app/core/session_controller.py`](../../app/core/session_controller.py)
`_render_time_context_block`, wired into
[`app/core/prompt_assembler.py`](../../app/core/prompt_assembler.py)
right after `world_block` and dropped in `aggressive` mode.

---

## H3. Mood drift narrator

Read-only periodic check on `affect_state` history and
`relationship_axes`. If Jacob's mood has been low for 3+ sessions or
Aiko's axes have drifted notably in a single direction (e.g.
`closeness` has been climbing for two weeks), surface a small
reflective note for Aiko to acknowledge gently — never mechanically
("you seem to be in a better place lately, I've noticed"). Key
files: [`app/core/affect_state.py`](../../app/core/affect_state.py),
[`app/core/relationship_axes.py`](../../app/core/relationship_axes.py),
[`app/core/session_controller.py`](../../app/core/session_controller.py)
inner-life providers.

---

## H4. Document-recall recency boost

Documents Jacob uploaded in the last 7 days get a `+0.05` retrieval
score in [`app/core/rag_retriever.py`](../../app/core/rag_retriever.py)
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
[`app/core/world_store.py`](../../app/core/world_store.py),
[`app/llm/tools/world_tools.py`](../../app/llm/tools/world_tools.py),
[`web/src/components/WorldTab.tsx`](../../web/src/components/WorldTab.tsx).
Out of scope for v1 because a single cozy room + garden already
covers the cookie use case; pick this up if the scene switch becomes
narratively useful.

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
- **SSML prosody for emotional speech.** Pocket-TTS supports speed +
  pitch but not full SSML; the prosody dispatcher in
  [`app/core/cadence.py`](../../app/core/cadence.py) does what it can
  with per-sentence reaction overrides. A real SSML pass — emphasis on
  key words, micro-pauses tied to commas, pitch contour for excitement
  — would be a much bigger expression bump than a new voice file.
- **Barge-in enabled by default for Live mode.** Currently
  `audio.barge_in_enabled: false` in [`config/default.json`](../../config/default.json).
  The plumbing is there in [`app/core/live_session.py`](../../app/core/live_session.py);
  flip the flag and validate against the existing
  `barge_in_min_speech_seconds` floor.
