# Shipped — Avatar & expressiveness (B-series)

Part of the [shipped log index](../shipped.md). One paragraph per entry; full detail lives in the linked implementation files.

---

## B1 / B2. Continuous expressiveness + listening micro-nods

`AmbientBodyChannel` drives `ParamBreath` from arousal and
`ParamBodyAngleY` from valence; `ExpressionChannel.tickPreModel`
does arousal-scaled overrides on the parameters declared in each
expression file. A new `avatar.expressiveness` slider (0.0-1.5)
scales the lot. Backchannel micro-nods (`_emit_backchannel_motion`)
map `agreement` / `disagreement` / `thinking` / `confused` onto
rate-limited idle-priority motions. See
[`docs/alexia-model-notes.md`](../../alexia-model-notes.md) §3 and
[`AGENTS.md`](../../../AGENTS.md).

---

## B4. Alexia visual-identity audit

The visual identity audit landed for the full Alexia rig — every
expression has a confirmed identity now. `lzx` (cheerful / amused),
`k` (sad / melancholy / concerned, plus `cry` fallback), `sq`
(angry / frustrated), `wh` (surprised / curious), `xxy` (excited /
enthusiastic), `lh` (warm / tender / gentle), `y` (the new
`confused` reaction — NOT `tired`, which now routes to body-slump
via `AmbientBodyChannel`), `zs1` (playful in day clothes;
falls through to amused / `lzx` in pajamas via the outfit gate).
Accessory tier (`bbt`, `dyj`, `mj`, `yjys1`, `yjys2`) is now reachable
via `[[overlay:X]]` + Phase 4 persistent toggles. Outfit envelopes
(`yf` / `yfmz` + the synthetic `day_clothes` baseline) drive
`OutfitChannel`. See [`docs/Alexia-my-observation.md`](../../Alexia-my-observation.md)
and [`docs/alexia-model-notes.md`](../../alexia-model-notes.md) §3 / §3a /
§3b / §3c.

**Phase 5 close-out.** The remaining vocabulary work landed:
`embarrassed` (→ `lh`, the shy / inward-tilted smile), `nervous`
(intentionally unmapped — falls through to the `concerned` →
`serious` → `thoughtful` neighbour chain so we don't fire the
`yfmz` pajamas envelope as a side effect; persona stacks
`[[reaction:nervous+sweat]]` for the visible Param44 sweat drop),
and `defiant` (→ `mj`, the head-sunglasses-on-hair tilt that reads
as cocky / "whatever" without disturbing outfit state). The
TypeScript `_REACTION_NEIGHBOURS` mirror in
[`web/src/live2d/channels/ExpressionChannel.ts`](../../../web/src/live2d/channels/ExpressionChannel.ts)
is now in lock-step with Python (covers all 27 reactions), with a
Vitest parity test that fails if a Python `REACTIONS` key gains a
neighbour chain on one side without the other. Persona idiom
`[[reaction:defiant+pout]]` (a silent no-op; no `pout` capability
exists) was replaced with `[[reaction:defiant+question]]`, which
routes to the `wh.exp3` question-mark pulse.

**Tail wag + ear wiggle on physics-driven rigs.** `[[overlay:tail_wag]]`
on Alexia was visibly broken because `Param_Angle_Rotation_*_ArtMesh202`
is a *physics output* of `ParamBreath` (`PhysicsSetting16`), and the
existing `tickTier3` direct-sine boost was overwritten by
`physics.evaluate()` every frame. Fix: in
[`AmbientBodyChannel.ts`](../../../web/src/live2d/channels/AmbientBodyChannel.ts)
`tickPreModel` (which runs *after* physics), boost `ParamBreath`
freq by 2.5x and amplitude by 1.5x while
`engineState.tailWagBoostUntil` is in the future and
`has_tail_wag` is on. Physics propagates the faster wave naturally
into the five tail segments. The `tickTier3` direct-sine boost
stays as a non-physics fallback for the Mini fixture and any
future minimal rigs. Overlay duration bumped from 1500 ms to 2000
ms (via a new `_OVERLAY_DURATION_OVERRIDES_MS` table in
[`avatar_mixin.py`](../../../app/core/session/avatar_mixin.py)) to
match the persona prompt's "~2 s burst" copy. The ear wiggle had
two compounding bugs: (a) Alexia's ear params are named `Hair 5`
/ `Hair 5-1` / `Hair 5-2` / `Hair 5-3` (Param13 / 14 / 15 / 18)
after the cdi3 translation pass, none of which match the
`_EAR_SEGMENT_SYNONYMS` list, so synonym detection set
`has_ear_wiggle=false`; (b) those four params are physics outputs
of `ParamEyeR/LOpen` (`PhysicsSetting13` / `14` — ears flick on
every blink), so even with detection working, `tickTier3` writes
would be clobbered. Fixed by adding an optional per-rig
`avatar_overrides.json` lookup in
[`avatar_profile.py`](../../../app/core/persona/avatar_profile.py) (supported
keys this pass: `cat_ear_param_ids`, `cat_tail_param_ids`),
shipping the Alexia override
([`data/personas/active/Alexia/avatar_overrides.json`](../../../data/personas/active/Alexia/avatar_overrides.json))
that pins the four `Hair 5*` IDs, and adding a `tickPreModel`
ear-wiggle write path in
[`GestureChannel.ts`](../../../web/src/live2d/channels/GestureChannel.ts)
that mirrors the `tickTier3` 4 Hz / amp 15 sine but lands after
physics. Slot lifecycle (rest-snap-then-null on expiry) now lives
exclusively in `tickPreModel` so the post-physics rest-write is
the last write of the expiry frame.

