# Live2D web runtime

Three files live here, all fetched by `scripts/fetch_live2d_core.py`:

| File | Source | License |
|---|---|---|
| `pixi.min.js` | https://pixijs.com/ | MIT |
| `pixi-live2d-display.min.js` | https://github.com/guansss/pixi-live2d-display | MIT |
| `live2dcubismcore.min.js` | https://cubism.live2d.com/sdk-web/cubismcore/ | [Live2D Proprietary Software License](https://www.live2d.com/eula/live2d-proprietary-software-license-agreement_en.html) |

`live2dcubismcore.min.js` cannot be redistributed in this repository — it is
downloaded on demand. If it is missing the persona panel will quietly disable
itself.

Run:

```
python scripts/fetch_live2d_core.py            # libs + core
python scripts/fetch_live2d_core.py --sample   # also fetch Hiyori sample model
```

Place additional models under `data/avatars/<name>/<name>.model3.json`.
