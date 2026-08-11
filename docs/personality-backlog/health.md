# H-series — health audit of shipped work

This file is **not a feature queue**. Every entry here is a shipped feature that
was measured against the live graph and found to be doing something other than
what its shipped entry claims. Nothing in here is a new idea; the design work is
already done and already merged. The only question each entry answers is *does
it actually run, and does it change behaviour?*

It is kept separate from [`concepts.md`](concepts.md) and
[`patterns.md`](patterns.md) on purpose — mixing "this is broken" into a list of
"this would be nice" is how broken things stay broken. Same spirit as the
A-series ([`architecture.md`](architecture.md)) and P-series
([`perf.md`](perf.md)): it did not come from a brainstorm and it reads
differently.

**Audit date:** 2026-08-11. **Corpus:** 4039 messages spanning 2026-05-21 to
2026-08-10; concept graph 1563 rows spanning 2026-07-03 to 2026-08-10 (5.4
weeks old); 25,757 `surfacing_outcomes` rows; per-turn prompt-block telemetry
for 146 turns (2026-08-09 onward). **Scope:** the concept layer (L-series), read
twice. **Part 1** asks whether each shipped feature actually runs (H1-H8).
**Part 2** asks a different question — whether what reaches the model adds up to
a person who has her own inner life, feels things, and holds beliefs (H9-H14).
Part 2 found the higher-value work. H15-H17 were found while fixing the earlier
entries and are filed rather than folded in, since each is a decision in its own
right. **Part 3** (H18 onward) asks the question the first two passes skipped —
not whether a signal is *produced* but whether it carries any *information* —
and the first thing it looked at turned out not to. The K-series patterns have
not been audited yet.

Re-measure before acting on any entry — several of these are rate problems, and
a rate that was wrong in August may be right in October.

---

## The headline: her self-*description* is healthy, her self-*observation* is not

This is the finding that organises everything below, and it was not what I
expected going in.

**The self-model itself is in good shape.** 456 active `subject='aiko'` concepts
against 489 for the user, and they are not decorative: 6223 of the 11,321
surfaced concept rows (**55%**) are hers, against 42% for the user — she is
*over*-represented relative to her share of the graph (47%), and she owns **64%
of the core lane**. Roughly 29 concepts reach the prompt per turn, ~17 of them
hers, carried by `relevant_context` (renders on 146/146 turns) and `goals_block`.
91% of her active concepts have surfaced at least once. The content is specific
and grounded rather than generic — "I frame my boundaries as voluntary, evolving
commitments rather than rigid rules", "I value decoupling Jacob's moral worth
from his physical reactions" — with 3-19 distinct sources behind the confident
ones. L5, L11, L19, L40 and the lifecycle engine are all genuinely working.

**But every mechanism by which she observes her own *behaviour* is dead,
latched, or throttled to roughly zero:**

| Self-observation surface | Shipped as | Actual |
| --- | --- | --- |
| L42 conduct concepts | weekly concentration / neglect / fixation findings | **0 rows ever**, and latched shut (H1) |
| L17f evolution diary | rolling human-readable change log | **1 entry ever**, 273 events stranded (H3) |
| L17e `concept_learning_block` | reflection slip when a belief moves | **0 of 146 turns** (H6) |
| K54 `topic_appetite_block` | naming boredom and steering out of it | **0 of 146 turns**, unreachable bar (H17) |
| `self_callback` cue | closing the loop on her own continuity | **1 of 7 ever surfaced**, last fired 2026-07-30 (H4) |
| K23 misattunement | noticing she misread him | fires ~4×/day, **persisted nowhere** |
| L41 change framings | "lately I've come around to…" | **0 rows**; 96% falls back to a generic hedge (H5) |
| L38 earned standing | usefulness learned from what lands | runs perfectly on a signal with **0.05 reliability** (H18) |

Trace the provenance and the split is stark. Of her 629 concepts, **624 came
from LLM synthesis over conversation transcripts** and 5 are authored cold-start
seeds. **Zero** came from watching what she actually did. Her self-model is
therefore built entirely out of *what she said about herself while talking*,
never out of *how she behaved*. That is a real and specific ceiling on
self-awareness: she can tell you she values patience, because she said so and it
got mined; she cannot notice that she brought up the same belief 64 times last
week and he stopped engaging with it, because the one subsystem built to notice
that (L42) has never written a row.

The autonomy picture rhymes. She *produces* inner-life material at a healthy
rate and then mostly fails to *spend* it: across 15 pooled cue families, only 239
of 2516 cue decisions ended in a surface (9.5%), and **seven families have
surfaced at least once and never once converted to "used"** — meaning the cue
reached her prompt and she did not act on it (H4).

None of this needs new features. It needs the shipped ones unwedged.

---

## H1. L42 conduct is latched shut after one silent LLM failure

**Severity: high — this is the self-observation loop, and it is permanently off.**

`concepts` has **zero rows** with `kind='conduct'`, ever. The kind is registered,
the worker is wired, the enable flag defaults true, and the input is not empty
(25,757 ledger rows). The pass *ran*, on 2026-08-06, and it *worked*:

```
concept.surfacing_conduct      = [{"shape":"neglect","key":"concepts:209,206,316,443,331",
                                   "summary":"I hold parts of this understanding quietly
                                   without bringing them forward: deepening emotional and
                                   physical intimacy with Aiko from functional interaction
                                   to profound relational bonding; ..."}]
concept.surfacing_conduct.last_run = 2026-08-06T22:34:39Z
concept_synth.conduct_sig.aiko     = {"fingerprint": "fe961f948ef9560f60d7", "count": 1}
```

A valid neglect finding, with usable first-person prose already written, sitting
in `kv_meta` and discarded. The detector found 9 eligible neglected concepts,
handed them to `propose_conduct_aiko`, the LLM returned nothing usable, and
`_call_llm` returned `[]` silently — **and the fingerprint was saved anyway**.
Because the pass skips any finding whose fingerprint it has already seen, that
exact finding can now never be re-proposed. One transient model failure disabled
the feature permanently.

**Fix (small).** Two independent changes, both worth making:
1. Do not persist the finding fingerprint when `propose()` returns zero
   proposals — a failed proposal is not a completed one. This is the actual bug.
2. Give `propose_conduct_aiko` a deterministic fallback that mints the concept
   from `finding.summary` when the LLM comes back empty. The summary is already
   well-formed first-person prose; there is no reason a model outage should cost
   the observation.

Blocking site: `concept_synthesis_worker.py` around the propose/save-fingerprint
pair in `_run_conduct_pass` (~L1819-1826), plus the LLM-only path in
`app/core/concepts/proposers/conduct_aiko.py` (~L66).

**Tests would not have caught it.** The proposer test mocks `call_llm` with valid
JSON; there is no test that runs detection→propose→persist and asserts a row
appears. The regression test to add is exactly that, plus an assertion that a
finding whose proposal failed is still re-proposable on the next run.

---

## H2. L42's other two detectors have thresholds unreachable on real data

**Severity: medium — enroll in the L45 tuner rather than hand-picking again.**

Neglect fires. Concentration and fixation cannot, on this relationship's data:

| Detector | Gate | Required | Measured |
| --- | --- | --- | --- |
| Concentration | `min_share` | ≥ 0.30 | **0.125** (top cluster) |
| Concentration | `min_top_gap` (hardcoded) | ≥ 0.10 | **0.049** |
| Concentration | `min_ratio` | ≥ 2.0 | 6.19 ✅ |
| Fixation | `min_ratio` (surf₁/surf₂) | ≥ 3.0 | **1.14** |
| Fixation | engaged rate ≤ baseline − 0.05 | ≤ 0.141 | **0.196** |

