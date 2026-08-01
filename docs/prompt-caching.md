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
in `app/core/session/prompt_support.py`. A block may claim more than one
header when one renderer covers several situations.

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

The split is worth roughly 14 k characters (~3.5 k tokens) off every
turn so far — against a persona that is now ~65 k, so a fifth of what
used to ship unconditionally. The T6 addition is bounded by how many
blocks fired: usually zero, at most one or two, since the cue priority
mutex allows a single gap-cue through per turn.

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
`usage.cached_tokens_pct`) without needing a log read.

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
