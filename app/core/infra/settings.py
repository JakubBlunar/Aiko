from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any


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
class FileWriteSettings:
    """Per-capability resource config for the ``file_write`` task.

    The reusable pattern (see ``docs/task-approvals.md``): a destructive
    capability owns a small nested settings block grouping its resource
    knobs. The *approval* policy is generic and lives on
    :class:`AgentSettings` (``task_approval_mode`` /
    ``task_approval_overrides``); this block is only the file-write
    resource limits.

    ``enabled`` is the master switch — when off, the ``write_file``
    workflow skill is never offered to the planner and the handler is
    not registered. ``max_bytes`` caps the resulting file size.
    ``allowed_extensions`` is the case-insensitive write allow-list
    (empty = allow everything, same convention as the read handler).
    """

    enabled: bool = False
    max_bytes: int = 262144
    allowed_extensions: tuple[str, ...] = (
        ".txt", ".md", ".rst", ".log",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".csv", ".tsv",
        ".py", ".js", ".ts", ".tsx", ".jsx",
        ".html", ".css", ".xml",
        ".sh", ".bat", ".ps1",
        ".sql",
    )


@dataclass(slots=True)
class VisionSettings:
    """Resource config for the local-vision ``describe_image`` task.

    The vision task does NOT introduce a second model: it reuses the
    already-loaded worker Ollama client + worker model, so the only
    requirement is that the worker model is multimodal (e.g.
    ``qwen3.5:27b`` / ``qwen3.6:27b``). That's why there's no
    ``base_url`` / ``keep_alive`` / ``num_ctx`` here — those are
    inherited from the worker client so there is genuinely one model
    config to reason about.

    * ``enabled`` — master switch. Off = the ``describe_image`` workflow
      skill is not offered and the handler is not registered.
    * ``model`` — OPTIONAL override. Empty (the default + recommended)
      reuses the effective worker model. A non-empty value points the
      vision call at a different local model, accepting a load/reload.
    * ``max_bytes`` — hard cap on the image file size that will be
      base64-encoded and sent to Ollama.
    * ``timeout_seconds`` — per-call ceiling (vision inference + a
      possible cold model load can be slow).
    * ``allowed_extensions`` — case-insensitive image extension
      allow-list (empty = allow everything).
    * ``default_prompt`` — instruction sent alongside the image when the
      caller doesn't supply a question.
    """

    enabled: bool = False
    model: str = ""
    max_bytes: int = 8 * 1024 * 1024
    timeout_seconds: int = 180
    allowed_extensions: tuple[str, ...] = (
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp",
    )
    default_prompt: str = (
        "Look at this image and describe what you see in a few natural "
        "sentences. Mention the main subject, setting, notable details, "
        "any visible text, and the overall mood."
    )


