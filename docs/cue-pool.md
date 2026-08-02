# The cue pool

A **cue** is not a memory. It is a conversational move Aiko is holding
but has not made yet: a topic that has gone quiet she might reopen, a
gap she noticed in what she knows, an association she wants to follow,
a question about the user's life she has been sitting on.

Seven workers produce these, and three more cues are chosen when their
block renders. Until schema v29 each of them kept its own bookkeeping in
one of three mutually incompatible shapes — a `kv_meta` JSON ring plus a
`surfaced_keys` set, a ring plus a watermark plus an in-memory slot, or
rows in the `memories` table — and none of those could answer the two
questions that decide whether any of it works.

**How much stock do I have?** A ring says what is newest, not what is
unspent. A worker could not stay dormant while cues were already
waiting, so every one of them leaned on a hand-picked daily cap to stop
itself instead: two a day here, three there, none with any evidence
behind them.

**Was it used?** The watermark advanced the moment a provider rendered
the block. A cue Aiko ignored was retired exactly like one she acted
on, which made the whole ring a write-only log of good intentions.

The pool is one table that answers both.

## The table

`cue_pool` (schema v29, created in
[`chat_database.py`](../app/core/infra/chat_database.py); wrapped by
[`CueStore`](../app/core/proactive/cue_store.py)).

The columns that carry the design:

| Column | Why it exists |
| --- | --- |
| `subject` | The cue's identity, normalised. Supersession keys on it and consumption matches against it. |
| `text` | The rendered cue line, written at production time so providers never re-render. |
| `state` | See below. |
| `surfaced_count` | Turns the cue sat in the prompt and Aiko did not raise it. |
| `ask_count` | Times she raised it and no answer came. |
| `not_before` | Re-ask cooldown; a released cue is invisible until it passes. |
| `used_evidence` | How the match was made (`lexical:0.67`), or why the cue died (`max_surfacings/...`). |
| `embedding` | The subject's vector, computed at write time so consumption never pays for an embed mid-turn. |

### States

```
pending ──▶ surfaced ──▶ used
                │  └────▶ awaiting ──▶ used
                └──────▶ pending (retry)
   any ────────────────▶ expired | superseded
```

`awaiting` exists because for some cue types Aiko saying the thing is
not the end of it: if she asks about X and the user answers about Y,
she asked and got nothing, the curiosity is not satisfied, and the cue
has to survive. See `fulfilment` below.

Terminal rows are **kept, not deleted**. "She was offered this four
times and never took it" is the satisfaction evidence, and it only
exists if the losers stay on the table.

### Two counters, not one

`surfaced_count` and `ask_count` bound different failure modes — the
model ignoring the cue versus the user not biting — and collapsing them
into one counter would make both invisible. Either exhausting sends the
cue to `expired`, which is what makes the retry loop safe even when the
matcher is wrong every single time.

## Policy

Everything type-specific lives in `CuePolicy` /`CUE_POLICIES` in
[`cue_accounting.py`](../app/core/proactive/cue_accounting.py). The
defaults are conservative at every field; a type that wants more says
so.

| Field | Meaning |
| --- | --- |
| `inventory_target` | How many pending cues counts as stocked. This is what replaced the daily caps. `0` means nothing stocks the type — see the surface-time ledger below. |
| `ttl_hours` | How long the cue stays worth saying. A dormant topic keeps for weeks; "did the espresso machine arrive" does not. |
| `max_surfacings` / `max_asks` | The two retry budgets. |
| `reask_cooldown_hours` | How long an unanswered question waits before she may ask again. |
| `surface_cooldown_hours` | How long after *any* cue of this type reached the prompt before a **new** one may. `0` (the default) means the shelf alone decides. |
| `fulfilment` | `spoken` (she said it), `answered` (she said it *and* the user engaged), `either_party` (the conversation landed on the subject, no matter who steered it there). |
| `match_mode` | `lexical` or `lexical_or_cosine`. |
| `match_scope` | Match her reply, or the whole turn. |
| `pick_order` | Which waiting cue to reach for first among those with the same number of chances. `newest` (the default) because a cue is built from context and the freshest framing still fits; `oldest` only for `tease_ledger`, where the wait *is* the content. |
| `handling_section` / `block` | The persona header hoisted into T6 alongside this cue, and the prompt block whose presence triggers it — see below. |

