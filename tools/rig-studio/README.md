# Rig Studio

[Wardrobe authoring](wardrobe.md) covers Aiko's fitting body, interchangeable costume files, mesh registration and cloth motion. Launch the prepared workspace with `npm run avatar:wardrobe` from the repository root.

Build and inspect a layered 2D character in a standalone browser workspace. The TypeScript runtime renders cutout rigs with Canvas 2D and deformable atlas rigs with WebGL. The optional reference pane uses Aiko's installed Pixi and Cubism libraries. The application backend can stay stopped.

From the Aiko repository root, run `npm run avatar:studio` and open [Rig Studio](http://127.0.0.1:5176). Stop its terminal with Ctrl+C. The launcher compiles the studio with the installed web TypeScript compiler. For a standalone copy, install the subproject's dev dependencies and run `npm start` inside it. The example and custom runtime work without Cubism.

## Compare a pose

1. The studio opens the local Aiko reconstruction when present and otherwise opens Pip. Use the Character menu to switch. Press Play, pause, drag the timeline or step by 1/30 second. A slider pauses the timeline and pins that parameter. Reset overrides releases all pins. Selecting a different clip resets time and overrides.
2. Load reference uses the installed `data/personas/active/Alexia/Alexia.model3.json`. To choose another local Cubism 3+ reference, run `npm run avatar:studio -- --reference "F:\Models\Character\Character.model3.json"`. Use `--port 5182` to change the port. The server binds to loopback and serves only the reference mesh and its listed textures alongside the studio's public files.
3. Read the mapping below each slider. Aiko uses [measured controls](src/alexia.ts) for gaze, head and ear motion, breathing, tail, clothing and expressions. Her new wrist bends are custom-only. Pip uses the [standard reference bindings](src/reference.ts). Each pane fits its neutral canvas into its viewport.
4. Both panes show the sampled pose at the timeline time. The reference receives the mapped values directly. Its automatic idle, physics and expression mixer are disabled to make backward seeking repeatable. The existing [avatar playground](../../docs/avatar-playground.md) runs the production avatar behavior.

### Layer a gesture over idle

Aiko opens on **Layered idle**. Press **Play** to see a quiet four-second breath, a light six-second tail sway, and staggered three-second finger curls. Select **Wrist greeting** during playback to wave while the idle continues. The gesture plays once per twelve-second preview and eases back into the idle. Clear **Layer with idle** to inspect the original clip on its own timeline. **Rest pose** gives you a still model for manual posing.

Open **Idle layers** in the inspector to adjust the strength of each motion during playback. Set a layer to 0% to mute it. **Export rig.json** includes the edited mix.

Select **Tail swing** during idle to add one six-second turn from the tail's current position. Switching to a greeting lets the turn finish while the hand moves. Solo playback shows the original two-second turn.

A rig can declare an optional `idleMotion` with a preview `duration` in seconds and an ordered list of `layers`. Each layer names a clip and may set a `weight` from 0 to 1 and a phase `offset` in seconds. Choose a preview duration containing whole cycles of each background clip and author matching start and end poses for those clips.

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

For layered playback, keep the idle clock running across gestures. Supply the elapsed seconds since each gesture started separately. For example, inside the app's animation tick:

```ts
import { gestureLayer, idleLayers, sampleLayers } from './dist/runtime.js'

const layers = idleLayers(rig, elapsedSeconds)
if (activeGesture) {
  layers.push(gestureLayer(activeGesture.clip, elapsedSeconds - activeGesture.startedAt))
}
renderer.render(sampleLayers(rig, layers, manualOverrides))
```

The mixer reads layers from lowest to highest priority. Each clip affects only its keyed parameters. Weight blends continuous values, and a weight of at least 0.5 selects a discrete option. Manual pins take priority, then outfit and pose gates resolve. For a sustained layer, pass `{clip, seconds, weight}` directly. A one-shot from `gestureLayer` eases in and out over 0.18 seconds by default.

For a cyclic parameter whose minimum and maximum draw the same pose, a clip can declare `"phaseGesture":{"parameter":"tail.curl","duration":6}`. The track must traverse the full parameter range. During layered playback the mixer adds that turn to the underlying phase and eases its speed at both ends. Keep sampling an in-progress turn through its gesture duration when starting a gesture on another body part.

### Compatibility with Aiko's expression channels

The [Rig Studio adapter](../../web/src/live2d/RigStudioAdapter.ts) accepts the existing app channels' expression names, Cubism parameter writes, focus calls and motion group/index commands. It reads Alexia's expression bindings from the same avatar manifest as the app. For example, a cheerful reaction selects `lzx` and writes `Param54 = 30`, which corresponds to `face.grin = 1`. The Live2D reference pane translates poses in the opposite direction. The main app still mounts its Pixi player.

Give the adapter the same engine state and millisecond clock as the channels. Start the engine once. On each render tick, sample one frame and pass the resulting pose to the custom renderer.

```ts
const adapter = new RigStudioAdapter(rig, manifest, engineState, now)
engine.start(adapter)
// Inside the render tick, nativeLayers can include a wrist gesture and the idle.
renderer.render(adapter.sampleFrame(nativeLayers))
```

Omitting the layer argument uses the rig's configured idle. The app's tail-wag action adds one native turn that finishes independently of the overlay deadline. Named motion groups resolve to matching authored clip names, including Alexia's nod, shake and bow. Expression-slot replacement, pulse expiry and speech suppression use the app's existing channels. Pre-model writes expire after each rendered frame. The adapter releases the custom mouth gate when the speaking grin drops below 5%.

All 16 installed Alexia expression files have corresponding full-strength poses. Star eyes, spiral eyes, both pajama expressions and crossed arms use discrete controls, so their intermediate poses differ from Cubism's fades. Gaze uses the rig's eye controls and eyelid openness is shared. Other model profiles and arbitrary Cubism motion files require their own bindings or authored clips.

With the wardrobe Studio running, replay the app actions against the local model from the repository root. The replay registers all twelve channels in app order and checks 211 rendered frames through the tail's return to idle.

```powershell
.venv\Scripts\python.exe tools/rig-studio/test/channel_replay.py --browser "C:\Program Files\Google\Chrome\Application\chrome.exe" --output tools/rig-studio/private/pose-review/expression-channel-replay.gif
```

## Project skills

- [Prepare persona art](.agents/skills/prepare-persona-art/SKILL.md) covers neutral masters, registered AI edits, occlusion repair, cuts and masks.
- [Animate a persona rig](.agents/skills/animate-persona-rig/SKILL.md) covers pivots, weights, expression timing, wardrobe and frame comparison.
- [Extend an atlas persona](.agents/skills/extend-atlas-persona/SKILL.md) covers measured deformers, artwork registration, compatibility between controls, and reference comparison.

## Verification

Run `npm --prefix tools/rig-studio test` for strict TypeScript compilation and runtime regressions. Run the cutter tests with `.venv\Scripts\python.exe scripts/run_tests.py tools/rig-studio/test/test_cut.py`. For a headless browser check after building, run `.venv\Scripts\python.exe tools/rig-studio/test/smoke.py --browser "<path to Chrome>" --reference`. Omit `--reference` for an installation without the purchased model. The smoke check starts and stops its own loopback server on a separate port.
