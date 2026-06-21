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
telemetry, P8 idle-worker queue visibility + multi-worker drain,
P12 bulk memory-mirror on startup, P13 route-driven worker
model + context (with the declarative `_worker_runtime_updaters`
cascade + worker-LLM priority gate), and P14 heuristic
tool-pass gate have shipped — see [`shipped.md`](shipped.md).)

---

## P3. Slice-cache validation still pays two SQLite reads

**Motivation.** `assemble_with_budget` ostensibly short-circuits
on a slice-cache hit, but the validation path still calls
`get_messages` + `get_latest_summary` + recomputes the cache key
before trusting cached slices. Voice prebuild helps the inner-life
blocks, but every typed turn pays those two reads even when
nothing changed since the last turn. A cheaper invalidator would
let hits actually be cheap.

**Key files.**
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py),
[`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py)
(lightweight `MAX(id)` / `summaries.updated_at` head query).

**Sketched approach.** Cache the `(max_message_id,
summary_updated_at)` tuple alongside the slice-cache entry. On
hit, do a single 2-column SELECT and compare; if equal, skip the
`get_messages` round-trip entirely. On miss, fall back to the
current full validation.

**Effort.** Small.

---

## P4. RAG memory-hit batch lookups

**Motivation.** After Lance ANN search, `RagRetriever.retrieve`
calls `memory_store.get(id)` per hit (up to `per_source_top_k`
times) to apply pin / tier / confidence / temporal scoring. The
mirror dict makes each call O(1) but the per-iteration try/except
+ scoring is repeated work; a single batch fetch (or pre-enriching
Lance rows once) would simplify the hot loop and make the next
optimisation wave (e.g. async scoring, cross-source de-dupe) much
easier to reason about.

**Key files.**
[`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py),
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py)
(`get_many` returning a dict; the in-memory mirror already has
all the data).

**Effort.** Small.

---

## P5. Novelty warm-up: full Lance scan

**Motivation.** `RagStore.list_recent_user_vectors` materialises
the entire Lance `messages` table via `to_arrow()` then filters in
Python. Cheap on day one, linear with chat history, runs once per
session on the first K6 detect. Multi-month installs will pay
real wall-time on the first novel turn after a fresh start.

**Key files.**
[`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py)
(`list_recent_user_vectors`),
[`app/core/conversation/novelty_detector.py`](../../app/core/conversation/novelty_detector.py)
(warmer call site).

**Sketched approach.** Push the role/session-prefix filter into
the Lance query (Lance supports SQL-like predicates), cap the row
limit at `2 * novelty_window`, and order by `created_at DESC`
server-side so we drop the Python sort entirely. Alternative: a
small `kv_meta` cache of the latest N user vector ids per user, refilled by the message indexer on write.

**Effort.** Medium.

---

## P6. MessageIndexer queue visibility

**Motivation.** Startup backfill walks every session/message with
embed + Lance write, competing with live-turn RAG/novelty embeds.
The queue is unbounded (only logs on impossible `put_nowait`
failure), so a slow Ollama response can stack thousands of
pending writes invisibly. There's no MCP surface for queue depth
/ lag — `get_rag_prefetcher_stats` exists for the prefetcher but
not the indexer.

**Key files.**
[`app/core/rag/message_indexer.py`](../../app/core/rag/message_indexer.py),
[`app/mcp/server.py`](../../app/mcp/server.py)
(new `get_message_indexer_stats` tool),
AGENTS.md log field table.

**Effort.** Small.

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
[`web/src/components/ChatView.tsx`](../../web/src/components/ChatView.tsx)
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

**Motivation.** The Virtuoso virtualisation fixed the *render*
cost on a long history, but `appendAssistantToken` still clones
the entire `messages` array on every chunk. Long conversations +
fast token streaming = unnecessary JS work that can re-introduce
the freeze symptom under load even with virtualisation.

**Key files.**
[`web/src/store.ts`](../../web/src/store.ts)
(`appendAssistantToken`),
[`web/src/components/ChatView.tsx`](../../web/src/components/ChatView.tsx)
(maybe move the streaming bubble to isolated state so updates
don't go through the global messages array).

**Sketched approach.** Either (a) keep the streaming text in a
per-bubble ref and only commit to the messages array on stream
end, or (b) use immer/structural sharing so the cloned messages
array reuses the prefix. Option (a) is the cleaner companion-AI
fit because it lets us render a "speaking" bubble distinctly
from finalised history.

**Effort.** Medium.

---

## P10. Schedule-learner missing index

**Motivation.** G2 / K3 query
`WHERE role='user' AND created_at >= ?` against `messages`. Code
comments assume row count stays small ("per-day, not per-message
so a full scan is cheap enough"), which is true today. Multi-year
installs will full-scan; a tiny `(role, created_at)` index closes
that out before it bites.

**Key files.**
[`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py)
(schema bump + migration),
[`app/core/infra/schedule_learner.py`](../../app/core/infra/schedule_learner.py)
(comment update).

