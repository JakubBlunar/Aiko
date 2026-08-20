"""Per-clip tempo matching, so consecutive sentences keep one pace.

The problem this solves
-----------------------
Third and last of the per-clip drifts, after :mod:`app.audio.loudness`
(level) and :mod:`app.audio.timbre` (brightness). The report was that the
first sentence of a reply "was spoken really great" and the second "was
much slower, that would need speeding up" -- and the natural suspicion was
the pacing slider or the affect-driven speed channel.

Neither. ``agent.tts_runtime_speed_enabled`` defaults off and is off in
the live config, so every sentence in that reply was handed the *identical*
rate multiplier. The variation was the model's.

The measurement that settles it: synthesising **the same sentence** six
times moves the delivered tempo by 10.7% to 20.9% depending on the
sentence, and across every clip measured the spread was 28% with the worst
clip 21.7% off her established pace. Identical words, so none of that is
her delivery -- exactly the shape of the timbre finding, one axis over.

Two readings were checked and rejected before building this:

* **It is not a length effect.** The first pass measured characters per
  second and found the long sentence 14% slower, which looked like a tidy
  explanation. In syllables per second the three test sentences average
  6.7, 6.7 and 7.0 -- indistinguishable. Character density differs between
  sentences; tempo does not. Anything measuring rate must count syllables.
* **It is not the pacing slider.** ``assistant.tts_length_scale`` is a
  single constant applied to every sentence alike. What it *does* do is
  make the noise worse to live with: an 8% deliberate slowdown on top of a
  draw that was already 20% slow is the sentence that gets complained
  about.

Why the target is her incumbent tempo
-------------------------------------
The 12 pocket-tts reference parts -- her voice as it has always sounded --
have a median of 6.55 syllables per second, and that is the target. Same
reasoning as the tilt target in :mod:`app.audio.timbre`: it is absolute,
so sentence one is corrected as confidently as sentence five, where an
adaptive target would have to hear a few sentences first and that is
precisely when a reply is most audible.

Correcting toward it takes the delivered spread from 28.2% to 10.2% and
puts seven of eight sentences within 5% of her pace, using stretches of up
to ±15%. The stretch is the pitch-preserving one in
:mod:`app.audio.timestretch`, and it is *folded into the speed factor the
playback path already applies*, so this costs no extra pass.

It also removes a bias worth naming: Chatterbox's raw output averages
6.26 syllables per second against her incumbent 6.55, so it has been
running about 5% slow all along, on top of the noise.

``MAX_CORRECTION`` is the honest part, as with tilt. Real speech varies
its tempo by sentence, and her own reference parts span 5.71 to 8.52
syllables per second -- an "Oh! I did not expect that at all." is
genuinely faster than a long calm statement. That variation is
*text-correlated*, though, while the drift measured here is random on
identical text, and a fixed-target correction cannot tell them apart. So
the limit is set above the noise and below the widest expressive swings:
it absorbs the former and leaves most of the latter.

The composition rule that keeps this honest
-------------------------------------------
The correction normalises the *realised* rate to ``target × intended``,
never to the bare target -- so when the affect channel is switched on, a
sentence asked to be 6% faster is delivered 6% faster instead of somewhere
in a ±14% cloud. Flattening intent would be the one way to make this
feature worse than the noise it removes, and it is why
:func:`correction_factor` takes the intended multiplier rather than being
applied after it.
"""

from __future__ import annotations

import re

import numpy as np

from app.audio.loudness import gated_rms

#: Her established pace, in syllables of *voiced* speech per second.
#: Measured, not chosen: the median over the twelve pocket-tts reference
#: parts in ``voices/reference/manifest.json``. Engines derive their own
#: target from that manifest when it is present; this is the fallback and
#: the documented value of "how fast Aiko talks".
DEFAULT_TARGET_SYL_S = 6.55

#: Ceiling on the stretch, as a fraction. Swept on the real engine over
#: eight sentences, measuring the tempo actually delivered:
#:
#: === ====== =========== ==================
#: cap spread worst clip  within 5% of target
#: === ====== =========== ==================
#: off  28.2%      13.7%  1 of 8
#: .10  21.5%      15.2%  6 of 8
#: .15  10.2%       8.9%  7 of 8
#: .20   8.1%       5.3%  7 of 8
#: === ====== =========== ==================
#:
#: 0.10 is not enough: it lands six of eight but leaves the tails, and one
#: draw 20% off her pace is what a listener actually notices. The knee is
#: at 0.15 -- eleven points of spread for the step from 0.10, two more for
#: the step to 0.20 -- and stopping there leaves more of the model's own
#: sentence-to-sentence variation intact, which is the thing a cap exists
#: to protect.
MAX_CORRECTION = 0.15

#: Clips with less voiced speech than this are left alone. Below about a
#: third of a second the syllable count is small enough that being one
#: out is a 30% error in the rate, and short interjections are exactly
#: where tempo is legitimately unusual.
MIN_SPEECH_S = 0.35

