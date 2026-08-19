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

## Adding an engine

1. Write a class in `adapters.py` (or its own module beside it)
   subclassing `Adapter`, with a `Caps` describing what it can be
   *told*. Fill `Caps` from upstream docs and leave `verified=False`
   until you have confirmed it here.
2. Implement `_load`, `voice_from_reference`, and `synth`. Optionally
   `voice_from_id`.
3. Register the factory in `REGISTRY`.

Keep it lazy — import the engine's package inside `_load`, not at module
scope, so a missing optional dependency shows up as one line in the
report's "would not run" list instead of breaking the whole bench.

Two rules worth honouring, both learned the hard way here:

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

- A browser-side cloning UI (record or drop a clip, hear it, save it to
  `voices/`). The plumbing it needs already exists — `set_voice()`
  hot-swaps at runtime and clears the audio cache, and `export_voice()`
  is still in the service hanging off nothing since the Qt dialog was
  deleted.
- A pitch-preserving time-stretch stage, which is the fix that lights up
  the cadence layer regardless of which engine wins. See the options
  doc; it has to work on ~50 ms chunks, which rules out the convenient
  offline helpers.
