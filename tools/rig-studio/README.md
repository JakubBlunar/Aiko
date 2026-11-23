# Rig Studio

Build and inspect a layered 2D character in a standalone browser workspace. The TypeScript runtime renders cutout rigs with Canvas 2D and deformable atlas rigs with WebGL. The optional reference pane uses Aiko's installed Pixi and Cubism libraries. The application backend can stay stopped.

From the Aiko repository root, run `npm run avatar:studio` and open [Rig Studio](http://127.0.0.1:5176). Stop its terminal with Ctrl+C. The launcher compiles the studio with the installed web TypeScript compiler. For a standalone copy, install the subproject's dev dependencies and run `npm start` inside it. The example and custom runtime work without Cubism.

## Compare a pose

1. The studio opens the local Aiko reconstruction when present and otherwise opens Pip. Use the Character menu to switch. Press Play, pause, drag the timeline or step by 1/30 second. A slider pauses the timeline and pins that parameter. Reset overrides releases all pins. Selecting a different clip resets time and overrides.
2. Load reference uses the installed `data/personas/active/Alexia/Alexia.model3.json`. To choose another local Cubism 3+ reference, run `npm run avatar:studio -- --reference "F:\Models\Character\Character.model3.json"`. Use `--port 5182` to change the port. The server binds to loopback and serves only the reference mesh and its listed textures alongside the studio's public files.
3. Read the mapping below each slider. Aiko uses [measured controls](src/alexia.ts) for gaze, head and ear motion, breathing, tail, clothing and expressions. Her new wrist bends are custom-only. Pip uses the [standard reference bindings](src/reference.ts). Each pane fits its neutral canvas into its viewport.
4. Both panes evaluate the **custom clip** at one time in seconds. The reference receives the mapped values directly. Its automatic idle, physics and expression mixer are disabled to make backward seeking repeatable. This comparison measures how a control changes the pose. The existing [avatar playground](../../docs/avatar-playground.md) runs the production avatar behavior.

Use **Open rig folder** to load a directory with `rig.json` and its images. The browser reads those files locally. **Edit joint pivot** shows parent-relative coordinates. Export the result as `rig.json` alongside the original layer files. Reloading the page discards unexported edits. The current purchased model and any work under `tools/rig-studio/private/` are gitignored. Keep private artwork, masks, generated variants and captures there.

Use **Export neutral PNG** on the loaded reference for a transparent registration image, 2048 pixels on the longer side. The export uses the reference's default parameters and pauses playback. Save it in the ignored private directory as the starting plate for cutting or redrawing.

## Cut artwork

To reconstruct the installed character, edit an existing atlas piece, or test TypeScript and rig edits with hot reload, use [the atlas workflow](atlas-workflow.md). The flat-art cutter below also works for original artwork.

The cutter uses Python and Pillow (available in Aiko's project virtual environment). A recipe takes a neutral RGBA sheet and parts in parent-first order. Each pivot is an absolute point on the full canvas. For each part, use a polygon in sheet coordinates, a white-keeps/black-removes grayscale mask, or a full-canvas transparent replacement image. Masks and polygons can be applied to replacement images. The cutter trims transparency, preserves registration, and writes PNG layers and a starter rig. Choose a fresh output directory for each export.

For example, given `neutral.png` and `body-mask.png` on a 320 × 340 canvas, save the following as `cut.json` beside them. Replace the illustrative wrist coordinates with the actual silhouette. The body mask should include the painted area behind the wrist.

```json
{
  "name": "My character",
  "sheet": "neutral.png",
  "parts": [
    {"id":"body", "pivot":[160,220], "mask":"body-mask.png"},
    {"id":"paw.left", "parent":"body", "pivot":[206,184],
     "polygon":[[199,181],[212,178],[222,206],[205,211]],
     "drives":[{"param":"hand.left.wave","channel":"rotation","keys":[[0,0],[1,-48]]}]}
  ],
  "drawOrder":["body","paw.left"],
  "parameters":[{"id":"hand.left.wave","label":"Left paw wave","min":0,"default":0,"max":1}],
  "clips":[{"id":"wave","duration":2,"tracks":{"hand.left.wave":[[0,0],[1,1],[2,0]]}}]
}
```

From the repository root on Windows:

```powershell
.venv\Scripts\python.exe tools/rig-studio/cut.py tools/rig-studio/private/cut.json tools/rig-studio/private/first-rig
```

An already separated transparent layer uses `"image":"left-sleeve.png"`. Every input plate shares the master canvas size. Pivots and masks are reproducible inputs. Hidden artwork, poses seen from another angle, and alternative garments require painted or generated source layers.

## Rig and animate

[The typed model format](src/model.ts) defines version 1. [Pip's rig](example/rig.json) is a complete editable example. Coordinates increase rightward and downward. Positive rotation is clockwise in degrees.

| Field | Interpretation |
| --- | --- |
| `parameters` | Stable public IDs, optional UI labels, ranges and neutral values. Names can be character-specific. |
| `bones` | Parent-first joints. Root coordinates are in canvas pixels. Children use parent-local pixels. A neutral `scaleX` of −1 mirrors a joint. |
| `drives` | A parameter maps through sorted piecewise-linear keys into translation, rotation, scale or opacity. Translation and rotation add. Scale and opacity multiply. |
| `layers` | Back-to-front image list. Each offset locates the top-left of the unscaled image relative to its bone. PNG, WebP, JPEG and SVG are supported. |
| `mesh` | Optional triangles indexing image-pixel vertices. Weights associate each vertex with named bones. The default parameter pose is the bind pose. See the tail mesh in Pip's rig. |
| `clips` | Duration in seconds and tracks keyed by parameter ID. Values interpolate linearly between keys and hold beyond each track's endpoints. Empty clips and parameter lists are supported for a static cutout. |

After `npm --prefix tools/rig-studio run build`, import the emitted ES modules in another browser application:

```ts
import { validateRig, loadImages, sample } from './dist/runtime.js'
import { createRigRenderer } from './dist/player.js'

const rig = validateRig(await (await fetch('/characters/pip/rig.json')).json())
const images = await loadImages(rig, src => '/characters/pip/' + src)
const renderer = createRigRenderer(canvas, rig, images)
const values = sample(rig, rig.clips[0], 1.25, { 'head.tilt': 8 })
renderer.render(values)
// On unmount or rig replacement:
renderer.dispose()
```

The caller owns time and the canvas. Use a fresh canvas when switching between Canvas 2D and WebGL contexts. `pose()` also exposes each joint's world transform and opacity. The atlas workflow documents the current reconstruction, its measured movements and the remaining differences in combined poses and physics.

## Project skills

- [Prepare persona art](.agents/skills/prepare-persona-art/SKILL.md) covers neutral masters, registered AI edits, occlusion repair, cuts and masks.
- [Animate a persona rig](.agents/skills/animate-persona-rig/SKILL.md) covers pivots, weights, expression timing, wardrobe and frame comparison.
- [Extend an atlas persona](.agents/skills/extend-atlas-persona/SKILL.md) covers measured deformers, artwork registration, compatibility between controls, and reference comparison.

## Verification

Run `npm --prefix tools/rig-studio test` for strict TypeScript compilation and runtime regressions. Run the cutter tests with `.venv\Scripts\python.exe scripts/run_tests.py tools/rig-studio/test/test_cut.py`. For a headless browser check after building, run `.venv\Scripts\python.exe tools/rig-studio/test/smoke.py --browser "<path to Chrome>" --reference`. Omit `--reference` for an installation without the purchased model. The smoke check starts and stops its own loopback server on a separate port.
