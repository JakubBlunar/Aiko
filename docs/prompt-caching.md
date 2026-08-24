# Prompt-cache prefix-stability contract

OpenAI (and any other provider with compatible prompt caching) hashes
the **token prefix** of every chat request and reuses the
intermediate activations from a previous matching request as long as
that previous request is still in the cache (currently ~5-10 min).
Cached input tokens are billed at about a tenth of the uncached
rate, which dominates Aiko's per-turn cost once the persona /
profile / summary stack is rich enough.

This doc is the contract every contributor needs to know before
adding a new block to the prompt assembler. It exists in addition to
the existing ["OpenAI prompt caching"](llm-providers.md#openai-prompt-caching)
walkthrough in `docs/llm-providers.md`; that one explains the
*pricing impact*, this one explains the *internal layout* that makes
it work.

## How OpenAI prefix caching works

- The cache key is the **literal byte stream** of `messages` plus
  `system`. It includes JSON whitespace, ordering, and every
  character of every system block.
- Match is **longest common prefix**. The cache stops matching at
  the first differing token. Everything after that is full-price.
- Cache eviction is approximate-LRU with a few-minute TTL. A long
  user silence forces a full-price re-warm; Aiko's typed-mode
  proactive nudges sometimes keep the cache warm "by accident".
- Observability is `usage.prompt_tokens_details.cached_tokens` on
  every response, lifted onto `ChatUsage.cached_tokens` and exposed
  on the `turn done:` INFO log line as `cached=N cached_pct=%.1f`.
- Non-OpenAI providers may or may not implement the same field —
  Ollama, Gemini, Groq, and most OpenRouter routes leave it at 0 in
  the returned payload, which is the correct null answer for our
  client.

## The prefix-stability ladder

`app/core/session/prompt_assembler.py` arranges `system_parts` from
most-stable (T0) to most-volatile (T6). The order is **strictly
prefix-cache-friendly**: a single byte change at tier T_k invalidates
every token after it, so by parking volatile blocks at the end we
preserve as much prefix as physically possible across consecutive
turns.

| Tier | Lifetime | Representative blocks | Cache behaviour |
|---|---|---|---|
| **T0 — stable** | Across sessions; flips only on persona / config edit | `persona`, speech / overlay / outfit / motion / touch grammar addenda, `narrative`, `profile`, `petname`, `catchphrase` | The cache prefix. ~Every turn after the first reads these for free. |
| **T1 — semi-stable** | A few times a day | `relationship`, `anniversary`, `axes`, `arc`, `agenda`, `goals`, `day_color` | Caches across most of an arc; flips a handful of times per session. |
| **T2 — summary (compaction-only)** | Only mutates when `SummaryWorker` compacts old history | `summary_text` | Stable between compaction events (10s of turns at a time). |
| **T3 — RAG memory** | Per-turn retrieval, topic-stable | `memory_block` | Same retrieval often repeats turn-to-turn on a single thread. |
| **T4 — ambient awareness** | Hourly to per-turn | `grounding_line`, `ambient`, `circadian`, `pajama`, `ambient_noise`, `world`, `activity`, `sensory_anchor` | Some blocks barely change inside a thread (`world`, `circadian`); others do (`grounding_line`). The tier sits above affect so the per-turn ambient noise doesn't blow up the more stable cluster above. |
| **T5 — affect / style (per-turn)** | Updates after every reply | `affect`, `mood_hint`, `mood_shell`, `style_signal`, `user_state`, `vocal_tone` | Volatile by design — affect state changes on every turn. |
| **T6 — detectors (live `user_text`-dependent)** | Per-turn, fired by the message you're answering right now | `belief_gaps`, `clarification`, `calibration`, `rupture`, `misattunement`, `opinion_injection`, `absence_curiosity`, `turning_over`, `novelty`, `stagnation`, `style_pattern`, `self_noticing`, `vulnerability_budget`, `touch_state`, `user_reactions`, `curiosity_seeds`, `knowledge_gaps`, `handling_notes_block` | The freshest tier the LLM reads before the user message. Almost always changes turn-to-turn. |

The full assignment lives in `_PROMPT_BLOCK_TIERS` near the top of
`app/core/session/prompt_assembler.py`. That constant is documentation
+ audit only; the actual ordering is enforced by the explicit
`if block: system_parts.append(block)` cascade in
`PromptAssembler.assemble_with_budget`. The cross-tier invariants are
locked in by `tests/test_prompt_assembler.py::PromptCachePrefixOrderingTests`.

### Hoisting conditional instructions out of T0 (the handling-notes split)

The persona is the biggest T0 block and the cheapest cache anchor we
have, so its size is mostly harmless — it is read for free from turn 2
onward. What is *not* harmless is that every token of it is also read
by the model on every turn, cached or not, and instructions that apply
to a situation which is absent 99 % of the time are noise in the middle
of the ones that always apply.

Many of the persona's sections are exactly that shape: paragraphs
explaining how to handle a cue ("when your mind wanders and connects two
things: …"), each one dead weight on the overwhelming majority of turns
where no such cue exists. They now live in T6, next to the block that
triggers them.

They also live in their own file. `data/persona/conditional_handling.txt`
sits beside `aiko_companion.txt` and holds them; the persona proper is
once again "the text that goes out every turn", which is the property
that makes it readable as a prompt. Keeping them inline worked — the
loader still accepts them there, and an inline copy wins, so an install
that customised one before the split keeps its wording — but it made the
persona file lie about itself.

`PromptAssembler._persona_split()` merges the two at load time. The
pairing is **keyed on the prompt block name**, from two registries: a
pooled cue names its header on `CuePolicy` in
`app/core/proactive/cue_accounting.py`, next to the matching and retry
rules for the same cue; anything else is listed in `HANDLING_SECTIONS`
in `app/core/session/prompt_support.py`. The relation is many-to-many. A
block may claim several headers when one renderer covers several
situations (`emotion_episode_block` takes both the feeling and the mask),
and a header may be claimed by several blocks when one passage covers a
family — the three repair detectors share "When you missed the beat:",
because the failure mode it warns about is the same for all of them. The
split therefore resolves each header *once* and hands the text to every
claimant, and `_render_handling_notes` deduplicates, so two of a family
firing together still ship the paragraph once.

The core persona keeps one short stanza (`HANDLING_PREAMBLE`) saying that
cues arrive with their own instructions attached, and
`_render_handling_notes(locals())` appends the notes for whichever blocks
actually rendered. It resolves them off the assembly frame's locals by
the names the tier ladder registers — the same three-candidate lookup
`block_char_table` uses — so hoisting a new block needs no edit at the
call site. Notes come out in ladder order, so on a turn two fire they
read paired with the blocks above them.

Both files' mtimes are in the split cache key and in the static slice
cache key, so editing either takes effect on the next turn rather than
the next restart.

The split is worth roughly 44 k characters (~11 k tokens) off every turn
across 51 blocks and 47 headers — against a persona that is now ~35 k, so
rather more than half of what used to ship unconditionally. The T6
addition is bounded by how many blocks fired: usually zero, at most one
or two, since the cue priority mutex allows a single gap-cue through per
turn.

Not everything conditional-sounding moved, and the reasons are worth
knowing before adding to the registry — both are recorded in
`prompt_support.py`, and `tests/test_persona_hoist.py` asserts nothing on
either list is ever registered.

`_STAYS_IN_T0` names the blocks that **read** like cues and **render**
most turns, where the trade below runs the wrong way. Four are obvious
once stated (`wants`, `user_state`, `day_color`, `profile`). Two were
caught only by checking the provider against the prose: `goals_block`
says "your context *may* include" and in fact renders whenever a single
goal is active, which the onboarding seed guarantees from the first time
a user sets their name; and `grounding_block` is the inverted case —
`grounding_line_mode` defaults to `off`, so a stock install pays for a
section whose block never renders, but the mode is binary and every
install that enables it gets the fused line on essentially every turn.
Hoisting it would optimise the configuration nobody using the feature is
in.

The second list is the sections interleaving always-on inline-tag
grammar (`[[remember:]]`, `[[moment:]]`, `[[predict:]]`, …) with the
handling for one block. `split_persona_section` moves whole sections, so
hoisting one wholesale would take the grammar with it and silently
delete the tag — Aiko simply stops being told she may emit it, and the
feature behind it stops receiving writes with nothing raising anywhere.
Three of these have since been split by hand into headers of their own
(`follow_up`, `anniversary`, `growth_witness`), which is what
`MixedProseSplitTests` guards. Two more turned out to have no
conditional half at all: `arc_block` and `knowledge_gaps_block` emit
*content* rather than handling for content — a direct state line and a
bullet list respectively — so their persona sections are pure tag
grammar and there is nothing to hoist.

Three ways the pairing comes apart silently, all covered in
`tests/test_persona_hoist.py`: a header renamed in the file (the section
stops being hoisted), a block name that is not on the tier ladder, and a
block name that never becomes a local in `assemble_with_budget`. In each
case the text is removed from T0 and nothing brings it back, with no
error anywhere.

This generalises. **The test for hoisting a block out of T0 is whether
its instructions are conditional on something the prompt already knows
about.** If a paragraph starts "when your context says X", the paragraph
belongs wherever X is rendered. What must stay in T0 is anything shaping
voice, values, or the response format — those apply unconditionally.
Note that this trades a *cached* token for an *uncached* one, so it is
only a win when the block is usually absent; hoisting something that
fires every turn would be strictly worse.

## Contributor guide — adding a new prompt block

1. **Pick the right tier.** Ask: "how often does the *content* of
   this block change?" Map honestly:

   - Worker output that mutates on a long cadence (hourly idle
     worker, weekly schedule, persona edit) → T0 or T1.
   - History rollups → T2.
   - Anything that takes `user_text` as input or is computed from
     the just-received turn → T6.
   - Pure affect / style derivatives → T5.

2. **If the block is usually absent, put it late.** This is the one
   place where the honest answer to step 1 leads you wrong. "How often
   does the content change?" invites you to reason about a rare cue as
   *stable* — a self-callback that fires twice a month clearly is not
   per-turn data — and stability points early. But an intermittent block
   does not sit still at its tier; it **flickers**, and appearing and
   disappearing are byte changes exactly like any other. Parked in T1 it
   would invalidate T1 through T6 on the turn it arrives *and again* on
   the turn it leaves, and it would do that for a payload the model sees
   a handful of times a month.

   So the question for a conditional block is not how often its content
   changes but **how often its presence does**, and the answer for
   anything cue-shaped is "twice per firing". Late placement makes those
   two invalidations cost almost nothing, because there is almost nothing
   after them.

   The corollary is that rarity is *not* a reason to promote a block up
   the ladder. It is the reason it is in T6 in the first place.

3. **Append within the right cluster.** In-tier ordering preserves
   behavioural clusters — e.g. K28 `turning_over` must follow K14
   `absence_curiosity` (both T6). Read the surrounding comments in
   `system_parts.append(...)` before slotting yours in. In-tier order has
   no cache consequence, so this is purely about the model reading
   related cues together.

4. **Update `_PROMPT_BLOCK_TIERS`.** Add the block name to its
   tier's tuple. The audit constant must stay in sync with the
   actual cascade so the tier doc here is honest.

5. **Add a per-block test if you're new to the file.** The pattern
   is in `tests/test_prompt_assembler.py` — one test that confirms
   the block renders, one test that confirms its in-tier position
   relative to a known neighbour. If your block introduces a brand
   new cross-tier invariant, add it to
   `PromptCachePrefixOrderingTests`.

### Anti-patterns

The following all destroy the cache prefix; reviewer should reject
on sight:

- **Inlining per-turn data into the persona file.** The persona
  block is the cheapest cache anchor we have. Adding the user's
  current mood to the persona — or anything that requires reading
  AffectState / the live turn — breaks the prefix for every model
  on the planet.
- **Putting `user_text` into a T0/T1 block.** Detectors that take
  the just-arrived message as input are T6 by definition. If you
  find yourself plumbing `user_text` into a T1 provider, the
  block has the wrong tier.
- **Appending blocks in `if`-conditional order rather than tier
  order.** "I added this block right next to the related one" is a
  fine behavioural heuristic when both blocks are in the same tier;
  it's a cache disaster across tiers.
- **Promoting a rare block up the ladder because "it barely ever
  changes".** See step 2: for a conditional block, arriving and leaving
  are the changes, and they land on every firing. Stability of *content*
  only earns an early tier when the block is also reliably *present*.
- **Re-inserting `system_parts.append(circadian_block)` ahead of
  the persona** "because circadian should be one of the first
  things the model reads." The model reads the entire system block
  — order doesn't affect comprehension, but it absolutely affects
  caching.

## Worked example — pricing impact at 50 k context

A realistic per-turn cost at 50 k input tokens + 250 output tokens
(typical for `max_tokens=512`), comparing zero cache to a
realistic warm cache:

| Model | Cold cache (0 % hit) | Warm cache (90 % hit) | Per 100 warm turns |
|---|---|---|---|
| `gpt-5-nano` | ~$0.0026 | ~$0.0004 | ~$0.04 |
| `gpt-5-mini` | ~$0.0130 | ~$0.0021 | ~$0.21 |
| `gpt-4.1-nano` | ~$0.0051 | ~$0.0015 | ~$0.15 |
| `gpt-4.1-mini` | ~$0.0210 | ~$0.0058 | ~$0.58 |

The whole point of the tier ladder is to keep cache-hit-rate up
around the 90 % column on consecutive turns inside one thread.

## Measuring it in practice

Two grep recipes against `data/app.log`:

```sh
# Per-turn cache hit-rate (highest is best; OpenAI healthy sessions
# settle around 80-95 from turn 2 onward).
rg 'turn done:' data/app.log | rg -o 'cached_pct=[0-9.]+' | sort | uniq -c
```

```sh
# Find turns where the cache hit-rate fell off a cliff — the next
# DEBUG `prompt built:` line above the regression usually shows
# which provider count changed.
rg -B 2 'turn done:.*cached_pct=[0-9]\.[0-9]' data/app.log
```

For a live-running app, the MCP `get_last_response_detail` tool
returns the same numbers (under `usage.cached_tokens` /
`usage.cached_tokens_pct`) without needing a log read. They also ride
the per-turn WS metrics as `cached_tokens` / `cached_pct`.

## Measuring *where* the prefix breaks (P44)

The hit-rate above tells you the prefix broke. It does not tell you
which block broke it, and guessing from a 30 KB prompt is hopeless. The
P44 telemetry answers that directly: every turn, hash each registered
block, compare against last turn, and report the **earliest** change in
ladder order plus the characters sitting at and after it.

Turn it on for a measuring session:

```json
"logging": { "prompt_cache_log_enabled": true }
```

Records go to `data/prompt-cache.jsonl`, **not** `app.log` — one line
per turn would bloat the main log, and the only consumer is a script.
The `app.promptcache` logger sets `propagate = False` precisely so these
records never reach the stderr / rotating-file / ring-buffer handlers
attached to `app`. Off by default; ~300 bytes per turn.

Then read it back:

```sh
python scripts/prefix_break_report.py
```

Each record pairs our *prediction* with the provider's *answer*:

| Field | Meaning |
| --- | --- |
| `diverged` / `tier` | earliest block whose content changed |
| `lost_chars` / `lost_pct` | characters at and after the break |
| `changed` / `changed_by_tier` | how many blocks moved, and where |
| `history_diverged` | first history index differing from last turn |
| `history_slid` | `>0` window shift, `-1` messages rewritten in place |
| `cached_tokens` / `cached_pct` | what the provider actually cached |
| `est_prompt_tokens` vs `actual_prompt_tokens` | estimator accuracy, **raw** |

Two notes on reading it:

- **`history_slid` is the discriminator.** A positive number is the
  window sliding, which is expected and leaves a stable tail for next
  turn. `-1` means messages that stayed in the window had their text
  rewritten — the fingerprint of the K-time1 relative-age prefixes
  (`history_age_prefix_enabled`, on by default) re-stamping `[3 min
  ago]` to `[4 min ago]` as the clock ticks. That defeats history
  caching in a way a slide does not.
- **The logged estimate is deliberately unscaled.** The breakdown rows
  in the UI are rescaled onto the provider's real token count
  (`_estimate_scale` in `chat_turn_mixin.py`) because a char heuristic
  and a real tokenizer cannot agree. Recording the scaled figure would
  drive `est_error_pct` to zero by construction.

### Settled: the telemetry is validated, and the prefix was breaking on 15 of 16 turns

A 16-turn Grok session answered the open question below, and it is the
second explanation: **the break point moves with `cached_tokens`.** On the
three turns where xAI's cache was warm, the provider's `cached_pct`
(59.9%) landed within half a point of our predicted `100 - lost_pct`
(59.4%). The prediction and the provider's answer are independent
measurements of the same thing, so agreement at that margin means the
`diverged` field can be trusted as the real cause and acted on directly.

What it said: the prefix broke on **15 of 16 turns**, losing a mean of
27,720 chars — **39.3%** of a 70 KB prompt. Three blocks, all fixed:

| Block | Breaks | Why it moved | Fix |
| --- | --- | --- | --- |
| `anniversary_block` | 7/16 | Renders, then stamps `last_anniversaried_at`, so it alternates content and empty on a 6h rotation | Re-tiered T1 → T6 |
| `profile_block` | 7/16 | Rebuilt every message, and `ORDER BY confidence DESC` had no tie-breaker, so equal-confidence bullets reordered on EMA jitter | `, field ASC` tie-breaker + confidence quantised for sorting |
| `narrative_block` | 1/16 | Read fresh per turn by design | Re-tiered T0 → T6 |

**The re-tiering is a real behaviour change, not a constant edit.**
`_PROMPT_BLOCK_TIERS` has to match the physical append cascade in
`assemble_with_budget` (`PromptLadderOrderTests.test_ladder_order_matches_the_cascade`
enforces exactly this), so both blocks moved to just before the user
message. Anniversaries and the narrative arc now read as late context
rather than standing instruction — worth watching in conversation, not
just in the hit-rate.

A separate estimator bug fell out of the same data: `chars_per_token`
starts at 3.5 against Grok's real 4.45 and its EMA (`alpha=0.05`) needed
50–100 turns to close that, resetting on every restart, which is why UI
context stats read ~27% high all session. `observe_actual_usage` now uses
`alpha = max(_CALIBRATION_ALPHA, 1/(samples+1))`, so the first real
observation snaps to the provider's ratio and it reverts to the slow EMA
after ~20 samples.

### Re-measured after the fixes: two of three worked, and the total barely moved

A second 20-turn `grok-4.3` session, run against a snapshot of the real
data in a container, says the three fixes above each did their job and the
number they were supposed to move **did not move**:

| | Aug 3, before | Aug 4, after |
| --- | --- | --- |
| `lost_pct` mean | 39.3% | **38.1%** |
| earliest break | `anniversary_block` 7, `profile_block` 7, `narrative_block` 1 | `arc_block` **17**, `axes_block` 1, `profile_block` 1 |
| `cached_pct` max | 59.9% | 63.9% |
| `est_error_pct` | 27% on turn 1, 50–100 turns to settle | 25.4% on turn 1, **0.2% from turn 2** |
| `chars_per_token` | 3.5 start, never converged | 4.397 first → 4.435 |

The estimator fix is unambiguous and needs no follow-up. The three
prefix-breakers are all gone from the `diverged` column — `profile_block`
fell from 7/16 to 1/20 and the two re-tiered blocks vanished entirely. But
`arc_block` immediately took over at 85%, and the cost of the break is
unchanged, because **`diverged` only ever names the *earliest* break.**
Fixing the top of the list promotes whatever was hiding behind it. `arc_block`
was invisible in the Aug 3 data purely because `anniversary_block` and
`profile_block` sat earlier in ladder order.

The lesson is structural rather than about any one block: **the early tiers
contain a population of per-turn-mutating blocks, so this is iterative by
construction, and each pass needs a fresh measurement.** Treat a
`diverged` histogram as one layer of an onion, not a to-do list.

`arc_block`'s own defect is worth naming because it is a pattern, not a
one-off. [`ConversationArcStore.render_block`](../app/core/conversation/conversation_arc.py)
renders `Conversation arc: playful banter (last ~4 turns).`, where the
count comes from `current_turn - state.since_turn` and `current_turn` is
the session's message count. That is **a monotonic counter baked into
cacheable text**: it is guaranteed to differ every turn the block renders,
no matter how stable the arc itself is. `axes_block` is the same shape with
continuous floats (four axes drifting up to ±0.08 per turn), and the
`history_slid = -1` churn is the same shape again (K-time1 re-stamping
`[3 min ago]` to `[4 min ago]`).

Two fixes are available and the second is better. Re-tiering to T6 is
mechanical and would work, but it evicts a semantically stable block from
the tier it belongs in for a formatting reason. **Quantising the varying
part** — bucketing `elapsed` to "just started" / "the last few turns" /
"a while now", the same medicine as the `profile_block` confidence
quantisation — fixes the churn *and* lets the block stay in T1, where a
slowly-changing conversation arc genuinely belongs. The distinction between
`~3 turns` and `~4 turns` is not one the model can act on.

The general rule this implies: **no monotonic counter and no unquantised
float may reach the rendered text of a T0–T2 block.** Worth a test rather
than a convention, since it is invisible until someone runs a 20-turn
measuring session.

Also settled by the same run: the persona measures **35,938 chars / 8,103
tokens**, not the "~78k chars, ~19.5k tokens" recorded in
[`perf.md`](personality-backlog/perf.md) under P31. And because T0 now
caches, `get_prompt_block_costs` ranks it *third* by effective cost behind
`relevant_context` (2,027) and `handling_notes_block` (1,216) — so the
persona trim is no longer where the value is. Note the shape of the loss
while you are here: `T1_semi_stable` renders **231 tokens in total**, and a
~50-token block in it is discarding roughly 7,000 tokens of T2–T6 behind it.
The blocks that cost the most are not the ones that are large.

### Aug 6: `profile_block` came back, and the onion is now visible all at once

A 67-turn session (16 `grok-4.3`, then 51 `gpt-5.6-luna`) put
`profile_block` back at the top of the histogram — **45 of 67 turns**,
worse than the 7/16 that started this whole thread, and mean `lost_pct`
back to 39.7%.

It is a regression, not a relapse. The Aug 3 fix quantised the
confidence sort in the SQLite profile query, and that query is still
doing its job. L28 then put **concepts** at the head of the same block,
reading through [`ConceptView`](../app/core/concepts/concept_view.py) —
a lane that sorts on raw live confidence and truncates at a cap, so it
bypassed the guard entirely and reintroduced the identical bug one layer
up. The store now holds 254 active `subject=user` identity/value
concepts, all above the bar, competing for 10 slots with neighbours
~0.003 apart; L3 nudges confidence every lifecycle tick, so both the
ordering *and the membership at the cut* moved almost every turn. Same
medicine applied in `core()`, `for_target()` and `core_lane()`, with
`concept_id` as the tie-breaker rather than the label — L17 can now
rewrite a label in place, which would put the churn straight back.

The bigger change is the measurement. Every pass so far has been blind
past the first break, which is what made this an onion: `diverged` names
one block and hides the rest, so each fix promotes an unknown successor
and needs a whole fresh session to identify it. `diagnose_divergence`
was already grouping the movers by tier and discarding the result, so
the fix was mostly plumbing — `changed_blocks` and `changed_by_tier` now
reach the JSONL, and `prefix_break_report.py` grew a **ladder
discipline** section that ranks every tier's change rate at once and
flags any inverted pair, naming the block filed higher than it behaves:

```
  verdict:
      INVERTED  T0_stable (66.7%) churns more than T1_semi_stable (21.7%)
      biggest offender: profile_block moved on 66.7% of turns while filed under T0_stable
```

That turns "no monotonic counter or unquantised float in a T0–T2 block"
from a convention into something a session can be checked against. Only
tiers that actually moved are ranked against each other — a silent tier
is perfectly stable, and scoring it as an inversion victim would bury
the real signal in noise.

Two findings from the same session that are **not** ours to fix by
re-tiering, recorded so the next pass does not chase them:

- **Cross-turn caching on `gpt-5.6-luna` was zero on 45 of 51 turns**,
  and the six hits are the second leg of a Responses tool round-trip
  hitting the first leg's cache seconds earlier — not turn-to-turn
  reuse. `prompt_cache_key` is set correctly and the persona prefix is
  genuinely byte-stable, so this is not explained by the divergence
  data. Worth a targeted experiment before assuming prompt structure is
  the lever.

  **Update, Aug 09 — it is now total, and it is not a reporting bug.**
  Summing `cached=` off the `turn done:` INFO lines in `data/app.log`
  gives 55,040 cached on Aug 02 (19/19 turns hit) and 73,728 on Aug 03
  (27/27), then 123,825 on Aug 05 with only 6 of 47 turns hitting, then
  **zero on all 138 turns across Aug 06–09** — roughly 3.2M uncached
  prompt tokens in four days. The cliff lands to the hour on `eadc80e`,
  which moved this model onto `/v1/responses`.

  Three things it is *not*, each checked: the Responses stream parser
  reads `input_tokens_details.cached_tokens` correctly (those six Aug 05
  hits were recorded *through that path*, so it demonstrably works and
  the zeros are real); `prompt_cache_key` is still threaded from
  `session_key`; and the prompt is ~17k tokens, far above the 1024-token
  floor. OpenAI's own usage dashboard agrees on the cached figure —
  its Aug 05 bar reads 123,825, matching our log to the digit — though
  its *uncached* series is separately broken, reporting 23,384 for a day
  that really cost 1.39M, so don't diff against that half of the chart.

  **Resolved, Aug 24 — it was a model behaviour change, and it needed an
  explicit breakpoint.** See
  [Aug 24: the zero was GPT-5.6 dropping prefix fallback](#aug-24-the-zero-was-gpt-56-dropping-prefix-fallback)
  below. Nothing about our prompt structure was the lever, which is why
  three rounds of re-tiering never moved it.

  Note for whoever picks this up: the `turn done:` line carries
  `cached=` and `cached_pct=` on **every** turn, so the history above is
  recoverable from `data/app.log` alone. The JSONL sink only needs
  turning on when the question is *where* the prefix broke, not whether
  it cached at all.
- **The tool pass is the real latency cost on tool turns** — p50 7.1 s,
  p90 28 s, max 41 s — and it is invisible in `first_token_ms`, which
  only ever measures the streaming pass.

### Aug 24: the zero was GPT-5.6 dropping prefix fallback

The four-day zero above was not a prefix problem at all, which is why
three passes of re-tiering never touched it. It is a **documented model
behaviour change**, and it makes almost everything above this section
history rather than guidance for `gpt-5.6-luna`.

From OpenAI's [prompt caching guide](https://developers.openai.com/api/docs/guides/prompt-caching):

> GPT-5.6 and later model families cache exact prompt prefixes at cache
> breakpoints. By default, the service places an implicit breakpoint at
> the latest user or tool message. **Unlike earlier models, it does not
> automatically fall back to the longest matching unmarked prefix before
> that breakpoint.**

Every earlier model — and Grok, which is why the `grok-4.3` turns in the
tables above cached fine — did best-effort matching in 128-token blocks
against the longest stable prefix. 5.6 does exactly one comparison, at
the latest user message. Aiko's prompt is a ~41k-char byte-stable head
followed by ~33k chars of per-turn blocks and then the new message, so
that single comparison **can never match**: the tail differs every turn
by construction. The stable head was invisible to the matcher.

It was also not free. On 5.6+ cache writes bill at **1.25× the uncached
input rate** (they were free before), so the failure mode is not "no
discount" but "a 25% surcharge on nearly every input token, to build an
entry nothing can read". Over Aug 05–24: 24.6M cache-write tokens,
123.8K cache-read, 26.7K uncached — an effective **1.244× multiplier on
24.8M tokens**.

Three things ruled out first, so the diagnosis isn't circumstantial:
`prompt_cache_key` was correctly threaded, the prompt is ~18.4k tokens
against a 1,024 floor, and the parser demonstrably works (the six Aug 05
hits arrived through it). The cliff also lands to the *minute* on
`eadc80e` — last hit 18:25:30Z, first zero 18:32:18Z **in the same
conversation**, commit timestamped 18:27Z — which is a deploy, not drift.

#### The fix: mark the end of the stable head

Two halves, and the seam between them is a private key on the system
message (`CACHE_BREAKPOINT_KEY`, the same smuggling pattern as
`_responses_output`):

1. **`prompt_assembler`** tracks `stable_head_parts` — the leading
   `system_parts` entries that are byte-stable turn over turn: persona
   plus the constant grammar addenda, stopping deliberately *short of*
   `profile_block`. `_stable_prefix_offset` renders that head with the
   same join and the same empty-part filter as the real prompt, so the
   offset always lands on a block boundary, and returns `0` (meaning
   "don't mark") when the head is under `_CACHE_BREAKPOINT_MIN_TOKENS`.
2. **`openai_compatible_client`** splits that message into two
   `input_text` blocks at the offset and puts
   `prompt_cache_breakpoint: {"mode": "explicit"}` on the first.

Measured on the live persona: the head is **40,676 chars — 55% of the
73,919-char system prompt, ~9,700 of 17,660 prompt tokens**. That moves
the effective input multiplier from 1.244× to ~0.617×, i.e. **input
costs roughly half** what it does now.

Four things that are easy to get wrong, all pinned by
`tests/test_prompt_cache_breakpoint.py`:

- **Explicit-only mode is deliberate.** `prompt_cache_options.mode =
  "explicit"` disables the implicit breakpoint. That is the *point*: the
  implicit one sits behind ~33k chars of per-turn blocks, so leaving it
  on keeps paying 1.25× to write an entry that can never be read. This is
  the one case the guide names for explicit-only — "a stable prefix
  followed by request-specific content that is unlikely to be reused".
- **Never set explicit mode without a breakpoint.** Per the guide, "if
  you set `mode` to `explicit` but provide no explicit breakpoints, the
  request does not use prompt caching" — it does not fall back to
  implicit, it turns caching *off*. So the flag is computed from both the
  model gate **and** an offset actually being present.
- **Gate on the model name, not `api_style`.** Grok is forced onto the
  Responses surface but still does implicit prefix matching and caches
  for us unaided; sending it an unknown field risks a 400 to fix a
  problem it doesn't have. `_supports_explicit_cache_breakpoints` parses
  the dotted version numerically (`>= (5, 6)`) so `gpt-6` isn't missed.
- **The bare key is stripped on `/v1/chat/completions`.** Breakpoints are
  expressed on content blocks there, and an unknown message field 400s on
  strict providers. A 5.6 route pinned to `api_style="chat_completions"`
  therefore keeps the old implicit-only behaviour — supporting that shape
  too is the obvious follow-up if anyone needs it.

**What this means for the re-tiering work above.** The breakpoint sits
*ahead* of both `arc_block` (45% of breaks) and `profile_block` (30%), so
their churn no longer costs anything on the cached head — the whole
"fixing the top of the list promotes whatever was hiding behind it"
treadmill stops being urgent on 5.6. It still matters for a *second*
breakpoint after T2, which is where the next ~10% would come from, and it
still matters on every other provider. `lost_pct` and the `diverged`
histogram remain the right instrument for those.

#### Confirmed live, Aug 24

Both sides agree, and the shape of the numbers is the proof rather than
just the size of them.

Ours, from `cached=` on the `turn done:` lines in `data/app.log`:

| Day | Turns | Hits | Cached tokens |
| --- | --- | --- | --- |
| Aug 21 | 53 | 0 | 0 |
| Aug 22 | 42 | 0 | 0 |
| Aug 23 | 23 | 0 | 0 |
| **Aug 24** | **60** | **42 (70%)** | **599,334** |

OpenAI's dashboard for the same day: hit rate **56.7%** (was 0.5%), cache
reads per write **11.52×** (was <0.01×), 578.5K read / 50.2K write /
391.5K uncached. That works out to an effective **0.502× input
multiplier against the 1.244× before it — input costs 60% less**, against
a predicted 50%.

The `cached_tokens` distribution is what actually validates the
mechanism, because it is trimodal with nothing in between:

| Value | Turns | What it is |
| --- | --- | --- |
| **10,423** | 31 | The stable head, read once. Byte-identical every turn — exactly what a fixed explicit breakpoint should produce. |
| **25,111** | 11 | Turns where the **tool pass also ran**, summing two requests. Tool schemas render into the prefix ahead of the messages, so that pass caches *more* than the head. |
| **0** | 18 | Cold starts — first turn of a session, or past the 30-minute TTL. |

Two things worth taking from that. The tool pass caches too, which was
not designed for and is worth ~15k tokens on the turns it runs (note the
9 turns reading 25,111 with `tools=0`: the gate ran the decision pass and
it chose not to call anything). And 18 cold starts in 60 turns is the
remaining headroom — each one is a full-price write, so anything that
shortens sessions or idles past 30 minutes pays for a re-warm.

### The original open question

Observed on a Grok (`xai` / `grok-4.3`) session: `cached_tokens` is
**bimodal with nothing in between** — `192` on most turns, `10304` /
`10496` on a few. Every value is a multiple of 64, so xAI caches in
64-token blocks: 3 blocks on a miss, ~161 on a hit, out of a
~15,400-token prompt.

Two explanations, and the divergence data separates them:

- Break point stays put while `cached_tokens` swings → the misses are
  xAI routing or cache TTL, and no prompt restructuring helps.
- Break point moves in step with `cached_tokens` → something inside the
  first ~192 tokens (~670 chars, the very front of `T0_stable`) changes
  most turns and discards everything behind it.

**Within-tier cue cycling is still not worth building**, and the
resolution above does not change that. Rotating cue order inside T6 to
preserve a prefix only pays off once the prefix reliably survives T5, and
the ~10,300-token ceiling this setup has cached sits around the T4/T5
boundary, below where the cue blocks live. Revisit only after a session
measured *post*-fix shows `lost_pct` down near 10% and the ceiling
actually risen — the three fixes above predict that, but predicting it is
not the same as having seen it.

## Worker prompts and the cache

Background workers (`SummaryWorker`, `MemoryExtractor`,
`ReflectionWorker`, `BeliefInferenceWorker`, …) use the same
`ChatClient`s as the main chat — they land in their own cache slot
because their `messages[0]` system prompt is different from Aiko's
turn loop. **Worker calls do not invalidate the main chat's cache.**
They also don't *share* its discount: each worker accrues its own
warm-cache benefit (or misses) independently.

Cost-wise this means: routing workers to local Ollama (the default)
keeps every worker pass free. Routing workers to OpenAI is fine —
they'll warm their own small cache slot — but the per-turn cost is
no longer dominated by the main chat.

## See also

- [`llm-providers.md`](llm-providers.md#openai-prompt-caching) —
  per-model pricing, cache TTL nuances, ergonomic notes.
- [`configuration.md`](configuration.md) — the `llm.routes[role].max_tokens`
  / `llm.routes[role].context_window` knobs that bound the input column.
- [`AGENTS.md`](../AGENTS.md) — top-level project conventions,
  including "Debugging via logs" → "Low cache-hit rate on OpenAI".
- `app/core/session/prompt_assembler.py::_PROMPT_BLOCK_TIERS` —
  the audit constant pinned next to the actual cascade.
- `tests/test_prompt_assembler.py::PromptCachePrefixOrderingTests` —
  the cross-tier invariants enforced in CI.
- `tests/test_prompt_assembler.py::PromptLadderOrderTests` — asserts the
  constant's order equals the real `system_parts` cascade, which
  `lost_chars` depends on.
- `app/core/session/prompt_prefix_telemetry.py` — the P44 divergence
  diagnosis and its JSONL sink.
