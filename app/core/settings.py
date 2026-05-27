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
    # How long Ollama should keep the chat model loaded in VRAM after a
    # request completes. Default is "5m" upstream; bumping to "30m" keeps
    # the model warm across the typical idle gap between conversational
    # turns so we don't pay model-load latency on first token. Accepts any
    # Ollama duration string ("30m", "1h", "-1" for "until unloaded").
    # Tune down for shared-GPU setups where holding VRAM is expensive.
    keep_alive: str = "30m"


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
    module_levels: dict[str, str] = field(default_factory=dict)
    file_enabled: bool = True
    file_path: str = "data/app.log"
    file_max_bytes: int = 5 * 1024 * 1024
    file_backup_count: int = 5


@dataclass(slots=True)
class EndpointingSettings:
    """Tiered live-mic endpointing knobs.

    See :mod:`app.stt.endpointing` for the semantics. With defaults, a
    finished sentence ("…thanks.") closes ~0.6 s after the last spoken
    chunk, an ambiguous pause closes at ~3 s, and a hesitation marker
    ("…and uh") resets the silence counter so the user has a fresh ~3 s
    window to find the next word — bounded by ``turn_silence_seconds``.

    ``barge_in_min_speech_seconds`` is the minimum amount of speech a
    capture must contain before it is allowed to interrupt Aiko's TTS
    (only consulted when ``audio.barge_in_enabled`` is on).
    """

    enabled: bool = True
    use_partial_transcript: bool = True
    phrase_silence_seconds: float = 1.0
    turn_silence_seconds: float = 3.0
    fast_close_silence_seconds: float = 0.6
    hesitation_extend_to_turn: bool = True
    barge_in_min_speech_seconds: float = 0.7
    hesitation_markers: list[str] = field(default_factory=list)
    sentence_final_markers: list[str] = field(default_factory=list)


# Allow-list for ``AvatarSettings.auto_outfit``. Single source of truth
# shared by the settings loader, the web ``PATCH /api/avatar`` validator,
# and ``SessionController.update_avatar_settings`` so adding a new
# outfit only requires one edit here. Update the matching TS literal
# (``AvatarSettingsKnobs.auto_outfit``) in ``web/src/types.ts`` when
# this changes.
OUTFIT_MODES: frozenset[str] = frozenset({
    "auto",
    "day",
    "pajamas",
    "pajamas_hooded",
})


# Persona-window geometry clamps. The lower bounds are picked so the
# avatar still has at least a thumbnail's worth of pixels to render
# into; the upper bounds prevent absurd values (e.g. someone hand-edits
# ``user.json`` to request a 5000x5000 floating window) from being
# blindly accepted. The same clamps run on every code path that touches
# the values: load-time in ``load_settings`` and runtime in
# ``SessionController.update_desktop_settings``.
PERSONA_WINDOW_MIN_WIDTH: int = 220
PERSONA_WINDOW_MAX_WIDTH: int = 800
PERSONA_WINDOW_MIN_HEIGHT: int = 280
PERSONA_WINDOW_MAX_HEIGHT: int = 1024


def clamp_persona_window_width(value: Any, *, fallback: int = 320) -> int:
    """Coerce + clamp a persona-window width into the allowed range.

    Accepts anything Pythonic that can be cast to int (str, float,
    json.loads-friendly numerics). Returns ``fallback`` for inputs we
    can't parse, so a malformed config never raises during load.
    """
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        coerced = int(fallback)
    return max(PERSONA_WINDOW_MIN_WIDTH, min(PERSONA_WINDOW_MAX_WIDTH, coerced))


def clamp_persona_window_height(value: Any, *, fallback: int = 480) -> int:
    """Coerce + clamp a persona-window height. Mirrors
    :func:`clamp_persona_window_width`."""
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        coerced = int(fallback)
    return max(PERSONA_WINDOW_MIN_HEIGHT, min(PERSONA_WINDOW_MAX_HEIGHT, coerced))


