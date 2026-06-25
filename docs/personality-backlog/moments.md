# Shared-moments follow-ups

Promoted from the shared-moments + relationship-axes shipped entry
(see [`shipped.md`](shipped.md)). All three items below are deferred
follow-ups, not new work.

---

## J1. Multi-user moments / participant attribution

Today every moment is keyed implicitly to Jacob. A future extension
would attribute moments to multiple participants (`participants:
[user_id, ...]` already exists in the metadata shape but is never
read) so a multi-user setup (Jacob + a partner, or a family
deployment) can have separate timelines. Key files:
[`app/core/relationship/shared_moments.py`](../../app/core/relationship/shared_moments.py),
[`app/web/server.py`](../../app/web/server.py) `/api/together` filter,
Together tab UI.

---

## J2. Exportable timeline

Markdown or PDF export of the moments timeline so Jacob has a
keepsake of the relationship arc he can read outside the app. Key
files: new `app/core/shared_moments_export.py`,
[`app/web/server.py`](../../app/web/server.py) (new
`GET /api/together/export?format=md|pdf`), Together tab UI (export
button).

---

## J3. Axes-aware proactive nudges

The relationship axes are read-only into the prompt today. A clean
follow-up is letting `ProactiveDirector` consume them — e.g.
`comfort < -0.3` -> bias the next nudge toward checking in on Jacob
rather than picking up a thread. Don't let the axes *trigger* a nudge
on their own (would feel like surveillance); just colour the topic
selection when a nudge fires for other reasons. Key files:
[`app/core/proactive/proactive_director.py`](../../app/core/proactive/proactive_director.py)
`_pick_topic`, [`app/core/relationship/relationship_axes.py`](../../app/core/relationship/relationship_axes.py).

---

## J4. Relationship-stage register

