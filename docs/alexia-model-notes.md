# Alexia Live2D rig — agent notes

The bundled avatar at `live-2d-models/Alexia/` is a third-party Cubism
4 model. The model files themselves are **gitignored** (see
`.gitignore` — `live-2d-models/`), so anything in this document refers
to the rig as users actually download it; clone the original Alexia
release into that folder before running the app.

This document captures the things that aren't obvious from inspecting
the files and that have already burned us once. **Read it before
changing anything in `app/core/avatar_profile.py` or the outfit /
overlay code paths in `web/src/components/Live2DAvatar.tsx`.**

---

## 1. Outfit system — the most counter-intuitive corner

Alexia ships **one alternate-outfit texture** (the pajamas) and a
**hood toggle** built on top of it. The naming and the parameter
semantics are not what they look like at first glance.

### Visual states (verified empirically)

| State | Param16 | Param17 | What you see |
|---|---|---|---|
| Baseline | 0 | 0 | Day clothes (streetwear, the default) |
| `yf` exp3 applied | 30 | 0 | Pajamas **with hood pulled up** |
| `yfmz` exp3 applied | 30 | 30 | Pajamas **with hood pulled down (off)** |

### The gotcha

The CDI3 metadata labels Param17 as `"Clothes (with hood)"`. The
original Chinese was `衣服托帽子` — and `托` literally means *"to lift /
hold up / take off the head"*. So the parameter is actually a
**"lift hood off"** toggle, NOT an "add hood" one. Driving Param17
toward 30 *removes* the hood, it does not add one.

The default art for the alternate-outfit (Param16=30 alone) is the
**hooded** pajama look. Param17 only matters once Param16 is up —
on its own it does nothing visible.

If you encounter behavior where the two pajama radios appear flipped,
this is almost certainly the cause. Re-confirm by toggling
`SettingsDrawer` and watching what changes; do **not** trust the
parameter name.

### Capability mapping (in `_ALEXIA_EXPR_TO_CAPABILITY`)

| Expression file | Capability | Resulting binding |
|---|---|---|
| `yf.exp3.json` | `pajamas_hooded` | `{Param16: 30}` |
| `yfmz.exp3.json` | `pajamas` | `{Param16: 30, Param17: 30}` |
| _(none)_ | `day_clothes` | `[]` (synthesised; baseline) |

**Why bare pajamas carries Param17 and hooded does not** — Because the
hooded look IS the default once Param16 is on; the only way to
"remove" the hood is to pump Param17 up to 30. The bare-pajamas
binding therefore needs both contributions; the hooded variant only
needs Param16.

### Day clothes is synthetic

There is no `.exp3.json` for day clothes — day clothes IS the model's
baseline state. `app/core/avatar_profile.py::_detect_capabilities`
synthesises an empty `OutfitBinding` for `day_clothes` whenever any
pajama variant is detected, so the SettingsDrawer always renders the
"Day" radio. The empty params list also guarantees that the day
binding never contributes a competing zero-value write to Param16
(which would otherwise zero out the active pajamas envelope's
contribution).

### The additive-sum renderer (why it has to be additive)

`Live2DAvatar.tsx` could write each binding's params sequentially
(`setParam(p.id, env * on_value)`), but **two of our three outfits
share Param16**. Sequential writes mean whichever binding runs last
overwrites the others, so the inactive envelope (=0) silently zeros
out the active one's contribution. Net effect: selecting one outfit
shows the other.

The fix in place: per-frame, accumulate every active binding's
contribution per param-id into `outfitParamSums`, then write the sum
once. During a `pajamas ↔ pajamas_hooded` crossfade, Param16 stays
locked at 30 (`0.5 * 30 + 0.5 * 30 = 30`) while Param17 fades from
30 → 0 (or 0 → 30) smoothly. A `day ↔ pajamas*` crossfade has only
one envelope contributing to Param16, so the linear fade falls out
naturally.

