# Performance + observability

Companion polish often loses to a hot path that's too slow or too
opaque to debug. The P-series collects performance and observability
gaps that aren't features in their own right but pay back across the
whole personality stack: every K-series entry rides on the same
turn-build, RAG retrieval, and idle-worker plumbing, so making those
faster or more measurable compounds.

These items are intentionally narrow — most are a single afternoon
once you sit down with them. Pair any K-series entry with the
relevant P-item if it's near the same code; otherwise pick whichever
unblocks the testing flow you're stuck on.

(P1 per-turn embed budget + timing, P2 prompt-build phase
telemetry, P3 cheap slice-cache validation, P4 RAG memory-hit
batch lookup, P5 + P23 Lance scan push-down for
`list_recent_user_vectors`, P6 MessageIndexer queue/stats
visibility (`get_message_indexer_stats`), P8 idle-worker queue
visibility + multi-worker drain, P9 streaming tokens off the
`messages` array, P10 `(role, created_at)`
schedule-learner index, P12 bulk memory-mirror on startup, P13
route-driven worker model + context (with the declarative
`_worker_runtime_updaters` cascade + worker-LLM priority gate),
P14 heuristic tool-pass gate, P17 K22 callback-detector filtered
mirror walk, P18 streaming-accumulator O(n²) fix, P19 RAG
reader-writer lock + parallel per-source searches, P20 deferred
(async) context compaction, P21 K29 borderline gate moved off the
hot path, P22 inner-life shared recent-history memo, P25 client
audio flush on abort, P27 lazy STT load + `stt.enabled`, P28 TTS
gated on `tts.enabled` + `release_model`, P29
`get_memory_breakdown`, P31a `get_prompt_block_costs`, P40 engine-swap
release, and P41 the two missing `messages` indexes have
shipped — see [`shipped.md`](shipped.md).
P15 was validated as **invalid** — the embedder LRU already
collapses repeated `user_text` embeds, so the hot path is already
at the 2-embed steady state it targeted; see its shipped.md note.)

**Audit note (first pass).** The open entries were re-read against the
code rather than trusted. Corrections are inline and flagged in place;
the headline ones were P9 (shipped, entry never retired), P26 (the
client-side analyser path already exists for mobile; the pacer is 20 Hz
not 30), P32(b) (the L3 graph walk is already batched), P30 (the mirror
ceiling is ~16k rows, not 5k, and `search` is a Python loop rather than
a matmul), and P27/P28 (line references had drifted to the pre-split
`session_controller.py`). P33-P40 came out of the same pass.

**Audit note (second pass).** The cheap wins from that list then
shipped, which produced three more corrections worth recording, because
they were all the same *kind* of error — asserting behaviour from the
shape of the code rather than from the code:

- **P40** claimed neither engine swap released the old service. The STT
  half had been fixed for a while.
- **P28** looked fixed at a glance because playback *was* gated
  correctly; the load thread started before any of those checks could
  matter.
- **`messages_in_range`**'s own docstring claimed an index covered it
  that could not (wrong leading column) — now P41.

Two of the "perf" findings also turned out to be plain behaviour bugs:
catchphrase blocks silently never surfacing (P33), and a late engine
callback emitting a second, un-aborted end-of-speech event (P25).
Measurement first is the lesson: P29 and P31a exist so the *remaining*
items stop being argued from static reading.

---

## P7. Typed-mode prefetch parity with voice

**Motivation.** RAG prefetch + static-slice prebuild fire from
`feed_stt_partial` / live capture only. Typed users compose for
seconds with zero prewarm — every typed turn pays full embed +
3× Lance search + ~15 inner-life providers cold. The voice win
documented in `rag_prefetcher.py` is achievable for typed turns
just by hooking the composer.

**Key files.**
[`app/core/rag/rag_prefetcher.py`](../../app/core/rag/rag_prefetcher.py),
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
(new `feed_typed_draft` entry point),
[`web/src/features/chat/ChatView.tsx`](../../web/src/features/chat/ChatView.tsx)
(debounced WS frame on draft length crossing a threshold).

**Sketched approach.** New WS command `composer_draft` with
`{text, length}`. Frontend debounces (~250 ms) and only sends
once `length > prefetch_min_chars`. Backend reuses the existing
`RagPrefetcher` with a `source="typed_draft"` tag so the cache
key doesn't collide with voice. Cancellable on send / clear /
component unmount.

**Open questions.** Privacy posture — typed drafts are even more
sensitive than partial STT (the user is mid-thought). Default
should be ON-but-bounded, with a settings opt-out and
draft-length cap (~120 chars) so we never prefetch a long-form
diary entry.

**Effort.** Medium.

---

## P9. Frontend streaming append: O(n) per token

