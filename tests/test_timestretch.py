"""Does the time-stretch actually decouple rate from pitch?

That is the entire claim, and it is the kind of claim that is easy to
believe and easy to get wrong -- a subtly broken overlap-add still
produces plausible-looking audio of the right length. So the fundamental
frequency is measured before and after, and the old varispeed path is
measured alongside as a control: these tests assert both that the new
stage holds pitch *and* that the thing it replaced does not. Without the
control, a test that measured F0 incorrectly would pass for both.
"""

from __future__ import annotations

import numpy as np
import pytest

from app.audio.timestretch import (
    RATE_MAX,
    RATE_MIN,
    time_stretch,
    varispeed,
)

RATE = 24000


def _voiced(
    f0: float = 200.0,
    seconds: float = 1.0,
    rate: int = RATE,
    jitter: float = 0.0,
) -> np.ndarray:
    """A vowel-like signal: a fundamental plus harmonics, not a sine.

    Harmonics matter. A pure sine survives almost any resampling scheme
    intact, so it cannot distinguish a working stretch from a broken one.
    A harmonic stack has phase relationships that a bad overlap-add
    visibly destroys.
    """
    t = np.arange(int(rate * seconds), dtype=np.float64) / rate
    phase = 2.0 * np.pi * f0 * t
    if jitter:
        # Slight frequency drift, so the search cannot rely on a
        # perfectly stationary signal -- real speech never is.
        phase += jitter * np.sin(2.0 * np.pi * 3.0 * t)
    signal = (
        1.0 * np.sin(phase)
        + 0.5 * np.sin(2 * phase)
        + 0.25 * np.sin(3 * phase)
        + 0.12 * np.sin(4 * phase)
    )
    return (signal / np.abs(signal).max() * 0.7).astype(np.float32)


def _fundamental(audio: np.ndarray, rate: int = RATE) -> float:
    """Estimate F0 by autocorrelation.

    Deliberately not an FFT peak-pick: the harmonic stack above has a
    strong second harmonic, and a naive spectral maximum would sometimes
    report 400 Hz for a 200 Hz voice, which would make the assertions
    below meaningless. Autocorrelation over a plausible speech range
    finds the period instead.
    """
    signal = np.asarray(audio, dtype=np.float64).reshape(-1)
    signal = signal - signal.mean()
    if signal.size < rate // 20:
        raise AssertionError("clip too short to estimate F0")
    correlation = np.correlate(signal, signal, mode="full")[signal.size - 1 :]
    lo = int(rate / 500.0)  # 500 Hz ceiling
    hi = int(rate / 70.0)   # 70 Hz floor
    lag = lo + int(np.argmax(correlation[lo:hi]))
    return rate / float(lag)


# ── the central claim ──


@pytest.mark.parametrize("rate", [0.85, 0.92, 1.08, 1.15])
def test_pitch_is_preserved_across_the_working_range(rate: float) -> None:
    """The range that matters: the service clamps speed to 0.88-1.12 and
    the pacing slider to 0.85-1.15."""
    source = _voiced(f0=200.0, seconds=1.0)
    stretched = time_stretch(source, rate, RATE)
    measured = _fundamental(stretched)
    assert measured == pytest.approx(200.0, rel=0.04), (
        f"pitch moved to {measured:.1f} Hz at rate {rate}"
    )


@pytest.mark.parametrize("rate", [0.85, 1.15])
def test_varispeed_moves_pitch_and_is_the_thing_being_replaced(
    rate: float,
) -> None:
    """The control. If this ever passes, the test above proves nothing."""
    source = _voiced(f0=200.0, seconds=1.0)
    measured = _fundamental(varispeed(source, rate))
    # Varispeed scales pitch by exactly the rate factor.
    assert measured == pytest.approx(200.0 * rate, rel=0.04)
    assert abs(measured - 200.0) > 10.0


def test_pitch_holds_on_a_non_stationary_signal() -> None:
    """Real speech drifts; the similarity search must cope."""
    source = _voiced(f0=180.0, seconds=1.2, jitter=0.35)
    measured = _fundamental(time_stretch(source, 1.12, RATE))
    assert measured == pytest.approx(180.0, rel=0.06)


# ── duration ──


@pytest.mark.parametrize("rate", [0.85, 0.9, 1.1, 1.15, 1.5])
def test_duration_follows_the_requested_rate(rate: float) -> None:
    source = _voiced(seconds=2.0)
    out = time_stretch(source, rate, RATE)
    assert out.size == pytest.approx(source.size / rate, rel=0.01)