**Alternative.** Persist daily/hourly aggregated buckets in
`kv_meta` keyed by `(user_id, iso_week, weekday, bucket)` so the
worker reads compact rows instead of raw messages. Heavier but
also unlocks multi-week recurrence trends past the rolling
window.

**Effort.** Small (index) or medium (aggregate cache).

---

## P11. Reclaim background-worker `num_predict` from reasoning leakage

**Motivation.** Reasoning-tuned models (qwen3.x family especially,
including the `jaahas/qwen3.5-uncensored:9b` build we run today)
ignore `think=False` and still emit `<think>...</think>` tokens
that count fully against `num_predict`. We strip those blocks
post-hoc in `OllamaClient`, and the truncation warning is now
downgraded to DEBUG when the visible answer reaches a natural
stop, so the noise is gone — but the *budget* is still being
spent on a trace that the operator never sees. A self-image pulse
with `max_tokens=320` may only have ~200 tokens of actual prose;
the rest is reasoning we throw away. That eats wall-time on every
worker run and forces us to over-provision the cap to avoid real
truncation.

**Key files.**
[`app/core/persona/self_image_worker.py`](../../app/core/persona/self_image_worker.py)
(`_PROMPT`),
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
`self_image_worker`, `relationship_pulse`, etc. If qwen3.5
uncensored ignores it, fall back to (a) wrapping prompts with
`<no_think>...</no_think>` tags some templates support, or (b)
running background workers on a non-reasoning Ollama model
(e.g. a small 3B instruct) via a `chat_llm.background_model`
setting; the main turn still uses the reasoning model.

**Open questions.** Does `/no_think` actually save tokens on
`jaahas/qwen3.5-uncensored:9b` specifically, or does this fine-
tune ignore both? If it ignores, the cleaner path is the dual-
model split (background_model setting), which is more code but
also unlocks faster background-worker turnaround independently.
Note: P13 (shipped) makes the dual-model split actually work —
set `llm.routes.worker_default.model` and every worker picks it
up via the declarative cascade, no restart. Before P13 the only
way to change the worker model was editing `ollama.chat_model`
in `user.json` directly.

**Effort.** Small (just the prompt suffix + before/after token
measurements). Medium if we need the dual-model split.

---

## P15. One user-text embed per turn, shared everywhere

**Motivation.** A single turn can fire 2–4 sequential HTTP
embedding calls on the turn thread: RAG retrieval embeds a
*contextual* query string (`recent || user`, so the LRU can't
collapse it with anything), K6 novelty embeds `user_text`, F2
`pick_relevant` embeds again, K29 opinion injection embeds again.
Post-turn adds another 2–3 (curiosity seeds, knowledge gaps,
K22/K30 assistant-text, K20 calibration). Each call holds the
embedder lock and competes with `MessageIndexer` for the same
Ollama endpoint — 50–200+ ms of pure waiting per turn at local
embed speeds, worse under contention.

