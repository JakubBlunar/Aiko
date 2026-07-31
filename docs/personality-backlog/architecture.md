# Architecture + code quality

Every other file in this backlog is about what Aiko *does*. This one is
about what the code costs to change. The A-series came out of the audit
that adopted ruff (see [`pyproject.toml`](../../pyproject.toml) for the
rule selection and `npm run lint` for the entry point), which cleared the
mechanical findings and left five structural ones that are too large to
fold into a lint pass.

**None of these is a defect the user can observe.** They are all the same
kind of risk: a change that *should* be local isn't, and nothing tells you
until something breaks at runtime. That framing matters when picking one
up — an A-item never buys a feature, it buys the next twenty features
being cheaper, so it competes with the K/L series on that basis and
usually loses. Pick one when it is actively in your way.

**The baseline is good, which is why these stand out.** As of the lint
adoption: 777 Python files / ~273k lines (162k in `app/`, 109k in
`tests/`), zero bare `except:`, zero star imports, `ruff check` green on
`F`/`E`/`W`/`B`, 7,204 tests passing, and every tracked text file LF. The
problems below are structural, not hygienic.

**Audit note.** The findings were re-measured against the tree after the
lint pass rather than carried over from the audit that produced them, and
one claim did not survive: the audit reported **3 static import cycles**.
An AST strongly-connected-components pass over every top-level `app.*`
import finds **zero** multi-module cycles. The single self-reference is
[`app/core/concepts/proposers/__init__.py`](../../app/core/concepts/proposers/__init__.py)
line 87 importing its own submodules by absolute path, which is the
ordinary package-header form. Corrected numbers are inline below; where a
count differs from what you remember, the one here is the measured one.

---

## A1. A real facade for `web/` and `mcp/`, instead of 294 reaches into `session._*`

> **Status: partly shipped.** `app/web/` is converted and pinned at **zero**
> private reaches behind
> [`WebFacadeMixin`](../../app/core/session/web_facade_mixin.py). The
> `_force_*` debug family is gone, replaced by
> [`DebugOverrides`](../../app/core/session/debug_overrides.py).
> `app/mcp/server_tools/` is down from 569 reaches to **483** and is
> ratcheted by [`tests/test_private_reach_guard.py`](../../tests/test_private_reach_guard.py).
> What remains is bucket 2 below: typed handles for the subsystems.
>
> Two corrections to the original numbers, both from the guard rather than a
> grep. The count was **636**, not 294, once reflective reads
> (`getattr(session, "_x", …)`) were included. And the `_force_*` family was
> **43** MCP-armable flags, not 14 — plus five more that a provider consumed
> but no tool could arm, one of which named in its own docstring a tool that
> did not exist.
>
> The debug flags turned out to be the load-bearing part. Cleanup was two
> hand-written lists in `lifecycle_mixin`: a session switch cleared 11 of the
> 43 and a memory wipe cleared 14, and the two disagreed about three more.
> Anything they missed stayed armed and fired later in an unrelated
> conversation. `DebugOverrides.clear()` drops the whole dict, so there is no
> longer a list to drift; `snapshot()` answers what is armed, which nothing
> could before; and arming an unregistered name now raises instead of writing
> a dead attribute, which is what a typo used to do. `TurnRunner`'s
> `_tool_gate_force_next` is the one holdout — same shape, different owner.

**Motivation.** The private state of `SessionController` *is* the API that
the REST routes and the MCP debug tools are written against: 294 call
sites reach through a leading underscore — 243 in
[`app/mcp/server_tools/`](../../app/mcp/server_tools/) and 51 in
[`app/web/`](../../app/web/) — touching **68 distinct** private attribute
names. This is the highest-severity finding in the audit, for one reason:
there is no signal when it breaks. Rename `_memory` or reorder two lines
of `__init__` and Python raises `AttributeError` from inside a route
handler or a debug tool at runtime, on a path a unit test may never take,
because the thing being verified is usually the *core* behaviour rather
than the tool wrapping it. The 22-mixin composition makes this worse:
which mixin owns a given `_name` is not visible from the call site, so
"who depends on this?" is a repo-wide grep every time.

The **14 `_force_*` debug slots** (`_force_next`, `_force_reroll`,
`_force_slip_next`, `_force_flat_affect`, …) are a *legitimate* sub-case
and need a different answer from the rest. They exist precisely to poke
internals from the MCP debug surface; laundering them into a public API
would be worse than leaving them private, because it would advertise as
supported an interface whose whole purpose is to violate invariants for
one turn.