@pytest.mark.parametrize("seconds", [0.15, 0.3, 0.6])
def test_short_utterances_keep_their_duration(seconds: float) -> None:
    """Regression: the overlap-add loop stops when a full frame no longer
    fits, which silently dropped up to 30 ms off the end.

    On a two-second clip that is 1% and hides inside any loose
    tolerance. On "Oh!" it was 14%, and it is the tail -- a trailing
    consonant or the decay of the final vowel. These lengths are what
    Aiko's one-word reactions actually are.
    """
    source = _voiced(seconds=seconds)
    for rate in (0.88, 1.12):
        out = time_stretch(source, rate, RATE)
        assert out.size == pytest.approx(source.size / rate, rel=0.06), (
            f"{seconds}s at rate {rate}: got {out.size / RATE:.3f}s, "
            f"wanted {seconds / rate:.3f}s"
        )


def test_the_end_of_the_utterance_survives() -> None:
    """The tail must still contain signal, not be truncated to silence.

    Checks energy rather than length, because a run that pads with zeros
    would satisfy a duration assertion while having thrown the audio
    away.
    """
    source = _voiced(seconds=0.4)
    out = time_stretch(source, 1.12, RATE)
    tail = out[-int(RATE * 0.02) :]
    assert float(np.sqrt(np.mean(tail**2))) > 0.05


def test_faster_is_shorter_and_slower_is_longer() -> None:
    """Guards the sign convention, which is easy to invert silently."""
    source = _voiced(seconds=1.0)
    assert time_stretch(source, 1.15, RATE).size < source.size
    assert time_stretch(source, 0.85, RATE).size > source.size


# ── invariants and edges ──


def test_unity_rate_returns_the_input_untouched() -> None:
    """No processing at all, so no artefact from a no-op call."""
    source = _voiced(seconds=0.5)
    out = time_stretch(source, 1.0, RATE)
    assert np.array_equal(out, source)


def test_negligible_rate_change_is_also_a_no_op() -> None:
    source = _voiced(seconds=0.5)
    assert np.array_equal(time_stretch(source, 1.0002, RATE), source)


def test_output_is_float32_and_finite() -> None:
    out = time_stretch(_voiced(seconds=1.0), 1.1, RATE)
    assert out.dtype == np.float32
    assert np.all(np.isfinite(out))


def test_amplitude_is_not_inflated() -> None:
    """The window-sum normalisation must not overshoot into clipping.

    An overlap-add that divides by a window sum near zero at the edges
    is the classic way to manufacture a click at the start of every
    sentence.
    """
    source = _voiced(seconds=1.0)
    out = time_stretch(source, 1.1, RATE)
    assert np.abs(out).max() <= np.abs(source).max() * 1.05


def test_no_edge_click() -> None:
    """First and last samples should not be wildly louder than their
    neighbourhood, which is what a bad envelope division produces."""
    out = time_stretch(_voiced(seconds=1.0), 0.9, RATE)
    body = float(np.sqrt(np.mean(out[RATE // 10 : -RATE // 10] ** 2)))
    assert abs(float(out[0])) < body * 4.0
    assert abs(float(out[-1])) < body * 4.0


def test_empty_input() -> None:
    assert time_stretch(np.zeros(0, dtype=np.float32), 1.2, RATE).size == 0


def test_silence_stays_silent() -> None:
    out = time_stretch(np.zeros(RATE, dtype=np.float32), 1.1, RATE)
    assert np.all(out == 0.0)


def test_very_short_clip_is_returned_rather_than_pitch_shifted() -> None:
    """Documented tradeoff: a 20 ms clip has nothing to search over, and
    resampling it would reintroduce the pitch shift being removed. A
    duration error there is inaudible; a pitch jump would not be."""
    source = _voiced(seconds=0.02)
    out = time_stretch(source, 1.15, RATE)
    assert np.array_equal(out, source)


@pytest.mark.parametrize("rate", [0.05, 10.0, -3.0])
def test_absurd_rates_are_clamped_not_crashed(rate: float) -> None:
    out = time_stretch(_voiced(seconds=1.0), rate, RATE)
    assert out.size > 0
    assert np.all(np.isfinite(out))
    bound = 1.0 / RATE_MIN if rate < 1.0 else 1.0 / RATE_MAX
    assert out.size == pytest.approx(RATE * bound, rel=0.05)


def test_mono_shape_is_enforced() -> None:
    """The service hands over whatever numpy shape the model produced."""
    source = _voiced(seconds=0.5).reshape(1, -1)
    out = time_stretch(source, 1.1, RATE)
    assert out.ndim == 1


def test_works_at_other_sample_rates() -> None:
    """Frame and search sizes are derived from the rate, not hardcoded."""
    for rate_hz in (16000, 22050, 44100):
        source = _voiced(f0=200.0, seconds=1.0, rate=rate_hz)
        out = time_stretch(source, 1.1, rate_hz)
        assert out.size == pytest.approx(source.size / 1.1, rel=0.03)
        assert _fundamental(out, rate_hz) == pytest.approx(200.0, rel=0.05)