If you ever add a third pajama-adjacent variant, it just plugs into
the same accumulator; no further structural change needed.

---

## 2. Parameter cheat-sheet (after the CDI3 translation pass)

The CDI3 file at `live-2d-models/Alexia/Alexia.cdi3.json` had its
269 Chinese `Name` fields translated to English by
`scripts/translate_alexia_cdi3.py`. Capability detection in
`_detect_capabilities` substring-matches against these names AND the
expression filenames (pinyin abbreviations).

Highlights of the parameter map relevant to the agent:

| Param ID | Translated `Name` | Used by |
|---|---|---|
| `Param11` | Sunglasses | `_CAPABILITY_SYNONYMS["sunglasses"]` |
| `Param64` | Eyeglasses | `_CAPABILITY_SYNONYMS["glasses"]` (matches `"eyeglasses"`, NOT bare `"glasses"`, to avoid stealing Sunglasses) |
| `Param16` | Clothes | Outfit toggle (see §1) |
| `Param17` | Clothes (with hood) | Hood-LIFT toggle (see §1) |
| `Param43` | Question mark | `has_question` |
| `Param44` | Sweat | `has_sweat` |
| `Param54` | Grin | `has_grin` (`lzx` exp3) |
| `Param55` | Star eyes | `has_stars` |
| `Param56` | Dizzy | `has_dizzy` |
| `Param57` | Angry | `has_angry_marks` |
| `Param58` | Blush | `has_blush` |
| `Param59` | Cry | `has_cry` |
| `Param60` | bbt | `has_sticker` (generic overlay; **visually a pronounced cry-face overlay — used for the `cry` reaction; see §3a**) |
| `Param61` | Pose 1 | `has_pose` (held to 0 by yf/yfmz exp3 to suppress pose during outfit fade) |

The standard Cubism head/body/breath/eye params (`ParamAngleX/Y/Z`,
`ParamBodyAngleX/Y/Z`, `ParamEyeBallX/Y`, `ParamBreath`,
`ParamMouthOpenY`, etc.) drive the body-language layer. They are
detected by exact ID match, not by name, so the translation pass
doesn't touch them.

---

## 3. Expression files (`.exp3.json`)

Sit in the model root: `live-2d-models/Alexia/*.exp3.json`. The
ones the agent uses:

| File | Pinyin → meaning | Maps to |
|---|---|---|
| `bbt.exp3.json` | bbt (opaque pinyin; visually a pronounced cry / distressed face overlay) | sticker overlay slot — owned by the `cry` reaction; see §3a |
| `dyj.exp3.json` | 带眼镜 (with glasses) | `has_glasses` |
| `lh.exp3.json`  | 脸红 (blush) | `has_blush` |
| `lzx.exp3.json` | 咧嘴笑 (grin) | `has_grin` |
| `mj.exp3.json`  | 墨镜 (sunglasses) | `has_sunglasses` |
| `sq.exp3.json`  | 生气 (angry) | `has_angry_marks` |
| `wh.exp3.json`  | 问号 (question) | `has_question` |
| `xxy.exp3.json` | 星星眼 (star eyes) | `has_stars` |
| `yf.exp3.json`  | 衣服 (clothes) | `pajamas_hooded` (see §1) |
| `yfmz.exp3.json`| 衣服托帽子 (clothes with hood lifted) | `pajamas` (see §1) |
| `yjys1.exp3.json` / `yjys2.exp3.json` | eye color | `has_eye_color_a/b` |

Capability detection uses `_ALEXIA_EXPR_TO_CAPABILITY` for explicit
overrides and falls back to `_CAPABILITY_SYNONYMS` substring matching
when the file isn't in the table. **`lzx` is in the explicit table
specifically because the synonym matcher was incorrectly grabbing it
for `has_hood` once upon a time** — keep it explicit.

`_parse_exp3_params` filters out zero-valued parameters before
building the `OutfitBinding`, so `Param17: 0` in `yf.exp3.json` does
NOT end up in the hooded binding. That's why the binding ends up as
`{Param16: 30}` cleanly.