**Key files.**
[`app/core/session/session_controller.py`](../../app/core/session/session_controller.py)
(the 22-mixin composition and `__init__` ordering),
[`app/mcp/server_tools/`](../../app/mcp/server_tools/) (243 sites — the
bulk, and the ones with the strongest claim to internals),
[`app/web/rest/`](../../app/web/rest/) and
[`app/web/server.py`](../../app/web/server.py) (51 sites),
[`app/core/session/__init__.py`](../../app/core/session/__init__.py)
(already honest that the mixins are file boundaries, not encapsulation).

**Sketched approach.** Inventory the 68 names first — that list, not the
294 sites, is the actual unit of work — and sort each into one of three
buckets:

1. **Read-only state** that a route wants for a response payload. Becomes
   a property on `SessionController` (or a small typed snapshot object,
   which is nicer for the REST layer since it serialises directly).
2. **Subsystem handles** (`_memory`, `_rag_store`, `_task_orchestrator`).
   Becomes a single accessor — `session.services.memory` — so that
   *reaching a subsystem* is legitimate and typed, while reaching into
   `SessionController`'s own bookkeeping is not.
3. **Debug pokes** (the `_force_*` family and friends). *Shipped* as
   `session.debug_overrides`, a registry keyed by name rather than a
   namespace of attributes. Naming it as debug was the point; keying it
   by name is what bought the rest — clearing, listing, and rejecting a
   name nobody consumes are all impossible when each flag is its own
   attribute.

Convert one attribute at a time with the suite as the net. A single
sweeping rename is the one way to get this wrong.

**Open questions**, now mostly answered by the shipped half. The facade
lives *on* `SessionController` as one more mixin: a separate object would
have needed a reference back for every call, and the mixin composition was
already the file-boundary convention. `mcp/` does get a wider contract than
`web/` — a budget rather than a ban — because debug tooling has a real need
the REST API doesn't. The enforcement is a test rather than a lint rule,
which turned out better than expected: the guard also checks that every
reached name *exists*, so a rename now fails a test instead of 404-ing a
route at runtime, and it ratchets in both directions so a package can't
silently re-earn headroom when an unrelated cleanup removes a reach.

What's left is bucket 2. The 483 remaining reaches are mostly subsystem
handles (`_memory`, `_rag_store`, `_affect_store`, …), which want a single
typed accessor — `session.services.memory` — rather than 483 individual
public methods.

**Effort.** Large, but splittable per attribute — the only A-item where
partial adoption is genuinely useful.

---

## A2. Connection ownership — 15 stores work around `ChatDatabase._get_conn()`

**Motivation.** [`chat_database.py`](../../app/core/infra/chat_database.py)
owns the schema and the migration ladder for everything, but not access
to the connection it configures. Fifteen store classes call
`_get_conn()` — `memory_store`, `memory_conflict_store`, `world_store`,
`task_store`, `task_inputs`, `task_events`, `belief_store`,
`concept_store`, `concept_event_store`, `cue_decision_store`,
`topic_cluster_store`, `surfacing_outcome_store`, `relationship_axes`,
`affect_state`, `memory_consolidator` — and 12 of the 15 carry a
`# type: ignore[attr-defined]` on the call, which is the code admitting in
place that it is going around the front door.

The exposure is that every one of those callers silently depends on how
that connection is *configured*: row factory, isolation level, WAL mode,
busy timeout, thread affinity. Those are exactly the settings you change
when you have a locking or a concurrency problem — and when you do,
you have 15 call sites that were never audited against the new posture,
with the type checker suppressed at each of them.

**Key files.**
[`app/core/infra/chat_database.py`](../../app/core/infra/chat_database.py)
(`_get_conn`, the migration ladder), plus the 15 stores above — grep
`_get_conn(` for the exact list, and `type: ignore[attr-defined]` for the
subset that already flagged itself.

**Sketched approach.** Give `ChatDatabase` the public seam the stores are
already improvising: a `connect()` context manager that yields a
configured connection, and probably a `transaction()` alongside it, since
every one of these stores hand-rolls its own `conn.commit()` today. Then
convert stores one at a time — the 12 `# type: ignore[attr-defined]`
comments are a ready-made worklist, and each one deleted is a unit of
progress. No behaviour
changes, so the existing per-store tests are sufficient cover.

**Open questions.** Does the seam need explicit transaction scoping, or
is a connection handle enough for every current caller? Should table
*ownership* also move — the stores read and write their tables but
`chat_database` owns their `CREATE`/migration, which is a defensible split
(one ladder, one version number) but means no store is self-contained.
Worth deciding explicitly rather than by default.

**Effort.** Medium, and mechanical once the seam exists.

---

## A3. The 23 Python and 3 TypeScript files over the declared budget

**Motivation.** [`AGENTS.md`](../../AGENTS.md) declares ~1,500 lines for
Python and ~1,000 for React/TS, with a hard "split before ~2,500". These
are the files past it, measured after the lint pass:

