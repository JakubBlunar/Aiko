"""The TTS provider registry: what exists, what works, what gets built.

The bug this guards against is the one that was already shipped: the
provider setting was stored, exposed in the API, switchable at runtime --
and never read by the factory, so every selection silently resolved to
pocket-tts. Tests that only checked "does TTS work" passed throughout.

So the assertions here are mostly about *not lying*: that availability
reflects the filesystem, that a chosen provider is the one built, and
that failure degrades in a stated order rather than either crashing the
app or quietly substituting a different engine.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.infra.settings import TtsProviderSettings, TtsSettings
from app.tts import registry


def _settings(**kwargs) -> TtsSettings:
    base = {
        "provider": "pocket-tts",
        "voice": "aiko1_refined.safetensors",
        "enabled": True,
    }
    base.update(kwargs)
    return TtsSettings(**base)


# ── catalogue shape ──


def test_every_provider_declares_at_least_one_device() -> None:
    for provider in registry.CATALOGUE:
        assert provider.devices, f"{provider.name} has no runnable device"


def test_pocket_tts_is_not_offered_a_gpu() -> None:
    """It is a CPU-only library; a device picker for it would be a lie."""
    assert registry.get("pocket-tts").devices == ("cpu",)


def test_multilingual_is_not_offered_a_cpu() -> None:
    """At RTF ~3.0 on CPU it would stutter, not merely lag. Offering the
    option would just be a way to reach a broken configuration."""
    assert "cpu" not in registry.get("chatterbox-multilingual").devices


def test_sidecar_engines_name_a_venv_and_an_engine_key() -> None:
    """One venv hosts several models, so both fields are needed and a
    missing one would spawn the wrong interpreter or the wrong class."""
    for provider in registry.CATALOGUE:
        if provider.venv:
            assert provider.sidecar_engine, f"{provider.name} has no engine key"


def test_unknown_provider_is_not_invented() -> None:
    assert registry.get("festival") is None
    usable, reason = registry.availability("festival")
    assert not usable
    assert "unknown" in reason


# ── availability is a filesystem question, not an import ──


def test_availability_never_imports_the_engine() -> None:
    """The load-bearing property. Chatterbox pins torch 2.6 against this
    app's 2.10, so importing it to check is not slow, it is impossible --
    and importing pocket-tts costs ~0.6-1 GB of PyTorch for a question
    that a file existence check answers.
    """
    before = set(sys.modules)
    for name in (p.name for p in registry.CATALOGUE):
        registry.availability(name)
    # Compared against a snapshot rather than checked absolutely: other
    # tests in the suite legitimately build a real engine, so an absolute
    # check would pass or fail on test ordering rather than on anything
    # this module does.
    heavy = {"torch", "chatterbox", "pocket_tts", "transformers"}
    added = {m.split(".")[0] for m in set(sys.modules) - before}
    assert not (added & heavy), f"availability imported {added & heavy}"


def test_missing_venv_is_reported_with_the_fix(tmp_path: Path) -> None:
    """The reason string reaches the settings drawer, so it should say
    what to do rather than only what is wrong."""
    with patch.object(registry, "VENV_ROOT", tmp_path):
        usable, reason = registry.availability("chatterbox-nano")
    assert not usable
    assert "envs install chatterbox-git" in reason


def test_a_missing_sidecar_outranks_a_missing_venv(tmp_path: Path) -> None:
    """The container case. The Docker build copies ``app/`` but not
    ``tools/``, so the worker is absent there -- and telling a container
    user to run a module out of a directory the image does not contain
    sends them hunting a packaging decision as though it were a bug."""
    with patch.object(registry, "SIDECAR", tmp_path / "absent.py"):
        with patch.object(registry, "VENV_ROOT", tmp_path):
            usable, reason = registry.availability("chatterbox-nano")
    assert not usable
    assert "container image" in reason
    assert "envs install" not in reason


def test_present_venv_reads_as_available(tmp_path: Path) -> None:
    interpreter = tmp_path / "chatterbox-git" / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    )
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    with patch.object(registry, "VENV_ROOT", tmp_path):
        usable, reason = registry.availability("chatterbox-nano")
    assert usable, reason


def test_describe_lists_unavailable_engines_too() -> None:
    """A greyed-out row explaining the install is more use than an
    option that never appears."""
    rows = {row["name"]: row for row in registry.describe()}
    assert len(rows) == len(registry.CATALOGUE)
    for row in rows.values():
        assert row["available"] or row["reason"]


# ── device resolution ──


@pytest.mark.parametrize(
    "name,requested,expected",
    [
        ("pocket-tts", "auto", "cpu"),
        ("pocket-tts", "cuda", "cpu"),      # cannot; downgraded
        ("chatterbox-nano", "auto", "cpu"),  # real-time on CPU already
        ("chatterbox-nano", "cuda", "cuda"),
        ("chatterbox-turbo", "auto", "cuda"),  # cannot reach realtime on CPU
        ("chatterbox-turbo", "cpu", "cpu"),    # allowed, user's call
        ("chatterbox-multilingual", "cpu", "cuda"),  # cannot; upgraded
    ],
)
def test_device_resolution(name: str, requested: str, expected: str) -> None:
    assert registry.resolve_device(name, requested) == expected


def test_auto_encodes_each_engine_s_own_economics() -> None:
    """Nano defaults to CPU because spending VRAM on it buys nothing;
    Turbo defaults to GPU because it cannot reach real time without one.
    A single global default would be wrong for one of them."""
    assert registry.resolve_device("chatterbox-nano", "auto") == "cpu"
    assert registry.resolve_device("chatterbox-turbo", "auto") == "cuda"


def test_garbage_device_falls_back_rather_than_raising() -> None:
    assert registry.resolve_device("pocket-tts", "tpu") == "cpu"
    assert registry.resolve_device("pocket-tts", "") == "cpu"


# ── per-provider settings ──


def test_legacy_pocket_voice_still_resolves() -> None:
    """Existing installs must keep her voice with no config edit: the
    shipped default.json has only the flat pocket_tts_voice field."""
    settings = _settings(pocket_tts_voice="aiko1_refined.safetensors")
    assert settings.for_provider("pocket-tts").voice == (
        "aiko1_refined.safetensors"
    )


def test_cloning_engines_get_no_legacy_voice() -> None:
    """The flat field holds a .safetensors embedding, which is
    meaningless to an engine that clones from a clip. Inheriting it would
    fail deep inside a tensor load instead of at the setting."""
    settings = _settings(pocket_tts_voice="aiko1_refined.safetensors")
    assert settings.for_provider("chatterbox-nano").voice == ""


def test_per_provider_voice_overrides_legacy() -> None:
    settings = _settings(
        providers={"pocket-tts": TtsProviderSettings(voice="other.safetensors")}
    )
    assert settings.for_provider("pocket-tts").voice == "other.safetensors"


def test_voices_are_kept_separately_per_provider() -> None:
    """The point of the nested block: a round trip between engines must
    not lose either voice."""
    settings = _settings(
        providers={
            "pocket-tts": TtsProviderSettings(voice="aiko.safetensors"),
            "chatterbox-nano": TtsProviderSettings(voice="aiko_ref.wav"),
        }
    )
    assert settings.for_provider("pocket-tts").voice == "aiko.safetensors"
    assert settings.for_provider("chatterbox-nano").voice == "aiko_ref.wav"


def test_device_set_without_voice_keeps_the_legacy_voice() -> None:
    """Setting a device should not silently reset the voice."""
    settings = _settings(
        pocket_tts_voice="aiko1_refined.safetensors",
        providers={"pocket-tts": TtsProviderSettings(device="cpu")},
    )
    resolved = settings.for_provider("pocket-tts")
    assert resolved.voice == "aiko1_refined.safetensors"
    assert resolved.device == "cpu"


def test_unconfigured_provider_resolves_to_defaults() -> None:
    resolved = _settings().for_provider("chatterbox-turbo")
    assert resolved.device == "auto"


# ── construction ──


def test_build_refuses_an_unavailable_provider(tmp_path: Path) -> None:
    with patch.object(registry, "VENV_ROOT", tmp_path):
        with pytest.raises(RuntimeError, match="unavailable"):
            registry.build("chatterbox-nano", _settings())


def test_fallback_lands_on_the_default_and_says_so() -> None:
    """Degrading is right -- the alternative to a working voice is a
    mute companion -- but it has to be loud, or a user spends an hour
    listening for a difference that was never applied.

    Asserted against the module's own logger rather than ``caplog``.
    ``caplog`` captures through a handler on the *root* logger, so it sees
    nothing once the app's logging setup stops ``app.*`` records
    propagating -- which made this pass alone and fail in the suite, for a
    reason that had nothing to do with log levels.
    """
    settings = _settings()
    sentinel = object()

    def _fake_build(name, _settings):
        if name == "chatterbox-turbo":
            raise RuntimeError("venv missing")
        return sentinel

    with patch.object(registry, "build", _fake_build):
        with patch.object(registry, "log") as logger:
            engine = registry.build_with_fallback("chatterbox-turbo", settings)

    assert engine is sentinel
    warnings = " ".join(
        str(call.args[0]) for call in logger.warning.call_args_list
    )
    assert "falling back" in warnings.lower()
    assert "unavailable" in warnings.lower()


def test_fallback_of_last_resort_is_a_null_engine() -> None:
    """Boot must not die because TTS could not be built."""
    with patch.object(
        registry, "build", side_effect=RuntimeError("nothing works")
    ):
        engine = registry.build_with_fallback("pocket-tts", _settings())
    assert getattr(engine, "is_null_engine", False)


def test_build_dispatches_on_the_name_it_was_given() -> None:
    """The original bug: the factory ignored the provider entirely."""
    settings = _settings()
    with patch("app.tts.pocket_tts_service.PocketTtsService") as pocket:
        registry.build("pocket-tts", settings)
    pocket.assert_called_once_with(settings)