**Key files.**
[`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py)
(`retrieve` — the contextual-query embed),
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(`assemble_with_budget` — natural place to compute the shared vector),
[`app/core/conversation/novelty_detector.py`](../../app/core/conversation/novelty_detector.py),
[`app/core/memory/knowledge_gap_extractor.py`](../../app/core/memory/knowledge_gap_extractor.py)
(`pick_relevant`),
[`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)
(`_render_opinion_injection_block`),
[`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
(curiosity / gaps / callback / calibration resolvers).

**Sketched approach.** Compute `user_vec = embed(user_text)` once
at the top of `assemble_with_budget` and thread it (or a tiny
`TurnEmbeds` bundle) into every provider that only needs user
semantics. Same pattern post-turn: one
`embed_turn_bundle(user, assistant)` reused across all resolvers.
RAG keeps its contextual-query embed (it's semantically
different), so the steady state is 2 embeds on the hot path and 1
post-turn instead of up to 7.

**Effort.** Medium.

---

## P16. Post-turn inner-life blocks the brain loop

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

## P17. K22 callback detector scans the full memory mirror every turn

**Motivation.** Post-turn, `callback_detector.detect` calls
`memory_store.list_recent(limit=10_000)` — copies the entire
in-memory mirror, sorts it twice, cosine-walks the candidates.
O(n) with n up to the 5000-row cap, on the brain-loop thread,
after **every** turn longer than 12 chars. Tens of ms today,
~100 ms+ as installs age.

**Key files.**
[`app/core/conversation/callback_detector.py`](../../app/core/conversation/callback_detector.py)
(`detect`),
[`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
(call site).

**Sketched approach.** Maintain a small pre-filtered candidate
index (callback-eligible kinds, salience floor, recency cap)
refreshed lazily on memory writes, so the per-turn walk touches
dozens of rows instead of thousands. Pairs naturally with P4's
`get_many` batch shape.

**Effort.** Medium.

---

## P18. Streaming accumulator rebuilds the full reply on every delta

**Motivation.** The stream loop does `accumulator.append(delta)`
then `full = "".join(accumulator)` *per token* — O(n²) total
work and allocation churn on long replies. The loop also re-runs
reaction-tag parsing on the rebuilt string each delta. Pure CPU
waste sitting between the LLM and TTS dispatch.

**Key files.**
[`app/core/session/turn_runner.py`](../../app/core/session/turn_runner.py)
(stream loop, ~577–646).

**Sketched approach.** Keep a single running `full += delta`
string (amortised linear in CPython) or an `io.StringIO`; make
the reaction-tag parse incremental (only re-attempt while
`len(full) < ~64` and the tag hasn't been resolved yet, since
`[[reaction:...]]` is contractually at the start).

**Effort.** Small.

---

## P19. RAG: one global lock + three sequential Lance searches

**Motivation.** All Lance reads and writes share one
`threading.Lock`, and `RagRetriever.retrieve` runs
`search_memories` → `search_messages` → `search_documents`
sequentially under it. Latencies add instead of max-ing, and the
turn thread serialises against `MessageIndexer` writes (a slow
embed + write on the indexer side stalls retrieval, and vice
versa).

**Key files.**
[`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py)
(`_search_table`, `add_message`, `_lock`),
[`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py)
(`retrieve`),
[`app/core/rag/message_indexer.py`](../../app/core/rag/message_indexer.py).

**Sketched approach.** Two independent wins: (a) parallelise the
three searches in a small shared thread pool; (b) narrow the lock
— embed outside it, use a reader-writer pattern so concurrent
reads don't queue behind each other (Lance datasets are safe for
concurrent reads). Do (a) first; it's contained in `retrieve`.

**Effort.** Medium.

---

## P20. Synchronous LLM compaction stalls the turn mid-flight

**Motivation.** When the projected context overflows,
`SummaryWorker.compact_now` runs a full summarisation LLM call
inline on the turn thread, then re-assembles the prompt — the
user sees a multi-second dead-air gap exactly on the turns that
are already heaviest. Rare, but the worst-case latency spike in
the whole system.

**Key files.**
[`app/core/session/turn_runner.py`](../../app/core/session/turn_runner.py)
(~485–503, compaction trigger),
[`app/core/proactive/summary_worker.py`](../../app/core/proactive/summary_worker.py)
(`compact_now`).

**Sketched approach.** On overflow, truncate history aggressively
for *this* turn only (drop oldest non-summary rows to fit) and
schedule the real compaction on the speaking-window scheduler so
the next turn gets the proper summary. Never run a worker LLM
call between user-send and first token. Watch for a *projected*
overflow threshold (e.g. 85% of budget) that triggers the async
compaction one turn early, making the inline fallback nearly
unreachable.

