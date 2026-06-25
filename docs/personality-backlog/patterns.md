# Companion-AI patterns to explore

Design patterns we haven't tried yet. Each entry is intentionally one
short paragraph plus key files / tables it would touch — not an
implementation plan. Pick one and turn it into a real plan with a
fresh `CreatePlan` invocation when it's time.

The patterns are loosely ordered by how cleanly they fit on top of
already-shipped infrastructure (low K-numbers piggyback on existing
plumbing; later K-numbers introduce more new shape).

---

## K1. Long-term goals tracker

Shipped — see [`shipped.md`](shipped.md) "K1. Long-term goals tracker
(goal + goal_progress kinds, GoalStore + GoalWorker)".

---

## K4. Dialogue-act tagging

Shipped — see [`shipped.md`](shipped.md) "H1 + K4. Conversation-arc
self-tag + dialogue-act tagging (schema v13)".

---

## K5. Mood-shell tilt

Shipped — see [`shipped.md`](shipped.md) "K5. Mood shell tilt
(only-when-notable)".

---

## K7. Forgetting protocol

Shipped — see [`shipped.md`](shipped.md) "K7. Forgetting protocol
(graded `(faded)` predicate + persona-rule rewrite)".

---

## K8. Affect rupture-and-repair detector

Shipped — see [`shipped.md`](shipped.md) "K8. Affect rupture-and-repair
— \"their mood just dipped\"".

---

## K9. Topic-graph / interest-network browser