@dataclass(slots=True)
class AvatarSettings:
    """Single bundled Live2D avatar (Alexia) + user-tunable knobs.

    The avatar files themselves are gitignored at ``root_dir``.
    ``scale_multiplier``, ``auto_outfit`` and ``expressiveness`` are
    the fields the UI lets the user change at runtime.
    """

    root_dir: str = "live-2d-models/Alexia"
    entry_filename: str = "Alexia.model3.json"
    scale_multiplier: float = 1.0
    # See ``OUTFIT_MODES`` above for the accepted values.
    #   "auto"            -> circadian-driven (pajamas at night when supported)
    #   "day"             -> always day clothes (baseline)
    #   "pajamas"         -> always pajamas (no sleeping cap)
    #   "pajamas_hooded"  -> always pajamas with sleeping cap
    auto_outfit: str = "auto"
    # Body-language intensity multiplier consumed by the renderer's
    # AmbientBodyChannel and ExpressionChannel. ``0.0`` mutes every
    # mood-driven amplitude (breath sway, body tilts, expression
    # strength, sass burst, …); ``1.0`` is the authored default;
    # ``1.5`` exaggerates within safe rig limits. Clamped on load.
    expressiveness: float = 1.0


@dataclass(slots=True)
class AgentSettings:
    """Lean v1 conversation agent knobs.

    Proactive nudges are driven by
    :class:`app.core.proactive_director.ProactiveDirector`.

    The ``summary_*`` knobs and ``max_prompt_tokens_pct`` together control
    context compaction (rolling summary + on-overflow squish) handled by
    :class:`app.core.summary_worker.SummaryWorker` and
    :class:`app.core.turn_runner.TurnRunner`.
    """

    proactive_silence_seconds: float = 45.0
    proactive_cooldown_seconds: float = 120.0
    # Rolling summary background worker.
    summary_idle_seconds: float = 15.0  # quiet time before summarising
    summary_min_unsummarized_messages: int = 6  # minimum new msgs to trigger
    summary_target_tokens: int = 600  # cap on the summary the LLM produces
    # When the *next* prompt would exceed this fraction of the context window,
    # schedule a background compaction immediately (don't wait for idle).
    max_prompt_tokens_pct: float = 0.8

    # ── Speaking-window scheduler (Phase 2a) ──────────────────────────
    # The scheduler drains LLM-driven background jobs (reflection, profile
    # updates, agenda grooming, narrative weaving, etc.) while Aiko is
    # speaking the previous reply. Hot-path stays cheap; the workers feel
    # "free" because they hide under TTS playback.
    scheduler_idle_seconds: float = 20.0  # quiet time before idle drain
    scheduler_speaking_window_grace_ms: int = 200  # soft-close grace
    scheduler_max_job_seconds: float = 8.0  # advisory per-job cap

    # ── Inner-life workers (Phase 2c onward) ──────────────────────────
    # ReflectionWorker fires after every turn unless skipped by emotional-delta
    # throttling. Set to a higher number to throttle more aggressively.
    reflection_min_seconds_between: float = 8.0
    reflection_emotional_delta_threshold: float = 0.05
    # User-profile worker runs every N user turns; lowered when each pass is
    # richer (covers all fields per pass).
    user_profile_min_turns: int = 6
    # Agenda groomer runs every N user turns when there are >= 1 agenda items.
    agenda_groom_every_n_turns: int = 8
    # Conversation-arc worker (cheap LLM, runs each turn at low priority).
    arc_update_every_n_turns: int = 1
    # Self-image pulse: once per UTC day in the first speaking window after
    # midnight. ``enabled=False`` skips entirely.
    self_image_pulse_enabled: bool = True
    # Prepared-nudge job runs in late speaking windows; cap how stale a
    # prepared nudge can be before ProactiveDirector re-synthesises.
    prepared_nudge_ttl_seconds: float = 600.0

    # ── Filler injection (Phase 1c) ───────────────────────────────────
    # If the LLM hasn't produced a first stream delta within this many
    # ms, the TurnRunner emits a short filler ("Hmm,", "Let me think,")
    # via TTS so Aiko isn't silent. Set ``filler_enabled`` to false to
    # disable globally.
    filler_enabled: bool = True
    filler_first_token_ms: int = 800

    # ── Memory consolidation (Phase 4b) ───────────────────────────────
    # MemoryConsolidator merges near-cosine clusters in the SQLite store
    # so we don't drown in tiny redundant fact-rows. Runs in chunks during
    # the speaking window so a single pass never exceeds ``chunk_size``
    # memories. ``enabled=false`` short-circuits.
    consolidator_enabled: bool = True
    consolidator_min_hours_between: float = 18.0
    consolidator_chunk_size: int = 40
    consolidator_similarity_threshold: float = 0.84
    consolidator_min_cluster_size: int = 2
    consolidator_use_llm_merge: bool = True

    # Weekly relationship-pulse: a single LLM pass that summarises
    # how the relationship has been going and writes it as a salience-
    # boosted "self_tagged" memory. Runs at most once per ``min_hours``.
    relationship_pulse_enabled: bool = True
    relationship_pulse_min_hours: float = 168.0  # ~7 days
    relationship_pulse_min_turns: int = 30

    # ── Cadence / prosody (Phase 5b) ──────────────────────────────────
    # ProsodyDispatcher inserts per-sentence reactions, occasional micro
    # prefixes ("Mm.", "Oh,") and gentle pause-style punctuation tweaks.
    # All hints are text-only — engines that ignore punctuation are safe.
    cadence_enabled: bool = True

    # ── Resume opener (Phase 2a) ──────────────────────────────────────
    # When the time since the last assistant turn exceeds this many
    # hours, controller bootstrap schedules a one-shot NarrativeWeaver
    # pass that primes a "welcome back" line into PreparedNudgeStore.
    # ProactiveDirector consumes it on first silence; on the typed path
    # the prompt assembler folds it into the system block so the LLM
    # opens naturally. Set to 0 to disable the opener entirely.
    resume_opener_min_hours: float = 4.0
    # TTL applied to the resume nudge so it survives until the user
    # actually starts a session — longer than the speaking-window TTL.
    resume_opener_ttl_seconds: float = 1800.0  # 30 min

    # ── Dream worker (Phase 2b) ───────────────────────────────────────
    # Bootstrap-time reflection that fires once per app start when the
    # gap since the last assistant turn exceeds this threshold. Writes
    # a salience-boosted ``reflection`` memory tagged ``[dream]`` so the
    # resume opener can prefer it. Set ``enabled=false`` to disable.
    dream_worker_enabled: bool = True
    dream_worker_min_hours_since_last: float = 6.0

    # ── Catchphrase miner (Phase 2c) ──────────────────────────────────
    # Walks the recent history and promotes 3-7-word phrases that recur
    # ≥ N times across both user and assistant turns. Surfaced through
    # the "Aiko's running jokes with Jacob:" inner-life block.
    catchphrase_miner_enabled: bool = True
    catchphrase_miner_min_seconds_between: float = 600.0
    catchphrase_miner_min_new_user_turns: int = 6
    catchphrase_miner_min_total_count: int = 3
    # Phase 4c: CuriosityWorker — emits a one-line "next-turn"
    # follow-up question when the recent conversation has gone shallow.
    curiosity_worker_enabled: bool = True
    curiosity_worker_min_turns_between: int = 3
    curiosity_worker_min_seconds_between: float = 60.0
    curiosity_worker_max_user_word_count: int = 8


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
    max_memories: int = 5000
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
    # Aiko's room: small set of tools that let her look around / move /
    # consume cookies. See :mod:`app.llm.tools.world`.
    world: bool = True