| Lines | File |
| --- | --- |
| 3,854 | `app/core/infra/memory_settings.py` |
| 3,244 | `app/core/session/prompt_assembler.py` |
| 3,095 | `app/core/session/inner_life_part2.py` |
| 2,750 | `app/mcp/server_tools/memory_worker_tools.py` |
| 2,713 | `app/core/session/speaking_workers_init_mixin.py` |
| 2,656 | `app/mcp/server_tools/proactive_task_tools.py` |
| 2,539 | `app/core/session/inner_life_part1.py` |
| 2,296 | `app/core/infra/agent_settings.py` |
| 2,241 | `app/core/rag/rag_retriever.py` |
| 2,229 | `app/core/concepts/concept_synthesis_worker.py` |
| 2,085 | `app/mcp/server_tools/self_state_tools.py` |
| 1,983 | `app/core/session/task_orchestration_mixin.py` |
| 1,912 | `app/core/session/post_turn_helpers_mixin.py` |
| 1,902 | `app/core/session/post_turn_mixin.py` |
| 1,892 | `app/core/infra/chat_database.py` |
| 1,869 | `app/llm/openai_compatible_client.py` |
| 1,855 | `app/core/infra/settings.py` |
| 1,853 | `app/core/session/inner_life_part3.py` |
| 1,783 | `app/core/infra/agent_settings_parse.py` |
| 1,757 | `app/core/conversation/topic_graph.py` |
| 1,608 | `app/core/memory/memory_store.py` |
| 1,522 | `app/core/world/world_store.py` |
| 1,506 | `app/core/session/turn_runner.py` |
| 1,889 | `web/src/types.ts` |
| 1,255 | `web/src/live2d/channels/ExpressionChannel.test.ts` |
| 1,077 | `web/src/features/settings/WorldTab.tsx` |

Seven are already past the 2,500 "split now" line. The list is here
mainly so the split order stops being a matter of feel.

**These are not all the same problem**, which is why one blanket pass
would be the wrong shape:

- `memory_settings.py` (3,854), `agent_settings.py` (2,296),
  `settings.py` (1,855) and `agent_settings_parse.py` (1,783) are
  overwhelmingly dataclass fields, defaults and their parse/validate
  mirrors. They are long because the feature surface is large, and they
  are *easy* to read. Splitting them per settings block is nearly free and
  buys little beyond the number going down — do it for the parse side,
  where a bug can hide, before the declaration side.
- `prompt_assembler.py` (3,244) and the `inner_life_part*` trio are dense
  logic on the hot path, and they are where the budget is actually
  earning its keep. These are the ones worth splitting properly, via the
  existing feature-mixin convention.
- The three `mcp/server_tools/` files are long because each is a flat list
  of independent tool definitions; a split there is close to free and
  purely a navigation win.
- `ExpressionChannel.test.ts` (1,255) is a *test*, so the ~1,000-line
  component budget doesn't really apply — noted so nobody spends a day
  splitting it on the strength of appearing in this table.

**Sketched approach.** Order by `lines × churn` (`git log --format= --name-only`
over the last few hundred commits, bucketed) rather than by line count.
A 3,800-line file nobody edits costs nothing; a 1,900-line file touched
every week is where the split pays. Then split along the conventions
already in use: `app/core/<area>/*_mixin.py` for the session tree,
feature folders for `web/`.

**Open questions.** Should `web/src/types.ts` (1,889) be split per feature
— it is a single import target today, which is genuinely convenient, and
splitting it touches nearly every component. Should the settings
dataclasses be generated from a schema instead of hand-mirrored across
declaration and parse, which is the real reason those four files are as
long as they are?

**Effort.** Medium per file, fully independent, and a good first task for
someone learning the tree.

---

## A4. The two layering inversions

**Motivation.** Two edges point the wrong way. Neither is a runtime
problem today; both are the kind of thing that makes a future extraction
or test-harness change unexpectedly expensive.

**`app/core/infra/` imports upward, at module level** — so "infra" is not
the bottom layer its name claims:

- [`user_profile.py`](../../app/core/infra/user_profile.py) line 30 —
  `from app.core.session.session_text_utils import resolve_user_name, speaker_label`
- [`schedule_learner.py`](../../app/core/infra/schedule_learner.py) line 36 —
  `from app.core.proactive.idle_worker import default_is_ready`

**`app/core/` imports `app/mcp/`** at four sites, all of them deferred
into a function body or a `TYPE_CHECKING` block, which is why there is no
cycle:

- [`detectors_init_mixin.py`](../../app/core/session/detectors_init_mixin.py)
  lines 852-853 (`McpServerRunner`, `create_mcp_server`)
- [`task_orchestration_mixin.py`](../../app/core/session/task_orchestration_mixin.py)
  line 729 (`ExternalMcpManager`)
