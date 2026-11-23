---
name: animate-persona-rig
description: Rig and animate a layered persona in Rig Studio with named parameters, weighted meshes, expressions, wardrobe layers and deterministic frame comparisons.
---

# Animate a persona rig

Start with [the format and runtime guide](../../../README.md#rig-and-animate), [the TypeScript types](../../../src/model.ts) and [Pip's working rig](../../../example/rig.json). Preserve the editable artwork and current rig when trying a new bind pose.

1. Identify the anchor for each part in neutral artwork pixels. Set the root pivot in canvas pixels and each child pivot relative to its parent. An image's top-left offset is measured from its joint. Draw order belongs to layers and is independent of bone order. In the inspector, move a pivot on Rest pose, show the skeleton and export the edited rig.
2. Expose an anatomical or expressive control with a stable ID such as `head.tilt`, `eyes.open` or `hand.left.wave`. Label it for the person operating the UI. Record units, minimum, neutral and maximum. Assign low, neutral and high driver keys on each affected joint. Verify each endpoint with every other parameter at default.
3. For continuous motion across a tail, sleeve or lock of hair, start with a short bone chain. Add image-local mesh vertices on both sides of each bend, triangulate the silhouette, and blend weights gradually across the joint. Each vertex's weights sum to one. Inspect both sweep directions with the wireframe on. Fix pinching with vertex placement or weights. Add joints when the silhouette needs another independent bend.
4. Build expression clips as timed tracks of existing controls. For delight, decide which features rise together and which settle later. A blink needs a brief closing phase between fully open keys. For a loop, match both endpoint poses and inspect the speed on either side of the seam. The inspector's manual slider values override motion tracks until reset.
5. Put garment variants on the same underlying skeleton. Use opacity drivers on separate layers or groups. Check the collar behind the head, the sleeves over the arms and the tail behind the hem. For a hard swap, author an abrupt transition with closely spaced keys or choose the exact endpoints manually.
6. In the studio, pause and sample the motion at neutral, maximum displacement and recovery. Check reverse scrubbing and single-frame steps. With a compatible Live2D reference loaded, compare shared parameters at the same time. Report the clip, seconds, parameter values and specific mismatches. Test custom-only gestures against the intended silhouette and joint limits.

Use the headless smoke check in the studio guide when changing the renderer. Keep personal comparison assets in `private/`.
