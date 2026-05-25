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
| `Param60` | bbt | `has_sticker` (generic overlay) |
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
| `bbt.exp3.json` | bbt | sticker overlay |
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

## 5. Things future agents have already gotten wrong

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

---

## 6. When in doubt, run these

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
