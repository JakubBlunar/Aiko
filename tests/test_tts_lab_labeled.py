"""The screening rules for a labelled training set.

Every assertion here is about a mistake that survives into a trained
model. A clip kept at the wrong length gets truncated by the trainer, a
mixed-rate set either fails to load or resamples behind your back, and a
label that does not match its audio teaches a mispronunciation. None of
those raise at build time, and all of them cost a training run to find.
"""

from __future__ import annotations

import numpy as np
import pytest

from tools.tts_lab import labeled
from tools.tts_lab.adapters import resample, write_wav


def _tone(seconds: float, rate: int = 24000) -> np.ndarray:
    """Speech-ish: a couple of formant-like tones, not a pure sine.

    A pure sine trips the silence and RMS heuristics in odd ways; two
    summed tones with an envelope lands in the range real speech does.
    """
    t = np.linspace(0.0, seconds, int(rate * seconds), endpoint=False)
    wave = 0.35 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * 700 * t)
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 3.0 * t)
    return (wave * envelope).astype(np.float32)


# ── rate selection ──


def test_single_rate_is_left_alone() -> None:
    rate, why = labeled.choose_rate([24000] * 5)
    assert rate == 24000
    assert "only rate" in why


def test_mode_wins_over_the_outlier() -> None:
    """The reason this is the mode and not the max.

    One 48 kHz file should not force a resampling pass over nine 24 kHz
    ones. Upsampling invents nothing, so paying for it across the
    majority of the set buys only a bigger set.
    """
    rate, why = labeled.choose_rate([24000] * 9 + [48000])
    assert rate == 24000
    assert "48000 Hz x1" in why


def test_ties_go_to_the_higher_rate() -> None:
    rate, _ = labeled.choose_rate([22050, 44100])
    assert rate == 44100


def test_explicit_request_overrides() -> None:
    rate, why = labeled.choose_rate([24000] * 9 + [48000], 32000)
    assert (rate, why) == (32000, "requested")


# ── the resampler that feeds it ──


def _relative_amplitude(sig: np.ndarray, rate: int, lo: float, hi: float) -> float:
    """Strongest component in a band, as a fraction of full scale.

    Normalised by length so figures from clips of different durations are
    comparable: a unit-amplitude sine's FFT magnitude is ``N / 2``.
    """
    spectrum = np.abs(np.fft.rfft(sig))
    freqs = np.fft.rfftfreq(sig.size, 1.0 / rate)
    band = spectrum[(freqs > lo) & (freqs < hi)]
    return float(band.max()) / (sig.size / 2.0) if band.size else 0.0


