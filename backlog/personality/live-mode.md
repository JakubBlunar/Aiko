# Live mode: remaining runtime work

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

Baseline passes and architecture are in [the historical implementation
record](shipped/live-mode.md). Live L19-L31 (which are different from concept L numbers) remain in
[initiative](live-initiative.md). Shared replay is DT4; no additional clock or speech owner is
proposed.

## Scenario regression harness

Scripted and loopback Ollama scenarios now exercise Live impulses, virtual
time, policy choices, cadence, topic handoff and cue feedback in isolation.
Use the fixtures to pin floor ownership, sleep, waits and learned-context
behavior before changing speech policy. [Scenario runner](../../docs/live-scenarios.md)

The [21 November local replay](../reviews/live-policy-replay-2026-11-21.md)
records the first repeated samples for quiet cadence, competing topics,
requested updates and voice interruption.

## L4. Authored ambient motion presets

**Status: Deferred / evidence or capability gate.**

**Implemented / available:** IdleLife semantic envelopes exist; motion_class stays none.

**Remaining:** Wait for a real authored idle-life motion group before a preset menu. This is a rig
capability limit, not policy prompt work.

[Implemented slice](shipped/audit-2026-10-24.md#live-mode-l4).

**Source check:** [IdleLifeChannel.ts](../../web/src/live2d/channels/IdleLifeChannel.ts),
[resolver.ts](../../web/src/live2d/behavior/resolver.ts).

## L5. Bounded micro and earcon delivery

**Status: Open implementation.**

**Implemented / available:** Validated micro-utterances and audible backchannels exist.

**Remaining:** Add validated delivery_style/backchannel_delivery enums mapped through shared
TTS/earcon controls, rejecting invalid text before synthesis.

**Source check:** [micro_utterance.py](../../app/core/live/micro_utterance.py).

## L9. Floor manners while the user owns the floor

**Status: Visual posture implemented.**

**Implemented / available:** The semantic plan gives space while the user
composes, leans in during speech, and settles at utterance end or when
yielding. The avatar uses its existing gaze and body channels. The replay
covers resumed speech, quiet, sleep and interruption during playback.

**Remaining:** Evaluate visual feel on the live rig. Audible backchannel
choices remain with L5.

[Listening posture replay](../../tests/scenarios/live/listening-posture.json)

## L11. Circadian quieting from a period token

**Status: Implemented in the Live frame and policy.**

**Implemented / available:** The frame carries the existing local circadian
period. Night and late night cap reaction intensity, soften playful
presence, and slow the idle breath. The time change adds no policy epoch.
Requested updates still use the normal floor and speech checks.

**Remaining:** Validate visual feel on the live rig.

[Circadian replay](../../tests/scenarios/live/circadian-quieting.json)

## L13. Glance targets from an allowlisted present-set

**Status: Implemented in the policy and avatar.**

**Implemented / available:** Live builds the target menu from the current frame using
semantic labels. An accepted look toward the present user has a 1.2-second policy
hold over the current shared or world activity. The policy, avatar target, and gaze
agree on the selected target. Typing, speech and sleep retain their posture rules.

**Remaining:** Evaluate visual timing on the live rig and sample natural
shared moments for when the local policy chooses to look over.

[Grounded-glance replay](../../tests/scenarios/live/grounded-glances.json)

## L14. Return and reconnect companion beat

**Status: Desktop visual return implemented.**

**Implemented / available:** A fresh foreground observation after desktop
idle or lock can trigger one brief return glance. The normal gaze rules
resume after 900 ms or when a higher-priority state takes over. Verified in
the replay and avatar channel tests.

**Remaining:** Validate visual timing on the live rig and evaluate whether
browser reconnect or shared-activity resume offers a comparable signal.

[Return glance replay](../../tests/scenarios/live/return-glance.json)

## L15. Compact policy prompt-v2 (keep the 4B reliable)

**Status: Partial.**

**Implemented / available:** Prompt v2 distinguishes fresh user input from aged quiet openings and
includes candidate/source/evaluation timing.

**Remaining:** Measure a compact actual-route prompt at equal decision quality, preserving
schema/rails/menu. Temporal context is implemented; smaller sufficient input is not validated.

[Implemented slice](shipped/audit-2026-10-24.md#live-mode-l15).

**Source check:** [prompt.py](../../app/core/live/prompt.py).

## Moved out of the active queue

- **L0** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l0).
- **L1** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l1).
- **L2** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l2).
- **L3** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l3).
- **L6** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l6).
- **L7** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l7).
- **L8** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l8).
- **L10** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l10).
- **L12** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l12).
- **L16** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l16).
- **L17** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l17).
- **L18** — shipped: [record](shipped/audit-2026-10-24.md#live-mode-l18).
