# TTS audition lab

A prototyping sandbox for choosing Aiko's next speech engine. Nothing
here is imported by `app/` — it exists to try candidates against her
actual voice and our actual pipeline constraints without touching the
production audio path.

Candidate list and the reasoning behind it:
[`docs/tts-engine-options.md`](../../docs/tts-engine-options.md).

## Her voice, and the two places it comes from

**Both of these are now solved. The history is worth keeping because it
explains why the tools are shaped the way they are.**

Aiko's voice lived in exactly two files — `voices/aiko1.safetensors` and
`voices/aiko1_refined.safetensors` — and both are **pocket-tts internal
speaker states, not audio**: a frozen transformer KV cache over about
eight seconds of conditioning, not invertible back to sound. So if
pocket-tts ever stopped loading, the voice was gone, and the whole
reason for this evaluation is that Torch is a crash surface. "Recoverable
only by running the thing we want to replace" is a bad place to sit.

`voicebank.py` fixed the backup by rendering her out to a plain WAV. Then
the **original recordings turned up** in a NAS backup of an old
university folder — a licensed voice pack, now in
`voices/sounds/aiko-original/`, 44.1 kHz and 15.7 kHz wide against the
6.2 kHz that survives in the generated reference. Those clips are what
the first clone was conditioned on years ago, which makes them the best
material available by a wide margin and is what the studio's clip picker
exists to use. They are gitignored: bought assets, not ours to ship.

So there are two routes to a reference now, and the newer one is better:

- **From the clips** — studio steps 1 and 2. One generation of loss
  shorter than anything below, since it skips pocket-tts entirely.
- **From the embedding** — `voicebank.py`, below. Still the only route
  if the source pack is unavailable, and still what
  `voices/reference/aiko_reference.wav` was built with.

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
generation of codec and sampling noise, and that loss is the entire
argument for the clip route: what reaches Chatterbox's 24 kHz decoder
path goes from 6.2 kHz of content to 15.7 by deleting pocket-tts from
the middle of the chain.

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

Pick source clips, build a reference out of them, clone it into any
installed engine, audition a phrase, save it to `voices/`.

**You do not need to build anything to audition.** The Voice picker also
lists everything already saved under `voices/` and defaults to
`reference/aiko_reference.wav`, so the page can speak in her voice the
moment it loads. Pick a `.safetensors` and you get the pocket-tts
embedding route; pick a `.wav` and the engine clones from it per call.
The picker says which is happening and refuses the combination that
cannot work — an embedding is meaningless to an engine that clones from
audio.

### Why the reference is a *set*, not a file

Real source material arrives as dozens of one-second files. A cloning
reference is one clip. So the interesting work is selection and ordering,
and ordering matters because Chatterbox **truncates**:

```python
ENC_COND_LEN = 15 * S3_SR      # 15 s -> the tokenizer prompt
DEC_COND_LEN = 10 * S3GEN_SR   # 10 s -> the decoder conditioning
```

Ten seconds reach the part that reconstructs waveforms, fifteen the part
that primes articulation, and the rest is read off disk and thrown away.
Her committed 27-second reference is therefore a ten-second reference
plus twelve seconds of decoration plus five that never existed as far as
the engine is concerned — and because the cut lands wherever the
concatenation happens to be, **part order silently decides which clips
condition the clone at all**. A file list and a Clone button lets you
spend an afternoon deciding clip 14 sounds bad when clip 14 was never
heard, so the selection renders as a proportional bar with both cutoffs
drawn on it and reordering is a first-class action.

Two other decisions, both places where the tidier-looking choice is
wrong:

- **Parts are stored unnormalised.** Level differences between real
  takes are the speaker, not an error. Per-clip peak normalisation is
  the reflex and would hand the clone a reference in which a whisper and
  a shout are the same size. Gain is applied once, to the joined clip.
- **Transcripts are never guessed.** See below; this one has teeth.

### Length is not a quality axis, it is the pacing

The first real reference built here was ten of the pack's brightest
clips, scored green on every number the studio had, played back
faultlessly — and cloned to a voice that drawled so plainly the audition
needed 1.5× in the browser to be listenable.

