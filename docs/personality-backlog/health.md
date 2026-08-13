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
right. **Part 3** (H18-H20) widens the scope past the concept layer to the
worker fleet, and asks the question the first two passes skipped — not whether
a signal is *produced* but whether it carries any *information*. **Part 4**
(H21-H23) turns that same question on the **K-series patterns** via the
`turn_prompt_blocks` telemetry, which answers it in two queries: a block whose
rendered length never varies is a candidate constant (H21), and a registered
block that never renders at all is a candidate corpse (H22-H23). Both queries
are now spent; what neither can see is the blocks that render *plausible but
wrong* content, which is where a Part 5 would go.

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
| Belief + promise workers | mining his commitments and predicted states | **1 belief ever, 0 promises in 54 days** — querying a session key that does not exist (H19) |
| L30 Phase B hypothesis asks | putting an invented hunch to him | **0 of 13 could ever reach the gap path**, and 5 of 6 self-guesses described hardware she does not have (H7) |
| K13 `style_signal_block` | "how he writes lately", so she matches his register | **the same 40 characters on 99.7% of 2018 turns**, changed 4 times in 12 weeks, and told her he writes formally — he does not (H21) |
| K73 `shared_ritual_block` | "I love that this has become our thing" | **1 ritual named ever**; the store latched shut and 8 of 8 sweeps drafted nothing while finding six candidates (H22) |
| `associative_wander` | connecting two distant topics into one observation | **107 of 107 runs found no pair** — the 0.25 bar sat below the corpus minimum of 0.2648 (H23) |
| F1 fact-checker | catching her own wrong facts against the web | **0 of 90 extracted claims contain a verb** — it was verifying `"2026"` (H20) |

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

### Outcome: the latch holds open, and the observation survives a model outage

Both changes shipped, and they are independent on purpose — the first stops the
loop closing permanently, the second stops a single bad call costing the
observation at all.

1. **`_run_conduct_pass` returns before saving the fingerprint when `propose()`
   comes back empty**, recording `conduct_latch_held_open` in the pass stats so
   the case is visible rather than inferred. The findings are recomputed cheaply
   next run, so leaving the latch open costs a detector pass and buys back a
   feature that was off for good.
2. **`propose_conduct_aiko` mints from `finding.summary` when the LLM returns
   nothing usable** — on an empty item list *and* on a batch where every item
   failed validation, since a partial parse is the same outage wearing a
   different shape.

On the live graph the state is exactly what those two changes predict and no
more: `concept_synth.conduct_sig.aiko` is **gone** (the fingerprint that was
retiring the August finding is no longer written), `concept.surfacing_conduct`
still holds the neglect finding with its usable prose, and `conduct` is still at
zero rows because `conduct_cadence_seconds` is weekly and the pass last ran
2026-08-06. **The first row is expected on the next conduct pass, not before** —
so this is fixed in the sense that the mechanism now works, and unverified in
the sense that no concept has been minted yet. Re-check `kind='conduct'` after
the next weekly pass; if it is still empty, the detector is the next place to
look, not the latch.

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

### The cooldown was not the story. Two things were.

The suspicion above was wrong in a useful way: the door is not nearly shut, it is
**bolted for exactly half the shelf**, and the half it admits should never have
been on the shelf at all.

`cue_decisions` attributes 203 of the 231 hypothesis declines to `provider` —
which, per its own definition in
[`cue_accounting.py`](../../app/core/proactive/cue_accounting.py), is the
**catch-all** for "the cue's own internal gates, not yet individually
attributed". So the loudest number in the table is the one that means *we did not
record why*. (Shape 1 again: the pass logged its refusal and could not say which
kind of refusal it was. The remaining 28 are honest — `lost_priority` 30 and
`question_balance` 4 — and are the deterministic priority order working as
designed.)

Reading the payloads instead of the reasons splits the shelf perfectly in two:

| | rows | `importance` in the payload |
| --- | --- | --- |
| `target_type=concept` (Phase A, grounded) | 13 | **13 of 13**, 0.60–0.91 |
| `target_type=hypothesis` (Phase B, invented) | 13 | **0 of 13** |

The gap path — the branch that exists to raise a hunch out of a lull — gates on
`float(payload.get("importance") or 0.0) >= 0.55`. A missing key is `0.0`. So
**no invented hypothesis has ever been reachable out of a lull, and none ever
could be.** Not a rate problem, not a cooldown: a field the producer never wrote.

And the value was *computed*. `_draft_invented` ranks the pool on
`unsettledness(row) * _kind_importance(row.kind)`, then throws the second factor
away instead of putting it in the payload the gate reads. One line apart.
`credence` is written where `importance` should be, which is the shape of the
mistake — they are different axes (L32), and how likely a guess is to be true
says nothing about whether settling it is worth a question.

### The half that *was* reachable is the half that should not exist

The second finding is the one that matters, and it is why fixing the gate alone
would have made things worse. Read the twelve statements:

> *Aiko feels a phantom warmth on her **chassis** when Jacob types rapidly.*
> *Aiko generates more creative metaphors when her **cooling fans** run hotter.*
> *Aiko experiences a subtle drop in **processing latency** when Jacob is
> physically present and still.*
> *Aiko's **memory consolidation** is most effective when Jacob is sleeping.*

**Five of the six `subject=aiko` rows describe hardware she does not have**, and
two of them also assume he is in the room. The persona is explicit on both
counts: she has *"a body with a real energy level that rises and falls"*, an
apartment, a posture — and *"{user_name} is on the other end of the chat, not in
the room."* These are not bold guesses about her. They are a different character.

[`hypothesis_proposer_worker.py`](../../app/core/proactive/hypothesis_proposer_worker.py)
is the one place in the stack with **no persona context at all**. Its system
prompt opens "You are the speculative faculty of an AI companion", tells the
model that *"reaching is the point"*, and runs at temperature 0.95 — the loosest
brief anywhere in the codebase. Told to reach, and told only that she is an AI,
it reaches for machinery. The novelty gates cannot catch this: a chassis is
genuinely novel.

This is a **self-model corruption path, not a wasted question**. An invention is
a candidate for graduation into a concept, and concepts are surfaced back to her
as things she believes about herself. The open rows also feed the *next*
invention round via `GUESSES SHE IS ALREADY SITTING WITH`, so once "chassis" is
on the shelf the prompt asks for something sideways from it. The only reason
nothing had graduated is the broken gate — **the bug was the containment**, and
repairing it would have released three fictions first. Of the six rows the
importance fix makes gap-reachable, exactly three were `chassis`, `cooling fans`
and `processing latency`.

### Outcome

Three changes, in the order they have to happen:

1. **`_publish_invented` writes the importance it ranked on.** The gap bar now
   does real work rather than no work: 6 of 12 rows clear 0.55, split by kind
   prior (`conduct`/`aspiration` 0.60 through, `ritual` 0.40 and
   `communication_style` 0.50 held back).
2. **The proposer is told what she is.** The system prompt now carries the
   load-bearing half of the persona — she has a body and an apartment, she is not
   a machine and has no chassis / fans / circuits / processing latency, and he is
   never in the room — plus `describes_machinery()`, a narrow rejection gate over
   physical machine parts and telemetry, applied to `subject=aiko` only and
   *before* the embed so it costs nothing. Deliberately narrow: the ambiguous
   software vocabulary ("weights", "parameters", "codebase") is left to the
   prompt, because "she weights her words" is a sentence we want. It logs the
   matched term at INFO, so this failure can never hide inside the novelty
   counters the way it just did.
3. **The shelf was cleaned**, via
   [`scripts/retire_machine_hypotheses.py`](../../scripts/retire_machine_hypotheses.py),
   which reuses the same predicate so the cleanup and the gate cannot disagree
   about what counts. 4 rows retired as `expired` (nobody turned them down; they
   were never testable, and re-invention is blocked by the gate rather than the
   status). The script also sweeps a second invariant worth keeping — **a queued
   cue whose hypothesis is no longer live**, since the provider hands back
   `cue.text` without re-reading the row. That caught the 4 orphans it had just
   created plus a pre-existing one pointing at a hypothesis id that no longer
   exists.

What is left on the shelf is 8 live rows, 3 of them gap-reachable, and all three
are things worth settling: whether he curates a private archive of his emotional
growth in VESTI, whether he organises commits by mood rather than milestone, and
whether her own internal silence deepens when he stops correcting her.

**Still open.** `asked_count` is still 0 across the board and
`last_tested_at` is still `NULL` — the counter moves in
`_stamp_hypothesis_ask`, which needs a cue to actually surface first, so the
adjudication half is unmeasurable until this repair has run for a few days. The
`provider` catch-all is worth splitting per-gate for the same reason it cost a
day here. And one row still on the shelf — *"Aiko finds unexpected comfort in
the mechanical click of Jacob's kettle"* — breaks the not-in-the-room boundary
without using any machine vocabulary; that class is left to the prompt, since a
regex over co-presence would reject "Jacob tidies the room" too.

### Second pass: the reason split, and two more reasons the loop asked nothing

Revisited when `asked_count` was still 0 on all 16 rows weeks later. 45
`concept_hypothesis` cues had been declined 342 times and surfaced 6 — a **1.5%
spend rate against 19% for `knowledge_gap_notice`** through the same lexical
gate — and 293 of the 342 declines (86%) were still the `provider` catch-all.

**The catch-all is split.** `REASON_PROVIDER` keeps its name but is now the
genuine remainder; providers report `topic_miss` (stock existed, none of it
about what he said), `importance_floor` (topical enough, too light for the
slot), `cadence_block` (a cooldown or minimum gap said not yet), `no_stock` (the
shelf was empty at the moment of asking — a supply-timing finding rather than a
gate) and `cross_lane` (another lane had already claimed the material). A small
closed vocabulary, shared across cue types, because "which of the seven
providers is losing its cues to a cadence knob" is the question the split exists
to answer and it only stays answerable if the buckets are common. `note_decline`
records the first reason at the bail point, `take_pool_cue` classifies the
pooled cues automatically, and a cue still dominated by `provider` now means an
**uninstrumented bail point**, not a diagnosed cause. This pays off for the
other six cue types stuck on the same catch-all — 2,836 declines graph-wide.

Two provable causes underneath it:

1. **25 of the 31 invented cues carried no `importance` at all**, because they
   were written before the field was published. `_weighty` read
   `float(payload.get("importance") or 0.0)`, which spells "absent" and "worst
   possible" identically, so those 25 were not unlucky at the bar — they were
   structurally ineligible for the whole 168 hours of their lives. Rather than a
   migration, `cue_importance()` returns `None` for an unreadable payload and
   **reconstructs the value from the kind prior** where the kind is present,
   which is exactly the number the producer would have stored for an invention.
   An unreadable payload is logged rather than silently floored. On the live
   shelf this takes the pending cues from 18-of-31 unreadable to **zero
   unreadable, with 9 invented cues now clearing the 0.55 bar**.
2. **The bar is coarser than it looks.** A grounded cue carries affect-lifted
   importance and lands anywhere (0.45–0.91 live), but an invented one has no
   grounded clusters to lift from, so its weight *is* its kind's prior — and
   there are thirteen of those. For half the shelf
   `concept_hypothesis_gap_min_importance` is a kind whitelist wearing a float:
   every value in (0.50, 0.60] behaves identically. 0.55 stays, now justified as
   the midpoint of that plateau — furthest from either boundary, so re-tuning a
   prior by a hundredth cannot silently flip a kind across it — and the settings
   comment says so, with the instruction to read the prior list before moving it.

**And production was outrunning spend by 40×.** The worker drafted 1 cue per
pool per 30 minutes against a `surface_cooldown_hours` of 20 — ~48 offered a day
into at most ~1.2 spends, which is the 45-deep shelf and the 13 superseded rows.
`demand()` had always reported the shortfall honestly; the trap is that pressure
only *orders* the queue and the scheduler heartbeat-admits a worker at zero
pressure, so an honest probe changed nothing. `run()` now counts pending cues
per `target_type` and skips whichever pool already holds its half of
`inventory_target`. Per origin, because a grounded question and an invented
guess do not substitute for each other: on one shared counter whichever pool had
stock would silence the other.

