# Shipped: K92–K95 — the third pass at leading

Part of the [shipped log index](../shipped.md). The open remainders — **K92
phase 3** and **K93's cue-pool half** — stay in
[`patterns.md`](../patterns.md), which links back here for the history.

Split out from `patterns.md` because
[`patterns-k31-k60.md`](patterns-k31-k60.md) had already grown to hold K31
through K91 and 1,356 lines, and this family added 645 more.

---

## Why the family exists

The will family (K52–K56) shipped the *permission* to lead. The second pass
(K85–K90) shipped the *inventory* to lead with, on the correct diagnosis that
permission was not the constraint. Both families are live and firing. The
number K90 exists to answer says neither worked:

| window | turns | ends-Q | words (med) | anaph | echo | own |
| --- | --- | --- | --- | --- | --- | --- |
| before 2026-08-09 | 1867 | 18.1% | 23 | **18%** | 19% | **77%** |
| since 2026-08-09 | 320 | 6.2% | 31 | **18%** | 20% | **71%** |

The anaphoric-opener rate — K88's own target, and the one metric here that is
independent of reply length — did not move by a single point. Own material, the
number K90 said out loud it wanted *up*, went down six. What did change is that
replies got 35% longer and she almost stopped ending on questions. Read
together: **she now writes noticeably more, about his subject, and asks about it
less.** That is not more agency; it is a more talkative follower. (Caveat worth
keeping: 320 turns is a modest sample and the length change mechanically dilutes
a ratio of own words to total words, which is exactly why the flat 18% is the
line to trust.)

So the third pass starts from a different diagnosis than either predecessor. It
is not permission and it is not inventory. Measured on the same telemetry, one
turn carries a **median of 30 rendered prompt blocks in ~74,000 characters**, of
which the blocks that ask her to bring something of her own are **two or three
of them and about 500 characters — 0.7% of the prompt**. `wants_block` alone is
present on 78% of turns. Nothing arbitrates between them, nothing represents
*following* as a choice she is making rather than the absence of a directive,
and nothing anywhere lets her decide to say less. Ten quiet permission slips at
the bottom of a very long prompt is a different failure from having none, and it
is not fixed by adding an eleventh.

### What the week actually taught

**K92 phases 1–2, K94, K95 and K93's ledger half all shipped 15–19 Aug**, and
the phasing paid for itself repeatedly: **every one of the three items filed as
a feature turned out to be a duplicated or unenforced decision instead.**

- A ceiling that was **recorded and not obeyed** (K95).
- **Four independent copies** of `max(wants, key=pressure)`, ranking by a clock
  (K93).
- **One habit two detectors could both speak to**, with no arbitration (K94).

That is the shape to check for before phase 3, because phase 3 is where the
arbiter finally gets to suppress providers instead of reporting on them — so it
will multiply this class of bug if the picks are not already shared.

Both problems phase 1 handed phase 2 also turned out to be **measurement
errors rather than tuning problems**, which is the second transferable lesson:
`HOLD` fired zero times because it asked *how many words* on a ladder about *how
much floor*, and `arc_protected` was 65% of all clamps because `arc` is a
conversation-level label being used as a per-turn veto.

---

## K92. Conversational stance — phases 1 and 2