@dataclass(slots=True)
class PersonaWindowSettings:
    """Tauri persona-window geometry knobs.

    Persisted in ``config/user.json`` so a window resize survives an app
    restart. Both clamps are enforced on load and again in
    ``SessionController.update_desktop_settings`` so an out-of-range value
    coming from anywhere (config file, REST PATCH) is funneled to the
    nearest valid one rather than crashing.
    """

    width: int = 320
    height: int = 480
    always_on_top: bool = True


@dataclass(slots=True)
class DesktopSettings:
    """Settings only the Tauri desktop shell consumes.

    Browser-only deployments leave these untouched. The frontend reads
    the same values out of the WS ``hello`` snapshot regardless of
    runtime so a browser tab can preview the configured persona size
    without doing anything with it.
    """

    persona_window: PersonaWindowSettings = field(
        default_factory=PersonaWindowSettings
    )


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
    endpointing: EndpointingSettings = field(default_factory=EndpointingSettings)
    avatar: AvatarSettings = field(default_factory=AvatarSettings)
    desktop: DesktopSettings = field(default_factory=DesktopSettings)


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


def read_user_overrides(*, path: Path | None = None) -> dict[str, Any]:
    """Return the deserialised contents of ``user.json``.

    Convenience wrapper around the cached ``_read_config`` so callers
    that need to look up a single user-only key (e.g. the last-active
    session id, which doesn't have a slot on the ``AppSettings``
    dataclass) don't have to import the private helper.
    Missing file → empty dict.
    """
    target = path or USER_CONFIG_PATH
    try:
        return _read_config(target)
    except Exception:
        return {}