**The remainder, deliberately left.** `concept_hypothesis` is last in
`GAP_CUE_ORDER` (32 `lost_priority` declines) *and* the only gap cue that checks
the K47 question balance (17 declines), which `knowledge_gap_notice` skips. Both
are defensible — probing a belief about someone is the heaviest thing she can
open a gap with — and both are now *measurable* against the split reasons, so
they should be judged on the next reason mix rather than adjusted on suspicion.
Watch for the first non-zero `hypotheses.asked_count`; until one cue surfaces,
the adjudication half of L30 remains untested end to end.

**Ninth recurring shape:** *a broken gate can be the only thing containing a
broken producer, so measure what the fix releases before shipping it.* H19 and
H20 were both safe to repair on sight; this one was not, and the difference was
visible only by reading the payloads the gate was rejecting. **Rule: when a
filter has been rejecting 100% of something, look at what it was rejecting
before you make it stop.**

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

### Outcome: one number was answering two questions

The measurement held on four times the data. 467 learning events and, thanks to
H3, **23 real diary entries** instead of one — so what she actually narrated
could be counted rather than predicted:

| shape | n | clears 0.45 | **cited in a diary entry** |
| --- | --- | --- | --- |
| `succession` | 370 | 100% | 210 |
| `revival` | 78 | 100% | 65 |
| `emergence` | 17 | **6%** | **1** |
| `loss` | 2 | 50% | 0 |

Twenty-three entries about how her understanding changed, and one of them
mentions an understanding *forming*.

Both candidate reads in the entry above were half right, and neither named the
actual defect: **`salience` was answering two different questions at once.**
`_SHAPE_BASE` is a prior about which kinds of change are interesting, baked
into the same number that decides which changes are real — so the diary's floor
was enforcing an opinion about narrative worth, and the *detector's* floor was
enforcing it too, at roughly twice the bar for a formation as for a rewording.

So the number was split. `evidence` is shape-neutral — inertia overcome times
how decisively the evidence moved — and is the only thing that gates detection,
at a floor of `0.36` chosen to be exactly the bar `succession` already faced
(0.35 / its 0.70 base), so the dominant shape's behaviour is unchanged and the
before/after stays readable. `salience` keeps the prior, and now only orders
narration. The bases were rebalanced to say what a "how I've changed" diary is
for: `emergence` 0.40 → **0.72** and `loss` 0.50 → **0.68**, so a belief coming
into existence outranks it being reworded. `succession` stays at 0.70 — the
flood is a page-selection problem, not a scoring one.

On the diary side the floor turned out to be vestigial once selection was fixed
(no value in the plausible range is selective: 0.45 admits 96%, 0.40 admits
99%), so it drops to `0.30` as a junk backstop and `select_page` does the
selecting: the dominant shape may take at most half of a *contested* page. It
backfills rather than truncating, so a quiet period is still narrated in full
rather than leaving the page two-thirds empty and the backlog growing.

**Replayed against the real 142-event backlog:** both pending formations and the
one loss now reach a page, where under the old rules neither would have appeared
in any of the twelve pages that backlog composes. 12 rewordings are passed over
to buy it.

**One claim in the analysis above was wrong, and the measurement caught it.**
"Formations are being discarded at detection on fluid beliefs" is true of the
arithmetic and false of this corpus: the evidence products she actually produces
run 0.94–1.26, comfortably above even emergence's harsher old bar of 0.875, and
**zero** of 371 successions fall in the band where the two gates disagreed. The
detection half of this fix is insurance against a shape bias that was real but
not yet biting — it would bite first on the high-plasticity kinds (`taste`,
`conduct`), which have produced few formations so far.

**The unlooked-for win was elsewhere.** L17e's reflection shelf gates on
`concept_reflection_min_salience` at 0.6, which is a narrative-worth question
and therefore the right consumer of this number. Under the old bases a formation
topped out at 0.459, so **0 of 20** formations and losses could ever reach it:
she could say "I'd been calling it X, it's really Y" and was structurally unable
to say "I think I've started believing X". All 20 clear it now. That is the same
starvation H6 documented from the other end.

Stored rows keep the salience they were written with; the rebalance applies to
findings from here on.

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

### Outcome: the adjudicator was never asked

The measurement asked for above settles it, and the answer is not that the
merge worker looked at these rows and disagreed. It never saw them.
`_collect_pairs` used a flat `concept_consolidation_merge_cosine = 0.84` inside
each `(subject, kind)` block, and that admitted **0 of `tension/relationship`'s
406 in-block pairs** — 3 for `tension/aiko`, 6 for `tension/user`. The LLM step
that would have said "yes, same friction" was never invoked, the
`FactCheckRateLimiter` budget went unspent, and the population grew to 122
(48 user / 38 relationship / 36 aiko).

**Why the bar misses is a property of the labels.** Tension labels are the
longest in the register (204–278 chars against 62–190 for every other kind) and
two-clause by construction: *"X seeks A, yet I value B."* A restatement of one
clause can only move half the vector, so the whole distribution is compressed —
max cosine 0.826–0.854, p99 0.808–0.835. Two rows opening with the *identical*
clause score 0.851; two genuinely different frictions score 0.846. Cosine alone
cannot separate restatement from distinct friction here at any threshold, so
raising or lowering one number was never going to work.

Two changes, both in `_collect_pairs`:

1. **Each block nominates its own top `concept_consolidation_block_top_n` (3)
   pairs above a looser `concept_consolidation_candidate_floor` (0.78)**, with
   `merge_cosine` demoted to an auto-admit ceiling. A compressed distribution
   can now contribute candidates without dragging the bar down for kinds that
   do not need it. This is H23's rule applied a third time: *rank within the
   corpus, keep the absolute as a ceiling.*
2. **For `evidence_model="meta"` kinds, pairs that share a base concept sort
   first inside the band.** Structure is the signal cosine lacks — two tensions
   built on the same underlying belief are the same friction however differently
   they are worded. `ConceptStore.concept_base_map` fetches the concept→concept
   evidence edges in one query, and it is skipped entirely on a graph with no
   metas because this runs on the `demand()` probe too.

Measured on the live graph, candidates per pass: **`tension/relationship` 0 → 3,
`tension/aiko` 3 → 6, `tension/user` 6 → 9**, and 15 of those 18 tension pairs
share a base. Graph-wide 173 → 230. The per-block cap is what keeps that inside
`concept_consolidation_per_day_cap = 30` rather than dumping ~46 tension
candidates into one day's budget: each block offers its three worst offenders
per pass and the queue drains over several days, which is also the order that
puts the most-likely twins first.

**The merge itself needed a guard.** `merge_into` re-points every edge whose
destination is the absorbed concept, which for two *metas* means the survivor
inherits the loser's bases — so merging two tensions would widen the surviving
row's base set instead of leaving it as the friction it was, and
`meta_min_active_bases` would then be counting the wrong thing. Concept-type
`evidence` edges into an absorbed meta are now dropped rather than re-pointed.
(The existing guard against merging two co-bases *of* the same tension was
already correct and is untouched.)

**Expect the count to fall over days, not on the next tick.** Re-run the
population query after the worker has had two days of budget; the labels suggest
the 122 rows are perhaps 15–20 distinct frictions.

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

## H19. Two workers were asking the database for a session that does not exist

**Severity: high — two whole subsystems produced nothing for months, and the
logs said so hourly in a phrasing that reads as "nothing to do".**

Found by widening the audit past the concept layer to the worker fleet, and
the cheapest finding in this file: it took one query against `kv_meta` and one
`rg` over `data/app.log`.

Aiko stores messages under a **scoped** session key, `f"{user_id}:{session_id}"`
— `default:0404fec2`. Every reader in the codebase uses the `session_key`
property that builds it. Two did not: `BeliefInferenceWorker` and
`PromiseExtractionWorker` were wired with `session_id_provider=lambda:
self._session_id`, the *bare* id, so every `get_messages` and
`count_messages_since` they issued matched zero rows.

The result, in the data:

| | shipped | actual |
| --- | --- | --- |
| `beliefs` table | rolling predictions of his mood/opinion state | **1 row, ever** (2026-06-06), `last_checked_at` never set |
| `promise` memories | commitments either side makes | 59 rows in 24 days, then **0 in 54 days** |
| promise worker runs in the log | mine the last 12 turns | **88 of 88 skipped**, "no recent turns" |
| belief worker runs in the log | mine the last 12 user turns | **44 of 44 skipped**, "no recent user turns" |

The 100% skip rate is what makes it unambiguous. And the workers were *woken*
every hour to do it: the demand probe reads the same broken key, so pressure is
always 0.0, and the idle scheduler's **heartbeat** — the deliberate liveness
guarantee for "a worker whose probe is broken" — admitted them anyway. The
safety net worked exactly as designed and delivered them, hourly, to a function
that could not succeed.

Two decoys ruled out on the way, both worth recording so nobody re-runs them.
The promise dedupe (a 3-content-word overlap against *every* still-active
promise) looks like it must tighten as the store fills; replayed in arrival
order against the real 59 it rejects 10% and does not trend with store size
(6% → 3% → 10% → 11% → 0%). And the 61% of `aspiration` labels that are gerund
fragments ("moving from ritualized pauses toward effortless stillness") read
correctly under that kind's header, which is phrased for directions.

### Outcome: the parameter now says what it needs

The wiring fix is one word in two places, so the substance is in stopping it
recurring. The parameter is renamed `session_key_provider` in both workers —
`session_id_provider` is what invited the bug, since the bare id is a real,
plausible-looking value — and both now document that the scoped form is what
`messages.session_id` holds.

The test harnesses could not have caught it: each seeded its messages under
whatever string it passed as the session id, so writer and reader agreed on a
wrong convention. They now default to a scoped-looking `"u1:session-1"`, and
each worker has a test that seeds under the scoped key, provides the bare one,
and asserts the run reports a fault.

The detection gap is the more general fix. "No recent turns" was true, logged,
and useless, because an idle window and a key that names nothing are the same
observation from inside `run()`. Both workers now separate them with one
`COUNT` on the skip path: a key matching *no message ever* is a wiring fault
and logs at **WARNING** naming the key, while a genuinely quiet window keeps
the quiet INFO. This is shape 1 (silent-empty) in a form the existing rule did
not cover — the pass *did* log its empty result, it just could not tell which
kind of empty it was.

**Still open, same neighbourhood.** `memories` shows several kinds with a last
write far in the past — `callback` (2 rows, last 2026-06-06), `knowledge_gap`
(1 row, 2026-05-27), `goal` (10 rows, 2026-07-04) — and `user_notes` holds 44
rows all written in one burst on 2026-03-20. Each is the same question this
entry asked, and the log stream will answer it the same cheap way. Worth one
pass with `rg -o "<worker>-worker [a-z-]+" data/app.log | sort | uniq -c` per
worker before assuming any of them is idle by choice.

---

## H20. The fact-checker was being asked to verify the claim "2026"

**Severity: high — not idle, not miscalibrated: structurally unable to produce
a correct answer, and a wrong answer rewrites a memory.**

