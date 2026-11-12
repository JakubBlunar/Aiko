# TTS engine contract

The backend synthesizes speech; the browser/Tauri client plays streamed PCM.
[Voice mode](voice-mode.md) owns audio transport and voice ownership. This guide
owns provider selection, shared playback behavior and synthesis lookahead.
Historical measurements and candidate research live in the
[engine audition notebook](../backlog/reviews/tts-engine-options.md).

## Provider catalogue

[`app/tts/registry.py`](../app/tts/registry.py) is the source of truth for provider
names, supported devices, availability and construction.

| Provider | Runtime | Voice input | Offered devices |
| --- | --- | --- | --- |
| `pocket-tts` | In-process | Saved speaker embedding (`.safetensors`) | CPU |
| `chatterbox-nano` | Separate interpreter/sidecar | Reference audio clip | CPU, CUDA |
| `chatterbox-turbo` | Separate interpreter/sidecar | Reference audio clip | CUDA, CPU |
| `chatterbox-multilingual` | Separate interpreter/sidecar | Reference audio clip | CUDA |

Availability probes inspect module locations/filesystem prerequisites without
importing engines. Only construction imports the selected service. Chatterbox
uses its own environment under `.venvs/` and the
[`tts_lab` sidecar](../tools/tts_lab/sidecar.py), keeping its incompatible PyTorch
dependency outside the application interpreter. Missing prerequisites produce an
availability reason; listing engines does not install them.

The registry's fallback path tries the configured engine, then Pocket-TTS, then
the null engine if neither can be constructed. An offered device is not proof
that the current host can run a particular model at conversation speed.

`tts.provider` selects the engine. `tts.providers.<provider>` carries provider
options such as reference voice, device, threads and generation settings;
Pocket-TTS also retains its existing flat voice/settings fields. Use the
[configuration reference](configuration.md) for exact fields and precedence.
Voice selection must follow the provider's `voice_kind`; an embedding cannot be
used as a reference audio clip. Audition/install tooling is documented in
[`tools/tts_lab/README.md`](../tools/tts_lab/README.md).

## Shared playback and character

Both services use [`PcmPlaybackMixin`](../app/tts/pcm_playback.py). Chunk sizing,
pre-roll, pacing, gain, matching, tempo adjustment and amplitude delivery belong
there rather than in individual engine adapters. Runtime audio timing stays on
the real clock.

[`reactions.py`](../app/tts/reactions.py) resolves reaction-to-speed behavior and
the final playback speed. Both services expose `set_length_scale` and
`set_runtime_speed_enabled`, so the pacing preference and affect-speed gate
apply across provider changes. `assistant.tts_length_scale` supplies the pacing
preference; a larger length scale means slower playback.

Generated clips can vary in loudness, brightness and tempo. Shared playback uses
the engine's loudness, spectral-tilt and syllabic-rate targets to reduce those
per-clip changes. Chatterbox derives brightness/rate targets from the reference
voice; provider loudness policy is resolved through the shared helper. Treat
these as voice characteristics, not new independent gains/speeds per sentence.
Exact measurement and matching helpers live under [`app/audio/`](../app/audio/).

`prepare_tts_text` changes only the spoken stream. Hidden inline tags are removed
before speech/transcript persistence; speech preparation must never rewrite the
saved conversation transcript.

## Prefetch and synthesis ownership

`TtsQueue` can call `generate_audio` ahead of playback. Both engine services use
[`ClipCache` and `SynthesisGate`](../app/tts/clip_cache.py):

1. Lookahead stores the synthesized clip under the same cache identity playback
   will request. It must not synthesize again merely because playback starts.
2. `speak_async` claims foreground synthesis priority so background lookahead
   cannot take a single-request engine ahead of the sentence being spoken.
3. Playback reaches the cache without acquiring its own synthesis gate again,
   avoiding a wait on its own claim.

Cancellation/cache cleanup belongs to that shared contract. Improving an engine
must preserve sentence ordering, interruption and playback timing rather than
duplicating the queue/pacer in a provider adapter.

## Adding or changing an engine

Register a provider descriptor and cheap availability probe, construct only the
chosen adapter, and reuse shared playback, reactions, matching and prefetch.
Keep installation as a deliberate tooling action. Focused regression coverage
includes [`test_tts_pacing_parity.py`](../tests/test_tts_pacing_parity.py) and
[`test_tts_queue_prefetch.py`](../tests/test_tts_queue_prefetch.py), alongside
registry/service tests. Those contracts do not substitute for listening and
latency measurements on the actual selected voice/device.
