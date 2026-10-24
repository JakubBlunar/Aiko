# Architecture and code quality

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

<a id="a1-a-real-facade-for-web-and-mcp-instead-of-294-reaches-into-session_"></a>

## A1. Typed public access for remaining MCP consumers

**Status: Partial.**

**Implemented / available:** WebFacadeMixin, registered DebugOverrides and the private-reach guard exist.

**Remaining:** Migrate remaining MCP subsystem reads to typed public handles and ratchet the actual
guard budget; the historical reach counts are stale.

[Implemented slice](shipped/audit-2026-10-24.md#architecture-a1).

**Source check:** [web_facade_mixin.py](../../app/core/session/web_facade_mixin.py),
[debug_overrides.py](../../app/core/session/debug_overrides.py),
[test_private_reach_guard.py](../../tests/test_private_reach_guard.py),
[session_controller.py](../../app/core/session/session_controller.py),
[server.py](../../app/web/server.py), [__init__.py](../../app/core/session/__init__.py).

<a id="a2-connection-ownership--15-stores-work-around-chatdatabase_get_conn"></a>

## A2. Public database connection and transaction ownership

**Status: Open implementation.**

**Implemented / available:** ChatDatabase owns connection configuration and migrations.

**Remaining:** Define a public connection/transaction contract and migrate private _get_conn consumers.

**Source check:** [chat_database.py](../../app/core/infra/chat_database.py),
[memory_store.py](../../app/core/memory/memory_store.py).

## A3. The files over the declared budget — and the fact that they grow

**Status: Partial.**

**Implemented / available:** Feature mixins and folders split some large owners.

**Remaining:** Continue splitting oversized owners when touched, especially prompt assembly,
inner-life providers, concept synthesis and settings. Recount current files rather than reusing the
historical inventory.

[Implemented slice](shipped/audit-2026-10-24.md#architecture-a3).

**Source check:** [prompt_assembler.py](../../app/core/session/prompt_assembler.py),
[concept_synthesis_worker.py](../../app/core/concepts/concept_synthesis_worker.py).

## A4. The two layering inversions

**Status: Open implementation.**

**Implemented / available:** Deferred/TYPE_CHECKING imports avoid immediate cycles.

**Remaining:** Resolve upward infra text/scheduling imports and decide the intended core-to-MCP
protocol boundary before refactoring.

**Source check:** [user_profile.py](../../app/core/infra/user_profile.py),
[schedule_learner.py](../../app/core/infra/schedule_learner.py),
[detectors_init_mixin.py](../../app/core/session/detectors_init_mixin.py),
[task_orchestration_mixin.py](../../app/core/session/task_orchestration_mixin.py),
[mcp_skills.py](../../app/core/tasks/workflow/mcp_skills.py),
[mcp_tool.py](../../app/core/tasks/handlers/mcp_tool.py).

## A5. What should actually gate a commit

**Status: Partial.**

**Implemented / available:** npm run lint runs Ruff and web TypeScript checks; managed
focused/affected-test runners exist.

**Remaining:** Decide and implement hooks/CI and narrow React hook linting. Further Ruff rules
remain optional. AGENTS.md supersedes the old full-suite-after-every-change proposal.

[Implemented slice](shipped/audit-2026-10-24.md#architecture-a5).

**Source check:** [package.json](../../package.json), [pyproject.toml](../../pyproject.toml),
[affected_tests.py](../../scripts/affected_tests.py), [AGENTS.md](../../AGENTS.md),
[package.json](../../web/package.json).

<a id="a6-107-render-methods-one-hand-copied-preamble"></a>

## A6. Shared provider gate and error frames

**Status: Partial.**

**Implemented / available:** Shared cue admission, debug overrides and prompt helpers remove some
repetition.

**Remaining:** Standardize remaining provider gate/error frames while preserving distinct decline
reasons, staging, protected blocks and prompt tiers.

[Implemented slice](shipped/audit-2026-10-24.md#architecture-a6).

**Source check:** [inner_life_part1.py](../../app/core/session/inner_life_part1.py),
[inner_life_part4.py](../../app/core/session/inner_life_part4.py),
[prompt_support.py](../../app/core/session/prompt_support.py),
[idle_workers_init_mixin.py](../../app/core/session/idle_workers_init_mixin.py),
[speaking_workers_init_mixin.py](../../app/core/session/speaking_workers_init_mixin.py).

<a id="a7-six-settings-that-parse-and-then-do-nothing"></a>

## A7. General settings-use regression guard

**Status: Partial.**

**Implemented / available:** The six filed settings were wired, deleted or explicitly reserved;
feature-specific regressions exist.

**Remaining:** Add a general settings-use guard with explicit reserved/compatibility exemptions. Do
not reopen the six resolved wiring bugs.

[Implemented slice](shipped/audit-2026-10-24.md#architecture-a7).

**Source check:** [agent_settings.py](../../app/core/infra/agent_settings.py),
[agent_settings_parse.py](../../app/core/infra/agent_settings_parse.py),
[engagement_tracker.py](../../app/core/affect/engagement_tracker.py),
[cue_render.py](../../app/core/tasks/cue_render.py).
