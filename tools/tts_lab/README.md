# TTS audition lab

A prototyping sandbox for choosing Aiko's next speech engine. Nothing
here is imported by `app/` — it exists to try candidates against her
actual voice and our actual pipeline constraints without touching the
production audio path.

Candidate list and the reasoning behind it:
[`docs/tts-engine-options.md`](../../docs/tts-engine-options.md).

## Start here: her voice is not backed up

Aiko's voice exists in exactly two places on this machine —
`voices/aiko1.safetensors` and `voices/aiko1_refined.safetensors` — and
both are **pocket-tts internal speaker states, not audio**. There is no
source recording anywhere in the repo.

Two consequences, and the second is the urgent one:

1. Every candidate engine clones from a *clip*, so without one there is
   nothing to audition against. The voice cannot move engines.
2. If pocket-tts ever stops loading, her voice is gone. The whole reason
   for this evaluation is that Torch is a crash surface, which makes
   "her voice is recoverable only by running the thing we want to
   replace" a bad place to be sitting.

So the first command produces a plain WAV, which is a backup and a
universal cloning source at the same time:

```bash
python -m tools.tts_lab.voicebank --roundtrip
```

That renders a phrase set through the live embedding, drops anything
that fails a quality check, and concatenates ~24 s into
`voices/reference/aiko_reference.wav`. `--roundtrip` additionally speaks
one held-back phrase three ways — from the original embedding, from the
new reference, and from a single 3-second part — so the cost of the
bootstrap is audible before anything gets built on top of it.

**Listen to those three first.** Re-cloning from generated audio loses a
generation of codec and sampling noise. If `from_reference` is clearly
worse than `original`, the honest conclusion is that the voice wants
re-recording rather than bootstrapping, and it is much cheaper to learn
that now than after installing four engines.

## Auditioning

```bash
python -m tools.tts_lab.bench --open
```

Runs every registered engine over a phrase set chosen to trip the usual
failures (a question, an exclamation, a long sentence, times and
numbers, an ellipsis), writes WAVs to `voices/audition/`, and builds a
self-contained `index.html`.

**The page is blind by default.** Labels are hidden and clip order is
shuffled per phrase, because the goal is to keep her current voice and
that makes the incumbent impossible to judge fairly once you know which
clip it is — the familiar one sounds correct by definition. Vote first,
reveal after. Votes persist in `localStorage`.

The page also prints two tables that matter more than the audio:

- **Control surface** — which `ProsodyParams` channels each engine can
  actually carry. This is the selection criterion, not sound quality:
  `gain_db` and the pauses are post-processing and survive any swap,
  while `speed_hint`, `reaction` and `prefix_reaction` need the engine's
  cooperation and are the reason the cadence layer's speed channel is
  dark today.
- **Declared capabilities** — with a `verified` row, because published
  RTF and "runs on CPU" claims are the most optimistic numbers in any
  model card.

## Cloning and testing a voice

```bash
python -m tools.tts_lab.serve --open        # http://127.0.0.1:6280
```

Record a reference in the browser (or drop in a 16-bit WAV), clone it
into any installed engine, audition a phrase, and save it to `voices/`.
It reads out the clip's quality numbers as you record and shows the same
phrase set `voicebank.py` uses as a script to read, so the reference
covers her range rather than being thirty seconds of one flat sentence.

Saving means one of two things. For pocket-tts it exports a real speaker
embedding via `export_voice()` — the same `.safetensors` the app loads
today, and the call the deleted Qt dialog used to make. For everything
else the engine clones per call from a clip, so the clip *is* the voice
and it gets copied into `voices/`.

Capture is raw PCM through the Web Audio API rather than
`MediaRecorder`, which would hand back WebM/Opus and need ffmpeg to
decode for the one job of writing a WAV. The app already works this way
for voice mode.

Loopback-only by default: it takes microphone audio and writes into
`voices/`.

## Candidate environments

