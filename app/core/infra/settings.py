from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any
from app.core.infra.settings_basic import FileWriteSettings, VisionSettings
from app.core.infra.agent_settings import AgentSettings
from app.core.infra.agent_settings_parse import parse_agent_settings
from app.core.infra.memory_settings import MemorySettings, parse_memory_settings


log = logging.getLogger("app.settings")


@dataclass(slots=True)
class OllamaSettings:
    base_url: str
    chat_model: str
    temperature: float
    embedding_base_url: str = ""  # empty = use base_url
    context_window: int | None = None  # None = auto-detect from Ollama API
    embedding_model: str = "qwen3-embedding:0.6b"
    timeout: int = 300  # HTTP timeout in seconds (shared by all Ollama clients)
    # GPU offload for the embedding model. ``None`` (default) leaves
    # Ollama's own placement untouched; ``0`` forces the embedder onto
    # CPU (passed as ``options.num_gpu=0`` on every /api/embeddings
    # call), freeing the ~5.5 GB the small embed model otherwise pins
    # in VRAM for the chat/worker model. The embedder is used almost
    # entirely by latency-tolerant background workers, so CPU is a fine
    # trade for the freed headroom. A positive value pins that many
    # layers to GPU.
    embedding_num_gpu: int | None = None
    # Context window the embedding model is loaded with (passed as
    # ``options.num_ctx`` on every /api/embeddings call). ``None``
    # (default) leaves Ollama's model default -- which for
    # ``qwen3-embedding`` is a 32k window that allocates a large KV
    # buffer and bloats the resident model to ~5.8 GB. Aiko only ever
    # embeds short texts (document chunks cap at ~1k chars / ~250
    # tokens, memories are shorter), so a small window like ``2048`` is
    # ample and shrinks the embedder's footprint dramatically -- handy
    # when offloading it to CPU (``embedding_num_gpu=0``) or just to
    # reclaim VRAM. Texts longer than the window are truncated by
    # Ollama, so keep it comfortably above the largest chunk.
    embedding_num_ctx: int | None = None
    # Extra ``num_predict`` budget the client adds automatically whenever
    # a call is made with ``think=True``. The historical caps every
    # worker passes as ``num_predict`` were sized for the ANSWER ONLY
    # (the text/JSON we parse) — with a reasoning model the trace shares
    # that same budget and would starve the answer. Rather than re-tune
    # every worker's cap, the client treats ``num_predict`` as the answer
    # budget and adds this headroom on top for the hidden trace. A 27B
    # model typically reasons within ~1-2k tokens; 2048 is a safe default.
    # Set to 0 to disable the auto-bump (then ``num_predict`` is the hard
    # total again, thinking included).
    think_num_predict_headroom: int = 2048


@dataclass(slots=True)
class ChatLlmSettings:
    """Chat-LLM provider routing layer.

    Sits in front of :class:`OllamaSettings`. When ``provider == "ollama"`` and
    ``base_url``/``model``/``api_key`` are blank the legacy local Ollama chat
    behaviour is preserved unchanged. Setting ``base_url`` to ``https://ollama.com``
    plus an ``api_key`` flips the same code path to Ollama Cloud Pro. The
    ``openai_compatible`` provider routes through
    :class:`app.llm.openai_compatible_client.OpenAICompatibleClient`
    and covers OpenAI / Google Gemini / xAI Grok / Groq / OpenRouter /
    DeepSeek / Together / Mistral via custom ``base_url``.
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
    # UI hint emitted by the curated preset picker. One of
    # ``""`` (unspecified / Custom), ``"ollama"``, ``"ollama_cloud"``,
    # ``"openai"``, ``"gemini"``, ``"groq"``, ``"openrouter"``. The
    # value is round-tripped to the React drawer so it can highlight
    # the active preset card; the controller does not read it.
    provider_preset: str = ""
    # When the chat provider is NOT Ollama and this is True, background
    # workers (reflection, dream, belief, memory extractor, ...) keep
    # talking to a local Ollama instance even though the main chat path
    # goes through the remote provider. Why True by default? Free-tier
    # remote quotas (Gemini = 1500 req/day) drain fast when the ~25
    # background workers each fire a few requests per hour. Workers
    # don't need a frontier model; routing them locally keeps the
    # remote quota for user-visible turns. Set False to opt workers
    # into the same provider — burns quota; useful when running
    # without a local Ollama at all.
    workers_use_local: bool = True
    # Reasoning-effort hint for OpenAI Responses-API-family models
    # (GPT-5 / o-series). Empty string = "auto": the client keeps its
    # built-in default (``minimal``). Providers disagree on the allowed
    # vocabulary — OpenAI gpt-5-mini takes ``minimal``; gpt-5.4-mini
    # rejects ``minimal`` and wants one of ``none`` / ``low`` / ``medium``
    # / ``high`` / ``xhigh`` — so this is free-text, sent verbatim only
    # for Responses-API models and ignored everywhere else.
    reasoning_effort: str = ""


@dataclass(slots=True)
class LlmProvider:
    """One entry in the provider catalogue.

    Catalogue-mode: credentials live here (one per provider), and
    :class:`LlmRoute` rows pick which catalogue entry serves which
    role. Multiple roles pointing at the same provider share one
    underlying :class:`ChatClient` instance (the cache key is
    ``(kind, base_url, resolved_api_key)``).

    Migrated from the legacy :class:`ChatLlmSettings` + :class:`OllamaSettings`
    blocks by :func:`_migrate_legacy_llm`. See ``docs/llm-providers.md``
    for the user-facing model.
    """

    id: str  # stable identifier (``"local_ollama"``, ``"openai"``, ``"custom_3"``…)
    name: str  # human-friendly label for the UI
    kind: str  # ``"ollama"`` | ``"openai_compatible"``
    base_url: str
    api_key: str = ""
    api_key_env: str = ""  # explicit env-var fallback; empty = inferred per host
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 300
    keep_alive: str = "30m"  # Ollama-only; ignored by openai_compatible
    # Provider-level reasoning-effort default (see ChatLlmSettings).
    # A route's own ``reasoning_effort`` overrides this when set.
    reasoning_effort: str = ""


@dataclass(slots=True)
class LlmRoute:
    """One row in the role-assignment table.

    Each role (``"main_chat"``, ``"worker_default"``, future
    ``"heavy_workers"``…) picks a provider from the catalogue and
    specifies the per-role model + budget. ``context_window`` /
    ``temperature`` of ``None`` mean "let the client decide" — the
    OpenAI-compat lookup table, Ollama's ``/api/show``, or the
    inherited ``OllamaSettings.temperature``.
    """

    provider_id: str  # references ``LlmProvider.id``
    model: str
    context_window: int | None = None
    max_tokens: int = 512
    temperature: float | None = None
    # Per-route reasoning-effort override (see ChatLlmSettings). Empty =
    # inherit the provider-level value, then the client default.
    reasoning_effort: str = ""


@dataclass(slots=True)
class LlmSettings:
    """Top-level container for the provider catalogue + role table.

    Lives on :class:`AppSettings`. When ``providers`` is empty at boot
    (first run after upgrade), :func:`_migrate_legacy_llm` synthesises
    one entry from each legacy block and wires the default routes.
    """

    providers: list[LlmProvider] = field(default_factory=list)
    routes: dict[str, LlmRoute] = field(default_factory=dict)


# Canonical role names. New roles can be added (Phase 3:
# ``"heavy_workers"``) without a schema migration; these are the two
# guaranteed-present roles after legacy migration.
LLM_ROLE_MAIN_CHAT = "main_chat"
LLM_ROLE_WORKER_DEFAULT = "worker_default"
# Background nested-workflow planner + skills. Mirrors
# ``worker_default`` by default (same provider/model/context) so it
# shares the single local worker Ollama instance -- zero extra VRAM.
# Only diverges when a user deliberately repoints it at a remote /
# bigger-context provider where VRAM is not the constraint.
LLM_ROLE_WORKFLOW = "workflow"


@dataclass(slots=True)
class AudioSettings:
    """Server-side audio knobs.

    The browser / Tauri client now owns mic capture and TTS playback, so
    device pickers and PTT bindings moved off the server. What remains
    are the parameters the server actually uses on the audio it
    receives: ``sample_rate`` / ``channels`` describe the format the
    STT / VAD pipeline expects (the client resamples to this rate),
    ``vad_*`` knobs drive endpointing on the decoded stream, and
    ``barge_in_enabled`` / ``earcons_enabled`` are user-facing toggles.

    A one-shot migration in :func:`load_settings` strips the legacy
    ``microphone_device``, ``output_device`` and ``live_ptt_*`` keys
    from ``user.json`` if they're still there, so an upgrade doesn't
    crash on stale config.
    """

    sample_rate: int
    channels: int
    enable_microphone: bool
    vad_level_threshold: float
    vad_silence_seconds: float
    barge_in_enabled: bool = False
    earcons_enabled: bool = True


@dataclass(slots=True)
class AssistantSettings:
    name: str
    remember_history: bool
    user_id: str = "default"  # Scopes memory per user
    user_display_name: str = ""  # Empty signals first-run onboarding required
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
    # UI debug log bridge — when ``ui_log_enabled`` is true the browser
    # POSTs structured events (WS dispatch, avatar channel decisions,
    # settings changes) to ``/api/logs/ui`` which interleaves them into
    # ``data/app.log`` with a ``[ui]`` prefix. Off by default; flip via
    # the Settings drawer "Debug logging" toggle when reproducing a bug.
    # ``ui_log_categories`` is the allow-list the endpoint enforces on
    # incoming ``source`` values so a misbehaving client can't spam
    # arbitrary lines; ``ui_log_max_batch`` caps the entries per request;
    # ``ui_log_max_payload_bytes`` truncates oversized payloads before
    # they hit the rotating log.
    ui_log_enabled: bool = False
    ui_log_categories: list[str] = field(
        default_factory=lambda: ["ws", "channel", "settings", "voice", "audio"],
    )
    ui_log_max_batch: int = 50
    ui_log_max_payload_bytes: int = 2048


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


# Phase 4 (expression overhaul): allowed values for the
# ``eye_color`` accessory enum. Toggle-style accessories (lollipop,
# eyeglasses, head_sunglasses, crossed_arms) accept plain booleans;
# ``eye_color`` is the one accessory with multiple discrete states
# so it gets a dedicated allow-list. Keep this in lock-step with the
# matching TS type literal in ``web/src/types.ts``
# (``EyeColorState``).
EYE_COLOR_STATES: frozenset[str] = frozenset({
    "default",
    "both_purple",
    "left_purple",
    "right_purple",
})


# Known accessory keys we accept in ``AvatarSettings.accessory_state``.
# Each entry is ``(key, value_kind)`` where ``value_kind`` is
# ``"bool"`` for on/off toggles or ``"enum"`` for fixed-vocabulary
# strings. Unknown keys are silently dropped at load time so a
# downgrade doesn't accidentally promote stale junk into the
# accessory namespace; new accessories land here first.
ACCESSORY_KEYS: dict[str, str] = {
    "lollipop": "bool",
    "eyeglasses": "bool",
    "head_sunglasses": "bool",
    "crossed_arms": "bool",
    "eye_color": "enum",
}


def _load_accessory_state(raw: Any) -> dict[str, str | bool]:
    """Validate and normalise the ``accessory_state`` payload.

    Boolean accessories accept any truthy / falsy value (``true`` /
    ``false`` / ``1`` / ``0`` / ``"on"`` / ``"off"`` etc.). Enum
    accessories are checked against :data:`EYE_COLOR_STATES`; an
    unrecognised value falls back to the enum's canonical default
    (``"default"`` for ``eye_color``). Unknown keys are dropped.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str | bool] = {}
    for key, kind in ACCESSORY_KEYS.items():
        if key not in raw:
            continue
        value = raw[key]
        if kind == "bool":
            if isinstance(value, bool):
                out[key] = value
            elif isinstance(value, (int, float)):
                out[key] = bool(value)
            elif isinstance(value, str):
                lowered = value.strip().lower()
                out[key] = lowered in {"1", "true", "yes", "on"}
            else:
                continue
        elif kind == "enum":
            if key == "eye_color":
                token = str(value).strip().lower() if value is not None else ""
                out[key] = token if token in EYE_COLOR_STATES else "default"
    return out