### 3a. ``bbt`` is the dramatic cry overlay (not a happy sticker)

The cdi3 / model3 label ``bbt`` (Param60) gives no clue about the
overlay's actual visual content. Param60 also sits adjacent to
``Param59 = Cry`` in the parameter list and inherits the same
"symbol expression" part group, which led the initial mapping pass
to treat it as a generic happy sticker slot for ``cheerful`` and
``amused`` reactions.

Visual inspection on the live rig (set ``Param60=30`` in the
SettingsDrawer) shows ``bbt`` actually renders as a **pronounced
cry-face overlay** — distinct from but more intense than ``k`` /
Param59 (the subtle "tear streaks" cry). Mapping a positive reaction
to it produced a regression where ``[[reaction:cheerful]]`` visibly
cried on screen.

Current authoritative mapping after the audit:

| Reaction       | Expression  | Param         | Visual                      |
|----------------|-------------|---------------|-----------------------------|
| ``cheerful``   | ``lzx``     | Param54 = Grin | toothy grin / wide smile    |
| ``amused``     | ``lzx``     | Param54 = Grin | (shared with cheerful)      |
| ``sad``        | ``k``       | Param59 = Cry | quiet tear streaks          |
| ``melancholy`` | ``k``       | Param59 = Cry | (shared with sad)           |
| ``concerned``  | ``k``       | Param59 = Cry | (shared with sad)           |
| ``cry``        | ``bbt``     | Param60 = bbt | dramatic / pronounced cry   |