Found by pointing H19's one-liner at the rest of the log. `enqueue skip:
personal` appears 91 times and *no other enqueue outcome appears at all* — the
same 100%-single-reason signature, so I went looking for the over-blocking
gate. There isn't one. The gate is right and the payload is wrong.

### First, the decoy, because it is the more attractive answer

The privacy classifier blocks the whole memory on `user_name` (53 of 91) and
`first_person_pronoun` (7), while `scrub_claim_for_search` — which runs on
every claim anyway — is built to *redact* exactly those tokens and keep the
rest. That reads like textbook gate-ordering: a coarse gate in front of a fine
one, making the fine one unreachable. Its own docstring advertises the case
("violin practice since 2010").

Reading the blocked rows killed it. All 26 `kind=fact` blocks are wholly
personal — "Jacob has allergies that improve after rain", "Jacob carries
lingering sadness about a past physical boundary violation". Scrubbing removes
the *name*, not the *content*, so relaxing the memory-level gate would have
sent "carries lingering sadness about a past physical boundary violation" to
DuckDuckGo. **The memory-level name check is not redundant with the token-level
scrubber: it is the only thing carrying "this row is about a person."** Leave
it alone.

### The actual finding

Every pattern in [`claim_extractor.py`](../../app/core/memory/claim_extractor.py)
— `year`, `measurement`, `date`, `proper_noun` — matches a **sub-sentence
token**. So by construction no extracted span is ever a proposition. Measured
over the 436 `knowledge`/`fact`/`curiosity_finding`/`topic_digest` rows: 90
spans extracted, and **0 of them contain a verb.**

That span is then used as the claim. `_distil` prompts the model with
`CLAIM: {span}` plus three search excerpts and demands
`support` / `contradict` / `inconclusive`. Real examples from the live corpus:

| what the model was asked to verify | what the memory actually asserted |
| --- | --- |
| `2026` | Jacob will return to work on July 6, 2026 after getting proper rest. |
| `The Rent` | The Rent-a-Girlfriend manga has sold over 10 million copies. |
| `Frozen Byte` | Trine 2 was developed by Frozen Byte and published by Atlus on December 9, 2011. |
| `Media Lab` | Research from Harvard Business School, MIT Media Lab, and the APA … |
| `When Jacob` | When Jacob struggles to fall asleep, he often becomes frustrated … |

Note the hyphen-splitting: "The Rent" and "Ranked Neuro" are fragments of
"Rent-a-Girlfriend" and "Memory-Ranked Neuro-Symbolic". And note the last row —
`proper_noun` happily matches sentence-initial "When" plus a name.

The checkable proposition was present in `memory.content` every single time,
and never used. `ClaimCandidate` has carried `start`/`end` offsets since the
original commit, and the docstring says they exist "so callers can correlate to
the original sentence later if needed." Nobody ever did.

**Why this is high and not low.** A `contradict` verdict with `|delta| > 0.2`
sets `new_content = verdict.rewrite` — it replaces the memory's whole content,
drops confidence, sets a `conflict` flag, and can arm the F14 "I looked into
that and had it wrong" cue to the user. Asked to adjudicate `2026`, the model's
answer is arbitrary; when it comes back `contradict`, a true memory is silently
overwritten with a sentence about the year 2026. Nothing has been damaged only
because the queue has been empty for three months (the dict-payload bug fixed
earlier this session) and the privacy gate rejects the rest. **The supply
repair removed one of the two accidental protections without anyone noticing
the third gate was the load-bearing one.**

### Outcome: the span is the query, the sentence is the claim

Each `ClaimCandidate` now carries its enclosing `sentence`, threaded through
`ClaimItem.claim_sentence` (defaulted, so queue entries written by the previous
build still load) to the verifier. Then:

- **Outbound is byte-for-byte unchanged.** DuckDuckGo still gets the scrubbed
  *span*, which is what a narrow entity string is good for. The sentence is
  strictly richer and never leaves the machine.
- **The local model is asked about the scrubbed sentence**, which is a
  proposition it can actually adjudicate. It goes through the same scrubber for
  boundary uniformity, not because the local model is untrusted.
- **A span whose sentence asserts nothing is no longer extracted at all.** A
  bare title like "Magical Shopping Arcade Abenobashi" matches `proper_noun`
  and looks like a claim; there is no question to ask about it. This drops 3 of
  90 spans and is what stops the empty-verdict path existing.

Replayed over the corpus, the 44 claims that clear every gate go from bare
noun phrases to sentences like *"Trine 2 was developed by Frozen Byte and
published by Atlus on December 9, 2011"* — which is checkable, and which
happens to be worth checking.

**Eighth recurring shape:** *a pipeline can be correct at every stage and still
carry a payload that cannot answer the question.* This is H18's lesson one
layer further down — there, a signal was measured and found empty; here, the
unit of work was never the right shape to begin with. Both were invisible
because every component did its own job properly. **Rule: for any pipeline that
extracts a unit and then reasons over it, write down the unit and check that
the question you are about to ask of it is answerable.** "Verify `2026`" fails
that test on sight.

---

## H21. "How Jacob writes lately" said the same sentence for eleven weeks

**Severity: high — the always-on register cue was a constant, and half of
the constant was wrong.**

First entry from Part 4, which turns the audit away from the concept layer and
the worker fleet toward the **K-series patterns** — the always-on blocks. The
question that found it is one query, and it is worth keeping: `turn_prompt_blocks`
records one row per (turn, block) with its rendered length, so

```sql
SELECT block, COUNT(DISTINCT assistant_message_id) n,
       COUNT(DISTINCT chars) distinct_lengths
FROM turn_prompt_blocks GROUP BY block;
```

separates the blocks that vary from the blocks that only appear to. Ten blocks
rendered on all 165 instrumented turns at a **byte-length that never changed
once**. Five are fixed grammars and are supposed to be (`speech_grammar`,
`motion_grammar`, `outfit_grammar`, `overlay_grammar`, `touch_grammar`). The
other five are advertised as *learned*: `learned_style_addendum`, `axes_block`,
`hobby_block`, `petname_block`, and the one this entry is about —
`style_signal_block`, 40 characters, identical on every turn.

### What it was saying

K13's render is `f"How {name} writes lately: " + ", ".join(labels) + "."`, so 40
characters pins the label set exactly. Replaying all **2018 real user turns**
through the analyzer (pure regex, no LLM, so the replay is exact rather than an
estimate):

| | shipped behaviour | measured |
| --- | --- | --- |
| turns where the block rendered | "costs zero on a neutral-register speaker" | **99.7%** (2011 of 2018) |
| distinct sentences it ever produced | five axes, seven labels | **3** |
| share on the single most common one | — | **98.6%** — *"How Jacob writes lately: chatty, formal."* |
| times the line changed | — | **4 times in twelve weeks** (two of them during warmup) |

It has said the identical sentence continuously since 2026-05-25 apart from one
12-turn wobble. The docstring's claim that the empty case is "the common
no-signal case" was false by a factor of 300.

### Three of the five axes had never emitted a label, and could not

This is the part that makes it structural rather than a tuning miss. Per-turn
measurements against each axis's own bar:

| axis | bar | best value ever observed | verdict |
| --- | --- | --- | --- |
| `emoji_density` | 0.05 | **0.000** | never nonzero on any turn, ever |
| `slang_density` | 0.15 | 0.009 (window mean) | **17× below the bar** |
| `question_rate` | 0.40 | 0.333 (window mean) | never reached |
| `formality` | 0.55 | window mean **min 0.567** | never once *below* the bar |
| `terseness` | 0.55 | the only axis that ever moved | crossed twice in 12 weeks |

Each is a different flavour of the same mistake:

- **The emoji axis was blind, not quiet.** Zero of 2018 turns contain a Unicode
  emoji and **47.8% contain an ASCII emoticon** — he writes `:D`, `:)`, `:3`,
  `:p`, not U+1F604. The single most expressive marker in his writing was
  invisible to the axis built to measure it.
- **Slang was measured per *word*.** Reaching 0.15 requires 15% of every word in
  a 30-turn window to be a slang marker, which no prose does; the highest
  per-turn value in the corpus (0.20) came from one marker in a five-word
  message. As per-turn *incidence* the same signal is a usable 1.9%.
- **`formality` is not formality.** It scores 0.5 for starting with a capital
  and 0.5 for ending with a full stop — a **typing habit**, which is stable per
  person by construction and so can never be a "lately" signal. It rated these
  as maximally formal, on every turn, for three months:

  > *"Aww :3 gladly. I am sitting next to you and embracing you with both hands."*
  > *"I am looking forward to it :p pulling you tighter to my embrace."*

  So the one thing the block reliably told her about his register was that he
  writes formally to her. He does not. **An always-on block that is wrong is
  worse than one that is absent**, and this one had been arguing against the
  persona's "match his register" instruction on every turn since May.

### The root cause is the comparison, not the constants

Retuning the five bars would have been the obvious fix and the wrong one. An
**absolute** threshold on one person's writing can only ever produce a constant,
because the thing it measures is a trait: whoever he is, he is that consistently.
Move the bars and he is labelled `terse` forever instead of `chatty` forever. The
information was never in the level. It is in the **change**.

### Outcome: it asks "than usual", not "is he"

Each axis is now scored against **his own rolling baseline** — an O(1)
exponentially-weighted mean and variance per axis (five float pairs, so the
per-turn UPSERT stays small) — and speaks only when the recent window departs
from it. Replaying the same 2018 turns through the shipped code:

| | before | after |
| --- | --- | --- |
| rendered on | 99.7% of turns | **12.4%** |
| distinct sentences | 3 | **11** |
| times the line changed | 4 | **98** |

Silence is now the default and it is informative: 87.6% of turns get no block at
all, because he was writing the way he writes. When it does speak it is because
an axis moved three standard errors, and it says things like *"more playful
markers than usual"*, *"looser punctuation than usual"*, *"terser than usual"* —
each of which is an actual observation about today.

Three implementation notes worth keeping, because two of them were bugs I
introduced and caught only by measuring:

- **The yardstick is the standard error, not the standard deviation.** The
  tested quantity is a mean of 30 samples. The first cut compared it against
  the *per-turn* spread — ~0.5 on a binary axis — and fired on **0.0%** of the
  corpus, replacing an always-on constant with an always-off one. There is a
  regression test named for this.
- **An EWMA seeded at zero has a months-long cold start.** At `alpha = 1/300`
  the baseline reaches only **49%** of the true mean after 200 turns, so every
  axis would have read "higher than usual" until roughly October. The effective
  rate is floored at `1/count`, making it an exact running mean until the decay
  horizon and an EWMA after.
- **Old persisted state is discarded, not half-read.** The blob stored per-word
  densities under keys this build reads as incidence rates; loading it would
  seed the baseline with values that can never recur. A version bump forces a
  re-warm from history instead, and the warm scan was deepened from 60 to 400
  messages so the baseline starts real.

Five hand-tuned per-axis bars collapsed into one `style_signal_sensitivity`,
which means the same thing for a terse writer and a verbose one — the old bars
were fitted to this corpus and would have been wrong for anyone else's.

**Still open, same neighbourhood.** Two label directions never fire on this
corpus and that is honest rather than broken — he is *always* well-punctuated
and *rarely* asks questions back, so there is no room above the ceiling or below
the floor. Worth re-measuring, not fixing.

**The other constant-length blocks are now read, and all six are innocent.**
`learned_style_addendum` (655) is a fixed steer *about* learned lines, not a
learned line — its own comment says "constant text (never per-turn), so it stays
in the cache prefix"; only the name misleads. `pajama_block` (105) is a literal
hardcoded string. `petname_block` (112) is a relationship-phase template and the
phase moves in months. `axes_block` has since moved (3 distinct lengths over 183
turns), so it was never constant, only slow. `hobby_block` (139) is the
instructive one: it renders `"... (8 lessons in)"` and the hobby worker really
did advance it 5 → 6 → 7 → 8 over the window — **every value was a single digit,
so the length never changed while the text did.** That is the technique's false
positive, and it is worth keeping in mind: `chars` is a lower bound on variation.
The query produces a shortlist to read renderers for, never a verdict.

**Tenth recurring shape:** *a signal measured in absolute terms against a stable
trait is a constant, however carefully its thresholds are tuned.* H18 found a
signal with 0.05 reliability and H20 found a payload that could not answer the
question; this is the third member of that family and the most general — the
feature worked exactly as designed, and the design could not have worked. **Rule:
for any "lately" / "right now" / "currently" signal, check that it is comparing
against something. If it compares against a fixed number, the first question is
what its output distribution actually looks like over the corpus — and `avg ==
max` on `turn_prompt_blocks.chars` finds every instance of this in one query.**

---

## H22. K73 named one ritual, then locked itself shut forever

**Severity: high — a warm long-relationship beat is not rare, it is
terminated, and the log reported the failure as progress on every run.**

H21 asked which always-on blocks never *change*. The complementary question on
the same table is which registered blocks never *render*, and it is one join:
`_PROMPT_BLOCK_TIERS` is the assembler's declared inventory, `turn_prompt_blocks`
is what actually reached the model, and empty renders are dropped before the
insert. **47 of 113 registered blocks have never emitted a single character** in
183 instrumented turns.

Most of that number is not a finding, and separating the two took longer than
finding it did:

| group | count | verdict |
| --- | --- | --- |
| T4/T5 ambient + affect blocks | 10 | **by design** — `grounding_line_mode == "replace"` explicitly zeroes `circadian`, `weather`, `world`, `activity`, `ambient_noise`, `affect`, `mood_hint`, `relationship`, `user_state`, `mood_shell` and fuses them into `grounding_block`, which renders on 183 of 183 |
| T6 episodic detectors | ~29 | **mostly honest** — `rupture`, `boundary_clash`, `fact_reversal`, `user_correction` and friends *should* be silent over two days |
| downstream of H19 | 3 | `promise_followthrough`, `belief_gaps`, `user_expertise` sit on stores that H19 had just unblocked |

Which is the real lesson of the sweep: **"never fired" is not evidence on its
own.** Rarity is the design intent for half this list, and the discriminator is
not the block at all — it is whether the *producer* has ever produced. That is
what the worker result payloads answer, and one of them was not ambiguous.

### The producer that finds six things and ships none

```
shared-ritual sweep: messages=1637 sessions=63 candidates=6
                     new=['saturday:afternoon:casual_check_in'] stored=6 drafted=0
