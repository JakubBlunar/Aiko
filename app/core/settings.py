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
    embedding_base_url: str = ""  # empty = use base_url
    proactive_planner_base_url: str = ""  # empty = use base_url
    context_window: int | None = None  # None = auto-detect from Ollama API
    embedding_model: str = "qwen3-embedding:0.6b"
    judge_model: str = "qwen2.5:0.5b"
    proactive_planner_model: str = ""  # empty = use judge_model for proactive director
    timeout: int = 300  # HTTP timeout in seconds (shared by all Ollama clients)


@dataclass(slots=True)
class ChatLlmSettings:
    """Chat-LLM provider routing layer.

    Sits in front of :class:`OllamaSettings`. When ``provider == "ollama"`` and
    ``base_url``/``model``/``api_key`` are blank the legacy local Ollama chat
    behaviour is preserved unchanged. Setting ``base_url`` to ``https://ollama.com``
    plus an ``api_key`` flips the same code path to Ollama Cloud Pro. The
    ``openai_compatible`` provider routes through ``langchain-openai``'s
    ``ChatOpenAI`` and covers OpenAI / xAI Grok / Groq / OpenRouter / DeepSeek /
    Together / Mistral via custom ``base_url``.
    """

    provider: str = "ollama"  # "ollama" | "openai_compatible"
    model: str = ""  # empty -> falls back to OllamaSettings.chat_model
    base_url: str = ""  # empty -> falls back to OllamaSettings.base_url for ollama provider
    api_key: str = ""  # empty -> looked up via api_key_env / inferred from base_url host
    api_key_env: str = ""  # explicit env var name; empty -> inferred per host
    context_window: int | None = None  # None -> auto-detect (ollama) or model lookup (openai)
    temperature: float | None = None  # None -> inherit OllamaSettings.temperature
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Hard cap on tokens generated per assistant reply. Without it, models
    # routinely emit 2k+ tokens of rambling on casual chat. 512 fits ~3
    # short paragraphs which is plenty for chat AND tool summaries; raise
    # for long-form code generation. Set to 0 / negative to disable.
    max_tokens: int = 512


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
    response_style: str = "balanced"  # balanced | conversational | concise | detailed | technical
    tts_length_scale: float = 1.0  # TTS speed: 0.65–1.35, higher = slower
    # Auto-greet on app launch (LLM call). Off by default since persisted
    # sessions already give the user conversational continuity.
    startup_greeting_enabled: bool = False


# DEPRECATED v0-only -- the legacy LangChain agent / tool-dispatch surface no
# longer exists in lean v1. SessionToolPolicySettings / SessionToolPoliciesSettings
# / AutonomyTurnPlanningSettings / AutonomySettings / ActionSettings are still
# loaded so existing user.json files keep parsing, but nothing in the v1
# runtime reads them. Phase F (tools) will introduce a new, narrower
# tool-policy block. Do not extend these.
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


# DEPRECATED v0-only.
@dataclass(slots=True)
class AutonomyTurnPlanningSettings:
    proactive_conversation: bool = True
    allow_action_suggestions: bool = True
    allow_proactive_actions: bool = False
    max_strategy_chars: int = 180


# DEPRECATED v0-only -- AutonomySettings drove the legacy goal-switch + reading
# session machinery. Lean v1 has a single chat session shape; these knobs are
# parsed for back-compat only.
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


# DEPRECATED v0-only -- ActionSettings was the action-dispatcher / GUI agent
# config. Removed from the v1 runtime; kept here so legacy user.json parses.
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
    dialog_geometries: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(slots=True)
class ToolingBridgeSettings:
    config_default_path: str
    config_user_path: str


@dataclass(slots=True)
class LoggingSettings:
    level: str = "INFO"


