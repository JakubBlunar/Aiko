# Alexia Live2D rig — agent notes

The bundled avatar at `live-2d-models/Alexia/` is a third-party Cubism
4 model. The model files themselves are **gitignored** (see
`.gitignore` — `live-2d-models/`), so anything in this document refers
to the rig as users actually download it; clone the original Alexia
release into that folder before running the app.

This document captures the things that aren't obvious from inspecting
the files and that have already burned us once. **Read it before
changing anything in `app/core/persona/avatar_profile.py` or the outfit /
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
baseline state. `app/core/persona/avatar_profile.py::_detect_capabilities`
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
| `Param11` | Sunglasses | `_CAPABILITY_SYNONYMS["head_sunglasses"]` — perched on the hair, NOT on the eyes |
| `Param64` | Eyeglasses | `_CAPABILITY_SYNONYMS["eyeglasses"]` (matches `"eyeglasses"`, NOT bare `"glasses"`, to avoid stealing the Sunglasses entry) |
| `Param16` | Clothes | Outfit toggle (see §1); zero-valued in `zs1` to force day_clothes (see §3c) |
| `Param17` | Clothes (with hood) | Hood-LIFT toggle (see §1); zero-valued in `zs1` |
| `Param43` | Question mark | `has_question` |
| `Param44` | Sweat | `has_sweat` |
| `Param54` | Grin | `has_grin` (`lzx` exp3) — mouth-overlay param, tapered against `audioAmplitude` during speech (see §3b) |
| `Param55` | Star eyes | `has_stars` |
| `Param56` | Dizzy | `has_dizzy` — owns the `confused` reaction (see §3) |
| `Param57` | Angry | `has_angry_marks` |
| `Param58` | Blush | `has_blush` |
| `Param59` | Cry | `has_cry` — tear streaks, used by `sad` and (via neighbour fallback) `cry` |
| `Param60` | bbt | `has_lollipop` — **lollipop / candy prop drawn in the mouth**; accessory tier (see §3a) |
| `Param61` | Pose 1 | `has_crossed_arms` — day-clothes-only crossed-arms pose (see §3c) |

The standard Cubism head/body/breath/eye params (`ParamAngleX/Y/Z`,
`ParamBodyAngleX/Y/Z`, `ParamEyeBallX/Y`, `ParamBreath`,
`ParamMouthOpenY`, etc.) drive the body-language layer. They are
detected by exact ID match, not by name, so the translation pass
doesn't touch them.

---

## 3. Expression files (`.exp3.json`)