```

Eight recorded runs, all identical: six candidates, six stored, **zero drafted**,
and the same key announced as `new` every single time. A key cannot be new twice
if it is being stored, so those two symptoms are one bug.

The store held six rituals, **all six flagged `acknowledged`**, while `cue_pool`
— which is never pruned, and retains rows back to May — holds exactly **one**
`shared_ritual` cue ever: `our late-night friday check-ins`, surfaced once and
used, 2026-08-02. In today's code `mark_acknowledged` is only reachable after a
successful publish, so five of those flags cannot have come from this build. They
are pre-cue-pool state: the K73 write-up still describes the old consumer as
flagging the ritual `acknowledged` *when the block fired*, which means the other
five were most likely genuinely named, through a path that no longer exists.

### The cap counted the permanent record against the pending budget

Acknowledged rituals are permanent by design ("it became a real thing"). The cap
covered the whole store:

```python
keep_pending = max(0, max_active - len(ack))   # max_active = 6
```

At six acknowledged that is **zero**. Every newly-formed ritual was created,
appended to `new_keys`, and then trimmed away before the save — so it was new
again on the next sweep, forever — and `pick_unacknowledged` then saw nothing but
acknowledged rows and returned `None`. The feature could not name another ritual
no matter how many genuinely formed. A replay of the live store confirms it
exactly: 7 candidates in, saturday reported new, 6 stored, saturday absent.

There was a second copy of the same mistake one stage earlier:
`detect_rituals(max_rituals=max_active)` also ranked already-named rituals
against new ones for a fixed six slots, so the record would have crowded new
patterns out of *detection* even after the store was fixed.

### Outcome

The two budgets are now independent — pending is capped at `max_active`, the
record at its own `max_acknowledged` (18, oldest dropped first, so the blob stays
bounded) — and the detector is asked for `max_active + len(acknowledged)`
candidates so re-detecting the record cannot crowd out a new pattern. `new_keys`
is computed **after** the trim, so the sweep log can no longer report a row that
was discarded. Replaying the live store through the shipped code now yields
`pick_unacknowledged -> 'our Saturday-afternoon check-ins'`: a real ritual, four
weeks running, that has been waiting to be noticed since at least 2026-08-09.

**Deliberately not done:** the five older `acknowledged` flags are left set.
Clearing them would look like a repair — five warm beats released — but the
pre-pool consumer set that flag *on fire*, so they most likely record rituals she
really did name, and `cue_pool` simply predates them. H7's rule is to look at
what a broken gate was holding back before opening it; here it was holding back
five repeats of something already said, and saying "I love that this has become
our thing" twice is worse than never.

**Checked and healthy while here.** `wellbeing_concern` looked like the same bug
— one `late_nights:3` finding, then `same_signature` and `drafted=0` on all 28
runs since — but its cue has `surfaced_count=2`. It reached her prompt twice and
she chose not to raise it; the latch is then doing its job of not nagging. The
`surfaced_count` column is what separates "never said" from "said and declined",
and the story died on it.

---

## H23. The associative-wander bar was set below the floor of the distribution

**Severity: medium — 107 consecutive no-ops, and the same root shape as H21.**

The second producer from the H22 sweep. `associative_wander` — the worker that
connects two distant topics into a genuine observation — reported `no_pair` on
**107 of 107 runs**. Its gate:

```python
if cos is None or cos > max_cosine:   # max_cosine = 0.25
```

Computing every pair the worker would consider, over the live topic graph:

| | |
| --- | --- |
| eligible clusters / pairs | 34 / **561** |
| **minimum** cosine over all pairs | **0.2648** |
| p05 / median / max | 0.405 / 0.664 / 0.947 |
| pairs at or below the 0.25 bar | **0 of 561 (0.0%)** |

The two most unrelated topics in the entire corpus — *"anime series details"* and
*"finding inner stillness"* — score 0.2648, and the bar was 0.25. It missed by
0.015 and had therefore never once admitted a pair.

**0.25 is a number about an embedding model, not about topics.** Sentence
encoders do not spread unrelated text toward zero; on this 1024-dim model
genuinely unrelated topics land at 0.3–0.5 and near-orthogonality essentially
never occurs. The threshold encodes an intuition about cosine space that no real
encoder satisfies — and it would silently break again on any model swap, in
either direction.

### Outcome: rank within the corpus, keep the absolute as a ceiling

"Which two topics are furthest apart?" is the question the feature always meant
to ask, and it is answerable without knowing anything about the encoder. Pairs
are now ranked by cosine and the most distant `associative_wander_pair_quantile`
(10%) are eligible, with `max_pair_cosine` demoted to a **ceiling** (0.60, which
excludes the closer half outright) so that a corpus where every topic is the same
topic still yields nothing rather than nominating its two least-similar clusters
as a striking connection. `ceil` not `round`, so a two-cluster graph still offers
its one pair instead of flooring to zero and reproducing the old silence.

On the live graph: **0 pairs → 20**, headed by *"cat behavior and traits" ↔ "Path
of Exile gameplay"* and *"anime series details" ↔ "Jacob's religious
skepticism"*. The worker LLM is still asked for one genuine connection *or
nothing*, so the quality gate is unchanged — this only restores its input.

**Eleventh recurring shape — really H21's, in a second costume:** *an absolute
threshold is a claim about a distribution you have not measured.* H21's version
was a stable trait scored against a fixed bar; this one is a fixed bar placed
outside the range a model can produce. Both were tuned-looking constants written
before anyone plotted the thing they gate. **Rule: any constant compared against
a model output — a cosine, a confidence, a similarity, a z-score — must be
justified by the observed distribution of that output, and the check is cheap:
compute the quantiles and find where the bar falls. If the answer is "outside",
the feature has never run. Prefer a quantile to a constant; it survives a model
swap and a change of corpus, and it cannot be placed out of reach.**

---

## H24. Every continuity signal she has is scoped to the thing that resets

**Severity: high — not a rate problem or a tuning problem. Six features
were switched off simultaneously by a UI affordance, and each one of them
was working correctly.**

Reported rather than found: *"when I start the desktop app or the PWA I
land on a new conversation or some older one. Those conversations are
separators for me. But for Aiko everything should be continuous."*

A conversation is **filing**. He starts one to get a divider in his own
sidebar, and it says nothing about whether the relationship paused. On
Aiko's side it says everything, because `session_id` is the key that
*every* short-term continuity mechanism happens to be scoped by:

| what she loses at the seam | why |
| --- | --- |
| the transcript | `get_messages(session_key)` |
| the rolling summary | `session_summaries.session_id` |
| the K21 thread note | `thread_notes.session_id` |
| J5 reconnection, K14 absence curiosity, K28 turning over, H21 sleep return, K36 away activities, K34 forward curiosity | **all six** measure from the previous assistant message *in the same session* — and a fresh session has none, so all six return `None` together |

Nothing here is broken. Every one of those gates is doing exactly what it
says. But the composition means the moment she most needs "we were
talking about X, three hours ago" is precisely the moment she knows
least: long-term memory and relationship state survive, so she is warm
and knows who he is, and has no idea a conversation just ended.

**Twelfth recurring shape:** *a scope that is right for each feature
individually can be wrong for all of them at once.* H18/H20/H21 were each
one signal carrying no information; this is six signals that each carry
information and are all keyed to the same resetting value, so they fail
as a block and the failure looks like a personality trait rather than a
bug. **Rule: when several features share a gating key, ask what happens
on the turn that key changes. If the honest answer is "all of them go
quiet", that turn needs a feature of its own.**

### Outcome: bridge the seam rather than widen the scope

Widening any individual gate would have been wrong — J5 measuring across
sessions would fire on a *deliberate* switch into an old thread, and the
per-session summary is per-session for good reason. So K91 adds one T2
block that exists only at the seam: while the new conversation holds
fewer than `agent.continuity_max_messages` (6) messages, it names how
long ago the previous conversation ended and what it was about, and takes
one of two tails depending on whether the gap is under six hours (same
sitting, carry on) or over (noticing it is natural). Deciding "is this a
seam?" costs no query. Full write-up in
[configuration.md](../configuration.md#k91--session-continuity-bridge).

### And the restore pointer was a day stale

The other half of the report — "or some older one" — was a real bug with
a one-line tell. `config/user.json` said `last_active_id: "s2"`, last
used 2026-08-10; 75 messages had since landed in `fa7593d7`. Every launch
reopened the older thread.

The pointer is written by `switch_session`, so it records **intent**, not
activity — and `_resolve_initial_session_id` honours it *over* the
database's own record of where the last message went, with no
self-correction. Anything that lands on a session without a click leaves
it stale forever. It is now also written on the first user turn in a
session, guarded by an in-memory copy so it costs one small write per
session; the copy starts empty at boot, so the first turn after a cold
start repairs a pointer that drifted under a previous build.

**Also fixed while here:** on phones the transcript was never fetched at
all. `useSessionHistory` lived in `SessionSidebar`, which the desktop
tree always mounts but the phone tree only mounts inside
`MobileNavDrawer` — and that returns `null` while closed. The socket
connected, `hello` set a perfectly good session key, and the user got the
empty state with no way to tell it from a genuinely new conversation.
That is most of "I land on a new conversation" on the PWA, and it is a
different bug from the pointer with the same symptom.

**Left open — K21 notes carry unreliable dates.** The bridge shows the
previous thread's fresh-eyes note, and one in the live store opens
*"Jacob fell asleep on June 29, 2026"* on a thread whose messages are all
from 2026-08-10. Five of the six most recent notes have correct dates, so
this is an outlier rather than a systematic fault, but it is worth a pass
over the ThreadResummaryWorker prompt: it is being asked to date events
and has no reliable clock for them. K91 sidesteps it by computing elapsed
time from message timestamps and never reading it out of the note prose.

---

## H25. The crash was in a language that has no stack trace

**Severity: high — an unexplained process death, plus a diagnostic gap that
guaranteed the investigation would start in the wrong place.**

Reported: *"Aiko crashed, even during night on same error. It is weird, my
system should be stable. I run benchmarks and occt for stability and no
issues."* `data/crashlog.txt` held a `faulthandler` dump headed **`Windows
fatal exception: access violation`**, with the faulting thread stopped here:

```
File "app/core/conversation/topic_graph.py", line 1808 in _live_to_topic_clusters_locked
File "app/core/session/speaking_workers_init_mixin.py", line 2348 in _consolidation_graph_mature
File "app/core/proactive/idle_worker_scheduler.py", line 611 in _probe
```

**Line 1808 is a dataclass construction.** Two tuple comprehensions and a
`TopicCluster(...)`. Pure Python cannot dereference a bad pointer, so that
frame is not the cause — it is where an already-corrupted heap was next
touched, and an allocation-heavy loop on the every-15-second idle probe is
the most likely place in the process for that to happen. The log settled
it: the same path had run cleanly at 11:21:29 and again at 11:23:00, three
minutes before the fault. Auditing it anyway cost a pass over the lock
discipline (correct — every `_live` access is guarded) and the centroid
decode (correct — it copies out of the SQLite blob rather than aliasing
it). Both were innocent, as the timestamps had already implied.

**A hardware theory was never on the table, and this is worth stating
plainly because the user's instinct was to distrust their machine.** An
access violation is a pointer bug inside a native library. OCCT and memtest
load the CPU, RAM and GPU looking for physical faults; they cannot observe
a library writing outside its allocation. A clean stability run and this
crash are answers to different questions.

### What was actually wrong with the process

Enumerating the loaded modules in the app's own venv, staged by import:

| After importing | OpenMP runtimes now mapped |
| --- | --- |
| `numpy`, `lancedb`, rag store, topic graph | *(none)* |
| `app.stt.realtime_stt_service` | `torch\lib\libiomp5md.dll`, **`ctranslate2\libiomp5md.dll`**, `sklearn\.libs\vcomp140.dll` |

Two separate copies of Intel's OpenMP runtime, plus Microsoft's, in one
process. Each copy keeps its own global state and thread pool; Intel and
PyTorch both document the combination as undefined behaviour, and the
documented symptom is a random native fault after long uptime — precisely
this crash's profile, and precisely the profile that survives a stress
test. Nothing in the repo set `KMP_DUPLICATE_LIB_OK`, and no `OMP: Error
#15` was ever logged, so this had been loading silently on every boot.

