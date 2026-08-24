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
release, P41 the two missing `messages` indexes, P47 paging
`GET /api/concepts`, and P48 the mobile render + audio-idle budget have
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
exposed via `setLipsyncListener`. (Since
[P48](shipped/perf.md#p48-the-avatar-and-the-audio-graph-ran-flat-out-on-phones)
that loop only runs while something is playing and stops when the page
hides, so extending the gate to desktop no longer means adding a
permanent 60 Hz wakeup there.) The socket hook only routes it
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

**Status: the cap is gone — `max_memories`, `scratchpad_cap` and
`archive_cap` all ship at `0`, meaning "never evict".** What is
left of this entry is the part that was previously guesswork: what
a growing corpus actually costs, and which three things to fix
when it starts to hurt. Those are now measured rather than
estimated, and the headline is that **none of them is the
database**.

**Why the cap went rather than moved.** `prune()` deletes rows; it
does not demote them. So a cap is a forgetting policy wearing a
performance guard's clothes, and every reason to keep it was a
performance reason. The original one — the old topic graph's
`O(n²)` in-process re-clustering on every read — is gone: the
graph is persisted, incrementally maintained, and batch-refits
through LanceDB ANN. `0` is a real sentinel rather than a large
number because every cap setting clamps to a floor of 50 to
survive a typo, and that floor is exactly what makes `max(50, n)`
unable to express "no cap".

### The measured curve

Real rows (real embeddings, real content lengths) duplicated in a
copy of the live database, so this is a curve and not an
extrapolation from one point. `search` is shown before and after
fix 2 below:

| rows | mirror load | resident | peak | `decay()` | `search()` was → now | plain SQL scan |
|---|---|---|---|---|---|---|
| 1,829 (today) | 0.08 s | 11 MB | 20 MB | 0.10 s | 5.6 → 0.1 ms | 0.7 ms |
| 10,000 | 0.42 s | 61 MB | 108 MB | 0.64 s | 28.9 → 0.5 ms | 0.8 ms |
| 20,000 | 0.85 s | 122 MB | 218 MB | 1.28 s | 60.8 → 1.1 ms | 1.2 ms |
| 50,000 | 2.16 s | 305 MB | 545 MB | 3.20 s | 153.6 → 2.8 ms | 2.5 ms |

Everything is linear in row count. Growth is running at ~450
memories/week and rising, which puts 20k inside a year and 50k
inside three.

*Measurement note:* an earlier version of this table put the load
at 14.7 s for 50k. That column had been timed with `tracemalloc`
running, which is what produced the memory figures either side of
it and which inflates allocation-heavy code several-fold. The
memory columns are sound; the load column is the re-measured one.

**SQLite is not in the picture and Postgres would not help.** A
full aggregate over 50k memories is 2.5 ms; the whole database is
58 MB. The corpus is a few tens of thousands of small rows read
by one process — nowhere near the point where the relational
engine is the constraint. Moving to Postgres would add a service
to operate and move none of the numbers above, because all of
them are Python-process costs. The one honest argument for it is
pgvector replacing LanceDB, and that trade should be judged on
the vector story, not on the row count.

This holds for the whole database, not just this table: the
fastest-growing tables are 25× this one and stay flat to 5M rows
under the same test — see
[P49](#p49-the-telemetry-ledgers-are-the-fastest-growing-tables).

### The three things to fix — two done

1. ~~**Embeddings are N separate arrays instead of one matrix.**~~
   **Shipped.** At 20k rows the vectors are 81.9 MB either way, so
   this was never about the bytes — it was that `search()` and
   `add()`'s dedupe pass were per-row Python cosine loops over
   them. Both now go through
   [`VectorIndex`](../../app/core/memory/vector_index.py), one
   contiguous `(rows, dim)` float32 matrix kept beside the mirror:
   **search 153.6 → 2.8 ms and dedupe 54.2 → 2.8 ms at 50k**.
   Dedupe is the one that mattered, since it ran on every write
   rather than on a secondary read path. Deletion is a tombstone,
   because compacting a row out of a matrix would make `prune()`
   quadratic.
2. **`decay()` re-reads the whole table.** It does the bulk
   `UPDATE` in SQL and then calls `_reload_mirror()`, so a
   scheduled decay costs a full disk re-read of every row and
   BLOB: 3.2 s at 50k, which is most of the 2.16 s load plus the
   update. The new salience is already computable in process;
   updating the mirror in place makes this ~free. **Still open**,
   and now the largest remaining item.
3. **`_reload_mirror` uses `fetchall()`.** Peak is 1.8× resident
   (218 MB vs 122 MB at 20k) purely because every row tuple and
   BLOB is materialised before the first `Memory` is built.
   Iterating the cursor removes the doubling for a few lines'
   change, and peak is what decides whether a load survives, not
   average. **Still open**, and cheap.

With those two done, 50k rows costs ~2 s at startup, ~305 MB
resident, and nothing measurable per query. RSS is then the only
thing that still grows, and 305 MB is small next to the STT/TTS
weights (P27/P28).

### The MemoryRetriever question is now separate

`MemoryStore.search` is not on the prompt path — retrieval goes
`RagRetriever` → `RagStore`. `MemoryRetriever` is constructed,
handed to `PromptAssembler`, and never invoked. Making `search`
fast has removed the urgency, but the dead wiring is still worth
either deleting or connecting on purpose.

### Still-open architectural fork

Is the mirror worth keeping once retrieval is fully ANN-backed, or
should it become a bounded LRU of the hot set (equivalently: evict
the `archive` tier and read it from LanceDB on demand)? Fix 1 and
fix 2 both get cheaper to abandon later, so neither forecloses
this. Overlaps with the F10 topic-graph utilisation cluster
([`awareness.md`](awareness.md)).

Unrelated but adjacent: `data/` is 1.37 GB, of which 1.09 GB
(26,766 files) is `lancedb-20260813.bak` from the version upgrade
and ~113 MB is stale `chat_sessions.db` backups. Nothing to design
around, just worth deleting once the upgrade is trusted.

**Key files.**
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py)
(`_normalize_cap`, `prune`, `decay`, `_reload_mirror`, `search`),
[`app/core/infra/memory_settings.py`](../../app/core/infra/memory_settings.py)
(`_parse_tier_cap`),
[`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py)
(`ensure_vector_index`),
[`config/default.json`](../../config/default.json).

**Effort.** Small (fix 1) → Small (fix 3) → Medium (fix 2, if
`search` is kept at all) → Large (mirror eviction / LRU rework).

---

## P31. Audit + trim the baseline system prompt (~25-30k resting floor)

**Status: (a) shipped, so this is no longer a guess.** Sketch item (a)
— the measurement — landed as the `get_prompt_block_costs` MCP tool and
`PromptTelemetry.block_chars`; see
[`shipped/perf.md`](shipped/perf.md#p31a-get_prompt_block_costs--per-block-prompt-cost-weighted-by-tier).
It ranks blocks by tokens × the tier's cache-miss probability, and
reports empty blocks as `0` rather than omitting them, so (c)'s
content-gating candidates surface directly.

**Correction, and it inverts this item's conclusion.** The figure recorded
here was "the persona (~78k chars, ~19.5k estimated tokens) dominates
everything else combined — so (b), the persona trim, is where the value
is". Both halves were wrong. A live re-measure on 2026-08-04 puts the
persona at **35,938 chars / 8,103 tokens** — the old char count was larger
than the entire system prompt, so it cannot have been the persona alone.
More importantly, the persona sits in `T0_stable`, and now that the
prefix-stability work has the early tiers actually caching, `effective_tokens`
ranks it **third**:

| Block | Tier | Tokens | Effective |
| --- | --- | --- | --- |
| `relevant_context` | T3_rag | 3,378 | **2,027** |
| `handling_notes_block` | T6 | 1,216 | **1,216** |
| `persona` | T0 | 8,103 | 405 |

A cached T0 block is billed at the cache discount, so the biggest block in
the prompt is not the most expensive one. The remaining value is in the
volatile tail — the T3 retrieval budget and the T6 detectors — which is
P42 + P43 below rather than a persona pass. Re-run
`get_prompt_block_costs` before acting on any number in this entry; the one
above is the only measured one.

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

**Status: shipped — and the entry was understating it.** This was
filed as "built once, never refreshed". Checking
`list_indices()` against the live store found `[]` on **every**
table: the index had never been built at all, and every retrieval
in the app's history has been a flat scan.

**Why it never fired.** `ensure_vector_index` carries its own
`min_rows=256` threshold, which the corpus passed long ago. But
its only caller was `topic_graph.rebuild()`, guarded by
`_ANN_REBUILD_THRESHOLD = 2000` — a threshold about whether
*clustering* should go dense or ANN, which has nothing to do with
whether an index is worth having. The corpus sat at ~1,900
memories, so the guard held and the index was never requested.
Two unrelated thresholds, and the stricter one silently owned the
decision.

**What shipped.**
- `ensure_vector_index` now covers `memories` **and** `messages`,
  and returns a per-table report (`rows` / `indexed` /
  `unindexed` / `action`) instead of a bool, so "is it there and
  is it fresh?" is answerable rather than inferable — sketch (b)
  and (c).
- **The refresh is the substantive part.** Lance does not add new
  rows to an existing IVF_PQ index; they accumulate as *unindexed*
  rows that every query scans on top of the ANN probe. An index
  built once therefore decays back into a flat scan at exactly the
  rate the corpus grows, invisibly — results stay correct, only
  slower. `ensure_vector_index` rebuilds with `replace=True` once
  more than `refresh_ratio` (20%) of rows are unindexed.
- Called from `RagMaintenanceWorker.run()`, after compaction:
  same trigger (writes since last pass), same exclusive lock
  already held, and the rebuild sees the compacted layout rather
  than indexing fragments about to move — sketch (a).

**Measured on a copy of the live store:** the worker builds both
indexes in 1.3 s; memories search 11.6 → 5.4 ms (2.15×), messages
15.7 → 5.5 ms (2.85×); a second run reports `fresh` and does
nothing. The multiple grows with the corpus, since the flat side
is linear and the ANN side is not.

`documents` is deliberately excluded: it is empty, and
`min_rows` would skip it anyway.

**Key files.**
[`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py)
(`ensure_vector_index`, `_ensure_one_index`, `_index_stats`),
[`app/core/rag/rag_maintenance_worker.py`](../../app/core/rag/rag_maintenance_worker.py)
(the call site, after `optimize()`).

**Left open.** The 2000-row `_ANN_REBUILD_THRESHOLD` call in
`topic_graph.rebuild()` is now redundant but harmless — it
requests an index that already exists. Worth deleting when that
file is next touched.

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

### Status: phase 1 shipped — demand-driven scheduling

Answered the open questions as: **neither ceiling alone**, and **no
preemption**.

- *Ceiling.* Wall-time, but split in two. The 6 s budget was never
  really about time — it was about one local Ollama serving both the
  chat path and the workers. So `idle_worker_tick_budget_ms` now governs
  only the **LLM lane**, sized by a `classify_contention()` grade
  (`none` / `queueing` / `swapping`) derived from comparing the
  `main_chat` and `worker_default` routes. Everything else draws on a
  separate **compute lane** that has no GPU to protect. Compute drains
  first within a tick, so cheap arithmetic never queues behind a
  generation. See [`llm_contention.py`](../../app/core/proactive/llm_contention.py).
- *Preemption.* Rejected. A returning message queues behind whatever is
  running rather than cancelling it, which made the in-flight worker's
  duration the user's real worst-case wait. That exposed a bug in the
  pre-existing anti-starvation rule: `ran >= 1` exempted the first
  worker of every tick from the budget entirely, so a worker with a 45 s
  average was admitted on every tick and `tick_budget_ms` bounded only
  slots two onward. The exemption is now depth-aware — at `just_left`
  slot 1 must fit its lane, from `away` on it need not — with two escape
  valves (a never-run worker has no measured cost to budget against, and
  three heartbeats overdue admits regardless).
- *(a) starvation reporting* — shipped as `last_admit_reason` /
  `last_pressure` / `last_urgency` / `last_lane` per worker in
  `get_idle_workers_status`, plus a new `probe_idle_worker_demand` tool
  that asks every worker what it thinks it has to do right now.
- *(c) dirty checks out of `run()`* — shipped as the `demand()` probe,
  which is the general form: a worker reports pending work *and* whether
  servicing it needs the LLM, and the scheduler ranks by urgency (70%
  pressure, 30% staleness) instead of by age. `interval_seconds` is
  demoted from cadence to heartbeat.

Nine workers migrated: the five world ones (`away_activity`,
`garden_visit`, `plant_growth`, `circadian_settle`, `room_evolution`)
and four thinking ones (`concept_lifecycle`, `concept_synthesis`,
`concept_consolidation`, `memory_decay`). The remaining ~40 keep their
old interval behaviour byte-for-byte, and
`idle_worker_pressure_enabled: false` restores the old path wholesale.

The mechanism itself is documented in
[`docs/idle-workers.md`](../idle-workers.md); follow-ups are
[P44](shipped/perf.md#p44-migrate-the-remaining-idle-workers-to-demand),
[P45](#p45-retire-the-per-hour--per-day-caps-in-favour-of-satisfaction)
and [P46](#p46-parallel-compute-lane-drain).

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

**Status: still open, but no longer the thing that hurts.** Both callers
are now bounded rather than batched —
[P47](shipped/perf.md#p47-get-apiconcepts-returned-the-whole-graph-untruncated)
pages the snapshot so evidence resolves for ~50 rows instead of all 822,
and the quality report loads on request rather than on every visit to the
tab. The N+1 is unchanged; N is. `evidence_for_many` is still the right
fix and is now a tidy-up rather than a UI-latency one. The pairwise
embedding walk in the quality report is the remaining real cost, and it
is the half this entry already called out as the larger question.

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


## P42. The retrieval budget is the residual of everything else

**Status: FOLDED INTO P43.** The diagnosis holds; the "small (floor +
telemetry)" effort estimate below does not, and the reason is structural rather
than a matter of tuning. A reservation is only a floor if someone yields to it,
and by the time `_size_context_budget` runs there is nothing left to take from.
`context_budget_min_tokens` already reads like the floor this entry asks for, but
it is clamped by `hi = max(0, avail - history_floor)`, and `avail` is computed
*after* the entire T4-T6 tail has been built and joined into `system_base`. So
the min is honoured only when it happens to fit — a floor by convenience. Making
it bind requires one of: shedding or trimming tail blocks, which is exactly
P43's value-aware arbitration; or eating into `history_floor_tokens`, which has
its own floor for its own reasons and just relocates the starvation. There is no
third option that doesn't overcommit the window. Sequence this behind P43 rather
than shipping a floor that silently doesn't hold, and see open question (3)
below — the composition with `aggressive` is the same problem P43 is replacing.

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
forced the degrade, would capture most of the value. **This turned out not to be
available** — see the status note above; the reservation has no slack to draw on,
so only the telemetry half is separable.

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

**Effort.** ~~Small (floor + telemetry)~~ — Medium, and not independently
shippable: the floor half needs P43's arbitration to have anything to yield.
Telemetry attributing a degrade to the tail is separable and small if it is
wanted before then.

**Depends on.** P43 (the arbitration that makes a floor enforceable). Measure
with P31a's `get_prompt_block_costs` first.

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

---


## P45. Retire the per-hour / per-day caps in favour of satisfaction

**Status: partly shipped.** The seven subject-naming cue workers now run
off the [cue pool](../cue-pool.md): they count unspent rows and report
pressure from the deficit against `CuePolicy.inventory_target`, which
retired five `*_daily_cap` keys outright. That is the *inventory* half
of the idea — a full shelf means the worker is not admitted — and it
lands the "she has said enough for now" intent through a mechanism that
is exact rather than arbitrary, since the pool knows what is unspent
where a ring never did. The *satisfaction* half below is still open, but
its input now exists for the pooled types: `used` vs. `expired` per cue
type is a direct read on whether the user is biting, at a resolution
`engaged_rate` never had. The remaining `*_per_hour_cap` keys on
non-pooled workers are untouched.

**Motivation.** Keys like `idle_curiosity_per_hour_cap` /
`idle_curiosity_per_day_cap` do two unrelated jobs. As *cost*
protection they are now redundant with the [P36](#p36-idle-worker-llm-pile-up-under-a-6-s-soft-budget)
lane budgets, which bound LLM time directly rather than by proxy. As
*behavioural* protection they are doing something real — five curiosity
cues an hour is annoying regardless of what it cost — but they express
it as an arbitrary count instead of as the thing actually meant: *she
has said enough for now*.

**Sketched approach.** Replace the count with a satisfaction signal
fed back from consumption. `SurfacingOutcomeStore` already tracks
whether a surfaced item was engaged with, neutral, or abandoned, and
exposes `engaged_rate`. A worker whose last few cues were never taken up
should report *low* pressure regardless of how many it is nominally
allowed; one whose cues keep landing should be free to keep going. That
turns "at most 5/hour" into "produce until the user stops biting", which
is both closer to the intent and self-tuning per user.

**Prerequisite.** [P44](shipped/perf.md#p44-migrate-the-remaining-idle-workers-to-demand)
for the workers concerned — the satisfaction signal has to live
somewhere, and `demand()` is that somewhere.

**Effort.** Medium. Needs a per-worker mapping from surfacing outcomes
back to the worker that produced the item, which does not exist yet for
every cue type.

---

## P46. Parallel compute-lane drain

**Motivation.** [P36](#p36-idle-worker-llm-pile-up-under-a-6-s-soft-budget)
drains compute-lane workers *before* LLM ones but still one at a time,
which captures most of the benefit (a cheap worker no longer waits out a
generation) without touching concurrency. Running the compute lane in
parallel would be the next step — the lane is CPU-bound and its workers
are mostly independent.

**Why it is not done.** An audit found enough shared mutable state to
make it unsafe today, and none of it fails loudly:

- `WorldStore.list_items` returns **live references** into the in-memory
  mirror, and `promote_stage` mutates `item.state` in place. (P36 added
  a read-only `stage_promotion_due` for the probe path specifically
  because of this.)
- `ConceptStore._concepts` / `_vectors` and the cached active matrix are
  unguarded.
- `MemoryStore` and `WorldStore` use `threading.local()` connections, so
  parallel writers get separate transactions against a WAL database with
  no global write lock.
- `EngagementClock` does a read-modify-write on `kv_meta` without one.
- The scheduler's own `last_run_at` / record updates assume a single
  drain thread.

**Prerequisite work.** Guard the in-memory mirrors, make
`list_items` hand out copies (or make the mutating helpers explicit
about ownership), and decide on a write-serialisation strategy for
SQLite. `RagStore` is already thread-safe via a reader-writer lock and
is the model to follow.

**Effort.** Medium-Large, and mostly prerequisite rather than scheduler
work.

---

## P49. The telemetry ledgers are the fastest-growing tables

**Motivation.** A census of every table, taken while sizing P30,
put the growth in a different place than expected. `memories` is
small and slow-growing. What is actually accumulating is the
observability we have been adding:

| table | rows | +1 yr | +3 yr | pruned? |
|---|---|---|---|---|
| `surfacing_outcomes` | 45,286 | ~596k | ~1.70M | `prune()` exists, never called |
| `concept_events` | 17,357 | ~214k | ~609k | never |
| `turn_prompt_blocks` | 13,244 | ~174k | ~497k | `prune()` exists, never called |
| `concept_edges` | 12,549 | ~143k | ~403k | has deletion paths |
| `cue_decisions` | 5,288 | ~70k | ~198k | `prune()` exists, never called |
| `messages` | 4,634 | ~22k | ~58k | never |
| `concepts` | 2,563 | ~28k | ~78k | lifecycle retires |
| `memories` | 1,829 | ~13k | ~35k | uncapped (P30) |

`surfacing_outcomes` alone is growing ~25× faster than
`memories`, and `concepts` about twice as fast.

**The reassuring half, which is most of it.** SQLite absorbs this
without complaint. Grown to 5M rows with realistic timestamps,
`stats_for(200 concepts, 90d)` — the one the lifecycle worker
runs — is **flat**: 5.2 ms at 45k, 6.3 ms at 5M. Every read takes
a window, and the window predicate resolves through a covering
index (`idx_surfacing_outcomes_created`), so total history costs
nothing. This is the measurement behind "SQLite is fine, don't
migrate" holding for the whole database and not just for
`memories`.

**What does drift.** `engaged_rate_by_cluster(window_days=90)`
goes 32 ms → 63 ms → 256 ms across 45k → 600k → 5M total rows,
despite the window holding a constant ~45k. So one of its arms is
not window-selective. It runs on an idle worker, so a quarter
second after eight years of use is not a problem — it is filed
because it is the one query whose shape does not match the others,
and that is usually worth knowing before it matters.

Disk is the real consequence: 1.2 GB at 5M rows, versus 58 MB
today.

**Sketched approach.** (a) Schedule the three existing `prune()`
methods from an existing maintenance worker with a generous
retention (a year keeps every analysis currently run). (b) Find
the non-selective arm of `engaged_rate_by_cluster`. (c) Decide
whether `concept_events` and `messages` want retention at all —
both are arguably history rather than telemetry, and `messages`
is the transcript itself.

**Key files.**
[`app/core/memory/surfacing_outcome_store.py`](../../app/core/memory/surfacing_outcome_store.py),
[`app/core/memory/cue_decision_store.py`](../../app/core/memory/cue_decision_store.py),
[`app/core/memory/turn_prompt_block_store.py`](../../app/core/memory/turn_prompt_block_store.py),
[`app/core/rag/rag_maintenance_worker.py`](../../app/core/rag/rag_maintenance_worker.py)
(the obvious host — it already runs a slow maintenance cadence).

**Effort.** Small (a), Small (b), Small (c, mostly a decision).

---

## P50. The persona hoist has no cap, and it is the second-largest block

**Motivation.** `handling_notes_block` is the [persona
hoist](../cue-pool.md#the-persona-hoist): conditional handling notes live
outside the always-on persona and are pasted in only on the turns their cue
actually renders. The design premise is stated in
`data/persona/conditional_handling.txt` itself — each section is for a
*minority* of turns.

It is not behaving like a minority. H52 measured it at **5,517 chars mean,
p90 8,017, max 18,446**, which makes it the largest block in the prompt
after the T3 region. And the mechanism has no brake at all:

```python
# _render_handling_notes: walk every block with a registered header,
# hoist the note of each one that rendered a non-empty string.
for block in self._handling_headers():
    ...
    if note and note not in seen:
        parts.append(note)
```

**There is no cap on how many sections can hoist on one turn.** With 56
registered headers at a 961-char mean, the theoretical ceiling is **53,807
chars** — larger than the persona. Deduplication is by full note text, so
families that share a section (three style detectors → one "Style patterns
I'm in:") collapse correctly; what does not collapse is a busy turn where a
lull, a repair, two topic reads and a gap cue each contribute a distinct
600–2,900 char section.

The largest sections are the ones most likely to co-fire: "Style patterns
I'm in:" (2,893), "Feelings at {user_name}:" (1,923, and its block pulls
"The mask:" too, for ~3,113 together), "When you have your own take:"
(1,748).

**Why it matters beyond tokens.** This is instruction text, and the failure
mode of hoisting fifteen sections is not cost — it is that she is handed a
manual on the turn she most needs to be responsive. P31 argues the same
thing about the resting persona floor; this is the acute version.

**Key files.**
[`prompt_assembler_helpers_mixin.py`](../../app/core/session/prompt_assembler_helpers_mixin.py)
(`_render_handling_notes`, `_handling_headers`),
[`prompt_support.py`](../../app/core/session/prompt_support.py)
(`HANDLING_SECTIONS`, `_STAYS_IN_T0`),
[`cue_accounting.py`](../../app/core/proactive/cue_accounting.py)
(`CuePolicy.handling_section`),
`data/persona/conditional_handling.txt` (the 56 sections).

**Sketched approach.** A cap on hoisted sections per turn, spent by
priority rather than by tier order — which is what the walk uses today, so a
T4 note wins over a T6 note for no reason other than where it was
registered. The priority signal already exists in two places: K92's stance
arbiter already decides which *cue* leads the turn
([`stance.py`](../../app/core/conversation/stance.py) `_OFFERS`), and L37's
outcome rates already say which cues land. Hoisting the notes for the cues
that won arbitration — rather than for all of them — is the same decision
made once instead of twice.

Instrument first: record hoisted section count and total chars per turn, so
the cap is chosen against the real distribution instead of guessed.

**Open questions.** Should a section whose cue lost arbitration hoist a
*shortened* form rather than nothing — the cue is still in the prompt, so
dropping its handling note entirely leaves an unexplained cue, which is the
failure the hoist exists to prevent. Does the cap interact with
`aggressive`, which currently drops cues but not their notes?

**Related.** P43 (block arbitration — this is the same problem one level
down, *inside* a single block), P31 (the resting persona floor),
[cue-pool.md](../cue-pool.md#the-persona-hoist).

**Effort.** Small (cap + telemetry), Medium (arbitration-driven selection).

---

## P51. `relevant_context` and the summary do not know about each other

**Motivation.** The T3 region is the most expensive block in the prompt at
**17,480 chars/turn** (H52), and the rolling summary sits three tiers above
it in T2. Nothing compares them.

The dedup that *does* exist in the retrieval path is thorough, which is why
the gap is easy to miss: exact-text dedup across merged hits, a
cluster-aware diversity cap, a 6-hour recency penalty on
recently-surfaced memories with a 7-day revival bonus, L23 habituation
damping for concepts, `profile_block` overlap suppression, and an explicit
exclusion of the *current session's messages* on the grounds that they are
already in the recent window:

```python
if exclude_session_id and h.source == "message":
    # Don't surface lines from the *current* session --
    # they're already in the recent-window context.
```

That comment is the exact argument for the missing case. A memory whose
content the summary already states is in the prompt twice, in two voices,
and the second copy costs retrieval budget that P42 has already established
is the scarcest in the assembly.

**Key files.**
[`inner_life_part1.py`](../../app/core/session/inner_life_part1.py)
(`build_relevant_context`),
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py) (the existing
dedup ladder and `exclude_session_id`),
[`context_budget_selector.py`](../../app/core/session/context_budget_selector.py)
(where a penalty would apply).

**Sketched approach.** The summary text is already available to the
assembler when the T3 region is built, and the embedding machinery is
already in hand — so a similarity penalty against the summary fits the
selector's existing weight model without new infrastructure. Prefer a
*penalty* over an exclusion: a memory the summary mentions in passing may
still be worth surfacing in full, and hard-excluding it would make the
summary suppress its own sources.

Measure first. This is filed on the strength of the code shape, not of an
observed overlap rate — the honest first step is to score the current T3
selection against the current summary for a few hundred turns and find out
whether the overlap is 2% or 30%.

**Open questions.** Does the same argument apply to `thread_note_text` and
to `continuity_block`, which are also narrative restatements of stored
material? Should the penalty be symmetric — should compaction avoid
summarising what T3 reliably surfaces anyway?

**Related.** P42 (the T3 budget is a residual, so wasted T3 tokens are the
expensive kind), P30.

**Effort.** Small to measure, Small to apply once the rate is known.
