# Avatar and expressiveness

Reviewed 24 Oct 2026 against checkout 8c666cf. Only unfinished implementation, explicit validation
and deferred decisions are listed below. Existing tests were inspected, not rerun; source inspection
is not live/model/device verification. Completed slices and superseded proposals are in [the shipped
audit](shipped/audit-2026-10-24.md).

## B3. Blink-rate modulation by arousal (deferred follow-up to B1)

**Status: Deferred / evidence or capability gate.**

**Implemented / available:** Authored blinking and wink behavior exist.

**Remaining:** Own or extend a safe blink interval setter, then modulate arousal without fighting
wink gestures; no implemented arousal-controlled driver was found.

[Implemented slice](shipped/audit-2026-10-24.md#avatar-b3).

**Source check:** [AvatarEngine.ts](../../web/src/live2d/AvatarEngine.ts).

## Moved out of the active queue

- **B7** — shipped: [record](shipped/audit-2026-10-24.md#avatar-b7).
