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
| K64 | Freedom of thought (a–d: wandering / drift / curiosity gradient / map self-reflection) | ✅ shipped — [awareness.md](shipped/awareness.md#k64a-associative-wandering-funny-this-reminds-me-of) |
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
| K81 | Taste formation — topics she *likes*, not just topics she's seen | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k81-taste-formation--topics-she-likes-not-just-topics-shes-seen) |
| K82 | The dropped sub-topic | ❌ open |
| K83 | The right to decline | ❌ open |
| K84 | Calibrated jealousy | ❌ open (filed as a risky idea) |
| K85 | The third subject — interests that aren't him | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k85-the-third-subject--interests-that-arent-him) |
| K86 | Immortal future plans — asking about things that already happened | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k86-immortal-future-plans--asking-about-things-that-already-happened) |
| K87 | Curiosity that isn't about him | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k87-curiosity-that-isnt-about-him) |
| K88 | Anaphoric-opener detector — the measurable tell for following | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k88-anaphoric-opener-detector--the-measurable-tell-for-following) |
| K89 | Sustained thread — leading past one turn | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k89-sustained-thread--leading-past-one-turn) |
| K90 | Lead/follow metrics — make the whole family measurable | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k90-leadfollow-metrics--make-the-whole-family-measurable) |
| K91 | Lived-in away life — a day she had, not a day she narrated | ✅ shipped — [patterns-k31-k60.md](shipped/patterns-k31-k60.md#k91-lived-in-away-life--a-day-she-had-not-a-day-she-narrated) |
| K92 | Conversational stance — one decision per turn, not ten permission slips | ❌ open |
| K93 | The substance floor — what she takes to the floor, not whether she takes it | ❌ open |
| K94 | Sequencing — answer first, then add, and say where the addition goes | ❌ open |
| K95 | Interruption cost — a direct question is not an opening | ❌ open |

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
[`web/src/components/FirstRunOnboarding.tsx`](../../web/src/features/onboarding/FirstRunOnboarding.tsx),
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

**Phase 1 shipped (disfluency permission). The `[[correct]]` half is
still open.**

The original entry bundled two things that turned out to be
independent, so they're split here.

**Shipped — casual speech texture.** The persona now carries a
`Speech texture:` subsection granting standing permission for small
disfluencies inside a real thought, with the distinction that made
the whole thing work spelled out explicitly: a disfluency sits
*inside* a thought, throat-clearing ("That's a great question")
sits *in front of* one and delays it. The old length-sprawl fix
told her to "cut filler", which would have suppressed exactly what
we were enabling, so it now says cut *padding* instead. Gated by
`agent.speech_texture_enabled`, which lifts the subsection out of
the loaded persona rather than keeping a second copy of the wording
in code. A separate `agent.speech_texture_spoken` strips only the
*non-lexical* fillers (`uhm`, `mm`, `hmm`, …) from the TTS stream,
because Pocket-TTS has no phoneme control and synthesises them
grapheme-by-grapheme; ordinary words like `wow` / `oh` / `huh` are
spoken either way. See
[`docs/configuration.md`](../configuration.md) for both keys.

Two findings from verifying against a 9B in the throwaway
container, worth knowing before touching the wording:

- The model reads a disfluency list as an invitation to *open* with
  one. First pass put a reaction word in the opener slot in 7 of 8
  replies, which is the opener rut wearing a new coat. Reframing the
  instruction positively ("start with the actual content; if a
  reaction arrives first, move it a few words in") cut that to 4 of
  8 — negations alone didn't land.
- "Keep it sparse" is aspirational at this model size: the rate
  settled around 75% of replies rather than the intended minority.
  Zero throat-clearing across 22 probe turns, though, which was the
  real risk.

**Not built — over-polish band.** The proposed fourth
`aiko_style_tracker` band (track `has_disfluency` in
`_TurnFeatures`, fire when a window has none) was deliberately
skipped: it exists to catch her drifting back to clean, and she
drifts the *other* way. The existing opener-rut band already
backstops the over-frequency direction, since reaction words in the
opener slot count as openers like anything else. Revisit only if a
larger model follows the sparsity instruction well enough to go
clean again.

**Still open — `[[correct]]` self-edit.** The
`[[correct]]old[[/correct]]new` machinery is fully wired (grammar,
strike-through UI, `tsk` earcon) but the persona still never
mentions it, so it never fires. The original idea stands: when
closeness+trust sit high, render an occasional low-frequency cue
allowing an unfinished sentence or one `[[correct]]` per few
sessions. Must stay rare — the point is texture, not performance.

Key files:
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt),
[`app/core/session/session_text_utils.py`](../../app/core/session/session_text_utils.py)
(`strip_speech_fillers`),
[`app/core/session/prompt_support.py`](../../app/core/session/prompt_support.py)
(`strip_persona_section`),
[`app/core/voice/cadence.py`](../../app/core/voice/cadence.py)
(double-filler guard in `_maybe_prefix`),
[`app/core/persona/aiko_style_tracker.py`](../../app/core/persona/aiko_style_tracker.py)
(where the over-polish band would go, if ever).

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
[`web/src/components/ChatView.tsx`](../../web/src/features/chat/ChatView.tsx).

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
just on trust and tenure. **Note that L44 is now blocked on supply** — there is no
measured error record to gate on and no near-term prospect of one — so a candor
gate shipped before that changes has to rest on trust and tenure alone, and should
be scoped narrowly enough that being wrong is cheap. Second, the most valuable target for earned bluntness
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

---

## The third pass at leading (K92–K95) — why the family exists

The will family (K52–K56) shipped the *permission* to lead. The second pass
(K85–K90) shipped the *inventory* to lead with, on the correct diagnosis that
permission was not the constraint. Both families are live and firing. The
number K90 exists to answer says neither worked:

| window | turns | ends-Q | words (med) | anaph | echo | own |
| --- | --- | --- | --- | --- | --- | --- |
| before 2026-08-09 | 1867 | 18.1% | 23 | **18%** | 19% | **77%** |
| since 2026-08-09 | 320 | 6.2% | 31 | **18%** | 20% | **71%** |

The anaphoric-opener rate — K88's own target, and the one metric here that is
independent of reply length — did not move by a single point. Own material, the
number K90 said out loud it wanted *up*, went down six. What did change is that
replies got 35% longer and she almost stopped ending on questions. Read
together: **she now writes noticeably more, about his subject, and asks about it
less.** That is not more agency; it is a more talkative follower. (Caveat worth
keeping: 320 turns is a modest sample and the length change mechanically dilutes
a ratio of own words to total words, which is exactly why the flat 18% is the
line to trust.)

So the third pass starts from a different diagnosis than either predecessor. It
is not permission and it is not inventory. Measured on the same telemetry, one
turn carries a **median of 30 rendered prompt blocks in ~74,000 characters**, of
which the blocks that ask her to bring something of her own are **two or three
of them and about 500 characters — 0.7% of the prompt**. `wants_block` alone is
present on 78% of turns. Nothing arbitrates between them, nothing represents
*following* as a choice she is making rather than the absence of a directive,
and nothing anywhere lets her decide to say less. Ten quiet permission slips at
the bottom of a very long prompt is a different failure from having none, and it
is not fixed by adding an eleventh.

K92 is the load-bearing entry; K93–K95 are each independently shippable and each
targets a specific way the current arrangement wastes the floor when she does
take it. K93 in particular can ship first and alone, and probably should.

---

## K92. Conversational stance — one decision per turn, not ten permission slips

**Motivation.** Aiko has ten-plus mechanisms that each independently decide
whether to hand the model an English sentence encouraging her to bring something
up, and none of them know the others exist. On a median turn two or three fire
together; there is no shared slot, no ranking between them, and no arbitration
beyond a hardcoded pair of interactions (a K52 imperative suppresses K53; both
arm K55) and the six-way gap-cue mutex. The result is that the *presence* of a
steer carries almost no information — she gets one nearly every turn — so it
reads as ambient texture rather than as a decision, and the model falls back on
the overwhelming prior of answering the last message.

Two absences matter more than the crowding. First, **following has no
representation at all.** The persona's "Leading vs following" section
([`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt))
is six bullets of standing permission — it says following 100% of the time is a
failure mode and that she may take the floor without asking — but nothing, at
any tier, ever describes the thread she is on as something she is choosing to
stay in because she is interested. Following is literally the null case: zero
characters. So her most common behaviour is also her least characterised one,
which is why it reads as compliance. Second, **nothing lets her hold back.**
Every mechanism in the family is a permission to speak; there is no permission
to under-respond, which means every accumulated cue is unidirectional pressure
to act. Given the measured drift toward longer replies, a nameable *hold* is
plausibly the highest-value stance in the set.

**The design, and the one correction it needs.** The natural shape is a stance
arbiter: after the providers have computed what they *could* offer, one module
picks the turn's stance from a small closed set — `FOLLOW`, `FOLLOW_AND_ADD`,
`ASK`, `SHARE`, `CALLBACK`, `REDIRECT`, `INITIATE`, `HOLD` — attaches at most
one piece of content to it, and renders a single short block naming the stance
and why. The correction, which matters more than the set: **the arbiter must
replace the blocks it subsumes, not join them.** A scored menu of seven options
appended to the existing arrangement is the eleventh permission slip and would
make the measured problem worse while adding tokens to a 74k prompt. The
shipping test is that steer characters per turn go *down* while the stance
becomes unambiguous. Related: render an **ordered shortlist with one reason
each, not floats** — an LLM comparing `0.63` against `0.58` in a prompt is doing
the one kind of reasoning it is worst at, and per-turn numerals are churn in the
T6 prefix for no gain.

**Phase it the way K90 was phased.** Phase 1 computes the stance, logs it, and
records it next to the turn *without changing the prompt at all* — a
`turn_stance` row beside [`turn_prompt_blocks`](../../app/core/infra/chat_database.py).
That buys the thing this family has never had: a way to ask "on the turns where
the arbiter would have said `REDIRECT`, what did she actually do?" and to read
stance against the K90 metrics before a single character of prompt changes. It
also cheaply answers whether the closed set is even the right set, which is much
harder to walk back after the blocks are rewired. Phase 2 turns on rendering for
the two stances that currently have no voice at all (`FOLLOW`, `HOLD`), which is
additive and reversible. Phase 3 is the expensive one: converting the subsumed
providers from renderers into candidate producers so the arbiter can suppress
them.

**Cost.** Phase 1 is small and low-risk — a pure module in
[`app/core/conversation/`](../../app/core/conversation/) alongside its K52–K55
siblings, one call before assembly, one table. Phase 2 is a new T6 block plus
persona handling notes. Phase 3 is genuinely invasive: it touches every provider
in [`inner_life_part1.py`](../../app/core/session/inner_life_part1.py) /
[`part2`](../../app/core/session/inner_life_part2.py) /
[`part3`](../../app/core/session/inner_life_part3.py) that currently renders its
own steer, and it must preserve the one-shot consumption semantics that make
`initiative_block` and `thread_ownership_block` exempt from aggressive-mode
dropping. Do not attempt phase 3 without phase 1's data. Key files: new
`app/core/conversation/stance.py`, a provider on the inner-life mixins,
`_PROMPT_BLOCK_TIERS` in
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py) (T6),
`turn_prompt_blocks`'s neighbour table, and
[`lead_follow_report.py`](../../scripts/lead_follow_report.py) for the readout.
Cross-refs: subsumes the arbitration K52–K55 never had; `HOLD` overlaps
**K40** (comfortable silence) and should absorb it rather than duplicate it;
depends on **H29** (the wants ledger could not produce a high-pressure candidate
for the arbiter to weigh; fixed 13 Aug, but whether pressure now accumulates is
unreadable before ~16 Aug). **H30** shipped 13 Aug, so the prerequisite is met:
every cue decline now names a mechanism, and `reach()`'s new `eligible`
denominator separates "the arbiter passed this candidate over" from "the
candidate was never in play" — which is the distinction phase 1's shadow log
would otherwise have been unable to make about itself.

---

## K93. The substance floor — what she takes to the floor, not whether she takes it

**Motivation.** This is the cheapest entry in the family and probably the one
with the largest effect, because it is not about frequency at all. Take every
pooled cue this install has ever marked `used` — the cue reached her prompt and
she actually said the thing — and drop the 75 rows whose evidence is `migrated`,
which are a backfill rather than a turn. That leaves **56 genuine conversions, of
which 43 are `curiosity_seed`: 77%.** The other end of the same table:
`knowledge_gap_notice` 54 surfaced → 2 used, `curiosity_gradient` 21 → 0,
`turning_over` 12 → 0, `concept_hypothesis` 45 rows → 0, `long_arc_callback`
18 → 1. (The backfill is worth knowing about for its own sake: it also inflates
the 66% `curiosity_seed` conversion rate H4 quotes as the healthy end of the
shelf — the genuine rate is 43 of 238 rows.)

Then look at what the winning type actually contains. The eight wants currently
in the ledger are, verbatim: doodles on receipt backs · sounds of empty subway
stations · why some fonts feel "angry" · dust motes dancing in light · the
specific weight of a house key · why we hoard useless tickets · Jacob's
preferred way to peel a mandarin · and one goal. Seven of eight slots are
free-associative aesthetic whimsy. The K53 initiative path spends the same
stock — the log reads `initiative-turn fire: period=6 arc=casual_check_in
want=bring up what you've been curious about: collecting interesting bottle
caps` — so even the deliberate floor-taking beat, which fires once every six
turns and is the most expensive social move she makes, is cashed out on bottle
caps.

The mechanism is a plain priority inversion rather than anything subtle. The
`curiosity_seed` producer has by far the highest throughput (238 rows against
`turning_over`'s 12), the loosest matching rule (`either_party`, whole-turn
scope, so it counts as used if *he* mentions the subject), the only per-turn
allowance above one (two), and it is the only source fast enough to claim a
freed wants-ledger slot — the log shows it refilling the ledger at roughly one
want every six minutes. Nothing scores a candidate on how much it is *about the
two of them*, so the fastest producer wins the entire own-material channel by
default. There is an uncomfortable interaction with shipped work here too:
**K87** deliberately put a subject quota on the curiosity generators so her
curiosity would stop being exclusively about Jacob, which was the right fix for
the problem it was aimed at, and the compound effect is that 90% of her own
material is now about neither of them.

**The design.** A substance score on candidates, applied wherever selection
happens — a rank key in the pool picker, or K92's candidate ranking if that lands
first. The wants ledger's share of this is already done: **H29** shipped
`wants_per_source_cap=4`, so seeds hold at most half the slots rather than seven
of eight. That is a blunt anti-monopoly rule, not a substance ordering; K93 is
still what decides *which* of the freed slots is worth taking to the floor. The ordering
it has to encode is roughly: something unresolved between them > something she
noticed about him > a thread from their shared history > a pursuit of her own
with accumulated state > a free-associative curiosity. Whimsy keeps a floor —
it is genuinely part of her character and it still converts better than anything
else on the shelf
— but it stops being eligible for the scarce slots (the K53 initiative beat, the
imperative band) and stops being able to monopolise the ledger.

**Cost.** Low, and it needs no new subsystem. `CuePolicy` in
[`cue_accounting.py`](../../app/core/proactive/cue_accounting.py) is already the
per-type policy table and is the obvious home for a `substance` weight;
`pick_pool_cue` in
[`cue_producer.py`](../../app/core/proactive/cue_producer.py) already has the
ordering seam (`surfaced_count ASC, created_at DESC`); the ledger side is
`add_want`'s cap check in
[`wants_ledger.py`](../../app/core/conversation/wants_ledger.py). The real work
is deciding the ordering and resisting the urge to express it as a float per
cue. Verify against the K90 own-material rate and, better, by reading twenty
consecutive `used` cues and asking whether a friend would have said them.

---

## K94. Sequencing — answer first, then add, and say where the addition goes

**Motivation.** Every mechanism in the family selects a *subject* and none of
them says anything about *placement*, yet placement is what the one honest
metric measures. K88's anaphoric-opener rate is specifically about her **first
sentence**, and it is the number that did not move at all across the whole
second pass (18% before, 18% after). Meanwhile the persona already contains
several rules pushing in this direction — lead with the substance, don't parrot,
vary the opener, move the reaction word a few words in — and they have not
shifted it either, which suggests the instruction she is missing is not another
prohibition on how to open but a positive account of the reply's *shape*.

The useful observation is that responsiveness and opener ownership are only in
tension if the reply is treated as one undifferentiated blob. "Answer his point,
but not in the first clause" and "put your own thing in the last sentence and
leave it open" are compatible with answering him completely. That decouples
being a good listener from opening on his words, which is exactly the knot the
last two families tried to cut by pushing her to change the subject instead —
the far more expensive move, and the one she sensibly refuses. It also gives
`FOLLOW_AND_ADD` (K92) an actual definition instead of a vibe, and it gives the
wants ledger's "spend one when a lull lands" somewhere concrete to land: the
end of a reply she was going to write anyway, rather than a pivot she has to
justify.

**Cost.** Very low — this is prompt-side, one or two sentences, and it is the
one item here that could be tried tonight as a persona edit before any code
exists. The risk is a formulaic reply shape (answer-then-tack-on, every turn),
so it wants a cadence rather than a standing rule, which is an argument for
attaching it to a stance (K92) rather than to the persona. Key files:
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt) for
the trial, then the `FOLLOW_AND_ADD` rendering and
[`conditional_handling.txt`](../../data/persona/conditional_handling.txt) for
the real version. Measured by K88's anaphoric rate and the opener histogram in
[`lead_follow_report.py`](../../scripts/lead_follow_report.py) — the current top
openers are `that` ×59, `i` ×51, `you` ×43, `then` ×31.

---

## K95. Interruption cost — a direct question is not an opening

**Motivation.** Insurance, and cheap. If K92–K94 work at all, the first
regression will be her leading over the top of something he actually asked, and
that single failure will cost more trust than a week of good initiative earns.
The only guard that exists today is a length proxy: K53 declines with
`user_substantial` when his message is 240 characters or more
([`initiative_director.py`](../../app/core/conversation/initiative_director.py)),
which correctly protects a long explanation and does nothing at all for a short
direct question — and short direct questions are the case where taking the floor
reads worst.

What is missing is a small read of *what his turn was doing*: did it end on a
question mark, is it the second or third turn of one explanation he is in the
middle of, is he working through a task with her, is he venting. Most of those
signals already exist and are not consulted for this purpose — K4 dialogue-act
tags, K69's vent-vs-fix-vs-reassure read, the arc, and K14 engagement. The
output should be a **hard filter on the candidate set rather than another
weight**: when he asked something directly, `INITIATE` and `REDIRECT` are simply
not available this turn and `FOLLOW_AND_ADD` is the ceiling. Encoding it as a
score invites it to be outvoted by an accumulated want, which is precisely the
failure being insured against.

**Cost.** Low, and it composes with everything else — a pure function taking his
last turn plus a couple of turns of context and returning a cost band, consumed
by K92's candidate filter (or, before K92 exists, wired straight into K53's gate
walk as a second `user_substantial`-style reason so it earns its keep
immediately). Key files: new predicate in
[`app/core/conversation/`](../../app/core/conversation/),
[`initiative_director.py`](../../app/core/conversation/initiative_director.py)'s
`decide`, the dialogue-act tags, and K69's read. Cross-refs: the mirror image of
**K82** (the dropped sub-topic — he said three things and she answered one),
which is the same "read what his turn was actually doing" capability pointed at
completeness instead of at turn-taking; the two should probably share the reader.

---

