"""Per-clip loudness matching, so consecutive sentences sit at one level.

The problem this solves
-----------------------
Aiko speaks a turn a sentence at a time: each sentence is an independent
synthesis, and until this module nothing downstream cared what level came
back. Measured over one twelve-sentence turn, gated speech level varied
by **8.3 dB on Chatterbox Nano and 8.4 dB on pocket-tts** -- so this is
not a quirk of one engine, it is what per-sentence synthesis does when
nobody matches the levels. It reads as someone moving her microphone
between sentences.

Worth stating plainly, because the first pass at this got it wrong: on
small samples Chatterbox looked three times worse than pocket-tts, and on
equal footing the two are indistinguishable. What *is* specific to
Chatterbox is a high-frequency deficit (about 59% of pocket-tts's energy
above 4 kHz), and no amount of gain fixes that -- see
``docs/tts-engine-options.md``.

Normalising each clip to a fixed target collapses the spread to nothing.
It also makes the *deliberate* level changes work properly for the first
time. ``gain_db`` -- ambient-noise compensation, plus the deltas from
``[[prosody:...]]`` tags -- was being applied on top of a random base, so
a tag asking for 3 dB softer was routinely swamped by an 8 dB swing in
the other direction. With the base pinned, gain means what it says.

Why gated RMS
-------------
Not whole-clip RMS: a clip is speech plus leading and trailing silence,
so whole-clip RMS falls as the silence fraction rises. Short utterances
("Oh!") are mostly silence and would be boosted hardest, which is exactly
backwards.

Not peak: the peak of a speech clip is one sample of one plosive. It says
nothing about loudness and everything about which consonant happened to
be loudest.

So: frame the clip, drop the frames that are more than ``GATE_DB`` below
the loudest frame, and take the RMS of what remains. That is the same
idea as the relative gate in ITU-R BS.1770, minus the K-weighting
filters and 400 ms blocks -- both of which assume material far longer
than a 2-second sentence. The gate is what matters here; the weighting
would refine a number that is already well inside the audible threshold.
"""

from __future__ import annotations

import numpy as np

#: Target gated speech level. Measured, not chosen: pocket-tts is the
#: incumbent and the loudness everyone is used to, and it averages -25.7
#: and -26.1 dBFS gated over two twelve-sentence runs. -26 sits inside
#: both, so switching normalisation on does not change how loud she is --
#: and neither does switching engine, which is the other half of the
#: point.
DEFAULT_TARGET_DBFS = -26.0

#: True peak ceiling. Speech has a high crest factor, so reaching the RMS
#: target can ask for a peak above full scale; the correction is backed
#: off rather than allowed to clip, because clipped speech is a far worse
#: artefact than being half a dB under target.
PEAK_CEILING_DBFS = -1.0

#: Frames quieter than this relative to the loudest frame are treated as
#: silence and excluded. -30 dB keeps genuine quiet speech (an unstressed
#: final syllable) and excludes room tone and inter-word gaps.
GATE_DB = -30.0

#: How far the correction may reach, in dB. A clip that needs more than
#: this is not quiet, it is broken -- near-silent, or a failed generation
#: that is mostly noise floor -- and boosting it 30 dB would turn a
#: silent failure into a loud one.
MAX_CORRECTION_DB = 12.0

_FRAME_SECONDS = 0.02


def _db(linear: float) -> float:
    return 20.0 * float(np.log10(max(float(linear), 1e-12)))


def _from_db(db: float) -> float:
    return float(10.0 ** (float(db) / 20.0))


def gated_rms(audio: np.ndarray, sample_rate: int) -> float:
    """Speech-only RMS, as a linear amplitude. 0.0 when there is none."""
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    if flat.size == 0 or sample_rate <= 0:
        return 0.0

    frame = max(1, int(sample_rate * _FRAME_SECONDS))
    if flat.size < frame:
        return float(np.sqrt(np.mean(flat.astype(np.float64) ** 2)))

    # Trim to whole frames and reshape, rather than looping: a 3-second
    # clip is 150 frames and this runs on the speak thread, in front of
    # the first audio the client hears.
    usable = (flat.size // frame) * frame
    frames = flat[:usable].astype(np.float64).reshape(-1, frame)
    power = np.mean(frames**2, axis=1)
    if not power.size:
        return 0.0

    loudest = float(power.max())
    if loudest <= 0.0:
        return 0.0
    gate = loudest * (_from_db(GATE_DB) ** 2)
    kept = power[power >= gate]
    if not kept.size:
        kept = power
    return float(np.sqrt(kept.mean()))


def correction_factor(
    audio: np.ndarray,
    sample_rate: int,
    *,
    target_dbfs: float = DEFAULT_TARGET_DBFS,
) -> float:
    """The linear multiplier that puts this clip at ``target_dbfs``.

    Returns 1.0 when the clip is empty or silent, so a caller can apply
    the result unconditionally.
    """
    flat = np.asarray(audio, dtype=np.float32).reshape(-1)
    if flat.size == 0:
        return 1.0

    speech = gated_rms(flat, sample_rate)
    if speech <= 0.0:
        return 1.0

    wanted_db = float(target_dbfs) - _db(speech)
    wanted_db = max(-MAX_CORRECTION_DB, min(MAX_CORRECTION_DB, wanted_db))
    factor = _from_db(wanted_db)

    # Back off to keep the true peak under the ceiling. Deliberately not
    # a limiter: dynamics inside the sentence are hers, and compressing
    # them to hit a number would flatten the delivery this whole file
    # exists to preserve.
    peak = float(np.abs(flat).max())
    if peak > 0.0:
        ceiling = _from_db(PEAK_CEILING_DBFS)
        if peak * factor > ceiling:
            factor = ceiling / peak

    return float(factor)