**Effort.** Medium.

---

## P21. K29 borderline gate runs a worker LLM call during prompt assembly

**Motivation.** When the opinion-injection heuristic returns
`borderline`, `_opinion_injection_llm_verdict` calls the worker
model synchronously inside `assemble_with_budget` — before any
streaming starts. When it fires, that's 0.5–8 s of added TTFT for
a one-line prompt cue. The rate limiter bounds frequency, not
per-fire latency.

**Key files.**
[`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)
(`_render_opinion_injection_block`,
`_opinion_injection_llm_verdict`).

**Sketched approach.** Move the borderline check post-turn: the
detector arms a *pending* verdict job; if the LLM confirms, the
cue renders on the NEXT turn (the stance hasn't changed in 30
seconds — one-turn lag is invisible). Hot path then only ever
pays the pure-Python heuristic. `opinion_injection_require_definite=true`
is the existing zero-code mitigation; this makes the borderline
path safe to keep on.

**Effort.** Small–medium.

---

## P22. Inner-life provider sweep: tiering + shared reads

**Motivation.** ~35 providers run inside `assemble_with_budget`
on every turn even on a slice-cache hit — each wrapped in a timed
phase, many doing their own SQLite reads or mirror walks. Two
providers (`_render_misattunement_block`,
`_render_self_noticing_block`) issue separate overlapping
`get_messages` queries for the same recent assistant rows within
one assembly. Individually cheap; collectively the per-turn floor
keeps creeping up with every new K-feature.

**Key files.**
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(provider loop),
[`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py).

**Sketched approach.** (a) Fetch the recent-history window once
per assembly and pass it into every provider that needs it;
(b) short-circuit disabled features before entering their timed
block; (c) classify providers "per-turn" vs "cacheable-for-N-
seconds" and skip the cacheable ones inside their TTL (most
trend/phase blocks change at minute-scale, not turn-scale). The
existing `provider_ms` telemetry makes the win measurable
per-provider.

**Effort.** Medium–large.

---

## P23. K28 turning-over provider triggers the full Lance scan on the hot path

**Motivation.** Sibling of P5, but worse placed: on
return-after-gap turns, `_render_turning_over_block` reaches
`list_recent_user_vectors`, which materialises the *entire* Lance
`messages` table via `to_arrow()` and filters in Python — a full
scan exactly on the "welcome back" turn, which is also the turn
that pays cold caches everywhere else. Fixing P5 fixes this; the
entry exists so the hot-path call site is on the map too.

**Key files.**
[`app/core/session/inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)
(`_render_turning_over_block`),
[`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py)
(`list_recent_user_vectors`).

**Sketched approach.** Same as P5 (predicate push-down + row cap
+ server-side order). Additionally consider reusing the K6
novelty ring (already warmed with recent user vectors) as the
candidate pool for K28's topical-similarity gate, dropping the
Lance call from this path entirely.

**Effort.** Small once P5 lands.

---

## P24. Voice latency batch: reaction-tag TTS gate, double STT pass, first-chunk threshold

**Motivation.** Three independent, individually-small voice-path
delays that compound into "she takes a beat too long to start
talking":

1. **Reaction-tag gate** — the stream loop only dispatches TTS
   chunks once `mood is not None`
   ([`turn_runner.py`](../../app/core/session/turn_runner.py)
   ~618). If the model leads with prose before
   `[[reaction:...]]`, *all* speech waits; the fallback only
   fires at stream end, flushing everything at once.
2. **Double STT pass** — `process_live_capture` re-transcribes
   the full WAV via `transcribe()` even when partial endpointing
   already produced a stable final text during capture
   ([`session_controller.py`](../../app/core/session/session_controller.py)
   ~7003, [`realtime_stt_service.py`](../../app/stt/realtime_stt_service.py)
   `transcribe`). 100–500 ms of pure re-work between "user
   stopped talking" and LLM start.
3. **First-chunk threshold** — `drain_tts_stream_chunks` holds
   the first sentence until ≥24 chars / ≥4 words
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

## P25. Client keeps playing scheduled audio after server-side TTS stop

