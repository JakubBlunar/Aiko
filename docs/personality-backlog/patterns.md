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

Build a lightweight cosine-cluster graph of memories so Aiko can
notice "this topic touches three threads we've been on" and surface
connections naturally. Reuses the embeddings already in
[`app/core/rag/rag_store.py`](../../app/core/rag/rag_store.py); the graph
itself is computed lazily and cached. UI side: a minimal "topic
graph" sub-tab in the Memory tab that visualises clusters so the user
can see what Aiko sees. Key files: new
`app/core/conversation/topic_graph.py`, [`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py)
(graph-aware multi-hop retrieval), Memory tab.

---

## K10. Persona regression tests

Periodic golden-prompt evals: a small fixture file
(`data/persona/golden_turns.jsonl`) with a handful of canonical
prompts + expected style markers (tone words, presence of specific
self-tags, no forbidden phrases). A worker compares replies to the
markers and flags drift. Catches the persona quietly drifting from
the sheet via prompt rot or memory contamination. Key files: new
`app/core/persona_regression_worker.py`, fixture file under
`data/persona/`, alert path that surfaces drift in the diagnostics
tab.

---

## K11. Counterfactual / pre-thought cache

G3 covers factual `open_question`s. A natural cousin is "what would I
say if Jacob asked me X" — Aiko occasionally drafts a reply to a
hypothetical and caches it in scratchpad memory, smoothing future
first responses without needing web access. Bounded queue, cheap LLM
call, scratchpad-tier so it ages out naturally if unused. Key files:
new `app/core/counterfactual_worker.py`,
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py) (new
`pre_thought` kind, scratchpad tier).

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

**Shipped** — see [shipped.md → K15](shipped.md#k15-self-disclosure--vulnerability-budget).

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

Compaction compresses history when context overflows; it doesn't
periodically re-synthesise "what this ongoing thread is *about*
now" for Aiko's inner voice. After ~50 turns or daily (whichever
comes first), an idle worker would draft a 3-sentence "current
state of this thread" note pinned to the session, separate from
the rolling summary. Improves long-thread coherence without
paying a per-turn token cost, and gives Aiko a clean place to
reset her read of where the conversation has actually gone. Key
files: new `app/core/thread_resummary_worker.py` adjacent to the
shipped `SummaryService` / `NarrativeWeaver`, inner-life provider
that prefers the fresh-eyes note over the rolling summary when
present, schema addition (a `thread_resummary` row per session or
a metadata field on the latest summary).

---

## K22. Callback / inside-joke detector

Shipped — see [`shipped.md`](shipped.md) "K22. Callback /
inside-joke detector (post-turn cosine pass + read-side bonus)".

---

## K23. Subtle misattunement detection

**Shipped** — per-turn provider-time detector with shrink + pivot
triggers, cooldown, and MCP-debuggable bypass. See
[`shipped.md` → K23](shipped.md#k23-subtle-misattunement-detection).

---

## K24. Sensory anchoring layer

**Shipped** — adaptive per-arc cadence + posture-kind matrix.
See [`shipped.md` → K24](shipped.md#k24-sensory-anchoring-layer--adaptive-per-arc-cadence--posture-kind-matrix).

---

## K25. Memory confidence time-decay

**Shipped** — read-side `effective_confidence` with a new
`(distant)` suffix distinct from `(uncertain)` and `(faded)`. See
[`shipped.md` → K25](shipped.md#k25-memory-confidence-time-decay).

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

**Shipped** — see [`docs/personality-backlog/shipped.md#k27-aikos-day--daily-personality-colour`](shipped.md#k27-aikos-day--daily-personality-colour).

---

## K28. "What I've been turning over" — between-session thought thread

**Shipped** — see [`docs/personality-backlog/shipped.md#k28-what-ive-been-turning-over-between-session-thought-thread`](shipped.md#k28-what-ive-been-turning-over-between-session-thought-thread).

---

## K29. Opinion injection — actually push back when she has a stance

**Shipped** — see [`docs/personality-backlog/shipped.md#k29-opinion-injection`](shipped.md#k29-opinion-injection-push-back-when-she-has-a-stance).

---

## K30. Self-noticing cues — agreement-streak / flat-affect / repeated-thought

**Shipped** — see [`shipped.md` → K30](shipped.md#k30-self-noticing-cues--agreement-streak--flat-affect--repeated-thought).

---

## K31. Soft physicality — virtual gestures *toward* the user

**Shipped** — see [`shipped.md` → K31 + K32](shipped.md#k31--k32-soft-physicality-round-trip--virtual-touch--user-side-reactions).

---

## K32. Reciprocity — user-side quick reactions on Aiko's bubbles

**Shipped** — see [`shipped.md` → K31 + K32](shipped.md#k31--k32-soft-physicality-round-trip--virtual-touch--user-side-reactions).

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

**Shipped.** See [`shipped.md`](shipped.md#k34-forward-curiosity-worker--ive-been-wondering). The [`ForwardCuriosityWorker`](../../app/core/proactive/forward_curiosity_worker.py) drafts one forward question about the user's life (from their `future_plan` + `callback` memories, biased by K3 routines) into a `kv_meta` ring during quiet windows; the [`_render_forward_curiosity_block`](../../app/core/session/inner_life_providers_mixin.py) provider surfaces one casual "you've been wondering ..." line on the first turn after a ≥4h typed gap, deferring to K28 turning-over + K36 away-activities so only one gap cue fires per return.

---

## K35. Memory consolidation worker — nightly merge of near-duplicates

**Shipped.** See [`shipped.md`](shipped.md#k35-memory-consolidation-worker--nightly-near-duplicate-merge). The [`MemoryConsolidationWorker`](../../app/core/memory/memory_consolidation_worker.py) clusters near-duplicate scratchpad rows (same-kind, non-contradicting, cosine >= `consolidation_similarity_threshold`) during quiet windows, fuses each cluster into its strongest member via a rate-limited worker-LLM merge (deterministic fallback), promotes that primary to `long_term` with `metadata.source_ids` provenance + a re-embedded vector, and archives the absorbed duplicates with `metadata.consolidated_into`. Complements F5 (which only handles contradicting pairs); the F5 contradiction heuristic is reused as a guard so the two workers never fight over a pair.

---

## K36. "Things I did while you were away" — idle-time world activities

**Shipped.** See [`shipped.md`](shipped.md#k36-things-i-did-while-you-were-away--idle-time-world-activities). The [`IdleAwayActivityWorker`](../../app/core/world/idle_activity_worker.py) mutates the world during quiet windows + journals each beat to a `kv_meta` ring; the [`_render_away_activities_block`](../../app/core/session/inner_life_providers_mixin.py) provider surfaces one casual line on the first turn after a ≥4h typed gap, deferring to K28 turning-over so only one gap cue fires per return.

---

## K37. Emotional contagion — Jacob's affect tilts Aiko's affect

Today `AffectUpdater` only reacts to Aiko's own emitted
`[[reaction:...]]`. When Jacob's affect swings strongly (from K14
implicit-engagement signals, the vocal-tone block, or dialogue-act
sentiment), Aiko's affect should tilt a small amount toward his
(~0.05/turn, capped). The residual reads as "I'm picking up on him"
without explicit narration. Persona block teaches her to register
without performing it. Key files:
[`app/core/affect/affect_updater.py`](../../app/core/affect/affect_updater.py)
(new `_apply_user_contagion` pass), settings knob
(`agent.contagion_strength`, `_max_per_turn`), persona addendum.

---

## K38. Self-correction "actually..." — next-turn contradiction catch

**Shipped.** See [`shipped.md`](shipped.md#k38-self-correction-cue--next-turn-contradiction-catch). The pure, embedding-free [`detect_self_correction`](../../app/core/conversation/self_correction_detector.py) runs post-turn over Aiko's just-finished reply: a content-word overlap shortlist picks candidate `fact`/`preference` memories (confidence ≥ floor), then the shared F5 [`conflict_heuristics.classify_pair`](../../app/core/memory/conflict_heuristics.py) decides whether a reply sentence actually contradicts one. On a hit, [`_maybe_arm_self_correction`](../../app/core/session/post_turn_mixin.py) stashes a one-shot `_pending_self_correction` slot (gated by a per-fire cooldown), and the [`_render_self_correction_block`](../../app/core/session/inner_life_providers_mixin.py) provider surfaces a gentle "oh wait — earlier I said X, that's not right" cue on the NEXT turn. Independent of the gap-cue family. The streaming same-reply "wait, actually" beat (abort + splice mid-stream) is deferred to **K41** below.

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