@dataclass(slots=True)
class AgentSettings:
    """Agent context, compression, and proactive conversation.

    Lean-v1 fields actively used today:
      - ``proactive_silence_seconds``, ``proactive_cooldown_seconds`` -- driven
        by :class:`app.core.proactive_director.ProactiveDirector`.
      - ``num_history_runs`` -- recent-window size hint.

    All other fields below are DEPRECATED v0-only (LangChain / triage judge /
    personality notebook / browser-snapshot compression / agent archive). They
    parse for back-compat with old ``user.json`` files but no v1 code reads
    them. Phase F will introduce a focused tool-settings block.
    """
    num_history_runs: int = 10
    compress_tool_results: bool = True
    compress_tool_results_limit: int | None = None
    compress_token_limit: int | None = None
    personality_prune_threshold: float = 0.15  # v0-only
    personality_decay_rate: float = 0.1  # v0-only
    personality_max_notes: int = 40  # v0-only
    personality_token_budget: int = 300  # v0-only
    proactive_silence_seconds: float = 45.0
    proactive_cooldown_seconds: float = 120.0
    proactive_planner_enabled: bool = True  # v0-only
    proactive_use_main_for_utterance: bool = False  # v0-only
    proactive_context_messages: int = 10  # v0-only
    proactive_background_interval_seconds: float = 90.0  # v0-only
    proactive_background_stale_seconds: float = 120.0  # v0-only
    proactive_brain_advise_main: bool = True  # v0-only
    proactive_brain_drive_speech: bool = True  # v0-only
    proactive_brain_influence_autonomy: bool = False  # v0-only
    proactive_brain_request_actions_via_main: bool = False  # v0-only
    proactive_speech_requires_live: bool = True  # v0-only
    archive_enabled: bool = True  # v0-only
    archive_days_threshold: int = 30  # v0-only
    browser_snapshot_compress: bool = True  # v0-only
    browser_snapshot_max_chars: int | None = None  # v0-only
    browser_snapshot_max_text_run: int | None = None  # v0-only
    tool_dispatch_mode: str = "controller"  # v0-only
    tool_iterations_max: int = 3  # v0-only
    triage_judge_enabled: bool = True  # v0-only
    triage_judge_timeout_seconds: float = 0.5  # v0-only


@dataclass(slots=True)
class McpServerSettings:
    enabled: bool = True
    port: int = 6274


