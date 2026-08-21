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

**Since Aug 2026 it is no longer the only engine.**
[`app/tts/registry.py`](../app/tts/registry.py) holds a provider
catalogue, `tts.provider` is actually honoured, and the Chatterbox
variants run as a subprocess in their own virtualenv. See
[Shipped: the provider registry](#shipped-the-provider-registry) below —
including why several conclusions in this file were CPU-only conclusions
that a GPU would overturn.

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

### What we used to do instead, and what it cost

Because there is no speed parameter, the service used to implement
speed by **scaling the playback sample rate** — a varispeed effect,
tape-style, that coupled rate and pitch at roughly 1.6 semitones per
10% of rate.

That coupling is why the cadence layer's whole speed channel was dark.
`cadence.py` computes a per-sentence `speed_hint` from mood, arousal,
circadian state and ambient noise; `[[prosody:slow]]` and
`[[prosody:fast]]` parse; and none of it reached the audio, because
`agent.tts_runtime_speed_enabled` defaulted to off. With it on,
affect-driven pacing detuned her and the voice audibly changed identity
between sentences. The planner was talking to itself.

The `[[prosody:…]]` overlay table shows the shape of it — only the
speed column was affected:

| tag | speed | gain | audible before | audible now |
|---|---|---|---|---|
| `whisper` | 0.97 | −6 dB | yes, via gain | yes |
| `firm` | — | + | yes, via gain | yes |
| `slow` | 0.95 | 0 dB | **no** | yes |
| `fast` | 1.05 | 0 dB | **no** | yes |

## Shipped: pitch-preserving time-stretch

[`app/audio/timestretch.py`](../app/audio/timestretch.py)
implements **WSOLA** (waveform-similarity overlap-add) and
`_speak_worker` now runs the PCM through it, declaring the *true* sample
rate to the client instead of a scaled one. Duration lives in the sample
count; pitch stays put.

Measured on her own voice via `python -m tools.tts_speed_ab --speeds
0.88 0.95 1.0 1.06 1.12`, as a spectral-centroid ratio against the
untouched source:

| speed | stretch | varispeed |
|---|---|---|
| 0.88 | 0.995x | 0.880x |
| 0.95 | 0.995x | 0.950x |
| 1.00 | 1.000x | 1.000x |
| 1.06 | 1.003x | 1.060x |
| 1.12 | 0.974x | 1.120x |

Varispeed tracks the speed factor exactly, because it is a pure
relabelling of the rate. The stretch holds the spectrum within half a
percent across most of the band, drifting to 2.6% at the 1.12 edge —
about 0.44 semitones, against varispeed's 1.96 there.

Notes worth keeping:

- **Cost is negligible**: ~0.2% of audio duration (6 ms for a
  three-second sentence), so this is not a latency tradeoff. Getting
  there needed the candidate search to use `np.correlate` plus a prefix
  sum of squares for the norms rather than a per-candidate norm; the
  naive version was 18x slower and would have been a real regression.
- **The "must be incremental on ~50 ms chunks" constraint did not
  bind.** It was written assuming a streaming engine. pocket-tts does
  not stream generation — `generate_audio` returns a finished array and
  `_emit_pcm` merely paces chunks out of it — so the whole clip is in
  hand and a one-shot stretch is the actual shape of the problem. A
  streaming engine would need this to become stateful; deliberately not
  written until one exists.
- **The input is zero-padded** so the overlap-add loop can reach the end.
  Without that it stopped when a whole frame no longer fitted and
  dropped up to 30 ms off the tail — 1% of a long sentence, 14% of "Oh!",
  and exactly where a clipped final consonant is audible.
- Licence-wise this stayed in-house: Rubber Band is excellent and has a
  realtime API, but it is GPL/commercial dual, awkward if Aiko is ever
  distributed. This is ~80 lines of numpy instead.
- A phase vocoder was not used — it smears transients, i.e. consonants.
  Full WORLD-style F0 resynthesis was not either: we never needed
  arbitrary pitch contours, only rate that does not drag pitch, and
  stacking analysis/resynthesis on an already codec-decoded signal
  mostly buys artefacts.
- `tts.pitch_preserving_speed=false` restores varispeed for A/B
  listening. It is not a performance switch.

**Still off by default:** `agent.tts_runtime_speed_enabled`. The
technical objection is gone, but per-sentence pacing is an audible
personality change, so it stays the user's call rather than a default
flipped on their behalf. `assistant.tts_length_scale` is honoured
regardless and is now pitch-clean, so it is the easiest way to hear the
difference.

**Both of those were pocket-tts-only for a while, and the failure was
silent.** `set_length_scale` and `set_runtime_speed_enabled` lived on
`PocketTtsService` alone, and `_apply_assistant_preferences` reaches them
through `getattr` — so on Chatterbox they were *absent* rather than
broken. The pacing slider did nothing at all, and the gate being off did
not stop the cadence layer's per-sentence hints being applied in full and
uncapped. The user-visible symptom was only "she is too fast on this
engine": an excited sentence ran at 1.12× where pocket-tts pinned it to
1.00×. Both knobs now come from
[`app/tts/reactions.py`](../app/tts/reactions.py) via
`resolve_playback_speed()`, which is where anything describing *her*
rather than a model belongs, and `tests/test_tts_pacing_parity.py` holds
the two engines to the same answer.

## The other fix that needs no engine change

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
| chatterbox-multilingual (500M) | 2.86–3.04 | ~9400 ms | 45 s cold | 23 languages, cross-lingual |

Since **neither engine streams generation**, RTF *is* responsiveness:
first audio equals full generation. Upstream's "3× realtime on 8 cores"
for Nano (RTF 0.33) is about twice as optimistic as this box measures.

RTF sets the *opening* latency and nothing else, though. Every sentence
after the first is prefetched while the previous one plays, so on a
three-sentence turn the real measured dead air on Nano is **0.03 s at
each boundary after the first**, not the ~2 s its RTF implies — see
[the prefetch](#the-prefetch-is-what-makes-a-sub-realtime-engine-usable)
below. The bound to watch is therefore RTF < 1.0 (generation finishes
inside the previous sentence's playback), which Nano clears and Turbo
does not.

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
  has been discarded before training starts. True of *generated* audio,
  which is all we had when this was written; the licensed 44.1 kHz clips
  below reopen the option.
- **Inherited quirks.** A student cannot exceed its teacher. Fine-tuning
  on pocket-tts output bakes in its prosody habits and its Mimi codec's
  reconstruction as though they were her voice, so the result is a
  *different* engine with the *same* ceiling.

So: generated audio for zero-shot cloning, yes. For fine-tuning, prefer
whatever the voice was originally cloned from — one generation closer,
and possibly higher-rate. This is why the studio accepts mp3/flac/ogg
rather than demanding WAV.

**The original mp3s are lost**, which settled it at the time: generating
from the embedding is the only path that keeps her voice rather than
substituting a different one. Still true of *her* voice, and superseded
as the plan — see the licensed corpus below.
`tools/tts_lab/dataset.py` does it —
203 prompts to 202 clips and ~7.9 min in under two minutes, at a 0.5%
rejection rate, with LJSpeech and GPT-SoVITS manifests and exact
transcripts (the text was the input, so there is no ASR or alignment
error in the labels).

#### What her voice file actually is, and why the original is unrecoverable

"Lost" turned out to be stronger than lost-off-the-internet. The two
March files in `voices/` are not audio and not speaker embeddings: each
holds six layers of transformer self-attention KV cache, F32, shaped
`[2, 1, N, 16, 64]`, with `N` = 99 for `aiko1_refined.safetensors` and
108 for the unused `aiko1.safetensors`. A pocket-tts "voice" is a
*prefilled attention state* — the model's frozen impression of a
reference clip, not the clip.

At Mimi's 12.5 Hz frame rate (`pocket_tts/config/b6369a24.yaml`) those
sequence lengths put an upper bound of **7.9 and 8.6 seconds** on the
audio she was ever cloned from, and the prompt also carries text and
speaker tokens, so the true figure is lower. Recovering audio from a KV
cache is not an analytic inversion, so there is nothing to extract: the
download never contained a recording, and no amount of searching for the
original files would produce one. Rendering through pocket-tts is not one
option among several, it is the only way audio has ever existed for her.

Which sets the real scale of the chain: `aiko_reference.wav` is 27
seconds generated from under 8 seconds of frozen attention state, and
Chatterbox then clones that. Two of the artefacts corrected in playback —
the 7.4 kHz energy ceiling and the narrow band that read as muffled —
trace to that origin rather than to any engine in the current path.

Two follow-ups this leaves open:

- `aiko1.safetensors` has 9% more conditioning than the file in use and
  has never been auditioned. Cheap to check before assuming `_refined`
  is the better of the two.
- A real single-speaker corpus is now the *only* way to beat the ceiling,
  because the teacher-swap above still starts from the same 8 seconds.
  That trades exact identity for genuine recordings — acceptable per the
  owner, who reports her voice is close to several anime characters and
  does not mind a small change. Preserve the option to go back: the
  safetensors are the sole copy of the incumbent voice.

#### The licensed corpus, and why cloning needs almost none of it

`voices/sounds/` holds two licensed packs, both gitignored — bought for
use elsewhere, which does not extend to redistributing them. `other/`
(`RFB_*`, 27 lines) is the voice she was *originally* meant to have.
`aiko-original/` (71 clips) is better than that: the actual source the
pocket-tts state was cloned from, recovered from a school backup.
Measured 21 Aug 2026:

| | aiko-original | other pack | her current origin |
|---|---|---|---|
| source | 44.1 kHz recordings | 44.1 kHz recordings | pocket-tts render of ~8 s |
| real bandwidth (99.9% energy) | **15.7 kHz** median | 11.0 kHz | 6.2 kHz |
| spectral centroid | **4345 Hz** | 3284 Hz | 1818 Hz |
| median F0 | 374 Hz (p10–p90 264–485) | 345 Hz | 333 Hz |
| clipped samples | 0 of 71 | 0 of 27 | 0 |
| total | 2.00 min, median clip 1.04 s | 1.45 min | 8 s |

On identity, provenance is direct: the owner cloned the original voice
from almost all of the English clips in this pack, and that is what
became the safetensors. So the 12% F0 gap — 374 Hz here against her
333 — is the render shifting pitch, not evidence of a second speaker,
which also puts a number on how much pocket-tts moves before anything
else does. Gated SNR, separately, is meaningless on this material: half the
clips carry digital-silence padding (arbitrarily high) and the
one-second ones are wall-to-wall speech (arbitrarily low). What holds is
zero clipping across both packs and no measurable floor where silence
exists.

The pack holds 12.07 seconds of English across nine clips (`MERRY XMAS`,
`READY`, `SCRAMBLE`, `THANK YOU (EN)`, `HAPPY HELLOWEEN`, `GO`, `zero`,
two of `WOW`) — and **these are the clips the original clone was
conditioned on**, which reframes the whole experiment.

It means accent was never the open question. Her incumbent voice is
already the product of English conditioning, pronounced cleanly, and any
English-versus-Japanese-reference A/B would only re-run a choice already
made correctly years ago. What is actually recoverable is narrower and
better defined: **the same source clips, one generation of loss shorter.**
The chain today is clips → pocket-tts conditioning (8 s of it, through
Mimi at 24 kHz) → 27 s render → Chatterbox clone. Handing Chatterbox the
original 44.1 kHz clips deletes the middle two steps and raises what
reaches its 24 kHz decoder path from 6.2 kHz of content to 15.7.

So the experiment is not a choice of language, it is a bypass. Build a
ten-second reference from the English clips directly and A/B it against
`aiko_reference.wav`. Same voice, same words, same engine — the only
variable removed is pocket-tts standing in the middle. Expect the
brightness matching in playback to have less to do, and check /r/ anyway,
since the conditioning path is different even though the source is not.

The studio does this now (`tools/tts_lab/refset.py`, steps 1–2): pick
clips, order them against the two conditioning cutoffs, build, audition,
save as a reference set. A first build of five English clips measured
**17.6 kHz wide against her reference's 6.2** at 44.1 kHz — the predicted
gain, present before any listening.

### Pacing clones along with timbre

The first real reference built this way sounded correct in the player and
came out **slow** the moment it was cloned — noticed immediately, since
1.5× in a browser player was needed to make the audition tolerable. Not a
bug in the path: the sidecar hands the reference to Chatterbox, which
resamples it itself, and the output header is the engine's own rate.
Measured on the same sentence and engine, 21 Aug 2026:

| reference | parts | median part | delivered |
| --- | --- | --- | --- |
| `reference/aiko_reference.wav` | 12 | 3.04 s | 6.83 syl/s |
| built from brightest clips | 10 | 0.92 s | 5.26 syl/s |
| built from sentence clips | 2 | 6.61 s | 6.07 syl/s |

**Chatterbox clones speaking rate along with voice.** The pack is game
audio, so its brightest clips are single drawled words followed by a gap,
and ten of those teach a clone to drawl — 24% slow, which is past the
app's 15% correction cap and therefore permanent. Two sentence-length
clips from the same pack, same speaker, land at 8% and inside the cap.

Selecting on brightness alone caused this, so `rank` now takes connected
speech first and brightness within it (`MIN_CONNECTED_S`, 1.4 s), falling
back to short clips only to top up a budget nothing else can fill. The
build report states the shape — part count, median part, gap share — and
says plainly when a reference is a list of words. And **Check pace**
speaks one probe sentence through the candidate and measures the result,
which is the only way to see this before saving: the reference clip
itself gives no hint, and every quality number the studio had was green.

That leaves the residual 8%, which the app cannot correct either, because
correction needs a tempo target and a target needs English transcripts
these Japanese clips do not have. So a manifest may now **declare**
`target_syl_s` outright and `_adopt_rate_target` honours it ahead of
measuring parts — "this is her, hold her to her own pace". A declaration
is an instruction where measured parts are an inference, so it wins when
both are present. It is a checkbox in the studio rather than a default,
since forcing her pace onto a genuinely different voice would be wrong.

### Sampling knobs, and what stability costs in pace

A reproducible artifact on the utterance-initial /h/ of "Hey, …" — the
hardest onset in that sentence, aspiration noise with no acoustic context
before it to anchor — clears up at a colder temperature. It is not free.
Measured on `chatterbox-full`, same voice and text, two draws each:

| temperature | delivered | vs her pace |
| --- | --- | --- |
| 0.8 (shipped) | 3.91 syl/s | +68% |
| 0.5 | 3.79 | +73% |
| 0.3 | 3.29 | +99% |

And on Nano, whose knobs are separate:

| knob | delivered | vs her pace | RTF |
| --- | --- | --- | --- |
| stock | 5.66 syl/s | +16% | 0.64 |
| `min_p=0.05` | 5.29 | +24% | 0.61 |
| `temperature=0.5` | 5.26 | +25% | 0.62 |
| `temperature=0.3` | 5.09 | +29% | 0.59 |

Two things fall out. **Every stability knob is paid for in tempo**, and
`min_p` — the knob upstream recommends for exactly this and which Nano
ships *off* — costs about as much as dropping temperature to 0.5. And
`chatterbox-full` is not a way out: at its own defaults it delivers
**3.91 syl/s against Nano's 5.66 on identical text**, 68% off her pace at
RTF 2.72. Reaching for the bigger model to fix an artifact trades one
audible fault for a slower, non-real-time voice.

Which puts the reference first in the ordering. It is the dominant term:
rebuilding from sentence clips moves the baseline 16 points, where the
knobs cost 8 to 13. Fix the reference, then spend the smallest knob you
can get away with, and let the declared tempo target absorb what is left.

None of this used to be able to reach the app, which sent no generation
kwargs at all — every voice spoke on its engine's shipped defaults, so a
value found in an audition was a finding with nowhere to go. A saved
voice's `manifest.json` now carries a `generate` block, alongside the
tempo and brightness targets that travel with it for the same reason, and
`tts.providers.<name>.generate` overrides it for a bare wav with no
manifest.

**Keyed by engine, and merged rather than replaced**, because the values
are absolute while the defaults they were chosen against are not:
replaying Nano's `min_p=0.05` on the full model — which already ships
that — is not conservative, it silently means something else. One
reference gets auditioned on several engines in a sitting, which is the
point of having them side by side, so a single block per voice would have
let the second save quietly discard the first engine's afternoon. An
engine with no entry gets its own defaults rather than somebody else's
numbers.

```json
"generate": {
  "chatterbox-nano": { "min_p": 0.05 },
  "chatterbox-turbo": { "temperature": 0.6 }
}
```

One trap found while building it, and it would have shipped silently.
The app derives her tempo target from the reference manifest's `phrase`
fields, so transcribing the picked clips looks like diligence. But these
are **isolated words**, and isolated words measure slow: four of them
yield a target of 5.05 syllables per second against her established
6.55 — 23% out, which would stretch every sentence she ever speaks to
the 15% correction limit, permanently, on evidence from clips that are
single words. `app/audio/speech_rate.py` says as much in its own comments
("short interjections are exactly where tempo is legitimately unusual")
without anything enforcing it. The studio now computes the target with
the app's own code and refuses to be quiet about it. **Leave the
transcripts blank unless the clips are full sentences**; no target means
no correction, which is the right default for found audio.

Also useful: every filename carries its own transcript in romaji plus an
English gloss, so a fine-tune needs no ASR pass. What it does need is
longer material — median clip 1.04 s and only 18 clips at or above 1.5 s
(62 s total), all isolated words and set phrases, which teaches sentence
prosody badly. Cloning wants ten good seconds and has them; training
would want a different corpus.

1.45 minutes reads thin for training and is *irrelevant* for cloning,
because Chatterbox truncates the reference hard:
`DEC_COND_LEN = 10 * S3GEN_SR` and `ENC_COND_LEN = 15 * S3_SR`
(`chatterbox/tts_turbo.py`, which Nano subclasses). **Ten seconds reach
the decoder and fifteen the tokenizer; the rest of the file is dropped.**
Two consequences worth acting on:

- The 27-second `aiko_reference.wav` is over half wasted, and because the
  cut is a truncation of a concatenation rather than a selection, *part
  order decides which of her phrases condition the clone at all*. The
  studio draws both cutoffs on the selection and names the clips that
  fall past them, because otherwise an audition can be a verdict on
  audio the engine never received.
- Cloning from the licensed clips needs the best ten seconds, which
  exists many times over. Try that before any training: it costs minutes
  and would raise the conditioning bandwidth from 7.4 kHz to the 12 kHz
  the decoder path can actually use.

On language: timbre is language-independent, phonetics and prosody are
not. Fine-tuning on Japanese is the case to avoid — Japanese has one
liquid where English has two, so /r/ and /l/ collapse, which is precisely
what the owner hit on the first attempt years ago and why the search for
English lines started. Cloning is the safer direction, since an
English-trained model supplies pronunciation and borrows only the voice.
Not risk-free: the reference enters twice, as a speaker embedding
(timbre, safe) and as `t3_cond_prompt_tokens`, actual speech tokens that
prime the autoregressive model and can pull articulation with them.
So listen for /r/ specifically. If accent does leak, Multilingual is a
usable offline teacher for an English corpus in her timbre — RTF 3.27
costs nothing when generation is not live.

Worth being clear about what a fine-tune on that set can and cannot buy.
It **cannot** exceed pocket-tts in fidelity, because that is the ceiling
of the training audio. It **can** still be a large win, because the
things we actually want are architectural rather than acoustic: an
emotion parameter the reaction label can drive, inline `[laugh]` in her
own voice, and streaming generation. (Rate control has dropped off that
list — the WSOLA stage above supplies it, engine-independently.) Those come from the model, not from
the data. Framing this as "quality upgrade" would be wrong; framing it as
"same voice, controllable" is right.

#### The teacher does not have to be the engine that ships

One consequence of the above that is easy to miss: **RTF is irrelevant
when building a dataset.** Generation is offline, so an engine at RTF 1.5
costs wall time and nothing else — while the same 1.5 is disqualifying
for live conversation. Since the teacher's quality is the ceiling of
whatever gets trained on its output, the set should be generated by
whichever engine *sounds* best, not by the one that could ship.

Concretely: on listening, chatterbox-turbo preserved her voice well and
pronounced more naturally than the incumbent, but at RTF 1.37–1.66 it can
never be the live engine. It is a perfectly good teacher.
`tools/tts_lab/dataset.py --engine chatterbox-turbo` exists for exactly
that, and it defeats the "cannot exceed pocket-tts" ceiling above by
changing which engine sets it.

The chain has a cost worth stating: pocket-tts embedding → reference clip
→ Turbo clone → generated set is *two* generations of copying, against
one for generating with pocket-tts directly. Whether the better teacher
outweighs the extra generation is an empirical question, and the honest
answer is to build both sets and listen. Neither is settled here.

#### Cross-lingual cloning: a Japanese voice speaking English

Worth knowing because it changes what counts as a candidate voice. In
this architecture speaker identity and linguistic content are separate
paths, so **the reference clip and the target text do not have to share a
language**. A Japanese reference can speak English. That matters when the
available voices for an anime-styled companion are overwhelmingly
Japanese — a constraint that looked like a dead end and is not one.

`chatterbox-multilingual` is registered in the lab for exactly this.
Verified against the installed wheel rather than the README:

- **23 languages, `ja` included** — read from `SUPPORTED_LANGUAGES` in
  `chatterbox.mtl_tts`, not from the docs.
- **`language_id` is a required positional** on `generate`, with no
  default, unlike every other knob. The sidecar injects `"en"` so a
  forgotten argument is not a `TypeError` from inside a subprocess.
- **Defaults are `exaggeration=0.5, cfg_weight=0.5`** — the original
  model's tuning, not Turbo's `0.0 / 0.0`.
- **No `t3_model` parameter**, so PyPI 0.1.7 gives Multilingual **V2**.
  The README describes V3 (better speaker similarity, fewer
  hallucinations); as with `nano=True`, the docs are ahead of the wheel.
- **No paralinguistic tags**, so it loses Turbo's `[laugh]` / `[sigh]` —
  the most Aiko-shaped feature on this page.

**Accent travels with the timbre.** Resemble's own description says the
model captures "timbre, accent, and rhythm" and holds them across target
languages, so a Japanese reference produces English with a Japanese
accent. For most products that is a defect to engineer around; for this
character it is plausibly the desired result, so nothing in the lab tries
to suppress it.

Measured here at **RTF 2.96** (16 threads, 24 kHz), roughly ten seconds
of compute per three seconds of speech and about twice Turbo's cost — as
expected from 500M against 350M. That rules it out of the conversation
loop completely. It does **not** rule it out as a dataset teacher, since
generation is offline: audition a Japanese voice, and if it lands better
than the current one, use this model to generate the English training set
in that voice and fine-tune something fast on the result.

#### Real audio beats both, when it exists

`tools/tts_lab/labeled.py` (and step 4 of the studio) takes real files
plus transcripts. Real audio has no generational loss and no inherited
engine habits, so it is the only route with no ceiling at all. It needs
labels, which is why faster-whisper drafts them for correction — see the
lab README for what the drafts get wrong and why every one is reviewed.

For Aiko specifically the original mp3s are gone, so this route is empty
today. It is built anyway because the moment any real recording turns up,
it is worth more than everything generated.

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
- **Native duration or rate control.** Rare in this class. Now a
  nice-to-have rather than a differentiator: it would retire the WSOLA
  stage, but that stage already works and costs 0.2% of audio duration,
  so there is little left to win here.
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

## Shipped: the provider registry

Chatterbox is now a live engine option, not only a bench candidate.

**Why a subprocess.** Chatterbox requires `torch==2.6.0`; this app runs
`2.10.0`. That is not a version range to negotiate, so the engine cannot
be imported into this process at any price. It runs in its own venv under
`.venvs/` and speaks the same JSON protocol the audition bench uses —
which means the engine Aiko speaks with is byte-for-byte the one that was
auditioned, rather than a reimplementation that might differ in exactly
the ways a listening test was meant to settle.

**What is shared and what is not.** `ChatterboxTtsService` is synthesis
plus process supervision. Everything after "here is a clip" — chunk
sizing, pre-roll depth, real-time pacing, barge-in, gain, the
pitch-preserving stretch, lip-sync amplitude, and the block that keeps
`TtsQueue`'s sentence timing honest — lives in
[`app/tts/pcm_playback.py`](../app/tts/pcm_playback.py), shared with
pocket-tts. Those constants were tuned against observed client behaviour
(pre-roll against audio-scheduler underruns, chunk size against Live2D
render stutter); a second copy would have drifted silently and produced
an engine that sounded worse for reasons nobody would trace to a number
in another file. Reaction-to-speed moved to
[`app/tts/reactions.py`](../app/tts/reactions.py) for the same reason:
how fast she talks when excited describes *her*, not the model.

**A "voice" means different things per engine, and the drawer has to
follow.** Pocket-TTS voices are `.safetensors` speaker embeddings;
Chatterbox voices are reference clips it clones from at load. So the
picker's contents change with the engine — and three things had to be
fixed before that worked. The list was only fetched when the drawer
opened, so after a switch it kept showing the previous engine's voices,
which reads as "the new engine has no voices". `tts_voice` reported the
flat `tts.voice` field, which holds whatever was last set on *any*
engine, so a pocket embedding showed as selected in a list of clips —
matching no option at all; it now reads the per-provider entry and falls
back to what the engine actually loaded. And `list_voices` returned every
wav under `voices/`, which on this box is 279 entries of audition
renders, fine-tuning datasets and studio takes; it now excludes the same
scratch subtrees the voice studio does, leaving 1.

**Every clip is levelled before it plays.** Each sentence is an
independent synthesis and no engine levels its own output, so over one
twelve-sentence turn gated speech level varied by **8.3 dB on Chatterbox
Nano and 8.4 dB on pocket-tts** — indistinguishable, and both audible as
her microphone moving between sentences.
[`app/audio/loudness.py`](../app/audio/loudness.py) matches each clip to
-26 dBFS gated (where pocket-tts already averages, so consistency
improves without loudness changing), which takes the spread to 0.0 dB
measured through the real playback path. It also makes `gain_db` work for
the first time: ambient compensation and `[[prosody:…]]` deltas were
being applied on top of a random base, so a tag asking for 3 dB softer
was routinely swamped by an 8 dB swing the other way. `tts.loudness_target_dbfs
= 0.0` disables it.

Worth recording because the first pass got it wrong: on a five-sentence
sample Chatterbox looked three times worse than pocket-tts here. On equal
footing over twelve they are the same. Level drift was never the thing
that distinguished the engines.

**What *is* Chatterbox-specific: it is duller.** Same twelve sentences,
energy above 4 kHz as a fraction of the total — her reference clip
0.43%, pocket-tts 0.34%, Chatterbox Nano 0.20%. The 99% rolloff and
spectral centroid agree (2904 Hz / 905 Hz against pocket's 3213 / 962).
So Chatterbox reproduces a little under 60% of the incumbent's high end,
which is the "muffled" impression. **Called architectural here first, and
that was half wrong** — worth keeping as written, because the correction
is the useful part. The tokenizer and speaker encoder do condition at
16 kHz, losing everything above 8 kHz, but they are not the only path in:

```python
s3gen_ref_wav, _sr = librosa.load(wav_fpath, sr=S3GEN_SR)   # 24 kHz
ref_16k_wav = librosa.resample(s3gen_ref_wav, ..., target_sr=S3_SR)
s3gen_ref_wav = s3gen_ref_wav[:self.DEC_COND_LEN]           # 10 seconds
s3gen_ref_dict = self.s3gen.embed_ref(s3gen_ref_wav, S3GEN_SR, ...)
```

The *decoder* reference stays at 24 kHz, so 12 kHz of bandwidth reaches
the part that reconstructs waveforms. Her reference clip carries content
only to 7.4 kHz, which means she has been feeding that path a little over
half of what it can use — so some of the dullness is the reference rather
than the engine, and a brighter reference is an untried lever that
brightness matching in playback has been compensating for. Untested at
the time of writing only because no brighter recording of her existed;
`voices/sounds/` changes that. Its noise floor, incidentally, measures *better*
than pocket-tts's and far more consistently (-77.4 dBFS mean over a
5.2 dB range, against -75.7 over 41.9 dB), so perceived noise is not a
floor problem.

**Every clip is also brightness-matched, and that turned out to fix the
dullness too.** With the level drift gone the remaining report was that
"she is changing the warmth and level a little between sentences" — and
level measured flat by then (0.00 dB gated, 0.23 dB K-weighted), so the
level half of that was brightness being *heard* as level. The
measurement that settled what the warmth half is: regenerating **the same
sentence** six times moved the low/high band ratio by 4.2 dB and the
centroid by 206 Hz. Identical words, so none of it is her delivery — the
model re-samples timbre per call.

[`app/audio/timbre.py`](../app/audio/timbre.py) shelves each clip toward
the spectral tilt of the reference clip being cloned. Measured through
the real playback path over a five-sentence turn: spread **5.79 → 0.69
dB**, and the same sentence five times **2.07 → 0.41 dB**. Because the
target is her reference rather than the engine's own average, it closes
the dullness at the same time — mean tilt went from **+1.96 dB warmer
than the reference to +0.15 dB** — which retires the "a high-shelf could
raise the measurement, but is it quality?" question above in favour of
"aim it at her reference and it is both". `tts.timbre_match_limit_db`
bounds the correction (default 4 dB, `0.0` disables) because content
changes brightness legitimately; only Chatterbox uses it, since
pocket-tts has no reference clip and is not audibly drifting.

**And every clip is tempo-matched, which is the same story a third
time.** With level and brightness pinned, the next report was that the
first sentence of a reply "was spoken really great" and the second "much
slower". The obvious suspects are the pacing slider and the affect-driven
speed channel, and both are innocent by inspection:
`agent.tts_runtime_speed_enabled` is off, so `resolve_playback_speed`
handed *both* sentences the identical multiplier. Synthesising the same
sentence six times moves the delivered tempo **10.7–20.9%** depending on
the sentence, and across every clip measured the spread was **24–28%**.

[`app/audio/speech_rate.py`](../app/audio/speech_rate.py) stretches each
clip toward the tempo of the voice being cloned — measured from the
`manifest.json` beside the reference, since a rate needs text as well as
audio — folded into the WSOLA pass that already applies `speed`, so it is
free. Through the real service over eight sentences: spread **24.1% →
5.9%**, worst clip **12.6% → 3.1%** off her pace, **8 of 8** within 5%.
It also removes a standing bias: Nano's raw output averages **6.26 syl/s
against her incumbent 6.55**, so it had been running ~5% slow all along,
which is why the complaint was "needs speeding up" rather than "keeps
changing".

Two measurement traps here, both of which produced a wrong answer first.
**Characters per second is not tempo** — it made the long sentence look
14% slower, and in syllables per second the two were identical, so that
number was measuring letter density. And **rate must be measured over
voiced seconds**, or every comma reads as slow speech. The correction
composes with intent rather than replacing it (it aims at `target ×
intended`), so if the affect channel is ever switched on, a sentence asked
to be 6% faster is *delivered* 6% faster instead of landing somewhere in a
±14% cloud — which is arguably the first time that channel could work at
all.

Three dead ends, recorded so they are not re-tried. **Sampling
parameters do nothing for this**: `temperature` at 0.5 / 0.6 / 0.7,
`cfg_weight`, `min_p` and `repetition_penalty` all land in the same
200–275 Hz centroid band, and an early single-run result suggesting
temperature 0.6 was a threefold improvement did not survive repetition.
**It is not the worse engine**: pocket-tts drifts *more* on centroid
(394 Hz against Nano's 245 Hz). **And it is not new** — fixing the
inter-sentence pauses is what made it audible, by putting sentences next
to each other instead of 2.5 s apart.

**Nothing heavy is imported until an engine is chosen.** Availability is
answered from the filesystem — pocket-tts by `find_spec`, Chatterbox by
whether its venv exists. Probing by importing would be impossible for
Chatterbox and would cost ~0.6–1 GB of PyTorch for pocket-tts, so an
uninstalled engine costs nothing at startup and a selected one pays only
its own price.

### Measured, 20 Aug 2026 (9950X3D, CPU, Nano via the real service)

The sentence was 4.3 s of audio, cloned from `reference/aiko_reference.wav`:

| threads | synthesis RTF |
|---|---|
| 4 | 0.78 |
| 8 | 0.78 |
| 16 | 0.93 |

**Torch's default of one thread per core is the wrong default here.** A
small autoregressive model hits memory bandwidth and sync overhead well
before it runs out of cores, so 16 threads is *slower* than 8 as well as
taking the whole machine. `registry.default_threads()` therefore uses half
the cores capped at 8 — faster and leaves room for whatever else is
running, which is the point of keeping TTS off the GPU.

**First audio is the real cost, and it grows with the sentence.**
Chatterbox generates the entire clip before emitting a byte, so
first-audio *is* the full synthesis: 3.5 s for that 4.3 s sentence,
against pocket-tts's flat ~570 ms. `TtsQueue` prefetches so that only
the *first* sentence of a reply pays it — but that first sentence is
exactly where a conversational pause is felt. This is the trade against
Nano's better pronunciation, and it is a trade rather than a win.

### The prefetch is what makes a sub-realtime engine usable

Everything above assumes the prefetch works, so it is worth stating what
it does and what it cost to get right. Measured end to end through the
real sidecar, three sentences with a 250 ms cadence pause between each:

| | boundary 1 | boundary 2 | total dead air |
|---|---|---|---|
| before | 2.52 s | 2.47 s | **5.07 s** |
| after | 0.58 s | 0.03 s | **0.70 s** |

"Dead air" is gap *minus* the pause the cadence layer intended, i.e. the
part that is just waiting. The residual 0.58 s is a floor, not a bug: the
opener is a one-second clip and cannot cover the three-second generation
of a long second sentence. Later boundaries reach zero because by then
playback is long enough to hide synthesis, which is what RTF < 1.0 buys.
On pocket-tts the same run reaches **0.00 s** at every boundary.

Four separate defects had to be fixed, and the reason to record them is
that each was individually invisible — the prefetch existed, was called,
and delivered nothing:

1. **It looked one chunk ahead and gave up unless that chunk was text.**
   The cadence layer brackets nearly every sentence with `pause_before` /
   `pause_after` silences, so the next chunk was almost always a pause,
   and the prefetch never ran in a real turn. The pause then hid the gap
   it was itself causing: what a listener heard was one unnaturally long
   beat, not a pause followed by a wait.
2. **Chatterbox had no clip cache.** The prefetch calls `generate_audio`
   and discards the result, which only pays off if the engine remembered
   it. pocket-tts did; Chatterbox synthesised, dropped it, and then
   synthesised the same sentence again for playback behind the sidecar's
   one-request pipe — strictly worse than no prefetch.
3. **The cache key included speed.** Speed is applied by the
   time-stretch at emission, so the clip does not depend on it. The
   prefetch guessed speed from the cadence hint while `speak_async` pins
   it to 1.0 whenever the runtime speed gate is off (the default), so the
   two disagreed on every sentence carrying prosody and the cached entry
   was never read.
4. **A sentence enqueued mid-playback was not prefetched.** Sentences
   arrive from the LLM stream while she is already talking, and only
   *dispatch* spawned a prefetch — so the newest sentence stayed cold
   until the chunk before it was dispatched, which on a two-sentence
   reply is after the first has finished. `enqueue` now spawns one too.

Making the prefetch fire then introduced a fifth problem worth naming,
because it is the failure mode any future work here will hit: a prefetch
that reaches the engine first **delays the sentence being spoken**. Both
engines synthesise under a single lock, so sentence two can take the pipe
and push sentence one behind it. That converts a mid-turn gap into a
silence at the top of the reply, which is worse. Two things prevent it —
`SynthesisGate` (playback claims; prefetch waits for idle) and the
ordering in `TtsQueue._dispatch` (hand the text to the engine, *then*
spawn the prefetch). It is not theoretical: with the spawn ordered first,
the real sidecar took 3.1 s to first audio on a one-second opener.

`tests/test_tts_queue_prefetch.py` pins all of it; the invariant it
enforces is **one synthesis per sentence, in sentence order, and the
sentence being spoken is never delayed by the next one**.

### The CPU-only conclusions in this file

Several judgements above — that only Nano can ship, that Turbo and
Multilingual are dataset teachers rather than live engines — follow from
CPU RTF alone. On a GPU the ordering changes: rough scaling for models
this size is 10–30×, which would put Turbo near RTF 0.1 and Multilingual
near 0.3, making the *sound* the deciding factor rather than the speed.

Two things block that today, and neither is a settings flag. Both venvs
carry **CPU-only torch wheels** (`2.10.0+cpu` and `2.6.0+cpu`), and
Chatterbox pins `torch==2.6.0`, which cannot drive a Blackwell card —
a 5090 is sm_120 and needs torch 2.7+ with CUDA 12.8. The pin has a door
in it, though:

```
torch==2.6.0; python_version < "3.14"
torch>=2.9.0; python_version >= "3.14"
```

So the GPU route is a **Python 3.14 sidecar venv with a cu128 torch**,
where upstream itself allows a Blackwell-capable version — not a fight
with the pin. `uv` can fetch cpython-3.14.3 today. Until someone does
that, `_device_report` in the sidecar refuses a CUDA request up front and
says why, rather than letting it silently run on CPU and stutter.
