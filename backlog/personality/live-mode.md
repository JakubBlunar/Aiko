# Live mode: remaining runtime work

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

Baseline passes and architecture are in [the historical implementation
record](shipped/live-mode.md). Live L19-L31 (which are different from concept L numbers) remain in
[initiative](live-initiative.md). Shared replay is DT4; no additional clock or speech owner is
proposed.

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

**Status: Open implementation.**

**Implemented / available:** Attend/yield/wait/backchannel and presence clamps exist.

**Remaining:** Add listen_posture as a bounded mapping to existing intents; typing cannot speak and
sleep/DND/floor ownership still win.

**Source check:** [presence.py](../../app/core/live/presence.py).

## L11. Circadian quieting from a period token

**Status: Open implementation.**

**Implemented / available:** Circadian time and quieter-only vitality/posture controls exist.

**Remaining:** Add deterministic period-token policy quieting with timephrase; night cannot increase
intensity or speech authority.

**Source check:** [policy_context.py](../../app/core/live/policy_context.py).

## L13. Glance targets from an allowlisted present-set

**Status: Open implementation.**

**Implemented / available:** Attention-target vocabulary and hysteresis exist.

**Remaining:** Construct/validate an allowlisted menu from present frame entities; absent/unknown
targets drop and private titles never enter policy context.

**Source check:** [assembler.py](../../app/core/live/assembler.py).

## L14. Return and reconnect companion beat

**Status: Open implementation.**

**Implemented / available:** Unlock/reconnect and caught-mid-activity evidence exist.

**Remaining:** Connect one bounded return beat with source/generation/dedupe checks and existing
speech acts; no automatic greeting or second speech loop.

**Source check:** [activity_notices.py](../../app/core/live/activity_notices.py).

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