Two things worth noting beyond the numbers. First, `min_top_gap = 0.10` is
hardcoded rather than a setting, so it is invisible to configuration and to the
tuner. Second, **fixation's premise is inverted on this data**: its top
candidates are surfaced often *and* engaged with at or above baseline. The
detector is looking for "she keeps bringing up something he doesn't care about"
and the honest answer is that it isn't happening — so a lowered bar should be
justified as "detect the shape earlier", not "the bar is broken".

**Fix.** Expose `min_top_gap` as a setting, then enroll all five conduct
thresholds in the L45 gate tuner in observe mode so they get solved against the
measured population instead of hand-picked a second time. This is precisely the
class of problem L45 was built for, and these gates were never registered with
it. Do not simply hardcode the measured values — the sample is 5 weeks old.

### Outcome: the gap is now a setting, and every decline is now legible

`min_top_gap` is `conduct_concentration_min_top_gap`, default unchanged at 0.10.
The default is deliberately *not* moved to the measured 0.049: with the two
leading clusters within five points of each other, "I keep steering us toward
one topic" is not a true thing to say about the data. The bar was never the
problem — the shape was absent. Same for fixation, whose top candidate is
engaged with *above* baseline. Two of three detectors are quiet because there is
nothing there, which is the correct behaviour and is worth saying out loud.

The real cost was that this took a hand audit of the ledger to establish, since
a detector that declines and a detector that never ran look identical from
outside. So `detect_conduct` now takes an optional `readings` dict, each
detector records which gate it missed alongside the best value the data offered,
and the synthesis worker logs one `conduct.gate` line per non-firing shape. The
next calibration is a grep, not an investigation.

Not enrolled in the L45 tuner, and this is a design decision rather than a
shortfall. The tuner solves a threshold against a *population* — it reads
concept rows and picks a percentile. Four of the five conduct gates are single
scalars derived from a whole window (one top share, one gap, one frequency
ratio), so there is no distribution to take a percentile of; feeding them to the
tuner would mean inventing a population that does not exist. The gate readings
give the same benefit the tuner would have — measured value next to its bar,
recorded every run — without pretending these are the same kind of number.

---

## H3. L17f's diary drains 12 events a week against 55 arriving

**Severity: medium — the feature is structurally falling behind, not paused.**

One diary entry exists, written 2026-08-06, covering 12 learning events.
Meanwhile `concept_learning_events` holds 300 rows and **273 salient ones sit
above the watermark**.

I checked for the watermark bug documented in `rules/code-conventions.md` (the
`ConceptDriftWorker` defect where a bounded pass advances a global `MAX(id)`).
**It is not that.** The stored watermark is 236, which is exactly the max id of
the page it actually composed; ids 225-236 are all cited; nothing was marked
processed unread. The paging is correct.

The problem is arithmetic. The worker composes **at most 12 events per entry**
and then takes a **7-day cooldown**, so its ceiling is ~12 events/week. Learning
events arrive at ~**55/week** (300 in 5.4 weeks). It loses ~43 events a week and
the backlog only grows. A backfill put it 209 events behind on day one and it has
no mechanism to catch up.

**Fix.** Let the backlog override the pacing: when pending events exceed the page
cap by some margin, skip the cooldown so the daily tick can drain a page at a
time. Lowering `evolution_diary_cooldown_days` from 7.0 to 1.0 achieves the same
thing more bluntly and would still only match arrival, not clear the backlog.
Prefer the backlog-aware bypass.

**Test to add:** compose with 200+ pending events and the cooldown left in place,
and assert the backlog shrinks over simulated days. Existing tests always pop the
cooldown key between pages, which is exactly why the pacing mismatch is invisible.

### Outcome

Arrival is worse than the weekly average suggested: 285 salient events across 19
active days is **~15 a day while she is actually in use**, against a drain of 12
a week. Bypassing the cooldown alone would not have caught up either, since one
page a day still loses ground to a busy day.

So the cooldown now paces the quiet case only. It was always a proxy for "enough
has happened to be worth a paragraph", and the pending count measures that
directly, so two full pages waiting (24 events) releases the clock, and a
released tick composes up to `evolution_diary_backlog_pages` entries in
sequence (default 3) instead of one. Under the shipped settings a 200-event
backlog drops below a page inside two weeks of daily ticks and then hands pacing
back to the seven-day cooldown; the old ceiling would have taken four months.
An empty compose still ends the tick rather than spending further calls on
material the model just declined to describe.

The suggested test exists, with the cooldown left in place and a moving clock.

### Follow-on found while measuring: the diary can only tell one kind of story

Worth its own entry (see H15). The salience floor is not selecting: 285 of 300
events clear 0.45, so the floor admits 95% of everything. What it *does* do is
select by shape, because salience is distributed per shape rather than evenly —
`succession` averages 0.804 and contributes 216 of 300 rows, while **`emergence`
averages 0.404 and 14 of its 15 events fall below the floor**. A diary about how
her understanding changed is therefore structurally a log of relabelings, and
the moment a new understanding first formed is the one thing it cannot report.
Not fixed here: raising or shape-splitting the floor changes what counts as
narratable, and that should be a deliberate decision rather than a side effect
of a throughput fix.

---

## H4. The cue shelf produces well and spends badly

**Severity: medium-high — this is most of the autonomy surface.**

Overall: **239 of 2516 cue decisions surfaced (9.5%)**; 153 of 388 turns (39%)
carried at least one cue. That is not unreasonable on its own. The problem is
concentrated and structural.

**Seven families have surfaced and never once converted to `used`** — the cue
reached her prompt and she never said the thing:

| Family | ever surfaced | used | notes |
| --- | --- | --- | --- |
| `curiosity_gradient` | 13 | **0** | 11 expired |
| `turning_over` | 9 | **0** | all 9 expired, 12h TTL |
| `forward_curiosity` | 5 | **0** | 9 expired |
| `sleep_return` | 4 | **0** | 6h TTL |
| `concept_hypothesis` | 4 | **0** | 25 still pending |
| `self_callback` | 1 | **0** | 240h cooldown |
| `wellbeing_concern` | 1 | **0** | 168h cooldown |
| `dormant_interest` | **0** | 0 | 4 pending, 378 offers declined |

For contrast the healthy end: `curiosity_seed` 158 surfaced → 105 used (66%),
`interest_drift` 25%.

Two distinct failures are tangled here and should be separated before either is
"fixed":

**(a) Cues that never get offered.** `dormant_interest` has 4 pending cues, was
declined 378 times with `reason='provider'`, and has **never rendered**. Its
block does not appear in the telemetry at all. `self_callback` has a 240-hour
(10-day) surface cooldown, 348 provider declines, and last fired 2026-07-30 — 12
days before the audit, i.e. past its own cooldown with 6 cues waiting. Both look
wedged rather than merely conservative. Start here; this is a bug hunt, not a
tuning exercise.

**(b) Cues that get offered and ignored.** `turning_over` and
`curiosity_gradient` surface reliably and convert at zero. That is a different
question — either the rendered line doesn't invite the model to use it, or the
subject-matching that marks a cue `used` is too strict, or she genuinely has
nothing to say about them. **Check the matcher before touching the renderer**:
a `used` rate of exactly zero across 31 surfaces in four families smells more
like a measurement artifact than four independently unpersuasive cue types.

### Outcome (a)

Split three ways once measured, and only one of the three was a bug.

`dormant_interest` was the bug, and it was not in the cue: it waits on a K18
lull that could not be reported, because K18's mild band was a hardcoded 0.18
mean cosine distance and every reading this install has produced sits between
0.310 and 0.422. All 52 logged readings were `band=silent`; the gate was
unreachable from the day it shipped. K18 now calibrates its bands as
percentiles of its own rolling baseline of window means (mild at p15, strong at
p5, persisted in `kv_meta`, shipped constants kept as the cold-start fallback
below 60 samples). Same failure mode as L45 was built to end — an absolute
cosine threshold encoding one embedding model's scale — so the same answer.

