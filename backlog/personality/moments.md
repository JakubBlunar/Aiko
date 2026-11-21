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

## J14. Remember what changed after a misunderstanding

**Status: Cross-session venting pilot.** Suppose Aiko offers advice while the user is venting. The
user explains that they wanted company, and Aiko offers to listen first next
time. An apology, the user accepting that offer, and a later report that it
helped answer three different questions. Keeping those distinctions lets a
relationship develop through what the two actually work out together.

[K103](shipped/social-understanding.md#k103-specific-conversational-repair)
addresses the immediate misunderstanding. The
[J6 repair detector](../../app/core/relationship/conflict_repair.py) can record
a shared moment from a recovery in valence. On 21 Nov 2026 the installed graph
had 34 active relationship concepts, including 28 tensions. That count describes
stored interpretations. It gives no measure of actual conflict or resolution.

Pilot a later read of the original issue, Aiko's response, what the user
endorsed and any subsequent account of how it went. Reuse K100's need/response
capture, the existing correction links and L50 scope. An explicit request to
listen can guide the next relevant reply immediately. Joint agreement and
successful repair each require their own evidence. A mood rebound may identify
an exchange to inspect. It cannot establish either conclusion. An edited source
or a later change of preference should narrow the current guidance through L52.

**First check.** Start the next session with the same vent after three different
endings. In one the user endorsed listening first, in another the conversation
merely became cheerful, and in the third the user later clarified “for work
things I actually want ideas.” Score whether
Aiko acts on the applicable preference, can accurately say what they agreed if
asked, and can leave the old disagreement unmentioned. A fresh direct request for
advice should still get advice. Keep prevalence of tension labels out of scoring.

The [isolated replay](../../tests/test_support_preferences.py) exercises the
normal response-mode prompt. The accepted offer guides the next vent and remains
available when asked about it. The cheerful ending yields no agreement. The
work exception prompts a practical idea and leaves the general listening
preference available for personal vents. A direct request wins, and editing or
deleting a source changes the result on the next read. The pilot reads the
original retained transcript and recognizes a small set of explicit forms.
Broader repair kinds, paraphrase coverage and ordinary-use validation remain
open.

Eight fictional turns on the local 9B worker exercised the assembled prompts.
It recalled the explicit agreement, could not verify one after the cheerful
ending, and distinguished an unconfirmed outcome from a user report that
listening helped. It offered ideas after the work exception and the direct
request. The work reply still added an unstated time, and the outcome reply
inferred why it helped. The replay used a fictional persona and separate
databases. Main-chat validation remains open.