Sit in the model root: `live-2d-models/Alexia/*.exp3.json`. The
**authoritative visual identity audit** (third pass, anchored on
the user's live observation in Cubism Viewer Standalone for SDK 5)
lives in [`docs/Alexia-my-observation.md`](Alexia-my-observation.md).
This table reflects that audit:

| File | CDI3 name / pinyin | Visual on the rig | Capability / reaction |
|---|---|---|---|
| `bbt.exp3.json` | bbt (棒棒糖) | **lollipop / candy prop drawn inside the mouth** (0–30 additive) | `has_lollipop` — accessory tier, NOT an emotional reaction. See §3a. |
| `dyj.exp3.json` | 带眼镜 (with glasses) | regular eyeglasses worn on the face (0–30) | `has_eyeglasses` |
| `h.exp3.json`   | 汗 (sweat) | single nervous sweat-drop at the top of the left eye, over the hair (0–30) | `has_sweat` |
| `k.exp3.json`   | (cry — Param59) | quiet tear streaks below the eyes, **mouth untouched** (0–30) | `cry` reaction falls back here via `sad` neighbour |
| `lh.exp3.json`  | 脸红 (blush) | cheek blush (0–30) | `has_blush` |
| `lzx.exp3.json` | 咧嘴笑 (grin) | toothy grin / closed teeth-joined smile (0–30) — **paints over the lip-sync mouth, see §3b** | `has_grin` — `cheerful` / `amused` |
| `mj.exp3.json`  | 墨镜 (sunglasses) | sunglasses **perched on top of the hair** (not on the eyes) | `has_head_sunglasses` |
| `sq.exp3.json`  | 生气 (angry) | shadow over the eyes, opacity-modulated | `has_angry_marks` |
| `wh.exp3.json`  | 问号 (question) | floating question mark under the right ear | `has_question` |
| `xxy.exp3.json` | 星星眼 (star eyes) | star-shaped retinas (0 / 30 only — intermediate values look broken) | `has_stars` |
| `y.exp3.json`   | 晕 (dizzy / confused) | spiral retinas | `has_dizzy` — `confused` reaction (NOT `tired`; see regression notes below) |
| `yf.exp3.json`  | (day-clothes baseline) | day clothes, hood off, arms by side (Param16=0, Param17=0, Param61=0) | `pajamas_hooded` outfit envelope (see §1) |
| `yfmz.exp3.json`| (pajamas-with-hood) | hooded pajamas (Param16=30, Param17=0 → hood lifted) | `pajamas` outfit envelope (see §1) |
| `yjys1.exp3.json` | (left eye purple) | left iris turns purple | `has_eye_color_a` |
| `yjys2.exp3.json` | (right eye purple) | right iris turns purple | `has_eye_color_b` |
| `zs1.exp3.json` | 姿势 1 (Pose 1) | **crossed arms** — exp3 zeroes Param16 / Param17 so it only renders against day_clothes; see §3c | `has_crossed_arms` — `playful` reaction in day clothes |

Capability detection uses `_ALEXIA_EXPR_TO_CAPABILITY` for explicit
overrides and falls back to `_CAPABILITY_SYNONYMS` substring matching
when the file isn't in the table. **`lzx` is in the explicit table
specifically because the synonym matcher was incorrectly grabbing it
for `has_hood` once upon a time** — keep it explicit.

`_parse_exp3_params` filters out zero-valued parameters before
building the `OutfitBinding`, so `Param17: 0` in `yf.exp3.json` does
NOT end up in the hooded binding. That's why the binding ends up as
`{Param16: 30}` cleanly. The **outfit-gate detector**
(`_detect_outfit_gated_expressions`) goes the other way: it reads the
zero-valued entries deliberately, because they're the rig's way of
saying "I require these envelope params to be off" — see §3c.

### 3a. ``bbt`` is a lollipop prop, not an emotion overlay

This is the third (and hopefully final) classification of ``bbt`` /
Param60. The history is worth keeping because the same misread could
happen again on a future rig with similarly opaque labels.

| Pass | Classification        | Mapped to                          | Bug we observed                        |
|------|------------------------|-------------------------------------|----------------------------------------|
| 1    | "happy sticker"        | `cheerful` / `amused` reactions     | A cheerful turn rendered with a candy prop in the mouth |
| 2    | "dramatic cry overlay" | `cry` reaction                      | A cry turn shoved a lollipop into Aiko's mouth mid-sob |
| 3    | **lollipop / candy prop** | accessory tier (no reaction maps to it) | none — visual matches the user's audit |

The clincher was the user's live observation in Cubism Viewer
Standalone (Alexia is MOC3 v5 / SDK 5.0, so the older Cubism 3 Viewer
for Unity can't load it): with `Param60=30` you can clearly see a
**lollipop / candy on a stick drawn inside her mouth**, distinct
from both the toothy grin (`lzx` / Param54) and the tear streaks
(`k` / Param59).

That puts `bbt` firmly in the *accessory* space alongside glasses
and sunglasses — not the *emotion* space. Today the only consumer is
the `has_lollipop` capability; Phase 4 of the expression-overhaul
plan will surface it as a persistent toggle in the SettingsDrawer
("give Aiko a lollipop"). Until then it's reachable as
`[[overlay:lollipop]]` via the grammar.

**Future rigs**: emotion reactions go on params that render an
emotion. Accessory props (candy, glasses, food, toys) get their own
capability and stay out of `_ALEXIA_REACTION_MAP`.

### 3b. Lip-sync conflicts — the `lzx` mouth-closure trap

`lzx` (Param54 = 咧嘴笑 = toothy grin) is a closed-mouth expression:
the rig draws her teeth meeting at the centre and the upper / lower
lip locked together. While that overlay is active at full amplitude,
the lip-synced jaw motion is **visually masked** — you can still
hear the audio but the mouth doesn't appear to move with it.

Detection chain:

1. `_detect_mouth_overlay_param_ids` walks the CDI3 and matches
   names against `_MOUTH_OVERLAY_SYNONYMS` (`咧嘴`, `grin`, `smirk`,
   `toothy`). For Alexia this returns `["Param54"]`.
2. `_detect_mouth_blocking_expressions` cross-references that param
   list against `expression_params`. Any expression whose binding
   touches a mouth-overlay param is flagged. For Alexia: `["lzx"]`.
3. The frontend `ExpressionChannel.tickPreModel` reads
   `mouth_overlay_param_ids` and per-frame writes any matching
   binding as `on_value * (1 - lipsyncSuppression)`. The suppression
   factor itself is driven by `audioAmplitude * 6` clamped to `[0,1]`
   with a 150 ms time constant. Crucially the mouth overlay is driven
   by this taper **alone** — it is NOT arousal-scaled or
   inertia-damped like the other expression params (see the
   "two mouths while silent" note below).

Net effect: `[[reaction:cheerful]]` keeps the persistent grin
between turns (where lip-sync is silent) but the grin tapers down to
zero whenever TTS is actually speaking, exposing the lip-synced
mouth underneath. As soon as audio drops back to silence the grin
recovers smoothly.

**The "two mouths while silent" regression (calm-turn arousal
scaling).** The mouth overlay used to also be multiplied by the
continuous-expressiveness `amplitudeScale` (`clamp(0.4 + 0.6 *
arousal, 0.4, 1.0)`). On a low-arousal (calm) turn that left the
grin at only ~40-50% of its authored value — enough to draw the
toothy grin but **too weak to mask the base lip-sync mouth**, so
both rendered at once even while she was silent. The fix drops the
arousal / expressiveness / inertia factors from the mouth-overlay
write specifically: a mask is binary by nature (fully on = hides the
base mouth, fully off = reveals it), so it rides the lip-sync taper
only. Non-mouth bindings on the same expression keep their
arousal / inertia scaling. Locked in by the "keeps the grin at full
when silent on a LOW-arousal turn" test in
`ExpressionChannel.test.ts`.

`mouth_blocking_expressions` is mostly informational today —
the per-param taper does the real work — but it's exposed on the
manifest so the SettingsDrawer (and future tooling) can answer "is
this expression safe to combine with active speech?" without
re-walking `expression_params`. Tests pin both the param-id
detection (`MouthOverlayParamDetectionTests`) and the
expression-name detection (`MouthBlockingExpressionsTests`).

**Future rigs**: any new model with a teeth-joined / mouth-closed
smile param needs its CDI3 name to match
`_MOUTH_OVERLAY_SYNONYMS`, or the synonyms list needs widening, or
the param + the owning expressions need adding to a curated
override. Without the gate the grin will visibly fight the lip-sync.

### 3c. Outfit-gated expressions — the `zs1` day-clothes-only pose

The crossed-arms pose (`zs1.exp3.json`, Param61 = 姿势 1) is
authored against the day-clothes silhouette. The rig's pose mesh
overlaps the pajamas / hooded-pajamas envelopes, so the exp3
explicitly writes Param16 = 0 and Param17 = 0 to **force the outfit
to baseline** while the pose is active. Firing `zs1` while pajamas
are active produces no visible change — the additive blend lands the
arms outside the visible silhouette, so it's a silent no-op.

Detection chain:

1. `_detect_outfit_gated_expressions` reads each `.exp3.json`
   directly (including zero-valued params, which the standard
   `_parse_exp3_params` would skip). Any expression whose Parameters
   list contains a zero-valued entry for an outfit envelope param
   gets stamped with `["day_clothes"]` — the only Alexia outfit
   whose baseline binding has zero outfit-param contributions.
2. The detection currently uses a "any zero-valued entry" heuristic.
   Future rigs with multiple baselines (e.g. a sport-outfit envelope
   that also zeroes the pajamas param) would extend the helper to
   compare zeroed-id sets against each outfit's binding.
3. `ExpressionChannel._applyTarget` queries the active outfit via
   `_resolveActiveOutfitCapability` (`day → day_clothes`, pajamas
   variants pass through) and calls `resolveReactionExpression`
   with that capability. The resolver walks the
   reaction-neighbour chain whenever the direct mapping fails the
   gate, so for Alexia: `playful` resolves to `zs1` in day clothes
   and falls through to `amused` → `lzx` in pajamas.
4. `onOutfitChange` re-applies the resolved target so a manual
   outfit toggle (or a circadian auto-outfit flip) immediately
   updates the persistent expression.

The fallback is intentionally permissive when the active outfit is
unknown (empty string) — a freshly loaded rig that hasn't yet
reported its outfit should still get *some* expression. `StoreBridge`
pushes `resolved_outfit` shortly after attach, at which point the
gate tightens normally.

Tests pinning this:

- `tests/test_avatar_profile.py::OutfitGatedExpressionsTests` —
  the heuristic detects `zs1: ["day_clothes"]` from the mini
  fixture's exp3 and leaves non-gated expressions out of the map.
- `web/src/live2d/channels/ExpressionChannel.test.ts` "outfit
  gate" describe — `playful` flips between `zs1` and `lzx` as the
  active outfit toggles, including the `onOutfitChange` recovery
  path and the legacy-payload (no `outfit_gated_expressions` field)
  fallback.

**Future rigs**: outfit-gated expressions are still rare in the
wild; most rigs let any expression render against any outfit. The
machinery only fires when the exp3 author deliberately zeroes
outfit envelope params, which is a strong "I require baseline"
signal.

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

### Mouth-overlay vs lip-sync — the "two mouths" trap

`Param54` (Grin) is a **stylised mouth shape** the rig paints on top
of the regular mouth artwork. The `lzx` expression Add-blends it to
30, which makes it visible whenever the rig is rendering with `lzx`
active (the persistent mapping for `cheerful` / `amused`, plus
the `[[overlay:grin]]` pulse). Crucially, **`Param54` is NOT a
lip-sync param** — `Groups.LipSync` only lists `ParamMouthOpenY`. So
when Aiko speaks while a grin reaction is on, both mouths render
simultaneously: the static toothy grin overlay and the flapping
lip-synced jaw underneath.

The fix lives in `app/core/persona/avatar_profile.py` and
`web/src/live2d/channels/ExpressionChannel.ts`:

- `_detect_mouth_overlay_param_ids` walks the cdi3 `Parameters`
  and returns every param whose `Name` substring-matches one of
  `_MOUTH_OVERLAY_SYNONYMS` (`咧嘴笑` / `咧嘴` / `grin` / `smirk` /
  `toothy`). On Alexia this resolves to `["Param54"]`. On rigs
  without a mouth overlay (the bare fixture, plain Cubism rigs)
  the list is empty and the renderer's lip-sync path runs
  unmodified.
- The list ships on the manifest as
  `mouth_overlay_param_ids: string[]`, alongside the existing
  `lip_sync_ids` / `eye_blink_ids`.
- `ExpressionChannel.tickPreModel` smooths a separate
  "lip-sync suppression" factor off `audioAmplitude` (gain ≈ 6,
  time constant ≈ 150 ms) and writes any expression-param binding
  whose id lands in the cached overlay set as
  `on_value * (1 - factor)` — full while silent (masks the base
  mouth → single grin), fading to zero as she speaks. The overlay
  is driven by the taper alone (no arousal / expressiveness /
  inertia scaling — a partial mask is the "two mouths" bug, see
  §3b). Non-mouth bindings on the same expression (cheek squint,
  eye crinkle, …) keep their arousal / inertia scaling — only the
  overlay mouth fades.
- `_MOUTH_OVERLAY_SYNONYMS` deliberately omits the word `"smile"`.
  A soft closed-mouth smile expression doesn't paint a competing
  mouth; only overlays that visibly add a second mouth belong here.
  `"smile"` would falsely flag friendly / warm reactions and mute
  legitimate cheek params.
- `ParamMouthOpenY` itself must NEVER end up in this list —
  suppressing the lip-sync param IS the bug. The
  `test_mouth_overlay_excludes_plain_mouth_open` regression test
  in `tests/test_avatar_profile.py` locks that in.

When adding a future rig with its own grin / smirk overlay, just
ensure the cdi3 `Name` field carries one of the listed synonyms;
the detection picks it up automatically. New rigs with novel
naming conventions can extend `_MOUTH_OVERLAY_SYNONYMS` (and the
matching test) — keep the list narrow.

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
3. **Synonym pollution between `eyeglasses` and `head_sunglasses`**.
   The bare word `"glasses"` substring-matches `"Sunglasses"` too, and
   Sunglasses is declared first in the CDI3, so it would steal the
   `has_eyeglasses` binding. The eyeglasses synonym is `"eyeglasses"`
   (unique to Param64). The capabilities were also **renamed** during
   the visual-audit pass — `has_glasses` → `has_eyeglasses` (Param64,
   worn on the face) and `has_sunglasses` → `has_head_sunglasses`
   (Param11, perched on top of the hair). Don't reintroduce the
   old names; they conflated the artwork's location.
4. **Adding a hood capability separately**. There is no standalone
   `has_hood` capability — the hood IS part of the alternate outfit.
   Adding one creates a phantom "Param17 alone" overlay that shows
   the hood floating over baseline (no body-clothes).
5. **`lzx` (grin) being misclassified as hood-related**. `lzx`
   (咧嘴笑 = "toothy grin") was getting fuzzy-matched into a
   nonexistent hood capability. It's in `_ALEXIA_EXPR_TO_CAPABILITY`
   explicitly to short-circuit the synonym pass.
6. **Forgetting to update the four-place validator allow-list**.
   `auto_outfit` is whitelisted in `app/core/infra/settings.py` (loader),
   `app/core/session/session_controller.py` (`update_avatar_settings`),
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
12. **Mapping reactions to props instead of emotions**. `bbt` /
    Param60 got classified twice as an emotion overlay (first
    "happy sticker" for `cheerful` / `amused`, then "dramatic cry"
    for `cry`) before the live-rig audit revealed it's a **lollipop
    in the mouth**. Same trap awaits any opaque-pinyin param sitting
    in the symbol-expression part group. The current rule: emotion
    reactions go on params that render an emotion; props (candy,
    glasses, hats, food) get an accessory-tier capability and stay
    out of `_ALEXIA_REACTION_MAP`. See §3a for the regression
    history.
13. **Mistaking `y` (Param56 = Dizzy) for "tired"**. The pinyin is
    opaque and the CDI3 name "Dizzy" translates ambiguously, so
    `tired` was pointed at it for two refactors. Visual: it draws
    **spiral retinas**, which read as confused / dazed, not weary.
    `tired` now maps to `""` (the body-slump in
    `AmbientBodyChannel` carries the weary visual instead) and the
    new canonical reaction `confused` owns the spiral-eyes overlay.
14. **Firing `zs1` without checking the outfit**. The crossed-arms
    pose zeroes the pajamas envelope params (Param16 = 0,
    Param17 = 0) in its exp3, which means it silently no-ops when
    pajamas are active. Either gate the dispatch on the active
    outfit (see §3c — `outfit_gated_expressions`) or accept that
    `[[reaction:playful]]` produces no visible change in pajamas.

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
