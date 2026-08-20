"""Per-clip spectral tilt matching, so consecutive sentences share a voice.

The problem this solves
-----------------------
:mod:`app.audio.loudness` fixed the level drift between sentences, and
the remaining complaint was that "she is changing the warmth and level a
little between sentences". Level was measured flat by then -- 0.00 dB of
gated spread, 0.23 dB on a K-weighted check -- so the level half of that
was brightness being *heard* as level, which is what brightness does.

The warmth half is real, and the measurement that settles what it is:
regenerating **the same sentence** six times moved the low/high band
ratio by 4.2 dB and the spectral centroid by 206 Hz. Identical words, so
none of that is her delivery. The model re-samples timbre on every call,
and a per-clip correction is therefore removing noise, not expression.

Three things were ruled out first, and are recorded so they are not
re-tried:

* **Sampling parameters do not help.** ``temperature`` at 0.5 / 0.6 /
  0.7, ``cfg_weight``, ``min_p`` and ``repetition_penalty``, each with
  repeats, all land in the same 200-275 Hz centroid band. An early
  single-run result suggesting temperature 0.6 was a threefold
  improvement did not survive repetition.
* **It is not a bad engine.** pocket-tts drifts *more* on centroid
  (394 Hz against Nano's 245 Hz). Only Chatterbox is corrected here
  because that is where it is audible, not because it is the worse model.
* **It is not new.** What changed is that fixing the inter-sentence
  pauses put sentences next to each other, so drift that used to be
  separated by 2.5 s of silence became directly comparable. The fix
  exposed it.

Why a shelf, aimed at her reference clip
----------------------------------------
Tilt is measured as the ratio of low-band to high-band energy over gated
speech, and corrected with a single high-shelf biquad. Measured on the
real engine, that takes the identical-sentence spread from 2.5 dB to
0.4 dB and a varied turn from 2.9 dB to 0.5 dB, using corrections of
about ±1.6 dB.

The target is the tilt of the *reference clip being cloned* rather than a
number chosen here or a running average of the engine's own output. Two
reasons. It is absolute, so sentence one is corrected as confidently as
sentence five -- an adaptive target has to hear a few sentences first,
which is exactly when a reply is most audible. And it happens to be the
right target anyway: Nano's output sits consistently 1.2-1.5 dB warmer
than her reference, so aiming there also closes the high-frequency
deficit that reads as "a little muffled", which had been written off as
an architectural limit of the model's 16 kHz conditioning.

``MAX_CORRECTION_DB`` is the honest part of this. Content genuinely
changes brightness -- an excited three-word opener really is brighter
than a long calm question -- and an unbounded correction would flatten
that along with the noise. The limit is set above the noise (~4 dB) and
below the widest expressive swings, so it absorbs the former while
leaving most of the latter.
"""

from __future__ import annotations

import numpy as np

# At module scope on purpose. ``scipy.signal`` costs ~610 ms to import and
# the filter itself ~1 ms, so importing it lazily would have put the whole
# cost on the first sentence of the first reply -- the one place in the
# turn where latency is most audible. Here it is paid while the engine is
# still loading, which already takes seconds. scipy is a hard dependency
# (see pyproject), so this adds nothing to the install.
from scipy.signal import lfilter

from app.audio.loudness import gated_rms

#: Corner frequency of the correcting shelf. Above the first formant
#: region so the correction moves brightness rather than the body of the
#: voice, and low enough to cover the 2-6 kHz band the tilt is measured
#: over.
SHELF_HZ = 1500.0

#: The two bands whose ratio defines "warmth" here. The low band starts
#: at 100 Hz to stay clear of rumble, and the high band stops at 6 kHz
#: because Nano's 16 kHz conditioning leaves the top octave sparse and
#: noisy -- including it would measure the model's bandwidth limit rather
#: than its tilt.
LOW_BAND = (100.0, 1000.0)
HIGH_BAND = (2000.0, 6000.0)

#: Ceiling on the correction, in dB of shelf gain. Sized from the
#: measurements above: sampling noise accounts for roughly 4 dB of tilt
#: spread, and a 4 dB ceiling absorbs that while leaving the larger
#: content-driven differences mostly intact.
MAX_CORRECTION_DB = 4.0

#: Clips shorter than this are left alone. A very short clip has too few
#: voiced frames for a band ratio to mean anything, and "Oh!" is exactly
#: the kind of utterance whose brightness is legitimately unusual.
MIN_DURATION_S = 0.35