The `match_mode` split is the non-obvious one. **A cue whose subject is
on-topic by construction cannot use cosine.** `knowledge_gap_notice`
says "I keep circling X and never dug into it", where X *is* what you
are talking about, so a high cosine between her reply and X measures
"she stayed on topic" rather than "she used the cue". The off-topic
types (`associative_wander` matched against the distant half of its
pair, `dormant_interest`, `curiosity_gradient`) have the opposite
property: a high cosine there means she actually pivoted, which is
exactly the event.

The cosine is computed even for lexical-only types and recorded in
`used_evidence` on every verdict. That is the calibration data — once
a few hundred accumulate, comparing the distribution on turns where
lexical fired against turns where it did not says where the real floor
belongs, and the conservative types can be promoted on evidence rather
than on a guess.

### Scarcity is a surfacing property, not a production one

`surface_cooldown_hours` exists because deficit-driven production broke
something that used to work by accident. A few cue types are rare *by
nature* rather than because material is short — `self_callback` is one
per ten days or so, and always was. But that pacing lived in the
producer: a worker gated on a ten-day `last_fired_at` drafted one cue a
fortnight, and one drafted is one surfaced. Move the same worker onto an
inventory target and the shelf fills, and a full shelf empties itself
over consecutive turns.

The fix is to state the rarity where it belongs. A producer cooldown was
throttling the wrong thing anyway: it meant that when the shelf ran dry
the cue simply did not exist for ten days, so a perfect moment inside
the window found nothing to say. The cadence gate keeps the stock and
paces the *saying*.

It gates **first claims only**. A row that has already surfaced is
unfinished business — Aiko was shown it and did not take it — and the
retry is the whole point of holding it. `pick_pool_cue` implements this
as a filter over candidates rather than a check on the winner, because
`pending()` sorts unseen cues first: rejecting the row it returns would
hide a legitimate retry sitting directly behind a fresh cue that is
merely early. The clock is `CueStore.last_surfaced_at()`, the newest
`last_surfaced_at` for the type across *all* states — a cue she used is
the strongest possible reason not to open another.

## Production: demand instead of caps

Workers use [`CueProducer`](../app/core/proactive/cue_producer.py),
which counts pending rows and reports pressure from the **deficit**
against `inventory_target` (`pressure_from_deficit` in
[`idle_worker.py`](../app/core/proactive/idle_worker.py)). A full shelf
means the worker is simply not admitted by the scheduler — no cap, no
counter, no config key. See [`idle-workers.md`](idle-workers.md) for
how pressure feeds admission.

`CueProducer.spoken_for()` gives a worker the subjects already in the
pool so it does not redraft one it is already holding.

This retired five config keys (`*_daily_cap` for associative wander,
interest drift, dormant interest, curiosity gradient, and forward
curiosity).

## Consumption: did she actually use it?

[`CuePoolMixin`](../app/core/session/cue_pool_mixin.py), called from
post-turn. The verdict is two cheap local tests
([`echo_detector`](../app/core/memory/echo_detector.py)) — never an
LLM, because post-turn is not a place to spend a generation.

**Stage A** judges Aiko's reply. Matching is against the cue's
*subject*, not its text: a cue reads "we haven't talked about film
photography in ages" and almost every word of that is framing that will
never appear in her reply. What identifies it is `film photography`.
Matching the sentence would dilute the overlap toward a miss on exactly
the turns where she used the cue perfectly.

- No match → `release` for another try, or `expire` at `max_surfacings`.
- Match, `fulfilment=spoken` → `used`.
- Match, `fulfilment=answered` → `awaiting`, and stage B decides.

**Stage B** (`_settle_awaiting_cues`) runs on the *next* turn against
the user's message — the same two-clock shape
`SurfacingOutcomeStore` uses, because the thing being measured only
exists in the next message. No answer → released under
`reask_cooldown_hours`, or expired at `max_asks`.

`either_party` cues (just `curiosity_seed`) are the one case that looks
beyond what surfaced: a seed is spent the moment the conversation lands
on its subject, whoever steered it there and whether or not Aiko was
ever shown the cue. Holding a seed open for a topic the two of them
just spent a turn on would be the opposite of curiosity.

Embeddings are shared, not recomputed: post-turn already embeds the
reply for K22 and the combined turn for the gap resolver, and
`_combined_turn_vec` caches the latter. The user's message is embedded
lazily and only when an `awaiting` row of a cosine-trusting type is
actually present.

## Cues chosen at render time (the surface-time ledger)