Candidate engines pin dependencies that would wreck the app's venv —
Chatterbox alone wants **torch 2.6.0 over our 2.10.0**, plus
`transformers 5.2`, `safetensors 0.5.3`, `pandas`, `numba` and `gradio`.
So each engine gets its own venv under `.venvs/`, and the bench talks to
it over a subprocess (`sidecar.py` on the far side, `remote.py` on this
one). The `Adapter` seam hides the difference, so the bench cannot tell
a local engine from a remote one.

```bash
python -m tools.tts_lab.envs list
python -m tools.tts_lab.envs install chatterbox       # Turbo + original
python -m tools.tts_lab.envs install chatterbox-git   # master, for Nano
python -m tools.tts_lab.envs remove chatterbox        # uninstalls entirely
```

Two traps found the hard way, both recorded in `envs.py`:

- **`setuptools<81` is load-bearing** for Chatterbox. `resemble-perth`
  imports `pkg_resources`, setuptools 81 removed it, and perth swallows
  the ImportError and leaves its watermarker class as `None` — which
  then fails six frames deeper as `TypeError: 'NoneType' object is not
  callable`.
- **PyPI trails the README.** `chatterbox-tts` 0.1.7 has no
  `nano=True`, so Nano needs the git env. The sidecar detects this by
  introspection and says so, rather than passing the argument and
  letting a `TypeError` escape a subprocess.

## Adding an engine

1. Write a class in `adapters.py` (or its own module beside it)
   subclassing `Adapter`, with a `Caps` describing what it can be
   *told*. Fill `Caps` from upstream docs and leave `verified=False`
   until you have confirmed it here.
2. Implement `_load`, `voice_from_reference`, and `synth`. Optionally
   `voice_from_id`.
3. Register the factory in `REGISTRY`.

If the engine's dependencies conflict with the app's — assume they do —
subclass `Remote` instead and add the engine to `sidecar.py`'s registry
and `envs.py`'s. Keep imports lazy either way: import the engine's
package inside `_load`, not at module scope, so a missing dependency
shows up as one line in the report's "would not run" list instead of
breaking the whole bench.

Rules worth honouring, all learned the hard way here:

- **Discard a warmup generation before timing.** Chatterbox Turbo
  reports RTF 3.93 on its first call and 1.5 on its second. Timing the
  first would have rejected it on a number 2.5× worse than the truth,
  and the effect size differs per engine so it cannot be corrected for
  afterwards.
- **Run an engine at its own defaults**, read off the installed code, not
  at numbers from a README. Turbo defaults to `exaggeration=0.0,
  cfg_weight=0.0` while every published tip says 0.5 / 0.5 — because the
  tips are about a different model in the same family.
- **State the thread count.** "3× realtime on 8 cores" is not
  reproducible without it. (It turned out not to be the lever here:
  Turbo is 1.51 at 16 threads and 1.67 at 8.)

- **Do not emulate a capability the engine lacks.** If there is no
  native rate control, ignore the `rate` argument and leave
  `native_rate=False`. A silent emulation (detuning, for instance) is
  exactly the confusion this lab exists to remove.
- **Distinguish streaming generation from chunked playback.** They look
  identical from the consumer's end and are completely different
  properties. `streams_generation` means audio starts arriving before
  the utterance finishes. The incumbent chunks a *finished* clip, which
  is why its first-audio latency for one long sentence is over two
  seconds despite emitting 50 ms frames.

## What is not here yet

- **A pitch-preserving time-stretch stage**, which is the fix that lights
  up the cadence layer's dark speed channel regardless of which engine
  wins. See the options doc; it has to work on ~50 ms chunks, which rules
  out the convenient offline helpers.
- **Streaming through the sidecar.** The protocol is request/response, so
  a remote engine's first-audio figure is its whole clip latency. That is
  the harness's limit, not the engine's, and it is only worth fixing for
  an engine that survives the first audition.
- **The other candidates** in the options doc: PocketTTS.cpp (our exact
  model on ONNX, which would delete PyTorch from the audio path and keep
  the voice bit-for-bit), Qwen3-TTS via its C runtime (the best control
  surface on the list), MOSS-TTS-Nano, TTS Lite.
