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

Aiko maintains 1-3 sustained personal goals (e.g. "learn more about
Jacob's hometown", "write more poetry") in a new `goal` memory kind,
distinct from `open_question` (one-shot factual). A worker periodically
reflects progress and writes a `goal_progress` note linked back to the
goal. Different shape from G3: G3 is "looked it up, done"; goals are
"I keep coming back to this." Key files:
[`app/core/memory_store.py`](../../app/core/memory_store.py)
(`VALID_KINDS` extension), new `app/core/goal_worker.py`,
[`app/core/rag_retriever.py`](../../app/core/rag_retriever.py)
(boost goal-aligned hits when the topic matches).

---

## K4. Dialogue-act tagging

Tag user turns by intent (question / story / vent / banter / planning
/ debugging). Pairs with H1 (arc tag) to give RAG and
`ProactiveDirector` a finer handle. Cheap regex + small LLM hybrid; no
new schema, just a `dialogue_act` column on `messages` and an
extractor mirroring the shape of
[`app/core/promise_extractor.py`](../../app/core/promise_extractor.py).
Key files: new `app/core/dialogue_act_tagger.py`,
[`app/core/chat_database.py`](../../app/core/chat_database.py) (column
addition), [`app/core/rag_retriever.py`](../../app/core/rag_retriever.py)
(weight act-matched memories higher when the current act repeats).

---

## K5. Mood-shell tilt

Aiko's overall "mood today" tilts mildly based on `relationship_axes`
drift + recent moments. Not a personality change, just a colouring
layer in front of the persona — more playful when comfort is high,
gentler when Jacob is venting often. Key files:
[`app/core/relationship_axes.py`](../../app/core/relationship_axes.py)
(emit a derived `mood_shell` summary), new inner-life provider in
[`app/core/session_controller.py`](../../app/core/session_controller.py)
that surfaces the tilt as a one-line directive ("today, lean warmer
than baseline"). Persona block stays canonical; the shell is a hint,
not a rewrite.

---

## K7. Forgetting protocol

Formalize a "I don't remember that as well anymore" gracenote when
retrieving low-salience or archive-tier memories. The tier system
already does the work; the verbal hedge isn't first-class yet. Pairs
naturally with the `(uncertain)` and `(curiosity)` suffixes added by
F3 / G3 — add a `(faded)` suffix for archive-tier hits and a persona
rule that turns it into a soft hedge rather than a flat omission.
Key files:
[`app/core/rag_retriever.py`](../../app/core/rag_retriever.py)
`format_block`, [`data/persona/aiko_companion.txt`](../../data/persona/aiko_companion.txt)
Memory section.

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

Companion AIs tune themselves on implicit feedback: how *Jacob*
reacted to Aiko's last turn (reply latency, message length delta,
emoji frequency, conversation abandonment) is a stronger signal
than anything Aiko self-tags. We have presence + activity but no
"reward" stream from the user side. A small post-turn
`EngagementTracker` would compute a per-turn engagement delta
(e.g. `latency_z`, `length_delta`, `abandoned`) and feed it into
two consumers: a tiny drift on relationship-axes (`closeness +=
+0.02 on high engagement, -0.02 on abandonment`) and a
`ProactiveDirector` bias (skip the next nudge if the last turn
got an abandoned signal — no point chasing a closed window). Key
files: new `app/core/engagement_tracker.py`, post-turn hook in
[`app/core/session_controller.py`](../../app/core/session_controller.py)
`_post_turn_inner_life`,
[`app/core/relationship_axes.py`](../../app/core/relationship_axes.py)
(small bounded delta channel),
[`app/core/proactive_director.py`](../../app/core/proactive_director.py)
(eligibility AND-fold).

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

## K16. Unified ambient grounding line

Today the prompt carries world, activity, affect, circadian,
routines, anniversary, and presence as separate inner-life blocks
(~4-8 lines on a typical turn). Companion-AI grounding research
suggests one fused "where we are right now" sentence ("Sunday
morning, you're working in Cursor, mood reads upbeat — usual
hangout slot") reads more human and saves tokens. Implementation
is purely additive: a new top-level provider that consumes the
existing block builders and renders them into one paragraph,
with the granular blocks dropped under `aggressive=True`. The
risk is over-fusion (an LLM-generated summary would drift); keep
it template-driven so it stays deterministic. Key files: new
`app/core/grounding_line.py`,
[`app/core/prompt_assembler.py`](../../app/core/prompt_assembler.py)
(provider order: grounding line first, granular blocks fallback),
persona note explaining the format.

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