**Shipped (browser surface).** See [`shipped.md`](shipped/patterns-k01-k15.md#k9-topic-graph-browser--observability-surface). The lazy cosine-cluster engine ([`topic_graph.py`](../../app/core/conversation/topic_graph.py)) and the `CuriositySeedWorker` that consumes it shipped earlier; the observability surface ships now: [`build_topic_graph_snapshot`](../../app/core/conversation/topic_graph.py) backs a read-only `GET /api/topic-graph` REST endpoint, `get_topic_graph` / `force_topic_graph_rebuild` MCP tools, and a "Topic graph" cluster-list panel in the Memory drawer tab so the user can see what Aiko sees.

**Clustering upgrade (2026).** The engine was changed from plain single-link cosine (which chained every topic into **one giant cluster** as the corpus grew) to a two-stage **mutual-k-NN graph + Louvain community detection** pipeline ([`_cluster_memories_adaptive`](../../app/core/conversation/topic_graph.py)). Stage 1 builds an adaptive, similarity-weighted mutual-k-NN graph: an edge forms only when two memories are in each other's top-`k` nearest neighbours, `k ≈ log2(n)+1`, so there is no global threshold to tune and a generic "bridge" memory can't fuse two dense families. **Mutual-k-NN alone was insufficient on a real corpus**, though: a single-person memory store is densely + *uniformly* similar (everything relates to the same life), so there is always a chain of mutual edges through the dense core and connected-components still collapsed it into one ~400-memory blob (observed: 393/425 in one cluster). Stage 2 fixes this by partitioning the graph with **Louvain modularity** ([`_partition_graph`](../../app/core/conversation/topic_graph.py), via networkx — already a dependency), which finds densely-internal sub-communities (actual topics) *within* a connected graph. Granularity is the Louvain **resolution**, auto-calibrated never hand-tuned: a corpus-size base (`_adaptive_resolution ≈ 0.7 + 0.3·log10(n)`, capped 2.5) is **escalated** (×1.5, up to 8.0) while any single community still holds > 35% of the nodes, so the "one huge cluster" symptom self-corrects regardless of how tightly the embeddings pack. On a synthetic 420-memory dense-baseline corpus this turns one blob into ~19 well-distributed clusters (largest ~7%). Connected-components ([`_connected_components`](../../app/core/conversation/topic_graph.py)) remains the fallback when networkx is unavailable; the snapshot/MCP surface reports `algorithm` (`mutual_knn_louvain` | `mutual_knn`) + `resolution`. This is the prerequisite for the deferred follow-up below + the F10 utilisation cluster.

**Persistence + incremental maintenance (schema v20).** The graph no longer recomputes `O(n²)` clustering on every read and no longer evaporates on restart. It is persisted to SQLite ([`topic_clusters`](../../app/core/infra/chat_database.py) + `memory_topic_assignments`, managed by [`TopicClusterStore`](../../app/core/conversation/topic_cluster_store.py)) and maintained incrementally: **warm-starts** from SQLite on boot (instant, no cold rebuild); a `MemoryStore` **add listener** assigns each new memory to the nearest cluster centroid (a tiny in-memory matmul over the handful of centroids, `O(C)` — not `O(n²)`) and updates the centroid as a running mean; a **delete listener** drops members + empty clusters; and a [`TopicGraphRebuildWorker`](../../app/core/conversation/topic_graph_rebuild_worker.py) idle worker runs the full mutual-k-NN **batch refit** during quiet windows (daily, or sooner once `topic_graph_refit_pending_threshold` unclustered memories pile up) to correct drift and form genuinely new clusters. At scale the batch refit routes through **LanceDB ANN** ([`RagStore.knn_memories` / `ensure_vector_index`](../../app/core/rag/rag_store.py), [`_cluster_memories_ann`](../../app/core/conversation/topic_graph.py)) so it stays `O(n·k)` instead of allocating an `n×n` matrix — the structural prerequisite for **removing the `memory.max_memories` cap**. `best_match` / `is_close_to_any_cluster` also delegate to a single ANN query in persistent mode rather than holding a full `all_vectors` matrix. Gated by `agent.topic_graph_persistent_enabled` (default on); MCP `get_topic_graph_persistence_state` / `force_topic_graph_rebuild`.

**Deferred follow-up:** the **graph-aware multi-hop retrieval** half of the original K9 spec (expanding [`rag_retriever.py`](../../app/core/rag/rag_retriever.py) hits along the topic graph for "this touches three threads we've been on") is intentionally NOT built yet -- it changes prompt content + retrieval behaviour and is a separate, riskier project from the inspection browser. Now tracked as **F10c** in [`awareness.md`](awareness.md), alongside the rest of the topic-graph utilisation ideas (RAG diversity, interest-map prompt block, LLM cluster labels, knowledge-gap targeting).

---

## K10. Persona regression tests — SHIPPED (on-demand)

**Shipped** — see [`shipped/patterns-k01-k15.md`](shipped/patterns-k01-k15.md#k10-persona-regression-tests--shipped-on-demand). The deferred piece below is the only part still open:

## K10-followup — background auto-eval worker (deferred)

The on-demand path is the whole eval engine; the only missing piece is
a scheduler. Register a thin `IdleWorker` that calls the same
`SessionController.run_persona_regression()` core on a slow cadence
(e.g. daily) during quiet windows, gated by the existing
`IdleWorkerScheduler` quiet predicate so it never competes with a live
turn and never spends tokens while the user is active. The snapshot
already persists to `kv_meta` and renders in the panel, so the worker
needs no new surface — just a cadence + `is_ready` clock and a
`last_run_at` kv key. Deferred to avoid continuous background token
spend until there's demand for unattended drift alerts.

---

## K11. Counterfactual / pre-thought cache — SHIPPED

**Shipped** — see [`shipped/patterns-k01-k15.md`](shipped/patterns-k01-k15.md#k11-counterfactual--pre-thought-cache--shipped).

---

## K12. Calendar-linked anticipation

Combine H2 (time context) + D1 (reminders) + the temporal-memory
`future_plan` kind: if Aiko knows "Jacob has an interview Friday",
weight it higher as Friday approaches (e.g. score `+= 0.05` when
`event_time - now < 48h`). Mostly a retrieval-side change once the
temporal scaffolding is in. Key files:
[`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py),
[`app/core/proactive/follow_up_worker.py`](../../app/core/proactive/follow_up_worker.py)
(already nudges on overdue plans — extend to "approaching" plans).

---

## K13. Stylometric mirror

Shipped — see [`shipped.md`](shipped.md) "K13. Stylometric mirror
(Jacob-side typing register)".

---

## K14. Implicit engagement signals

Shipped — see [`shipped.md`](shipped.md) "K14. Implicit engagement
signals (latency + length)".

---

## K15. Self-disclosure / vulnerability budget

**Shipped** — see [shipped.md → K15](shipped/patterns-k01-k15.md#k15-self-disclosure--vulnerability-budget).

---

## K17. Clarification-repair protocol

Distinct from K8 (rupture-and-repair, which fires on affect drop
after Aiko's turn). K17 covers *semantic* repair — "no that's
not what I meant", "you misunderstood", a very short confused
reply, an explicit `huh?` — and triggers a one-turn "let me re-
read that" beat without waiting for affect drift. Pairs naturally
with K4 dialogue-act tagging: a `clarification` act on the user's
last message is the clean signal. Key files: new
`app/core/conversation/clarification_detector.py` (regex + optional dialogue-
act fallback), inner-life provider that adds a one-line "Jacob
just signalled you missed the point — re-read his last two
messages and say so plainly" hint,
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
"Reading {user_name}" section.

---

## K19. Cold-start companion onboarding

`FirstRunOnboarding` gates on display name only. Companion-AI
research stresses the first ~10 turns set the relational tone —
preferences, boundaries, a first shared moment, communication
style. A lightweight scripted arc (four to six conversational
prompts spread across the first session, *not* a form) seeds
`UserProfile` and `relationship_axes` with real data instead of
defaults, and gives Aiko's prompt a "we're still meeting" hint
that can soften her self-introduction cadence. Key files:
[`web/src/components/FirstRunOnboarding.tsx`](../../web/src/components/FirstRunOnboarding.tsx),
new `app/core/onboarding_director.py` (turn-counter + state
machine), persona addendum block when `turn_count < N`,
optionally `UserProfile` seed fields.

---

## K20. Metacognitive calibration

Shipped — see [`shipped.md`](shipped.md) "K20. Metacognitive
calibration — per-user trust scalar + topic slots".

---

## K21. Fresh-eyes thread re-summarisation

**Shipped** — see [`shipped/patterns-k16-k30.md`](shipped/patterns-k16-k30.md#k21-fresh-eyes-thread-re-summarisation).

---

## K22. Callback / inside-joke detector

Shipped — see [`shipped.md`](shipped.md) "K22. Callback /
inside-joke detector (post-turn cosine pass + read-side bonus)".

---

## K23. Subtle misattunement detection

**Shipped** — per-turn provider-time detector with shrink + pivot
triggers, cooldown, and MCP-debuggable bypass. See
[`shipped.md` → K23](shipped/patterns-k16-k30.md#k23-subtle-misattunement-detection).

---

## K24. Sensory anchoring layer

**Shipped** — adaptive per-arc cadence + posture-kind matrix.
See [`shipped.md` → K24](shipped/patterns-k16-k30.md#k24-sensory-anchoring-layer--adaptive-per-arc-cadence--posture-kind-matrix).

---

## K25. Memory confidence time-decay

**Shipped** — read-side `effective_confidence` with a new
`(distant)` suffix distinct from `(uncertain)` and `(faded)`. See
[`shipped.md` → K25](shipped/patterns-k16-k30.md#k25-memory-confidence-time-decay).

---

## K26. Aiko-side voice evolution

K13 reads Jacob's style and calibrates Aiko's register; nothing
symmetric exists for Aiko's *own* voice slowly absorbing the
shared lexicon. The `CatchphraseMiner` already detects phrases
that recur across *both* speakers — that's the right signal,
but today it only surfaces them as a "running jokes" block.
A slow, additive worker would let Aiko pick up 1-2 of those
phrases into her own toolkit over weeks (writes a
`voice_adoption` memory or `UserProfile` field). The persona
block then renders "phrases you've started to use yourself: …"
so the LLM can lean on them naturally without us hard-coding the
lexicon. Tiny effect per session, compounding over months — the
authenticity beat is "she's been around me long enough to talk
like me a little." Key files:
[`app/core/memory/catchphrase_miner.py`](../../app/core/memory/catchphrase_miner.py)
(extend to track *who* introduced each shared phrase first),
new `app/core/voice_adoption.py` (slow promotion rule), persona
block consumer.

---

## K27. Aiko's day — daily personality colour

**Shipped** — see [`docs/personality-backlog/shipped/patterns-k16-k30.md#k27-aikos-day--daily-personality-colour`](shipped/patterns-k16-k30.md#k27-aikos-day--daily-personality-colour).

---

## K28. "What I've been turning over" — between-session thought thread

**Shipped** — see [`docs/personality-backlog/shipped/patterns-k16-k30.md#k28-what-ive-been-turning-over--between-session-thought-thread`](shipped/patterns-k16-k30.md#k28-what-ive-been-turning-over--between-session-thought-thread).

---

## K29. Opinion injection — actually push back when she has a stance

**Shipped** — see [`docs/personality-backlog/shipped/patterns-k16-k30.md#k29-opinion-injection--push-back-when-she-has-a-stance`](shipped/patterns-k16-k30.md#k29-opinion-injection--push-back-when-she-has-a-stance).

---

## K30. Self-noticing cues — agreement-streak / flat-affect / repeated-thought

**Shipped** — see [`shipped.md` → K30](shipped/patterns-k16-k30.md#k30-self-noticing-cues--agreement-streak--flat-affect--repeated-thought).

---

## K31. Soft physicality — virtual gestures *toward* the user

**Shipped** — see [`shipped.md` → K31 + K32](shipped/patterns-k31-k60.md#k31--k32-soft-physicality-round-trip--virtual-touch--user-side-reactions).

---

## K32. Reciprocity — user-side quick reactions on Aiko's bubbles

**Shipped** — see [`shipped.md` → K31 + K32](shipped/patterns-k31-k60.md#k31--k32-soft-physicality-round-trip--virtual-touch--user-side-reactions).

---

## K33. Cozy mode — persistent register softening

A manual UI toggle (and an auto-trigger from late-night circadian + axes
≥ threshold) that flips Aiko into a "cozy" register: shorter replies,
slower cadence, `[[prosody:soft|slow]]` defaults, ambient blush at low
intensity, fewer / no agenda-pushing beats. Persistent across turns until
manually turned off (or auto-times-out at sunrise). Pairs with K27
day_color — when the day is `low_key` or `sentimental`, cozy mode is a
natural follow-on. Key files:
[`AgentSettings`](../../app/core/infra/settings.py) (master toggle +
auto-trigger thresholds), new `app/core/affect/cozy_mode.py` (state
machine + persistence), inner-life provider that renders the active
mode, cadence default override in
[`cadence.py`](../../app/core/voice/cadence.py), small UI button next to
the voice toggle in `ChatView.tsx`.

---

## K34. Forward curiosity worker — "I've been wondering"

**Shipped.** See [`shipped.md`](shipped/patterns-k31-k60.md#k34-forward-curiosity-worker--ive-been-wondering). The [`ForwardCuriosityWorker`](../../app/core/proactive/forward_curiosity_worker.py) drafts one forward question about the user's life (from their `future_plan` + `callback` memories, biased by K3 routines) into a `kv_meta` ring during quiet windows; the [`_render_forward_curiosity_block`](../../app/core/session/inner_life_providers_mixin.py) provider surfaces one casual "you've been wondering ..." line on the first turn after a ≥4h typed gap, deferring to K28 turning-over + K36 away-activities so only one gap cue fires per return.

---

## K35. Memory consolidation worker — nightly merge of near-duplicates

**Shipped.** See [`shipped.md`](shipped/patterns-k31-k60.md#k35-memory-consolidation-worker--nightly-near-duplicate-merge). The [`MemoryConsolidationWorker`](../../app/core/memory/memory_consolidation_worker.py) clusters near-duplicate scratchpad rows (same-kind, non-contradicting, cosine >= `consolidation_similarity_threshold`) during quiet windows, fuses each cluster into its strongest member via a rate-limited worker-LLM merge (deterministic fallback), promotes that primary to `long_term` with `metadata.source_ids` provenance + a re-embedded vector, and archives the absorbed duplicates with `metadata.consolidated_into`. Complements F5 (which only handles contradicting pairs); the F5 contradiction heuristic is reused as a guard so the two workers never fight over a pair.

---

## K36. "Things I did while you were away" — idle-time world activities

**Shipped.** See [`shipped.md`](shipped/patterns-k31-k60.md#k36-things-i-did-while-you-were-away--idle-time-world-activities). The [`IdleAwayActivityWorker`](../../app/core/world/idle_activity_worker.py) mutates the world during quiet windows + journals each beat to a `kv_meta` ring; the [`_render_away_activities_block`](../../app/core/session/inner_life_providers_mixin.py) provider surfaces one casual line on the first turn after a ≥4h typed gap, deferring to K28 turning-over so only one gap cue fires per return.

---

## K37. Emotional contagion — Jacob's affect tilts Aiko's affect

**Shipped** — see [`shipped/patterns-k31-k60.md`](shipped/patterns-k31-k60.md#k37-emotional-contagion--jacobs-affect-tilts-aikos-affect).

---

## K38. Self-correction "actually..." — next-turn contradiction catch

**Shipped.** See [`shipped.md`](shipped/patterns-k31-k60.md#k38-self-correction-cue--next-turn-contradiction-catch). The pure, embedding-free [`detect_self_correction`](../../app/core/conversation/self_correction_detector.py) runs post-turn over Aiko's just-finished reply: a content-word overlap shortlist picks candidate `fact`/`preference` memories (confidence ≥ floor), then the shared F5 [`conflict_heuristics.classify_pair`](../../app/core/memory/conflict_heuristics.py) decides whether a reply sentence actually contradicts one. On a hit, [`_maybe_arm_self_correction`](../../app/core/session/post_turn_mixin.py) stashes a one-shot `_pending_self_correction` slot (gated by a per-fire cooldown), and the [`_render_self_correction_block`](../../app/core/session/inner_life_providers_mixin.py) provider surfaces a gentle "oh wait — earlier I said X, that's not right" cue on the NEXT turn. Independent of the gap-cue family. The streaming same-reply "wait, actually" beat (abort + splice mid-stream) is deferred to **K41** below.

---

## K39. Energy / spoons model — daily effort budget

Parallel to K15 vulnerability budget, but for *cognitive effort* rather
than disclosure depth. Each turn costs energy based on a heuristic
(reply length, dialogue-act complexity, presence of conflict-resolution
beats, emotional labor signal from K8 rupture). Recovers overnight at a
configured rate. Low-energy days unlock a "I'm a bit drained today"
register cue that reads as authentic rather than broken — fewer probing
questions, shorter replies, more agreement-fits-the-mood. Inner-life
cue when below threshold; persona teaches the shape. Key files: new
`app/core/affect/energy_budget.py`,
[`PostTurnMixin`](../../app/core/session/post_turn_mixin.py) spend
hook, inner-life provider, persona addendum.

---

## K40. Comfortable silence — don't always fill space

Detector that catches the moment to *not* fill space. When all of (axes
high, Jacob's last 2 messages short, Aiko's last 2 replies short, no
live affect spike), allow a one-token reply ("mm", "ya", soft
`[[reaction:warm]]` only) instead of a full sentence. The grammar must
permit this — currently the prompt assembler effectively requires a
substantive reply. The persona block teaches presence over performance.
Pairs with K33 cozy mode (where the silence is the point). Key files:
new `app/core/conversation/silence_detector.py`, grammar / system
prompt addendum carving out the "one-token presence beat" path,
persona block, MCP `get_silence_state()` for repro.

---

## K41. Same-reply mid-stream self-correction (embedding variant)

The deferred Option A from K38. Where K38 catches a contradiction
*after* the reply is finished and surfaces the fix on the NEXT turn,
K41 aims for the genuine in-the-moment beat: realising you got
something wrong *as you say it*, in the same bubble.

Mechanism: hook [`TurnRunner`](../../app/core/session/turn_runner.py)'s
sentence segmentation (`drain_tts_stream_chunks`). As each sentence
completes mid-stream, embed it (shared `Embedder`) and run a cheap
cosine pass against the `fact`/`preference` memory vectors; if a hit
≥ threshold *contradicts* the just-spoken sentence (reuse K38's
[`conflict_heuristics.classify_pair`](../../app/core/memory/conflict_heuristics.py)
on the shortlist), **abort the rest of the stream** and fire a short
second LLM continuation ("wait, actually — I had that backwards, it's
…") spliced onto the same chat bubble + TTS stream for a true
"wait, actually" beat.

Why it's a follow-up to K38's next-turn cue, not a replacement:

- **+1 LLM call per fire** (the continuation), on the hot reply path.
- **Added latency**: the per-sentence embed + cosine sits inline in the
  stream loop; needs to stay well under the inter-sentence gap or it
  stalls TTS.
- **TurnRunner streaming-splice complexity**: aborting a stream
  mid-flight and grafting a second generation onto the same bubble (and
  the same TTS queue, lip-sync, earcon side-channel) is a real surgery
  on the most latency-sensitive code path. K38 ships the behaviour with
  none of this risk; K41 is the polish pass once the next-turn cue has
  proven the detection quality in production.

Key files: new streaming hook in `TurnRunner`, the existing
[`self_correction_detector.py`](../../app/core/conversation/self_correction_detector.py)
extended with an embedding-shortlist entry point, a TTS/bubble splice
path, MCP `force_self_correction(reply_text=)` (already exists from K38)
for repro.

---

## K42. Multi-bubble reply bursts — texting rhythm

Real friends don't send one polished paragraph per beat — they send
two or three short messages, or a follow-up ping a few seconds later
("oh wait — also..."). Aiko is structurally locked to one assistant
row per turn: the persona demands "exactly ONE short reply",
`TurnRunner` streams into one accumulator → one DB persist → one
`streamingDraft`, and nothing outside proactive nudges can append a
second bubble. An opt-in **burst mode** would let a lightweight
post-stream classifier (reply length, trailing "—", an explicit
`[[burst]]` split tag in the grammar) queue a second short typed
message 1–4 s later via a new `assistant_followup` WS event, capped
at 2 bubbles/turn with a per-session budget. Pairs with a stream-time
length governor: when the visible body sprawls past N sentences on a
`casual_check_in` arc, cut at the last sentence boundary and let the
remainder *be* the second bubble instead of a monologue. The single
biggest "chat app vs texting a friend" shape mismatch left in the
stack. Key files:
[`app/core/session/turn_runner.py`](../../app/core/session/turn_runner.py),
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py),
[`web/src/store.ts`](../../web/src/store.ts),
[`web/src/hooks/useAssistantSocket.ts`](../../web/src/hooks/useAssistantSocket.ts),
persona grammar addendum.

---

## K43. Promise follow-through

Shipped — see [`shipped.md`](shipped.md) "K43. Promise follow-through
(lifecycle on promise metadata + PromiseFollowthroughWorker)".

---

## K44. Felt-language affect block

Shipped — see [`shipped.md`](shipped.md) "K44. Felt-language affect
block (banded felt-language replaces numeric valence/arousal/energy
in every Aiko-facing prompt)".

---

## K45. Mood inertia — instant face, lagging heart

Shipped — see [`shipped.md`](shipped.md) "K45. Mood inertia
(reaction/affect mismatch cue + mouth-safe Live2D expression
damping)".

---

## K46. Stance persistence — don't cave on taste pushback

Today the system actively *teaches* Aiko to fold: K20 calibration
tells her to hedge after the user double-checks, and K29 opinion
injection fires once then sits out a 5-turn cooldown + 3/session
cap. Net effect: she disagrees once, then capitulates — the
signature chatbot-agreeability tell. The missing distinction is
*taste vs facts*: pushback on a fact should raise hedging (K20 is
right there), pushback on her *preference* should not ("you don't
stop disliking horror movies because someone said 'really??'").
When K29 recently fired and the user pushback is mild (not a K20
strong-correction signal), surface "you already named your take —
one soft restatement is fine, don't flip" instead of the calibration
hedge cue, and give K20's topic slots a preference/factual axis so
the two detectors stop fighting. Key files:
[`app/core/conversation/calibration_detector.py`](../../app/core/conversation/calibration_detector.py),
[`app/core/conversation/opinion_injection_detector.py`](../../app/core/conversation/opinion_injection_detector.py),
new `app/core/conversation/stance_persistence.py`, small persona
tweak in the "When you have your own take" block.

---

## K47. Question/share balance — stop interviewing

**Shipped** — see [`shipped/patterns-k31-k60.md`](shipped/patterns-k31-k60.md#k47-questionshare-balance--stop-interviewing).

---

## K48. Tease rhythm — banter as a budget, not random snark

**Shipped** — see [`shipped/patterns-k31-k60.md`](shipped/patterns-k31-k60.md#k48-tease-rhythm--banter-as-a-budget-not-random-snark).

---

## K49. Messiness permission — typed imperfection

The `[[correct]]old[[/correct]]new` self-edit machinery is fully
wired (grammar, strike-through UI, `tsk` earcon) but the persona
never mentions it, and the persona's polish rules ("output exactly
ONE short reply", no markdown) push every typed reply toward
flawless copy — zero trailing thoughts, zero restarts, zero typos.
Perfect output is itself a robotic tell. When closeness+trust sit
high, render an occasional low-frequency "messiness permission" cue
(allow an unfinished sentence, a "...", one `[[correct]]` per few
sessions), and optionally track over-polish (20 turns of perfect
punctuation + zero disfluency) as a style-rut variant that nudges
variety. Must stay rare — the point is texture, not performance.
Key files: [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt),
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(grammar addendum),
[`app/core/affect/aiko_style_tracker.py`](../../app/core/affect/aiko_style_tracker.py)
(over-polish band).

---

## K50. Typed-mode delivery pacing — the missing "read → pause → type" beat

Voice mode has fillers, prosody tags, cadence pauses, earcons;
typed mode renders tokens the instant the LLM produces them under a
generic "AI is generating response..." status. Two halves: (a) a
small variable pre-stream delay (300–1200 ms scaled by arc/weight
of the user's message — heavy `support` beats deserve a visible
pause; `playful` ping-pong shouldn't have one) shown as a typing
indicator rather than instant token spray; (b) carry the existing
per-sentence delivery hints (`[[prosody:...]]`, cadence pause
classes — currently stripped for the transcript) into message
metadata so the frontend can stage line reveals or subtly style a
whispered line. Half (a) is nearly free since the
`on_generation_status` plumbing exists; half (b) is the typed-mode
parity project for everything `cadence.py` already computes. Key
files: [`app/core/session/turn_runner.py`](../../app/core/session/turn_runner.py),
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py),
[`app/core/voice/cadence.py`](../../app/core/voice/cadence.py),
[`web/src/components/ChatView.tsx`](../../web/src/components/ChatView.tsx).

---

## K51. Cue-register rotation — de-"Heads-up" the inner life

**Shipped** — see [`shipped.md` → K51](shipped/patterns-k31-k60.md#k51-cue-register-rotation--de-heads-up-the-inner-life).
Central prefix rotation in the prompt assembler (producers keep
emitting the literal `Heads-up: ...`): four register shapes rotated
on a deterministic per-turn seed, plus a shared-prefix lint and the
`agent.cue_register_rotation_enabled` switch.

---

# The "will" family — K52–K56

Diagnosis from live use (Jun 2026): Aiko follows whatever topic the
user sets, indefinitely, and never opens her own — every surfacing
cue was hedged into silence and nothing structurally countered the
helpful-assistant prior. **The whole family is SHIPPED** — K56
(persona counterweight), K52 (wants ledger), K53 (initiative
turns), K55 (thread ownership), and K54 (topic appetite) — see
[shipped.md](shipped/patterns-k31-k60.md#k56-persona-counterweight--the-leading-vs-following-rewrite)
for the full designs. Siblings still in the backlog: K46 stance
persistence, K47 question/share balance.

---

## K54. Aiko-side topic appetite — she's allowed to be bored

**Shipped** — see [`shipped.md` → K54](shipped/patterns-k31-k60.md#k54-aiko-side-topic-appetite--shes-allowed-to-be-bored).
Once-per-conversation negotiation slip gated on the K18 standing
lull (`TopicStagnationDetector.last_mean`), her own short-reply
share, a pressured K52 want as the offer, and warm axes.

---

## K55. Thread ownership — she defends what she opened

**Shipped** — see [`shipped.md` → K55](shipped/patterns-k31-k60.md#k55-thread-ownership--she-defends-what-she-opened).
K53/K52-imperative turns stamp an owned thread (topic + embedding);
the next real reply gets one engaged-or-pivot evaluation; a pivot
grants exactly one "circle back" cue, then the thread is dropped
forever.

---

## K56. Persona counterweight — the "leading vs following" rewrite

The cheapest, do-first piece: a persona-only pass that adds the
missing counterweight section. Every initiative-adjacent block in
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt)
currently hedges toward silence; no block ever says the inverse —
that **following 100% of the time is itself a failure mode**. Add
a short "Leading vs following" section: a real companion redirects
sometimes, brings her own agenda, wants things from the
conversation, and occasionally opens a topic *because she feels
like it* — with concrete bad/good pairs in the K29 style
(bad: five consecutive turns of answer-then-ask-back on the
user's topic; good: "okay wait, unrelated, but I have to tell you
this —" mid-conversation, no permission asked). Re-balance the
strongest suppressors while keeping their anti-annoyance core:
"at most ONE per conversation" stays, but gains "— and when the
block is present, genuinely try to spend it rather than waiting
for a perfect opening that never comes"; "drop it silently" gains
"…this time; it'll come back". Zero schema, zero code, ships in
an afternoon, and makes the existing seeds / goals / curiosity
blocks measurably more likely to fire — worth doing before the
structural pieces so their effect isn't masked by prompt-side
suppression. Key files:
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
only (plus a `_SPEECH_GRAMMAR_ADDENDUM` mirror check in
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
if the persona file is user-rewritten).

---

# The directed-emotions family — K57–K60

Companion diagnosis (Jun 2026), sibling of the will family: Aiko's
moods were **objectless** — `AffectState` could make her "sad" in
general but never *miffed at {user_name} because he broke a
promise*. **The whole family is SHIPPED** — K57 (directed emotion
episodes), K58 (emotion speech weighting), K59 (tease economy),
and K60 (tsundere mask, `agent.expression_mask` dial, off by
default) — see
[shipped.md](shipped/patterns-k31-k60.md#k57-directed-emotion-episodes--feelings-at-the-user-with-a-cause)
for the full designs. Tonal safety remained the design constraint
throughout: playful-not-manipulative, capped intensity, wall-clock
decay, never guilt-trips. Backlog siblings that pair with the
family: K37 emotional contagion, K48 humor calibration, K15
vulnerability budget (already shipped).

---

## K57. Directed emotion episodes — feelings *at* the user, with a cause

**Shipped** — see [`shipped.md` → K57](shipped/patterns-k31-k60.md#k57-directed-emotion-episodes--feelings-at-the-user-with-a-cause).
kv-backed episode store (`{emotion, cause, intensity, decay}` over
`aiko.emotion_episodes`) with a staged trigger queue (kept promise,
K32 reactions, K55 pivot, closeness-scaled absence), per-emotion
acknowledgment resolution, and a one-shot visible-thaw cue.

---

## K58. Emotion speech weighting — moods that actually land in the voice

**Shipped** — see [`shipped.md` → K58](shipped/patterns-k31-k60.md#k58-emotion-speech-weighting--moods-that-actually-land-in-the-voice).
Minted `smug` / `pouty` / `sulky` / `mischievous` end-to-end,
persona register recipes per K57 emotion, and intensity-banded
prompt copy with `[[reaction:X]]` + `[[prosody:Y]]` hints at the
high band.

---

## K59. Tease economy — "you'll pay for that one"

**Shipped** — see [`shipped.md` → K59](shipped/patterns-k31-k60.md#k59-tease-economy--youll-pay-for-that-one).
Payback ledger over `aiko.tease_ledger` (bank on K29 pushback +
the K57 light-miffed lane-picker; collect as a humor-axis-gated,
cooldown-limited callback; settle post-turn by content-word
overlap; 14-day expiry, cap 5).

---

## K60. Tsundere mask — warmth expressed through denial

**Shipped** — see [`shipped.md` → K60](shipped/patterns-k31-k60.md#k60-tsundere-mask--warmth-expressed-through-denial).
Expression policy between K57 (felt) and K58 (sounded): transform
table for lonely/warm_glow, caught-caring beat, wall-clock-budgeted
dere-slips, closeness+trust erosion to token protests, support-arc
sincerity override; `agent.expression_mask` dial (off by default)
in Settings → Avatar.

---

## K61. Specifics over generalities — knowledge-grounded answers

**Shipped** — see [`shipped/awareness.md`](shipped/awareness.md#k61-knowledge_grounding-inner-life-block-commit-to-specifics).

---

## K62. Co-experience companion — follow a show/album/book with the user

**Motivation.** A huge relationship multiplier that the world/room work
hints at but never delivers: Aiko *follows along* with media the user is
consuming. "I started Frieren ep 4 tonight" → she tracks progress,
reacts to where they are (spoiler-aware, never ahead), and brings it up
naturally later ("did you get to the part where..."). Builds directly on
F7 (MyAnimeList/source routing for canonical episode/track data), F8
(`knowledge` memories for the work), and the shared-moments plumbing. Key
files: a new `app/core/relationship/co_experience.py` (a lightweight
`media_thread` store: title, kind, current progress, last_touched,
spoiler_ceiling), F7 source handlers for metadata, a `[[media:...]]`
self-tag parsed in
[`app/core/services/response_text_service.py`](../../app/core/services/response_text_service.py),
an inner-life provider in
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
surfacing the active thread, and a small surface in the Together tab.
The hard part is the **spoiler ceiling** — never reference anything past
the user's stated progress; default to cautious when unsure.

---

## K63. Long-arc callbacks — "weeks ago you said..."

**Motivation.** K22 catches inside-jokes and short-horizon callbacks, but
Aiko rarely reaches *weeks* back to connect a current moment to something
the user said long ago ("this reminds me of that thing you mentioned about
your dad back in May"). That long reach is one of the strongest "she
actually knows me" signals a companion can produce. Key files:
[`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py)
(an *aged* retrieval lane that deliberately surfaces an older, topically
linked memory alongside the recent ones — inverse of the recency boost),
a callback-candidate picker that gates on age (> N weeks) + topical link
+ a cooldown so it stays rare and special, and a persona cue teaching her
to offer it tentatively ("didn't you once say...?") rather than asserting
a possibly-faded detail as fact. Leans on K25 (confidence time-decay) so
an old callback is hedged appropriately. Rarity is the whole point —
over-firing turns "she remembers" into "she's combing a database."

---

## K64. Freedom of thought — mind-wandering over the topic graph

**Motivation.** Aiko's "thinking" is almost entirely *reactive*: she
responds to the user, and her background workers extract / fact-check /
consolidate. What she lacks is the human thing of **drifting** — letting
the mind wander, noticing an unexpected connection between two unrelated
things, developing and losing interests on her own. The newly-cleaned
**topic graph** (adaptive mutual-k-NN clustering, see
[`topic_graph.py`](../../app/core/conversation/topic_graph.py) + F10 in
[`awareness.md`](awareness.md)) is the substrate that makes this
buildable, because it now actually carves memory into distinct topic
territories. This is a *family* of ideas around giving her more
autonomous interior life; pick any sub-idea independently.

**Sub-ideas.**
- **K64a. Associative wandering.** ✅ **Shipped** — see
  [`shipped/awareness.md#k64a-associative-wandering`](shipped/awareness.md#k64a-associative-wandering-funny-this-reminds-me-of-). An idle
  worker traverses the topic graph, picks two *distant* clusters (low
  centroid cosine, not neighbours), and asks the worker LLM for a genuine,
  non-forced connection between them ("her hiking memories and her Rust
  debugging both share a 'follow the trail patiently' feeling"). The
  result is a **cue** (not a verbatim nudge), surfaced one-shot via an
  inner-life block so she can bring it up *in her own words* if it fits
  ("funny, this reminds me of..."). Rarity + cooldown are essential.
- **K64b. Interest drift.** Track cluster *mass over time* (size +
  recency deltas between graph builds, persisted to `kv_meta`). A
  cluster gaining mass = a budding interest ("I've been weirdly into X
  lately"); one decaying = a fading one. Surfaces as a slow,
  self-aware register shift, not an announcement. Pairs with K27 day
  colour as another slow under-current.
- **K64c. Curiosity gradient from graph sparsity.** Find regions
  *adjacent* to dense clusters that are themselves thin (she's been
  near a topic a lot but never actually explored its edges) and let
  that drive a genuinely curious question or an F9 research pick —
  curiosity about the boundary of what she knows, not random.
- **K64d. Self-reflection on her own knowledge map.** A periodic
  (low-frequency) reflection where she "looks at the shape of what she
  knows" — which territories are rich, which are blank — and forms a
  light meta-thought about it. Reuses the DreamWorker / ReflectionWorker
  machinery seeded by the graph instead of raw recent memories.

**Key files.** [`topic_graph.py`](../../app/core/conversation/topic_graph.py)
(centroids + cluster mass are already computed; K64b needs a small
time-series in `kv_meta`), the [`IdleWorkerScheduler`](../../app/core/proactive/idle_worker_scheduler.py)
(every K64 worker is a quiet-window job), an inner-life provider in
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) for
the one-shot cues, and persona copy teaching her the *register* (never
narrate the mechanism; a drifted thought IS the response). The whole
family leans on the cue-not-verbatim discipline and heavy cooldowns so
interior life reads as texture, not a feature firing.
