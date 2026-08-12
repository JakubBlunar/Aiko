# Shipped -- Tools & dev tooling (D / DT-series)

Part of the [shipped log index](../shipped.md). Capabilities Aiko can call and
the debug tooling we use to build her. The image-vision work (D2, both parts)
lives with the task machinery in
[`proactive-tasks.md`](proactive-tasks.md#d2-part-a-local-vision-describe_image-task--one-model-no-cloud-image-tokens).
Open items still live in [`tools.md`](../tools.md).

---

## D3. Fast synchronous web-search brain tool (+ knowledge write-back) — SHIPPED

**Motivation.** `web_search` existed but had been **deliberately pulled
out of the brain's live registry**: "A DuckDuckGo round-trip is too slow
for the fast conversational lane." Both remaining paths were
**asynchronous** — the goal-workflow skill and the F1/F9/G3 workers'
private instances — so results always landed a *later* turn. She could
not look something up mid-conversation and answer from it, which is the
common case: the user brings up a show that aired after her training
cutoff and she either guesses or hedges.

**Status.** Shipped. What changed since it was cut is that the premise
did: LangSearch replaced the DDG scrape, and the P14 gate now keeps the
decision pass off banter turns.

**Measured, not estimated.** The open entry guessed "LangSearch
sub-second to ~2s". Against the live key it is **2.6–3.1s**, consistent
across queries — the dominant cost, and worth knowing before designing
around it. The rest of the budget, from the logs: the decision pass runs
at p50 3.0s / p90 8s, and the streaming pass reaches first token at p50
1.5s. So a turn that searches costs **~7s to her first word** against
1.5s normally. Three results is ~575 tokens, five is ~960; LangSearch's
long-text summaries hit the provider's 1500-char cap and were being
truncated mid-sentence at the tool's 600, so the brain lane uses **3
results at 400 chars** (~450 tokens) — cheaper *and* cleaner.

**The gate is what makes it affordable.** Replaying 800 real user turns
through the new `web` family: **zero** additional decision passes beyond
those already opening for other families, with full recall on twelve
search-shaped probes. Getting there meant dropping the patterns that
looked plausible and weren't — bare `reveal` ("your tail revealed your
feelings"), `released`, `recently`, `do you know about` (that's a recall
question), and `new model` / `new version` / `new patch`, which fire
constantly in a developer's conversation. The novelty patterns allow an
intervening title ("the new *Dandadan* season").

**Corrected assumption.** The open entry's cost model claimed the tool
result "also enters conversation **history**, so it lingers in the next
few turns". It does not: the tool exchange lives only in the per-turn
message list, and history is rebuilt from the persisted user/assistant
text. One turn, then gone.

**Decisions locked.**
- **LangSearch-only, effectively.** The brain lane builds its own
  provider (`build_brain_search_provider`) with **no DuckDuckGo
  fallback** and its own `brain_timeout_seconds` (6s vs the workers'
  12s). A LangSearch outage surfaces as a `ToolError` she narrates, not
  as a silent 10s scrape — the exact regression that got the tool cut.
- **Throttle priority: accept the wait.** The brain tool shares the
  process-wide 1.1s LangSearch gate with the workers. A queue-jump was
  considered and skipped; the worst case is +1.1s on an already-multi-
  second turn, which isn't worth a second scheduling mechanism.
- **Storage: distill async after the turn.** A speaking-window job
  (`_maybe_schedule_search_distill_job`) hands the hits to
  `IdleKnowledgeWorker.distil_and_store`, reusing the F9 impersonal-fact
  prompt, confidence floor, semantic dedupe and `source_urls` citation
  unchanged. Raw snippets are never written. The distil prompt's
  "evergreen, not news" rule does useful double duty here: an episode
  count is kept, a match result isn't.

**Privacy is enforced, not requested.** The query is authored by the chat
model *with the persona, retrieved memories and transcript in view*, so
"does my girlfriend's favourite show have a season 3" is a query it can
plausibly emit. The schema asks for a standalone topic query; the
scrubber (`scrub_claim_for_search`, shared with the F1 fact-checker)
makes a slip non-fatal — names and first-person tokens are dropped, and
hard identifiers (URL, email, address) refuse outright with a `ToolError`
telling her to rephrase. A refused query never reaches the network, and
what the write-back stores is the *scrubbed* query, not the original
phrasing.

**The wait is narrated.** Before dispatch she speaks one short line
("hang on, let me check") through the TTS callback, reusing the
`FillerInjector` pattern — **spoken only**, never `on_token`, so the
transcript stays exactly her own words. The slow-first-token filler is
suppressed for that turn so "hang on, let me check" isn't followed by
"Hmm,". Typed chat gets the existing tool-activity chip instead.

