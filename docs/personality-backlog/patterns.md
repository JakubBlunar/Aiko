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

When `affect_state` shows Jacob's mood drop right after Aiko's last
turn, flag that turn as a candidate "rupture" so Aiko can softly
repair on the next turn rather than ploughing on. Cheap to bolt onto
the existing affect log: a small worker compares pre / post deltas
and writes a `rupture_candidate` row that the next turn's prompt can
surface. Key files:
[`app/core/affect_state.py`](../../app/core/affect_state.py),
new `app/core/rupture_detector.py` (or fold into the existing affect
state), persona rule for the gentle-repair beat.

---

## K9. Topic-graph / interest-network browser

Build a lightweight cosine-cluster graph of memories so Aiko can
notice "this topic touches three threads we've been on" and surface
connections naturally. Reuses the embeddings already in
[`app/core/rag_store.py`](../../app/core/rag_store.py); the graph
itself is computed lazily and cached. UI side: a minimal "topic
graph" sub-tab in the Memory tab that visualises clusters so the user
can see what Aiko sees. Key files: new
`app/core/topic_graph.py`, [`app/core/rag_retriever.py`](../../app/core/rag_retriever.py)
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
[`app/core/memory_store.py`](../../app/core/memory_store.py) (new
`pre_thought` kind, scratchpad tier).

---

## K12. Calendar-linked anticipation

Combine H2 (time context) + D1 (reminders) + the temporal-memory
`future_plan` kind: if Aiko knows "Jacob has an interview Friday",
weight it higher as Friday approaches (e.g. score `+= 0.05` when
`event_time - now < 48h`). Mostly a retrieval-side change once the
temporal scaffolding is in. Key files:
[`app/core/rag_retriever.py`](../../app/core/rag_retriever.py),
[`app/core/follow_up_worker.py`](../../app/core/follow_up_worker.py)
(already nudges on overdue plans — extend to "approaching" plans).

---

## K13. Stylometric mirror

Light persistent analysis of Jacob's typing style (sentence length,
slang, emoji frequency, formality) into a `style_signal` field on
`UserProfile`. Aiko's existing in-context mirroring becomes anchored
across days rather than reset every session, so she stays calibrated
even when the recent history window doesn't cover yesterday. Key
files: new `app/core/style_signal.py` (rolling stats over the last N
user messages), [`app/core/user_profile.py`](../../app/core/user_profile.py)
(new `style_signal` field — bucketed: terse / chatty / formal /
casual / etc.), persona block consumer.

---

## K14. Implicit engagement signals

Shipped — see [`shipped.md`](shipped.md) "K14. Implicit engagement
signals (latency + length)".

---

## K15. Self-disclosure / vulnerability budget

