# Shipped -- Shared moments & relationship (J-series)

Part of the [shipped log index](../shipped.md). Follow-ups promoted out of the
shared-moments + relationship-axes batch that have since landed: the derived
relationship stage and the beats built on top of it. Open items still live in
[`moments.md`](../moments.md).

---

## J4. Relationship-stage register

> ✅ **Shipped** — the doc entry was simply never updated.
> [`relationship_stage()`](../../../app/core/relationship/relationship_axes.py)
> is the pure helper the spec below asks for (axes + tenure, with hysteresis
> via a `current_stage` argument so it can't flap), cached per session on
> `_last_relationship_stage` and read through `relationship_stage_now()` in
> [`inner_life_part4.py`](../../../app/core/session/inner_life_part4.py).
> A `stage_rank` comparison gates roughly a dozen call sites — gestures,
> teasing, disclosure and petname use all sit behind a stage floor, exactly
> as sketched. The stage colours behaviour and is never named at the user.

**Motivation.** The four axes
([`relationship_axes.py`](../../../app/core/relationship/relationship_axes.py))
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
[`relationship_axes.py`](../../../app/core/relationship/relationship_axes.py)
(pure function over axes + tenure, hysteresis so it doesn't flap), a
terse stage line in the relationship prompt block
([`prompt_assembler.py`](../../../app/core/session/prompt_assembler.py)),
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
[`app/core/session/session_controller.py`](../../../app/core/session/session_controller.py)
(detect the gap on session resume),
[`prompt_assembler.py`](../../../app/core/session/prompt_assembler.py)
(a one-shot reconnection cue),
[`app/core/relationship/relationship_axes.py`](../../../app/core/relationship/relationship_axes.py)
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
[`app/core/relationship/shared_moments.py`](../../../app/core/relationship/shared_moments.py)
(new repair-flavoured moment or kind),
[`app/core/affect/`](../../../app/core/affect/) rupture detector (the
natural producer — write the repair record when a detected rupture
resolves), retrieval surfacing in
[`rag_retriever.py`](../../../app/core/rag/rag_retriever.py). Privacy/tone
guard: never weaponise a past conflict ("you always do this") — the point
is "we're good at sorting things out," not a grievance ledger.

---

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
[`app/core/affect/`](../../../app/core/affect/) (Aiko-side state source),
[`relationship_axes.py`](../../../app/core/relationship/relationship_axes.py)
(trust/stage gate),
[`prompt_assembler.py`](../../../app/core/session/prompt_assembler.py)
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

---

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
[`shared_moments.py`](../../../app/core/relationship/shared_moments.py) /
[`relationship_axes.py`](../../../app/core/relationship/relationship_axes.py)
(source signals + closeness gate),
[`prompt_assembler.py`](../../../app/core/session/prompt_assembler.py)
(rare appreciation cue), K15 budget for pacing. Tonal guard: specific
and earned, never a generic compliment generator.

---

## J11. Affection-style learning — "how he likes to be cared for" — SHIPPED

> **Shipped.** Implemented as a per-user weighting over five affection
> kinds (`touch` / `teasing` / `appreciation` / `words` / `space`) in
> the pure module
> [`affection_style.py`](../../../app/core/relationship/affection_style.py)
> (kv_meta key `aiko.affection_style`, no schema). Per the design
> feedback "reactions should be confirmation, not required", the
> **primary** signal is *passive*: the post-turn hook attributes the
> just-observed K14 `EngagementResult` (engaged / disengaged /
> abandoned + length z) back to the affection kind(s) Aiko expressed the
> previous turn (`classify_turn_affection` → `apply_observation`). K32
> reactions are an optional confirmation booster
> (`apply_reaction_confirmation` in `world_mixin.apply_user_reaction`,
> `REACTION_TO_KIND`). Weights are floored ("bias, never collapse"),
> slowly decay toward uniform via the idle
> [`AffectionStyleDecayWorker`](../../../app/core/relationship/affection_style_worker.py),
> and are **never rendered into a prompt** — they only tilt the J10
> appreciation cooldown and the K59 tease-collection cooldown via
> `_affection_style_bias(kind)` (touch self-paces post-B7, so it has no
> gate to bias). The K32 tray also gained four kinds (🙏 grateful, 🥰
> melted, 🙄 eye-roll, 🥺 moved) and a Settings → Avatar reactions
> legend. Settings: `agent.affection_style_*` (enabled / learning_rate /
> reaction_weight / floor / decay_half_life_days / bias_strength /
> bias_floor / bias_ceil / decay_interval_seconds). MCP:
> `get_affection_style_state`, `set_affection_style`,
> `reset_affection_style`, `force_affection_style_decay`. Tests:
> `tests/test_affection_style.py`, `tests/test_affection_style_worker.py`.

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
reading [`user_reactions.py`](../../../app/core/relationship/user_reactions.py)
+ engagement signals, a new field on
[`user_profile.py`](../../../app/core/infra/user_profile.py), and a weighting
read in the K31/K48/J10 gates. **Tonal guard:** bias, don't collapse —
keep variety so she never feels like a single-note affection machine, and
never announce the finding ("I've noticed you like it when I…"). Pairs
with J12. **Effort.** Medium.
