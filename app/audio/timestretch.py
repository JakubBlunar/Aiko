"""Pitch-preserving time-stretch (WSOLA), so speech rate stops moving pitch.

Why this exists
---------------
Pocket-TTS has no speed parameter. Rate was therefore faked by lying to
the client about the sample rate -- ``playback_rate = sample_rate *
speed`` -- which is varispeed, exactly what happens when a tape is played
fast. Duration and pitch move together, at roughly **1.6 semitones per
10% of rate**.

That coupling is why Aiko's whole affect-driven pacing channel is
switched off in production. ``cadence.py`` computes a ``speed_hint`` per
sentence from mood, arousal, circadian state and ambient noise, and
``[[prosody:slow]]`` / ``[[prosody:fast]]`` exist and parse -- but
``agent.tts_runtime_speed_enabled`` defaults to ``False`` because the
only available implementation made her sound like a chipmunk when
excited and a ghost when tired. The planner has been talking to itself.

Separating rate from pitch is what turns that dark channel on, and it is
worth doing regardless of which engine wins the audition: it is our code,
it works on PCM, and it survives an engine swap. An engine with native
rate control would retire it, and none of the current candidates have
one.

Why WSOLA and not something cleverer
------------------------------------
We do not need arbitrary pitch contours. We need *duration* to change
while pitch stays put, over a modest range (the service clamps speed to
0.88-1.12 and the user's pacing slider to 0.85-1.15). That is the exact
problem overlap-add solves.

**WSOLA** (waveform-similarity overlap-add) beats plain OLA for the one
reason that matters here: instead of cutting the next frame at a fixed
position, it searches nearby for the frame that best *continues* the
waveform already written. Splicing at a waveform-similar point keeps
successive pitch periods in phase, and phase discontinuity is what makes
naive OLA sound metallic and warbly on voiced speech.

Two heavier options were deliberately not taken. A **phase vocoder**
works on the spectrum and is better suited to music than to speech,
where it smears transients -- consonants -- and adds the characteristic
"phasiness". **Full WORLD-style analysis and resynthesis** (F0 contour,
spectral envelope, aperiodicity) can do far more than we need, and
stacking a full analysis/resynthesis pass on top of an already
codec-decoded signal mostly buys artefacts.

Licensing also mattered. Rubber Band is excellent and has a real-time
API, but it is GPL/commercial dual-licensed, which is fine for personal
use and awkward the moment Aiko is distributed. This is ~80 lines of
numpy against a dependency with a licence question.

One-shot, and why that is not a shortcut
----------------------------------------
The design note in ``docs/tts-engine-options.md`` requires the stage to
work incrementally on ~50 ms chunks, which ruled out offline helpers like
``librosa.effects.time_stretch``. That constraint turned out not to bind:
pocket-tts **does not stream generation**. ``generate_audio`` returns a
finished array and ``_emit_pcm`` merely paces chunks out of it, so the
whole clip is in hand before a single sample is sent. Stretching it in
one call is therefore not a compromise, it is the actual shape of the
problem.

If a streaming engine ever wins, this needs to become stateful: keep the
tail of the previous input frame plus the overlap-add accumulator across
calls, and hold back one frame of output so the search window always has
future samples to look at. Deliberately not written yet -- an untested
streaming path built for a hypothetical engine is a liability, not
foresight.
"""

from __future__ import annotations

import numpy as np

#: Analysis/synthesis frame. Needs to span several pitch periods for the
#: similarity search to lock onto periodicity rather than onto noise: at
#: 24 kHz this is 720 samples, about six periods of a 200 Hz voice.
FRAME_MS = 30.0

#: How far the search may move a frame from its ideal position. Must
#: cover at least one pitch period or the search cannot find the
#: in-phase splice it is looking for; 12 ms covers down to ~83 Hz, well
#: below any speaking voice.
SEARCH_MS = 12.0

#: Beyond this the artefacts stop being worth it and the request is
#: almost certainly a bug. Speech at half or double rate also stops
#: sounding like the same speaker, which defeats the purpose.
RATE_MIN = 0.5
RATE_MAX = 2.0

#: Below this the stretch is inaudible and not worth the work -- or the
#: risk, since every pass is one more chance to add an artefact.
RATE_EPSILON = 1e-3


def _periodic_hann(size: int) -> np.ndarray:
    """Hann window that sums to exactly 1.0 at 50% overlap.

    ``np.hanning`` is the *symmetric* variant, which does not: it is
    zero at both endpoints, so a 50% overlap-add leaves a periodic ripple
    at the hop rate -- an audible buzz at, here, 66 Hz. The periodic
    form is the correct one for overlap-add.
    """
    n = np.arange(size, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * n / size)