**Motivation.** When the server stops TTS (stop command, future
barge-in), already-scheduled `AudioBufferSourceNode`s on the
client keep playing to the end of their buffers —
`AudioOutputManager.flush()` exists but is only called on
`dispose()`. Interrupt latency is therefore "whatever is already
scheduled", often 0.5–3 s. This also blocks the barge-in default
flip (immersion.md minor polish) from feeling right: server-side
barge-in without client flush still talks over the user.

**Key files.**
[`web/src/hooks/useAssistantSocket.ts`](../../web/src/hooks/useAssistantSocket.ts)
(`tts_state` handler — no flush call),
[`web/src/audio/AudioOutputManager.ts`](../../web/src/audio/AudioOutputManager.ts)
(`flush`),
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
(`stop_tts`).

**Sketched approach.** Emit an explicit `audio_flush` WS event
from `stop_tts` (or piggyback on `tts_state: stopped`); the
client handler calls `audioOutput.flush()`. Also flush on
voice-ownership takeover. Then flip `audio.barge_in_enabled`
default and validate the floor.

**Effort.** Small.

---

## P26. Lip-sync rides the server clock, not the playback clock

**Motivation.** Mouth animation is driven by server-paced
amplitude JSON (30 Hz throttle) + a network hop + the client's
first-clip idle margin + 150 ms smoothing — so the mouth runs a
noticeable, variable beat behind the audio the user actually
hears, and main-thread jank desyncs it further.

**Key files.**
[`app/tts/pocket_tts_service.py`](../../app/tts/pocket_tts_service.py)
(`_amplitude_pacer`),
[`web/src/audio/AudioOutputManager.ts`](../../web/src/audio/AudioOutputManager.ts),
[`web/src/live2d/channels/LipsyncChannel.ts`](../../web/src/live2d/channels/LipsyncChannel.ts).

**Sketched approach.** Derive amplitude client-side from an
`AnalyserNode` hanging off the `AudioOutputManager` output —
zero protocol change, perfectly aligned to playback by
construction, and the server pacer becomes voice-strip-meter-only.
Fallback option: timestamp amplitude frames server-side and have
`LipsyncChannel` align them to scheduled `startAt`.

**Effort.** Medium.

---

## P27. STT Whisper model loaded eagerly + unconditionally (biggest resident-RAM lever)

**Motivation.** The single largest in-process ML weight is the
STT model, and it is loaded for **every** install regardless of
whether voice is ever used. `SessionController.__init__`
constructs `RealtimeSttService(settings.stt, settings.audio)`
unconditionally (no `stt.enabled` gate exists — `SttSettings`
only has `model` + `language`), and the service's constructor
**synchronously and eagerly** builds the `AudioToTextRecorder`,
which loads the faster-whisper weights into the Python process
and holds them for the whole process lifetime — there is no
unload/release path (shutdown only calls `stop_context()`). The
shipped default is **`large-v1`** (~1.5B params), which measures
on the order of **~1.5–3 GB resident** depending on precision —
the dominant chunk of the observed ~6 GB `python.exe`. A
typed-only user (e.g. chat on a remote `gpt-5-mini` route, no
mic) pays this in full for nothing. NOTE: this is the #1 fix for
the RAM report — the embedder is HTTP→Ollama (negligible Python),
the in-memory memory mirror is ~20 MB at the 5000-row cap, and
the LLM context window lives in Ollama/OpenAI, not Python, so
none of those are the cause.

**Key files.**
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
(~L944, unconditional `RealtimeSttService` construct),
[`app/stt/realtime_stt_service.py`](../../app/stt/realtime_stt_service.py)
(~L56–74 eager load in `__init__`, `_create_recorder` ~L81–94),
[`app/core/infra/settings.py`](../../app/core/infra/settings.py)
(`SttSettings` ~L206–208 — add `enabled` + optional `device` /
`compute_type`),
[`config/default.json`](../../config/default.json) (`stt.model`
L233).

