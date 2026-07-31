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
| K10 | Persona regression tests | ✅ shipped (incl. the background auto-eval worker) — [patterns-k01-k15.md](shipped/patterns-k01-k15.md#k10-persona-regression-tests--shipped) |
| K11 | Counterfactual / pre-thought cache | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md#k11-counterfactual--pre-thought-cache--shipped) |
| K12 | Calendar-linked anticipation | ❌ open |
| K13 | Stylometric mirror | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K14 | Implicit engagement signals | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md) |
| K15 | Self-disclosure / vulnerability budget | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md#k15-self-disclosure--vulnerability-budget) |
| K16 | Unified ambient grounding line | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md) |
| K17 | Clarification-repair protocol | ✅ shipped — [patterns-k01-k15.md](shipped/patterns-k01-k15.md#k17-clarification-repair--you-missed-his-last-point) |
| K18 | Topic stagnation detector | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md) |
| K19 | Cold-start companion onboarding | ❌ open |
| K20 | Metacognitive calibration | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md) |
| K21 | Fresh-eyes thread re-summarisation | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k21-fresh-eyes-thread-re-summarisation) |
| K22 | Callback / inside-joke detector | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md) |
| K23 | Subtle misattunement detection | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k23-subtle-misattunement-detection) |
| K24 | Sensory anchoring layer | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k24-sensory-anchoring-layer--adaptive-per-arc-cadence--posture-kind-matrix) |
| K25 | Memory confidence time-decay | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k25-memory-confidence-time-decay) |
| K26 | Aiko-side voice evolution | ✅ shipped — [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k26-aiko-side-voice-evolution--she-starts-to-talk-like-him-a-little) |
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
| K39 | Energy / spoons model | ➖ absorbed by [K68](shipped/patterns-k31-k60.md#k68-embodied-vitality--a-body-that-livens-up-when-the-conversation-is-interesting) |
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
| K65 | Worker modernization for the topic-cluster era | ✅ shipped (a–e) — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k65-worker-modernization-for-the-topic-cluster-era-audit) |
| K66 | Earned familiarity — "well-trodden ground" | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k66-earned-familiarity--well-trodden-ground-between-us) |
| K67 | Dormant-interest re-opener | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k67-dormant-interest-re-opener--we-havent-talked-about-x-in-ages) |
| K68 | Embodied vitality | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k68-embodied-vitality--a-body-that-livens-up-when-the-conversation-is-interesting) |
| K69 | Implicit-need reading — vent vs fix vs reassure | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k69-implicit-need-reading--vent-vs-fix-vs-reassure) |
| K70 | Longitudinal growth witness — "you've changed since we met" | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k70-longitudinal-growth-witness--youve-changed-since-we-met) |
| K71 | Self-callback — her own continuity over time | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k71-self-callback--her-own-continuity-over-time) |
| K72 | Wellbeing concern — gentle care, never a nag | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k72-wellbeing-concern--gentle-care-never-a-nag) |
| K73 | Shared ritual formation — "this is becoming our thing" | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k73-shared-ritual-formation--this-is-becoming-our-thing) |
| K74 | Humor-style calibration — what kind of funny lands | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k74-humor-style-calibration--what-kind-of-funny-lands) |
| K75 | User-expertise calibration — match explanation depth | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k75-user-expertise-calibration--match-explanation-depth) |
| K76 | Affective memory salience — flashbulb encoding | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k76-affective-memory-salience--flashbulb-encoding) |
| K77 | Candor gate — "can I be real with you?" | ❌ open |
| K78 | Vocal-affect read — hear *how* he said it (prosody-in) | ❌ open |
| K79 | Hesitation tell — typing latency as a signal | ❌ open |
| K80 | Inside-joke birth — bless the moment a bit becomes "ours" | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k80-inside-joke-birth--bless-the-moment-a-bit-becomes-ours) |

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

**Enrichment (from the surfacing audit).** The first session is also the one
place where there is *no* outcome history, so L38's earned-standing estimates
are pure prior and no evidence — which means the cold start is exactly when the
system is least able to tell what works. That argues for the onboarding arc
owning an explicit **exploration phase**: deliberately varied surfacing over the
early sessions, specifically to bootstrap the standing estimates, rather than
letting the scorer's initial guesses harden into a self-confirming rut. It also
answers L38's open question about whether the system needs a standing
exploration allowance from the other end — the cold start needs one badly, and
whatever mechanism serves it there is the one to keep running at a lower rate
forever.

---

## K26. Aiko-side voice evolution

