from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class OllamaSettings:
    base_url: str
    chat_model: str
    temperature: float


@dataclass(slots=True)
class AudioSettings:
    sample_rate: int
    channels: int
    enable_microphone: bool
    enable_system_audio: bool
    microphone_device: int | None
    loopback_device: int | None


@dataclass(slots=True)
class ScreenSettings:
    enable_screen_context: bool
    capture_interval_seconds: int


@dataclass(slots=True)
class AssistantSettings:
    name: str
    mode: str
    remember_history: bool


@dataclass(slots=True)
class TtsSettings:
    provider: str
    voice: str
    enabled: bool


@dataclass(slots=True)
class AppSettings:
    assistant: AssistantSettings
    ollama: OllamaSettings
    audio: AudioSettings
    screen: ScreenSettings
    tts: TtsSettings


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def _required(section: dict[str, Any], key: str) -> Any:
    if key not in section:
        raise KeyError(f"Missing config key: {key}")
    return section[key]


def load_settings(config_path: Path | None = None) -> AppSettings:
    path = config_path or DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assistant = raw["assistant"]
    ollama = raw["ollama"]
    audio = raw["audio"]
    screen = raw["screen"]
    tts = raw["tts"]

    return AppSettings(
        assistant=AssistantSettings(
            name=_required(assistant, "name"),
            mode=_required(assistant, "mode"),
            remember_history=bool(_required(assistant, "remember_history")),
        ),
        ollama=OllamaSettings(
            base_url=_required(ollama, "base_url"),
            chat_model=_required(ollama, "chat_model"),
            temperature=float(_required(ollama, "temperature")),
        ),
        audio=AudioSettings(
            sample_rate=int(_required(audio, "sample_rate")),
            channels=int(_required(audio, "channels")),
            enable_microphone=bool(_required(audio, "enable_microphone")),
            enable_system_audio=bool(_required(audio, "enable_system_audio")),
            microphone_device=(
                int(audio["microphone_device"]) if audio.get("microphone_device") is not None else None
            ),
            loopback_device=(
                int(audio["loopback_device"]) if audio.get("loopback_device") is not None else None
            ),
        ),
        screen=ScreenSettings(
            enable_screen_context=bool(_required(screen, "enable_screen_context")),
            capture_interval_seconds=int(_required(screen, "capture_interval_seconds")),
        ),
        tts=TtsSettings(
            provider=_required(tts, "provider"),
            voice=_required(tts, "voice"),
            enabled=bool(_required(tts, "enabled")),
        ),
    )