Everything above assumes a worker drafted the cue in advance and the
prompt merely claims one. The gap-return family cannot work that way.
Their arming event carries no content — `_pending_turning_over_seconds`
is a float — and *which* reflection or journal beat gets used is picked
when the block renders, against the message being answered. Drafting
ahead would throw that query-awareness away. `long_arc_callback` is the
same shape for a stronger reason: its candidate *is* a RAG hit against
the message being answered.

So the row is written at the moment it surfaces, by
`CuePoolMixin.record_surfaced_cue()`, already in `surfaced` state and
registered for the same post-turn settle. Render order becomes: take a
released row if one is pending, else run the existing picker and record
what it chose. Everything after that point is shared — a miss releases
the row, and the next render finds it via `take_pool_cue()` without
needing a second gap to arm the slot.

These types set `inventory_target=0`, which is a claim rather than a
tuning choice: **nothing stocks them, the pool is only a retry buffer.**
Two things read it. The scheduler must not see a permanently empty shelf
as a deficit to fill, and `armed_cues()` treats a released row as an
opportunity in its own right rather than requiring the slot too — the
usual slot-and-content conjunction would report them unarmed on exactly
the turn they are about to fire.

The slot itself is no longer spent on read. `_spend_gap_slot()` clears it
when the block fires and otherwise holds it for a couple of assemblies
(`_GAP_SLOT_ATTEMPTS`): a picker that came up empty means nothing
reached the prompt, so nothing was used up, and the picker weighs
reflections against a conversation that moves. Bounded because a
welcome-back goes stale and each held turn costs another picker run.

## Which cues belong in the pool: the lexical-trace test

Not every conditional block should be a cue. `absence_curiosity` sits in
the middle of the gap-return family and stays off the pool; `mood_drift`
and `mood_inertia` are hoisted out of T0 like pool cues are, without
being pool cues. The line between them is one question:

> **If Aiko acts on this cue, will her reply contain the cue's subject?**

Consumption is measured by looking for the subject in what she said. So
a cue that names something — a topic, a claim, a thing she was mulling —
leaves a lexical trace when she uses it, and the pool can tell success
from silence. That is the whole mechanism, and it is what makes the
retry loop safe.

A cue that changes *how* she speaks rather than *what* she says leaves
no such trace. `absence_curiosity` is a register instruction built from
a gap duration; a warm welcome-back can be letter-perfect without
reusing a word of it. `mood_inertia` says "let the words catch up",
which shows in pacing. `mood_drift` explicitly says *don't* name the
observation. Pool any of these and every single firing scores as a miss,
which is worse than no measurement: the retry loop would keep re-showing
a cue she followed perfectly, and the per-type scoreboard would report
the feature as broken.

The rule of thumb: **if you cannot say what string you would grep her
reply for, it is not a pool cue.** Such a block can still be hoisted out
of T0 — that is what `HANDLING_SECTIONS` is for — it just carries no
ledger.

## The persona hoist

Handling text is paired to a **prompt block**, from two registries: a
pooled cue names its header in `CuePolicy.handling_section` alongside
`CuePolicy.block`, and anything else lives in `HANDLING_SECTIONS` in
[`prompt_support.py`](../app/core/session/prompt_support.py). The
sections themselves live in
[`conditional_handling.txt`](../data/persona/conditional_handling.txt) —
a companion to the persona rather than part of it, because they are the
one piece of Aiko's character text that is *not* always-on. At load time
`PromptAssembler._persona_split()` reads both files and caches the
sections; `_render_handling_notes(locals())` appends the notes for
whichever blocks rendered this turn, as a `handling_notes_block` in T6.

What stays in T0 is one short stanza (`HANDLING_PREAMBLE`) saying that
cues arrive with their own instructions attached.