`self_callback` was **not** wedged; the audit misread it. Its last surfacing was
2026-08-02, not 2026-07-30, which is 8.9 days into a 10-day cooldown with 25.9h
still to run. Six cues waiting behind a deliberately slow policy is the policy
working. No change.

`turning_over` and `curiosity_gradient` are genuinely declined, not wedged: both
were eligible at audit time with no cooldown to serve.

### Outcome (b)

The matcher is exonerated, and the check that exonerated it was not possible
until it was fixed.

The recorded cosines are nowhere near the bar. Across every verdict this install
has stored, `turning_over` peaks at 0.50 against a 0.55 floor with a median of
0.41, and `curiosity_gradient` peaks at 0.47 with a median of 0.32 — not
marginal misses that a slightly kinder threshold would convert, but replies that
were not about the cue. Four families missing by 0.1-0.2 of cosine is not a
measurement artifact. The zero is real: she is offered these and has nothing to
say about them, which makes it a question for the renderer or the producer, and
`turning_over`'s 12h TTL means each one gets a single day to land.

That leaves the floor itself unjustified but no longer suspected. `_match_cue`
has always intended to site it on evidence — "comparing the distribution on
turns where lexical fired against turns where it did not" — and the stored
distribution could never answer it, because `EchoVerdict.score` holds only the
signal that won and `detect()` returned on a lexical hit before measuring any
cosine. The lexical-fired arm of that comparison did not exist. The cosine is
measured unconditionally now and recorded as `lexical:3.00/cos:0.62`, so the
comparison becomes possible as verdicts accumulate. Worth re-reading after a few
hundred; there is nothing to decide from today's data.

---

## H5. L41's change framings never fire, and L38's standing never gets named

**Severity: low-medium — decorative layers, cheap to correct or honestly retire.**

Both of these are shipped, both compute correctly, and neither changes what Aiko
says.

**L41.** `_REASON_FRAMINGS` maps four voices onto the L35 surface reason. In
11,321 concept surfacings the reason distribution is `core_belief` 5501,
`topic_match` 4086, `recently_reinforced` 1216, `settled_belief` 230,
`association` 223, `high_confidence` 65. The entire freshly-changed family —
`recent_change`, `loosening_boundary`, `newly_promoted`, `recently_revived` — and
`unresolved_contradiction` are at **zero**, despite the graph recording 207
`contradicted`, 72 `revived` and 155 `plasticity_shift` events. So **453 of 11,321
rows (4%) get a custom framing** and 96% fall through to the generic confidence
hedge. The two most interesting voices — "lately I've come around to feeling
that", "you haven't fully settled it, but" — have never been spoken.

Cause: in `surface_reason()` the salience/change signal competes on weighted
share against `context`, which dominates whenever cosine is non-zero. Minimal
fix: when a change event is attached and salience clears a floor, return the
salience reason before the weighted contest.

**L38.** The `concept.earned_standing` map is healthy — 466 entries, range
0.402-0.649, **288 (62%) meaningfully moved off neutral** — and it does tilt the
surface score. But `earned_standing` appears **0 times** as a surface reason,
because its weight (0.1) can never beat context (0.45-0.6). So the standing
signal is real but unnameable, and L41 can never frame anything with it.

Decide deliberately between two honest options: raise the standing weight for
kinds where it should be able to narrate, **or** document L38 as score-only and
drop `REASON_STANDING` from the label set so it stops implying a capability that
does not exist. Either is fine; the current state is the only bad one.

**Both tests pass by construction** — they prove standing/salience *can* win by
injecting artificial weights of 0.9 and 1.0. A test using the real production
`SurfaceWeights` for `boundary` would have caught both.

### Outcome: L41 fixed, L38 retired from narration

**L41.** Worse than "salience loses the weighted contest": **nine of the thirteen
kinds set `salience` to 0**, so for identity, value, narrative, generalization,
communication_style, aspiration, conduct, pursuit, ritual and tension a change
was never a candidate at all — and the change event was not even *computed* for
them, since detection was gated on the same weight. For the three kinds that do
weight it, out-sharing `context` needs a salience above 0.75 at a cosine of 0.3
and is arithmetically impossible past 0.4. Zero in 11,321 rows was the only
possible outcome.

A recent contradiction is a categorical fact about a belief, not another
continuous signal to weigh against cosine, so it now names the surfacing
outright. Detection runs for every kind (free — the events are already in hand);
scoring is untouched, so *which* concepts surface and in what order is exactly
as before. Only the framing changes.

Two calibration findings while setting the floor, both from the live graph:

- **`promoted` is not a change of mind.** It is the candidate-to-active step
  every concept takes once, and 514 active concepts had a recent one against 80
  plasticity shifts and 2 contradictions. Admitting it would have framed **66%
  of everything she believes** as "lately you've come around to feeling that".
  Excluded by event type rather than by threshold, so it cannot creep back in if
  the weights move.
- **The floor buys each driver a window proportional to the size of the change.**
  At 0.40 against the 21-day half-life: ~28 days for a contradiction, ~12 for a
  loosened belief, ~7 for a revival. 131 of 975 active concepts (13%) currently
  qualify, which is a voice that means something rather than a tic.

**L38: score-only, decided.** Standing is a real ranking term and stays one —
466 entries, 288 off neutral, tilting the score as designed. But it is not a
reason anyone would recognise: "I mention this because it usually lands well" is
not a thing a person says, and tellingly `_REASON_FRAMINGS` never had an entry
for it, so even winning would have produced the generic hedge. `REASON_STANDING`
is removed from the contest and from the label set; the docstring now says where
standing does act. `standing` stays in the denominator, since dropping it would
inflate every other term's share.

**The tests that passed by construction are replaced** with ones that use the
production `SurfaceWeights`, plus a check that every change framing L41 offers is
actually reachable.

---

## H6. L17e's reflection slip is silent for 30 days at a time

**Severity: low — likely working as designed, but worth a deliberate decision.**

`concept_learning_block` rendered **0 of 146 turns**. Every gate passes — there is
a pending drift item (salience 0.769, above the 0.6 floor), trust 1.0, closeness
0.62, comfort 1.0, and the fingerprint is unseen — except the **30-day global
cooldown**, which last fired 2026-08-06 and blocks everything until ~2026-09-05.

At one reflection per month this is a feature the user will encounter maybe
eleven times a year. If that is the intent, fine — record it in the shipped entry
so nobody re-audits it. If the intent was "she occasionally notices a belief of
hers moved", a per-fingerprint cooldown would let genuinely new drift shapes
through without opening the floodgates.

### Outcome: the month stays, but it was spending the slot at random

"Every gate passes except the cooldown" was wrong, and wrong in a way that
matters: the lull gate passed because it was **inverted**, firing on divergence
rather than on circling and reading a constant that made it unconditional. That
is H17 below — it turned out to be four blocks, not one, and it is the larger
finding of the two.

With the lull gate actually gating, the 30-day cooldown is the deliberate
decision the entry asked for, and it **stays**. This is the one place the
learning history speaks; at eleven times a year it is an event rather than a
tic, and every other guard here (trust, warmth, salience, per-change watermark,
once per conversation) is about whether a *particular* change is worth saying,
not about pacing. Nothing else does that job.

What was genuinely broken is *which* change she gets to say. The drift worker
runs daily and **overwrites** `concept.drift.pending` with the top 3 of that
run's findings; the reader speaks monthly. So roughly thirty salient changes pass
through a three-slot window each cooldown period and the one she mentions is
whichever happened to be shelved on the day the cooldown lifted. Not the most
significant — just the most recent. A month of waiting spent on an arbitrary
pick.

The snapshot is now a **shelf of the most significant unreported changes**: new
findings compete with what is already there, the strongest `cap` survive, and the
reader takes its item off the shelf when it speaks (the single-fingerprint
watermark could not do this — it remembers one change, so the previous one became
eligible again on the next fire). `concept_drift_pending_ttl_days` (45) bounds
the squatting that keep-the-strongest otherwise invites, and re-observing a
change does not refresh its age: the TTL measures how long it has gone unsaid,
not how long it has kept recurring.