**Chatterbox clones speaking rate along with timbre.** The brightest
clips in a game pack are single drawled words followed by a gap. Ten of
those, median part 0.92 s, delivered 5.26 syllables per second against
6.83 from her sentence-length reference: 24% slow, past the app's 15%
correction cap, unfixable downstream. Two sentence-length clips from the
same pack land at 6.07.

So the suggestion takes connected speech first (1.4 s and up) and
brightness only within it, the build report states the shape in words
(`10 parts, median 0.92s, 18% gaps — isolated words`), and **Check pace**
speaks one probe sentence through the candidate and measures what came
out. Use it. It is the only signal here that a reference clip cannot
give you by ear, and it also paces any *saved* voice, so a candidate can
be compared against her incumbent on identical words.

Order matters when you start tuning. The reference is the dominant term —
rebuilding from sentence clips moved delivery 16 points, where every
sampling knob costs 8 to 13 — so fix the clips first and spend the
smallest knob you can afterwards. And don't reach for the bigger model:
`chatterbox-full` delivers 3.91 syl/s against Nano's 5.66 on identical
text, 68% off her pace, at RTF 2.72. It cannot be a live engine, so
fixing an artifact there trades one audible fault for a slower voice.

If the pace check says a reference is slow but within the app's cap, tick
**hold her to her own pace** and rebuild. That writes `target_syl_s` into
the manifest, which the app honours ahead of measuring transcripts — the
escape hatch for source audio in a language you cannot honestly
transcribe. It is off by default, because forcing her pace onto a
genuinely different voice would be the wrong thing to do quietly.

### Transcripts, and the way filling them in helpfully backfires

The manifest's `phrase` fields are what the *app* measures her tempo
target from. So a wrong transcript does not degrade gracefully — it aims
her pacing at a number derived from words nobody said.

Two ways that bites, both live on this machine:

- A found voice pack names files by English *gloss* of Japanese audio
  (`GOOD MORNING (PROPER)-ohayougozaimasu1.mp3`). Auto-filling from the
  filename would have produced exactly the plausible-looking wrong
  answer, which is why nothing is prefilled.
- A pack is made of **one-word interjections**, and isolated words
  measure slow. Transcribing four of them yields a target near 5.0
  syllables per second against her established 6.55, which tells the app
  to stretch *every sentence she ever speaks* to the 15% correction
  limit — permanently, on evidence from clips that are single words. The
  studio computes the target with the app's own code and says so in red
  when it lands that far out.

Leave the transcripts blank unless the clips are full English sentences.
Blank parts do not count, three measurable ones are the minimum, and no
target means no correction — which is the right default for found audio.

### Saving

For pocket-tts it exports a real speaker embedding via `export_voice()`
— the same `.safetensors` the app loads today, and the call the deleted
Qt dialog used to make.

For everything else the engine clones per call, so the clip *is* the
voice. A built reference saves as a **folder**: the wav, its `parts/`,
and `manifest.json`. That shape is a contract, not tidiness —
`ChatterboxTtsService._adopt_rate_target` looks for the manifest *beside*
the reference and the parts *under* it, and disables tempo matching with
one INFO line when either is missing. Copying just the wav would look
like it worked. `tests/test_refset_manifest_contract.py` is the only
place that contract is asserted, since neither side imports the other.

Then pick `<name>/reference.wav` in Aiko's voice settings.

There is no microphone. Her voice cannot be performed, so recording
could only ever produce a different one, and offering the button implied
a choice that was not there. The raw-PCM capture path went with it.

Loopback-only: it writes into `voices/`.

### Tuning an engine

**Read this engine's knobs** loads the engine and reports the real
`generate()` keywords with the installed defaults beside them, which is
the only way to get this right: the docs for the Chatterbox family
describe `exaggeration` and `cfg_weight` for the original 500M model and
say nothing about whether Turbo and Nano kept them, and Turbo ships
`0.0 / 0.0` where every published tip quotes `0.5 / 0.5`. A panel built
from the model card would have offered dials that do nothing on the one
variant fast enough to ship. A field left blank sends nothing at all,
which is not the same as sending the default — "as shipped" is the only
defensible baseline for an audition.

