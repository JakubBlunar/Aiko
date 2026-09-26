# Cognitive continuity implementation

## Live L18. Cue purpose and exact handoff

Shipped 27 Sep 2026. Live projection assigns server-owned purpose to known
share/continuation sources; remaining registered cues conservatively remain
asks and unknown types cannot wake speech. A neutral `share_observation`
urge preserves non-question content through the question gate. The numbered
menu carries purpose, not cue bodies or titles, and the model cannot change
it through proposal arguments. Parking, merging, and expiry preserve it.

Main-turn rendering resolves the selected ID through `CueStore.available`
and `take_pool_cue(cue_id=...)`; it cannot substitute a sibling, spend a
consumed/expired cue, or bypass type cadence. Dispatch rechecks speech
permission, quiet/DND, and source availability. No speech budget increased.

Owners: [cue_adapter.py](../../../app/core/live/cue_adapter.py),
[main_wake.py](../../../app/core/live/main_wake.py), and
[live_mode_mixin.py](../../../app/core/session/live_mode_mixin.py).
Regression coverage: [test_live_mode_pass27.py](../../../tests/test_live_mode_pass27.py)
and [test_cue_pool_consumption.py](../../../tests/test_cue_pool_consumption.py).
Human evaluation of grounded contributions versus interruption remains T5;
this is a tested delivery contract, not evidence of greater naturalness.