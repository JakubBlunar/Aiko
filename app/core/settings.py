from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class OllamaSettings:
    base_url: str
    chat_model: str
    temperature: float
    context_window: int | None = None  # None = auto-detect from Ollama API
    embedding_model: str = "qwen3-embedding:0.6b"
    judge_model: str = "qwen2.5:0.5b"
    timeout: int = 300  # HTTP timeout in seconds (shared by all Ollama clients)


@dataclass(slots=True)
class DatabaseSettings:
    provider: str  # "sqlite" | "postgres"
    url: str | None  # for postgres: connection URL


@dataclass(slots=True)
class AudioSettings:
    sample_rate: int
    channels: int
    enable_microphone: bool
    microphone_device: int | None
    vad_level_threshold: float
    vad_silence_seconds: float
    barge_in_enabled: bool = False
    output_device: int | None = None
    live_input_mode: str = "voice_detection"
    live_ptt_type: str = "keyboard"
    live_ptt_key: str | None = None
    live_ptt_mouse_button: str | None = None
    live_ptt_toggle: bool = False
    earcons_enabled: bool = True


@dataclass(slots=True)
class AssistantSettings:
    name: str
    remember_history: bool
    background: str
    background_path: str = ""  # If set, load multiline background from this file (relative to project root)
    user_id: str = "default"  # Scopes Agno Learning (user profile + memory) per user
    response_style: str = "balanced"  # balanced | concise | detailed (reply length)
    tts_length_scale: float = 1.0  # TTS speed: 0.65–1.35, higher = slower


@dataclass(slots=True)
class SessionToolPolicySettings:
    native_tool_calls_enabled: bool = False
    allowed_tool_prefixes: tuple[str, ...] = ()
    pre_execution_narration_default: bool = False


@dataclass(slots=True)
class SessionToolPoliciesSettings:
    agentic: SessionToolPolicySettings = field(
        default_factory=lambda: SessionToolPolicySettings(
            native_tool_calls_enabled=True,
            allowed_tool_prefixes=("mcp.",),
            pre_execution_narration_default=True,
        )
    )
    chat: SessionToolPolicySettings = field(default_factory=SessionToolPolicySettings)
    reading: SessionToolPolicySettings = field(
        default_factory=lambda: SessionToolPolicySettings(
            native_tool_calls_enabled=True,
            allowed_tool_prefixes=("mcp.",),
            pre_execution_narration_default=False,
        )
    )


@dataclass(slots=True)
class AutonomyTurnPlanningSettings:
    proactive_conversation: bool = True
    allow_action_suggestions: bool = True
    allow_proactive_actions: bool = False
    max_strategy_chars: int = 180


@dataclass(slots=True)
class AutonomySettings:
    enabled: bool
    mode: str
    auto_goal_switch: bool
    default_goal: str
    goal_switch_min_confidence: float
    turn_planning: AutonomyTurnPlanningSettings = field(default_factory=AutonomyTurnPlanningSettings)
    force_agentic_session: bool = False
    agentic_max_auto_steps: int = 3
    agentic_narration_level: str = "summary"
    session_tool_policies: SessionToolPoliciesSettings = field(default_factory=SessionToolPoliciesSettings)
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
    model: str
    language: str | None
    diagnostics: "SttDiagnosticsSettings" = field(default_factory=lambda: SttDiagnosticsSettings())
    prosody: "SttProsodySettings" = field(default_factory=lambda: SttProsodySettings())


@dataclass(slots=True)
class SttDiagnosticsSettings:
    record_seconds: float = 5.0
    vad_filter: bool = True
    initial_prompt: str = ""


@dataclass(slots=True)
class SttProsodySettings:
    enabled: bool = False
    include_in_prompt: bool = True


@dataclass(slots=True)
class TtsSettings:
    provider: str
    voice: str
    enabled: bool
    kokoro_model_path: str
    kokoro_voices_path: str
    pykokoro_pause_mode: str = "auto"
    llasa_model: str = ""
    llasa_codec_model: str = ""
    llasa_device: str = "cuda"
    llasa_temperature: float = 0.8
    llasa_top_p: float = 0.95
    llasa_max_length: int = 2048
    llasa_max_vram_mb: int = 0
    pocket_tts_voice: str = "alba"
    pocket_tts_temp: float = 0.7
    pocket_tts_custom_voices_dir: str = ""


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


@dataclass(slots=True)
class LoggingSettings:
    level: str = "INFO"