**Files.** [`web_search_brain.py`](../../../app/llm/tools/web_search_brain.py)
(new tool), [`providers.py`](../../../app/llm/search/providers.py)
(`build_brain_search_provider`),
[`tool_pass_gate.py`](../../../app/core/session/tool_pass_gate.py) (`web`
family), [`tools_registry_mixin.py`](../../../app/core/session/tools_registry_mixin.py)
(registration), [`turn_runner.py`](../../../app/core/session/turn_runner.py)
(`_SLOW_TOOLS` / `_announce_slow_tool`),
[`idle_knowledge_worker.py`](../../../app/core/proactive/idle_knowledge_worker.py)
(`distil_and_store`),
[`speaking_window_jobs_mixin.py`](../../../app/core/session/speaking_window_jobs_mixin.py)
(post-turn job). Switches: `tools.web_search` +
`search.brain_tool_enabled`. Reference:
[`docs/tools.md`](../../tools.md#synchronous-web-search-d3).

---

## DT1. Virtual clock / time-travel for time-gated features — SHIPPED

**Motivation.** A large fraction of Aiko's behaviour is **wall-clock
gated**, which makes it brutal to verify end-to-end in the live app:
memory decay + tier promotion (schema v8), anniversaries + milestones
(J8), the cooldowns on nearly every inner-life cue, reconnection /
gap-return (J5 / K28 / K36), day colour (K27), routine learning (K3),
vulnerability-budget regen (K15), the conflict-repair watch window (J6).
The only way to exercise these in a running instance used to be to **wait
real hours or days**.

**Status.** Shipped, gated behind `AIKO_DEBUG_CLOCK=1`.

The seam already existed and was built for this:
[`timephrase.now()`](../../../app/core/infra/timephrase.py) routes through a
swappable `_now_provider` whose docstring names DT1 as its intended
consumer — but nothing in production ever called `set_now_provider`, and
only five call sites used `timephrase.now()`. The work was **adopting**
the seam, not designing one.

[`DebugClock`](../../../app/core/infra/debug_clock.py) holds an in-memory
`timedelta` offset and installs itself as the provider. Offset-based
rather than absolute, so time keeps *flowing* while shifted and the
un-virtualised monotonic paths stay coherent. A new `timephrase.utcnow()`
gives the UTC-normalised twin the rest of the app needs (`now()` stays
local for its five existing callers), and ~60 per-module `_utcnow()` /
`_now_iso()` helpers now delegate to it. Two of those redirects carry most
of the leverage: `IdleWorkerScheduler._utcnow()` is the single `now`
handed to every worker's `is_ready()`, and the `clock=` constructor
defaults on ~20 workers.

**Two levers, because wall-clock is not the interesting one.** Concept
(L3) and memory decay run on
[`EngagementClock`](../../../app/core/infra/engagement_clock.py) —
accumulated *active-conversation* seconds — so `advance_clock(days=60)`
does nothing to them at all. `advance_engagement(days)` credits that
counter instead. It is the one persisted, destructive operation, so it
stashes an undo anchor (`engagement.debug_anchor`) that `reset_clock`
restores.

Five MCP tools in
[`debug_clock_tools.py`](../../../app/mcp/server_tools/debug_clock_tools.py):
`get_clock_status`, `advance_clock`, `set_clock`, `advance_engagement`,
`reset_clock`. A live offset is echoed into `get_status` and logged at
WARNING on every advance, so it can never be silently in effect.

**The dividing line:** `datetime`-based *narrative* time moves; every
`time.monotonic()` / `time.time()` reader stays real. That rule maps
almost exactly onto the must-not-virtualise list (LLM latency, TTS/STT
audio, brain loop, HTTP timeouts, tick budgets, worker perf metrics are
all monotonic), so the safety boundary came nearly for free. Also left on
real time on purpose: log + crash timestamps, `app/core/tasks/` stall
detection (a runtime timeout compared against real wall time in
`task_heartbeat.py` — virtualising the stamps but not the comparison
would manufacture phantom stalls), and outbound weather API dates.

**Acceptance.** Against a copy of a live `chat_sessions.db`, simulating 60
engaged days took ~90 seconds and moved mean active-concept confidence
0.817 → 0.483, with concepts crossing the dormant floor — a transition the
L22 scoreboard says needs ~54 hours of real conversation to observe.

**Known sharp edges.** Rows written while advanced keep their virtual
timestamps after a reset; nothing can rewrite them, so run against a
database copy. And L3 decay is catch-up-clamped per sweep
(`concept_decay_max_catchup_days`, default 3), so a long simulation means
interleaving `advance_engagement(3)` with `force_concept_lifecycle`
rather than one big jump.

**The failure mode to watch for when adding a worker**, since it is silent
and only shows up as "time travel didn't affect this one": a worker that
calls `datetime.now(timezone.utc)` directly instead of taking `clock=` opts
itself out of the seam. `ConceptGateTunerWorker` did exactly that and went
unnoticed for weeks — its daily cadence *and* its `concept_min_history_days`
maturity gate, the one whose entire purpose is waiting for calendar time,
could not be advanced while the rest of the concept stack could. It surfaced
from the other end, as a test that compared a fixed `last_run` against real
time and aged into failure. Take `clock=` and default it to
`timephrase.utcnow`, as every other concept worker does.

**Workflow:** [`rules/debugging.md` §f](../../../rules/debugging.md).
**Tool reference:** [`rules/mcp-server.md`](../../../rules/mcp-server.md).