@dataclass(slots=True)
class AgentSettings:
    """Lean v1 conversation agent knobs.

    Proactive nudges are driven by
    :class:`app.core.proactive.proactive_director.ProactiveDirector`.

    The ``summary_*`` knobs and ``max_prompt_tokens_pct`` together control
    context compaction (rolling summary + on-overflow squish) handled by
    :class:`app.core.proactive.summary_worker.SummaryWorker` and
    :class:`app.core.session.turn_runner.TurnRunner`.
    """

    proactive_silence_seconds: float = 45.0
    proactive_cooldown_seconds: float = 120.0
    # ── Typed-mode proactive (Aiko speaks first in typed chat) ────────
    # Independent timing knobs from the voice-mode ones above so the
    # two cadences can differ. Defaults intentionally long (4 min
    # silence, 10 min cooldown) so a heads-down typed session never
    # gets nag-y. Gated client-side by browser visibility / Tauri
    # window focus — see ``SessionController._user_present``.
    proactive_typed_enabled: bool = True
    proactive_silence_seconds_typed: float = 240.0  # 4 min
    proactive_cooldown_seconds_typed: float = 600.0  # 10 min
    # When ``False`` (default) the typed-mode proactive director respects
    # ``_user_present``: every connected window hidden / blurred -> no
    # autonomous chime. Flip to ``True`` to opt in to "Aiko can chat
    # in even when I'm not at the window" — the silence timer fires
    # regardless of whether any client window is visible. Voice-mode
    # proactive ignores presence on purpose (mic users are present in
    # conversation even when away from the screen) so this flag does
    # not affect it.
    proactive_typed_when_away: bool = False
    # When ``True`` a typed-mode proactive line is ALSO spoken via TTS
    # (same enqueue path as voice-mode proactive). Default ``False``:
    # typed proactive is text-only because the nudge can land minutes
    # later when the user may be away from the speakers, and an unprompted
    # spoken line then is more startling than helpful. Voice-mode
    # proactive always speaks regardless of this flag.
    proactive_typed_tts_enabled: bool = False
    # ── World-notice proactive (Aiko reaches out about her room) ──────
    # Master switch for the WorldNoticeWorker, which primes a proactive
    # nudge when the user has left something in Aiko's room or after a
    # long quiet stretch. The actual cadence / cooldown / daily-cap live
    # in ``MemorySettings.world_notice_*`` alongside the other idle
    # workers; this just turns the whole behaviour on or off.
    world_notice_enabled: bool = True
    # ── Activity awareness (desktop opt-in) ───────────────────────────
    # When enabled and running inside the Tauri desktop shell, the
    # foreground application name is forwarded over WebSocket so Aiko
    # can naturally reference what the user is doing. App name only —
    # never window titles or URLs (see ``docs/presence-and-activity``
    # for the privacy posture). Off by default; browser shells render
    # the toggle but can never produce a non-null active app.
    activity_awareness_enabled: bool = False
    # ── Shared moments + relationship depth (schema v7) ───────────────
    # ``shared_moments_enabled`` is the master switch for the entire
    # subsystem (inline tag extraction, LLM detector, Together tab,
    # anniversary block). With it off, ``[[moment:]]`` tags still get
    # stripped from chat (the strip pattern lives upstream) but they're
    # not persisted.
    # ``shared_moments_llm_enabled`` toggles only the LLM Track 2
    # detector — turning it off keeps Aiko-curated tags and manual UI
    # button working.
    # The LLM detector is gated by ``shared_moments_min_turn_gap``
    # (cadence) AND ``shared_moments_cooldown_seconds`` (wall-clock) so
    # back-to-back warm exchanges produce at most one moment per window.
    shared_moments_enabled: bool = True
    shared_moments_llm_enabled: bool = True
    shared_moments_min_turn_gap: int = 5
    shared_moments_cooldown_seconds: float = 300.0
    # Anniversary surfacing renders a single "On your mind today — a
    # year ago today, …" line in the system prompt when a shared moment
    # matches one of the 1mo/3mo/6mo/1yr/Nyr windows. Independent of
    # ``shared_moments_enabled`` so you can keep moments off but
    # surface anniversaries from a historical archive (or vice versa).
    anniversary_surfacing_enabled: bool = True
    # Relationship axes: 4 floats (closeness, humor, trust, comfort)
    # that drift per turn from reactions, moments, milestones. Cheap
    # (one SQL upsert). The prompt block is terse and only renders
    # when an axis exceeds the notable threshold (default 0.5).
    relationship_axes_enabled: bool = True
    # J8: milestone-celebration cue. When a relationship milestone crosses
    # (100 turns / 1 week / 1 month / 100 days / 6 months / 1 year), a
    # one-shot warm acknowledgement is surfaced into the next turn's
    # prompt (stage-aware register via J4). Off = the milestone is still
    # recorded as a memory but never actively acknowledged.
    milestone_celebration_enabled: bool = True
    # J5: reconnection ritual. On the first reply after a long absence
    # (>= reconnection_base_gap_hours, closeness-scaled so a closer
    # relationship notices a gap sooner), surface a one-shot warm
    # re-anchoring cue that colours the opener. Distinct from the K57
    # lonely episode (felt) and K28/K36 (next-turn "what I was up to").
    reconnection_enabled: bool = True
    reconnection_base_gap_hours: float = 24.0
    # J10: appreciation beats. Rare, specific unprompted gratitude anchored
    # to a recent positive shared moment. Gated by closeness + a long
    # wall-clock cooldown so it stays a treat, never a tic.
    appreciation_beats_enabled: bool = True
    appreciation_min_closeness: float = 0.25
    appreciation_cooldown_hours: float = 72.0
    appreciation_max_anchor_age_days: float = 21.0
    # J9: reciprocal vulnerability. Rare cue authorising Aiko to open up
    # about something she's sitting with, so the user gets to be the
    # supportive one. Stage (familiar+) + trust gated, paced by the K15
    # budget, and hard-suppressed when the user is in a low-mood window.
    reciprocal_vulnerability_enabled: bool = True
    reciprocal_vulnerability_cooldown_hours: float = 96.0
    reciprocal_vulnerability_min_trust: float = 0.2
    # J6: conflict-repair memory. When a K8 rupture resolves (the user's
    # valence recovers within a turn window), record a durable
    # ``repair``-vibe shared moment so Aiko can reference "we sorted this
    # out" instead of re-litigating. Cooldown stops one rough patch from
    # spawning several rows.
    conflict_repair_enabled: bool = True
    conflict_repair_watch_turns: int = 5
    conflict_repair_recovery_epsilon: float = 0.05
    conflict_repair_min_recovery_rise: float = 0.10
    conflict_repair_cooldown_hours: float = 12.0
    # ── F1 personality backlog: background fact-checker ───────────────
    # Master switch. When off, the queue still persists but the
    # IdleFactChecker worker never runs (so any pending claims simply
    # sit there harmlessly until the flag is flipped back on or the
    # underlying memory is deleted).
    fact_checker_enabled: bool = True
    # Hourly + daily rate caps. Token-bucket persisted to ``kv_meta``.
    # The defaults give the worker a generous budget while still
    # keeping a chatty session from burning unbounded web queries.
    fact_checker_per_hour_cap: int = 10
    fact_checker_per_day_cap: int = 50
    # ── G2 personality backlog: schedule learner ──────────────────────
    # Master switch for :class:`app.core.infra.schedule_learner.ScheduleLearner`,
    # the IdleWorker that buckets ``messages.created_at`` into a
    # ``usual_hours`` user-profile field. Cheap to run; safe to leave on.
    schedule_learner_enabled: bool = True
    # Minimum number of user messages in the rolling window before the
    # worker writes anything. Below this threshold the field stays
    # untouched so a fresh DB doesn't claim a confident schedule.
    schedule_learner_min_samples: int = 5
    # Rolling window the bucketing scan considers. 30 days keeps the
    # picture current without being noisy after a single anomalous day.
    schedule_learner_window_days: int = 30
    # ── K3 personality backlog: routine / ritual awareness ────────────
    # Master switch for the second pass inside ``ScheduleLearner`` that
    # detects named recurring slots ("Sunday-morning chats") and writes
    # them into the ``routines`` user-profile field. Disabling this
    # leaves the G2 ``usual_hours`` write intact; only the K3 pass is
    # skipped. Cheap (no LLM, no embedder), safe to leave on.
    routine_detection_enabled: bool = True
    # ── G3 personality backlog: idle curiosity worker ─────────────────
    # Master switch for
    # :class:`app.core.proactive.idle_curiosity_worker.IdleCuriosityWorker`. When
    # disabled, ``open_question`` memories simply never get web-searched.
    idle_curiosity_enabled: bool = True
    # Hourly + daily caps on web searches the curiosity worker is
    # allowed to issue. Strictly tighter than the fact-checker so a
    # multi-week absence (with a backlog of open questions) cannot dump
    # a wall of "I was reading about" beats on the user when they
    # return. Token-bucket persisted to ``kv_meta`` under a separate key.
    idle_curiosity_per_hour_cap: int = 2
    idle_curiosity_per_day_cap: int = 6
    # ── F5 personality backlog: conflicting-memory detector ──────────
    # Master switch for
    # :class:`app.core.memory.memory_conflict_worker.MemoryConflictWorker`.
    # When disabled the worker never registers its idle tick and the
    # Conflicts sub-tab in the Memory drawer is hidden.
    conflict_detector_enabled: bool = True
    # Hourly + daily caps on LLM verification calls the worker is
    # allowed to issue. The hybrid heuristic gate keeps most pairs
    # below this cap; only borderline (e.g. numerical-mismatch) pairs
    # consume budget. The token-bucket is persisted to ``kv_meta`` via
    # a dedicated :class:`FactCheckRateLimiter` with
    # ``state_key='conflict_detector.rate_state'`` so an idle pass
    # from the F1 fact-checker can't starve the F5 budget (and vice
    # versa).
    conflict_detector_per_hour_cap: int = 6
    conflict_detector_per_day_cap: int = 30
    # ── K35 personality backlog: memory consolidation worker ─────────
    # Master switch for
    # :class:`app.core.memory.memory_consolidation_worker.MemoryConsolidationWorker`.
    # When disabled the worker never registers its idle tick. The
    # per-hour / per-day caps bound the worker-LLM merge calls (one per
    # cluster), persisted to ``kv_meta`` via a dedicated
    # :class:`FactCheckRateLimiter` with
    # ``state_key='memory_consolidation.rate_state'`` so the merge
    # budget is independent of F1 / F5 / G3. Clusters that can't get a
    # token fall back to the deterministic "keep the strongest member
    # verbatim" path, so a starved budget never blocks consolidation.
    memory_consolidation_enabled: bool = True
    memory_consolidation_per_hour_cap: int = 6
    memory_consolidation_per_day_cap: int = 30
    # ── K2 personality backlog: theory-of-mind / belief tracking ─────
    # Master switch for the whole K2 surface (worker + gap detector +
    # tag parser + REST + UI). When disabled the worker never runs,
    # the gap detector is short-circuited, and the Beliefs sub-tab in
    # the Memory drawer is hidden. Self-tag emissions
    # (``[[predict:...]]``) are still stripped from chat so they
    # never leak to the user, but the parsed payload is dropped.
    belief_tracking_enabled: bool = True
    # Master switch for the background inference worker only. With
    # ``belief_tracking_enabled=True`` but
    # ``belief_worker_enabled=False`` Aiko's self-tag fast path still
    # writes beliefs and the gap detector still surfaces mismatches;
    # only the autonomous inference pass is suppressed.
    belief_worker_enabled: bool = True
    # Hourly + daily caps on LLM extraction calls the worker is
    # allowed to issue. Lower-cap by default than the F1 fact-checker
    # because belief inference is a "nice-to-have" mining job, not a
    # correctness gate. Dedicated
    # :class:`FactCheckRateLimiter` with
    # ``state_key='belief_worker.rate_state'``.
    belief_worker_per_hour_cap: int = 8
    belief_worker_per_day_cap: int = 40
    # ── Phase 3c (reworked): context-aware promise extraction worker ──
    # Master switch for
    # :class:`app.core.memory.promise_worker.PromiseExtractionWorker`,
    # the sole writer of ``kind="promise"`` memories. When disabled the
    # worker is never registered and no promises are auto-extracted
    # (the ``[[remember:...]]`` self-tag path is unaffected).
    promise_worker_enabled: bool = True
    # Hourly + daily caps on LLM extraction calls. Generous by default
    # because the worker runs frequently but each call is bounded by
    # these caps -- the real spend ceiling. Dedicated
    # :class:`FactCheckRateLimiter` with
    # ``state_key='promise_worker.rate_state'``.
    promise_worker_per_hour_cap: int = 10
    promise_worker_per_day_cap: int = 60
    # ── K6 personality backlog: surprise / novelty detector ──────────
    # Master switch for :class:`app.core.conversation.novelty_detector.NoveltyDetector`.
    # When disabled the detector is never instantiated and the
    # ``novelty`` inner-life provider is left unregistered, so the
    # prompt-assembler short-circuits the block with zero cost on the
    # hot path. The detector itself is purely in-process (one
    # Embedder.embed call per turn + a tiny ring buffer); there's no
    # rate-cap because the per-turn cost is the same as RAG retrieval.
    novelty_detection_enabled: bool = True
    # ── K18 personality backlog: topic stagnation detector ────────────
    # Master switch for
    # :class:`app.core.conversation.topic_stagnation.TopicStagnationDetector`.
    # The detector is a pure streak counter over the per-turn distance
    # K6 already computes (no extra embedding) so it's effectively
    # free; this knob exists to silence the cue when a tester wants
    # to focus on K6 alone. Leaving it on with conservative
    # thresholds is the intended default.
    topic_stagnation_enabled: bool = True
    # ── K9 personality backlog: topic graph + curiosity seeds ─────────
    # Master switch for the in-process topic graph wrapper around
    # :attr:`MemoryStore._mirror`. Disabling skips both the seed
    # worker's "have we discussed this already?" filter AND the
    # eventual Memory-tab cluster panel; the rest of the app keeps
    # functioning unchanged. Cheap on its own (rebuilds from the
    # existing in-memory mirror; no embedding work).
    topic_graph_enabled: bool = True
    # Master switch for
    # :class:`app.core.proactive.curiosity_seed_worker.CuriositySeedWorker`.
    # When ``False`` the worker never registers its idle tick and
    # the seed surfacing path (inner-life bullet + NarrativeWeaver
    # candidate) silently produces empty output. Default ON because
    # the worker is the headline behaviour change of K9.
    curiosity_seed_enabled: bool = True
    # Cap on how many active (un-consumed) seeds the worker keeps
    # alive at once. ``is_ready`` short-circuits when the count is
    # at the cap so a fast-talking session can't pile up forty
    # never-mentioned seeds. Two seeds is a normal active steady
    # state; six is the headroom for "user only chats on weekends".
    curiosity_seed_max_active: int = 6
    # Cap on how many candidates the worker writes per successful
    # tick. The LLM proposes up to 5; this is the post-filter cap on
    # how many of the survivors actually become memories. Keeping
    # it at 2 keeps the inner-life bullet list readable.
    curiosity_seed_max_per_run: int = 2
    # Novelty floor against existing seeds: a candidate whose cosine
    # to ANY active seed >= this is rejected (would be a near-
    # duplicate). Lower = more eager to write; higher = stricter.
    # 0.85 lines up with the dedupe threshold used by the rest of
    # the memory store.
    curiosity_seed_min_novelty: float = 0.85
    # Cosine match threshold for the post-turn auto-resolve hook.
    # When (current user_text + assistant_text) cosines this high
    # against a seed embedding the seed is marked consumed and
    # demoted to archive tier. Lower than the graph filter on
    # purpose -- partial / oblique mentions should still count, the
    # alternative is a seed that hangs around forever once the
    # conversation drifts past it.
    curiosity_seed_resolve_threshold: float = 0.50
    # ── K11 pre-thought / counterfactual cache ───────────────────────
    # Master switch for
    # :class:`app.core.proactive.pre_thought_worker.PreThoughtWorker`.
    # When ``False`` the worker never registers its idle tick. The
    # cached ``pre_thought`` memories already written stay in the store
    # and keep surfacing through RAG until they decay out.
    pre_thought_enabled: bool = True
    # Cap on how many active pre-thoughts the worker keeps alive at
    # once. ``is_ready`` short-circuits when the count is at the cap so
    # a long idle stretch can't pile up dozens of speculative drafts;
    # ``run`` also prunes the oldest beyond this cap after writing.
    pre_thought_max_active: int = 12
    # How many candidate questions the first-stage LLM call proposes
    # per tick (the worker drafts replies for up to ``max_per_run`` of
    # the survivors).
    pre_thought_candidates: int = 4
    # Cap on how many drafted pre-thoughts the worker writes per
    # successful tick (one second-stage draft LLM call each).
    pre_thought_max_per_run: int = 2
    # Novelty floor against existing pre-thoughts: a candidate question
    # whose cosine to ANY active pre-thought question >= this is
    # rejected as a near-duplicate. Mirrors ``curiosity_seed_min_novelty``.
    pre_thought_min_novelty: float = 0.85
    # Per-hour / per-day budget on the worker's LLM calls (a tick can
    # spend 1 question call + up to ``max_per_run`` draft calls). The
    # worker runs on the local worker model, so the caps are generous —
    # they only exist to stop a misconfigured fast cadence from running
    # the local box hot.
    pre_thought_per_hour_cap: int = 6
    pre_thought_per_day_cap: int = 40
    # ── K21 fresh-eyes thread re-summary ─────────────────────────────
    # Master switch for
    # :class:`app.core.proactive.thread_resummary_worker.ThreadResummaryWorker`.
    # When ``False`` the worker never registers its idle tick and the
    # prompt never carries a "where this thread is now" block.
    thread_resummary_enabled: bool = True
    # Floor on conversation length before a fresh-eyes note is worth
    # drafting at all (a 3-message thread doesn't need re-synthesis).
    thread_resummary_min_messages: int = 12
    # Re-draft once this many new messages have landed since the note's
    # ``messages_at`` watermark (the "~50 turns" trigger from the
    # backlog).
    thread_resummary_message_interval: int = 50
    # Re-draft when the existing note is older than this many hours even
    # if the message-interval trigger hasn't fired (the "daily,
    # whichever comes first" trigger).
    thread_resummary_max_age_hours: float = 24.0
    # Per-hour / per-day budget on the worker's LLM calls (one call per
    # successful tick). Runs on the local worker model; the caps only
    # stop a misconfigured fast cadence from running the box hot.
    thread_resummary_per_hour_cap: int = 6
    thread_resummary_per_day_cap: int = 24
    # ── K52 wants ledger — desire with pressure ──────────────────────
    # Master switch for the wants ledger: the feeder worker, the
    # prompt provider, and the post-turn acted-on detection all gate
    # on this. Default ON — the ledger is the structural half of the
    # "will" family.
    wants_ledger_enabled: bool = True
    # How fast a want's pressure grows per wall-clock day. At 0.25 a
    # fresh want (initial 0.15) crosses the imperative threshold
    # (0.7) in roughly 2.2 days of being ignored.
    wants_growth_per_day: float = 0.25
    # Pressure at which the prompt cue flips from the soft "spend one
    # when a lull lands" list to the imperative "bring it up THIS
    # conversation" directive.
    wants_imperative_threshold: float = 0.7
    # Maximum live wants. At the cap the feeder refuses new wants
    # (expiry and acting are the only exits) so pressure ordering
    # stays honest.
    wants_cap: int = 8
    # Wants never acted on expire after this many days — an itch
    # that old has faded, and dropping it keeps the ledger from
    # becoming a guilt list.
    wants_max_age_days: float = 14.0
    # After a want is acted on, its source_ref is blocked from
    # re-entry for this many days so the feeder doesn't immediately
    # re-add the same topic.
    wants_reentry_cooldown_days: float = 5.0
    # Feeder worker cadence (idle scheduler). Hourly matches the
    # other kv-backed maintenance workers.
    wants_worker_interval_seconds: float = 3600.0
    # ── K53 initiative turns — deterministic floor-taking ────────────
    # Master switch for the per-turn initiative directive ("this turn
    # is yours"). Default ON — the scheduled directive is the
    # highest-leverage piece of the will family.
    initiative_turns_enabled: bool = True
    # Base cadence in turns between directives, before arc / axes
    # modulation (light arcs -2, cold axes +2/+4, floor 3).
    initiative_base_period: int = 8
    # Turns at the start of a session before the first directive can
    # fire — turn 1 is never a floor-grab.
    initiative_warmup_turns: int = 3
    # User messages at or above this many characters skip the
    # directive silently (the escape hatch); the counter does not
    # reset, so the next short turn fires instead.
    initiative_substantial_chars: int = 240
    # ── K55 thread ownership — she defends what she opened ───────────
    # Master switch. When a K53 directive / K52 imperative fires, the
    # turn is stamped as Aiko's thread; a short pivot away in the
    # next user reply grants exactly one "circle back" cue.
    thread_ownership_enabled: bool = True
    # Replies at or above this many characters count as engaged when
    # no embedding comparison is available (length-only fallback).
    thread_engaged_chars: int = 80
    # Cosine threshold between the user reply and the opened-topic
    # embedding at or above which the reply counts as engaged
    # regardless of length ("yeah I loved it" is an answer).
    thread_min_topical_similarity: float = 0.30
    # ── K54 topic appetite — she's allowed to be bored ────────────────
    # Master switch for the once-per-conversation "tapped out on this
    # topic, here's my offer instead" permission slip.
    topic_appetite_enabled: bool = True
    # Assistant replies below this many characters count as
    # ack-and-ask (not substantive) when measuring her contribution.
    appetite_short_reply_chars: int = 160
    # Share of recent assistant replies that must be short before
    # she reads as disengaged (boredom needs BOTH a looped topic and
    # her only nodding along).
    appetite_short_share_threshold: float = 0.6
    # Number of recent assistant replies examined for the share.
    appetite_window: int = 6
    # Minimum K52 want pressure required as the offer — negotiating
    # the topic without something to offer is just rudeness.
    appetite_min_want_pressure: float = 0.35
    # Both relationship axes (closeness AND comfort) must be at or
    # above this — the topic tug-of-war is an earned-intimacy move.
    appetite_min_axes: float = 0.15
    # ── K57 directed emotion episodes — feelings at the user ─────────
    # Master switch for the episode store (lonely / miffed / warm_glow
    # / smug / playful_jealous / hurt with cause + decay + thaw).
    emotion_episodes_enabled: bool = True
    # Live episodes kept at once; the strongest wins the prompt.
    emotion_episode_cap: int = 3
    # Base absence (hours) before a gap can register as loneliness;
    # shortened by up to 30% as closeness grows.
    emotion_lonely_threshold_hours: float = 5.0
    # Intensity at or above which the episode cue switches from
    # "let it tint the register" to "this is the register".
    emotion_high_band: float = 0.5
    # ── K59 tease economy — "you'll pay for that one" ────────────────
    # Master switch for the payback ledger (bank on K29 pushback /
    # light offences, collect later as a callback tease).
    tease_economy_enabled: bool = True
    # Most debts kept at once; the oldest is evicted by a newcomer.
    tease_cap: int = 5
    # Unrepaid debts expire after this many days — an old grudge
    # stops being funny.
    tease_expiry_days: float = 14.0
    # Wall-clock hours between collection offers — the running bit
    # must never tip into needling.
    tease_collect_cooldown_hours: float = 12.0
    # Humor axis floor for collection (the bit needs an established
    # teasing register to land).
    tease_min_humor: float = 0.2
    # A debt must age this long before it can be collected — an
    # immediate callback isn't a callback.
    tease_min_age_hours: float = 1.0
    # ── K60 tsundere expression mask ─────────────────────────────────
    # User-facing flavour dial: "off" (default) / "tsundere_light"
    # (masks lonely + warm_glow, frequent dere-slips) /
    # "tsundere_full" (also masks the thaw beat, rarer slips).
    expression_mask: str = "off"
    # Wall-clock days between dere-slips in light mode (full mode
    # uses 2.5x this value).
    mask_slip_cooldown_days: float = 2.0
    # Cosine threshold consumed by
    # :meth:`app.core.conversation.topic_graph.TopicGraph.is_close_to_any_cluster`
    # when the seed worker filters LLM candidates. Anything cosine-
    # close to any existing memory at or above this is rejected as
    # "we've already covered that." Default 0.65 sits between the
    # 0.55 single-link clustering threshold and the 0.85 dedupe
    # threshold so the filter catches "same topic, different angle"
    # without rejecting "adjacent but new" candidates.
    topic_graph_filter_threshold: float = 0.65
    # ── K1 personality backlog: Aiko's long-term goals ────────────────
    # Master switch for the K1 system: goal store + worker + persona +
    # tools + RAG bonus. Flipping ``False`` keeps the SQLite rows
    # intact (so goals survive between toggles), unregisters the
    # ``GoalWorker`` idle tick, silences the "Aiko's quiet long-term
    # goals" inner-life block via the renderer's gate, and stops the
    # ``[[goal:...]]`` self-tag from persisting new rows. The four
    # agent tools (``add_goal`` / ``update_goal_progress`` /
    # ``archive_goal`` / ``list_goals``) are independently gated by
    # ``tools.goals`` below — disabling the master switch leaves the
    # tools wired but they raise immediately because the store skips
    # initialisation. Default ON because the worker only bootstraps
    # once per cold install (single LLM call) and the reflection tick
    # is rate-capped to ``goal_worker_per_*_cap`` below.
    goals_enabled: bool = True
    # Cold-start bootstrap controls whether the ``GoalWorker`` is
    # allowed to fire its initial "propose ~3 goals from persona +
    # rolling summary" LLM call when the store is empty. Flip ``False``
    # if you'd rather seed goals manually via the Memory tab and never
    # let the worker propose its own. The reflection path is
    # unaffected -- once at least one active goal exists, the
    # bootstrap branch is never entered. Default ON so a fresh install
    # arrives with a small set of goals already in place.
    goal_worker_bootstrap_enabled: bool = True
    # Hourly + daily caps on LLM calls the GoalWorker may issue, both
    # the bootstrap pass and per-goal reflection ticks combined.
    # Dedicated :class:`app.core.memory.fact_check_rate_limiter.FactCheckRateLimiter`
    # with ``state_key='goal_worker.rate_state'``. The hourly cap of
    # 3 lines up with the worker's hourly tick cadence with two extra
    # slots for manual ``force_run`` calls; the daily cap of 12 lets
    # Aiko reflect on each of the five active goals twice a day with
    # headroom for the bootstrap pass on day one. Set both to 0 to
    # disable autonomous calls entirely without unregistering the
    # worker (e.g. when you want only the ``[[goal:...]]`` self-tag
    # and the in-turn tools to write goals).
    goal_worker_per_hour_cap: int = 3
    goal_worker_per_day_cap: int = 12
    # ── K16. Unified ambient grounding line ───────────────────────────
    # The grounding line is one paragraph at the top of the system
    # prompt that fuses the seven "ambient" inner-life signals
    # (circadian, world, activity-awareness, affect/mood,
    # relationship-pulse, user_state, ambient_noise) into a single
    # continuous-awareness paragraph. The companion-feel hypothesis is
    # that the LLM treats one paragraph as continuous awareness rather
    # than seven separate facts to recite.
    #
    # Three modes (the canonical reference; mirrored verbatim in
    # docs/personality-backlog/shipped.md and AGENTS.md):
    #
    # ``off`` (default): no grounding line; the seven granular blocks
    #   render as today. Safe rollback target. Use this until you've
    #   verified ``replace`` reads well in your sessions.
    # ``replace``: the grounding line replaces all eight ambient
    #   blocks (the seven listed above plus mood_hint). Cleanest test
    #   of the hypothesis. Most aggressive.
    # ``split``: the grounding line replaces situational signals
    #   (circadian, world, activity, ambient_noise) but keeps
    #   {affect, mood_hint, relationship, user_state} as standalone
    #   blocks. Use when you want to keep the trend phrasing
    #   (affect "lately you've been..."; relationship phase line)
    #   that the fused line cannot represent without dilution.
    #
    # Suppression matrix (which blocks render in which mode):
    #
    #   block            off    split    replace
    #   grounding_line   empty  shown    shown
    #   circadian        shown  dropped  dropped
    #   world            shown  dropped  dropped
    #   activity         shown  dropped  dropped
    #   ambient_noise    shown  dropped  dropped
    #   affect           shown  shown    dropped
    #   mood_hint        shown  shown    dropped
    #   relationship     shown  shown    dropped
    #   user_state       shown  shown    dropped
    #   anniversary, profile bullets, pajama, knowledge_gaps,
    #   belief_gaps, novelty, stagnation, agenda, axes, petname,
    #   vocal_tone, catchphrase, narrative, arc -- ALWAYS shown,
    #   never affected by this mode.
    #
    # Verifying the flip took effect:
    #   - MCP ``get_last_response_detail`` shows
    #     ``provider_ms.grounding_line`` non-zero in ``replace``/``split``,
    #     missing or zero in ``off``.
    #   - DEBUG ``prompt built:`` log line: ``providers=`` count drops
    #     by the number of suppressed granular blocks.
    #
    # Invalid values (anything other than off/replace/split) clamp to
    # ``off`` with a debug log so a typo in the config never breaks the
    # prompt.
    grounding_line_mode: str = "off"
    # ── K-time1. Wall-clock prefixes on chat history ──────────────────
    # When True (the default), every message in the chat history sent to
    # the LLM is prefixed with a short relative-age tag like ``[2 min
    # ago] ...`` / ``[just now] ...`` / ``[yesterday 18:45] ...``. The
    # current user message Aiko is replying to is appended separately
    # and never gets a prefix.
    #
    # Why this exists: without per-message timestamps the LLM has no
    # clock against the conversation -- e.g. {user} saying "I'm
    # planning to visit my grandparents in half an hour" 2 minutes ago
    # gets pattern-matched as a completed past event, and Aiko asks
    # "did you make it back?". The prefix gives the LLM an explicit
    # clock so future plans stay future and recent moments read as
    # recent. The accompanying persona block teaches Aiko how to use
    # the prefix (and not to quote it back).
    #
    # Token cost: ~4-6 tokens per kept history message. Negligible
    # against the configured ``ollama.context_window`` budget.
    #
    # Turn OFF if you want a byte-identical history to the pre-K-time1
    # behaviour (e.g. for A/B comparison, or if your LLM treats the
    # bracketed metadata as part of the dialogue).
    history_age_prefix_enabled: bool = True
    # K51 -- cue-register rotation. When ON, inner-life cue blocks that
    # open with the literal "Heads-up:" get the prefix rotated across a
    # few register shapes ("Heads-up:" / "Quiet note:" / "Noticing:" /
    # bare) at prompt-assembly time, deterministic per turn, so the
    # model never reads the same coach template several times in one
    # prompt. OFF = byte-identical legacy cues (the shared-prefix lint
    # still runs).
    cue_register_rotation_enabled: bool = True
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
    # ``num_predict`` ceiling for the self-image LLM call. The prompt asks
    # for a 60–120 word paragraph (~160 tokens), but reasoning models like
    # qwen3.x can leak chain-of-thought into the response and eat budget
    # before the actual paragraph starts. The default leaves headroom for
    # that without being so large that a runaway response is unbounded.
    # Bump this if you keep seeing ``surface=self_image_worker`` truncation
    # warnings in the log.
    self_image_max_tokens: int = 320
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

    # ── P14: heuristic tool-pass gate ─────────────────────────────────
    # When true (default), turns with no tool-shaped signal skip the
    # forced ``chat_with_tools`` decision pass entirely — the largest
    # avoidable time-to-first-token contributor when tools are enabled.
    # Continuity signals (finished-task block, active tasks, previous
    # turn dispatched a tool) always run the pass. Set to false to
    # restore the old always-run behaviour (the kill-switch if tool
    # recall ever regresses). See
    # [`app/core/session/tool_pass_gate.py`](../session/tool_pass_gate.py).
    tool_pass_gate_enabled: bool = True

    # ── Skills framework: progressive tool disclosure ─────────────────
    # When true, the brain exposes only the matched tool families plus the
    # always-on core (``brain_core_skills``) on a tool-shaped turn, instead
    # of the whole registry. Off (default) = today's behaviour: every
    # registered tool every gated turn. ``world`` is in the core so Aiko's
    # spontaneous room actions (sip tea, shift posture) survive on turns
    # whose text named no item. See docs/skills-framework.md.
    skill_router_enabled: bool = False
    brain_core_skills: tuple[str, ...] = ("time", "recall", "world")
    # Worker-lane router: narrows the workflow planner's skill menu to the
    # group(s) relevant to the goal before each plan. Off (default) = full
    # menu, today's behaviour.
    workflow_skill_router_enabled: bool = False

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
    # ``num_predict`` ceiling for the weekly pulse. The prompt asks for
    # 1–2 sentences (≤50 words ~ 70 tokens), but qwen3.x-style models
    # can leak hidden reasoning before the answer starts. 256 leaves
    # comfortable headroom; bump it if you still see truncation warnings
    # tagged ``surface=relationship_pulse``.
    relationship_pulse_max_tokens: int = 256

    # ── Cadence / prosody (Phase 5b) ──────────────────────────────────
    # ProsodyDispatcher inserts per-sentence reactions, occasional micro
    # prefixes ("Mm.", "Oh,") and gentle pause-style punctuation tweaks.
    # All hints are text-only — engines that ignore punctuation are safe.
    cadence_enabled: bool = True
    # Layer 4 (expressive speech): auto-sprinkle ``breath`` / ``soft_sigh``
    # earcons on the first sentence of a melancholy / wistful / sad
    # turn. Cooldown-gated inside the cadence layer so a long
    # heart-to-heart conversation doesn't wheeze. Set to false to
    # silence all auto-sprinkle behaviour; the LLM can still emit
    # ``[[breath]]`` / ``[[chuckle]]`` etc. inline regardless.
    earcon_auto_sprinkle: bool = True
    # Layer 1c (expressive speech): opt-in gate for runtime per-reaction
    # ``model.temp`` mutation. Pocket-TTS is sensitive to temperature
    # excursions away from its tuned baseline -- empirically a delta
    # of even ±0.05 can introduce pitch / timbre artefacts on some
    # voices. Default OFF so the engine always uses the configured
    # ``tts.pocket_tts_temp`` baseline; flip on once you've validated
    # the deltas in :data:`app.tts.pocket_tts_service._REACTION_TEMP_DELTA`
    # sound right on the active voice file.
    tts_runtime_temp_enabled: bool = False
    # Layer 5 (expressive speech): opt-in gate for per-reaction speed
    # jitter. Pocket-TTS implements speed by scaling the playback
    # ``sample_rate``, which couples speed and pitch (a 10% faster
    # sentence is also ~1.6 semitones higher). With per-reaction
    # sub-caps active, that pitch couples to the affect channel and
    # the user perceives "her voice keeps changing" between sentences
    # -- even if each individual band is small. Default OFF so every
    # sentence plays at the engine's tuned 1.0× baseline; flip on once
    # you've listened to the active voice through
    # ``tools/tts_speed_ab.py`` at the proposed band. The user's
    # static pacing slider (``assistant.tts_length_scale``) is honoured
    # regardless of this gate -- it's a deliberate global knob, not
    # per-sentence affect drift.
    tts_runtime_speed_enabled: bool = False

    # ── Aiko style-pattern tracker (response-variability anti-rut) ────
    # Watches Aiko's own recent assistant turns for opener / question /
    # length ruts and surfaces a soft "Heads-up" inner-life cue when
    # one of the bands trips. Sibling architecture to the K6 / K18
    # detectors above; the persona's "Style patterns I'm in" section
    # pairs with the cues this tracker emits. Defaults are calibrated
    # to the diagnostic captured against ~120 assistant messages:
    # opener concentration ~39%, question-end rate ~87%, avg ~52
    # words / 4.9 sentences. Tune via these knobs without code changes.
    style_tracker_enabled: bool = True
    style_tracker_window: int = 12
    style_tracker_warmup: int = 6
    style_tracker_opener_count_threshold: int = 4
    style_tracker_opener_topk_share: float = 0.60
    style_tracker_question_rate_threshold: float = 0.75
    style_tracker_avg_questions_threshold: float = 1.5
    style_tracker_length_avg_threshold: float = 50.0
    style_tracker_cue_cooldown_turns: int = 5

    # ── K47: question/share balance (stop interviewing) ───────────────
    # Proactive complement to the reactive style-tracker question
    # saturation cue. A rolling per-session ratio of Aiko's replies that
    # contain a question; once it exceeds ``ratio_threshold`` over a full
    # ``window``, the question-pushing inner-life providers
    # (curiosity_seeds / forward_curiosity / follow_up / knowledge_gaps +
    # the narrative open_question nudge) are suppressed for the next
    # ``suppress_turns`` turns and a share-first cue is injected BEFORE
    # the LLM call. See
    # [`app/core/conversation/question_balance.py`](../conversation/question_balance.py).
    question_balance_enabled: bool = True
    question_balance_ratio_threshold: float = 0.55
    question_balance_window: int = 10
    question_balance_suppress_turns: int = 2

    # ── K48: tease rhythm (banter as a budget) ────────────────────────
    # Classify tease-shaped assistant turns over a rolling window, read
    # whether the previous tease landed (K32 laugh reaction vs. a
    # short/curt reply), and surface an "ease off" or "one more step is
    # safe" cue. Escalation is gated by the ``humor`` relationship axis
    # so early-relationship Aiko stays gentle. See
    # [`app/core/conversation/tease_rhythm.py`](../conversation/tease_rhythm.py).
    tease_rhythm_enabled: bool = True
    tease_rhythm_window: int = 6
    tease_rhythm_consecutive_cap: int = 3
    tease_rhythm_green_light_humor: float = 0.2
    tease_rhythm_cooldown_turns: int = 3

    # ── K13: stylometric mirror (Jacob-side stylometry) ───────────────
    # Tracks Jacob's writing style across recent user turns and emits
    # a one-line "How Jacob writes lately: terse, casual, asks back
    # often" directive so Aiko's register stays calibrated even when
    # the recent history window doesn't cover yesterday. Five axes:
    # terseness / formality / emoji / slang / question rate. Pure
    # rolling-window analyzer (no embedder, no LLM); persisted via a
    # tiny ``user_style_signal`` JSON-blob table so the window
    # survives restart. Unlike the K6/K18/anti-rut cues this block is
    # ALWAYS rendered (including aggressive mode) because it shapes
    # register, which is the first thing aggressive mode wants to
    # preserve. See [`app/core/persona/style_signal.py`](style_signal.py).
    style_signal_enabled: bool = True
    style_signal_window: int = 30
    style_signal_warmup_min: int = 8
    style_signal_terse_threshold: float = 0.55
    style_signal_formal_threshold: float = 0.55
    style_signal_emoji_threshold: float = 0.05
    style_signal_slang_threshold: float = 0.15
    style_signal_question_threshold: float = 0.40

    # ── K14: implicit engagement signals (latency + length) ──────────
    # Per-turn detector that scores Jacob's reply latency + message
    # length against rolling baselines and routes the signal to two
    # consumers:
    #   * voice mode → ``closeness_delta`` folded into the
    #     relationship-axes updater (snappy replies nudge closeness up;
    #     long voice gaps + curt messages nudge it down)
    #   * typed mode → ``absence_seconds`` band feeds a one-shot
    #     "absence-curiosity" inner-life cue on the NEXT user turn,
    #     and a label of ``"abandoned"`` suppresses the typed
    #     proactive nudge (mirrors the K4 vent gate).
    # Typed latency is deliberately NOT fed into closeness drift -- per
    # the project's design note, a typed pause is thinking time, not
    # disengagement. The latency window is voice-only; the length
    # window is shared with the K13 stylometric mirror via its
    # ``recent_word_counts()`` method (no duplicate buffer).
    # See [`app/core/affect/engagement_tracker.py`](engagement_tracker.py).
    engagement_tracker_enabled: bool = True
    engagement_window: int = 12
    engagement_warmup_min: int = 6
    engagement_latency_z_strong_drop: float = 1.5
    engagement_length_z_strong_drop: float = -1.0
    engagement_closeness_delta_max: float = 0.04
    engagement_absence_curiosity_enabled: bool = True
    engagement_absence_curiosity_min_seconds: float = 1800.0
    # When ``True`` (default), the typed-proactive eligibility check
    # treats an ``"abandoned"`` engagement label as a hard reason to
    # skip the silence-break nudge. Set to ``False`` to ignore the
    # engagement label on the proactive path (the typed nudge then
    # falls back to the legacy cooldown / presence / vent gates only).
    engagement_proactive_gate: bool = True

    # ── K5: mood shell tilt ──────────────────────────────────────────
    # Per-turn one-line emotional directive derived from the live
    # :class:`AffectState` (valence + arousal) and
    # :class:`RelationshipAxesState` (closeness/humor/trust/comfort).
    # NOT a topic suggestion -- a tonal register cue that colours
    # delivery only (pacing, word choice, sentence length, warmth).
    # Returns ``""`` on the common turn; only fires when affect is
    # off-baseline AND/OR a relationship axis crosses
    # ``mood_shell_axis_threshold`` (default 0.5, mirrors the existing
    # ``relationship_axes._NOTABLE_THRESHOLD``). Part of the K16
    # ``replace`` suppression set (the unified grounding line folds
    # the same surface area). See [`app/core/affect/mood_shell.py`](mood_shell.py).
    mood_shell_enabled: bool = True
    mood_shell_axis_threshold: float = 0.5

    # ── K17: clarification-repair detector ────────────────────────────
    # Per-turn regex classifier that fires when Jacob signals he was
    # misunderstood ("no that's not what I meant", "huh?", "wait
    # what"). The post-turn flow stashes a one-shot result and the
    # next-turn inner-life provider renders a "Heads-up: you missed
    # his last point" cue so Aiko re-reads, owns it, and answers
    # what was actually asked. No LLM cold path; the regex hot path
    # is the whole detector. Two bands -- ``strong`` (explicit
    # correction) vs ``mild`` (soft confusion). See
    # [`app/core/conversation/clarification_detector.py`](clarification_detector.py).
    clarification_repair_enabled: bool = True

    # ── K8: affect rupture-and-repair ─────────────────────────────────
    # Per-turn detector that fires when {user_name}'s valence drops
    # by more than ``rupture_valence_drop_threshold`` between the
    # pre-turn affect snapshot and the post-turn AffectUpdater
    # result, *and* Aiko's just-emitted reaction wasn't already an
    # empathetic one (concerned/gentle/sad/calm -- those would
    # trigger false positives because Aiko was responding to
    # existing bad news, not causing it). The post-turn flow
    # stashes a one-shot result on the controller; the next turn's
    # inner-life provider renders a "Heads-up: their mood just
    # dipped right after your last reply" cue so Aiko softens and
    # checks in once. See
    # [`app/core/affect/affect_rupture_detector.py`](affect_rupture_detector.py).
    rupture_repair_enabled: bool = True
    rupture_valence_drop_threshold: float = 0.12

    # ── K37: emotional contagion ──────────────────────────────────────
    # Aiko's affect tilts a small, capped amount toward the user's
    # estimated affect each turn (separate from how it reacts to her own
    # ``[[reaction:...]]``). ``contagion_strength`` is the fraction of
    # the valence/arousal gap closed per turn; ``contagion_max_per_turn``
    # is the hard per-axis ceiling on that move, so a big mismatch can
    # only ever pull her this far in one turn. See
    # [`app/core/affect/affect_state.py`](affect_state.py)
    # (``estimate_user_affect`` + ``_apply_user_contagion``).
    contagion_enabled: bool = True
    contagion_strength: float = 0.15
    contagion_max_per_turn: float = 0.05

    # ── K45: mood inertia (instant face, lagging heart) ───────────────
    # Master switch for the one-shot "your face jumped to X but
    # underneath you're still Y — let the words catch up" cue armed
    # post-turn when the fresh ``[[reaction:...]]`` tag's implied
    # affect target strongly outruns the smoothed AffectState.
    # Thresholds + cooldown live on ``MemorySettings.mood_inertia_*``;
    # the avatar-side damping flag is ``AvatarSettings
    # .mood_inertia_damping``. See
    # [`app/core/affect/mood_inertia.py`](mood_inertia.py).
    mood_inertia_enabled: bool = True

    # ── K23: subtle misattunement detection ──────────────────────────
    # Per-turn detector that fires ``mild_disengagement`` when {user}
    # goes very short or pivots topics right after a substantial Aiko
    # reply. Sits in the gap between K17 (explicit "that's not what I
    # meant" regex) and K14 (multi-turn engagement aggregate). The
    # cue lands on the SAME turn that's about to reply -- pulling
    # back IS the next response.
    #
    # Two trigger paths, both gated by the cooldown:
    #
    # 1. ``shrink``: ``prev_aiko_words >= shrink_min_prev_words``
    #    AND ``this_user_words <= shrink_max_user_words``. A one-word
    #    reply after a 60-word answer reads as "you went quiet".
    # 2. ``pivot``: K6 :class:`NoveltyDetector` band is
    #    ``strong_novelty`` AND ``this_user_words <=
    #    pivot_max_user_words``. A short pivot away without engaging
    #    Aiko's last point.
    #
    # Cooldown lives on :class:`SessionController` and counts down
    # one per turn regardless of trigger state. Default ``3`` keeps
    # the cue from stacking across consecutive disengaged turns
    # (the conditions can persist when {user} is genuinely busy).
    #
    # See
    # [`app/core/affect/misattunement_detector.py`](../affect/misattunement_detector.py).
    misattunement_detection_enabled: bool = True
    misattunement_shrink_min_prev_words: int = 30
    misattunement_shrink_max_user_words: int = 8
    misattunement_pivot_max_user_words: int = 8
    misattunement_cooldown_turns: int = 3

    # ── K30: self-noticing cues (agreement / flat-affect / repeated) ──
    # K20 metacognitive calibration tracks {user}'s trust in Aiko;
    # K30 is the symmetric loop -- Aiko notices HER own patterns.
    # One master switch fans into three sub-detectors that can be
    # toggled independently while tuning:
    #
    # * ``self_noticing_agreement_streak_enabled`` -- per-provider
    #   call regex over the last ``self_noticing_window`` rendered
    #   assistant replies (SQLite round-trip, K23-style). Fires when
    #   the agreement-token share crosses
    #   ``self_noticing_agreement_threshold`` AND pushback count
    #   sits at or below ``self_noticing_max_pushback``.
    # * ``self_noticing_flat_affect_enabled`` -- reads a small
    #   in-memory ``(valence, arousal, reaction)`` ring populated
    #   post-turn (there's no ring on ``AffectState`` itself). Fires
    #   when both scalar ranges sit at or below their thresholds AND
    #   no reaction outside ``LOW_BAND_REACTIONS`` fired in the
    #   window.
    # * ``self_noticing_repeated_thought_enabled`` -- post-turn
    #   cosine pass on Aiko's just-finished reply against a tiny
    #   embedding ring (last 3 assistant vectors, reusing K22's
    #   synchronous ``turn_vec`` -- no extra embed call). Fires
    #   when ``max_cosine >= self_noticing_repeated_cosine_threshold``;
    #   the cue surfaces on the NEXT turn (one-shot carry-forward
    #   flag), matching v1's detect-and-log discipline.
    #
    # ``self_noticing_cooldown_turns`` arms after the streak
    # detectors fire so the same Heads-up doesn't re-stack for the
    # next several turns. Repeated-thought has no multi-turn
    # cooldown -- the carry-forward flag is naturally one-shot.
    # See
    # [`app/core/affect/self_pattern_detector.py`](../affect/self_pattern_detector.py).
    self_noticing_enabled: bool = True
    self_noticing_agreement_streak_enabled: bool = True
    self_noticing_flat_affect_enabled: bool = True
    self_noticing_repeated_thought_enabled: bool = True
    self_noticing_window: int = 6
    self_noticing_warmup: int = 4
    self_noticing_agreement_threshold: float = 0.80
    self_noticing_max_pushback: int = 0
    self_noticing_flat_valence_range: float = 0.10
    self_noticing_flat_arousal_range: float = 0.10
    self_noticing_repeated_cosine_threshold: float = 0.85
    self_noticing_cooldown_turns: int = 5

    # ── K27: daily personality colour (Aiko's day) ────────────────────
    # Master switch for the slow ambient colour rolled once per local
    # day from the 10-entry palette in
    # [`app/core/affect/day_color.py`](../affect/day_color.py).
    # When off, the inner-life block short-circuits to ``""`` and the
    # :class:`DayColorWorker` skips its tick -- no roll, no read.
    #
    # K27 sits between two adjacent layers:
    #
    # * K5 mood-shell tilt is *reactive* and decays toward baseline;
    #   K27 is the slow under-current K5 reacts on top of.
    # * K30 self-noticing flat-affect detects when Aiko's session
    #   has gone flat; K27 gives her a non-flat starting point so
    #   the K30 measurement actually means "she's slipped" rather
    #   than "she has no colour to begin with".
    #
    # The :class:`DayColorWorker` is the canonical path (runs every
    # ``day_color_check_interval_seconds`` and only writes when the
    # local date has rolled over). The provider has a cheap lazy
    # fallback for the first-turn-after-midnight case when the
    # idle-worker hasn't fired yet.
    day_color_enabled: bool = True
    # Cadence of the idle-worker tick. Defaults to 1h (3600s) -- the
    # tick is cheap (one kv_get + one date compare) so a tighter
    # cadence has negligible cost. Floored at 60s in ``_parse_agent``
    # so a buggy override can't spin the scheduler.
    day_color_check_interval_seconds: int = 3600

    # ── K15: self-disclosure / vulnerability budget ───────────────────
    # Master switch for the rolling token-bucket that paces Aiko's
    # personal disclosures (``[[remember:self:...]]`` tags). When off,
    # the post-turn spend hook is a no-op and the provider returns
    # ``""`` -- no kv_meta writes, no prompt cue.
    #
    # K15 sits between two adjacent layers:
    #
    # * K27 day_color is the slow weather (stable for the day).
    # * The relationship-axes / shared-moments system tracks
    #   closeness + trust which K15 reads at provider time to size
    #   the bucket capacity.
    #
    # Soft enforcement only: the cue surfaces in the prompt but
    # never blocks the reply or suppresses the underlying memory
    # write. The persona block teaches Aiko to read the cue but
    # explicitly allows real moments to override -- the budget is
    # pacing, not a rule.
    vulnerability_budget_enabled: bool = True
    # Capacity floor when closeness + trust are both deeply negative
    # (or at first-boot defaults). Min 1 so the bucket math always
    # has a non-zero divisor.
    vulnerability_budget_min_capacity: int = 1
    # Capacity ceiling when closeness + trust are both at +1. 12 is
    # roughly "four tier-3 disclosures or twelve tier-1 surface
    # taste lines in one session before the cue starts firing".
    vulnerability_budget_max_capacity: int = 12
    # Bucket regeneration rate in tokens / hour. Default 0.5 means
    # a full max-cap bucket (12 tokens) refills in ~24h; a single
    # tier-3 spend (6 tokens) regenerates in ~12h. Tuned so a real
    # soft moment from yesterday is mostly recovered today.
    vulnerability_budget_regen_per_hour: float = 0.5
    # Per-tier costs. Tier 1 = surface preference, tier 2 = mild
    # admission, tier 3 = genuine softness. The 1 / 3 / 6 ladder
    # means three tier-1 lines cost the same as one tier-2, and
    # two tier-2 lines cost the same as one tier-3.
    vulnerability_budget_tier1_cost: int = 1
    vulnerability_budget_tier2_cost: int = 3
    vulnerability_budget_tier3_cost: int = 6

    # ── K31 + K32: soft physicality (touch + reactions) ───────────────
    # Master switch for the K31 ``[[touch:KIND]]`` tag family. When
    # off, the streaming parser silently drops touch tags before they
    # reach the avatar or the bubble badge; ``TouchService`` is still
    # constructed (so the persisted state survives a settings flap)
    # but ``try_dispatch`` always returns ``dispatched=False,
    # reason="disabled"``.
    touch_enabled: bool = True
    # Per-kind override map, e.g.
    # ``{"hug": {"cooldown_seconds": 300, "daily_cap": 6}}``. Lets
    # users adjust the cadence without code changes; unknown fields
    # or unknown kinds are silently ignored. Falls back to the
    # taxonomy defaults in :data:`app.core.touch.touch_gestures`.
    touch_per_kind_overrides: dict[str, Any] = field(default_factory=dict)

    # ── K10 persona regression (on-demand golden-turn eval) ───────────
    # Master switch for the persona-drift harness. When off,
    # ``run_persona_regression()`` is a no-op returning an empty snapshot
    # and the Diagnostics panel shows a disabled state. Purely on-demand
    # (MCP tool / "Run check" button / pytest); no background spend.
    persona_regression_enabled: bool = True
    # JSONL fixture of canonical "golden turns" to replay. Relative to
    # the working directory; ships beside the persona sheet.
    persona_regression_fixture_path: str = "data/persona/golden_turns.jsonl"

    # ── Brain orchestration: long-running tasks (schema v16) ──────────
    # Master switch for the whole task subsystem. Off disables the
    # ``start_*`` tools, the ``TaskOrchestrator`` rejects spawns, and
    # the cue / escalation paths stay silent. See
    # :mod:`app.core.tasks` and ``docs/brain-orchestration.md``.
    tasks_enabled: bool = True
    # Max concurrent ``running`` + ``awaiting_input`` rows per user.
    # ``TaskOrchestrator.start_task`` rejects with
    # ``reason=per_user_cap`` past this. Tuning up = more parallel
    # tasks per user (and more memory + WS chatter). Tuning down =
    # tighter back-pressure on long-running work.
    tasks_per_user_cap: int = 8
    # When True, non-terminal task rows surviving a restart get
    # surfaced to Aiko as a one-line cue on her next turn ("the X
    # task stopped when we last talked -- want me to retry?"). Off
    # silently demotes interrupted rows without prompting Aiko.
    # Implemented by ``recover_interrupted_tasks`` in
    # ``app/core/tasks/recovery.py``.
    tasks_resume_on_boot: bool = True
    # When True, ``InnerLifeProvidersMixin._render_running_tasks_block``
    # renders a T6 block listing live tasks for the active user. Off
    # hides the block entirely (Aiko has no inner-prompt awareness of
    # her own running work; only the TaskStrip in the UI does).
    tasks_running_block_enabled: bool = True
    # ``BrainLoop`` deferred-event poll interval in milliseconds.
    # Smaller = deferred items retry sooner when the free-to-speak
    # gate clears (lower latency on the no-interrupt invariant), but
    # the consumer thread wakes more often on idle. Clamped to
    # ``[10, 5000]``. Default 100 = a tenth of a second.
    brain_loop_deferred_grace_ms: int = 100
    # Wall-clock age (in seconds) above which a parked cue is
    # silently dropped on the next dequeue / sweep. Protects against
    # awkward stale-context messages ("the YouTube tab I opened 3
    # hours ago is still going") if the user vanished. Clamped to
    # ``[60, 86400]``. Default ``1800`` = 30 minutes.
    task_cue_max_age_seconds: int = 1800
    # Hard cap on cues rendered into a single turn's prompt T6 block.
    # Excess cues stay in the DB / WS strip so the user sees them,
    # but get dropped from the prompt to keep T6 cheap (the most
    # volatile tier, no cache hits). Clamped to ``[1, 20]``.
    task_cue_max_aggregated: int = 5
    # ── Duration-hybrid task replies (fold-fast + reply-on-complete) ──
    # Master switch for the reply-on-complete behaviour. When True the
    # ``start_file_*`` tools fold a fast result into the same turn and
    # flag slower tasks ``reply_when_done`` so their result is rendered
    # in full (not a terse bullet) when it surfaces. Off = legacy
    # behaviour (terse cue only, no inline fast fold).
    task_reply_on_complete_enabled: bool = True
    # How long a ``start_file_read`` / ``start_file_search`` call blocks
    # waiting for the handler to finish so the result can be folded into
    # the SAME reply (the "fast" half of the duration hybrid). Tasks
    # that don't finish in this window fall back to the reply-on-complete
    # path. Clamped to ``[0, 30]``; 0 disables the inline fast path.
    task_inline_grace_seconds: float = 3.0
    # ── C6: worker-model task-report decision ──────────────────────────
    # Master switch for the worker-LLM decision that runs when a
    # reportable background task finishes. Decides surface_now / park /
    # drop and drafts a short "angle" framing hint the chat model uses to
    # compose the report. When False the legacy binary park+arm path runs
    # for every ``notify_aiko=True`` task (behaviour before C6).
    task_report_decision_enabled: bool = True
    # How the decision treats user-requested tasks (the always-report
    # floor). ``shadow`` keeps the hard floor (park+arm immediately) and
    # only logs the verdict the worker WOULD have produced, plus enriches
    # the cue with the drafted angle — use this to evaluate the worker
    # before trusting it. ``enforce`` makes the verdict authoritative for
    # floor tasks too. Unknown values fall back to ``shadow``.
    task_report_decision_floor_mode: str = "shadow"
    # Whether to enrich parked report cues with the worker-drafted angle
    # hint (rendered as a private ``(angle: …)`` suffix in the T6 cue
    # block; the chat model phrases the actual report). Applies to both
    # the shadow-floor and discretionary tiers.
    task_report_angle_enabled: bool = True
    # Configured roots for the read-only filesystem task handlers
    # (``file_search`` / ``file_read``). Each entry is a dict with
    # ``label`` (human-readable id used in path prefixes like
    # ``"Documents:notes.md"``), ``path`` (absolute or relative to
    # the app root), and an optional ``read_only`` flag reserved
    # for phase 2. Empty default = no filesystem access; the
    # handlers run but every resolve returns ``no_match``. Validate
    # at boot via :func:`app.core.tasks.sandbox.validate_roots`;
    # missing / wrong-type roots get a WARNING but stay in the
    # list so a temporarily-unmounted external drive doesn't auto-
    # disappear from the config. See ``docs/brain-orchestration.md``.
    task_file_allowed_roots: tuple[dict[str, Any], ...] = ()
    # When ``False``, the built-in workflow file skills (``file_search`` /
    # ``read_file`` / ``write_file``) are not offered to the planner.
    # Intended for users who handle files exclusively through a filesystem
    # MCP server: removes the built-in-vs-MCP overlap (two path
    # conventions for the same directory) that makes the planner hand a
    # label/relative path to an MCP file tool and get "path outside
    # allowed directories". Default ``True`` (built-ins on).
    builtin_file_skills_enabled: bool = True
    # ── Chunk 12: file_read handler safety caps ────────────────────────
    # ``FileReadHandler`` is the first phase-1 handler that emits a
    # ``TaskInputNeeded`` (multi-root disambiguation: a bare path that
    # matches in more than one configured root). It also opens and
    # reads file contents, so a small set of safety caps gate what
    # actually reaches the LLM as a tool result.
    #
    # ``task_file_read_max_bytes`` — hard cap on bytes read off disk
    # per call. Files larger than this are truncated at the byte
    # boundary and the result row sets ``truncated=True``. Default
    # 256 KiB — big enough for a Markdown doc, small enough that a
    # rogue 4 GB log can't OOM Aiko's process.
    task_file_read_max_bytes: int = 262144
    # ``task_file_read_max_lines`` — secondary cap applied after the
    # byte read so a 256 KiB single-line minified blob can still be
    # rejected. Default 2000 lines.
    task_file_read_max_lines: int = 2000
    # ``task_file_read_allowed_extensions`` — case-insensitive
    # extension allow-list. Empty tuple = "allow everything that
    # passes the magic-byte text check". When non-empty, anything
    # outside the list is rejected up-front (the magic-byte check
    # still runs as a secondary filter). Defaults to a sensible
    # text-only catalogue so the LLM can't accidentally read a PDF
    # or a database file.
    task_file_read_allowed_extensions: tuple[str, ...] = (
        ".txt", ".md", ".rst", ".log",
        ".py", ".js", ".ts", ".tsx", ".jsx",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".html", ".css", ".xml",
        ".csv", ".tsv",
        ".sh", ".bat", ".ps1",
        ".sql",
        ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".java", ".kt",
        ".rb", ".lua",
    )
    # ── External MCP-server clients ────────────────────────────────────
    # Master switch for connecting to the external MCP servers configured
    # under ``mcp_clients.servers``. When off (or no servers configured),
    # the manager never starts and no MCP tools are registered. MCP tools
    # are surfaced only to the background-worker (workflow planner) lane,
    # so this is only meaningful when ``workflow_enabled`` is also true.
    mcp_clients_enabled: bool = True
    # ── Nested goal workflows ──────────────────────────────────────────
    # Master switch for the ``GoalWorkflowHandler`` + ``start_workflow``
    # brain tool. When off, ``start_workflow`` is not registered and the
    # workflow handler is never built; the fast-lane file tools still work.
    workflow_enabled: bool = True
    # Hard cap on planner iterations (plan->act->observe cycles) before a
    # workflow force-finishes. Bounds runaway loops. Clamped ``[1, 30]``.
    workflow_max_iterations: int = 6
    # Hard cap on child tasks a single workflow may spawn. Clamped
    # ``[1, 50]``.
    workflow_max_children: int = 8
    # Max number of workflows that may run concurrently per user.
    # ``start_workflow`` refuses past this. Clamped ``[1, 8]``.
    workflow_max_concurrent: int = 2
    # Char budget for the planner blackboard (short observations folded
    # back each iteration). Clamped ``[500, 20000]``.
    workflow_planner_history_budget_chars: int = 4000
    # Separate, larger char budget for the final aggregated reply
    # (fuller child content, not the 200-char observations). Clamped
    # ``[1000, 40000]``.
    workflow_reply_budget_chars: int = 6000
    # Max seconds the planner waits on a single child task to reach a
    # terminal state before treating it as timed out. Clamped ``[5, 600]``.
    workflow_child_wait_timeout_seconds: int = 120
    # ``num_predict`` for the planner's JSON decision call. Small — the
    # planner only emits a tiny ``{action, args, reason}`` object.
    # Clamped ``[64, 2048]``.
    workflow_planner_max_tokens: int = 512
    # Circuit breaker: stop a workflow after this many child steps fail /
    # time out *in a row* (a success resets the counter). Catches the
    # "service unavailable" loop the exact-(skill,args) repeat guard
    # misses — e.g. a browser workflow when Chrome / the extension isn't
    # running, where every varied call fails. Clamped ``[1, 20]``.
    workflow_max_consecutive_failures: int = 2
    # Wall-clock budget for a whole workflow loop, in seconds. The loop
    # force-finishes (partial) once exceeded so piled-up slow timeouts
    # can't run for many minutes. ``0`` disables. Clamped ``[0, 3600]``.
    workflow_max_wall_seconds: int = 300
    # Cap on the per-task capability-gap log (missing_capability entries).
    # Clamped ``[1, 500]``.
    workflow_capability_gap_log_max: int = 50
    # ── Task approvals (reusable across destructive capabilities) ───────
    # Generic approval policy shared by every destructive task
    # capability (today: ``file_write``; later: shell exec / http post /
    # send email). ``task_approval_mode`` is the global default —
    # ``"ask"`` gates every destructive action behind a TaskStrip
    # approval prompt, ``"auto"`` performs without asking. Per-capability
    # overrides live in ``task_approval_overrides`` (e.g.
    # ``{"file_write": "auto"}`` to stop asking for writes only). A
    # session "approve all" click (handled in-memory by the controller)
    # rides on top of both — it never persists. See
    # :mod:`app.core.tasks.approval` + ``docs/task-approvals.md``.
    task_approval_mode: str = "ask"
    task_approval_overrides: dict[str, str] = field(default_factory=dict)
    # ── file_write capability resource config ───────────────────────────
    # Nested per-capability block (master switch + byte cap + extension
    # allow-list). The destructive-write APPROVAL is governed by the
    # generic ``task_approval_*`` fields above, not here.
    file_write: FileWriteSettings = field(default_factory=FileWriteSettings)
    # ── vision (describe_image) capability resource config ───────────────
    # Reuses the worker model; ``model`` empty = inherit the effective
    # worker model. Master switch gates the describe_image workflow skill.
    vision: VisionSettings = field(default_factory=VisionSettings)
    # ── Worker-LLM priority gate ────────────────────────────────────────
    # Master switch for the priority gate in front of the shared worker
    # Ollama client. Off = pass-through proxies (zero behaviour change).
    worker_llm_gate_enabled: bool = True
    # Concurrency bound on the worker model. Default 1 (a 30B on one GPU
    # serialises anyway). Clamped ``[1, 8]``.
    worker_llm_max_concurrency: int = 1
    # Optional per-consumer tier overrides: maps the proxy name
    # (``"conversation"`` / ``"maintenance"`` / ``"task"``) to a tier
    # name, letting any consumer be nudged up/down without code.
    worker_llm_priority_overrides: dict[str, str] = field(default_factory=dict)
    # Master switch for the K32 user-reaction tray. When off, the
    # REST endpoints reject with 503 and the inner-life cue stays
    # silent. The frontend hides the hover tray when the connection
    # advertises the feature as disabled.
    user_reactions_enabled: bool = True
    # When True, every K32 reaction click also bumps relationship
    # axes via :meth:`RelationshipAxesUpdater.apply_user_reaction`.
    # Off lets you keep the cue + persistence without moving the
    # axes (useful for debugging or for users who don't want the
    # relationship signal to ride on a UI affordance).
    user_reactions_axes_enabled: bool = True
    # Cumulative absolute axis-movement cap per axis per UTC day,
    # from reactions only. Tuned so 4-5 reactions in a session feels
    # meaningful without grinding closeness to +1 from clicks alone.
    # Implementation in
    # :func:`app.core.relationship.user_reactions.apply_daily_cap`.
    user_reactions_daily_axis_cap: float = 0.15
    # Master switch for the persona-mode action banner (the small
    # transient surface near the avatar in the Tauri overlay window
    # that shows what Aiko just did + the reaction tray). Off hides
    # the banner entirely in the persona webview; the underlying
    # avatar animation still plays.
    persona_touch_banner_enabled: bool = True
    # Visible duration (seconds) of the persona banner. Clamped to
    # ``[1, 120]`` in ``_parse_agent`` so a typo can't pin the
    # banner permanently. Default 20s -- long enough for a glance
    # + a reaction click, short enough not to clutter the overlay.
    persona_touch_banner_duration_seconds: int = 20
    # Chunk 15 (brain orchestration): master switch for the
    # ``PersonaTaskBanner`` -- the persona-window mirror of the
    # ``TaskStrip`` chip in the main chat. Surfaces an
    # ``awaiting_input`` task as a transient pill near the avatar
    # so the user can click an option (or type a free-text answer)
    # without switching back to the chat window. The banner never
    # cancels the underlying task on dismiss; it only hides the
    # surface so the chat-channel answer path still works. Off
    # hides the banner entirely; the strip in the chat window is
    # unaffected.
    persona_task_banner_enabled: bool = True

    # ── Brain orchestration phase 2 (schema v17): lifecycle safety ────
    # Sweep interval for the in-process heartbeat zombie detector
    # (:class:`HeartbeatChecker`). The detector wakes every N seconds,
    # asks the task store for ``status='running'`` rows whose
    # ``heartbeat_at`` is older than :attr:`task_stalled_seconds`, and
    # either logs a WARNING or moves them to ``failed`` depending on
    # :attr:`task_stalled_action`. Clamped to ``[5, 3600]`` in
    # :func:`_parse_agent` so a typo can't either spin the CPU or
    # silently disable the sweep.
    task_heartbeat_check_interval_seconds: int = 30
    # Wall-clock age above which a ``running`` row is considered
    # stalled. The orchestrator bumps ``heartbeat_at`` on every emit
    # so a healthy handler comfortably stays under this threshold.
    # Tune up for long-running, low-emit handlers (e.g. a research
    # task that spends 10 minutes inside one network call); tune down
    # for the agent-y workloads where 5-minute silence is itself a
    # failure signal. Clamped to ``[60, 86400]``.
    task_stalled_seconds: int = 300
    # What :class:`HeartbeatChecker` does with stalled rows. ``"warn"``
    # logs a WARNING + appends an ``EVENT_HEARTBEAT_STALLED`` event
    # but leaves the row running; ``"fail"`` additionally promotes
    # the row to ``failed`` with a "stalled" error. Default is the
    # conservative ``"warn"`` so an aggressive threshold can't kill
    # legitimate slow handlers. See
    # :class:`app.core.tasks.task_heartbeat.HeartbeatChecker`.
    task_stalled_action: str = "warn"
    # Cascade-cancel toggle. When True (the default),
    # :meth:`TaskOrchestrator.cancel` recursively cancels every
    # active child in the task tree. Off keeps the legacy phase-1
    # behaviour (cancel only the named row; children keep running
    # until they emit a terminal outcome themselves).
    task_cascade_cancel_children: bool = True
    # Wall-clock retention window for terminal task rows. The
    # :class:`TaskCleanupWorker` deletes terminal rows whose
    # ``completed_at`` is older than this. Cascade-deletes the
    # associated event log + input history. Clamped to
    # ``[1, 3650]`` so the cleanup never accidentally targets
    # rows that just finished, and never proposes "retain forever".
    task_cleanup_retention_days: int = 30
    # How often the cleanup worker runs (idle scheduler tick gating
    # applies on top). Default 6h. Clamped to ``[600, 604800]``
    # (10 minutes to a week).
    task_cleanup_interval_seconds: int = 21600

    # ── K29: opinion injection (push back when she has a stance) ──────
    # Master switch for the per-turn detector that fires a one-line
    # cue when {user_name}'s latest message contradicts one of Aiko's
    # stored ``kind="self"`` stance memories. The whole feature exists
    # to make the persona's "have opinions, disagree when you
    # disagree" claim actually fire against LLM RLHF agreeability --
    # without flipping into contrarianism.
    #
    # Anti-contrarianism is layered: only opinion-shaped stance
    # memories qualify (predicate filter), only ``definite`` heuristic
    # verdicts and (when budget allows) borderline+LLM-YES verdicts
    # fire, and a hard per-session cap bounds the worst case. See
    # [`app/core/affect/opinion_injection_detector.py`](../affect/opinion_injection_detector.py).
    #
    # ``require_definite=True`` is the strictest no-LLM-cost
    # configuration (Path C in the design plan); leave at ``False``
    # (Path B, the default) for the heuristic + LLM-gated borderline
    # behaviour.
    opinion_injection_enabled: bool = True
    opinion_injection_require_definite: bool = False

    # ── K28: "What I've been turning over" ────────────────────────────
    # Master switch for the between-session reflection-surfacing cue.
    # Off → no turning-over block ever lands in the prompt. On (default)
    # → the post-turn pipeline arms ``_pending_turning_over_seconds``
    # whenever a typed turn lands after a gap of at least
    # ``memory.turning_over_min_gap_minutes`` (default 90 min), and the
    # next prompt assembly runs the picker
    # (:mod:`app.core.session.inner_life.turning_over`). The picker is
    # silent when no recent ``reflection`` memory clears the topical
    # match, so the cue stays rare even with the switch on. See
    # [`app/core/session/inner_life/turning_over.py`](../session/inner_life/turning_over.py).
    turning_over_enabled: bool = True

    # ── K36: "things I did while you were away" ───────────────────────
    # Master switch for the idle-activity producer + its surfacing cue.
    # Off → the IdleAwayActivityWorker never registers and the
    # away-activities prompt block never lands. On (default) → the worker
    # gives Aiko a small autonomous room life during quiet windows
    # (sip the tea, read a book, move the cat, …) and the first turn
    # after a long typed gap may surface one casual line about it. The
    # cadence + gap knobs live on ``MemorySettings.away_activities_*``.
    away_activities_enabled: bool = True

    # ── K34: "forward curiosity" ──────────────────────────────────────
    # Master switch for the forward-question producer + its surfacing
    # cue. Off → the ForwardCuriosityWorker never registers and the
    # forward-curiosity prompt block never lands. On (default) → during
    # quiet windows Aiko drafts a genuine "I've been wondering ..."
    # question about the user's life (from their future_plan / callback
    # memories, biased by K3 routines) and the first turn after a long
    # typed gap may surface one. Cadence + gap knobs live on
    # ``MemorySettings.forward_curiosity_*``.
    forward_curiosity_enabled: bool = True

    # FollowUpWorker master switch. When a user-mentioned future_plan's
    # event time passes, the worker drafts a private "you can ask how it
    # went" cue into the ``aiko.follow_up_cues`` kv ring and the
    # ``_render_follow_up_block`` provider surfaces it on the next turn.
    # Off = no proactive follow-up cue (the retrieval-tag path still
    # lets Aiko ask retrospectively when the memory surfaces).
    follow_up_enabled: bool = True

    # ── K43: promise follow-through ───────────────────────────────────
    # Master switch for the promise lifecycle + follow-through cue. When
    # ON, assistant-side ``kind="promise"`` memories carry an
    # open → surfaced → fulfilled | dropped state machine: the
    # PromiseFollowthroughWorker arms a one-shot "you said you'd look
    # into X — close the loop (or own that you haven't)" cue during
    # quiet windows, the post-turn hook auto-fulfils promises Aiko's
    # reply delivered on, and finished background tasks auto-fulfil
    # matching promises. Off → no cue, no lifecycle writes. Cadence +
    # age knobs live on ``MemorySettings.promise_followthrough_*``.
    promise_followthrough_enabled: bool = True

    # ── K38: self-correction cue ──────────────────────────────────────
    # Master switch for the next-turn self-correction cue. When ON, a
    # post-turn lexical detector checks whether Aiko's just-finished
    # reply contradicted one of her own high-confidence fact/preference
    # memories and, if so, arms a one-shot cue so she owns the slip on
    # her next turn. Thresholds + cooldown live on
    # ``MemorySettings.self_correction_*``.
    self_correction_enabled: bool = True

    # ── K25: memory confidence time-decay ─────────────────────────────
    # Master switch for the ``(distant)`` suffix the RAG retriever
    # stamps on age-decayed memory rows. The three numeric knobs that
    # govern the decay formula and threshold live on
    # :class:`MemorySettings` (``confidence_decay_horizon_days``,
    # ``confidence_decay_floor``, ``confidence_decay_distant_threshold``)
    # because they describe a memory-store concept; only the on/off
    # gate lives here so it sits alongside the rest of the per-feature
    # master switches. Flipping ``False`` disables the ``(distant)``
    # suffix entirely — ``_confidence_penalty`` still reads stored
    # confidence for the score offset, K7 ``(faded)`` still fires,
    # ``(uncertain)`` still fires.
    confidence_time_decay_enabled: bool = True

    # ── K22: callback / inside-joke detector ──────────────────────────
    # Master switch for the post-turn cosine pass that detects when
    # Aiko's reply semantically reaches back to an older eligible
    # memory and stamps ``metadata.callback_count``. Off → no rows
    # gain new callback stamps. The retriever's read-side bonus on
    # rows already stamped stays on either way, so flipping this off
    # freezes the loop without losing earned weight. Knob detail
    # lives on :class:`MemorySettings` (``callback_*`` fields). See
    # [`app/core/conversation/callback_detector.py`](callback_detector.py).
    callback_detector_enabled: bool = True

    # ── K20: metacognitive calibration detector ────────────────────────
    # Master switch for the post-turn classifier that detects
    # Jacob's calibration signal toward Aiko's claims (pushback /
    # softening / affirmation) and writes per-user
    # CalibrationState. Off → no new calibration updates; the
    # inner-life provider also goes silent because
    # ``_render_calibration_block`` short-circuits on this flag. Knob
    # detail lives on :class:`MemorySettings` (``calibration_*``
    # fields). See [`app/core/affect/calibration_detector.py`](calibration_detector.py).
    calibration_detection_enabled: bool = True

    # ── K24: sensory anchoring layer ──────────────────────────────────
    # Master switch for the adaptive per-arc cadence that
    # occasionally surfaces a "small physical beat available" cue.
    # Off → ``_render_sensory_anchor_block`` short-circuits and no
    # beats are ever offered to Aiko. Knob detail lives on
    # :class:`MemorySettings` (``sensory_anchor_*`` fields). See
    # [`app/core/conversation/sensory_anchor.py`](sensory_anchor.py).
    sensory_anchor_enabled: bool = True

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
    # the "Aiko's running jokes with <user>:" inner-life block.
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
    # ── F2.1 personality backlog: knowledge-gap memory-match resolver ─
    # Companion to F1's web-search resolver. F1 closes a gap by going
    # to look the answer up; this worker closes it by noticing the
    # answer is already in the memory store (e.g. a ``preference`` row
    # written by the post-summary extractor after the user answered the
    # question in chat). Without this the same gap re-injects into the
    # prompt every session for weeks because nothing else marks it
    # resolved. See :class:`app.core.conversation.idle_gap_resolver.IdleGapResolver`.
    gap_resolver_enabled: bool = True
    # Cadence in seconds. The work is pure cosine over the in-memory
    # mirror, so it's cheap; 10 minutes is a "show up shortly after a
    # gap was minted" cadence without spamming logs on quiet stretches.
    gap_resolver_interval_seconds: int = 600
    # Cosine threshold for "this memory answers this gap." Slightly
    # stricter than the curiosity-seed resolve threshold (0.50) because
    # closing a gap is a stronger claim than consuming a seed: a false
    # positive here means a real open question gets buried, where a
    # seed false positive just means we skip a topic that came up once.
    gap_resolver_threshold: float = 0.55
    # Max gaps the worker resolves per tick. The journal cap is 20 and
    # the typical steady state is a handful of opens, so 5 per tick
    # drains a normal backlog within minutes without spiking CPU.
    gap_resolver_per_tick: int = 5
    # Cosine threshold for the post-turn user-answer resolver in
    # :meth:`PostTurnMixin._resolve_knowledge_gaps`. Mirrors the
    # ``curiosity_seed_resolve_threshold`` shape: the same combined
    # ``user_text + assistant_text`` embedding is reused, and any open
    # gap scoring at-or-above this is closed with
    # ``resolved_by="user_answer"`` in metadata. Lower than the worker
    # threshold because the post-turn check has stronger context (the
    # user *just* spoke about the topic) so false positives are rarer.
    gap_user_answer_resolve_threshold: float = 0.50


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
class MemorySettings:
    """Long-term memory: cross-session vector store of durable facts.

    Populated by background extraction after each summary, plus any
    ``[[remember:...]]`` tags Aiko emits inline.

    Schema v8 added tiered memory: ``scratchpad`` (fast decay, gets
    promoted to ``long_term`` when used or revived; deleted if never
    used), ``long_term`` (the default home), ``archive`` (decay ~ 0).
    The ``MemoryPromotionWorker`` shuffles rows between tiers on a
    configurable cadence; the ``MemoryDecayWorker`` applies
    wall-clock-driven decay so an intermittently-running desktop app
    still applies the right amount of decay on resume.
    """

    enabled: bool = True
    top_k: int = 6
    score_threshold: float = 0.4
    max_memories: int = 5000  # long_term cap
    dedupe_threshold: float = 0.92
    extractor_enabled: bool = True
    self_tagged_salience: float = 0.7

    # ── Schema v8: tier + decay + revival ────────────────────────────
    tiers_enabled: bool = True
    # Per-tier salience decay per day (applied proportionally to
    # elapsed wall-clock time -- running every hour applies 1/24 per
    # call). ``archive`` defaults to 0 so cold history doesn't fade.
    decay_rate_scratchpad: float = 0.05
    decay_rate_long_term: float = 0.02
    decay_rate_archive: float = 0.0
    # Revival mechanic. When Aiko's reply mentions enough keywords from
    # a surfaced memory, ``revival_score`` is bumped by
    # ``revival_per_hit``. Each decay tick applies a small rebate
    # proportional to revival_score (``revival_coefficient * elapsed``)
    # and then walks revival_score itself back down by
    # ``revival_decay_per_day * elapsed``. ``min_word_overlap`` controls
    # how strict the citation detection is.
    revival_coefficient: float = 0.05
    revival_per_hit: float = 0.15
    revival_decay_per_day: float = 0.02
    revival_min_word_overlap: int = 3
    # Promotion / demotion / cleanup gates used by
    # :class:`MemoryPromotionWorker`.
    scratchpad_ttl_days: int = 14
    scratchpad_promote_min_age_days: int = 7
    scratchpad_promote_min_use_count: int = 3
    scratchpad_promote_min_revival: float = 0.3
    archive_demote_idle_days: int = 180
    # Per-tier caps (long_term cap reuses ``max_memories`` above).
    scratchpad_cap: int = 1000
    archive_cap: int = 10000
    # Safety clamp on wall-clock catch-up: even if the app was offline
    # for months, decay won't try to apply more than this many days'
    # worth at once. Keeps the per-call magnitude bounded.
    decay_max_catchup_days: float = 30.0
    # ── K7 personality backlog: forgetting protocol ───────────────────
    # Master switch for the ``(faded)`` suffix appended by
    # :func:`app.core.rag.rag_retriever._is_faded_memory`. Flipping ``False``
    # disables every fade hedge — including the archive-tier suffix that
    # was the original K7 implementation — so users who'd rather Aiko
    # speak from memory without ever hedging "I think you said this
    # once, ages ago…" get a single clean kill switch. Default ON
    # because the persona rule already gates the hedge on "only when
    # the memory is actually load-bearing for your reply", so the
    # cosmetic cost of leaving it on is small.
    fade_hedge_enabled: bool = True
    # Salience floor for a long_term row to register as faded. Together
    # with ``faded_idle_days`` below, this picks up the
    # "decayed-in-place" window between freshly written and demoted-to-
    # archive. With the long_term decay rate of 0.02/day a fresh
    # salience-0.5 row hits the 0.20 threshold around day 15; combined
    # with the 30-day idle floor, only rows that genuinely haven't
    # surfaced in over a month qualify. Higher → only the very faded
    # rows hedge; lower → more aggressive hedging on lukewarm memories.
    # Archive-tier rows ignore this threshold and always fade (when
    # ``fade_hedge_enabled`` is on).
    faded_salience_threshold: float = 0.20
    # Minimum days since ``last_used_at`` (or ``created_at`` if a row
    # has never been touched) before a low-salience long_term row picks
    # up the ``(faded)`` suffix. The strict ``>`` semantics means a row
    # idle for exactly 30 days does NOT fade — that one-day buffer
    # prevents a row Aiko mentioned a month ago to the day from
    # flipping to hedged on the anniversary. Higher → only very stale
    # rows fade; lower → more aggressive hedging.
    faded_idle_days: int = 30
    # ── K25: memory confidence time-decay ─────────────────────────────
    # Read-side time-decay on memory confidence. Pure derived value at
    # ``format_block`` time — no schema change, no decay-writer. Each
    # retrieval recomputes ``effective_confidence = stored * max(floor,
    # 1 - days_since_created / horizon_days)``. Pinned rows bypass
    # (return stored as-is) since a pin reads as "the user explicitly
    # trusts this row". When ``effective_confidence`` falls below
    # ``confidence_decay_distant_threshold``, the retriever stamps the
    # row with ``(distant)`` — a third suffix distinct from
    # ``(uncertain)`` (low stored value) and ``(faded)`` (K7 tier +
    # idle). The persona maps each tag to a different verbal hedge:
    # ``(distant)`` → "a while back", "don't quote me" (time-flavoured),
    # ``(uncertain)`` → "I think", "if I'm remembering right"
    # (source-doubt), ``(faded)`` → "ages ago", "I might be wrong"
    # (cold-history). See
    # [`app/core/rag/rag_retriever.py`](../rag/rag_retriever.py)
    # ``_is_distant_memory``. Master switch lives on
    # :class:`AgentSettings` as ``confidence_time_decay_enabled``.
    #
    # Tuning rules:
    # * ``horizon_days`` — days at which the multiplier reaches
    #   ``floor``. Higher → slower decay, the hedge fires later in a
    #   memory's life.
    # * ``floor`` — minimum decay multiplier. Below ~0.1 the floor
    #   stops mattering (an old row's effective value is already
    #   below the threshold anyway); above ~0.5 the hedge effectively
    #   never fires on default-confidence rows.
    # * ``distant_threshold`` — effective confidence value below
    #   which the suffix fires. Mirrors the existing 0.5 cutoff used
    #   for ``(uncertain)``. Lower → only very-decayed claims hedge;
    #   higher → more hedging.
    confidence_decay_horizon_days: int = 365
    confidence_decay_floor: float = 0.3
    confidence_decay_distant_threshold: float = 0.5
    # ── K29 personality backlog: opinion injection numeric knobs ─────
    # The five numbers governing the K29 detector + caller plumbing.
    # The on/off / require-definite gates live on :class:`AgentSettings`
    # alongside the rest of the master switches; the rest of the
    # tunables describe a memory/retrieval concept so they sit here.
    #
    # * ``min_cosine`` — top-cosine floor between the live user
    #   message and a stance memory's embedding. Default ``0.55``
    #   matches K22 callback / K6 strong_novelty. Lower → easier
    #   topical match; higher → only near-exact topical brushes.
    # * ``min_user_words`` — short messages ("ok", "yeah", "lol")
    #   are K23 territory and never claim a contradiction. Default
    #   ``4`` words.
    # * ``cooldown_turns`` — turns between fires. Longer than K23
    #   (3 turns) because a stance disagreement is a heavier
    #   conversational beat than a soft-drift cue. Default ``5``.
    # * ``per_session_cap`` — hard cap per session. Five
    #   contradictions in a single session almost certainly means
    #   the detector is misfiring; the cap silently suppresses
    #   the rest. Default ``3``.
    # * ``per_hour_cap`` / ``per_day_cap`` — LLM-gate budgets for
    #   the borderline path. The detector only spends an LLM call
    #   when the heuristic says ``borderline`` and the limiter has
    #   tokens. Matches the F5 conflict-detector defaults.
    opinion_injection_min_cosine: float = 0.55
    opinion_injection_min_user_words: int = 4
    opinion_injection_cooldown_turns: int = 5
    opinion_injection_per_session_cap: int = 3
    opinion_injection_per_hour_cap: int = 6
    opinion_injection_per_day_cap: int = 30

    # ── K28 personality backlog: turning-over picker ─────────────────
    # The "What I've been turning over" cue (see ``AgentSettings.
    # turning_over_enabled`` for the master switch) only arms when
    # the gap between Aiko's last reply and the current user message
    # is at least this long. The default (90 min) sits inside K14's
    # absence-curiosity band [30 min, 4h) by design -- the two cues
    # stack: K14 frames the welcome-back, K28 adds "...and I was
    # thinking about X". Clamped to ``>= 5`` so a misconfiguration
    # can't make the cue fire on every typed turn. Voice-mode turns
    # never arm K28 (same gating as K14).
    turning_over_min_gap_minutes: float = 90.0
    # Picker age window for candidate reflections (the picker only
    # considers rows with ``min_age_hours <= age <= max_age_hours``).
    # Lower bound prevents a reflection written 5 minutes ago from
    # surfacing as "I've been turning this over"; upper bound keeps
    # the cue tied to the most recent between-session window. The
    # parser clamps ``max`` to ``>= min + 1h`` so the window is
    # always non-empty.
    turning_over_min_age_hours: float = 24.0
    turning_over_max_age_hours: float = 72.0
    # Cosine similarity floor for the candidate reflection against
    # the union of active-goal vectors and recent user-message
    # vectors. Below this, the candidate is dropped as "not relevant
    # to the current thread". 0.30 is conservative -- the picker
    # would rather stay silent than surface an off-topic reflection.
    # Clamped to ``[0, 1]``.
    turning_over_min_topical_similarity: float = 0.30
    # How many recent user-message vectors to pull from the RAG
    # store as the "thread" pool. 0 disables the thread pool
    # (picker would then only match against active goals). Default
    # 12 mirrors K6's :data:`NoveltyDetector.window`.
    turning_over_recent_msgs_window: int = 12

    # ── K22 personality backlog: callback / inside-joke detector ─────
    # Post-turn cosine pass between Aiko's reply and older eligible
    # memories. Hits stamp ``metadata.callback_count`` and bump
    # ``salience`` + ``revival_score`` so the retriever's read-side
    # bonus (``_RAG_CALLBACK_BONUS``) prefers memories Aiko has
    # actually managed to weave back into a reply over equally-
    # relevant siblings that have never been cited. The reinforcement
    # is invisible to the LLM by design — see :mod:`app.core.conversation.callback_detector`.
    #
    # Minimum days since ``created_at`` before a memory is eligible to
    # be counted as a callback target. Lower than this and the row is
    # treated as "still part of the current thread", not a callback.
    # Default 3 days roughly maps to "this isn't the same session and
    # the memory has had time to settle". Higher → only very-old
    # rows qualify; lower → easier callbacks.
    callback_age_floor_days: int = 3
    # Cosine similarity floor for the assistant-reply embedding vs a
    # candidate memory's embedding. ``0.55`` is the same conservative
    # threshold K6 uses for ``strong_novelty`` — high enough that
    # generic word overlap doesn't trip it but loose enough that
    # paraphrased callbacks still register. Clamped to ``[0, 1]``.
    callback_similarity_threshold: float = 0.55
    # Maximum number of memories stamped as called-back on a single
    # turn. One reply rarely references more than a handful of beats,
    # so the cap prevents a single high-similarity sentence from
    # blanket-bumping every near-duplicate row.
    callback_max_hits_per_turn: int = 3
    # Per-row cooldown in hours. A memory called back less than this
    # ago stays silent on subsequent matches so back-to-back replies
    # on a similar topic don't spam the same row. Higher → callbacks
    # cluster less; lower → faster compounding on a recent thread.
    callback_cooldown_hours: int = 24
    # Salience bump applied to each called-back row at record time.
    # The store clamps the result to ``[0, 1]`` so already-pinned /
    # high-salience rows simply stay at the ceiling. Higher → louder
    # compounding via the retriever's salience-aware base score;
    # lower → only the read-side ``_RAG_CALLBACK_BONUS`` drives the
    # preference.
    callback_salience_bump: float = 0.05
    # Revival-score bump applied to each called-back row at record
    # time. The store clamps to ``[0, 1]``. Acts as a tier-promotion
    # signal: a long_term row that keeps getting called back will
    # have its revival_score nudge it toward salience=1.0 over the
    # promotion worker's next sweeps.
    callback_revival_bump: float = 0.10
    # ── K20 personality backlog: metacognitive calibration ───────────
    # Tracks Jacob's calibration signal toward Aiko's claims (pushback /
    # softening rephrase / affirmation) into a per-user
    # CalibrationState (global scalar + bounded ring of topic slots).
    # Surfaced as a one-line hedge cue on the next turn when the
    # global score sits below ``calibration_global_low_threshold`` or
    # a topic slot sits below ``calibration_topic_low_threshold``.
    # K20 deliberately does NOT touch RAG retrieval scores -- F3
    # already owns per-memory accuracy hedging. K20 is the per-user /
    # per-topic register tilt on top of it. See
    # :mod:`app.core.affect.calibration_detector` and
    # :mod:`app.core.affect.calibration_store`.
    #
    # Baseline score the global + topic slots decay toward in the
    # absence of new signals. ``0.80`` reads as "neutral-positive"
    # (Aiko speaks confidently by default); lowering it makes Aiko
    # more reflexively hedgy.
    calibration_baseline: float = 0.80
    # Render thresholds for the inner-life cue. The global cue fires
    # only when ``global_score < calibration_global_low_threshold``;
    # the topic cue (which wins on tie) fires when any topic slot is
    # below ``calibration_topic_low_threshold``. Lower → cue is
    # rarer; higher → cue fires more readily.
    calibration_global_low_threshold: float = 0.55
    calibration_topic_low_threshold: float = 0.50
    # Exponential half-life in days for the drift toward baseline.
    # Topic slots decay slower (multiplier in
    # ``calibration_detector.decay``) so a learned topic stance
    # outlives a general bad day. Higher → calibration persists
    # longer; lower → faster recovery to baseline.
    calibration_half_life_days: float = 5.0
    # Cosine similarity floor between an incoming assistant_vec and
    # an existing topic centroid for the slot to absorb the signal
    # (rather than allocating a new slot). Higher → narrower topics,
    # more slots; lower → broader topics, fewer slots.
    calibration_topic_merge_threshold: float = 0.78
    # Cosine similarity floor between user_vec and the prior
    # assistant_vec for the softening detector to fire (the
    # hedge-token regex must also match -- both conditions are AND).
    # Higher → only near-paraphrases fire; lower → looser cosine
    # gate (raises false positives, the regex stays the safety net).
    calibration_softening_threshold: float = 0.70
    # Hard cap on the topic-slot ring. Eviction prefers the slot
    # whose ``abs(score - baseline)`` is smallest AND whose
    # ``last_signal_at`` is oldest. Higher → finer topic resolution
    # at the cost of memory + storage; lower → coarser, more global
    # behaviour.
    calibration_max_topic_slots: int = 8
    # ── K24 personality backlog: sensory anchoring layer ─────────────
    # Adaptive per-arc cadence layer that occasionally surfaces a
    # "small physical beat available" cue so Aiko substitutes a
    # sensory detail for an emotional statement. State is in-memory
    # on the controller (no DB, no persistence). See
    # :mod:`app.core.conversation.sensory_anchor`.
    #
    # Global minimum cooldown between beats; the per-arc cooldown
    # adds on top via ``max(arc_min, min_turn_gap)`` so this is a
    # *floor*, not a ceiling. Raise to make beats rarer overall;
    # the per-arc table still drives the band shape.
    sensory_anchor_min_turn_gap: int = 4
    # Multiplier on the per-arc probability. ``1.0`` = ship as
    # designed; ``< 1.0`` = rarer (e.g. ``0.5`` halves every band);
    # ``> 1.0`` = more often (e.g. ``2.0`` would push ``support``'s
    # 0.45 probability up against the 1.0 clamp). Clamped
    # ``[0.0, 2.0]`` so a buggy user.json can't accidentally
    # silence the feature entirely or push the dice into "always
    # fire" territory.
    sensory_anchor_probability_scale: float = 1.0
    # No-repeat ring size. After firing on the tea pot, the same
    # slug stays out of the candidate pool until ``max_recent``
    # other items have fired (or the deque overflows). Lower →
    # more repetition tolerance; higher → more variety required.
    sensory_anchor_max_recent_items: int = 4
    # Hard cap on how many room items the selector considers per
    # tick. The world is small today (~10 items per location), but
    # this protects future "100-item garden" scenarios from a
    # quadratic blow-up in the weighted sample step.
    sensory_anchor_max_window_items: int = 6
    # ── Background workers (schema v8) ───────────────────────────────
    # Worker intervals in seconds. Both workers are idempotent: running
    # more often is safe but wastes a little CPU. Drop to ~60 for
    # active testing. Lowered from 3600 -> 1800 since idle workers no
    # longer block the brain and there's ample local-LLM headroom.
    promotion_worker_interval_seconds: int = 1800
    decay_worker_interval_seconds: int = 1800
    # F1 personality backlog: how often the IdleFactChecker drains the
    # claim queue. Defaults to 5 minutes so a steady drip of newly
    # written memories gets verified over a session. The worker still
    # respects the per-hour/per-day rate caps in :class:`AgentSettings`.
    fact_checker_interval_seconds: int = 300
    # G2: schedule learner cadence. The bucket scan is cheap and the
    # picture changes slowly, so once a day is plenty.
    schedule_learner_interval_seconds: int = 86400
    # ── K3: routine / ritual awareness thresholds ────────────────────
    # The K3 pass piggybacks on the G2 cadence (same worker, same
    # window). These knobs only control whether a (weekday, bucket)
    # cell qualifies as a named ritual.
    #
    # Minimum number of *distinct ISO weeks* the slot must light up
    # before it's considered recurrent. 3 is the smallest value that
    # actually reads as "happens regularly" (twice could be a
    # coincidence; once is just one moment). Lower this for active
    # testing, never below 1.
    routine_min_touches: int = 3
    # Proportional floor: the slot must light up in at least this
    # share of weeks across the rolling window. With a 30-day window
    # the denominator is 5 weeks, so 0.30 means "covered 2 of 5".
    # This stops a long window from minting a "routine" off three
    # weeks at the start of the window when the user has since drifted
    # to other slots.
    routine_min_share: float = 0.30
    # Cap on how many named routines the worker writes into the
    # ``routines`` profile field. The 240-char ``ProfileEntry`` cap is
    # the hard upper bound; this knob is the soft one that keeps the
    # rendered phrase from growing into a list. Top-N by recurrence
    # density.
    routine_max_active: int = 5
    # G3: idle curiosity worker cadence. Each tick web-searches at most
    # one open question, so a 30-minute interval combined with the
    # rate-cap gives the worker room to chip away at a backlog without
    # hammering the search engine.
    idle_curiosity_interval_seconds: int = 1800
    # K9: curiosity-seed worker cadence. One LLM call + a handful of
    # embeddings per tick, so an hour between successful runs is
    # plenty -- the worker also ``is_ready=False``s when the seed
    # store is at ``curiosity_seed_max_active`` so the cadence is a
    # ceiling, not a floor.
    curiosity_seed_interval_seconds: int = 3600
    # K11: pre-thought / counterfactual worker cadence. A tick is one
    # question-generation LLM call plus up to ``pre_thought_max_per_run``
    # in-persona draft calls, so an hour between successful runs is
    # plenty; the worker also ``is_ready=False``s when the pre-thought
    # store is at ``pre_thought_max_active``, making the cadence a
    # ceiling not a floor.
    pre_thought_interval_seconds: int = 3600
    # K21: fresh-eyes thread re-summary worker cadence. The is_ready
    # gate already enforces the real triggers (message-interval / age),
    # so this is just how often the idle scheduler bothers to check —
    # hourly is plenty.
    thread_resummary_interval_seconds: int = 3600
    # WorldNoticeWorker cadence + pacing. The worker checks for a freshly
    # user-given item (kv watermark) or a long-enough quiet stretch and
    # primes a single proactive "I noticed my room" nudge. Runs often
    # (default 5 min) because it's cheap and quiet-gated, but a
    # per-fire cooldown (default 1h) plus a daily cap keep the actual
    # nudges rare so she stays subtle rather than chatty. ``ttl`` bounds
    # how long a primed nudge stays fresh before the proactive director
    # drops it unspoken.
    world_notice_interval_seconds: int = 300
    world_notice_cooldown_seconds: int = 3600
    world_notice_daily_cap: int = 4
    world_notice_ttl_seconds: int = 1800
    # K36 IdleAwayActivityWorker cadence + pacing. The worker runs during
    # quiet windows (default every 20 min) and, paced by a per-fire
    # cooldown (default 90 min) + daily cap, performs one small room
    # activity, mutating the world + journaling it. ``min_gap_hours`` is
    # the typed-absence threshold the surfacing provider gates on (only
    # mention "while you were away" after a real gap). ``journal_max``
    # bounds the kv ring of recent activities.
    away_activities_interval_seconds: int = 1200
    away_activities_cooldown_seconds: int = 5400
    away_activities_daily_cap: int = 6
    away_activities_min_gap_hours: float = 4.0
    away_activities_journal_max: int = 8
    # K34 ForwardCuriosityWorker cadence + pacing. The worker runs during
    # quiet windows (default every 30 min) and, paced by a per-fire
    # cooldown (default 1h) + daily cap, drafts one forward question into
    # the ``aiko.forward_curiosity`` kv ring. ``min_gap_hours`` is the
    # typed-absence threshold the surfacing provider gates on (only
    # surface "I've been wondering" after a real gap). ``journal_max``
    # bounds the kv ring of drafted questions.
    forward_curiosity_interval_seconds: int = 900
    forward_curiosity_cooldown_seconds: int = 3600
    forward_curiosity_daily_cap: int = 4
    forward_curiosity_min_gap_hours: float = 4.0
    forward_curiosity_journal_max: int = 8
    # FollowUpWorker cue ring size (``aiko.follow_up_cues``). Bounds the
    # number of drafted "ask how their plan went" cues kept around.
    follow_up_journal_max: int = 8
    # K43 PromiseFollowthroughWorker cadence + pacing. The worker runs
    # during quiet windows (default every 30 min). ``min_age_hours`` is
    # how long an assistant promise must sit open before the cue arms
    # (closing the loop 5 minutes later reads robotic, not attentive).
    # ``cooldown_hours`` paces consecutive cues so a backlog of old
    # promises doesn't turn every turn into loop-closing.
    # ``drop_after_days`` ages out promises nobody followed up on (a
    # 3-week-old "I'll check" resurfacing is weirder than letting it
    # go). ``fulfil_min_overlap`` is the content-word overlap a reply /
    # finished task must share with the promise body to count as
    # fulfilled.
    promise_followthrough_interval_seconds: int = 900
    promise_followthrough_min_age_hours: float = 4.0
    promise_followthrough_cooldown_hours: float = 6.0
    promise_followthrough_drop_after_days: float = 14.0
    promise_fulfil_min_overlap: int = 3
    # ── K38: self-correction cue thresholds ───────────────────────────
    # ``min_confidence`` is the floor a fact/preference memory must clear
    # to count as a durable claim worth correcting toward. ``min_overlap``
    # is the number of shared content words a reply sentence and a memory
    # must have before the contradiction heuristic runs (lexical
    # shortlist). ``max_candidates`` caps the candidate pool per turn.
    # ``cooldown_turns`` is the per-fire suppression window so a single
    # slip doesn't nag every turn.
    self_correction_min_confidence: float = 0.6
    self_correction_min_overlap: int = 2
    self_correction_max_candidates: int = 50
    self_correction_cooldown_turns: int = 3
    # K45 mood inertia: effective-mismatch score (whiplash bonus
    # included) at or above which the one-shot cue arms (floor 0.1),
    # and how many post-turn assessments to skip after a fire so one
    # big mood swing doesn't nag on consecutive turns.
    mood_inertia_mismatch_threshold: float = 0.45
    mood_inertia_cooldown_turns: int = 3
    # Output-token ceiling for the memory extractor's JSON response.
    # The old hardcoded 512 truncated the ``"memories": [...]`` array
    # mid-object on longer transcripts, losing the whole batch; 1024
    # fits the capped output and the salvage parser recovers the rest.
    memory_extractor_max_tokens: int = 1024
    # K1: cap on simultaneously-active long-term goals Aiko carries.
    # When :meth:`GoalStore.add_goal` would push past the cap, the
    # oldest un-pinned active goal is archived (its progress history
    # is preserved). Five lines up with the "carrying ~5 things" feel
    # the persona block suggests; bumping past ~7 makes the prompt
    # bullet list noisy and the worker spread thin across too many
    # reflection candidates. Pinned goals do not count against the
    # cap; archived goals never do.
    goal_max_active: int = 5
    # K1: per-goal cap on retained reflection (``goal_progress``)
    # rows. Once the cap is hit the oldest progress row on that goal
    # is pruned each time a new one is appended. The most recent
    # entry is also mirrored into the parent goal's
    # ``metadata.last_progress_note`` so the prompt block stays cheap
    # to render. 12 is roughly two weeks of one-reflection-per-day
    # cadence; lower it for a tighter context budget, raise it for a
    # richer audit trail in the Memory tab.
    goal_max_progress_per_goal: int = 12
    # K1: goal worker tick cadence. The worker's
    # ``is_ready`` predicate fires no more than once per this
    # interval, and the reflection path picks the oldest-touched
    # active goal each turn. One hour gives every active goal a
    # daily-ish reflection at the default ``goal_max_active=5``
    # without ever queueing two ticks in a row. Lower it for a
    # tester loop (e.g. 60 seconds to watch the reflection arrive
    # within a minute); raise it for a calmer cadence.
    goal_reflection_interval_seconds: int = 3600
    # F5: conflicting-memory detector cadence. The all-pairs cosine
    # scan is cheap (NumPy on the in-memory mirror) but the heuristic
    # gate + occasional LLM call adds up, so once an hour is plenty.
    conflict_detector_interval_seconds: int = 1800
    # Cosine similarity band used to short-circuit the candidate
    # filter. Pairs below ``min`` are topically distant (no point
    # checking for contradiction); pairs >= ``max`` are dedupe-likely
    # (the row would already have been merged at write time). The
    # default 0.80-0.92 was chosen so paraphrases sit just above and
    # related-but-distinct claims sit in-band.
    conflict_detector_similarity_min: float = 0.80
    conflict_detector_similarity_max: float = 0.92
    # When the F3 confidence delta between the two halves of a
    # confirmed conflict is at least this big, the worker auto-demotes
    # the loser instead of asking the user. Higher = more cautious
    # auto-resolution; lower = more eager. 0.30 means
    # MemoryExtractor-default (0.7) vs F1-verified (0.95) auto-resolves
    # but two MemoryExtractor rows (both 0.7) always surface to the
    # Conflicts tab.
    conflict_detector_auto_resolve_delta: float = 0.30
    # Caps on the candidate corpus and pair count per tick. The all-
    # pairs loop is O(n^2) on the corpus; ``max_corpus`` keeps that
    # bounded for tens of thousands of memories. ``max_pairs_per_run``
    # caps the heuristic+LLM work per tick so a hot streak of
    # contradictions doesn't burn the per-day LLM budget on one run.
    conflict_detector_max_corpus: int = 1000
    conflict_detector_max_pairs_per_run: int = 50
    # ── K35 personality backlog: memory consolidation worker ─────────
    # Nightly-ish cadence (default 6h so it gets several chances to land
    # in a quiet window per day; caps keep the cost bounded regardless).
    consolidation_interval_seconds: int = 21600
    # Only scratchpad rows created within this many days are scanned —
    # the noisy auto-extracted backlog, not durable long_term anchors.
    consolidation_lookback_days: int = 30
    # Cosine at/above which two same-kind, non-contradicting rows are
    # treated as near-duplicates and fused. Sits just under the 0.92
    # insert-dedupe so it catches the band that escaped write-time
    # merge.
    consolidation_similarity_threshold: float = 0.90
    # O(n^2) corpus cap + per-run cluster cap. ``max_clusters_per_run``
    # bounds the worker-LLM merge calls per tick; ``min_cluster_size``
    # is the smallest group worth merging (2 = a single duplicate pair).
    consolidation_max_corpus: int = 1000
    consolidation_max_clusters_per_run: int = 20
    consolidation_min_cluster_size: int = 2
    # ── K2 personality backlog: theory-of-mind / belief tracking ─────
    # Background inference worker cadence. The worker spends one LLM
    # call per tick to extract beliefs from the last
    # ``belief_worker_lookback_turns`` user turns; once an hour leaves
    # plenty of room between calls without making the model feel
    # forgetful.
    belief_worker_interval_seconds: int = 1200
    # How many recent **user** messages the worker passes to the LLM
    # per extraction. Larger windows give a richer signal but cost
    # more tokens; 12 is enough to span a few conversational beats.
    belief_worker_lookback_turns: int = 12
    # ── Phase 3c (reworked): context-aware promise extraction worker ──
    # Cadence + context budgets for
    # :class:`app.core.memory.promise_worker.PromiseExtractionWorker`.
    # Frequent by default (every 10 min) because real spend is bounded
    # by the per-hour / per-day caps, not the interval.
    promise_worker_interval_seconds: int = 600
    # How many recent turns (both user and assistant) the worker reads.
    # Promises come from both sides, so unlike the belief worker this
    # keeps assistant lines too.
    promise_worker_lookback_turns: int = 12
    # Max promises persisted per run -- a single noisy window can't
    # flood the store; the next tick picks up anything dropped.
    promise_worker_max_per_run: int = 5
    # Per-message + overall transcript char budgets for the snapshot.
    # Generous so the LLM has enough surrounding context to resolve
    # pronouns/objects into self-contained promises; only truncate to
    # protect the worker-LLM token budget.
    promise_worker_max_msg_chars: int = 2000
    promise_worker_max_transcript_chars: int = 8000
    # Gap-detector thresholds. The mood pass surfaces a gap when
    # ``|val_pred - val_obs|`` exceeds ``belief_gap_valence_threshold``,
    # ``|aro_pred - aro_obs|`` exceeds ``belief_gap_arousal_threshold``,
    # or the recomputed valence band crosses into opposing territory.
    # Tuned conservatively so a small affect drift can't pelt Aiko
    # with "am I reading this wrong?" beats every turn.
    belief_gap_valence_threshold: float = 0.30
    belief_gap_arousal_threshold: float = 0.25
    # Window the mood-gap pass considers. Predictions older than this
    # are skipped on the mood pass (they age out via the stale sweep
    # instead). Opinion beliefs have no recency window because a long-
    # held belief can still be contradicted by a fresh message.
    belief_recent_window_hours: int = 24
    # Active beliefs untouched (no check, no update) for this many
    # days are bulk-flipped to ``stale`` on the gap detector's first
    # sweep of the tick. Stale rows stay in the table as audit
    # history but are dropped from future detector passes.
    belief_stale_after_days: int = 90
    # Hard ceiling on ``active`` beliefs per user. The worker prunes
    # the lowest-confidence + oldest active rows down to this cap on
    # every tick so a runaway extraction can't flood the store.
    # Confirmed / contradicted / stale audit rows are kept regardless.
    belief_max_active_per_user: int = 200
    # ── K6 personality backlog: surprise / novelty detector ──────────
    # Size of the rolling centroid window. The detector keeps the
    # last N user-message embeddings (cross-session per user) in an
    # in-memory ring; the centroid is their re-normalised mean.
    # Bigger windows smooth more aggressively, smaller ones react
    # faster to topic pivots. 12 spans a few conversational beats
    # without being so long that a real shift gets averaged away.
    novelty_window: int = 12
    # Minimum ring size before the detector starts emitting a band.
    # Below this we just collect vectors and stay silent so a cold
    # start (or a brand-new install) doesn't fire "this is novel" on
    # the first three turns of every session.
    novelty_warmup_min: int = 3
    # Distance band thresholds. ``distance = 1.0 - cosine`` against
    # the centroid (vectors are unit-norm, so distance lives in
    # ``[0, 2]`` but practical values cluster well below 1.0).
    # Tuned conservatively so small lexical variations (greetings,
    # filler) stay below ``mild`` and only real topic pivots cross
    # ``strong``. Set ``strong < mild`` and the detector falls back
    # to single-threshold behaviour.
    novelty_mild_threshold: float = 0.35
    novelty_strong_threshold: float = 0.55
    # Turns to suppress further novelty signals after a hit. Prevents
    # "you keep saying surprising things" piles when a user runs
    # through several genuinely-new topics in a row. The current turn
    # still contributes to the centroid so the baseline keeps moving.
    novelty_cooldown_turns: int = 2
    # ── K18: topic-stagnation detector thresholds ────────────────────
    # The K18 detector is a pure streak counter over the K6 distance
    # stream -- no embeddings, no rag_store, no per-user state. These
    # knobs only control when a sustained low-divergence streak counts
    # as a "lull". Defaults are conservative on purpose; calibration
    # is best done live and the persona explicitly tells Aiko that
    # *not* hearing the cue is also a signal.
    #
    # Number of distance samples to average before scoring. 6 covers
    # roughly a conversational beat (greeting, two follow-ups, two
    # answers, a recap) so a single tight exchange doesn't fire by
    # itself.
    stagnation_window: int = 6
    # Mean-distance band thresholds. Note the inversion vs K6: lower
    # mean = MORE stagnant, so ``strong < mild``. A 6-turn mean
    # below 0.18 reads as "we've been on this for a bit"; below 0.10
    # reads as "we've been *very* on this". Set ``strong > mild`` and
    # the detector falls back to a single-threshold behaviour using
    # the tighter value.
    stagnation_mild_threshold: float = 0.18
    stagnation_strong_threshold: float = 0.10
    # Turns to suppress further stagnation signals after a hit. The
    # window is longer than K6's because lulls are by nature
    # drawn-out; refiring on consecutive turns is almost never
    # useful, even when the mean stays below threshold.
    stagnation_cooldown_turns: int = 4
    # Turns to keep K18 quiet after a K6 hit. Right after novelty
    # fires the centroid is mid-shift, so distances are noisy for a
    # few turns; waiting a beat avoids the "you just pivoted, but
    # also you've been on this forever" weirdness.
    stagnation_post_novelty_suppression_turns: int = 3
    # IdleWorkerScheduler tick + quiet gate. Lowering ``wake_seconds``
    # makes workers fire sooner after a quiet period starts but
    # increases idle CPU; ``quiet_threshold`` is how long since the
    # last user activity before the scheduler considers itself idle.
    idle_worker_wake_seconds: float = 60.0
    idle_worker_quiet_threshold_seconds: int = 30
    # P8: per-tick wall-time budget in milliseconds. The scheduler runs
    # as many due workers as fit into this budget per wake-up so the
    # natural typing/speaking gap between turns drains backlog instead
    # of one worker at a time. Anti-starvation always lets the
    # most-overdue worker fire even if its EMA estimate exceeds the
    # remaining budget. Set to a small value (e.g. 500) to approximate
    # the old one-per-tick behaviour; ``max_per_tick`` (0 = unlimited)
    # is a hard cap if you want to clamp tick log volume on heavy
    # backlogs.
    idle_worker_tick_budget_ms: int = 3000
    idle_worker_max_per_tick: int = 0


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
            base_url=_required(ollama, "base_url"),
            embedding_base_url=str(ollama.get("embedding_base_url", "") or "").strip(),
            chat_model=_required(ollama, "chat_model"),
            temperature=float(_required(ollama, "temperature")),
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
        agent=AgentSettings(
            proactive_silence_seconds=max(10.0, float(agent_raw.get("proactive_silence_seconds", 45.0))),
            proactive_cooldown_seconds=max(30.0, float(agent_raw.get("proactive_cooldown_seconds", 120.0))),
            # Typed-mode floors: silence 60s (anything shorter reads as
            # nag-y at typed speed) and cooldown 120s. The defaults are
            # well above both floors; the clamps are belt-and-braces
            # for hand-edited config files.
            proactive_typed_enabled=bool(agent_raw.get("proactive_typed_enabled", True)),
            proactive_silence_seconds_typed=max(
                60.0, float(agent_raw.get("proactive_silence_seconds_typed", 240.0)),
            ),
            proactive_cooldown_seconds_typed=max(
                120.0, float(agent_raw.get("proactive_cooldown_seconds_typed", 600.0)),
            ),
            proactive_typed_tts_enabled=bool(
                agent_raw.get("proactive_typed_tts_enabled", False)
            ),
            proactive_typed_when_away=bool(
                agent_raw.get("proactive_typed_when_away", False),
            ),
            world_notice_enabled=bool(
                agent_raw.get("world_notice_enabled", True),
            ),
            activity_awareness_enabled=bool(
                agent_raw.get("activity_awareness_enabled", False),
            ),
            fact_checker_enabled=bool(
                agent_raw.get("fact_checker_enabled", True),
            ),
            fact_checker_per_hour_cap=max(
                0, int(agent_raw.get("fact_checker_per_hour_cap", 10))
            ),
            fact_checker_per_day_cap=max(
                0, int(agent_raw.get("fact_checker_per_day_cap", 50))
            ),
            schedule_learner_enabled=bool(
                agent_raw.get("schedule_learner_enabled", True),
            ),
            schedule_learner_min_samples=max(
                1, int(agent_raw.get("schedule_learner_min_samples", 5)),
            ),
            schedule_learner_window_days=max(
                1, int(agent_raw.get("schedule_learner_window_days", 30)),
            ),
            routine_detection_enabled=bool(
                agent_raw.get("routine_detection_enabled", True),
            ),
            idle_curiosity_enabled=bool(
                agent_raw.get("idle_curiosity_enabled", True),
            ),
            idle_curiosity_per_hour_cap=max(
                0, int(agent_raw.get("idle_curiosity_per_hour_cap", 2)),
            ),
            idle_curiosity_per_day_cap=max(
                0, int(agent_raw.get("idle_curiosity_per_day_cap", 6)),
            ),
            conflict_detector_enabled=bool(
                agent_raw.get("conflict_detector_enabled", True),
            ),
            conflict_detector_per_hour_cap=max(
                0, int(agent_raw.get("conflict_detector_per_hour_cap", 6)),
            ),
            conflict_detector_per_day_cap=max(
                0, int(agent_raw.get("conflict_detector_per_day_cap", 30)),
            ),
            memory_consolidation_enabled=bool(
                agent_raw.get("memory_consolidation_enabled", True),
            ),
            memory_consolidation_per_hour_cap=max(
                0, int(agent_raw.get("memory_consolidation_per_hour_cap", 6)),
            ),
            memory_consolidation_per_day_cap=max(
                0, int(agent_raw.get("memory_consolidation_per_day_cap", 30)),
            ),
            belief_tracking_enabled=bool(
                agent_raw.get("belief_tracking_enabled", True),
            ),
            belief_worker_enabled=bool(
                agent_raw.get("belief_worker_enabled", True),
            ),
            belief_worker_per_hour_cap=max(
                0, int(agent_raw.get("belief_worker_per_hour_cap", 8)),
            ),
            belief_worker_per_day_cap=max(
                0, int(agent_raw.get("belief_worker_per_day_cap", 40)),
            ),
            promise_worker_enabled=bool(
                agent_raw.get("promise_worker_enabled", True),
            ),
            promise_worker_per_hour_cap=max(
                0, int(agent_raw.get("promise_worker_per_hour_cap", 10)),
            ),
            promise_worker_per_day_cap=max(
                0, int(agent_raw.get("promise_worker_per_day_cap", 60)),
            ),
            novelty_detection_enabled=bool(
                agent_raw.get("novelty_detection_enabled", True),
            ),
            topic_stagnation_enabled=bool(
                agent_raw.get("topic_stagnation_enabled", True),
            ),
            topic_graph_enabled=bool(
                agent_raw.get("topic_graph_enabled", True),
            ),
            curiosity_seed_enabled=bool(
                agent_raw.get("curiosity_seed_enabled", True),
            ),
            curiosity_seed_max_active=max(
                1, int(agent_raw.get("curiosity_seed_max_active", 6)),
            ),
            curiosity_seed_max_per_run=max(
                1, int(agent_raw.get("curiosity_seed_max_per_run", 2)),
            ),
            curiosity_seed_min_novelty=max(
                0.0,
                min(1.0, float(agent_raw.get("curiosity_seed_min_novelty", 0.85))),
            ),
            curiosity_seed_resolve_threshold=max(
                0.0,
                min(
                    1.0,
                    float(agent_raw.get(
                        "curiosity_seed_resolve_threshold", 0.50,
                    )),
                ),
            ),
            pre_thought_enabled=bool(
                agent_raw.get("pre_thought_enabled", True),
            ),
            pre_thought_max_active=max(
                1, int(agent_raw.get("pre_thought_max_active", 12)),
            ),
            pre_thought_candidates=max(
                1, int(agent_raw.get("pre_thought_candidates", 4)),
            ),
            pre_thought_max_per_run=max(
                1, int(agent_raw.get("pre_thought_max_per_run", 2)),
            ),
            pre_thought_min_novelty=max(
                0.0,
                min(1.0, float(agent_raw.get("pre_thought_min_novelty", 0.85))),
            ),
            pre_thought_per_hour_cap=max(
                0, int(agent_raw.get("pre_thought_per_hour_cap", 6)),
            ),
            pre_thought_per_day_cap=max(
                0, int(agent_raw.get("pre_thought_per_day_cap", 40)),
            ),
            thread_resummary_enabled=bool(
                agent_raw.get("thread_resummary_enabled", True),
            ),
            thread_resummary_min_messages=max(
                1, int(agent_raw.get("thread_resummary_min_messages", 12)),
            ),
            thread_resummary_message_interval=max(
                1, int(agent_raw.get("thread_resummary_message_interval", 50)),
            ),
            thread_resummary_max_age_hours=max(
                0.0, float(agent_raw.get("thread_resummary_max_age_hours", 24.0)),
            ),
            thread_resummary_per_hour_cap=max(
                0, int(agent_raw.get("thread_resummary_per_hour_cap", 6)),
            ),
            thread_resummary_per_day_cap=max(
                0, int(agent_raw.get("thread_resummary_per_day_cap", 24)),
            ),
            topic_graph_filter_threshold=max(
                0.0,
                min(
                    1.0,
                    float(agent_raw.get(
                        "topic_graph_filter_threshold", 0.65,
                    )),
                ),
            ),
            wants_ledger_enabled=bool(
                agent_raw.get("wants_ledger_enabled", True),
            ),
            wants_growth_per_day=max(
                0.0, float(agent_raw.get("wants_growth_per_day", 0.25)),
            ),
            wants_imperative_threshold=max(
                0.0,
                min(
                    1.0,
                    float(agent_raw.get("wants_imperative_threshold", 0.7)),
                ),
            ),
            wants_cap=max(1, int(agent_raw.get("wants_cap", 8))),
            wants_max_age_days=max(
                1.0, float(agent_raw.get("wants_max_age_days", 14.0)),
            ),
            wants_reentry_cooldown_days=max(
                0.0,
                float(agent_raw.get("wants_reentry_cooldown_days", 5.0)),
            ),
            wants_worker_interval_seconds=max(
                30.0,
                float(agent_raw.get("wants_worker_interval_seconds", 3600.0)),
            ),
            initiative_turns_enabled=bool(
                agent_raw.get("initiative_turns_enabled", True),
            ),
            initiative_base_period=max(
                3, int(agent_raw.get("initiative_base_period", 8)),
            ),
            initiative_warmup_turns=max(
                0, int(agent_raw.get("initiative_warmup_turns", 3)),
            ),
            initiative_substantial_chars=max(
                1,
                int(agent_raw.get("initiative_substantial_chars", 240)),
            ),
            thread_ownership_enabled=bool(
                agent_raw.get("thread_ownership_enabled", True),
            ),
            thread_engaged_chars=max(
                1, int(agent_raw.get("thread_engaged_chars", 80)),
            ),
            thread_min_topical_similarity=min(
                1.0,
                max(
                    0.0,
                    float(
                        agent_raw.get("thread_min_topical_similarity", 0.30)
                    ),
                ),
            ),
            topic_appetite_enabled=bool(
                agent_raw.get("topic_appetite_enabled", True),
            ),
            appetite_short_reply_chars=max(
                1, int(agent_raw.get("appetite_short_reply_chars", 160)),
            ),
            appetite_short_share_threshold=min(
                1.0,
                max(
                    0.0,
                    float(
                        agent_raw.get("appetite_short_share_threshold", 0.6)
                    ),
                ),
            ),
            appetite_window=max(
                2, int(agent_raw.get("appetite_window", 6)),
            ),
            appetite_min_want_pressure=max(
                0.0,
                float(agent_raw.get("appetite_min_want_pressure", 0.35)),
            ),
            appetite_min_axes=min(
                1.0,
                max(
                    -1.0,
                    float(agent_raw.get("appetite_min_axes", 0.15)),
                ),
            ),
            emotion_episodes_enabled=bool(
                agent_raw.get("emotion_episodes_enabled", True),
            ),
            emotion_episode_cap=max(
                1, int(agent_raw.get("emotion_episode_cap", 3)),
            ),
            emotion_lonely_threshold_hours=max(
                0.5,
                float(
                    agent_raw.get("emotion_lonely_threshold_hours", 5.0)
                ),
            ),
            emotion_high_band=min(
                1.0,
                max(
                    0.0,
                    float(agent_raw.get("emotion_high_band", 0.5)),
                ),
            ),
            tease_economy_enabled=bool(
                agent_raw.get("tease_economy_enabled", True),
            ),
            tease_cap=max(1, int(agent_raw.get("tease_cap", 5))),
            tease_expiry_days=max(
                0.5, float(agent_raw.get("tease_expiry_days", 14.0)),
            ),
            tease_collect_cooldown_hours=max(
                0.0,
                float(
                    agent_raw.get("tease_collect_cooldown_hours", 12.0)
                ),
            ),
            tease_min_humor=min(
                1.0,
                max(
                    -1.0, float(agent_raw.get("tease_min_humor", 0.2)),
                ),
            ),
            tease_min_age_hours=max(
                0.0, float(agent_raw.get("tease_min_age_hours", 1.0)),
            ),
            expression_mask=(
                str(agent_raw.get("expression_mask", "off")).strip().lower()
                if str(
                    agent_raw.get("expression_mask", "off")
                ).strip().lower()
                in ("off", "tsundere_light", "tsundere_full")
                else "off"
            ),
            mask_slip_cooldown_days=max(
                0.0,
                float(agent_raw.get("mask_slip_cooldown_days", 2.0)),
            ),
            grounding_line_mode=_parse_grounding_line_mode(
                agent_raw.get("grounding_line_mode", "off"),
            ),
            history_age_prefix_enabled=bool(
                agent_raw.get("history_age_prefix_enabled", True),
            ),
            cue_register_rotation_enabled=bool(
                agent_raw.get("cue_register_rotation_enabled", True),
            ),
            goals_enabled=bool(
                agent_raw.get("goals_enabled", True),
            ),
            goal_worker_bootstrap_enabled=bool(
                agent_raw.get("goal_worker_bootstrap_enabled", True),
            ),
            goal_worker_per_hour_cap=max(
                0, int(agent_raw.get("goal_worker_per_hour_cap", 3)),
            ),
            goal_worker_per_day_cap=max(
                0, int(agent_raw.get("goal_worker_per_day_cap", 12)),
            ),
            shared_moments_enabled=bool(
                agent_raw.get("shared_moments_enabled", True),
            ),
            shared_moments_llm_enabled=bool(
                agent_raw.get("shared_moments_llm_enabled", True),
            ),
            shared_moments_min_turn_gap=max(
                1, int(agent_raw.get("shared_moments_min_turn_gap", 5)),
            ),
            shared_moments_cooldown_seconds=max(
                30.0,
                float(agent_raw.get("shared_moments_cooldown_seconds", 300.0)),
            ),
            anniversary_surfacing_enabled=bool(
                agent_raw.get("anniversary_surfacing_enabled", True),
            ),
            relationship_axes_enabled=bool(
                agent_raw.get("relationship_axes_enabled", True),
            ),
            milestone_celebration_enabled=bool(
                agent_raw.get("milestone_celebration_enabled", True),
            ),
            reconnection_enabled=bool(
                agent_raw.get("reconnection_enabled", True),
            ),
            reconnection_base_gap_hours=max(
                1.0, float(agent_raw.get("reconnection_base_gap_hours", 24.0)),
            ),
            appreciation_beats_enabled=bool(
                agent_raw.get("appreciation_beats_enabled", True),
            ),
            appreciation_min_closeness=max(
                -1.0, min(1.0, float(
                    agent_raw.get("appreciation_min_closeness", 0.25),
                )),
            ),
            appreciation_cooldown_hours=max(
                1.0, float(agent_raw.get("appreciation_cooldown_hours", 72.0)),
            ),
            appreciation_max_anchor_age_days=max(
                1.0,
                float(agent_raw.get("appreciation_max_anchor_age_days", 21.0)),
            ),
            reciprocal_vulnerability_enabled=bool(
                agent_raw.get("reciprocal_vulnerability_enabled", True),
            ),
            reciprocal_vulnerability_cooldown_hours=max(
                1.0,
                float(
                    agent_raw.get(
                        "reciprocal_vulnerability_cooldown_hours", 96.0,
                    )
                ),
            ),
            reciprocal_vulnerability_min_trust=max(
                -1.0, min(1.0, float(
                    agent_raw.get("reciprocal_vulnerability_min_trust", 0.2),
                )),
            ),
            conflict_repair_enabled=bool(
                agent_raw.get("conflict_repair_enabled", True),
            ),
            conflict_repair_watch_turns=max(
                1, int(agent_raw.get("conflict_repair_watch_turns", 5)),
            ),
            conflict_repair_recovery_epsilon=max(
                0.0, float(
                    agent_raw.get("conflict_repair_recovery_epsilon", 0.05),
                ),
            ),
            conflict_repair_min_recovery_rise=max(
                0.0, float(
                    agent_raw.get("conflict_repair_min_recovery_rise", 0.10),
                ),
            ),
            conflict_repair_cooldown_hours=max(
                0.0, float(
                    agent_raw.get("conflict_repair_cooldown_hours", 12.0),
                ),
            ),
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
            self_image_max_tokens=max(120, int(agent_raw.get("self_image_max_tokens", 320))),
            prepared_nudge_ttl_seconds=max(30.0, float(agent_raw.get("prepared_nudge_ttl_seconds", 600.0))),
            filler_enabled=bool(agent_raw.get("filler_enabled", True)),
            filler_first_token_ms=max(150, int(agent_raw.get("filler_first_token_ms", 800))),
            tool_pass_gate_enabled=bool(agent_raw.get("tool_pass_gate_enabled", True)),
            skill_router_enabled=bool(agent_raw.get("skill_router_enabled", False)),
            brain_core_skills=tuple(
                str(s).strip()
                for s in (
                    agent_raw.get("brain_core_skills")
                    if isinstance(agent_raw.get("brain_core_skills"), list)
                    else ["time", "recall", "world"]
                )
                if str(s).strip()
            )
            or ("time", "recall", "world"),
            workflow_skill_router_enabled=bool(
                agent_raw.get("workflow_skill_router_enabled", False)
            ),
            consolidator_enabled=bool(agent_raw.get("consolidator_enabled", True)),
            consolidator_min_hours_between=max(0.5, float(agent_raw.get("consolidator_min_hours_between", 18.0))),
            consolidator_chunk_size=max(8, int(agent_raw.get("consolidator_chunk_size", 40))),
            consolidator_similarity_threshold=max(0.5, min(0.99, float(agent_raw.get("consolidator_similarity_threshold", 0.84)))),
            consolidator_min_cluster_size=max(2, int(agent_raw.get("consolidator_min_cluster_size", 2))),
            consolidator_use_llm_merge=bool(agent_raw.get("consolidator_use_llm_merge", True)),
            relationship_pulse_enabled=bool(agent_raw.get("relationship_pulse_enabled", True)),
            relationship_pulse_min_hours=max(24.0, float(agent_raw.get("relationship_pulse_min_hours", 168.0))),
            relationship_pulse_min_turns=max(5, int(agent_raw.get("relationship_pulse_min_turns", 30))),
            relationship_pulse_max_tokens=max(80, int(agent_raw.get("relationship_pulse_max_tokens", 256))),
            cadence_enabled=bool(agent_raw.get("cadence_enabled", True)),
            earcon_auto_sprinkle=bool(
                agent_raw.get("earcon_auto_sprinkle", True),
            ),
            tts_runtime_temp_enabled=bool(
                agent_raw.get("tts_runtime_temp_enabled", False),
            ),
            tts_runtime_speed_enabled=bool(
                agent_raw.get("tts_runtime_speed_enabled", False),
            ),
            style_tracker_enabled=bool(
                agent_raw.get("style_tracker_enabled", True),
            ),
            style_tracker_window=max(
                2, int(agent_raw.get("style_tracker_window", 12)),
            ),
            style_tracker_warmup=max(
                2, int(agent_raw.get("style_tracker_warmup", 6)),
            ),
            style_tracker_opener_count_threshold=max(
                2,
                int(
                    agent_raw.get(
                        "style_tracker_opener_count_threshold", 4,
                    )
                ),
            ),
            style_tracker_opener_topk_share=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_tracker_opener_topk_share", 0.60,
                        )
                    ),
                ),
            ),
            style_tracker_question_rate_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_tracker_question_rate_threshold", 0.75,
                        )
                    ),
                ),
            ),
            style_tracker_avg_questions_threshold=max(
                0.0,
                float(
                    agent_raw.get(
                        "style_tracker_avg_questions_threshold", 1.5,
                    )
                ),
            ),
            style_tracker_length_avg_threshold=max(
                1.0,
                float(
                    agent_raw.get(
                        "style_tracker_length_avg_threshold", 50.0,
                    )
                ),
            ),
            style_tracker_cue_cooldown_turns=max(
                0,
                int(
                    agent_raw.get("style_tracker_cue_cooldown_turns", 5)
                ),
            ),
            question_balance_enabled=bool(
                agent_raw.get("question_balance_enabled", True),
            ),
            question_balance_ratio_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get("question_balance_ratio_threshold", 0.55)
                    ),
                ),
            ),
            question_balance_window=max(
                2, int(agent_raw.get("question_balance_window", 10)),
            ),
            question_balance_suppress_turns=max(
                0, int(agent_raw.get("question_balance_suppress_turns", 2)),
            ),
            tease_rhythm_enabled=bool(
                agent_raw.get("tease_rhythm_enabled", True),
            ),
            tease_rhythm_window=max(
                2, int(agent_raw.get("tease_rhythm_window", 6)),
            ),
            tease_rhythm_consecutive_cap=max(
                1, int(agent_raw.get("tease_rhythm_consecutive_cap", 3)),
            ),
            tease_rhythm_green_light_humor=max(
                -1.0,
                min(
                    1.0,
                    float(
                        agent_raw.get("tease_rhythm_green_light_humor", 0.2)
                    ),
                ),
            ),
            tease_rhythm_cooldown_turns=max(
                0, int(agent_raw.get("tease_rhythm_cooldown_turns", 3)),
            ),
            style_signal_enabled=bool(
                agent_raw.get("style_signal_enabled", True),
            ),
            style_signal_window=max(
                2, int(agent_raw.get("style_signal_window", 30)),
            ),
            style_signal_warmup_min=max(
                2, int(agent_raw.get("style_signal_warmup_min", 8)),
            ),
            style_signal_terse_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_signal_terse_threshold", 0.55,
                        )
                    ),
                ),
            ),
            style_signal_formal_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_signal_formal_threshold", 0.55,
                        )
                    ),
                ),
            ),
            style_signal_emoji_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_signal_emoji_threshold", 0.05,
                        )
                    ),
                ),
            ),
            style_signal_slang_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_signal_slang_threshold", 0.15,
                        )
                    ),
                ),
            ),
            style_signal_question_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_signal_question_threshold", 0.40,
                        )
                    ),
                ),
            ),
            engagement_tracker_enabled=bool(
                agent_raw.get("engagement_tracker_enabled", True),
            ),
            engagement_window=max(
                2, int(agent_raw.get("engagement_window", 12)),
            ),
            engagement_warmup_min=max(
                2, int(agent_raw.get("engagement_warmup_min", 6)),
            ),
            engagement_latency_z_strong_drop=max(
                0.1,
                float(
                    agent_raw.get("engagement_latency_z_strong_drop", 1.5),
                ),
            ),
            engagement_length_z_strong_drop=min(
                -0.1,
                float(
                    agent_raw.get("engagement_length_z_strong_drop", -1.0),
                ),
            ),
            engagement_closeness_delta_max=max(
                0.0,
                min(
                    0.08,
                    float(
                        agent_raw.get(
                            "engagement_closeness_delta_max", 0.04,
                        )
                    ),
                ),
            ),
            engagement_absence_curiosity_enabled=bool(
                agent_raw.get(
                    "engagement_absence_curiosity_enabled", True,
                ),
            ),
            engagement_absence_curiosity_min_seconds=max(
                60.0,
                float(
                    agent_raw.get(
                        "engagement_absence_curiosity_min_seconds",
                        1800.0,
                    )
                ),
            ),
            engagement_proactive_gate=bool(
                agent_raw.get("engagement_proactive_gate", True),
            ),
            mood_shell_enabled=bool(
                agent_raw.get("mood_shell_enabled", True),
            ),
            mood_shell_axis_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get("mood_shell_axis_threshold", 0.5),
                    ),
                ),
            ),
            clarification_repair_enabled=bool(
                agent_raw.get("clarification_repair_enabled", True),
            ),
            rupture_repair_enabled=bool(
                agent_raw.get("rupture_repair_enabled", True),
            ),
            rupture_valence_drop_threshold=max(
                0.0,
                min(
                    2.0,
                    float(
                        agent_raw.get(
                            "rupture_valence_drop_threshold", 0.12,
                        )
                    ),
                ),
            ),
            contagion_enabled=bool(
                agent_raw.get("contagion_enabled", True),
            ),
            contagion_strength=max(
                0.0,
                min(1.0, float(agent_raw.get("contagion_strength", 0.15))),
            ),
            contagion_max_per_turn=max(
                0.0,
                min(0.5, float(agent_raw.get("contagion_max_per_turn", 0.05))),
            ),
            misattunement_detection_enabled=bool(
                agent_raw.get("misattunement_detection_enabled", True),
            ),
            misattunement_shrink_min_prev_words=max(
                0,
                int(
                    agent_raw.get(
                        "misattunement_shrink_min_prev_words", 30,
                    )
                ),
            ),
            misattunement_shrink_max_user_words=max(
                0,
                int(
                    agent_raw.get(
                        "misattunement_shrink_max_user_words", 8,
                    )
                ),
            ),
            misattunement_pivot_max_user_words=max(
                0,
                int(
                    agent_raw.get(
                        "misattunement_pivot_max_user_words", 8,
                    )
                ),
            ),
            misattunement_cooldown_turns=max(
                0,
                int(
                    agent_raw.get(
                        "misattunement_cooldown_turns", 3,
                    )
                ),
            ),
            self_noticing_enabled=bool(
                agent_raw.get("self_noticing_enabled", True),
            ),
            self_noticing_agreement_streak_enabled=bool(
                agent_raw.get(
                    "self_noticing_agreement_streak_enabled", True,
                ),
            ),
            self_noticing_flat_affect_enabled=bool(
                agent_raw.get(
                    "self_noticing_flat_affect_enabled", True,
                ),
            ),
            self_noticing_repeated_thought_enabled=bool(
                agent_raw.get(
                    "self_noticing_repeated_thought_enabled", True,
                ),
            ),
            self_noticing_window=max(
                1,
                int(agent_raw.get("self_noticing_window", 6)),
            ),
            self_noticing_warmup=max(
                1,
                int(agent_raw.get("self_noticing_warmup", 4)),
            ),
            self_noticing_agreement_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "self_noticing_agreement_threshold", 0.80,
                        )
                    ),
                ),
            ),
            self_noticing_max_pushback=max(
                0,
                int(agent_raw.get("self_noticing_max_pushback", 0)),
            ),
            self_noticing_flat_valence_range=max(
                0.0,
                float(
                    agent_raw.get(
                        "self_noticing_flat_valence_range", 0.10,
                    )
                ),
            ),
            self_noticing_flat_arousal_range=max(
                0.0,
                float(
                    agent_raw.get(
                        "self_noticing_flat_arousal_range", 0.10,
                    )
                ),
            ),
            self_noticing_repeated_cosine_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "self_noticing_repeated_cosine_threshold", 0.85,
                        )
                    ),
                ),
            ),
            self_noticing_cooldown_turns=max(
                0,
                int(agent_raw.get("self_noticing_cooldown_turns", 5)),
            ),
            day_color_enabled=bool(
                agent_raw.get("day_color_enabled", True),
            ),
            day_color_check_interval_seconds=max(
                60,
                int(
                    agent_raw.get("day_color_check_interval_seconds", 3600)
                ),
            ),
            vulnerability_budget_enabled=bool(
                agent_raw.get("vulnerability_budget_enabled", True),
            ),
            vulnerability_budget_min_capacity=max(
                1, int(agent_raw.get("vulnerability_budget_min_capacity", 1)),
            ),
            vulnerability_budget_max_capacity=max(
                1, int(agent_raw.get("vulnerability_budget_max_capacity", 12)),
            ),
            vulnerability_budget_regen_per_hour=max(
                0.01,
                float(
                    agent_raw.get("vulnerability_budget_regen_per_hour", 0.5)
                ),
            ),
            vulnerability_budget_tier1_cost=max(
                0, int(agent_raw.get("vulnerability_budget_tier1_cost", 1)),
            ),
            vulnerability_budget_tier2_cost=max(
                0, int(agent_raw.get("vulnerability_budget_tier2_cost", 3)),
            ),
            vulnerability_budget_tier3_cost=max(
                0, int(agent_raw.get("vulnerability_budget_tier3_cost", 6)),
            ),
            touch_enabled=bool(
                agent_raw.get("touch_enabled", True),
            ),
            touch_per_kind_overrides=(
                dict(agent_raw.get("touch_per_kind_overrides", {}))
                if isinstance(
                    agent_raw.get("touch_per_kind_overrides"), dict,
                )
                else {}
            ),
            persona_regression_enabled=bool(
                agent_raw.get("persona_regression_enabled", True),
            ),
            persona_regression_fixture_path=str(
                agent_raw.get(
                    "persona_regression_fixture_path",
                    "data/persona/golden_turns.jsonl",
                ),
            ),
            tasks_enabled=bool(agent_raw.get("tasks_enabled", True)),
            tasks_per_user_cap=max(
                1, int(agent_raw.get("tasks_per_user_cap", 8))
            ),
            tasks_resume_on_boot=bool(
                agent_raw.get("tasks_resume_on_boot", True)
            ),
            tasks_running_block_enabled=bool(
                agent_raw.get("tasks_running_block_enabled", True)
            ),
            brain_loop_deferred_grace_ms=max(
                10,
                min(
                    5000,
                    int(agent_raw.get("brain_loop_deferred_grace_ms", 100)),
                ),
            ),
            task_cue_max_age_seconds=max(
                60,
                min(
                    86400,
                    int(
                        agent_raw.get("task_cue_max_age_seconds", 1800)
                    ),
                ),
            ),
            task_cue_max_aggregated=max(
                1,
                min(
                    20,
                    int(agent_raw.get("task_cue_max_aggregated", 5)),
                ),
            ),
            task_reply_on_complete_enabled=bool(
                agent_raw.get("task_reply_on_complete_enabled", True)
            ),
            task_inline_grace_seconds=max(
                0.0,
                min(
                    30.0,
                    float(agent_raw.get("task_inline_grace_seconds", 3.0)),
                ),
            ),
            task_report_decision_enabled=bool(
                agent_raw.get("task_report_decision_enabled", True)
            ),
            task_report_decision_floor_mode=(
                str(
                    agent_raw.get("task_report_decision_floor_mode", "shadow")
                ).strip().lower()
                if str(
                    agent_raw.get("task_report_decision_floor_mode", "shadow")
                ).strip().lower()
                in ("shadow", "enforce")
                else "shadow"
            ),
            task_report_angle_enabled=bool(
                agent_raw.get("task_report_angle_enabled", True)
            ),
            task_file_allowed_roots=_parse_task_file_allowed_roots(
                agent_raw.get("task_file_allowed_roots", ())
            ),
            builtin_file_skills_enabled=bool(
                agent_raw.get("builtin_file_skills_enabled", True)
            ),
            task_file_read_max_bytes=max(
                1024,
                min(
                    16 * 1024 * 1024,
                    int(agent_raw.get("task_file_read_max_bytes", 262144)),
                ),
            ),
            task_file_read_max_lines=max(
                10,
                min(
                    50000,
                    int(agent_raw.get("task_file_read_max_lines", 2000)),
                ),
            ),
            task_file_read_allowed_extensions=_parse_extension_list(
                agent_raw.get(
                    "task_file_read_allowed_extensions",
                    (
                        ".txt", ".md", ".rst", ".log",
                        ".py", ".js", ".ts", ".tsx", ".jsx",
                        ".json", ".yaml", ".yml", ".toml",
                        ".ini", ".cfg", ".conf",
                        ".html", ".css", ".xml",
                        ".csv", ".tsv",
                        ".sh", ".bat", ".ps1",
                        ".sql",
                        ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
                        ".java", ".kt",
                        ".rb", ".lua",
                    ),
                )
            ),
            mcp_clients_enabled=bool(agent_raw.get("mcp_clients_enabled", True)),
            workflow_enabled=bool(agent_raw.get("workflow_enabled", True)),
            workflow_max_iterations=max(
                1, min(30, int(agent_raw.get("workflow_max_iterations", 6)))
            ),
            workflow_max_children=max(
                1, min(50, int(agent_raw.get("workflow_max_children", 8)))
            ),
            workflow_max_concurrent=max(
                1, min(8, int(agent_raw.get("workflow_max_concurrent", 2)))
            ),
            workflow_planner_history_budget_chars=max(
                500,
                min(
                    20000,
                    int(
                        agent_raw.get(
                            "workflow_planner_history_budget_chars", 4000
                        )
                    ),
                ),
            ),
            workflow_reply_budget_chars=max(
                1000,
                min(
                    40000,
                    int(agent_raw.get("workflow_reply_budget_chars", 6000)),
                ),
            ),
            workflow_child_wait_timeout_seconds=max(
                5,
                min(
                    600,
                    int(
                        agent_raw.get(
                            "workflow_child_wait_timeout_seconds", 120
                        )
                    ),
                ),
            ),
            workflow_planner_max_tokens=max(
                64,
                min(
                    2048,
                    int(agent_raw.get("workflow_planner_max_tokens", 512)),
                ),
            ),
            workflow_max_consecutive_failures=max(
                1,
                min(
                    20,
                    int(agent_raw.get("workflow_max_consecutive_failures", 2)),
                ),
            ),
            workflow_max_wall_seconds=max(
                0,
                min(
                    3600,
                    int(agent_raw.get("workflow_max_wall_seconds", 300)),
                ),
            ),
            workflow_capability_gap_log_max=max(
                1,
                min(
                    500,
                    int(agent_raw.get("workflow_capability_gap_log_max", 50)),
                ),
            ),
            task_approval_mode=_normalize_approval_mode(
                agent_raw.get("task_approval_mode", "ask")
            ),
            task_approval_overrides=_parse_approval_overrides(
                agent_raw.get("task_approval_overrides", {})
            ),
            file_write=_parse_file_write_settings(
                agent_raw.get("file_write", {})
            ),
            vision=_parse_vision_settings(
                agent_raw.get("vision", {})
            ),
            worker_llm_gate_enabled=bool(
                agent_raw.get("worker_llm_gate_enabled", True)
            ),
            worker_llm_max_concurrency=max(
                1, min(8, int(agent_raw.get("worker_llm_max_concurrency", 1)))
            ),
            worker_llm_priority_overrides=(
                {
                    str(k): str(v)
                    for k, v in agent_raw.get(
                        "worker_llm_priority_overrides", {}
                    ).items()
                }
                if isinstance(
                    agent_raw.get("worker_llm_priority_overrides"), dict
                )
                else {}
            ),
            user_reactions_enabled=bool(
                agent_raw.get("user_reactions_enabled", True),
            ),
            user_reactions_axes_enabled=bool(
                agent_raw.get("user_reactions_axes_enabled", True),
            ),
            user_reactions_daily_axis_cap=max(
                0.0,
                float(
                    agent_raw.get("user_reactions_daily_axis_cap", 0.15),
                ),
            ),
            persona_touch_banner_enabled=bool(
                agent_raw.get("persona_touch_banner_enabled", True),
            ),
            persona_touch_banner_duration_seconds=max(
                1,
                min(
                    120,
                    int(
                        agent_raw.get(
                            "persona_touch_banner_duration_seconds", 20,
                        )
                    ),
                ),
            ),
            persona_task_banner_enabled=bool(
                agent_raw.get("persona_task_banner_enabled", True),
            ),
            task_heartbeat_check_interval_seconds=max(
                5,
                min(
                    3600,
                    int(
                        agent_raw.get(
                            "task_heartbeat_check_interval_seconds", 30
                        )
                    ),
                ),
            ),
            task_stalled_seconds=max(
                60,
                min(
                    86400,
                    int(agent_raw.get("task_stalled_seconds", 300)),
                ),
            ),
            task_stalled_action=(
                str(agent_raw.get("task_stalled_action", "warn")).strip().lower()
                if str(agent_raw.get("task_stalled_action", "warn")).strip().lower()
                in ("warn", "fail")
                else "warn"
            ),
            task_cascade_cancel_children=bool(
                agent_raw.get("task_cascade_cancel_children", True),
            ),
            task_cleanup_retention_days=max(
                1,
                min(
                    3650,
                    int(agent_raw.get("task_cleanup_retention_days", 30)),
                ),
            ),
            task_cleanup_interval_seconds=max(
                600,
                min(
                    604800,
                    int(
                        agent_raw.get("task_cleanup_interval_seconds", 21600)
                    ),
                ),
            ),
            opinion_injection_enabled=bool(
                agent_raw.get("opinion_injection_enabled", True),
            ),
            opinion_injection_require_definite=bool(
                agent_raw.get("opinion_injection_require_definite", False),
            ),
            turning_over_enabled=bool(
                agent_raw.get("turning_over_enabled", True),
            ),
            away_activities_enabled=bool(
                agent_raw.get("away_activities_enabled", True),
            ),
            forward_curiosity_enabled=bool(
                agent_raw.get("forward_curiosity_enabled", True),
            ),
            follow_up_enabled=bool(
                agent_raw.get("follow_up_enabled", True),
            ),
            promise_followthrough_enabled=bool(
                agent_raw.get("promise_followthrough_enabled", True),
            ),
            self_correction_enabled=bool(
                agent_raw.get("self_correction_enabled", True),
            ),
            mood_inertia_enabled=bool(
                agent_raw.get("mood_inertia_enabled", True),
            ),
            confidence_time_decay_enabled=bool(
                agent_raw.get("confidence_time_decay_enabled", True),
            ),
            callback_detector_enabled=bool(
                agent_raw.get("callback_detector_enabled", True),
            ),
            calibration_detection_enabled=bool(
                agent_raw.get("calibration_detection_enabled", True),
            ),
            sensory_anchor_enabled=bool(
                agent_raw.get("sensory_anchor_enabled", True),
            ),
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
            gap_resolver_enabled=bool(
                agent_raw.get("gap_resolver_enabled", True),
            ),
            gap_resolver_interval_seconds=max(
                30,
                int(agent_raw.get("gap_resolver_interval_seconds", 600)),
            ),
            gap_resolver_threshold=max(
                0.0,
                min(
                    1.0,
                    float(agent_raw.get("gap_resolver_threshold", 0.55)),
                ),
            ),
            gap_resolver_per_tick=max(
                1, int(agent_raw.get("gap_resolver_per_tick", 5)),
            ),
            gap_user_answer_resolve_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "gap_user_answer_resolve_threshold", 0.50,
                        )
                    ),
                ),
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
        memory=MemorySettings(
            enabled=bool(memory_raw.get("enabled", True)),
            top_k=max(0, int(memory_raw.get("top_k", 6))),
            score_threshold=max(0.0, min(1.0, float(memory_raw.get("score_threshold", 0.4)))),
            max_memories=max(50, int(memory_raw.get("max_memories", 5000))),
            dedupe_threshold=max(0.5, min(0.999, float(memory_raw.get("dedupe_threshold", 0.92)))),
            extractor_enabled=bool(memory_raw.get("extractor_enabled", True)),
            self_tagged_salience=max(0.0, min(1.0, float(memory_raw.get("self_tagged_salience", 0.7)))),
            tiers_enabled=bool(memory_raw.get("tiers_enabled", True)),
            decay_rate_scratchpad=max(
                0.0, min(1.0, float(memory_raw.get("decay_rate_scratchpad", 0.05)))
            ),
            decay_rate_long_term=max(
                0.0, min(1.0, float(memory_raw.get("decay_rate_long_term", 0.02)))
            ),
            decay_rate_archive=max(
                0.0, min(1.0, float(memory_raw.get("decay_rate_archive", 0.0)))
            ),
            revival_coefficient=max(
                0.0, min(1.0, float(memory_raw.get("revival_coefficient", 0.05)))
            ),
            revival_per_hit=max(
                0.0, min(1.0, float(memory_raw.get("revival_per_hit", 0.15)))
            ),
            revival_decay_per_day=max(
                0.0, min(1.0, float(memory_raw.get("revival_decay_per_day", 0.02)))
            ),
            revival_min_word_overlap=max(
                1, int(memory_raw.get("revival_min_word_overlap", 3))
            ),
            scratchpad_ttl_days=max(
                1, int(memory_raw.get("scratchpad_ttl_days", 14))
            ),
            scratchpad_promote_min_age_days=max(
                0, int(memory_raw.get("scratchpad_promote_min_age_days", 7))
            ),
            scratchpad_promote_min_use_count=max(
                0, int(memory_raw.get("scratchpad_promote_min_use_count", 3))
            ),
            scratchpad_promote_min_revival=max(
                0.0,
                min(1.0, float(memory_raw.get("scratchpad_promote_min_revival", 0.3))),
            ),
            archive_demote_idle_days=max(
                1, int(memory_raw.get("archive_demote_idle_days", 180))
            ),
            scratchpad_cap=max(50, int(memory_raw.get("scratchpad_cap", 1000))),
            archive_cap=max(50, int(memory_raw.get("archive_cap", 10000))),
            fade_hedge_enabled=bool(
                memory_raw.get("fade_hedge_enabled", True),
            ),
            faded_salience_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("faded_salience_threshold", 0.20)),
                ),
            ),
            faded_idle_days=max(
                1, int(memory_raw.get("faded_idle_days", 30)),
            ),
            confidence_decay_horizon_days=max(
                1, int(memory_raw.get("confidence_decay_horizon_days", 365)),
            ),
            confidence_decay_floor=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("confidence_decay_floor", 0.3)),
                ),
            ),
            confidence_decay_distant_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "confidence_decay_distant_threshold", 0.5,
                        )
                    ),
                ),
            ),
            opinion_injection_min_cosine=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("opinion_injection_min_cosine", 0.55)
                    ),
                ),
            ),
            opinion_injection_min_user_words=max(
                0,
                int(memory_raw.get("opinion_injection_min_user_words", 4)),
            ),
            opinion_injection_cooldown_turns=max(
                0,
                int(memory_raw.get("opinion_injection_cooldown_turns", 5)),
            ),
            opinion_injection_per_session_cap=max(
                0,
                int(memory_raw.get("opinion_injection_per_session_cap", 3)),
            ),
            opinion_injection_per_hour_cap=max(
                0,
                int(memory_raw.get("opinion_injection_per_hour_cap", 6)),
            ),
            opinion_injection_per_day_cap=max(
                0,
                int(memory_raw.get("opinion_injection_per_day_cap", 30)),
            ),
            # ── K28: turning-over picker ──────────────────────────────
            # ``turning_over_min_gap_minutes`` clamped to >= 5 so a
            # misconfigured value can't make the cue fire on every
            # typed turn.
            turning_over_min_gap_minutes=max(
                5.0,
                float(
                    memory_raw.get("turning_over_min_gap_minutes", 90.0)
                ),
            ),
            # ``min_age_hours`` clamped to >= 1; ``max_age_hours``
            # clamped to >= min_age + 1 so the picker window is always
            # non-empty even with a hostile config.
            turning_over_min_age_hours=max(
                1.0,
                float(
                    memory_raw.get("turning_over_min_age_hours", 24.0)
                ),
            ),
            turning_over_max_age_hours=max(
                max(
                    1.0,
                    float(
                        memory_raw.get("turning_over_min_age_hours", 24.0)
                    ),
                )
                + 1.0,
                float(
                    memory_raw.get("turning_over_max_age_hours", 72.0)
                ),
            ),
            turning_over_min_topical_similarity=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "turning_over_min_topical_similarity", 0.30,
                        )
                    ),
                ),
            ),
            turning_over_recent_msgs_window=max(
                0,
                int(
                    memory_raw.get("turning_over_recent_msgs_window", 12)
                ),
            ),
            callback_age_floor_days=max(
                1, int(memory_raw.get("callback_age_floor_days", 3)),
            ),
            callback_similarity_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("callback_similarity_threshold", 0.55)
                    ),
                ),
            ),
            callback_max_hits_per_turn=max(
                1, int(memory_raw.get("callback_max_hits_per_turn", 3)),
            ),
            callback_cooldown_hours=max(
                1, int(memory_raw.get("callback_cooldown_hours", 24)),
            ),
            callback_salience_bump=max(
                0.0,
                min(
                    0.5,
                    float(memory_raw.get("callback_salience_bump", 0.05)),
                ),
            ),
            callback_revival_bump=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("callback_revival_bump", 0.10)),
                ),
            ),
            calibration_baseline=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("calibration_baseline", 0.80)),
                ),
            ),
            calibration_global_low_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "calibration_global_low_threshold", 0.55,
                        )
                    ),
                ),
            ),
            calibration_topic_low_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "calibration_topic_low_threshold", 0.50,
                        )
                    ),
                ),
            ),
            calibration_half_life_days=max(
                0.1,
                float(memory_raw.get("calibration_half_life_days", 5.0)),
            ),
            calibration_topic_merge_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "calibration_topic_merge_threshold", 0.78,
                        )
                    ),
                ),
            ),
            calibration_softening_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "calibration_softening_threshold", 0.70,
                        )
                    ),
                ),
            ),
            calibration_max_topic_slots=max(
                1, int(memory_raw.get("calibration_max_topic_slots", 8)),
            ),
            sensory_anchor_min_turn_gap=max(
                1, int(memory_raw.get("sensory_anchor_min_turn_gap", 4)),
            ),
            sensory_anchor_probability_scale=max(
                0.0,
                min(
                    2.0,
                    float(
                        memory_raw.get(
                            "sensory_anchor_probability_scale", 1.0,
                        )
                    ),
                ),
            ),
            sensory_anchor_max_recent_items=max(
                1,
                int(memory_raw.get("sensory_anchor_max_recent_items", 4)),
            ),
            sensory_anchor_max_window_items=max(
                1,
                int(memory_raw.get("sensory_anchor_max_window_items", 6)),
            ),
            decay_max_catchup_days=max(
                1.0, float(memory_raw.get("decay_max_catchup_days", 30.0))
            ),
            promotion_worker_interval_seconds=max(
                10,
                int(memory_raw.get("promotion_worker_interval_seconds", 1800)),
            ),
            decay_worker_interval_seconds=max(
                10, int(memory_raw.get("decay_worker_interval_seconds", 1800))
            ),
            fact_checker_interval_seconds=max(
                30,
                int(memory_raw.get("fact_checker_interval_seconds", 300)),
            ),
            schedule_learner_interval_seconds=max(
                60,
                int(
                    memory_raw.get("schedule_learner_interval_seconds", 86400)
                ),
            ),
            routine_min_touches=max(
                1,
                int(memory_raw.get("routine_min_touches", 3)),
            ),
            routine_min_share=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("routine_min_share", 0.30)),
                ),
            ),
            routine_max_active=max(
                1,
                int(memory_raw.get("routine_max_active", 5)),
            ),
            idle_curiosity_interval_seconds=max(
                60,
                int(memory_raw.get("idle_curiosity_interval_seconds", 1800)),
            ),
            curiosity_seed_interval_seconds=max(
                60,
                int(memory_raw.get("curiosity_seed_interval_seconds", 3600)),
            ),
            pre_thought_interval_seconds=max(
                60,
                int(memory_raw.get("pre_thought_interval_seconds", 3600)),
            ),
            thread_resummary_interval_seconds=max(
                60,
                int(memory_raw.get("thread_resummary_interval_seconds", 3600)),
            ),
            world_notice_interval_seconds=max(
                30,
                int(memory_raw.get("world_notice_interval_seconds", 300)),
            ),
            world_notice_cooldown_seconds=max(
                0,
                int(memory_raw.get("world_notice_cooldown_seconds", 3600)),
            ),
            world_notice_daily_cap=max(
                0,
                int(memory_raw.get("world_notice_daily_cap", 4)),
            ),
            world_notice_ttl_seconds=max(
                60,
                int(memory_raw.get("world_notice_ttl_seconds", 1800)),
            ),
            away_activities_interval_seconds=max(
                30,
                int(memory_raw.get("away_activities_interval_seconds", 1200)),
            ),
            away_activities_cooldown_seconds=max(
                0,
                int(memory_raw.get("away_activities_cooldown_seconds", 5400)),
            ),
            away_activities_daily_cap=max(
                0,
                int(memory_raw.get("away_activities_daily_cap", 6)),
            ),
            away_activities_min_gap_hours=max(
                0.0,
                float(memory_raw.get("away_activities_min_gap_hours", 4.0)),
            ),
            away_activities_journal_max=max(
                1,
                int(memory_raw.get("away_activities_journal_max", 8)),
            ),
            forward_curiosity_interval_seconds=max(
                30,
                int(memory_raw.get("forward_curiosity_interval_seconds", 900)),
            ),
            forward_curiosity_cooldown_seconds=max(
                0,
                int(memory_raw.get("forward_curiosity_cooldown_seconds", 3600)),
            ),
            forward_curiosity_daily_cap=max(
                0,
                int(memory_raw.get("forward_curiosity_daily_cap", 4)),
            ),
            forward_curiosity_min_gap_hours=max(
                0.0,
                float(memory_raw.get("forward_curiosity_min_gap_hours", 4.0)),
            ),
            forward_curiosity_journal_max=max(
                1,
                int(memory_raw.get("forward_curiosity_journal_max", 8)),
            ),
            follow_up_journal_max=max(
                1,
                int(memory_raw.get("follow_up_journal_max", 8)),
            ),
            promise_followthrough_interval_seconds=max(
                30,
                int(
                    memory_raw.get(
                        "promise_followthrough_interval_seconds", 900,
                    )
                ),
            ),
            promise_followthrough_min_age_hours=max(
                0.0,
                float(
                    memory_raw.get("promise_followthrough_min_age_hours", 4.0)
                ),
            ),
            promise_followthrough_cooldown_hours=max(
                0.0,
                float(
                    memory_raw.get("promise_followthrough_cooldown_hours", 6.0)
                ),
            ),
            promise_followthrough_drop_after_days=max(
                1.0,
                float(
                    memory_raw.get(
                        "promise_followthrough_drop_after_days", 14.0,
                    )
                ),
            ),
            promise_fulfil_min_overlap=max(
                1,
                int(memory_raw.get("promise_fulfil_min_overlap", 3)),
            ),
            self_correction_min_confidence=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("self_correction_min_confidence", 0.6)),
                ),
            ),
            self_correction_min_overlap=max(
                1,
                int(memory_raw.get("self_correction_min_overlap", 2)),
            ),
            self_correction_max_candidates=max(
                1,
                int(memory_raw.get("self_correction_max_candidates", 50)),
            ),
            self_correction_cooldown_turns=max(
                0,
                int(memory_raw.get("self_correction_cooldown_turns", 3)),
            ),
            mood_inertia_mismatch_threshold=max(
                0.1,
                float(
                    memory_raw.get("mood_inertia_mismatch_threshold", 0.45)
                ),
            ),
            mood_inertia_cooldown_turns=max(
                0,
                int(memory_raw.get("mood_inertia_cooldown_turns", 3)),
            ),
            memory_extractor_max_tokens=max(
                256,
                int(memory_raw.get("memory_extractor_max_tokens", 1024)),
            ),
            goal_max_active=max(
                1, int(memory_raw.get("goal_max_active", 5)),
            ),
            goal_max_progress_per_goal=max(
                1, int(memory_raw.get("goal_max_progress_per_goal", 12)),
            ),
            goal_reflection_interval_seconds=max(
                60,
                int(memory_raw.get("goal_reflection_interval_seconds", 3600)),
            ),
            conflict_detector_interval_seconds=max(
                60,
                int(
                    memory_raw.get("conflict_detector_interval_seconds", 1800),
                ),
            ),
            conflict_detector_similarity_min=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "conflict_detector_similarity_min", 0.80
                        ),
                    ),
                ),
            ),
            conflict_detector_similarity_max=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "conflict_detector_similarity_max", 0.92
                        ),
                    ),
                ),
            ),
            conflict_detector_auto_resolve_delta=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "conflict_detector_auto_resolve_delta", 0.30
                        ),
                    ),
                ),
            ),
            conflict_detector_max_corpus=max(
                10,
                int(memory_raw.get("conflict_detector_max_corpus", 1000)),
            ),
            conflict_detector_max_pairs_per_run=max(
                1,
                int(
                    memory_raw.get(
                        "conflict_detector_max_pairs_per_run", 50,
                    ),
                ),
            ),
            consolidation_interval_seconds=max(
                60,
                int(memory_raw.get("consolidation_interval_seconds", 21600)),
            ),
            consolidation_lookback_days=max(
                0,
                int(memory_raw.get("consolidation_lookback_days", 30)),
            ),
            consolidation_similarity_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "consolidation_similarity_threshold", 0.90
                        ),
                    ),
                ),
            ),
            consolidation_max_corpus=max(
                10,
                int(memory_raw.get("consolidation_max_corpus", 1000)),
            ),
            consolidation_max_clusters_per_run=max(
                1,
                int(
                    memory_raw.get("consolidation_max_clusters_per_run", 20),
                ),
            ),
            consolidation_min_cluster_size=max(
                2,
                int(memory_raw.get("consolidation_min_cluster_size", 2)),
            ),
            belief_worker_interval_seconds=max(
                60,
                int(memory_raw.get("belief_worker_interval_seconds", 1200)),
            ),
            belief_worker_lookback_turns=max(
                1,
                int(memory_raw.get("belief_worker_lookback_turns", 12)),
            ),
            promise_worker_interval_seconds=max(
                60,
                int(memory_raw.get("promise_worker_interval_seconds", 600)),
            ),
            promise_worker_lookback_turns=max(
                1,
                int(memory_raw.get("promise_worker_lookback_turns", 12)),
            ),
            promise_worker_max_per_run=max(
                1,
                int(memory_raw.get("promise_worker_max_per_run", 5)),
            ),
            promise_worker_max_msg_chars=max(
                200,
                int(memory_raw.get("promise_worker_max_msg_chars", 2000)),
            ),
            promise_worker_max_transcript_chars=max(
                500,
                int(
                    memory_raw.get(
                        "promise_worker_max_transcript_chars", 8000
                    )
                ),
            ),
            belief_gap_valence_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("belief_gap_valence_threshold", 0.30)),
                ),
            ),
            belief_gap_arousal_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("belief_gap_arousal_threshold", 0.25)),
                ),
            ),
            belief_recent_window_hours=max(
                1,
                int(memory_raw.get("belief_recent_window_hours", 24)),
            ),
            belief_stale_after_days=max(
                1,
                int(memory_raw.get("belief_stale_after_days", 90)),
            ),
            belief_max_active_per_user=max(
                10,
                int(memory_raw.get("belief_max_active_per_user", 200)),
            ),
            novelty_window=max(
                2,
                int(memory_raw.get("novelty_window", 12)),
            ),
            novelty_warmup_min=max(
                2,
                int(memory_raw.get("novelty_warmup_min", 3)),
            ),
            novelty_mild_threshold=max(
                0.0,
                min(
                    2.0,
                    float(memory_raw.get("novelty_mild_threshold", 0.35)),
                ),
            ),
            novelty_strong_threshold=max(
                0.0,
                min(
                    2.0,
                    float(memory_raw.get("novelty_strong_threshold", 0.55)),
                ),
            ),
            novelty_cooldown_turns=max(
                0,
                int(memory_raw.get("novelty_cooldown_turns", 2)),
            ),
            stagnation_window=max(
                2,
                int(memory_raw.get("stagnation_window", 6)),
            ),
            stagnation_mild_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("stagnation_mild_threshold", 0.18)),
                ),
            ),
            stagnation_strong_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("stagnation_strong_threshold", 0.10)),
                ),
            ),
            stagnation_cooldown_turns=max(
                0,
                int(memory_raw.get("stagnation_cooldown_turns", 4)),
            ),
            stagnation_post_novelty_suppression_turns=max(
                0,
                int(
                    memory_raw.get(
                        "stagnation_post_novelty_suppression_turns", 3,
                    )
                ),
            ),
            idle_worker_wake_seconds=max(
                1.0, float(memory_raw.get("idle_worker_wake_seconds", 60.0))
            ),
            idle_worker_quiet_threshold_seconds=max(
                0,
                int(memory_raw.get("idle_worker_quiet_threshold_seconds", 30)),
            ),
            idle_worker_tick_budget_ms=max(
                0,
                int(memory_raw.get("idle_worker_tick_budget_ms", 3000)),
            ),
            idle_worker_max_per_tick=max(
                0,
                int(memory_raw.get("idle_worker_max_per_tick", 0)),
            ),
        ),
        chat_llm=_parse_chat_llm(chat_llm_raw),
        llm=_parse_llm(llm_raw),  # populated below if empty
        tools=ToolsSettings(
            enabled=bool(tools_raw.get("enabled", True)),
            get_time=bool(tools_raw.get("get_time", True)),
            recall=bool(tools_raw.get("recall", True)),
            web_search=bool(tools_raw.get("web_search", True)),
            world=bool(tools_raw.get("world", True)),
            goals=bool(tools_raw.get("goals", True)),
            file_tasks=bool(tools_raw.get("file_tasks", True)),
            workflow=bool(tools_raw.get("workflow", True)),
            calculate=bool(tools_raw.get("calculate", True)),
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