Values you set are kept per engine across reloads, and **Save writes them
into the voice's manifest**, which is what makes tuning worth doing: the
app used to send no generation kwargs at all, so every voice spoke on its
engine's shipped defaults and a value found here had nowhere to go. So
the loop is: pick clips, build, audition on an engine, tune until it
sounds right, save. Switch that engine in Aiko's settings and she sounds
like the audition did — which is only true because of the next section.

They are stored **per engine within the voice**, and saving on a second
engine adds to the first rather than replacing it — one reference tuned
for Nano and for Turbo keeps both, and each engine reads only its own.
That is not tidiness: these are absolute numbers chosen against defaults
that differ, so Nano's `min_p=0.05` is a real intervention where the full
model already ships it. An engine with no entry uses its own defaults.
For a bare wav with no manifest, or to try a value in the live app
without rebuilding a reference to hold it, there is
`tts.providers.<name>.generate` in `config/user.json`, which outranks the
voice.

Two measured things worth knowing before you turn anything. Every
stability knob is paid for in tempo — on Nano, `min_p=0.05` costs about
as much as dropping temperature to 0.5 — and the reference matters more
than any of them, so fix the clips before the dials.

### Play it as Aiko will sound

Aiko does not play what the engine generates. Four stages sit between
synthesis and the socket — brightness shelved toward the reference's own
spectral tilt, level matched to a target, tempo stretched toward the
reference's syllable rate, and the pitch-preserving stretch that carries
all of it — and the lab used to apply **none** of them. It wrote
`generate_audio`'s array to a wav and played that. So the lab auditioned
the engine while Aiko played the engine plus four corrections, and the
predictable thing happened: a reference that sounded right here sounded
wrong from her.

The checkbox under **Speak** is on by default and runs the app's own
`app.tts.shaping.shape_clip` on the clip, with the targets derived exactly
as the service derives them on clone. The line under it reports what each
stage did, which is the part worth reading:

```
as Aiko: level +1.7 dB to -26.0 dBFS · brightness -6.5 → -2.8 dB
         (target 11.32) · tempo ×0.936 toward 6.55 syl/s
```

Turn it **off** to hear the engine raw. That comparison is the diagnostic:
if a voice is good raw and bad shaped, the problem is a target, not the
clips or the knobs.

Which is not hypothetical. One phrase through `chatterbox-nano`, her two
references, measured:

| | `reference/aiko_reference.wav` | `aiko2/reference.wav` |
| --- | --- | --- |
| tilt target | +11.32 dB | −2.16 dB |
| level applied | −3.27 dB | **+2.24 dB** |
| brightness applied | 11.62 → 11.54 | 1.67 → **−1.70** |
| tempo applied | ×1.15 (at the cap) | none — no target |

Higher tilt is more low-band energy, so `aiko2` — built from anime
voice-pack exclamations — asks to be 13.5 dB **brighter** than her
pocket-tts reference. Nano generates dark, so `aiko2` gets every sentence
brightened 3.4 dB *and* boosted 2.24 dB, on top of the excited register
already cloned from those clips. Brighter, louder, more excited: that is
the chipmunk, and all three push the same way. Her committed reference is
the opposite — attenuated, and tonally left alone because the generation
already matches it. None of this was audible here before this section
existed.

Two things to notice in that table beyond the chipmunk. `aiko2` has no
tempo target at all, because deriving one needs three parts with `phrase`
text and it has none — so its pacing is whatever Nano felt like. And the
tempo correction on her *real* reference is **saturated**: ×1.15 is the
`tts.speech_rate_match_limit` ceiling, meaning Nano runs more than 15%
slower than her declared 6.55 syl/s and the stage gives back all it is
allowed to. A saturated corrector passes variance straight through, so
that is the number to look at first if sentences still differ in pace.

## Auditioning a voice in another language

`chatterbox-multilingual` clones **across** languages: the reference clip
and the target text need not share one. So a Japanese voice clip can
speak English, which is the difference between "no suitable candidate
voices exist" and "most of them are in the wrong language".

