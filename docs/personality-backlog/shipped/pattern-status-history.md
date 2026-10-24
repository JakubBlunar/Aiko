# Historical companion-pattern catalogue

Snapshot before the 24 Oct cleanup. Shipped rows are an implementation catalogue, not fresh model validation; use [the current queue](../patterns.md) and [T5](../testing.md) for remaining work.

## Status at a glance

**30 Sep 2026: contextual judgment shadow implemented.** The existing situation
worker samples answer coverage, progress, response mode, attunement, explanation
depth and completion without applying its judgments. Collection and inspection
details: [conversation judgment shadow](cognitive-continuity.md#conversation-judgment-shadow-30-sep-2026).
Behavioral activation and model-quality evaluation remain open.

| ID | Item | Status |
|----|------|--------|
| K1 | Long-term goals tracker | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md) |
| K2 | Theory-of-mind / belief tracking | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md) |
| K3 | Routine / ritual awareness | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md) |
| K4 | Dialogue-act tagging | ✅ shipped — [features.md](features.md#h1--k4-conversation-arc-self-tag--dialogue-act-tagging-schema-v13) |
| K5 | Mood-shell tilt | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md) |
| K6 | Surprise / novelty detector | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md) |
| K7 | Forgetting protocol | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md) |
| K8 | Affect rupture-and-repair detector | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md) |
| K9 | Topic-graph browser + clustering | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md#k9-topic-graph-browser--observability-surface) + [awareness.md → F10](awareness.md#f10-topic-graph-utilisation-rag--prompt--knowledge-integration) (F10c bounded topic expansion also shipped; its earlier deferred note is superseded) |
| K10 | Persona regression tests | ✅ shipped (incl. the background auto-eval worker) — [patterns-k01-k15.md](patterns-k01-k15.md#k10-persona-regression-tests--shipped) |
| K11 | Counterfactual / pre-thought cache | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md#k11-counterfactual--pre-thought-cache--shipped) |
| K12 | Calendar-linked anticipation | Opt-in local calendar and approaching-event pilot shipped; UI/sync/evaluation open |
| K13 | Stylometric mirror | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md) |
| K14 | Implicit engagement signals | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md) |
| K15 | Self-disclosure / vulnerability budget | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md#k15-self-disclosure--vulnerability-budget) |
| K16 | Unified ambient grounding line | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md) |
| K17 | Clarification-repair protocol | ✅ shipped — [patterns-k01-k15.md](patterns-k01-k15.md#k17-clarification-repair--you-missed-his-last-point) |
| K18 | Topic stagnation detector | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md) |
| K19 | Cold-start companion onboarding | ❌ open |
| K20 | Metacognitive calibration | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md) |
| K21 | Fresh-eyes thread re-summarisation | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md#k21-fresh-eyes-thread-re-summarisation) |
| K22 | Callback / inside-joke detector | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md) |
| K23 | Subtle misattunement detection | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md#k23-subtle-misattunement-detection) |
| K24 | Sensory anchoring layer | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md#k24-sensory-anchoring-layer--adaptive-per-arc-cadence--posture-kind-matrix) |
| K25 | Memory confidence time-decay | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md#k25-memory-confidence-time-decay) |
| K26 | Aiko-side voice evolution | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md#k26-aiko-side-voice-evolution--she-starts-to-talk-like-him-a-little) |
| K27 | Aiko's day — daily personality colour | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md#k27-aikos-day--daily-personality-colour) |
| K28 | "What I've been turning over" | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md#k28-what-ive-been-turning-over--between-session-thought-thread) |
| K29 | Opinion injection — push back on a stance | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md#k29-opinion-injection--push-back-when-she-has-a-stance) |
| K30 | Self-noticing cues | ✅ shipped — [patterns-k16-k30.md](patterns-k16-k30.md#k30-self-noticing-cues--agreement-streak--flat-affect--repeated-thought) |
| K31 | Soft physicality — virtual gestures | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k31--k32-soft-physicality-round-trip--virtual-touch--user-side-reactions) |
| K32 | Reciprocity — user-side quick reactions | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k31--k32-soft-physicality-round-trip--virtual-touch--user-side-reactions) |
| K33 | Cozy mode — persistent register softening | ❌ open |
| K34 | Forward curiosity worker | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k34-forward-curiosity-worker--ive-been-wondering) |
| K35 | Memory consolidation worker | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k35-memory-consolidation-worker--nightly-near-duplicate-merge) |
| K36 | "Things I did while you were away" | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k36-things-i-did-while-you-were-away--idle-time-world-activities) |
| K37 | Emotional contagion | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k37-emotional-contagion--jacobs-affect-tilts-aikos-affect) |
| K38 | Self-correction "actually…" (next-turn) | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k38-self-correction-cue--next-turn-contradiction-catch) |
| K39 | Energy / spoons model | ➖ closed — same mechanic as, and absorbed by, [K68](patterns-k31-k60.md#k68-embodied-vitality--a-body-that-livens-up-when-the-conversation-is-interesting) |
| K40 | Comfortable silence | Explicit-completion slice shipped; inferred silence open |
| K41 | Same-reply mid-stream self-correction | ❌ open |
| K42 | Multi-bubble reply bursts | ❌ open |
| K43 | Promise follow-through | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md) |
| K44 | Felt-language affect block | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md) |
| K45 | Mood inertia | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md) |
| K46 | Stance persistence | ✅ shipped — [shipped/patterns-k31-k60.md](patterns-k31-k60.md#k46-stance-persistence--dont-cave-on-taste-pushback) |
| K47 | Question/share balance | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k47-questionshare-balance--stop-interviewing) |
| K48 | Tease rhythm — banter budget | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k48-tease-rhythm--banter-as-a-budget-not-random-snark) |
| K49 | Messiness permission — typed imperfection | ❌ open |
| K50 | Typed-mode delivery pacing | ❌ open |
| K51 | Cue-register rotation | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k51-cue-register-rotation--de-heads-up-the-inner-life) |
| K52 | Wants ledger (will family) | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k56-persona-counterweight--the-leading-vs-following-rewrite) |
| K53 | Initiative turns (will family) | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k56-persona-counterweight--the-leading-vs-following-rewrite) |
| K54 | Aiko-side topic appetite | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k54-aiko-side-topic-appetite--shes-allowed-to-be-bored) |
| K55 | Thread ownership | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k55-thread-ownership--she-defends-what-she-opened) |
| K56 | Persona counterweight (leading vs following) | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k56-persona-counterweight--the-leading-vs-following-rewrite) |
| K57 | Directed emotion episodes | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k57-directed-emotion-episodes--feelings-at-the-user-with-a-cause) |
| K58 | Emotion speech weighting | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k58-emotion-speech-weighting--moods-that-actually-land-in-the-voice) |
| K59 | Tease economy | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k59-tease-economy--youll-pay-for-that-one) |
| K60 | Tsundere mask | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k60-tsundere-mask--warmth-expressed-through-denial) |
| K61 | Specifics over generalities (knowledge grounding) | ✅ shipped — [awareness.md](awareness.md#k61-knowledge_grounding-inner-life-block-commit-to-specifics) |
| K62 | Co-experience companion (follow a show/album) | Durable per-user episode/chapter progress shipped; broader media and evaluation open |
| K63 | Long-arc callbacks — "weeks ago you said…" | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k63-long-arc-callbacks--weeks-ago-you-said) |
| K64 | Freedom of thought (a–d: wandering / drift / curiosity gradient / map self-reflection) | ✅ shipped — [awareness.md](awareness.md#k64a-associative-wandering-funny-this-reminds-me-of) |
| K65 | Worker modernization for the topic-cluster era | ✅ shipped (a–e) — [patterns-k31-k60.md](patterns-k31-k60.md#k65-worker-modernization-for-the-topic-cluster-era-audit) |
| K66 | Earned familiarity — "well-trodden ground" | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k66-earned-familiarity--well-trodden-ground-between-us) |
| K67 | Dormant-interest re-opener | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k67-dormant-interest-re-opener--we-havent-talked-about-x-in-ages) |
| K68 | Embodied vitality | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k68-embodied-vitality--a-body-that-livens-up-when-the-conversation-is-interesting) |
| K69 | Implicit-need reading — vent vs fix vs reassure | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k69-implicit-need-reading--vent-vs-fix-vs-reassure) |
| K70 | Longitudinal growth witness — "you've changed since we met" | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k70-longitudinal-growth-witness--youve-changed-since-we-met) |
| K71 | Self-callback — her own continuity over time | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k71-self-callback--her-own-continuity-over-time) |
| K72 | Wellbeing concern — gentle care, never a nag | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k72-wellbeing-concern--gentle-care-never-a-nag) |
| K73 | Shared ritual formation — "this is becoming our thing" | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k73-shared-ritual-formation--this-is-becoming-our-thing) |
| K74 | Humor-style calibration — what kind of funny lands | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k74-humor-style-calibration--what-kind-of-funny-lands) |
| K75 | User-expertise calibration — match explanation depth | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k75-user-expertise-calibration--match-explanation-depth) |
| K76 | Affective memory salience — flashbulb encoding | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k76-affective-memory-salience--flashbulb-encoding) |
| K77 | Candor gate — "can I be real with you?" | Partial: opt-in taste-only pilot |
| K78 | Vocal-affect read — hear *how* he said it (prosody-in) | ❌ open |
| K79 | Hesitation tell — typing latency as a signal | ❌ open |
| K80 | Inside-joke birth — bless the moment a bit becomes "ours" | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k80-inside-joke-birth--bless-the-moment-a-bit-becomes-ours) |
| K81 | Taste formation — topics she *likes*, not just topics she's seen | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k81-taste-formation--topics-she-likes-not-just-topics-shes-seen) |
| K82 | The dropped sub-topic | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k82-the-dropped-sub-topic--he-said-three-things-she-answered-one) |
| K83 | The right to decline | Partial: opt-in disclosure-only pilot |
| K84 | Calibrated jealousy | Partial: opt-in, explicitly invited pilot |
| K85 | The third subject — interests that aren't him | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k85-the-third-subject--interests-that-arent-him) |
| K86 | Immortal future plans — asking about things that already happened | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k86-immortal-future-plans--asking-about-things-that-already-happened) |
| K87 | Curiosity that isn't about him | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k87-curiosity-that-isnt-about-him) |
| K88 | Anaphoric-opener detector — the measurable tell for following | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k88-anaphoric-opener-detector--the-measurable-tell-for-following) |
| K89 | Sustained thread — leading past one turn | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k89-sustained-thread--leading-past-one-turn) |
| K90 | Lead/follow metrics — make the whole family measurable | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k90-leadfollow-metrics--make-the-whole-family-measurable) |
| K91 | Lived-in away life — a day she had, not a day she narrated | ✅ shipped — [patterns-k31-k60.md](patterns-k31-k60.md#k91-lived-in-away-life--a-day-she-had-not-a-day-she-narrated) |
| K92 | Conversational stance — one decision per turn, not ten permission slips | 🟡 phases 1–2 shipped 15 + 19 Aug — [patterns-k92-k95.md](patterns-k92-k95.md#k92-conversational-stance--phases-1-and-2); **phase 3 open** below |
| K93 | The substance floor — what she takes to the floor, not whether she takes it | 🟡 ledger half shipped 19 Aug — [patterns-k92-k95.md](patterns-k92-k95.md#k93-the-substance-floor--the-ledger-half); **cue-pool half open** below |
| K94 | Sequencing — answer first, then add, and say where the addition goes | ✅ shipped 19 Aug — [patterns-k92-k95.md](patterns-k92-k95.md#k94-sequencing--answer-first-then-add-and-say-where-the-addition-goes) (a third stance axis, fired on evidence at 6.4% of turns) |
| K95 | Interruption cost — a direct question is not an opening | ✅ shipped 19 Aug — [patterns-k92-k95.md](patterns-k92-k95.md#k95-interruption-cost--a-direct-question-is-not-an-opening) (reader landed inside K92 phase 1, enforcement in K53's gate walk) |
| K96 | In-turn deliberation / second thought | Partial; post-reply worker shipped, 27 Sep runtime failures need diagnosis below |
| K97 | A small causal working set for the current thread | Implementation shipped; behavioral comparison open |
| K98 | Continue an interest after a good answer | Implementation shipped; behavioral comparison open |
| K99 | Common ground with delivery provenance | Implementation shipped; behavioral comparison open |
| K100 | Evidence-bound memory admission | Two-writer pilot implemented; broader entailment and behavioral evaluation open |
| K101 | Route an unresolved premise to a bounded next step | Strict read-only recall pilot implemented; broader routing/evaluation open |
| K102 | Pragmatic frames and indirect meaning | Bounded implementation and baseline, 24 Oct; broader behavioral validation open below |
| K103 | Repair the specific misunderstanding | Same-turn repair and cited target, 24 Oct; broader behavioral validation open below |
| K104 | Cause-specific feelings and mixed emotions | Cause-aware deterministic slice, 24 Oct 2026 |
| K105 | Expression follows communicative intent | Partial: pause-only cue and mood mist, 24 Oct 2026 |
| K106 | Shared shorthand with negotiated meaning | Open proposal, 23 Oct 2026 |
| K107 | Provider outcomes are not character evidence | Explicit outcome provenance implemented; semantic attribution open, 23 Oct 2026 |

---