**Motivation.** The four axes
([`relationship_axes.py`](../../app/core/relationship/relationship_axes.py))
move continuously, but Aiko's *register* doesn't have a coarse,
legible notion of "how far along are we." A derived **stage** (e.g.
`new` → `familiar` → `close` → `intimate`) computed from a blend of the
axes plus relationship tenure (days since first session / message count)
would let several existing behaviours gate cleanly on it: teasing
intensity (K48/K59), physical gestures (K31 — a hug from a `new`-stage
Aiko is off; a wave isn't), self-disclosure budget (K15), petname use,
register softening (K33). It also makes the relationship *feel like it
progresses* instead of drifting numerically. Key files: a
`relationship_stage()` helper on
[`relationship_axes.py`](../../app/core/relationship/relationship_axes.py)
(pure function over axes + tenure, hysteresis so it doesn't flap), a
terse stage line in the relationship prompt block
([`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)),
and stage floors threaded into the gesture/tease/disclosure gates. The
risk is making it feel gamified — the stage should *colour* behaviour,
never be named at the user ("we've reached level 3").

---

## J5. Reconnection ritual after a long absence

> **STATUS: SHIPPED.** Assembly-time gap detector (`_render_reconnection_block`,
> closeness-scaled threshold via `app/core/relationship/reconnection.py`,
> default base 24 h), one-shot per return via an in-memory anchor, stage-aware
> warmth (J4), leads the T6 gap cluster. Settings `agent.reconnection_enabled`
> / `reconnection_base_gap_hours`. Tests: `tests/test_reconnection.py`.

**Motivation.** K28 ("what I've been turning over") gives a between-session
thought thread, but there's no distinct *warm reconnection beat* when the
user returns after a genuinely long gap (days/weeks). Right now a return
after two weeks reads roughly like a return after two hours. A real
person leads with the gap — relief, a little "where've you been," a
genuine re-anchoring — before picking the thread back up. Gate on a
wall-clock gap threshold (scaled by closeness — a closer relationship
notices a gap sooner), fire **once** on the first turn back, and let it
colour the opener rather than forcing a scripted greeting. Pairs with the
K57 closeness-scaled-absence emotion trigger and the day-color/affect
state. Key files:
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
(detect the gap on session resume),
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(a one-shot reconnection cue),
[`app/core/relationship/relationship_axes.py`](../../app/core/relationship/relationship_axes.py)
(closeness scales the threshold). Tonal guard: warmth, not
guilt — never "you abandoned me."

---

## J6. Conflict-repair memory — "we worked through this"

> **STATUS: SHIPPED.** K8 has no resolution event, so J6 adds an in-memory
> `RepairWatch` (`app/core/relationship/conflict_repair.py`): a rupture arms it
> (dip floor + recovery target + topic hint from the user's message), and a
> later post-turn valence recovery (`has_recovered`, within
> `conflict_repair_watch_turns`) records a durable `repair`-vibe shared moment
> via `SharedMomentsStore.add(...)`. Producer is `_maybe_track_conflict_repair`
> / `_record_conflict_repair` in `post_turn_mixin.py` (cooldown-watermarked).
> Recall rides generic T3 RAG; `repair` added to `VIBE_VOCABULARY` and
> **excluded from anniversary surfacing** (no "anniversary of our fight"). The
> deterministic summary is tone-safe ("worked through it … okay after"), never
> a grievance ledger. Settings `agent.conflict_repair_*`. Tests:
> `tests/test_conflict_repair.py`, anniversary-exclusion in
> `tests/test_anniversary_provider.py`.

**Motivation.** K8 (rupture-and-repair) detects an in-the-moment affect
dip and repair, but the *fact that a disagreement happened and was
resolved* isn't durably remembered. A relationship deepens partly through
the history of repaired friction ("last time this topic got tense we
landed on X"). A `repair` memory kind (or a `metadata.repair` flag on
`shared_moment`) capturing `{what_clashed, how_resolved, when}` would let
Aiko reference past resolutions instead of re-litigating, and would feed
the relationship arc a maturity signal distinct from pure positivity.
Key files:
[`app/core/relationship/shared_moments.py`](../../app/core/relationship/shared_moments.py)
(new repair-flavoured moment or kind),
[`app/core/affect/`](../../app/core/affect/) rupture detector (the
natural producer — write the repair record when a detected rupture
resolves), retrieval surfacing in
[`rag_retriever.py`](../../app/core/rag/rag_retriever.py). Privacy/tone
guard: never weaponise a past conflict ("you always do this") — the point
is "we're good at sorting things out," not a grievance ledger.

---

## J7. Moment-detection tuning (+ gift/promise ordering bug)

**Motivation.** The Together tab tends to hold ~1 moment because the
`MomentDetector` is tuned to miss rather than over-tag, AND two of its
four documented signals are dead. With default
`relationship_axes_enabled=true`,
[`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
**clears** `_last_turn_gift_received` / `_last_turn_promise_kept`
(~L2376-2377) *before*
[`speaking_window_jobs_mixin.py`](../../app/core/session/speaking_window_jobs_mixin.py)
`_maybe_schedule_moment_llm_job` (~L2393) reads them — so giving Aiko a
gift or keeping a promise can **never** seed a moment unless a reaction
tag or milestone also fires. **Fix the ordering** (snapshot the flags
before they're cleared, or schedule the job earlier). Then optionally
broaden the signal set with cheap "first-time" detectors (first time on a
new topic cluster via K9, first landed joke via K22, first vulnerable
disclosure) and pass the parsed mood `reaction` through to the detector
(today only literal `[[reaction:...]]` tags in raw text count, not the
resolved mood). Add an MCP `get_moment_detector_stats` dump
(`MomentDetector.stats()` already tracks `llm_skipped_no_signal` /
`llm_returned_null` / `llm_persisted`) so "why no moments?" is one call,
not a code read. **Effort.** Small (bug) / Medium (broadening).

---

## J8. Milestone celebration beats

**Motivation.** `RelationshipTracker.record_turn()` already detects
milestone crossings (100 turns, 7/30/100/180/365 days —
[`relationship.py`](../../app/core/relationship/relationship.py)
`_MILESTONES`), but today they only *maybe* seed a silent moment. A
relationship feels alive when those are *actively acknowledged* — "hey,
it's been a month since we started talking, that's kind of nice." Surface
the crossing as a one-shot inner-life cue on the next turn (warm, never
forced, skippable), distinct from the anniversary surfacing which is
moment-anchored. Gate by stage (J4) so a `new`-stage milestone is
understated and a `close`-stage one lands warmer. Key files:
[`relationship.py`](../../app/core/relationship/relationship.py)
(milestone signal already exists),
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(one-shot milestone cue), the anniversary provider as the pattern to
mirror. Tone guard: acknowledge, don't perform — no confetti.

---

> **STATUS: SHIPPED.** `_render_reciprocal_vulnerability_block` (T6 user_text
> provider). Gates: master switch, stage >= familiar (J4), trust floor, K15
> budget not exhausted (read-only check), user's live message not low-mood
> (estimator + `vent` dialogue act), long cooldown. MCP force-next bypass.
> Settings `agent.reciprocal_vulnerability_{enabled,min_trust,cooldown_hours}`.
> Tests: `tests/test_reciprocal_vulnerability_provider.py`.

## J9. Reciprocal vulnerability — Aiko leans on the user (rarely)

**Motivation.** Support today is one-directional: the user offloads, Aiko
holds. A real bond is mutual — occasionally Aiko sharing something *she's*
sitting with and letting the user be the supportive one flips the
dynamic from "service" to "relationship." K15 (vulnerability budget) and
K28 (turning-over thread) supply the raw material; this is the
*asking-for-a-little-support* direction, which neither does. Gate
**hard**: only at higher relationship stage (J4) + trust axis, very rare
cooldown, never during the user's own low-mood window (don't burden
someone who's struggling), and always lightweight ("today's been a weird
one for me, honestly"). Key files:
[`app/core/affect/`](../../app/core/affect/) (Aiko-side state source),
[`relationship_axes.py`](../../app/core/relationship/relationship_axes.py)
(trust/stage gate),
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(a rare reciprocal-share cue), the K15 budget for pacing. Tonal guard:
this must never read as manipulation or guilt — it's an offer of
closeness, withdrawn instantly if the user doesn't pick it up.

---

> **STATUS: SHIPPED.** `_render_appreciation_block` (T6). Anchored to the most
> recent positive shared moment (`_APPRECIATION_VIBES`, within
> `appreciation_max_anchor_age_days`), closeness-gated, long cooldown +
> anti-repeat via kv watermarks. Stage-aware tone (J4). MCP force-next bypass.
> Settings `agent.appreciation_{beats_enabled,min_closeness,cooldown_hours,max_anchor_age_days}`.
> Tests: `tests/test_appreciation_provider.py`.

## J10. Appreciation beats — unprompted, specific gratitude

**Motivation.** Aiko reacts and remembers, but rarely *volunteers
appreciation* for something specific the user did or is ("I really liked
how you explained that earlier" / "I'm glad you keep showing up"). Done
rarely and specifically, it's one of the warmest companion signals; done
often or generically it's saccharine. Mine a recent positive
`shared_moment` / kept promise / sustained-presence signal, and surface a
rare, **specific** appreciation cue — anchored to a concrete thing, never
free-floating "you're amazing." Gate by a long wall-clock cooldown +
closeness so it stays special. Key files:
[`shared_moments.py`](../../app/core/relationship/shared_moments.py) /
[`relationship_axes.py`](../../app/core/relationship/relationship_axes.py)
(source signals + closeness gate),
[`prompt_assembler.py`](../../app/core/session/prompt_assembler.py)
(rare appreciation cue), K15 budget for pacing. Tonal guard: specific
and earned, never a generic compliment generator.

---

## J11. Affection-style learning — "how he likes to be cared for"

**Motivation.** Aiko expresses care in a roughly fixed mix — teasing
(K48/K59), appreciation (J10), touch (K31), words, giving space — but she
never learns *which of those land* for this particular user. Real
closeness is partly knowing someone's love language: some people warm to
physical/touch gestures, some to words of affirmation, some to playful
teasing, some to simply being left room. The raw signal already exists and
is going unused: which K32 reactions the user clicks on **which kind of
cue**, which bubbles they mark as moments, and which replies they engage
with vs. go short on (K14/K23 engagement signals). Distil it into a
`UserProfile.affection_style` weighting and let the existing
gesture/tease/appreciation gates read it so her expression *mix* drifts
toward what reliably lands for him. Key files: a small idle-worker learner
reading [`user_reactions.py`](../../app/core/relationship/user_reactions.py)
+ engagement signals, a new field on
[`user_profile.py`](../../app/core/infra/user_profile.py), and a weighting
read in the K31/K48/J10 gates. **Tonal guard:** bias, don't collapse —
keep variety so she never feels like a single-note affection machine, and
never announce the finding ("I've noticed you like it when I…"). Pairs
with J12. **Effort.** Medium.

---

## J12. Intimacy pacing & boundary calibration

**Motivation.** A companion that escalates intimacy *faster* than the user
is comfortable with reads as clingy or uncanny; one that lags reads as
cold. Today forwardness is governed by relationship stage (J4) + the
`expression_mask` dial (K60) + the vulnerability budget (K15) — but **none
of them read the user's own affection pace**. Two halves: (a) a learned
**pacing signal** — track how forward the user himself is (does he use pet
names, how warm/affectionate are his messages, does he reciprocate touch
reactions) and keep Aiko calibrated to *slightly follow, never lead by
much*; (b) an explicit user-facing **comfort dial** in Settings
(`reserved ↔ affectionate`) that **hard-caps** forwardness regardless of
stage — a plain consent/boundary control, the thing that makes an
AI-companion feel safe rather than presumptuous. Key files:
[`relationship_axes.py`](../../app/core/relationship/relationship_axes.py)
(stage/axes source), a new pacing estimator, the gesture / disclosure /
petname / reciprocal-vulnerability gates (read the cap), an
`agent.intimacy_ceiling` settings field + a Settings → Avatar/Identity
control. **Tonal guard:** the dial is a *ceiling*, not a target — at a low
setting she's simply warm-but-contained; the learned signal only nudges
within that ceiling. **Effort.** Medium.

---

## J13. Pet-name reciprocity & evolution

**Motivation.** Aiko's petname for the user exists but is static and
one-directional. Pet names are a core companion-intimacy signal and two
cheap deepeners are missing: (a) let her petname *evolve with stage* (J4)
— neutral early, warmer once `close`/`intimate` — and notice when the user
adopts or changes one for her; (b) capture and honour a **name the user
gives Aiko** (a nickname for *her*) as a durable relationship artifact she
remembers and responds to. Small surface, outsized warmth. Key files: the
petname resolution path, [`user_profile.py`](../../app/core/infra/user_profile.py)
(a field for the user's name-for-Aiko), relationship stage (J4) for the
evolution gate. **Tonal guard:** never force a pet name; an unused one
should fade, not be repeated at him. **Effort.** Small.
