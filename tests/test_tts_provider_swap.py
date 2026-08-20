"""Swapping TTS engines while she is running.

The framework's whole point is that the provider can change mid-session
without the audio path noticing, so these tests care about the seams
rather than about either engine's synthesis: that the swap is refused
when it cannot work, that the PCM listener and queue survive it, and that
per-provider voices are not clobbered on the way through.

The Chatterbox engine is exercised against a fake sidecar -- a plain
Python script speaking the same JSON protocol -- so the process
supervision, timeouts and WAV round trip are all real while the model is
not. A test that needed torch 2.6 installed would not run anywhere.
"""

from __future__ import annotations

import json
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from app.core.infra.settings import TtsProviderSettings, TtsSettings
from app.tts import registry

FAKE_SIDECAR = '''
import json, sys, wave, math, struct

def reply(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    op = msg.get("op")
    if op == "quit":
        break
    if op == "load":
        reply({"ok": True, "sample_rate": 24000, "load_ms": 1.0,
               "accepts": [], "defaults": {}, "device": "cpu",
               "torch": "2.6.0+cpu", "python": "3.12.9"})
    elif op == "clone":
        reply({"ok": True, "voice": 0})
    elif op == "synth":
        rate, seconds = 24000, 0.2
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(i * 0.05)))
            for i in range(int(rate * seconds))
        )
        with wave.open(msg["out"], "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(frames)
        reply({"ok": True, "total_ms": 1.0, "sample_rate": rate,
               "samples": int(rate * seconds), "out": msg["out"]})
    else:
        reply({"ok": False, "error": "unknown op"})
'''