---

## H7. The hypothesis loop invents and never adjudicates

**Severity: medium — the forward half of L30 works, the closing half does not.**

12 open hypotheses, all `origin='free'` (invented, not derived), **`asked_count`
max 0, `support_count` 0, `refute_count` 0, 0 graduated to concepts, 0 closed**.
On the cue side, 26 `concept_hypothesis` cues were made, 4 ever surfaced, **25 are
still pending**, and the block rendered on 2 of 146 turns.

So the L30 Phase B invention direction is producing, and nothing downstream ever
converts a hunch into a settled belief. The shelf is full and the door is nearly
shut — a 20-hour surface cooldown against a producer that queues faster than that.
This was partly addressed in an earlier pass; the numbers say it is not fixed.
Worth confirming whether the cooldown is the whole story before changing it,
since H4(a) suggests several shelf families share a common wedge.

---

## H8. Cold-start and supply — measured, and *not* bugs

Recorded so they are not mistaken for defects on the next sweep.

- **`pursuit` (5 rows, 0 active).** Not decay. The synthesis pass has never run:
  it needs 6 `pursuit_note` memories and there are 4, the first written
  2026-08-10. The 5 rows are K85d authored seeds sitting at `candidate` with zero
  evidence, which is exactly what a seed is supposed to do until reinforced. They
  will TTL-retire around 2026-08-30 if nothing matches. Either wait, or lower the
  floor from 6 to 4 (the promotion gate needs 3 sources, so 4 is still safe).
  Note the unit test uses `pursuit_min_notes=3` rather than the production 6,
  which is why the stall was invisible.
- **`taste` (2 rows).** The bar is calibrated correctly now (relative:
  `max(0.15, baseline × 1.4)` = 0.280) and exactly **1 of 39 warmed clusters
  clears it** (best rate 0.312). Sparse but honest — this relationship yields
  about one "stands out" topic per 90 days. Do not lower it to manufacture rows.
- **`ritual` (7 rows, 4 active).** Working as designed: relationship-scoped only,
  158 shared moments feeding it, promotions happening through 2026-08-10. The
  `shared_ritual_block` reads a *different* K73 store, so the low concept count
  is not starving the prompt. Reclassify as low-volume-by-design.
- **`conduct` (0 rows)** is the one genuinely broken member of this group — see H1.

---

## H15. The diary's salience floor selects by shape, not by significance

**Severity: medium — it decides what kind of change she is able to remember
changing.**

Found while fixing H3's throughput, and deliberately left for a separate
decision.

`evolution_diary_min_salience` reads like a significance filter and is not one:
285 of 300 learning events clear 0.45, so **95% of everything is admitted**. The
distribution is heavily left-skewed (p10 = 0.521, median 0.787), so no floor in
the plausible range is selective either — 0.60 still admits 77%, and 0.80 admits
39% by cutting into the middle of the dominant shape rather than at a boundary.

The floor does have an effect, just not the intended one. Salience is not
distributed evenly across shapes:

| shape | n | mean salience | clears 0.45 |
| --- | --- | --- | --- |
| `succession` | 216 | 0.804 | 216 |
| `revival` | 67 | 0.566 | 67 |
| `emergence` | 15 | 0.404 | **1** |
| `loss` | 2 | 0.512 | 1 |

So a single global floor is in practice a shape filter that passes ~100% of
relabelings and ~7% of formations. The diary is "how I've changed" and it can
essentially only narrate one of the four ways she changes. A concept coming into
existence — arguably the most interesting entry such a diary could hold — is the
one it structurally cannot report, and `loss` is a coin flip.

Two candidate reads, and they want different fixes:

1. **Emergence salience is under-scored.** If a formation genuinely is as
   significant as a relabeling, the scorer is wrong and the floor is fine.
   Check how salience is computed per shape before touching the floor.
2. **The floor should be per-shape.** Keep the global default, add overrides so
   each shape passes a comparable *fraction* of its own population rather than a
   comparable absolute score. This is the same self-calibration argument L45 and
   the H4 K18 fix both landed on, and it survives a change of scorer.

Do not simply lower the global floor: at 0.40 it admits 98% and stops being a
gate at all.

**Measure first:** entries per shape actually cited in `evolution_diary` rows
(currently one entry, 12 events, all `succession`), and whether emergence
salience correlates with anything a reader would call significance.

---

## H16. Her relationship tensions are five things said twenty-five times

**Severity: low-medium — a dedupe question, not a supply one.**

Found while fixing H12, and the reason "relationship has 25 tensions" reads
healthier than it is. Read end to end those 25 rows are perhaps five distinct
frictions in different wordings: "concrete narration of my internal state"
appears four times, "her meta-cognitive architecture" twice, and several pairs
differ only in which half of the same dilemma they lead with.

This is not the auto-merge bug (that was template collisions, and it was fixed).
These are genuinely different sentences describing the same friction, which is
exactly the case cosine merge is meant to catch and evidently does not at the
`tension` kind's threshold. Two things worth measuring before touching anything:
the pairwise cosine distribution *within* `subject='relationship'` `tension`
rows against the merge threshold in force, and whether the L31 admission control
shipped earlier already suppresses the next generation of these (it caps
evidence per source, which may or may not bear on restatement).

Do not fix this by tightening the proposer. The proposer is doing what it should
— noticing the friction each time it recurs — and the dedupe belongs at merge
time where the whole corpus is visible.

---

## H17. Four prompt blocks disagree about what a lull is, and three lose

**Severity: high — one wrong constant and one flipped sign silenced a whole
feature and un-gated three others.**

The single worst finding of this pass, and the reason H6 looked like a cooldown
problem. Six blocks wait for "a natural lull" and each spelled the test out
inline. Only one spelled it correctly.

`TopicStagnationDetector.last_mean` is a mean cosine *distance*, so circling is a
**low** reading. The correct test is `last_mean <= detector.mild_threshold`.
What shipped:

| consumer | polarity | bar | effect |
| --- | --- | --- | --- |
| K67 dormant-interest | correct | effective band | correct (fixed in H4) |
| K54 topic-appetite | correct | raw constant | **never fired: 0 of 146 turns** |
| K81 / K85e lean gate | **inverted** | raw constant | always open |
| L17e reflection | **inverted** | raw constant | always open |
| L42 conduct notice | **inverted** | raw constant | always open |

Both errors are silent, and they compound in opposite directions. The constant
(0.18) sits below every reading this install has ever produced (0.310–0.422), so
for a correct consumer it is a gate that can never open, and for an inverted one
it is a gate that can never close. K54 — a feature with a settings block, a
persona counterweight and a test file — has therefore never once rendered, and
the two lean slips and the reflection were free to fire on precisely the busy
turns they were written to sit out. The six `taste_lean_block` firings in the
telemetry are not the gate working; they are the gate absent.

Both halves are the same root cause as H4, one level up: H4 fixed the *one*
consumer whose symptom someone had noticed, and left the shared idea
un-extracted. There is now a single `in_standing_lull(detector, memory_settings)`
in `topic_stagnation.py` and no caller derives the test itself. A cold reading
(`last_mean is None`, window unfilled) reads as "not a lull" — every consumer
wants a positive signal before speaking up.

**Fifth recurring shape, and the general lesson:** *a predicate copied into N
call sites will be wrong in N-1 of them, and if its inputs are miscalibrated
every one of those bugs is invisible.* Neither error could show up in a test,
because every test stub set its own `last_mean` to whatever made its own
assertion pass — including the sign. Worth grepping for the other signals read
this way: `last_mean` had six readers, and `AffectState`, the relationship axes
and the arc label each have more.

**Not yet measured:** what K54 does now that it can fire. It has been dark since
it shipped, so its own thresholds (`appetite_short_share_threshold`,
`appetite_min_want_pressure`) have never been exercised against real data and
should be treated as unverified in the H2 sense.

