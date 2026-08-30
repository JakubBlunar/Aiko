# H-series — health audit of shipped work

This file is **not a feature queue**. Every entry is a shipped feature that was
measured against the live install and found to be doing something other than
what its shipped entry claims. Nothing here is a new idea; the design work is
already done and already merged. The only question each entry answers is *does
it actually run, and does it change behaviour?*

It is kept separate from [`concepts.md`](concepts.md) and
[`patterns.md`](patterns.md) on purpose — mixing "this is broken" into a list of
"this would be nice" is how broken things stay broken. Same spirit as the
A-series ([`architecture.md`](architecture.md)) and P-series
([`perf.md`](perf.md)).

**What lives where.** This file holds the entries with **work still to do**,
plus the [recurring-shapes catalogue](#recurring-shapes), which is the part
worth re-reading before shipping anything. Entries whose fix has shipped and
been verified move to [`shipped/health.md`](shipped/health.md) with their full
text intact, and the [status index](#status-index) below lists every entry
either way. The file had grown to 53 entries of which 43 were finished, which
made "what is still open" a research task in its own right.

**Original audit date:** 2026-08-11, over a 4,039-message corpus spanning
2026-05-21 to 2026-08-10. Parts 1-4 are described in
[`shipped/health.md`](shipped/health.md); the audit's organising finding and its
closing summary live there too, since every row of it has since been fixed.

**Re-measure before acting on any entry** — several of these are rate problems,
and a rate that was wrong in August may be right in October. Verification
passes on 28-29 Aug re-read nine entries and changed the conclusion on four of
them.

---

## Status index

All 54 entries, so "what is still open" is one table rather than a read of the
whole file. Closed entries keep their full text in
[`shipped/health.md`](shipped/health.md) — an audit entry is worth more after it
is fixed than before, because the *shape* is the reusable part.

### Open (11)

| # | severity | entry |
| --- | --- | --- |
| H2 | medium | [L42's other two detectors have thresholds unreachable on real data](#h2-l42s-other-two-detectors-have-thresholds-unreachable-on-real-data) |
| H4 | medium | [The cue shelf produces well and spends badly](#h4-the-cue-shelf-produces-well-and-spends-badly) |
| H7 | medium | [The hypothesis loop invents and never adjudicates](#h7-the-hypothesis-loop-invents-and-never-adjudicates) |
| H8 | — | [Cold-start and supply — measured, and *not* bugs](#h8-cold-start-and-supply--measured-and-not-bugs) |
| H11 | medium | [The prompt tells her what she won't do 4.5× more than how she feels](#h11-the-prompt-tells-her-what-she-wont-do-45-more-than-how-she-feels) |
| H14 | medium | [Almost nothing feels bad](#h14-almost-nothing-feels-bad) |
| H17 | high | [Four prompt blocks disagree about what a lull is, and three lose](#h17-four-prompt-blocks-disagree-about-what-a-lull-is-and-three-lose) |
| H34 | medium | [Echo is a reliable measure, and it is not a measure of him](#h34-echo-is-a-reliable-measure-and-it-is-not-a-measure-of-him) |
| H35 | low | [A third of the surfacing ledger cannot be scored by either signal](#h35-a-third-of-the-surfacing-ledger-cannot-be-scored-by-either-signal) |
| H44 | medium | [Nothing has ever graduated, and at this calibration nothing can](#h44-nothing-has-ever-graduated-and-at-this-calibration-nothing-can) |
| H54 | medium | [`topic_miss` at 95% of eligible declines is not, by itself, a starvation](#h54-topic_miss-at-95-of-eligible-declines-is-not-by-itself-a-starvation) |

### Closed (43) — in [`shipped/health.md`](shipped/health.md)

| # | severity | entry |
| --- | --- | --- |
| H1 | high | [L42 conduct is latched shut after one silent LLM failure](shipped/health.md#h1-l42-conduct-is-latched-shut-after-one-silent-llm-failure) |
| H3 | medium | [L17f's diary drains 12 events a week against 55 arriving](shipped/health.md#h3-l17fs-diary-drains-12-events-a-week-against-55-arriving) |
| H5 | low | [L41's change framings never fire, and L38's standing never gets named](shipped/health.md#h5-l41s-change-framings-never-fire-and-l38s-standing-never-gets-named) |
| H6 | low | [L17e's reflection slip is silent for 30 days at a time](shipped/health.md#h6-l17es-reflection-slip-is-silent-for-30-days-at-a-time) |
| H9 | high | [Her emotions have no dynamic range — every topic feels identical](shipped/health.md#h9-her-emotions-have-no-dynamic-range--every-topic-feels-identical) |
| H10 | high | [Her internal conflicts never reach the prompt](shipped/health.md#h10-her-internal-conflicts-never-reach-the-prompt) |
| H12 | medium | ["Us" is not a first-class subject](shipped/health.md#h12-us-is-not-a-first-class-subject) |
| H13 | medium | [She has very little life that is not about him](shipped/health.md#h13-she-has-very-little-life-that-is-not-about-him) |
| H15 | medium | [The diary's salience floor selects by shape, not by significance](shipped/health.md#h15-the-diarys-salience-floor-selects-by-shape-not-by-significance) |
| H16 | low | [Her relationship tensions are five things said twenty-five times](shipped/health.md#h16-her-relationship-tensions-are-five-things-said-twenty-five-times) |
| H18 | high | [L38 spent three months learning from a coin flip](shipped/health.md#h18-l38-spent-three-months-learning-from-a-coin-flip) |
| H19 | high | [Two workers were asking the database for a session that does not exist](shipped/health.md#h19-two-workers-were-asking-the-database-for-a-session-that-does-not-exist) |
| H20 | high | [The fact-checker was being asked to verify the claim "2026"](shipped/health.md#h20-the-fact-checker-was-being-asked-to-verify-the-claim-2026) |
| H21 | high | ["How Jacob writes lately" said the same sentence for eleven weeks](shipped/health.md#h21-how-jacob-writes-lately-said-the-same-sentence-for-eleven-weeks) |
| H22 | high | [K73 named one ritual, then locked itself shut forever](shipped/health.md#h22-k73-named-one-ritual-then-locked-itself-shut-forever) |
| H23 | medium | [The associative-wander bar was set below the floor of the distribution](shipped/health.md#h23-the-associative-wander-bar-was-set-below-the-floor-of-the-distribution) |
| H24 | high | [Every continuity signal she has is scoped to the thing that resets](shipped/health.md#h24-every-continuity-signal-she-has-is-scoped-to-the-thing-that-resets) |
| H25 | high | [The crash was in a language that has no stack trace](shipped/health.md#h25-the-crash-was-in-a-language-that-has-no-stack-trace) |
| H26 | high | [Every signal said healthy, and the recovery button was a no-op](shipped/health.md#h26-every-signal-said-healthy-and-the-recovery-button-was-a-no-op) |
| H27 | high | [Two workers burned an hour of GPU to write nothing, in a log that said so](shipped/health.md#h27-two-workers-burned-an-hour-of-gpu-to-write-nothing-in-a-log-that-said-so) |
| H28 | high | [A different block spent K52's fuel, and three features starved](shipped/health.md#h28-a-different-block-spent-k52s-fuel-and-three-features-starved) |
| H29 | high | [A want cannot outlive two showings of its source cue](shipped/health.md#h29-a-want-cannot-outlive-two-showings-of-its-source-cue) |
| H30 | low | [Half the cue declines still say only "provider", in nine specific cues](shipped/health.md#h30-half-the-cue-declines-still-say-only-provider-in-nine-specific-cues) |
| H31 | medium | [Every turn was mined five times, and the fifth pass is where she moved a hobby](shipped/health.md#h31-every-turn-was-mined-five-times-and-the-fifth-pass-is-where-she-moved-a-hobby) |
| H32 | low | [The one cue that never fired, and the two numbers that said so were both invented](shipped/health.md#h32-the-one-cue-that-never-fired-and-the-two-numbers-that-said-so-were-both-invented) |
| H33 | medium | [The vector store had a write path, a read path, and no third one](shipped/health.md#h33-the-vector-store-had-a-write-path-a-read-path-and-no-third-one) |
| H36 | medium | [Nine remembered arguments, and all nine were the clock](shipped/health.md#h36-nine-remembered-arguments-and-all-nine-were-the-clock) |
| H37 | medium | [She learned to write a heart as the number three, from us](shipped/health.md#h37-she-learned-to-write-a-heart-as-the-number-three-from-us) |
| H38 | medium | [She was in pajamas at five in the afternoon, and the weather did it](shipped/health.md#h38-she-was-in-pajamas-at-five-in-the-afternoon-and-the-weather-did-it) |
| H39 | medium | [A conversation-level label used as a per-turn veto muted her for eight days](shipped/health.md#h39-a-conversation-level-label-used-as-a-per-turn-veto-muted-her-for-eight-days) |
| H40 | high | [She asked about a delivery she had already helped unpack](shipped/health.md#h40-she-asked-about-a-delivery-she-had-already-helped-unpack) |
| H41 | high | [Not one of his promises had ever been resolved, and no deadline was readable](shipped/health.md#h41-not-one-of-his-promises-had-ever-been-resolved-and-no-deadline-was-readable) |
| H42 | — | [She had not wondered anything for seven days, and the log said healthy](shipped/health.md#h42-she-had-not-wondered-anything-for-seven-days-and-the-log-said-healthy) |
| H43 | — | [The gate blamed for 94.5% of her silences accepts a third of everything](shipped/health.md#h43-the-gate-blamed-for-945-of-her-silences-accepts-a-third-of-everything) |
| H45 | high | [The test suite was choosing which conversation she woke up in](shipped/health.md#h45-the-test-suite-was-choosing-which-conversation-she-woke-up-in) |
| H46 | medium | [A quarter-second of speech after the sentence, that nothing could retract](shipped/health.md#h46-a-quarter-second-of-speech-after-the-sentence-that-nothing-could-retract) |
| H47 | — | [She was shown 604 of them and said nothing, which is not a supply problem](shipped/health.md#h47-she-was-shown-604-of-them-and-said-nothing-which-is-not-a-supply-problem) |
| H48 | — | ["She stopped being lively" — the fix that was measured on the wrong engine](shipped/health.md#h48-she-stopped-being-lively--the-fix-that-was-measured-on-the-wrong-engine) |
| H49 | — | [The lab was auditioning a different voice than the app played](shipped/health.md#h49-the-lab-was-auditioning-a-different-voice-than-the-app-played) |
| H50 | — | [Her transcript could not hold the words she said](shipped/health.md#h50-her-transcript-could-not-hold-the-words-she-said) |
| H51 | — | [Marking a belief correct was indistinguishable from deleting it](shipped/health.md#h51-marking-a-belief-correct-was-indistinguishable-from-deleting-it) |
| H52 | — | [The 2,700 tokens of grammar blocks are the cheapest text in the prompt](shipped/health.md#h52-the-2700-tokens-of-grammar-blocks-are-the-cheapest-text-in-the-prompt) |
| H53 | — | [Four fully wired reasoning blocks, none of which has ever rendered](shipped/health.md#h53-four-fully-wired-reasoning-blocks-none-of-which-has-ever-rendered) |

---

## What to do next

Written 29 Aug, replacing the running order-of-work log that is now an
[appendix](shipped/health.md#appendix-the-working-queue-as-it-was-kept) to the
closed file. That log is worth reading once for its **retractions** — three
separate passes reported a cue as dead on a denominator that meant "never had a
chance", and the third happened after the warning was written down — but it is
no longer a to-do list.

**One finding was treated as bigger than any open entry, and it has an
entry now.** [H54](#h54-topic_miss-at-95-of-eligible-declines-is-not-by-itself-a-starvation)
is the 1,703 / 1,759 `topic_miss` headline: it is still ~96% of eligible
declines a week later, and that is not by itself evidence the five topic-gated
cues are starving. Read it before touching those cues' gates, [H4(a)](#h4-the-cue-shelf-produces-well-and-spends-badly),
or [H17](#h17-four-prompt-blocks-disagree-about-what-a-lull-is-and-three-lose)
on the theory that a shared opening is the missing piece. Four of the cues that
looked dead on the raw table turned out to be at 100% reach.

**Then, roughly by value per unit of risk:**

1. **H7's resolver gap, then H44.** Four hypotheses were asked, answered, and
   never scored — which is where graduation's missing second confirmation should
   come from. Do this before touching `hypothesis_graduate_min_support`; under
   one reading the bar needs no change at all.
2. **H2.** The 19 Aug reading settled that `kind='conduct'` at 0 of 3,294
   concepts is the *detector*, not H1's latch, so H2 is no longer blocked on
   anything. Read the `conduct.gate shape=… declined_on=… reading=…` lines,
   which exist for exactly this.
3. **H11 — the ratio itself.** Now judgeable: H10 put a generative kind into the
   lane and it has been taking 28.6% of surfacings against `affective`'s 7.1%.
4. **H14.** Newly actionable. H9's feed recovered (5 real `neg` clusters);
   minting still could not fire because only one was annotatable and L13
   required two. The singleton strong-neg exception, polarity-balanced focus,
   succession skip, and T3 evidence-cluster boost shipped 30 Aug 2026. Keep
   this entry open until a later pass shows at least one true-negative *user*
   affective that also appears in T3 on a matching-topic turn. Watch-line:
   `python scripts/concept_openness_report.py` → Affect polarity.
5. **H8.** Decisions to record rather than code to write.

**H34 and H35 block nothing** — both are measurement-boundary entries, and H34's
group means need the message-length control every time.

### Carried forward from closed entries

Decisions the closed entries deliberately left rather than defects, listed here
because a "deliberately not done" buried in a fixed entry is invisible:

- **K73's ritual detector counts calendar cells** ([H22](shipped/health.md#h22-k73-named-one-ritual-then-locked-itself-shut-forever)).
  It produced 17 rituals from one habit — *"our Saturday-morning check-ins"*
  beside *"our late-night Tuesday check-ins"*, every one of them the arc
  detector's fallback shape — and she declined all eleven offers. The
  re-announce loop is fixed, so the feature is safely quiet; making it *work*
  means deciding what distinguishes a ritual from a habit.
- **K13's fifth style axis has never spoken under either design**
  ([H21](shipped/health.md#h21-how-jacob-writes-lately-said-the-same-sentence-for-eleven-weeks)).
  `question` is dead in both directions, so the block is really a four-axis
  instrument. Worth deciding whether the axis stays.
- **K54's short-reply bar is an absolute char count**
  ([H17](#h17-four-prompt-blocks-disagree-about-what-a-lull-is-and-three-lose)).
  Reply lengths drifted and the gate swung from 90.9% open to 10.5% open with no
  code change. Every relative replacement tried was worse, because reply length
  is strongly autocorrelated; that is why nothing shipped.
- **The fact-checker's claim selector is keyed to the old unit**
  ([H20](shipped/health.md#h20-the-fact-checker-was-being-asked-to-verify-the-claim-2026)).
  It extracts from 6 of 59 knowledge rows because it still demands a named
  entity the sentence unit does not need. Widening it costs a search plus a
  round trip per false positive, so the trade is recorded rather than taken.

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

### Re-measure 30 Aug 2026 — the map recovered; minting still could not fire

H9's feed fix worked. User cluster affect map: **59** clusters, **5** with
valence ≤ −0.20 (was 0; old floor −0.112). Active `kind=affective`: user
**68 / 0 negative**, aiko **175 / 0 true self-negative**. Candidates did not
fill the gap.

| cid | valence | samples / valence_samples | bucket | annotatable (≥3 both + label)? | label |
| --- | --- | --- | --- | --- | --- |
| 47 | −0.46 | 2 / 2 | neg/mid | no | promised: talk about dreams / inner speech |
| 53 | −0.40 | 1 / 1 | neg/mid | no | orphan (not in `topic_clusters`) |
| 51 | −0.40 | 1 / 1 | neg/mid | no | multi-provider LLM integration |
| 65 | −0.40 | 1 / 1 | neg/low | no | orphan |
| **30** | **−0.272** | 21 / **3** | neg/mid | **yes** | Shared moment (tender): bedtime cuddles / comfort |

Only cid 30 cleared the annotation floor, so L13's two-cluster rule still
could not mint. **Do not mint cid 30 as-is** (−0.272 is just past the `neg`
cut on a tender cluster; likely tiredness at bedtime, not "cuddles drain
him").

Shipped 30 Aug 2026, still this entry (do not move to shipped until a
true-negative user affective also reaches T3 on a matching-topic turn):

- 1-cluster exception at `|valence| >= 0.35` with `valence_samples >= 3`
  (`memory.concept_synthesis_affect_singleton_abs_valence`). Pair-floor
  stays the default. Mild-neg (cid 30) stays out; 47/51 can mint once they
  earn samples.
- Valence-band grouping in the user proposer (`neg/mid` + `neg/low` may
  share "drains him").
- Polarity-balanced focus: reserve one slot for a dirty `neg` cluster so
  large warm ones cannot starve it.
- Succession: an existing warm affective that cites a now-`neg` focus
  cluster is listed as superseded and cannot be reinforced.
- T3 evidence-cluster boost: when the live topic is in an affective
  concept's cluster evidence, treat that as a context hit (not a global
  preference for negative feelings).
- Watch-line: `python scripts/concept_openness_report.py` → Affect polarity.

Do not lower the global sample floor. Do not copy guilt out of `value`.
Do not pin affective into the core lane.

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

### Re-measured, 28 Aug: the fix holds, and the constant beside it is the one to watch

**K54 fires.** `topic_appetite_block` rendered on 3 of 1,062 turns — 14, 15 and
21 Aug — against "never, 0 of 146" when this was filed. 3 turns is 5% of the 60
conversations in the corpus, and the block is once-per-conversation by design,
so that is a rare permission slip behaving like one rather than a gate still
stuck shut.

**The extraction held, which is the part most likely to have rotted.** Every
consumer still reaches the predicate through `in_standing_lull`; the only places
`last_mean` is read raw are two MCP read-outs that display it. `decide()` keeps
a `lull_threshold` parameter with the old `0.18` default, but the caller passes
`lull_band(detector, memory_settings)`, so the constant that could never fire is
no longer reachable from the live path.

**Its own thresholds are all inside their distributions**, which is what this
entry asked for and is the boring answer:

| threshold | value | observed |
| --- | --- | --- |
| `appetite_min_axes` | 0.15 | closeness/comfort far above it |
| `appetite_min_want_pressure` | 0.35 | 5 of 8 live wants clear it, median 0.483 |
| `appetite_short_share_threshold` | 0.6 | met on 46% of turns lifetime |

**And that 46% is the finding.** `appetite_short_reply_chars` is an absolute
160-character bar, and her reply length has moved underneath it by 80%:

| | median reply | share under 160 | gate open (4+ of last 6 short) |
| --- | --- | --- | --- |
| W26–W28 (late Jun – mid Jul) | 101–109 | 83–87% | **90.9%** |
| W32–W35 (Aug, current) | 182–190 | 23–31% | **10.5%** |

Same constant, no config change, and the gate went from admitting nine turns in
ten to admitting one. Lifetime numbers hide this completely: the 46% above is
just the average of a 91 and a 10. This is *this entry's own shape* one seat
over — H17 self-calibrated the lull bar precisely because "the constant encodes
one embedding model's distance scale and sits below every reading ever taken",
and the sibling constant in the same `decide()` call is still raw and has
already drifted further than the lull one ever did.

**No fix shipped, because the obvious one does not work and the measurement
says so.** Replacing 160 with a bar relative to her own trailing replies —
`0.8 x median(last 60)` — narrows the weekly swing from 92 points to 33, and a
percentile bar does no better (32.7 points at p30, 34.9 at p35, 38.8 at p40).
The residual is not the bar: it is autocorrelation. The gate asks for *four of
six consecutive* replies to be short, and runs of short replies are era-specific
even at a fixed marginal rate, so no per-reply threshold can flatten it. A
"fix" that cuts the swing 3x and is presented as stabilising it would be worse
than the current state, because the next reader would stop checking.

What is actually open is a product question this file cannot answer: should
"tapped out" mean *short in absolute terms* (in which case 91% in July was
correct and the constant is fine) or *short for her* (in which case the gate
needs a distribution and a tolerance for run-length, not a threshold). Filed as
a calibration question, not a defect.

**The generalisation this entry recommended does not reproduce, and the reason
is worth more than the check.** It suggested grepping the other signals with
many readers — `AffectState`, the relationship axes, the arc label. Measured:
affect thresholds live in `affect_state.py`, their owning module, and the three
comparisons outside it (voice cadence, activity selection) ask different
questions; there is exactly one `min(closeness, comfort) < floor` axes gate in
the codebase; and the arc label has one duplication, `_BLOCKED_ARCS =
frozenset({"support", "reflection"})` in both `initiative_director.py` and
`topic_appetite.py`, which currently **agree**. Two copies that agree are the
precondition for this shape, not an instance of it.

So reader count was not the predictor. What made `last_mean` uniquely dangerous
is that its polarity is *counterintuitive* — it is a distance, so a lull is a
low reading — and its scale is install-specific, so both the sign and the bar
were guessable and neither was checkable against intuition. Valence and arousal
are normalised and signed the way their names imply, and nobody gets them
backwards. **Revised rule: copied-predicate risk scales with how surprising the
predicate is, not with how many callers it has.**

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

## H44. Nothing has ever graduated, and at this calibration nothing can

**Severity: medium — the L30 loop's last exit has never once been taken, and the
arithmetic says it is not waiting on more data.**

Found while auditing "still open" notes (see [H7's third
pass](#third-pass-it-adjudicates-now-and-graduation-is-the-leg-that-never-ran)).
The forward half of L30 invents, the middle now asks and occasionally adjudicates
— and the exit that turns a settled guess into a durable concept or belief has
**zero instances, lifetime**. `graduated_concept_id` and `graduated_memory_id` are
`NULL` on all 23 rows.

**Re-counted 25 Aug (27 rows, 2,867 assistant turns), and the arithmetic below
holds — the shelf is now mostly expiring.** Status split: 14 `expired`, 11 `open`,
1 `refuted`, 1 `supported`, still **0 graduated**. Lifetime 7 rows ever asked
(25.9%), 2 ever answered (7.4%), so 28.6% of asks produce a verdict — the
one-in-five in the table below has become closer to one-in-three, and it changes
nothing, because **expiry now outruns resolution 7:1**. The single `supported` row
has `support_count = 1` against a bar of 2 and has never been asked. Four months
of operation have produced 27 guesses and 2 verdicts, which is the same
conclusion the arithmetic reached prospectively: the bar is not waiting on data.

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

## H54. `topic_miss` at 95% of eligible declines is not, by itself, a starvation

**Severity: medium — an open measurement, not a defect with a patch. The
headline that sat at the top of this file as "the next problem" is the same
number asked a different question.**

The 19 Aug reading left `topic_miss` at **1,703 of 1,759 eligible declines
(96.8%)**, spread across five cues (`concept_hypothesis`, `curiosity_gradient`,
`interest_drift`, `associative_wander`, `knowledge_gap_notice`) that each look
starved. Re-run on 29 Aug, the live 7-day window is **1,273 of 1,331 (95.6%)**,
and the post-H47 era as a whole is 1,597 of 1,657 (96.4%). The rate did not
move. The question is whether it was ever a problem.

It was asked because maybe she simply chose a different reply, or maybe the
topic gate labelled the turn wrong. Those are two different mechanisms, they
live in two different columns, and mixing them is how this number keeps getting
read as "she never gets to bring anything up".

### "She decided to respond differently" is real, and it is not `topic_miss`

`topic_miss` is recorded when the cue **never reached the prompt**. The
provider walked the shelf, the predicate refused every row, and she was not
shown it. She cannot have "decided" anything about a line she did not see.

The thing that *is* her decision sits one stage later, and [H47](shipped/health.md#h47-she-was-shown-604-of-them-and-said-nothing-which-is-not-a-supply-problem)
already measured it: of expired cues, **92.6% had been rendered in front of her
and passed over**. Re-counted on the five topic-gated types, 29 Aug:

| cue | expired never shown | expired after a showing | used |
| --- | --- | --- | --- |
| `knowledge_gap_notice` | 2 | 92 | 4 |
| `curiosity_gradient` | 2 | 53 | 3 |
| `interest_drift` | 3 | 46 | 19 |
| `associative_wander` | 1 | 36 | 6 |
| `concept_hypothesis` | 22 | 17 | 1 |

Four of five take every live chance they are *handed* and then she mostly
declines to speak them. That is a judgement about fit, mood, and whether the
line would be an announcement — and it is exactly the instinct behind "maybe
she responded differently". It does not explain the `topic_miss` bucket, because
those turns never got that far.

`concept_hypothesis` is the exception on that table (22 never shown) and belongs
with [H7](#h7-the-hypothesis-loop-invents-and-never-adjudicates) / [H44](#h44-nothing-has-ever-graduated-and-at-this-calibration-nothing-can),
not with this gate.

### The rate going *up* is what H47 predicted, not a regression

[H43](shipped/health.md#h43-the-gate-blamed-for-945-of-her-silences-accepts-a-third-of-everything)
found the original 94.5% figure was mostly cadence holds wearing this label, and
that the lexical gate as shipped **accepted 33.2% of every (subject, message)
pair** — nearly a no-op. [H47](shipped/health.md#h47-she-was-shown-604-of-them-and-said-nothing-which-is-not-a-supply-problem)
then turned the stoplist on at admission (21 Aug), dropping acceptance from
32.3% of pairs to **3.6%**, and wrote in as many words that `topic_miss` should
*rise* as a share of decisions, because refusing had become honest. Split by
era:

| window | eligible declines | `topic_miss` share |
| --- | --- | --- |
| before H43 (19 Aug) | 4,706 | **35.8%** (the rest was mostly the old `provider` catch-all) |
| H43 → H47 | 446 | 90.4% |
| since the stoplist | 1,657 | **96.4%** |

A thin shelf of leftover specific subjects (2–20 pending rows per type right
now, of which `pick_pool_cue` sees at most a handful) against open-ended chat
will miss most turns **by construction**. "I need to go have dinner" against
`path of exile gameplay` / `the bitterness in his morning coffee` / `teaching
myself guitar` is a correct miss. Treating 96% as starvation inverts H47's
whole point: we asked the gate to stop matching on `and`, and then counted the
refusals as failure.

### "The analyzer marked it wrong" is the remaining question

False *positives* were H43/H47's subject — cues admitted on a shared `and`.
False *negatives* are this one: a turn that really was about the pending
subject, refused anyway.

A proxy (current pending shelf scored against the user text of the last twelve
post-stoplist `topic_miss` turns, lexical arm only — the live gate also has a
0.55 cosine, and the shelf is last-value-only so this is not a reconstruction)
admitted **1 of 45 pairs**. The one hit was `"rest of the house"` against
`"jacob's village connected his house to a new sewage"` — a leftover lexical
coincidence, not a rescue. Adjacent misses that a person might call related
(`cleaning my room` against `kitchen cleanliness habits` / `household chores
promises`; `kingdom come` against `path of exile gameplay`) are the shape a
false-negative finding would have, and they are also the shape a 0.55 cosine
on a thin leftover shelf is *supposed* to refuse: "games" is not the same
subject, and "chores" is not the same as a specific sewage anecdote.

**Do not loosen the gate to chase this.** H43 inverted that fix once already:
tightening a 33% no-op in service of a problem it was not causing would have
made her quieter. The work, if any, is a **hand-check of cosine-near-misses**
on recorded (subject, message) pairs that the gate refused — not another
threshold. `scripts/topic_gate_report.py` is the instrument; it still does not
know per-turn shelf depth, which is why a "would have admitted" reconstruction
cannot close the question from `cue_decisions` alone.

**Rule, because this headline has now been the next problem three times.** A
decline reason that is 95% of a denominator after you made the predicate
stricter is usually the predicate doing its job. Before treating it as
starvation, split "never shown" from "shown and passed over", and split
"correct miss" from "same subject, different words". The first split is H47.
The second is this entry, and it is still open.

---

<a id="recurring-shapes"></a>

## The twenty-nine recurring shapes

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
[H22](shipped/health.md#h22-k73-named-one-ritual-then-locked-itself-shut-forever)
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
