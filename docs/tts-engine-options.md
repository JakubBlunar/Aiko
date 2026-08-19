# TTS engine options

Research notes for replacing or supplementing **pocket-tts**. Written
Aug 2026 while evaluating what it would take to give Aiko real control
over speaking rate and voice colour.

The transport side — how PCM reaches the browser — is
[`voice-mode.md`](voice-mode.md). This file is only about the engine
that produces the samples.

## Where we are

`app/tts/pocket_tts_service.py` wraps Kyutai's **pocket-tts**: a ~100M
parameter LM over the Mimi neural codec, 24 kHz mono, MIT-licensed,
CPU-only by design. Torch in this venv is `2.10.0+cpu`, so there is no
CUDA path even though the box has an RTX 5090 — and that is deliberate.
TTS on the GPU would land as bursts of compute between game frames; on
CPU it stays out of the way.

It does the important thing well: zero-shot voice cloning from a few
seconds of reference audio, which is where `aiko1_refined.safetensors`
comes from.

**It does not stream generation, and it reads as if it does.** The
service emits ~50 ms PCM chunks, but `_speak_worker` runs
`generate_audio` to completion and only then paces the finished array
out through `_emit_pcm`. So first audio waits for the last token:
measured on the 9950X3D, a 2.3 s utterance starts after 0.58 s and an
8.7 s one after **2.16 s**. Production stays responsive only because
text arrives a sentence at a time. Chunked playback is not chunked
generation, and an engine that genuinely streams would be a real
latency win rather than a like-for-like swap.

**Her voice is not backed up.** `voices/` holds two `.safetensors`
speaker states and no audio, so the voice is currently recoverable only
by running the engine we are trying to replace. `tools/tts_lab/voicebank.py`
extracts a portable reference WAV — both as a backup and as the cloning
source every candidate needs. Measured on this box: 24 kHz, RTF ~0.25,
model load ~2.3 s.

## The actual constraint

`TTSModel` exposes four knobs, all at load time:

```
load_model(config, temp, lsd_decode_steps, noise_clamp, eos_threshold)
get_state_for_audio_prompt(audio_conditioning, truncate)
```

There is **no speed, no pitch, no timbre parameter**. Everything about
how Aiko sounds — accent, timbre, and pacing — is entangled in the
reference clip.

This is not pocket-tts being lazy. It is inherent to this class of
model. An autoregressive LM over audio tokens conditions on the
reference prompt as a single acoustic context; pacing is not separable
from voice identity in that representation. **Any engine in this
category will have the same shape**, so "switch to a nicer engine" is
not by itself a route to a speed slider.

### What we do instead, and what it costs

Because there is no speed parameter, the service implements speed by
**scaling the playback sample rate** — a varispeed effect, tape-style,
that couples rate and pitch at roughly 1.6 semitones per 10% of rate.

Two features are gated off by default as a direct result
(`pocket_tts_service.py`, the `_runtime_*_enabled` flags):

- `agent.tts_runtime_speed_enabled` — silences the cadence layer's
  per-sentence `speed_hint` and the per-reaction speed sub-caps. With
  it on, affect-driven pacing pitch-couples to the affect channel and
  the voice audibly changes identity between sentences.
- `agent.tts_runtime_temp_enabled` — same story for runtime
  temperature variation.

So Aiko already has a cadence layer that derives per-sentence pacing
from affect, and **its speed channel is permanently dark** because the
only mechanism available to express it also detunes her.

This reaches the `[[prosody:…]]` tag. The overlay table in
[`cadence.py`](../app/core/voice/cadence.py) maps each label to a
`(speed_mult, gain_db_delta, pause_before_ms)` triple, and only the
speed column is gated:

| tag | speed | gain | audible today? |
|---|---|---|---|
| `whisper` | 0.97 | −6 dB | yes, via gain |
| `firm` | — | + | yes, via gain |
| `slow` | 0.95 | 0 dB | **no** |
| `fast` | 1.05 | 0 dB | **no** |

`slow` and `fast` have no channel other than speed, so Aiko can emit
`[[prosody:slow]]` today and nothing changes in the audio. Worth
noting the band is only ±5% — about 0.8 semitones of detune at the
coupling rate — so the gate may be more conservative than it needs to
be. `tools/tts_speed_ab.py` exists to listen-test before flipping it.

The user's static pacing slider (`assistant.tts_length_scale`) is
honoured regardless, since it is a deliberate constant rather than
per-sentence drift.

That is the concrete cost of the current design, and it is the thing
to fix first.

