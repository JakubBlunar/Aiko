# Background workers

Shared scheduling for idle background jobs. G1 (`IdleWorkerScheduler`)
shipped as part of schema v8; G2 (schedule learning) and G3 (idle
curiosity) shipped on top of it. See [`shipped.md`](shipped.md) and
[`docs/memory-tiers.md`](../memory-tiers.md) for implementation
details.

Open: **G5** (self-tuning cooldowns) and **G6** (per-provider decline
attribution) below — both follow-ups to G4, which has shipped — plus **G7**
(worker prompts have no input-token accounting). New background workers should
register with the existing
[`IdleWorkerScheduler`](../../app/core/proactive/idle_worker_scheduler.py)
rather than spinning up their own threads, and should mirror the
INFO-level audit logging pattern established by
[`app/core/memory/idle_fact_checker.py`](../../app/core/memory/idle_fact_checker.py)
and [`app/core/proactive/idle_curiosity_worker.py`](../../app/core/proactive/idle_curiosity_worker.py).

For new worker ideas not yet committed to a section letter, see
[`patterns.md`](patterns.md) — several entries (K1 long-term goals,
K8 affect rupture, K10 persona regression, K14 engagement signals,
K21 fresh-eyes resummary) would naturally take the shape of an idle
worker.

---

## G4. Cue outcome accounting — SHIPPED

