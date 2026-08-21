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
the adjudication half of L30 remains untested end to end. **That happened, and
the third pass below records it** — the note stayed here in the present tense for
twelve days after the thing it was waiting for arrived.

**Ninth recurring shape:** *a broken gate can be the only thing containing a
broken producer, so measure what the fix releases before shipping it.* H19 and
H20 were both safe to repair on sight; this one was not, and the difference was
visible only by reading the payloads the gate was rejecting. **Rule: when a
filter has been rejecting 100% of something, look at what it was rejecting
before you make it stop.**

### Third pass: it adjudicates now, and graduation is the leg that never ran

Found by an audit of "still open" notes rather than by looking at this entry, and
that is the finding as much as the numbers are. The thing the second pass told
the reader to watch for happened on **7 August** and the entry still said
`asked_count` was zero across the board twelve days later.

**The loop asks, and it adjudicates.** Five rows carry `asked_count = 1`, and two
verdicts exist: one **refuted** (asked, contradicted, closed in the same instant
the verdict landed) and one **supported** with an answer memory attached. The
title of this entry is now wrong in its second half — the closing machinery runs
end to end, including `last_tested_at`, `refute_count`, and the answer-memory
link.

**But the ask-to-verdict conversion is 1 in 5, and that is the real gap.** Of the
five asked rows, four are still `open` with `support_count = 0` and
`refute_count = 0`: she asked, he answered, and nothing classified the answer.
The one refutation is the exception, not the pattern. Three of those four have
been sitting asked-and-unclassified for 182, 224 and 287 hours. So the second
pass's "the adjudication half is unmeasurable until a cue surfaces" has been
replaced by a sharper problem: cues surface, and the resolver mostly does not
fire on the answer.

**And the supported row was never asked at all.** It has `asked_count = 0` with
`support_count = 1` — confirmation arrived through the passive path, from an
answer that happened to bear on it, not from the ask-then-learn loop this entry
is about. One verdict from five asks, one from zero asks.