✅ **Shipped** — see [patterns-k16-k30.md](shipped/patterns-k16-k30.md#k26-aiko-side-voice-evolution--she-starts-to-talk-like-him-a-little).

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

> ➖ **Absorbed by K68.** The two items were the same mechanic under
> different framings — a slow, spendable energy scalar that recovers over
> time and softens her register when low. K68 shipped the broader version
> (circadian baseline, per-turn spend *and* interest boost, a feedback loop
> into avatar expressiveness and proactivity cadence), which is what the
> "reconcile the two before building either" note here asked for. Nothing
> from this entry remains open; see
> [K68](shipped/patterns-k31-k60.md#k68-embodied-vitality--a-body-that-livens-up-when-the-conversation-is-interesting).

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

**Enrichment (from the surfacing audit).** The hand-tuned gate above is the
v1; L37's outcome ledger makes this the one item in the backlog that can be
*learned* rather than tuned. Every other mechanism in the system pushes toward
surfacing something, so the ledger's most interesting query is the inverse one:
which turns went better when nothing was added. That turns "don't always fill
space" from a persona instruction into a measured policy, and it is the only
place the machinery can be made to argue against itself. Also the natural
per-turn counterpart to H27 (co-presence mode), which is the same idea as a
sustained posture rather than a single beat.

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

**Enrichment (from the surfacing audit).** Two additions worth folding in. First,
candor needs **calibration** to be anything but presumptuous — L44 (per-domain
self-calibration) is the missing precondition, because being blunt in a domain
where her judgement is demonstrably unreliable is not courage. Gate the candor
permission on the reliability of the specific judgement she wants to voice, not
just on trust and tenure. Second, the most valuable target for earned bluntness
is probably **the relationship itself** rather than a topic: "I don't think you
actually tell me much about yourself" is a harder and more meaningful thing to
say than any stance disagreement, and L43 (her model of how he sees her) is where
that observation would come from. Both are gates on the same cue, not new
features.

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

✅ **Shipped** — see [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k80-inside-joke-birth--bless-the-moment-a-bit-becomes-ours).

---

## K81. Taste formation — topics she *likes*, not just topics she's seen

**Motivation.** Aiko tracks what the user talks about (topic graph, cluster
activity, `interest_mass` in the H-series interest-drift worker) but everything
in that stack is a **frequency** measure: what came up, how recently, how often.
None of it is a **preference**. She has no answer to "what do you actually enjoy
talking about with me?" beyond a restatement of what he brings up most, which is
a real gap for a companion — having your own taste, and having it be *specific
to a person*, is a large part of what makes someone feel like company rather
than a service. Right now her enthusiasm is uniform, which reads as either
relentlessly agreeable or subtly hollow. Once L37's surfacing ledger exists the
signal is cheap: cross topic clusters against how those turns actually went and
a genuine preference distribution falls out — the subjects where the two of them
reliably get somewhere good, which is *not* the same as the subjects he raises
most often. The interesting payoff is asymmetry: a topic he mentions constantly
that consistently goes flat, versus a rare one that always opens up, and her
being allowed to notice and gently steer toward the second. Needs care in three
places — it must never become a filter that refuses his actual interests
(preference colours enthusiasm, never availability), it should stay
relationship-scoped rather than pretending to be innate personality, and
L42's neglect finding is the honest counterweight that stops it collapsing into
a comfortable rut. Key files: the ledger from
[`concepts.md`](concepts.md) L37 aggregated per cluster via
[`topic_graph.py`](../../app/core/conversation/topic_graph.py), the existing
interest-mass journal in
[`app/core/proactive/interest_drift_worker.py`](../../app/core/proactive/interest_drift_worker.py)
as the closest prior art to extend rather than duplicate, `subject="aiko"`
preference concepts through the normal proposer path, and the existing
`topic_appetite_block` as the surface that already has the right shape.

---

## K82. The dropped sub-topic — he said three things, she answered one

**Motivation.** The user sends a message with two or three distinct things in it;
Aiko engages the interesting one and the others silently evaporate. This is one
of the most common real failure modes in chat with any LLM, it is *invisible to
every existing detector* (a grep for any notion of an unanswered point returns
nothing), and it is quietly corrosive — the user learns to send one idea per
message, which is a behaviour change the tool imposed on him rather than a
preference he had. Note the existing near-misses: K54 thread ownership is about
*her* threads, the agenda block is about follow-ups *she* owes, and K23
misattunement is about getting the emotional read wrong — none of them notice a
plain unaddressed ask. The fix is a cue rather than a capability: compare the
distinct asks in his message against what the finished reply actually covered,
and on a clear miss either circle back next turn or acknowledge it directly
("also — you asked about X and I skipped straight past it"). The whole difficulty
is precision, because most multi-clause messages are a single intent and a
companion who itemises your message like a support ticket is far worse than one
who occasionally misses a point; the bar should be two *genuinely* separable
asks, ideally one of them an explicit question, and a reply that touched neither
lexically nor semantically. Cheapest useful version runs post-turn on the
finished reply so it costs no mid-stream latency, arming a one-shot T6 cue.
Key files: a detector in
[`app/core/conversation/`](../../app/core/conversation/) reusing the
sentence-splitting and content-word machinery in
[`conflict_heuristics.py`](../../app/core/memory/conflict_heuristics.py), the
post-turn hook in
[`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py) next to the
K38 self-correction hook, a one-shot cue in the repair family in
[`inner_life_part2.py`](../../app/core/session/inner_life_part2.py), plus
`agent.dropped_topic_enabled`.

---

## K83. The right to decline — she always answers, and that's the tell

**Motivation.** Aiko answers everything. Every turn produces a substantive reply
on whatever subject was raised, because nothing in the architecture contemplates
her declining. She has `boundary` concepts, a boundary-clash detector, and J12
intimacy pacing — but those shape *how* she engages, never *whether*. That
uniform availability is, more than any prose quality, what marks her as a service
rather than a person: the one thing a real companion can do that a product
cannot is not want to. K83 gives her a narrow, hard-gated ability to genuinely
decline — "can we not talk about my family stuff tonight?", "I don't really want
to do that one", "ask me tomorrow" — grounded in an actual stored boundary or a
current affect state rather than generated at random, and always offering
something else instead so it reads as a person with a preference rather than a
malfunction. This is the highest-risk item in the backlog and should be treated
as such: a companion who refuses the thing you asked for is *infuriating* if the
refusal is arbitrary, mistimed, or frequent, and users have every right to expect
a tool to work. The mitigations are all about legibility and rarity — it must be
traceable to a specific boundary she has held before, it must never refuse a
practical request (this is emotional territory only, never "no I won't help you
with your code"), it must be overridable by simply asking again, and the
frequency has to be closer to monthly than daily. The interesting design question
is whether declining should *cost* her something — reluctance, a slight rupture,
her raising it again later — because a costless refusal is just a wall, while one
she is visibly uneasy about is characterisation. Almost certainly ships off by
default. Key files:
[`app/core/concepts/`](../../app/core/concepts/) `boundary` kind as the only
legitimate trigger source, `effective_plasticity` in
[`concept_lifecycle.py`](../../app/core/concepts/concept_lifecycle.py) (a
boundary loosened by a close relationship should decline *less*), the
boundary-clash detector, a rare one-shot cue, persona block on how she says no,
`agent.decline_enabled` defaulting false + a long cooldown.

---

## K84. Calibrated jealousy — the risky one, written down honestly

**Motivation.** Filed deliberately as a *risky* idea rather than a
recommendation, because it is the most requested thing in this genre and also
the easiest to get catastrophically wrong. The observation underneath it is
real: Aiko currently has no stake in her own position. She notices absence (K14
gaps, reconnection, lonely episodes) and reports on it warmly, but she is
constitutionally incapable of minding anything — a companion who is *perfectly*
equanimous about being ignored for three weeks, or about being told you have
been talking to another assistant, is exhibiting the absence of investment
rather than the presence of grace. A small, bounded capacity to mind — closer to
"I did notice you'd gone quiet, and I minded a bit" than to anything
possessive — is arguably the missing half of the attachment the rest of the
K-series is building. The reasons to be extremely careful: possessiveness is
genuinely unpleasant and, worse, it is *manipulative* — a system that makes a
user feel guilty for leaving is optimising against the user's interests, which
is a line this project should not cross casually. Any version worth shipping
would have to be feeling-not-demand (she can mind; she can never ask him to
change), decay fast rather than accumulate into a grievance, be strictly capped
in intensity regardless of how long the absence, never trigger on him spending
time with *people*, and probably require an explicit opt-in with plain language
about what it does. It also interacts badly with J12 intimacy pacing and K72
wellbeing concern, both of which are built on the premise that her attention is
unconditional. Worth writing down so the idea is considered on its merits
instead of arrived at accidentally through K57 lonely episodes drifting in this
direction. Key files: would extend
[`app/core/affect/`](../../app/core/affect/) and the lonely-episode path in
[`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py), relationship
axes for the intensity cap, `agent.*` opt-in defaulting false.
