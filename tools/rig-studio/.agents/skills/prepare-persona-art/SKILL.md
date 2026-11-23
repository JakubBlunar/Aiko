---
name: prepare-persona-art
description: Generate or separate neutral character artwork into registered, transparent layers for Rig Studio. Use for an original persona, a costume, or a pose-matched replacement part.
---

# Prepare persona artwork

Find the studio root three directories above this skill. The image cutter, recipe example and folder conventions are described in [the studio guide](../../../README.md#cut-artwork). Use a new directory under the studio's ignored `private/` for personal references, drafts and comparison renders.

1. Identify the requested movement before generating art. For a small head tilt one head layer can suffice. A blink needs independent lids or eye artwork, an opening mouth needs an interior, and a waving arm needs shoulder, elbow and wrist overlap. Mark left and right from the character's perspective.
2. Establish one approved neutral master. Record canvas dimensions, palette, face landmarks, line weight and lighting direction. Keep an unobstructed version of the face, torso and limbs wherever clothing or hair can move. Preserve this master for future variants.
3. For image generation, describe the registered edit and its acceptance condition. For example, “Using the supplied neutral front view, paint the left sleeve on a transparent canvas of the same dimensions. Match the shoulder seam at (x, y), the cuff at (x, y), the warm upper-left light and the ink thickness. Continue the upper sleeve underneath the shoulder. Leave everything outside the sleeve transparent.” Substitute actual coordinates. Overlay each output on the master and correct drift before cutting.
4. Use the master with a grayscale keep-mask or polygon for visible pieces. Supply a full-canvas repaired layer via `image` wherever motion reveals hidden pixels. Paint a continuous patch through the joint and rotate the neighboring limb to its intended extreme to measure whether the overlap covers it. Extracting a silhouette cannot reconstruct an occluded surface.
5. Put parent pivots before children in the cut recipe. Give left/right parts distinct IDs and list the compositing order from back to front. Save grayscale masks at master resolution. Zoom in on light and dark backgrounds to find matte fringes, transparent pinholes and a duplicated outline at joints.
6. Run the cutter into a fresh folder. Open that folder in the studio, select Rest pose and compare against the neutral master. Keep the recipe, repaired plates, masks and editable source together so another artist can reproduce the same offsets. Hand the resulting `rig.json` and PNGs to the rigging workflow.

For a new persona, use its own neutral design and choose its controls from its intended motions. The bundled Pip example works independently of the optional purchased reference.