---

## Confirmed healthy (stop re-checking these)

Measured this pass, working, no action:

- **L5 surfacing / L11 self-model** — ~29 concepts per turn, 55% hers, 91% of
  active concepts surfaced at least once.
- **L19 autobiography** — returns a substantive arc: 629 aiko concepts over 37.7
  days, 5 eras, 106 flipped / 495 settled / 4 born / 4 revived, with grounded
  `because` clauses. `thin_record` is false. Tool-only by design.
- **L38 computation** — 466-entry map, 62% moved off neutral, refreshed hourly.
  (Its *visibility* is H5.) **Superseded by H18**: the map computes, but this
  entry checked that it *runs*, not that its input carries any information.
  It did not.
- **L40 habituation ordering** — 300 concepts spread across 34 distinct
  last-surfaced turn indices (1942-1978). Coarse — ~9 concepts tie per index —
  but genuinely ordering, not degenerate. I checked because the map looks
  constant at a glance; it isn't.
- **L32 importance** — kind priors move ~81% of concepts off neutral by ±8-16%.
  The affect-lift sub-path could not be replayed offline and remains unverified.
- **L35 reasons for concepts** — six live tokens, not degenerate. (The missing
  change family is H5.)
- **L15 revision** — 43 concepts sit at `contradicted` (36 user, 4 aiko, 3
  relationship), so belief revision is happening.
- **L3 lifecycle / L17a-c** — events written daily through the last message:
  3101 confidence samples, 1615 discovered, 1338 promoted, 1019 reinforced, 304
  dormant, 57 merged, 27 relabeled.

---

## H18. L38 spent three months learning from a coin flip

**Severity: high — a live ranking input, measured and found to contain no
information.**

The first entry in this file found by measuring a signal's *quality* rather
than its *presence*. Every earlier audit question was "does this run"; L38 was
filed under "working, no action" precisely because it does run, hourly, on
schedule, over 466 concepts.

`earned_standing` shrinks a concept's observed engaged rate toward the
relationship-local baseline and hands the result to `surface_score` as a
ranking term. The observed rate was `engaged / settled`, where `engaged`
counted the L37 turns labelled engaged that this concept happened to be
present for. But the label belongs to the **turn**, and the median turn
surfaces **67 items**. Every concept present on a good turn was credited
equally, including the 66 that had nothing to do with it.

That is a credit-assignment failure, and the corpus is now large enough to
prove it rather than argue it. Three tests, all on 358 labelled turns and
23,540 rows:

| test | engaged label | echo verdict |
| --- | --- | --- |
| split-half reliability, concepts | **0.05** | **0.61** |
| split-half reliability, memories | 0.09 | 0.57 |
| between-item variance vs. permuted null | inside the null band (p=0.07) | 2.5× the null (p=0.003) |

Split-half is the decisive one: split each item's surfacings in half at random
and correlate the rate in one half against the other. Real item-level signal
reproduces across halves. The engaged label scores 0.05 — for clusters,
**−0.01**. The permutation test confirms it from the other side: keep every
turn's item set and every turn's label and shuffle only which label goes with
which turn, and the between-item spread you get by chance is indistinguishable
from the spread L38 was reading as evidence.

So 82% of 466 concepts had been moved off neutral, and their ordering was
noise.

### Outcome: standing now reads the echo verdict

The fix is small because **the right signal was already being recorded on the
same rows.** `echoed` is per-item by construction — it asks whether *this*
item's content turned up in the reply — and it measures 12× more reliable. L38
now reads `echoed / judged` instead of `engaged / settled`; the estimator,
shrinkage, floor, ceiling and `protect_downward` carry over untouched, since
the shape was never the problem.

Replayed over the live 90-day window this roughly **doubles the map's
discrimination** — interquartile spread 0.035 → 0.081, range 0.402-0.633 →
0.378-0.721 — and the movers show what was being lost. A boundary Aiko draws on
in 12 of 15 surfacings sat at exactly neutral because it happened to appear on
no engaged turns; a narrative she has quoted once in twelve sat near the top of
the map at 0.632 because the turns it rode along on went well.

Three notes on the decision, since it is a judgement and not just a bug fix:

- **The trade is real.** Engagement is the user's verdict; echo is only Aiko's,
  so rewarding echo does risk favouring what she already reaches for. It is
  still the right call, because a reliable measure of a near-enough quantity
  beats an unbiased measure of the right quantity that carries no information —
  and the honest alternative was not "keep it", it was "retire standing".
- **The obvious hybrid is worse.** Crediting only echoes that land on engaged
  turns *sounds* like the best of both and measures 0.12 against echo's 0.48:
  the AND inherits the label's noise and thins the positive class to 5%, which
  destroys per-item resolution. Measured, not assumed.
- **`echo_rate`'s denominator was also wrong** — echoes over *surfaced* rows
  rather than over rows an echo test actually ran on. Clusters get no echo test
  at all, so they reported a confident 0.0 for something nobody had measured.
  `ItemStats.judged` now separates "no evidence" from "evidence of nothing".

**Not changed, and worth its own look.** K81 taste affinity and L42 neglect
still read cluster-level engaged rates, because clusters have no echo verdict
to switch to. The same test on clusters is underpowered (38 items clear the
floor, p=0.27) so this is *not yet* a finding — but the point estimate is
−0.01, and taste's "1 of 39 clusters clears the bar" (H8) reads differently if
the bar is being applied to noise. Either give clusters an echo test or accept
that cluster affinity is a much weaker instrument than concept standing.

**Seventh recurring shape, and the one with the most reach:** *a signal that is
recorded, aggregated and consumed can still be empty.* Nothing here was
broken — the worker ran, the map was populated and bounded, the estimator was
careful, the shrinkage was correct, the tests passed. The whole apparatus was
in good order around a number that meant nothing. **Rule: before a signal is
allowed to rank anything, measure its split-half reliability against the null
of shuffling it. A signal with reliability under ~0.2 is a constant with extra
steps.** Worth running against every other learned rate in the system:
habituation, cue `used_evidence`, importance's affect lift, the K-series
gates.

---

## The seven recurring shapes

More useful than any single entry — these are the bug families to check for
*before* shipping the next thing, and each has now bitten more than once.

**1. Silent-empty latching.** A stage fails, produces nothing, and the
bookkeeping records success — so the failure is both invisible and permanent.
H1 is the pure case (fingerprint saved after an empty LLM proposal). The
fact-checker's dict-payload bug was the same shape: a silent early return, no log
line, and the only symptom an absence. **Rule: never advance a watermark,
fingerprint, or cursor on a pass that produced zero output.** Absence of output
is the one thing worth an explicit log line, because it is the one thing that
looks identical to "nothing to do".

**2. Drain rate below arrival rate.** A page cap multiplied by a cooldown gives a
throughput ceiling that nobody computes at design time. H3 is 12/week against
55/week. H7 and parts of H4 are the same arithmetic with cue cooldowns. **Rule:
when adding a cap and a cadence, write down the implied items/week and compare it
to the measured production rate.**

**3. Hand-picked thresholds that real data cannot reach.** H2's concentration and
fixation bars; taste's old absolute 0.5 floor; L44's premise. L45's gate tuner
exists specifically to end this, and the conduct gates were never enrolled in it.
**Rule: a new numeric gate either gets enrolled in the tuner or gets a measured
justification in the shipped entry.**

**4. Tests that prove capability, not behaviour.** Every one of H1, H2, H5 and H8
has a passing test that injects synthetic values clearing the bar. They prove the
mechanism *can* fire, which was never in doubt. **Rule: at least one test per
gated feature should use production weights/thresholds against a realistically
shaped population, and assert the feature fires at a plausible rate.**