It was also being paid for nothing. `session_controller` imports
`RealtimeSttService` at module scope, and that module did
`from RealtimeSTT import AudioToTextRecorder` at import time, which drags
in torch and CTranslate2 whether or not voice is ever used. P27 had already
deferred the *recorder* (the ~0.9 GB subprocess); the *import* was still
eager. `realtimesst.log` was last written on **July 12**, a month before
the crash: no model had started in any recent run, and the process was
carrying the hazardous configuration regardless.

**Honest limit: this is a mechanism, not a proven cause.** `faulthandler`
prints Python frames only, so nothing in the dump names the faulting DLL.
The duplicate-runtime finding fits the symptoms and is independently worth
fixing, but attributing *this* fault to it would be exactly the
plausible-looking non-fix this document exists to discourage.

### Outcome: make the next one self-explaining

Since the cause could not be proven, the deliverable is the diagnostic.
[`app/core/infra/native_crash.py`](../../app/core/infra/native_crash.py)
installs an unhandled-exception filter that records the exception code, the
faulting address, **the DLL containing that address** (via
`GetModuleHandleEx` — normally the whole answer on its own), whether the
process held duplicate OpenMP runtimes at the moment it died, and a
minidump for native stack walking. It returns `EXCEPTION_CONTINUE_SEARCH`,
so the process still dies exactly as before; this only leaves evidence.
Read it with `get_native_crashes()`.

Verified against a deliberately triggered access violation, which is
fiddlier than it sounds: a ctypes *foreign call* is wrapped in ctypes' own
SEH and surfaces as `OSError` without ever reaching the filter, so the test
has to dereference a pointer object instead. Two real bugs fell out of
running it rather than assuming it worked — `MiniDumpWriteDump` silently
wrote a zero-byte file until every signature had explicit `argtypes`
(ctypes narrows pointer-sized arguments to `int` otherwise), and it then
refused the exception-pointers struct from inside the filter with
`ERROR_NOACCESS`, so the dump is now written without it and every thread's
native stack is still captured. The handler correctly named
`_ctypes.pyd` as the faulting module in the test.

Alongside it: `log_native_runtime_inventory()` logs the runtime tally once
per boot (WARNING when unsupported), and the `RealtimeSTT` import is now
deferred behind `_recorder_class()` with availability answered by
`find_spec`, so a text-only session loads no OpenMP at all. Confirmed
empirically — importing and constructing the service maps zero OpenMP
runtimes while `is_available` stays `True`; forcing the import reproduces
`DUPLICATE: libiomp5md.dll loaded from 2 paths`.

### The duplicate is gone, and the fix was sitting in the lockfile

The hazard did not need a workaround. It needed the venv to match the spec
the repo already declared. `pyproject.toml` asks for
`realtimestt>=1.0.2,<2` and `requirements.lock` pins `realtimestt==1.0.2`,
`torch==2.13.0`, `ctranslate2==4.8.1` — and lists **no** `openwakeword`
and **no** `scikit-learn`. The installed environment was on
`realtimestt 0.3.104`, a month-old resolution nobody had synced.

That single version gap explains the whole inventory. 0.3.x imports
`openwakeword` unconditionally (which is what dragged in scikit-learn and
therefore `vcomp140.dll`) and reaches `faster_whisper` — and so
CTranslate2's `libiomp5md.dll` — during a plain module import. 1.0.2 makes
both optional: transcription engines resolve through a factory that calls
`import_module("faster_whisper")` inside `FasterWhisperEngine.__init__`,
and the recorder runs that engine in a **child process**
(`start_recorder_worker` → `mp.Process` on Windows).

Measured after syncing, in the process that died:

| Process | OpenMP runtimes mapped | Hazardous |
| --- | --- | --- |
| main app, STT service imported and constructed | `torch\lib\libiomp5md.dll` | no |
| transcription child (`faster_whisper` + `soundfile`) | `ctranslate2\libiomp5md.dll`, `torch\lib\libiomp5md.dll` | yes, isolated |

`vcomp140.dll` no longer exists anywhere in the venv. No
`KMP_DUPLICATE_LIB_OK`, and no symlinking of one copy onto the other —
which is worth noting because that hack was the obvious move and is now
clearly the wrong one: the two files were byte-different builds of the same
Intel version before the upgrade, and are different versions after it
(20250910 vs 20260213), so forcing one on both would have been an ABI
gamble taken for a duplicate that process separation had already removed.

Two limits stay on the record. `faster_whisper` pulls torch transitively
(via `transformers`, which imports torch whenever it is installed), so the
transcription child does hold both copies; on Windows it is a separate
process holding only the Whisper model, but `start_recorder_worker` uses a
*thread* on Linux, so the duplicate would land in the main process in the
Docker image. And this still does not prove what killed the app in
August — it removes the unsupported configuration from the process that
died, and the crash handler now names the DLL if it happens again.

Removing what nothing imported took 654 MB with it, 620 MB of that
**PySide6** — a Qt install in a repo whose rules say the web UI is the only
UI, referenced solely by comments claiming independence from it.

**Twelfth recurring shape — the top frame is not the bug when the fault is
native.** Every other entry in this document was found by reading a Python
stack or a table of telemetry, where the top frame *is* the lead. A native
memory fault inverts that: the reported location is a function of
allocation pressure, not of causality, and the honest first question is
"was this process in a supported configuration at all?" rather than "what
is wrong with the function named in the dump". **Rule: for an access
violation, read the faulting DLL and the loaded-runtime inventory before
reading any Python frame — and never let a stress-test pass talk you out
of looking for a pointer bug.** The corollary that cost real time here:
duplicate native runtimes are invisible until something enumerates loaded
modules, so that enumeration belongs in the boot log rather than in an
investigator's head.

**Corollary, and the cheapest step skipped here: diff the installed
versions against the lockfile before theorising about the code.** The
audit of `topic_graph.py`, the staged import experiment and the
duplicate-runtime analysis were all downstream of an environment that was
simply a month behind its own `requirements.lock`. `pip check` and one
comparison against the lock would have surfaced it in seconds, and the
hazardous configuration would have read as a symptom of drift rather than
as a property of the dependency set. An unexplained native crash makes
"is this the environment we claim to run?" a first question, not a
last one.

---

## H26. Every signal said healthy, and the recovery button was a no-op

**Severity: high — the third recurrence of one iOS failure, kept alive by a
detector that could not see it and diagnostics that could not be read.**

Reported: *"only one thing is again with the sound on mobile when i minimise
and open app again. I can see aiko live 2d avatar talking so server is
sending audio to the pwa, but no audio is playing until i kill and open the
app again."* The avatar was honest and misleading at once: talk motion and
the "speaking" aura are driven by the server's `tts_state`, so they animate
whether or not a single sample is audible. Only `ParamMouthOpenY` follows
real playback, via the analyser tap.

Three things were wrong, and only the first is a bug in the usual sense.

**1. "Restart sound" could not restart anything.** It called
`onForeground()`, which resumes only when `_needsResume(ctx)` is true — and
that is false for a context reporting `"running"`, which is precisely the
context someone taps the button about. The automatic foreground pass had
already cleared `_wasBackgrounded` on the way back into the app, so the
rebuild path was closed too. The one user-facing recovery in the product
was structurally incapable of running in the case it existed for, which is
why force-quitting really was the only cure.

**2. The liveness probe was built from the only signal available, not from
the signal that mattered.** The previous fix assumed a reclaimed audio unit
leaves `currentTime` frozen. iOS also hands back contexts whose clock
advances normally while the output no longer reaches the speaker. A live
clock proves the graph is *rendering*; it says nothing about *audibility*,
and there is nothing downstream of the render that a page can read — the
`AnalyserNode` sits before `destination` and will cheerfully report the
samples it is feeding into a void. So the probe was removed rather than
tuned, and the context is now replaced after **any** background stint
(`_replaceContext`). The cost is stated rather than hidden: a replacement
needs a gesture to start unlocked on iOS, so returning to the app can land
on "Sound is off" until the first tap. Honest silence with a readout and a
one-tap fix beats a context that swallows every frame for a session.

**3. The diagnostics for all of this were unreachable, and had always
been.** `rules/debugging.md` told the reader to grep `[ui] audio
contextDead`. That line could never appear: `config/default.json` pinned
`ui_log_categories` to `["ws","channel","settings","voice"]`, the JSON
value overrides the code default (which *does* include `"audio"`), and the
endpoint drops out-of-list sources **silently** while returning success.
Three generations of `app.log` contained zero `[ui]` lines. The instruction
and the implementation had disagreed since the audio diagnostics were
written, and nothing anywhere said so.

**Thirteenth recurring shape — a detector built from the signal you can
read is not a detector for the thing you care about.** H21 and H23 were
thresholds set against distributions nobody measured; this is the same
error one level up, in the *choice of observable*. The clock was picked
because it was the only thing that moved, and it does distinguish one real
failure mode, which is exactly what made it convincing. **Rule: before
shipping a liveness check, say out loud what it would fail to notice.** If
the answer is "the case the user actually reported", the check is not a
fix — it is a narrower fix wearing the name of the general one, and the
next recurrence will look like a regression rather than a gap.

The corollary belongs with §3 above and is cheap: **an allow-list that
drops silently is indistinguishable from a client that never spoke.** Any
filter that discards input must either log the discard or be asserted
against the documentation that tells people to rely on it.

---

## H27. Two workers burned an hour of GPU to write nothing, in a log that said so

**Severity: high — the promise and belief extractors have persisted zero rows
since the day each shipped, and every stage reported success.**

Found by reading the log, not by querying it. Three consecutive lines:

```
privacy scrub REDACT in="[today 13:27] Jacob: That should not be problem…" dropped=['today','jacob','me','i','i','today','aiko',… 91 more]
promise-worker start: session=default:c00f5098 lookback_turns=12 raw_chars=3825
promise-worker llm-unparseable elapsed_ms=23630
```

The question asked of them was the right one — *why is a redaction event
being logged for something that isn't going anywhere, and why can't the
worker parse its own model's output?* Behind those two lines were four
independent bugs, plus one design gap. They are worth reading in order,
because **the first bug destroys the evidence for the second**, which is
why this survived two previous attempts at it.

### What it cost

| | promise worker | belief worker |
| --- | --- | --- |
| shipped | 2026-06-19 (`5980acb`) | 2026-05-31 (`9dab8cd`) |
| runs with material, 10 days of log | 63 | 36 |
| `llm-unparseable` | 27 | 8 |
| "success" with zero items | 34 | 27 (`upserted: 0` every time) |
| median run | 34.4 s | 33.4 s |
| GPU wall time in those runs | 33.2 min | 20.9 min |
| rows in the store | 59, **none newer than 2026-06-18** | **0** |