**Status: SHIPPED** — moved to
[`shipped/perf.md`](shipped/perf.md#p9-streaming-tokens-no-longer-clone-the-message-array).
Option (a) from the sketch: mid-stream text lives in a separate
`streamingDraft` and commits into `messages` once at stream end, so the
array is identity-stable for the whole turn. The residual per-token cost
that survived the rework — `ChatView` re-rendering to re-pin scroll — is
tracked as [P37](#p37-residual-per-token-and-per-mic-frame-react-re-renders).

---

## P11. Reclaim background-worker `num_predict` from reasoning leakage

**Motivation.** Reasoning-tuned models (qwen3.x family especially,
including the `jaahas/qwen3.5-uncensored:9b` build we run today)
ignore `think=False` and still emit `<think>...</think>` tokens
that count fully against `num_predict`. We strip those blocks
post-hoc in `OllamaClient`, and the truncation warning is now
downgraded to DEBUG when the visible answer reaches a natural
stop, so the noise is gone — but the *budget* is still being
spent on a trace that the operator never sees. A relationship pulse
with `max_tokens=320` may only have ~200 tokens of actual prose;
the rest is reasoning we throw away. That eats wall-time on every
worker run and forces us to over-provision the cap to avoid real
truncation.

**Key files.**
[`app/core/relationship/relationship_pulse.py`](../../app/core/relationship/relationship_pulse.py)
(`_build_pulse_prompt`),
[`app/core/proactive/curiosity_worker.py`](../../app/core/proactive/curiosity_worker.py),
[`app/core/memory/promise_extractor.py`](../../app/core/memory/promise_extractor.py),
[`app/core/proactive/dream_worker.py`](../../app/core/proactive/dream_worker.py),
[`app/core/conversation/conversation_arc.py`](../../app/core/conversation/conversation_arc.py),
[`app/core/goals/agenda.py`](../../app/core/goals/agenda.py),
[`app/core/memory/memory_consolidator.py`](../../app/core/memory/memory_consolidator.py),
[`app/core/proactive/reflection_worker.py`](../../app/core/proactive/reflection_worker.py),
[`app/core/infra/user_profile.py`](../../app/core/infra/user_profile.py),
[`app/core/relationship/shared_moment_extractor.py`](../../app/core/relationship/shared_moment_extractor.py),
[`app/core/proactive/prepared_nudge.py`](../../app/core/proactive/prepared_nudge.py),
[`app/llm/ollama_client.py`](../../app/llm/ollama_client.py)
(maybe a centralized `no_think_hint` helper applied to the user
message of any background-worker call).

**Sketched approach.** Append `/no_think` to the user-content
side of every background-worker prompt (qwen3 honours it as a
soft directive in some fine-tunes; it's a no-op on
non-reasoning models). Compare before/after: the
`completion_tokens` field on the MCP `get_last_response_detail`
should drop noticeably for surfaces tagged
`relationship_pulse`, `reflection_worker`, etc. If qwen3.5
uncensored ignores it, fall back to (a) wrapping prompts with
`<no_think>...</no_think>` tags some templates support, or (b)
running background workers on a non-reasoning Ollama model
(e.g. a small 3B instruct) by pointing
`llm.routes.worker_default` at it; the main turn still uses the
reasoning model on `llm.routes.main_chat`. No new setting is
needed — the route table already separates the two.

**Open questions.** Does `/no_think` actually save tokens on
`jaahas/qwen3.5-uncensored:9b` specifically, or does this fine-
tune ignore both? If it ignores, the cleaner path is the dual-
model split (a second worker route), which is more code but
also unlocks faster background-worker turnaround independently.
Note: P13 (shipped) makes the dual-model split actually work —
set `llm.routes.worker_default.model` and every worker picks it
up via the declarative cascade, no restart. Before P13 the only
way to change the worker model was editing `ollama.chat_model`
in `user.json` directly.

**Effort.** Small (just the prompt suffix + before/after token
measurements). Medium if we need the dual-model split.

---

## P16. Post-turn inner-life blocks the brain loop

**Status: now measurable.** The entry's own "audit before splitting"
caution stood for a while because there was *no instrumentation at all*
on the cascade — nobody knew whether it cost 5 ms or 500. It is now
stamped as `post_turn_ms` on the metrics record (and on the WS
`Metrics` type), with a log line that escalates from DEBUG to INFO past
`_POST_TURN_SLOW_MS`. Note that `post_turn_ms` is deliberately *not*
folded into `total_ms`: the cascade runs after the reply has finished
streaming, so adding it would inflate the number the latency badge
shows. Also worth knowing when reading the figure: `embedder.end_turn()`
fires *before* the cascade runs, so post-turn's 1-4 embeds land in the
next turn's embed budget rather than this one's. **Collect real numbers
before attempting the split below** — if the cascade is single-digit
milliseconds on a normal turn, the Large refactor buys nothing.

**Motivation.** `chat_once_streaming` doesn't return until
`_post_turn_inner_life` finishes — detector cascade, embed burst
(see P15), K22 callback scan (see P17), SQLite writes. The brain
loop is a single consumer, so a user who fires a quick follow-up
message waits for all of the *previous* turn's bookkeeping before
their message even starts assembling. Streaming + TTS may already
be done from the user's perspective; the system is busy doing
homework.

**Key files.**
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
(`chat_once_streaming`, post-turn call ~6120),
[`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
(`_post_turn_inner_life`),
[`app/core/brain/loop.py`](../../app/core/brain/loop.py).

**Sketched approach.** Split post-turn into a *fast lane*
(anything that arms one-shot slots the NEXT prompt reads —
clarification, rupture, self-correction, belief gaps) and a *slow
lane* (embeds, callback scan, calibration, axes drift). Fast lane
stays inline; slow lane moves to a background job with a
turn-ordering guarantee (drop the job if a newer turn already
superseded it). Alternatively run the whole post-turn as a brain
event at lower priority than user messages.

**Open questions.** Which one-shot slots are actually read by the
next prompt vs. merely eventually-consistent? Audit before
splitting — a wrong call here makes cues silently miss a turn.

**Effort.** Large.

---

## P24. Voice latency batch: reaction-tag TTS gate, double STT pass, first-chunk threshold

**Motivation.** Three independent, individually-small voice-path
delays that compound into "she takes a beat too long to start
talking":

1. **Reaction-tag gate** — the stream loop only dispatches TTS
   chunks once `mood is not None`
   ([`turn_runner.py`](../../app/core/session/turn_runner.py)
   ~797-804). If the model leads with prose before
   `[[reaction:...]]`, *all* speech waits; the fallback
   (`mood = "neutral"`, ~860-871) only fires at stream end,
   flushing everything at once.
2. **Double STT pass** — `process_live_capture` re-transcribes
   the full WAV via `transcribe()` even when partial endpointing
   already produced a stable final text during capture. The
   stable partial *is* read back — but only to fire one last RAG
   prefetch (`feed_stt_partial(final=True)`), after which
   `transcribe(wav_path)` runs unconditionally
   ([`voice_capture_mixin.py`](../../app/core/session/voice_capture_mixin.py)
   ~366-396 — the entry previously pointed at `session_controller.py`,
   before the voice split;
   [`realtime_stt_service.py`](../../app/stt/realtime_stt_service.py)
   `transcribe`). 100–500 ms of pure re-work between "user
   stopped talking" and LLM start.
3. **First-chunk threshold** — `drain_tts_stream_chunks` holds a
   sentence until ≥24 chars **or** ≥4 spaces **or** a newline
   ([`session_text_utils.py`](../../app/core/session/session_text_utils.py)
   ~246), so short openers ("Sure.", "Okay!") wait for more
   tokens before any audio.

**Sketched approach.** (1) Start TTS with a provisional
`neutral` mood immediately and upgrade when the tag arrives
(reaction-to-speed already tolerates a mid-stream change, the
expression channel just lands a few hundred ms later); (2) trust
the partial-endpointing final when its text is stable across the
last two partials, keep the WAV re-pass as a fallback for
low-confidence captures; (3) voice-specific first-chunk floor
(~8 chars or first clause boundary) — sentence two onward keeps
the current threshold.

**Effort.** Small each; ship as one voice-latency pass.

---

## P26. Lip-sync rides the server clock, not the playback clock

**Status: PARTLY SHIPPED (mobile only).** The sketched fix exists
in the codebase and is wired for **mobile audio-owners**; desktop
and the Tauri main window still ride the server clock. What's left
is extending the existing path, not building it.

**Motivation.** On the server-clock path, mouth animation is
driven by server-paced amplitude JSON (`_amplitude_pacer`, 50 ms
hop = **20 Hz**, not the 30 Hz this entry originally claimed) + a
network hop + the client's first-clip idle margin
(`FIRST_CLIP_IDLE_MARGIN_SEC`, 0.1 s) + per-frame smoothing
(`SMOOTH_FACTOR = 0.35`, ≈150 ms time constant at 60 Hz) — so the
mouth runs a noticeable, variable beat behind the audio the user
actually hears, and main-thread jank desyncs it further.

**What already exists.** `AudioOutputManager` builds
`AnalyserNode → destination` and RAF-samples RMS at ~60 Hz,
exposed via `setLipsyncListener`. The socket hook only routes it
into the store when `isMobileViewport() && playsAudioHere`, and
correspondingly ignores the `audio_amplitude` WS frame in that
case. So both paths are live and mutually exclusive — per client,
not per install.

**Key files.**
[`app/tts/pocket_tts_service.py`](../../app/tts/pocket_tts_service.py)
(`_amplitude_pacer` ~967-974),
[`web/src/audio/AudioOutputManager.ts`](../../web/src/audio/AudioOutputManager.ts)
(analyser ~108-117, RAF sampler ~428-492),
[`web/src/hooks/useAssistantSocket.ts`](../../web/src/hooks/useAssistantSocket.ts)
(the mobile-only routing gate ~76-88 and the mirrored
`audio_amplitude` skip ~615-627),
[`web/src/live2d/channels/LipsyncChannel.ts`](../../web/src/live2d/channels/LipsyncChannel.ts).

**Remaining approach.** Drop the `isMobileViewport()` condition so
any audio-owning client derives amplitude from its own playback
tap, and reduce the server pacer to voice-strip-meter duty for
non-owning windows. Then re-tune `SMOOTH_FACTOR` — 150 ms of
smoothing was compensating for server-clock jitter that the
analyser path doesn't have, so the same constant now *adds* lag it
no longer needs to hide.

**Open questions.** What do non-owning windows (persona window,
second browser tab) show? They have no local audio to analyse, so
they still need the WS frames — meaning the pacer stays, and the
choice is per-client rather than a protocol removal.

**Effort.** Small (extend the gate) + Small (re-tune smoothing),
down from Medium now that the analyser path exists.

---

## P30. Raise / disable the `memory.max_memories` cap

**Motivation.** `memory.max_memories` defaults to 5000 and
`MemoryStore.prune()` enforces it (plus per-tier
`scratchpad` / `archive` caps). The cap exists mostly because the
old topic graph recomputed an `O(n²)` cosine clustering in-process
on every read — a hard scaling wall. That wall is now gone: the
topic graph is persisted + incrementally maintained and its batch
refit routes through LanceDB ANN (`O(n·k)`), so clustering no
longer caps the corpus. With web-search knowledge enrichment
landing distilled `kind="knowledge"` rows, 5000 will fill *fast*,
and the user wants to let the corpus grow much larger (ideally
uncapped) and lean on **topic-relevant RAG** rather than a small
flat pool. This entry is the "actually let it grow" follow-up to
the topic-graph persistence work.

**What's already safe at scale.**
- Topic graph: persisted (`topic_clusters` / `memory_topic_assignments`),
  incremental add/delete, ANN batch refit. See
  [K9](shipped/patterns-k01-k15.md#k9-topic-graph-browser--observability-surface).
- RAG retrieval: LanceDB ANN (`search_memories`) is sub-linear
  *once an index exists* (`RagStore.ensure_vector_index` builds one
  above 256 rows) — but see item 5 below: the index is built once
  and never refreshed, which is now tracked as its own item (P35).

**What still assumes a small corpus (the actual work).**
1. **In-memory mirror.** `MemoryStore._mirror` holds every row +
   its embedding in process. Note the real ceiling is **~16,000
   rows, not 5,000**: `max_memories` is enforced *per tier* along
   with `scratchpad_cap` (1000) and `archive_cap` (10000), so the
   three caps sum. At ~4 KB per float32 embedding that's ~64 MB of
   vectors alone before content and metadata; at 100k+ it's
   hundreds of MB, and `_reload_mirror` / `decay` / `prune` walk it
   linearly. Either accept the larger RSS (still cheap vs the
   STT/TTS weights in P27/P28) or move the cold tail (archive tier)
   out of the mirror and read it from LanceDB on demand.
2. **O(n) mirror sweeps.** `decay()`, `prune()`, the K22 callback
   detector (P17), and the K6/K28 warm scans (P5/P23) all walk the
   full mirror. These need the P5/P17 fixes first, or they become
   the new wall the moment the cap lifts. Note `decay()` is worse
   than a walk: it does the bulk `UPDATE` in SQL and then calls
   `_reload_mirror()`, re-reading **every** row and BLOB from disk
   rather than updating the mirror in place.
3. **`search()` brute-force fallback.** `MemoryStore.search` is a
   per-row Python cosine loop over the whole mirror — *not* a NumPy
   matmul as this entry claimed (contrast `ConceptStore.nearest`,
   which does stack a matrix), so it's slower than the original
   estimate, not faster. The prompt hot path does **not** use it:
   retrieval goes through `RagRetriever` → `RagStore`. It survives
   in three secondary callers (`memory_retriever.py`,
   `concept_contradiction.py`, `idle_gap_resolver.py`), and
   `MemoryRetriever` itself is constructed and handed to
   `PromptAssembler` but never actually invoked — dead weight worth
   deleting or wiring deliberately.
4. **prune() semantics.** With the cap raised/disabled, decide what
   (if anything) still bounds growth: keep a generous hard ceiling
   as a safety valve, rely on decay + archive-tier demotion to keep
   the *hot* set small, or both. Pinned rows must stay immune
   either way.
5. **The ANN index is build-once.** Tracked separately as
   [P35](#p35-the-lance-ann-index-is-built-once-never-refreshed) —
   it is a hard prerequisite for this entry, because raising the cap
   without index maintenance moves retrieval onto the flat-scan
   fallback exactly when the corpus gets big.

**Key files.**
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py)
(`_max` / `_tier_caps`, `prune`, `decay`, `_reload_mirror`,
`search`),
[`app/core/infra/memory_settings.py`](../../app/core/infra/memory_settings.py)
(`max_memories` default / a new `max_memories: 0 = uncapped`
sentinel),
[`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py)
(call `ensure_vector_index` on a schedule / after bulk knowledge
ingest so the ANN index actually exists at scale),
[`config/default.json`](../../config/default.json),
[`docs/configuration.md`](../../docs/configuration.md).

**Sketched approach.** Phase it: (a) bump the default cap (e.g.
5000 → 20000) and add a `0 = uncapped` sentinel, cheap and
reversible; (b) land P5 + P17 (and confirm P4) so the per-turn /
post-turn mirror sweeps stay sub-linear; (c) ensure
`ensure_vector_index` is invoked from a maintenance worker (e.g.
the topic-graph rebuild worker, or the idle scheduler) so the ANN
index is rebuilt as the corpus grows rather than only opportunistically;
(d) optionally evict the `archive` tier from the in-memory mirror,
reading it lazily from LanceDB only when retrieval needs it, so
RSS tracks the *hot* set, not the whole history.

**Open questions.** Is the in-memory mirror worth keeping at all
once retrieval is fully ANN-backed, or should the mirror become a
bounded LRU of the hot set? That's the deeper architectural fork
behind "RAG should focus on relevant topics instead of fetching
memories directly" — overlaps with the F10 topic-graph utilisation
cluster ([`awareness.md`](awareness.md)).

**Effort.** Small (a, cap bump + sentinel) → Medium (b/c, depends
on P5/P17) → Large (d, mirror eviction / LRU rework).

---

## P31. Audit + trim the baseline system prompt (~25-30k resting floor)

**Status: (a) shipped, so this is no longer a guess.** Sketch item (a)
— the measurement — landed as the `get_prompt_block_costs` MCP tool and
`PromptTelemetry.block_chars`; see
[`shipped/perf.md`](shipped/perf.md#p31a-get_prompt_block_costs--per-block-prompt-cost-weighted-by-tier).
It ranks blocks by tokens × the tier's cache-miss probability, and
reports empty blocks as `0` rather than omitting them, so (c)'s
content-gating candidates surface directly. The first thing it says is
that the persona (~78k chars, ~19.5k estimated tokens) dominates
everything else combined — so (b), the persona trim, is where the value
is, and it wants its own pass because the persona is user-editable
content rather than code.

**Motivation.** On a *fresh* session with a single message the
system prompt already measures ~25-30k tokens — that's the resting
floor every turn pays before a word of history or RAG lands, and
it's the dominant term in context occupancy now that the tool-pass
double-count is fixed (see below). The floor is legitimate — it's
the persona plus the full inner-life stack (K-series detectors,
affect / mood shell, world, relationship, day-colour, circadian,
grounding line, arc/agenda/goals, prosody + reaction grammar,
etc.) — but it has never been *audited* block-by-block for
value-per-token. Some blocks render every turn but only matter
occasionally (K27 day-colour, K3 routines, anniversary windows);
some persona sections may have grown redundant with the tag-
grammar addenda; and OpenAI prompt caching only discounts the
*stable prefix*, so a bloated-but-stable T0-T1 is cheap-per-turn
while a bloated *volatile* T5-T6 pays full price every turn. A
principled trim (or lazy-render) of the heaviest volatile blocks
is likely the single biggest lever on both per-turn cost and
effective context headroom.

**Context (why now).** The recent double-count fix means the
occupancy readout is finally *truthful*: `context_prompt_tokens`
(largest single call) drives the widget + compaction trigger, not
the summed tool+stream `prompt_tokens`. So the ~25-30k is now
visible as the real floor rather than being masked by the ~2x
tool-turn inflation — making this the obvious next lever. The
per-block telemetry from P2 (`PromptTelemetry` already carries
`affect_tokens` / `circadian_tokens` / `profile_tokens` / … per
inner-life block, plus `provider_ms` / `slowest_provider`) means
the measurement surface for the audit already exists.

**Key files.**
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(`assemble_with_budget`, the `_PROMPT_BLOCK_TIERS` T0→T6 ladder,
the `if block: system_parts.append(block)` cascade),
[`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)
+ the `inner_life_part*.py` / `post_turn_mixin.py` providers (the
per-block renderers),
[`app/core/session/prompt_support.py`](../../app/core/session/prompt_support.py)
(`PromptTelemetry` per-block token fields — the audit's ruler),
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
(the largest single T0 block; candidate for redundancy trimming
against the `_SPEECH_GRAMMAR_ADDENDUM` in `prompt_assembler.py`).

**Sketched approach.** (a) **Measure** — **done**, see the status note
above: `get_prompt_block_costs` ranks every block by tokens × tier, so
the heaviest *volatile* (T5-T6) blocks — the ones paying full price past
the cache prefix — are visible in one MCP call. (b) **Persona trim** —
diff the persona's tag/grammar guidance against
`_SPEECH_GRAMMAR_ADDENDUM` and collapse duplication (a persona-
trim pass already shipped once; this is the follow-up now that
more K-blocks exist). (c) **Lazy-render the occasional blocks** —
several blocks render every turn but only carry signal rarely
(day-colour tagline, routines, anniversary, upcoming-horizon when
empty); gate them to render only when they actually have content,
so an empty block costs 0 tokens instead of a boilerplate header.
(d) **Tier hygiene** — confirm no volatile block accidentally
sits in the stable prefix inflating cache-miss cost (cross-check
against `_PROMPT_BLOCK_TIERS`).

**Open questions.** What's the floor we're willing to accept —
is ~15k a realistic target without losing personality fidelity,
or does the K-series richness genuinely need ~25k? Which blocks
are safe to make *conditional* vs. which provide value every turn
even when terse (affect, relationship, persona core)? Does moving
occasional blocks behind a content-gate risk them silently never
firing (the same audit-before-splitting caution as P16)?

**Effort.** ~~Small (a, measurement tool)~~ → Medium (b/c, per-block
trim + lazy-render, one block family at a time) → the persona
diff is a focused afternoon.

---

## P32. Concept layer — worker budget + unbounded graph growth

**Motivation.** The concept layer (see
[`concepts.md`](concepts.md), L-series) adds standing cost the P-series
should size up front rather than discover in production. Three lines:
(a) the **synthesis proposer** (L2) is another worker-LLM job competing
for idle windows on the `IdleWorkerScheduler`; (b) the **lifecycle
engine** (L3, incl. the L15/L16/L17 passes) walks the `concept_edges`
graph every cycle — O(edges) work that grows as concepts accrue; (c)
**snapshots** (L17/L19) and edges grow **without bound** by design (the
autobiography is meant to be permanent), so storage + scan cost climb
forever if left unmanaged.

**Status per line (re-audited).**
- (a) **Still true.** `ConceptSynthesisWorker` registers on the scheduler
  at a 1800 s interval and issues `chat_stream` calls with up to 4096
  tokens. Its `is_ready` is interval-only; the dirty check lives *inside*
  `run()` (kv signatures short-circuit the LLM when nothing moved), so
  the worker still occupies an idle slot to decide it has nothing to do.
  The broader contention picture is now [P36](#p36-idle-worker-llm-pile-up-under-a-6-s-soft-budget).
- (b) **No longer true.** L3 does *not* walk the graph. Each tick it
  takes `list_stalest(batch_size)` (default 100) over the in-memory
  concept set and does bounded per-concept SQL (`evidence_of` /
  `edges_into` / `dependents_of`), so the cost is
  O(batch × edges-per-concept) and full coverage happens over
  `ceil(N/100)` ticks. `concept_edges` is already indexed on both
  endpoints. What the sketch asked for is effectively in place — except
  that it is round-robin rather than *dirty*-triggered, so a quiet graph
  still pays a tick's worth of work forever.
- (c) **Still true, and the sharpest edge is `concept_events`.** It is
  strictly append-only — `ConceptEventStore` has `add` plus reads, no
  delete or thin path — and L3 can emit several rows per concept per tick
  (status change + `reinforced` + `confidence_sample` + `plasticity_shift`).
  `concept_edges` only shrinks on concept/memory delete, consolidation
  merge, or the integrity sweep. Note "snapshots" needs no thinning today
  for a different reason than assumed: `concept_snapshot.py` builds
  ephemeral API payloads, not a stored table. Folded into
  [P34](#p34-unbounded-tables-messages-lance-mirror-concept_events).

**Key files.**
[`idle_worker_scheduler.py`](../../app/core/proactive/idle_worker_scheduler.py)
(where the proposer + lifecycle engine register + their cadence),
[`concept_lifecycle_worker.py`](../../app/core/concepts/concept_lifecycle_worker.py)
(the batched round-robin, ~L219-220),
[`concept_event_store.py`](../../app/core/concepts/concept_event_store.py)
(append-only; no thinning API),
the L1 `ConceptStore` (`concepts` + `concept_edges` tables),
`app/mcp/server.py` (the L26 trace is also the perf-observability hook).

**Sketched approach.** (a) **Cadence + budget** — run the proposer on a
low, change-triggered cadence (weekly / on material cluster change, per
L2), never every idle tick, and count it against the same idle-window
budget as the other LLM workers (P8 queue visibility applies). Lifting
its dirty check from `run()` up into `is_ready()` would also stop it
consuming a slot to decide it has nothing to do. (b) **Bounded
graph walks** — **done** via the batched round-robin above; the
remaining refinement is making it dirty-triggered so an unchanging graph
costs nothing. (c) **Snapshot thinning** — keep recent
self-snapshots dense and **thin older ones** (monthly → quarterly →
yearly) so L19 stays traversable without linear growth; retired concepts
archive rather than delete (L19 durability) but drop out of the hot scan
set. For `concept_events` specifically the cheapest honest thinning is
"keep every status transition forever, thin the `confidence_sample` /
`reinforced` noise past N months" — those are the high-volume, low-value
rows. (d) **Hot-path guarantee** — nothing here runs on the turn stream;
the only per-turn concept cost is L23 selection over the small `active`
set, which reuses the shared user-text embed (P15).

**Open questions.** Snapshot thinning schedule? A cap on `active` concepts
(soft-merge via L20 when it's exceeded) to bound per-turn selection? Does
the lifecycle engine share an idle slot with the memory decay/promotion
workers or contend with them?

**Effort.** Small-Medium (mostly cadence + indexing + a thinning sweep;
cheap if designed in, expensive to retrofit once the graph is large).

---

## P33. Inner-life providers walk the whole memory mirror every turn

**Status: (a) shipped and the correctness bug fixed; (b)/(c) open.** The
inline "**Fixed**" markers below are current. Tests:
`tests/test_memory_tiers.py::TestKindFilteredListing`.

**Motivation.** P17 fixed the worst instance (the K22 callback
detector's `list_recent(10_000)`) but the *pattern* is still spread
across the provider set: a provider needs "the few rows of kind X"
and gets there by copying the entire mirror. Two shapes, both
per-turn:

- **Copy + double sort.** `MemoryStore.list_top()` / `list_recent()`
  used to do `list(self._mirror.values())` and then sort the whole
  thing, applying the `kind` filter *after* the copy. At the ~16k-row
  ceiling (P30) that's a 16k-element sort to return 3 rows. **Fixed:**
  both now filter by `kind` inside the lock, before the sort.
- **Post-filtering the wrong rows** — a correctness bug, not a perf one.
  Callers asked for the top N of *any* kind and filtered to their kind in
  Python, so once N higher-salience rows of other kinds existed the
  caller silently got nothing. **Fixed** in the four single-kind sites:
  `_render_catchphrase_block` (running jokes stopped surfacing at all),
  `_known_catchphrases` and `CatchphraseMiner._existing_catchphrase_phrases`
  (both K80 dedupe guards — they could re-bless a bit already recorded),
  and `_top_inner_life_contents` (the dream pass's seed rows). **Still
  open:** `relationship_pulse._collect_bullets` and
  `prepared_nudge._collect_candidates` have the same shape over a *set* of
  kinds, which wants a `list_top(kinds=…)` variant rather than a one-line
  change.
- **Full-kind walks.** `iter_by_kind` is cheap per row but unbounded
  in count: K9 curiosity seeds, K29 opinion injection
  (`iter_by_kind("self")` plus an embed), and F9 knowledge grounding
  (concatenates **every** `knowledge` + `curiosity_finding` row, then
  cosine-scans) all run on qualifying turns.

Individually each is single-digit milliseconds today. The reason to
track it is P30: these are exactly the sweeps that become the new wall
the moment the corpus cap lifts, and they're invisible in telemetry
because `provider_ms` attributes them to the *provider*, not to the
mirror.

**Key files.**
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py)
(`list_top` / `list_recent` ~L1585-1642, `iter_by_kind` ~L1644),
[`app/core/session/inner_life_part3.py`](../../app/core/session/inner_life_part3.py)
(K29 ~L112-120, curiosity seeds ~L1256-1272),
[`app/core/session/inner_life_part2.py`](../../app/core/session/inner_life_part2.py)
(F9 knowledge grounding ~L116-118).

**Sketched approach.** (a) Make the cheap fix universal: filter by
`kind` *inside* the lock before sorting, so a kind-scoped `list_top`
sorts only candidate rows. (b) For the genuinely unbounded walks, add
a kind-scoped index to the mirror (`dict[str, set[int]]` maintained on
add/delete/update) so `iter_by_kind` is O(matching) rather than
O(total). (c) For F9, stop concatenating the whole knowledge corpus —
it should go through RAG ANN like every other retrieval path.

**Effort.** Small (a) → Small-Medium (b, one index to keep coherent
across add/update/delete/reload) → Medium (c, changes retrieval
semantics).

---

## P34. Unbounded tables: `messages`, Lance mirror, `concept_events`

**Motivation.** `memories` has caps and `prune()`; `beliefs` has
`prune_to_cap`; `task_events` has a 30-day cleanup worker. Three of
the highest-volume stores have **no** global bound:

- **`messages`** — deletable only per session (`clear_messages` /
  `delete_session`). At ~2 rows per turn this is the fastest-growing
  table in the app, and several queries scan it by time (see P10 and
  the new indexes).
- **The LanceDB `messages` mirror** — `MessageIndexer` indexes every
  message and backfills all sessions on startup, so it tracks SQLite
  row-for-row, each with a vector.
- **`concept_events`** — append-only by construction (`add` plus
  reads, no delete API), and L3 emits several rows per concept per
  tick including the high-volume `confidence_sample` /
  `reinforced` pair. See P32(c).

None of this is urgent at current scale; the point is that "grows
forever" is a design decision that should be made deliberately rather
than by omission, and the retention story differs per table (chat
history is arguably *meant* to be permanent; `confidence_sample` rows
are noise after a few months).

**Key files.**
[`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py)
(`messages` schema + the per-session delete paths),
[`app/core/rag/message_indexer.py`](../../app/core/rag/message_indexer.py)
(~L283-309 startup backfill),
[`app/core/concepts/concept_event_store.py`](../../app/core/concepts/concept_event_store.py),
[`app/core/tasks/task_cleanup_worker.py`](../../app/core/tasks/task_cleanup_worker.py)
(the existing retention worker to model a sweep on).

**Sketched approach.** Decide a posture per table rather than a
global policy. For `messages`, keep everything but confirm the time-
range queries stay indexed as it grows. For the Lance mirror,
consider indexing only messages above a length/substance threshold —
"ok" and "haha" cost a vector and are never a useful retrieval hit.
For `concept_events`, a thinning sweep that keeps all status
transitions and drops sampling noise past N months, modelled on
`task_cleanup_worker`.

**Open questions.** Does anything read `confidence_sample` rows older
than the L17b window? If not, thinning is free. Does dropping short
messages from the Lance mirror hurt K-time verbatim recall (which
reads SQLite, not Lance — probably not)?

**Effort.** Small (per-table decision + one sweep worker) → Medium
(if the Lance indexing threshold needs backfill reconciliation).

---

## P35. The Lance ANN index is built once, never refreshed

**Motivation.** `RagStore.ensure_vector_index` is idempotent by
design — `create_index` runs with `replace=False`, so the second call
is a no-op once an index exists. Its **only** caller is
`topic_graph.rebuild()`, and only when the corpus is already above
the 2000-row ANN rebuild threshold. So in practice the index is built
at most once, at whatever corpus size happened to trip that path, and
never rebuilt as rows accumulate. Lance degrades gracefully (a failed
or absent index falls back to a flat scan, exceptions swallowed) —
which is the problem: retrieval gets slower with no signal that the
index went stale. This is a hard prerequisite for P30, since raising
the memory cap without index maintenance is exactly the scenario where
the flat-scan fallback hurts most.

**Key files.**
[`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py)
(`ensure_vector_index` ~L942-985 — note the swallowed exceptions and
`min_rows=256`),
[`app/core/conversation/topic_graph.py`](../../app/core/conversation/topic_graph.py)
(~L1580-1582, the sole caller; `_ANN_REBUILD_THRESHOLD` ~L147).

**Sketched approach.** (a) Call `ensure_vector_index` from a
maintenance worker on a slow cadence (or from the existing
topic-graph rebuild worker unconditionally, not only above
threshold), and pass `replace=True` when the row count has grown by
more than some factor since the last build. (b) Report index state —
present / row count at build / current row count — through the
existing RAG stats surface so "the index is stale" is observable
rather than inferred. (c) Consider building it for `messages` and
`documents` too, not just `memories`.

**Effort.** Small (a + b), Small (c).

---

## P36. Idle-worker LLM pile-up under a 6 s soft budget

**Motivation.** P8 gave the idle scheduler queue visibility and a
multi-worker drain; it did not bound how much LLM work can queue up
behind it. There are now **~55 worker registrations**, of which
**~22-28 make LLM calls** when they fire (concept synthesis and
consolidation, fact-checker, curiosity, knowledge, memory conflict
detector, consolidation, belief, promise, topic label + digest, goal,
pre-thought, thread resummary, curiosity seed, follow-up, diary,
hobby, room evolution, associative wander, knowledge-map reflection,
the L3-triggered belief reviser, …). They drain **sequentially**
(concurrency 1) against a **6 s** soft per-tick budget with
`idle_worker_max_per_tick: 0` (unlimited), and the anti-starvation
rule runs at least one due worker even when it blows the budget. A
single worker generation on a local 9B model can exceed the whole
tick budget on its own.

Each worker self-limits (hourly/daily caps, dirty flags, batch
sizes), which is why this works in practice. But there is no global
"LLM seconds per hour" ceiling, and the failure mode is invisible:
workers that are perpetually deferred just never run, and nothing
says so. Related: the default config points `main_chat` **and**
`worker_default` at the same local model, so worker generations and
the user's turn contend for the same Ollama slot. The
`LlmPriorityGate` (`worker_llm_gate_enabled` /
`worker_llm_max_concurrency`) mitigates this between workers, but a
worker generation already in flight still delays the next user turn
server-side.

**Key files.**
[`app/core/proactive/idle_worker_scheduler.py`](../../app/core/proactive/idle_worker_scheduler.py)
(~L302-367, the ranked drain + budget skip + anti-starvation rule),
[`app/core/session/idle_workers_init_mixin.py`](../../app/core/session/idle_workers_init_mixin.py)
and [`app/core/session/speaking_workers_init_mixin.py`](../../app/core/session/speaking_workers_init_mixin.py)
(the registration sites),
[`app/llm/llm_gate.py`](../../app/llm/llm_gate.py)
(the existing priority semaphore),
[`config/default.json`](../../config/default.json)
(`idle_worker_budget_ms` ~L458, `idle_worker_max_per_tick` ~L459).

**Sketched approach.** (a) Report starvation: track per-worker
"consecutive ticks deferred" and surface the worst offenders in
`get_idle_worker_stats`, so "this worker hasn't run in two days" is
visible. (b) An LLM-specific budget — at most N worker generations
per tick / per hour, independent of the wall-clock tick budget, since
one generation's cost is much lumpier than the 6 s estimate assumes.
(c) Cheapest of all: move interval-only `is_ready` checks that are
really dirty checks (P32(a)) into `is_ready`, so nothing spends a
slot to conclude it has no work.

**Open questions.** Is the right ceiling wall-time or generation
count? Should the user's turn be able to *preempt* an in-flight
worker generation (Ollama has no cancel-and-requeue, so this may mean
not starting one when a turn looks imminent — the speaking-window
scheduler already reasons about this).

**Effort.** Small (a), Small-Medium (b), Small (c).

---

## P37. Residual per-token and per-mic-frame React re-renders

**Status: (a) and (b) shipped; (c) still open.**

- **(a) Per token — fixed.** The signature moved to
  [`streamRepin.ts`](../../web/src/features/chat/streamRepin.ts) and is
  quantised to 48-char buckets, so a ~1200-char reply re-renders
  `ChatView` ~26 times instead of ~1200. The draft **id** stays
  un-bucketed: without it, a new turn starting at length 0 would collide
  with the previous turn's first bucket and every reply's first re-pin
  would be silently skipped — which `streamRepin.test.ts` pins
  explicitly. Stream end is still covered from two directions (the
  signature goes empty, and the commit into `messages` triggers
  Virtuoso's own `followOutput`).
- **(b) Per mic frame — fixed.** `ChatView` and `PersonaWindow` no
  longer subscribe to `audioLevel` at all. The three elements that
  actually react to it subscribe at the leaf: `MicPulseRing` inside
  [`MicButton`](../../web/src/features/voice/MicButton.tsx), and
  `LevelDot` / `AudioMeter` inside
  [`VoiceStrip`](../../web/src/features/chat/VoiceStrip.tsx). The meter
  additionally quantises its selector to the *number of lit bars*, so
  most level updates return an unchanged value and don't re-render even
  the leaf. This mattered most in the persona window, which renders the
  Live2D canvas — a 20 Hz re-render there was the expensive version of
  the problem.
- **(c) Still open.** The Virtuoso `itemContent` / `Footer` closures are
  still recreated per `ChatView` render, so Virtuoso loses referential
  stability on each one. Much less frequent now that (a) cut the render
  count by ~45x, but the fix (hoist into `useCallback` / module scope) is
  unchanged and independent.

**Motivation.** P9 moved streaming text off the `messages` array, so
finalised bubbles no longer re-render mid-stream. Two subscriptions in
`ChatView` still re-rendered the whole chat chrome at high frequency:

1. **Per token.** `streamingSignature` selected
   `` `${draft.id}:${draft.content.length}` ``, which changed on every
   chunk. That was deliberate (it re-pins the scroll), but it re-rendered
   a large component with ~18 store selectors and recreated the
   Virtuoso `itemContent` / `Footer` closures each time — so Virtuoso
   lost referential stability on every token even though the memoised
   bubbles below it didn't re-render.
2. **Per mic frame.** `ChatView` also subscribed to `audioLevel`,
   which the mic worklet updates every ~50 ms (~20 Hz) during capture,
   and the server's `audio_level` frames update again. The transcript
   is static during capture; the whole chrome re-rendered anyway.

Neither was fatal, but they land on the same main thread that schedules
TTS audio buffers, which is precisely the contention the audio layer's
own comments warn about.

**Key files.**
[`web/src/features/chat/ChatView.tsx`](../../web/src/features/chat/ChatView.tsx)
(selectors ~L70-92, `streamingSignature` ~L148-151, inline Virtuoso
callbacks ~L515-535),
[`web/src/hooks/useMicCapture.ts`](../../web/src/hooks/useMicCapture.ts)
(~L76-82),
[`web/public/mic-pcm-worklet.js`](../../web/public/mic-pcm-worklet.js)
(the ~50 ms RMS emit).

**Sketched approach.** (a) Coarsen the streaming signature — sample
draft length in buckets so scroll re-pinning fires every ~N chars
instead of every token. (b) Move the `audioLevel` subscription down
into whichever leaf actually renders the meter. (c) Hoist the
Virtuoso `itemContent` / `Footer` closures into `useCallback` /
module scope so identity is stable across renders.

**Effort.** Small each.

---

## P38. Live2D channels allocate a store snapshot several times per frame

**Motivation.** `Live2DAvatar.getStoreSnapshot()` builds a **new
object** on every call, and the animation channels call it
independently from their own ticker / RAF paths: lipsync (pre-model),
gaze, ambient body (tier-3 *and* pre-model), and expression — which
alone calls it three times inside one tier-3 tick. That's roughly 5-8
short-lived objects per frame, ~300-480/s at 60 Hz, purely to read
state that hasn't changed within the frame.

GC pressure of this shape is usually harmless, but it's on the same
main thread as Pixi rendering and the audio scheduling, and the
avatar is the one surface where a dropped frame is *visible*.

**Key files.**
[`web/src/features/avatar/Live2DAvatar.tsx`](../../web/src/features/avatar/Live2DAvatar.tsx)
(`getStoreSnapshot` ~L203-228),
[`web/src/live2d/channels/ExpressionChannel.ts`](../../web/src/live2d/channels/ExpressionChannel.ts)
(three calls per tick, ~L520 / ~L603 / ~L744),
[`web/src/live2d/channels/LipsyncChannel.ts`](../../web/src/live2d/channels/LipsyncChannel.ts),
[`web/src/live2d/channels/GazeChannel.ts`](../../web/src/live2d/channels/GazeChannel.ts),
[`web/src/live2d/channels/AmbientBodyChannel.ts`](../../web/src/live2d/channels/AmbientBodyChannel.ts).

**Sketched approach.** Cache one snapshot per frame in the engine
tick and pass it to the channels, instead of each channel pulling its
own. The channels already receive a `deps` object, so this is a
signature change plus deleting the internal calls.

**Open questions.** Do any channels *rely* on reading state mid-frame
after another channel mutated it? If so, that ordering dependency
should be explicit rather than implicit in snapshot timing.

**Effort.** Small.

---

## P39. Concept snapshot + quality report N+1 the evidence edges

**Motivation.** `build_concept_snapshot` loops `store.all()` and calls
`store.evidence_of(concept_id)` inside the loop — one SQL query per
concept, plus label resolution. `build_concept_quality` repeats the
pattern and adds an **O(n²) pairwise embedding comparison** within
each (kind, subject) group. Neither is on the turn path (they back
`GET /api/concepts`, the settings UI, and the MCP debug tools), so
this is a UI-latency and debug-ergonomics issue rather than a hot-path
one — but it scales with concept count, which is the number the
L-series is explicitly trying to grow.

**Key files.**
[`app/core/concepts/concept_snapshot.py`](../../app/core/concepts/concept_snapshot.py)
(~L139-153),
[`app/core/concepts/concept_quality.py`](../../app/core/concepts/concept_quality.py)
(~L239-242 the N+1, ~L454-467 the pairwise walk),
[`app/core/concepts/concept_store.py`](../../app/core/concepts/concept_store.py)
(`evidence_of` ~L749-754 — add a batch sibling).

**Sketched approach.** Add `evidence_for_many(concept_ids)` returning
a `dict[int, list[Edge]]` from a single `WHERE dst_id IN (…)` query
(chunked to stay under SQLite's parameter limit) and have both callers
use it. The pairwise embedding walk is a separate, larger question —
it's doing a small clustering job and should probably borrow the ANN
path the topic graph already uses.

**Effort.** Small (batch evidence lookup), Medium (pairwise walk).

---

## P40. Engine swaps leak the previous model

**Status: SHIPPED, and the entry was half wrong.** The STT half was
already fixed when this entry was written — `set_stt_model` has called
`old.shutdown()` on a daemon thread for a while, precisely to avoid the
orphaned-child failure the `shutdown()` docstring warns about. Both
setters also already early-returned on a no-op swap, so the "pays a full
reload" claim didn't hold either. What *was* real: `set_tts_provider`
dropped the outgoing `PocketTtsService` without releasing its weights,
because there was nothing to call — the release path is P28(b).

With that in place, the TTS swap moved into a shared
`_rebuild_tts_engine` (also used by the enable/disable toggle) which
releases the outgoing engine after rewiring the PCM listener, queue, and
prosody dispatcher to the new one. See
[`shipped/perf.md`](shipped/perf.md#p28-tts-respects-ttsenabled-and-the-runtime-toggle-frees-the-weights).

Lesson for the next audit: this entry asserted two behaviours from
reading the *sketch* of the code rather than the code, and both were
stale. Worth re-checking claims of the form "nothing calls X".

---

## P42. The retrieval budget is the residual of everything else

**Motivation.** Memories and concepts — the only blocks whose content is chosen
*because it is relevant to this turn* — get whatever tokens are left after every
other block has taken what it wants.

The arithmetic is explicit in `assemble_with_budget`
([`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)):
`system_base` is the join of **every block except T3**, which means persona plus
all of T1, T2, T4, T5 and the ~60 T6 detector blocks. That total is handed to
`_size_context_budget`
([`prompt_assembler_helpers_mixin.py`](../../app/core/session/prompt_assembler_helpers_mixin.py))
as `system_base_tokens`, and the surfacing reservation is computed from what
remains:

```
avail     = budget_tokens - system_base_tokens - user_tokens
surfacing = min(fraction * ctx, max_tokens) clamped to [0, avail - history_floor]
```

So the T3 reservation is a *residual*, and two things follow. A turn where many
T6 cues happen to fire directly shrinks the space available for memory and
concept retrieval — routine ambient chrome outbids turn-relevant recall.  And
the persona, at 77,940 chars (about 22k tokens at the cold-start 3.5 chars/token
ratio, less once calibration settles), is subtracted first and is never capped
or trimmed, so on a modest context window T3 can be squeezed toward its
`context_budget_min_tokens` floor with only `history_floor_tokens` (default
1024) standing between retrieval and the history.

The `degrade_level=1` signal already reports when the floor forced the budget
below target, so this is observable today — worth reading before changing
anything, because the frequency in real sessions determines whether this is a
theoretical concern or a daily one.

**Key files.**
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(`assemble_with_budget` — the `system_base` join and the `t3_insert_index`
insertion),
[`prompt_assembler_helpers_mixin.py`](../../app/core/session/prompt_assembler_helpers_mixin.py)
(`_size_context_budget`),
[`context_budget_selector.py`](../../app/core/session/context_budget_selector.py)
(what the reservation is spent on).

**Sketched approach.** Reserve the T3 slice against a *floor* rather than a
residual: compute the reservation from the stable base (T0-T2, which is where
persona lives and is genuinely non-negotiable) plus a floor for T3, and let the
T4-T6 tail take what is left over instead of the other way around. The tail is
overwhelmingly one-line cues, so this rarely changes anything on a quiet turn
and only bites on exactly the turns where the current behaviour is worst.

The minimal version does not need block arbitration (that is P43): a `min`
reservation that the tail cannot eat into, plus telemetry when the tail is what
forced the degrade, would capture most of the value.

Worth measuring first with `get_prompt_block_costs` on a busy turn: if the whole
T4-T6 tail is only a few hundred tokens in practice, this is a smaller problem
than the code shape suggests and P31's persona trim is the better lever.

**Open questions.** (1) Is a hard T3 floor right, or should it scale with how
good the candidates are — a turn with no relevant memories should not reserve
tokens it cannot use? The selector already knows this, but only *after* the
reservation is chosen. (2) Does the `history_floor_tokens` default of 1024
survive scrutiny — that is very little history to protect. (3) Interaction with
`aggressive`, which already forces T3 to floors-only, so the two pressure paths
need to compose sensibly rather than both firing.

**Effort.** Small (floor + telemetry) / Medium (if the reservation becomes
candidate-quality-aware).

**Depends on.** Nothing. Measure with P31a's `get_prompt_block_costs` first.
Superseded in part by P43 if that lands.

---

## P43. 105 blocks, no arbitration -- replace the aggressive denylist

**Motivation.** `_PROMPT_BLOCK_TIERS` registers 105 blocks, and the assembly
rule for all of them is `if block: system_parts.append(block)`. There is no
competition: whatever a provider returns goes in, in tier order, and the only
value-aware selection anywhere in the system prompt is *inside* the T3 region,
where `ContextBudgetSelector` weighs candidates against a budget.

When the prompt overflows, the relief valves are history (oldest messages
dropped) and a second `aggressive=True` assembly pass that skips a
**hand-maintained list** of about thirty providers. That list is the de facto
statement of what Aiko can afford to lose under pressure, and it has drifted
into some hard-to-defend places — `belief_gaps_block` is dropped while its
sibling `clarification_block` is kept; `wants_block` and `curiosity_seeds_block`
go while several rarely-firing cues stay. Nobody chose those pairings against
each other; they accumulated one item at a time.

The deeper problem is that the denylist encodes *importance* as a boolean fixed
at authoring time, when the thing we actually want to shed is low **value per
token** — which depends on the turn, and after L37 is something we can measure
rather than assert.

**Key files.**
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) (the
cascade, `_PROMPT_BLOCK_TIERS`, and every `if not aggressive` guard scattered
through the provider calls),
[`prompt_support.py`](../../app/core/session/prompt_support.py)
(`_safe_provider`, `PromptTelemetry.block_chars`),
[`context_budget_selector.py`](../../app/core/session/context_budget_selector.py)
(the arbitration pattern to generalise — it already does floors, caps, weights
and a greedy fill, which is most of what is needed).

**Sketched approach.** Give each block a small policy record — tier, a value
weight, and whether it is floor-protected — and run the same
floors-then-weighted-greedy selection the T3 region already uses across the
whole block set. `aggressive` then stops being a denylist and becomes a lower
budget, which is both less code and much harder to let drift.

The value weight starts as a hand-assigned constant (no worse than today's
boolean, and strictly more expressive), and becomes learned once G4's cue
outcome rates exist: a block whose cues consistently land keeps its slot under
pressure, one that never lands sheds first. That is the point at which the
prompt starts allocating itself according to what actually works.

Two properties to preserve deliberately: the persona and grammar addenda stay
privileged and uncapped (they are the contract the rest of the output depends
on), and the tier order of what survives must not change, or the prompt-cache
prefix ladder breaks — arbitration decides *whether* a block is included, never
*where* it sits.

Ship the telemetry before the behaviour: a per-block "would have been dropped"
report under a simulated tighter budget makes the policy reviewable before it
is load-bearing.

**Open questions.** (1) Is 105 blocks the actual problem? A cheaper reading of
this audit is that most blocks are empty most of the time and the real cost is
the ~60 provider calls per turn, which is a different fix (P31's lazy-render).
Measure before building an allocator. (2) How to keep a rarely-firing but
critical block (rupture, clarification) from being shed just because its
historical engaged rate is thin — floor-protection has to be explicit, not
learned. (3) Whether `_safe_provider` swallowing exceptions into `""` needs
fixing first: a permanently broken block currently looks identical to a gated
one, which would poison any learned value estimate.

**Effort.** Medium (policy records + generalised selection) / Large (with
learned weights).

**Depends on.** P42 (or supersedes it), G4 and L37 for the value signal.
Related to P31 (lazy-render) — that reduces the *cost* side of the same ratio.