This takes ~41 k characters off the always-on prompt across 47 blocks and
puts the instructions next to the thing they are about. Most of those
blocks are not pool cues at all — the mechanism started with the cue
families and then generalised, so `HANDLING_SECTIONS` now carries the
turn-taking permissions, the topic-pitch reads, the repair detectors and
the register nudges alongside them. The registry is also many-to-many in
both directions: one renderer may claim several headers, and one passage
may cover a family of blocks. See
[`prompt-caching.md`](prompt-caching.md#hoisting-conditional-instructions-out-of-t0-the-handling-notes-split)
for the general rule, the two categories that deliberately stayed behind,
and the resolution mechanism.

A section left inline in
[`aiko_companion.txt`](../data/persona/aiko_companion.txt) is still
honoured, and **wins** over the companion file, so an install that had
customised one before the split keeps its wording. The shipped persona
carries none of them.

That is also the shape of a container upgrade: the entrypoint seeds
`data/persona/` copy-if-absent per file, so an existing volume keeps its
old persona (sections inline) *and* gains
`conditional_handling.txt`, which is then ignored. The loader logs an
`INFO` line naming the shadowed headers when that happens — delete the
sections from the persona to switch over.

The headers are matched literally and `split_persona_section` is a
deliberate no-op on a header it cannot find (both files are
user-editable, so a rename must not crash a turn). That makes typos
silent at runtime — the section simply stops being hoisted and its cue
goes out with no handling note — which is why two tests check every
header: one next to the policy definitions
(`tests/test_cue_accounting.py::CuePolicyTests::test_handling_sections_exist_in_the_notes_file`)
and one next to the loader, in `tests/test_persona_hoist.py`, which also
asserts the persona is not still carrying them and that every registered
block reaches the call site.

## Watching it work

**The Cues panel** (Memory tab → Cues,
[`CuesPanel.tsx`](../web/src/features/settings/memory/CuesPanel.tsx))
lists the pool with type and status filters, backed by
`GET /api/cue-pool`. Per-type counts sit above the list. A cue flipping
to `used` arrives live over the `cue_pool_updated` WS event, so you can
watch one turn green in the same beat Aiko spends it.

**`get_cue_outcomes`** (MCP) reports the pool alongside the older reach
accounting. The two measure different things and the gap between them
is the interesting part: reach stops at "the block rendered", `used`
means the subject actually turned up in what was said. A type with
reach and no uses is one Aiko is being handed and quietly dropping —
which reach alone reads as a success.

Three numbers to read there:

- **`deficit`** — why a worker is dormant. Zero means its shelf is
  full, which is correct rather than broken.
- **`used` vs `expired`** — the real verdict on a cue type.
- **`mean_surfacings_before_use`** — the framing check. 1.0 means she
  takes a cue the first time she sees it; a number near the type's
  `max_surfacings` means the cue line is not reading as something to
  act on.

## The self-state one-shots

Two of the four self-state cues moved in the second batch, and how they
moved is worth recording because they are opposite cases.

**`self_correction`** is queued from the turn path, not by a worker: a
contradiction only exists once Aiko has said the contradicting thing.
`_maybe_arm_self_correction` composes the line (via
`self_correction_detector.render_cue`) and writes it with
`CuePoolMixin._queue_pool_cue()`, the session-side twin of
`CueProducer.publish`. The subject is the memory she should correct
*to*, never her own snippet — she is likely to quote the slip while
owning it, and matching the snippet would read repeating the mistake as
having fixed it. `ttl_hours=0.5`, because the line opens "a moment ago
you said" and that is a fiction by the turn after next.

The migration also closed a leak. The old provider cleared its one-shot
slot and *then* validated the snippet and memory text, so a hit missing
either half burned the arm having said nothing. Validation now happens
at write time, and a cue that renders empty is simply never written — so
the cooldown is not spent either.

**`self_callback`** is the full-stack case: worker → `CueProducer` →
pool → provider, plus `demand()` from shelf depth. It is also the type
that motivated `surface_cooldown_hours`, since its rarity used to come
from a producer cooldown (see above). Its `aiko.self_callback` kv ring
is still written — `get_self_callback_state` reads it, and it is the
signature source for the per-memory dedupe — but nothing surfaces from
it any more, and its `last_surfaced_at` watermark is gone.

`mood_inertia` and `mood_drift` stayed off the pool: they fail the
lexical-trace test above. They were hoisted out of T0 through
`HANDLING_SECTIONS` in the same pass, and `mood_drift`'s sampler got a
heartbeat `demand()` — full pressure until today's sample lands, nothing
after — so the lazy-sample fallback in its provider stops being
load-bearing.

## The relationship one-shots

The third batch is the one where the two mechanisms above — the shelf
and the ledger — landed side by side, and where one type declined both.

**`wellbeing_concern`** and **`shared_ritual`** are ordinary shelf
types: a worker drafts, `demand()` reports the deficit, the provider
claims. Both had a producer cooldown (7 days, 3 days) that became
`surface_cooldown_hours`. Neither keeps a wall-clock gate of its own,
but both keep a *domain* gate, which is a different thing. The concern
worker keeps `wellbeing_concern.last_signature` so an identical ongoing
pattern does not re-draft while an escalation — more nights, a new
neglect category — is a new signature and gets through. The ritual
worker keeps `aiko.shared_rituals`, the durable model of which patterns
exist and how many weeks each has run, and flips `acknowledged` when
the cue is *published* rather than when it is said: from the store's
side the only question is whether the ritual still needs an offer, and
the pool answers everything after that.

**`long_arc_callback`** is a ledger type. Its candidate is a RAG hit
against the message being answered, so drafting ahead is impossible for
the same reason as the gap-return family. It is also the one type whose
`match_mode` had to be `lexical` on principle rather than by
disposition: the candidate was *selected* by cosine against the user's
message, so a cosine match afterwards is close to guaranteed and
measures nothing. `min_overlap=3` compensates for judging a
several-week-old memory by words alone.

Its retry needed one thing the gap family does not. A released callback
is about a specific old memory, and the conversation has moved on by the
next turn — re-offering "you mentioned woodworking a month ago" while
the user is talking about their tax return is worse than dropping it. So
`take_pool_cue` gets a `relevant` predicate backed by
`long_arc_callback.still_relevant()`, a lexical check of the cue's
subject against the current message. The pool holds the row; relevance
decides whether this is the turn for it. The per-session cap applies to
first claims only — a retry is the same callback, not a second one.

**`tease_ledger`** is the migration that replaced a store rather than a
schedule. K59's mock-grudges lived in one `kv_meta` JSON key with
hand-written versions of five things the pool already did — a cap, an
expiry sweep, an `offered_at` stamp, a settle pass and a re-offer on the
miss. All five went; a debt is now a `pending` row, an offer is
`mark_surfaced`, and a collection is ordinary stage-A matching.

It is worth reading for the two places the shared machinery was
*wrong* rather than merely absent. Its cues want to be **stale** —
collecting an hour after banking is a comeback, and the gap is what
makes it a callback — so it is the one type that sets
`pick_order=oldest`, and its rows go in sealed for an hour through
`hold_hours` on `add()`. And its subject is the user's quote rather
than the cue's own description, because `what` is a *constant* on the
K29 lane ("they pushed back hard on a take of yours", every time):
keyed on that, each new pushback would have superseded the one before
it, and matched on that, any reply containing "pushed", "back" and
"hard" would have settled a debt about something else.

Three gates stayed outside the policy because no field expresses them.
The humor-axis floor is a relationship read. The offer cooldown is
divided by the J11 affection-style bias, so it moves with how well
teasing lands for this user — `surface_cooldown_hours=0` and the
renderer spends it against `last_surfaced_at` instead. And the pool
supersedes on an *exact* normalised subject, which is right for a topic
slug and too strict for a quote, so banking keeps a near-duplicate
check: three shared content words against `recent_subjects()`, using
the same tokeniser consumption uses.

**`promise_followthrough`** stayed off the pool, and it is the clearest
example of the boundary. A promise is a memory before it is a cue:
`memories` holds the commitment and its lifecycle, the post-turn hook
already decides whether Aiko made good on it, and the row outlives any
single nudge. Pooling the cue would mean two stores answering "has she
dealt with this yet", which is the drift the pool exists to end. It took
`demand()` anyway — the pending slot and the arm cooldown are two kv
reads, and that is a better admission signal than a fixed interval.

## What is not in the pool yet

Sixteen types are pooled: ten worker-produced, four on the surface-time
ledger (`turning_over`, `sleep_return`, `away_activities`,
`long_arc_callback`), and two banked on the turn path
(`self_correction`, `tease_ledger`). The remaining cue types in
`CUE_SPECS` (`self_noticing`, `absence_curiosity`, and friends) still
ride their `kv_meta` rings and are still exempt from consumption
matching.

That exemption is correct for most of them, by the lexical-trace test:
"echo is meaningless for a cue, because a cue is an instruction" holds
for a tone or posture instruction and fails only for a cue that names a
specific subject.

## See also

- [`idle-workers.md`](idle-workers.md) — demand-driven scheduling, which
  is what `CueProducer` reports into.
- [`prompt-caching.md`](prompt-caching.md) — the tier ladder and the
  hoisting rule.
- [`app/core/proactive/cue_store.py`](../app/core/proactive/cue_store.py) —
  the state machine, in the module docstring.
- [`app/core/proactive/cue_accounting.py`](../app/core/proactive/cue_accounting.py) —
  policies, and the older armed-to-surfaced accounting.
- `tests/test_cue_pool_consumption.py` — the consumption contract.
