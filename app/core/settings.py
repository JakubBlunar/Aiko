from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


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
    microphone_device: int | None
    vad_level_threshold: float
    vad_silence_seconds: float


@dataclass(slots=True)
class ScreenSettings:
    enable_screen_context: bool
    ocr_profile: str
    monitor_index: int
    ocr_max_side_px: int
    capture_active_window_only: bool
    decision_mode: str
    decision_cooldown_seconds: int
    min_ocr_chars: int
    unchanged_reuse_seconds: int
    enable_uia: bool = True


def list_screen_ocr_profiles() -> list[str]:
    return ["fast", "balanced"]


@dataclass(slots=True)
class AssistantSettings:
    name: str
    mode: str
    remember_history: bool
    background: str
    thinking_model: str | None


@dataclass(slots=True)
class AutonomySettings:
    enabled: bool
    mode: str
    proactive_conversation: bool
    allow_action_suggestions: bool
    allow_proactive_actions: bool
    max_strategy_chars: int
    auto_goal_switch: bool
    default_goal: str
    goal_switch_min_confidence: float
    agentic_max_auto_steps: int = 3
    agentic_full_narration: bool = False
    agentic_narration_level: str = "summary"
    reading_session_memory_enabled: bool = True
    reading_max_scroll_steps: int = 6
    reading_max_quotes: int = 4
    reading_max_quote_chars: int = 500
    reading_trusted_window_titles: list[str] | None = None


@dataclass(slots=True)
class ActionSettings:
    enabled: bool
    dry_run: bool
    require_confirmation: bool
    decision_mode: str
    max_actions_per_turn: int
    mcp_repair_attempts: int
    min_confidence: float
    min_action_interval_seconds: float
    emergency_hotkey: str
    allowlist_window_titles: list[str]


@dataclass(slots=True)
class SttSettings:
    provider: str
    model: str
    language: str | None
    diagnostic_record_seconds: float = 5.0
    diagnostic_vad_filter: bool = True
    diagnostic_initial_prompt: str = ""
    prosody_enabled: bool = False
    prosody_include_in_prompt: bool = True


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
    decision_trace_filters: dict[str, bool] = field(default_factory=dict)
    decision_trace_limit: int | None = None
    decision_trace_window_x: int | None = None
    decision_trace_window_y: int | None = None
    decision_trace_window_width: int | None = None
    decision_trace_window_height: int | None = None


@dataclass(slots=True)
class ToolingBridgeSettings:
    config_default_path: str
    config_user_path: str
    enable_runtime_overrides: bool


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
    tooling: ToolingBridgeSettings


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.json"
USER_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "user.json"


