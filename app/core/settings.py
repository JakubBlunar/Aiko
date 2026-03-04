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
    vad_level_threshold: float
    vad_silence_seconds: float


@dataclass(slots=True)
class ScreenSettings:
    enable_screen_context: bool
    ocr_profile: str
    capture_interval_seconds: int
    monitor_index: int
    ocr_max_side_px: int
    capture_active_window_only: bool
    decision_mode: str
    decision_cooldown_seconds: int
    min_ocr_chars: int
    unchanged_reuse_seconds: int


def list_screen_ocr_profiles() -> list[str]:
    return ["fast", "balanced"]


@dataclass(slots=True)
class AssistantSettings:
    name: str
    mode: str
    remember_history: bool
    personality: str
    thinking_model: str | None


@dataclass(slots=True)
class AutonomySettings:
    enabled: bool
    proactive_conversation: bool
    allow_action_suggestions: bool
    allow_proactive_actions: bool
    max_strategy_chars: int
    auto_goal_switch: bool
    default_goal: str
    goal_switch_min_confidence: float


@dataclass(slots=True)
class ActionSettings:
    enabled: bool
    dry_run: bool
    require_confirmation: bool
    decision_mode: str
    max_actions_per_turn: int
    min_confidence: float
    min_action_interval_seconds: float
    emergency_hotkey: str
    allowlist_window_titles: list[str]


@dataclass(slots=True)
class SttSettings:
    provider: str
    model: str
    language: str | None


@dataclass(slots=True)
class TtsSettings:
    provider: str
    voice: str
    enabled: bool
    llasa_model: str
    llasa_codec_model: str
    llasa_device: str
    llasa_temperature: float
    llasa_top_p: float
    llasa_max_length: int
    llasa_max_vram_mb: int


@dataclass(slots=True)
class UiSettings:
    window_x: int | None
    window_y: int | None
    window_width: int | None
    window_height: int | None


@dataclass(slots=True)
class AppSettings:
    assistant: AssistantSettings
    autonomy: AutonomySettings
    ollama: OllamaSettings
    audio: AudioSettings
    screen: ScreenSettings
    actions: ActionSettings
    stt: SttSettings
    tts: TtsSettings
    ui: UiSettings


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.yaml"
USER_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "user.yaml"


_SCREEN_PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "fast": {
        "capture_interval_seconds": 2,
        "ocr_max_side_px": 1024,
        "capture_active_window_only": True,
        "decision_mode": "keywords",
        "decision_cooldown_seconds": 2,
        "min_ocr_chars": 12,
        "unchanged_reuse_seconds": 8,
    },
    "balanced": {
        "capture_interval_seconds": 2,
        "ocr_max_side_px": 1280,
        "capture_active_window_only": True,
        "decision_mode": "model",
        "decision_cooldown_seconds": 6,
        "min_ocr_chars": 20,
        "unchanged_reuse_seconds": 20,
    },
}


def normalize_screen_ocr_profile(profile: str | None) -> str:
    normalized = str(profile or "balanced").strip().lower() or "balanced"
    if normalized not in _SCREEN_PROFILE_DEFAULTS:
        return "balanced"
    return normalized


def get_screen_ocr_profile_defaults(profile: str | None) -> dict[str, Any]:
    normalized = normalize_screen_ocr_profile(profile)
    return dict(_SCREEN_PROFILE_DEFAULTS[normalized])


def apply_screen_ocr_profile(screen: ScreenSettings, profile: str | None) -> str:
    normalized = normalize_screen_ocr_profile(profile)
    defaults = _SCREEN_PROFILE_DEFAULTS[normalized]
    screen.ocr_profile = normalized
    screen.capture_interval_seconds = int(defaults["capture_interval_seconds"])
    screen.ocr_max_side_px = int(defaults["ocr_max_side_px"])
    screen.capture_active_window_only = bool(defaults["capture_active_window_only"])
    screen.decision_mode = str(defaults["decision_mode"])
    screen.decision_cooldown_seconds = int(defaults["decision_cooldown_seconds"])
    screen.min_ocr_chars = int(defaults["min_ocr_chars"])
    screen.unchanged_reuse_seconds = int(defaults["unchanged_reuse_seconds"])
    return normalized