To try one, select the clip in studio step 1 and build it in step 2, pick
`chatterbox-multilingual` in step 3, type English, and Speak. The
sidecar defaults `language_id` to `en`; override it in the extra-options
box (`{"language_id": "ja"}`) to hear the same voice in its own language
for comparison.

Two things to expect. **Accent comes with the voice** — the speaker
encoder carries phonetic habit as well as timbre, so a Japanese
reference gives accented English. That is not suppressed, because for an
anime-styled companion it may well be the point. And it is **slow**:
measured RTF 2.96 here, twice Turbo's, so treat it as an audition and
dataset-generation tool rather than a deployment candidate. Also no
`[laugh]` / `[sigh]` tags, which Turbo has.

## Building a training dataset

Two routes, and they are not equivalent. **Generating** from her existing
embedding always works and always inherits a ceiling. **Labelling real
audio** has no ceiling but needs audio to exist.

### From real files (`labeled.py`, or studio step 5)

Prefer this whenever any real recording survives. Real audio carries no
generational loss and none of pocket-tts's habits, so a fine-tune on it
can come out *better* than the current voice rather than a slightly worse
copy of it.

The expensive part is not the audio, it is the labels — a transcript per
clip, matching what was actually said. Typing a few hundred by ear is
where this kind of project gets abandoned half-done, and a half-labelled
set is worth nothing. So **faster-whisper drafts them and you correct**,
which turns hours of transcription into minutes of proofreading.

```bash
python -m tools.tts_lab.labeled --dir clips/ --transcribe
python -m tools.tts_lab.labeled --manifest labels.tsv     # path<TAB>text
```

Or step 5 of the studio, which is the same code with players and editable
transcripts: drop the files in, hit **Draft missing transcripts**, fix
what is wrong, build. Typed text is kept in `localStorage` keyed by
filename and size, so a reload or a server restart does not cost you the
afternoon.

Whisper's drafts are drafts. It normalises numbers ("2026", not "twenty
twenty six"), guesses proper nouns, and punctuates to taste — and for TTS
training the transcript must match the *sounds*, so all three are wrong.
Clips are therefore flagged for review on low confidence, poor speech
coverage, non-English detection, and **any digit in the output**. That
last one fired immediately on the first real run, on "It arrived at 4.15,
which is 20 minutes earlier" — exactly the line a human needs to rewrite.

Three screens reject rather than quietly accept, because each one costs a
training run to discover otherwise:

- **Over 15 s** — most fine-tuners window shorter and would truncate the
  clip, wasting the label typed for it. Cut the file up first.
- **Transcript far longer than the audio can hold** — means a cut-off
  clip, a label from the wrong file, or a pairing that slipped by one.
  All three teach a mispronunciation, and none are visible in a waveform.
- **Mixed sample rates** are converged on the *most common* rate present,
  not the highest, so the majority of the set is untouched and only
  outliers are converted. Conversion goes through a polyphase filter:
  linear interpolation leaves an alias from a 10 kHz tone at **63% of
  full scale** when going 48 k → 16 k, against −57 dB for polyphase, and
  in a training set that folded noise is permanent.

### From her existing voice (`dataset.py`)

Written when the audio her voice was cloned from was believed lost, which
made generating from the embedding the only path that kept *her* voice
rather than substituting a different one. `voices/sounds/aiko-original/`
has since turned up, so this is no longer the only option — but it is
still the one that produces exact transcripts and unlimited material,
and two minutes of found clips is not a fine-tuning set.

```bash
python -m tools.tts_lab.dataset --dry-run          # corpus check, no audio
python -m tools.tts_lab.dataset --minutes 10
python -m tools.tts_lab.dataset --temps 0.6 0.75   # more variety
python -m tools.tts_lab.dataset --text-file mine.txt
python -m tools.tts_lab.dataset --engine chatterbox-turbo --minutes 10
```

That last one matters more than it looks. **Generation is offline, so
generation speed is irrelevant here.** An engine at RTF 1.5 is
disqualifying for live conversation and costs nothing but wall time for a
dataset — and since the teacher's quality is the ceiling of whatever gets
trained on the output, the set should be built by whichever engine
*sounds* best, not the one that would ship. Temperature is delivered
however each engine expresses it: at load time for pocket-tts, as a
generate keyword for the Chatterbox family.