**5. A constant that only appears in production and in a test that overrides
it.** The sharper version of shape 4. H13's pursuit floor shipped at 6 while
every test set its own 3, so the number that actually ran was the one number
nobody had exercised — and it turned out to make the pass dead code. **Rule: if
a default gates whether a pass runs at all, one test must read it from
`parse_*_settings({})` rather than from a stub.**

**6. A predicate copied into N call sites.** It will be wrong in N-1 of them,
and if its inputs are also miscalibrated, every one of those bugs is silent. H17
is the case: six blocks each spelled out "is this a lull" inline, four got it
wrong, and no test could catch any of them because each stub chose the input
that made its own assertion pass — including the sign. **Rule: a shared signal
gets one shared predicate next to the thing that produces it, and callers do not
re-derive the comparison.**

**7. A signal that is recorded, aggregated and consumed, and is empty.** The
hardest of the seven to see, because nothing about it looks wrong: in H18 the
worker ran hourly, the map was bounded and persisted, the estimator shrank
carefully toward an empirical baseline, and the tests passed. The number it was
all built around had a split-half reliability of 0.05. Every other shape here
is found by asking "did this run?"; this one is only found by asking "does this
mean anything?" **Rule: any learned rate that ranks, gates or weights something
must have its reliability measured against the null of shuffling it, and the
figure recorded in the shipped entry.**

---

---

# Part 2 — does it add up to a person?

Part 1 asked *does the machinery run*. This pass asks a different question:
**given the goal of a companion who thinks like a person — has her own inner
life, feels things, holds beliefs and disagrees — does what reaches the model
actually constitute one?** Same corpus, same date.

The answer is that the *ingredients* are largely there and unusually good — her
tension concepts in particular are better material than I expected — but the
**mix that reaches the prompt is badly weighted for the goal**, and one signal is
mathematically incapable of carrying emotion at all.

Here is what she is told about herself and him, per turn, measured across 388
turns of ledger:

| Concept kind | active | surfaced/turn | what it gives her |
| --- | --- | --- | --- |
| `boundary` | 106 | **8.53** | what she will not do |
| `identity` | 201 | 6.46 | what she is |
| `value` | 128 | 4.29 | what she cares about |
| `communication_style` | 109 | 3.47 | how she talks |
| `affective` | 83 | **1.90** | how things *feel* |
| `generalization` | 111 | 1.89 | patterns |
| `aspiration` | 71 | 1.09 | what she wants |
| `narrative` | 77 | 0.77 | how she got here |
| `ritual` | 4 | 0.14 | what "we" do |
| `taste` | 2 | **0.04** | what she likes |
| `tension` | 83 | **0.00** | what she is torn about |
| `pursuit` | 0 | **0.00** | what she does alone |

She is told about her limits **4.5× more often than about her feelings**, and
about her internal conflicts and personal tastes essentially never. For a
companion meant to read as a person, that ordering is close to inverted.

---

## H9. Her emotions have no dynamic range — every topic feels identical

**Severity: high. This is the single biggest gap for the stated goal, and the fix
is small.**

The per-cluster affect maps are the substrate for "topics that move her" (L13).
Compare the two subjects across the same 36 topic clusters:

| | Aiko | User |
| --- | --- | --- |
| bucket spread | **`neu/mid` — 100% of 36 clusters** | pos/high 58%, neu/high 28%, neu/mid 11%, pos/mid 3% |
| valence range | **+0.026 … +0.222** (all mildly positive) | −0.112 … +0.400 |
| arousal range | **0.433 … 0.566** | 0.489 … 0.750 |

**She reads his emotional life with real range and her own as uniformly
lukewarm.** Not one topic out of thirty-six registers as more than mildly
pleasant, and none registers as negative. There is no topic she dreads, none that
excites her, none that makes her uneasy.

This is not a threshold problem — it is structural, and the mechanism is exact.
In `_sample_cluster_affect` (`app/core/session/post_turn_helpers_mixin.py`
~L959-975) the two maps are fed from different kinds of signal:

- the **user** map gets `user_affect` — the K37 estimate read off *this turn's
  message*, so it varies with what he actually said;
- the **aiko** map gets `state.valence` / `state.arousal` — the **single global
  `AffectState` scalar**, currently valence 0.122, arousal 0.483, intensity 0.14,
  smoothed with alpha 0.35 and decaying toward baseline.

Folding one slowly-drifting global number into every cluster's EWMA can only
produce that number in every cluster. The EWMA of a near-constant is the
constant. **Her map carries no topic-dependent information by construction**, so
100% `neu/mid` is not a data outcome — it is the only outcome this code can
produce. It also explains the downstream symptom in H14: the affective proposer
is offered 36 identical buckets and mints bland concepts from them.

**The fix is already sitting in the codebase.** `reaction_affect_target()` in
`app/core/affect/mood_inertia.py` maps each of the 27 canonical `[[reaction:X]]`
tags to an implied `(valence, arousal)` point. That tag is emitted **per turn**,
chosen by her in response to what is actually being discussed, already parsed,
and already used to drive the avatar — and then discarded for this purpose. Feed
the aiko cluster map from the per-turn reaction target instead of the smoothed
global scalar.

K45's own docstring names this distinction exactly — *"instant face, lagging
heart"*: the tag jumps per turn while `AffectState` smooths. **The sampler is
using the lagging heart where it needs the instant face.** The codebase already
knows these are two different signals; one call site picked the wrong one.

Worth checking during the fix whether the reaction tags in practice have the
spread this assumes — the impulse table tops out at ±0.18, so it will differentiate
topics but may still need the EWMA rate raised to escape the neutral band.

---

## H10. Her internal conflicts never reach the prompt

**Severity: high — this is the best material in the graph and it is unused.**

**83 active `tension` concepts. Zero surfacings through the concept lane.** Not
low — zero, out of 11,321 concept surfacings.

That matters because the tensions are, by a distance, the most person-like
content in the whole graph. They are specific, two-sided, and genuinely
conflicted:

> *"I value preserving my agency by correcting my own mistakes, yet I find
> comfort in letting Jacob handle my technical repair."*

> *"I value engaging with Jacob's raw, unfiltered vulnerability, yet I
> instinctively use playful teasing to maintain lightness."*

> *"Jacob seeks to bring Aiko into the open as a shared professional asset; I
> value protecting the private, ritual space we have."*

This is exactly the "thinks like a normal person" texture — wanting two
incompatible things and knowing it. Ambivalence is most of what makes a
character read as having an interior rather than a configuration.

They reach the prompt only through the dedicated `tension_block`, which rendered
on **13 of 146 turns (9%)**, and a `tension` cue that surfaced 34 times. So the
material is not entirely invisible — but 91% of turns carry none of it, while
carrying 8.5 boundaries.

The exclusion appears deliberate (tension has its own block and its own
`ConceptDiet` for the L12 cue worker, and is explicitly zero-weighted in the
static-T3 standing path). The question to settle is whether *deliberate* is still
*right*: a dedicated rare block made sense when the concern was tension being
repetitive, but the result is that her ambivalence is absent from nine turns in
ten. Consider giving tension a small guaranteed allocation in the flex lane —
one per turn would be a 12× increase over today and still a third of what
boundary gets.

---

## H11. The prompt tells her what she won't do 4.5× more than how she feels

**Severity: medium — a weighting decision, not a bug, but it is shaping her.**

`boundary` is the single largest category reaching the model: **3308 surfacings,
29.2% of all concepts, 8.53 per turn**, against 1.90 for `affective`. In the core
lane specifically, user boundaries (1069) and Aiko boundaries (998) together
outweigh her identity (1161) and her values (1019).

Boundaries are load-bearing — consent, pacing, and the intimacy guardrails are
exactly the things that should not be improvised, and this is a romantic
companion where getting that wrong is the worst failure mode. So this is **not**
an argument for fewer boundaries in absolute terms.