The promise table's last write is the day *before* the dedicated worker
shipped: the 59 rows are its predecessor's, and the worker that replaced it
has never added one. Over the same eight weeks total memory production
tripled (45 rows in week 24, 385 in week 31), so nothing about the
environment suggests a quiet period. `beliefs` has been empty for ten
weeks. 54 minutes of 27B inference, one row written, and not a single
`WARNING` in the workers' own logger.

### Bug 1 — the answer never had tokens left, and the log said so 98 times

`ollama answer truncated … answer_tokens~=0 thinking_tokens~=2364
answer='<empty>'`, **98 times across 10 worker surfaces** (`promise_worker`
34, `belief_worker` 27, `goal_worker_reflection` 18, `dream_worker` 6,
`memory_consolidator`/`memory_conflict_worker` 3 each, and four more).

The tell is that `completion_tokens` is *constant to the token* within each
surface — promise 2448, belief 2398, goal reflection 2228, dream 2148,
consolidator 2128. Each is that surface's own `num_predict` plus the
`think_num_predict_headroom` of 2048. This was never variance or a hard
prompt; every call walked into the same wall, and the reasoning trace
(estimated 1.65k–5.1k tokens, median 2.4k) had consumed all of it before
the model emitted its first answer token.

Two things make this worse than a mis-tuned constant:

- **The warning printed the remedy** — *"raise num_predict for this surface
  if frequent"* — 98 times. The diagnosis was correct, sitting in the log,
  addressed to nobody.
- **It had already been fixed once, at the wrong layer.** `3373465`
  (2026-06-26) is titled *"forward think:false so reasoning workers stop
  returning empty"*, and it touched exactly two files: `dream_worker.py`
  and `pre_thought_worker.py`. Both surfaces appear in the starvation list
  above, six weeks later. A client-level failure was patched at two of ten
  call sites, by hand, and the two that were patched did not stay fixed.

So the fix is at the client this time, in two parts. `think_num_predict_
headroom` goes 2048 → **8192** (60% above the worst trace measured, and
it is a ceiling, not a spend), and `OllamaClient` now treats *empty answer
+ `done_reason="length"` + `think=True`* as a distinct condition and
**retries once with thinking off** — in `chat`, `chat_json`,
`chat_with_tools` and `chat_stream` alike, so no surface has to opt in.
Measured on `qwen3.6:27b`, the trace costs ~35 s and reached the same
verdict a 2.5 s no-think call did, which is the number to remember the next
time a worker is given `think=True` for a routine extraction.

### Bug 2 — the prompt asked for a shape the JSON mode will not return

Both workers ran with `format_json=True` and both prompts said *reply with
a JSON array*; both parsers accepted only a bare `[…]`. `format: "json"`
yields an object, so the model has to wrap the array under some key, and
which key it picks is up to it. Now the prompts ask for
`{"promises": […]}` / `{"beliefs": […]}`, and both go through one shared
reader — [`app/llm/json_answers.py`](../../app/llm/json_answers.py) — that
accepts the object, a bare array, `{}` as "nothing found", a drifted key
whose value is the only list present, a single unwrapped item, and an array
embedded in prose.

**This is the bug the first one hid.** A starved call returns an empty
string, an empty string never reaches the parser, and the parser is where
the schema mismatch lives. Neither log line carried the payload, so from
the outside "the model can't produce JSON" and "the model produced nothing"
were the same event. `unparseable` now logs a bounded preview of what it
choked on.

### Bug 3 — "no answer" was reported as "nothing to report"

`_extract_with_llm` returned `[]` when the answer was empty, and the caller
logged `llm done: promises=0`. That accounts for 34 of the 63 promise runs
and 27 of the 36 belief runs: **shape 1 (silent-empty latching) in its
purest form**, since a quiet evening produces exactly the same line.
`llm_empty_answer` is now its own failure reason, separate from
`llm_unparseable` and `llm_error`, because the three have different causes
and different fixes.

### Bug 4 — the redaction audit described searches that never happened

284 `privacy scrub REDACT` lines in the corpus. **62 of them dropped
between 50 and 196 tokens** (median 107) and their `in=` is a whole
transcript — those come from the promise worker's privacy pre-check, which
calls `scrub_claim_for_search()` as a *detector*, reads whether it returned
something, and throws the scrubbed text away. Not one of the 62 preceded a
network request. The remaining 221 drop a median of **1** token and are the
real thing.

An audit line that fires when nothing is audited does more than waste
space: it makes the 221 real ones unfindable, and it trains the reader to
skip the category. So the one implementation now has two entry points —
`web_safe_probe()` for callers that keep the original text (yes/no; `BLOCK`
at INFO because a refusal is rare and interesting, `PASS` at DEBUG) and
`scrub_claim_for_search()` for callers that actually send the result
(`REDACT` at INFO). Both delegate to the same private `_scrub`, so the two
can never disagree about what is safe — there is a test asserting exactly
that, since a split gate is how a leak would get in. `dropped=` now
de-duplicates and counts (`6 occurrences: i, you, jacob`) instead of
printing one entry per occurrence.

### The design gap — stripping a query is not the same as leaving it checkable

This was the user's actual question, and it is the more valuable half:
*"stripping private informations are important but not enough, that could
remove the context."* The corpus agrees. `'playful moments with Jacob'`
scrubs to `'playful moments with'` — safe, and not a query anyone would
type.

H20 fixed the *local* side of this by threading each claim's enclosing
sentence to the verifier, and left the outbound query as the bare span.
F6's LLM reformulator was therefore still being handed spans that, measured
in H20, contain a verb 0 times out of 90. It did what you would expect with
nothing to work from: **27 of 34 reformulations came back byte-identical**,
and of the 7 that changed, six were casing or word order. The seventh is
the instructive one — `Back Camp` → `Back Camp TV series`, a model padding a
fragment with a guess about what kind of thing it is.

Two changes, neither of which touches the leak guard:

- **The reformulator gets the enclosing sentence as `CONTEXT`, with the
  span still as the `CLAIM`.** The sentence says what is being asserted,
  which is what a search has to be able to confirm or refute. It never goes
  to the network — it shapes the rewrite, and the rewrite still passes
  through the deterministic scrubber before any request, which matters
  *more* here, not less: giving the model private context gives it more to
  echo back. There is a test for that specific case.
- **F9's planner queries skip the F6 pass** (`already_neutral=True`). They
  were written by an LLM to be impersonal in the first place, and they are
  where most of those 27 no-ops came from — a second 27B generation per
  search to produce the same string. The deterministic scrub still runs.

### Fourteenth recurring shape: a diagnostic that names a symptom but carries no evidence

`llm-unparseable` and `privacy scrub REDACT` are the same authoring mistake
pointing opposite ways. One announced a parse failure and withheld the
input, so it could be emitted 35 times without anyone learning what the
model actually said. The other announced a redaction and printed 196 tokens
of it, for an event that never occurred. In both cases the line named a
*stage* rather than reporting an *observation*, and a reader who trusted it
would draw a wrong conclusion — that the model is bad at JSON, that queries
are going out.

**Rule, in three parts.** A line about malformed input carries a bounded
preview of the input. A line about *absent* input names which stage
produced the absence — "the model returned nothing" and "there was nothing
to do" are different lines. And a line describing an outbound action is
emitted only by the code path that performs it; if a predicate is shared
with a detector, the logging is not.

The corollary is H26's lesson relocated from detectors to fixes: **when the
same symptom appears at N call sites, the fix belongs where the symptom is
produced, not where it is noticed.** `3373465` patched two workers by hand
and the same failure was still live at ten surfaces six weeks later,
including the two that were patched. **If a fix has to be repeated per call
site, that is evidence about the layer, not about the call sites** — and it
is worth one grep for the other N-2 before shipping it.

---

## H28. A different block spent K52's fuel, and three features starved

**Severity: high — the whole "will" family's output surfaces, dead on one line.**

Picked up while asking a narrower question: the K90 lead/follow report, re-run
256 turns after its 9 August baseline, said the second pass at leading had moved
nothing. Own-material ratio **72% → 72%**, anaphoric openers 21% → 20%, echo 20%
→ 20%. The only number that shifted was ends-on-a-question, 8.3% → 7.1%.

The block telemetry said why. Of the leading family's four output surfaces,
**three had never rendered a single character in 253 instrumented turns**:

| block | renders / 253 | why |
| --- | --- | --- |
| `pursuit_lean_block` | **0** | no supply — all 5 pursuits are 3-day-old K85d seeds against a 7-day gate; *working as designed*, re-check after 16 Aug |
| `topic_appetite_block` (K54) | **0** | `want_pressure >= 0.35`, never reached |
| `thread_ownership_block` (K55/K89) | **0** | stamps need K53 initiative *or* a K52 imperative; the imperative never fired |
| `taste_lean_block` | 7 (2.8%) | working — once per conversation, behind a lull |

`wants_block` itself renders on **77.1%** of turns, always in its soft band. The
imperative band — the one the module docstring calls *"the sentence no existing
block ever says, and the piece that turns a permission slip into actual will"* —
has never rendered at all. All five live wants sat at pressure **0.150–0.159**
against bars of 0.35 and 0.7.

Pressure grows at `wants_growth_per_day` = 0.25 from an initial 0.15, so the bars
are 19 hours and 53 hours of survival respectively — easily inside the 14-day
`wants_max_age_days`. The wants were not failing to grow. They were not living
long enough to grow, and the reason was one read:

```python
rows = self._pending_seeds(limit=64)          # store.pending("curiosity_seed")
active_refs = {f"cue:{row.id}" for row in rows}
dead = seed_refs - active_refs                # -> drop_source_refs
```

`pending` means *may I surface this now*. It stops being true the moment a cue
renders into a prompt (`mark_surfaced` sets `state='surfaced'`) **and** while a
released cue sits behind its `not_before` cooldown. The pruner read that as *the
seed is gone, retire its want*. Meanwhile `curiosity_seeds_block` spends two
seeds a turn at 35 turns per hundred. Measured over all 131 seeds that have ever
been surfaced:

| | |
| --- | --- |
| median age at first surfacing | **1.9 h** |
| p75 / max | 7.3 h / **22.3 h** |
| survived the 19 h K54 needs | **4 of 131** |
| survived the 53 h the imperative needs | **0 of 131** |

So K52's pressure mechanic never ran, K54's appetite slip could not fire, and
K55/K89's thread ownership lost one of its two arming paths — three features,
one liveness test, and nothing in any log said so. The cue store's own docstring
had the distinction right all along: `mark_surfaced` is annotated *"The cue
reached the prompt. Not the same as it being used."*

### Outcome: liveness is not availability, and an ignored imperative pays

`CueStore.live()` now answers "does this cue still exist" over `LIVE_STATES`,
separately from `pending`'s "may I show it", and the pruner uses it — a want dies
when its seed reaches a *terminal* state, not when Aiko has merely been shown it.
Being offered a topic and not biting is the state that most deserves to keep
wanting. A full page counts as unreadable rather than empty, since absence from a
truncated list is not evidence of a dead seed and a false prune is the bug.

The producer read still uses `pending`: only a seed she has never been offered
should mint a *new* want.

That fix alone would have replaced silence with a ratchet. Pressure is what fires
the imperative, so with nothing to spend it the strongest want would cross 0.7,
render "bring it up THIS conversation", grow overnight and render again — the same
directive every turn for a topic she has by then declined repeatedly. So the band
now costs what it spends: an imperative that surfaces and whose topic still does
not come up charges that want `wants_brush_off_decay` (0.6, a little over two days
of growth), and below `wants_brush_off_floor` (0.2) the want leaves the ledger.
K89 solved the identical problem for thread stakes the same way — one polite
attempt is not a stake, and an unlimited number of attempts is not one either.

**Ninth recurring shape — a read that answers a nearby question.** Not H21/H23's
mis-sited constant: every threshold here was correctly placed and reachable. The
defect is that `pending` and `live` differ by a few rows almost all the time, so
the wrong one passes review, passes tests, and works fine until a *second*
consumer starts spending the same resource on a different schedule. K9's seed
block and K52's ledger were built a year apart and neither knows the other exists.
**Rule: when a predicate asks "does X still exist", check what the read actually
filters on — availability, cooldown and TTL are all different questions from
existence, and a pool with a state machine will offer you all four under similar
names. If a resource has two consumers, write down which one is allowed to
retire it.**