The motivation, the design and the still-open **phase 3** brief live in
[`patterns.md`](../patterns.md#k92-conversational-stance--one-decision-per-turn-not-ten-permission-slips).
This is what phases 1 and 2 established.

### Phase 1 shipped 15 Aug — and it is backfillable, which changed the plan

Both prerequisites were confirmed met before starting: H30's `provider` share of
declines is 0.0% since 13 Aug (was 84.5% before), and H29's ledger holds a want
minted on the 13th still growing on the 15th at pressure 0.62.

**Pure module** ([`stance.py`](../../../app/core/conversation/stance.py)): the
closed set as an explicit ladder from `HOLD` to `INITIATE`, a `_OFFERS` table
mapping rendered block → the stance it offers, `compute_ceiling` for what the
user's turn permits, and `decide` returning `min(desire, ceiling)` plus an
ordered shortlist of `(stance, block)` — no floats, per the design note.
`SUBSTANTIAL_CHARS` is shared with `initiative_director` so the ceiling and
K53's own escape hatch cannot drift apart. K95 is folded in from the start as a
**hard filter**: the test that matters pushes seven providers at once against a
direct question and asserts the ceiling still holds.

**One row per turn** in `turn_stance` (schema v36), recording `stance`,
`reason`, `desire`, `ceiling` and the shortlist. Written from
`_record_turn_stance` on the same post-turn seam and the same
`telemetry.block_chars` as G4 and K90. Nothing renders;
[`tests/test_stance.py`](../../../tests/test_stance.py) pins that as an import
edge — no module that builds the prompt may import the arbiter — and that test
is meant to be deleted when phase 2 starts.

**The plan said wait; it turned out not to need to.** Every input is durable
(`turn_prompt_blocks` for the offers, `messages` for the act, arc and text), so
unlike K90 the arbiter can be replayed over history.
[`scripts/backfill_turn_stance.py`](../../../scripts/backfill_turn_stance.py) does
that, `INSERT OR REPLACE` keyed per turn, stamping each row with the timestamp
of the turn it describes. The intended loop is therefore *edit a rule, re-run,
re-read* — which matters because phase 1's whole question is whether the set is
right, and a set you can only evaluate two weeks after each edit is a set nobody
edits.

**What the first replay over 432 turns says:**

| stance | chosen | wanted |
| --- | --- | --- |
| `HOLD` | **0 (0.0%)** | 0 |
| `FOLLOW` | 22 (5.1%) | 17 (3.9%) |
| `FOLLOW_AND_ADD` | 213 (49.3%) | 78 (18.1%) |
| `ASK` | 23 (5.3%) | 53 (12.3%) |
| `CALLBACK` | 2 (0.5%) | 6 (1.4%) |
| `SHARE` | 130 (30.1%) | **217 (50.2%)** |
| `REDIRECT` | 5 (1.2%) | 9 (2.1%) |
| `INITIATE` | 37 (8.6%) | 52 (12.0%) |

Three readings, in order of how much they should change the next phase.

**`HOLD` is unreachable, and that is the finding rather than a bug.** It was
specced as the stance for a turn with nothing on the table, and only 17 of 432
turns (3.9%) have an empty shortlist — none of which were the short beat the
rule also requires. Some provider is always offering something, which is the
"she gets a steer nearly every turn" problem stated as a single number. If
holding back is to be a real option it has to be reachable *against* offers,
which means it is not the bottom of this ladder but a separate axis. Phase 2
cannot render `HOLD` as designed.

**The interruption ceiling binds on 32.6% of turns.** K95 was filed as cheap
insurance against a regression phases 2–3 might introduce; it is already load-
bearing on one turn in three. `arc_protected` accounts for 74 of the 141 clamps,
`direct_question` for 41. Worth checking before phase 2 whether the support /
reflection cap is too broad, since it is doing more work here than any other
rule and it inherited its arc list from K53 rather than earning it.

**`SHARE` is the oversupplied stance** — wanted on 50.2% of turns, chosen on
30.1%. It is what the gap-return family, the lean blocks and the idle seeds all
resolve to, and it loses more often than anything else. That is the same
priority inversion **K93** describes, visible from the other side: the question
is not whether she takes the floor but what she has to take it *with*.

Phases 2 and 3 are unchanged and still gated on reading this against the K90
metrics over real use — the table above is a replay of turns that happened
before the arbiter existed, which is exactly as much as it claims to be.

### Phase 2 shipped 19 Aug — both of phase 1's findings turned out to be measurement errors

Phase 1 handed phase 2 two problems and a plan to render around them. Measuring
both before building says neither was what it looked like, so phase 2 is mostly
the two corrections and only then the block.

**`HOLD` was a category error, not a mis-tuned threshold.** Over 682 turns it
fired zero times, and the replay says why twice over: only 2.5% of turns have an
empty shortlist, *and his turns are never short* — 78% run 60–239 characters and
1.2% fall under the 25-character backchannel bar. Of the five turns that did
clear the bar, two are "Sorry :(" and "See you later then Aiko.", where
under-responding would be a plain error. So the rule could not be rescued by
moving the number. Every other rung answers *how much of the floor do I take*;
`HOLD` answers *how many words do I use*, and the two are independent — she can
bring something of her own in fifteen words. Brevity is therefore a **second,
orthogonal output**, keyed off her own recent replies rather than off the size of
his turn: two replies of 40+ words in a row engages it. That is also where the
measured regression actually lives (median 19 words over messages 400–1600, 34
over the last 200), so the new signal points at the problem the old one only
gestured toward. A direct question overrides it — answering something in six
words is a non-answer, not restraint.

**`arc_protected` was gagging her for days.** Phase 1 flagged it as suspicious
because the arc list was inherited from K53; the truth is worse than a broad
list. `arc` is a *conversation-level* label, not a per-turn read: over 2,355
turns it forms 137 runs averaging **17 turns**, with **not one run of length 1**,
and the longest protected spans reach **110 turns of `support` across eight
days**. Used as a per-turn hard filter, one hard thing he said on Monday
suppressed her through Thursday. K53 fires once in six turns so a sticky arc
merely damped it; a ceiling consulted every turn is a different exposure
entirely. The cap now applies only while the span is fresh
(`PROTECTED_ARC_FRESH_TURNS = 4`), which keeps the protection where it was
earned. The per-turn caps (`vent`, `direct_question`) are untouched and keep
working for exactly as long as their signal is present — which is the property
`arc` was wrongly assumed to have.

Replaying the same corpus under both corrections:

| | phase 1 rules | phase 2 rules |
| --- | --- | --- |
| held back by his turn | 252 (36.9%) | **196 (28.7%)** |
| `arc_protected` clamps | 164 (65.1% of clamps) | **79 (40.3%)** |
| `SHARE` chosen | 189 (27.7%) | **238 (34.8%)** |
| `FOLLOW_AND_ADD` chosen | 369 (54.0%) | **312 (45.7%)** |
| brevity asked for | unreachable | 80 (11.7%) |

Fifty-one turns move from "answer him and append something" to "her own
material", and the binding constraint is now `direct_question` (68) at roughly
the same weight as the arc (79) — a per-turn signal rather than a stale label.

**What renders, and what deliberately does not.** One T6 block
(`stance_block`, last in `T6_detectors`, behind `agent.stance_block_enabled`)
that speaks for `FOLLOW` and for brevity and returns `""` for every other rung.
The silence is the shape of the phase: the other five rungs already have a
provider putting a sentence in the prompt, and a second sentence agreeing with it
is the eleventh permission slip K92 exists to argue against. Both clauses it does
render are *restraint* — the one direction this family has never been able to ask
for — so neither adds to the steer budget phase 3 has to bring down.

**The ledger records the decision the prompt was built from, not a
reconstruction.** `_render_stance_block` computes the decision at assembly time
and stashes it; `_record_turn_stance` prefers the stash and only recomputes when
there is none (rendering disabled, or a backfill). This is not tidiness. By
post-turn, `_recent_reply_words` has already grown by this turn's reply and the
dialogue-act tagger has re-run, so a recomputation answers a slightly different
question and the row quietly stops describing the prompt it exists to explain.
Two tests pin it: the stash is preferred, and it is consumed so it cannot
describe two turns.

Two inputs are new session state, both reset on session switch and wipe:
`_arc_age_turns` (incremented post-turn when the new arc matches the previous
one) and `_recent_reply_words`. Both are stale by one turn at assembly, which is
fine and deliberate — an arc changes once every seventeen turns, and the case
where a stale act would matter, *he asked something*, is caught independently off
the question mark on the live text. `brevity` and `brevity_reason` are schema
v37; the three thresholds are settings-backed so the live session and
`backfill_turn_stance.py` can be pointed at the same values, and the backfill now
replays both new inputs per session and prints its rule set with its numbers.

Phase 3 is unchanged and still the expensive one. What phase 2 adds to its case:
`wants_block` now renders on **93%** of turns and `hobby_block` on **100%**, so
the two largest steers in the system are ambient by any definition, and the
arbiter's `FOLLOW_AND_ADD` share is mostly just reporting that fact back.

---

## K93. The substance floor — the ledger half

The motivation, the design and the still-open **cue-pool half** live in
[`patterns.md`](../patterns.md#k93-the-substance-floor--what-she-takes-to-the-floor-not-whether-she-takes-it).

### The ledger half shipped 19 Aug — and re-measuring first changed the diagnosis

Re-measured before designing, because the original numbers predate H29's
per-source cap. **The monopoly got worse, not better**: `curiosity_seed` is now
**217 of 244 genuine conversions (88.9%)**, up from 43 of 56 (77%). The rest of
the shelf is unchanged in character — `knowledge_gap_notice` 85 surfaced → 3
used, `concept_hypothesis` 12 → 0, `turning_over` 22 → 1, `curiosity_gradient`
29 → 1.

**H29's cap works and is not the lever.** The live ledger holds 6 wants, of which
seeds hold 4 — exactly at `wants_per_source_cap`. That reads like the monopoly
persisting, and it is worth being careful here: the cap *is* holding four slots
open for other producers, and only two got claimed, because one goal and one
pursuit are all that exist to claim them. **The two empty slots are a supply
problem, so tightening the cap would only shrink the ledger, not add substance.**
Which is what this entry said in the first place — the cap is a blunt
anti-monopoly rule, and K93 is what decides which of the survivors is worth the
floor.

**Pressure cannot decide that, and measuring why found two mechanisms rather than
one.** Pressure is a pure clock (`pressure += growth_per_day * elapsed_days`), so
ranking by it ranks by age — a fine tie-break and the wrong first key. Both of the
following favour whimsy at every age:

1. **A starting offset that never closes.** Seeds and forward-curiosity wants are
   minted at 0.15, goals at 0.05, pursuits at 0.04, and all then grow at the same
   0.25/day. A seed therefore carries a permanent ~0.11 lead — about half a day —
   over the two sources carrying anything durable. Straight off the live ledger:
   the seed at 0.60 and the goal at 0.50 are **exactly the same age (1.8 days)**.
   The seed led only because of where it started.
2. **Saturation makes it worse.** At 0.25/day every want reaches the 1.0 clamp in
   under four days, well inside the 14-day TTL, after which they all tie. `sorted`
   is stable, so the tie-break became ledger insertion order — and the worker
   ingests seeds first. Whimsy won the top slot structurally once the ledger was
   more than a few days old.

So the fix is ordering, and substance is the first key with pressure as the
tie-break — a small ordered table, not a float per want, on the same argument K92
made about handing an LLM `0.63` against `0.58`. Tiers: **anchored** (`goal`,
`pursuit`, and a `forward_curiosity` *share*, which is a subject of hers) >
**about him** (a `forward_curiosity` *ask*) > **whimsy** (`curiosity_seed`).
Unknown sources land in the middle so a producer added later inherits neither
whimsy's demotion nor a goal's promotion silently.

**One deviation from the entry's own ordering, stated because it is arguable.**
K93 ranked "something she noticed about him" *above* "a pursuit of her own".
That is inverted, for two reasons: K93's own shipping test is the K90
**own-material** rate, and the only source on that tier emits a literal
`ask {name} …` line — the interview shape K87 shipped a quota to suppress, and
which the worker's own comment already calls "an interview line under a different
label". Promoting it would move the metric this entry exists to move, downward.

**Where it applies, and the one trap.** Every site that picks a want to *say* now
goes through `strongest_for_floor` — there were four picking independently with a
copy of the same `max(…, key=pressure)`, and two of them are supposed to agree
*exactly* (the imperative render, and the K55 thread-open recording what she was
told to raise; divergence there opens a thread on a subject the prompt never
mentioned). The trap is that every consumer already had a pressure bar of its own,
so the helper **filters before it ranks**: reorder across K54's
`appetite_min_want_pressure` and a fresh goal displaces a qualifying seed and then
fails the gate, turning "a better offer" into no offer at all. Filter, then rank,
and each caller's fire rate is untouched.

Two things deliberately unchanged. The **soft wants list stays in pressure order**,
so whimsy keeps its voice — it is part of her character and still converts better
than anything else on the shelf; what it loses is the scarce slots. And whether the
imperative band fires at all is untouched: the qualifying set is a prefix of
pressure order, so it is non-empty on exactly the turns the old test admitted. A
test pins that, because a change here that made her go imperative *more* often
would be a regression wearing this entry's clothes.

Dry-run against the live ledger, which is the whole of the claim: K53's directive
content and K54's offer both move from a seed at 0.60 to her goal at 0.50, and the
imperative band stays empty. Gated on `agent.wants_substance_ordering` (default
on) for the A/B, and `get_wants_state` now lists wants in pick order with their
tier, so the debug view answers "what would she raise next?" rather than "what has
waited longest?".

---

## K94. Sequencing — answer first, then add, and say where the addition goes

**Motivation.** Every mechanism in the family selects a *subject* and none of
them says anything about *placement*, yet placement is what the one honest
metric measures. K88's anaphoric-opener rate is specifically about her **first
sentence**, and it is the number that did not move at all across the whole
second pass (18% before, 18% after). Meanwhile the persona already contains
several rules pushing in this direction — lead with the substance, don't parrot,
vary the opener, move the reaction word a few words in — and they have not
shifted it either, which suggests the instruction she is missing is not another
prohibition on how to open but a positive account of the reply's *shape*.

The useful observation is that responsiveness and opener ownership are only in
tension if the reply is treated as one undifferentiated blob. "Answer his point,
but not in the first clause" and "put your own thing in the last sentence and
leave it open" are compatible with answering him completely. That decouples
being a good listener from opening on his words, which is exactly the knot the
last two families tried to cut by pushing her to change the subject instead —
the far more expensive move, and the one she sensibly refuses. It also gives
`FOLLOW_AND_ADD` (K92) an actual definition instead of a vibe, and it gives the
wants ledger's "spend one when a lull lands" somewhere concrete to land: the
end of a reply she was going to write anyway, rather than a pivot she has to
justify.

**Cost.** Very low — this is prompt-side, one or two sentences, and it is the
one item here that could be tried tonight as a persona edit before any code
exists. The risk is a formulaic reply shape (answer-then-tack-on, every turn),
so it wants a cadence rather than a standing rule, which is an argument for
attaching it to a stance (K92) rather than to the persona. Key files:
[`data/persona/aiko_companion.txt`](../../../data/persona/aiko_companion.txt) for
the trial, then the `FOLLOW_AND_ADD` rendering and
[`conditional_handling.txt`](../../../data/persona/conditional_handling.txt) for
the real version. Measured by K88's anaphoric rate and the opener histogram in
[`lead_follow_report.py`](../../../scripts/lead_follow_report.py) — the current top
openers are `that` ×59, `i` ×51, `you` ×43, `then` ×31.

### Shipped 19 Aug — as a stance axis, and the cadence is the whole design

The premise held up on re-measure. The anaphoric-opener rate is **16% over the
last 7 days, 18% over 30, 17% all-time** — flat across every window, exactly as
this entry predicted, and still flat after two families of persona rules aimed at
it. Two other numbers shaped what shipped: own-material is **falling** (75%
all-time → 69% at 30d → 67% at 7d) while replies got longer (median 26 → 33
words), which is the "more talkative follower" reading getting worse, not better.

**Attached to the stance, per this entry's own recommendation, as a third axis.**
`FOLLOW_AND_ADD` is the rung that means "answer him and bring something", so it is
the only one where placement is even a question — and it gives that rung the
definition K92 admitted it lacked. Sequencing sits beside `brevity` rather than
inside `stance` for the same reason brevity does: how much of the floor she takes,
how many words she uses, and *where in the reply her own material goes* are three
independent questions, and collapsing any two loses whichever was asked second.

**The cadence is the design, not a tuning detail.** `FOLLOW_AND_ADD` is chosen on
45.7% of turns. A placement clause on all of them would be ambient by K92's own
definition — the exact thing that entry exists to argue against — and formulaic by
this entry's own warning. So it fires only when **her previous reply actually
opened anaphorically**, read off K88's tracker. That makes it evidence-driven and,
better, **self-extinguishing**: stop opening that way and it stops asking. Measured
over the same 683-turn replay it lands on **44 turns (6.4%)**, in the same band as
brevity's 11.7% and nowhere near 46%.

**It stands down when K88's band is already speaking.** `style_pattern_block`
addresses the same habit from a twelve-turn window with a cooldown; two voices on
one habit in one prompt is precisely the crowding K92 exists to arbitrate. The
check costs nothing because the arbiter is already handed the offer set — the
suppression is just `"style_pattern_block" in blocks`, which means it also replays
correctly in the backfill with no new input.

**What the copy deliberately does not say.** Nothing about ending on a question.
Her question-ending rate is already down to **3.1% from 14.3% all-time**, and this
entry's "leave it open" phrasing, read by a model as "ask him something", walks
straight back into the interviewing pattern several other features were built to
suppress. The addition goes last *as a statement he can pick up*. The clause also
says "answer him fully — just not in your first clause", because the one way this
could do real damage is being read as licence to under-answer; a test pins both.

Sequencing is `turn_stance` schema **v38**, gated on
`agent.stance_sequencing_enabled`, and `backfill_turn_stance.py` replays it with a
`--no-sequencing` A/B. One honest limitation, documented at the seam: the
**post-turn recompute path leaves the column at 0**. By then the style tracker has
already ingested this turn's reply, so reading it would ask whether the reply
answered the cue meant to shape it — circular rather than merely stale like the
other two inputs there. The backfill replays `messages` in order and can see the
previous reply, so it is the honest source for history.

**What to read in a week.** The anaphoric rate in
[`lead_follow_report.py`](../../../scripts/lead_follow_report.py), against the 16% /
18% / 17% baseline above, plus the opener histogram (currently `i` ×73, `that` ×49,
`then` ×41, `you` ×34 — note `i` has already overtaken `that` since this entry was
filed). Two ways this fails that the rate alone will not show: replies that open on
her own footing and then *thin out* the answer, and a formulaic shape appearing on
the 6.4%. Both need reading turns, not counting them.

---

## K95. Interruption cost — a direct question is not an opening

**Motivation.** Insurance, and cheap. If K92–K94 work at all, the first
regression will be her leading over the top of something he actually asked, and
that single failure will cost more trust than a week of good initiative earns.
The only guard that exists today is a length proxy: K53 declines with
`user_substantial` when his message is 240 characters or more
([`initiative_director.py`](../../../app/core/conversation/initiative_director.py)),
which correctly protects a long explanation and does nothing at all for a short
direct question — and short direct questions are the case where taking the floor
reads worst.

What is missing is a small read of *what his turn was doing*: did it end on a
question mark, is it the second or third turn of one explanation he is in the
middle of, is he working through a task with her, is he venting. Most of those
signals already exist and are not consulted for this purpose — K4 dialogue-act
tags, K69's vent-vs-fix-vs-reassure read, the arc, and K14 engagement. The
output should be a **hard filter on the candidate set rather than another
weight**: when he asked something directly, `INITIATE` and `REDIRECT` are simply
not available this turn and `FOLLOW_AND_ADD` is the ceiling. Encoding it as a
score invites it to be outvoted by an accumulated want, which is precisely the
failure being insured against.

**Cost.** Low, and it composes with everything else — a pure function taking his
last turn plus a couple of turns of context and returning a cost band, consumed
by K92's candidate filter (or, before K92 exists, wired straight into K53's gate
walk as a second `user_substantial`-style reason so it earns its keep
immediately). Key files: new predicate in
[`app/core/conversation/`](../../../app/core/conversation/),
[`initiative_director.py`](../../../app/core/conversation/initiative_director.py)'s
`decide`, the dialogue-act tags, and K69's read. Cross-refs: the mirror image of
**K82** (the dropped sub-topic — he said three things and she answered one),
which is the same "read what his turn was actually doing" capability pointed at
completeness instead of at turn-taking; the two should probably share the reader.

### Shipped 19 Aug — filed as insurance, found already load-bearing

The escape hatch in the paragraph above is what shipped, and the entry was wrong
about only one thing: this was never insurance against a *future* regression.

**The reader shipped inside K92 and nothing enforced it.** Phase 1 folded K95 in
as `compute_ceiling`, which caps the stance ladder at `FOLLOW_AND_ADD` on a direct
question, and phase 2 measured that cap binding on 68 turns. But the stance block
speaks only for `FOLLOW` and brevity, so the ceiling was **recorded and not
obeyed** — and K53, the most deliberate floor-taking move Aiko makes, still gated
on his message being 240+ characters, exactly the length proxy this entry was
filed to replace. Joining the two ledgers: `initiative_block` rendered on 75
turns and **17 of them (23%) sat under a `direct_question` ceiling the director
could not see.** Roughly one in four of her deliberate floor-grabs landed on top
of a question, and the arbiter had already written down that it shouldn't.

**Enforcing it costs no initiative, which is why it was safe here.** This family's
measured problem is too *little* own material, so adding a gate to K53 needs an
argument. K53's counter resets only when the directive actually fires (the
`user_substantial` precedent), so the gate **defers** the beat to the next
non-question turn rather than spending it: same rate, better placement. That is
K94's insight — placement, not frequency — arriving early and for free. A test
pins it: eight consecutive question turns cost nothing and the directive lands on
the ninth.