## Two fixes that need no engine change

**Speed — pitch-preserving time-stretch.** A WSOLA or phase-vocoder
stage on the PCM before it leaves the service decouples rate from
pitch. It is small, engine-independent, survives any future engine
swap, and it is what unlocks the cadence layer. This is the highest
value item on this page.

It has to work **incrementally on ~50 ms chunks**, which rules out the
convenient offline helpers (`librosa.effects.time_stretch` and
friends) — they want the whole clip and are slow. WSOLA-family
libraries suit speech better than a phase vocoder anyway. Watch the
licence: Rubber Band is excellent and has a realtime API but is
GPL/commercial dual, which is fine for personal use and awkward if
Aiko is ever distributed.

Full F0 resynthesis (WORLD-style analysis into pitch contour, spectral
envelope and aperiodicity) is the heavier hammer and worth *not*
reaching for first. We do not need arbitrary pitch contours; we need
rate that does not drag pitch along with it. Adding a full
analysis/resynthesis pass on top of an already codec-decoded signal
mostly buys artefacts.

**Voice colour — a library, not a dial.** Since timbre lives in the
reference clip, "colour" means curating several Aiko references
(brighter, softer, slower) and switching between them. `set_voice()`
already hot-swaps at runtime and clears the audio cache, so the
plumbing exists.

The UI for this now exists as `python -m tools.tts_lab.serve`: record or
upload a reference, clone it into any installed engine, audition it, and
save it — as a `.safetensors` embedding for pocket-tts (via the
`export_voice` that had been sitting in the service unused since the Qt
dialog was deleted) or as a reference clip for engines that clone per
call. It is deliberately a standalone tool: cloning loads candidate
engines with their own torch, and a prototype that can break should not
be able to break the thing Aiko talks through. If an engine wins, the
*narrow* version — pick from saved voices, no cloning — is what belongs
in the settings drawer.

## The selection criterion

The planner half of this is **already built**. Proposals to add a
"speech-expression worker" that turns affect into emotion / intensity
/ pace / pause values are describing
[`cadence.py`](../app/core/voice/cadence.py), which already emits
exactly that as `ProsodyParams` and already reads both the affect
channel and the `[[prosody:…]]` tag.

So the question is not "which engine is most expressive". It is
**which engine exposes a control surface that `ProsodyParams` can be
mapped onto**. An engine with a numeric intensity or rate parameter is
easier to drive from a float than one that wants a sentence of English
prose, and an engine with none at all — our current situation — leaves
the planner talking to itself.

A second, related payoff: Aiko already emits stage-direction earcons
(`[[laugh]] [[sigh]] [[gasp]] [[chuckle]] …`) that today trigger
sampled audio clips, and `audio.earcons_enabled` is currently **off**.
Any engine with native inline `[laugh]` / `[sigh]` support turns those
back on as vocalisations *in her own cloned voice* rather than canned
samples. That is probably the single most Aiko-shaped feature on this
page.

## Candidates

Verified against upstream Aug 2026. "RT" = realtime factor.

| Engine | Params | CPU story | Clones? | Control surface | License |
|---|---|---|---|---|---|
| pocket-tts *(current)* | 100M | Torch CPU, ~6× RT | zero-shot | **none** | MIT |
| Qwen3-TTS | 0.6B / 1.7B | via community C runtime | 3 s clone | emotion, rate, NL instruct | see repo |
| Chatterbox Nano | 110M | ~3× RT on 8 cores | yes | numeric, `[laugh]` tags | see repo |
| Chatterbox (full) | 500M | rough | zero-shot | `exaggeration`, CFG, temp | see repo |
| MOSS-TTS-Nano | 100M | **ONNX**, 1–4 cores | zero-shot | none documented | see repo |
| TTS Lite | 112M | ONNX CPU | no transcript | paralinguistic tags | Apache 2.0 |
| VoxCPM2 | 2B | **GPU** (see below) | controllable | style guidance, speed | Apache 2.0 |
| IndexTTS2 | — | GPU-oriented | zero-shot | 8-dim emotion vector | see repo |
| Kokoro-82M | 82M | fastest here | **no** | — | Apache 2.0 |
| XTTS-v2 | — | ~5× RT — too slow | yes | explicit speed | CPML |

### Measured here, 20 Aug 2026 (9950X3D, 16 threads, CPU)

Auditioned with `tools/tts_lab/`, every engine cloned from the *same*
reference clip so the comparison isolates the engine. Steady-state
figures — the first generation is discarded, because Turbo reports RTF
3.93 cold and 1.5 warm and the size of that effect varies per engine.

