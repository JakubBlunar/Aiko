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
        default_factory=lambda: ["ws", "channel", "settings", "voice"],
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
    # ── Typed-mode proactive (Aiko speaks first in typed chat) ────────
    # Independent timing knobs from the voice-mode ones above so the
    # two cadences can differ. Defaults intentionally long (4 min
    # silence, 10 min cooldown) so a heads-down typed session never
    # gets nag-y. Gated client-side by browser visibility / Tauri
    # window focus — see ``SessionController._user_present``.
    proactive_typed_enabled: bool = True
    proactive_silence_seconds_typed: float = 240.0  # 4 min
    proactive_cooldown_seconds_typed: float = 600.0  # 10 min
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
    # Master switch for :class:`app.core.schedule_learner.ScheduleLearner`,
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
    # :class:`app.core.idle_curiosity_worker.IdleCuriosityWorker`. When
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
    # :class:`app.core.memory_conflict_worker.MemoryConflictWorker`.
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
    belief_worker_per_hour_cap: int = 4
    belief_worker_per_day_cap: int = 20
    # ── K6 personality backlog: surprise / novelty detector ──────────
    # Master switch for :class:`app.core.novelty_detector.NoveltyDetector`.
    # When disabled the detector is never instantiated and the
    # ``novelty`` inner-life provider is left unregistered, so the
    # prompt-assembler short-circuits the block with zero cost on the
    # hot path. The detector itself is purely in-process (one
    # Embedder.embed call per turn + a tiny ring buffer); there's no
    # rate-cap because the per-turn cost is the same as RAG retrieval.
    novelty_detection_enabled: bool = True
    # ── K18 personality backlog: topic stagnation detector ────────────
    # Master switch for
    # :class:`app.core.topic_stagnation.TopicStagnationDetector`.
    # The detector is a pure streak counter over the per-turn distance
    # K6 already computes (no extra embedding) so it's effectively
    # free; this knob exists to silence the cue when a tester wants
    # to focus on K6 alone. Leaving it on with conservative
    # thresholds is the intended default.
    topic_stagnation_enabled: bool = True
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
    # ── Background workers (schema v8) ───────────────────────────────
    # Worker intervals in seconds. Both workers are idempotent: running
    # more often is safe but wastes a little CPU. Drop to ~60 for
    # active testing.
    promotion_worker_interval_seconds: int = 3600
    decay_worker_interval_seconds: int = 3600
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
    # F5: conflicting-memory detector cadence. The all-pairs cosine
    # scan is cheap (NumPy on the in-memory mirror) but the heuristic
    # gate + occasional LLM call adds up, so once an hour is plenty.
    conflict_detector_interval_seconds: int = 3600
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
    # ── K2 personality backlog: theory-of-mind / belief tracking ─────
    # Background inference worker cadence. The worker spends one LLM
    # call per tick to extract beliefs from the last
    # ``belief_worker_lookback_turns`` user turns; once an hour leaves
    # plenty of room between calls without making the model feel
    # forgetful.
    belief_worker_interval_seconds: int = 3600
    # How many recent **user** messages the worker passes to the LLM
    # per extraction. Larger windows give a richer signal but cost
    # more tokens; 12 is enough to span a few conversational beats.
    belief_worker_lookback_turns: int = 12
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
            belief_tracking_enabled=bool(
                agent_raw.get("belief_tracking_enabled", True),
            ),
            belief_worker_enabled=bool(
                agent_raw.get("belief_worker_enabled", True),
            ),
            belief_worker_per_hour_cap=max(
                0, int(agent_raw.get("belief_worker_per_hour_cap", 4)),
            ),
            belief_worker_per_day_cap=max(
                0, int(agent_raw.get("belief_worker_per_day_cap", 20)),
            ),
            novelty_detection_enabled=bool(
                agent_raw.get("novelty_detection_enabled", True),
            ),
            topic_stagnation_enabled=bool(
                agent_raw.get("topic_stagnation_enabled", True),
            ),
            grounding_line_mode=_parse_grounding_line_mode(
                agent_raw.get("grounding_line_mode", "off"),
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
            ui_log_enabled=bool(logging_raw.get("ui_log_enabled", False)),
            ui_log_categories=[
                str(token).strip().lower()
                for token in (
                    logging_raw.get("ui_log_categories")
                    or ["ws", "channel", "settings", "voice"]
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
            decay_max_catchup_days=max(
                1.0, float(memory_raw.get("decay_max_catchup_days", 30.0))
            ),
            promotion_worker_interval_seconds=max(
                10,
                int(memory_raw.get("promotion_worker_interval_seconds", 3600)),
            ),
            decay_worker_interval_seconds=max(
                10, int(memory_raw.get("decay_worker_interval_seconds", 3600))
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
            conflict_detector_interval_seconds=max(
                60,
                int(
                    memory_raw.get("conflict_detector_interval_seconds", 3600),
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
            belief_worker_interval_seconds=max(
                60,
                int(memory_raw.get("belief_worker_interval_seconds", 3600)),
            ),
            belief_worker_lookback_turns=max(
                1,
                int(memory_raw.get("belief_worker_lookback_turns", 12)),
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
        tools=ToolsSettings(
            enabled=bool(tools_raw.get("enabled", True)),
            get_time=bool(tools_raw.get("get_time", True)),
            recall=bool(tools_raw.get("recall", True)),
            web_search=bool(tools_raw.get("web_search", True)),
            world=bool(tools_raw.get("world", True)),
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
            accessory_state=_load_accessory_state(avatar_raw.get("accessory_state")),
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
