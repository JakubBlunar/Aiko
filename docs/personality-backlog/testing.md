# Testing + evaluation (T-series)

How we verify the behavioural system stays correct as the concept work
(L-series) and future K-patterns pile on. Parked here after a design
discussion: unit tests cover the *pure helpers* well, but nothing
exercises the **worker -> cue -> prompt -> reply -> post-turn -> state**
chain end-to-end, and the LLM sits in the middle as an untestable box.

The organising idea is **bracket the LLM**: everything *before* it
(`PromptAssembler` turning state into a prompt) and everything *after*
it (`response_text_service` tag parsing + `_post_turn_inner_life` state
deltas) is deterministic and testable with a scripted LLM double. The
model's *content quality* is a separate concern handled by an eval
track (T5), never a red/green unit gate.

**Relationship to the DT-series** (in [`tools.md`](tools.md)): DT1/DT2/DT4
are *live-app* MCP-driven debug tooling (drive the running instance,
advance its clock, snapshot state). The T-series is the *offline* pytest
counterpart — the same behaviours, but as fast, deterministic, CI-runnable
tests with a fake LLM. They share concepts (fixed clock, "which blocks
fired") and should share code where sensible (T6 `Clock` seam feeds DT1;
the T2 harness and the DT4 replay harness are the offline / live faces of
one idea).

**Relationship to K10 / L22.** K10 persona regression (SHIPPED, on-demand)
and L22 concept-quality eval are both members of the T5 eval family;
[`data/persona/golden_turns.jsonl`](../../data/persona/golden_turns.jsonl)
is the existing seed corpus. T5 is the umbrella that gives them a shared
runner + scoreboard.

---

## T1. Shared `FakeChatClient` + `BehaviorHarness`

**Motivation.** Every worker / turn test today hand-rolls its own LLM
stub (`MagicMock`, `FakeOllama`, `_FakeOllama`, `_OllamaBlock`, …), so
there is no single, trustworthy LLM double and no reusable way to stand
up the collaborators a behavioural test needs. This duplication is the
reason no end-to-end chain test exists (T2 is blocked on it). Build the
missing seam once.

**Key files (new + existing).**
- Existing contract: [`app/llm/chat_client.py`](../../app/llm/chat_client.py)
  — the `ChatClient` structural protocol (`chat`, `chat_with_tools`,
  `chat_stream`, `chat_json`) that `OllamaClient` /
  `OpenAICompatibleClient` satisfy. The fake implements the same protocol.
- New: `tests/support/fake_chat_client.py` — a scripted `FakeChatClient`
  that returns canned responses per call (queue of replies, or a
  `{prompt-substring -> reply}` map), records every call for assertions,
  and can be told to *raise* (for the T3 fault-injection path). Must
  cover `chat_stream` (yield tagged deltas), `chat_with_tools` (return a
  scripted tool call or `respond_directly`), and `chat_json`.
- New: `tests/support/harness.py` — a `BehaviorHarness` that assembles
  a temp `ChatDatabase` + `MemoryStore` + real `IdleWorkerScheduler` +
  real `PromptAssembler`, wired to a frozen clock + seeded RNG (see T6),
  with `FakeChatClient` injected as both the chat and maintenance client.

**Sketched approach.**
- Follow the existing temp-DB patterns (`test_schedule_learner.py`,
  `test_agenda.py`) for store setup.
