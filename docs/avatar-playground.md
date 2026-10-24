# Avatar playground

A permanent version of the mist preview, using the production Live2D renderer
and the selected model's real capabilities. Start from the project directory:

```powershell
npm run avatar:playground
```

Open [the playground](http://127.0.0.1:5174/avatar-playground.html).
Node.js, the project's Python environment and installed web dependencies are
required, just as for local development. The chat backend need not be running.

The presets recreate warm/excited, tender, thoughtful and playful looks, plus
warm/uncertain and winding-down states. **Cycle showcase** changes the preset
every eight seconds. **Presentation view** hides the controls.

For manual studies, adjust lingering mood, reaction, intensity, arousal,
valence, mist, avatar size and body language. Speaking and mouth-opening
controls are silent visual previews; they do not synthesize or play audio.
The model section lists its loaded expression files, available outfits,
overlays and motions. Raw expressions still obey the renderer's outfit gates.
**Reset preview** restores the initial state and reloads the renderer.

## Another model

Point the launcher at a local folder containing a Cubism `.model3.json` or
`.model.json`, together with its referenced textures, motions and expressions:

```powershell
npm run avatar:playground -- --model "F:\Models\AnotherAvatar" --name "Another avatar"
```

Use `--port 5194` to run another instance alongside the first. Capabilities
come from the same profile detector used by the app; the playground is useful
for seeing what an integration exposes, but does not add unsupported rig
parameters or guarantee that an arbitrary model has the same capabilities as
Alexia. No model files are copied into the repository.

The server binds to `127.0.0.1`. The playground has its own browser origin and
does not connect to chat, the WebSocket, a model provider or the database.
Preview controls update only this page's state. The mist switch is saved for
the playground origin. Normal app settings and model mappings stay separate.

Press **Ctrl+C** in its terminal to stop. The launcher owns a small generated
manifest under `.tmp-avatar-playground/run-*` and removes it on exit, including
normal interruption. After a forced process kill, remove only that run's
directory after confirming its server has stopped. The source page, controls
and launch command remain available for future sessions.