def _required(section: dict[str, Any], key: str) -> Any:
    if key not in section:
        raise KeyError(f"Missing config key: {key}")
    return section[key]


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _resolve_screen_settings(raw_screen: dict[str, Any], user_screen: dict[str, Any]) -> dict[str, Any]:
    screen = dict(raw_screen)
    profile = normalize_screen_ocr_profile(screen.get("ocr_profile", "balanced"))
    screen["ocr_profile"] = profile

    profile_defaults = _SCREEN_PROFILE_DEFAULTS[profile]
    for key, value in profile_defaults.items():
        if key not in user_screen:
            screen[key] = value
    return screen


def load_settings(config_path: Path | None = None) -> AppSettings:
    path = config_path or DEFAULT_CONFIG_PATH
    base = _read_yaml(path)
    user = _read_yaml(USER_CONFIG_PATH)
    raw = _deep_merge(base, user)

    assistant = raw["assistant"]
    autonomy = raw.get("autonomy", {})
    ollama = raw["ollama"]
    audio = raw["audio"]
    user_screen = user.get("screen", {}) if isinstance(user.get("screen", {}), dict) else {}
    screen = _resolve_screen_settings(raw["screen"], user_screen)
    actions = raw.get("actions", {})
    stt = raw.get("stt", {})
    tts = raw["tts"]
    ui = raw.get("ui", {})

    return AppSettings(
        assistant=AssistantSettings(
            name=_required(assistant, "name"),
            mode=_required(assistant, "mode"),
            remember_history=bool(_required(assistant, "remember_history")),
            personality=str(assistant.get("personality", "friendly")),
            thinking_model=(
                str(assistant.get("thinking_model")).strip()
                if assistant.get("thinking_model") is not None
                else None
            ),
        ),
        autonomy=AutonomySettings(
            enabled=bool(autonomy.get("enabled", False)),
            proactive_conversation=bool(autonomy.get("proactive_conversation", True)),
            allow_action_suggestions=bool(autonomy.get("allow_action_suggestions", True)),
            allow_proactive_actions=bool(autonomy.get("allow_proactive_actions", False)),
            max_strategy_chars=max(40, int(autonomy.get("max_strategy_chars", 180))),
            auto_goal_switch=bool(autonomy.get("auto_goal_switch", True)),
            default_goal=str(autonomy.get("default_goal", "general_conversation")).strip()
            or "general_conversation",
            goal_switch_min_confidence=max(
                0.0,
                min(float(autonomy.get("goal_switch_min_confidence", 0.6)), 1.0),
            ),
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
            vad_level_threshold=float(audio.get("vad_level_threshold", 0.02)),
            vad_silence_seconds=float(audio.get("vad_silence_seconds", 1.0)),
        ),
        screen=ScreenSettings(
            enable_screen_context=bool(_required(screen, "enable_screen_context")),
            ocr_profile=str(screen.get("ocr_profile", "balanced")),
            capture_interval_seconds=int(_required(screen, "capture_interval_seconds")),
            monitor_index=int(screen.get("monitor_index", 1)),
            ocr_max_side_px=max(0, int(screen.get("ocr_max_side_px", 1600))),
            capture_active_window_only=bool(screen.get("capture_active_window_only", False)),
            decision_mode=str(screen.get("decision_mode", "model")),
            decision_cooldown_seconds=int(screen.get("decision_cooldown_seconds", 8)),
            min_ocr_chars=max(0, int(screen.get("min_ocr_chars", 20))),
            unchanged_reuse_seconds=max(0, int(screen.get("unchanged_reuse_seconds", 30))),
        ),
        actions=ActionSettings(
            enabled=bool(actions.get("enabled", False)),
            dry_run=bool(actions.get("dry_run", True)),
            require_confirmation=bool(actions.get("require_confirmation", True)),
            decision_mode=str(actions.get("decision_mode", "explicit_only")),
            max_actions_per_turn=max(1, int(actions.get("max_actions_per_turn", 1))),
            min_confidence=max(0.0, min(float(actions.get("min_confidence", 0.75)), 1.0)),
            min_action_interval_seconds=max(
                0.0,
                float(actions.get("min_action_interval_seconds", 1.0)),
            ),
            emergency_hotkey=str(actions.get("emergency_hotkey", "ctrl+alt+f12")),
            allowlist_window_titles=[
                str(item).strip()
                for item in actions.get("allowlist_window_titles", [])
                if str(item).strip()
            ],
        ),
        stt=SttSettings(
            provider=str(stt.get("provider", "faster_whisper")),
            model=str(stt.get("model", "base")),
            language=(str(stt.get("language")).strip() if stt.get("language") is not None else None),
        ),
        tts=TtsSettings(
            provider=_required(tts, "provider"),
            voice=_required(tts, "voice"),
            enabled=bool(_required(tts, "enabled")),
            llasa_model=str(tts.get("llasa_model", "NandemoGHS/Anime-Llasa-3B")),
            llasa_codec_model=str(tts.get("llasa_codec_model", "HKUSTAudio/xcodec2")),
            llasa_device=str(tts.get("llasa_device", "cuda")),
            llasa_temperature=float(tts.get("llasa_temperature", 0.8)),
            llasa_top_p=float(tts.get("llasa_top_p", 0.95)),
            llasa_max_length=max(256, int(tts.get("llasa_max_length", 2048))),
            llasa_max_vram_mb=max(0, int(tts.get("llasa_max_vram_mb", 0))),
        ),
        ui=UiSettings(
            window_x=int(ui["window_x"]) if ui.get("window_x") is not None else None,
            window_y=int(ui["window_y"]) if ui.get("window_y") is not None else None,
            window_width=int(ui["window_width"]) if ui.get("window_width") is not None else None,
            window_height=int(ui["window_height"]) if ui.get("window_height") is not None else None,
        ),
    )