The ``cry`` reaction is the most distressed entry in the canonical
reaction set (``app/core/reactions.py`` — see ``REACTIONS``), with
the lowest TTS speed (0.92, right at the safe-range floor) and the
most negative valence impulse (-0.18 vs sad's -0.15) in
``app/core/affect_state.py``. The persona prompt explicitly tells the
LLM to reserve it for genuinely moving / distressing moments.

The ``[[overlay:grin]]`` LLM grammar tag still pulses ``lzx`` as a
transient overlay; OverlayChannel re-applies the persistent reaction
on slot release, so a cheerful turn that also emits
``[[overlay:grin]]`` simply sustains the existing grin without
churning between expressions.

**Future rigs**: when bringing in a new model with a softer-smile
overlay (e.g. closed-eye smile, blush-cheek smile), prefer that for
``cheerful`` and reserve ``lzx``-equivalents for ``amused`` /
``playful``. Do not reintroduce ``bbt`` for any positive reaction.

---

## 4. Motion files (`.motion3.json`)

Generated programmatically by `scripts/generate_alexia_motions.py`
because the rig didn't ship with usable head-gesture motions. Output
lives at the model root:

- `nod.motion3.json` — head nod (yes)
- `shake.motion3.json` — head shake (no)
- `bow.motion3.json` — small bow
- `dh.motion3.json` — cloth sway (came with the rig; not in the
  motion grammar registry, deliberately)

The patches into `Alexia.model3.json`'s `Motions` block are also done
by the script. If you regenerate motions, re-run the script; the
patcher is idempotent.

---

## 5. Cubism update pipeline — write order matters for lip-sync

> **As of Phase 11 of the engine refactor**, every per-frame
> parameter write lives in `web/src/live2d/channels/` and is
> driven by `AvatarEngine`. `Live2DAvatar.tsx` now only does Pixi
> setup, model load, and the idle-motion timer. The
> `beforeModelUpdate` hook below is owned by `LipsyncChannel`
> (registered on the engine, fired exactly once per frame). When
> debugging mouth-freezing during TTS, check `LipsyncChannel`
> first, not the component.

This isn't Alexia-specific but it's traps you'll hit the moment you
touch the live2d engine. Each frame, `pixi-live2d-display`'s
`Cubism4InternalModel#update(dt, now)` runs in this exact order
(verified by reading
`web/node_modules/pixi-live2d-display/dist/cubism4.es.js`,
class `Cubism4InternalModel`):

1. `emit("beforeMotionUpdate")`
2. `motionManager.update(coreModel, now)` — drives parameters from
   the active `.motion3.json` curves. **Talk motions and idle
   motions can include `ParamMouthOpenY` keyframes**, in which case
   they unconditionally overwrite whatever the previous step wrote.
3. `emit("afterMotionUpdate")`
4. `coreModel.saveParameters()` — snapshots the current parameter
   values for the next frame's restore.
5. `expressionManager.update(coreModel, now)` — applies the active
   expression's per-parameter values using the expression's `Blend`
   mode (`Add` / `Multiply` / `Overwrite`).
6. `eyeBlink` (if no motion is active), `updateFocus`,
   `updateNaturalMovements` (breath), `physics.evaluate`,
   `pose.updateParameters`.
7. `emit("beforeModelUpdate")` — last hook before render.
8. `coreModel.update()` — applies parameters to the rig and renders.
9. `coreModel.loadParameters()` — restores the snapshot from step 4
   so the next frame's input baseline is post-motion, pre-expression.

### Lip-sync MUST hook step 7

The mouth amplitude is broadcast from the backend over WebSocket
(`audio_amplitude` events) and stored in
`useAssistantStore.audioAmplitude`. Driving the rig from a plain
`requestAnimationFrame` callback fires **before step 1**, which
means any talk-motion mouth curve at step 2 silently overwrites our
write. The visible symptom is "mouth frozen during TTS", and it is
particularly cruel because it works for rigs whose talk motion
doesn't include the mouth — so the regression went unnoticed for a
while.

The fix in place lives in `web/src/live2d/channels/LipsyncChannel.ts`:

```typescript
tickPreModel(): void {
  const target = deps.getStoreSnapshot().audioAmplitude || 0;
  this._smoothed = clamp(
    this._smoothed + (target - this._smoothed) * SMOOTH_FACTOR,
    0, 1,
  );
  for (const id of this._paramIds) {
    adapter.setParam(id, this._smoothed);
  }
}
```

`AvatarEngine.start()` wires `adapter.onBeforeModelUpdate(...)`
exactly once and fans the hook out to every channel that
implements `tickPreModel`. Because step 7 fires AFTER motion +
expression + breath, our value is what gets rendered in step 8 —
regardless of what any of the upstream stages wrote. Any future
per-frame parameter that competes with motions / expressions (e.g.
a custom blink override) should be implemented as a channel that
writes inside `tickPreModel`.

### Expressions don't auto-clear

`pixi-live2d-display`'s `Live2DModel.expression(name)` is
apply-only. There is no `model.expression()`-to-cycle convention
that survives the bundle's expression manager idiom. To clear the
active expression you must reach into the manager:

```typescript
model.internalModel.motionManager.expressionManager?.resetExpression();
```

`resetExpression()` swaps to a synthesised empty expression
(`defaultExpression`, created in `ExpressionManager.init()` with no
parameters), which immediately stops any param overrides from the
previous expression. This is what `ExpressionChannel` does for
empty / unmapped reactions — see
`web/src/live2d/channels/ExpressionChannel.ts` (`_applyTarget`).
Without it, a reaction expression applied earlier in the turn
stayed frozen on the face after TTS ended (idle motion would
resume but the eyes/mouth shape from the last expression remained
baked in until the next non-empty reaction).

### Overlay pulses also push expressions — and need a restore

A second class of expression-on-the-rig writes is the `expr:`-bound
overlay pulse. When the LLM emits e.g. `[[overlay:grin]]` and the
binding's `param_id` starts with `expr:` (overlays that don't map to
a single param — grin, stars, sticker), `OverlayChannel` fires
`adapter.expression(exprName)` once when the pulse first lands.
That call goes through the same expression slot as the persistent
reaction, which means it both (a) clobbers the persistent reaction
expression for the duration of the pulse, and (b) sits permanently
on the rig when the pulse ends — `expression(name)` is apply-only,
and the persistent reaction is only re-applied when its value
*changes*. Two consecutive turns with the same reaction value
(both `neutral`, both `cheerful`, …) would otherwise leave the
overlay expression baked in forever.