def _best_start(
    source: np.ndarray,
    lo: int,
    hi: int,
    frame: int,
    target: np.ndarray,
) -> int:
    """Position in ``[lo, hi]`` whose frame best continues ``target``.

    Normalised cross-correlation rather than a plain dot product, so the
    search picks the best *shape* match instead of simply the loudest
    candidate -- an unnormalised search drifts toward vowel peaks and
    away from the position it was asked about.

    Both halves are computed in ways that keep this off the latency
    budget, because it runs once per output frame -- roughly 200 times
    for a three-second sentence, in the live speech path.

    ``np.correlate`` gives every candidate's dot product in one C loop,
    without materialising the overlapping-frames matrix. The norms then
    come from a prefix sum of squares, so each candidate's norm is two
    lookups instead of a pass over its 720 samples. That is the
    difference between O(candidates x frame) and O(segment), and it is
    most of why this stage costs ~1% of audio duration rather than ~4%.
    """
    if hi <= lo:
        return lo
    segment = source[lo : hi + frame]
    if segment.size < frame:
        return lo
    scores = np.correlate(segment, target, mode="valid").astype(np.float64)
    squares = np.empty(segment.size + 1, dtype=np.float64)
    squares[0] = 0.0
    np.cumsum(np.square(segment, dtype=np.float64), out=squares[1:])
    energy = squares[frame:] - squares[: segment.size - frame + 1]
    # Guarded rather than clipped: a silent candidate has zero energy and
    # must score zero, not divide.
    scores /= np.sqrt(np.maximum(energy, 0.0)) + 1e-9
    return lo + int(np.argmax(scores[: hi - lo + 1]))


def time_stretch(
    audio: np.ndarray,
    rate: float,
    sample_rate: int,
    *,
    frame_ms: float = FRAME_MS,
    search_ms: float = SEARCH_MS,
) -> np.ndarray:
    """Change duration by ``rate`` without changing pitch.

    ``rate`` follows the same convention as the service's speed: greater
    than 1.0 is faster and shorter, less than 1.0 slower and longer.
    Returns float32 at the *same* sample rate as the input, which is the
    whole point -- the caller can now declare the true rate to the client
    instead of a scaled one.

    Short inputs are returned unchanged rather than stretched. Under a
    couple of frames there is nothing for the similarity search to work
    with, and the alternative -- falling back to resampling -- would
    reintroduce the exact pitch shift this function exists to remove. A
    sub-60 ms clip whose duration is off by 10% is inaudible; one whose
    pitch jumped would not be.
    """
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    if flat.size == 0 or abs(rate - 1.0) < RATE_EPSILON:
        return flat
    rate = float(min(RATE_MAX, max(RATE_MIN, rate)))

    frame = int(sample_rate * frame_ms / 1000.0)
    frame -= frame % 2
    if frame < 4 or flat.size < frame * 2:
        return flat
    hop_out = frame // 2
    hop_in = hop_out * rate
    radius = int(sample_rate * search_ms / 1000.0)

    # Padded so the loop can run past the last real sample. Without this
    # it stops as soon as a whole frame no longer fits, which abandons up
    # to one frame of audio -- 30 ms, at the *end* of the utterance,
    # where the trailing consonant and the decay of the final vowel
    # live. Inaudible on a long sentence and 14% of a short one like
    # "Oh!", which is precisely where a clipped tail is noticed.
    #
    # Padding rather than copying the remainder through afterwards,
    # because the similarity search may place a frame up to ``radius``
    # behind where it was asked to, so "how much input is left" and "how
    # much output room is left" are different quantities and reconciling
    # them by hand gets the duration wrong. Here the last frames simply
    # blend into silence -- the natural end of a clip -- and duration is
    # settled by the trim at the bottom.
    padded = np.concatenate([flat, np.zeros(frame, dtype=np.float32)])
    target_len = int(round(flat.size / rate))

    # Generous allocation, trimmed at the end. float64 keeps the
    # overlap-add sum exact enough that the window-sum normalisation
    # below does not introduce its own ripple.
    capacity = target_len + 3 * frame
    out = np.zeros(capacity, dtype=np.float64)
    weight = np.zeros(capacity, dtype=np.float64)
    window = _periodic_hann(frame)

    read = 0.0
    write = 0
    # The waveform that would naturally have followed the frame just
    # written. None for the first frame, which has nothing to continue.
    target: np.ndarray | None = None
    filled = 0

    while write + frame <= capacity:
        ideal = int(round(read))
        if ideal >= flat.size:
            break
        if target is None:
            start = ideal
        else:
            start = _best_start(
                padded,
                max(0, ideal - radius),
                min(padded.size - frame, ideal + radius),
                frame,
                target,
            )

        out[write : write + frame] += padded[start : start + frame] * window
        weight[write : write + frame] += window
        filled = write + frame

        tail = start + hop_out
        target = (
            padded[tail : tail + frame]
            if tail + frame <= padded.size
            else None
        )
        write += hop_out
        read += hop_in

    # Divide out the window envelope. In the interior it is 1.0 by
    # construction; at the two ends only one frame contributed, so this
    # restores the original amplitude instead of leaving a fade -- which
    # on the first frame would soften the onset of every sentence.
    live = weight > 1e-6
    out[live] /= weight[live]
    return out[: min(target_len, filled)].astype(np.float32)


def varispeed(audio: np.ndarray, rate: float) -> np.ndarray:
    """The old behaviour: resample, moving pitch with duration.

    Kept for A/B comparison, and used by nothing in the live path. Having
    it callable is what makes "does the new stage actually preserve
    pitch?" a measurable question rather than an opinion -- see
    ``tests/test_timestretch.py``, which asserts that this shifts the
    fundamental and that :func:`time_stretch` does not.
    """
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    if flat.size == 0 or abs(rate - 1.0) < RATE_EPSILON:
        return flat
    count = max(1, int(round(flat.size / float(rate))))
    positions = np.linspace(0.0, flat.size - 1.0, count)
    return np.interp(positions, np.arange(flat.size), flat).astype(np.float32)
