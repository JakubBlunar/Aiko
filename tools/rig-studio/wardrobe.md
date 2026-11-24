# Wardrobe authoring

The wardrobe workspace has one body, one skeleton and a list of costumes. The **Outfit** selector includes **Body fitting**, **Teal studio dress**, **Casual black** and the two pajama hood positions. Select the dress and play **Foot tap** to see its hem trail the leg shift. **Wrist greeting** tests the sleeve at the elbow. Drag **Skirt swing** to pose the fabric by hand.

## Open the workspace

The workspace opens in **Casual black**. **Finger curl and release** and the **Fingers** sliders use the original open-hand artwork in casual and both pajama states.

Run these commands from the repository root. The preparation step reads the local atlas rig described in [the atlas workflow](atlas-workflow.md). It requires a fresh destination so it can preserve edited characters.

```powershell
node tools/rig-studio/build.mjs
node tools/rig-studio/wardrobe.mjs prepare tools/rig-studio/private/aiko tools/rig-studio/private/aiko-wardrobe
npm run avatar:wardrobe
```

For an existing wardrobe, run just `npm run avatar:wardrobe` and open [the local wardrobe studio](http://127.0.0.1:5177). With the reference enabled, custom costumes compare the same pose against Alexia in casual black. Edits to the body, a costume definition or its textures reload from disk. For **Open rig folder** or another JS player that expects a single manifest, rebuild the portable `rig.json` after any definition edits:

```powershell
node tools/rig-studio/wardrobe.mjs compile tools/rig-studio/private/aiko-wardrobe
```

## What belongs to the body

`body.json` owns the joints, rest coordinates, meshes, vertex weights, pose controls and shared motions. The head, neck, bare hands and forearms, exposed abdominal art, ears, hair and tail use the installed artwork. The upper-arm joins, torso under the shirt, legs and feet are reconstructed fitting meshes. Their neutral textures include opaque athletic coverage. They establish a front-view silhouette for garment authoring. Deep bends, other viewing angles and uncovered areas requiring finished illustration need additional art.

The original clothes include baked details such as a glove painted onto a hand, skin at the edge of a shoe, and shading that only belongs under one sleeve. The stock costume files retain those pieces together. A stock costume can use `hideBody` for covered fitting pieces and `bodyPoses` for original registration and visibility, including hair under the hood. New costumes start from the shared fitting body. Its texture files live in `body/`. The stock garment textures live in `stock/`.

## Add a garment

1. Choose **Body fitting** and **Rest pose**, then use **Export pose PNG** for a transparent 1125 × 2048 drawing reference. Export additional held poses to check hems, cuffs and shoulders. All patterns use the same neutral character coordinate system. A fitted texture uses the attachment's UV layout, which can differ from the full-body drawing.
2. Put the new garment textures in their own folder inside the wardrobe. Add a JSON file under `costumes/` and append its path to `wardrobe.json`. Give it an unused numeric value. This example reuses the torso mesh and UVs. Paint the top in the UV space of `body/torso.svg` with transparent pixels outside its coverage.

   ```json
   {
     "value": 5,
     "label": "Fitted shirt",
     "pieces": [{
       "id": "shirt",
       "label": "Shirt front",
       "attach": "torso",
       "src": "shirt/front.png",
       "order": 76
     }]
   }
   ```

3. For a different silhouette, supply `mesh.xy` in neutral artwork pixels, `mesh.uv` as normalized texture coordinates and `mesh.indices` as triangle vertex indices. The fitter projects each vertex onto an attachment triangle and interpolates its deformation offsets and skeletal weights. A vertex outside the attachment follows its closest edge. The body declares named attachments for the torso, abdomen, neck and each shoulder, arm, leg and foot. The dress sleeves use `arm.left` and `arm.right`, which carry elbow and wrist weighting through a cuff. Left and right are Aiko's own sides.
4. Give front panels, back panels and sleeves distinct orders. A higher order draws later. Use `masks` to clip a piece to another piece's alpha, or `inverted: true` to cut out that area. The demo dress has a back lining, front skirt, fitted bodice, clipped embroidery and two sleeves. Its JSON and textures are independent of the body.
5. Give a loose panel a costume parameter and a `shapes` track. Store offsets per vertex at each key and include the neutral key. Body deformation and these offsets add together. Inspect `costumes/studio-dress.json` for a hem whose waist stays fixed and whose swing increases toward the bottom edge.

### Costume motion

A costume can supply `defaults` for existing parameters, additional named `parameters`, `clips` and `springs`. Default values yield to clip tracks and manual pins. A spring declares its input track, output parameter, gain, frequency in hertz and damping ratio between zero and one. The runtime computes the lag of an underdamped spring driven by the clip's piecewise-linear input. Pausing, stepping backward and seeking directly to a time produce the same shape. A manual pin on the output gives the artist control of that channel. Live pointer or slider input does not currently generate a spring history.

### Verify a wardrobe

Run `npm --prefix tools/rig-studio test`. To compare the three stock looks under a held wrist pose, start the original atlas studio on 5176 and the wardrobe studio on 5177, then run:

```powershell
.venv\Scripts\python.exe tools/rig-studio/test/wardrobe_smoke.py --browser "C:\Program Files\Google\Chrome\Application\chrome.exe" --original-url http://127.0.0.1:5176
```

The browser check also switches all five outfits, exports the fitting PNG, poses the dress sleeve and checks hem motion after seeking. Pass `--output tools/rig-studio/private/pose-review/wardrobe.png` to save a comparison sheet and the transparent fitting guide. The generated workspace and renders stay in the ignored private directory.