@pytest.fixture
def fake_engine(tmp_path: Path):
    """A ChatterboxTtsService whose sidecar is a stub, not a model."""
    from app.tts.chatterbox_service import ChatterboxTtsService

    sidecar = tmp_path / "fake_sidecar.py"
    sidecar.write_text(FAKE_SIDECAR, encoding="utf-8")
    voices = tmp_path / "voices"
    (voices / "reference").mkdir(parents=True)
    reference = voices / "reference" / "aiko_reference.wav"
    with wave.open(str(reference), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(24000)
        handle.writeframes(b"\x00\x00" * 2400)

    with patch("app.tts.chatterbox_service.VOICES_DIR", voices):
        engine = ChatterboxTtsService(
            TtsSettings(provider="chatterbox-nano", voice="", enabled=True),
            interpreter=Path(sys.executable),
            sidecar=sidecar,
            engine_key="chatterbox-nano",
        )
        try:
            yield engine
        finally:
            engine.shutdown()


# ── the engine contract ──


def test_engine_satisfies_the_protocol(fake_engine) -> None:
    """The queue and session call these by duck typing, so a missing one
    fails at runtime on a real utterance rather than at construction."""
    for method in (
        "get_status", "warmup_sync", "warmup_async", "stop",
        "set_pcm_listener", "speak_async", "list_voices", "set_voice",
        "reaction_to_speed", "speak_silence_async", "generate_audio",
        "release_model", "load_model_now",
    ):
        assert callable(getattr(fake_engine, method, None)), method


def test_it_loads_clones_and_reports_ready(fake_engine) -> None:
    assert fake_engine.warmup_sync() is True
    state, message = fake_engine.get_status()
    assert state == "ready", message
    described = fake_engine.describe()
    assert described["sample_rate"] == 24000
    assert described["voice"] == "reference/aiko_reference.wav"


def test_speaking_emits_paced_pcm_and_fires_clip_end(fake_engine) -> None:
    assert fake_engine.warmup_sync()
    chunks: list[bytes] = []
    rates: set[int] = set()
    ended = threading.Event()
    fake_engine.set_pcm_listener(
        lambda rate, channels, pcm: (chunks.append(pcm), rates.add(rate)),
        end_listener=ended.set,
    )
    done = threading.Event()
    amplitudes: list[float] = []
    fake_engine.speak_async(
        "hello", on_done=done.set, on_amplitude=amplitudes.append
    )
    assert done.wait(timeout=30.0), "speak never completed"
    assert chunks, "no PCM emitted"
    assert rates == {24000}
    assert ended.is_set()
    # Lip sync has to keep working across a provider swap; it is driven
    # from the shared playback path rather than from either engine.
    assert amplitudes


def test_reaction_speed_matches_pocket_tts(fake_engine) -> None:
    """Her pacing describes her, not the model rendering her, so a swap
    must not quietly change how fast she talks when excited."""
    from app.tts.reactions import REACTION_SPEED

    for reaction, expected in REACTION_SPEED.items():
        assert fake_engine.reaction_to_speed(reaction) == expected
    assert fake_engine.reaction_to_speed(None) == 1.0
    assert fake_engine.reaction_to_speed("not-a-reaction") == 1.0


def test_release_model_ends_the_child_process(fake_engine) -> None:
    """On CUDA this is how VRAM actually returns to the system, which is
    the entire point when TTS is switched off to play a game."""
    assert fake_engine.warmup_sync()
    assert fake_engine._proc is not None  # noqa: SLF001
    assert fake_engine.release_model() is True
    assert fake_engine._proc is None  # noqa: SLF001
    state, _ = fake_engine.get_status()
    assert state == "error"


def test_it_reloads_after_release(fake_engine) -> None:
    assert fake_engine.warmup_sync()
    fake_engine.release_model()
    fake_engine.load_model_now()
    assert fake_engine.warmup_sync() is True
    assert fake_engine.get_status()[0] == "ready"


def test_a_missing_reference_clip_is_a_clear_failure(tmp_path: Path) -> None:
    """Rather than a tensor error from inside a foreign venv."""
    from app.tts.chatterbox_service import ChatterboxTtsService

    sidecar = tmp_path / "fake_sidecar.py"
    sidecar.write_text(FAKE_SIDECAR, encoding="utf-8")
    with patch("app.tts.chatterbox_service.VOICES_DIR", tmp_path / "nope"):
        engine = ChatterboxTtsService(
            TtsSettings(provider="chatterbox-nano", voice="", enabled=True),
            interpreter=Path(sys.executable),
            sidecar=sidecar,
            engine_key="chatterbox-nano",
        )
        try:
            assert engine.warmup_sync() is False
            state, message = engine.get_status()
            assert state == "error"
            assert "reference clip not found" in message
        finally:
            engine.shutdown()


def test_a_dead_sidecar_does_not_hang_the_speak_thread(tmp_path: Path) -> None:
    """TtsQueue waits on that thread, so a wedged child must surface as
    an error rather than stopping her voice with no explanation."""
    from app.tts.chatterbox_service import ChatterboxTtsService

    sidecar = tmp_path / "exits.py"
    sidecar.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    engine = ChatterboxTtsService(
        TtsSettings(provider="chatterbox-nano", voice="", enabled=True),
        interpreter=Path(sys.executable),
        sidecar=sidecar,
        engine_key="chatterbox-nano",
    )
    try:
        assert engine.warmup_sync() is False
        assert engine.get_status()[0] == "error"
    finally:
        engine.shutdown()


# ── the swap itself ──


from app.core.session.voice_mixin import VoiceMixin  # noqa: E402


class _Session(VoiceMixin):
    """The real mixin, with only the collaborators a swap touches.

    Inheriting rather than binding methods one by one, so these tests
    exercise the shipped logic instead of a paraphrase of it.
    """

    def __init__(self, provider: str = "pocket-tts") -> None:
        self.tts = TtsSettings(
            provider=provider,
            voice="aiko1_refined.safetensors",
            enabled=True,
            pocket_tts_voice="aiko1_refined.safetensors",
        )
        self._settings = SimpleNamespace(tts=self.tts)
        self.traces: list[tuple[str, str]] = []
        self.rebuilds = 0
        # No engine: ``set_tts_voice`` reaches for ``set_voice`` on it via
        # getattr, so None exercises the "engine cannot take it" branch
        # while still recording the setting.
        self._tts_engine = None
        self._tts = SimpleNamespace(stop=lambda: None)

    def _trace(self, tag: str, message: str) -> None:
        self.traces.append((tag, message))

    def _rebuild_tts_engine(self) -> None:
        self.rebuilds += 1


def test_switching_to_an_unavailable_provider_is_refused(
    tmp_path: Path,
) -> None:
    """Landing silently on pocket-tts while the setting reads
    'chatterbox-turbo' wastes an hour listening for a difference that was
    never applied."""
    session = _Session()
    with patch.object(registry, "VENV_ROOT", tmp_path):
        session.set_tts_provider("chatterbox-turbo")
    assert session.tts.provider == "pocket-tts"
    assert session.rebuilds == 0
    assert any("unavailable" in message for _, message in session.traces)


def test_switching_to_an_available_provider_rebuilds(tmp_path: Path) -> None:
    session = _Session()
    with patch.object(registry, "availability", return_value=(True, "")):
        session.set_tts_provider("chatterbox-nano")
    assert session.tts.provider == "chatterbox-nano"
    assert session.rebuilds == 1


def test_switching_to_the_same_provider_is_a_no_op() -> None:
    session = _Session()
    session.set_tts_provider("pocket-tts")
    assert session.rebuilds == 0


def test_the_current_provider_stays_listed_when_it_breaks(
    tmp_path: Path,
) -> None:
    """So the UI shows what is configured rather than appearing to be
    set to something else."""
    session = _Session(provider="chatterbox-nano")
    with patch.object(registry, "VENV_ROOT", tmp_path):
        listed = session.list_tts_providers()
    assert "chatterbox-nano" in listed


def test_setting_a_device_keeps_that_provider_s_voice() -> None:
    session = _Session()
    session.tts.providers["pocket-tts"] = TtsProviderSettings(
        voice="custom.safetensors"
    )
    with patch.object(registry, "resolve_device", return_value="cpu"):
        session.set_tts_device("cpu")
    assert session.tts.for_provider("pocket-tts").voice == "custom.safetensors"
    assert session.rebuilds == 1


def test_a_device_the_engine_cannot_use_is_not_written() -> None:
    session = _Session()
    session.set_tts_device("tpu")
    assert "pocket-tts" not in session.tts.providers
    assert session.rebuilds == 0


def test_voices_survive_a_round_trip_between_providers() -> None:
    """pocket-tts -> chatterbox -> pocket-tts must come back to the voice
    that was set, not to the default."""
    session = _Session()
    set_voice = session.set_tts_voice

    set_voice("mine.safetensors")
    with patch.object(registry, "availability", return_value=(True, "")):
        session.set_tts_provider("chatterbox-nano")
    set_voice("reference/aiko_reference.wav")
    with patch.object(registry, "availability", return_value=(True, "")):
        session.set_tts_provider("pocket-tts")

    assert session.tts.for_provider("pocket-tts").voice == "mine.safetensors"
    assert session.tts.for_provider("chatterbox-nano").voice == (
        "reference/aiko_reference.wav"
    )


# ── thread default ──


def test_default_threads_leaves_the_machine_room() -> None:
    """Measured: Nano is faster on 8 threads than 16, so taking every
    core is both slower and rude to whatever else is running."""
    with patch("os.cpu_count", return_value=32):
        assert registry.default_threads() == 8
    with patch("os.cpu_count", return_value=16):
        assert registry.default_threads() == 8
    with patch("os.cpu_count", return_value=4):
        assert registry.default_threads() == 2
    with patch("os.cpu_count", return_value=1):
        assert registry.default_threads() == 1
