# Proactive and presence

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

## C3. Persisting last-fired typed cooldown to disk

**Status: Open implementation.**

**Implemented / available:** Typed proactive cooldown uses process-local _last_typed_run_monotonic.

**Remaining:** Persist a wall-clock last-fired stamp and safely restore remaining cooldown after
restart; keep runtime interval math monotonic.

**Source check:** [proactive_director.py](../../app/core/proactive/proactive_director.py),
[session_controller.py](../../app/core/session/session_controller.py).

## C6. Companion mode — the desktop as a sensory channel

**Status: Partial.**

**Implemented / available:** Phases 1-5 implement collection, redacted storage, Level-1 aggregation,
Level-2 interpretation and pooled companion_activity cues. Live projects an observational share;
C7-C9 are implemented.

**Remaining:** Retain memories only after corroborated repeating patterns with a retention/redaction
contract. UIA remains deferred pending process/performance/privacy design. Later grounded impulses
and route-aware disclosure belong with Live L25/L27/L29.

[Implemented slice](shipped/audit-2026-10-24.md#proactive-c6).

**Source check:** [aggregation_worker.py](../../app/core/activity/aggregation_worker.py),
[interpretation_worker.py](../../app/core/activity/interpretation_worker.py),
[companion_cue_worker.py](../../app/core/activity/companion_cue_worker.py),
[test_activity_pipeline.py](../../tests/test_activity_pipeline.py).

## Moved out of the active queue

- **C2** — shipped: [record](shipped/audit-2026-10-24.md#proactive-c2).
- **C4** — shipped: [record](shipped/audit-2026-10-24.md#proactive-c4).
- **C7** — shipped: [record](shipped/audit-2026-10-24.md#proactive-c7).
- **C8** — shipped: [record](shipped/audit-2026-10-24.md#proactive-c8).
- **C9** — shipped: [record](shipped/audit-2026-10-24.md#proactive-c9).

<a id="live-pilot-and-later-impulses"></a>

C6 above owns repeated-pattern memory/UIA; Live L25/L27/L29 own later impulses and route-aware disclosure.