The fix in place lives in three coordinated channels:

- `OverlayChannel` writes `engineState.exprSlotLockUntil = pulse.until`
  on the first frame of an `expr:`-bound pulse and fires
  `adapter.expression(name)` exactly once.
- `ExpressionChannel` reads `exprSlotLockUntil > now` to defer
  reaction writes while the overlay owns the slot (otherwise a
  fresh reaction would cut the overlay short).
- `AvatarEngine` watches `exprSlotLockUntil` on every tier-3 tick,
  zeroes it on expiry, and fans `onExpressionSlotReleased` to every
  channel. `ExpressionChannel.onExpressionSlotReleased` re-applies
  whatever its current target is (reaction or voice-mode override
  or `resetExpression()` for empty mappings). This is the **only**
  path that handles same-value reaction sequences.

### Wall-clock vs monotonic clock — pulse deadlines

`overlay.expiresAt` arrives from the WS handler as `Date.now() +
duration_ms` (wall-clock, ~1.7e12). The engine + channels use
`performance.now()` (monotonic, ~1e4 on a fresh page). Comparing
them directly is the kind of bug that turns every pulse into a
permanent on-state and every gesture boost into a permanent
multiplier. The conversion is centralised in
`AvatarEngine.dispatchOverlay`:

```typescript
const remainingMs = Math.max(0, overlay.expiresAt - Date.now());
const until = this._now() + remainingMs;
this._fanOut("onOverlay", (channel) => channel.onOverlay?.({ name, until }));
```

