"""Resolving a configured voice name to a Pocket-TTS speaker state.

Written after the Docker full image shipped without her voice. The image
copies ``config/`` (which names ``aiko1_refined.safetensors``) but not
``voices/``, so every container resolved a missing file, substituted the
stock "alba" speaker, and logged nothing. The result was a companion that
worked perfectly and did not sound like herself, with no evidence
anywhere that a substitution had happened.

Nothing covered ``_resolve_voice`` at all, which is why a silent fallback
survived. The substitution itself is correct -- a missing file should not
cost her the ability to speak -- so the assertions here are about it being
*audible in the log*, not about removing it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import app.tts.pocket_tts_service as service_module
from app.tts.pocket_tts_service import _BUILTIN_VOICES, PocketTtsService


@pytest.fixture
def service() -> PocketTtsService:
    """A service instance without the load thread or a real model."""
    settings = SimpleNamespace(
        provider="pocket-tts",
        voice="",
        enabled=False,  # keeps __init__ from starting a load
        pocket_tts_voice="aiko1_refined.safetensors",
        pocket_tts_temp=0.6,
        pocket_tts_custom_voices_dir="",
        pocket_tts_frames_after_eos=1,
        pitch_preserving_speed=True,
    )
    return PocketTtsService(settings)  # type: ignore[arg-type]


def test_a_builtin_voice_is_passed_straight_through(service) -> None:
    model = MagicMock()
    service._resolve_voice(model, "alba")  # noqa: SLF001
    model.get_state_for_audio_prompt.assert_called_once_with("alba")


def test_every_builtin_name_resolves_without_touching_the_disk(
    service, tmp_path: Path
) -> None:
    for name in _BUILTIN_VOICES:
        model = MagicMock()
        service._resolve_voice(model, name)  # noqa: SLF001
        model.get_state_for_audio_prompt.assert_called_once_with(name)


def test_an_existing_file_resolves_to_its_path(service, tmp_path: Path) -> None:
    voice = tmp_path / "custom.safetensors"
    voice.write_bytes(b"\x00")
    model = MagicMock()
    service._resolve_voice(model, str(voice))  # noqa: SLF001
    model.get_state_for_audio_prompt.assert_called_once_with(str(voice))


def test_a_missing_voice_falls_back_but_says_so(service) -> None:
    """The Docker bug. The fallback keeps her talking, which is right;
    doing it silently is what cost a release its voice.

    Asserted against the module's own logger, not ``caplog``: ``caplog``
    captures through a handler on the *root* logger, so it sees nothing
    once the app's logging setup stops ``app.*`` records propagating.
    That makes a caplog assertion here pass alone and fail in the suite,
    for a reason unrelated to the behaviour under test.
    """
    model = MagicMock()
    with patch.object(service_module, "log") as logger:
        service._resolve_voice(model, "definitely-not-here.safetensors")  # noqa: SLF001

    model.get_state_for_audio_prompt.assert_called_once_with("alba")
    assert logger.warning.called, "the substitution was silent"
    call = logger.warning.call_args
    # Rendered the way logging would, so a %-arg left out of the format
    # string still fails here rather than reaching a log file as a
    # half-written sentence.
    logged = str(call.args[0]) % tuple(call.args[1:])
    assert "definitely-not-here.safetensors" in logged
    assert "alba" in logged
    # The operator needs to know *where* it looked, or "not found" is not
    # actionable -- the whole Docker failure was a path expectation.
    assert "voices" in logged


def test_the_shipped_default_voice_is_present_in_this_checkout() -> None:
    """Guards the packaging side of the same bug from the other end.

    ``config/default.json`` names a voice file; if that file is not in the
    tree, every install resolves to "alba". Asserted here rather than in a
    Docker test because it is the *pairing* that matters, and it is as
    easy to break by renaming the voice as by editing the Dockerfile.
    """
    import json

    config = json.loads(
        Path("config/default.json").read_text(encoding="utf-8")
    )
    name = (config.get("tts") or {}).get("pocket_tts_voice", "")
    assert name, "default.json names no pocket_tts_voice"
    if name in _BUILTIN_VOICES:
        return
    assert (Path("voices") / name).is_file(), (
        f"config/default.json asks for {name!r}, which is not in voices/ "
        "-- every install would silently speak as 'alba'"
    )