Output is `voices/datasets/<speaker>-<stamp>/` with `wavs/`,
`metadata.csv` (LJSpeech layout, which most training code reads),
`<speaker>.list` (GPT-SoVITS's format — the one engine in the options doc
that would actually fine-tune), and `dataset.json` carrying the full
provenance including rejections. Both manifests are emitted because the
trainer is not chosen yet, and that is much cheaper than regenerating
audio later for a format change.

**One real advantage over a recorded dataset: the transcripts are exact.**
Recording requires ASR plus forced alignment, and both introduce errors
that then get trained on as if true. Here the text *is* the input.

Three decisions worth knowing about, since each is a place the obvious
choice is wrong:

- **Levels are normalised once across the whole set**, not per clip.
  Per-clip peak normalisation is the reflex and it would erase the level
  difference between a whisper and an exclamation — part of what makes
  the voice worth training on. A measured run kept a 3.7× RMS spread
  while landing the set peak on 0.95 with nothing clipped.
- **Truncation is screened by duration against character count.** A clip
  cut off mid-word looks perfectly healthy by peak and RMS, and it
  teaches the model to stop early. Rejections are reported by reason
  rather than dropped quietly: if a fifth of the set is being thrown
  away, the temperature is wrong and that should be visible.
- **The corpus is Harvard sentences plus conversational lines**, because
  those cover different things. Harvard (IEEE 1965, public domain) is
  phonetically balanced and prosodically flat; a voice trained only on it
  learns to read aloud rather than talk. The conversational half supplies
  question and exclamation contours. `--dry-run` reports the balance and
  warns below 8% of either, which is how the first version of this corpus
  was caught shipping one exclamation-final line in 164.

Everything in the corpus is deliberately generic — no names, nothing that
happened. A dataset is the easiest artifact to hand someone by accident,
and this one is her *voice*; it does not need her *life* attached to be
useful.

Measured: 203 prompts yield 202 clips and ~7.9 minutes in under two
minutes of generation, at a 0.5% rejection rate. One pass is about eight
minutes of audio, so a longer target needs extra `--temps` values or a
bigger `--text-file` — re-rendering the same prompt at the same
temperature mostly duplicates it.

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
python -m tools.tts_lab.envs install chatterbox       # Turbo, original, multilingual
python -m tools.tts_lab.envs install chatterbox-git   # master, for Nano
python -m tools.tts_lab.envs remove chatterbox        # uninstalls entirely
```

One venv hosts several models, which is why `env_name` and
`sidecar_engine` are separate fields on `Remote`: `chatterbox-turbo`,
`chatterbox-full` and `chatterbox-multilingual` all live in the
`chatterbox` env and differ only by which class the sidecar imports.

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

- **A trainer.** Both dataset routes stop at manifests. Picking the
  fine-tuning target is the next decision, and it is the one that decides
  whether the labelled route was worth the transcription effort.
- **Clip splitting.** A 40-minute recording is rejected as too long
  rather than cut into sentences, so long-form source material needs
  chopping elsewhere first. Whisper already returns segment boundaries,
  so this is mostly wiring.
- **Refinement past selection.** Zero-shot cloning has no training step,
  so "refine" here means a better ten seconds and better knobs, and both
  are now reachable in the studio. A genuine refinement pass means a
  fine-tune, which needs the trainer above.
- **Streaming through the sidecar.** The protocol is request/response, so
  a remote engine's first-audio figure is its whole clip latency. That is
  the harness's limit, not the engine's, and it is only worth fixing for
  an engine that survives the first audition.
- **A multi-sentence audition.** The shaping now matches the app, but the
  lab still speaks one phrase where Aiko speaks a queue of them, and the
  between-sentence drift those stages exist to correct is therefore still
  not something you can hear here. Every complaint about her sounding
  inconsistent has been about sentence *two*.
- **The other candidates** in the options doc: PocketTTS.cpp (our exact
  model on ONNX, which would delete PyTorch from the audio path and keep
  the voice bit-for-bit), Qwen3-TTS via its C runtime (the best control
  surface on the list), MOSS-TTS-Nano, TTS Lite.