_SCREEN_PROFILE_DEFAULTS: dict[str, dict[str, Any]] = {
    "fast": {
        "ocr_max_side_px": 1024,
        "capture_active_window_only": True,
        "decision_mode": "keywords",
        "decision_cooldown_seconds": 2,
        "min_ocr_chars": 12,
        "unchanged_reuse_seconds": 8,
    },
    "balanced": {
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


_NO_CHANGE = object()


def _deep_diff(base: Any, value: Any) -> Any:
    """Return only keys/values from value that differ from base.

    Returns _NO_CHANGE when there is no difference.
    """
    if isinstance(base, dict) and isinstance(value, dict):
        result: dict[str, Any] = {}
        keys = set(base.keys()) | set(value.keys())
        for key in keys:
            if key not in value:
                continue
            if key not in base:
                result[key] = value[key]
                continue
            nested = _deep_diff(base[key], value[key])
            if nested is not _NO_CHANGE:
                result[key] = nested
        return result if result else _NO_CHANGE

    if base == value:
        return _NO_CHANGE
    return value


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _read_merged_overrides(*paths: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in paths:
        try:
            current = _read_config(path)
        except Exception:
            continue
        merged = _deep_merge(merged, current)
    return merged


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
    if config_path is not None:
        base = _read_config(config_path)
    else:
        base = _read_merged_overrides(DEFAULT_CONFIG_PATH)
    user = _read_merged_overrides(USER_CONFIG_PATH)
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
    tooling = raw.get("tooling", {})

    stt_diag_seconds_raw = stt.get("diagnostic_record_seconds", 5.0)
    stt_diag_seconds = 5.0 if stt_diag_seconds_raw is None else float(stt_diag_seconds_raw)
    stt_diag_vad_raw = stt.get("diagnostic_vad_filter", True)
    stt_diag_vad = True if stt_diag_vad_raw is None else bool(stt_diag_vad_raw)
    stt_diag_prompt_raw = stt.get("diagnostic_initial_prompt", "")
    stt_diag_prompt = "" if stt_diag_prompt_raw is None else str(stt_diag_prompt_raw)
    stt_prosody_enabled_raw = stt.get("prosody_enabled", False)
    stt_prosody_enabled = False if stt_prosody_enabled_raw is None else bool(stt_prosody_enabled_raw)
    stt_prosody_prompt_raw = stt.get("prosody_include_in_prompt", True)
    stt_prosody_prompt = True if stt_prosody_prompt_raw is None else bool(stt_prosody_prompt_raw)

    narration_level_raw = str(autonomy.get("agentic_narration_level", "")).strip().lower()
    if narration_level_raw not in {"full", "summary", "off"}:
        narration_level_raw = (
            "full"
            if bool(autonomy.get("agentic_full_narration", False))
            else "summary"
        )

    return AppSettings(
        assistant=AssistantSettings(
            name=_required(assistant, "name"),
            mode=_required(assistant, "mode"),
            remember_history=bool(_required(assistant, "remember_history")),
            background=str(assistant.get("background", "")).strip(),
            thinking_model=(
                str(assistant.get("thinking_model")).strip()
                if assistant.get("thinking_model") is not None
                else None
            ),
        ),
        autonomy=AutonomySettings(
            enabled=bool(autonomy.get("enabled", False)),
            mode=(
                str(autonomy.get("mode", "interactive")).strip().lower()
                if str(autonomy.get("mode", "interactive")).strip().lower()
                in {"manual", "interactive", "automatic"}
                else "interactive"
            ),
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
            agentic_max_auto_steps=max(1, min(int(autonomy.get("agentic_max_auto_steps", 3)), 20)),
            agentic_full_narration=bool(autonomy.get("agentic_full_narration", False)),
            agentic_narration_level=narration_level_raw,
            reading_session_memory_enabled=bool(autonomy.get("reading_session_memory_enabled", True)),
            reading_max_scroll_steps=max(1, min(int(autonomy.get("reading_max_scroll_steps", 6)), 30)),
            reading_max_quotes=max(1, min(int(autonomy.get("reading_max_quotes", 4)), 8)),
            reading_max_quote_chars=max(120, min(int(autonomy.get("reading_max_quote_chars", 500)), 2400)),
            reading_trusted_window_titles=[
                str(item).strip()
                for item in autonomy.get("reading_trusted_window_titles", ["chrome", "firefox", "edge", "vscode", "notepad"])
                if str(item).strip()
            ]
            or ["chrome", "firefox", "edge", "vscode", "notepad"],
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
            microphone_device=(
                int(audio["microphone_device"]) if audio.get("microphone_device") is not None else None
            ),
            vad_level_threshold=float(audio.get("vad_level_threshold", 0.02)),
            vad_silence_seconds=float(audio.get("vad_silence_seconds", 1.0)),
        ),
        screen=ScreenSettings(
            enable_screen_context=bool(_required(screen, "enable_screen_context")),
            ocr_profile=str(screen.get("ocr_profile", "balanced")),
            monitor_index=int(screen.get("monitor_index", 1)),
            ocr_max_side_px=max(0, int(screen.get("ocr_max_side_px", 1600))),
            capture_active_window_only=bool(screen.get("capture_active_window_only", False)),
            decision_mode=str(screen.get("decision_mode", "model")),
            decision_cooldown_seconds=int(screen.get("decision_cooldown_seconds", 8)),
            min_ocr_chars=max(0, int(screen.get("min_ocr_chars", 20))),
            unchanged_reuse_seconds=max(0, int(screen.get("unchanged_reuse_seconds", 30))),
            enable_uia=bool(screen.get("enable_uia", True)),
        ),
        actions=ActionSettings(
            enabled=bool(actions.get("enabled", False)),
            dry_run=bool(actions.get("dry_run", True)),
            require_confirmation=bool(actions.get("require_confirmation", True)),
            decision_mode=str(actions.get("decision_mode", "explicit_only")),
            max_actions_per_turn=max(1, int(actions.get("max_actions_per_turn", 1))),
            mcp_repair_attempts=max(0, min(int(actions.get("mcp_repair_attempts", 2)), 20)),
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
            diagnostic_record_seconds=max(1.0, min(stt_diag_seconds, 30.0)),
            diagnostic_vad_filter=stt_diag_vad,
            diagnostic_initial_prompt=stt_diag_prompt.strip(),
            prosody_enabled=stt_prosody_enabled,
            prosody_include_in_prompt=stt_prosody_prompt,
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
            decision_trace_filters=(
                {
                    str(k): bool(v)
                    for k, v in ui.get("decision_trace_filters", {}).items()
                }
                if isinstance(ui.get("decision_trace_filters"), dict)
                else {}
            ),
            decision_trace_limit=(
                int(ui["decision_trace_limit"])
                if ui.get("decision_trace_limit") is not None
                else None
            ),
            decision_trace_window_x=(
                int(ui["decision_trace_window_x"])
                if ui.get("decision_trace_window_x") is not None
                else None
            ),
            decision_trace_window_y=(
                int(ui["decision_trace_window_y"])
                if ui.get("decision_trace_window_y") is not None
                else None
            ),
            decision_trace_window_width=(
                int(ui["decision_trace_window_width"])
                if ui.get("decision_trace_window_width") is not None
                else None
            ),
            decision_trace_window_height=(
                int(ui["decision_trace_window_height"])
                if ui.get("decision_trace_window_height") is not None
                else None
            ),
        ),
        tooling=ToolingBridgeSettings(
            config_default_path=str(tooling.get("config_default_path", "config/tooling.default.json")),
            config_user_path=str(tooling.get("config_user_path", "config/tooling.user.json")),
            enable_runtime_overrides=bool(tooling.get("enable_runtime_overrides", True)),
        ),
    )


def save_runtime_preferences(
    *,
    chat_model: str,
    thinking_model: str | None,
    remember_history: bool,
    autonomy_mode: str,
    microphone_device: int | None,
    vad_level_threshold: float,
    vad_silence_seconds: float,
    action_min_interval_seconds: float,
    tts_provider: str,
    tts_voice: str | None,
    stt_model: str | None = None,
    stt_diagnostic_record_seconds: float | None = None,
    stt_diagnostic_vad_filter: bool | None = None,
    stt_diagnostic_initial_prompt: str | None = None,
    stt_prosody_enabled: bool | None = None,
    stt_prosody_include_in_prompt: bool | None = None,
    enable_microphone: bool,
    enable_screen_context: bool,
    screen_ocr_profile: str | None = None,
    window_x: int | None = None,
    window_y: int | None = None,
    window_width: int | None = None,
    window_height: int | None = None,
    ui_decision_trace_filters: dict[str, bool] | None = None,
    ui_decision_trace_limit: int | None = None,
    ui_decision_trace_window_x: int | None = None,
    ui_decision_trace_window_y: int | None = None,
    ui_decision_trace_window_width: int | None = None,
    ui_decision_trace_window_height: int | None = None,
    path: Path | None = None,
) -> None:
    target = path or USER_CONFIG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    current_user = _read_merged_overrides(
        USER_CONFIG_PATH,
    )
    base = _read_merged_overrides(
        DEFAULT_CONFIG_PATH,
    )
    effective = _deep_merge(base, current_user)

    updates: dict[str, Any] = {
        "ollama": {
            "chat_model": chat_model,
        },
        "assistant": {
            "remember_history": bool(remember_history),
            "thinking_model": thinking_model,
        },
        "autonomy": {
            "mode": (
                str(autonomy_mode or "interactive").strip().lower()
                if str(autonomy_mode or "interactive").strip().lower()
                in {"manual", "interactive", "automatic"}
                else "interactive"
            ),
        },
        "audio": {
            "microphone_device": microphone_device,
            "vad_level_threshold": round(vad_level_threshold, 4),
            "vad_silence_seconds": round(vad_silence_seconds, 2),
            "enable_microphone": bool(enable_microphone),
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
        "stt": {},
    }

    stt_updates: dict[str, Any] = updates["stt"]
    model_value = str(stt_model or "").strip()
    if model_value:
        stt_updates["model"] = model_value
    if stt_diagnostic_record_seconds is not None:
        stt_updates["diagnostic_record_seconds"] = round(
            max(1.0, min(float(stt_diagnostic_record_seconds), 30.0)),
            1,
        )
    if stt_diagnostic_vad_filter is not None:
        stt_updates["diagnostic_vad_filter"] = bool(stt_diagnostic_vad_filter)
    if stt_diagnostic_initial_prompt is not None:
        stt_updates["diagnostic_initial_prompt"] = str(stt_diagnostic_initial_prompt or "").strip()
    if stt_prosody_enabled is not None:
        stt_updates["prosody_enabled"] = bool(stt_prosody_enabled)
    if stt_prosody_include_in_prompt is not None:
        stt_updates["prosody_include_in_prompt"] = bool(stt_prosody_include_in_prompt)

    ui_updates: dict[str, Any] = {}
    if any(value is not None for value in (window_x, window_y, window_width, window_height)):
        ui_updates.update(
            {
                "window_x": int(window_x) if window_x is not None else None,
                "window_y": int(window_y) if window_y is not None else None,
                "window_width": int(window_width) if window_width is not None else None,
                "window_height": int(window_height) if window_height is not None else None,
            }
        )
    if ui_decision_trace_filters is not None:
        ui_updates["decision_trace_filters"] = {
            str(key): bool(value)
            for key, value in ui_decision_trace_filters.items()
        }
    if ui_decision_trace_limit is not None:
        ui_updates["decision_trace_limit"] = max(20, min(int(ui_decision_trace_limit), 5000))
    if ui_decision_trace_window_x is not None:
        ui_updates["decision_trace_window_x"] = int(ui_decision_trace_window_x)
    if ui_decision_trace_window_y is not None:
        ui_updates["decision_trace_window_y"] = int(ui_decision_trace_window_y)
    if ui_decision_trace_window_width is not None:
        ui_updates["decision_trace_window_width"] = max(300, int(ui_decision_trace_window_width))
    if ui_decision_trace_window_height is not None:
        ui_updates["decision_trace_window_height"] = max(220, int(ui_decision_trace_window_height))
    if ui_updates:
        updates["ui"] = ui_updates

    updated_effective = _deep_merge(effective, updates)
    minimal_overrides = _deep_diff(base, updated_effective)
    payload = minimal_overrides if isinstance(minimal_overrides, dict) else {}
    target.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