def save_runtime_preferences(
    *,
    chat_model: str,
    thinking_model: str | None,
    personality: str,
    remember_history: bool,
    microphone_device: int | None,
    loopback_device: int | None,
    vad_level_threshold: float,
    vad_silence_seconds: float,
    action_min_interval_seconds: float,
    tts_provider: str,
    tts_voice: str | None,
    stt_model: str | None = None,
    enable_microphone: bool,
    enable_system_audio: bool,
    enable_screen_context: bool,
    screen_ocr_profile: str | None = None,
    window_x: int | None = None,
    window_y: int | None = None,
    window_width: int | None = None,
    window_height: int | None = None,
    path: Path | None = None,
) -> None:
    target = path or USER_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    current = _read_yaml(target)
    updates: dict[str, Any] = {
        "ollama": {
            "chat_model": chat_model,
        },
        "assistant": {
            "personality": personality,
            "remember_history": bool(remember_history),
            "thinking_model": thinking_model,
        },
        "audio": {
            "microphone_device": microphone_device,
            "loopback_device": loopback_device,
            "vad_level_threshold": round(vad_level_threshold, 4),
            "vad_silence_seconds": round(vad_silence_seconds, 2),
            "enable_microphone": bool(enable_microphone),
            "enable_system_audio": bool(enable_system_audio),
        },
        "screen": {
            "enable_screen_context": bool(enable_screen_context),
            "ocr_profile": normalize_screen_ocr_profile(screen_ocr_profile),
        },
        "actions": {
            "min_action_interval_seconds": round(max(0.0, action_min_interval_seconds), 2),
        },
        "tts": {
            "provider": str(tts_provider or "piper").strip().lower() or "piper",
            "voice": str(tts_voice or "").strip(),
        },
        "stt": {
            "model": str(stt_model or "").strip() or None,
        },
    }

    if any(value is not None for value in (window_x, window_y, window_width, window_height)):
        updates["ui"] = {
            "window_x": int(window_x) if window_x is not None else None,
            "window_y": int(window_y) if window_y is not None else None,
            "window_width": int(window_width) if window_width is not None else None,
            "window_height": int(window_height) if window_height is not None else None,
        }

    merged = _deep_merge(current, updates)
    target.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