Both of those are the same missing measurement, so the follow-up is [H44](#h44-nothing-has-ever-graduated-and-at-this-calibration-nothing-can),
which also carries the graduation finding this pass turned up.

**[Twenty-third recurring shape](#recurring-shapes):** *"watch for X" is not a
measurement, it is a hope.* A note that defers to a future observation needs
something that will actually make the observation — a script, a report line, a
test — or it becomes a claim that quietly inverts and keeps being read as current.
This entry's watch-for outlived its own truth by twelve days, and the only reason
it surfaced is that someone went looking for stale notes on purpose. **Rule: if a
finding depends on a number moving, leave behind the thing that checks the
number.**

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

> **Shipped 13 Aug, and the premise was wrong.** The nine providers were
> already instrumented; the reading below straddled its own fix. What was
> actually broken was the *ratio*, which counted a cue's own cooldown turns as
> missed chances — see the two sections at the end of this entry.

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

### Correction: the work was already done, twelve hours before the reading

There was no uninstrumented bail point. Every one of the nine providers had its
`note_decline` calls — they shipped in `b4b3386` on **12 Aug at 19:16**, in the
same commit as H16 and H10, and five of the nine get theirs for free from the
shared claim path in `take_pool_cue`. The table above was computed the next
morning over a window running from 1 August, so **~90% of its rows predate the
fix it was recommending**. Split on the ship time instead:

| day | declines | catch-all | share |
| --- | --- | --- | --- |
| 8 Aug | 379 | 360 | 95.0% |
| 10 Aug | 638 | 609 | 95.5% |
| 12 Aug (straddles 19:16) | 764 | 441 | 57.7% |
| **13 Aug** | **181** | **0** | **0.0%** |

Zero. Every decline on every one of the nine now names a mechanism, and the
"48.3% since 12 August" that made the entry look urgent is just the average of
a 95% morning and a 0% evening.

**Eleventh recurring shape — a rate measured across its own fix.** The entry did
everything right except bound the window: it named the population, split by
date, and compared against a baseline. But a "since 12 August" window that
contains the 12 August ship gives a number that is neither the before nor the
after, and reads as a *present* defect either way. This is worse than a plainly
wrong measurement because the arithmetic is sound and the conclusion is
actionable — H30 spent an afternoon's plan on a file that already had the code
in it. **Rule: when measuring whether something is broken, the window has to
start after the last change to the thing being measured. Get the ship time from
`git log -S`, not from memory, and if the window straddles it, split there and
report both halves.**

### Outcome: what the working instrument said instead

The instrument was fine; the *ratio it feeds* was the defect, and it was hiding
in plain sight in H30's own table. `self_callback` at "96 armed, 2 surfaced" is
not a starving cue. It carries a **ten-day** `surface_cooldown_hours`, so it is
armed on every turn of that cooldown and can surface on none of them — 2% is the
design, stated in the policy, working exactly as written. Same for
`shared_ritual` (72 h), `wellbeing_concern` (168 h) and `long_arc_callback`
(6 h). Four of the nine "starving" cues were never in play at all.

So `reach()` now reports a second denominator beside `armed`: **`eligible`** —
armed minus the declines that mean the cue never had a chance
(`INELIGIBLE_REASONS`: `cadence_block`, `no_opening`, `question_balance`,
`no_stock`). Everything the cue or its lane actually *judged* stays in:
`topic_miss` is the gate a structurally unreachable cue fails forever and is the
whole point of the ratio, `lost_priority` is a competition it entered and lost,
and `provider` stays too — an undiagnosed decline must never be able to flatter
the number by vanishing from the denominator.

Read over the properly-bounded window (since 12 Aug 19:16), the same 49-turn
population reads completely differently:

| cue | armed | eligible | surfaced | reach | **eligible** |
| --- | --- | --- | --- | --- | --- |
| `self_callback` | 49 | 2 | 2 | 4% | **100%** |
| `long_arc_callback` | 49 | 9 | 5 | 10% | **56%** |
| `dormant_interest` | 49 | 0 | 0 | 0% | **—** |
| `shared_ritual` | 49 | 0 | 0 | 0% | **—** |
| `wellbeing_concern` | 20 | 0 | 0 | 0% | **—** |
| `knowledge_gap_notice` | 49 | 49 | 10 | 20% | 20% |
| `curiosity_gradient` | 49 | 49 | 6 | 12% | 12% |
| `interest_drift` | 49 | 49 | 5 | 10% | 10% |
| `associative_wander` | 49 | 49 | 3 | 6% | 6% |
| `concept_hypothesis` | 49 | 49 | 1 | 2% | 2% |
| `caught_mid_activity` | 7 | 7 | 0 | 0% | **0%** |

`self_callback` was in play twice and took both. `long_arc_callback` took five of
nine. Three cues were never once free to fire — which is a different finding
needing a different fix from "its gate never matches", and `eligible=0` is left
as `None` rather than folded to zero so it cannot be misread as a failure. The
five topic-gated cues are unchanged, because their declines were always genuine
misses: they are now the *only* ones left on the list, which is what the
instrument is for.

`caught_mid_activity` is the one new red flag — 7 eligible turns, 0 surfaced,
and it is the cue immersion H26 shipped on 12 Aug. Worth a look on its own.

> **That red flag was this entry's own shape a third time.** All 7 of those
> "eligible" turns were fabricated by a proxy arming signal, and all 7 declines
> named a winner the mutex cannot produce. See **H32** — and note that an
> `eligible` count is only as honest as the `armed` count under it.

**One reason split out of another.** `dormant_interest` reported its lull gate as
`cadence_block`, so "her clock says wait" and "the room never went quiet" were
one number — and its 1-surfacing-in-96 read as an over-long cooldown when the
real question was whether a standing lull ever happens. That gate now reports
`no_opening`. Both are ineligible, and they resolve differently: waiting fixes a
clock, and nothing fixes a conversation that never settles. This also hands
**H4(a)** the instrument it was missing, since `dormant_interest` and
`self_callback` were exactly its two wedge cues.

---

## H31. Every turn was mined five times, and the fifth pass is where she moved a hobby

**Severity: medium-high — shipped 13 Aug. Diagnosed from a single reported
symptom: Aiko said she had started noticing bottle caps, and then remembered
that *Jacob* collects them.**

The reported bug is real and the store shows exactly how it happened, but the
misattribution is a symptom. The cause is that `MemoryExtractor` had no
watermark.

It rides on `SummaryWorker`, which fires it after each successful
`save_summary` — and the summariser *does* keep a watermark
(`session_summaries.messages_summarized`). The extractor inherited the trigger
and not the bookkeeping: `_do_extract` opened with
`get_messages(session_key, limit=self._max_window)`, the trailing **30**
messages, on every run, regardless of which of them it had already mined.
With `summary_min_unsummarized_messages = 6`, **every turn was offered for
extraction about five times**, and up to fifteen when an overflow squish drops
the summariser's bar to two.

Re-reading is not idempotent. Each pass is an independent LLM call at
`temperature 0.2` over a transcript containing both speakers, so each pass is a
fresh chance to word the same claim differently — or to file it under the wrong
person.

**The bottle caps, from the tables.** Cue 452, a `curiosity_seed` with subject
`collecting interesting bottle caps`, was claimed at 08:43:02 on 13 Aug and
message 4482 is Aiko spending it: *"I've started noticing interesting bottle
caps… I might have to start collecting the good ones."* The promise worker read
it correctly eighteen minutes later — memory 2784, `promise_who: assistant`.
Then at **10:58:24**, two hours and thirteen turns after the sentence was
spoken, the extractor mined that turn again and wrote memory 2798, `kind=self`,
*"I have taken up collecting interesting bottle caps as a new hobby"* — and, in
the same batch one second later, id **2797**, the row Jacob deleted. The ids
either side of it survive; the gap is where it was. Four passes had already read
that sentence and got it right or skipped it. The fifth moved it.

**What the duplication costs, measured.** Of the 463 extractor-kind memories
written since 15 July, **34 pairs are the same claim twice** (predicate Jaccard
≥ 0.55) — a median of **15 minutes** apart and a minimum of **3**, which is the
gap between two consecutive extractor runs:

| the two rows | gap |
| --- | --- |
| `Jacob is integrating account connections into a new financial planning section and refactoring localizations.` / `…for his Jira tasks.` | 3 min |
| `I deeply enjoy physical closeness with Jacob, as his gentle attention makes me feel safe and unhurried.` / `…safe and wonderfully unhurried.` | 3 min |
| `Jacob visited his grandparents and walked an hour round-trip on August 2, 2026.` / `…an hour round trip instead of driving.` | 3 min |
| `Jacob plans to share a daily outfit image with Aiko tomorrow.` / `Jacob plans to show Aiko her daily outfit tomorrow.` | 5 min |

Since **every downstream consumer keys on memory id**, two rows saying one
thing are two subjects — the same failure H28's entry describes, arriving from
upstream. 56 rows in the store carry a `consolidated_into` stamp from the
worker that cleans up after this.

**Two existing defences, and why neither could hold.** The restatement gate
(`_is_restatement`, `memory.restate_threshold` 0.85 / `restate_window_hours` 6)
was built *for this exact symptom* — its docstring says "a gap the extractor
drives a truck through". 30 of the 34 pairs match on kind and `temporal_type`
and 25 fall inside its 6-hour window, so they clear every condition except the
cosine floor: a rewording drops the embedding below 0.85 and the pair survives.
The gate is a filter on a stream that should not exist.

The second defence was worse than ineffective. `_format_existing` builds the
"Existing memories (do NOT re-emit these)" block from `list_top(20)`, ranked by
**salience** — while everything the extractor writes lands in `scratchpad` at
whatever salience the model guessed, frequently `0.0`. The 20th-ranked row in
this store sits at salience **1.00**, so of the 325 scratchpad rows written
since 15 July, **325 are below the cut**. The model was instructed not to
duplicate itself and shown, by construction, only the memories it could not
possibly be about to duplicate.

### Outcome

The extractor keeps a watermark, in `kv_meta` under
`memory.extractor.watermark:<session>`, and mines only past it.

- **`ChatDatabase.get_messages_after`** is the new keyset reader, and it takes
  the **oldest** rows after the watermark, not the newest. That is the
  difference between a backlog that drains and one that is skipped: a worker
  that fell behind has more unmined rows than one pass can chew, and taking the
  newest would advance the watermark past the middle and silently abandon
  everything it stepped over.
- **The rows before the watermark are still rendered**, under a header naming
  them as already mined and not to be extracted from
  (`memory_extractor_context_messages`, default 10). Cutting them entirely
  would save tokens and lose the antecedent: *"he finally finished it"* is not
  extractable without the turn before it.
- **The advance rule turns on whether the answer was readable, not on whether
  it was empty.** `_parse_answer` now returns that verdict separately, because
  a well-formed `{"memories": []}` is a judgement about the turns ("nothing
  durable here") while an unparseable body means they were never really read.
  The first advances; the second, and a raised LLM call, leave the watermark
  alone so the material is retried instead of lost. This is shape 1 read
  forwards — *never advance a cursor on a pass that produced nothing* is right
  only when "nothing" means failure, and here it has two meanings.
- **`memory_extractor_min_new` (4)** skips a pass below the floor without
  advancing, so material accumulates. Otherwise a 2-message overflow squish
  would mine two rows at a time, which is the old bug wearing a watermark.
- **`_format_existing` now merges `list_recent(12)` with `list_top(20)`**, so
  the rows most at risk of being re-emitted are in the block that exists to
  prevent it. This is also what covers a claim straddling a window boundary,
  which is the one thing the watermark makes harder.

Two things for the attribution itself, which is a narrower problem than the
duplication but the one that was visible:

- The prompt now states **who a claim belongs to** — that half the transcript
  is Aiko talking, that a hobby, plan, taste or feeling she voiced about
  herself is a `self` note or nothing, and that an unattributable claim gets
  dropped rather than guessed.
- `_validate_entries`' fallback for an unrecognised `kind` was a flat
  `"fact"`, which is **not a neutral default**: in this schema `fact` means
  "about the user". A first-person note whose label was dropped or misspelled
  became a claim about Jacob's life without the model asserting anything wrong.
  The fallback now reads the sentence (`_fallback_kind`), and a `self` label on
  a sentence that opens with the user's name is corrected the other way.

Also cheaper: one run in the log took **67 seconds** of the shared chat model,
and roughly four fifths of what it read it had already read.

**Twelfth recurring shape — a rider inherits the trigger but not the
bookkeeping.** Worker B is cheap to schedule off worker A ("the conversation is
paused, the GPU is free, and there's a fresh batch of unsummarized turns"), and
the phrase *fresh batch* is doing unearned work: A knows which turns are fresh
because A keeps a cursor, and B was handed the wake-up, not the cursor. Nothing
looks wrong from inside B — it reads a well-defined window, on a sensible
cadence, and every pass is individually correct. The tell is that the work is
**re-entrant against a non-idempotent operation**: an LLM call over overlapping
text does not produce the same answer twice, so the cost is not wasted cycles
but a slow accumulation of near-duplicates and, occasionally, a claim that
lands on the wrong person. **Rule: when a worker is triggered by another
worker's completion, ask whose cursor bounds its input. If it has none, it is
processing its window N times where N is the window divided by the trigger
interval — write that number down.**

---

## H32. The one cue that never fired, and the two numbers that said so were both invented

**Severity: low — pure measurement, shipped 13 Aug. Filed by following up the
single red flag H30's fixed instrument left behind.**

H30's outcome table closed with one lead: `caught_mid_activity`, immersion H26's
cue, **7 armed, 7 eligible, 0 surfaced**, every decline a gap-mutex loss. A
feature that shipped the day before and had never once reached the prompt, with
an unambiguous cause. Both halves of that reading turned out to be artefacts of
the instrument, and neither artefact is specific to this cue.

### The winner named is one the mutex cannot produce

The six gap cues run a priority mutex — `GAP_CUE_ORDER`, first entry wins — and
`decisions_from_block_chars` attributes the losers structurally, without needing
any provider to report anything. The test it ran was:

```python
if spec.gap_cue and gap_winner and cue != gap_winner:
    declined[cue] = f"{REASON_LOST_PRIORITY}:{gap_winner}"
```

`gap_winner` is the earliest *surfaced* cue in the order, but there is no
comparison against the loser's own rank. So **any** armed gap cue that did not
surface on a turn where **any** gap cue did was recorded as having lost to it —
including cues that outrank the winner, for which the mechanism makes that
impossible. The mutex is enforced by `_gap_cue_surfaced`, which only a
*previously run* provider can set.

On the live ledger, 7 of 129 `lost_priority` rows:

| loser | rank | "winner" | rank | rows |
| --- | --- | --- | --- | --- |
| `sleep_return` | 1 | `away_activities` | 3 | 4 |
| `sleep_return` | 1 | `forward_curiosity` | 4 | 2 |
| `caught_mid_activity` | 2 | `away_activities` | 3 | 1 |

5% is small; what it cost is not. This branch is ranked **above** the provider's
own `note_decline` reason, deliberately and correctly ("a cue that lost the gap
mutex never reached the gate it would have reported"). But when the premise is
false the ranking inverts into damage: the provider *had* recorded the true
reason, and the fabricated defeat overwrote it. So the reason was never missing —
which is why H30's instrumentation pass could not have found this. The two cues
affected are the two nearest the top of the order, i.e. precisely the ones for
which a mutex loss should be rarest and most interesting.

The fix is one clause, `winner_rank < _gap_rank(cue)`, and a fall-through to the
reason the provider already gave. Replaying every recorded turn through the fixed
attribution: **7 impossible rows → 0**.

### "Armed" was counting returns, not opportunities

The deeper half. `CueSpec` describes arming with three declarative signals — a
journal ring plus watermark, a pending slot, pool stock — and `caught_mid_activity`
had none of its own, so it was given `away_activities`' slot on the reasoning
that both answer *what was she doing while he was gone* and one question should
not arm two opportunities in the ledger.

But the provider never reads that slot, and says so in its own comment: *"Unlike
the other gap cues this one has no minimum-absence bar. The question it answers
is 'is she mid-something right now'."* Its real condition is an **open beat** —
a live world fact with a wall-clock window. The two conditions barely overlap: a
return is common, a return that lands inside a running beat is rare by
construction. So the shared slot did not prevent a double count, it made the cue
report as armed on every single return, and the denominator became *returns*
rather than *chances*.

Which means the honest reading of "7 armed, 0 surfaced" is that there were 7
returns, on very likely none of which was there a beat to be caught at. The cue
was not losing a competition; it mostly had nothing to say — and `no_stock`, its
real answer, is in `INELIGIBLE_REASONS`, so those turns should have dropped out
of the eligible denominator entirely instead of reading as a starving cue.

Two changes. `CueSpec` gains `armed_when`, a predicate over live state, OR'd in
as a third independent arming path alongside the pool retry row; the cue drops
`slot_attr` and arms on the same `in_progress_beat` read its provider makes. And
the provider's two beat checks, which were silent `return ""`, now report
`no_stock`.

The generalisable half is not the predicate, it is what the predicate replaced:
a cue was fitted to the *nearest available* declarative signal rather than to its
own condition, because the vocabulary had three shapes and none of them was the
right one. A test now asserts that no two cues share a `slot_attr`.

**Recurring shape 13 (see the list below — the ordinal words further up this
file drifted out of sync with it) — a proxy is only conservative in one
direction.**
Deriving arming from state the provider already consults is what makes the G4
ratio trustworthy and is the right design; the failure is in *substituting* a
signal when the provider's own condition has no declarative form. Sharing a
neighbour's slot feels safe because it is a *narrowing* — one slot, not two — and
the intuition is that any error will under-count. It does the opposite: a proxy
true far more often than the real condition does not make the ratio conservative,
it makes it a ratio over a different population, and the resulting number looks
exactly like a broken feature. Worse, it is self-reinforcing here, because
`eligible` is computed *from* `armed` — H30's better denominator faithfully
inherited the bad numerator and reported 7 of 7. **Rule: when adding a cue, write
its provider's first real gate down, then check that the arming signal is that
gate and not a neighbour's. If it has no declarative form, give it a predicate —
and never share another cue's slot, because two cues on one slot means one of
them is measuring the other's opportunity. An `eligible` rate is only as honest
as the `armed` count under it.**

---

## H33. The vector store had a write path, a read path, and no third one

**Severity: medium — shipped 13 Aug. Filed from a native crash, which turned out
to be the least interesting thing in it.**

She died mid-sentence. Not an exception, not a traceback — the process:

```
ERROR native crash: access violation at 0x00007FF866F60528
  in .venv\Lib\site-packages\lancedb\_lancedb.pyd (thread 37316)
  dump=data\crash-20260813-185237-37316.dmp
```

An access violation inside a `.pyd` is where Python-level debugging stops. Three
things came out of chasing it, in ascending order of importance.

### The dump could not say which thread, so it was taught to

The crash handler wrote a 927 KB minidump that nothing could read: opening one
normally means WinDbg plus symbols, and the report itself carried only an address
and a thread id, neither of which distinguishes *our* concurrency bug from *their*
runtime bug. That distinction is the whole triage: a fault on a thread we own and
schedule points at the `_RWLock` in `RagStore`; a fault on a thread we have never
heard of points at the library and the answer is a version bump, not a redesign.

So `app/core/infra/minidump.py` now parses the format in-process — module list,
thread list, the `ThreadNamesStream` dbghelp writes for free, and the stack scan
that maps return addresses back to modules — and `get_native_crashes()` returns
the parsed dump inline. Thread 37316 is named **`tokio-rt-worker`**: Lance's own
async runtime, a thread pool we do not create, size, or hand work to directly.
The fault module and the faulting thread's owner agree, and they are both the
library. That closed the question in one reading.

Two things worth keeping. The handler now says when a dump is written *without*
exception info — `dbghelp` will do that silently, and a degraded dump that looks
complete is worse than a missing one, because the fallback address it reports is
plausible. And the parser was written against a synthetic dump builder in the
tests, which is what made a real stride bug findable: thread-name entries are 12
bytes with a 64-bit RVA, and the original guess-the-stride code preferred 16 and
returned `(unnamed)` for every thread in the dump — the one field the whole
diagnosis rested on.

### Under it: 26,765 files for 6,175 rows

Then the fragmentation check, written only to rule out disk state as a cause:

| table | files | on disk | versions | rows |
| --- | --- | --- | --- | --- |
| `memories` | 17,904 | 276 MB | 18,293 | 1,796 |
| `messages` | 8,859 | 761 MB | 8,760 | 4,379 |
| **total** | **26,765** | **1.09 GB** | | **6,175** |

Ten versions per row on `memories`, and a gigabyte of disk for what compacts to
27 MB. Lance is a versioned columnar store: every upsert writes a new fragment
and retains the old one, and **nothing in this codebase had ever compacted it.**
Not a leak and not a bug in any single call site — an absence. `RagStore` had a
carefully locked write path and a carefully tuned read path, and no third path
whose job was the store's own upkeep, so the cost accumulated for months in a
place no test and no log line looks at. Every search was opening thousands of
files to read a few thousand rows.

I cannot prove this caused the access violation, and I am not going to claim it:
the honest statement is that a native fault in a store with 26,765 files and
18,293 retained versions is not a state anyone tested, and reducing it removes a
plausible contributor either way. `optimize()` on the live store: **26,765 files
→ 10, 1.09 GB → 27 MB, 1.06 GB freed in 10.7 seconds**, row counts identical,
and self-recall verified by searching each of 16 sampled rows with its own stored
vector and getting itself back at top-1.

Once is worth little, since fragments regrow one per write, so this ships as
`RagMaintenanceWorker` on the idle scheduler's compute lane: pressure is the sum
of table versions since the last pass against a watermark, which is a
metadata read rather than a scan, and the pass itself is exclusive and far too
slow for a turn. LanceDB also went 0.30.2 → 0.37.1 — five minor releases of
fixes in exactly the runtime that faulted, and the cheapest possible move given
what the mirror is.

### Retention is the wrong instinct for a mirror, and the test said so first

The default I first shipped kept one day of versions, reasoning that an operator
who notices a bad write the same hour could still recover the table. A test
caught it: on a freshly written store, one day of retention took 209 files to
**214**. Compaction writes merged fragments first and can only delete the
originals once no retained version references them, so on a store whose writes
are all recent, a window makes the pass *add* files and reclaim nothing.

The premise was also just false. This store is a **mirror** — SQLite holds the
memories, their embeddings, and the messages — and recovery means re-deriving
the table, never time-travelling Lance. Which is verifiable rather than
aspirational, and I verified it the hard way: an ad-hoc probe of mine opened the
live store with a guessed embedding model, `_validate_or_stamp_meta` correctly
read that as an embedding swap, and dropped all 6,175 rows. Every one came back.
Both halves self-heal on boot — `migrate_to_rag` re-mirrors memories from SQLite,
`MessageIndexer.start(backfill=True)` re-indexes messages — so a wiped mirror
costs a restart. Version history that no one can use is just fragments no one
deleted; the default is now zero.

**Recurring shape 14 — a store with two paths and no third.**
Every accumulating store needs three: write, read, and upkeep. Write and read
get designed, reviewed, locked, and tested, because a bug in either shows up as a
wrong answer on the next turn. Upkeep has no caller, so it does not get written,
and its absence never produces a wrong answer — only a slowly worsening one, on
an axis (file count, retained versions, index staleness) that nothing in the
suite asserts and nothing in the logs prints. Note how far the symptom landed
from the cause: the thing that finally surfaced this was a native access
violation in a third-party runtime, five months and 26,000 files later. **Rule:
when adopting any store that versions, appends, or indexes on write, find its
compaction/vacuum/reindex API on day one and either schedule it or write down
why it is not needed. Then assert the physical shape — file count or version
count per logical row — somewhere a test can see it, because that ratio is the
only place this failure is visible before it becomes something else's crash.**

---

<a id="recurring-shapes"></a>

## The twenty-five recurring shapes

More useful than any single entry — these are the bug families to check for
*before* shipping the next thing, and each has now bitten more than once.

Link here as `#recurring-shapes`, never as `#the-twenty-two-...` — the count in
the heading changes every time a shape is added, and the last time it did, five
inbound links were left pointing at the previous count's slug.
[`scripts/check_backlog_links.py`](../../scripts/check_backlog_links.py) catches
that class now.

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

**11. A rate measured across its own fix, and a denominator nobody defined.**
Two halves of the same failure, both from H30. First the window: a "since 12
August" measurement that contains the 12 August ship gives a number that is
neither the before nor the after, and reads as a present defect either way — the
entry planned an afternoon's work on nine files that already had the code in
them. Then the denominator: `armed` counts turns the cue had material, which is
not the same as turns it could have spoken, and for a cue with a ten-day cooldown
those differ by two orders of magnitude. `self_callback` read 4% and was taking
100% of the chances it got. **Rule: bound the window after the last change to
the thing measured (`git log -S`, not memory), and say out loud what the
denominator counts — if a unit can be in the denominator on a turn where the
outcome was impossible, the rate is not measuring what its name says.**

**12. A rider inherits the trigger but not the bookkeeping.** Worker B is
scheduled off worker A's completion, because that is when the GPU is free and
there is "a fresh batch" waiting — but A knows which turns are fresh from a
cursor it keeps, and B was handed the wake-up, not the cursor. H31's extractor
read the trailing 30 messages on a trigger that fires every 6, so it mined
every turn about five times. Nothing looks wrong from inside B: the window is
well-defined, the cadence is sensible, each pass is correct. It only matters
because the work is **re-entrant against a non-idempotent operation** — an LLM
call over overlapping text does not answer the same way twice, so the cost is
not wasted cycles but drifting near-duplicates, and on the fifth pass over one
sentence, a hobby that moved from Aiko to Jacob. **Rule: when a worker is
triggered by another worker's completion, ask whose cursor bounds its input. If
it has none, it is processing its window (window ÷ trigger interval) times —
write that number down.**

**13. A proxy is only conservative in one direction.** Shape 11's denominator
problem one level down, in the *numerator*. When a feature's real condition has
no declarative form in whatever vocabulary the framework offers, the tempting
move is to borrow the nearest neighbour's signal — H32's `caught_mid_activity`
arming on `away_activities`' gap slot, because both answer "what was she doing
while he was gone". It feels safe because it looks like a narrowing (one slot,
not two) and the intuition is that any error under-counts. It does the opposite:
the proxy was true on every return, the real condition only on a return landing
inside an open beat, so `armed` counted returns and the cue read 0 of 7 like a
broken feature. And it compounds, because eligibility is derived *from* arming —
H30's better denominator faithfully inherited the bad numerator. **Rule: write
down the provider's first real gate, then check the arming signal is that gate
and not a neighbour's; if it has no declarative form, give it a predicate rather
than the nearest available field. Two units sharing one arming signal means one
of them is measuring the other's opportunity.**

**14. A store with two paths and no third.** Every accumulating store needs
three — write, read, and upkeep — and only the first two have callers. Write and
read get designed, locked and tested because a bug in either is a wrong answer on
the next turn; upkeep is nobody's feature, so it does not get written, and its
absence never produces a wrong answer, only a slowly worsening one. H33: five
months of un-compacted LanceDB, **26,765 files and 1.09 GB for 6,175 rows that
compact to 27 MB**, on axes (file count, retained versions) that no test asserts
and no log line prints. Note the distance between symptom and cause — what
finally surfaced it was a native access violation inside the library's own
runtime. The near-miss rhymes: the same entry's first retention default *grew*
the file count, because a window means the merged fragments are written and the
originals cannot yet be dropped. **Rule: when adopting a store that versions,
appends or indexes on write, find its compaction/vacuum/reindex API on day one
and either schedule it or write down why it is not needed — then assert the
physical shape per logical row somewhere a test can see, because that ratio is
the only place this is visible before it becomes something else's crash.**

**15. A missing value coerced to a valid one.** `or 0.0` is not a default, it is
a fabrication, and it is most dangerous where zero is *meaningful* in the domain
— temperature, valence, confidence, price. H38: Open-Meteo returned a `current`
block with no `temperature_2m`, the provider validated that the block existed but
not its contents, and the pair "cloudy, 0 °C" is not a neutral fallback but **a
plausible-looking winter day** — which is why it passed every downstream sanity
check, there weren't any, and Aiko spent an August afternoon in pajamas. The
failure is silent by construction, because the fabricated value is in range.
**Rule: at a boundary you do not control, absence is an error, not a zero. Coerce
only where the domain has no meaningful zero, and put the plausibility check on
the consumer as well, since the next partial response will be shaped
differently.**

**16. A signal reused at the wrong timescale.** A label is borrowed from an
existing consumer and inherits that consumer's tolerance for staleness rather
than the new one's. H39: `arc` is a *conversation-level* tag — over 2,355 turns
it forms 137 runs averaging 17, with **not one run of length 1**, the longest 110
turns of `support` across eight days — and K92's ceiling used it as a per-turn
hard veto, so one hard thing he said on Monday muted her through Thursday. Every
input was correct and every stage did its job; the defect lived entirely in the
lifetime mismatch. It had gone unnoticed because the original consumer (K53)
fires once in six turns, where a sticky label merely damps an occasional beat.
**Rule: before using a label as a gate, measure its run-length distribution — a
signal that never describes a single turn should not answer per-turn questions.
The visible tell is a rule that dominates its siblings: one of five caps
accounting for 65% of all clamps was legible for four days before anyone asked
why.**

**17. A predicate answering a narrower question than its caller needs.** Not
shape 6 (one rule copied into N sites, wrong in N−1) — here there is exactly one
predicate, one caller, and nothing to disagree with. H40's
`has_relative_deictic` answers "will this wording go stale?", correctly, for all
eighteen words it matches. Its caller needed "…and which way does it point",
helped itself to the boolean it had, and so read `tomorrow` as evidence that
something had already happened: a courier due the next morning was filed as
history and stamped at write time, into a lane with no upkeep pass and no
retirement. The predicate's docstring was accurate the whole time; the bug lived
in the gap between what it promised and what the branch below it decided.
**Rule: when a boolean gates a branch that does more than one thing, check the
question it asks decides all of them. A predicate whose name is a strict subset
of the decision it drives — `has_X` gating *what kind of* X — is the tell, and
the cheap guard is a test asserting the predicate is silent about what it does
not know.**

**18. A handoff documented in prose that no code implements.** Shape 14 (write and
read but no upkeep) with the twist that lets it survive far longer: the upkeep pass
was not missing, it was **assigned**. H41's `promise_lifecycle` stated that the
user's own commitments were `FollowUpWorker`'s territory; that worker is real and
scheduled and selects on `temporal_type == "future_plan"`, while promises are
written `durable` — so the delegation named a component that could not match a
single row, and **86 of 86 user-side promises had never once been resolved**, the
oldest 86 days. Nothing was failing on either side, so nothing logged. The comment
was worse than silence would have been: shape 14 is found by asking "what retires
these?" and hearing nothing, whereas here you ask and get a confident name, so the
audit stops one step early. **Rule: when a docstring delegates a responsibility,
check that the selection criteria on the far side actually admit this data, and
prefer a test over the prose. A seam between two correct components is covered by
neither one's tests — assert that the receiving side sees the sending side's
output.**

Its sibling, from the same entry: **structured data written into prose is
write-only.** The promise deadline was extracted, formatted, stored and shown to
the model, and was still unreadable by every mechanism that needed it, because it
lived inside a sentence (`"… (by 2026-08-19)"`) instead of a field. It reviews as
complete, since the information is visibly *there*. **The tell is a field that
exists in the producer's schema and in no consumer's** — grep for the name; if the
only hit is the line that writes it, the data does not exist as far as the system
is concerned.

**19. A cap enforced by refusal, whose only working release is a clock.** A
resource limit has several release paths on paper; one of them is meant to carry
the traffic and is starved, so the slowest becomes the only one, and the limit
stops being a limit and becomes a **duty cycle**. H42: `hypothesis_max_open = 12`
should drain by guesses being asked and answered, but the ask needs a topical
match and was declined `topic_miss` on 382 of 444 decisions, leaving a 336-hour
TTL as the sole exit. So the lane ran as *invent twelve, then say nothing for a
fortnight while they age out together* — seven days into that silence with seven
to go, on a shelf where nine of the twelve had never been asked once.

Two things make this survive scrutiny. The refusal **reports as health**
(`skipped: max_open` is exactly what a correctly-full shelf says, and it is true),
so shape 1's rule about logging absence does not help — the line is there and it
is honest. And the release path that *is* starved usually looks fine in isolation:
declining a cue on a topic mismatch is correct behaviour, per-decision. It is only
wrong as a rate, and only when it is load-bearing.

Note that this was the **third** distinct cause of the same silence; the first two
(a fingerprint latch, then a TTL that exempted asked rows) were real, correctly
fixed, and still working. A cap can be starved in as many ways as it has exits.

**Rule: for every cap, list its exits and measure the throughput of each, not just
that each works. If one exit accounts for nearly all of the drain, the cap's real
period is that exit's period — write it down. And prefer a cap that *replaces* to
one that refuses: refusing is silent by construction, whereas replacing has to
choose, and a choice can be logged.** The eviction rule wants two guards of its
own — an age floor, so replacement cannot become churn, and lazy evaluation, so a
barren pass costs nothing.

**20. A written-down warning, re-violated, because the metric is easier to reach
than the correction.** A measurement mistake is diagnosed, fixed, and documented in
prose right beside the data — and then made again by a reader who had read the
warning. This is not inattention; it is that **the wrong number stayed the cheapest
thing to compute.** The rule against it lived in a paragraph, and paragraphs do not
run.

The cue lane did this three times over one week. H30 established that
`INELIGIBLE_REASONS` must come out of the denominator and said so; the next pass
grouped `cue_decisions` by `reason` anyway and declared four healthy cues dead. Told
that, the pass after it fixed the denominator, found `self_callback` with an *empty*
one, read undefined reach as low reach, and wrote down "is a gate that closed 399
times a rate limit or a deadlock?" — a question whose answer (`surface_cooldown_hours
= 240`, 78.5h still to run, surfacings exactly 10.0 days apart) was sitting in a code
comment directly above `INELIGIBLE_REASONS`. Each pass corrected the previous one's
error and committed a fresh instance of the same shape.

Two tells that a warning is going to be re-violated. It is phrased as a caution
rather than a default (*prefer the eligible rate* — so the raw one is still one
`GROUP BY` away, and the correct one needs a join). And the correct computation
lives somewhere the person asking cannot reach: `get_cue_outcomes` needed a running
app, while the question always gets asked during offline forensics.

**Rule: when a measurement has been got wrong twice, stop writing the warning and
ship the instrument — in the place where the question is actually asked, importing
the production predicate rather than restating it.** Then go one further, because a
correct number is still a number: **wherever the reading has a computable verdict,
print the verdict, not the input.** `2 of 452` invites a conclusion and `+78.5h
remaining of a 240h cooldown, by design` forecloses one. Ranking an undefined value
as `0.0%` is the same error in miniature — give the empty denominator its own
section rather than a row.

**21. A predicate blamed for a system's behaviour that is far too permissive to be
causing it.** A gate accounts for nearly all of some outcome in the ledger, so it
gets read as the thing doing the deciding — and every fix proposed for it is a
tightening. Nobody measures the one number that would settle it: **what fraction
of candidates does it actually accept?** H43: `topic_relevant` was 94.5% of all
eligible cue declines and accepts **33.2% of every real subject-message pair**,
which on a five-cue shelf is an ~87% chance of matching something every turn. It
was very nearly a no-op, so it could not have been declining those turns; the
declines were a *different* gate wearing its label (see shape 22). Tightening it
would have cut five cue types by 9× to fix a problem they never had.

The same measurement redirects the fix. A gate accepting a third of everything,
combined with **first-past-the-post selection**, means the choice among admitted
candidates is being made by whatever the query's `ORDER BY` happens to be — here
surfacings then recency, neither of which is about the live subject. So the signal
was not too weak to gate with, it was being *used* to gate when it should have been
used to rank. Generalised: **a signal good enough to rank with is rarely good
enough to gate on, because gating discards exactly the cases the signal is only
approximately right about.** K93's wants ledger had already learned this; H43 is
the second instance in a fortnight.

**Rule: before tightening a gate that "causes" an outcome, measure its acceptance
rate on the real candidate population. If it accepts most of them it is not the
cause, and if it also feeds a first-match selection, the bug is in the ordering.**
The corollary is cheap and worth doing unprompted: **for any threshold on a
similarity score, measure the null** — the score between pairs known to be
unrelated. Word overlap's hits sat at cosine 0.370 against a null median of 0.369,
which is the whole finding in two numbers, and neither was expensive to get.

**22. A decline reason inferred from overlapping causes, biased in one direction.**
Two gates can both refuse the same turn, and when the reason is *reconstructed*
afterwards from state rather than recorded at the point of decision, the tie-break
is a guess. H43's `take_pool_cue` asked "did the predicate reject anything" and "is
this type cadence-blocked" — but a cadence hold removes most of the shelf *before*
the predicate runs, so the survivors' refusal was scored as the predicate's.

What makes this more than untidy is that the two labels sit on **opposite sides of
a denominator**: `topic_miss` is an eligible decline and `cadence_block` is not. So
the mislabelling could only ever inflate the population that every reach figure is
measured against, and it did so for the largest bucket in the ledger. Shape 15's
proxy-arming problem is the sibling — same lesson, different field.

**Rule: when several gates can refuse the same turn, have the walk return counts
per cause rather than letting the caller infer from residual state. Where inference
is unavoidable, pick the tie-break that under-counts the bucket your metric is most
sensitive to.**

**23. A note that defers to a future observation, and outlives its own truth.**
"Watch for the first non-zero `asked_count`" is not a measurement, it is a hope,
and hopes do not run. H7's second pass ended on exactly that sentence; the counter
went non-zero eight days later and the entry went on saying zero for another
twelve, because nothing was going to notice. Every "still open" note in the
backlog is this shape waiting to happen — a claim written in the present tense
about a state that is free to change, stored in a file nothing re-reads.

Two of the three claims this audit checked were stale, and one of them
([G-CLEANUP](workers.md#g-cleanup-consolidator_statelast_cluster_index-is-not-dead-weight--do-not-drop-it))
had turned actively dangerous: its recommended fix would have broken a feature
built after it was filed. The failure is not that the notes were wrong when
written — they were all correct — it is that **correctness has a shelf life and
nothing was stamping it.**

**Rule: if a finding depends on a number moving, leave behind the thing that
checks the number** — a line in an existing report, a test that fails when the
state changes, a script. Where that is genuinely not worth it, write the claim in
the past tense with its date ("as of 12 Aug, `asked_count` was 0") so a later
reader can see it is a snapshot rather than a status. Prefer the report line: the
cue lane's three misreads in shape 20 were all cured by
[`scripts/cue_reach_report.py`](../../scripts/cue_reach_report.py) printing the
verdict, not by another paragraph asking people to be careful.

**24. The test suite writes the live install's state, and the damage is
diagnosed as a product bug.** A test drives real production code, that code
persists something by design, and the path it persists to is the developer's
own. The write is correct, the test passes, and nothing in the run mentions it —
so the corruption surfaces later, detached from its cause, wearing the costume
of a feature that does not work. H45: fourteen tests rewrote
`session.last_active_id`, leaving the app pointed at a conversation from May,
and the first investigation concluded the *pointer logic* was at fault and
shipped a mechanism to compensate.

What makes this shape nasty rather than merely untidy is that **the compensating
fix appears to work.** K91's write-on-first-turn genuinely repaired the pointer —
one turn after every launch — so the symptom became intermittent instead of
constant, which is the signature of a fix aimed one layer above the cause.
Compare shape 15: a proxy standing in for the thing you meant to measure.

The give-away is available in one step and was not taken: **compare the file on
disk against what the code reads back.** `config/user.json` said `4f909abd`
while `read_user_overrides()` in a fresh process returned `s2` — at which point
there is nothing left to theorise about.

**Rule: any state a test can reach that also belongs to the running install must
be redirected in `conftest.py`, autouse and session-scoped, plus a teardown
tripwire that fails the run if the live artefact changed.** Two corollaries,
both learned here. Per-test `mock.patch.object(settings, "USER_CONFIG_PATH", …)`
is not sufficient and is worse than nothing, because it makes the suite look
isolated: a module that did `from … import USER_CONFIG_PATH` holds a *copy* that
the patch never reaches. And prefer the tripwire to the redirect list — the
redirect covers the paths you thought of, the tripwire covers the next one.

**25. A change declined on a cost that was never measured, beside a benefit
that was measured exhaustively.** The report runs, the upside is quantified to
three decimal places, and the change is then held back by a *sentence* about
what it would cost — which reads as caution and is actually the one number in
the decision nobody computed. H43 measured what the stoplist drops (median
cosine 0.378 against a null of 0.384; 3.3% above the null's p95) and declined to
ship it because "it would make five cue types dramatically quieter", where the
scarcity of what they'd be quieter *with* was assumed. Measured a week later:
of 652 expired cues, **604 (92.6%) had already been rendered into her prompt,
1.4 to 2.0 times each, and passed over.** Reach was never scarce. The cost being
protected against did not exist.

Distinct from shape 23, which is a claim that was true when written and went
stale. This one was never checked. And distinct from shape 21, where the
*accused* gate went unmeasured — here it is the objection to the fix.

The tell is grammatical, which makes it cheap to grep for: the benefit is
written as a number and the cost as an adverb. "Dramatically quieter",
"markedly worse", "most of their reach" — every one is a quantity in prose
clothing, and each of them is a query.

**Rule: a trade-off needs both sides measured before either is acted on, and
the deferral note has to say which query would settle it.** H43 did the honest
half of this — it wrote down "a change to make on production evidence" — and it
is worth seeing why that was not enough: nothing was going to run that query,
because nobody owned it (shape 23 again, one level up). Name the report and the
number that would decide, or expect the deferral to be permanent.

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

## H34. Echo is a reliable measure, and it is not a measure of him

**Severity: medium — no bug, a boundary. Filed 15 Aug while auditing
surfacing; it puts a number on a risk H18 named and could not yet size.**

H18 replaced L38's reward signal because the old one was noise: the engaged
label has a split-half reliability of 0.05 at item level, since it belongs to
the turn and the median turn surfaces 67 items. `echoed` scores 0.61 on the
same test and is per-item by construction, so standing now reads `echoed /
judged`. That was the right call and this entry does not reverse it.

H18 also wrote down the trade in as many words: *"engagement is the user's
verdict; echo is only Aiko's, so rewarding echo does risk favouring what she
already reaches for."* The corpus is now big enough to ask how large that risk
is, and the answer is that the two signals are not partially aligned. They are
orthogonal.

Over the 628 turns carrying both a settled engagement label and at least ten
judged items, the share of surfaced items Aiko echoed has a Spearman
correlation with how the user then reacted of **+0.003**. Group means look like
a gradient in the wrong direction — engaged 0.198, neutral 0.217, disengaged
0.220, abandoned 0.261 — but the rank test says that is noise, and the obvious
confound accounts for the shape: echo share rises with the *length of his
message* (0.200 under 80 chars, 0.218 at 80–240, 0.249 over 240), because a
longer message retrieves more overlapping material. It is a property of the
input, not a verdict on the output.

So `earned_standing` is well named except for the word earned. It reliably
measures which of her own material Aiko reaches for again, which is a real and
stable quantity — it just carries no information about whether reaching for it
worked. A concept can rise to the top of the standing map without a single
signal from the user ever having touched it.

**Not a call to revert.** A reliable measure of self-consistency still beats an
unbiased measure of nothing, which is the choice H18 faced. The point of filing
it is that the gap cannot be closed by re-weighting the two signals against
each other, because there is no correlation to trade along — closing it needs a
user-side signal that is per-item, and the only cheap one in the building is
K32 reactions, which today feed tease rhythm and affection-style and never
reach the ledger. The other route is to stop surfacing 67 items a turn, which
is **K92**'s territory rather than this file's.

Re-run before acting: this is one install's 628 turns, and the confound above
means the honest read of the group means needs the message-length control every
time.

---

## H35. A third of the surfacing ledger cannot be scored by either signal

**Severity: low — a gap H18 spotted in passing and left unfiled. Filed 15 Aug
so it stops being a footnote.**

`surfacing_outcomes` has two outcome columns. `engagement_label` is turn-level
and shown by H18 to carry no item-level information; `echoed` is per-item and
is what L38 now reads. Clusters and cues have **neither**: `echoed` is `NULL`
on all 13,480 cluster rows and all 446 cue rows, because no echo test is ever
run for those two kinds. That is 31% of the 44,735-row ledger with no usable
verdict of any sort.

It matters in two live places. K81 taste affinity and L42 neglect both read
**cluster-level engaged rates** — the signal H18 measured at −0.01 split-half
reliability for clusters specifically — because there is nothing else to read.
H18's own note is the cleanest statement of the problem: taste's "1 of 39
clusters clears the bar" (H8) reads very differently if the bar is being applied
to noise. And cues, the one item kind that exists precisely to make Aiko say
something, are the kind whose landing is least measurable.

Cues are arguably correct as `NULL`: a cue is an instruction ("you can ask how
it went"), not a remembered item she might quote, so echo genuinely has no
meaning for it — and the cue pool already tracks the equivalent through its own
`used` / `expired` settlement. The honest fix there is not an echo test but a
join, so cue landing can be read out of the ledger alongside everything else
rather than only from a different table with different semantics.

Clusters are the real gap: a cluster is a topic, echo *does* have a meaning for
it ("did her reply stay in this topic"), and the same cosine she was selected on
would answer it. Either give clusters an echo verdict or accept in writing that
cluster affinity is a much weaker instrument than concept standing — and mark
K81 and L42 accordingly, since both currently read it as if it were not.

**Cost.** Low. The cluster echo test is one cosine against the reply embedding
that is already computed for K22, in `_record_surfacing_outcomes`
([`post_turn_helpers_mixin.py`](../../app/core/session/post_turn_helpers_mixin.py)),
next to the concept and memory tests that already run there. The denominator
correction H18 made to `echo_rate` (echoes over rows an echo test actually ran
on, not over all surfaced rows) means adding one does not silently move any
existing number.

---

## H36. Nine remembered arguments, and all nine were the clock

**Severity: medium — fixed 15 Aug. Found by the user reading the Together tab:
"there is a Repair shared moment that we hit a tense patch around 'Where is my
love?' … it is not matching our conversation. I was asking it but it was not
tense."**

He was right, and the miss was not a bad LLM judgement — no model was involved.
The record is written by arithmetic, and the arithmetic was subtracting two
numbers that are not comparable.

### The two numbers

`AffectUpdater.apply_turn` does its work in a fixed order. **Step 1** decays
valence toward baseline for the time elapsed since the last turn (30-minute
half-life); **step 3** blends in the reaction impulse. `AffectStore.get`, by
contrast, is a raw row read — no decay, no clock. So the pre-turn snapshot
`post_turn_mixin` takes carries whatever valence held whenever the previous turn
happened, and the post-turn value it is compared against has already had the gap
subtracted from it.

K8's stated signal is "the *pre/post* delta on a single turn". What it actually
computed was that delta **plus every minute the user was away**. With a
30-minute half-life, a gap of about 26 minutes from a warm parting clears the
0.12 threshold on its own, with no emotional content whatsoever.

The 15 Aug misfire, with the real numbers:

| | valence |
| --- | --- |
| stored prior (warm goodbye, 16:16) | **+0.266** |
| after 2h43m of decay, before any impulse | +0.006 |
| observed post-turn (18:59, "Where is my love?") | **+0.045** |
| implied reaction target | **+0.117** |

The impulse was *positive* — she warmed up by 0.039 — and the detector logged
`drop=0.221`. The whole "rupture" was 163 minutes of silence.

### Why every false positive looked like a greeting

This failure mode selects for one specific turn: **the first message after a
gap, following a warm goodbye.** High stored prior, long elapsed, and a
reunion's positive impulse nowhere near large enough to cover the decay. Which
is exactly what the nine `repair` moments in the store turned out to be, every
one anchored on an opener:

> "Where is my love?" · "Good morning aiko. Have you slept well?" · "Boo I am
> back aiko. Are you sketching?" · "Hi cutie I sneaked out from my work after
> meeting to see you" · "Oh aiko again i am late here :D But i have had really
> produc…" · "Aiko you won t believe what I did…"

Not one is tense. Nine fabricated arguments, `salience` up to 0.97, mirrored
into LanceDB and eligible for RAG recall — a relationship history of conflicts
that never happened, assembled entirely out of the user being away.

### Two smaller defects behind it

**A mood cooling to normal was called a rupture.** Both of the day's events
landed at +0.045 and +0.030 against a baseline of 0.0 — neither ever went
negative. A fall from unusually warm back to merely fine crosses a drop
threshold easily and wounds no one; the turn has to leave her below her own
resting valence to mean anything.

**"You were okay after" was never checked.** J6's `has_recovered` qualifies on
`rose_from_floor` — a 0.10 climb off the dip. From a floor of −0.50 that is
−0.40, still thoroughly miserable, and it unlocks a durable memory whose text
reads *"you talked it out and were okay after"*. The predicate could assert
recovery while the mood was underwater, because it only ever compared against
the floor, never against normal.

Also worth noting for whoever reads a repair summary next: the *topic* comes
from the arming turn but the *citation* comes from the recovery turn, which can
be several turns later. The 15 Aug row quotes "Where is my love?" (18:59) and
cites messages 4849/4850 (19:02, mid-cookie-scene). Two unrelated moments
stapled together.

> **STATUS: FIXED.** Decay is now a shared pure function
> (`decay_toward` / `AffectState.decayed()` in
> [`affect_state.py`](../../app/core/affect/affect_state.py)) rather than a
> private step inside `apply_turn`, and the K8 call site passes the **decayed**
> prior, so the delta is the impulse alone. `detect` takes an optional
> `baseline_valence` and refuses to fire on a turn that leaves her at or above
> her resting point. `has_recovered` takes an optional `baseline` and will not
> certify a repair while valence is still below it. Both gates are optional
> parameters, so the pure predicates stay usable without an `AffectState`. The
> rupture log line now carries `stored_prior` and `baseline` beside the decayed
> pair, which is what made this diagnosable at all. Replaying the two real
> events: both go quiet; a same-turn dip into negative valence still fires.
> Tests: `ElapsedTimeIsNotARuptureTests`, `DetectBaselineGateTests`,
> `AffectStateDecayedTests` in
> [`tests/test_affect_rupture_detector.py`](../../tests/test_affect_rupture_detector.py),
> `RecoveryBaselineGateTests` in
> [`tests/test_conflict_repair.py`](../../tests/test_conflict_repair.py).

**Where else this pattern lives.** The bug is not really about ruptures — it is
that a *raw* `AffectStore.get()` snapshot describes a moment in the past, and
nothing in the type system says so. The same `affect_before` object is handed to
several other consumers from the same place in `post_turn_mixin`.

**K45 mood inertia is affected, more mildly.** It differences the snapshot
against a reaction's *implied target* rather than against a post-`apply_turn`
value, so the elapsed gap does not enter its subtraction and it cannot
manufacture a false event the way K8 did. But its docstring calls
`affect_before` "what Aiko still actually feels", and after a long gap that is
false by exactly the decay margin — on a reunion turn it reads her as still
sitting at +0.27 when she is really near baseline, so every mismatch it measures
there is against a feeling she no longer has. Worth a follow-up, but it needs
its own look at the `mood_inertia_mismatch_threshold` calibration (the
thresholds were tuned against the stale numbers), so it is not a one-line
substitution.

Any *future* consumer that differences the snapshot against a post-turn value
inherits H36 outright. `AffectState.decayed()` exists so the correct thing is
one call away.

---

## H37. She learned to write a heart as the number three, from us

**Severity: medium — fixed 16 Aug. Found by the user: "Aiko learned to use
smilies like `<3`, `:3`, `;)` and they are being incorrectly filtered in the
text and 3 is then leaking into tts. It's nice that she can learn it"**

Two of the three faces were fine. `<3` was not, and the reason it wasn't is a
five-character character class in the *input* sanitiser:

```python
cleaned = re.sub(r"[^\w\s\.,!?;:'\"()\-]", " ", cleaned)   # sanitize_user_text
```

`<` is not on that list. `3` is a word character, so it is. An incoming `<3`
was therefore stored as a bare ` 3` — the `>` of `>_<` and both carets of `^^`
went the same way, which is why the database holds **zero** user messages
containing a `^`.

### Why a mangled user turn is worse than a mangled reply

This exact bug was fixed on Aiko's side months ago, and the fix came with a
written rule (`rules/code-conventions.md`): emoticons are a spoken-side-only
cleanup, because banning them at the last mile produces broken halves rather
than none. `sanitize_assistant_text` keeps every printable ASCII character and
lets `prepare_tts_text` do the stripping. **`sanitize_user_text` was never
brought along**, and nobody noticed, because the symptom does not appear on the
surface it was broken on.

The stored transcript is not just a display artefact. It is the conversation
replayed into the prompt every turn — the history Aiko reads as *hers*. So the
whitelist was not filtering a user turn, it was **writing training data**:

| | |
| --- | --- |
| user turns stored as "I love you 3" | **230** of 2,359 (9.7%) |
| her replies that had copied it | **12**, all in the last week |
| user turns still containing a `^` | **0** |

She had no way to reach the right answer. Every instance of affection-as-a-
symbol in her context was the digit, so the digit is what she imitated:

The raw-response log confirms she is not emitting a mangled `<3` — the `<` was
never there. `llm raw response: '… Sleep well, Jacob. 3'`. And a bare `3` is a
number to a grapheme-driven engine, so nothing downstream had any reason to stop
it: `ws tts_state {"text": "Sleep well, Jacob. 3"}`. It went out of the speaker
as **"Sleep well, Jacob. three."**

That is the whole loop, and it is worth naming because it is not specific to
punctuation: *a cleanup applied to the user's half of the transcript is a
behavioural intervention on Aiko, delivered with no logging, no config flag and
no obvious causal link back to the line of code that did it.* The three months
between the two halves of this fix are the cost of treating the two sanitisers
as doing the same kind of job on different strings.

> **STATUS: FIXED.**
> **1. The source.** `sanitize_user_text` now applies its punctuation filter
> *between* `_EMOTICON_RE` matches rather than over them, so a matched face is
> handed through whole and everything else is filtered exactly as before (`5 < 7`
> still loses its angle brackets). The two regexes moved above it so both
> sanitisers share one definition, and its duplicate pictograph pattern is now
> `_EMOJI_RE`.
> **2. The digit she already learned.** `_SWALLOWED_HEART_RE` in
> `prepare_tts_text` drops a lone `3` token that is followed by end-of-text or
> the start of a new sentence — the shape every real instance has. Measured
> against all 4,768 stored messages: 10 of her 10 hearts caught, and the only
> bare threes she ever meant as a number ("at nearly 3 a.m.") left alone. A
> capitalised clock is excluded by hand, since "meet me at 3 AM" loses its point
> without the number where a hypothetical "level 3 Boss" only loses a digit the
> transcript still shows. Spoken-side only; the transcript keeps what she wrote.
> **3. A run of hearts.** `<3<3` matched only its first heart — the second fails
> the leading `(?<![\w])` boundary because it follows a `3` — so the spoken copy
> kept an orphan digit from the middle. The heart arm is now `(?:<3+)+`.
> **4. The history.** [`scripts/repair_swallowed_hearts.py`](../../scripts/repair_swallowed_hearts.py)
> puts the `<` back on 241 hearts across 235 messages, skipping the 7 genuine
> numbers behind two short word lists and printing them for review. The LanceDB
> mirror is deliberately left alone: the prompt reads SQLite, and `3` versus
> `<3` moves an embedding by nothing.
> **5. The persona.** The emoticon line now lists `<3` and says to write the
> whole face or none of it, because a bare `3` is a number and gets read out as
> one.
> Tests: `SanitizeUserKeepsEmoticonsTests`, `SwallowedHeartTests` in
> [`tests/test_session_text_utils_tts.py`](../../tests/test_session_text_utils_tts.py).

**Where else this pattern lives.** The generalisable claim is not about `<`. It
is that **the user's stored turn is prompt text**, so every transform on the way
in is a persona edit with none of a persona edit's visibility. Two places to
check with that in mind: the `\u2018\u2019\u201c\u201d` quote folding in
`sanitize_assistant_text` has no counterpart on the way in (a smart quote from a
phone keyboard survives into her context as a character she is told not to
write), and `sanitize_user_text` still deletes `*`, `/`, `~`, `&`, `%`, `@`, `#`
and `=` outright — harmless for prose, but `*ruffles your hair*` reaches her as
`ruffles your hair`, which is a stage direction stripped of the convention that
marks it as one. Neither is urgent. Both are the same shape as this bug, and
neither would show up anywhere except in how she writes.

---

## H38. She was in pajamas at five in the afternoon, and the weather did it

**Severity: medium — fixed 16 Aug. Found by the user: "circadian is currently
driving her clothes incorrectly, she is in pajamas when it is day. I have it set
to auto"**

The suspicion was reasonable and wrong in an instructive way. Circadian was
correct throughout — the WS payload carried `"circadian_period": "afternoon"`
next to `"resolved_outfit": "pajamas"`, and `resolve_auto_outfit` cannot return
pajamas for `afternoon` from its circadian branch at all. The LLM had not asked
for it either: **zero** `outfit=Y` tags in the whole 10-hour log.

The outfit had a second, undocumented driver. `_apply_weather_seasonal_decor`
nudges her toward pajamas on a cold sky, through the same sticky override the
`[[outfit:…]]` tag uses. The log names the moment exactly:

```
16:20:23  weather seasonal decor: blanket=True open_window=False condition=cloudy temp_c=0.0
16:20:23  weather fetched: condition=cloudy temp=0.0C season=summer loc=Kamenná Poruba
16:50:55  weather seasonal decor: blanket=False open_window=True condition=clear temp_c=32.3
```

**Zero degrees in August, thirty minutes before it was 32.3 °C.** Every other
reading that day walked 21.5 → 30.1 °C.

### Three defects in a row, each harmless alone

**1. A partial response became a winter afternoon.** Open-Meteo returned a
`current` block with no `temperature_2m`. The provider validated that the block
*existed* but not its contents, and `float(cur.get("temperature_2m") or 0.0)`
turned absence into `0.0`. Missing `weather_code` did the same through
`condition_from_wmo(None) -> CONDITION_CLOUDY`. The pair "cloudy, 0 °C" is not a
neutral default — **it is a plausible-looking winter day**, which is why it
passed every downstream sanity check: there weren't any.

**2. The threshold read it as freezing.** `want_blanket = condition == "snow" or
temp_c <= 5.0`. Fabricated zero clears that by five degrees.

**3. The nudge was one-way.** `if want_blanket: emit("pajamas")` had no
counterpart. When the sky corrected itself at 16:50 the *decor* was fixed —
blanket removed, window opened — but nothing ever withdrew the outfit. The only
exit was the circadian period rolling over, so one bad reading pinned her
wardrobe for the rest of the afternoon.

### Fixed

The provider now **raises** on a `current` block with no temperature, so a
partial fetch is recorded as `errored` and the last good snapshot survives
instead of being overwritten by a fiction. A genuine `0.0` still passes.
The decor hook refuses to decide anything from a non-numeric or implausible
(`|t| > 70 °C`) reading, and logs when it declines. And the nudge is now
symmetric: overrides carry a `source`, so the weather feed can withdraw its own
cold-sky nudge once the sky warms without ever cancelling an `[[outfit:…]]`
Aiko chose herself.

### The shape worth keeping

This is [recurring shape 15](#recurring-shapes) and the first
instance of it here: **a missing value coerced to a valid one**. `or 0.0` is not
a default, it is a fabrication, and it is most dangerous where zero is a
*meaningful* value in the domain — temperature, valence, confidence, price. The
failure is silent by construction, because the fabricated value is in range.

It is worth grepping for siblings. `WeatherSnapshot.from_dict` has the identical
`float(blob.get("temperature") or 0.0)` on the rehydrate path, which the
consumer guard now covers but the type does not.

A second, smaller lesson: the user's mental model named the wrong subsystem
("circadian") because **circadian is the only outfit driver that is documented**.
A passive feed reaching into a channel owned by another layer should say so in a
log line at INFO, which the withdrawal path now does.

---

## H39. A conversation-level label used as a per-turn veto muted her for eight days

**Severity: medium — fixed 19 Aug. Found by measuring K92 phase 1's own output
before building phase 2.**

K92 phase 1 recorded, per turn, the most floor-taking stance the providers
offered (`desire`) and the most the user's turn permitted (`ceiling`). It never
touched the prompt, so this is a defect in a shadow log — but the same ceiling
was about to start steering, and one of its five rules turned out to be wrong by
a factor of seventeen.

`arc_protected` accounted for **164 of 252 clamps (65.1%)** — more than the other
four rules combined. Phase 1 flagged that as suspicious on the grounds that the
arc list (`support`, `reflection`) was inherited from K53 rather than earned.
Measuring it found something worse than a list that is too broad.

**`arc` is a conversation-level label, not a per-turn read.** Over 2,355 turns it
forms 137 runs averaging **17 turns**, and there is **not one run of length 1**.
The longest protected spans are **110 consecutive turns of `support`, spanning
eight days.** Used as a per-turn hard filter, one hard thing he said on Monday
vetoed everything above `FOLLOW_AND_ADD` through Thursday — including turns about
guitar solos that happened to sit inside the same labelled span.

The reason this had never surfaced is instructive: K53 reads the same arc list
and fires **once in six turns**, so a sticky label merely damped an occasional
beat. A ceiling consulted on *every* turn is a completely different exposure to
the same staleness, and the list was copied across without anyone re-asking what
its lifetime was.

### Fixed

The cap now applies only while the span is fresh — `arc_age_turns <
PROTECTED_ARC_FRESH_TURNS` (4), tracked as session state and incremented
post-turn when the new arc matches the previous one. Four turns is the width of
an opening beat: long enough that "I had a rough week" is met with listening
rather than with her own news, short enough that it cannot outlive the subject.
Replaying the corpus, `arc_protected` clamps fall **164 → 79** and the overall
clamp rate **36.9% → 28.7%**, moving 51 turns from `FOLLOW_AND_ADD` to `SHARE`.
The binding constraint is now `direct_question` (68) at about the same weight as
the arc — a per-turn signal rather than a stale one.

The per-turn caps (`vent`, `direct_question`, `planning`, `user_substantial`)
were deliberately left untimed. They are re-derived from the current turn every
time, so they are present exactly as long as their evidence is, which is the
property `arc` was wrongly assumed to have.

### The shape worth keeping

**[Recurring shape 16](#recurring-shapes): a signal reused at the
wrong timescale.** Every
input here was correct — the arc tagger is doing its job, and 110 turns of
`support` is an accurate description of that conversation. The defect is entirely
in the *lifetime mismatch* between a label that describes a conversation and a
consumer that asks it a question about a turn. A signal borrowed from another
consumer inherits that consumer's tolerance for staleness, not its own.

The cheap guard is to measure the run-length distribution of any label before
using it as a gate. "Not one run of length 1" is a two-line query and it settles
the question immediately: a signal that never describes a single turn should not
be answering per-turn questions. The related tell is a rule that dominates its
own siblings — 65% of clamps from one of five rules was the visible symptom, and
it was visible for four days before anyone asked why.

A second lesson about phasing: this was found *because* phase 1 shipped as a
shadow log that recorded `desire` and `ceiling` separately. Had the arbiter
rendered from the start, the arc veto would have quietly suppressed her for days
and shown up, if at all, as a vague sense that she had gone flat. Recording both
sides of a decision you have not yet acted on is what made a wrong rule legible
as a number instead of as a mood.

---

## H40. She asked about a delivery she had already helped unpack

**Severity: high — fixed 19 Aug. Found by the user: "aiko is tangling dates. I
told her that it should come today and she remembered that it will come today.
But courier surprised me yesterday and i build everything and also discussed it
with her and she asked me today about the delivery."**

Two lines of code, one at write time and one at read time, between them
guaranteed she could not tell a plan from its outcome. The LLM's date arithmetic
is also imperfect, and that is the part everyone looks at first, but it is not
what caused this.

### The write: a word that means "not yet" read as "already"

The K-time10 backstop exists for a good reason. `durable` is the default
temporal type and renders with **no time tag at all**, so a note worded "Jacob
mowed the lawn today" keeps reaching the prompt months later still asserting the
present. Re-reading such a row as an event anchored at write time is the honest
interpretation:

```python
if temporal_type_normalized in (
    "durable", "preference",
) and timephrase.has_relative_deictic(cleaned):
    temporal_type_normalized = "past_event"
    if event_time_clean is None:
        event_time_clean = now
```

`has_relative_deictic` answers one narrow question — *will this sentence still
mean what it says in a month?* — and answers it correctly for all eighteen words
it matches. But **five of them point at the future**: `tomorrow`, `tonight`,
`next week`, `soon`, `this weekend`. For those, the word is proof the thing has
*not happened*, and the branch concludes that it has, then stamps it at the
moment of writing. Staleness and direction are two different questions about the
same word, and the code asked one and used the answer for the other.

Being wrongly filed as `past_event` is the worst available outcome, because that
lane has no upkeep. Nothing retires it — `MemoryDecayWorker` only ever moves rows
*into* `past_event`. The "Coming up for Jacob" block (K-time3) reads
`future_plan` exclusively, so it never saw the courier at all. And **17 of 2,095
rows had ever reached `future_plan`, 0.8%**, while 54 plans sat in `past_event`
dated into their own future — the feature was starved and nobody noticed, because
a starved feature and a quiet week look identical.

### The read: a fabricated recency

Then the retrieval bullet finished the job. Asked to describe a past event whose
timestamp is in the future, `humanize_past` did not refuse:

```python
delta = (now - when).total_seconds()
if delta < 0:
    return "moments ago"
```

So the row claimed to be brand new for the whole interval between being written
and its own event time. **54 rows, a median of 14.8 hours each, 1,316 hours in
total.** The worst single row reported having just happened for **188 consecutive
hours**. The test covering this was named
`test_future_input_is_defensive` and asserted `"moments ago"` — it was written as
a guard and was in fact pinning the fabrication.

### What the prompt actually said

Six rows about the one delivery reached her together. Paraphrased to the same
shape, with the real time tags:

```
<he> expects the courier tomorrow morning.                      (moments ago)
<he> says it arrived unexpectedly yesterday, August 19, 2026    (moments ago)
<he> assembled it today after the courier came.                 (moments ago)
The parts arrive tomorrow, for the upgrade on Tuesday 18 Aug.   (20 hours ago)
<he> received the delivery on August 19, 2026                   [no tag]
<he> promised: sleep before the courier arrives (by 2026-08-19) [no tag]
```

Four stamped **equally fresh**, two carrying no time tag at all, and the set
contradicts itself in three places — arriving tomorrow, arriving yesterday, and
already assembled. Two days of events collapsed onto one instant, with "a courier
comes tomorrow" presented as the most recent thing he had said. Asking about the
delivery was the only thing she could have done with that.

The last line is its own finding, and became H41: a promise's deadline is written
into the middle of the sentence, where nothing but the model can read it.

The LLM errors are real and secondary. `[3201]` resolved "Wednesday" to Tuesday
25 August from a Sunday; `[3475]` wrote "yesterday, August 19, 2026" *on* August
19. The extractor prompt told the model to resolve relative phrases against
"today" while the transcript it receives carries per-line `[age]` stamps the
prompt never mentioned — the promise worker names them explicitly, so the
extractor was the one worker missing the better operand.

### Fixed

`_RELATIVE_DEICTICS` is now two lists and `deictic_direction` reports which way a
sentence points. Future-pointing wording becomes `future_plan`; past-pointing
keeps the old behaviour, which was right for its half. Note the asymmetry in what
each branch may invent: a past deictic licenses "it happened when this was
written", true by construction, while a future one licenses **no timestamp at
all** — "soon" does not name a moment, and guessing one is the same fabrication
in a different costume. Where a sentence carries both, future wins, because
mis-filing a plan as history strands it in the lane with no upkeep whereas the
reverse self-corrects within the hour.

`humanize_past` returns `"in the past"` for a future timestamp, and
`temporal_suffix` prefers `event_time` only while it is actually past, falling
back to `created_at` — the anchor we genuinely know, since the note was written
whatever it claims about its subject. That keeps the tag informative ("3 hours
ago") where using the stated time would erase it.

A direction check now runs at the store: a `past_event` dated after the moment it
was recorded is two fields disagreeing about whether the thing happened, and
`event_time` wins because it is the more specific claim and the label is the
field producers get wrong. It compares against the **write time, not against
now**, so replaying an old row cannot re-decide it.

One near-miss worth recording, because it would have been a worse bug than the
one being fixed. A type carries an expiry rule, and `derive_relevance_until` ran
*upstream* in the extractor — so the first version of this fix reclassified rows
while leaving them holding `durable`'s `relevance_until`, which is `None`, and
`list_by_temporal_type` **skips rows whose `relevance_until` is NULL**. Every
promoted row would have been invisible to every upkeep pass that could retire it:
immortal. The derivation now lives beside the writer that can change the type,
and the pre-existing `durable → past_event` path turns out to have had the same
hole since K-time10.

Finally, the extractor prompt now points at the per-line stamps like the promise
worker does, is told not to write a weekday or calendar date into `content`
unless the user stated it outright, and is told that the past/future split is the
one distinction that matters because the two are handled by different machinery.

### The shape worth keeping

**[Recurring shape 17](#recurring-shapes): a predicate answering a
narrower question than its caller needs.** `has_relative_deictic` is correct, its
docstring is accurate, and its tests pass — it says "this wording will go stale".
The caller needed "and which way does it point", helped itself to the answer it
had, and inverted a third of its inputs. This is not the shared-predicate problem
of shape 6 (one rule copied into six call sites and wrong in four); here there is
exactly one predicate, one caller, and no disagreement to find. **Rule: when a
boolean gates a branch that does more than one thing, check that the question it
asks decides all of them. A predicate whose name is a strict subset of the
decision it drives — `has_X` gating *what kind of* X — is the tell.** The cheap
guard is a test that asserts the predicate is *silent* on the thing it does not
know, which is what `test_it_says_nothing_about_direction` now does.

The second lesson is about defaults at the boundary, and it is
[shape 15](#recurring-shapes) again in a place nobody thought to
look: **an impossible input should be refused, not rounded**. A past event in the
future is not a near-miss to smooth over, and "moments ago" was the single most
destructive string in the chain — it took four memories written across two days
and made them indistinguishable. Where H38's fabrication came from a partial API
response, this one came from a *display helper*, which is why it survived so long:
nobody audits a formatter for correctness. **A formatter that cannot represent
its input should say less, not guess.**

---

## H41. Not one of his promises had ever been resolved, and no deadline was readable

**Severity: high — fixed 19 Aug. Found by following H40's loose thread: the
deadline a promise states is written into the middle of the content sentence, so
nothing but the model can read it.**

That thread turned out to be the smallest of three findings. Promises are the one
memory kind with an explicit state machine —
`open → surfaced → fulfilled | dropped`, plus sidedness — and on a store of 160
rows the machine was running on one side only:

| side | open | surfaced | fulfilled | dropped |
|---|---|---|---|---|
| Aiko | 18 | 23 | 26 | 7 |
| Jacob | **86** | 0 | 0 | 0 |

**Every user-side promise ever recorded was still `open`.** The oldest was 86
days old; 36 were past the 14-day bar that would have retired an equivalent
promise of hers. They were all still scoring into retrieval, so a fragment of
small talk from May — one of the many rows that were never really commitments,
still carrying salience 0.93 — remained eligible to be quoted back at him. And
the lane is not slowing down: 44 promises were written in week 33 and **57 in
week 34**, about eight a day.

### The handoff that never met

`promise_lifecycle`'s docstring said why the worker only scanned her side, and it
read like a decision rather than a gap:

> Only **assistant-side** promises participate in follow-through — the user's own
> commitments are the `FollowUpWorker` / proactive-callback territory.

`FollowUpWorker` is real, is scheduled, and does retire things. It selects with
`list_by_temporal_type("future_plan")`, and that predicate opens
`if mem.temporal_type != normalized: continue`. Promises are written by
`PromiseExtractionWorker._persist`, which passes no `temporal_type` at all, so
every one of them is `durable`. **The delegation named a worker that could not
match a single row.**

Nothing failed, so nothing logged. Both halves are individually correct and both
have passing tests. The comment is worse than no comment would have been: a
reader asking "who retires the user's promises?" got a confident answer and
stopped looking. It survived from K43 to now on the strength of one sentence.

### A deadline nothing could read

37 of the 160 promises state a deadline. It is folded into the content string at
extraction —

```python
if deadline_str and deadline_str.lower() not in {"null", "none", ""}:
    body = f"{what} (by {deadline_str})"
```

— and `grep` for anything that reads it back finds nothing. `promise_deadline`,
`deadline`, `(by ` : the only match in `app/` is the line that writes it. So
every lifecycle decision was made from `promise_age_hours`, i.e. from
`created_at`, and the two questions came apart in both directions: **a promise
made this morning and due by lunch read as fresh all afternoon, while a standing
"I'll help when you ask" read as late purely for being old.** Retirement had the
same blind spot in the more expensive direction: a commitment agreed three weeks
ahead of the day it fell due was scheduled to be dropped for staleness on that
very day.

The prompt had asked for "a specific time or day if one was stated" without
naming a format, and got six:

| register | n |
|---|---|
| `2026-08-19` | 22 |
| `Monday, August 17, 2026` | 8 |
| other prose (`Before August 18, 2026`) | 4 |
| `2026-08-17T23:30:00.000Z` | 2 |
| `tomorrow` | 1 |

All 37 reached the prompt verbatim, **24 of them already past** — so she was
being handed raw ISO dates and asked to work out whether they had happened, which
is the arithmetic H40 exists to stop her doing. The literal `tomorrow` is the
same bug as H40's stored deictics, in a field nobody had thought of as stored
text: it re-anchors to whenever it is next read, forever.

### The nudge that ignored the lifecycle

`prepared_nudge._collect_candidates` treats a promise like a callback or a
reflection: filter by kind, cap by `use_count`, rank by salience. It never read
`promise_status`. **14 of the 33 resolved promises were sitting inside that
window** — the top one at salience 0.89, marked `fulfilled`, never used, one of
hers and a tender one — waiting to be spoken through the template
`"Quick check — did you ever get to {x}?"`. Asking after something she already
did is worse than saying nothing; it reads as not having been paying attention.

The RAG guard against exactly this failure was blind for a structural reason
worth recording. `_temporal_filter_drops` says in its own docstring that expired
rows are dropped because surfacing them "produces the exact *asking about
progress on something that already finished* bug this work targets" — but it
inspects `past_event` rows with a `relevance_until`. Promises are `durable` with
`relevance_until` NULL. **The check written for this failure mode could not see
the rows most likely to cause it.**

### Fixed

`timephrase.parse_loose_datetime` reads a day or time out of text we did not get
to format — ISO, month names in either field order, bare weekdays, and the
handful of relative words a model actually reaches for. It returns `None` rather
than a guess for wording that names no moment, because a fabricated deadline
means reporting something overdue that was never due. All six live registers
parse; the prompt now asks for ISO so the parser is a backstop rather than the
plan. Two details are load-bearing: an offset-less stamp is read as **local**,
where `parse_iso` promotes to UTC — that function reads timestamps we wrote, and
we write UTC, while this one reads a model describing somebody's afternoon — and
a bare day lands at **23:59**, since "by August 19" is not breached at 00:01 on
the 19th.

The parsed value goes to `metadata.promise_deadline`, deliberately not to
`event_time`. `event_time` means "when this happened or happens" and the decay
worker retires rows by it, which is the wrong reflex here: an unkept promise is
the point, not expired bookkeeping. Keeping it out of the temporal columns leaves
`promise_status` the single authority on a promise's fate. The stored sentence now
carries an absolute, weekday-bearing form (`(by Wed Aug 19)`) so nothing goes
stale and nobody does calendar arithmetic; unparseable wording is kept verbatim
only while it would still be true next month.

Lateness is now its own axis — `promise_deadline` / `overdue_hours` /
`is_overdue` — and the worker uses it three ways: a missed deadline **outranks**
mere age when choosing what to raise, it **bypasses** the settling period that
exists so she doesn't ask about something she said twenty minutes ago, and the
cue **says so** ("That was due 3 days ago, so it's late") instead of reporting
only how long ago she made it. Retirement now runs on both sides, on whichever
clock applies: a deadline still ahead protects a promise however old, a passed one
earns a full grace window measured from itself rather than inheriting what was
left of the creation-age one, and no deadline at all falls back to the original
rule. Surfacing stays assistant-only — retiring his promises is upkeep, raising
them is nagging, and that is a separate decision. Retired promises are also hidden
from the live RAG block; `fulfilled` ones stay, because those happened and that
makes them ordinary shared history.

Dry-run against the live store: **37 deadlines become readable** (from zero),
**36 stale user rows retire** on the first sweep, **33 resolved promises leave the
nudge pool**, and **19 promises are currently late but inside their grace
window** — 8 of them hers, so the cue has real material for the first time.
Existing rows keep their deadline only in the content string, so
`scripts/backfill_promise_deadlines.py` re-reads the suffix with the same parser,
anchored to each row's own `created_at` so a relative word resolves against the
day it was written. It is a re-read of a date the model already committed to, not
an inference, and it is `--dry-run` by default.

### The shape worth keeping

**[Recurring shape 18](#recurring-shapes): a handoff documented in
prose that no code implements.** This is shape 14 (a store with write and read but
no upkeep) with a twist that made it survive far longer: the upkeep pass was not
missing, it was **assigned**. One sentence in a docstring named a real, scheduled
worker as the owner, and that worker selected on a field the producer never set.
Every component was correct in isolation, every test passed, and the accumulating
side had no symptom other than getting slowly worse.

The reason it lasted is that the comment answered the question a reader would
have asked. Shape 14's version is found by asking "what retires these?" and
getting silence. Here you ask and get a name — so the check has to go one step
further. **Rule: when a docstring delegates a responsibility to another
component, verify the selection criteria on the far side actually admit this
data, and prefer stating it as a testable claim over prose. A handoff between two
correct components is not covered by either component's tests, and the seam
produces no log line because nothing on either side is failing.** The cheap guard
is a test that asserts the *receiving* side sees the sending side's output — which
is what `RetirementTests.test_a_stale_user_promise_is_retired` now does, on the
real 84-day-old row.

Second, smaller lesson, and the one that generalises furthest: **structured data
written into prose is write-only.** The deadline was extracted, formatted, stored
and shown to the model, and was still unreadable by every mechanism that needed
it — because it lived inside a sentence rather than in a field. It looked
complete at every review, since the information is visibly *there*. The tell is a
field that exists in the producer's schema (the LLM was asked for `deadline`) and
in no consumer's.

---

## H42. She had not wondered anything for seven days, and the log said healthy

Found by reading the overdue predictions in the watch list below rather than by a
symptom, which is the only way this one surfaces: every line it writes reads fine.

`hypotheses.asked_count` was H7's signal that the loop had closed at all, and it
had — 5 asks across 17 rows, so the ask path works. But **the newest hypothesis
was seven days old**, and every proposer run in between had reported:

```
idle_worker run done: hypothesis_proposer (0ms, avg=0ms)
    result={'skipped': True, 'reason': 'max_open', 'live': 12, 'expired': 0}
```

Twelve live rows against `hypothesis_max_open = 12`, so the shelf is full and the
worker refuses. That is exactly what the cap is for, and the line is honest about
what it did. The problem is what it does not say: whether a full shelf is a
*healthy* full shelf.

### It was not. This shelf was a stalled one

| what | reading |
|---|---|
| live rows | 12, at the cap |
| ages | 158 h – 279 h, against a 336 h TTL |
| never asked | **9 of 12** |
| linked to a concept | 0 |
| past TTL | 0 |

Nothing is broken in that table, which is the difficulty. TTL is behaving. None
of the rows is the unreachable linked kind that `hypothesis_state` was built to
catch. They are simply twelve guesses between six and twelve days old, filling
the shelf, and nine of them have never been put to him once.

**The cap has two exits and only one of them carries traffic.** A row leaves by
being asked and answered, or by aging out at 336 h. The ask exit needs the
`concept_hypothesis` cue to win a slot, and that cue is declined `topic_miss` on
**382 of 444 decisions** — because it needs the shelf to match what is actually
being discussed, and a shelf stocked with `ritual`/`conduct` guesses about Aiko
does not match a week of hardware and anime. So in practice the fortnightly clock
is the *only* exit.

Which makes the lane a duty cycle rather than a trickle: **invent twelve, then say
nothing for a fortnight while they age out together.** Measured mid-August, it was
seven days into that silence with 7.4 to go, and the arrival pattern confirms it —
six rows on 7 Aug, two on 8, one on 10, five across 12, then nothing.

### This is the third distinct cause, and the first two were real

Worth stating plainly, because it is the reason the entry exists. The same
silence has been diagnosed and fixed twice:

1. **The fingerprint latch** — `_run_conduct_pass`'s sibling bug, where an empty
   proposal pass saved its watermark and retired valid findings forever.
   Fixed; the latch is held open.
2. **TTL exempting asked rows** — a row asked once and never answered could
   neither be re-asked (one ask per invention) nor expire, so it held a slot
   permanently. `expire_stale` now keys the exemption on `last_tested_at`
   (*answered*) rather than on having been asked. Both docstrings say "twelve of
   them shut the live lane down completely".

Both fixes were correct and both are still working — `expired: 0` on these runs is
TTL agreeing that nothing is due yet. The lane died anyway, for a third reason
neither fix touches: not that rows *can't* leave, but that the cap refuses to
accept a better guess while they wait.

### The fix: a full shelf replaces instead of refusing

When the shelf is full and a novel guess has cleared both novelty gates, retire
the stalest never-asked row and take its slot.

**What is protected from that** (`HypothesisStore.stalest_evictable`), all of it
about not destroying information:

- **`open` only.** `supported` has a confirmation behind it and is on its way to
  graduating, which is the outcome the layer exists for; no amount of staleness
  makes discarding it right.
- **Never asked, never answered.** A question already put to him may still be
  answered, and that answer is worth more than a fresh guess. `last_tested_at` is
  checked as well as `asked_count`, since a restated row was answered without the
  counter necessarily moving. These rows still leave — at the full TTL, where "he
  is not going to answer now" has become true.
- **Older than `hypothesis_evict_min_age_hours` (168 h).** This is the knob that
  keeps replacement from becoming churn: a shelf filled an hour ago is not stale
  stock and refusing is right for it. Deliberately **half** of
  `hypothesis_ttl_hours`, and the pair should stay in that ratio — eviction is
  early TTL under demand, not a second policy with its own opinion.

Three properties of the shape are load-bearing:

**Eviction is lazy.** Nothing is given up until a candidate has passed both
novelty gates, so a pass whose every candidate was rejected leaves the shelf
exactly as it found it. Deciding up front would have spent stock on barren runs.

**It is self-limiting.** Inventing freely lowers the shelf's age below the 168 h
bar, at which point replacement stops and refusing resumes. The equilibrium is a
shelf that turns over weekly — about **1.7 inventions a day** — rather than
twelve in a burst and then nothing.

**A run cannot spend the slot on a paraphrase of what it gave up.** `expired` is
deliberately the one status the novelty gate lets past (so an unasked guess does
not burn its ground), which would otherwise let a pass evict a row and re-invent
it: two writes that report as healthy for a straight swap. Rows evicted *this
pass* keep blocking until the next one.

The row is closed as `expired`, not deleted — the same exit TTL uses, carrying the
same meaning, so the guess can be wondered again later.

`demand` now separates the two cases as well, since reporting a stale full shelf
and a fresh one both as `shelf_full` is how a week of silence read as health. A
replaceable shelf reports `shelf_stale` at one slot's worth of pressure:
deliberately low, because giving up a guess to make room is worth less than
filling an empty slot and should lose to any worker with real hunger.

### What this does not fix

`topic_miss` at 382 of 444 is still there, and it is the deeper question — a
shelf that tracks recent subjects should match more often, so the rate ought to
improve on its own, but the ask lane's topical gate is an **H7 remainder / H4(a)**
question and is not addressed here. The honest claim for this entry is narrower:
invention no longer stops, so whatever the ask lane's hit rate is, it is applied
to stock that is at most a week old instead of at most a fortnight.

### Shape 19

Added below: **a cap enforced by refusal, whose only working release is a clock.**

### Shape 20

Added below, from the cue-reach retraction that followed this entry: **a
written-down warning, re-violated, because the metric is easier to reach than the
correction.** H42 is one of its exhibits — `topic_miss` at 382 of 444 was named as
the deeper question here, and the very next reading pass buried it under four cues
that were not broken at all.

*Answered by **H43**, and not in the direction this entry expected: the gate is
not too strict, and `topic_miss` was never mostly the gate.*

---

## H43. The gate blamed for 94.5% of her silences accepts a third of everything

Started as the deeper question H42 deferred and H30 named twice: `topic_miss` is
**1,873 of 1,981 eligible cue declines, 94.5%**, and all of it comes from five
providers — `concept_hypothesis`, `curiosity_gradient`, `interest_drift`,
`associative_wander`, `knowledge_gap_notice` — sharing one fourteen-line
predicate. One shared word of three or more characters between the cue's label
and his message.

The obvious reading is that the gate is too strict. It is the opposite, and
getting there required discarding two of my own hypotheses first, which is worth
recording because both looked like findings.

### Two dead ends, and why they were dead

**The pick window.** `pick_pool_cue` applies the predicate to the first `limit=8`
rows only, and I measured shelves of 22 to 45 — so 30-odd cues never examined.
That measurement was wrong. It reconstructed "what was pending at time T" as
`created_at <= T <= expires_at`, which ignores `not_before` (the re-ask cooldown)
and the `pending`/`surfaced` state split. The **actually available** shelf is 0 to
5 rows per type. The window has never once been binding.

**Missing labels.** Two sampled `concept_hypothesis` payloads appeared to have no
`label` key, which the gate reads — and an empty topic returns `False`
unconditionally, so those cues would be permanently unreachable. All 47 have
labels. My probe filtered payload values by length for display, so the long ones
vanished from the printout and not from the data.

Both were caught by the same discipline and it is the transferable part of this
entry: **a reconstruction of past state needs an arm whose answer is known in
advance.** Mine replayed the gate over turns it had already declined, so the
"what the gate saw" arm had to come back at ~0%. It came back at 55–82%, which
said the reconstruction was wrong before any conclusion was drawn off it.

### What the calibration data said, once someone read it

The *consumption* half of the cue system has been banking a cosine on every
verdict for months against exactly this question, with a note in `_match_cue`
that the read was worth retrying once verdicts accumulated. Retried:

| population | median cosine |
|---|---|
| verdicts decided by word overlap | **0.370** |
| verdicts decided semantically | 0.530 (p10 0.510) |
| **null** — 4,000 random message × cue pairs | **0.369** |

The first and third are the same number. **When word overlap said "this is the
moment", the two texts were as related as two texts drawn at random.** Controls
bracket it: hand-checked related pairs land 0.55–0.69, unrelated 0.25–0.36.

Why: counting which tokens actually carry the matches, **82% are function words.**
`and` (39k), `the` (34k), `you` (26k), then `your`, `with`, `that`, `when`, `for`
— plus her own name at 5.5k, because cue subjects are written *about* the two of
them and he addresses her by name constantly. A three-character floor was doing
the job a stoplist should do, and English's commonest words are three and four
letters long.

### The number that inverted the fix

Over every real (subject, message) pair, the gate **as shipped accepts 33.2%**.
With a shelf of five that is an ~87% chance something "matches" every turn: the
gate is very nearly a **no-op**. So it cannot have been what declined those 1,873
turns — and it wasn't. That was an attribution bug (below).

Which kills the tightening. Stoplist plus a null-calibrated cosine floor accept
3.8%, a **9× tightening**, on five cue types that the entire K92–K95 family
exists to make *more* forthcoming. Correct-looking, and it would have made her
markedly quieter in service of a problem she did not have.

### The fix: rank, don't gate

What the numbers indict is not admission but **choice**. `pick_pool_cue` returned
the *first* admitted row, ordered by surfacings then recency — nothing to do with
what he just said. A shelf where a third of everything "matches", plus
first-past-the-post, is precisely how a cue surfaces on a shared `and`.

So the cosine orders the admitted candidates instead of vetoing them. The
acceptance set is untouched, so **reach cannot fall**; only which cue she is
handed changes. Same correction K93 made to the wants ledger, for the same
reason: *a signal good enough to rank with is rarely good enough to gate on,
because gating discards the cases the signal is only approximately right about.*

Dry-run against the live shelf, 120 real messages:

| cue | fired | winner changed | median cosine, old → new |
|---|---|---|---|
| `concept_hypothesis` | 106 | **49.1%** | 0.417 → 0.456 |
| `curiosity_gradient` | 108 | **49.1%** | 0.344 → 0.393 |
| `associative_wander` | 78 | 29.5% | 0.417 → 0.428 |
| `knowledge_gap_notice` | 86 | 8.1% | 0.510 → 0.521 |
| `interest_drift` | 72 | 2.8% | 0.414 → 0.414 |

The cosine arm *is* additive at admission (+1.2% of pairs), catching what word
overlap structurally cannot — the same subject in different words. Its floor is
sited on the measured null (~2% of unrelated pairs clear 0.55) rather than picked.
The stoplist is implemented, measured, and **off at admission**: ranking removes
most of its value, and 30.7% of pairs is a change to make on production evidence.
What it drops is genuinely noise (median cosine 0.380 against a null of 0.392).

### The attribution bug underneath the headline number

`take_pool_cue` inferred its decline reason from what it could still see: "did the
predicate reject anything" and "is this type cadence-blocked". Those overlap. A
cadence hold restricts the pick to cues that have already had a showing, removing
most of the shelf **before** the predicate sees it; the survivors then fail, and
the turn was recorded `topic_miss`.

That only ever pushes one way. `topic_miss` is **eligible**, `cadence_block` is
**ineligible**, so every mislabelled turn inflates the denominator every reach
figure is measured against. `note_as` now requires that the predicate was the
*only* thing that refused — an undercount when both apply, which is the safe
direction for the largest bucket in the ledger.

`pick_pool_cue` returns a `CuePick` carrying the counts, so the caller no longer
infers. `considered` is also the **only** record anywhere of shelf depth on a
given turn: `state`, `not_before` and `surfaced_count` are all last-value-only, so
availability history is unrecoverable after the fact. It is logged for that reason.

### What this does not fix

The winners above still sit near the null. Ranking picks the best of a thin shelf,
and on most turns the shelf's best is *still* not about what he said — 0 to 5
live cues against open-ended conversation. That is the supply problem, K93's open
cue-pool half, and it is the follow-up rather than a caveat: 66–90% of cues expire
unused and `concept_hypothesis` is **0 used of 47 ever created**.

Re-read `scripts/cue_reach_report.py` after a few days: with attribution fixed,
`topic_miss` should fall sharply and `cadence_block` should rise by roughly as
much. If it doesn't, the tie-break is wrong and not the measurement.

### Shapes 21 and 22

Added above: **a predicate blamed for a system's behaviour that was too permissive
to be causing it, because nobody measured its acceptance rate** — and its
enabler, **a decline reason inferred from overlapping causes, biased in one
direction.**

---

*Superseded — H9 and H10 both shipped. Kept for the reasoning, which held up:
H9 was the difference between a companion who has feelings about things and one
who reports a uniform mild pleasantness, and several other findings (H14, the
blandness of the affective concepts, L13's whole premise) were downstream of it.
H10 turned out to be a flag rather than new code, as predicted, though it needed
H16's dedupe first — see the current ordering below.*

---

## H44. Nothing has ever graduated, and at this calibration nothing can

**Severity: medium — the L30 loop's last exit has never once been taken, and the
arithmetic says it is not waiting on more data.**

Found while auditing "still open" notes (see [H7's third
pass](#third-pass-it-adjudicates-now-and-graduation-is-the-leg-that-never-ran)).
The forward half of L30 invents, the middle now asks and occasionally adjudicates
— and the exit that turns a settled guess into a durable concept or belief has
**zero instances, lifetime**. `graduated_concept_id` and `graduated_memory_id` are
`NULL` on all 23 rows.

**This is not a bug, which is what makes it worth an entry.** Every part works as
built. `is_ready` requires `refute_count == 0`, `support_count >= 2`
(`hypothesis_graduate_min_support`) and `credence >= 0.7`, and `supported` is
correctly a live status, so a confirmed row genuinely remains eligible. The
problem is that the bar was set without anyone computing what the pipeline can
deliver against it — [shape 2](#recurring-shapes), drain rate below arrival rate,
in its subtlest form yet: not a cap versus a cadence, but **a two-event
requirement against a window that only ever sees one event.**

**The arithmetic.** The TTL is 336 hours (`hypothesis_ttl_hours`, 14 days).
Graduation needs two independent confirmations landing on the *same row* inside
that window. Observed over the corpus:

| | |
| --- | --- |
| Rows ever asked | 5, all `asked_count = 1` |
| Asks that produced any verdict | 1 of 5 |
| Rows that reached `support_count = 1` | 1 (and it was never asked) |
| Rows that reached `support_count = 2` | **0** |
| Exits actually taken | 10 `expired`, 1 `refuted`, 0 graduated |

The single `supported` row has held `support_count = 1` for 182 of its 336 hours
with `credence = 0.95` — everything but the second confirmation. It will expire
around 26 Aug. So expiry is not one exit among several, it is **the** exit: ten of
eleven closed rows, and the one exception was a refutation.

**Two candidate readings, and they want different fixes.** Either (a) the
ask-to-verdict conversion is the real defect and `min_support = 2` is fine once
answers get classified — H7's third pass found four rows asked, answered, and
never scored, which is where the confirmations *should* be coming from; or (b) two
confirmations inside 14 days is the wrong shape for a belief about a person, since
the natural evidence for "he organises commits by mood" arrives every few weeks,
not twice a fortnight. These are distinguishable: fix the resolver first and
re-read, because under (a) the bar needs no change and under (b) no amount of
resolver work will produce a graduation.

**Do the resolver first, and do not touch the bar yet.** This is
[shape 9](#recurring-shapes) — the gate may be the only thing containing a
producer that has not been measured. Lowering `min_support` to 1 today would let
the *one* passively-confirmed row graduate, and a row that was never asked and
never contradicted is exactly the kind of guess that should not become a durable
belief about him on a single ambient signal.

**Sequencing.** After [H7](#h7-the-hypothesis-loop-invents-and-never-adjudicates)'s
resolver gap, and it should be measured with a report line rather than a note that
says to watch for it, per [shape 23](#recurring-shapes). The natural home is a
`--hypotheses` section in
[`scripts/cue_reach_report.py`](../../scripts/cue_reach_report.py), which already
owns "did the lane spend what it produced" for the cue side.

**Effort.** Small to measure and to add the report line; the resolver work behind
it is medium and belongs to H7.

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

- **H1** — ~~the latch is open; `kind='conduct'` should go non-zero on the next
  weekly pass. If it does not, look at the detector.~~ **Read 19 Aug: it did not,
  and it is the detector.** The pass has been running — `last_run` 13 Aug, on its
  weekly cadence — and returns `conduct_findings: 0` every time, with the snapshot
  an empty list and the signature hashing the empty list. So the latch fix is
  correct and irrelevant: there is nothing for it to latch. `kind='conduct'` is
  **0 of 3,294 concepts** while all eleven other kinds took rows the same day.
  That makes **H2** (the concentration/fixation bars, shape 3) the live suspect
  after all, not a follow-on — read the `conduct.gate shape=… declined_on=…
  reading=…` lines, which exist for exactly this and were designed in for it.
  Adjacent, same reading: `taste` is 2 rows (newest 5 Aug) and `pursuit` is 8, so
  the near-dead tail is three kinds wide, not one.
- **H16** — tension candidates per pass went 0/3/6 → 3/6/9 by block. Re-run the
  population query after two days of merge budget; 122 rows should fall toward
  the ~15–20 distinct frictions the labels suggest.
- **H10** — tension should move off 0 of 14,240 concept-lane surfacings without
  displacing `affective` (H11's ratio is still unsettled). Land it *after* H16
  has drained, or she says the same thing eleven ways.
- **H7** — ~~`provider` should drop below half of `concept_hypothesis` declines
  now the reasons are split, the shelf should stop growing, and the first
  non-zero `hypotheses.asked_count` is the signal the loop closed at all.~~
  **Read 19 Aug: the loop did close — 5 asks across 17 rows — and then the lane
  stopped inventing entirely for seven days.** See **H42**: the shelf sat at the
  cap with nine of twelve never asked, so the only exit left was a fortnightly
  TTL. Fixed by replacing instead of refusing. What H42 deliberately leaves is
  the other half of this entry: `topic_miss` on **382 of 444** declines, which is
  the ask lane's topical gate and belongs with **H4(a)**. Expect it to improve on
  its own now the stock is at most a week old rather than a fortnight — that
  improvement is the thing to measure before touching the gate.
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

0. **H7's resolver gap, then H44.** Newest and the cheapest real defect on the
   list: four hypotheses were asked, answered, and never scored, which is where
   graduation's missing second confirmation should be coming from. Do that before
   touching `hypothesis_graduate_min_support` — under one reading the bar needs no
   change at all, and the numbers are now printed by
   [`scripts/cue_reach_report.py`](../../scripts/cue_reach_report.py) rather than
   waiting on someone to check.
1. **H11** — the ratio itself, and now the decision is live: H10 just put a
   generative kind into the lane boundary has been dominating (28.6% of
   surfacings against `affective`'s 7.1%). Judge it on the post-H10 mix.
2. **H4(a)** — the wedge behind `dormant_interest` and `self_callback`, and
   **H30 has now answered half of it for free.** `self_callback` is not the
   wedge: it was eligible twice in the window and surfaced both times, so its 4%
   reach rate was its ten-day cadence being counted as failure.
   `dormant_interest` is the real one, and the shape is now named — **0 eligible
   turns of 49**, every decline `no_opening`, meaning the K18 standing lull never
   arrived rather than a cooldown being too long. Read that against **H17**
   (which is about what counts as a lull) before touching this cue's own gates.
3. **H7 remainder** — `concept_hypothesis`'s last place in `GAP_CUE_ORDER` and
   its K47 asymmetry. Deliberately deferred: both are defensible, and the split
   reasons will say whether they matter. Note the order gained a member on
   12 Aug (immersion H26's `caught_mid_activity`, ahead of `away_activities`),
   so the queue behind it is one deep — read the decline reasons against the
   post-12-Aug window only, and after **H32**: before it, a `lost_priority`
   reason did not verify that the winner outranked the loser, so pre-13-Aug
   mutex rows cannot be used to argue about this order at all.
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

**Both shipped the same day.** H29's prune now asks for settlement rather than
availability, and `wants_per_source_cap=4` stops the seed feeder owning the
ledger; it is a measurement item now, earliest honest read 16 Aug. H30 turned out
to need no provider work at all — the instrumentation had shipped twelve hours
before the reading, and the entry's window straddled it. What *was* broken was
the reach ratio, which counted a cue's own cooldown turns as missed chances; the
new `eligible` denominator is in place and it retired four of the nine
"starving" cues on the spot. Read H30's last two sections before quoting any
armed/surfaced number from this file.

**What the fixed instrument leaves open**, in the order it now suggests:
`caught_mid_activity` at 0 of 7 eligible (new on 12 Aug, so a real zero worth
one look); `dormant_interest` at 0 eligible with every decline `no_opening`,
which is an **H17** question rather than an H4(a) one; and the five topic-gated
cues, whose 2–20% eligible rates are genuine misses and are now the only entries
on the list that were ever mysterious.

**The first of those was taken and was not what it looked like** — see **H32**,
shipped 13 Aug. `caught_mid_activity`'s 7 armed turns were 7 *returns*, because
its arming signal was another cue's gap slot, and all 7 declines named a mutex
winner that ranks below it. Both numbers were artefacts. The two remaining leads
above are untouched, and `dormant_interest`'s is now the one worth reading first:
it is the only cue whose zero has survived scrutiny of the instrument.

**Added 15 Aug**, from a surfacing audit prompted by the suggestion that Aiko
needed an attention-economics layer with a dozen scored signals. She mostly has
one — the audit reproduced H18's 67-items-per-turn dilution from scratch before
finding H18 had already fixed it — so the two entries filed are the parts that
survived: **H34** (echo is reliable and is orthogonal to the user's reaction,
rho +0.003 over 628 turns — H18's flagged risk, sized) and **H35** (31% of the
ledger has no verdict of either kind). Both are measurement-boundary entries
rather than defects; neither blocks anything.

Two prerequisites confirmed landed on the same pass. H30's `provider` catch-all
went from **84.5%** of declines before 13 Aug to **0.0%** of the 1,391 after it,
and H29's ledger now carries a want at pressure 0.62 that was minted on the 13th
— so pressure survives days rather than dying at two showings. That cleared
**K92**, whose phase 1 shipped the same day. Its shadow log immediately returned
two things this file should watch: the interruption ceiling binds on **32.6%**
of turns, which makes K95 load-bearing rather than insurance, and `HOLD` was
chosen **0 times in 432 turns** because some provider is always offering
something — the same "she gets a steer nearly every turn" shape H28 and K92 both
describe, now visible as a single number.

## Reading of 19 Aug — the overdue predictions

Every entry above with a date attached, read at once. Three resolved, one is a new
defect (**H42**, shipped), one moved to a different owner, and the block-firing
table is worth keeping as the denominator for anything below.

**Resolved.** H7's loop *did* close (5 asks across 17 rows) — and then the lane
stopped dead, which became H42. H1 did **not** resolve and the detector is the
cause, not the latch; it now points at H2 rather than being blocked by it. Both
entries above are struck through with the reading.

**The two blocks that have never once rendered**, across 683 recorded turns:
`thread_ownership_block` and `conduct_block`. `topic_appetite_block` has fired
twice all-time and not at all in the last three days. That trio is H28/H29's
outstanding prediction and it has now missed its ~16 Aug date, so the soft band
spending `ranked[:2]` (H29's withdrawn drain 2) is the next suspect. `stance_block`
also shows zero, but only because K92 phase 2 shipped at 13:21 on the 19th and the
last recorded turn is 10:25 — the shelf of 683 stance rows is phase 1 plus the
backfill, and phase 2's live behaviour is genuinely unread. Do not treat that zero
as evidence of anything.

**The cue reading, and a retraction.** The first version of this section grouped
`cue_decisions` by `reason` and reported four cues as dead — `self_callback` at 2
of 452 (`cadence_block` 426), `caught_mid_activity` at 2 of 265 (`no_stock` 246),
`dormant_interest` at 4 of 336 (`no_opening` 225), `shared_ritual` at 4 of 450 —
and concluded the next lead was *supply, not selection*. **That was wrong, and
wrong in the direction that invents work.** All four of those dominant reasons are
in `INELIGIBLE_REASONS`: they mean the cue never had a chance that turn, which is
not a chance it passed up. H30 built the `eligible` denominator for exactly this
and the entry above says to read it first. It was read with the raw one anyway.

On the correct denominator, `surfaced / (surfaced + eligible declines)`, over the
6.5 days since H30's instrumentation landed (a 7-day window still catches the
12 Aug tail of the old uninstrumented `provider` catch-all and reads differently —
that bare reason stops at **zero** on 13 Aug and has stayed there, exactly as H30
claimed):

| cue | surfaced | eligible declines | reach | dominant eligible reason |
|---|---|---|---|---|
| `concept_hypothesis` | 9 | 390 | **2.3%** | `topic_miss` 362 |
| `curiosity_gradient` | 18 | 335 | 5.1% | `topic_miss` 335 |
| `interest_drift` | 31 | 368 | 7.8% | `topic_miss` 368 |
| `associative_wander` | 32 | 292 | 9.9% | `topic_miss` 292 |
| `knowledge_gap_notice` | 65 | 334 | 16.3% | `topic_miss` 334 |
| `caught_mid_activity` | 2 | 10 | 16.7% | `lost_priority` 10 |
| `long_arc_callback` | 17 | 12 | 58.6% | `topic_miss` 12 |
| `tension` / `turning_over` / `follow_up` | 50 / 20 / 11 | 0 | **100%** | — |
| `shared_ritual` / `dormant_interest` / `wellbeing_concern` | 4 / 3 / 2 | 0 | **100%** | — |
| `self_callback` | 0 | 0 | **n/a** | — (401 structural, `cadence_block` 399) |

**One gate accounts for 1,703 of the 1,759 eligible declines — 96.8% of them —
and it is `topic_miss`**, spread across five cues that each look like their own
problem. That is one finding, not five, and it is the same one H30's entry named:
"the five topic-gated cues, whose 2–20% eligible rates are genuine misses and are
now the only entries on the list that were ever mysterious." H42 has just changed
one of those five's input (a shelf at most a week old rather than a fortnight), so
`concept_hypothesis` should be re-read before the gate itself is touched.

Three of the four retracted cues turn out to be at **100% reach** — they take
every live chance they are given and are simply scarce. `caught_mid_activity` is
also not a defect, and the retracted claim got its mechanism wrong twice: its
stock is not `cue_pool` and has nothing to do with `agenda` (which is a goals
table, unrelated). Supply is a live kv blob, `away_activity.in_progress`, holding
a beat the away-life worker chose to leave *running* instead of journalling;
`no_stock` means no beat is open at this instant, which the provider's own
docstring calls "by far this cue's most common outcome". Counted from the rolled
logs, **7 of 29 beats were left open — 24% against a designed
`away_activities_in_progress_ratio` of 0.3.** The producer is healthy.

**`self_callback` looked like it survived the retraction, and it did not — this is
the same mistake a third time, one layer in.** It is *never eligible*: 0 surfaced
and 0 eligible declines against 401 structural ones, 399 `cadence_block`. An empty
denominator is undefined reach rather than low reach, which reads as "the one case
that points somewhere real", and the question was written down as *is a gate that
closed on 399 consecutive attempts a rate limit or a deadlock?* It is a rate limit.
The type carries `surface_cooldown_hours=240` — ten days — it last surfaced 161h
ago, **78.5h of the window are still to run**, and the two surfacings on record sit
exactly 10.0 days apart. The gate is metronomic. `cue_accounting.py` says so in as
many words directly above `INELIGIBLE_REASONS`: *"`self_callback` carries a ten-day
`surface_cooldown_hours`, so on 96 armed turns it was inside its own cooldown on
nearly all of them."*

So the whole "cues that never win" line of inquiry is **empty**, and the finding is
`topic_miss` alone.

**What the third repeat actually shows is that prose warnings do not work, and the
instrument has to close the question rather than pose it.** All three passes were
committed by someone who had read the warning; the failure is not attention, it is
that a number was reported where a verdict was available. Two of these are
computable, so the report now computes them instead of printing a row that invites
the wrong reading:

- Every cue's cooldown state is resolved against its own policy and printed as a
  verdict — `inside cooldown, by design (cooldown 240h, last surfaced …, +78.5h
  remaining)`. The rate-limit-or-deadlock question cannot be asked twice.
- The one shape here that *is* a bug gets its own section: a `cadence_block` dated
  **after** `last_surfaced_at + cooldown`, which no cooldown explains. Nothing
  currently qualifies, which is the useful answer.

**The instrument now exists offline.** The reason this was got wrong after being
warned about is that the correct denominator lived only in code and in
`get_cue_outcomes`, an MCP tool needing a running app — and offline forensics is
exactly when the question gets asked. `scripts/cue_reach_report.py` imports
`is_eligible_decline` from production rather than restating it, ranks by reach,
separates the two decline classes, and aggregates eligible declines by gate so a
single shared gate cannot read as several starving cues, resolves each cue's
cooldown against its own policy and prints that as a verdict, and breaks out empty
denominators rather than ranking them as 0%. Use it instead of a hand-written query.

This reading is **Shape 20**: a written-down warning re-violated, because the wrong
number stayed the cheapest thing to compute. Two window traps found along the way,
both of which moved the headline and are now noted in the script: bare `provider`
stops at zero on 13 Aug, so a 30-day window fills 58% with declines whose reason was
never recorded; and `created_at` carries a `+00:00` offset and is compared as text,
so a bound built from local time silently shifts the window by that offset.

---

## H45. The test suite was choosing which conversation she woke up in

**Severity: high — it corrupted live install state on every full run, and the
first investigation of the symptom blamed the wrong component and shipped a
mechanism to compensate for it.**

Reported as "the last conversation is still not working properly, I am at an old
conversation usually, and need to switch to the latest one after restarting" —
*still*, because this was looked at once before and declared fixed.

### The one-step tell

The restore pointer is `session.last_active_id` in `config/user.json`.

| | |
| --- | --- |
| The file, read directly | `4f909abd` — correct, the newest session |
| `read_user_overrides()`, fresh process | `s2` |

Two different answers to the same question means the theorising is over. `s2`
was not a stale value the app had written: **it was written by the test suite,
minutes earlier, into the developer's live config.**

### Mechanism

Any test that drives a real `SessionController` through a turn reaches
`_touch_last_active_session`, which persists the pointer *by design* — that is
K91, the mechanism added by the previous investigation. A guard that reported
every write to the live path put the count at **14 tests**, all through that
method plus one via `switch_session`, across `test_voice_merge.py`,
`test_post_turn_timing.py` and others. None of them had any interest in the
config file; they were exercising merge buffers and log levels.

The reason this presented as *an old conversation* rather than an obviously
broken one is that the ids the tests use are plausible, and two of them are real:

| id a test wrote | in the live DB |
| --- | --- |
| `main` | 8 messages, last used 23 May |
| `s2` | 157 messages, last used 12 Aug |

So the app booted, honoured the pointer exactly as specified, and opened a
conversation from May. Everything downstream worked perfectly.

### The previous investigation got this backwards

The earlier entry concluded that `switch_session` records *intent*, so the
pointer could name a session the user had moved on from, and added
`_touch_last_active_session` to also record *activity*. Every word of that
reasoning is sound and the mechanism is worth keeping. It was not the cause.

Worse, it **hid** the cause: writing the pointer on the first user turn repairs
it one turn after every launch, so the corruption became intermittent — visible
only if you restarted, looked, and had not yet spoken — instead of constant. A
fix aimed one layer above the cause, whose partial success is what buys the real
cause another month. This is [shape 24](#recurring-shapes), and its exhibit.

### The fix is isolation, not more compensation

An autouse session-scoped fixture in `tests/conftest.py` points
`USER_CONFIG_PATH` at a throwaway file for the whole run. It starts **empty**
rather than as a copy of the live file, so no result can depend on whose machine
it ran on.

Two details that are the actual content of this fix:

- **`gate_tuning_store` is redirected too.** It binds the path with `from … import
  USER_CONFIG_PATH`, so it holds a *copy* and is untouched by patching the
  settings module. This is why the per-test
  `mock.patch.object(settings_mod, "USER_CONFIG_PATH", …)` calls in
  `test_session_controller_session_restore.py` — which are correct, and which
  that file needs because it asserts on contents — were never going to be
  enough as a general answer.
- **A teardown tripwire.** The fixture hashes the live file before the run and
  fails loudly if it changed. The redirect covers the two globals that exist
  today; the tripwire covers the third one somebody adds, a hardcoded path, or a
  subprocess. Confirmed on a full run: `writes to the real config/user.json:
  none`, hash unchanged.

`tests/test_session_controller_session_restore.py` also gains three tests
asserting the suite cannot reach the live path, because the failure mode is
invisible from inside a green run.

### A second bug, found by asking the same question of TTS

The engine picker had the same "it does not stick" complaint, and it was a plain
omission rather than anything subtle: `set_tts_provider`, `set_tts_device` and
`set_tts_voice` mutated `self._settings.tts` and never called
`persist_user_overrides`. Every neighbouring setter does — the avatar scale, the
search provider, the weather location, her display name — so the choice applied,
broadcast, and read back correctly from memory, and lasted exactly as long as the
process.

Now persisted as a **per-provider** entry (`tts.providers.<engine>.voice/device`)
rather than a flat field, so a round trip pocket-tts → Chatterbox → pocket-tts
returns to the voice that was set; a refused provider is not written, so the next
boot cannot read a setting the engine already rejected; and empty values are
skipped, since a cloning engine with no voice picks its own reference clip and
recording that absence as `""` is noise.

### Answering "when is it saved?"

Worth stating plainly, because the question is reasonable and the answer was not
written down anywhere:

| | |
| --- | --- |
| On an explicit switch in the sidebar | immediately (`switch_session`) |
| On your first message in a session | immediately (`_touch_last_active_session`) |
| On close / shutdown | **never** — and `docs/configuration.md` said it did |

So a session you open and never speak in is not remembered, which is deliberate
(it keeps "New session" → close → reopen on the fresh empty one). The
`configuration.md` line claiming `SessionController.shutdown()` writes it was
simply wrong and is corrected.

---

## H46. A quarter-second of speech after the sentence, that nothing could retract

**Severity: medium — audible on every engine, and neither half of the cause is a
bug on its own.**

Reported as a fragment occasionally spoken after a sentence ends, "when the
buffer ends", and — the detail that made it worth chasing — *still happening on
the new engine*. An artifact that survives replacing the entire synthesiser is
not the synthesiser's.

### Two wrong theories, each cheap to kill

Worth recording because both were plausible and the measurements that ruled them
out are reusable.

**The text.** The reported sound was a word-like syllable, and she emits inline
tags (`[[remember:…]]` and a dozen more) that are stripped before speaking. A
partial tag leaking at a chunk boundary would be exactly this. So: stream 14 tag
shapes through the real `safe_visible_prefix` → `drain_tts_stream_chunks` →
`prepare_tts_text` path at one, three and seven characters per delta, and
compare what reaches the engine against the fully-stripped reply. Zero
mismatches, including unclosed tags, tags with a `]` inside, back-to-back tags,
and a tag glued to a sentence stop. The holdback is sound.

**The engine.** Autoregressive TTS is known for trailing noise. Measured the
tail after speech ends on eight sentences: 0.22–0.40 s at 2–6% of body RMS,
which is decay. The first pass at this used an energy gate and was the wrong
instrument — anything loud enough to hear is loud enough to be classified as
speech, so the measurement hides what it is looking for. Re-cut as speech/silence
runs, asking whether the *last* run is detached from the body by a real gap:
zero of eight clips end in a detached sound.

Neither the text nor the audio. Which leaves the part in between.

### The cause: two correct decisions that are wrong together

| | |
| --- | --- |
| `PcmPlaybackMixin._PRE_ROLL_CHUNKS = 5` | ship ~250 ms immediately, then pace at real time, so the client's scheduler never underruns |
| `AudioOutputManager._onAudioEnd` | "Nothing to flush here" — the next sentence chains onto this one's tail, so discarding would clip every sentence short |

Both are right, and each has a comment explaining why. Together they mean the
client is **permanently holding ~250 ms of speech that has not been heard**, and
when a clip is cut — barge-in, a stop between sentences — the server stops
sending and fires clip-end, while the audio already scheduled in the Web Audio
graph plays anyway. A quarter-second of her voice, mid-word, after the sentence
appeared to end.

Engine-independent because the pre-roll lives in the mixin both engines share.
Occasional because it needs a cut to land while audio is in flight. And
unfixable from either side alone: the protocol had exactly one frame for "this
clip is over" (`0x13 audio_end`), whose docstring said *flush the matching
queue* while the client deliberately did not — a contract mismatch that had been
sitting in the two files for as long as both comments.

### The fix is a second frame, because there are two questions

`0x14 audio_cancel` means *drop what you have not played*, and is sent only for
a cut. `audio_end` keeps its existing meaning and its existing no-flush handling,
so sentences still chain. The mixin distinguishes the cases by whether its emit
loop was broken or exhausted; `stop()` also fires cancel directly, which covers
a stop landing between clips while the client still holds the previous
pre-roll. On the client, cancel stops the scheduled sources and rewinds
`nextStartTime`, so the next clip starts from now rather than chaining onto a
schedule that was just thrown away.

Deliberately not gated on the server's `_stream_started` flag: an `audio_end`
closes the sending side but the client is still playing, so a cancel arriving
just after one has to go out anyway.

### Shape

A third instance of [shape 12](#recurring-shapes) — two components each
individually correct, with the fault living in the contract between them, and
each side's comment explaining why its own behaviour is right. Neither file's
tests could have caught it: the mixin's would have to know what the browser does
with a scheduled buffer, and the client's would have to know the server runs
ahead of real time. The new tests state the *pairing* on both sides — a cut must
cancel, a natural end must not — which is the only place the invariant is
expressible.

### Also found

`tools/tts_lab/adapters.py` reached for a `_cache_lock` that the `ClipCache`
refactor absorbed, so auditioning pocket-tts in the lab had been raising
`AttributeError` — invisible, because the lab was only ever used for the
engine being evaluated.

---

## H47. She was shown 604 of them and said nothing, which is not a supply problem

Started from his question, which was better than the one I was going to ask.
Told that `topic_miss` is the largest eligible cue decline, he asked whether it
is a problem at all — *"maybe Aiko didn't want to bring it up because it did not
fit the current mood of conversation."*

The mechanism does not work that way: `topic_relevant` is word overlap plus a
cosine arm, with no notion of mood in it. But the instinct was right about the
conclusion and it inverted the investigation, because the honest version of his
question is **"when she declines, is she right to?"** — and that is answerable.

### What the ledger says once "unused" is split in two

`63.5% of cues expire unused` has been quoted as a supply problem, including by
me earlier in the same session. It is not one. Splitting expired rows by whether
they ever reached her prompt:

| | rows | share |
|---|---|---|
| expired, **never** rendered to her | 48 | 7.4% |
| expired, **rendered and passed over** | 604 | **92.6%** |

Per-cue showings run 1.4 to 2.0 for every type except two. She was not short of
material. She was handed it, repeatedly, and declined it — which is what being
handed a cue matched on `and` looks like from the inside.

Two exceptions, and they are the real starvation: `concept_hypothesis` at **0.4
showings per cue with 22 of 49 never shown**, and `forward_curiosity` at 0.4
with 15 of 30. Those two are a separate problem from this one and are not fixed
here.

### Three of my own numbers that did not survive contact

Worth recording because each looked like a finding and each was an artefact of
the instrument.

**The reach improvement.** Splitting `cue_decisions` at the H43 commit showed
reach jumping 11.2% → 28.9%, which I read as the fix working. The split was
wrong: a *second* instrument change (12 Aug, `b4b3386`) had closed the
`provider` bucket — the fallback for a provider that declined without saying
why — and every one of its 2,836 rows predates `2026-08-12T16:05`. Since
`provider` counted as an *eligible* decline, its disappearance alone moves every
reach figure. Re-cut so both eras have attribution complete, the gain is real
but smaller, and H43's stated prediction only half held: `topic_miss` fell
42.1% → 33.6% as forecast, `cadence_block` rose 1.5pp rather than absorbing it,
and the difference became **surfacings** rather than another decline reason.

**The used-rate table.** `curiosity_seed` converts at 41.6% and
`concept_hypothesis` at 0%, which is a 13× gap and looks like proof that
conversation-seeded production beats graph-seeded. It compares two different
verbs. `curiosity_seed` is `FULFILMENT_EITHER` on a post-turn cosine at 0.50 —
the conversation *touching* the subject retires the row, whether or not she ever
raised it — while the failing types need `answered`. Cross-type conversion rates
in this pool are not comparable without reading `CUE_POLICIES` first.

**`concept_hypothesis` has never reached her mouth.** It has surfaced 19 times
and she asked 7 of them. The zero is `used`, meaning *answered*, which is H44's
finding and not this one.

### The change

H43 built the stoplist, measured it, and left it **off at admission**, writing
that 30.7% of pairs "is a change to make on production evidence rather than on a
pair-population estimate". This is that evidence, and it points the opposite way
from the fear that held it back — the reach being protected was not scarce.

So the stoplist is on. Re-running `scripts/topic_gate_report.py` against current
data, at the shipped 0.55 floor:

| | share of pairs |
|---|---|
| gate as shipped | 32.3% |
| stoplisted lexical arm | 1.8% |
| cosine arm | 2.0% |
| **new admission (either arm)** | **3.6%** |

and what it drops sits at **median cosine 0.378 against a null median of
0.384** — very slightly *less* related than two texts drawn at random — with
3.3% above the null's p95. The carriers are unchanged from H43's count: `and`
(6,372), `the` (1,225), `with` (690), then **`aiko` at 580**, because cue
subjects are written about the two of them and he addresses her by name
constantly. The names are not hardcoded; `GateOptions` carries them from
`assistant.name` / `assistant.user_name`, since only a caller holding settings
can know them.

`GateOptions` exists rather than two more parameters because the strictness and
the names always travel together through six providers and four shared helpers,
and threading two arguments through all of that is how the original predicate
acquired the shape nobody could see into. `GateOptions.shipped()` reproduces the
pre-H47 gate exactly, which is what `agent.cue_topic_stoplist=false` returns and
what the tests assert against.

### What to watch, and the dial

This is a 9× tightening and it is the largest single change made to her
forthcomingness. The prediction: `topic_miss` should *rise* as a share of
decisions, because refusing is now the honest answer more often — and per-cue
**conversion** should rise, because what reaches her is on-topic. If reach falls
and conversion does not move, the trade failed and the exit is one setting.

Prefer relaxing `agent.cue_topic_min_cosine` to 0.50 over turning the stoplist
back off: that widens the semantic arm (7.3% of unrelated pairs clear 0.50
against 1.8% at 0.55) rather than readmitting function-word noise wholesale.
0.45 admits 20.2% of unrelated pairs and is not worth having.

Re-read `scripts/cue_reach_report.py` in a few days, and split expired rows by
`surfaced_count` — that ratio, not the expiry rate, is the number this entry
exists to move.

### Shape 25

Added above: **a change declined on a cost that was never measured, beside a
benefit that was measured exhaustively.** The tell is grammatical — the benefit
written as a number, the cost as an adverb — which makes it cheap to look for.
Also a second instance of [shape 23](#recurring-shapes) one level up: H43 said
what evidence would settle it and nothing was going to go and get it.
