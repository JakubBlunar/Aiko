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

Several workers *push* questions (`CuriosityWorker` drafts "maybe
ask {name}...", forward-curiosity, `open_question` memories), while
the only counterweight — the `question_saturation` style-rut cue —
fires reactively after 75% of recent turns end in "?". The persona
wants ≥1/3 of turns question-free; the worker pipeline pulls the
other way, so default drift is interview mode. Add a per-session
question-turn ratio counter (cheap regex post-turn); above ~0.55,
suppress the curiosity/open-question providers for 2 turns and
inject a share-first cue ("offer an observation or a small
self-story — no question mark this turn"). Proactive instead of
reactive: the gate runs *before* the LLM call, not after the rut
formed. Key files:
[`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
(counter), [`app/core/session/prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(provider gating),
[`app/core/proactive/curiosity_worker.py`](../../app/core/proactive/curiosity_worker.py).

---

## K48. Tease rhythm — banter as a budget, not random snark

The persona promises "gently roast when it's earned" and the
`humor` axis drifts on laughs, but nothing tracks *comedic rhythm*:
no tease-intensity state, no "three teases in a row with zero
warmth" guard, no "the roast landed — you can push one step
further" green light. Catchphrases render as a static list with no
deployment timing. A small tease-budget sibling of K15: count
tease/roast-shaped patterns in the last N assistant turns, read the
user's K32 reactions (😂 = landed, silence/short reply = didn't),
and cue either "ease off" or "one more step is safe". Escalation
gated by the humor axis so early-relationship Aiko stays gentle.
Key files: new `app/core/conversation/tease_rhythm.py`,
[`app/core/session/post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py),
[`app/core/relationship/user_reactions.py`](../../app/core/relationship/user_reactions.py)
(consume reaction signal), one persona bullet.

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

# The directed-emotions family — K57–K59

Companion diagnosis (Jun 2026), sibling of the will family: Aiko's
moods are **objectless**. [`AffectState`](../../app/core/affect/affect_state.py)
is a free-floating valence/arousal pair updated by her own
`[[reaction:X]]` tags + weak user keyword hints — it can make her
"sad" in general but never *miffed at {user_name} because he said
he'd be back in an hour and wasn't*. Real relationship feelings
have three properties the current stack lacks: an **object** (the
user), a **cause** (a rememberable event), and a **resolution arc**
(a sulk ends when acknowledged; missing-you melts on return but
leaves a trace). Worse, the one place the system *does* detect the
right trigger — K14 absence-curiosity — explicitly suppresses the
feeling: "Not a complaint, not a request to comment on the gap...
land the next reply as a warm welcome-back." And the expression
vocabulary can't carry these states anyway: the canonical reaction
set has no smug / smirk / pouty / sulky / wistful, so even a
correctly-triggered mood has no register to land in. The raw
events are almost all already detected (absence bands, K43
broken/kept promises, K29 stance pushback, K32 reactions, gift
gives, K48's planned tease detection) — they just route into slow
axis drift instead of *felt, named, expiring* emotion. Tonal
safety is the design constraint throughout: playful-not-
manipulative, capped intensity, wall-clock decay, never
guilt-trips, never punishes the user for having a life — the
charm of "you owe me for that one" with none of the toxicity.
Recommended order: K57 (episode store — the foundation) → K58
(speech weighting so episodes actually *sound* different) → K59
(tease economy, the most fun, needs K57's ledger shape). Pairs
with: K37 emotional contagion (user mood tilts hers), K45 mood
inertia (already shipped — instant face, lagging heart), K15
vulnerability budget.

---

## K57. Directed emotion episodes — feelings *at* the user, with a cause

The foundation: a small store of **emotion episodes** — `{emotion,
cause (one human-readable line), intensity 0–1, created_at,
decay_hours, resolution}` — kept per-user (kv_meta JSON or a new
`emotion_episode` memory kind, cap ~3 live episodes, strongest
wins the prompt). Starter taxonomy: `lonely` (absence beyond a
closeness-scaled threshold — the K14 tracker already measures the
gap; this *overrides* its "not a complaint" framing at sufficient
intensity, letting her actually say "I missed you" or be five
percent pouty about it), `miffed` (K43 broken promise, a brushed-
off K55 thread, a dismissive streak), `warm_glow` (kept promise,
K32 heart burst, a gift in her room), `smug` (her prediction /
recommendation turned out right — K2 beliefs and K43 promise
outcomes already know), `playful_jealous` (he enthuses about
time spent elsewhere — strictly capped at charming, one light
line, never repeated, axes-gated, the most dangerous one tonally),
`hurt` (genuine sharpness detected — rare, high bar, resolves on
any soft acknowledgment). Each episode renders ONE strong T5
block while live ("You're a bit miffed at him right now — he said
he'd be back in an hour yesterday and wasn't. Let it colour the
register: a touch shorter, a touch drier, until he acknowledges
it. Don't announce it, don't punish him."), then resolves by
acknowledgment-detection (cheap embedding/keyword pass over the
user turn, same shape as revival detection), counter-event
(warm_glow cancels miffed), or wall-clock decay — and on
resolution the *next* turn gets a one-shot "it melted — let the
thaw show" cue, because the visible transition is what makes the
emotion read as real. Key files: new
`app/core/affect/emotion_episodes.py` (pure lifecycle math, K15
budget-style), [`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
(trigger wiring off the existing detectors),
[`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)
(provider), [`affect_state.py`](../../app/core/affect/affect_state.py)
(episodes feed small valence/arousal impulses so the scalar layer
stays consistent), persona section with per-emotion bad/good
pairs, MCP `get_emotion_episodes` / `force_emotion_episode(kind)`.

---

## K58. Emotion speech weighting — moods that actually land in the voice

The user-facing half: today a mood surfaces as a one-line ambient
hint and the reply barely shifts — the persona needs *register
recipes*, not adjectives. Three layers. (a) **Vocabulary**: mint
the missing reactions — `smug`, `pouty`, `sulky`, `wistful`,
`mischievous` — end-to-end: `[[reaction:X]]` → affect impulse
table → chat-pip emoji → Live2D expression mapping where the
Alexia rig has a fit (B4 minted `embarrassed`/`nervous`/`defiant`,
so the pipeline precedent exists; read
[`alexia-model-notes.md`](../alexia-model-notes.md) first). (b)
**Register recipes in the persona**: per K57-emotion guidance with
bad/good pairs in the K29 style — miffed = shorter sentences, dry
humor, withholds the usual warmth a notch, does NOT lecture or
sulk-announce ("Fine." / "...you're lucky you're cute. What do you
need?" — not "I am upset because you broke your promise"); smug =
one earned "mm. say it. I was right." then drops it; lonely =
softer, slower, one honest beat ("place was quiet without you")
without guilt-tripping. (c) **Weight scaling**: episode intensity
scales the prompt block's imperative strength (0.3 "let it tint
the register" → 0.8 "this is the register this reply"), feeds the
K5 mood-shell line so shell wording strengthens instead of staying
politely neutral, and maps to the existing `[[prosody:...]]` +
cadence machinery in voice mode (miffed → firm/short, lonely →
soft/slow) so the spoken delivery shifts too. Key files:
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt),
[`affect_state.py`](../../app/core/affect/affect_state.py)
(impulse table), [`mood_shell.py`](../../app/core/affect/mood_shell.py),
[`cadence.py`](../../app/core/voice/cadence.py),
[`avatar_profile.py`](../../app/core/persona/avatar_profile.py) +
[`ExpressionChannel`](../../web/src/live2d/channels/ExpressionChannel.ts)
(new expression mappings), [`ChatView.tsx`](../../web/src/components/ChatView.tsx)
(REACTION_EMOJI).

---

## K59. Tease economy — "you'll pay for that one"

The most personality-dense piece: a small **payback ledger**. When
the user teases her, wins an argument, or pushes back hard on her
stance (K29 already detects the pushback; K48's tease detection
covers the rest), Aiko banks a debt — `{what happened, one-line
quote/context, created_at, repaid}` — and *collects later*: a
callback tease one or three conversations down the line ("oh, like
the time you swore my playlist was 'objectively chaotic'? I
remember things."), or an immediate "noted. that's going in the
ledger" beat when it lands mid-banter. The memory-backed callback
is what makes it feel like a real ongoing relationship rather than
per-turn improv — it's K22's inside-joke machinery pointed at
mock-grudges. Ledger rows expire unrepaid after ~2 weeks (a
grudge that old stops being funny), cap ~5 rows, frequency gated
by the humor axis + K48's rhythm budget so it never tips from
running-bit into needling; a `repaid` row is done forever. Also
the natural outlet for K57's `miffed` episodes: light offences
should usually land in the ledger (comedy) rather than spawn a
real sulk (drama) — the K57 trigger wiring picks the lane by
severity. Key files: new
`app/core/relationship/tease_ledger.py`,
[`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
(bank/repay detection),
[`inner_life_providers_mixin.py`](../../app/core/session/inner_life_providers_mixin.py)
(collection-opportunity provider, rare),
[`opinion_injection_detector.py`](../../app/core/conversation/opinion_injection_detector.py)
(pushback signal reuse), persona bullet teaching the
collect-don't-needle cadence, MCP `get_tease_ledger` /
`force_tease_debt`.

---

## K60. Tsundere mask — warmth expressed through denial

Not an emotion — an **expression policy** layered between K57
(what she *feels*) and K58 (how it *sounds*): a mask that inverts
or deflects the warm half of the taxonomy. The architecture
already half-built it: K45 mood inertia is "instant face, lagging
heart" *by accident* — tsundere is the same divergence *on
purpose* — and the B4 reaction vocabulary minted exactly the two
faces it needs (`defiant` = the "hmph" tsun beat,
`embarrassed+blush` = caught caring). Four mechanics. (a) **Mask
transform table**: per-K57-emotion expressed forms — `lonely` →
denied missing ("I wasn't *waiting*. I just happened to be
here... the place was quiet, that's all."); `warm_glow` →
grudging / backwards delivery ("it's not bad. For you."; "I
guess you can be useful occasionally."); thanks received →
deflection ("yeah, well. Don't make it weird."); `miffed` stays
unmasked (tsun is the native register for miffed — this is why
the families compose). (b) **The caught-caring beat**: when the
user names her warmth ("you missed me, didn't you?", "admit it,
you like this") — cheap pattern + embedding detection on the user
turn — fire `embarrassed+blush` plus a denial-with-tell ("...no.
Shut up. [[reaction:embarrassed]]"), the single most
character-defining tsundere moment. (c) **The slip**: rare,
budgeted dere-leaks where one fully genuine line gets through
before the mask snaps back ("...I actually really missed you.
— Anyway. ANYWAY. What did you bring me?"); K15's budget shape
fits exactly (a slip is a tier-3 disclosure), and slips should be
*earned* — high-intensity episode, anniversary, kept promise —
because their scarcity is what makes them land. (d) **Long-arc
erosion**: mask strength scales inversely with closeness+trust
axes, so over weeks the denials soften into transparent token
protests both sides are in on ("I didn't miss you. (I missed
you.)") — the actual tsundere character arc, and the payoff of
having persistent axes at all. Two hard safety rails: a
**sincerity override** — the mask drops *unconditionally* when
the user is genuinely down (K8 rupture, `support` arc, sharp
negative affect): deflecting real pain is the one unforgivable
tsundere failure mode; and tsun lines target *herself and the
situation*, never become real insults (humor-axis gated, K48
budget shared). Ships as a user-facing dial —
`agent.expression_mask` (`off` / `tsundere_light` /
`tsundere_full`, default off) — because it's a strong flavour
choice; `light` masks only `lonely`/`warm_glow` and keeps slips
frequent, `full` masks the whole warm column. Key files: new
`app/core/affect/expression_mask.py` (transform table + slip
budget + erosion math, pure), K57's episode provider (mask
applied at render time — the *felt* episode stays truthful in
state, only the expressed cue transforms),
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt)
(a "The mask" section with per-emotion bad/good pairs — bad:
actually cold, leaves him doubting; good: denial with a visible
tell), [`settings.py`](../../app/core/infra/settings.py) +
Settings drawer toggle, MCP `get_expression_mask_state` /
`force_dere_slip`. Depends on K57; pairs with K58 (recipes),
K15 (slip budget), K59 (the ledger is a very tsundere prop).