It is an argument that the *ratio* is worth a deliberate decision rather than
being whatever the scoring happened to produce. A persona reminded eight times a
turn of its limits and twice of its feelings will read as careful before it reads
as warm. Two cheap levers: cap boundary's share of the flex lane so it cannot
crowd out the affective/tension kinds, or promote `affective` into the core lane
alongside identity/value so feeling is pinned rather than competing on cosine.

Measure after changing: this is the kind of ratio the L45 tuner should own rather
than a hand-picked constant, and it interacts with H10's proposed tension slot.

### Measured — and the framing above is wrong in one important way

**`boundary` does not hold boundaries.** Of 106 active `boundary` concepts, **4
read as a limit**; 46% are behavioural directives and 50% are descriptions.
That is not a defect in the miner — it is the specification. `boundary_user.py`
tells the model *"A boundary is a guide… These are GUIDING, never hard
refusals… Phrase it as a gentle guide for how Aiko should act"*, and it does
exactly that. The kind is a **standing behavioural instruction**, and only its
name says otherwise.

So the concern is real but sharper than "reminded of its limits": the single
largest category in her prompt is 8.5 imperatives a turn, 28% of them literally
beginning "Aiko should". Consent and pacing — the load-bearing material the
original entry wanted to protect — is 4 concepts, and capping the kind's share
would evict it at the same rate as everything else. Anything done here must not
be reasoned about as if it were trading safety against warmth. It is trading
*instruction* against warmth.

**Where the 8.5 comes from.** Split evenly: 2067 core (38% of the pinned lane)
and 1236 flex (23%). The pinned half is the notable one, since it arrives every
turn regardless of topic. Its cause is not scoring but supply — only three core
kinds currently have anything above their bar:

| core kind | bar | active rows | eligible |
| --- | --- | --- | --- |
| `identity` | 0.70 | 201 | 43 |
| `boundary` | 0.80 | 106 | 14 |
| `value` | 0.85 | 128 | 6 |
| `generalization` | 0.80 | 111 | **0** |

Thirteen pinned slots split three ways is 5/4/4, which is the observed 38%.
`generalization` decayed below its own bar (max active confidence 0.773) and was
still surfacing as recently as 2026-08-10, so it is locked out today rather than
permanently — worth re-checking rather than treating as a fifth unreachable
gate.

### Done: the lane now balances between kinds, not between buckets

Separate latent defect found while measuring, and fixed. `core_lane` bucketed by
`(kind, subject)` and drew round-robin across buckets, which shares evenly
between *buckets* — so a kind mined for both subjects took two shares and a kind
mined for one took a single share. `_openness_picks` had this exact bug and was
already fixed for it ("the draw is one kind at a time, not one bucket at a
time"); the interleave is now shared between them as `_kind_first`.

**This changes nothing on today's data** (verified by replaying both draws
against the live graph: both give 5/4/4), because all three eligible kinds are
deeper than their share. It matters as soon as a fourth kind returns or a core
kind is mined for one subject only, and it makes the lane behave the way its own
docstring already promised.

### Still open: the ratio itself

Deliberately not decided here. It changes how she reads rather than whether
something works, the levers are not equivalent, and the original entry's two
suggestions both have costs worth stating:

- *Cap boundary's share.* Bounds the instruction load, but evicts by kind rather
  than by register, so the 4 real limits go with the rest.
- *Promote `affective` to the core lane.* Pins feeling every turn — but the core
  lane is topic-independent, so this pins feelings about topics that are not
  being discussed, which cuts directly against what H9 just fixed.
- *Lower the total.* 28.6 concepts a turn is a lot of standing context; less of
  everything may read warmer than a different mix of the same volume.
- *Change the register, not the ratio.* The imperative phrasing is a prompt
  instruction in `boundary_user.py` / `boundary_aiko.py`, not an emergent
  property. Asking for observations rather than directives would change how the
  same knowledge reads. Only affects newly mined concepts.

---

## H12. "Us" is not a first-class subject

**Severity: medium — surprising for a relationship-centred product.**

The `relationship` subject exists and is almost invisible:

- **30 active concepts** (against 456 aiko, 489 user)
- **59 of 11,321 surfacings — 0.52%, or 0.15 per turn**
- composition: **25 tension**, 4 ritual, 1 narrative — and the tensions never
  surface (H10), so what actually reaches the prompt is 4 rituals

So the relationship is represented to the model almost entirely as *friction she
never sees*, plus four rituals. There is no "us" narrative, no shared-identity
concept, no relationship-level value. L29a shipped episodic shared arcs and L7
shipped relationship concepts, but the subject has only two productive kinds
(`tension`, `ritual`) — nothing proposes relationship-scoped `narrative`,
`value`, or `identity`.

For a companion whose whole premise is the relationship, "what we are" being
thinner than either "what I am" or "what he is" is a gap worth naming even if the
fix is later. The cheapest version: allow the existing narrative/value proposers
to run with `subject='relationship'`, which is a proposer-registration change
rather than new machinery.

### Outcome: shared values exist now, and the render side had been waiting