# ── Identity (first-run onboarding) ─────────────────────────────────────

# Sentinel used when no display name is configured yet. We intentionally
# keep this generic so a stray render before onboarding completes never
# leaks a developer-specific name. Onboarding gates the UI so users
# normally never see it.
USER_DISPLAY_NAME_FALLBACK = "friend"


def resolve_user_display_name(settings: "AppSettings") -> str:
    """Return the configured user display name, or a safe fallback.

    Single source of truth for the human user's name. All renderers,
    transcript formatters, and worker LLM prompts route through this so
    a rename via onboarding / settings ripples everywhere without each
    call site doing its own ``or "friend"`` dance.
    """
    name = (getattr(settings.assistant, "user_display_name", "") or "").strip()
    return name or USER_DISPLAY_NAME_FALLBACK


def is_onboarding_needed(settings: "AppSettings") -> bool:
    """True when no user_display_name has been configured yet.

    Drives the first-run modal in the frontend.
    """
    return not (getattr(settings.assistant, "user_display_name", "") or "").strip()


@dataclass(slots=True)
class AvatarSettings:
    """Single bundled Live2D avatar (Alexia) + user-tunable knobs.

    The avatar files themselves are gitignored at ``root_dir``.
    ``scale_multiplier``, ``auto_outfit`` and ``expressiveness`` are
    the fields the UI lets the user change at runtime.
    """

    root_dir: str = "data/personas/active/Alexia"
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
    # K45 mood inertia: when true, ExpressionChannel damps non-mouth
    # expression params proportionally to the gap between the fresh
    # reaction tag's implied affect and the smoothed mood — so the
    # face carries the residue too. Mouth params (lipsync ids +
    # mouth-overlay grin) are NEVER damped: lipsync and the grin
    # taper keep owning the mouth while she talks.
    mood_inertia_damping: bool = True
    # Phase 4 (expression overhaul): persistent accessory toggles.
    # Each key is an accessory capability name from the loaded rig
    # (``lollipop`` / ``eyeglasses`` / ``head_sunglasses`` /
    # ``eye_color`` / ``crossed_arms`` for Alexia). Boolean values
    # are toggles; string values pick from a fixed enum
    # (``eye_color``: ``default`` / ``both_purple`` / ``left_purple``
    # / ``right_purple``). Missing keys default to off — additive in
    # the schema so a rollback to a pre-Phase-4 build silently drops
    # the field without breaking persisted configs.
    accessory_state: dict[str, str | bool] = field(default_factory=dict)


@dataclass(slots=True)
class McpServerSettings:
    enabled: bool = True
    port: int = 6274