The accounting and the report have shipped; see
[`shipped/awareness.md`](shipped/awareness.md#g4-cue-outcome-accounting--which-of-the-50-odd-workers-earn-their-keep).
`get_cue_outcomes` reports the armed-to-surfaced ratio per cue, the decline
reasons, and which registered cues have never been armed at all.

Two pieces of the original sketch were deliberately **not** shipped with it
and are groomed separately below: self-tuning cooldowns (**G5**) and
per-provider decline attribution (**G6**). Three of the sketch's assumptions
turned out to be wrong and are worth keeping in mind for the follow-ups —
the gap-cue "one-of lottery" is a deterministic priority order rather than a
tie-break, "surfaced" needed no provider instrumentation at all (P31a's
`block_chars` already had it), and declines could not reuse L37's
`surfacing_outcomes` table without corrupting every aggregate over it.

---

## G5. Self-tuning cue cooldowns

**Motivation.** Every cue cooldown and daily cap is a hand-picked constant.
G4 now measures what they produce, so tuning them stops being a guess: a cue
with a healthy engaged rate can earn a shorter cooldown, one that
consistently lands flat a longer one.

**Sketched approach.** Read `CueDecisionStore.reach` plus the cue rows in the
L37 leaderboard (surfaced cues settle with an engagement label already), and
scale the configured cooldown by a factor clamped to a sane band around the
default — so the setting stays meaningful and a bad week cannot silence a cue
permanently. Opt-in behind a setting: observability first, adaptation second.

**Wait for data before building this.** Two of G4's numbers have to be read
first, and both can invalidate the design. A cue whose `reach_rate` is near
zero needs its *gate* looked at, not its cooldown — shortening the interval
would just produce more supersessions. And the five cues in `coarse_arming`
have an over-counted armed denominator, so their rates are floors; driving a
feedback loop off a floor would systematically over-shorten exactly the cues
whose measurement is weakest. Those five want G6-grade attribution (or a
real watermark) before they join any automatic loop.

**Open question.** Should a near-zero reach rate lengthen the worker's
*interval* automatically, or stay a report a human acts on? Leaning
report-only for the LLM-calling workers, where the cost is real but so is the
risk of switching off something that was about to matter.

**Effort.** Small-Medium. **Depends on.** G4 (shipped) + a few weeks of data.

---

## G6. Per-provider decline attribution

**Motivation.** G4 attributes the two *structural* declines precisely — a
gap cue that lost the priority mutex names its winner, and the K47
question-balance veto is named. Some provider gates now report their own
reasons, but uninstrumented bail points still fall into `provider`. A topic
gate that never matches, a cooldown that is too long, and a picker whose
candidates never clear the thresholds need different fixes.

**Why it was deferred rather than finished.** The four `inner_life_part*.py`
files hold 94 render providers and **491** `return ""` sites between them;
the ~15 cue providers are the gate-heavy ones (`turning_over` alone has
about ten distinct decline paths), so a full sweep is on the order of a
hundred edits across files already close to the 1,500-line budget. That is a
large mechanical change with real regression risk, spent before any data
says which cues need it — and G4's `reach_rate` plus `never_armed` already
identify *which* cues are failing, just not why.

**Sketched approach.** Do it per cue, worst `eligible_rate` with a substantial
`provider` bucket first, rather than as a sweep. Use
`note_decline(self, cue, reason)` at the deciding bail point; the existing
vocabulary includes `topic_miss`, `importance_floor`, `cadence_block`,
`age_window`, `no_opening`, `no_stock`, `no_candidates`, and `cross_lane`. The recorder and
`decline_reasons` already carry these without a schema change. Prefer
splitting a cue provider into a helper when its gate cascade is long enough
that the instrumentation makes it unreadable.

**Decision: distinguish gate misses from candidate supply.** Keep
`topic_miss` for stocked material that does not match the turn; use
`importance_floor` when topical candidates exist but their score or weight
does not clear the picker, and `age_window` when reflections exist but fall
outside the configured age range. Those are gates or weights to tune. Reserve
`no_stock` for a shelf that was armed but empty by claim time; it is a
supply-timing problem, not evidence that a threshold is too strict. Use
`no_candidates` when an armed gap slot has no usable source rows or topic
references. A truly empty corpus normally never arms a pool-backed cue,
so it belongs in the existing `never_armed` / pool-inventory view instead.
Read these separately before changing weights: more weight cannot create
missing candidates, and adding candidates will not fix a gate that never
matches.

**Effort.** Small per cue, Medium for all of them.
**Depends on.** G4 (shipped).

---

## G7. Worker prompts have no input-token accounting

**Motivation.** The chat prompt is budgeted to the token: `assemble_with_budget`
sizes every region against the context window, `context_budget_fraction` caps
surfacing, and the T3 selector drops the lowest-weighted survivors when the room
runs out. **Worker prompts have none of that.** A worker assembles a string and
hands it to the model, and if the string is too long the only feedback is a
truncated or degraded answer — silent, and indistinguishable from the model
simply doing badly.

L28 fixed one instance of this and that is what surfaced the general gap: before
concept diets, "read the concept layer" quietly meant "read all of it" as the
store grew, so the diet budget (`concept_diet_token_fraction` capped by
`concept_diet_max_tokens`) is the first worker input with a size. Nothing else a
worker packs has one.

**The clearest offender.**
[`SummaryWorker._maybe_summarize`](../../app/core/proactive/summary_worker.py)
calls `self._db.get_messages(session_key, offset=offset)` with **no limit** and
renders every returned row through `format_transcript`, so the prompt is
proportional to however many messages accumulated since the last summary. The
normal case is small — `min_unsummarized_messages` is 6, and
`_maybe_compact_on_pressure` fires on prompt pressure well before a window gets
big — which is exactly why this has never been noticed: the bad case needs a
long unsummarized run (a crash before a summary landed, a long voice session, a
`min_msgs_override` path), and when it happens the failure is a worse summary
rather than an error. A summary that silently drops its oldest beats also feeds
compaction, so the loss propagates into the chat prompt.

**Decision: one shared budget helper, worker-owned reduction.** The helper
receives the *actual call's* route context window, output-token cap, fixed
system/user instructions, and rendered candidate items. It estimates the
whole request (including message framing), reserves the output cap plus a
margin for tokenizer error / reasoning headroom, and returns the input
allowance. Do not infer the window from the loaded client: routes can share a
provider while having different windows, and a worker such as `SummaryWorker`
sets its own `num_predict` (`summary_target_tokens`) rather than using the
route's default `max_tokens`. Resolve the window from the route used for that
call (`_worker_route_model_ctx` for `worker_default`, the explicit route for
others) and the output reserve from the call's actual limit. Unknown window
or an oversized fixed prompt must be reported, not treated as unlimited space.

**Measure and reduce.** Record rendered chars, estimated input tokens,
context window, reserved output tokens, and items dropped per run. If the
request exceeds its allowance, let the worker choose which items to omit and
re-render until it fits; the shared helper does the arithmetic, not a generic
"drop the first N" policy. Warn when items are omitted or the non-droppable
instructions alone cannot fit. Never send a known over-budget prompt and
silently hope the model truncates it. Use `estimate_tokens` plus message
framing initially; the estimator is approximate, so keep a safety margin and
compare estimates with actual usage before tightening it.

**Summary cursor invariant.** The previous sketch said to drop the oldest
transcript rows and keep the prior summary. That only works if those rows
are *already* covered by that summary. `SummaryWorker` currently advances
`messages_summarized` to `total` after a successful call: dropping unsummarized
rows while doing so would permanently skip them. Pack the earliest
unsummarized rows that fit (plus bounded already-summarized overlap), advance
the cursor only through the last included row, and leave the rest for the next
pass. Other workers choose their own reduction order (messages, memories,
concepts) without sharing this cursor policy.

**Effort.** Small (measurement) / Small-Medium (caps, per worker).

**Depends on.** Nothing. Related to P31a (`block_chars`, which did the same
thing for *prompt-block* sizes) and to L28's diet budget, which is the pattern to
copy.

**Implementation status: partial rollout.** The shared helper now accounts for
whole-message input and reserved output against the worker route's context
window. Summary and memory extraction pack oldest-first and advance their
cursors only through included messages; belief inference keeps newest evidence
and sheds optional hints first. All three report sizes and omissions. Other
worker prompt builders still need to adopt the helper with their own reduction
rules; this is not yet a global cap on every background call.

---

## G-CLEANUP. `consolidator_state.last_cluster_index` is not dead weight — do not drop it

Filed as a trivial cleanup ("nothing reads it, drop the column in the next schema
bump") on the strength of the comment in
[`memory_consolidator.py`](../../app/core/memory/memory_consolidator.py), which
does say the field is unused. The comment is accurate about *the consolidator*
and misleading about the column, and the original suggested fix would have
broken a shipped feature.

[`relationship_pulse.py`](../../app/core/relationship/relationship_pulse.py)
stores its own state in `consolidator_state` under a namespaced key
(`_relationship_pulse_state:<user_id>`) and uses `last_cluster_index` to hold
**the relationship's `total_turns` as of the last pulse** — nothing to do with
clusters. `_read_state` reads it back and the value gates the pulse:
`if current_turns - last_turns < self._min_turns: return False`. Live, the row
reads `last_cluster_index = 1699`. Drop the column and the pulse either raises
on its `SELECT` or, if the read is removed along with it, silently loses its
"enough has happened since last time" gate and fires on elapsed hours alone.

**So the actual cleanup is the opposite of the one filed.** Either rename the
column to something both callers can honestly share (a schema bump plus two
call sites), or give the pulse its own `kv_meta` key and leave the consolidator's
column genuinely unused. Prefer the second: the pulse is squatting, and the
namespaced-key trick that makes the squat work is itself the tell. Whichever
way, the source comment should say "unused *by the consolidator*; see
`relationship_pulse`", because that comment is what made this look safe.

**Recurring shape:** a comment saying a field is unused is a claim about one
reader, not about the field. Grep the column name, not the module. Effort:
small.

For perf / observability gaps that aren't workers in their own
right (turn-level embed budget, idle-worker queue visibility,
typed-mode prefetch, etc.), see
[`perf.md`](perf.md).
