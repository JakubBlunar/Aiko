# Atlas character workflow

The local Aiko study has 213 textured meshes, 21 skeletal joints and 51 public controls. Nine of the 222 source drawable entries have no triangles. Each retained mesh has its own editable PNG. A local inventory records its source atlas, crop rectangle, original drawable ID and part hierarchy. Both atlas pages contribute artwork.

## Open and edit

Run `npm run avatar:studio` from the repository root. The studio opens the generated character from `tools/rig-studio/private/aiko/` when present. The Character menu also includes Pip. Use **Load reference** for side-by-side comparison and **Follow pointer** for gaze with a small head turn. Pointer position is measured inside the custom pane. Turning tracking off releases its four controls to the selected clip and inspector.

Try **Tail swing**, **Toothy grin**, **Wrist greeting** and **Foot tap**. Pause and use **Isolate artwork** to inspect one visible piece. **Show skeleton** adds triangle edges and joint connections. The outfit menu includes casual black, hood-up pajamas and hood-down pajamas. Crossed arms are available in casual clothing. Grin strength and jaw drop can animate together while the ordinary mouth-opening control stays gated. Eye styles switch between natural, stars and spirals.

To edit a piece, find its file through `inventory.json`, keep its canvas dimensions and registration, and save it in place. To change a shape key, edit the matching layer in `rig.json`. The server watches the selected local folder. Rig and artwork edits reload at the current playhead and pinned pose. CSS updates apply in place. Saving TypeScript rebuilds the studio and restores the character, motion, time, pins, pointer pose, skeleton, selected piece and loaded reference. A failed compile leaves the current preview usable and reports the error. Export any unsaved joint edits before changing code or reloading the source rig.

`Open rig folder` reads a browser-selected snapshot. For automatic disk updates to an arbitrary character, launch with `npm run avatar:studio -- --rig "F:\Characters\MyPersona"`. Code reload can restore a browser-selected snapshot through temporary browser storage. The server runs its own watcher until stopped. Use `--no-watch` for a fixed preview. Changes to the server itself take effect on restart.

## Reproduce the character

The importer uses the repository's installed Cubism Core offline to read posed drawable geometry. It captures the default shape, texture coordinates, triangle indices, opacity, clipping and draw order, then samples each control at its knots in each outfit. The independent TypeScript/WebGL renderer plays the resulting character using the PNGs and rig JSON. The optional Live2D comparison pane uses the original compiled model.

From the repository root on Windows:

```powershell
.venv\Scripts\python.exe tools/rig-studio/build_atlas.py
```

The default output is `tools/rig-studio/private/aiko/`. A fresh export requires a new destination when an editable copy already exists. For example:

```powershell
.venv\Scripts\python.exe tools/rig-studio/build_atlas.py --output tools/rig-studio/private/aiko-next
npm run avatar:studio -- --rig tools/rig-studio/private/aiko-next
```

The sampler keeps the original aspect ratio at a 2048-pixel reference height. The cutter processes each atlas at up to 4096 pixels by default, crops by mesh UV bounds, retains a filtering margin around triangles and remaps UVs into the new PNG. Use `--texture-size 8192` for close-up artwork study. Both the registered source and the exports stay local.

## Identify a source control

Run an endpoint sweep for the installed model. Pick a new report filename for each run:

```powershell
npm --prefix tools/rig-studio run build
node tools/rig-studio/sample-atlas.mjs data/personas/active/Alexia/Alexia.model3.json tools/rig-studio/private/influences-next.json --audit
```

The report lists maximum vertex travel in 2048-height artwork pixels, opacity differences and draw-order changes per visible mesh and outfit. A change in an obscured piece can still be numerically measurable. Use the image pair and isolation control to verify what appears on screen.

| Control | Finding on the installed Alexia |
| --- | --- |
| Ear flex | The raw endpoints of Param38–41 yielded no visible ear displacement in the isolated sweep. Param14 moves the screen-left pair and Param18 the screen-right pair. Public left/right follows the character. |
| Chest lift | ParamBreath alone yielded no direct mesh displacement. The custom breathing clip pairs it with a modest excursion of Param10, a vertical upper-body physics output that also carries sleeves and arms. Param4 supplies upper-body sway. |
| Gaze and bow | The eye direction and head controls deform separate geometry. Pointer tracking drives both with smaller head travel. The bow combines head pitch and torso pitch using the installed motion's timing and the model's pitch limit. |
| Clothes and arms | Param16 selects pajamas. Param17 raises the hood-off control. The three exported wardrobes each have their own measured base. Param61 selects crossed arms in casual clothing. |
| Face | Param54 has authored endpoints at 0 and 30. Between them the teeth fade in, the underlying mouth fades out and nearby facial geometry changes. At full grin Param50 moves the joined teeth about 6.4 pixels vertically. Separate jaw drop, brow lift and brow shape controls use measured source geometry. Star eyes retain their discrete setting. |
| Tail | The production ambient channel staggers the five rotations by 0.7 radians. The studio samples 33 poses at its 10-degree mid-arousal amplitude and offsets phase zero to match neutral. Both ends of the phase slider draw the same pose. Solo playback traces the circle in two seconds. Layered playback adds one six-second turn to the moving idle phase, easing its speed at entry and release. |
| Limbs | Elbow and wrist weights share the continuous arm and sleeve artwork. The pelvis carries the legs. Knee influence blends through pants, and ankle influence blends through socks and shoes. Foot tap is a small front-view gesture. |
| Hair and clothing | The ponytail and hanging waist ribbons have separate weighted pivots. The knot stays on the pelvis. Face overlays follow the head, the raised hood lining follows the head, and the lowered lining follows the torso. |