#: And below this many syllables, for the same reason from the other side.
MIN_SYLLABLES = 3

#: Text that is mostly not letters -- a URL, a number-heavy line, an
#: emoticon -- gets no correction, because the syllable estimate below is
#: a letter-pattern heuristic and would be guessing.
MIN_ALPHA_RATIO = 0.55

_WORD = re.compile(r"[A-Za-z']+")
_VOWEL_GROUP = re.compile(r"[aeiouy]+")
_FRAME_SECONDS = 0.010
_SMOOTH_FRAMES = 5

#: Frames this far below the clip's gated level count as silence. Looser
#: than ``loudness.GATE_DB`` on purpose: here the goal is to exclude the
#: pauses *between* words so tempo is measured on speech alone, not to
#: preserve every quiet syllable in a level estimate.
_VOICED_FRACTION = 0.1


def syllables(text: str) -> int:
    """Estimated syllable count. Vowel groups, minus silent trailing 'e'.

    Deliberately a heuristic rather than a dictionary lookup: it runs on
    the speak thread in front of the first audio the client hears, and
    the correction it feeds is bounded and gated on
    :data:`MIN_ALPHA_RATIO`, so being one syllable out on an unusual word
    cannot move the result far. Returns 0 for text with nothing to count,
    which callers read as "do not correct".
    """
    words = _WORD.findall(text or "")
    if not words:
        return 0
    total = 0
    for word in words:
        lowered = word.lower()
        count = len(_VOWEL_GROUP.findall(lowered))
        if lowered.endswith("e") and count > 1:
            count -= 1
        total += max(1, count)
    return total


def is_measurable(text: str) -> bool:
    """Whether :func:`syllables` can be trusted for this text."""
    stripped = (text or "").strip()
    if not stripped:
        return False
    letters = sum(1 for ch in stripped if ch.isalpha() or ch.isspace())
    if not letters:
        return False
    return letters / len(stripped) >= MIN_ALPHA_RATIO


def speech_seconds(audio: np.ndarray, sample_rate: int) -> float:
    """Duration of the voiced part of the clip.

    Not the clip length: leading and trailing silence, and the pauses at
    commas, would otherwise read as slow speech -- and a sentence with a
    comma in it is not spoken more slowly than one without.
    """
    if audio is None or sample_rate <= 0:
        return 0.0
    flat = np.asarray(audio, dtype=np.float64).reshape(-1)
    hop = max(1, int(_FRAME_SECONDS * sample_rate))
    if flat.size < hop:
        return 0.0
    usable = (flat.size // hop) * hop
    frames = flat[:usable].reshape(-1, hop)
    rms = np.sqrt(np.mean(frames**2, axis=1) + 1e-12)
    # Syllable nuclei sit around 4-8 Hz, so a ~50 ms box keeps the
    # syllable structure and drops glottal-period detail.
    kernel = np.ones(_SMOOTH_FRAMES) / float(_SMOOTH_FRAMES)
    smooth = np.convolve(rms, kernel, mode="same")
    floor = gated_rms(flat, sample_rate) * _VOICED_FRACTION
    if floor <= 0.0:
        return 0.0
    return float(np.count_nonzero(smooth > floor)) * hop / float(sample_rate)


def measured_rate(audio: np.ndarray, sample_rate: int, text: str) -> float:
    """Delivered tempo of this clip, in syllables per second of speech.

    0.0 when it cannot be measured, which callers read as "do not
    correct" rather than as "infinitely fast".
    """
    count = syllables(text)
    if count < MIN_SYLLABLES or not is_measurable(text):
        return 0.0
    speech = speech_seconds(audio, sample_rate)
    if speech < MIN_SPEECH_S:
        return 0.0
    return count / speech


def correction_factor(
    audio: np.ndarray,
    sample_rate: int,
    text: str,
    *,
    target_syl_s: float = DEFAULT_TARGET_SYL_S,
    intended: float = 1.0,
    limit: float = MAX_CORRECTION,
) -> float:
    """Extra speed multiplier that brings this clip to the wanted tempo.

    ``intended`` is the multiplier the rest of the pipeline already means
    to apply (reaction speed, cadence hint, pacing slider). The tempo
    aimed for is ``target_syl_s * intended``, so the correction removes
    the model's noise without erasing a deliberate choice -- see the
    module docstring.

    Returns exactly 1.0 whenever the clip or the text cannot support a
    measurement, so the caller can multiply unconditionally.
    """
    if target_syl_s <= 0.0:
        return 1.0
    rate = measured_rate(audio, sample_rate, text)
    if rate <= 0.0:
        return 1.0
    wanted = float(target_syl_s) * max(0.1, float(intended))
    factor = wanted / rate
    if not np.isfinite(factor) or factor <= 0.0:
        return 1.0
    bound = abs(float(limit))
    return float(np.clip(factor, 1.0 - bound, 1.0 + bound))