def spectral_tilt_db(audio: np.ndarray, sample_rate: int) -> float:
    """Low-band over high-band energy, in dB. Higher is warmer.

    Measured on the whole clip rather than gated frames: the bands are a
    ratio, so the silence that would drag an absolute level measurement
    down affects numerator and denominator alike.
    """
    if audio is None or sample_rate <= 0:
        return 0.0
    flat = np.asarray(audio, dtype=np.float64).reshape(-1)
    if flat.size < 16:
        return 0.0
    spectrum = np.abs(np.fft.rfft(flat))
    freqs = np.fft.rfftfreq(flat.size, 1.0 / float(sample_rate))
    low = float(np.sum(spectrum[(freqs >= LOW_BAND[0]) & (freqs < LOW_BAND[1])] ** 2))
    high = float(np.sum(spectrum[(freqs >= HIGH_BAND[0]) & (freqs < HIGH_BAND[1])] ** 2))
    if low <= 0.0 or high <= 0.0:
        return 0.0
    return 10.0 * float(np.log10(low / high))


def _high_shelf_coefficients(
    gain_db: float, sample_rate: int
) -> tuple[np.ndarray, np.ndarray]:
    """Audio-EQ-cookbook high shelf, slope 1."""
    amp = 10.0 ** (float(gain_db) / 40.0)
    w0 = 2.0 * np.pi * SHELF_HZ / float(sample_rate)
    cos_w0 = float(np.cos(w0))
    alpha = float(np.sin(w0)) / 2.0 * float(np.sqrt(2.0))
    sqrt_a_alpha = 2.0 * float(np.sqrt(amp)) * alpha

    b = np.array(
        [
            amp * ((amp + 1.0) + (amp - 1.0) * cos_w0 + sqrt_a_alpha),
            -2.0 * amp * ((amp - 1.0) + (amp + 1.0) * cos_w0),
            amp * ((amp + 1.0) + (amp - 1.0) * cos_w0 - sqrt_a_alpha),
        ],
        dtype=np.float64,
    )
    a = np.array(
        [
            (amp + 1.0) - (amp - 1.0) * cos_w0 + sqrt_a_alpha,
            2.0 * ((amp - 1.0) - (amp + 1.0) * cos_w0),
            (amp + 1.0) - (amp - 1.0) * cos_w0 - sqrt_a_alpha,
        ],
        dtype=np.float64,
    )
    return b / a[0], a / a[0]


def _filter(audio: np.ndarray, b: np.ndarray, a: np.ndarray) -> np.ndarray:
    """Run the biquad. Costs about 1 ms on a three-second clip."""
    flat = np.asarray(audio, dtype=np.float64).reshape(-1)
    return np.asarray(lfilter(b, a, flat), dtype=np.float64)


def correction_db(
    audio: np.ndarray,
    sample_rate: int,
    *,
    target_tilt_db: float,
    limit_db: float = MAX_CORRECTION_DB,
) -> float:
    """Shelf gain that moves this clip's tilt toward ``target_tilt_db``.

    Positive brightens (the clip was warmer than the target). Returns
    0.0 for clips too short or too quiet to measure, so a correction is
    never guessed from noise.
    """
    if audio is None or sample_rate <= 0:
        return 0.0
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    if flat.size < int(MIN_DURATION_S * sample_rate):
        return 0.0
    if gated_rms(flat, sample_rate) <= 0.0:
        return 0.0
    error = spectral_tilt_db(flat, sample_rate) - float(target_tilt_db)
    if not np.isfinite(error):
        return 0.0
    bound = abs(float(limit_db))
    return float(np.clip(error, -bound, bound))


def match_tilt(
    audio: np.ndarray,
    sample_rate: int,
    *,
    target_tilt_db: float,
    limit_db: float = MAX_CORRECTION_DB,
) -> np.ndarray:
    """Return ``audio`` shelved toward ``target_tilt_db``.

    Returns the input unchanged when no correction is warranted, so the
    caller pays nothing on the paths that do not need it.
    """
    gain = correction_db(
        audio, sample_rate, target_tilt_db=target_tilt_db, limit_db=limit_db
    )
    if abs(gain) < 0.1:
        return audio
    b, a = _high_shelf_coefficients(gain, sample_rate)
    filtered = _filter(audio, b, a)
    # The shelf can push peaks past full scale; the emission path clips,
    # so scale back rather than letting it distort.
    peak = float(np.max(np.abs(filtered))) if filtered.size else 0.0
    if peak > 1.0:
        filtered = filtered / peak
    return filtered.astype(np.float32, copy=False)