@dataclass(slots=True)
class WebServerSettings:
    """FastAPI/WebSocket layer that serves the React UI."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 6275


@dataclass(slots=True)
class MemorySettings:
    """Long-term memory: cross-session vector store of durable facts.

    Populated by background extraction after each summary, plus any
    ``[[remember:...]]`` tags Aiko emits inline.
    """

    enabled: bool = True
    top_k: int = 6
    score_threshold: float = 0.4
    max_memories: int = 500
    dedupe_threshold: float = 0.92
    extractor_enabled: bool = True
    self_tagged_salience: float = 0.7


@dataclass(slots=True)
class ToolsSettings:
    """Lean v1 tool-calling configuration.

    Tools are dispatched in :class:`app.core.turn_runner.TurnRunner` via a
    pre-stream ``chat_with_tools`` pass. Each switch below toggles a single
    tool; setting ``enabled=False`` disables the whole tool registry.
    """

    enabled: bool = True
    get_time: bool = True
    recall: bool = True
    web_search: bool = True


@dataclass(slots=True)
class PersonaSettings:
    """Live2D persona avatar configuration."""

    enabled: bool = False
    mode: str = "embedded"  # "embedded" | "overlay"
    model_path: str = "data/avatars/hiyori/Hiyori.model3.json"
    scale: float = 0.25
    anchor: str = "bottom-center"
    mirror: bool = False
    lip_sync_gain: float = 1.2
    expression_map: dict = field(default_factory=dict)
    overlay_x: int | None = None
    overlay_y: int | None = None
    overlay_width: int | None = None
    overlay_height: int | None = None
    embedded_width: int | None = None


_DEFAULT_EXPRESSION_MAP: dict[str, str] = {
    "neutral": "",
    "cheerful": "F01",
    "excited": "F02",
    "friendly": "F03",
    "calm": "F04",
    "serious": "F05",
    "sad": "F06",
    "gentle": "F07",
    "angry": "F08",
    "surprised": "F08",
}


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
    web_server: WebServerSettings = field(default_factory=WebServerSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    persona: PersonaSettings = field(default_factory=PersonaSettings)
    chat_llm: ChatLlmSettings = field(default_factory=ChatLlmSettings)
    tools: ToolsSettings = field(default_factory=ToolsSettings)


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


def _normalize_tool_dispatch_mode(value: Any) -> str:
    s = str(value or "controller").strip().lower()
    if s in ("controller", "plain_only", "legacy_react"):
        return s
    return "controller"


def _normalize_response_style(value: Any) -> str:
    s = str(value or "balanced").strip().lower()
    if s in ("balanced", "conversational", "concise", "detailed", "technical"):
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
    web_server_raw = raw.get("web_server", {}) or {}
    memory_raw = raw.get("memory", {}) or {}
    persona_raw = raw.get("persona", {}) or {}
    chat_llm_raw = raw.get("chat_llm", {}) or {}
    tools_raw = raw.get("tools", {}) or {}

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
            startup_greeting_enabled=bool(assistant.get("startup_greeting_enabled", False)),
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
            embedding_base_url=str(ollama.get("embedding_base_url", "") or "").strip(),
            proactive_planner_base_url=str(ollama.get("proactive_planner_base_url", "") or "").strip(),
            chat_model=_required(ollama, "chat_model"),
            temperature=float(_required(ollama, "temperature")),
            context_window=(int(ollama["context_window"]) if ollama.get("context_window") is not None else None),
            embedding_model=str(ollama.get("embedding_model", "qwen3-embedding:0.6b")).strip() or "qwen3-embedding:0.6b",
            judge_model=str(ollama.get("judge_model", "qwen2.5:0.5b")).strip() or "qwen2.5:0.5b",
            proactive_planner_model=str(ollama.get("proactive_planner_model", "") or "").strip(),
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
            dialog_geometries=(
                {
                    str(k): {str(ik): int(iv) for ik, iv in v.items()}
                    for k, v in ui.get("dialog_geometries", {}).items()
                    if isinstance(v, dict)
                }
                if isinstance(ui.get("dialog_geometries"), dict)
                else {}
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
            proactive_planner_enabled=bool(agent_raw.get("proactive_planner_enabled", True)),
            proactive_use_main_for_utterance=bool(agent_raw.get("proactive_use_main_for_utterance", False)),
            proactive_context_messages=max(2, min(int(agent_raw.get("proactive_context_messages", 10)), 40)),
            proactive_background_interval_seconds=max(20.0, float(agent_raw.get("proactive_background_interval_seconds", 90.0))),
            proactive_background_stale_seconds=max(30.0, float(agent_raw.get("proactive_background_stale_seconds", 120.0))),
            proactive_brain_advise_main=bool(agent_raw.get("proactive_brain_advise_main", True)),
            proactive_brain_drive_speech=bool(agent_raw.get("proactive_brain_drive_speech", True)),
            proactive_brain_influence_autonomy=bool(agent_raw.get("proactive_brain_influence_autonomy", False)),
            proactive_brain_request_actions_via_main=bool(agent_raw.get("proactive_brain_request_actions_via_main", False)),
            proactive_speech_requires_live=bool(agent_raw.get("proactive_speech_requires_live", True)),
            archive_enabled=bool(agent_raw.get("archive_enabled", True)),
            archive_days_threshold=max(7, min(int(agent_raw.get("archive_days_threshold", 30)), 365)),
            browser_snapshot_compress=bool(agent_raw.get("browser_snapshot_compress", True)),
            browser_snapshot_max_chars=(
                int(agent_raw["browser_snapshot_max_chars"])
                if agent_raw.get("browser_snapshot_max_chars") is not None
                else None
            ),
            browser_snapshot_max_text_run=(
                int(agent_raw["browser_snapshot_max_text_run"])
                if agent_raw.get("browser_snapshot_max_text_run") is not None
                else None
            ),
            tool_dispatch_mode=_normalize_tool_dispatch_mode(
                agent_raw.get("tool_dispatch_mode", "controller")
            ),
            tool_iterations_max=max(
                1, min(int(agent_raw.get("tool_iterations_max", 3)), 20)
            ),
            triage_judge_enabled=bool(agent_raw.get("triage_judge_enabled", True)),
            triage_judge_timeout_seconds=max(
                0.1, min(float(agent_raw.get("triage_judge_timeout_seconds", 0.5)), 10.0)
            ),
        ),
        logging=LoggingSettings(
            level=str(logging_raw.get("level", "INFO")).strip().upper() or "INFO",
        ),
        mcp_server=McpServerSettings(
            enabled=bool(mcp_server_raw.get("enabled", True)),
            port=max(1, int(mcp_server_raw.get("port", 6274))),
        ),
        web_server=WebServerSettings(
            enabled=bool(web_server_raw.get("enabled", True)),
            host=str(web_server_raw.get("host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1",
            port=max(1, int(web_server_raw.get("port", 6275))),
        ),
        memory=MemorySettings(
            enabled=bool(memory_raw.get("enabled", True)),
            top_k=max(0, int(memory_raw.get("top_k", 6))),
            score_threshold=max(0.0, min(1.0, float(memory_raw.get("score_threshold", 0.4)))),
            max_memories=max(50, int(memory_raw.get("max_memories", 500))),
            dedupe_threshold=max(0.5, min(0.999, float(memory_raw.get("dedupe_threshold", 0.92)))),
            extractor_enabled=bool(memory_raw.get("extractor_enabled", True)),
            self_tagged_salience=max(0.0, min(1.0, float(memory_raw.get("self_tagged_salience", 0.7)))),
        ),
        persona=_parse_persona(persona_raw),
        chat_llm=_parse_chat_llm(chat_llm_raw),
        tools=ToolsSettings(
            enabled=bool(tools_raw.get("enabled", True)),
            get_time=bool(tools_raw.get("get_time", True)),
            recall=bool(tools_raw.get("recall", True)),
            web_search=bool(tools_raw.get("web_search", True)),
        ),
    )


def _parse_chat_llm(raw: dict[str, Any]) -> ChatLlmSettings:
    """Validate the chat_llm config block, falling back to defaults on missing keys."""

    payload = raw if isinstance(raw, dict) else {}

    provider_raw = str(payload.get("provider", "ollama") or "ollama").strip().lower()
    if provider_raw not in {"ollama", "openai_compatible"}:
        provider_raw = "ollama"

    headers_raw = payload.get("extra_headers") or {}
    if isinstance(headers_raw, dict):
        extra_headers = {
            str(k).strip(): str(v).strip()
            for k, v in headers_raw.items()
            if str(k).strip() and v is not None
        }
    else:
        extra_headers = {}

    ctx_raw = payload.get("context_window")
    try:
        context_window = int(ctx_raw) if ctx_raw not in (None, "", 0) else None
    except (TypeError, ValueError):
        context_window = None

    temp_raw = payload.get("temperature")
    try:
        temperature = float(temp_raw) if temp_raw not in (None, "") else None
    except (TypeError, ValueError):
        temperature = None

    max_tokens_raw = payload.get("max_tokens", 512)
    try:
        max_tokens = int(max_tokens_raw) if max_tokens_raw not in (None, "") else 512
    except (TypeError, ValueError):
        max_tokens = 512

    return ChatLlmSettings(
        provider=provider_raw,
        model=str(payload.get("model", "") or "").strip(),
        base_url=str(payload.get("base_url", "") or "").strip(),
        api_key=str(payload.get("api_key", "") or "").strip(),
        api_key_env=str(payload.get("api_key_env", "") or "").strip(),
        context_window=context_window,
        temperature=temperature,
        extra_headers=extra_headers,
        max_tokens=max_tokens,
    )


def _parse_persona(raw: dict[str, Any]) -> PersonaSettings:
    mode = str(raw.get("mode", "embedded")).strip().lower()
    if mode not in {"embedded", "overlay"}:
        mode = "embedded"
    expression_map_raw = raw.get("expression_map") or {}
    if isinstance(expression_map_raw, dict):
        expression_map = {
            str(k).strip().lower(): str(v or "").strip()
            for k, v in expression_map_raw.items()
            if str(k).strip()
        }
    else:
        expression_map = {}
    merged_map = dict(_DEFAULT_EXPRESSION_MAP)
    merged_map.update(expression_map)
    return PersonaSettings(
        enabled=bool(raw.get("enabled", False)),
        mode=mode,
        model_path=str(raw.get("model_path", "data/avatars/hiyori/Hiyori.model3.json") or "").strip()
        or "data/avatars/hiyori/Hiyori.model3.json",
        scale=max(0.05, min(float(raw.get("scale", 0.25) or 0.25), 2.0)),
        anchor=str(raw.get("anchor", "bottom-center") or "bottom-center").strip().lower(),
        mirror=bool(raw.get("mirror", False)),
        lip_sync_gain=max(0.1, min(float(raw.get("lip_sync_gain", 1.2) or 1.2), 4.0)),
        expression_map=merged_map,
        overlay_x=int(raw["overlay_x"]) if raw.get("overlay_x") is not None else None,
        overlay_y=int(raw["overlay_y"]) if raw.get("overlay_y") is not None else None,
        overlay_width=int(raw["overlay_width"]) if raw.get("overlay_width") is not None else None,
        overlay_height=int(raw["overlay_height"]) if raw.get("overlay_height") is not None else None,
        embedded_width=int(raw["embedded_width"]) if raw.get("embedded_width") is not None else None,
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
    ui_dialog_geometries: dict[str, dict[str, int]] | None = None,
    persona_enabled: bool | None = None,
    persona_mode: str | None = None,
    persona_model_path: str | None = None,
    persona_scale: float | None = None,
    persona_anchor: str | None = None,
    persona_mirror: bool | None = None,
    persona_lip_sync_gain: float | None = None,
    persona_expression_map: dict[str, str] | None = None,
    persona_overlay_geometry: dict[str, int] | None = None,
    persona_embedded_width: int | None = None,
    chat_llm_provider: str | None = None,
    chat_llm_model: str | None = None,
    chat_llm_base_url: str | None = None,
    chat_llm_api_key: str | None = None,
    chat_llm_api_key_env: str | None = None,
    chat_llm_context_window: int | None = None,
    chat_llm_temperature: float | None = None,
    chat_llm_extra_headers: dict[str, str] | None = None,
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
            "provider": str(tts_provider or "kokoro").strip().lower() or "kokoro",
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
    if ui_dialog_geometries is not None:
        existing = dict(effective.get("ui", {}).get("dialog_geometries", {}))
        existing.update(ui_dialog_geometries)
        ui_updates["dialog_geometries"] = {
            str(k): {str(ik): int(iv) for ik, iv in v.items()}
            for k, v in existing.items()
            if isinstance(v, dict)
        }
    if ui_updates:
        updates["ui"] = ui_updates

    persona_updates: dict[str, Any] = {}
    if persona_enabled is not None:
        persona_updates["enabled"] = bool(persona_enabled)
    if persona_mode is not None:
        mode_norm = str(persona_mode).strip().lower()
        persona_updates["mode"] = mode_norm if mode_norm in {"embedded", "overlay"} else "embedded"
    if persona_model_path is not None:
        persona_updates["model_path"] = str(persona_model_path).strip()
    if persona_scale is not None:
        persona_updates["scale"] = max(0.05, min(float(persona_scale), 2.0))
    if persona_anchor is not None:
        persona_updates["anchor"] = str(persona_anchor).strip().lower() or "bottom-center"
    if persona_mirror is not None:
        persona_updates["mirror"] = bool(persona_mirror)
    if persona_lip_sync_gain is not None:
        persona_updates["lip_sync_gain"] = max(0.1, min(float(persona_lip_sync_gain), 4.0))
    if persona_expression_map is not None:
        persona_updates["expression_map"] = {
            str(k).strip().lower(): str(v or "").strip()
            for k, v in persona_expression_map.items()
            if str(k).strip()
        }
    if persona_overlay_geometry is not None:
        persona_updates.update(
            {
                "overlay_x": int(persona_overlay_geometry["x"])
                if "x" in persona_overlay_geometry else None,
                "overlay_y": int(persona_overlay_geometry["y"])
                if "y" in persona_overlay_geometry else None,
                "overlay_width": int(persona_overlay_geometry["width"])
                if "width" in persona_overlay_geometry else None,
                "overlay_height": int(persona_overlay_geometry["height"])
                if "height" in persona_overlay_geometry else None,
            }
        )
    if persona_embedded_width is not None:
        persona_updates["embedded_width"] = max(180, min(int(persona_embedded_width), 900))
    if persona_updates:
        updates["persona"] = persona_updates

    chat_llm_updates: dict[str, Any] = {}
    if chat_llm_provider is not None:
        provider_norm = str(chat_llm_provider).strip().lower()
        if provider_norm not in {"ollama", "openai_compatible"}:
            provider_norm = "ollama"
        chat_llm_updates["provider"] = provider_norm
    if chat_llm_model is not None:
        chat_llm_updates["model"] = str(chat_llm_model).strip()
    if chat_llm_base_url is not None:
        chat_llm_updates["base_url"] = str(chat_llm_base_url).strip()
    if chat_llm_api_key is not None:
        chat_llm_updates["api_key"] = str(chat_llm_api_key)
    if chat_llm_api_key_env is not None:
        chat_llm_updates["api_key_env"] = str(chat_llm_api_key_env).strip()
    if chat_llm_context_window is not None:
        try:
            chat_llm_updates["context_window"] = (
                int(chat_llm_context_window) if chat_llm_context_window else None
            )
        except (TypeError, ValueError):
            chat_llm_updates["context_window"] = None
    if chat_llm_temperature is not None:
        try:
            chat_llm_updates["temperature"] = float(chat_llm_temperature)
        except (TypeError, ValueError):
            chat_llm_updates["temperature"] = None
    if chat_llm_extra_headers is not None:
        chat_llm_updates["extra_headers"] = {
            str(k).strip(): str(v).strip()
            for k, v in chat_llm_extra_headers.items()
            if str(k).strip() and v is not None
        }
    if chat_llm_updates:
        updates["chat_llm"] = chat_llm_updates

    updated_effective = _deep_merge(effective, updates)
    minimal_overrides = _deep_diff(base, updated_effective)
    payload = minimal_overrides if isinstance(minimal_overrides, dict) else {}
    target.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