| engine | RTF | first audio, 2.3 s line | load | notes |
|---|---|---|---|---|
| pocket-tts | **0.24** | **~570 ms** | 2.2 s | incumbent |
| chatterbox-nano (110M) | 0.59–0.87 | ~1920 ms | 37 s cold | tags work |
| chatterbox-turbo (350M) | 1.37–1.66 | ~4150 ms | 8.9 s | slower than realtime |

Since **neither engine streams generation**, RTF *is* responsiveness:
first audio equals full generation. Nano beats realtime and would still
mean roughly a two-second pause before every sentence, against
pocket-tts's ~0.6 s. Upstream's "3× realtime on 8 cores" for Nano (RTF
0.33) is about twice as optimistic as this box measures.

Thread count is not the lever: Turbo runs RTF 1.51 at 16 threads and
1.67 at 8, so there is no configuration where it becomes viable.

Three practical findings worth keeping:

- **Chatterbox needs `setuptools<81`.** `resemble-perth` imports
  `pkg_resources`, setuptools 81 removed it, and perth's `__init__`
  catches the ImportError and leaves `PerthImplicitWatermarker = None`.
  The failure then surfaces six frames later inside model construction
  as `TypeError: 'NoneType' object is not callable`. This is almost
  certainly the "CPU loading bugs" this family is known for, and pinning
  setuptools is the fix — stubbing perth out would also work and would
  be us disabling someone's watermarking to save a pin.
- **PyPI is behind the README.** `chatterbox-tts` 0.1.7 has no
  `from_pretrained(nano=True)` despite the documented example; Nano
  needs a git install. Turbo *does* accept `exaggeration` and
  `cfg_weight`, but its defaults are `0.0 / 0.0`, not the `0.5 / 0.5`
  every published tip quotes — those tips are about the original model.
  Benchmarking Turbo at 0.5 would have auditioned a configuration its
  authors did not choose.
- **A longer reference is not a better one for pocket-tts.** The speaker
  state keeps the whole clip, so file size tracks clip length: a 27 s
  reference produced a 16 MB `.safetensors` against `aiko1_refined`'s
  4.8 MB, with no measurable generation-speed difference.
  `get_state_for_audio_prompt` takes `truncate: bool = False`.

### Is generated audio good enough to *train* on?

Different question from cloning, and it needs a different answer. Cloning
carries voice identity, which survives a generation of copying easily.
Fine-tuning inherits whatever the training audio actually contains.

Measured on `voices/reference/aiko_reference.wav` (generated from the
live embedding):

| property | value |
|---|---|
| sample rate | 24 kHz, Nyquist 12 kHz |
| 99.9% of energy | below 7.4 kHz |
| 8–12 kHz relative to the 300–3400 Hz speech band | −30 dB |
| noise floor | −74.9 dBFS |
| dynamic range | 74 dB |

The encouraging half: it is **clean**. No hiss, no noise floor problem,
nothing that would poison a dataset, and the top octave is attenuated
rather than brickwalled — consistent with ordinary speech, where energy
does concentrate below 8 kHz.

The limits are real but specific:

- **A permanent 24 kHz ceiling.** Train on this and a 48 kHz engine
  (MOSS-TTS-Nano, VoxCPM2) can never pay off, because its main advantage
  has been discarded before training starts.
- **Inherited quirks.** A student cannot exceed its teacher. Fine-tuning
  on pocket-tts output bakes in its prosody habits and its Mimi codec's
  reconstruction as though they were her voice, so the result is a
  *different* engine with the *same* ceiling.

So: generated audio for zero-shot cloning, yes. For fine-tuning, prefer
whatever the voice was originally cloned from — one generation closer,
and possibly higher-rate. This is why the studio accepts mp3/flac/ogg
rather than demanding WAV.

### Worth trying first

**Qwen3-TTS** ([QwenLM/Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS),
~13k stars) is the strongest upstream on the list: Alibaba's Qwen team,
0.6B and 1.7B, streaming, 3-second cloning, voice design, and
natural-language control of tone, rate and emotion. The control surface
is the best match for `ProsodyParams` of anything here.