@dataclass(slots=True)
class ExternalMcpServer:
    """One configured EXTERNAL MCP server the app connects to as a client.

    Distinct from :class:`McpServerSettings` (which is the embedded MCP
    server the app *exposes* for debugging). These rows describe servers
    the app launches/connects to in order to *consume* their tools, which
    are surfaced only to the background-worker (workflow planner) lane.

    ``transport``:
      * ``"stdio"`` — launch ``command`` + ``args`` as a child process and
        speak MCP over its stdin/stdout (the common case: ``npx -y
        @modelcontextprotocol/server-filesystem <dir>``).
      * ``"sse"`` — connect to an already-running server at ``url``.

    ``env`` values support ``${ENV:NAME}`` indirection, resolved from the
    process environment at launch (so a token can live in an env var
    instead of in ``config/user.json``).

    Two complementary tool filters (applied in
    :meth:`ExternalMcpManager._refresh_tools`):
      * ``expose_tools`` — optional *allow-list*: when non-empty, ONLY
        these tool names are registered for the planner. Empty = expose
        every tool the server advertises.
      * ``disabled_tools`` — optional *deny-list*: tool names to drop
        even when they pass the allow-list. Convenient for hiding a few
        unwanted tools (e.g. a browser server's debug group) without
        enumerating every tool you want to keep. Applied after the
        allow-list, so a name in both is dropped.
    """

    id: str
    name: str
    transport: str = "stdio"
    command: str = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    enabled: bool = True
    autostart: bool = True
    timeout_seconds: float = 30.0
    expose_tools: tuple[str, ...] = ()
    disabled_tools: tuple[str, ...] = ()


@dataclass(slots=True)
class ExternalMcpSettings:
    """Catalogue of external MCP servers to connect to as a client."""

    servers: list[ExternalMcpServer] = field(default_factory=list)


@dataclass(slots=True)
class BrowserPerceptionSettings:
    """Server-agnostic "browser perception layer" over an MCP browser server.

    When ``enabled``, the result of a configured accessibility-snapshot
    tool (``snapshot_tools`` on the server identified by ``server_id``) is
    parsed by the named ``adapter`` into a normalized accessibility tree,
    then deduped / form-grouped / heading-context-injected / heuristically
    ranked / diffed against the previous page state, and re-rendered as a
    compact, ranked block for the workflow planner. The MCP server stays a
    swappable transport: switching servers means adding an
    ``mcp_clients.servers`` entry and pointing ``server_id`` / ``adapter``
    at it — the perception pipeline is unchanged.

    Ranking is purely heuristic (no embeddings). ``weight_*`` knobs tune
    the per-element ``interaction_likelihood`` score. ``state_memory_pages``
    bounds the in-process (ephemeral) previous-page-state LRU.
    """

    enabled: bool = False
    server_id: str = "browser"
    snapshot_tools: tuple[str, ...] = ("browser_snapshot",)
    adapter: str = "real_browser"
    max_ranked_elements: int = 40
    state_memory_pages: int = 8
    weight_role: float = 1.0
    weight_visibility: float = 1.0
    weight_position: float = 1.0
    weight_text: float = 1.0
    weight_context: float = 1.0