def test_downsampling_does_not_alias() -> None:
    """A tone above the new Nyquist must be filtered, not folded.

    10 kHz resampled 48 k -> 16 k has nowhere legitimate to go: the new
    Nyquist is 8 kHz. A correct resampler removes it. Linear
    interpolation instead folds it to |16000 - 10000| = 6 kHz, inside the
    band, where it is indistinguishable from signal -- and in a training
    set that is permanent, because the model learns the folded noise as
    part of the voice.

    The contrast is the reason this test exists rather than a comment:
    polyphase leaves the image around -57 dB, linear leaves it at roughly
    *63% amplitude*. That is not a subtle quality difference.
    """
    rate_in, rate_out = 48000, 16000
    t = np.linspace(0.0, 0.5, rate_in // 2, endpoint=False)
    tone = np.sin(2 * np.pi * 10_000 * t).astype(np.float32)

    out = resample(tone, rate_in, rate_out)
    assert out.size == pytest.approx(rate_out // 2, rel=0.02)
    aliased = _relative_amplitude(out, rate_out, 5000, 7000)
    assert aliased < 0.02, f"aliased image at {aliased:.3f} of full scale"

    # The old behaviour, asserted so this cannot quietly regress to it.
    count = int(round(tone.size * rate_out / rate_in))
    linear = np.interp(
        np.linspace(0.0, tone.size - 1.0, count),
        np.arange(tone.size),
        tone,
    ).astype(np.float32)
    assert _relative_amplitude(linear, rate_out, 5000, 7000) > 0.3


def test_in_band_content_passes_through_unharmed() -> None:
    """The other half of the claim: the filter must not eat real signal."""
    rate_in, rate_out = 48000, 16000
    t = np.linspace(0.0, 0.5, rate_in // 2, endpoint=False)
    tone = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    out = resample(tone, rate_in, rate_out)
    assert _relative_amplitude(out, rate_out, 900, 1100) > 0.9


def test_upsampling_preserves_a_tone() -> None:
    t = np.linspace(0.0, 0.5, 12000, endpoint=False)
    tone = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    out = resample(tone, 24000, 48000)
    freqs = np.fft.rfftfreq(out.size, 1.0 / 48000)
    peak = freqs[int(np.argmax(np.abs(np.fft.rfft(out))))]
    assert peak == pytest.approx(1000, abs=20)


# ── build screening ──


def _write(tmp_path, name: str, seconds: float, rate: int = 24000):
    return write_wav(tmp_path / name, _tone(seconds, rate), rate)


def _text_for(seconds: float) -> str:
    """A transcript whose length plausibly matches ``seconds`` of speech.

    Needed because the screen being tested compares duration against
    length-derived expectation, so an arbitrarily short fixture label
    reads as a truncated clip -- the check firing on the test's own data
    rather than on anything it meant to assert.
    """
    from tools.tts_lab.dataset import CHARS_PER_SECOND

    words = (
        "the quick brown fox jumped over a very lazy dog while nobody "
        "was watching it happen at all today and then again later on"
    ).split()
    want = int(seconds * CHARS_PER_SECOND)
    out: list[str] = []
    size = 0
    while size < want:
        word = words[len(out) % len(words)]
        out.append(word)
        size += len(word) + 1
    return " ".join(out).capitalize() + "."


def test_untranscribed_clip_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "a.wav", 3.0)
    result, _ = labeled.build([labeled.Item(path, "  ")], tmp_path / "out")
    assert not result.samples
    assert result.rejects[0].reason == "no transcript"


def test_overlong_clip_is_rejected_not_truncated(tmp_path) -> None:
    """Silently keeping it would waste the label typed for it."""
    path = _write(tmp_path, "long.wav", labeled.MAX_SECONDS + 4.0)
    result, _ = labeled.build(
        [labeled.Item(path, "x " * 200)], tmp_path / "out"
    )
    assert not result.samples
    assert "too long" in result.rejects[0].reason


def test_tiny_clip_is_rejected(tmp_path) -> None:
    path = _write(tmp_path, "short.wav", 0.3)
    result, _ = labeled.build([labeled.Item(path, "hi")], tmp_path / "out")
    assert not result.samples
    assert "too short" in result.rejects[0].reason


def test_label_far_longer_than_the_audio_is_caught(tmp_path) -> None:
    """A transcript that cannot fit its clip is a mislabel.

    Three seconds of audio against a paragraph means the audio is cut
    off, the label belongs to another file, or the pairing slipped by
    one. All three poison a fine-tune, and none are visible in the
    waveform.
    """
    path = _write(tmp_path, "mismatch.wav", 2.0)
    result, _ = labeled.build(
        [labeled.Item(path, "word " * 120)], tmp_path / "out"
    )
    assert not result.samples
    assert "truncated" in result.rejects[0].reason


def test_missing_file_is_reported_by_name(tmp_path) -> None:
    result, _ = labeled.build(
        [labeled.Item(tmp_path / "nope.wav", "hello")], tmp_path / "out"
    )
    assert result.rejects[0].reason == "missing file"
    assert result.rejects[0].text == "nope.wav"


def test_good_clips_build_and_keep_provenance(tmp_path) -> None:
    items = [
        labeled.Item(_write(tmp_path, "one.wav", 3.0), _text_for(3.0)),
        labeled.Item(_write(tmp_path, "two.wav", 4.0), _text_for(4.0)),
    ]
    out = tmp_path / "out"
    result, info = labeled.build(items, out)
    assert len(result.samples) == 2
    assert result.sample_rate == 24000
    assert info["resampled"] == 0
    # Renamed to an index for the manifest, so the original filename is
    # the only way back to where a bad clip came from.
    assert [s.source for s in result.samples] == ["one.wav", "two.wav"]
    assert [s.file for s in result.samples] == ["0001.wav", "0002.wav"]
    assert (out / "wavs" / "0001.wav").exists()


def test_mixed_rates_converge_on_one_rate(tmp_path) -> None:
    items = [
        labeled.Item(_write(tmp_path, "a.wav", 3.0, 24000), _text_for(3.0)),
        labeled.Item(_write(tmp_path, "b.wav", 3.0, 24000), _text_for(3.0)),
        labeled.Item(_write(tmp_path, "c.wav", 3.0, 44100), _text_for(3.0)),
    ]
    result, info = labeled.build(items, tmp_path / "out")
    assert result.sample_rate == 24000
    assert info["resampled"] == 1
    assert len(result.samples) == 3


def test_manifests_survive_a_pipe_in_the_transcript(tmp_path) -> None:
    """Both manifest formats are pipe-delimited, so a pipe would shift
    every field after it and silently corrupt the set."""
    item = labeled.Item(
        _write(tmp_path, "p.wav", 3.0), _text_for(2.0) + " Wait | what was that?"
    )
    out = tmp_path / "out"
    result, _ = labeled.build([item], out)
    assert result.samples
    labeled.write_manifests(result, out, speaker="aiko")
    for line in (out / "metadata.csv").read_text(encoding="utf-8").splitlines():
        assert line.count("|") == 2
    for line in (out / "aiko.list").read_text(encoding="utf-8").splitlines():
        assert line.count("|") == 3


# ── manifest reading ──


def test_manifest_paths_resolve_beside_the_manifest(tmp_path) -> None:
    """So a label file works from any working directory."""
    (tmp_path / "clips").mkdir()
    _write(tmp_path / "clips", "x.wav", 2.0)
    manifest = tmp_path / "labels.tsv"
    manifest.write_text(
        "# a comment\n\nclips/x.wav\tHello there.\n", encoding="utf-8"
    )
    items = labeled.read_manifest(manifest)
    assert len(items) == 1
    assert items[0].path.exists()
    assert items[0].text == "Hello there."


def test_manifest_accepts_pipes_too(tmp_path) -> None:
    manifest = tmp_path / "labels.txt"
    manifest.write_text("a.wav|Some words.\n", encoding="utf-8")
    items = labeled.read_manifest(manifest)
    assert items[0].text == "Some words."
