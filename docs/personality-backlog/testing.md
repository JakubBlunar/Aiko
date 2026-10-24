# Testing and evaluation

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

## T1. Shared `FakeChatClient` + `BehaviorHarness`

**Status: Open implementation.**

**Implemented / available:** Many feature-specific fakes/fixtures and managed temporary databases
exist; the suite now has shared conftest isolation.

**Remaining:** Build a reusable ChatClient-protocol fake and minimal real-collaborator
BehaviorHarness. Do not assume no shared fixtures or no integration tests exist.

**Source check:** [conftest.py](../../tests/conftest.py), [chat_client.py](../../app/llm/chat_client.py).

## T2. End-to-end behavioural chain tests

**Status: Partial.**

**Implemented / available:** Targeted wiring/chain regressions cover several features, including
staged cue admission, correction signals and delivery.

**Remaining:** Add reusable worker -> cue -> final prompt -> parsed reply -> post-turn state chains
through T1; existing local tests do not cover every behavioral chain.

[Implemented slice](shipped/audit-2026-10-24.md#testing-t2).

**Source check:** [test_moment_schedule_signals.py](../../tests/test_moment_schedule_signals.py),
[test_prompt_assembler.py](../../tests/test_prompt_assembler.py),
[response_text_service.py](../../app/core/services/response_text_service.py),
[post_turn_mixin.py](../../app/core/session/post_turn_mixin.py).

## T3. Worker registry conformance + smoke + fault injection

**Status: Partial.**

**Implemented / available:** Scheduler and individual workers have smoke/failure regressions.

**Remaining:** Add registry-wide uniqueness/readiness purity, seeded/empty-store repeat runs and LLM
fault injection without booting live install state.

[Implemented slice](shipped/audit-2026-10-24.md#testing-t3).

**Source check:** [idle_worker_scheduler.py](../../app/core/proactive/idle_worker_scheduler.py),
[test_idle_worker_scheduler.py](../../tests/test_idle_worker_scheduler.py).

## T4. Prompt-build + tag-parse contract (golden) tests

**Status: Partial.**

**Implemented / available:** Prompt-order/prefix, handling, tag parsing and stream tests exist.

**Remaining:** Build a deliberate compact cross-feature golden contract layer for both assembled
prompt and parsed output; avoid duplicating implementation-only unit tests.

[Implemented slice](shipped/audit-2026-10-24.md#testing-t4).

**Source check:** [test_prompt_assembler.py](../../tests/test_prompt_assembler.py),
[test_response_text_service.py](../../tests/test_response_text_service.py).

## T5. LLM behavioural eval suite (separate track, not a CI gate)

**Status: Partial.**

**Implemented / available:** K10, L55 and social-reasoning corpora/runners exist; K102/K103 have a
small configured-model baseline, and K104/K105 have deterministic slices.

**Remaining:** Complete repeated held-out/blinded multi-turn and listening comparisons listed below,
with equal budgets, source correctness, silence/decline/failure controls and no live relationship
mutation.

[Implemented slice](shipped/audit-2026-10-24.md#testing-t5).

**Source check:** [evaluate_social_reasoning.py](../../scripts/evaluate_social_reasoning.py),
[social_reasoning_t5.json](../../tests/fixtures/social_reasoning_t5.json),
[reasoning_l55.json](../../tests/fixtures/reasoning_l55.json).

## T6. Determinism seams for flake-free chain tests

**Status: Partial.**

**Implemented / available:** timephrase/DT1 clock injection and engagement-clock tests exist.

**Remaining:** Standardize seeded RNG injection across remaining workers and advance-clock/scheduler
chain tests; keep narrative clock separate from real runtime timing.

[Implemented slice](shipped/audit-2026-10-24.md#testing-t6).

**Source check:** [timephrase.py](../../app/core/infra/timephrase.py),
[test_debug_clock.py](../../tests/test_debug_clock.py),
[test_engagement_clock.py](../../tests/test_engagement_clock.py),
[builtins.py](../../app/llm/tools/builtins.py).

## Named behavioral comparisons still open

- **K97:** referents, corrected premises, interruptions and topic changes versus history/situation
  baseline at equal context budgets.
- **K98:** answer-earned semantic progression versus repeated asks, with quiet and subject-change controls.
- **K99:** never-presented versus interrupted/played/acknowledged content, stale/duplicate
  acknowledgements, reconnect and hardware listening tests.
- **K102:** repeated held-out/blinded indirect meaning, mixed serious/playful spans, unsupported
  sarcasm, quotations, pretend exits, boundaries, languages and long-history displacement.
- **K103:** wrong-referent recurrence, excess apologies/clarifications/callbacks versus K17; real
  interrupted-audio listening separately from text probes.
- **K104/K105, J12/L36:** cause-specific mixed expression, semantic delivery/listening, explicit
  relationship boundaries and outcome-grounded transfer.
- **K78:** coarse vocal-tone evidence against consented/synthetic audio and actual listening,
  keeping uncertainty and avoiding emotion/intent certainty.
- **Live L17/L18 and K12/K62/K77/K83/K84/K96/K100/K101/K107:** matched route-level continuity,
  silence, consent, source correctness, cancellation and provider-migration controls; preserve each
  active item's narrower acceptance gate.

<a id="27-sep-2026-multi-turn-naturalness-track"></a>
<a id="23-oct-2026-social-reasoning-and-expression-track"></a>
<a id="23-oct-2026-provider-migration-and-learning-provenance"></a>

These are pending comparisons. Earlier small synthetic runs and historical pass totals are not
general naturalness, real audio or current-deployment proof.