Every exported layer has an anatomical attachment below the scene root. Several movements use sampled shape changes, including the tail wave, brow shape and lip-sync mouth. The five source tail outputs can be compared as a combined path through **Tail swing phase**.

### Hand poses

**Finger curl and release** bends four fingers on each hand in succession. **Beckon** bends the right index twice while the other fingers stay curled. The **Fingers** inspector controls also work independently of wrist and elbow rotation. **Wrist greeting** opens and curls the waving fingers. **Breathing** adds a smaller, asynchronous settling movement to both hands. Neutral sliders restore the painted open pose.

The custom bends in [the hand profile](src/alexia-hands.ts) act on the registered bare and fingerless-glove meshes. Their offsets start past the knuckles, before wrist skinning. The thumb shares painted pixels with another finger and stays with the palm. A closed fist, thumb opposition or another palm angle would need additional artwork. The crossed-arm pose has its own baked hands and disables the open-hand controls.

### Overlays and gestures

Choose **Puzzled tilt** and scrub to 0.48 seconds to see the question mark reach full strength. Aiko holds the head tilt before the symbol fades. Use **Face overlays** in the inspector to pin a symbol, shadow, blush, tears, eye style or either purple iris. **Accessories** holds the lollipop, glasses on the face and sunglasses on the head. To combine a manual overlay with motion, choose the motion first, then pin the slider and press Play.

| Artwork | Source channels | Behavior |
|---|---|---|
| Question mark, sweat drop, angry shadow | Param43, Param44, Param57 | Each fades and changes position as its strength rises. |
| Purple irises | Param62 (character's left), Param63 (right) | The colors mix independently. Stars retain the tint. Spirals cover both irises. Returning to natural or stars restores the pinned tint. |
| Lollipop and glasses | Param60, Param64 (face), Param11 (head) | Independent accessory strengths can be combined with facial expressions and wardrobe choices. |
| Nod yes, Shake no | ParamAngleY, ParamAngleX | The pose tracks use the timing and angles of the installed nod and shake. Puzzled tilt is a studio-authored combination. |

The installed `dh` clothing motion drives Param27 from 0 to 10. A direct sweep in unit steps produced no visible mesh change in any of the three wardrobe states. [Visual observations](../../docs/Alexia-my-observation.md) · [Model notes](../../docs/alexia-model-notes.md)

## Extend the format

- [Calibration](src/alexia.ts) contains public units, sampled knots, source mappings, wardrobe options and clips. The sampler and reference pane share that mapping.
- [Anatomy](src/anatomy.ts) registers the custom skeleton and wrist weights in neutral artwork coordinates. Its landmarks and source groups belong to Alexia. For a new character, use the same runtime and fit that character's own landmarks.
- [Surface data](src/surface.ts) describes each triangle mesh. Every coordinate pair in a shape key is a displacement from the corresponding neutral vertex. Shape contributions add. A wardrobe-specific pose may replace the base positions, opacity, order and shape keys for that layer. A single skin influence or an array of influences can share a vertex. The layer joint retains its unassigned weight.
- [Rendering](src/surface-renderer.ts) uses textured triangles, alpha masks, inverted masks, draw order, and normal, additive or multiply blending. WebGL 2 enables mipmapping for the cropped textures. A WebGL 1 fallback uses linear filtering.
- [The project skill](.agents/skills/extend-atlas-persona/SKILL.md) describes how to choose, fit and verify a new control. The separate [artwork skill](.agents/skills/prepare-persona-art/SKILL.md) covers generating new registered artwork.

## Compare reproducibly

With the private rig installed, the motion check also compares overlay opacity across all 213 meshes and all three wardrobes, including mixed strengths and eye styles. The browser check saves a face gallery in `overlays.png` when given `--output`.

```powershell
npm --prefix tools/rig-studio test
node tools/rig-studio/sample-atlas.mjs data/personas/active/Alexia/Alexia.model3.json tools/rig-studio/private/aiko/rig.json --check-motion
.venv\Scripts\python.exe tools/rig-studio/test/atlas_smoke.py --browser "C:\Program Files\Google\Chrome\Application\chrome.exe" --output tools/rig-studio/private/pose-review
.venv\Scripts\python.exe tools/rig-studio/test/reload_smoke.py --browser "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

The paired check captures the custom pane on the left and the installed reference on the right. It compares silhouette overlap and foreground color error across outfits and poses. With `--output` it also saves a two-second `tail-swing.gif`. The motion check measures the maximum tail vertex error between sampled poses, checks the loop endpoints for identical geometry, and measures grin geometry and opacity with the jaw open. Browser checks exercise the weighted limbs, ponytail, ribbons, pointer gaze and backward stepping. The reload check uses a disposable fictional character and exercises a failed compile, its recovery, a file-backed rig edit and restoration of a picked folder.

The output is an editable reconstruction of sampled shapes. Combined extremes such as simultaneous full yaw, pitch and blink can need additional joint shape keys. Hair inertia and integration into Aiko's production avatar remain further work.
