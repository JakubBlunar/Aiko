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
    embedding_base_url: str = ""  # empty = use base_url
    context_window: int | None = None  # None = auto-detect from Ollama API
    embedding_model: str = "qwen3-embedding:0.6b"
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
    user_id: str = "default"  # Scopes memory per user
    tts_length_scale: float = 1.0  # TTS speed: 0.65–1.35, higher = slower


@dataclass(slots=True)
class SttSettings:
    model: str
    language: str | None


@dataclass(slots=True)
class TtsSettings:
    provider: str
    voice: str
    enabled: bool
    pocket_tts_voice: str = "alba"
    pocket_tts_temp: float = 0.7
    pocket_tts_custom_voices_dir: str = ""


@dataclass(slots=True)
class LoggingSettings:
    level: str = "INFO"


@dataclass(slots=True)
class AgentSettings:
    """Lean v1 conversation agent knobs.

    Driven by :class:`app.core.proactive_director.ProactiveDirector`.
    """

    proactive_silence_seconds: float = 45.0
    proactive_cooldown_seconds: float = 120.0


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
class AppSettings:
    assistant: AssistantSettings
    ollama: OllamaSettings
    audio: AudioSettings
    stt: SttSettings
    tts: TtsSettings
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    mcp_server: McpServerSettings = field(default_factory=McpServerSettings)
    web_server: WebServerSettings = field(default_factory=WebServerSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
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


def _normalize_tts_length_scale(value: Any) -> float:
    try:
        f = float(value if value is not None else 1.0)
        return max(0.65, min(f, 1.35))
    except (TypeError, ValueError):
        return 1.0


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


def load_settings(config_path: Path | None = None) -> AppSettings:
    if config_path is not None:
        base = _read_config(config_path)
    else:
        base = _read_merged_overrides(DEFAULT_CONFIG_PATH)
    user = _read_merged_overrides(USER_CONFIG_PATH)
    raw = _deep_merge(base, user)

    assistant = raw.get("assistant", {}) or {}
    ollama = raw.get("ollama", {}) or {}
    audio = raw.get("audio", {}) or {}
    stt = raw.get("stt", {}) or {}
    tts = raw.get("tts", {}) or {}
    agent_raw = raw.get("agent", {}) or {}
    logging_raw = raw.get("logging", {}) or {}
    mcp_server_raw = raw.get("mcp_server", {}) or {}
    web_server_raw = raw.get("web_server", {}) or {}
    memory_raw = raw.get("memory", {}) or {}
    chat_llm_raw = raw.get("chat_llm", {}) or {}
    tools_raw = raw.get("tools", {}) or {}

    return AppSettings(
        assistant=AssistantSettings(
            name=_required(assistant, "name"),
            remember_history=bool(_required(assistant, "remember_history")),
            user_id=str(assistant.get("user_id", "default") or "default").strip() or "default",
            tts_length_scale=_normalize_tts_length_scale(assistant.get("tts_length_scale")),
        ),
        ollama=OllamaSettings(
            base_url=_required(ollama, "base_url"),
            embedding_base_url=str(ollama.get("embedding_base_url", "") or "").strip(),
            chat_model=_required(ollama, "chat_model"),
            temperature=float(_required(ollama, "temperature")),
            context_window=(int(ollama["context_window"]) if ollama.get("context_window") is not None else None),
            embedding_model=str(ollama.get("embedding_model", "qwen3-embedding:0.6b")).strip() or "qwen3-embedding:0.6b",
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
        stt=SttSettings(
            model=str(stt.get("model", "base")),
            language=(str(stt.get("language")).strip() if stt.get("language") is not None else None),
        ),
        tts=TtsSettings(
            provider=str(tts.get("provider", "pocket-tts")),
            voice=str(tts.get("voice", "")),
            enabled=bool(tts.get("enabled", True)),
            pocket_tts_voice=str(tts.get("pocket_tts_voice", "alba")),
            pocket_tts_temp=float(tts.get("pocket_tts_temp", 0.7)),
            pocket_tts_custom_voices_dir=str(tts.get("pocket_tts_custom_voices_dir", "")),
        ),
        agent=AgentSettings(
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
        chat_llm=_parse_chat_llm(chat_llm_raw),
        tools=ToolsSettings(
            enabled=bool(tools_raw.get("enabled", True)),
            get_time=bool(tools_raw.get("get_time", True)),
            recall=bool(tools_raw.get("recall", True)),
            web_search=bool(tools_raw.get("web_search", True)),
        ),
    )