- Expose small ergonomics: `harness.run_worker(name)` (wraps
  `scheduler.force_run`), `harness.assemble_prompt(user_text)`,
  `harness.run_turn(scripted_reply)` (drives `TurnRunner` /
  post-turn with the fake's reply), and assertion helpers over `kv_meta`
  / stores / rendered prompt blocks.
- Keep it in `tests/support/` (a plain package, not a pytest `conftest`
  — the suite is `unittest`-based with no shared fixtures today).

**Open questions.**
- How much of `SessionController` to instantiate for a "turn" test — the
  full mixin stack is heavy; likely a minimal `ChatTurnMixin` host with
  only the collaborators a given test touches.
- Whether `_post_turn_inner_life` runs whole or gets a per-test
  allow-list (some pieces are expensive; `test_vulnerability_budget_post_turn.py`
  already stubs the whole thing out today — the harness should make
  running *most* of it cheap enough to keep).

**Effort.** Medium. Unlocks T2 and the T3 fault-injection variant.

---

## T2. End-to-end behavioural chain tests

**Motivation.** Unit tests verify "the cue *would* render given this
state" and "the worker's internal logic is correct" — but not that the
pieces are wired together. A worker writing to the wrong `kv_meta`
journal key, a T6 provider reading a stale watermark, or the tag parser
dropping a `[[goal:]]` all sail through green today. These wiring bugs
are exactly what gets more likely as the concept layer adds providers
and journals.

**Key files (existing seams).**
- `IdleWorkerScheduler.force_run(name)` — fire a worker past the timer /
  quiet gate.
- `PromptAssembler.set_inner_life_providers(...)` + the T6 providers —
  assert the cue actually lands in the assembled prompt.
- [`response_text_service.py`](../../app/core/services/response_text_service.py)
  — tag parsing (`[[reaction:]]`, `[[goal:]]`, `[[predict:]]`,
  `[[remember:]]`).
- [`post_turn_mixin.py`](../../app/core/session/post_turn_mixin.py)
  `_post_turn_inner_life` — the state deltas to assert against.

**Sketched approach.** On top of T1's harness, one test per chain:
1. seed state; `harness.run_worker("forward_curiosity")`
2. assert the cue landed in `kv_meta`
3. `harness.assemble_prompt(...)`; assert the T6 block contains it
4. `harness.run_turn(scripted_reply="[[goal:...]] ...")`
5. assert `GoalStore` / affect / journal watermark advanced

Start with **one** path (forward-curiosity cue -> prompt -> goal) as a
proof-of-concept, then template the rest (rupture/repair, mood drift,
wants ledger, belief prediction, task cues).

**Open questions.**
- Which chains are highest-value to cover first (lean: the ones with the
  most cross-file hops — curiosity, beliefs/predictions, task cues).
- How to keep these from becoming brittle snapshot tests — assert on
  *presence of the cue payload*, not the exact rendered wording.

**Effort.** Medium (small per chain once T1 exists).

---

## T3. Worker registry conformance + smoke + fault injection

**Motivation.** There are ~51 `*_worker.py` files. Most "is this worker
even doing its job" failures are mechanical: not registered, crashes on
an empty DB, non-idempotent `force_run`, `is_ready` with a hidden side
effect, a name collision, or a wedged scheduler thread when the worker's
optional LLM call throws. One parametrized test over *every registered
worker* buys enormous coverage for almost no code.

**Key files (existing).**
- [`idle_worker_scheduler.py`](../../app/core/proactive/idle_worker_scheduler.py)
  + the `IdleWorker` protocol (`app/core/proactive/idle_worker.py`).
- The registration mixins (`speaking_workers_init_mixin.py`,
  `idle_workers_init_mixin.py`) — the source of the "all registered
  workers" list to iterate.

**Sketched approach.**
- **Conformance:** iterate all registered workers; assert unique `name`,
  positive `interval_seconds`, `is_ready(now, last_run_at)` is pure
  (calling it twice doesn't mutate state), and the object satisfies the
  protocol.
- **Smoke:** `force_run` each worker twice against (a) an empty DB and
  (b) a lightly-seeded DB from T1's harness; assert no exception and no
  state corruption (second run idempotent or monotonic).
- **Fault injection:** run the LLM-using workers (consolidation,
  conflict, diary, dream, belief, topic label/digest) with a
  `FakeChatClient` set to raise; assert the worker degrades gracefully
  and the scheduler thread survives (no wedge, error logged, next tick
  still fires).

**Open questions.**
- Getting a clean "all registered workers" handle without booting the
  full `SessionController` — may need a small registry-collection seam in
  the init mixins.
- Which workers legitimately need seeded state to run at all (skip-list
  vs. minimal seed helper in the harness).

**Effort.** Small–Medium.

---

## T4. Prompt-build + tag-parse contract (golden) tests

**Motivation.** The two deterministic halves the LLM sits between deserve
explicit contract tests independent of any worker chain: (a) given a
frozen state, the assembled prompt contains exactly the expected blocks
in the expected T0->T6 order; (b) given a raw tagged model output, the
parse + post-turn produces exactly the expected state delta. Together
they pin the prompt-cache prefix ladder and the tag vocabulary — both
easy to break silently when adding a block or a tag kind.

**Key files (existing).**
- `prompt_assembler.py` `assemble_with_budget` + `_PROMPT_BLOCK_TIERS`
  — the tier-order invariant (the T0->T6 prefix-stability ladder).
- `response_text_service.py` — the tag grammar.
- `tool_pass_gate.should_run_tool_pass` — a pure function; golden inputs
  -> run / skip decisions (guards the "don't over-fire tools on banter"
  contract).

**Sketched approach.**
- Prompt goldens: fixed provider set -> assert block *identities* and
  order (not full wording) so the cache-prefix ladder is regression-safe;
  a separate coarse "resting token floor" assertion (ties to P31).
- Parse goldens: a table of `raw_output -> expected (tags, cleaned_text,
  state_delta)` covering every inline tag kind, including malformed tags
  (assert graceful strip, no crash).

**Open questions.**
- Snapshot storage: inline expected values vs. `tests/goldens/*.txt`
  (lean inline for parse, file-based only if a prompt snapshot gets big).

**Effort.** Small.

---

## T5. LLM behavioural eval suite (separate track, not a CI gate)

**Motivation.** The one thing you genuinely *cannot* assert is "did the
model behave in character / use the cue naturally." Trying to pin that
with unit assertions is the trap. It needs an **eval** track: scripted
scenarios run against a real, *pinned* small model, scored and tracked as
a **scoreboard over time**, so a model swap or a prompt-tier change shows
up as a *score regression* rather than a flaky red build. This is the
umbrella over K10 (persona regression, shipped/on-demand) and L22
(concept-quality eval).

**Key files (existing + new).**
- [`data/persona/golden_turns.jsonl`](../../data/persona/golden_turns.jsonl)
  — the seed corpus; extend to multi-turn scenarios.
- New: `scripts/eval_runner.py` — runs scenarios, emits a scored report;
  reuses the DT4 replay harness once it exists.
- MCP `send_message(skip_tts=true)` + `get_last_response_detail` — the
  live-drive path (shared with DT4).

**Sketched approach.**
- **Structural / cheap (can gate CI):** well-formed tags emitted when the
  scenario should trigger them; tool-pass fires on tool-shaped turns and
  *not* on banter; no forbidden meta-tag leakage into the transcript.
- **Semantic / rubric (scoreboard only, never a gate):** LLM-as-judge
  against a rubric (in character? recalled the cue naturally? no Barnum
  vagueness?). Slow, costly, drift-prone — track the trend, don't block.
- Keep it out of the default `pytest -q` run; a separate `make eval` /
  explicit invocation with its own budget.

**Open questions.**
- Which small model to pin as the eval baseline (must be reproducible;
  quantised local model vs. a hosted pinned version).
- Judge model + rubric stability — the judge itself drifts; consider a
  fixed judge version and periodic human spot-audit of the scores.
- **L37's surfacing outcome ledger is a second, non-judge objective**, and
  arguably a better one: golden turns and a rubric judge both score whether
  output *looks* right, while the ledger records what actually happened next
  in real sessions. It can't replace the scoreboard (no ground truth per
  scenario, and it only exists for turns a real user had), but a rubric score
  that moves *opposite* to the engaged rate is a strong signal the rubric is
  measuring taste rather than effect. Worth reporting side by side.

**Effort.** Medium (largely unlocked once DT4 + a pinned model exist).

---

## T6. Determinism seams for flake-free chain tests

**Motivation.** Two sources of nondeterminism block reliable chain tests:
the `get_time` tool ([`app/llm/tools/builtins.py`](../../app/llm/tools/builtins.py))
reads the live system clock with no injection seam, and several workers
default to `random.Random()` with system entropy. The T2 chain tests will
be subtly flaky without a frozen clock + seeded RNG threaded through.

**Sketched approach.** Thread a single process-wide `Clock` seam through
the time-gated paths (this is the *same* seam DT1 needs for the live
virtual clock — build it once, share it) and give the RNG-using workers
an injectable seeded `random.Random`. The T1 harness sets both to fixed
values by default. Scope carefully: the brain loop's own timing must
**not** be virtualised (per the DT1 note) — only the relationship /
memory / cue time math and the `get_time` tool.

**Open questions.** Same scoping question as DT1 — enumerate which
subsystems read time directly and must move to the seam vs. which stay.
Land this alongside or just before DT1 so they share the `Clock`.

**Effort.** Small–Medium (shared with DT1).
