# The cue pool

A **cue** is not a memory. It is a conversational move Aiko is holding
but has not made yet: a topic that has gone quiet she might reopen, a
gap she noticed in what she knows, an association she wants to follow,
a question about the user's life she has been sitting on.

Seven workers produce these. Until schema v29 each of them kept its own
bookkeeping in one of three mutually incompatible shapes — a `kv_meta`
JSON ring plus a `surfaced_keys` set, a ring plus a watermark plus an
in-memory slot, or rows in the `memories` table — and none of those
could answer the two questions that decide whether any of it works.

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
| `inventory_target` | How many pending cues counts as stocked. This is what replaced the daily caps. |
| `ttl_hours` | How long the cue stays worth saying. A dormant topic keeps for weeks; "did the espresso machine arrive" does not. |
| `max_surfacings` / `max_asks` | The two retry budgets. |
| `reask_cooldown_hours` | How long an unanswered question waits before she may ask again. |
| `fulfilment` | `spoken` (she said it), `answered` (she said it *and* the user engaged), `either_party` (the conversation landed on the subject, no matter who steered it there). |
| `match_mode` | `lexical` or `lexical_or_cosine`. |
| `match_scope` | Match her reply, or the whole turn. |
| `handling_section` | The persona header hoisted into T6 alongside this cue — see below. |

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

## The persona hoist

Each pooled type names a header in `handling_section`, and the section
under that header lives in
[`cue_handling.txt`](../data/persona/cue_handling.txt) — a companion to
the persona rather than part of it, because it is the one piece of
Aiko's character text that is *not* always-on. At load time
`PromptAssembler._persona_split()` reads both files and caches the
sections; `_render_cue_handling()` appends the notes for whichever cue
types rendered a block this turn, as a `cue_handling_block` in T6.

What stays in T0 is one short stanza (`CUE_HANDLING_PREAMBLE`) saying
that cues arrive with their own instructions attached.

This takes ~5.9 k characters off the always-on prompt and puts the
instructions next to the thing they are about. See
[`prompt-caching.md`](prompt-caching.md#hoisting-conditional-instructions-out-of-t0-the-cue-handling-split)
for the general rule.

A section left inline in
[`aiko_companion.txt`](../data/persona/aiko_companion.txt) is still
honoured, and **wins** over the companion file, so an install that had
customised one before the split keeps its wording. The shipped persona
carries none of them.

That is also the shape of a container upgrade: the entrypoint seeds
`data/persona/` copy-if-absent per file, so an existing volume keeps its
old persona (sections inline) *and* gains `cue_handling.txt`, which is
then ignored. The loader logs an `INFO` line naming the shadowed cue
types when that happens — delete the sections from the persona to switch
over.

The headers are matched literally and `split_persona_section` is a
deliberate no-op on a header it cannot find (both files are
user-editable, so a rename must not crash a turn). That makes typos
silent at runtime — the section simply stops being hoisted and its cue
goes out with no handling note — which is why two tests check every
header: one next to the policy definitions
(`tests/test_cue_accounting.py::CuePolicyTests::test_handling_sections_exist_in_the_cue_file`)
and one next to the loader, in `tests/test_persona_cue_hoist.py`, which
also asserts the persona is not still carrying them.

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

## What is not in the pool yet

Seven types are pooled. The other ~7 journal-backed cue types in
`CUE_SPECS` (`turning_over`, `sleep_return`, `away_activities`,
`self_noticing`, and friends) still ride their `kv_meta` rings and are
still exempt from consumption matching.

That exemption is correct for most of them. The old reasoning — "echo
is meaningless for a cue, because a cue is an instruction" — holds for
a tone or posture instruction; it fails only for a cue that names a
specific subject, which is exactly the set that moved. Migrating the
rest is worth doing only for the ones that name a subject.

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