def persist_user_overrides(
    patch: dict[str, Any], *, path: Path | None = None
) -> None:
    """Deep-merge ``patch`` into ``user.json`` and write it back atomically.

    Used by callers that mutate user-tunable knobs at runtime (avatar
    scale, outfit, etc.) and need the change to survive an app restart.
    The next ``load_settings`` call sees the new values because we
    invalidate the in-process cache for the touched path.

    The file is created on first write; existing keys outside ``patch``
    are preserved by the deep-merge (so persisting an avatar tweak does
    not clobber the tts/audio overrides the user set in another tab).
    """
    target = path or USER_CONFIG_PATH
    if not isinstance(patch, dict) or not patch:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = _read_config(target)
    except Exception:
        existing = {}
    merged = _deep_merge(existing, patch)
    # Atomic-ish write: stage to a sibling temp and rename so a crash
    # mid-write can't truncate the live file. ``Path.replace`` is atomic
    # on the same volume on Windows + POSIX.
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)
    # Drop the cache entry so the next ``_read_config`` re-reads from
    # disk instead of returning the stale pre-patch dict.
    _config_cache.pop(str(target), None)


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

    keep_alive_raw = payload.get("keep_alive", "30m")
    keep_alive = (
        str(keep_alive_raw).strip()
        if keep_alive_raw not in (None, "")
        else "30m"
    )

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
        keep_alive=keep_alive,
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
    endpointing_raw = raw.get("endpointing", {}) or {}
    avatar_raw = raw.get("avatar", {}) or {}
    desktop_raw = raw.get("desktop", {}) or {}
    persona_window_raw = (desktop_raw.get("persona_window", {}) or {}) if isinstance(desktop_raw, dict) else {}

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
            summary_idle_seconds=max(2.0, float(agent_raw.get("summary_idle_seconds", 15.0))),
            summary_min_unsummarized_messages=max(2, int(agent_raw.get("summary_min_unsummarized_messages", 6))),
            summary_target_tokens=max(120, int(agent_raw.get("summary_target_tokens", 600))),
            max_prompt_tokens_pct=max(0.3, min(0.95, float(agent_raw.get("max_prompt_tokens_pct", 0.8)))),
            scheduler_idle_seconds=max(2.0, float(agent_raw.get("scheduler_idle_seconds", 20.0))),
            scheduler_speaking_window_grace_ms=max(0, int(agent_raw.get("scheduler_speaking_window_grace_ms", 200))),
            scheduler_max_job_seconds=max(1.0, float(agent_raw.get("scheduler_max_job_seconds", 8.0))),
            reflection_min_seconds_between=max(0.0, float(agent_raw.get("reflection_min_seconds_between", 8.0))),
            reflection_emotional_delta_threshold=max(0.0, float(agent_raw.get("reflection_emotional_delta_threshold", 0.05))),
            user_profile_min_turns=max(1, int(agent_raw.get("user_profile_min_turns", 6))),
            agenda_groom_every_n_turns=max(1, int(agent_raw.get("agenda_groom_every_n_turns", 8))),
            arc_update_every_n_turns=max(1, int(agent_raw.get("arc_update_every_n_turns", 1))),
            self_image_pulse_enabled=bool(agent_raw.get("self_image_pulse_enabled", True)),
            prepared_nudge_ttl_seconds=max(30.0, float(agent_raw.get("prepared_nudge_ttl_seconds", 600.0))),
            filler_enabled=bool(agent_raw.get("filler_enabled", True)),
            filler_first_token_ms=max(150, int(agent_raw.get("filler_first_token_ms", 800))),
            consolidator_enabled=bool(agent_raw.get("consolidator_enabled", True)),
            consolidator_min_hours_between=max(0.5, float(agent_raw.get("consolidator_min_hours_between", 18.0))),
            consolidator_chunk_size=max(8, int(agent_raw.get("consolidator_chunk_size", 40))),
            consolidator_similarity_threshold=max(0.5, min(0.99, float(agent_raw.get("consolidator_similarity_threshold", 0.84)))),
            consolidator_min_cluster_size=max(2, int(agent_raw.get("consolidator_min_cluster_size", 2))),
            consolidator_use_llm_merge=bool(agent_raw.get("consolidator_use_llm_merge", True)),
            relationship_pulse_enabled=bool(agent_raw.get("relationship_pulse_enabled", True)),
            relationship_pulse_min_hours=max(24.0, float(agent_raw.get("relationship_pulse_min_hours", 168.0))),
            relationship_pulse_min_turns=max(5, int(agent_raw.get("relationship_pulse_min_turns", 30))),
            cadence_enabled=bool(agent_raw.get("cadence_enabled", True)),
            resume_opener_min_hours=max(0.0, float(agent_raw.get("resume_opener_min_hours", 4.0))),
            resume_opener_ttl_seconds=max(60.0, float(agent_raw.get("resume_opener_ttl_seconds", 1800.0))),
            dream_worker_enabled=bool(agent_raw.get("dream_worker_enabled", True)),
            dream_worker_min_hours_since_last=max(
                0.0, float(agent_raw.get("dream_worker_min_hours_since_last", 6.0)),
            ),
            catchphrase_miner_enabled=bool(agent_raw.get("catchphrase_miner_enabled", True)),
            catchphrase_miner_min_seconds_between=max(
                30.0, float(agent_raw.get("catchphrase_miner_min_seconds_between", 600.0)),
            ),
            catchphrase_miner_min_new_user_turns=max(
                1, int(agent_raw.get("catchphrase_miner_min_new_user_turns", 6)),
            ),
            catchphrase_miner_min_total_count=max(
                2, int(agent_raw.get("catchphrase_miner_min_total_count", 3)),
            ),
            curiosity_worker_enabled=bool(
                agent_raw.get("curiosity_worker_enabled", True),
            ),
            curiosity_worker_min_turns_between=max(
                1, int(agent_raw.get("curiosity_worker_min_turns_between", 3)),
            ),
            curiosity_worker_min_seconds_between=max(
                0.0, float(agent_raw.get("curiosity_worker_min_seconds_between", 60.0)),
            ),
            curiosity_worker_max_user_word_count=max(
                1, int(agent_raw.get("curiosity_worker_max_user_word_count", 8)),
            ),
        ),
        logging=LoggingSettings(
            level=str(logging_raw.get("level", "INFO")).strip().upper() or "INFO",
            module_levels={
                str(name): str(level).strip().upper()
                for name, level in (logging_raw.get("module_levels") or {}).items()
                if name and level
            },
            file_enabled=bool(logging_raw.get("file_enabled", True)),
            file_path=str(logging_raw.get("file_path", "data/app.log") or "data/app.log"),
            file_max_bytes=max(64 * 1024, int(logging_raw.get("file_max_bytes", 5 * 1024 * 1024))),
            file_backup_count=max(0, int(logging_raw.get("file_backup_count", 5))),
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
            max_memories=max(50, int(memory_raw.get("max_memories", 5000))),
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
        endpointing=EndpointingSettings(
            enabled=bool(endpointing_raw.get("enabled", True)),
            use_partial_transcript=bool(
                endpointing_raw.get("use_partial_transcript", True)
            ),
            phrase_silence_seconds=max(
                0.2, float(endpointing_raw.get("phrase_silence_seconds", 1.0))
            ),
            turn_silence_seconds=max(
                0.4, float(endpointing_raw.get("turn_silence_seconds", 3.0))
            ),
            fast_close_silence_seconds=max(
                0.1, float(endpointing_raw.get("fast_close_silence_seconds", 0.6))
            ),
            hesitation_extend_to_turn=bool(
                endpointing_raw.get("hesitation_extend_to_turn", True)
            ),
            barge_in_min_speech_seconds=max(
                0.0, float(endpointing_raw.get("barge_in_min_speech_seconds", 0.7))
            ),
            hesitation_markers=[
                str(x) for x in (endpointing_raw.get("hesitation_markers") or []) if x
            ],
            sentence_final_markers=[
                str(x) for x in (endpointing_raw.get("sentence_final_markers") or []) if x
            ],
        ),
        avatar=AvatarSettings(
            root_dir=str(avatar_raw.get("root_dir", "live-2d-models/Alexia") or "live-2d-models/Alexia").strip(),
            entry_filename=str(avatar_raw.get("entry_filename", "Alexia.model3.json") or "Alexia.model3.json").strip(),
            scale_multiplier=max(0.1, min(8.0, float(avatar_raw.get("scale_multiplier", 1.0) or 1.0))),
            auto_outfit=(
                str(avatar_raw.get("auto_outfit", "auto") or "auto").strip().lower()
                if str(avatar_raw.get("auto_outfit", "auto") or "auto").strip().lower() in OUTFIT_MODES
                else "auto"
            ),
            expressiveness=max(0.0, min(1.5, float(avatar_raw.get("expressiveness", 1.0) or 1.0))),
        ),
        desktop=DesktopSettings(
            persona_window=PersonaWindowSettings(
                width=clamp_persona_window_width(
                    persona_window_raw.get("width", 320)
                ),
                height=clamp_persona_window_height(
                    persona_window_raw.get("height", 480)
                ),
                always_on_top=bool(
                    persona_window_raw.get("always_on_top", True)
                ),
            ),
        ),
    )
