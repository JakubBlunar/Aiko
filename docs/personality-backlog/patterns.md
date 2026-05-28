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

## K3. Routine / ritual awareness

Beyond G2's hour buckets, name recurring patterns ("Sunday-morning
chats", "post-work check-ins"). Less surveillance-feeling than "you
log on at 9pm" because it's framed as ritual. Builds on the shipped
`usual_hours` field. Key files:
[`app/core/schedule_learner.py`](../../app/core/schedule_learner.py)
(extend the bucketing pass to detect named clusters),
[`app/core/user_profile.py`](../../app/core/user_profile.py)
(new `routines` field — list of `{name, when_pattern, last_seen_at}`),
persona note teaching Aiko to invoke the ritual frame when one
matches.

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

## K6. Surprise / novelty detector

Flag user inputs that diverge significantly from the recent
topic-cluster baseline (cosine distance from a rolling centroid of the
last N user turns). Lets Aiko react with actual surprise ("oh — that's
a new one") rather than blank acceptance. Key files: new
`app/core/novelty_detector.py`,
[`app/core/rag_store.py`](../../app/core/rag_store.py) (reuses the
existing user-message embeddings — no new index needed),
[`app/core/session_controller.py`](../../app/core/session_controller.py)
(novelty score becomes an inner-life signal the LLM can react to).

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