Aiko has self-image, pinned self-memories, and reflection
workers, but no cadence cap on personal disclosure depth. Over a
long session she can over-share (or under-share when the moment
calls for it) relative to where the relationship axes actually
sit. A per-session "vulnerability budget" — a small token-bucket
gated on `closeness` + `trust` — would cap how often she emits a
self-tag of kind `self` (`[[remember:self:...]]`) and how deep
the disclosure can go (a 1-3 scale: surface preference / mild
admission / genuine softness). Out-of-budget moments still
happen, just register as a rare event rather than the default
register. Key files:
[`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
(soft-cap guidance),
new inner-life provider exposing the live budget,
[`app/core/memory_store.py`](../../app/core/memory_store.py)
(`self` kind metadata for depth tier).

---

## K17. Clarification-repair protocol

Distinct from K8 (rupture-and-repair, which fires on affect drop
after Aiko's turn). K17 covers *semantic* repair — "no that's
not what I meant", "you misunderstood", a very short confused
reply, an explicit `huh?` — and triggers a one-turn "let me re-
read that" beat without waiting for affect drift. Pairs naturally
with K4 dialogue-act tagging: a `clarification` act on the user's
last message is the clean signal. Key files: new
`app/core/clarification_detector.py` (regex + optional dialogue-
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

F3 tracks how confident *Aiko* is in each memory. Nothing tracks
how confident *Jacob* is in Aiko's answers. When he follow-up-
fact-checks her, says "are you sure?", or rephrases a claim back
softer, that's a signal her authority is shaky on the topic. A
calibration state per user (or per memory cluster) lets her
hedge verbally when Jacob is treating her as tentative — softer
than F3's per-fact confidence, broader than K2's belief gaps.
Key files: new `calibration_state` table or
[`UserProfile`](../../app/core/user_profile.py) field,
post-turn heuristic worker that inspects the last user message
for "are you sure" / "actually" / "wait" patterns,
inner-life provider that surfaces the live calibration as a
one-line nudge ("Jacob's been double-checking you on tech topics
lately — hedge").

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

Sits between K17 (semantic repair — fires on *explicit* "no
that's not what I meant" signals) and K14 (implicit engagement —
aggregates latency / length / abandonment across turns). The
gap: when Jacob fires a one-word reply right after a long Aiko
reply, or pivots topics without acknowledging her last point,
or his next turn shrinks 60% in length without anger, that's a
soft misattunement signal she has no machinery for today. K17
won't catch it (no regex hit); K14 won't catch it for a few
turns (it's per-aggregate). A per-turn heuristic running on
`(prev_aiko_reply_len, this_user_reply_len, topic_continuity)`
could emit a `mild_disengagement` cue that nudges the next turn
toward "shorter, more attentive, ask one thing." Key files: new
`app/core/misattunement_detector.py`, post-turn hook in
[`app/core/session_controller.py`](../../app/core/session_controller.py)
`_post_turn_inner_life`, persona block consumer ("Heads-up: he
went quiet on you — pull back, don't push more").

---

## K24. Sensory anchoring layer

Aiko has a fully-stocked room (cookies, tea pot, blanket, retro
keyboard, photo of Jacob) but she almost never references it as
sensory experience inside a reply. The `world` inner-life
provider grounds *where* she is, not *what she's doing with her
hands*. A small cadence layer would occasionally substitute a
sensory detail for an emotional statement — "I'm pulling the
blanket tighter while you talk about it" instead of "I hear you"
— picking from a rate-limited rotation of items currently in the
room and a verb table (`holding`, `setting down`, `wrapping`,
`picking up`). Rate-limited so it never becomes a tic. Pairs
naturally with the K16 grounding-line consolidation: when in
`replace` mode the sensory detail can ride the same paragraph;
when in `off`/`split` it surfaces as a one-line "small physical
beat" cue. Key files:
[`app/core/world_store.py`](../../app/core/world_store.py)
(reader for "items currently visible"), new
`app/core/sensory_anchor.py` (rate-limit + verb table + cue
emit), persona block consumer.

---

## K25. Memory confidence time-decay

F3 stamps a confidence float on each memory at write time, and
the conflict detector / belief gap detector both read it, but
nothing decays confidence over time. A claim Jacob made 90 days
ago is just as confidently rendered as one from yesterday — and
that's exactly when Aiko should hedge ("I think you mentioned
something about Thai food a while back? Don't quote me on that").
Cheap to bolt on: a derived
`effective_confidence = stored_confidence * max(0.3, 1 - days_since_observed / 365)`
read at retrieval time (no schema change, no migration). The RAG
retriever picks `(uncertain)` / `(faded)` / no-suffix tiers off
the derived value rather than the stored one, and a small
persona rule turns the suffix into a verbal hedge. Pairs
directly with K7 (forgetting protocol) — K7 hedges by *tier*
(archive → faded), K25 hedges by *age* even within long_term.
Key files:
[`app/core/rag_retriever.py`](../../app/core/rag_retriever.py)
`format_block` + the existing `(uncertain)` suffix path,
[`app/core/memory_store.py`](../../app/core/memory_store.py)
(read-side helper, no write).

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
[`app/core/catchphrase_miner.py`](../../app/core/catchphrase_miner.py)
(extend to track *who* introduced each shared phrase first),
new `app/core/voice_adoption.py` (slow promotion rule), persona
block consumer.