@dataclass(slots=True)
class AgentSettings:
    """Agent context, compression, personality evolution, and proactive conversation."""
    num_history_runs: int = 10
    compress_tool_results: bool = True
    compress_tool_results_limit: int | None = None
    compress_token_limit: int | None = None
    personality_prune_threshold: float = 0.15
    personality_decay_rate: float = 0.1
    personality_max_notes: int = 40
    personality_token_budget: int = 300
    proactive_silence_seconds: float = 45.0
    proactive_cooldown_seconds: float = 120.0


@dataclass(slots=True)
class McpServerSettings:
    enabled: bool = True
    port: int = 6274


@dataclass(slots=True)
class AppSettings:
    assistant: AssistantSettings
    autonomy: AutonomySettings
    ollama: OllamaSettings
    audio: AudioSettings
    database: DatabaseSettings
    actions: ActionSettings
    stt: SttSettings
    tts: TtsSettings
    ui: UiSettings
    tooling: ToolingBridgeSettings
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    mcp_server: McpServerSettings = field(default_factory=McpServerSettings)


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "default.json"
USER_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "user.json"


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


_config_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    key = str(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    cached = _config_cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    result = raw if isinstance(raw, dict) else {}
    _config_cache[key] = (mtime, result)
    return result


def _read_merged_overrides(*paths: Path) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for path in paths:
        try:
            current = _read_config(path)
        except Exception:
            continue
        merged = _deep_merge(merged, current)
    return merged


def _parse_session_tool_policy(
    raw_policy: object,
    *,
    defaults: SessionToolPolicySettings,
) -> SessionToolPolicySettings:
    policy = raw_policy if isinstance(raw_policy, dict) else {}
    prefixes_raw = policy.get("allowed_tool_prefixes", defaults.allowed_tool_prefixes)
    if isinstance(prefixes_raw, (list, tuple)):
        cleaned_prefixes = tuple(
            str(item).strip().lower()
            for item in prefixes_raw
            if str(item).strip()
        )
    else:
        cleaned_prefixes = defaults.allowed_tool_prefixes

    return SessionToolPolicySettings(
        native_tool_calls_enabled=bool(
            policy.get("native_tool_calls_enabled", defaults.native_tool_calls_enabled)
        ),
        allowed_tool_prefixes=cleaned_prefixes,
        pre_execution_narration_default=bool(
            policy.get("pre_execution_narration_default", defaults.pre_execution_narration_default)
        ),
    )


def _parse_session_tool_policies(raw: object) -> SessionToolPoliciesSettings:
    payload = raw if isinstance(raw, dict) else {}

    agentic_defaults = SessionToolPolicySettings(
        native_tool_calls_enabled=True,
        allowed_tool_prefixes=("mcp.",),
        pre_execution_narration_default=True,
    )
    chat_defaults = SessionToolPolicySettings(
        native_tool_calls_enabled=False,
        allowed_tool_prefixes=("mcp.",),
        pre_execution_narration_default=False,
    )
    reading_defaults = SessionToolPolicySettings(
        native_tool_calls_enabled=True,
        allowed_tool_prefixes=("mcp.",),
        pre_execution_narration_default=False,
    )

    return SessionToolPoliciesSettings(
        agentic=_parse_session_tool_policy(payload.get("agentic", {}), defaults=agentic_defaults),
        chat=_parse_session_tool_policy(payload.get("chat", {}), defaults=chat_defaults),
        reading=_parse_session_tool_policy(payload.get("reading", {}), defaults=reading_defaults),
    )


def _normalize_response_style(value: Any) -> str:
    s = str(value or "balanced").strip().lower()
    if s in ("balanced", "concise", "detailed"):
        return s
    return "balanced"


def _normalize_tts_length_scale(value: Any) -> float:
    try:
        f = float(value if value is not None else 1.0)
        return max(0.65, min(f, 1.35))
    except (TypeError, ValueError):
        return 1.0


def load_settings(config_path: Path | None = None) -> AppSettings:
    if config_path is not None:
        base = _read_config(config_path)
    else:
        base = _read_merged_overrides(DEFAULT_CONFIG_PATH)
    user = _read_merged_overrides(USER_CONFIG_PATH)
    raw = _deep_merge(base, user)

    assistant = raw.get("assistant", {}) or {}
    autonomy = raw.get("autonomy", {}) or {}
    ollama = raw.get("ollama", {}) or {}
    audio = raw.get("audio", {}) or {}
    database = raw.get("database") or {}
    actions = raw.get("actions", {}) or {}
    stt = raw.get("stt", {}) or {}
    tts = raw.get("tts", {}) or {}
    ui = raw.get("ui", {}) or {}
    tooling = raw.get("tooling", {}) or {}
    agent_raw = raw.get("agent", {}) or {}
    logging_raw = raw.get("logging", {}) or {}
    mcp_server_raw = raw.get("mcp_server", {}) or {}

    turn_planning = autonomy.get("turn_planning", {}) if isinstance(autonomy.get("turn_planning", {}), dict) else {}
    stt_diagnostics = stt.get("diagnostics", {}) if isinstance(stt.get("diagnostics", {}), dict) else {}
    stt_prosody = stt.get("prosody", {}) if isinstance(stt.get("prosody", {}), dict) else {}

    stt_diag_seconds_raw = stt_diagnostics.get("record_seconds", 5.0)
    stt_diag_seconds = 5.0 if stt_diag_seconds_raw is None else float(stt_diag_seconds_raw)
    stt_diag_vad_raw = stt_diagnostics.get("vad_filter", True)
    stt_diag_vad = True if stt_diag_vad_raw is None else bool(stt_diag_vad_raw)
    stt_diag_prompt_raw = stt_diagnostics.get("initial_prompt", "")
    stt_diag_prompt = "" if stt_diag_prompt_raw is None else str(stt_diag_prompt_raw)
    stt_prosody_enabled_raw = stt_prosody.get("enabled", False)
    stt_prosody_enabled = False if stt_prosody_enabled_raw is None else bool(stt_prosody_enabled_raw)
    stt_prosody_prompt_raw = stt_prosody.get("include_in_prompt", True)
    stt_prosody_prompt = True if stt_prosody_prompt_raw is None else bool(stt_prosody_prompt_raw)

    narration_level_raw = str(autonomy.get("agentic_narration_level", "summary")).strip().lower()
    if narration_level_raw not in {"full", "summary", "off"}:
        narration_level_raw = "summary"

    session_tool_policies = _parse_session_tool_policies(
        autonomy.get("session_tool_policies", {}),
    )

    return AppSettings(
        assistant=AssistantSettings(
            name=_required(assistant, "name"),
            remember_history=bool(_required(assistant, "remember_history")),
            background=str(assistant.get("background", "")).strip(),
            background_path=str(assistant.get("background_path", "") or "").strip(),
            user_id=str(assistant.get("user_id", "default") or "default").strip() or "default",
            response_style=_normalize_response_style(assistant.get("response_style")),
            tts_length_scale=_normalize_tts_length_scale(assistant.get("tts_length_scale")),
        ),
        autonomy=AutonomySettings(
            enabled=bool(autonomy.get("enabled", False)),
            mode=(
                str(autonomy.get("mode", "interactive")).strip().lower()
                if str(autonomy.get("mode", "interactive")).strip().lower()
                in {"manual", "interactive", "automatic"}
                else "interactive"
            ),
            auto_goal_switch=bool(autonomy.get("auto_goal_switch", True)),
            default_goal=str(autonomy.get("default_goal", "general_conversation")).strip()
            or "general_conversation",
            goal_switch_min_confidence=max(
                0.0,
                min(float(autonomy.get("goal_switch_min_confidence", 0.6)), 1.0),
            ),
            turn_planning=AutonomyTurnPlanningSettings(
                proactive_conversation=bool(turn_planning.get("proactive_conversation", True)),
                allow_action_suggestions=bool(turn_planning.get("allow_action_suggestions", True)),
                allow_proactive_actions=bool(turn_planning.get("allow_proactive_actions", False)),
                max_strategy_chars=max(40, int(turn_planning.get("max_strategy_chars", 180))),
            ),
            force_agentic_session=bool(autonomy.get("force_agentic_session", False)),
            agentic_max_auto_steps=max(1, min(int(autonomy.get("agentic_max_auto_steps", 3)), 20)),
            agentic_narration_level=narration_level_raw,
            session_tool_policies=session_tool_policies,
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
            context_window=(int(ollama["context_window"]) if ollama.get("context_window") is not None else None),
            embedding_model=str(ollama.get("embedding_model", "qwen3-embedding:0.6b")).strip() or "qwen3-embedding:0.6b",
            judge_model=str(ollama.get("judge_model", "qwen2.5:0.5b")).strip() or "qwen2.5:0.5b",
            timeout=int(ollama.get("timeout", 300)),
        ),
        audio=AudioSettings(
            sample_rate=int(_required(audio, "sample_rate")),
            channels=int(_required(audio, "channels")),
            enable_microphone=bool(_required(audio, "enable_microphone")),
            microphone_device=(
                int(audio["microphone_device"]) if audio.get("microphone_device") is not None else None
            ),
            output_device=(
                int(audio["output_device"]) if audio.get("output_device") is not None else None
            ),
            vad_level_threshold=float(audio.get("vad_level_threshold", 0.02)),
            vad_silence_seconds=float(audio.get("vad_silence_seconds", 1.0)),
            barge_in_enabled=bool(audio.get("barge_in_enabled", False)),
            live_input_mode=str(audio.get("live_input_mode", "voice_detection")).strip() or "voice_detection",
            live_ptt_type=str(audio.get("live_ptt_type", "keyboard")).strip().lower() or "keyboard",
            live_ptt_key=(str(audio["live_ptt_key"]).strip() or None) if audio.get("live_ptt_key") is not None else None,
            live_ptt_mouse_button=(str(audio["live_ptt_mouse_button"]).strip().lower() or None) if audio.get("live_ptt_mouse_button") is not None else None,
            live_ptt_toggle=bool(audio.get("live_ptt_toggle", False)),
        ),
        database=DatabaseSettings(
            provider=str(database.get("provider", "sqlite")).strip().lower() or "sqlite",
            url=(str(database["url"]).strip() if database.get("url") else None) or (str(os.environ.get("DATABASE_URL", "")).strip() or None),
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
            model=str(stt.get("model", "base")),
            language=(str(stt.get("language")).strip() if stt.get("language") is not None else None),
            diagnostics=SttDiagnosticsSettings(
                record_seconds=max(1.0, min(stt_diag_seconds, 30.0)),
                vad_filter=stt_diag_vad,
                initial_prompt=stt_diag_prompt.strip(),
            ),
            prosody=SttProsodySettings(
                enabled=stt_prosody_enabled,
                include_in_prompt=stt_prosody_prompt,
            ),
        ),
        tts=TtsSettings(
            provider=str(tts.get("provider", "kokoro")),
            voice=str(tts.get("voice", "af_heart")),
            enabled=bool(tts.get("enabled", True)),
            kokoro_model_path=str(tts.get("kokoro_model_path", "kokoro-v1.0.onnx")),
            kokoro_voices_path=str(tts.get("kokoro_voices_path", "voices-v1.0.bin")),
            pykokoro_pause_mode=str(tts.get("pykokoro_pause_mode", "auto")),
            llasa_model=str(tts.get("llasa_model", "NandemoGHS/Anime-Llasa-3B")),
            llasa_codec_model=str(tts.get("llasa_codec_model", "HKUSTAudio/xcodec2")),
            llasa_device=str(tts.get("llasa_device", "cuda")),
            llasa_temperature=float(tts.get("llasa_temperature", 0.8)),
            llasa_top_p=float(tts.get("llasa_top_p", 0.95)),
            llasa_max_length=max(256, int(tts.get("llasa_max_length", 2048))),
            llasa_max_vram_mb=max(0, int(tts.get("llasa_max_vram_mb", 0))),
            pocket_tts_voice=str(tts.get("pocket_tts_voice", "alba")),
            pocket_tts_temp=float(tts.get("pocket_tts_temp", 0.7)),
            pocket_tts_custom_voices_dir=str(tts.get("pocket_tts_custom_voices_dir", "")),
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
        ),
        agent=AgentSettings(
            num_history_runs=max(1, min(int(agent_raw.get("num_history_runs", 10)), 50)),
            compress_tool_results=bool(agent_raw.get("compress_tool_results", True)),
            compress_tool_results_limit=(
                int(agent_raw["compress_tool_results_limit"])
                if agent_raw.get("compress_tool_results_limit") is not None
                else None
            ),
            compress_token_limit=(
                int(agent_raw["compress_token_limit"])
                if agent_raw.get("compress_token_limit") is not None
                else None
            ),
            personality_prune_threshold=max(0.0, min(float(agent_raw.get("personality_prune_threshold", 0.15)), 1.0)),
            personality_decay_rate=max(0.0, min(float(agent_raw.get("personality_decay_rate", 0.1)), 0.5)),
            personality_max_notes=max(5, min(int(agent_raw.get("personality_max_notes", 40)), 100)),
            personality_token_budget=max(50, min(int(agent_raw.get("personality_token_budget", 300)), 1000)),
            proactive_silence_seconds=max(10.0, float(agent_raw.get("proactive_silence_seconds", 45.0))),
            proactive_cooldown_seconds=max(30.0, float(agent_raw.get("proactive_cooldown_seconds", 120.0))),
        ),
        logging=LoggingSettings(
            level=str(logging_raw.get("level", "INFO")).strip().upper() or "INFO",
        ),
        mcp_server=McpServerSettings(
            enabled=bool(mcp_server_raw.get("enabled", True)),
            port=max(1, int(mcp_server_raw.get("port", 6274))),
        ),
    )


def save_runtime_preferences(
    *,
    chat_model: str,
    remember_history: bool,
    autonomy_mode: str,
    microphone_device: int | None,
    output_device: int | None = None,
    vad_level_threshold: float = 0.02,
    vad_silence_seconds: float = 1.0,
    live_input_mode: str | None = None,
    live_ptt_type: str | None = None,
    live_ptt_key: str | None = None,
    live_ptt_mouse_button: str | None = None,
    live_ptt_toggle: bool | None = None,
    barge_in_enabled: bool | None = None,
    action_min_interval_seconds: float = 1.0,
    tts_provider: str,
    tts_voice: str | None,
    stt_model: str | None = None,
    stt_diagnostic_record_seconds: float | None = None,
    stt_diagnostic_vad_filter: bool | None = None,
    stt_diagnostic_initial_prompt: str | None = None,
    stt_prosody_enabled: bool | None = None,
    stt_prosody_include_in_prompt: bool | None = None,
    pocket_tts_voice: str | None = None,
    pocket_tts_temp: float | None = None,
    enable_microphone: bool,
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
            "output_device": output_device,
            "vad_level_threshold": round(vad_level_threshold, 4),
            "vad_silence_seconds": round(vad_silence_seconds, 2),
            "enable_microphone": bool(enable_microphone),
        },
        "actions": {
            "min_action_interval_seconds": round(max(0.0, action_min_interval_seconds), 2),
        },
        "tts": {
            "provider": str(tts_provider or "piper").strip().lower() or "piper",
            "voice": str(tts_voice or "").strip(),
            **({"pocket_tts_voice": str(pocket_tts_voice).strip()} if pocket_tts_voice else {}),
            **({"pocket_tts_temp": round(float(pocket_tts_temp), 2)} if pocket_tts_temp is not None else {}),
        },
        "stt": {
            "diagnostics": {},
            "prosody": {},
        },
    }
    audio_updates = updates["audio"]
    if live_input_mode is not None:
        audio_updates["live_input_mode"] = str(live_input_mode).strip() or "voice_detection"
    if live_ptt_type is not None:
        audio_updates["live_ptt_type"] = str(live_ptt_type).strip().lower() or "keyboard"
    if live_ptt_key is not None:
        audio_updates["live_ptt_key"] = (str(live_ptt_key).strip() or None)
    if live_ptt_mouse_button is not None:
        audio_updates["live_ptt_mouse_button"] = (str(live_ptt_mouse_button).strip().lower() or None)
    if live_ptt_toggle is not None:
        audio_updates["live_ptt_toggle"] = bool(live_ptt_toggle)
    if barge_in_enabled is not None:
        audio_updates["barge_in_enabled"] = bool(barge_in_enabled)

    stt_updates: dict[str, Any] = updates["stt"]
    diagnostics_updates = stt_updates["diagnostics"]
    prosody_updates = stt_updates["prosody"]
    model_value = str(stt_model or "").strip()
    if model_value:
        stt_updates["model"] = model_value
    if stt_diagnostic_record_seconds is not None:
        diagnostics_updates["record_seconds"] = round(
            max(1.0, min(float(stt_diagnostic_record_seconds), 30.0)),
            1,
        )
    if stt_diagnostic_vad_filter is not None:
        diagnostics_updates["vad_filter"] = bool(stt_diagnostic_vad_filter)
    if stt_diagnostic_initial_prompt is not None:
        diagnostics_updates["initial_prompt"] = str(stt_diagnostic_initial_prompt or "").strip()
    if stt_prosody_enabled is not None:
        prosody_updates["enabled"] = bool(stt_prosody_enabled)
    if stt_prosody_include_in_prompt is not None:
        prosody_updates["include_in_prompt"] = bool(stt_prosody_include_in_prompt)

    if not diagnostics_updates:
        stt_updates.pop("diagnostics", None)
    if not prosody_updates:
        stt_updates.pop("prosody", None)
    if not stt_updates:
        updates.pop("stt", None)

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