---

## B5. Auto-cascade safety — voice mode / backchannel must not pick "heavy" expressions

A perfectly cheerful turn was visibly rendering Alexia crying for a
2-4 s thinking window while she resolved tool calls (`recall` then
`change_posture`). Root cause was the auto-cascade chain inside
[`ExpressionChannel.ts`](../../../web/src/live2d/channels/ExpressionChannel.ts):
`_MODE_TO_REACTION.thinking` cascaded `thoughtful` (empty on Alexia)
into `concerned` -> `k` (Param59 = tear streaks). Same shape on
`_BACKCHANNEL_TO_REACTION.concern` and `.disagreement`. Fix routes
auto-cascades to soft / neutral alternatives only; the explicit
`[[reaction:concerned]]` from the LLM still resolves to the rig's
mapping (intentional empathy beat). A second trace surfaced a
follow-up path through the explicit `[[reaction:X]]` neighbour
fallback in [`reactions.py`](../../../app/core/affect/reactions.py) /
[`ExpressionChannel.ts`](../../../web/src/live2d/channels/ExpressionChannel.ts)
`_REACTION_NEIGHBOURS`: non-sad reactions (`thoughtful`, `serious`,
`frustrated`, `angry`) chained through `concerned` as fallback. Fix
dropped `concerned` (and any other sad-family entry) from non-sad
chains; the sad family still chains within itself so legitimate
`[[reaction:sad]]` emits paint the right tears. Lock-in:
[`tests/test_reactions.py`](../../../tests/test_reactions.py)
`CryCascadeGuardTests` plus the existing
"auto-cascade avoids heavy expressions" block in
[`ExpressionChannel.test.ts`](../../../web/src/live2d/channels/ExpressionChannel.test.ts).

**Design rule going forward.** When adding entries to
`_MODE_TO_REACTION` or `_BACKCHANNEL_TO_REACTION`, every candidate
must read as a *micro-expression* on any rig. Reactions that imply
strong narrative emotion on at least one supported rig
(`concerned`, `sad`, `melancholy`, `cry`, `angry`, `frustrated`,
`defiant`) belong only in the *explicit* `[[reaction:X]]` path,
never the auto-cascade fallback.

---

## B6. UI debug logging bridge

The cry-cascade investigation (B5) needed a single timeline that
showed *both* what the backend emitted (mood / reaction tag /
filler / tool dispatch / voice mode) and what the renderer actually
did with it (which reaction the channel picked, which `.exp3.json`
it landed on, when overlays expired, when the WS reconnected).
Previously only the backend half existed in
[`data/app.log`](../../../data/app.log); the UI half lived in DevTools
and didn't survive a tab refresh. Sharing one file when reporting a
bug now reconstructs the whole flow.

`logging.ui_log_enabled` (added to
[`LoggingSettings`](../../../app/core/infra/settings.py)) gates the feature;
off by default. The "Debug logging" block in
**Settings drawer -> Chat -> Diagnostics** flips it via
`PATCH /api/settings`, the server broadcasts
`logging_settings_changed` over the WS, and every tab's
`debugLog.setEnabled` mirrors the new value. When enabled, the
browser captures structured events (`{ ts, source, kind, payload }`)
into a 2000-entry ring buffer
([`web/src/log.ts`](../../../web/src/log.ts)) and batches them out
every ~500 ms to `POST /api/logs/ui`
([`app/web/server.py`](../../../app/web/server.py)). The handler caps
the batch, allow-lists `source` by prefix, truncates oversized
payloads, and emits each entry on the `app.ui` logger as
`INFO [ui] {source} {kind} {payload_json}` so it interleaves into
`data/app.log` with the existing backend lines. The "Download
buffer" button serialises the in-memory ring to
`alexia-ui-log-<iso>.json` for cases where the backend isn't
responding. Disabling the toggle returns `403` on `/api/logs/ui`,
the batcher drains, and `debugLog.log` becomes a free no-op.

Sources instrumented today (kept tight to the cry-cascade /
lip-sync / reconnection forensic surface): `ws`, `voice`,
`channel.expression`, `channel.overlay`, `channel.motion`,
`channel.outfit`, `channel.accessory`. Per-frame work (lip-sync
amplitude, Pixi ticks) is intentionally not logged. Tests:
[`tests/test_web_server_ui_logs.py`](../../../tests/test_web_server_ui_logs.py),
[`tests/test_web_server_settings.py`](../../../tests/test_web_server_settings.py)
(`LoggingSettingsRoundTripTests`),
[`web/src/log.test.ts`](../../../web/src/log.test.ts),
[`web/src/store.logging.test.ts`](../../../web/src/store.logging.test.ts),
and the "debug instrumentation" block in
[`web/src/live2d/channels/ExpressionChannel.test.ts`](../../../web/src/live2d/channels/ExpressionChannel.test.ts).

---