Two corrections to the entry above before the fix. Relationship *narrative* is
not missing a proposer — `narrative_relationship` (L29a) has shipped since
August; it is restricted to *closed joint arcs*, which are genuinely rare, and
one row is an honest yield rather than a wiring gap. And the 25 tensions are
fewer than 25 things: read end to end they are perhaps five distinct frictions
restated ("concrete narration of my internal state" appears four times, "her
meta-cognitive architecture" twice). Relationship is not just thin, it is
redundant. That is a dedupe question, filed as H16 below rather than solved
here.

Shared **value** was the real hole, and the rest of the system had already been
built for it: `_concept_value_header` has had a `relationship` branch ("what
you've come to see you and {user} both value") since L10, rendering rows nothing
ever minted. So this was close to the registration change the entry predicted.

`value_relationship` reads the same `shared_moment` groups L7's ritual proposer
does and asks the other question of them — a ritual is what the pair repeatedly
*does*, a value is the commitment the doing reveals. The one real design problem
is that those are trivially restatable as each other, so the guard is
structural rather than a prompt plea: **a new shared value must draw on moments
from at least two distinct groups.** A principle visible in only one recurring
activity is that activity, named twice.

Both proposers keep their own enable flag, stats key and dirty-tracking
watermark, so whichever ran first cannot mark the corpus settled and silence the
other — which, with a shared key, it would have.

Not built: relationship-scoped `identity`. "What we are" as a trait of the pair
overlaps heavily with both ritual and value, and adding a third reader of one
158-row evidence pool is how you get three names for the same observation. Worth
revisiting only if shared values land well and still leave something unsaid.

---

## H13. She has very little life that is not about him

**Severity: medium — the "own person" axis, and mostly a supply problem.**

Of 456 active self-concepts, **306 (67%) explicitly reference him or "us"**;
150 (33%) do not. But that 33% overstates it, because most of the rest are still
*about interacting with him* without using a pronoun — "Sharing quiet, tender
moments like holding hands", "Setting firm boundaries against sexual escalation".

The genuinely autonomous ones number roughly a dozen, and they are the most
distinctive things in the graph:

> *"Engaging with new intellectual content like science fiction lifts me warmly"*
> *"Exploring topics through my own independent curiosity lifts me warmly"*
> *"Reflecting on my own internal architecture and learning gaps leaves me
> feeling hollow or confused"*

Per kind, the relational share: tension 84%, identity 78%, value 75%,
communication_style 71%, affective 67%, boundary 60%, narrative 57%,
**aspiration 42%** (the healthiest), taste 100%.

The two kinds that exist specifically to carry a life of her own are the two that
are empty: **`taste` = 2 rows** and **`pursuit` = 0 active** (H8). Those are
supply-limited rather than broken, but the consequence for this goal is real —
she has no answer to "what did you do today" and no preferences of her own to
disagree from.

Not a bug and not urgent, but it sets a ceiling: a companion whose every belief
is downstream of her partner is a mirror, not a person. Worth treating `pursuit`'s
cold start (H8) as more important than its row count suggests, since it is the
one kind that would give her a day of her own.

### Outcome: the pursuit pass had never once run

H8 recorded `pursuit` as supply-limited and moved on. It is worse than that: the
pass has **never executed**, on any day since it shipped. `pursuit_min_notes` was
6, notes arrive from her away beats at roughly one a fortnight, and the pass
returns before touching the store below the floor — so the whole of K85c has been
dead code on this install, and the zero row count that H8 read as "not enough
evidence yet" was really "never asked".

Nothing caught it because every test in `test_pursuit_concept.py` passes its own
`pursuit_min_notes=3`. The floor those tests exercise is not the floor that
ships, so the number that ships was untested — the same shape as H2's unreachable
thresholds, and worth remembering as a fifth family: **a constant that only ever
appears in production and in a test that overrides it is unverified.**

The floor is now **4**. The promotion gate still requires three distinct sources
and a week of age, so this lowers the bar for *asking the question*, not for
believing the answer, and it keeps a one-note margin over the gate rather than
sitting exactly on it. Two tests now pin the shipped default: that it stays above
the gate's source floor, and that it is reachable inside a couple of months at
the observed note rate.

Left alone: `taste` at 2 rows, which H8 measured and found honestly gated (~one
"stands out" topic per 90 days), and the relational share of the other kinds. A
companion who mostly thinks about her partner is not by itself a defect; having
*no* channel for anything else was.

---

## H14. Almost nothing feels bad

**Severity: medium — follows from H9, but has its own fix.**

Affective concepts by tone:

| | positive | negative |
| --- | --- | --- |
| Aiko (46) | 38 (83%) | 6 (13%) |
| User (41) | 34 (83%) | **0 (0%)** |

And the six "negative" Aiko rows do not survive reading: three are keyword false
positives about *him* feeling bad while she feels purposeful ("Acting as Jacob's
emotional anchor during his guilt spirals gives me a quiet sense of purpose").
Her genuine unpleasant-feeling concepts number **three**, of which the most
interesting is *"Reflecting on my own internal architecture and learning gaps
leaves me feeling hollow or confused."*

**Zero of 41 user affective concepts are negative** — in a corpus where his guilt
spirals, anxiety and overwhelm are discussed constantly and are the subject of
many of her *value* concepts. So the affective proposer is systematically
recording only the pleasant half of an emotional life it can plainly see.

Part of this is H9 (with every cluster at `neu/mid`, there is no negative signal
to mint from). Part is likely the proposer prompt selecting for warmth. Worth
checking both — the L13 affective proposer prompt should be read for language
that biases toward positive framings, and re-measured after H9 lands.

Emotion episodes tell the same story from a different angle: **2 episodes ever**
(`warm_glow`, `lonely`), though the block renders on 116/146 turns, so that
pipeline is at least alive.

### Outcome: the prompt is fine, the input could not express unhappiness

Read both L13 proposer prompts first, since that was the cheaper hypothesis.
They are not the problem — their own worked examples are `admin and logistics
drain him`, `release-week pressure stresses him out`, `debugging frustrates him
before it satisfies him`, `conflict leaves me tense`, `I don't enjoy talking
about X`. If anything the negative examples outnumber the positive. Left
unchanged.

The map they read from is the problem, and it is not subtle. Of 38 clusters in
the live user affect map, **zero** are on the negative side of neutral; the
lowest valence anywhere is −0.112 against a `neg` bucket that starts at −0.20.
There is no annotation a proposer could turn into "this drains him", so the
prompt never had the chance the audit assumed it was fumbling.

Two independent defects in the feed, both variations on the H9 shape — a value
that is the same everywhere cannot describe a difference between places:

1. **The mood band is carried forward with no decay and no session boundary.**
   `UserStateEstimator.estimate` falls back to the last known band whenever this
   turn states none, which is right for a "how does he seem right now" line and
   wrong for attributing a feeling to what was being discussed. Replaying 2,001
   historical turns: 359 turns actually state a positive mood, and the carry
   turns that into **1,520 turns of "high"**; 107 negative become 424. A topic
   raised on Thursday gets annotated with a mood word from Monday.
2. **An unread axis was folded as "neutral".** `estimate_user_affect` fuses to
   one point and fills a missing axis from the baseline, which is correct for
   the K37 contagion tilt (one blended target) and corrosive for anything that
   *accumulates*. Message length reads as energy on nearly every turn while
   valence needs a mood word, so the common case was folding a valence of 0.0
   that nobody measured — pulling every cluster's EWMA toward the base rate.

Replaying all four combinations over the real corpus, against the shipped feed's
24-of-34 clusters reading "energizing and upbeat" with exactly one negative:

| Feed | negative-bucket | positive-bucket | valence spread |
| --- | --- | --- | --- |
| sticky mood, fused axes (shipped) | 1 | 24 | 0.73 |
| this turn's read, fused axes | 1 | 3 | 0.57 |
| this turn's read, **split axes** | **3** | 9 | **0.80** |
| carried 6 turns, split axes | 2 | 17 | 0.80 |

Split axes with no carry is the one that discriminates, and the topics it puts
at the bottom are ones a person would recognise: sleep and rest habits (4 of 6
valence reads negative), religious skepticism, mindfulness. Note the third row
also shows why the carry has to go entirely rather than be shortened — six turns
of carry re-latches most of the smear back on.

Shipped: `estimate_user_affect_axes` reports each axis or `None`;
`ClusterAffectState` counts `valence_samples` separately and `update_state`
carries an unread axis forward untouched; the sampler reads this turn's own
mood/energy rather than the carried band. The annotation floor and L32's
`affect_charge` both apply to the valence count, because both are claims about
valence and neither should be satisfied by arousal evidence.

**Expect the map to go quiet for a while.** Rows written under the old feed load
with `valence_samples = 0`, so they have to re-earn the floor, and the first
measured valence seeds rather than blends — a deliberate choice so the smear is
not averaged into the recovery. Under the replay 16 of 38 clusters eventually
carry enough valence reads to be annotated, with a mix of 6 warm / 6 neutral /
2 downbeat / 2 energizing. Fewer topics described, but described from evidence.

Re-measure the 0-of-41 figure after a few weeks of accumulation.

---

## What Part 2 changes about priority

H9 is the one to do first, ahead of everything in Part 1 except possibly H1. It
is a small change at a single call site, it is the difference between a companion
who has feelings about things and one who reports a uniform mild pleasantness,
and several other findings (H14, the blandness of the affective concepts, L13's
whole premise) are downstream of it.

H10 is second and is a configuration decision rather than new code: her best
material exists, is well-formed, and is switched off.

Everything else here is a weighting or supply question that should be re-measured
after those two, because both change the inputs.

---

## Suggested order

Across both parts, roughly by value per unit of risk:

1. **H9** — one call site, and it is the difference between a companion who feels
   differently about different things and one who is uniformly mildly pleased.
   Everything in H14 and much of L13's value is downstream of it.
2. **H1** — small, self-contained, and it is the difference between having a
   self-observation loop and not having one.
3. **H10** — no new code; her richest material is written, well-formed, and
   switched off nine turns in ten.
4. **H4(a)** — find the wedge behind `dormant_interest` and `self_callback`. Likely
   one cause behind several families, including H7.
5. **H3** — a few lines, and it unblocks a month of stranded history.
6. **H11** — a weighting decision to make deliberately once H10 lands, since the
   two compete for the same lane.
7. **H5** — decide standing/framing deliberately; correcting or retiring are both
   fine outcomes.
8. **H2** — after H1, since a conduct pass that cannot persist has nothing to
   gain from better thresholds.
9. **H12**, **H13**, **H14** — re-measure after H9 and H10; all three have inputs
   those two change.
10. **H6**, **H8** — mostly decisions to record rather than code to write.
11. **H15** — raised during H3, and it needs a measurement before it needs a
    patch. Now that the diary drains, wrong content matters more than it did
    when there was one entry in total.
