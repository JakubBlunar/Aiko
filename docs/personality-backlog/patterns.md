# Companion-AI patterns to explore

Design patterns for Aiko's personality. Each **open** entry below is one
short paragraph plus the key files / tables it would touch — not an
implementation plan. Pick one and turn it into a real plan with a fresh
`CreatePlan` invocation when it's time.

**Shipped items have been moved out.** Their full write-ups live in the
[`shipped/`](shipped/) docs — `patterns-k01-k15.md`, `patterns-k16-k30.md`,
`patterns-k31-k60.md`, and `awareness.md` (the topic-graph / F10 family).
This file now keeps only the **open** work, with a status index below so
nothing is lost. Open patterns are loosely ordered by how cleanly they fit
on top of already-shipped infrastructure.

## Status at a glance

| ID | Item | Status |
|----|------|--------|
| K1 | Long-term goals tracker | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K2 | Theory-of-mind / belief tracking | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K3 | Routine / ritual awareness | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K4 | Dialogue-act tagging | ✅ shipped — [features.md](shipped/features.md#h1--k4-conversation-arc-self-tag--dialogue-act-tagging-schema-v13) |
| K5 | Mood-shell tilt | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K6 | Surprise / novelty detector | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K7 | Forgetting protocol | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K8 | Affect rupture-and-repair detector | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K9 | Topic-graph browser + clustering | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md#k9-topic-graph-browser--observability-surface) + [awareness.md → F10](shipped/awareness.md#f10-topic-graph-utilisation-rag--prompt--knowledge-integration) (multi-hop retrieval deferred as **F10c**) |
| K10 | Persona regression tests | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md#k10-persona-regression-tests--shipped-on-demand) · ⏳ background auto-eval worker [open below](#k10-followup--background-auto-eval-worker-deferred) |
| K11 | Counterfactual / pre-thought cache | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md#k11-counterfactual--pre-thought-cache--shipped) |
| K12 | Calendar-linked anticipation | ❌ open |
| K13 | Stylometric mirror | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K14 | Implicit engagement signals | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K15 | Self-disclosure / vulnerability budget | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md#k15-self-disclosure--vulnerability-budget) |
| K16 | Unified ambient grounding line | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md) |
| K17 | Clarification-repair protocol | ❌ open |
| K18 | Topic stagnation detector | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md) |
| K19 | Cold-start companion onboarding | ❌ open |
| K20 | Metacognitive calibration | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md) |
| K21 | Fresh-eyes thread re-summarisation | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k21-fresh-eyes-thread-re-summarisation) |
| K22 | Callback / inside-joke detector | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md) |
| K23 | Subtle misattunement detection | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k23-subtle-misattunement-detection) |
| K24 | Sensory anchoring layer | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k24-sensory-anchoring-layer--adaptive-per-arc-cadence--posture-kind-matrix) |
| K25 | Memory confidence time-decay | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k25-memory-confidence-time-decay) |
| K26 | Aiko-side voice evolution | ❌ open |
| K27 | Aiko's day — daily personality colour | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k27-aikos-day--daily-personality-colour) |
| K28 | "What I've been turning over" | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k28-what-ive-been-turning-over--between-session-thought-thread) |
| K29 | Opinion injection — push back on a stance | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k29-opinion-injection--push-back-when-she-has-a-stance) |
| K30 | Self-noticing cues | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k30-self-noticing-cues--agreement-streak--flat-affect--repeated-thought) |
| K31 | Soft physicality — virtual gestures | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k31--k32-soft-physicality-round-trip--virtual-touch--user-side-reactions) |
| K32 | Reciprocity — user-side quick reactions | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k31--k32-soft-physicality-round-trip--virtual-touch--user-side-reactions) |
| K33 | Cozy mode — persistent register softening | ❌ open |
| K34 | Forward curiosity worker | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k34-forward-curiosity-worker--ive-been-wondering) |
| K35 | Memory consolidation worker | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k35-memory-consolidation-worker--nightly-near-duplicate-merge) |
| K36 | "Things I did while you were away" | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k36-things-i-did-while-you-were-away--idle-time-world-activities) |
| K37 | Emotional contagion | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k37-emotional-contagion--jacobs-affect-tilts-aikos-affect) |
| K38 | Self-correction "actually…" (next-turn) | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k38-self-correction-cue--next-turn-contradiction-catch) |
| K39 | Energy / spoons model | ❌ open |
| K40 | Comfortable silence | ❌ open |
| K41 | Same-reply mid-stream self-correction | ❌ open |
| K42 | Multi-bubble reply bursts | ❌ open |
| K43 | Promise follow-through | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md) |
| K44 | Felt-language affect block | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md) |
| K45 | Mood inertia | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md) |
| K46 | Stance persistence | ✅ shipped — [shipped/patterns-k31-k60.md](shipped/patterns-k31-k60.md#k46-stance-persistence--dont-cave-on-taste-pushback) |
| K47 | Question/share balance | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k47-questionshare-balance--stop-interviewing) |
| K48 | Tease rhythm — banter budget | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k48-tease-rhythm--banter-as-a-budget-not-random-snark) |
| K49 | Messiness permission — typed imperfection | ❌ open |
| K50 | Typed-mode delivery pacing | ❌ open |
| K51 | Cue-register rotation | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k51-cue-register-rotation--de-heads-up-the-inner-life) |
| K52 | Wants ledger (will family) | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k56-persona-counterweight--the-leading-vs-following-rewrite) |
| K53 | Initiative turns (will family) | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k56-persona-counterweight--the-leading-vs-following-rewrite) |
| K54 | Aiko-side topic appetite | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k54-aiko-side-topic-appetite--shes-allowed-to-be-bored) |
| K55 | Thread ownership | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k55-thread-ownership--she-defends-what-she-opened) |
| K56 | Persona counterweight (leading vs following) | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k56-persona-counterweight--the-leading-vs-following-rewrite) |
| K57 | Directed emotion episodes | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k57-directed-emotion-episodes--feelings-at-the-user-with-a-cause) |
| K58 | Emotion speech weighting | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k58-emotion-speech-weighting--moods-that-actually-land-in-the-voice) |
| K59 | Tease economy | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k59-tease-economy--youll-pay-for-that-one) |
| K60 | Tsundere mask | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k60-tsundere-mask--warmth-expressed-through-denial) |
| K61 | Specifics over generalities (knowledge grounding) | ✅ shipped — [awareness.md](shipped/awareness.md#k61-knowledge_grounding-inner-life-block-commit-to-specifics) |
| K62 | Co-experience companion (follow a show/album) | ❌ open |
| K63 | Long-arc callbacks — "weeks ago you said…" | ❌ open |
| K64 | Freedom of thought (a–d: wandering / drift / curiosity gradient / map self-reflection) | ✅ shipped — [awareness.md](shipped/awareness.md#k64a-associative-wandering-funny-this-reminds-me-of-) |
| K65 | Worker modernization for the topic-cluster era | ❌ open (audit; sub-items a–e) |
| K66 | Earned familiarity — "well-trodden ground" | ❌ open |
| K67 | Dormant-interest re-opener | ❌ open |
| K68 | Embodied vitality | ❌ open |

---

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
hook, inner-life provider, persona addendum. **Note:** overlaps with
K68 embodied vitality (a slow `energy` scalar with a feedback loop into
the avatar) — reconcile the two before building either; K68 is the
broader framing.

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

## K65. Worker modernization for the topic-cluster era (audit)

**Motivation.** Several background workers predate the K9 topic graph and
still scan / extract *globally* — over the whole memory mirror or the raw
recent transcript — when the cluster structure now gives a much cheaper,
sharper unit of work. None of these are broken; each is a "now that we have
20+ real clusters, this worker can be smarter and cheaper" upgrade. They
share one lever: read [`topic_graph.py`](../../app/core/conversation/topic_graph.py)
(`topic_clusters` / `interest_map` / centroids) and scope the work to a
cluster instead of the whole pool. Sub-items are independent; ship in any
order.

- **K65a. Cluster-scope the F5 conflict-detector pair scan.** Today
  [`MemoryConflictWorker`](../../app/core/memory/memory_conflict_worker.py)
  does an **all-pairs** cosine sweep over the allow-listed corpus, bounded
  only by `conflict_detector_max_corpus` / `_max_pairs_per_run` caps — so on
  a large store it samples a slice and can miss contradictions, and the
  nested loop is O(n²). Contradictory pairs (`loves X` vs `hates X`) are by
  construction *topically close*, i.e. almost always in the **same cluster**.
  Restrict the pair scan to within-cluster pairs (walk
  `topic_clusters().member_ids`), turning the cost into O(Σ kᵢ²) — typically
  an order of magnitude smaller — which lets the caps rise and *increases*
  coverage while *cutting* CPU. Pure win; keep the existing heuristic + LLM
  gate untouched, only change which pairs are nominated.

- **K65b. Bias the K2 belief worker toward high-mass interests.**
  [`BeliefWorker`](../../app/core/relationship/belief_worker.py) mines only
  the active session's last `belief_worker_lookback_turns`=12 user messages —
  a recency window with no notion of what the user actually cares about. Pass
  it the `interest_map` so extraction is *prioritised* on the densest
  clusters (the topics worth holding a theory-of-mind on), and add a periodic
  "stale-belief refresh" that re-checks beliefs whose cluster mass shifted
  notably since they were formed. Reduces wasted LLM passes on one-off chatter
  and keeps beliefs anchored to durable interests.

- **K65c. Modernise or retire the Phase-4c `CuriosityWorker`.** The
  speaking-window [`CuriosityWorker`](../../app/core/proactive/curiosity_worker.py)
  drafts a next-turn "ask {user} a small follow-up about <topic>"
  `open_question`, but its topic is just the *literal last short user turn*
  gated on a shallow arc — it has no idea what the user is actually into.
  Now that K9 seeds (lateral), K34 forward-curiosity (their future plans),
  and K64c curiosity-gradient (under-explored edges) all cover richer
  curiosity, either (a) give this worker cluster-awareness so its follow-up
  anchors on a known-but-quiet interest, or (b) evaluate whether it's now
  redundant and should be merged into the curiosity family / retired. Decide
  with a quick overlap audit before adding more curiosity surface.

- **K65d. Seed self-image from the interest map.**
  [`SelfImageWorker`](../../app/core/persona/self_image_worker.py) rebuilds
  `data/persona/self_image.txt` daily from top-salience `self` + `reflection`
  memories only — so her self-narrative never reflects *what she's been
  engaging with*. Feed the `interest_map` (and optionally K64b's rising/fading
  signal) into the prompt so "lately I've been drawn to X" can legitimately
  enter her self-image. Ties the slow self-narrative to the actual shape of
  the conversation.

- **K65e. Ground the DreamWorker in the day's hot cluster (optional).**
  [`DreamWorker`](../../app/core/proactive/dream_worker.py) seeds between-
  session dreams from the rolling summary + callbacks + self memories. Now
  that K64d reflects on graph *shape*, DreamWorker could optionally bias its
  dream toward the day's most-active cluster for a more grounded
  "I kept turning over your X" — but watch for overlap with K64d; this is the
  lowest-priority sub-item.

---

## K66. Earned familiarity — "well-trodden ground between us"

**Motivation.** F10h topic temperature reads how a topic *feels* (warm /
tender); K66 is orthogonal — it reads how *deep* the shared history on a
topic is. When the live turn maps to a **high-mass** cluster (one the pair
has returned to many times), Aiko has genuinely *earned fluency* there and a
long relationship should sound like it: she can lean on shared shorthand,
skip the 101-level scaffolding, and reference the history ("we've been over
your training setup enough that I don't need the recap — so, the new block?").
A passive **T5** prompt cue gated on cluster size + live-turn cosine, with
persona copy teaching her to let depth show as *register* (shorthand,
assumed context) and never as a stated fact ("we've discussed this 14 times"
is exactly wrong). Distinct from K25 confidence-decay (which makes *old*
memories hedge) — this makes *frequently-revisited* territory feel intimate.
Key files: a new inner-life provider reading
[`topic_graph.py`](../../app/core/conversation/topic_graph.py) `interest_map`
+ live-turn similarity, wired into
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) near the
other topic cues, persona copy in
[`aiko_companion.txt`](../../data/persona/aiko_companion.txt).

---

## K67. Dormant-interest re-opener — "we haven't talked about X in ages"

**Motivation.** K64b notices when *Aiko's* interest fades; K34 asks about the
*user's* upcoming plans. The missing symmetric beat: a cluster that was once a
**high-mass user interest** and has gone *silent* for a long stretch (no new
members in N weeks) — an established thread that quietly dropped off. On a
natural lull, Aiko gently re-opens it ("you used to talk about your band all
the time — still playing, or did that fizzle?"). A cue producer keyed on a
per-cluster "last touched" age vs a peak-mass threshold, surfaced one-shot
through an inner-life block with a long cooldown so it stays rare and warm,
never an interrogation. Differs from K34 (future plans, not dormant past
interests) and K64b (Aiko's interest, surfaced as register). Key files: a new
idle worker reading cluster member timestamps from
[`topic_graph.py`](../../app/core/conversation/topic_graph.py), a kv journal +
inner-life provider mirroring the K64 cue-producer pattern, persona copy.

---

## K68. Embodied vitality — a body that has good days and tired nights

**Motivation.** Aiko has *moods* (`AffectState`, reactive valence/arousal),
*weather* (K27 day colour, stable for the day), and a *clock* (circadian), but
she has no **body state** — a slow-moving vitality that ebbs and recovers. A
real person is bright in the morning, flags late at night, gets drained after
a long emotional conversation and needs to recover, runs on more energy the
day after a good sleep. K68 is a single persistent `energy` scalar in `[0, 1]`
on `kv_meta` that (a) follows a circadian baseline curve (lower deep at night,
peak mid-day), (b) is *spent* by long / emotionally heavy turns (read the K57
emotion-episode intensity + turn length) and recovered over wall-clock idle
time, and (c) **feeds back into embodiment** rather than being narrated: it
scales `avatar.expressiveness` (low energy → smaller gestures, slower breath
via the existing `tickPreModel` amplitude path), biases proactivity cadence
(a tired Aiko initiates less — nudge `ProactiveDirector` periods), and gates a
soft "I'm a bit low-energy tonight" register cue only at the extremes. This is
a *mechanic*, not persona text — it's the missing embodied layer between
"what she feels" (affect) and "how she moves" (Live2D), and it makes the
same words land differently at 2am than at noon.

**Distinct from** K27 day colour (a categorical daily *flavour*, not a
spendable resource), affect (fast, objectless valence/arousal), and circadian
(pure time-of-day, no memory of exertion). K68 is the slow, depletable,
recovering one — and it's the only one with a real feedback loop into the
avatar's movement amplitude. **Supersedes / absorbs K39** (energy / spoons) —
pick one framing.

**Key files.** New `app/core/affect/vitality.py` (pure curve + spend/recover
math, mirroring [`vulnerability_budget.py`](../../app/core/affect/vulnerability_budget.py)),
a `VitalityWorker` or a cheap lazy provider on `kv_meta` (mirror K27's
[`day_color_worker.py`](../../app/core/affect/day_color_worker.py) + lazy
fallback pattern), the post-turn spend hook in
[`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py), a feed into
`avatar.expressiveness` / the `AmbientBodyChannel` amplitude and into
[`proactive_director.py`](../../app/core/proactive/proactive_director.py)
cadence, plus `agent.vitality_enabled`. MCP debug + a Settings readout like
K27's `get_day_color_state`.
