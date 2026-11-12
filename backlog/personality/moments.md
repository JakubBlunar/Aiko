# Shared moments and boundaries

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

## J1. Multi-user moments / participant attribution

**Status: Open implementation.**

**Implemented / available:** Shared moments and Together exist.

**Remaining:** Add participant attribution, scoped reads/UI and migration for implicitly attributed
historical moments.

**Source check:** [shared_moments.py](../../app/core/relationship/shared_moments.py),
[server.py](../../app/web/server.py).

## J2. Exportable timeline

**Status: Open implementation.**

**Implemented / available:** Together displays stored moments.

**Remaining:** Implement a source-faithful Markdown/PDF export endpoint and UI action, preserving
participant scope and deletion behavior.

**Source check:** [server.py](../../app/web/server.py).

## J3. Axes-aware proactive nudges

**Status: Open implementation.**

**Implemented / available:** Axes guide expression and several gates.

**Remaining:** Wire axes into proactive topic selection when another reason already permits a nudge;
axis movement must not independently trigger speech.

**Source check:** [proactive_director.py](../../app/core/proactive/proactive_director.py),
[relationship_axes.py](../../app/core/relationship/relationship_axes.py).

<a id="j7-moment-detection-tuning--giftpromise-ordering-bug"></a>

## J7. Moment signal expansion and detector statistics

**Status: Partial.**

**Implemented / available:** Gift/promise flags are snapshotted before clearing and explicitly
passed to the scheduler; the ordering bug has regression coverage.

**Remaining:** Evaluate first-time and resolved-reaction signals and expose consolidated detector
statistics. Current scheduling still derives reaction signals from raw tags; broaden without
fabricating milestones.

[Implemented slice](shipped/audit-2026-10-24.md#moments-j7).

**Source check:** [test_moment_schedule_signals.py](../../tests/test_moment_schedule_signals.py),
[post_turn_mixin.py](../../app/core/session/post_turn_mixin.py),
[speaking_window_jobs_mixin.py](../../app/core/session/speaking_window_jobs_mixin.py).

<a id="j12-intimacy-pacing--boundary-calibration"></a>

## J12. Explicit relationship preferences and boundary propagation

**Status: Partial.**

**Implemented / available:** Original consent ceiling, learned user-pace EMA, prompt cue,
disclosure/reciprocity gates, settings persistence path and Avatar dial are implemented.

**Remaining:** Implement the 23 Oct extension: independent friend/flirty/romantic and affection-kind
preferences, same-turn de-escalation, queued gesture/cue invalidation across clients, restart and
paired checks. B7 removed touch dispatch gates, so the dial does not prove a universal hard touch
cap.

[Implemented slice](shipped/audit-2026-10-24.md#moments-j12).

**Source check:** [intimacy_pacing.py](../../app/core/relationship/intimacy_pacing.py),
[test_intimacy_pacing.py](../../tests/test_intimacy_pacing.py),
[AvatarTab.tsx](../../web/src/features/settings/AvatarTab.tsx),
[sessions_settings_routes.py](../../app/web/rest/sessions_settings_routes.py),
[relationship_axes.py](../../app/core/relationship/relationship_axes.py).

<details>
<summary>Retained acceptance constraints for further work</summary>

**Acceptance.** A warm friendship never auto-upgrades to romance from engagement
alone. An invited romantic register still answers practical questions directly.
Pausing flirtation removes pet names, relevant touches and queued romantic
beats across clients without erasing ordinary shared history. Restart preserves
explicit preferences. Declining affection does not lower relationship axes or
create sulking/jealousy cues. Evaluate behavior with K102/K104; do not optimize
for more affection or longer sessions.

</details>

<a id="j13-pet-name-reciprocity--evolution"></a>

## J13. Learned nickname adoption and reciprocity

**Status: Partial.**

**Implemented / available:** Address-style guidance already changes with relationship phase; it is
not static as originally claimed.

**Remaining:** Persist explicitly adopted names for Aiko, handle changes/reciprocity and let unused
names fade within J12 boundaries. Phase-based permission is not learned nickname adoption.

[Implemented slice](shipped/audit-2026-10-24.md#moments-j13).

**Source check:** [relationship.py](../../app/core/relationship/relationship.py),
[test_relationship_petname.py](../../tests/test_relationship_petname.py),
[user_profile.py](../../app/core/infra/user_profile.py).