Channels can blindly compare against `deps.now()`. Any new
timer-driven state added to a channel must use `deps.now()` (or
the engine's monotonic clock), never `Date.now()`.

---

## 6. Things future agents have already gotten wrong

Specific traps that have actually happened in this codebase and the
mitigation in place:

1. **Inverting `yf` / `yfmz` mapping**. Don't trust the parameter
   name. Verify visually in the SettingsDrawer. The test
   `test_alexia_outfit_capability_mapping_matches_visual_rig` locks
   the current mapping in.
2. **Sequential outfit param writes in the renderer**. Two outfits
   reference Param16; sequential writes silently zero each other out.
   Locked in by `test_pajama_variants_share_param16_for_additive_renderer`.
3. **Synonym pollution between `glasses` and `sunglasses`**. The bare
   word `"glasses"` substring-matches `"Sunglasses"` too, and Sunglasses
   is declared first in the CDI3, so it would steal the `has_glasses`
   binding. The synonym is `"eyeglasses"` (unique to Param64).
4. **Adding a hood capability separately**. There is no standalone
   `has_hood` capability — the hood IS part of the alternate outfit.
   Adding one creates a phantom "Param17 alone" overlay that shows
   the hood floating over baseline (no body-clothes).
5. **`lzx` (grin) being misclassified as hood-related**. `lzx`
   (咧嘴笑 = "toothy grin") was getting fuzzy-matched into a
   nonexistent hood capability. It's in `_ALEXIA_EXPR_TO_CAPABILITY`
   explicitly to short-circuit the synonym pass.
6. **Forgetting to update the four-place validator allow-list**.
   `auto_outfit` is whitelisted in `app/core/settings.py` (loader),
   `app/core/session_controller.py` (`update_avatar_settings`),
   `app/web/server.py` (`PATCH /api/avatar`), and `web/src/types.ts`.
   Use the `OUTFIT_MODES` constant on the Python side; only
   `web/src/types.ts` needs a manual edit when adding a new mode.
7. **Driving lip-sync from a pre-`update()` rAF**. Writing
   `ParamMouthOpenY` from a plain `requestAnimationFrame` runs
   before `motionManager.update()`, which silently overwrites the
   value if the active motion has mouth keyframes. Use
   `internalModel.on("beforeModelUpdate", ...)` instead — see §5.
8. **Treating `model.expression(name)` as both apply AND clear**.
   It only applies. To clear, call
   `internalModel.motionManager.expressionManager.resetExpression()`.
   The reaction-on-neutral path was previously a silent no-op; it
   left the previous expression stuck.
9. **Mixing motion and overlay tags**. `tail_wag`, `wink_left`,
   `wink_right`, and `ear_wiggle` are advertised as `[[overlay:X]]`
   even though they animate over time. Motions are only `.motion3.json`
   file stems (`wave`, `nod`, `shake`, `bow`, `shrug`, `stretch`,
   `dance`). The LLM has emitted `[[motion:tail_wag]]` in the wild;
   `SessionController._emit_avatar_motion` now re-routes the misroute
   to `_emit_avatar_overlay` when the corresponding `has_<name>`
   capability exists, but the prompt grammar should still steer the
   model. The contrast clarifier in both grammar builders
   (`_build_motion_grammar_addendum` /
   `_build_overlay_grammar_addendum`) is what nudges the LLM at the
   source.
10. **Wall-clock-typed deadlines compared against `performance.now()`**.
    `overlay.expiresAt` is `Date.now() + duration_ms`; the tier-3 RAF
    uses `performance.now()`. Convert at the boundary when ingesting
    overlays into `pulses` / `gestures`. See §5 for the snippet. The
    pre-fix bug made every pulse and gesture sticky forever (~50 year
    delta between the two clocks).
11. **`expr:`-bound overlay sticking past its lifetime**. The tier-3
    pulse handler fires `model.expression(name)` once per pulse. The
    `expression(name)` call goes through the same slot as the
    persistent reaction; without an explicit restore the overlay
    expression sits on the rig forever. See §5 for the restore
    pattern (`lastExprOverlayUntilRef` + unconditional `applyReaction`
    on expiry).

---

## 7. Continuous expressiveness — `tickPreModel` overrides + Backchannel motion group

Two channels write parameters from `tickPreModel` (the
`beforeModelUpdate` event hook), which is the **last writable point**
before `model.update -> loadParameters` ships the frame to GL. Any
write that has to win against the `expressionManager` (Add blend), the
`focusController` (Add blend on `ParamAngleX/Y/Z`, `ParamBodyAngleX`,
`ParamEyeBallX/Y`), or the `breath` driver (Add blend on `ParamBreath`)
must happen here.

```text
beforeMotionUpdate -> motionManager -> afterMotionUpdate -> saveParameters
  -> expressionManager -> eyeBlink -> updateFocus
  -> breath.updateParameters
  -> physics -> pose
  -> beforeModelUpdate                   <- our tickPreModel runs here
  -> model.update -> loadParameters
```

### `AmbientBodyChannel.tickPreModel`

- **Breath**. When the rig advertises `has_breath`
  (`avatar_profile._BODY_ANGLE_PROBES` adds `"ParamBreath"`), the
  channel writes `ParamBreath = 0.5 + 0.5 * sin(2*pi*freq*t)` with
  `freq = BREATH_BASE_HZ * (0.7 + 0.7 * arousal)`. This **replaces**
  the auto-breath driver's contribution rather than adding to it —
  the absolute write at `tickPreModel` time wins over the upstream
  Add blend. `expressiveness == 0` collapses the write to a static
  `0.5` so the rig doesn't freeze in a half-inhale pose.
- **Valence body tilt**. When the rig advertises `has_body_angle_y`,
  the channel adds `valence * VALENCE_TILT_AMPLITUDE * expressiveness`
  to `ParamBodyAngleY`. Smoothed via `approach()` in a private
  `_valenceTilt` field so a mood swap doesn't snap the body.
- **Tier-3 amplitudes**. The discrete `LEAN_IN_AMPLITUDE`,
  `SLUMP_AMPLITUDE`, `SASS_AMPLITUDE`, idle bounce, idle breathing
  sway etc. in `tickTier3` are all **multiplied by
  `expressiveness`** at write time so the slider also dampens the
  pulse-style cues.

### `ExpressionChannel.tickPreModel`

The `AvatarProfile` carries `expression_params: dict[str,
list[ExpressionParam]]` — a parallel of `OutfitBinding.params` but for
expression files. While an expression is active and the slot isn't
locked by an overlay (`engineState.exprSlotLockUntil <= now`), the
channel writes each declared parameter directly with
`on_value * scale * expressiveness`, where
`scale = clamp(0.4 + 0.6 * arousal, 0.4, 1.0)` and `arousal` is read
from the snapshot every tick. The standard `expressionManager`
fade-in still runs (the channel still calls `adapter.expression(name)`
on apply); the override just dominates while the value is live. The
value is capped by the file's authored `on_value` even at
`expressiveness = 1.5` because we never write above
`on_value * 1.0 * 1.5 == 1.5 * on_value`, and the rig's natural
on-value is the upper bound — but in practice
`scale * expressiveness` stays at-or-below 1.5 only when arousal is
high, and the visual ceiling is the rig's authored expression.

### `Backchannel` motion group

The Alexia rig now ships a fourth motion group, `Backchannel`,
containing `tilt_left`, `tilt_right`, and `microshake`. They're
generated by the same script that emits the `Tap` group — see
`scripts/generate_alexia_motions.py` (`Motion.group` field).
`SessionController._emit_backchannel_motion` resolves a hint to a
motion *name*, looks it up in `avatar.motions` regardless of the
group key, and broadcasts the resolved `(group, index)` with
`priority: "idle"`. The frontend's `MotionChannel` translates that
into `MotionPriority.IDLE`, which pixi-live2d-display
**automatically pre-empts** when a `MotionPriority.NORMAL` motion
fires (e.g. an LLM-driven reaction `[[motion:wave]]`). No explicit
cancellation logic is needed on either side.

### Trip-wires that took us a while

- **Two consumers of `beforeModelUpdate` on the same engine**. Today
  `LipsyncChannel`, `AmbientBodyChannel`, and `ExpressionChannel` all
  subscribe through the engine's central fan-out. The order is
  registration order, so writes by later channels can stomp on earlier
  ones. The current registration order
  (`AmbientBodyChannel -> ExpressionChannel -> LipsyncChannel`) is
  deliberate: lipsync owns `ParamMouthOpenY` outright; the other two
  touch disjoint params.
- **`expressiveness == 0` and breath**. Setting it to `0.0` correctly
  silences the new continuous overrides, but you don't want the breath
  to literally freeze at 0 (it draws as a fully-deflated chest pose).
  The channel writes `0.5` instead — a relaxed mid-inhale.
- **Capability gating on minimal rigs**. `has_breath` and
  `expression_params` both default to "missing" on rigs that don't
  declare them. The channels short-circuit out of `tickPreModel`
  early so a vanilla Cubism sample model riding the same code path
  pays nothing.

---

## 8. When in doubt, run these

```bash
python -m pytest tests/test_avatar_profile.py -v        # capability detection
python -m pytest tests/test_session_controller_avatar_commands.py -v  # outfit dispatch
python -m pytest tests/test_prompt_assembler.py -v      # LLM grammar
```

Then start the app (`python -m app.main`) and hit each radio in the
SettingsDrawer outfit panel. The four valid combinations are:

- **Auto** + day-period → day clothes
- **Auto** + night/late-night-period → bare pajamas (no hood)
- **Day** → day clothes
- **Pajamas** → bare pajamas (Param16=30, Param17=30 — hood lifted)
- **Pajamas (hooded)** → hooded pajamas (Param16=30, Param17=0)

If any radio renders the wrong silhouette, re-read §1.
