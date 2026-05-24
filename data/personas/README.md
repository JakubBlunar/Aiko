# Live2D personas

This folder is managed by `app/core/persona_manager.PersonaManager`. Only one
model is active at a time, extracted into:

```
data/personas/
  active/
    _persona.json          # manifest written by persona_manager
    <name>.model3.json     # entrypoint (Cubism 3+/4)  -- or *.model.json for Cubism 2.1
    <name>.moc3            # ditto for Cubism 2: <name>.moc
    textures/...           # textures listed in the entry JSON
    motions/...
    expressions/...
```

The FastAPI server mounts this folder at `/personas/`, so the React frontend
can fetch model assets at `/personas/active/<entry_filename>` once a model is
uploaded.

Upload a model from the React UI: **Settings → Persona avatar → choose a
.zip**.

## Licensing -- read this before deploying

### SDK runtimes (vendored under `web/public/live2d/`)

| File                              | Source     | License                                                            |
| --------------------------------- | ---------- | ------------------------------------------------------------------ |
| `live2dcubismcore.min.js`         | Live2D Inc | Cubism Core "Free Material License" -- personal / non-commercial.  |
| `live2d.min.js`                   | Live2D 2.1 | Same Free Material License (no longer downloadable from the official site). |
| `pixi-live2d-display` (npm)       | guansss    | MIT                                                                |
| `pixi.js@^6` (npm)                | PixiJS     | MIT                                                                |

The **Cubism Core SDKs** are free for personal / non-commercial use; for any
revenue-generating product see <https://www.live2d.com/en/sdk/license/>.

### Model assets

The models in `live-2d-models/` (and any zip you upload) are typically
extracted from games. The source repository's README states "do not use for
commercial purposes" and these models are usually not licensed for
redistribution.

**Practical rules of thumb:**

- Treat `data/personas/active/` as private. Do **not** expose the web app to
  the public internet with someone else's IP loaded -- bind FastAPI to
  `localhost` (which is the default).
- Do **not** commit the contents of `data/personas/active/` to git.
- If you publish screenshots / videos, credit the original artist when known.

A `.gitignore` at the repo root should exclude this folder's `active/`
contents already; if you've added one manually, keep it that way.