The CPU path is a **community** project —
[gabriele-mastrapasqua/qwen3-tts](https://github.com/gabriele-mastrapasqua/qwen3-tts),
pure C + BLAS, no Python or Torch, with AVX-512/VNNI/BF16 kernels that
the 9950X3D would exercise well. It advertises `.qvoice` profiles,
inline emotion, `[laugh]`, pauses, rate and an OpenAI-compatible
server. **Maturity caveat: 78 stars and zero forks.** Single author,
essentially unexercised. Treat it as an experiment, not a dependency —
and benchmark rather than trusting the published RTF numbers, which
are from other machines.

**Chatterbox Nano** (Resemble AI, 110M) is the pragmatic one: ~3×
realtime on 8 cores, clones from a reference, and the Turbo/Nano
family has native `[laugh]` / `[chuckle]` / `[cough]` tags. The full
500M model exposes `exaggeration` plus CFG — a genuinely numeric
expressiveness dial, which is rare and easy to drive from cadence.
Two caveats: the CPU path has had loading bugs, and generated audio
carries an imperceptible Perth watermark.

**PocketTTS.cpp**
([VolgaGerm/PocketTTS.cpp](https://github.com/VolgaGerm/PocketTTS.cpp))
deserves more attention than its billing. It is our *current* model on
ONNX Runtime with cloning, streaming and a C FFI — i.e. the
minimum-change way to delete PyTorch from the audio path while keeping
the voice we already have. It buys no new expressiveness, but it is
the cheapest answer to the crash surface. Same maturity warning: 51
stars, zero forks.

**MOSS-TTS-Nano** and **TTS Lite** keep their entries from the
original sweep: ONNX CPU, no Torch, 48 kHz stereo and 32 kHz
respectively, and TTS Lite additionally streams word-level timestamps
where the avatar's mouth is currently driven off raw amplitude.

### Corrections to note

**VoxCPM2 is not a CPU candidate.** The official repo is
[OpenBMB/VoxCPM](https://github.com/OpenBMB/VoxCPM) (not the personal
fork that circulates), and upstream's own table puts it at **RTF ~0.30
on an RTX 4090 and ~8 GB VRAM**. A 2B tokenizer-free diffusion-AR model
at 0.3 RTF on a 4090 will not stream in realtime on any CPU, and its
diffusion timesteps fight low first-chunk latency. It is a lovely
model — 48 kHz, 30 languages, Voice Design, Apache-2.0 — but it wants
the GPU we are explicitly trying to keep free.

**IndexTTS2** separates speaker identity from an 8-dimensional emotion
vector, which is the most attractive control surface on this page in
the abstract. It is also GPU-oriented. Keep it as the quality bar to
beat, not a deployment target.

**Kokoro-82M** is ruled out despite being fastest: fixed voice packs,
no cloning, so adopting it loses Aiko's voice. **XTTS-v2** has the
explicit speed control we want but cannot stream at ~5× RT on CPU.
**CosyVoice 3**, **F5-TTS** and **GPT-SoVITS** are all credible
cloners whose deployment stories are GPU-first; GPT-SoVITS is the one
to revisit if we ever record a proper Aiko dataset and want to
fine-tune rather than zero-shot.

## What to check on any candidate

Our pipeline constrains the choice more than raw quality scores do:

- **Chunked streaming** with a first-audio budget in the low hundreds
  of ms. `SessionController` emits ~50 ms PCM chunks over the WS; an
  engine that only returns a finished clip forces a rewrite.
- **Cloning from a clip we already have**, ideally without a
  transcript, so `aiko1_refined` can be regenerated from the same
  source audio rather than re-recorded.
- **Native duration or rate control.** Rare in this class — if a
  candidate has it, that is a genuine differentiator and it retires
  the time-stretch stage.
- **Thread behaviour under a lock.** `generate_audio` is serialised by
  `self._lock`; anything spawning its own worker pool needs the same
  treatment.
- **License**, for the model weights as well as the inference code.

## Verified here, Aug 2026

- pocket-tts API surface: read from `TTSModel` by introspection.
- Torch is `2.13.0+cpu`; `torch.cuda.is_available()` is `False`.
- The speed/pitch coupling and both `_runtime_*_enabled` gates are
  documented in `pocket_tts_service.py` itself.
- Qwen3-TTS upstream is `QwenLM/Qwen3-TTS` (~13k stars). Its CPU
  runtime is a third-party project at 78 stars / 0 forks.
- `PocketTTS.cpp` is 51 stars / 0 forks.
- VoxCPM2's home is `OpenBMB/VoxCPM`; the RTF ~0.30 / ~8 GB VRAM
  figures for a 4090 are upstream's own.

Star counts are recorded because two of the most interesting options
are single-author projects, and that should be re-checked before
anything depends on them.
