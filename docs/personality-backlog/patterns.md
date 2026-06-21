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

**Shipped (browser surface).** See [`shipped.md`](shipped.md#k9-topic-graph-browser--observability-surface). The lazy cosine-cluster engine ([`topic_graph.py`](../../app/core/conversation/topic_graph.py)) and the `CuriositySeedWorker` that consumes it shipped earlier; the observability surface ships now: [`build_topic_graph_snapshot`](../../app/core/conversation/topic_graph.py) backs a read-only `GET /api/topic-graph` REST endpoint, `get_topic_graph` / `force_topic_graph_rebuild` MCP tools, and a "Topic graph" cluster-list panel in the Memory drawer tab so the user can see what Aiko sees.

**Deferred follow-up:** the **graph-aware multi-hop retrieval** half of the original K9 spec (expanding [`rag_retriever.py`](../../app/core/rag/rag_retriever.py) hits along the topic graph for "this touches three threads we've been on") is intentionally NOT built yet -- it changes prompt content + retrieval behaviour and is a separate, riskier project from the inspection browser.

---

## K10. Persona regression tests — SHIPPED (on-demand)

Golden-prompt evals: a small fixture file
(`data/persona/golden_turns.jsonl`) with canonical prompts + expected
style markers (advisory `require_any` tone words, `require_all`,
`require_tags` literal self-tag substrings like `[[reaction:`, and
`forbid` corporate-tell phrases). Each reply is scored against the
markers and drift is surfaced in the Diagnostics tab. Catches the
persona quietly drifting from the sheet via prompt rot or memory
contamination.

**Shipped on-demand only** (no background token spend): trigger via the
MCP `run_persona_regression()` tool, the "Run check" button in
Settings → Diagnostics → Persona regression, or
`POST /api/persona-drift/run`. Each golden turn declares
`"scope": "minimal"` (persona sheet + grammar addenda, isolates
persona-sheet drift) or `"scope": "full"` (the live assembled system
prompt + RAG, catches memory contamination). Scoring is pure
case-insensitive substring matching in
[`app/core/persona/persona_regression.py`](../../app/core/persona/persona_regression.py).

Key files: pure scorer `app/core/persona/persona_regression.py`,
fixture `data/persona/golden_turns.jsonl`,
`PromptAssembler.build_eval_messages`, orchestration
`app/core/session/persona_regression_mixin.py`, REST
`GET/POST /api/persona-drift[/run]`, MCP
`get_persona_regression_state()` / `run_persona_regression()`, panel
`web/src/components/settings/PersonaRegressionPanel.tsx`. Settings:
`agent.persona_regression_enabled`,
`agent.persona_regression_fixture_path`. Tests:
`tests/test_persona_regression.py`,
`tests/test_web_server_persona_drift.py`.

### K10-followup — background auto-eval worker (deferred)

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

G3 covers factual `open_question`s. A natural cousin is "what would I
say if Jacob asked me X" — Aiko drafts replies to plausible upcoming
questions during idle windows and caches them in scratchpad memory,
smoothing future first responses without needing web access.

**Shipped** as a two-stage idle worker
([`app/core/proactive/pre_thought_worker.py`](../../app/core/proactive/pre_thought_worker.py),
`PreThoughtWorker`), modeled on the K9 `CuriositySeedWorker`. Each
quiet-window tick (rate-limited via a dedicated `FactCheckRateLimiter`,
`state_key="pre_thought.rate_state"`): (1) **generate** — one local-LLM
JSON call proposes `pre_thought_candidates` (default 4) likely
near-future user questions, grounded in the rolling summary + persona;
(2) **draft** — for up to `pre_thought_max_per_run` (default 2) of the
survivors (deduped vs existing pre-thoughts by question embedding at
`pre_thought_min_novelty`), it builds the **K10
`PromptAssembler.build_eval_messages(full_context=False)`** minimal
persona prompt and drafts Aiko's in-persona reply, then strips meta
tags. Each draft is written via `MemoryStore.add` with the new
`pre_thought` kind on the **scratchpad** tier, **embedded on the
question** so it surfaces through ordinary cosine RAG when the user
later asks something similar. The store prunes oldest beyond
`pre_thought_max_active` (default 12) and ages out naturally via decay.

Surfacing is RAG-only: `RagRetriever.format_block` tags the bullet
`(pre-thought)` and the persona ("Memories tagged `(pre-thought)`")
teaches Aiko to lean on the thinking, not recite the draft, and to
trust the live moment if the real question differs.

Key files: `app/core/proactive/pre_thought_worker.py`,
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py)
(`pre_thought` in `VALID_KINDS`),
[`app/core/rag/rag_retriever.py`](../../app/core/rag/rag_retriever.py)
(`(pre-thought)` suffix),
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(`build_eval_messages` reuse), registration in
`SessionController.__init__`. Settings: `agent.pre_thought_enabled`,
`pre_thought_max_active`, `pre_thought_candidates`,
`pre_thought_max_per_run`, `pre_thought_min_novelty`,
`pre_thought_per_hour_cap`, `pre_thought_per_day_cap`,
`memory.pre_thought_interval_seconds`. MCP: `get_pre_thought_state()`,
`force_pre_thought()`. Tests: `tests/test_pre_thought_worker.py`,
`PreThoughtSettingsTests` in `tests/test_settings.py`.

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