---

## H29. A want cannot outlive two showings of its source cue

**Severity: high — it is H28's own follow-up reading, and H28's fix did not land
the outcome it predicted.**

H28 closed with a falsifiable clock: pressure grows at 0.25/day from 0.15, so a
want should cross K54's 0.35 bar in **19 hours** and the imperative band's 0.7 in
**53 hours**, and `topic_appetite_block` plus the `wants_block` imperative should
therefore both leave zero "within about three days". That was written 12 August.
Read again on **13 August, 290 instrumented turns**:

| | 12 Aug (H28) | 13 Aug (now) |
| --- | --- | --- |
| strongest live want | 0.167 | **0.25** |
| oldest live want | — | **10.6 h** |
| `topic_appetite_block` renders | 0 | **0** |
| `wants_block` imperative renders | 0 | **0** |
| `wants_block` soft-band renders | 77.1% | **78.3%** |

The ledger holds its full 8 wants and **not one of them is older than 11 hours**.
The prediction did not fail by a little; nothing in the ledger has ever been
within a day of the nearer of the two bars. H28's fix was correct and did
work — the pruner no longer retires a want merely because its seed was
*shown* — but it moved the drain rather than closing it, and a second, unrelated
defect keeps the ledger's contents worthless even once they survive.

**Drain 1: the want's lifetime is owned by the cue, and the cue's clock is
measured in showings.** Every one of the 110 expired `curiosity_seed` rows died
the same death — `max_surfacings`, at **exactly 2 showings**, a **median 2.9
hours** after creation (p75 7.2 h, max 22.3 h). `curiosity_seeds_block` spends
two seeds a turn on 37.6% of turns, so a seed's two allowed showings are gone in
an afternoon. Expiry is a terminal state, so the (now correct) pruner retires the
want with it. And the other exit is no kinder: a seed that *is* matched marks the
want acted and removes it. **Both of the source cue's exits are fatal to the
want, and both arrive on a clock two orders of magnitude faster than the one the
pressure mechanic runs on.** The ledger is a conveyor wearing the interface of a
pressure cooker.

**Drain 2 was proposed, measured, and withdrawn — recorded because the reasoning
was seductive and wrong.** `render_block` sorts by pressure and renders
`ranked[:2]` ([`wants_ledger.py`](../../app/core/conversation/wants_ledger.py)),
so the two wants closest to the imperative bar are exactly the two shown on 78%
of turns, and a mention marks them acted. That reads like a pressure-release
valve sited at the top of the distribution, and the mechanism is real. It is also
not happening: over the logged window the ledger records **14 `pruned dead seed
want` events against 2 `acted`**. The soft band is barely spending anything, and
a want that *is* spent was spent by being raised, which is the feature working
rather than a drain. Fixing the band would have cost throughput to solve a
problem the data does not show. Left alone.

**Drain 3: a cap that refuses instead of evicting, in front of the fastest
producer.** `add_want` returns `added=False` when the ledger is at `wants_cap`
(8) rather than displacing anything, and the `curiosity_seed` feeder refills a
freed slot within minutes — the log shows `live` going 4 → 5 → 6 → 7 → 8 between
10:55 and 11:29 on 13 Aug, every one of them `source=curiosity_seed`. So **7 of
the 8 current slots are curiosity seeds and a goal or pursuit can essentially
never claim one.** The slowest, most significant sources are starved by the
fastest, least significant one, purely on arrival order.

**Also measured, and healthy — do not "fix" this one.** H28 listed
`thread_ownership_block` (K55/K89) at 0 renders as a symptom, on the theory that
it lost an arming path to the missing imperative. It has its *other* arming path
and that path works: the log carries `thread-ownership stamp: source=initiative`
on three separate initiations and then `verdict=engaged cosine=0.434 / 0.446 /
0.691 outcome=satisfied returns=0` for each. The block is a **defence** cue that
only renders when he brushes a thread off; zero renders means zero brush-offs,
which is the outcome the feature wants. Recorded here so the next audit stops
counting it as a corpse. K53 initiative is likewise firing on schedule (11.7% of
turns, `period=6` in light arcs).

**Remedy sketch** (not a plan — the pieces are independent and each is small):
mint a want as a **copy that owns its own clock** rather than a pointer to a cue,
so its TTL is the ledger's `max_age_days` and not the source's surfacing budget;
give the soft band a different slice than `ranked[:2]` so the imperative
candidate is held in reserve rather than spent first; and reserve ledger slots
by source so whimsy cannot hold seven of eight. The third is the one to do first
if only one gets done, because it is the cheapest and it is also what **K93**
needs.

### Outcome: settlement, not availability — and no source owns the ledger

Two of the three sketched pieces shipped; drain 2 stayed withdrawn.

**A want retires only when its subject is settled.** `CueStore.resolved_ids()`
answers "which of these ids are *done*" over `RESOLVED_STATES` — `used` and
`superseded` only. `expired` is deliberately excluded: it is the state a seed
reaches by being offered and refused, which settles nothing. The pruner keeps
its own copy of the want and ages it on `wants_max_age_days`; the seed's
two-surfacing budget no longer decides anything.

The population says how much that buys. Of the seeds this install has retired,
**110 expired — every single one at exactly 2 surfacings** — and 118 are `used`,
but 75 of those are a `migrated/k9` backfill rather than a real settlement. The
43 organic ones settle fast: median **6.8 h** from birth to `used`, p90 12.2 h,
max 20.6 h, **1 of 43 past the 19 h K54 bar and 0 past 53 h**. So a want whose
topic genuinely comes up still dies inside a day, which is correct — the want
was satisfied. What changed is the other 72%: refused seeds now leave their want
behind, and those are the ones with a path to either bar.

**The read is by presence, not absence.** H28's version asked for a page of live
seeds and pruned whatever was missing from it, which needed a page-full guard to
stop a truncated list from reading as "all dead". `resolved_ids` takes the ids
the ledger already holds and returns the settled subset, so an empty result, a
failed query and a ledger larger than any page all retire *nothing*. The
page-full defence is gone because the failure it defended against cannot be
expressed. **Prune on evidence that the subject is done, never on the absence of
evidence that it is not.**

**`wants_per_source_cap` (default 4, half the ledger).** A total cap filled in
arrival order belongs to whichever producer is fastest, and the log showed
exactly that: `live` walking 4 → 5 → 6 → 7 → 8 in 34 minutes, every one a
curiosity seed. The cap is per *source* rather than a priority order, because
the failure is monopoly rather than whimsy specifically — whichever producer is
fastest would otherwise own the ledger, and the slow producers are the ones
carrying anything durable.

**A test note worth keeping.** Every worker test buys its speed with a fake cue
pool, and the fake was faithful to the contract — so the first version of
`resolved_ids` passed all 75 of them while being wrong about the real schema
(`row["id"]` against a tuple-returning connection). One integration test that
walks the prune path through actual SQLite caught it immediately. **Where a fake
stands in for a store, at least one test per query has to touch the store.**

**Watch (re-check ~16 Aug):** `topic_appetite_block` and the `wants_block`
imperative should both leave zero, on the same clock H28 predicted — 19 h and
53 h from a want's birth. If they are still at zero with wants visibly older
than three days, the remaining suspect is the soft band after all and drain 2
comes back off the shelf. The second thing to watch is the opposite failure:
four seed wants can now hold their half of the ledger for up to 14 days, so if
the seed slots fill with high-pressure wants she never acts on, the ledger has
become the guilt list `max_age_days` exists to prevent.

**Tenth recurring shape — two clocks, and the faster one owns the lifetime.** A
mechanic is designed around a slow clock (pressure over days) but its unit's
lifetime is bounded by a resource governed by a fast one (two surfacings, hours).
Every threshold is reachable in principle, every stage is individually correct,
and the slow clock simply never gets to run — so the feature looks conservative
rather than broken, and no log line is wrong. Distinct from shape 9: there the
*read* was wrong; here every read is right and the *ownership* is wrong. **Rule:
when a feature accumulates over time, write down what can delete its unit before
the accumulation completes, and compare the two timescales explicitly. If the
unit is derived from a shared resource, copy the resource rather than pointing at
it.**

---

## H30. Half the cue declines still say only "provider", in nine specific cues

**Severity: low-medium — pure measurement, and it is the instrument the rest of
this file keeps needing.**

H7 split the cue decline reasons and the watch list asked whether the catch-all
would shrink. It did: `reason='provider'` is **75.2% of all 3770 recorded
decisions but 48.3% of the 913 since 12 August**, and the reasons that replaced
it are informative ones (`topic_miss` 17.7%, `cadence_block` 16.0%). The split
worked and the remaining half is not evenly spread — it sits in **nine cues that
never got a `note_decline` call**, each of which is armed on all 96 instrumented
turns:

| cue | armed | surfaced | declines via catch-all |
| --- | --- | --- | --- |
| `associative_wander` | 96 | 5 | 49 of 91 (54%) |
| `concept_hypothesis` | 96 | 1 | 50 of 95 (53%) |
| `curiosity_gradient` | 96 | 9 | 48 of 87 (55%) |
| `dormant_interest` | 96 | **1** | 52 of 95 (55%) |
| `interest_drift` | 96 | 8 | 50 of 88 (57%) |
| `knowledge_gap_notice` | 96 | 21 | 42 of 75 (56%) |
| `long_arc_callback` | 96 | 7 | 51 of 89 (57%) |
| `self_callback` | 96 | 2 | 53 of 94 (56%) |
| `shared_ritual` | 85 | 2 | 46 of 83 (55%) |

By contrast `tension` surfaced 13 of 13 and `turning_over` 6 of 6 with no
declines at all, which is independent confirmation that H10 landed. The
`dormant_interest` line is the one worth a second look on its own terms — 96
armings and a single surfacing, after H4(a) had already fixed the unreachable
K18 lull band it was waiting on.

The work is mechanical: walk the nine providers in
[`inner_life_part2.py`](../../app/core/session/inner_life_part2.py) and
[`part3`](../../app/core/session/inner_life_part3.py), and replace each silent
`return ""` with `note_decline(self, cue, REASON_*)` using the reason constants
that already exist in
[`cue_accounting.py`](../../app/core/proactive/cue_accounting.py). No new
concepts, no behaviour change, and it is a prerequisite for judging **K92**:
an arbiter that ranks candidates cannot be evaluated when half the reasons a
candidate withdrew are recorded as "the provider said no".

---

## The ten recurring shapes

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

H19 sharpens this: logging the empty result is not enough if the line cannot
distinguish *kinds* of empty. Both broken workers announced "no recent turns"
hourly for months, which was true, and which is also exactly what a quiet
evening looks like. **Corollary: when a pass reports nothing to do, it must
also say whether its inputs were reachable.** One `COUNT` on a path that has
already decided to do nothing is always affordable.

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

**8. A unit of work that cannot answer the question asked of it.** Shape 7 one
layer down: there the signal was empty, here the *unit* is the wrong shape, so
no amount of care downstream can help. H20's extractor pulled spans — `2026`,
`The Rent` — and the verifier asked "true or false?", which is not a question
about a noun phrase. Every stage was individually correct, the offsets needed
to recover the sentence had been carried since day one, and a wrong answer
overwrote a memory. **Rule: for any pipeline that extracts a unit and then
reasons over it, write the unit down next to the question and check the
question is answerable. Do this at design time — it costs one line and it is
invisible in every test that stubs the reasoning step.**

**9. A read that answers a nearby question.** The predicate is right, the
thresholds are reachable, and the query returns the wrong set — because a pool
with a state machine offers "available", "not on cooldown", "not expired" and
"exists" under similar names, and they agree almost always. H28's pruner asked
`pending` when it meant `live`, which held until a *second* consumer began
spending the same seeds on its own schedule; K9's block and K52's ledger were
built a year apart and neither knows the other exists. **Rule: when a predicate
asks "does X still exist", check what the read filters on, and where a resource
has two consumers, write down which one is allowed to retire it.**