**One predicate, two consumers.**
[`turn_shape.py`](../../../app/core/conversation/turn_shape.py) holds
`is_direct_question`; `stance.compute_ceiling` and `initiative_director.decide`
both call it, and a test asserts the *same function* backs both rather than two
copies that agree today — a ceiling recording `direct_question` while the prompt
carries a floor-taking directive is precisely the failure being prevented, so it
must not be reachable by drift. `stance.py` had already learned this with
`SUBSTANTIAL_CHARS`. The two signals are OR-ed (K4 act tag, trailing question
mark) because the tag is stamped post-turn and lags a turn, so taking either means
a stale tag can only ever *add* a deferral — the safe direction for a guard, since
a false positive costs one deferred beat and a false negative is her talking over
him.

Gated on `agent.initiative_respect_direct_question` (default on), which exists to
A/B the placement change rather than because the old behaviour is wanted. The gate
sits above the length hatch so a long question reports the more specific reason;
`force` still bypasses it, or the MCP repro tool could not reproduce a directive on
a question turn.

**What this does not do.** The ceiling's other two caps (`planning`, and `vent` via
K69) are still recorded and unenforced. `planning` was left out deliberately: it is
available only as the lagging act tag with no live counterpart, so as a hard gate it
would suppress initiative on turns where he has already moved on, and it accounted
for 20 of 683 turns against the question's 68. The general fix for all of them is
K92 phase 3, where the arbiter suppresses providers instead of reporting on them.