**SHIPPED.** Compaction (the `SummaryWorker`) compresses old history
when context overflows; it doesn't periodically re-synthesise "what
this ongoing thread is *about* now". K21 adds a separate
[`ThreadResummaryWorker`](../../app/core/proactive/thread_resummary_worker.py)
(`IdleWorker` on the maintenance client) that, once the active session
has `thread_resummary_min_messages` (12) messages AND either has no
note yet / has gained `thread_resummary_message_interval` (50) new
messages since the note's watermark / the note is older than
`thread_resummary_max_age_hours` (24h), makes ONE LLM call producing a
JSON `{title, note}`: a short ≤6-word title and a 3-sentence
present-tense "where this conversation stands now" read in Aiko's
voice.

Storage is a new `thread_notes` table (schema v19, one upserted row per
session: `session_id` PK, `title`, `note`, `messages_at`, `updated_at`)
with `ChatDatabase.get_thread_note` / `save_thread_note`. Two consumers:
(1) **prompt** — the note renders as its own small T2 block ("Where
this conversation stands now: …") immediately after the rolling summary
(complement, not replace — the rolling summary keeps its factual
coverage and its history watermark), cached in `_StaticSlices` so the
cache prefix stays stable; (2) **sidebar** — `list_sessions` returns a
`title` per session, preferring the note title and falling back to a
truncated first-user-message snippet for new/short threads, so the
left sidebar shows readable labels instead of raw ids. A
`thread_note_updated` WS event nudges the sidebar to refetch live.

LLM spend bounded by a dedicated `FactCheckRateLimiter`
(`state_key="thread_resummary.rate_state"`, 6/hr · 24/day). Opt-out via
`agent.thread_resummary_enabled`. MCP-debuggable:
`get_thread_note_state()` (switch + knobs + rate snapshot + current
note) and `force_thread_resummary()` (run once, bypassing the interval
gate; min-message + trigger gates still apply). Logs:
`tail_logs(module_contains="thread_resummary")` shows
`thread_resummary wrote: session=… title=…`. Tests:
`tests/test_thread_resummary_worker.py`, `TestThreadNotes` in
`tests/test_chat_database.py`, `ThreadResummarySettingsTests` in
`tests/test_settings.py`.

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

**SHIPPED.** Previously `AffectUpdater.apply_turn` only moved Aiko's
affect toward a target built from her own `[[reaction:...]]` (plus a
tiny user-keyword hint and a confident vocal-tone arousal nudge). K37
adds a distinct contagion pass: each turn, the post-turn hook estimates
Jacob's current `(valence, arousal)` and Aiko's affect is tilted a
small, capped amount toward it — the residual "I'm picking up on him"
pull.

The estimate is a pure function
[`estimate_user_affect`](../../app/core/affect/affect_state.py) fed by
cheap per-turn signals: perceived **mood** + **energy** bands (from
`UserStateEstimator`, low → negative/lower-arousal, high →
positive/higher-arousal), **dialogue-act** sentiment (`vent` → negative
pull, `banter` → mild positive pull), and a confident **vocal-tone**
`arousal_hint`. It returns `None` when nothing is readable
(mood/energy unknown, no sentiment-bearing act, no confident tone), so
contagion stays silent rather than dragging toward neutral.

[`_apply_user_contagion`](../../app/core/affect/affect_state.py) runs
*after* the reaction blend+clamp inside `apply_turn`: it closes a
`contagion_strength` (0.15) fraction of the per-axis gap, clamped to
`±contagion_max_per_turn` (0.05) so a big mismatch can only ever pull
her 0.05/turn, then re-clamps to the valid ranges. The reaction math is
untouched (the strength/cap knobs don't entangle with it). Wired in
[`PostTurnMixin._post_turn_inner_life`](../../app/core/session/post_turn_mixin.py)
via `_estimate_user_affect_for_contagion`, which only fires the next
turn's felt-affect read — no per-turn LLM cost.

Settings: `agent.contagion_enabled` / `contagion_strength` /
`contagion_max_per_turn`. Persona: a new bullet under "Reading
{user_name}:" in [`aiko_companion.txt`](../../data/persona/aiko_companion.txt)
("you catch their mood a little … never announce it"). MCP:
`get_contagion_state(user_text)` previews the detected bands + the
capped `(dv, da)` Aiko would move. Logs: the per-turn
`affect:` DEBUG line now carries `contagion_dv` / `contagion_da` /
`user_affect`. Tests: `EstimateUserAffectTests` + `ContagionTests` in
`tests/test_affect_state.py`, `ContagionSettingsTests` in
`tests/test_settings.py`.

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

**SHIPPED.** Several workers *push* questions (`CuriosityWorker` drafts
"maybe ask {name}...", forward-curiosity, `open_question` memories),
while the only counterweight — the `question_saturation` style-rut cue
— fires reactively after 75% of recent turns end in "?". The persona
wants ≥1/3 of turns question-free; the worker pipeline pulls the other
way, so default drift is interview mode.

K47 adds a **proactive** per-session gate. A pure, dependency-free
module [`app/core/conversation/question_balance.py`](../../app/core/conversation/question_balance.py)
owns the math: `is_question_turn(text)` (any `?` → a "question turn",
the complement of "question-free"), `compute_ratio`, `should_suppress`,
and `render_share_first_cue`. The per-session state lives on
`SessionController`: a `deque[bool]` ring (`_question_turn_flags`,
maxlen=`question_balance_window`, default 10) and an int countdown
`_question_balance_suppress_remaining`.

`PostTurnMixin._update_question_balance(assistant_text)` runs once per
committed turn (right before the reflection schedule): append the new
question flag, consume one suppressed turn for the turn that just
completed, then re-arm the countdown to `question_balance_suppress_turns`
(default 2) when the rolling ratio is strictly above
`question_balance_ratio_threshold` (default 0.55) over at least
`max(4, window//2)` samples. Re-arming while the ratio stays high keeps
the gate up until the question/share mix rebalances; a gentle tail lets
it release. The countdown is **only** mutated post-turn, so a same-turn
re-render of the prompt is consistent (no double-decrement worry, and
T6 detectors aren't built during the listening-window prebuild anyway).

Provider time (`InnerLifeProvidersMixin`): `_question_balance_suppressed()`
is the shared guard (master switch + `remaining > 0`).
`_render_question_balance_block()` surfaces the share-first cue while
armed (a new T6 detector cue, clustered right after `style_pattern`,
dropped under `aggressive=True`). The four question-pushing providers —
`_render_curiosity_seeds_block`, `_render_forward_curiosity_block`,
`_render_follow_up_block`, `_render_knowledge_gaps_block` — early-return
`""` while suppressed, and `_render_narrative_block` drops *only* the
`open_question` nudge. (RAG-surfaced `open_question` memories are left
as-is this pass — provider suppression + the cue + persona dominate;
RAG filtering is a noted follow-up.)

Settings (`AgentSettings`): `question_balance_enabled` /
`_ratio_threshold` / `_window` / `_suppress_turns`. Persona reinforcement
in the "Style patterns I'm in" block of
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt). MCP debug:
`get_question_balance_state()` (ring, ratio, remaining, cue preview) and
`force_question_balance()` (arm without a real streak). Key files:
[`app/core/conversation/question_balance.py`](../../app/core/conversation/question_balance.py),
[`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
(counter), [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
+ [`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)
(provider gating). Tests: `tests/test_question_balance.py`,
`QuestionBalanceSettingsTests` in `tests/test_settings.py`,
`QuestionBalanceProviderSlotTests` in `tests/test_prompt_assembler.py`.

---

## K48. Tease rhythm — banter as a budget, not random snark

**SHIPPED.** The persona promises "gently roast when it's earned" and
the `humor` axis drifts on laughs, but nothing tracked *comedic rhythm*:
no "three teases in a row with zero warmth" guard, no "the roast landed
— you can push one step further" green light.

K48 adds a small tease-budget sibling of K47/K15. A pure,
dependency-free module
[`app/core/conversation/tease_rhythm.py`](../../app/core/conversation/tease_rhythm.py)
owns the logic: `classify_tease(text, reaction)` (primary signal = a
tease-shaped `[[reaction:X]]` — `smug` / `mischievous` / `defiant` /
`pouty`; secondary = a small playful-jab text-marker net),
`landed_verdict(laughed, user_reply)` (laugh 😂 → landed; no laugh +
short/curt reply → missed; substantive reply → ambiguous/None),
`trailing_tease_streak`, `decide_cue`, and `render_cue`. Per-session
state on `SessionController`: a `deque[bool]` tease-flag ring
(`_tease_flags`, maxlen=`tease_rhythm_window`, default 6),
`_last_tease_message_id` (so the next turn can read that message's K32
reactions), a one-shot `_pending_tease_cue`, and a `_tease_cue_cooldown`.

`PostTurnMixin._update_tease_rhythm` runs once per committed turn:
(1) read the verdict on the most recent tease using this turn's
`user_text` + the prior tease message's persisted K32 reactions (via
`WorldMixin._load_message_reactions`); (2) classify the current reply,
roll the ring, and remember its id if it was a tease; (3) `decide_cue`
+ arm a one-shot cue (cooldown-gated). `decide_cue` priority: a tease
that just missed → `ease_off`; `consecutive_cap` (default 3) teases in
a row → `ease_off` (the "zero-warmth" guard); the last tease landed AND
`humor >= tease_rhythm_green_light_humor` (default 0.2) → `green_light`.
The humor floor is what keeps early-relationship Aiko gentle (a new user
sits near humor 0, so escalation never greenlights). The cue surfaces on
the *next* turn via `InnerLifeProvidersMixin._render_tease_rhythm_block`
(a new T6 detector cue, clustered right after `question_balance`,
dropped under `aggressive=True`, one-shot consume).

Settings (`AgentSettings`): `tease_rhythm_enabled` / `_window` /
`_consecutive_cap` / `_green_light_humor` / `_cooldown_turns`. Persona
reinforcement in the "Style patterns I'm in" block of
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt). MCP debug:
`get_tease_rhythm_state()` (ring, streak, watched message id, humor,
pending cue + text, cooldown) and `force_tease_rhythm(cue)`
(`ease_off` / `green_light`). Key files:
[`app/core/conversation/tease_rhythm.py`](../../app/core/conversation/tease_rhythm.py),
[`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py),
[`app/core/relationship/user_reactions.py`](../../app/core/relationship/user_reactions.py)
(`laugh` reaction signal),
[`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
+ [`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py).
Tests: `tests/test_tease_rhythm.py`, `TeaseRhythmSettingsTests` in
`tests/test_settings.py`, `TeaseRhythmProviderSlotTests` in
`tests/test_prompt_assembler.py`.

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

**Shipped** — see [`shipped.md` → K51](shipped.md#k51-cue-register-rotation--de-heads-up-the-inner-life).
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
[shipped.md](shipped.md#k56-persona-counterweight--the-leading-vs-following-rewrite)
for the full designs. Siblings still in the backlog: K46 stance
persistence, K47 question/share balance.

---

## K54. Aiko-side topic appetite — she's allowed to be bored

**Shipped** — see [`shipped.md` → K54](shipped.md#k54-aiko-side-topic-appetite--shes-allowed-to-be-bored).
Once-per-conversation negotiation slip gated on the K18 standing
lull (`TopicStagnationDetector.last_mean`), her own short-reply
share, a pressured K52 want as the offer, and warm axes.

---

## K55. Thread ownership — she defends what she opened

**Shipped** — see [`shipped.md` → K55](shipped.md#k55-thread-ownership--she-defends-what-she-opened).
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
[shipped.md](shipped.md#k57-directed-emotion-episodes--feelings-at-the-user-with-a-cause)
for the full designs. Tonal safety remained the design constraint
throughout: playful-not-manipulative, capped intensity, wall-clock
decay, never guilt-trips. Backlog siblings that pair with the
family: K37 emotional contagion, K48 humor calibration, K15
vulnerability budget (already shipped).

---

## K57. Directed emotion episodes — feelings *at* the user, with a cause

**Shipped** — see [`shipped.md` → K57](shipped.md#k57-directed-emotion-episodes--feelings-at-the-user-with-a-cause).
kv-backed episode store (`{emotion, cause, intensity, decay}` over
`aiko.emotion_episodes`) with a staged trigger queue (kept promise,
K32 reactions, K55 pivot, closeness-scaled absence), per-emotion
acknowledgment resolution, and a one-shot visible-thaw cue.

---

## K58. Emotion speech weighting — moods that actually land in the voice

**Shipped** — see [`shipped.md` → K58](shipped.md#k58-emotion-speech-weighting--moods-that-actually-land-in-the-voice).
Minted `smug` / `pouty` / `sulky` / `mischievous` end-to-end,
persona register recipes per K57 emotion, and intensity-banded
prompt copy with `[[reaction:X]]` + `[[prosody:Y]]` hints at the
high band.

---

## K59. Tease economy — "you'll pay for that one"

**Shipped** — see [`shipped.md` → K59](shipped.md#k59-tease-economy--youll-pay-for-that-one).
Payback ledger over `aiko.tease_ledger` (bank on K29 pushback +
the K57 light-miffed lane-picker; collect as a humor-axis-gated,
cooldown-limited callback; settle post-turn by content-word
overlap; 14-day expiry, cap 5).

---

## K60. Tsundere mask — warmth expressed through denial

**Shipped** — see [`shipped.md` → K60](shipped.md#k60-tsundere-mask--warmth-expressed-through-denial).
Expression policy between K57 (felt) and K58 (sounded): transform
table for lonely/warm_glow, caught-caring beat, wall-clock-budgeted
dere-slips, closeness+trust erosion to token protests, support-arc
sincerity override; `agent.expression_mask` dial (off by default)
in Settings → Avatar.