@dataclass(slots=True)
class WebServerSettings:
    """FastAPI/WebSocket layer that serves the React UI."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 6275


@dataclass(slots=True)
class ToolsSettings:
    """Lean v1 tool-calling configuration.

    Tools are dispatched in :class:`app.core.session.turn_runner.TurnRunner` via a
    pre-stream ``chat_with_tools`` pass. Each switch below toggles a single
    tool; setting ``enabled=False`` disables the whole tool registry.
    """

    enabled: bool = True
    get_time: bool = True
    recall: bool = True
    # F10d cluster-scoped recall (``recall_topic``): browse a whole topic
    # cluster of the memory graph rather than the few closest snippets.
    # Needs the topic graph wired (no-ops to an empty result otherwise).
    recall_topic: bool = True
    web_search: bool = True
    # Aiko's room: small set of tools that let her look around / move /
    # consume cookies. See :mod:`app.llm.tools.world`.
    world: bool = True
    # K1 long-term goals: ``add_goal`` / ``update_goal_progress`` /
    # ``archive_goal`` / ``list_goals``. Independent from
    # ``agent.goals_enabled``: flipping the master switch ``False`` skips
    # store + worker + prompt block but leaves the tool registry path
    # untouched (the tools themselves no-op because the store is unset).
    # See :mod:`app.llm.tools.goals`.
    goals: bool = True
    # Brain-orchestration filesystem tools (chunk 10):
    # ``start_file_search`` / ``cancel_file_task``. Returns a task
    # id immediately so Aiko can say "I'm searching for that, I'll
    # let you know" while the orchestrator walks the configured
    # roots in the background. Gated independently from
    # ``agent.tasks_enabled`` so a developer can keep the
    # subsystem on but hide the tools from the LLM during prompt
    # experiments. See :mod:`app.llm.tools.file_tasks`.
    file_tasks: bool = True
    # Nested goal workflows (``start_workflow`` / ``check_my_work`` /
    # ``cancel_work``). The brain-facing control surface for the
    # background ``GoalWorkflowHandler``. Gated independently from
    # ``agent.workflow_enabled`` (which owns the handler itself) so the
    # tools can be hidden during prompt experiments. See
    # :mod:`app.llm.tools.workflow_tools`.
    workflow: bool = True
    # Synchronous exact-arithmetic tool (``calculate``). Evaluates an
    # expression via an AST whitelist (no ``eval``) and returns the
    # result in the same turn so Aiko never has to guess a number. See
    # :mod:`app.llm.tools.calc`.
    calculate: bool = True
    # H11: synchronous weather tools (``get_weather`` / ``get_forecast``).
    # Lets Aiko answer "what's the forecast?" for the configured home
    # location or any named city (geocoded at call time). Independent of
    # the passive ambient ``agent.weather_sync_enabled`` feed -- the tools
    # work even with the ambient overlay off. See :mod:`app.llm.tools.weather`.
    weather: bool = True


@dataclass(slots=True)
class SearchSettings:
    """Web-search backend configuration.

    Aiko's background workers (F1 fact-checker, G3 curiosity, F9
    knowledge) and the goal-workflow ``web_search`` lane share one
    pluggable provider built from this block (see
    :func:`app.llm.search.build_search_provider`). DuckDuckGo is the
    keyless default; LangSearch is used when ``provider == "langsearch"``
    and an API key resolves (explicit ``api_key`` or the ``api_key_env``
    environment variable). ``api_key`` is masked in the REST snapshot
    (``has_api_key``) and only written via the dedicated credential path.
    """

    provider: str = "duckduckgo"  # ``"duckduckgo"`` | ``"langsearch"``
    api_key: str = ""  # write-only; masked in REST as ``has_api_key``
    api_key_env: str = "LANGSEARCH_API_KEY"
    # LangSearch request knobs (ignored by the DuckDuckGo path).
    langsearch_summary: bool = True
    langsearch_freshness: str = "noLimit"
    langsearch_count: int = 10
    fallback_to_duckduckgo: bool = True
    timeout_seconds: float = 12.0
    # LangSearch enforces a ~1 request/second API limit. This is the
    # minimum wall-clock spacing the provider keeps between consecutive
    # LangSearch requests process-wide (across every background worker +
    # the brain's web_search tool), so a burst of queued topics can't trip
    # the limit. Set to 0 to disable the throttle.
    langsearch_min_interval_seconds: float = 1.1
    # F6: rewrite a personal claim into a neutral, name-free topic query
    # with the local worker model before searching (post-filtered by the
    # deterministic scrubber). Master switch for the whole reformulation
    # step; when off the workers fall back to the deterministic scrub.
    query_reformulation_enabled: bool = True


@dataclass(slots=True)
class WeatherSettings:
    """H11 real-world co-location: weather + season sync configuration.

    Drives both the passive ambient feed (the
    :class:`~app.core.world.weather_worker.WeatherWorker` fetches the
    home sky on a cadence -> prompt cue + persona overlay + optional K27
    nudge / seasonal decor) and the on-demand brain tools
    (:mod:`app.llm.tools.weather`). The master on/off switch is
    ``agent.weather_sync_enabled`` (ambient feed) / ``tools.weather``
    (brain tools); this block holds the location + backend knobs.

    Privacy posture: coarse, consent-gated location only -- a manually
    entered city name (geocoded once to ``latitude`` / ``longitude``) or
    hand-set coordinates. Never GPS, never an address. Off by default.
    See :func:`app.llm.weather.build_weather_provider`. The weather and
    geocoding backends are deliberately independent (``provider`` vs
    ``geocoder``) so swapping one never breaks the other.
    """

    provider: str = "open_meteo"  # weather backend (lat/lon only)
    geocoder: str = "open_meteo"  # place-name -> lat/lon (decoupled)
    # Human label of the configured home location (city granularity).
    location_name: str = ""
    # Cached coordinates resolved from ``location_name`` once at save time.
    # ``None`` means "not yet resolved" -> the ambient feed stays silent.
    latitude: float | None = None
    longitude: float | None = None
    # ``"metric"`` (Celsius / km-h) or ``"imperial"`` (Fahrenheit / mph).
    units: str = "metric"
    # Minutes between ambient fetches. Clamped to >= 15 so the keyless
    # Open-Meteo endpoint is never hammered; raising it makes the shared
    # sky update less often (lower API traffic), lowering it refreshes
    # sooner. The brain tools are on-demand and ignore this.
    refresh_interval_minutes: int = 30
    # For a future keyed backend; masked in REST as ``has_api_key``.
    api_key: str = ""
    api_key_env: str = "WEATHER_API_KEY"
    timeout_seconds: float = 10.0


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
    # External MCP servers the app connects to as a client (their tools
    # are surfaced only to the background-worker lane). Distinct from
    # ``mcp_server`` (the embedded debug server the app exposes).
    mcp_clients: ExternalMcpSettings = field(default_factory=ExternalMcpSettings)
    # Optional middleware over an MCP browser server's accessibility
    # snapshot (parse -> dedup -> group -> rank -> diff). Disabled by
    # default; the underlying browser server is configured under
    # ``mcp_clients.servers``.
    browser_perception: BrowserPerceptionSettings = field(
        default_factory=BrowserPerceptionSettings
    )
    web_server: WebServerSettings = field(default_factory=WebServerSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    chat_llm: ChatLlmSettings = field(default_factory=ChatLlmSettings)
    # PR 2: provider catalogue + role-assignment table. Populated by
    # ``_migrate_legacy_llm`` when ``llm.providers`` is missing/empty.
    # Coexists with the legacy ``chat_llm`` + ``ollama`` blocks; those
    # are still readable and writable via back-compat shims so a
    # downgrade still boots.
    llm: LlmSettings = field(default_factory=LlmSettings)
    tools: ToolsSettings = field(default_factory=ToolsSettings)
    search: SearchSettings = field(default_factory=SearchSettings)
    # H11 real-world co-location (weather + season sync).
    weather: WeatherSettings = field(default_factory=WeatherSettings)
    endpointing: EndpointingSettings = field(default_factory=EndpointingSettings)
    avatar: AvatarSettings = field(default_factory=AvatarSettings)


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "default.json"
USER_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "user.json"


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


# Cache signature is ``(st_mtime_ns, st_size)``: nanosecond mtime
# resolution plus a size discriminator. The old float-seconds
# ``st_mtime`` key collided whenever the same path was rewritten
# within one coarse mtime tick (notably in tests that rewrite a temp
# config repeatedly), returning a stale parse. ``st_mtime_ns`` gives
# far finer granularity and ``st_size`` catches the residual
# same-tick-different-content case (distinct config values almost
# always change the serialised byte length).
_config_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}


def _read_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    key = str(path)
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:
        sig = (0, 0)
    cached = _config_cache.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    raw = json.loads(path.read_text(encoding="utf-8"))
    result = raw if isinstance(raw, dict) else {}
    _config_cache[key] = (sig, result)
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


_GROUNDING_LINE_MODES: frozenset[str] = frozenset({"off", "replace", "split"})


def _parse_grounding_line_mode(value: Any) -> str:
    """Clamp ``agent.grounding_line_mode`` to the K16 mode set.

    Accepts ``"off"`` / ``"replace"`` / ``"split"`` (case-insensitive,
    whitespace-stripped). Anything else falls back to ``"off"`` with a
    debug log so a typo in the config never wedges the prompt. See the
    full mode table on
    :attr:`AgentSettings.grounding_line_mode`.
    """
    raw = str(value if value is not None else "off").strip().lower()
    if raw in _GROUNDING_LINE_MODES:
        return raw
    log.debug(
        "settings: invalid agent.grounding_line_mode=%r; falling back to 'off'",
        value,
    )
    return "off"


def _parse_task_file_allowed_roots(value: Any) -> tuple[dict[str, Any], ...]:
    """Normalise ``agent.task_file_allowed_roots`` into a tuple of dicts.

    Each entry must be a dict with at least ``label`` (non-empty
    string) + ``path`` (non-empty string). Optional ``read_only``
    defaults to ``True`` (phase 1 is read-only; the flag is plumbed
    for phase 2). Malformed entries are dropped with a debug log so
    a stray dict in user config never crashes the parser — boot-time
    validation in :func:`app.core.tasks.sandbox.validate_roots` is
    what surfaces the WARNING for bad paths. We deliberately *don't*
    validate ``label`` characters here (the sandbox does that
    consistently across boot + runtime config updates).

    Returns a tuple so the dataclass field is hashable, matching the
    convention used elsewhere in :class:`AgentSettings`.
    """
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        log.debug(
            "settings: agent.task_file_allowed_roots ignored "
            "(not a list/tuple): %r",
            type(value).__name__,
        )
        return ()
    out: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            log.debug(
                "settings: agent.task_file_allowed_roots entry skipped "
                "(not a dict): %r",
                entry,
            )
            continue
        label = entry.get("label", "")
        path = entry.get("path", "")
        if not isinstance(label, str) or not label.strip():
            log.debug(
                "settings: agent.task_file_allowed_roots entry skipped "
                "(missing/empty label): %r",
                entry,
            )
            continue
        if not isinstance(path, str) or not path.strip():
            log.debug(
                "settings: agent.task_file_allowed_roots entry skipped "
                "(missing/empty path): %r",
                entry,
            )
            continue
        out.append(
            {
                "label": label.strip(),
                "path": path.strip(),
                "read_only": bool(entry.get("read_only", True)),
            }
        )
    return tuple(out)


def _parse_extension_list(value: Any) -> tuple[str, ...]:
    """Normalise an extension allow-list into a tuple of ``.ext`` strings.

    Accepts any iterable of strings. Strings are lowercased, stripped,
    and prefixed with ``.`` if missing. An empty string or non-string
    is silently dropped. Returns a tuple so the dataclass field stays
    hashable. An empty input tuple is the documented sentinel for
    "allow everything that passes the magic-byte text check".
    """
    if value is None:
        return ()
    if isinstance(value, str):
        # A bare string is ambiguous; treat as a single extension to
        # be forgiving toward config typos.
        value = [value]
    if not isinstance(value, (list, tuple)):
        log.debug(
            "settings: extension list ignored (not iterable): %r",
            type(value).__name__,
        )
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, str):
            continue
        ext = entry.strip().lower()
        if not ext:
            continue
        if not ext.startswith("."):
            ext = "." + ext
        if ext in seen:
            continue
        seen.add(ext)
        out.append(ext)
    return tuple(out)


_APPROVAL_MODES: frozenset[str] = frozenset({"ask", "auto"})


def _normalize_approval_mode(value: Any, *, default: str = "ask") -> str:
    """Clamp an approval mode to ``ask`` / ``auto`` (fallback ``ask``)."""
    raw = str(value if value is not None else default).strip().lower()
    if raw in _APPROVAL_MODES:
        return raw
    log.debug(
        "settings: invalid approval mode=%r; falling back to %r",
        value,
        default,
    )
    return default


def _parse_approval_overrides(value: Any) -> dict[str, str]:
    """Normalise ``agent.task_approval_overrides`` into ``{cap: mode}``.

    Keys are capability ids (free-text strings); values are clamped to
    ``ask`` / ``auto``. Non-dict input -> empty map. A value that isn't
    a valid mode is dropped (not coerced) so a typo doesn't silently
    flip a capability to ``auto``.
    """
    if not isinstance(value, dict):
        if value not in (None, {}):
            log.debug(
                "settings: agent.task_approval_overrides ignored "
                "(not a dict): %r",
                type(value).__name__,
            )
        return {}
    out: dict[str, str] = {}
    for key, mode in value.items():
        cap_id = str(key).strip()
        if not cap_id:
            continue
        raw = str(mode or "").strip().lower()
        if raw not in _APPROVAL_MODES:
            log.debug(
                "settings: task_approval_overrides[%r] dropped "
                "(invalid mode %r)",
                cap_id,
                mode,
            )
            continue
        out[cap_id] = raw
    return out


def _parse_file_write_settings(value: Any) -> FileWriteSettings:
    """Build a :class:`FileWriteSettings` from the raw ``file_write`` block.

    Missing / non-dict input yields the defaults (disabled). ``max_bytes``
    is clamped to ``[1 KiB, 16 MiB]``; ``allowed_extensions`` reuses
    :func:`_parse_extension_list` (empty = allow all).
    """
    raw = value if isinstance(value, dict) else {}
    defaults = FileWriteSettings()
    if "allowed_extensions" in raw:
        extensions = _parse_extension_list(raw.get("allowed_extensions"))
    else:
        extensions = defaults.allowed_extensions
    try:
        max_bytes = int(raw.get("max_bytes", defaults.max_bytes))
    except (TypeError, ValueError):
        max_bytes = defaults.max_bytes
    max_bytes = max(1024, min(16 * 1024 * 1024, max_bytes))
    return FileWriteSettings(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        max_bytes=max_bytes,
        allowed_extensions=extensions,
    )


def _parse_vision_settings(value: Any) -> VisionSettings:
    """Build a :class:`VisionSettings` from the raw ``vision`` block.

    Missing / non-dict input yields the defaults (disabled). ``max_bytes``
    is clamped to ``[1 KiB, 64 MiB]``; ``timeout_seconds`` floors at 5s;
    ``allowed_extensions`` reuses :func:`_parse_extension_list` (empty =
    allow all); ``model`` / ``default_prompt`` are trimmed strings.
    """
    raw = value if isinstance(value, dict) else {}
    defaults = VisionSettings()
    if "allowed_extensions" in raw:
        extensions = _parse_extension_list(raw.get("allowed_extensions"))
    else:
        extensions = defaults.allowed_extensions
    try:
        max_bytes = int(raw.get("max_bytes", defaults.max_bytes))
    except (TypeError, ValueError):
        max_bytes = defaults.max_bytes
    max_bytes = max(1024, min(64 * 1024 * 1024, max_bytes))
    try:
        timeout_seconds = int(raw.get("timeout_seconds", defaults.timeout_seconds))
    except (TypeError, ValueError):
        timeout_seconds = defaults.timeout_seconds
    timeout_seconds = max(5, timeout_seconds)
    model = str(raw.get("model", defaults.model) or "").strip()
    default_prompt = str(
        raw.get("default_prompt", defaults.default_prompt) or ""
    ).strip() or defaults.default_prompt
    return VisionSettings(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        model=model,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
        allowed_extensions=extensions,
        default_prompt=default_prompt,
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

    keep_alive_raw = payload.get("keep_alive", "30m")
    keep_alive = (
        str(keep_alive_raw).strip()
        if keep_alive_raw not in (None, "")
        else "30m"
    )

    # ``provider_preset`` is a UI hint only — the controller ignores it.
    # We still clamp to the known preset names (plus "") so a typo from
    # the React drawer can't confuse the round-trip.
    preset_raw = str(
        payload.get("provider_preset", "") or "",
    ).strip().lower()
    _KNOWN_PRESETS: frozenset[str] = frozenset({
        "", "ollama", "ollama_cloud", "openai", "gemini",
        "groq", "openrouter",
    })
    if preset_raw not in _KNOWN_PRESETS:
        preset_raw = ""

    workers_use_local_raw = payload.get("workers_use_local", True)
    workers_use_local = bool(workers_use_local_raw)

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
        provider_preset=preset_raw,
        workers_use_local=workers_use_local,
        reasoning_effort=_norm_reasoning_effort(
            payload.get("reasoning_effort")
        ),
    )


# PR 2: provider catalogue + role-assignment parsers + legacy migration.


def _norm_reasoning_effort(raw: Any) -> str:
    """Normalise a reasoning-effort hint to a trimmed lowercase string.

    Free-text on purpose: providers disagree on the allowed vocabulary
    (``minimal`` / ``none`` / ``low`` / ``medium`` / ``high`` / ``xhigh``
    and counting), so we don't restrict it — empty means "auto" (let the
    client keep its built-in default)."""
    if raw is None:
        return ""
    return str(raw).strip().lower()


def _parse_llm_provider(payload: dict[str, Any]) -> LlmProvider | None:
    """Validate one entry from ``llm.providers``.

    Returns ``None`` when the entry is malformed (missing id, unknown
    kind, etc.) so callers can drop it without aborting the whole
    load. Trimming + lowercasing matches the legacy ``_parse_chat_llm``
    contract exactly.
    """
    if not isinstance(payload, dict):
        return None
    provider_id = str(payload.get("id", "") or "").strip()
    if not provider_id:
        return None
    kind = str(payload.get("kind", "") or "").strip().lower()
    if kind not in {"ollama", "openai_compatible"}:
        return None
    name = str(payload.get("name", "") or "").strip() or provider_id
    base_url = str(payload.get("base_url", "") or "").strip()
    headers_raw = payload.get("extra_headers") or {}
    if isinstance(headers_raw, dict):
        extra_headers = {
            str(k).strip(): str(v).strip()
            for k, v in headers_raw.items()
            if str(k).strip() and v is not None
        }
    else:
        extra_headers = {}
    timeout_raw = payload.get("timeout_seconds", 300)
    try:
        timeout_seconds = max(1, int(timeout_raw))
    except (TypeError, ValueError):
        timeout_seconds = 300
    return LlmProvider(
        id=provider_id,
        name=name,
        kind=kind,
        base_url=base_url,
        api_key=str(payload.get("api_key", "") or "").strip(),
        api_key_env=str(payload.get("api_key_env", "") or "").strip(),
        extra_headers=extra_headers,
        timeout_seconds=timeout_seconds,
        keep_alive=str(payload.get("keep_alive", "30m") or "30m").strip() or "30m",
        reasoning_effort=_norm_reasoning_effort(
            payload.get("reasoning_effort")
        ),
    )


def _parse_llm_route(payload: dict[str, Any]) -> LlmRoute | None:
    """Validate one entry from ``llm.routes``."""
    if not isinstance(payload, dict):
        return None
    provider_id = str(payload.get("provider_id", "") or "").strip()
    if not provider_id:
        return None
    ctx_raw = payload.get("context_window")
    try:
        context_window = (
            int(ctx_raw) if ctx_raw not in (None, "", 0) else None
        )
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
    return LlmRoute(
        provider_id=provider_id,
        model=str(payload.get("model", "") or "").strip(),
        context_window=context_window,
        max_tokens=max_tokens,
        temperature=temperature,
        reasoning_effort=_norm_reasoning_effort(
            payload.get("reasoning_effort")
        ),
    )


def _parse_external_mcp_server(payload: dict[str, Any]) -> ExternalMcpServer | None:
    """Validate one entry from ``mcp_clients.servers``.

    Returns ``None`` for malformed rows (missing id, or a stdio server
    with no command, or an sse server with no url) so a single bad entry
    never aborts the whole load. Mirrors :func:`_parse_llm_provider`.
    """
    if not isinstance(payload, dict):
        return None
    server_id = str(payload.get("id", "") or "").strip()
    if not server_id:
        return None
    transport = str(payload.get("transport", "stdio") or "stdio").strip().lower()
    if transport not in {"stdio", "sse"}:
        transport = "stdio"
    command = str(payload.get("command", "") or "").strip()
    url = str(payload.get("url", "") or "").strip()
    if transport == "stdio" and not command:
        log.warning("mcp_clients: stdio server %r has no command, skipped", server_id)
        return None
    if transport == "sse" and not url:
        log.warning("mcp_clients: sse server %r has no url, skipped", server_id)
        return None
    args_raw = payload.get("args") or []
    args = tuple(str(a) for a in args_raw) if isinstance(args_raw, (list, tuple)) else ()
    env_raw = payload.get("env") or {}
    if isinstance(env_raw, dict):
        env = {
            str(k).strip(): str(v)
            for k, v in env_raw.items()
            if str(k).strip()
        }
    else:
        env = {}
    expose_raw = payload.get("expose_tools") or []
    expose_tools = (
        tuple(str(t).strip() for t in expose_raw if str(t).strip())
        if isinstance(expose_raw, (list, tuple))
        else ()
    )
    disabled_raw = payload.get("disabled_tools") or []
    disabled_tools = (
        tuple(str(t).strip() for t in disabled_raw if str(t).strip())
        if isinstance(disabled_raw, (list, tuple))
        else ()
    )
    timeout_raw = payload.get("timeout_seconds", 30.0)
    try:
        timeout_seconds = max(1.0, float(timeout_raw))
    except (TypeError, ValueError):
        timeout_seconds = 30.0
    return ExternalMcpServer(
        id=server_id,
        name=str(payload.get("name", "") or "").strip() or server_id,
        transport=transport,
        command=command,
        args=args,
        env=env,
        url=url,
        enabled=bool(payload.get("enabled", True)),
        autostart=bool(payload.get("autostart", True)),
        timeout_seconds=timeout_seconds,
        expose_tools=expose_tools,
        disabled_tools=disabled_tools,
    )


def _parse_external_mcp(raw: Any) -> ExternalMcpSettings:
    """Validate the ``mcp_clients`` config block.

    Drops malformed rows and skips duplicate ids; returns an empty
    catalogue when the block is missing/malformed.
    """
    if not isinstance(raw, dict):
        return ExternalMcpSettings()
    servers: list[ExternalMcpServer] = []
    seen_ids: set[str] = set()
    for entry in raw.get("servers") or []:
        parsed = _parse_external_mcp_server(entry)
        if parsed is None:
            continue
        if parsed.id in seen_ids:
            log.warning("mcp_clients: duplicate server id %r skipped", parsed.id)
            continue
        seen_ids.add(parsed.id)
        servers.append(parsed)
    return ExternalMcpSettings(servers=servers)


def _parse_browser_perception(raw: Any) -> BrowserPerceptionSettings:
    """Validate the ``browser_perception`` config block.

    Returns defaults (disabled) when the block is missing/malformed so a
    bad entry never aborts the load. Weights and caps are clamped to sane
    floors; ``snapshot_tools`` falls back to the default when empty.
    """
    defaults = BrowserPerceptionSettings()
    if not isinstance(raw, dict):
        return defaults

    def _weight(key: str, fallback: float) -> float:
        try:
            return max(0.0, float(raw.get(key, fallback)))
        except (TypeError, ValueError):
            return fallback

    tools_raw = raw.get("snapshot_tools")
    if isinstance(tools_raw, (list, tuple)):
        snapshot_tools = tuple(
            str(t).strip() for t in tools_raw if str(t).strip()
        )
    else:
        snapshot_tools = ()
    if not snapshot_tools:
        snapshot_tools = defaults.snapshot_tools

    try:
        max_ranked = max(1, int(raw.get("max_ranked_elements", defaults.max_ranked_elements)))
    except (TypeError, ValueError):
        max_ranked = defaults.max_ranked_elements
    try:
        state_pages = max(1, int(raw.get("state_memory_pages", defaults.state_memory_pages)))
    except (TypeError, ValueError):
        state_pages = defaults.state_memory_pages

    return BrowserPerceptionSettings(
        enabled=bool(raw.get("enabled", defaults.enabled)),
        server_id=str(raw.get("server_id", defaults.server_id) or defaults.server_id).strip()
        or defaults.server_id,
        snapshot_tools=snapshot_tools,
        adapter=str(raw.get("adapter", defaults.adapter) or defaults.adapter).strip()
        or defaults.adapter,
        max_ranked_elements=max_ranked,
        state_memory_pages=state_pages,
        weight_role=_weight("weight_role", defaults.weight_role),
        weight_visibility=_weight("weight_visibility", defaults.weight_visibility),
        weight_position=_weight("weight_position", defaults.weight_position),
        weight_text=_weight("weight_text", defaults.weight_text),
        weight_context=_weight("weight_context", defaults.weight_context),
    )


def _parse_weather_settings(raw: Any) -> WeatherSettings:
    """Validate the ``weather`` config block (H11).

    Returns defaults when missing/malformed so a hand-edited config never
    aborts boot. Coordinates are clamped to valid lat/lon ranges (or
    dropped to ``None``), ``units`` falls back to ``"metric"``, and the
    refresh interval is floored at 15 minutes.
    """
    defaults = WeatherSettings()
    if not isinstance(raw, dict):
        return defaults

    def _coord(key: str, lo: float, hi: float) -> float | None:
        val = raw.get(key)
        if val is None or val == "":
            return None
        try:
            num = float(val)
        except (TypeError, ValueError):
            return None
        if num < lo or num > hi:
            return None
        return num

    units = str(raw.get("units", defaults.units) or defaults.units).strip().lower()
    if units not in ("metric", "imperial"):
        units = defaults.units
    try:
        interval = max(15, int(raw.get("refresh_interval_minutes", defaults.refresh_interval_minutes)))
    except (TypeError, ValueError):
        interval = defaults.refresh_interval_minutes
    try:
        timeout = max(1.0, float(raw.get("timeout_seconds", defaults.timeout_seconds)))
    except (TypeError, ValueError):
        timeout = defaults.timeout_seconds

    return WeatherSettings(
        provider=str(raw.get("provider", defaults.provider) or defaults.provider).strip().lower()
        or defaults.provider,
        geocoder=str(raw.get("geocoder", defaults.geocoder) or defaults.geocoder).strip().lower()
        or defaults.geocoder,
        location_name=str(raw.get("location_name", "") or "").strip()[:80],
        latitude=_coord("latitude", -90.0, 90.0),
        longitude=_coord("longitude", -180.0, 180.0),
        units=units,
        refresh_interval_minutes=interval,
        api_key=str(raw.get("api_key", "") or ""),
        api_key_env=str(
            raw.get("api_key_env", defaults.api_key_env) or defaults.api_key_env
        ).strip(),
        timeout_seconds=timeout,
    )


def _parse_llm(raw: Any) -> LlmSettings:
    """Validate the ``llm`` config block.

    Returns an empty :class:`LlmSettings` when the block is missing
    or malformed; the caller (``load_settings``) then runs
    :func:`_migrate_legacy_llm` to synthesise providers + routes from
    the legacy ``chat_llm`` + ``ollama`` blocks.
    """
    if not isinstance(raw, dict):
        return LlmSettings()
    providers: list[LlmProvider] = []
    seen_ids: set[str] = set()
    for entry in raw.get("providers") or []:
        parsed = _parse_llm_provider(entry)
        if parsed is None:
            continue
        if parsed.id in seen_ids:
            log.warning("llm: duplicate provider id %r skipped", parsed.id)
            continue
        seen_ids.add(parsed.id)
        providers.append(parsed)
    routes: dict[str, LlmRoute] = {}
    raw_routes = raw.get("routes") or {}
    if isinstance(raw_routes, dict):
        for role, route_payload in raw_routes.items():
            role_name = str(role or "").strip()
            if not role_name:
                continue
            parsed_route = _parse_llm_route(route_payload)
            if parsed_route is None:
                continue
            routes[role_name] = parsed_route
    return LlmSettings(providers=providers, routes=routes)


_LEGACY_LOCAL_OLLAMA_ID = "local_ollama"
_LEGACY_CHAT_PROVIDER_ID = "chat_migrated"


def _migrate_legacy_llm(
    *,
    chat_llm: ChatLlmSettings,
    ollama: OllamaSettings,
    timeout: int,
) -> LlmSettings:
    """Synthesise a :class:`LlmSettings` from the legacy blocks.

    Called by :func:`load_settings` when ``llm.providers`` is empty.
    Idempotent: subsequent boots see the populated ``llm`` block and
    skip this path. The legacy blocks remain readable indefinitely
    so downgrades still boot — the new code also mirror-writes them
    on every save (see ``persist_user_overrides`` flow in
    SessionController) so external scripts that read ``chat_llm.*``
    keep working.

    Migration rules (from the plan):

    1. Synthesize ``local_ollama`` from the existing ``ollama.*`` block.
    2. If ``chat_llm.provider == "ollama"`` AND base_url matches local,
       route ``main_chat -> local_ollama`` with ``chat_llm.model`` /
       ``chat_llm.context_window``.
    3. Otherwise synthesize a second provider (id from
       ``chat_llm.provider_preset`` when set, else ``chat_migrated``)
       and route ``main_chat`` to it.
    4. Route ``worker_default -> local_ollama`` always (workers stay
       on local by default; user can later flip via UI).
    """
    providers: list[LlmProvider] = []

    # Step 1: local_ollama from the legacy ``ollama`` block.
    local_provider = LlmProvider(
        id=_LEGACY_LOCAL_OLLAMA_ID,
        name="Local Ollama",
        kind="ollama",
        base_url=(ollama.base_url or "http://127.0.0.1:11434").strip(),
        api_key="",
        api_key_env="",
        extra_headers={},
        timeout_seconds=int(timeout) if timeout else 300,
        keep_alive="30m",
    )
    providers.append(local_provider)

    # Steps 2-3: where does ``main_chat`` go?
    chat_provider_id = _LEGACY_LOCAL_OLLAMA_ID
    chat_model = (chat_llm.model or ollama.chat_model or "").strip()
    chat_context_window = chat_llm.context_window

    chat_base = (chat_llm.base_url or "").strip()
    local_base = local_provider.base_url
    chat_is_local = (
        chat_llm.provider == "ollama"
        and (not chat_base or _urls_match(chat_base, local_base))
    )

    if not chat_is_local:
        # Need a second provider entry for the remote chat path.
        # Prefer the provider_preset string as the id (stable across
        # restarts) when it's set to something the user picked.
        preset = (chat_llm.provider_preset or "").strip().lower()
        candidate_id = preset or _LEGACY_CHAT_PROVIDER_ID
        # Avoid collisions with the local entry.
        if candidate_id == _LEGACY_LOCAL_OLLAMA_ID:
            candidate_id = _LEGACY_CHAT_PROVIDER_ID
        kind = (chat_llm.provider or "openai_compatible").strip().lower()
        if kind not in {"ollama", "openai_compatible"}:
            kind = "openai_compatible"
        # Friendly name from preset id, capitalised; falls back to
        # the kind label.
        if preset:
            name = preset.replace("_", " ").title()
        else:
            name = "Chat provider"
        remote_provider = LlmProvider(
            id=candidate_id,
            name=name,
            kind=kind,
            base_url=chat_base,
            api_key=chat_llm.api_key or "",
            api_key_env=chat_llm.api_key_env or "",
            extra_headers=dict(chat_llm.extra_headers or {}),
            timeout_seconds=int(timeout) if timeout else 300,
            keep_alive=chat_llm.keep_alive or "30m",
            reasoning_effort=(
                getattr(chat_llm, "reasoning_effort", "") or ""
            ).strip().lower(),
        )
        providers.append(remote_provider)
        chat_provider_id = remote_provider.id

    # Step 4: routes.
    routes: dict[str, LlmRoute] = {
        LLM_ROLE_MAIN_CHAT: LlmRoute(
            provider_id=chat_provider_id,
            model=chat_model,
            context_window=chat_context_window,
            max_tokens=int(chat_llm.max_tokens or 512),
            temperature=chat_llm.temperature,
            reasoning_effort=(
                getattr(chat_llm, "reasoning_effort", "") or ""
            ).strip().lower(),
        ),
        LLM_ROLE_WORKER_DEFAULT: LlmRoute(
            provider_id=_LEGACY_LOCAL_OLLAMA_ID,
            model=(ollama.chat_model or "").strip(),
            context_window=ollama.context_window,
            max_tokens=512,
            temperature=None,
        ),
        # Nested-workflow planner. Mirrors worker_default exactly so it
        # resolves to the SAME cached client (ClientCache key is
        # (kind, base_url, key)) -- one Ollama instance, no extra VRAM.
        LLM_ROLE_WORKFLOW: LlmRoute(
            provider_id=_LEGACY_LOCAL_OLLAMA_ID,
            model=(ollama.chat_model or "").strip(),
            context_window=ollama.context_window,
            max_tokens=512,
            temperature=None,
        ),
    }
    return LlmSettings(providers=providers, routes=routes)


def _urls_match(a: str, b: str) -> bool:
    """Loose URL equality for legacy-migration purposes.

    Trailing slash + case are normalised so
    ``"http://127.0.0.1:11434"`` and ``"http://127.0.0.1:11434/"``
    are treated as the same provider entry.
    """
    return (a or "").strip().rstrip("/").lower() == (b or "").strip().rstrip("/").lower()


def _migrate_legacy_audio_keys(user_path: Path) -> None:
    """One-shot migration: drop ``audio.microphone_device`` /
    ``audio.output_device`` / ``audio.live_*`` from ``user.json``.

    These keys used to drive the server-side ``sounddevice`` stack;
    they're meaningless now that the browser owns the audio
    interfaces. We rewrite the file with the keys removed so
    upgraded users don't see them resurface in
    :func:`patch_user_overrides` round-trips.
    """
    if not user_path.is_file():
        return
    try:
        existing = _read_config(user_path)
    except Exception:
        return
    audio_block = existing.get("audio")
    if not isinstance(audio_block, dict):
        return
    stale_keys = (
        "microphone_device",
        "output_device",
        "live_input_mode",
        "live_ptt_type",
        "live_ptt_key",
        "live_ptt_mouse_button",
        "live_ptt_toggle",
    )
    removed = [k for k in stale_keys if k in audio_block]
    if not removed:
        return
    for key in removed:
        audio_block.pop(key, None)
    try:
        tmp = user_path.with_suffix(user_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(user_path)
        _config_cache.pop(str(user_path), None)
    except Exception:
        # Migration is best-effort; the in-memory load below already
        # ignores these keys, so a write failure is non-fatal.
        return


def load_settings(config_path: Path | None = None) -> AppSettings:
    # Strip legacy server-audio keys before the first read so a stale
    # ``user.json`` never re-introduces them via deep-merge.
    try:
        _migrate_legacy_audio_keys(USER_CONFIG_PATH)
    except Exception:
        pass
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
    llm_raw = raw.get("llm", {}) or {}
    tools_raw = raw.get("tools", {}) or {}
    search_raw = raw.get("search", {}) or {}
    weather_raw = raw.get("weather", {}) or {}
    endpointing_raw = raw.get("endpointing", {}) or {}
    avatar_raw = raw.get("avatar", {}) or {}

    settings = AppSettings(
        assistant=AssistantSettings(
            name=_required(assistant, "name"),
            remember_history=bool(_required(assistant, "remember_history")),
            user_id=str(assistant.get("user_id", "default") or "default").strip() or "default",
            user_display_name=str(assistant.get("user_display_name", "") or "").strip()[:32],
            tts_length_scale=_normalize_tts_length_scale(assistant.get("tts_length_scale")),
        ),
        ollama=OllamaSettings(
            # The ``ollama`` block is now the "local Ollama base + embeddings"
            # block, not the chat-routing block (chat/worker models, context
            # windows and temperatures live in ``llm.routes`` — see
            # docs/llm-providers.md). These three keys are kept tolerant
            # (default instead of _required) so the block can be slimmed to
            # just its infra/embedding keys without crashing the loader.
            # ``chat_model`` still seeds the legacy migration + the
            # local-Ollama fresh-install default, so it ships in default.json.
            base_url=str(ollama.get("base_url", "http://127.0.0.1:11434") or "http://127.0.0.1:11434").strip(),
            embedding_base_url=str(ollama.get("embedding_base_url", "") or "").strip(),
            chat_model=str(ollama.get("chat_model", "") or "").strip(),
            temperature=float(ollama.get("temperature", 0.6) if ollama.get("temperature") is not None else 0.6),
            context_window=(int(ollama["context_window"]) if ollama.get("context_window") is not None else None),
            embedding_model=str(ollama.get("embedding_model", "qwen3-embedding:0.6b")).strip() or "qwen3-embedding:0.6b",
            timeout=int(ollama.get("timeout", 300)),
            embedding_num_gpu=(
                int(ollama["embedding_num_gpu"])
                if ollama.get("embedding_num_gpu") is not None
                else None
            ),
            embedding_num_ctx=(
                int(ollama["embedding_num_ctx"])
                if ollama.get("embedding_num_ctx") is not None
                else None
            ),
            think_num_predict_headroom=max(
                0, int(ollama.get("think_num_predict_headroom", 2048)),
            ),
        ),
        audio=AudioSettings(
            sample_rate=int(_required(audio, "sample_rate")),
            channels=int(_required(audio, "channels")),
            enable_microphone=bool(_required(audio, "enable_microphone")),
            vad_level_threshold=float(audio.get("vad_level_threshold", 0.02)),
            vad_silence_seconds=float(audio.get("vad_silence_seconds", 1.0)),
            barge_in_enabled=bool(audio.get("barge_in_enabled", False)),
            earcons_enabled=bool(audio.get("earcons_enabled", True)),
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
        agent=parse_agent_settings(agent_raw),
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
            ui_log_enabled=bool(logging_raw.get("ui_log_enabled", False)),
            ui_log_categories=[
                str(token).strip().lower()
                for token in (
                    logging_raw.get("ui_log_categories")
                    or ["ws", "channel", "settings", "voice", "audio"]
                )
                if str(token).strip()
            ],
            ui_log_max_batch=max(1, min(500, int(logging_raw.get("ui_log_max_batch", 50)))),
            ui_log_max_payload_bytes=max(
                256, min(64 * 1024, int(logging_raw.get("ui_log_max_payload_bytes", 2048))),
            ),
        ),
        mcp_server=McpServerSettings(
            enabled=bool(mcp_server_raw.get("enabled", True)),
            port=max(1, int(mcp_server_raw.get("port", 6274))),
        ),
        mcp_clients=_parse_external_mcp(raw.get("mcp_clients", {})),
        browser_perception=_parse_browser_perception(
            raw.get("browser_perception", {})
        ),
        web_server=WebServerSettings(
            enabled=bool(web_server_raw.get("enabled", True)),
            host=str(web_server_raw.get("host", "127.0.0.1") or "127.0.0.1").strip() or "127.0.0.1",
            port=max(1, int(web_server_raw.get("port", 6275))),
        ),
        memory=parse_memory_settings(memory_raw),
        chat_llm=_parse_chat_llm(chat_llm_raw),
        llm=_parse_llm(llm_raw),  # populated below if empty
        tools=ToolsSettings(
            enabled=bool(tools_raw.get("enabled", True)),
            get_time=bool(tools_raw.get("get_time", True)),
            recall=bool(tools_raw.get("recall", True)),
            recall_topic=bool(tools_raw.get("recall_topic", True)),
            web_search=bool(tools_raw.get("web_search", True)),
            world=bool(tools_raw.get("world", True)),
            goals=bool(tools_raw.get("goals", True)),
            file_tasks=bool(tools_raw.get("file_tasks", True)),
            workflow=bool(tools_raw.get("workflow", True)),
            calculate=bool(tools_raw.get("calculate", True)),
            weather=bool(tools_raw.get("weather", True)),
        ),
        search=SearchSettings(
            provider=(
                str(search_raw.get("provider", "duckduckgo") or "duckduckgo")
                .strip()
                .lower()
            ),
            api_key=str(search_raw.get("api_key", "") or ""),
            api_key_env=str(
                search_raw.get("api_key_env", "LANGSEARCH_API_KEY")
                or "LANGSEARCH_API_KEY"
            ).strip(),
            langsearch_summary=bool(search_raw.get("langsearch_summary", True)),
            langsearch_freshness=str(
                search_raw.get("langsearch_freshness", "noLimit") or "noLimit"
            ).strip(),
            langsearch_count=max(
                1, min(10, int(search_raw.get("langsearch_count", 10)))
            ),
            fallback_to_duckduckgo=bool(
                search_raw.get("fallback_to_duckduckgo", True)
            ),
            timeout_seconds=max(
                1.0, float(search_raw.get("timeout_seconds", 12.0))
            ),
            langsearch_min_interval_seconds=max(
                0.0,
                float(
                    search_raw.get("langsearch_min_interval_seconds", 1.1)
                ),
            ),
            query_reformulation_enabled=bool(
                search_raw.get("query_reformulation_enabled", True)
            ),
        ),
        weather=_parse_weather_settings(weather_raw),
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
            root_dir=str(avatar_raw.get("root_dir", "data/personas/active/Alexia") or "data/personas/active/Alexia").strip(),
            entry_filename=str(avatar_raw.get("entry_filename", "Alexia.model3.json") or "Alexia.model3.json").strip(),
            scale_multiplier=max(0.1, min(8.0, float(avatar_raw.get("scale_multiplier", 1.0) or 1.0))),
            auto_outfit=(
                str(avatar_raw.get("auto_outfit", "auto") or "auto").strip().lower()
                if str(avatar_raw.get("auto_outfit", "auto") or "auto").strip().lower() in OUTFIT_MODES
                else "auto"
            ),
            expressiveness=max(0.0, min(1.5, float(avatar_raw.get("expressiveness", 1.0) or 1.0))),
            mood_inertia_damping=bool(
                avatar_raw.get("mood_inertia_damping", True),
            ),
            accessory_state=_load_accessory_state(avatar_raw.get("accessory_state")),
        ),
    )

    # ── PR 2: legacy LLM migration (idempotent) ─────────────────────
    #
    # If ``llm.providers`` is empty (first boot after upgrade), synthesise
    # the catalogue + routes from the legacy ``chat_llm`` + ``ollama``
    # blocks. Subsequent boots see the populated ``llm`` block and skip.
    # The legacy blocks remain readable indefinitely — back-compat is
    # the contract, not opt-in.
    if not settings.llm.providers:
        settings.llm = _migrate_legacy_llm(
            chat_llm=settings.chat_llm,
            ollama=settings.ollama,
            timeout=settings.ollama.timeout,
        )

    # Backfill the workflow route for installs that migrated before the
    # nested-workflow feature shipped (persisted routes without a
    # ``workflow`` entry). Mirror ``worker_default`` so it shares the
    # same cached client -- no extra VRAM, no behaviour change.
    if (
        LLM_ROLE_WORKFLOW not in settings.llm.routes
        and LLM_ROLE_WORKER_DEFAULT in settings.llm.routes
    ):
        worker_route = settings.llm.routes[LLM_ROLE_WORKER_DEFAULT]
        settings.llm.routes[LLM_ROLE_WORKFLOW] = LlmRoute(
            provider_id=worker_route.provider_id,
            model=worker_route.model,
            context_window=worker_route.context_window,
            max_tokens=worker_route.max_tokens,
            temperature=worker_route.temperature,
        )

    return settings