- [`tasks/workflow/mcp_skills.py`](../../app/core/tasks/workflow/mcp_skills.py)
  line 32 and
  [`tasks/handlers/mcp_tool.py`](../../app/core/tasks/handlers/mcp_tool.py)
  line 33 (both `TYPE_CHECKING`-only)

Related and larger in raw count: **721 imports sit inside function bodies**
across 83 files (397 in `app/core/session/`, 209 in
`app/mcp/server_tools/`, measured by AST rather than by grep, so
`TYPE_CHECKING` blocks are excluded). Most are deliberate — breaking a
would-be cycle, or keeping a heavy optional dependency off the startup
path — and the `mcp/server_tools/` share is lazy-by-design tool loading.
But a deferred import is also how a layering violation hides from every
static check, so the number is worth watching rather than acting on.

**Key files.** The five sites above; the two `infra` ones are the whole
of the actionable part.

**Sketched approach.** The `infra` pair is small and worth just fixing:
`resolve_user_name` / `speaker_label` are text helpers with no session
dependency and belong in a neutral module; `default_is_ready` is a
predicate that `schedule_learner` could take as an argument instead of
importing. Both are afternoon-sized.

`core → mcp` is a decision, not a refactor. Either core defines the
protocol it needs (`ToolProvider`, `ServerRunner`) and `mcp` implements
it — clean, and it costs a new indirection for a dependency that is
deferred and therefore harmless — or the direction is accepted explicitly
with a comment at each site saying so. Writing down *which* of those we
chose is the deliverable; the current state is neither.

**Open questions.** Is the protocol inversion worth an indirection for a
dependency that cannot cause a cycle in practice? Would a lint rule
(`flake8-tidy-imports`-style banned-API, which ruff supports) pin the
layering once it is decided, so the next inversion fails the lint instead
of being discovered by an audit?

**Effort.** Small for the `infra` pair; Medium for `core → mcp`, and
mostly deliberation rather than code.

---

## A5. What should actually gate a commit

**Motivation.** As of the lint adoption, `npm run lint` exists and is
green, and [`AGENTS.md`](../../AGENTS.md) requires running it — which in
this repo is real enforcement, since agents do most of the editing and
read that file every session. But it is *only* that: `.github/` holds
nothing but `copilot-instructions.md`, `.git/hooks/` holds nothing but
the stock samples, and a rule enforced by asking nicely will drift.

The specific asymmetry worth closing: `app/` now has ruff, while `web/`'s
196 `.ts`/`.tsx` files are guarded by `tsc -b` alone. There is no ESLint
config anywhere under `web/`. Type checking catches type errors; the bugs
this frontend actually produces are stale-closure and dependency-array
bugs in the Zustand subscriptions and the Pixi render loop, which are
exactly what `tsc` cannot see and what `react-hooks/exhaustive-deps` is
for.

**Key files.** [`package.json`](../../package.json) (the `lint`,
`lint:py`, `lint:py:fix`, `lint:web` scripts),
[`web/package.json`](../../web/package.json) (`typecheck`),
[`pyproject.toml`](../../pyproject.toml) (rule selection, and the comment
block arguing what was left out), `.github/` (empty of workflows).

**Sketched approach**, cheapest first:

1. **A pre-commit hook running ruff on staged Python only.** Sub-second,
   catches the regression at the moment it is introduced, and needs no
   infrastructure. Highest value per minute spent of anything in this
   file.
2. **A GitHub Actions workflow** running `npm run lint` then `pytest -q`.
   The suite is ~7 minutes, which is fine for CI and far too slow for a
   hook — that split is the whole reason to have both.
3. **ESLint in `web/`, narrowly.** `react-hooks` (especially
   `exhaustive-deps`) plus `@typescript-eslint` on the rules that find
   bugs rather than style. Adopting `recommended` wholesale would produce
   the same shape of mechanical diff the ruff pass just absorbed, so pick
   the rules deliberately and expand later.
4. **The ruff rule sets deliberately deferred** during adoption, each as
   its own commit against a green baseline: `I` (isort), `UP`
   (pyupgrade), `SIM` (flake8-simplify). `ruff format` stays off — this
   codebase is full of hand-wrapped prose comments and the formatter
   rewraps them, which would destroy exactly the commentary that makes it
   readable, in a diff too large to review.

**Open questions.** Are hooks even meaningful when agents do the editing?
A hook is bypassable with `--no-verify` and an agent that hits one may
simply pass the flag, so the honest posture is probably hook *and*
`AGENTS.md` instruction, treating the hook as a catch for human slips
rather than as a boundary. And should CI gate merges to `main` or only
report, given this is effectively a single-developer repo where a red
`main` blocks nobody but is still worth knowing about?

**Effort.** Small each, and independent — (1) is worth doing on its own
even if nothing else here happens.
