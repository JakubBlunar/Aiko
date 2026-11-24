---
name: extend-atlas-persona
description: Reconstruct or extend an editable Rig Studio character from a locally installed texture atlas and posed mesh reference. Use for measured deformers, garment variants, weighted joints and paired pose comparisons.
---

# Extend an atlas persona

Find Rig Studio three directories above this skill. Start with [the atlas workflow](../../../atlas-workflow.md). For Aiko, read [the user's visual observations](../../../../../docs/Alexia-my-observation.md) and [the parameter and outfit notes](../../../../../docs/alexia-model-notes.md). The installed art and derived character live in ignored local directories.

## Identify the movement

- Start the studio, pause on Rest pose, and isolate a visible piece in each relevant outfit. Identify the attachment and movement from the character's perspective. Mesh group names can use the viewer's perspective.
- For a shipped behavior, run the influence sweep. Search the private report for the part group, then rank candidate parameters by pixel travel, opacity change or draw-order change. Inspect at least one negative and one positive endpoint where available. Empty effects for a named parameter are evidence about that isolated input. Physics may drive a different output parameter during playback.
- Check the raw parameter range, default and affected neighbors. Changing one ear should leave the other ear stable. Check that chest rise carries nearby clothing. Check where the iris meets the eyelid while looking diagonally. Compare with a held frame in the running reference for each outfit in which the artwork is visible.

## Author the control

For interchangeable clothing, follow [Wardrobe authoring](../../../wardrobe.md). Start from the transparent fitting pose. Identify which skin is present in the atlas and which joints need reconstructed coverage. Author the garment in its own costume file. Check a fitted piece against the body at the elbow and chest, and check loose pieces at a held frame and on return from motion.

- For reference-derived shapes, add one public control and its raw knots to `src/alexia.ts`. Rebuild to a fresh private folder. Inspect the resulting piece, mask IDs and local UVs in its inventory. The exporter measures each wardrobe state relative to its own neutral shape. Keep stable public names and place source IDs in the adapter or import tooling.
- For original movement, decide what must bend. Use a bone for a rigid piece. Use a partial second-joint weight across a continuous wrist, or sampled vertex offsets for a changing silhouette. Paint a registered replacement PNG if the requested movement exposes missing ink or fabric. Keep the image dimensions and UV mapping together. A new character with different landmarks needs its own calibration and anatomy profile.
- Put compatibility conditions in the rig. Give hard swaps named choices. In Aiko's case, compare arms crossed in casual clothing and relaxed in pajamas. Check mouth opening with the toothy grin selected. Document which behaviors are custom-only.
- Use the file-backed local character for live asset edits. Pause, pin the test pose, edit one PNG or the rig JSON, and let the watcher reload. For a new sampler or calibration change, regenerate into a fresh folder and launch the server with `--rig` pointing at it.

## Verify and hand off

Run the unit tests and the paired browser check in the atlas workflow. Compare a neutral, an endpoint, a combined pose and a recovery frame. For a subtle movement, inspect a face or joint crop at larger scale in addition to the whole-image metric. Custom-only controls need their own deformation check. Record which outfit, timestamp, raw sources and public values produced each finding. Put private renders and numeric reports beside the local character, and document transferable findings in the workflow.
