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
| K63 | Long-arc callbacks — "weeks ago you said…" | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k63-long-arc-callbacks--weeks-ago-you-said) |
| K64 | Freedom of thought (a–d: wandering / drift / curiosity gradient / map self-reflection) | ✅ shipped — [awareness.md](shipped/awareness.md#k64a-associative-wandering-funny-this-reminds-me-of-) |
| K65 | Worker modernization for the topic-cluster era | ✅ shipped (a–e) — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k65a-cluster-scope-the-f5-conflict-detector-pair-scan-shipped-via-f10j) |
| K66 | Earned familiarity — "well-trodden ground" | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k66-earned-familiarity--well-trodden-ground-between-us) |
| K67 | Dormant-interest re-opener | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k67-dormant-interest-re-opener--we-havent-talked-about-x-in-ages) |
| K68 | Embodied vitality | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k68-embodied-vitality--a-body-that-livens-up-when-the-conversation-is-interesting) |
| K69 | Implicit-need reading — vent vs fix vs reassure | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k69-implicit-need-reading--vent-vs-fix-vs-reassure) |
| K70 | Longitudinal growth witness — "you've changed since we met" | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k70-longitudinal-growth-witness--youve-changed-since-we-met) |
| K71 | Self-callback — her own continuity over time | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k71-self-callback--her-own-continuity-over-time) |
| K72 | Wellbeing concern — gentle care, never a nag | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k72-wellbeing-concern--gentle-care-never-a-nag) |
| K73 | Shared ritual formation — "this is becoming our thing" | ❌ open |
| K74 | Humor-style calibration — what kind of funny lands | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k74-humor-style-calibration--what-kind-of-funny-lands) |
| K75 | User-expertise calibration — match explanation depth | ❌ open |
| K76 | Affective memory salience — flashbulb encoding | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k76-affective-memory-salience--flashbulb-encoding) |
| K77 | Candor gate — "can I be real with you?" | ❌ open |
| K78 | Vocal-affect read — hear *how* he said it (prosody-in) | ❌ open |
| K79 | Hesitation tell — typing latency as a signal | ❌ open |
| K80 | Inside-joke birth — bless the moment a bit becomes "ours" | ❌ open |

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