**Sketched approach.** Three independent wins, cheapest first:
(a) **zero-code lever today** — document `stt.model: "small"` /
`"base"` in `user.json` (drops ~1.5–2 GB immediately; quality is
fine for short companion utterances); (b) **lazy load** — defer
`AudioToTextRecorder` construction until the first voice
activation (first mic frame / Live-mode enable) instead of in
`__init__`, so typed-only sessions never pay it; (c) **idle
release** — add an `stt.enabled` flag and an unload path
(drop `self._recorder`, force GC) after N minutes with no voice
use, rebuilding on demand. (b) gives most of the benefit for the
common typed-first user. Also expose `compute_type` (faster-
whisper `int8` on CPU roughly halves the footprint vs fp16/fp32)
since Aiko currently passes neither `device` nor `compute_type`
and inherits library defaults.

**Open questions.** Does lazy-load add an unacceptable cold-start
delay on the first voice turn (large-v1 load is multi-second)? If
so, pair (b) with a background warm-load triggered when the
client reports mic permission / Live toggle, not at first frame.

**Effort.** Small (a/config), Small–medium (b/lazy), Medium
(c/idle-release + flag).

---

## P28. TTS engine + PyTorch load even when `tts.enabled=false`; never released

**Motivation.** `_build_tts_service` constructs
`PocketTtsService(settings.tts)` unconditionally — it does not
consult `settings.tts.enabled` — and the service's constructor
spawns a daemon load thread that pulls Pocket-TTS (~100M params)
plus the PyTorch CPU runtime into memory (~0.6–1 GB combined),
held for the process lifetime (`stop()` clears only the 8-entry
audio cache, not `self._model`). For a user who has disabled TTS
this is pure waste; even for a TTS user it's the second-largest
resident block and the place the shared PyTorch runtime first
gets paged in. Lower urgency than P27 (the model is genuinely
needed when TTS is on, which is the common case), but the
"loads even when disabled" path is a clear bug.

**Key files.**
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
(`_build_tts_service` ~L7472–7478 — gate on `settings.tts.enabled`),
[`app/tts/pocket_tts_service.py`](../../app/tts/pocket_tts_service.py)
(`__init__` load thread ~L236–237, `_load_model` ~L265–286,
`stop` ~L367–370 — add a model-release path).

**Sketched approach.** (a) Skip the load entirely when
`tts.enabled` is false (return a no-op engine, or defer the load
thread until the first enable); (b) add an explicit
`release_model()` so toggling TTS off at runtime frees the
weights; (c) investigate Pocket-TTS int8 quantization (~230 MB
vs ~450 MB baseline per upstream) as a config knob. (a) is the
quick correctness fix.

**Effort.** Small (a), Small (b), Medium (c — depends on upstream
quantization support).

---

## P29. No process-memory observability (RSS breakdown + the second python process)

**Motivation.** The RAM investigation that produced P27/P28 was
pure static code reading — there is no runtime surface that says
"STT is holding X, TTS Y, the mirror Z". `get_status` reports
model names and metrics but not resident memory. Diagnosing
"why is the server 6 GB?" should be one MCP call, not an
archaeology session. Separately, the reporter's Task-Manager
screenshot showed **three** `python.exe` under one tree
(~6.2 GB, ~946 MB, ~0.6 MB); the main process is understood
(STT+TTS+runtime) but the **~946 MB second process is
unidentified** — it could be a multiprocessing child, a
faster-whisper/CTranslate2 worker, or a stray spawn, and it's
~1 GB we can't currently account for.

**Key files.**
[`app/mcp/server.py`](../../app/mcp/server.py) (new
`get_memory_breakdown` debug tool),
[`app/web/__main__.py`](../../app/web/__main__.py) (process
spawn audit — identify what the second interpreter is),
AGENTS.md MCP tool table.

**Sketched approach.** Add an MCP `get_memory_breakdown` tool:
total RSS via `psutil.Process().memory_info().rss`, plus
best-effort per-subsystem attribution (STT loaded/unloaded +
model name, TTS loaded + model, memory-mirror row count ×
vector bytes, LanceDB on-disk size, embedder LRU size). For the
second process: enumerate `psutil.Process().children(recursive=
True)` with `cmdline()` so we can see whether it's ours and
what launched it (the MCP servers are `cmd /c npx` → node, not
python, so a python child is something else). Pairs with P6 /
P8 which already added per-subsystem stat tools.

**Effort.** Small (breakdown tool), Small (children enumeration).
