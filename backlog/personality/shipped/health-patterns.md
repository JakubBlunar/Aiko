# Recurring health-audit patterns

Reference catalogue moved out of the active queue on 24 Oct 2026.

<a id="recurring-shapes"></a>

## The thirty recurring shapes

More useful than any single entry — these are the bug families to check for
*before* shipping the next thing, and each has now bitten more than once.

Link here as `#recurring-shapes`, never as `#the-twenty-two-...` — the count in
the heading changes every time a shape is added, and the last time it did, five
inbound links were left pointing at the previous count's slug.
[`scripts/check_backlog_links.py`](../../../scripts/check_backlog_links.py) catches
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
([G-CLEANUP](../workers.md#g-cleanup-consolidator_statelast_cluster_index-is-not-dead-weight--do-not-drop-it))
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
[`scripts/cue_reach_report.py`](../../../scripts/cue_reach_report.py) printing the
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

**26. A tool built to make a decision, which does not exercise the path the
decision governs.** Not a measurement error: everything the tool reports is
correct, and its own tests pass. The flaw is an omission, which no assertion about
its outputs can catch. H49 is the case — `tools/tts_lab` auditioned engine output
while the app played that output through four shaping stages, so every reference
and every generation knob was chosen against a signal production never emits. The
tool was written before those stages existed and each one was added with a test
proving it worked; none had a reason to ask whether the audition still previewed
anything.

The tell is a tool and a production path that share a model but not a code path,
and the smell is strongest when the tool predates the pipeline. Distinct from shape
12: there two correct components fault at their interface, here two correct
components never meet, and the only instrument that can notice is a human saying
"it sounded better in the other one".

**27. A lossy transform whose output is well-formed.** The corruption survives
because it does not look like corruption. H50 deleted every character without an
ASCII spelling from her stored replies — for the entire history, 448k characters
with not one survivor — and went unnoticed because what it produced was clean,
readable, plausible text: "Kamenn Poruba" reads like a place, where mojibake
("KamennÃ¡") or a replacement character would have been reported within a day. The
severity and the detectability ran in opposite directions, so a filter with a 100%
loss rate was quieter than one that mangles a single byte.

Two tells. A filter defined by what it *keeps* rather than by what it removes —
`32 <= ord(ch) <= 126` names an allowed set and is silent about the cost of
everything outside it, where "drop control characters" names the target and cannot
overreach. And an invariant that is never asserted anywhere: no test said her
transcript could hold a letter she can pronounce, so nothing failed. When a store
is a lossy funnel, compare it against a sibling store that is not — memories and
concepts had been keeping accents the whole time, and one query across both would
have shown it.

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

**28. A rate measured over a window shorter than the cadence it is measuring.**
H53 is the case: `concept_learning_block` is a deliberately monthly beat, was
read over an eighteen-day table, and reported `0 / 961`. Nothing was broken, and
the instrument was correct at every step — it simply cannot see an event that
falls outside its window, and it says nothing about the window when it reports
the rate.

What makes this worse than an ordinary small sample is that density reads as
authority: `0 / 1,062` looks like a more thorough refutation than `0 / 20`, when
in fact the two are equally uninformative if neither window contains the event.
The tell is a rate quoted as `n / turns` against a gate expressed in days.
**Rule: before calling a rate of zero a defect, compare the instrument's age to
the feature's cadence; if the window is shorter, report the window instead of the
rate.** Related to shape 23 (a note that outlives its truth) from the other end —
there the claim aged out, here the claim was never old enough.

**29. A "this already happened" flag stored on a row that a size cap can
evict.** The flag's lifetime is the row's, the fact's lifetime is forever, and
every consumer that reads it as permanent becomes a bug the moment the cap
trips — usually by redoing something that must happen once.
[H22](health.md#h22-k73-named-one-ritual-then-locked-itself-shut-forever)
produced three separate defects from this in a single function: a permanent
record starving the pending budget, a detector ranking the record against new
candidates, and finally eviction resurrecting an offer — an evicted ritual that
still qualified re-entered as brand-new, was announced a *second* time, and its
refreshed `first_seen` then made it new enough to evict a genuinely older row,
rotating the loop through the whole record. The live store sat at 17 against a
cap of 18. **Rule: a flag recording that something irreversible has happened
does not belong on an evictable row — give it a ledger whose lifetime matches
the fact.** The near-miss detail is worth keeping too: the test that evicted
forty rows passed **no candidates**, so nothing it dropped could come back. A
cap test that never re-supplies the evicted item cannot see this shape at all,
which is the same fixture blind spot as shape 12.

**30. One pattern used as both the validator and the sanitiser, so strictness
leaks.** Two jobs with opposite risk profiles share a regex: *interpreting* a
tag body wants to be strict, because garbage must not drive behaviour, while
*stripping* it wants to be permissive, because anything tag-shaped must never
reach the user. Sharing one pattern silently couples them, and the coupling runs
the wrong way — the same refusal that skips the side-effect also skips the
strip, so the stricter the parser, the more of her private grammar gets spoken
aloud.

The observed case was one space. `[[reaction: wistful+blush]]` is a legal stack
by every rule the persona teaches; `([\w+]+)` does not admit the space, so the
tag was not "ignored" — it was rendered into the chat bubble, spoken by TTS, and
written to `messages`, twice in one day. The mood was lost as well: no
expression change, no affect impulse. Note the comment above the pattern had
already reasoned about this and concluded the opposite way ("the parser still
rejects `[[reaction:hello world]]` and other garbage tokens"), which is the tell
— **a strictness argument that never names what happens to the text it
refuses.** `split_reaction_stack` even trimmed whitespace defensively, with a
docstring calling it "more of a defensive nicety than a real concern"; the
nicety was load-bearing and the concern was real.

Two things make this worse than a normal parse bug. It **compounds through the
transcript**, exactly as in
[H37](health.md#h37-she-learned-to-write-a-heart-as-the-number-three-from-us)
— the stored reply is what she reads back as her own voice, so a leaked tag
teaches her the malformed form and the second occurrence is plausibly caused by
the first. And it was
**latent in eight tags at once** (reaction, prosody, arc, overlay, outfit,
motion, touch, predict), because they were written to one house style; only the
tag she happened to fumble showed it.

**Rule: strip broadly, interpret narrowly, and never with the same expression.**
A permissive backstop over the tag *vocabulary* removes anything shaped like a
closed tag regardless of body
([`_META_TAG_BACKSTOP_PATTERN`](../../../app/core/services/response_text_service.py)),
and the strict patterns keep deciding what actually fires. The generalisation
beyond tags: wherever a predicate gates both "act on this" and "hide this",
split it, because the failure mode of the second is always louder than the
failure mode of the first.