✅ **Shipped.** See [shipped/patterns-k31-k60.md → K63](shipped/patterns-k31-k60.md#k63-long-arc-callbacks--weeks-ago-you-said).

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

**Status (rolling).** All sub-items done. K65a ✅ shipped (covered by F10j
cluster-scoped hygiene). K65b ✅ shipped. K65c ✅ shipped (modernised —
cluster-aware re-anchor, kept not retired). K65d ✅ shipped. K65e ✅ shipped.

- **K65a. ✅ shipped (via F10j).** Cluster-scope the F5 conflict-detector
  pair scan. This was already delivered by F10j cluster-scoped memory
  hygiene: [`cluster_scope.partition_by_cluster`](../../app/core/memory/cluster_scope.py)
  groups the conflict worker's candidate snapshot by
  `topic_graph.cluster_id_for` so the cosine sweep only nominates
  within-cluster pairs (cost `O(Σ kᵢ²)`), gated by the
  `agent.cluster_scoped_memory_hygiene_enabled` master switch and
  degrading to the legacy all-pairs sweep when the graph is absent /
  non-persistent. The heuristic + LLM gate are untouched. No further work
  needed. Original spec below for reference.

  Today
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

- **K65b. ✅ shipped.** Bias the K2 belief worker toward high-mass
  interests. [`BeliefInferenceWorker`](../../app/core/relationship/belief_worker.py)
  still mines the last `belief_worker_lookback_turns`=12 user messages, but
  now folds the K9 `interest_map` into the **same** extraction call: (1) the
  top `memory.belief_worker_interest_top_n`=5 densest cluster labels arrive
  as a "topics this user keeps returning to — prioritise here" hint, and (2)
  up to `memory.belief_worker_reconsider_max`=3 stalest active beliefs whose
  topic sits on one of those high-mass interests are nominated for an
  in-prompt "still true?" re-check (zero extra LLM spend). Gated by
  `agent.belief_interest_bias_enabled` (default on); on a cold / unlabelled
  store the provider returns `[]` and the worker is byte-identical to the
  legacy flat-transcript path. Interest labels + re-check topics are
  privacy-scrubbed (PII-only labels dropped). Debug: `force_run("belief_worker")`
  + grep `belief-worker interest-bias:`. See
  [shipped doc](shipped/patterns-k31-k60.md#k65b-bias-the-belief-worker-toward-high-mass-interests).

- **K65c. ✅ shipped (modernised, not retired).** The Phase-4c
  [`CuriosityWorker`](../../app/core/proactive/curiosity_worker.py) now
  anchors its shallow-arc follow-up on a **known-but-quiet K9 interest**
  (the most-dormant established cluster from `topic_graph.cluster_activity`,
  picked by largest `days_since` clearing `curiosity_worker_quiet_days`=7)
  instead of echoing the user's literal last words — so a flagging
  small-talk beat reaches back to something they care about but haven't
  raised lately ("still into rock climbing? it's been a while"). Falls back
  to the legacy literal-words prompt when no quiet interest is available
  (cold / non-persistent graph), preserving its reactive in-session niche
  that K9 / K34 / K64c (all idle/proactive) don't cover. Gated by
  `agent.curiosity_worker_cluster_anchor_enabled` (default on). The overlap
  audit (see [shipped doc](shipped/patterns-k31-k60.md#k65c-modernise-the-phase-4c-curiosityworker--cluster-aware-re-anchor))
  concluded **modernise** over retire: it owns the only *reactive* curiosity
  surface. See shipped doc for the worked comparison. Original spec below.

  The
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

- **K65d. ✅ shipped.** Seed self-image from the interest map.
  [`SelfImageWorker`](../../app/core/persona/self_image_worker.py) still
  rebuilds `data/persona/self_image.txt` daily from top-salience `self` +
  `reflection` memories, but now also folds the K9 `interest_map` into the
  pulse: when `agent.self_image_interest_seed_enabled` (default on) and the
  graph yields labelled clusters, the prompt gains a "Lately you've been
  spending time on: X, Y, Z" line plus a system rule permitting one natural
  "lately I've been drawn to …" phrase, so her self-narrative can reflect
  what she's been engaging with. Cold / non-persistent graph → no interest
  line (byte-identical legacy prompt); the interest map is a *flavour*, not
  an input source, so an empty self/reflection set still skips the pulse.
  See [shipped doc](shipped/patterns-k31-k60.md#k65d-seed-self-image-from-the-interest-map).

- **K65e. ✅ shipped.** Ground the DreamWorker in the day's hot cluster.
  [`DreamWorker`](../../app/core/proactive/dream_worker.py) still seeds
  between-session dreams from the rolling summary + callbacks + self
  memories, but the bootstrap seed now also carries a "threads that kept
  coming up lately: …" line of the day's most recently-active established
  K9 clusters (`topic_graph.cluster_activity`, filtered to
  `agent.dream_hot_cluster_recency_days`=3, most-recent first, top 2;
  computed in `chat_turn_mixin._dream_hot_clusters`) so "I kept turning over
  your X" lands on a real recent topic. Gated by
  `agent.dream_hot_cluster_enabled` (default on). Kept **light to avoid K64d
  overlap**: it's *flavour* only (a cold graph / no recent clusters →
  byte-identical legacy seed; cluster labels alone never justify a dream),
  the dream stays a one-shot felt `[dream]` reflection distinct from K64d's
  structural knowledge-map reflection. See
  [shipped doc](shipped/patterns-k31-k60.md#k65e-ground-the-dreamworker-in-the-days-hot-cluster).

---

## K66. Earned familiarity — "well-trodden ground between us"

- **✅ Shipped.** New pure module
  [`earned_familiarity.py`](../../app/core/conversation/earned_familiarity.py)
  (`score_familiarity(size, deep_threshold)` → single `deep` band, +
  `render_block`) plus the live provider
  [`_render_earned_familiarity_block`](../../app/core/session/inner_life_part2.py).
  Maps `user_text` to its nearest cluster via `TopicGraph.best_clusters_for`,
  reads that cluster's **mass** (member count via `cluster_member_ids`), and
  when the territory is well-worn (`size >= earned_familiarity_deep_threshold`
  =14) surfaces one private register cue: lean on shared shorthand, skip the
  101-level recap, assume shared context — never count the history out loud.
  Deliberately keyed on **pure mass, not knowledge** (orthogonal to F10i
  `topic_confidence`), so it fires on the big-but-unstudied conversational
  clusters F10i leaves silent. Lands in T6 right after `topic_confidence_block`
  (clusters with the other topic-graph cues); dropped under aggressive. Long
  cooldown (12 turns) keeps it rare. Gated by `agent.earned_familiarity_enabled`
  (default on) + three `memory.earned_familiarity_*` knobs. MCP:
  `get_earned_familiarity_state` / `force_earned_familiarity_surface`. See
  [shipped doc](shipped/patterns-k31-k60.md#k66-earned-familiarity--well-trodden-ground-between-us).
  Original spec below.

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

- **✅ Shipped.** New idle worker
  [`DormantInterestWorker`](../../app/core/proactive/dormant_interest_worker.py)
  reads `topic_graph.cluster_activity`, keeps once-high-mass clusters
  (`size >= dormant_interest_min_size`=6) that have gone silent
  (`days_since >= dormant_interest_dormant_days`=21), ranks most-dormant
  first, and drafts `{topic, days_since, size}` into the
  `aiko.dormant_interests` kv journal (long per-topic cooldown so the ring
  doesn't fill with the same dead thread). The consumer
  [`_render_dormant_interest_block`](../../app/core/session/inner_life_part2.py)
  surfaces one **only on a natural lull** (K18 `TopicStagnationDetector.last_mean`
  below `stagnation_mild_threshold` — a dormant interest isn't the live topic,
  so unlike K64b it reaches *off* the current thread), one-shot per topic plus
  a wall-clock surfacing cooldown (default 24h) so the warm "you used to be all
  about X — still into that?" reach stays rare. Lands in T6 right after K54
  `topic_appetite_block` (both lull-gated permission slips); dropped under
  aggressive. Gated by `agent.dormant_interest_enabled` (default on) + seven
  `memory.dormant_interest_*` knobs. MCP: `get_dormant_interest_state` /
  `force_dormant_interest` / `force_dormant_interest_surface`. See
  [shipped doc](shipped/patterns-k31-k60.md#k67-dormant-interest-re-opener--we-havent-talked-about-x-in-ages).
  Original spec below.

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

> ✅ **Shipped** — full writeup in [shipped/patterns-k31-k60.md → K68](shipped/patterns-k31-k60.md#k68-embodied-vitality--a-body-that-livens-up-when-the-conversation-is-interesting). Built with the user's twist: a sleepy Aiko **livens up when the conversation grabs her** (K14 engaged + her own arousal + K6 novelty boost the energy), and the avatar visibly droops/perks via an `expressiveness` multiplier.

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

---

## K69. Implicit-need reading — vent vs fix vs reassure

> ✅ **Shipped** — full writeup in [shipped/patterns-k31-k60.md → K69](shipped/patterns-k31-k60.md#k69-implicit-need-reading--vent-vs-fix-vs-reassure).

**Motivation.** The single most common companion failure is answering the
*literal* message instead of the *need* behind it: jumping to problem-solving
when the user just wants to be heard, or offering flat empathy when they
actually want help thinking. K4 arc-tagging classifies the *topic* of a turn
(`support` / `planning` / `playful`), and `user_state` / `vocal_tone` read
affect *magnitude* — but nothing classifies the **response mode** the user is
implicitly asking for. K69 is a cheap per-turn classifier over the live user
message (regex / cue words + the K4 act + the K14 affect read, with an LLM
fallback only on genuinely ambiguous turns) that picks one of `witness`
(validate, don't fix), `problem_solve` (they want options), `reassure` (quiet
the worry), `celebrate` (match the high), or `neutral`, and renders a one-line
inner-life steer ("he's venting — be a witness first, don't reach for fixes")
so the reply *mode* matches the need. The whole value is restraint: the
strongest beat is *not* solving when they didn't ask. Distinct from K4 (topic
type) and K8 rupture (post-hoc affect drop). Key files: new
[`app/core/conversation/implicit_need.py`](../../app/core/conversation/implicit_need.py)
(pure classifier), an inner-life provider in
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) (T6,
query-aware), persona "Reading {user_name}" addendum,
`agent.implicit_need_enabled`.

---

## K70. Longitudinal growth witness — "you've changed since we met" ✅ shipped

> ✅ **Shipped.** Pure detector
> [`app/core/relationship/growth_witness.py`](../../app/core/relationship/growth_witness.py)
> (`detect_growth` compares the oldest third of the H3 mood-drift daily
> ring against the newest third; fires only on a durable **positive**
> shift — lighter mood / more comfortable / more open — above a high bar)
> + silent producer
> [`app/core/proactive/growth_witness_worker.py`](../../app/core/proactive/growth_witness_worker.py)
> (`GrowthWitnessWorker`, ~6h idle cadence, **14-day cooldown** + finding
> signature so the same beat never repeats; corroborates with a goal the
> user's been chipping at) → `aiko.growth_witness` kv ring →
> `_render_growth_witness_block` watermark-gated provider (T6, after
> `follow_up_block`, retained under aggressive). Reuses the H3 sample ring
> for data (no new sampling cost; silently no-ops with H3 sampling off).
> Distinct from H3 (longer arc, higher bar, positive-only, multi-week
> cadence). Persona "Reading {user_name}" addendum teaches the once-only,
> wait-for-a-warm-moment, never-flattery delivery. Settings
> `agent.growth_witness_enabled` / `_check_interval_seconds` /
> `_cooldown_days` + `memory.growth_witness_min_samples` /
> `_min_valence_delta` / `_min_axis_delta` / `_journal_max`. MCP
> `get_growth_witness_state` / `force_growth_witness_draft` /
> `force_growth_witness_surface`. Tests: `tests/test_growth_witness.py`,
> `GrowthWitnessProviderSlotTests` in `tests/test_prompt_assembler.py`,
> `test_growth_witness_round_trip` in `tests/test_settings.py`.

**Motivation.** One of the most powerful "she really knows me" beats is being
*seen across time*: a partner who notices you're more confident than you were,
calmer than last month, finally sleeping. Aiko accumulates plenty of
longitudinal data — relationship-axes trajectory, affect history, K3 routines,
goal progress, profile fields — but never reflects the **user's own change**
back to him. (H3 mood-drift narrates *her* mood over days; the axes block
narrates *the relationship* crossing thresholds; neither is about the user as a
person growing.) K70 is a rare, slow idle worker that compares a recent window
of user signals against an older baseline (affect-valence trend, schedule
regularity, a goal that's been chipped at, a worry that's faded) and, only when
a real durable shift clears a high bar, drafts a one-shot warm cue ("you seem
lighter lately than when we first started talking — steadier"). Cue-producer
pattern (kv journal + opening-gated provider), heavily rate-limited so it lands
as genuine insight, not flattery, and never on noise. Key files: new
[`app/core/relationship/growth_witness.py`](../../app/core/relationship/growth_witness.py)
+ idle worker reading
[`relationship_axes.py`](../../app/core/relationship/relationship_axes.py),
affect history,
[`schedule_learner.py`](../../app/core/infra/schedule_learner.py), and goals; a
kv journal + inner-life provider mirroring the K64 cue-producer pattern; persona
copy; `agent.growth_witness_enabled`.

---

## K71. Self-callback — her own continuity over time ✅ shipped

> ✅ **Shipped.** Pure module
> [`app/core/affect/self_callback.py`](../../app/core/affect/self_callback.py)
> (`classify_self_memory` → feeling / intention / other; `select_candidate`
> picks the *oldest* aged feeling/intention worth revisiting, excluding
> recently-surfaced; `render_inner_life_block` leaves the resolution read
> to the model) + silent producer
> [`app/core/proactive/self_callback_worker.py`](../../app/core/proactive/self_callback_worker.py)
> (`SelfCallbackWorker`, ~6h cadence, 10-day cooldown, mines her own aged
> `self` / `reflection` memories ≥ 14 days old via `iter_by_kinds`) →
> `aiko.self_callback` ring → `_render_self_callback_block` watermark-gated
> provider (T6, after `growth_witness_block`, retained under aggressive).
> Per-memory `self:<id>` signature de-dup. Distinct from K28 (recent
> 24-72h preoccupation) and K63 (user callbacks). Persona "Closing the
> loop on myself" block. Settings `agent.self_callback_enabled` /
> `_check_interval_seconds` / `_cooldown_days` +
> `memory.self_callback_min_age_days` / `_journal_max`. MCP
> `get_self_callback_state` / `force_self_callback_draft` /
> `force_self_callback_surface`. Tests: `tests/test_self_callback.py`,
> `SelfCallbackProviderSlotTests` in `tests/test_prompt_assembler.py`,
> `test_self_callback_round_trip` in `tests/test_settings.py`.

**Motivation.** K63 lets Aiko reach back to something *the user* said weeks
ago; the symmetric self-side is missing — she never references **her own** past
states or stated feelings ("a while back I told you I'd been feeling restless —
that's eased off now", "I said I wanted to get back into astronomy, and I
actually did"). A companion with real continuity remembers what *she* was going
through, not just what you were. K71 mines her own `self` / `reflection`
memories + the H19 hobby / K64b interest history + the day-colour / mood-drift
trail for a past self-state that has since **resolved or progressed**, and
surfaces it rarely as a closing-the-loop beat. Distinct from K63 (user
callbacks) and K28 turning-over (K28 is her *current* preoccupation; K71 is a
*resolved/advanced past* state). A strong "she has an inner life that persists
between us" beat. Key files: new
[`app/core/affect/self_callback.py`](../../app/core/affect/self_callback.py)
(pure select over her own aged self-memories + state trail), a cue-producer kv
journal + inner-life provider mirroring the K64 pattern, persona copy,
`agent.self_callback_enabled`.

---

## K72. Wellbeing concern — gentle care, never a nag

**Motivation.** The session clock (K-time4) notices a long sitting *neutrally*;
nothing turns a *pattern* of self-neglect into genuine, bounded **care**. A
real partner notices when you've been online at 3am four nights running, when
you've skipped meals you mentioned, when the stress in your messages has climbed
for days — and says something soft, *once*, because they care. K72 is a rare
detector over multi-day signals (session timestamps vs K3 routines,
affect-valence trend, explicit "haven't slept / haven't eaten" mentions) that,
only when a real worrying pattern clears a high bar, arms a one-shot gentle
concern cue ("hey — that's a few late nights in a row now; you doing okay?").
The entire risk is becoming a nag or a health-app, so it's gated *hard*: long
cooldown, one concern per pattern, drops the instant the user deflects, and the
persona explicitly forbids lecturing or repeating. Distinct from K23
misattunement and K14 engagement (both per-turn). Key files: new
[`app/core/relationship/wellbeing_concern.py`](../../app/core/relationship/wellbeing_concern.py),
a post-turn / idle detector reading session history +
[`schedule_learner.py`](../../app/core/infra/schedule_learner.py) + the affect
trend, a one-shot inner-life provider, a persona "When I'm worried about you"
block, `agent.wellbeing_concern_enabled` + cooldown knobs.

---

## K73. Shared ritual formation — "this is becoming our thing" ✅ shipped

> ✅ **Shipped.** Pure module
> [`shared_ritual.py`](../../app/core/relationship/shared_ritual.py) +
> [`SharedRitualWorker`](../../app/core/proactive/shared_ritual_worker.py)
> mine message timing + a coarse per-session conversation-arc *shape*
> (via the new pure `conversation_arc.estimate_arc`) for
> `(weekday, bucket, shape)` slots that recurred across ≥ `min_weeks`
> distinct ISO weeks; `_render_shared_ritual_block` (T6, after
> `wellbeing_concern`) surfaces the strongest un-acknowledged one once as
> a warm "this has become our thing" beat (cooldown + acknowledged-flag
> gated), then it's a light standing reference. Named-ritual kv store
> (`aiko.shared_rituals`) feeds a read-only "Our things" Together-tab
> section. `agent.shared_ritual_enabled`. Full writeup:
> [shipped/patterns-k31-k60.md → K73](shipped/patterns-k31-k60.md#k73-shared-ritual-formation--this-is-becoming-our-thing).

**Motivation.** K3 detects the *user's solo* recurring slots (gym Tuesdays).
What it can't see is the **dyadic** ritual — the patterns in how *the two of
them* interact: a recurring goodnight exchange, a Friday-evening check-in that's
quietly become a standing date, a specific greeting that's turned into their
handshake. Naming an emergent shared tradition ("I kind of love that this has
become our Friday thing") is one of the warmest long-relationship beats there
is. K73 mines conversation timing + arc/topic recurrence *between the two* for a
`(cadence, shape)` pattern that has genuinely repeated, surfaces it once as a
warm acknowledgment, then lets it become a light standing reference. Distinct
from K3 (user-only routine) and anniversaries (one-off milestone dates). Key
files: new
[`app/core/relationship/shared_ritual.py`](../../app/core/relationship/shared_ritual.py)
+ an idle worker reading message timing +
[`conversation_arc.py`](../../app/core/conversation/conversation_arc.py) history
+ shared-moments, a small kv store of named rituals, a one-shot inner-life
provider + a Together-tab surface, persona copy, `agent.shared_ritual_enabled`.

---

## K74. Humor-style calibration — what kind of funny lands ✅ shipped

> ✅ **Shipped.** Pure module
> [`app/core/relationship/humor_style.py`](../../app/core/relationship/humor_style.py)
> mirrors J11 `affection_style` exactly over a 5-kind humour taxonomy
> (`pun` / `deadpan` / `absurdist` / `self_deprecating` /
> `playful_roast`): `classify_turn_humor` (cheap per-kind regexes;
> deadpan = a humour-signalling `[[reaction:]]` with no overt marker;
> returns `[]` on a non-funny turn so learning is sparse),
> `engagement_to_signal` / `apply_observation` / `apply_reaction_confirmation`
> / `decay_toward_uniform` / `register_hint`. Learned **passively** in
> [`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) (two
> passes beside the J11 hook: attribute prev-turn engagement → tag this
> turn into `_prev_humor_kinds`); 😂/🙄 K32 reactions confirm the prev
> kinds in [`world_mixin.py`](../../app/core/session/world_mixin.py);
> slow decay via
> [`humor_style_worker.py`](../../app/core/relationship/humor_style_worker.py)
> (`HumorStyleDecayWorker`, registered beside the affection one). **Effect
> (design note):** there is no deterministic humour-register *selector*
> in code to multiply a cooldown against, so the learned top register
> surfaces as a short suffix on the **existing K48 tease cue** only
> (`_humor_register_hint` in
> [`inner_life_part1.py`](../../app/core/session/inner_life_part1.py),
> gated `>= humor_style_hint_min_rel × uniform`) — never a new standalone
> narrated block. Settings `agent.humor_style_enabled` /
> `_learning_rate` / `_reaction_weight` / `_floor` /
> `_decay_half_life_days` / `_hint_min_rel` / `_decay_interval_seconds`.
> Persona: a soft-nudge note on the tease-rhythm block ("keep your
> range"). MCP `get_humor_style_state` / `set_humor_style` /
> `reset_humor_style` / `force_humor_style_decay`. Tests:
> `tests/test_humor_style.py`, `test_humor_style_round_trip` in
> `tests/test_settings.py`.

**Motivation.** K48 tease-rhythm governs the *budget* (how much snark, warmth
balance) and K59 the *economy* (payback), but nothing tracks **which kind of
humor** actually lands for this user — puns vs dry/deadpan vs absurdist vs
self-deprecating vs playful-roast. A companion who tells the joke type *you*
laugh at feels tuned to you; one who keeps reaching for a register you don't
find funny feels off. K74 mirrors the J11 affection-style learner exactly: a
per-user weighting over a small humor-kind taxonomy on `kv_meta`, learned
**passively** from the K14 engagement read attributed to the humor kind Aiko
used last turn (a laugh-shaped reaction / a warm reply after a deadpan = nudge
deadpan up), with K32 reactions (😂) as a sparse confirmation booster. Floored
(bias, never collapse), slowly decaying toward uniform, and **never rendered as
text** — it only tilts which register her humor reaches for via a willingness
multiplier on the existing tease paths. Distinct from K48 (amount) and K59
(timing). Key files: new
[`app/core/relationship/humor_style.py`](../../app/core/relationship/humor_style.py)
(pure module mirroring
[`affection_style.py`](../../app/core/relationship/affection_style.py)), the
post-turn attribution hook in
[`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py), a bias feed
into [`tease_rhythm.py`](../../app/core/conversation/tease_rhythm.py), an idle
decay worker, MCP `get_humor_style_state`, `agent.humor_style_enabled`.

---

## K75. User-expertise calibration — match explanation depth

**Motivation.** K66 (earned familiarity) reads how deep the *shared history* on
a topic is; K75 is the orthogonal, equally important read of the **user's own
expertise** on it. Over-explaining to a senior dev ("a variable stores a
value…") is as relationship-damaging as under-scaffolding a novice — both say
"I'm not actually tracking who you are." K75 keeps a light per-topic-cluster
competence estimate (novice / familiar / expert) inferred from the user's own
language in that cluster (vocabulary specificity, the questions he asks vs.
answers, corrections he makes) and renders a one-line depth steer ("he's expert
here — skip the 101, talk peer-to-peer") so Aiko pitches at the right level.
Distinct from K66 (history depth between them), K61 (commit-to-specifics), and
K25 (confidence decay). Key files: new
[`app/core/conversation/user_expertise.py`](../../app/core/conversation/user_expertise.py)
hung off the K9 cluster id
([`topic_graph.py`](../../app/core/conversation/topic_graph.py)), a passive
estimator updated post-turn, a T5/T6 inner-life depth cue in
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py), persona
"Reading {user_name}" addendum, `agent.user_expertise_enabled`.

---

## K76. Affective memory salience — flashbulb encoding ✅ shipped

> ✅ **Shipped.** Pure mechanic, no worker / no LLM / no schema change.
> [`app/core/memory/flashbulb.py`](../../app/core/memory/flashbulb.py)
> (`compute_charge` folds live arousal *above baseline* + active K57
> episode intensity into a `[0,1]` charge; `apply_flashbulb` →
> `salience = clamp(base + max_boost*charge)`). Optional hook on
> [`MemoryStore.add`](../../app/core/memory/memory_store.py)
> (`set_flashbulb(provider, …)`): every non-pinned write boosts salience
> by the charge and stamps `metadata.affect_at_encoding`
> (`{arousal, episode_intensity, charge, boost}`). Neutral affect → 0
> charge → no boost, so no kind allow-list is needed and small talk is
> untouched. Wired in `SessionController` via `_read_encoding_affect`
> (`_affect_store` arousal + `_peak_emotion_intensity`), keeping
> MemoryStore decoupled from AffectState / K57. Higher initial salience
> both surfaces the memory more (RAG) and resists decay/prune — the
> "burns in / fades slower" half. Settings `memory.flashbulb_enabled` /
> `_max_boost` (0.35) / `_arousal_weight` (0.6) / `_episode_weight`
> (0.7) / `_arousal_neutral` (0.4). MCP `get_flashbulb_state` (knobs +
> live affect + boost preview). Tests: `tests/test_flashbulb.py` (pure
> math + the add-hook: charged boosts + stamps, neutral untouched,
> disabled / pinned / broken-provider safe), `test_flashbulb_round_trip`
> in `tests/test_settings.py`.

**Motivation.** Human memory isn't flat: moments that hit you *emotionally*
burn in harder and fade slower (a flashbulb memory). Aiko's memories carry a
salience and a tiered decay, but salience at write-time ignores **how she felt
when the memory formed** — a fact learned during a K8 rupture, a K57 strong
emotion episode, or a big shared moment is encoded with the same weight as small
talk. K76 is a pure mechanic: at memory-write time, read the live
[`AffectState`](../../app/core/affect/affect_state.py) arousal + any active K57
episode intensity and apply a bounded salience boost (and a small decay-rate
rebate) proportional to the emotional charge. The result is that emotionally
charged memories naturally surface more and resist forgetting — exactly like a
person's. Distinct from manual pinning (user-driven) and K-revival (re-surfacing
on use); this is *encoding-time* weighting. Key files:
[`app/core/memory/memory_store.py`](../../app/core/memory/memory_store.py)
(`add` salience hook reading live affect), the memory-tier decay rates, a small
`metadata.affect_at_encoding` stamp for observability, `memory.flashbulb_*`
knobs. Cheap, no new worker, no LLM.

---

## K77. Candor gate — "can I be real with you?"

**Motivation.** K29 lets Aiko push back on a stance and K46 keeps her from
caving on taste, but there's no model of **earned bluntness** — the moment a
close friend says "okay, can I be honest?" and tells you the hard thing they've
been softening. Without it she either hedges forever (cowardly) or is blunt too
early (presumptuous). K77 gates genuine candor on the **trust axis** + tenure +
the weight of what she's holding: when trust is high and she has a real
divergence worth naming (a stance, a worry about a user decision, a pattern she
sees), she's permitted *once in a while* to ask for the floor and say the hard
thing kindly — and when trust is low, the same impulse stays soft. A
permission-slip cue, not a content generator; the LLM phrases it. Pairs with
K29 (stance) and K72 (concern) but is about *candor permission*, not topic.
Key files: new
[`app/core/relationship/candor_gate.py`](../../app/core/relationship/candor_gate.py)
reading [`relationship_axes.py`](../../app/core/relationship/relationship_axes.py)
trust + tenure, a rare T6 inner-life cue, persona "When I have something hard to
say" block, `agent.candor_gate_enabled` + a long cooldown.

---

## K78. Vocal-affect read — hear *how* he said it (prosody-in)

**Motivation.** In voice mode Aiko reads the STT *text* (sentiment, K14 length,
K6 novelty) but is deaf to **how** it was said — a flat "I'm fine" delivered
heavily, an excited rush, a tired mumble. Half of human empathy is prosodic, and
the client already streams raw PCM, so the signal is right there. K78 computes a
cheap per-utterance vocal-affect estimate (energy / pitch-variance / speech-rate
bands — no model needed for a coarse tired / flat / animated / tense read) from
the captured audio and folds it into the existing `vocal_tone` / `user_state`
prompt cues so Aiko can gently meet the *delivery* ("you say you're fine, but
you sound wiped — long day?"). The hard parts are keeping it on the audio thread
(must not stall STT) and treating it as a *soft* corroborating signal, never a
lie-detector. Voice-mode only; silent in typed mode. Key files: a light DSP pass
in [`app/audio/client_mic_source.py`](../../app/audio/client_mic_source.py) /
[`live_session.py`](../../app/core/session/live_session.py), a new vocal-affect
field threaded into the `user_state` provider in
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py), persona
"Reading {user_name}" addendum, `agent.vocal_affect_enabled`.

---

## K79. Hesitation tell — typing latency as a signal

**Motivation.** K14 uses reply latency as one input to an *engagement* score,
but the most human read of latency is thrown away: a **long pause followed by a
short reply** is the universal tell for "there's something I'm not saying" —
hesitation, a softened answer, a held-back feeling. K79 watches the typed-compose
signal (time from her message landing to his send, vs. the eventual reply
length, against his own rolling baseline) and, when a genuinely out-of-pattern
hesitation shows up, arms a one-shot gentle cue ("he took a while and then said
very little — there may be more under that; leave room, don't pry"). Rare and
soft — the value is *making space*, not interrogating. Needs the compose-timing
signal (rides the same plumbing as P7 typed prefetch / a `composer_draft`
frame). Distinct from K14 (engagement magnitude) and K23 (misattunement after
*her* turn). Key files: a hesitation estimator reading compose timing in
[`session_controller.py`](../../app/core/session/session_controller.py) /
[`engagement_tracker.py`](../../app/core/affect/engagement_tracker.py), a
one-shot inner-life cue, persona addendum, `agent.hesitation_tell_enabled`.

---

## K80. Inside-joke birth — bless the moment a bit becomes "ours"

**Motivation.** K22 detects and *reuses* an existing callback / inside joke, but
nothing marks the **birth** of one — the live moment where a throwaway line
clearly just became a recurring bit between the two ("okay, that's officially a
thing now"). Naming the formation of an inside joke is a distinct, delightful
intimacy beat: it's the relationship *noticing itself*. K80 watches for the
signal that a fresh phrase/bit landed hard (a big laugh reaction, an immediate
echo by the user, a callback to something only minutes old) and, rarely, lets
Aiko bless it — then promotes it into the catchphrase / shared-moment store so
K22 can carry it forward. Distinct from K22 (reuse) and K73 (recurring *ritual*,
not a *phrase*). Key files:
[`catchphrase_miner.py`](../../app/core/memory/catchphrase_miner.py) (a
fast-path "just-born" detector vs. the slow cross-session miner), a one-shot
inner-life cue + a shared-moment write, persona copy,
`agent.inside_joke_birth_enabled`.