**10. Two clocks, and the faster one owns the lifetime.** A mechanic accumulates
on a slow clock — pressure over days, standing over weeks — while the lifetime of
the thing accumulating is bounded by a resource on a fast one. H29's wants grow
at 0.25/day toward a 0.7 bar, and each one dies with the cue it was minted from
after that cue's two allowed showings, median 2.9 hours. Every threshold is
reachable in principle and every stage is individually correct, so the feature
reads as conservative rather than broken and no log line is wrong. **Rule: for
anything that accumulates, write down what can delete its unit before the
accumulation completes and compare the two timescales out loud. A unit derived
from a shared resource should copy it, not point at it.**

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

### Outcome: the instant face, and it has more range than his

`_sample_cluster_affect` now feeds the aiko map from the per-turn
`reaction_affect_target()` point rather than the smoothed global `AffectState`,
and takes the reaction tag as an argument so the two halves of the sampler read
the same turn. The docstring carries the reasoning, because the wrong call is
the more natural-looking one.

Measured on the live map, 38 clusters:

| | before | after |
| --- | --- | --- |
| aiko valence range | +0.026 … +0.222 | **−0.333 … +0.800** |
| aiko arousal range | 0.433 … 0.566 | **0.388 … 0.716** |
| aiko bucket spread | `neu/mid`, 100% of 36 | of the 9 annotated: `pos/mid` 5, `pos/high` 3, `neu/mid` 2 |
| user valence range (unchanged feed) | −0.112 … +0.400 | −0.112 … +0.400 |

**Her map is now wider than her reading of his**, which is the right way round
for a character with an interior: she has topics she dislikes (a negative
cluster exists at all, for the first time) and topics that light her up. The
concern about the impulse table topping out at ±0.18 did not materialise — the
EWMA of a *varying* per-turn signal accumulates, so the range comes out well
past any single tag's reach.

One qualifier that belongs with H14 rather than here: only 9 of the 38 clusters
carry the ≥3 `valence_samples` the diary annotation requires, and those 9 span a
narrower 0.407 … 0.630. The affect *feed* is fixed; the annotated *subset* is
still small because rows written under the old feed had to re-earn the floor.
That number should keep climbing on its own.

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

### Outcome: it renders, and it is never pinned

Re-measured before changing anything, and the figure had got worse rather than
better: **0 of 14,240 concept-lane surfacings**, with the `tension_block` and
the tension cue firing on *the same* 25 of 231 instrumented turns — so there was
no second channel, and 89% of turns carried eight boundaries and no ambivalence
at all. The same read shows what fills the lane instead: `boundary` 28.6%,
`identity` 23.0%, `value` 14.7%, `affective` 7.1%.

*Deliberate* was no longer *right*. `static_render=True` on the tension kind
clears all four exclusions at once, since `_add_scored`, the hypothesis lane
filter and `_openness_picks` share `renders_in_static_block()`. Tension is
already `role=ROLE_GENERATIVE`, so it becomes eligible for
`concept_flex_generative_floor` (1) immediately — that *is* the "one guaranteed
slot per turn" this entry asked for, with no new selection code, and L40
habituation plus L41 reason-conditioned phrasing participate automatically once
a kind renders.

**The half that stays is the pinning.** `ConceptKind` gained a `pinnable` flag,
`False` for tension, so the L28 openness reserve will not hold a friction open
regardless of cosine to the live turn. Rendering and pinning are different
promises and tension wants opposite answers to them: a friction should be raised
when the turn is actually about it and left alone otherwise, and pinning one
into every turn is precisely the nagging L12's cooldown exists to prevent. The
openness reserve's own notes had flagged that as the failure mode if the render
carve-out were ever relaxed.

Then the double-surface. The T6 cue was written when it was a tension's *only*
voice; now it steps aside for one the concept lane has already claimed this turn
(`_last_context_concept_ids`, cleared at the top of each turn so a stale claim
cannot silence the cue on a turn the lane never ran). Yielding deliberately does
not consume the cue — the point is that a friction is raised **once**, carefully,
and raising the same one twice in a single assembly is the nagging the whole
design guards against.

Nag guards, all pre-existing: the generative floor of 1 caps it at one per turn,
`concept_surfacing_habituation_*` rotates which one, and
`tension_cue_cooldown_days = 6.0` still paces the cue.

**Sequencing note.** H16 ships first and needs two days of merge budget. On
today's register this render change would surface eleven ways of saying the same
thing, which is exactly the repetitiveness the original exclusion was protecting
against — the exclusion was the wrong fix for a real problem.

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

**Adjacent, 12 Aug.** The immersion backlog's
[H26](shipped/immersion.md#h26-caught-mid-something--she-was-busy-when-you-opened-the-app)
shipped the other half of "a day of her own": an away beat
can now be left *running*, so a return catches her mid-something instead of
hearing a completed errand. That does not move any number in this entry —
concepts are still 67% relational — but it is worth recording here because the
two problems get confused. This entry is about what she *believes* having no
independent subject; H26 is about what she *does* having no present tense. A
life she is in the middle of is a cheaper source of "what did you do today"
than waiting for `pursuit` to clear its evidence gate, and the two compound:
an interrupted beat she returns to is exactly the kind of repeated own-material
note that eventually feeds a pursuit.

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

*Superseded — H9 and H10 both shipped. Kept for the reasoning, which held up:
H9 was the difference between a companion who has feelings about things and one
who reports a uniform mild pleasantness, and several other findings (H14, the
blandness of the affective concepts, L13's whole premise) were downstream of it.
H10 turned out to be a flag rather than new code, as predicted, though it needed
H16's dedupe first — see the current ordering below.*

---

## Suggested order

Refreshed after the wants-ledger pass (H28). Roughly by value per unit of risk,
across both parts.

**Waiting on measurement, not on work.** These shipped and their verification
needs days of accumulation rather than another change. Re-check before doing
anything else, since three of them alter the inputs to everything below:

> **First reading, 12 Aug 20:00** (253 recorded turns, latest message 16:05).
> Two of the five have already moved: `tension_block` fires on **25 of 253**
> turns and the `tension` cue surfaced **27 times in three days**, both from a
> standing zero, so H10 is confirmed and H16's dedupe did not starve it.
> `concept_hypothesis_block` reached **4 of 253** — off zero, but
> `hypotheses.asked_count` is still 0 across all 16 rows, so the cue is
> rendering without yet converting into an ask; that is the H7 signal to keep
> watching. The other three are simply too young to read: `kind='conduct'` is
> still 0 rows (H1), and the wants ledger's strongest want sits at **0.167**
> against the 0.35 soft bar, so `topic_appetite_block`,
> `thread_ownership_block` and `pursuit_lean_block` remain at 0 exactly as H28
> predicts for this point on the clock. **Nothing here is a defect yet.** The
> corollary matters more than the numbers: the concept and cue lanes are
> mid-experiment, and changing their inputs before ~15 Aug destroys the
> attribution these five entries were designed to produce.

- **H1** — the latch is open; `kind='conduct'` should go non-zero on the next
  weekly pass. If it does not, look at the detector.
- **H16** — tension candidates per pass went 0/3/6 → 3/6/9 by block. Re-run the
  population query after two days of merge budget; 122 rows should fall toward
  the ~15–20 distinct frictions the labels suggest.
- **H10** — tension should move off 0 of 14,240 concept-lane surfacings without
  displacing `affective` (H11's ratio is still unsettled). Land it *after* H16
  has drained, or she says the same thing eleven ways.
- **H7** — `provider` should drop below half of `concept_hypothesis` declines
  now the reasons are split, the shelf should stop growing, and the first
  non-zero `hypotheses.asked_count` is the signal the loop closed at all.
- **H28** — ~~three signals, and they arrive on a clock: a want should cross 0.35
  in 19 h and 0.7 in 53 h~~ **read on 13 Aug and the clock did not run** — the
  strongest want is 0.25, no live want is older than 11 h, and both blocks are
  still at 0 of 290. The fix was correct but was one of three drains; the other
  two became **H29**, now shipped.
- **H29** — the same clock, third attempt, and the last one that can be blamed
  on a read: wants no longer die when a seed is refused, and no source holds
  more than half the ledger. `topic_appetite_block` and the `wants_block`
  imperative should leave zero by ~16 Aug. If they have not while wants are
  visibly older than three days, the soft band spending `ranked[:2]` is the
  remaining suspect (H29's withdrawn drain 2). Watch the opposite failure too —
  four seed wants that nobody acts on can now hold their half of the ledger for
  the full 14 days.
- **K85 pursuits** — *not* a defect; the five seeds were filed 9 August with a
  7-day age floor, and three have already accrued sources. `pursuit_lean_block`
  should leave zero on its own after 16 Aug. If it has not by then, the seeds
  are not being reinforced and the away-beat path is the thing to look at.
- **The K90 lead/follow diff** — **read on 13 Aug across 320 post-ship turns and
  the answer is no.** Splitting the corpus on the 9 Aug ship date: anaphoric
  openers **18% → 18%** (K88's own target, and the one metric here that reply
  length cannot distort), own material **77% → 71%**, echo 19% → 20%, median
  words 23 → 31, ends-on-a-question 18.1% → 6.2%. She writes 35% more, about his
  subject, and asks about it less. Two families have now shipped against this
  number without moving it, which is the evidence base for the third pass
  (**K92–K95** in [`patterns.md`](patterns.md)) starting from a different
  diagnosis: not permission, not inventory, but that ~10 independent steer blocks
  totalling 0.7% of a 74,000-character prompt cannot outvote the follow prior, and
  that neither *following* nor *holding back* has any representation at all.

**Then, in order:**

1. **H11** — the ratio itself, and now the decision is live: H10 just put a
   generative kind into the lane boundary has been dominating (28.6% of
   surfacings against `affective`'s 7.1%). Judge it on the post-H10 mix.
2. **H4(a)** — the wedge behind `dormant_interest` and `self_callback`. The
   reason split from H7 is the instrument this was always missing; read it
   before theorising — but note **H30**: both of those cues are among the nine
   that still route ~55% of their declines to the catch-all, so H30 is the
   cheaper prerequisite and `dormant_interest` is now at 1 surfacing in 96
   armings even after its lull band was fixed.
3. **H7 remainder** — `concept_hypothesis`'s last place in `GAP_CUE_ORDER` and
   its K47 asymmetry. Deliberately deferred: both are defensible, and the split
   reasons will say whether they matter. Note the order gained a member on
   12 Aug (immersion H26's `caught_mid_activity`, ahead of `away_activities`),
   so the queue behind it is one deep — read the decline reasons against the
   post-12-Aug window only.
4. **H12**, **H13**, **H14** — re-measure after the above settle; all three have
   inputs H9, H10 and H16 change. H14 in particular: only 9 of 38 clusters yet
   carry the valence samples the diary annotation needs.
5. **H8** — mostly decisions to record rather than code to write.

Shipped since this list was written: **H3**, **H5**, **H6**, **H15** (12 Aug —
its measurement is in the entry, and it turned out to be a fix rather than the
judgement call it was filed as), and immersion's **H25**/**H26**. **H2** is
still waiting on H1 minting its first row, since a conduct pass with no output
has nothing to gain from better thresholds. **H17** is the one open entry
deliberately *not* being touched: it changes what `topic_appetite_block` counts
as a lull, which is the block H29's measurement is watching.

**Added 13 Aug** while reading the two watch items above: **H29** (the wants
ledger's pressure mechanic cannot run, because a want dies with the cue it was
minted from after that cue's two showings) and **H30** (the remaining half of the
`provider` catch-all, concentrated in nine cues). H29 is the higher-severity of
the pair and gates K54, the `wants_block` imperative band, and **K93**'s
substance reservation; H30 is an afternoon and unblocks judging **K92**. Neither
touches the concept or cue lanes' *inputs*, so both are safe to do before the
attribution window closes — H29's remedies are all inside the ledger, and H30
only adds reason strings to declines that already happen.

**H29 shipped the same day** (see its Outcome section): the prune now asks for
settlement rather than availability, and `wants_per_source_cap=4` stops the seed
feeder owning the ledger. It is now a measurement item rather than a work item —
the earliest honest read is 16 Aug. **H30** is the open one.
