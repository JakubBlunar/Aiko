"""Lean session controller for Aiko (witty companion edition).

This is the single hub the UI talks to. It owns:

- Settings + chat database
- Ollama client + TurnRunner (the conversation loop)
- TtsQueue + TTS engine
- Microphone + RealtimeSTT
- Background workers: SummaryWorker, ProactiveDirector
- Embedded MCP server (optional, for Cursor debugging)

The earlier ~2700-line implementation is preserved on the ``legacy-v0`` git
tag if anything needs to be referenced. This rewrite drops:
  - The LangChain agent + tool dispatch + triage judge + autonomy planner
  - Embedding/recent-topics search
  - Live2D avatar
  - Action/agentic UI automation
  - Structured learner profile + 0.5B judge model

Public surface intentionally retains the method names the UI and MCP server
already use, so callers don't have to change.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.audio.client_mic_source import ClientMicSource
from app.audio.earcons import EarconPlayer
from app.core.affect.affect_state import AffectStore, AffectUpdater
from app.core.conversation.backchannel_classifier import BackchannelGate, BackchannelHint
from app.core.infra.chat_database import ChatDatabase
from app.core.affect import circadian as _circadian
from app.core.infra.crash_logging import log_event
from app.core.memory.memory_extractor import MemoryExtractor
from app.core.memory.memory_retriever import MemoryRetriever
from app.core.memory.memory_store import MemoryStore
from app.core.persona.avatar_profile import AvatarProfile, AvatarProfileError, from_disk as _avatar_from_disk
from app.core.proactive.proactive_director import ProactiveDirector
from app.core.session.prompt_assembler import PromptAssembler
from app.core.session import (
    AvatarMixin,
    InnerLifeProvidersMixin,
    MemoryFacadeMixin,
    PostTurnMixin,
    SpeakingWindowJobsMixin,
    TaskOrchestrationMixin,
    WorldMixin,
)
from app.core.world.world_store import WorldStore
from app.core.session.session_text_utils import (
    infer_tts_reaction,
    prepare_tts_text,
    sanitize_user_text,
)
from app.core.infra.settings import (
    AppSettings,
    LLM_ROLE_MAIN_CHAT,
    LLM_ROLE_WORKER_DEFAULT,
    LlmProvider,
    LlmRoute,
    _urls_match,
    persist_user_overrides,
    read_user_overrides,
)
from app.core.voice.speaking_window_scheduler import SpeakingWindowScheduler
from app.core.proactive.summary_worker import SummaryWorker
from app.core.voice.tts_queue import TtsQueue
from app.core.session.turn_runner import TurnRunner
from app.llm.chat_client import ChatClient
from app.llm.embedder import Embedder
from app.llm.factory import ClientCache
from app.llm.ollama_client import OllamaClient
from app.llm.openai_compatible_client import OpenAICompatibleClient
from app.llm.token_utils import estimate_tokens
from app.stt import endpointing as _endpointing
from app.stt.realtime_stt_service import RealtimeSttService


log = logging.getLogger("app.session")


@dataclass(slots=True)
class SessionState:
    mic_enabled: bool
    session_type: str


@dataclass
class _MergeBuffer:
    """Per-session state that lets the next live phrase merge into the
    current in-flight LLM turn instead of bargeing in.

    Set when ``chat_once_streaming`` begins streaming a live-mode turn.
    Cleared on TTS start (window closes), on the merged-restart path,
    on barge-in (existing flow), on session change, and on shutdown.

    Locked via ``SessionController._merge_lock`` because it's read on the
    capture-loop thread (``feed_stt_partial`` early abort) and written on
    the chat thread (``chat_once_streaming`` TTS-start hook).
    """
    session_key: str
    turn_runner: TurnRunner
    user_text: str
    user_message_id: int
    tts_started: bool = False
    awaiting_phrase_b: bool = False


class _BackchannelMotionGate:
    """Per-instance rate-limiter for backchannel-driven motion fan-out.

    Distinct from :class:`BackchannelGate` because:
      - The overlay/expression gate works on the *partial text* (it
        runs the regex classifier itself).
      - This gate works on the already-classified *hint label*, so we
        can independently rate-limit the motion path even when the
        overlay path didn't suppress.

    The 1.5s default mirrors ``BackchannelGate.min_repeat_seconds``
    so a chatty listening window doesn't physically jolt the rig
    every other partial.
    """

    def __init__(self, *, min_repeat_seconds: float = 1.5) -> None:
        self._min_repeat = max(0.0, float(min_repeat_seconds))
        self._last_at: float = 0.0

    def consider(self, *, now: float) -> bool:
        """Return True if a motion may fire now; False if rate-limited."""
        if (now - self._last_at) < self._min_repeat:
            return False
        self._last_at = now
        return True

    def reset(self) -> None:
        self._last_at = 0.0


# ── Provider helpers (env-name fallback for OpenAI-compatible base URLs) ──

_PROVIDER_ENV_HINTS: tuple[tuple[str, str], ...] = (
    ("ollama.com", "OLLAMA_API_KEY"),
    ("api.openai.com", "OPENAI_API_KEY"),
    ("api.groq.com", "GROQ_API_KEY"),
    ("api.x.ai", "XAI_API_KEY"),
    ("openrouter.ai", "OPENROUTER_API_KEY"),
    ("generativelanguage.googleapis.com", "GEMINI_API_KEY"),
)


def _resolve_env_var_name(*, base_url: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    host = (base_url or "").lower()
    for needle, env_name in _PROVIDER_ENV_HINTS:
        if needle in host:
            return env_name
    return ""


# Curated provider preset table. Exposed verbatim via
# ``GET /api/llm/presets`` so the React drawer can render
# self-documenting cards without re-encoding these strings on the
# client. The ``free_tier`` label is intentionally vague (rate limits
# move around quarterly); the goal is to give users a hint, not to
# enforce a hard quota.
_PROVIDER_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "id": "ollama",
        "label": "Local Ollama",
        "provider": "ollama",
        "base_url": "http://127.0.0.1:11434",
        "recommended_models": [
            "llama3.1:8b",
            "qwen2.5:7b",
            "jaahas/qwen3.5-uncensored:9b",
        ],
        "env_hint": "",
        "api_key_required": False,
        "free_tier": "Unlimited (runs on your machine)",
        "docs_url": "https://ollama.com",
        "default_workers_use_local": False,
        # ``None`` -> auto-detect via Ollama's ``/api/show`` per model.
        "default_context_window": None,
    },
    {
        "id": "ollama_cloud",
        "label": "Ollama Cloud",
        "provider": "ollama",
        "base_url": "https://ollama.com",
        "recommended_models": [
            "llama3.1:70b",
            "qwen2.5:72b",
        ],
        "env_hint": "OLLAMA_API_KEY",
        "api_key_required": True,
        "free_tier": "Paid plan required",
        "docs_url": "https://ollama.com/cloud",
        "default_workers_use_local": True,
        "default_context_window": None,
    },
    {
        "id": "gemini",
        "label": "Google Gemini",
        "provider": "openai_compatible",
        "base_url": (
            "https://generativelanguage.googleapis.com/v1beta/openai/"
        ),
        "recommended_models": [
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
        ],
        "env_hint": "GEMINI_API_KEY",
        "api_key_required": True,
        "free_tier": "Free tier: ~15 req/min, ~1500 req/day",
        "docs_url": "https://ai.google.dev",
        "default_workers_use_local": True,
        # 128 k cap from 1-2 M native — see ``_CONTEXT_WINDOW_TABLE``
        # in ``openai_compatible_client.py`` for the rationale.
        "default_context_window": 131_072,
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "provider": "openai_compatible",
        "base_url": "https://api.openai.com/v1",
        # GPT-5 (Aug 2025+) is the default chat suggestion — newer
        # architecture, ~40 % cheaper than 4.1-mini on cached input,
        # 400 k native context. The four-model shortlist matches
        # the user's evaluation set (gpt-5-mini for chat,
        # gpt-5-nano for cheap workers, 4.1 family as fallback).
        # Pricier flagship variants (gpt-5, gpt-5.4-pro, …) still
        # appear in the dropdown via the live ``/v1/models`` response.
        "recommended_models": [
            "gpt-5-mini",
            "gpt-5-nano",
            "gpt-4.1-mini",
            "gpt-4.1-nano",
        ],
        "env_hint": "OPENAI_API_KEY",
        "api_key_required": True,
        "free_tier": "Paid (no free tier)",
        "docs_url": "https://platform.openai.com",
        "default_workers_use_local": True,
        "default_context_window": 131_072,
    },
    {
        "id": "groq",
        "label": "Groq",
        "provider": "openai_compatible",
        "base_url": "https://api.groq.com/openai/v1",
        "recommended_models": [
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "mixtral-8x7b-32768",
        ],
        "env_hint": "GROQ_API_KEY",
        "api_key_required": True,
        "free_tier": "Free tier: 30 req/min",
        "docs_url": "https://console.groq.com",
        "default_workers_use_local": True,
        "default_context_window": 131_072,
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "provider": "openai_compatible",
        "base_url": "https://openrouter.ai/api/v1",
        "recommended_models": [
            "anthropic/claude-3.5-sonnet",
            "openai/gpt-4o-mini",
            "google/gemini-2.5-flash",
        ],
        "env_hint": "OPENROUTER_API_KEY",
        "api_key_required": True,
        "free_tier": "Pay-per-token (some models free)",
        "docs_url": "https://openrouter.ai/docs",
        "default_workers_use_local": True,
        "default_context_window": 131_072,
    },
)


def _build_chat_client(
    *,
    chat_llm: Any,
    ollama_settings: Any,
    role: str,
) -> ChatClient:
    """Factory: pick a concrete chat client for ``chat_llm.provider``.

    ``role`` is one of ``"chat"`` (main TurnRunner path) or ``"worker"``
    (background workers). It's used only for logging clarity — the two
    callers in :class:`SessionController` go through the same code
    path and only diverge on whether ``chat_llm.workers_use_local``
    forces a local fallback.

    Resolves the API key in this order:
    1. ``chat_llm.api_key`` (explicit override)
    2. ``os.environ[chat_llm.api_key_env or inferred]``
    """
    base_url = (chat_llm.base_url or "").strip() or ollama_settings.base_url
    api_key_explicit = (chat_llm.api_key or "").strip()
    api_key_env_name = _resolve_env_var_name(
        base_url=base_url,
        explicit=(chat_llm.api_key_env or "").strip(),
    )
    api_key = api_key_explicit or os.environ.get(
        api_key_env_name, "",
    ).strip()
    extra_headers = {
        str(k).strip(): str(v).strip()
        for k, v in dict(chat_llm.extra_headers or {}).items()
        if str(k).strip() and v is not None
    }
    provider = (chat_llm.provider or "ollama").strip().lower()
    if provider == "openai_compatible":
        model = (chat_llm.model or "").strip()
        if not model:
            # Empty model = config not finished yet. Falling through to
            # a local Ollama client keeps the boot healthy until the
            # user picks one in the drawer.
            log.warning(
                "chat_llm.provider=openai_compatible but model is empty; "
                "falling back to local Ollama for role=%s. Configure "
                "chat_llm.model in user.json or via Settings → Chat.",
                role,
            )
            return OllamaClient(
                ollama_settings,
                base_url=base_url,
                api_key=api_key or None,
                extra_headers=extra_headers or None,
                keep_alive=chat_llm.keep_alive,
            )
        return OpenAICompatibleClient(
            ollama_settings,
            base_url=base_url,
            api_key=api_key or None,
            model=model,
            extra_headers=extra_headers or None,
            keep_alive=chat_llm.keep_alive,
        )
    # Default path: Ollama (local or cloud, distinguished only by base_url).
    return OllamaClient(
        ollama_settings,
        base_url=base_url,
        api_key=api_key or None,
        extra_headers=extra_headers or None,
        keep_alive=chat_llm.keep_alive,
    )


def _seed_avatar_root_if_empty(avatar_root: Path) -> None:
    """Copy the Live2D bundle from the source tree into the runtime path.

    Mirrors ``scripts/setup-macos.sh``: when the configured avatar
    directory is missing or empty, look for a matching bundle under
    ``<repo>/live-2d-models/<name>/`` and copy its contents in.

    This makes the Windows / Linux ``npm run desktop`` flow self-healing
    (no setup-macos.sh equivalent) and recovers from a user manually
    cleaning out ``data/personas/`` to shrink the working tree. Silent
    no-op if either the target is already populated or no source bundle
    exists — the regular ``_avatar_from_disk`` call downstream will then
    surface the "not loaded" payload to the frontend.
    """
    try:
        if avatar_root.exists() and any(avatar_root.iterdir()):
            return
    except OSError:
        return

    repo_root = Path(__file__).resolve().parents[3]
    source = repo_root / "live-2d-models" / avatar_root.name
    if not source.is_dir():
        return

    import shutil

    avatar_root.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        target = avatar_root / child.name
        if target.exists():
            continue
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    log.info(
        "Seeded Live2D bundle into %s from %s",
        avatar_root,
        source,
    )


# ── Controller ─────────────────────────────────────────────────────────


class SessionController(
    AvatarMixin,
    MemoryFacadeMixin,
    WorldMixin,
    InnerLifeProvidersMixin,
    SpeakingWindowJobsMixin,
    PostTurnMixin,
    TaskOrchestrationMixin,
):
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._user_id = (settings.assistant.user_id or "default").strip() or "default"
        # Restore the session the user was last viewing so closing the
        # browser tab (or the whole app) doesn't snap them back to the
        # primordial "main" conversation. Persistence happens in
        # ``switch_session`` — see ``_resolve_initial_session_id`` for
        # the fallback chain.
        self._session_id = self._resolve_initial_session_id(default="main")

        # ── Chat LLM clients (provider-aware split) ──────────────────────
        # Two clients live side by side:
        #   - ``self._chat_client`` is the user-visible path (TurnRunner +
        #     ProactiveDirector). Routes through whatever ``chat_llm.provider``
        #     points at — local Ollama, Ollama Cloud, or any OpenAI-compatible
        #     endpoint (Gemini, OpenAI, Groq, OpenRouter, ...).
        #   - ``self._worker_client`` is the background-worker path
        #     (reflection, dream, belief, ~24 workers in total). Defaults to
        #     a local Ollama instance so a switch to Gemini doesn't drain its
        #     1500-req/day free tier within the hour; set
        #     ``chat_llm.workers_use_local = False`` to share the chat
        #     client instead.
        # ``self._ollama`` is a back-compat alias for the worker client — too
        # many older test patches and a few external scripts reach in for
        # it for us to rename in this round.
        chat_llm = settings.chat_llm
        # PR 2: shared client cache for the provider catalogue. Routes
        # pointing at the same provider share one underlying ChatClient
        # so credentials / TCP pool / TLS cost are paid once. The
        # legacy code path below builds its own clients without the
        # cache for unchanged back-compat; the new public methods
        # (``update_route``, ``test_provider``, …) go through the
        # cache via :func:`app.llm.factory.build_client_for_route`.
        self._client_cache = ClientCache(settings.ollama)
        self._chat_client: ChatClient = _build_chat_client(
            chat_llm=chat_llm,
            ollama_settings=settings.ollama,
            role="chat",
        )
        if (
            (chat_llm.provider or "ollama").strip().lower() != "ollama"
            and bool(getattr(chat_llm, "workers_use_local", True))
        ):
            # Workers stay on a local Ollama instance with the configured
            # base_url ignored — we use the canonical OllamaSettings.base_url
            # (typically http://127.0.0.1:11434) so the user doesn't have
            # to set two URLs.
            self._worker_client: ChatClient = OllamaClient(
                settings.ollama,
                base_url=settings.ollama.base_url,
                keep_alive=chat_llm.keep_alive,
            )
        else:
            # Either pure Ollama (one client serves both roles) or the
            # user explicitly opted workers into the remote provider.
            self._worker_client = self._chat_client
        self._ollama = self._worker_client  # back-compat alias
        self._chat_provider = (chat_llm.provider or "ollama").strip().lower()

        chat_model_override = (chat_llm.model or "").strip()
        self._effective_chat_model = (
            chat_model_override
            or (settings.ollama.chat_model or "").strip()
            or "llama3.1:8b"
        )
        # Workers route through ``self._worker_client``; when that's a
        # separate local Ollama instance (the common "chat = OpenAI,
        # workers = local" case) the worker model MUST come from
        # ``settings.ollama.chat_model`` — sending the chat model
        # name (e.g. ``gpt-5-mini``) to a local Ollama 404s with
        # ``model 'gpt-5-mini' not found``. When the worker client
        # IS the chat client (pure-Ollama or ``workers_use_local=False``),
        # both models collapse to the same value.
        if self._worker_client is self._chat_client:
            self._effective_worker_model = self._effective_chat_model
        else:
            self._effective_worker_model = (
                (settings.ollama.chat_model or "").strip()
                or "llama3.1:8b"
            )

        # Resolve context window: explicit config override > Ollama /api/show > fallback.
        # ``self._context_source`` records which path won (used for stats display).
        ctx_override = chat_llm.context_window or getattr(
            settings.ollama, "context_window", None
        )
        self._context_window, self._context_source = self._resolve_context_window(
            ctx_override, self._effective_chat_model,
        )
        self._max_tokens = max(64, int(chat_llm.max_tokens or 512))
        temp = chat_llm.temperature
        if temp is None:
            temp = float(settings.ollama.temperature)
        self._temperature = float(temp)

        # ── Database ─────────────────────────────────────────────────────
        storage_path = (
            Path(__file__).resolve().parents[3] / "data" / "chat_sessions.db"
        )
        self._chat_db = ChatDatabase(storage_path)

        # ── Live2D avatar (fixed Alexia bundle) ─────────────────────────
        # Replaces the old upload-based persona pipeline. The avatar root
        # is gitignored so missing files at boot have to degrade
        # gracefully — fall through with ``self._avatar = None`` and
        # the FastAPI / WS layers serve a minimal "not loaded" payload
        # instead of crashing the controller.
        avatar_root = Path(settings.avatar.root_dir)
        if not avatar_root.is_absolute():
            avatar_root = Path(__file__).resolve().parents[3] / avatar_root
        self._avatar_root: Path = avatar_root
        # If the runtime path is missing/empty (common on Windows where
        # there's no setup-macos.sh seeding step, or after a manual
        # cleanup of ``data/personas/``), seed from the source bundle at
        # ``live-2d-models/<name>/`` automatically. Mirrors the macOS
        # setup script so all platforms self-heal on boot.
        try:
            _seed_avatar_root_if_empty(avatar_root)
        except Exception as exc:  # noqa: BLE001 - never block boot on seeding
            log.warning("Avatar auto-seed failed (%s); continuing", exc)
        try:
            self._avatar: AvatarProfile | None = _avatar_from_disk(
                avatar_root, display_name=Path(settings.avatar.entry_filename).stem,
            )
        except AvatarProfileError as exc:
            log.warning("Avatar load failed (%s); rendering will be disabled", exc)
            self._avatar = None
        # User-tunable knobs that layer on top of the immutable profile.
        # ``accessory_state`` is the Phase 4 (expression overhaul)
        # persistent-accessory cache — keys are validated against the
        # current rig's capabilities by ``update_avatar_accessories``
        # so a saved ``lollipop: true`` on a model without
        # ``has_lollipop`` doesn't silently render nothing.
        self._avatar_settings_runtime = {
            "scale_multiplier": float(settings.avatar.scale_multiplier),
            "auto_outfit": str(settings.avatar.auto_outfit),
            "expressiveness": float(settings.avatar.expressiveness),
            "accessory_state": dict(settings.avatar.accessory_state or {}),
        }
        self._avatar_settings_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._avatar_overlay_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._avatar_motion_listeners: list[Callable[[dict[str, Any]], None]] = []
        # K31 soft physicality. ``_avatar_touch_listeners`` carries the
        # WS-broadcast listener (registered by ``app/web/server.py``);
        # ``_current_turn_gestures`` is the per-turn accumulator that
        # the post-turn pass seals onto ``messages.gestures``. Cleared
        # at the start of every typed/voice turn so a previous turn's
        # gestures never leak into the next bubble.
        self._avatar_touch_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._current_turn_gestures: list[str] = []
        # K32 user reactions: queue of recently-applied ``(message_id, kind)``
        # tuples drained by the ``user_reactions`` inner-life provider
        # on Aiko's next turn. Bounded so a frantic clicker can't pin
        # the prompt cue to "Jacob just reacted x500 times".
        from collections import deque
        self._pending_user_reactions: deque[tuple[int, str]] = deque(maxlen=10)
        # K32 broadcast listeners. ``app/web/server.py`` registers a
        # ``message_reaction_updated`` broadcaster here so both
        # webviews re-render the reaction strip.
        self._message_reaction_listeners: list[
            Callable[[dict[str, Any]], None]
        ] = []
        # LLM-driven sticky outfit override. Set when the assistant says
        # ``[[outfit:NAME]]`` mid-reply; cleared when the circadian
        # period rolls over OR when the user manually flips
        # ``auto_outfit`` to anything non-``"auto"``. Stored alongside
        # the period that was active when the override landed so we
        # can detect a flip without subscribing to circadian events.
        self._llm_outfit_override: str = ""
        self._llm_outfit_override_period: str = ""

        # ── Affect state (Phase 2b) ───────────────────────────────────────
        # Persistent valence/arousal + named mood, updated post-turn (cheap
        # math, no LLM). Read on the hot path by PromptAssembler to inject
        # a tiny ambient block. ``mood_state`` WS event lets the avatar tint
        # its idle motion.
        self._affect_store = AffectStore(self._chat_db)
        self._affect_updater = AffectUpdater(self._affect_store)
        self._mood_listeners: list[Callable[[dict[str, Any]], None]] = []

        # ── Backchannel classifier (Phase 1a) ────────────────────────────
        # Regex-only — pure CPU, <1ms — runs on every stt_partial event.
        # The gate rate-limits identical hints so the avatar overlay doesn't
        # spam the same expression mid-phrase.
        self._backchannel_gate = BackchannelGate(min_repeat_seconds=1.5)
        self._backchannel_listeners: list[
            Callable[[BackchannelHint, str], None]
        ] = []
        # Phase B2 — backchannel-driven *motion* broadcast. Separate
        # from the gate the *overlay/expression* path runs through
        # because we want a slightly different rate (the overlay is a
        # cheap visual fade, but a motion physically moves the rig
        # and we want at most one every 1.5s).
        self._backchannel_motion_gate = _BackchannelMotionGate(
            min_repeat_seconds=1.5,
        )
        # Alternation counter for ``thinking`` -> tilt_left vs
        # tilt_right. Even -> left, odd -> right.
        self._backchannel_thinking_index = 0
        self._stt_partial_listeners: list[Callable[[str], None]] = []
        # Most recent partial we observed during the current live phrase,
        # keyed by session_key. ``process_live_capture`` reads it to fire
        # one final RAG prefetch right before transcribe(wav) so retrieval
        # runs in parallel with Whisper.
        self._last_live_partial: dict[str, str] = {}
        # Throttle for the WS partial broadcast so a 5 Hz cap doesn't
        # require touching every listener implementation.
        self._last_partial_broadcast_at: float = 0.0

        # ── Voice utterance merge ───────────────────────────────────────
        # When the user pauses mid-thought ("Hey aiko how … are you doing
        # today"), the endpointer commits phrase A and the LLM starts
        # streaming. If a partial of phrase B arrives before TTS has
        # started speaking, we abort the in-flight turn, merge the texts
        # into the existing user row, and re-run with the combined text.
        # See ``feed_stt_partial`` (early abort) and ``process_live_capture``
        # (merge branch) for the runtime flow.
        self._merge_buffer: dict[str, _MergeBuffer] = {}
        self._merge_lock = threading.Lock()

        # ── Vocal tone (Phase 1a) ────────────────────────────────────────
        # The most recent vocal-tone signal produced by ``analyse_wav``
        # in ``process_live_capture``. Read by the prompt provider and by
        # ``_post_turn_inner_life`` so AffectUpdater can react. Reset to
        # ``None`` after the turn closes so a typed turn or a long pause
        # doesn't replay stale paralinguistics.
        self._last_vocal_tone: Any = None
        self._vocal_tone_lock = threading.Lock()

        # ── Long-term memory (cross-session) ─────────────────────────────
        self._memory_settings = settings.memory
        self._embedder: Embedder | None = None
        self._memory_store: MemoryStore | None = None
        self._memory_retriever: MemoryRetriever | None = None
        self._memory_extractor: MemoryExtractor | None = None
        self._memory_listeners: list[Callable[[Any], None]] = []
        # Identity-rename listeners (workers cache the display name in
        # pre-built prompt strings and re-render on this event).
        self._identity_listeners: list[Callable[[str], None]] = []
        # RAG: LanceDB-backed retrieval substrate. Owned by SessionController
        # so it can be shared with MessageIndexer and DocumentIngestor.
        self._rag_store = None  # type: ignore[var-annotated]
        if self._memory_settings.enabled:
            try:
                self._embedder = Embedder(settings.ollama)
                self._memory_store = MemoryStore(
                    storage_path,
                    max_memories=self._memory_settings.max_memories,
                    scratchpad_cap=self._memory_settings.scratchpad_cap,
                    archive_cap=self._memory_settings.archive_cap,
                    dedupe_threshold=self._memory_settings.dedupe_threshold,
                )
                # Boot RAG store (best-effort -- if probe / Lance fail, we
                # gracefully fall back to the SQLite path).
                try:
                    from app.core.rag.rag_store import auto_open as _rag_auto_open

                    rag_root = (
                        Path(__file__).resolve().parents[3] / "data" / "lancedb"
                    )
                    self._rag_store = _rag_auto_open(
                        rag_root,
                        embedder_model=self._embedder.model,
                        embedder_probe=self._embedder,
                    )
                except Exception:
                    log.warning("RAG bring-up failed", exc_info=True)
                    self._rag_store = None
                if self._rag_store is not None:
                    try:
                        self._memory_store.attach_rag_store(self._rag_store)
                        self._memory_store.migrate_to_rag(self._rag_store)
                    except Exception:
                        log.warning("memory -> RAG migration failed", exc_info=True)
                # Hook the chat-message indexer for live + backfill embedding.
                self._message_indexer = None
                if self._rag_store is not None and self._embedder is not None:
                    try:
                        from app.core.rag.message_indexer import MessageIndexer

                        self._message_indexer = MessageIndexer(
                            self._chat_db, self._rag_store, self._embedder,
                        )
                        self._message_indexer.start(backfill=True)
                    except Exception:
                        log.warning("MessageIndexer failed to start", exc_info=True)
                        self._message_indexer = None
                self._memory_retriever = MemoryRetriever(
                    self._memory_store,
                    self._embedder,
                    top_k=self._memory_settings.top_k,
                    score_threshold=self._memory_settings.score_threshold,
                )
                # RagRetriever is the new read path; keeps the legacy
                # MemoryRetriever as a fallback inside PromptAssembler.
                self._rag_retriever = None
                if self._rag_store is not None:
                    try:
                        from app.core.rag.rag_retriever import RagRetriever

                        # H1 + K4 providers: read the live arc state per
                        # retrieve() and run the regex hot-path tagger
                        # on the query text. Wired here so the retriever
                        # stays self-contained and PromptAssembler doesn't
                        # need to know about either subsystem.
                        def _arc_state_provider() -> Any:
                            store = getattr(self, "_arc_store", None)
                            if store is None:
                                return None
                            try:
                                return store.get(self._user_id)
                            except Exception:
                                return None

                        def _dialogue_act_provider(text: str) -> str | None:
                            try:
                                from app.core.conversation.dialogue_act_tagger import tag_regex
                            except Exception:
                                return None
                            try:
                                return tag_regex(text).act
                            except Exception:
                                return None

                        self._rag_retriever = RagRetriever(
                            self._rag_store,
                            self._embedder,
                            top_k=self._memory_settings.top_k,
                            score_threshold=self._memory_settings.score_threshold,
                            memory_store=self._memory_store,
                            chat_db=self._chat_db,
                            arc_state_provider=_arc_state_provider,
                            dialogue_act_provider=_dialogue_act_provider,
                            fade_hedge_enabled=getattr(
                                self._memory_settings, "fade_hedge_enabled", True,
                            ),
                            faded_salience_threshold=getattr(
                                self._memory_settings,
                                "faded_salience_threshold",
                                0.20,
                            ),
                            faded_idle_days=getattr(
                                self._memory_settings, "faded_idle_days", 30,
                            ),
                            confidence_time_decay_enabled=bool(
                                getattr(
                                    self._settings.agent,
                                    "confidence_time_decay_enabled",
                                    True,
                                )
                            ),
                            confidence_decay_horizon_days=getattr(
                                self._memory_settings,
                                "confidence_decay_horizon_days",
                                365,
                            ),
                            confidence_decay_floor=getattr(
                                self._memory_settings,
                                "confidence_decay_floor",
                                0.3,
                            ),
                            confidence_decay_distant_threshold=getattr(
                                self._memory_settings,
                                "confidence_decay_distant_threshold",
                                0.5,
                            ),
                        )
                    except Exception:
                        log.warning("RagRetriever failed to init", exc_info=True)
                        self._rag_retriever = None
                # DocumentIngestor: lets users upload notes / PDFs that get
                # indexed into the same RagStore.
                self._document_ingestor = None
                if self._rag_store is not None and self._embedder is not None:
                    try:
                        from app.core.rag.document_ingestor import DocumentIngestor

                        docs_root = (
                            Path(__file__).resolve().parents[3] / "data" / "documents"
                        )
                        self._document_ingestor = DocumentIngestor(
                            self._rag_store,
                            self._embedder,
                            storage_root=docs_root,
                        )
                    except Exception:
                        log.warning("DocumentIngestor failed to init", exc_info=True)
                        self._document_ingestor = None
            except Exception:
                log.warning("memory subsystem failed to initialise", exc_info=True)
                self._embedder = None
                self._memory_store = None
                self._memory_retriever = None
                self._rag_store = None
                self._message_indexer = None
                self._rag_retriever = None
                self._document_ingestor = None
        else:
            self._message_indexer = None
            self._rag_retriever = None
            self._document_ingestor = None

        # ── Aiko's room (virtual world) ──────────────────────────────────
        # Small persistent world model: locations, items, and a singleton
        # row holding Aiko's current location/posture/activity. Drives the
        # "world" inner-life prompt block and a handful of agent tools
        # (look_around / move_to / consume / ...). Seeded with a rich
        # default room on first boot so Aiko has a sense of place from
        # turn one.
        self._world_store: WorldStore | None = None
        self._world_listeners: list[Callable[[dict[str, Any]], None]] = []
        try:
            self._world_store = WorldStore(storage_path)
            try:
                self._world_store.seed_default(
                    user_display_name=self.user_display_name,
                )
            except Exception:
                log.warning("world seed_default failed", exc_info=True)
            # Additive migration: older worlds were seeded before the
            # garden existed. ``ensure_garden_seed`` is idempotent so
            # calling it on every boot is safe for both paths.
            try:
                self._world_store.ensure_garden_seed()
            except Exception:
                log.warning("world ensure_garden_seed failed", exc_info=True)
        except Exception:
            log.warning("WorldStore failed to initialise", exc_info=True)
            self._world_store = None

        # ── TTS engine + queue ───────────────────────────────────────────
        # Audio frame listener hook — wired by the web server when the
        # WS hub is built. Until then PCM is discarded (used by the
        # CLI / tests without a connected client).
        self._audio_frame_listener: Callable[[str, int, int, bytes], None] | None = None
        self._audio_frame_end_listener: Callable[[str], None] | None = None

        self._tts_engine = self._build_tts_service(settings)
        # Earcon player must be created before TtsQueue so the queue can
        # use it for stage-direction splicing (Phase 1c). Construction
        # is cheap (no I/O until the first tone is requested).
        self._earcons = EarconPlayer(
            enabled=getattr(settings.audio, "earcons_enabled", True),
        )
        self._tts_engine.set_pcm_listener(
            lambda rate, ch, pcm: self._emit_audio_frame("tts", rate, ch, pcm),
            end_listener=lambda: self._emit_audio_frame_end("tts"),
        )
        self._earcons.set_pcm_listener(
            lambda rate, ch, pcm: self._emit_audio_frame("earcon", rate, ch, pcm),
            end_listener=lambda: self._emit_audio_frame_end("earcon"),
        )
        self._tts = TtsQueue(
            self._tts_engine,
            enabled=bool(settings.tts.enabled),
            state_listener=self._on_tts_state,
            earcon_player=self._earcons,
        )
        # Phase 5b: ProsodyDispatcher wraps tts.enqueue with per-sentence
        # cadence. Context provider is wired below once affect/circadian
        # are available.
        try:
            from app.core.voice.cadence import ProsodyDispatcher

            self._prosody = ProsodyDispatcher(
                self._tts.enqueue,
                enabled=bool(settings.agent.cadence_enabled),
                earcon_auto_sprinkle=bool(
                    getattr(settings.agent, "earcon_auto_sprinkle", True),
                ),
            )
            # Layer 2: real timed pauses. Wire the queue-side
            # silence provider so the cadence dispatcher's
            # ``ProsodyParams.pause_*_ms`` produce actual silent PCM
            # gaps instead of just punctuation rewrites.
            try:
                self._prosody.set_silence_provider(self._tts.enqueue_silence)
            except Exception:
                log.debug("silence provider wire failed", exc_info=True)
            # Layer 4: auto-sprinkle soft breath / sigh on opener
            # sentences of sad turns. Same earcon path the LLM uses
            # for inline ``[[breath]]`` etc. — just driven by cadence.
            try:
                self._prosody.set_earcon_provider(self._tts.enqueue_earcon)
            except Exception:
                log.debug("earcon provider wire failed", exc_info=True)
        except Exception:
            log.warning("ProsodyDispatcher init failed", exc_info=True)
            self._prosody = None
        self._apply_assistant_preferences()

        # ── Microphone + STT ─────────────────────────────────────────────
        # The client streams mic PCM as 0x01/0x02 WS frames; the WS
        # layer calls ``feed_audio_frame`` / ``feed_audio_start`` /
        # ``feed_audio_end`` on us which forwards into the source.
        self._microphone = ClientMicSource(settings.audio)
        self._realtime_stt = RealtimeSttService(settings.stt, settings.audio)

        # ── Prompt + workers + runner ────────────────────────────────────
        self_image_path = (
            Path(__file__).resolve().parents[3] / "data" / "persona" / "self_image.txt"
        )
        self._prompt_assembler = PromptAssembler(
            self._chat_db,
            memory_retriever=self._memory_retriever,
            rag_retriever=getattr(self, "_rag_retriever", None),
            self_image_path=self_image_path,
            history_age_prefix_enabled=bool(
                getattr(self._settings.agent, "history_age_prefix_enabled", True)
            ),
        )


        # Listening-window telemetry: extensions counter set by
        # ``capture_live_phrase`` and consumed by ``TurnRunner`` for the
        # "turn done:" log line. Reset per phrase.
        self._last_listen_extensions: int = 0

        # Phase 1b: speculative RAG pre-fetcher. While the user is still
        # talking (stt_partial events), we kick off background retrieval so
        # the prompt build can reuse the result on the hot path.
        self._rag_prefetcher = None
        if getattr(self, "_rag_retriever", None) is not None:
            try:
                from app.core.rag.rag_prefetcher import RagPrefetcher

                self._rag_prefetcher = RagPrefetcher(
                    self._rag_retriever,
                    ttl_seconds=30.0,
                    debounce_ms=400,
                    min_partial_chars=12,
                    similarity_threshold=0.55,
                    user_display_name_provider=lambda: self.user_display_name,
                )
                self._prompt_assembler.set_rag_prefetch_lookup(
                    self._lookup_prefetched_rag_block,
                )
            except Exception:
                log.warning("RagPrefetcher init failed", exc_info=True)
                self._rag_prefetcher = None

        # Wire the display-name resolver lazily so RAG block headers
        # (``What you know about <name>``) reflect onboarding edits on
        # the very next turn without a re-init.
        try:
            self._prompt_assembler.set_user_display_name_provider(
                lambda: self.user_display_name,
            )
        except Exception:
            log.debug("set_user_display_name_provider failed", exc_info=True)

        # Phase 3 of listening_window_prefetch: small 1-worker executor for
        # cheap RAM/SQLite pre-warm tasks (static prompt slice rebuilds)
        # so the capture loop thread isn't blocked. Separate from
        # ``_rag_prefetcher`` because that one is sized for RAG retrieval
        # latency and we don't want prompt prebuilds queued behind it.
        try:
            from concurrent.futures import ThreadPoolExecutor

            self._listening_window_executor: ThreadPoolExecutor | None = (
                ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="listen-prebuild",
                )
            )
        except Exception:
            self._listening_window_executor = None
        # Track whether a prebuild is already in-flight so we don't pile up
        # duplicates from rapid partial updates.
        self._prebuild_in_flight: bool = False

        # Phase 2c: ReflectionWorker — LLM journal that runs inside the
        # speaking window at low priority. Writes open_question / callback
        # / reflection memories that the RAG retriever surfaces later.
        self._reflection_worker = None
        try:
            from app.core.proactive.reflection_worker import ReflectionWorker

            self._reflection_worker = ReflectionWorker(
                ollama=self._ollama,
                memory_store=self._memory_store,
                embedder=self._embedder,
                model=self._effective_worker_model,
                min_seconds_between=settings.agent.reflection_min_seconds_between,
                emotional_delta_threshold=settings.agent.reflection_emotional_delta_threshold,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("ReflectionWorker init failed", exc_info=True)
            self._reflection_worker = None

        # Phase 2b: DreamWorker — bootstrap-time reflection that runs
        # once per app start when the gap since the last assistant turn
        # exceeds a threshold. Writes a kind=reflection memory tagged
        # ``[dream]`` so the resume opener / NarrativeWeaver can prefer
        # it as a candidate when seeding the welcome-back line.
        self._dream_worker = None
        if bool(getattr(settings.agent, "dream_worker_enabled", True)):
            try:
                from app.core.proactive.dream_worker import DreamWorker

                self._dream_worker = DreamWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    model=self._effective_worker_model,
                    chat_db=self._chat_db,
                    min_hours_since_last=float(
                        getattr(
                            settings.agent,
                            "dream_worker_min_hours_since_last", 6.0,
                        ),
                    ),
                    user_display_name_provider=lambda: self.user_display_name,
                )
            except Exception:
                log.warning("DreamWorker init failed", exc_info=True)
                self._dream_worker = None

        # Phase 2c: CatchphraseMiner — speaking-window job that mines
        # recurring 3-7-word phrases across recent user + assistant
        # turns. Surfaced via the catchphrase inner-life block.
        self._catchphrase_miner = None
        if bool(getattr(settings.agent, "catchphrase_miner_enabled", True)):
            try:
                from app.core.memory.catchphrase_miner import CatchphraseMiner

                self._catchphrase_miner = CatchphraseMiner(
                    chat_db=self._chat_db,
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    min_seconds_between=float(
                        getattr(
                            settings.agent,
                            "catchphrase_miner_min_seconds_between", 600.0,
                        ),
                    ),
                    min_new_user_turns=int(
                        getattr(
                            settings.agent,
                            "catchphrase_miner_min_new_user_turns", 6,
                        ),
                    ),
                    min_total_count=int(
                        getattr(
                            settings.agent,
                            "catchphrase_miner_min_total_count", 3,
                        ),
                    ),
                )
            except Exception:
                log.warning("CatchphraseMiner init failed", exc_info=True)
                self._catchphrase_miner = None

        # Phase 4b: ambient-noise tracker. EMAs the mic floor during
        # silence-only chunks so the prompt + Pocket-TTS know whether
        # the room is quiet, hums, or is loudly noisy. Optional: the
        # capture path is a no-op if the tracker is None.
        self._ambient_noise = None
        try:
            from app.core.affect.ambient_noise import AmbientNoiseTracker

            self._ambient_noise = AmbientNoiseTracker()
        except Exception:
            log.warning("AmbientNoiseTracker init failed", exc_info=True)
            self._ambient_noise = None

        # Phase 4c: CuriosityWorker — emits a small "next-turn"
        # follow-up question into the open_question store when the
        # current arc is shallow and the user hasn't been asking much.
        self._curiosity_worker = None
        if bool(getattr(settings.agent, "curiosity_worker_enabled", True)):
            try:
                from app.core.proactive.curiosity_worker import CuriosityWorker

                self._curiosity_worker = CuriosityWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    model=self._effective_worker_model,
                    min_turns_between=int(
                        getattr(settings.agent, "curiosity_worker_min_turns_between", 3),
                    ),
                    min_seconds_between=float(
                        getattr(settings.agent, "curiosity_worker_min_seconds_between", 60.0),
                    ),
                    max_user_word_count=int(
                        getattr(settings.agent, "curiosity_worker_max_user_word_count", 8),
                    ),
                    user_display_name_provider=lambda: self.user_display_name,
                )
            except Exception:
                log.warning("CuriosityWorker init failed", exc_info=True)
                self._curiosity_worker = None

        # Phase 3b: relationship tracker (turn / session counters + phase
        # + milestones). Hot-path safe: a single SQLite row per user.
        self._relationship_store = None
        self._relationship_tracker = None
        try:
            from app.core.relationship.relationship import (
                RelationshipStore, RelationshipTracker,
            )
            self._relationship_store = RelationshipStore(self._chat_db)
            self._relationship_tracker = RelationshipTracker(
                self._relationship_store,
            )
            # Bump session counter on init.
            try:
                self._relationship_tracker.register_session_start(self._user_id)
            except Exception:
                log.debug("relationship session start failed", exc_info=True)
        except Exception:
            log.warning("RelationshipTracker init failed", exc_info=True)
            self._relationship_store = None
            self._relationship_tracker = None

        # Phase 4a: agenda store + LLM grooming worker.
        self._agenda_store = None
        self._agenda_worker = None
        try:
            from app.core.goals.agenda import AgendaStore, AgendaWorker

            self._agenda_store = AgendaStore(self._chat_db)
            self._agenda_worker = AgendaWorker(
                ollama=self._ollama,
                store=self._agenda_store,
                model=self._effective_worker_model,
                every_n_turns=settings.agent.agenda_groom_every_n_turns,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("AgendaStore/AgendaWorker init failed", exc_info=True)
            self._agenda_store = None
            self._agenda_worker = None

        # Phase 3c: promise extractor (regex post-turn + LLM speaking-window).
        # Both tracks persist promises as ``kind="promise"`` memories so RAG
        # surfaces them naturally; ProactiveDirector benefits implicitly.
        self._promise_extractor = None
        try:
            from app.core.memory.promise_extractor import PromiseExtractor

            self._promise_extractor = PromiseExtractor(
                ollama=self._ollama,
                memory_store=self._memory_store,
                embedder=self._embedder,
                model=self._effective_worker_model,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("PromiseExtractor init failed", exc_info=True)
            self._promise_extractor = None

        # K4: per-turn dialogue-act tagger. Regex hot path runs inline
        # in ``_post_turn_inner_life``; the LLM cold path (~3 user-turn
        # cadence) upgrades any low-confidence regex result on the
        # speaking-window scheduler.
        self._dialogue_act_tagger = None
        try:
            from app.core.conversation.dialogue_act_tagger import DialogueActTagger

            self._dialogue_act_tagger = DialogueActTagger(
                ollama=self._ollama,
                chat_db=self._chat_db,
                model=self._effective_worker_model,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("DialogueActTagger init failed", exc_info=True)
            self._dialogue_act_tagger = None

        # Phase 3a: structured user profile + per-turn user-state estimator.
        # The store is hot-path-safe (small SQL reads) and the estimator
        # runs after every turn (regex only). The worker is LLM-driven and
        # only fires every N user turns inside the speaking window.
        self._user_profile_store = None
        self._user_profile_worker = None
        self._user_state_store = None
        self._user_state_estimator = None
        try:
            from app.core.infra.user_profile import (
                UserProfileStore, UserProfileWorker,
            )
            from app.core.affect.user_state import UserStateEstimator, UserStateStore

            self._user_profile_store = UserProfileStore(self._chat_db)
            self._user_state_store = UserStateStore(self._chat_db)
            self._user_state_estimator = UserStateEstimator(self._user_state_store)
            self._user_profile_worker = UserProfileWorker(
                ollama=self._ollama,
                db=self._chat_db,
                store=self._user_profile_store,
                model=self._effective_worker_model,
                min_user_turns=settings.agent.user_profile_min_turns,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("user-profile / user-state init failed", exc_info=True)
            self._user_profile_store = None
            self._user_profile_worker = None
            self._user_state_store = None
            self._user_state_estimator = None

        # Phase 2d: daily self-image pulse + pinned top-self-memories.
        # The pulse rebuilds data/persona/self_image.txt at most once per
        # ~20h. Pinned bullets get folded into the prompt every turn so we
        # don't depend on the file existing yet.
        self._self_image_pulse_enabled = bool(
            settings.agent.self_image_pulse_enabled
        )
        self._self_image_worker = None
        if self._self_image_pulse_enabled:
            try:
                from app.core.persona.self_image_worker import SelfImageWorker

                self._self_image_worker = SelfImageWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    target_path=self_image_path,
                    model=self._effective_worker_model,
                    max_tokens=settings.agent.self_image_max_tokens,
                )
            except Exception:
                log.warning("SelfImageWorker init failed", exc_info=True)
                self._self_image_worker = None

        # Phase 4b: memory consolidator (cluster + merge near-cosine groups).
        self._consolidator = None
        if (
            settings.agent.consolidator_enabled
            and self._memory_store is not None
        ):
            try:
                from app.core.memory.memory_consolidator import MemoryConsolidator

                self._consolidator = MemoryConsolidator(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    chat_db=self._chat_db,
                    model=self._effective_worker_model,
                    chunk_size=settings.agent.consolidator_chunk_size,
                    similarity_threshold=settings.agent.consolidator_similarity_threshold,
                    min_cluster_size=settings.agent.consolidator_min_cluster_size,
                    min_hours_between=settings.agent.consolidator_min_hours_between,
                    use_llm_merge=settings.agent.consolidator_use_llm_merge,
                    user_display_name_provider=lambda: self.user_display_name,
                )
            except Exception:
                log.warning("MemoryConsolidator init failed", exc_info=True)
                self._consolidator = None

        # Phase 4b: weekly relationship pulse (LLM summary as self_tagged memory).
        self._relationship_pulse = None
        if (
            settings.agent.relationship_pulse_enabled
            and self._memory_store is not None
            and self._embedder is not None
        ):
            try:
                from app.core.relationship.relationship_pulse import RelationshipPulseWorker

                self._relationship_pulse = RelationshipPulseWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    relationship_store=getattr(self, "_relationship_store", None),
                    chat_db=self._chat_db,
                    embedder=self._embedder,
                    model=self._effective_worker_model,
                    min_hours=settings.agent.relationship_pulse_min_hours,
                    min_turns=settings.agent.relationship_pulse_min_turns,
                    max_tokens=settings.agent.relationship_pulse_max_tokens,
                    user_display_name_provider=lambda: self.user_display_name,
                )
            except Exception:
                log.warning("RelationshipPulseWorker init failed", exc_info=True)
                self._relationship_pulse = None

        # Schema v7: shared moments + relationship axes. Both are cheap;
        # the LLM detector is the only place we'd burn an extra call and
        # it's gated tightly (see ``_maybe_schedule_moment_llm_job``).
        self._shared_moments_store = None
        self._moment_detector = None
        if (
            settings.agent.shared_moments_enabled
            and self._memory_store is not None
            and self._embedder is not None
        ):
            try:
                from app.core.relationship.shared_moments import SharedMomentsStore

                self._shared_moments_store = SharedMomentsStore(
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                )
            except Exception:
                log.warning("SharedMomentsStore init failed", exc_info=True)
                self._shared_moments_store = None

        # F2 personality backlog: knowledge-gap journal. Cheap — pure
        # regex + a dedicated MemoryStore wrapper, no LLM. Wired
        # whenever long-term memory is available so the [[gap:...]]
        # extraction path always has somewhere to write.
        self._knowledge_gap_store = None
        if (
            self._memory_store is not None
            and self._embedder is not None
        ):
            try:
                from app.core.memory.knowledge_gap_extractor import KnowledgeGapStore

                self._knowledge_gap_store = KnowledgeGapStore(
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                )
            except Exception:
                log.warning("KnowledgeGapStore init failed", exc_info=True)
                self._knowledge_gap_store = None

        # K1 personality backlog: long-term goals journal. Cheap —
        # pure self-tag parsing + a dedicated MemoryStore wrapper,
        # the LLM-driven reflection runs out-of-band in
        # :class:`GoalWorker`. Wired whenever long-term memory is
        # available so the [[goal:...]] extraction path always has
        # somewhere to write, even when the worker is disabled.
        self._goal_store = None
        if (
            self._memory_store is not None
            and self._embedder is not None
        ):
            try:
                from app.core.goals.goal_store import GoalStore

                self._goal_store = GoalStore(
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    max_active=int(getattr(
                        settings.memory, "goal_max_active", 5,
                    )),
                    max_progress_per_goal=int(getattr(
                        settings.memory,
                        "goal_max_progress_per_goal",
                        12,
                    )),
                )
            except Exception:
                log.warning("GoalStore init failed", exc_info=True)
                self._goal_store = None
            # K1: tell the RAG retriever about the goal store so its
            # per-hit goal-alignment bonus has the active vectors to
            # check against. The retriever was constructed earlier in
            # the bootstrap; this hooks the dependency up after both
            # exist (the retriever's setter is None-safe).
            if (
                self._goal_store is not None
                and getattr(self, "_rag_retriever", None) is not None
                and hasattr(self._rag_retriever, "set_goal_store")
            ):
                try:
                    self._rag_retriever.set_goal_store(self._goal_store)
                except Exception:
                    log.debug(
                        "RagRetriever set_goal_store failed", exc_info=True,
                    )

        # F1 personality backlog: persistent claim queue + cancellation
        # event. The queue is enqueued from the ``_notify_memory_added``
        # path so every memory write site automatically feeds it. The
        # IdleFactChecker worker (registered below alongside decay /
        # promotion) drains it on the idle scheduler.
        self._fact_check_queue = None
        self._fact_check_cancel: threading.Event | None = None
        if (
            self._memory_store is not None
            and bool(getattr(settings.agent, "fact_checker_enabled", True))
        ):
            try:
                from app.core.memory.fact_check_queue import FactCheckQueue

                self._fact_check_queue = FactCheckQueue(self._chat_db)
            except Exception:
                log.warning("FactCheckQueue init failed", exc_info=True)
                self._fact_check_queue = None
            try:
                self._fact_check_cancel = threading.Event()
            except Exception:
                self._fact_check_cancel = None

        if (
            self._shared_moments_store is not None
            and settings.agent.shared_moments_llm_enabled
        ):
            try:
                from app.core.relationship.shared_moment_extractor import MomentDetector

                def _persist_moment_candidate(candidate: Any) -> None:
                    store = self._shared_moments_store
                    if store is None:
                        return
                    row = store.add_from_candidate(
                        candidate,
                        source_session=self.session_key,
                    )
                    if row is not None:
                        self._notify_shared_moment_added(row)

                self._moment_detector = MomentDetector(
                    ollama=self._ollama,
                    model=self._effective_worker_model,
                    persist_callback=_persist_moment_candidate,
                    min_turn_gap=settings.agent.shared_moments_min_turn_gap,
                    cooldown_seconds=settings.agent.shared_moments_cooldown_seconds,
                    user_display_name_provider=lambda: self.user_display_name,
                )
            except Exception:
                log.warning("MomentDetector init failed", exc_info=True)
                self._moment_detector = None

        self._relationship_axes_store = None
        self._relationship_axes_updater = None
        if settings.agent.relationship_axes_enabled:
            try:
                from app.core.relationship.relationship_axes import (
                    RelationshipAxesStore,
                    RelationshipAxesUpdater,
                )

                self._relationship_axes_store = RelationshipAxesStore(self._chat_db)
                self._relationship_axes_updater = RelationshipAxesUpdater(
                    self._relationship_axes_store,
                )
            except Exception:
                log.warning("RelationshipAxes init failed", exc_info=True)
                self._relationship_axes_store = None
                self._relationship_axes_updater = None

        # K31 soft physicality: TouchService state machine. Constructed
        # AFTER ``_relationship_axes_store`` so the dispatch path can
        # read live axes for the per-kind gate. Always built (even when
        # ``touch_enabled=False``) so the persisted cooldown state
        # survives a settings flap without resetting.
        self._touch_service = None
        try:
            from app.core.touch.touch_gestures import TouchService

            self._touch_service = TouchService(
                chat_db=self._chat_db,
                settings=settings.agent,
            )
        except Exception:
            log.warning("TouchService init failed", exc_info=True)
            self._touch_service = None

        # Listeners for the REST/WS layer. Shared moments fire on create
        # and on every edit/delete; axes fire only when an axis crosses a
        # 0.05 step (debounced server-side — see ``set_user_present`` /
        # the axes update path).
        self._shared_moment_listeners: list[
            Callable[[dict[str, Any]], None]
        ] = []
        self._relationship_axes_listeners: list[
            Callable[[dict[str, Any]], None]
        ] = []
        # F2 personality backlog: knowledge-gap listeners fire on create,
        # on resolve, and on delete. Patches carry ``gap`` (full row dict)
        # or ``deleted_gap_id``. WS hub broadcasts as
        # ``knowledge_gap_updated``.
        self._knowledge_gap_listeners: list[
            Callable[[dict[str, Any]], None]
        ] = []
        self._axes_last_broadcast: dict[str, float] = {
            "closeness": 0.0,
            "humor": 0.0,
            "trust": 0.0,
            "comfort": 0.0,
        }

        # Per-turn cache: was a moment created on the most recent turn?
        # Used to feed the axes updater the moment-vibes list without
        # re-querying the store, and to decide whether to render the
        # anniversary block on the *next* turn (a moment created right
        # now isn't an anniversary today).
        self._last_turn_moment_vibes: list[str] = []
        self._last_turn_milestone: str | None = None
        self._last_turn_promise_kept: bool = False
        self._last_turn_gift_received: bool = False
        # Wire all hot-path providers (each cheap: SQL/mirror reads or
        # pure functions). Token accounting runs through PromptTelemetry.
        self._prompt_assembler.set_inner_life_providers(
            affect=self._render_affect_block,
            circadian=self._render_circadian_block,
            day_color=self._render_day_color_block,
            vulnerability_budget=self._render_vulnerability_budget_block,
            profile=self._render_user_profile_block,
            user_state=self._render_user_state_block,
            relationship=self._render_relationship_block,
            agenda=self._render_agenda_block,
            goals=self._render_goals_block,
            arc=self._render_arc_block,
            narrative=self._render_narrative_block,
            vocal_tone=self._render_vocal_tone_block,
            catchphrase=self._render_catchphrase_block,
            petname=self._render_petname_block,
            ambient_noise=self._render_ambient_noise_block,
            avatar_capabilities=self._avatar_capabilities,
            pajama=self._render_pajama_block,
            motion_names=self._avatar_motion_names,
            world=self._render_world_block,
            activity=self._render_activity_block,
            anniversary=self._render_anniversary_block,
            axes=self._render_axes_block,
            knowledge_gaps=self._render_knowledge_gaps_block,
            belief_gaps=self._render_belief_gaps_block,
            clarification=self._render_clarification_block,
            calibration=self._render_calibration_block,
            sensory_anchor=self._render_sensory_anchor_block,
            rupture=self._render_rupture_block,
            misattunement=self._render_misattunement_block,
            opinion_injection=self._render_opinion_injection_block,
            absence_curiosity=self._render_absence_curiosity_block,
            turning_over=self._render_turning_over_block,
            mood_shell=self._render_mood_shell_block,
            novelty=self._render_novelty_block,
            stagnation=self._render_stagnation_block,
            style_pattern=self._render_style_pattern_block,
            style_signal=self._render_style_signal_block,
            self_noticing=self._render_self_noticing_block,
            curiosity_seeds=self._render_curiosity_seeds_block,
            grounding_line=self._render_grounding_line,
            user_reactions=self._render_user_reactions_block,
            touch_state=self._render_touch_state_block,
        )
        self._prompt_assembler.set_pinned_self_memories_provider(
            self._top_pinned_self_memories,
        )
        # K16: register the grounding-line mode so the assembler knows
        # which granular blocks to suppress on each turn. Idempotent;
        # safe to re-call on settings reload.
        try:
            self._prompt_assembler.set_grounding_line_mode(
                getattr(self._settings.agent, "grounding_line_mode", "off"),
            )
        except Exception:
            log.debug("grounding_line_mode setter failed", exc_info=True)

        # Phase 5b: feed the prosody dispatcher live affect/circadian.
        prosody = getattr(self, "_prosody", None)
        if prosody is not None:
            try:
                prosody.set_context_provider(self._cadence_context)
            except Exception:
                log.debug("prosody context provider wire failed", exc_info=True)

        if (
            self._memory_settings.enabled
            and self._memory_settings.extractor_enabled
            and self._embedder is not None
            and self._memory_store is not None
        ):
            try:
                self._memory_extractor = MemoryExtractor(
                    self._chat_db,
                    self._memory_store,
                    self._embedder,
                    self._ollama,
                    model=self._effective_worker_model,
                    user_display_name_provider=lambda: self.user_display_name,
                )
                self._memory_extractor.add_listener(self._notify_memory_added)
            except Exception:
                log.warning("memory extractor failed to initialise", exc_info=True)
                self._memory_extractor = None

        # ── Speaking-window scheduler (Phase 2a) ─────────────────────
        # Drains LLM-driven background jobs while Aiko is mid-TTS so the
        # hot path stays cheap. Workers register themselves with this and
        # submit jobs from `_post_turn` rather than running their own daemon
        # threads. The scheduler is created up-front so workers can take a
        # reference at construction time.
        self._scheduler = SpeakingWindowScheduler(
            speaking_window_grace_ms=settings.agent.scheduler_speaking_window_grace_ms,
            max_job_seconds=settings.agent.scheduler_max_job_seconds,
            idle_seconds=settings.agent.scheduler_idle_seconds,
            is_quiet=lambda: not self._turn_in_progress,
        )
        self._scheduler.start_idle_loop()

        self._summary_worker = SummaryWorker(
            self._chat_db,
            self._ollama,
            model=self._effective_worker_model,
            is_busy=lambda: self._turn_in_progress,
            idle_seconds=settings.agent.summary_idle_seconds,
            min_unsummarized_messages=settings.agent.summary_min_unsummarized_messages,
            target_tokens=settings.agent.summary_target_tokens,
            memory_extractor=self._memory_extractor,
        )
        self._summary_worker.start()
        # Schema v8 — background workers run through a single shared
        # :class:`IdleWorkerScheduler` instead of a dedicated decay
        # thread. The scheduler skips during Live mode + within the
        # configured quiet threshold of any user activity (see
        # :meth:`_is_user_idle`). New workers (memory promotion,
        # wall-clock decay, future F1/G2/G3) register here.
        self._last_user_activity_at: float = time.monotonic()
        self._idle_scheduler: "IdleWorkerScheduler | None" = None
        if self._memory_store is not None and self._memory_settings.tiers_enabled:
            try:
                from app.core.proactive.idle_worker_scheduler import IdleWorkerScheduler
                from app.core.memory.memory_decay_worker import MemoryDecayWorker
                from app.core.memory.memory_promotion_worker import (
                    MemoryPromotionWorker,
                )

                self._idle_scheduler = IdleWorkerScheduler(
                    wake_seconds=self._memory_settings.idle_worker_wake_seconds,
                    is_quiet_callback=self._is_user_idle,
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    tick_budget_ms=self._memory_settings.idle_worker_tick_budget_ms,
                    max_per_tick=self._memory_settings.idle_worker_max_per_tick,
                )
                self._idle_scheduler.register(
                    MemoryPromotionWorker(self._memory_store, self._memory_settings)
                )
                self._idle_scheduler.register(
                    MemoryDecayWorker(
                        self._memory_store,
                        self._memory_settings,
                        knowledge_gap_store=getattr(
                            self, "_knowledge_gap_store", None
                        ),
                    )
                )
                # K27 — daily personality colour roll. Cheap (hourly
                # kv_get + date compare; writes only on local-date
                # rollover). Registered immediately after the memory
                # workers so it shares their quiet-window gate. The
                # provider has a lazy fallback for the first-turn-
                # after-midnight case when this worker hasn't fired
                # yet -- see _render_day_color_block.
                if bool(
                    getattr(settings.agent, "day_color_enabled", True)
                ):
                    try:
                        from app.core.affect.day_color_worker import (
                            DayColorWorker,
                        )

                        self._idle_scheduler.register(
                            DayColorWorker(
                                chat_db=self._chat_db,
                                settings=settings.agent,
                            )
                        )
                    except Exception:
                        log.warning(
                            "day_color worker registration failed",
                            exc_info=True,
                        )
                # F1 — background fact-checker. Registered last because
                # it depends on the knowledge-gap store (created above)
                # and the (lazy) web-search helper. Failures here only
                # drop fact-checking; the rest of the scheduler stays.
                self._idle_fact_checker = None
                self._fact_check_rate_limiter = None
                if (
                    self._fact_check_queue is not None
                    and self._fact_check_cancel is not None
                    and bool(getattr(settings.agent, "fact_checker_enabled", True))
                ):
                    try:
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.memory.idle_fact_checker import IdleFactChecker
                        from app.llm.tools.builtins import WebSearchTool

                        try:
                            web_search_tool = WebSearchTool()
                        except Exception:
                            log.info(
                                "fact-checker disabled: web_search tool "
                                "unavailable (duckduckgo-search missing?)"
                            )
                            web_search_tool = None
                        if web_search_tool is not None:
                            self._fact_check_rate_limiter = FactCheckRateLimiter(
                                self._chat_db,
                                per_hour_cap=int(
                                    getattr(
                                        settings.agent,
                                        "fact_checker_per_hour_cap",
                                        10,
                                    )
                                ),
                                per_day_cap=int(
                                    getattr(
                                        settings.agent,
                                        "fact_checker_per_day_cap",
                                        50,
                                    )
                                ),
                            )
                            self._idle_fact_checker = IdleFactChecker(
                                queue=self._fact_check_queue,
                                memory_store=self._memory_store,
                                agent_settings=settings.agent,
                                memory_settings=self._memory_settings,
                                ollama=self._ollama,
                                chat_model=self._effective_worker_model,
                                web_search_tool=web_search_tool,
                                rate_limiter=self._fact_check_rate_limiter,
                                cancel_event=self._fact_check_cancel,
                                knowledge_gap_store=getattr(
                                    self, "_knowledge_gap_store", None
                                ),
                                embedder=self._embedder,
                                notify_memory_updated=self._notify_memory_updated,
                                # Privacy gate inputs — late-bound so a
                                # mid-session rename of the user (or
                                # the assistant) is picked up on the
                                # next tick.
                                user_names_provider=self._fact_check_user_names,
                                assistant_name_provider=self._fact_check_assistant_name,
                            )
                            self._idle_scheduler.register(self._idle_fact_checker)
                    except Exception:
                        log.warning(
                            "IdleFactChecker boot failed", exc_info=True
                        )
                        self._idle_fact_checker = None

                # G3 — idle curiosity worker. Picks Aiko's existing
                # ``open_question`` memories one at a time, web-searches
                # them, and writes the answer back as a
                # ``curiosity_finding`` memory. Reuses the F1 fact-
                # checker's ``WebSearchTool`` instance and cancel event
                # so a starting turn aborts both workers cleanly. The
                # rate limiter is a *separate* ``FactCheckRateLimiter``
                # instance keyed on ``"idle_curiosity.rate_state"`` so
                # the two web-search budgets don't share counters.
                self._idle_curiosity = None
                self._idle_curiosity_rate_limiter = None
                if (
                    self._fact_check_cancel is not None
                    and self._embedder is not None
                    and bool(
                        getattr(
                            settings.agent, "idle_curiosity_enabled", True,
                        )
                    )
                ):
                    try:
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.proactive.idle_curiosity_worker import (
                            IdleCuriosityWorker,
                        )
                        from app.llm.tools.builtins import WebSearchTool

                        # ``WebSearchTool`` is a thin DDGS wrapper with
                        # no state to share between workers, so a fresh
                        # instance is fine. Build one here so the
                        # curiosity worker survives the F1 path being
                        # disabled / failing.
                        try:
                            curiosity_search_tool = WebSearchTool()
                        except Exception:
                            log.info(
                                "idle_curiosity disabled: web_search "
                                "tool unavailable",
                            )
                            curiosity_search_tool = None
                        if curiosity_search_tool is not None:
                            self._idle_curiosity_rate_limiter = (
                                FactCheckRateLimiter(
                                    self._chat_db,
                                    per_hour_cap=int(
                                        getattr(
                                            settings.agent,
                                            "idle_curiosity_per_hour_cap",
                                            2,
                                        )
                                    ),
                                    per_day_cap=int(
                                        getattr(
                                            settings.agent,
                                            "idle_curiosity_per_day_cap",
                                            6,
                                        )
                                    ),
                                    state_key="idle_curiosity.rate_state",
                                )
                            )
                            self._idle_curiosity = IdleCuriosityWorker(
                                memory_store=self._memory_store,
                                embedder=self._embedder,
                                ollama=self._ollama,
                                chat_model=self._effective_worker_model,
                                web_search_tool=curiosity_search_tool,
                                rate_limiter=(
                                    self._idle_curiosity_rate_limiter
                                ),
                                cancel_event=self._fact_check_cancel,
                                agent_settings=settings.agent,
                                memory_settings=self._memory_settings,
                                user_names_provider=(
                                    self._fact_check_user_names
                                ),
                                assistant_name_provider=(
                                    self._fact_check_assistant_name
                                ),
                                notify_memory_added=(
                                    self._notify_memory_added
                                ),
                                notify_memory_updated=(
                                    self._notify_memory_updated
                                ),
                            )
                            self._idle_scheduler.register(
                                self._idle_curiosity,
                            )
                    except Exception:
                        log.warning(
                            "IdleCuriosityWorker boot failed",
                            exc_info=True,
                        )
                        self._idle_curiosity = None

                # F2.1: IdleGapResolver. Closes ``knowledge_gap`` rows
                # whose answer is already living in the memory store as
                # a ``preference`` / ``fact`` / etc. Without this, the
                # gap-injection block re-asks the same question every
                # time the topic recurs because nothing else marks
                # such gaps resolved (F1 only resolves via fresh web
                # search). Failure is non-fatal — the journal stays
                # readable, gaps just won't auto-close.
                self._idle_gap_resolver = None
                if (
                    self._memory_store is not None
                    and self._knowledge_gap_store is not None
                    and bool(
                        getattr(
                            settings.agent, "gap_resolver_enabled", True,
                        )
                    )
                ):
                    try:
                        from app.core.conversation.idle_gap_resolver import (
                            IdleGapResolver,
                        )

                        self._idle_gap_resolver = IdleGapResolver(
                            memory_store=self._memory_store,
                            gap_store=self._knowledge_gap_store,
                            agent_settings=settings.agent,
                            memory_settings=self._memory_settings,
                            cancel_event=self._fact_check_cancel,
                            notify_memory_updated=(
                                self._notify_memory_updated
                            ),
                        )
                        self._idle_scheduler.register(
                            self._idle_gap_resolver,
                        )
                    except Exception:
                        log.warning(
                            "IdleGapResolver boot failed",
                            exc_info=True,
                        )
                        self._idle_gap_resolver = None

                # K9: TopicGraph + CuriositySeedWorker. The graph is a
                # zero-cost wrapper around the in-process memory mirror;
                # the worker registers as an idle tick that proposes
                # "topics we haven't touched yet" using the graph as
                # the "we already discussed that" filter. Both are
                # opt-out via ``agent.topic_graph_enabled`` /
                # ``agent.curiosity_seed_enabled``. Failures here are
                # non-fatal: the rest of the app keeps working without
                # the seed surface.
                self._topic_graph = None
                self._curiosity_seed_worker = None
                if (
                    self._memory_store is not None
                    and self._embedder is not None
                    and bool(
                        getattr(
                            settings.agent, "topic_graph_enabled", True,
                        )
                    )
                ):
                    try:
                        from app.core.conversation.topic_graph import TopicGraph

                        self._topic_graph = TopicGraph(
                            self._memory_store,
                            similarity=0.55,
                            min_cluster_size=3,
                            filter_threshold=float(
                                getattr(
                                    settings.agent,
                                    "topic_graph_filter_threshold",
                                    0.65,
                                )
                            ),
                        )
                    except Exception:
                        log.warning(
                            "TopicGraph init failed", exc_info=True,
                        )
                        self._topic_graph = None

                if (
                    self._topic_graph is not None
                    and self._fact_check_cancel is not None
                    and bool(
                        getattr(
                            settings.agent, "curiosity_seed_enabled", True,
                        )
                    )
                ):
                    try:
                        from app.core.proactive.curiosity_seed_worker import (
                            CuriositySeedWorker,
                        )

                        persona_path_seed = (
                            Path(__file__).resolve().parents[3]
                            / "data" / "persona" / "aiko_companion.txt"
                        )

                        def _persona_provider() -> str:
                            try:
                                return persona_path_seed.read_text(
                                    encoding="utf-8",
                                )
                            except OSError:
                                return ""

                        def _summary_provider() -> str:
                            try:
                                row = self._chat_db.get_latest_summary(
                                    self.session_key,
                                )
                                return (row.summary if row is not None else "") or ""
                            except Exception:
                                return ""

                        def _assistant_name_provider() -> str:
                            return (
                                self._fact_check_assistant_name() or "Aiko"
                            )

                        self._curiosity_seed_worker = CuriositySeedWorker(
                            memory_store=self._memory_store,
                            topic_graph=self._topic_graph,
                            embedder=self._embedder,
                            ollama=self._ollama,
                            chat_model=self._effective_worker_model,
                            cancel_event=self._fact_check_cancel,
                            agent_settings=settings.agent,
                            memory_settings=self._memory_settings,
                            persona_provider=_persona_provider,
                            rolling_summary_provider=_summary_provider,
                            user_display_name_provider=(
                                lambda: self.user_display_name
                            ),
                            assistant_display_name_provider=(
                                _assistant_name_provider
                            ),
                            notify_memory_added=self._notify_memory_added,
                        )
                        self._idle_scheduler.register(
                            self._curiosity_seed_worker,
                        )
                    except Exception:
                        log.warning(
                            "CuriositySeedWorker boot failed",
                            exc_info=True,
                        )
                        self._curiosity_seed_worker = None

                # K1: GoalWorker. Cold-start bootstrap when the ring
                # is empty, reflection ticks otherwise. Each LLM call
                # passes through a dedicated FactCheckRateLimiter so
                # the worker's daily budget stays independent of F1's.
                # Failures here only drop autonomous reflection; the
                # self-tag write path and agent tools still work
                # against ``self._goal_store``.
                self._goal_worker = None
                self._goal_worker_rate_limiter = None
                if (
                    self._goal_store is not None
                    and self._fact_check_cancel is not None
                    and bool(getattr(settings.agent, "goals_enabled", True))
                ):
                    try:
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.goals.goal_worker import GoalWorker

                        self._goal_worker_rate_limiter = FactCheckRateLimiter(
                            self._chat_db,
                            per_hour_cap=int(getattr(
                                settings.agent,
                                "goal_worker_per_hour_cap",
                                3,
                            )),
                            per_day_cap=int(getattr(
                                settings.agent,
                                "goal_worker_per_day_cap",
                                12,
                            )),
                            state_key="goal_worker.rate_state",
                        )

                        persona_path_goal = (
                            Path(__file__).resolve().parents[3]
                            / "data" / "persona" / "aiko_companion.txt"
                        )

                        def _persona_provider_goal() -> str:
                            try:
                                return persona_path_goal.read_text(
                                    encoding="utf-8",
                                )
                            except OSError:
                                return ""

                        def _summary_provider_goal() -> str:
                            try:
                                row = self._chat_db.get_latest_summary(
                                    self.session_key,
                                )
                                return (row.summary if row is not None else "") or ""
                            except Exception:
                                return ""

                        def _assistant_name_provider_goal() -> str:
                            return (
                                self._fact_check_assistant_name() or "Aiko"
                            )

                        self._goal_worker = GoalWorker(
                            goal_store=self._goal_store,
                            ollama=self._ollama,
                            chat_model=self._effective_worker_model,
                            cancel_event=self._fact_check_cancel,
                            agent_settings=settings.agent,
                            memory_settings=self._memory_settings,
                            rate_limiter=self._goal_worker_rate_limiter,
                            persona_provider=_persona_provider_goal,
                            rolling_summary_provider=_summary_provider_goal,
                            user_display_name_provider=(
                                lambda: self.user_display_name
                            ),
                            assistant_display_name_provider=(
                                _assistant_name_provider_goal
                            ),
                            notify_memory_added=self._notify_memory_added,
                            notify_memory_updated=self._notify_memory_updated,
                        )
                        self._idle_scheduler.register(self._goal_worker)
                    except Exception:
                        log.warning(
                            "GoalWorker boot failed", exc_info=True,
                        )
                        self._goal_worker = None
                        self._goal_worker_rate_limiter = None

                # Aiko's living garden — plant stage promotion + visiting
                # the garden during idle daylight windows. Both workers
                # piggyback on the shared scheduler so they share the
                # quiet-window gate; they're a no-op when the WorldStore
                # never loaded. Failures here only drop garden cycling;
                # the manual tools still work.
                if getattr(self, "_world_store", None) is not None:
                    try:
                        from app.core.world.garden_visit_worker import (
                            GardenVisitWorker,
                        )
                        from app.core.world.plant_growth_worker import (
                            PlantGrowthWorker,
                        )

                        self._idle_scheduler.register(
                            PlantGrowthWorker(
                                self._world_store,
                                notify=self._notify_world,
                            )
                        )
                        self._idle_scheduler.register(
                            GardenVisitWorker(
                                self._world_store,
                                notify=self._notify_world,
                                kv_get=self._chat_db.kv_get,
                                kv_set=self._chat_db.kv_set,
                            )
                        )
                    except Exception:
                        log.warning(
                            "garden idle workers failed to register",
                            exc_info=True,
                        )
                self._idle_scheduler.start()
            except Exception:
                log.warning("idle worker scheduler boot failed", exc_info=True)
                self._idle_scheduler = None
        self._turn_runner = TurnRunner(
            self._chat_client,
            self._chat_db,
            self._prompt_assembler,
            model=self._effective_chat_model,
            context_window=self._context_window,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            summary_worker=self._summary_worker,
            memory_store=self._memory_store,
            embedder=self._embedder,
            self_tagged_salience=self._memory_settings.self_tagged_salience,
            max_prompt_tokens_pct=settings.agent.max_prompt_tokens_pct,
            on_memory_added=self._notify_memory_added,
            on_tool_call=lambda name, args: self._notify_tool_event(
                "call", {"name": name, "arguments": args},
            ),
            on_tool_result=lambda name, content, ok: self._notify_tool_event(
                "result", {"name": name, "ok": bool(ok), "preview": (content or "")[:200]},
            ),
            filler_threshold_ms=settings.agent.filler_first_token_ms,
            filler_enabled=settings.agent.filler_enabled,
            listen_extensions_provider=lambda: int(
                getattr(self, "_last_listen_extensions", 0) or 0
            ),
        )
        self._tool_event_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._tool_registry = None
        try:
            self.rebuild_tool_registry()
        except Exception:
            log.warning("initial tool registry build failed", exc_info=True)
        # Phase 4c: conversation arc tracker (regex hot-path + LLM smoother).
        self._arc_store = None
        self._arc_estimator = None
        self._arc_smoother = None
        try:
            from app.core.conversation.conversation_arc import (
                ArcEstimator,
                ArcSmootherWorker,
                ArcStore,
            )

            self._arc_store = ArcStore(self._chat_db)
            self._arc_estimator = ArcEstimator(self._arc_store)
            self._arc_smoother = ArcSmootherWorker(
                ollama=self._ollama,
                store=self._arc_store,
                model=self._effective_worker_model,
                every_n_turns=max(
                    1, int(settings.agent.arc_update_every_n_turns) * 6
                ),
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("ArcStore/ArcEstimator init failed", exc_info=True)
            self._arc_store = None
            self._arc_estimator = None
            self._arc_smoother = None

        # Phase 4c: prepared nudge store + narrative weaver.
        self._prepared_nudge_store = None
        self._narrative_weaver = None
        try:
            from app.core.proactive.prepared_nudge import (
                NarrativeWeaver,
                PreparedNudgeStore,
            )

            self._prepared_nudge_store = PreparedNudgeStore(self._chat_db)
            self._narrative_weaver = NarrativeWeaver(
                ollama=self._ollama,
                store=self._prepared_nudge_store,
                memory_store=self._memory_store,
                agenda_store=getattr(self, "_agenda_store", None),
                model=self._effective_worker_model,
                every_n_turns=4,
                ttl_seconds=settings.agent.prepared_nudge_ttl_seconds,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("PreparedNudgeStore/NarrativeWeaver init failed", exc_info=True)
            self._prepared_nudge_store = None
            self._narrative_weaver = None

        # Schema v10 — follow-up worker rides the same idle scheduler
        # as the decay/promotion workers and uses the prepared-nudge
        # store the NarrativeWeaver normally fills. Wired here so both
        # dependencies are guaranteed available; failures only drop
        # the proactive callback path (the persona rule + retrieval
        # annotations still work).
        if (
            self._idle_scheduler is not None
            and self._memory_store is not None
            and self._prepared_nudge_store is not None
        ):
            try:
                from app.core.proactive.follow_up_worker import FollowUpWorker

                self._idle_scheduler.register(
                    FollowUpWorker(
                        memory_store=self._memory_store,
                        prepared_nudge_store=self._prepared_nudge_store,
                        user_id_provider=lambda: self._user_id,
                        user_display_name_provider=(
                            lambda: self.user_display_name
                        ),
                    )
                )
            except Exception:
                log.warning("FollowUpWorker init failed", exc_info=True)

        # G2 — schedule learner. Independent of the FollowUpWorker
        # gate above (no prepared-nudge dependency), so wired after
        # the same idle scheduler. Reads only ``messages.created_at``
        # — never message content — and writes a single
        # ``usual_hours`` profile field. Failures only drop the
        # schedule field; the rest of the scheduler stays.
        if (
            self._idle_scheduler is not None
            and self._user_profile_store is not None
            and bool(
                getattr(settings.agent, "schedule_learner_enabled", True)
            )
        ):
            try:
                from app.core.infra.schedule_learner import ScheduleLearner

                self._idle_scheduler.register(
                    ScheduleLearner(
                        chat_db=self._chat_db,
                        profile_store=self._user_profile_store,
                        user_id_provider=lambda: self._user_id,
                        agent_settings=settings.agent,
                        memory_settings=self._memory_settings,
                    )
                )
            except Exception:
                log.warning("ScheduleLearner init failed", exc_info=True)

        # F5 — conflicting-memory detector. Always builds the store
        # (REST endpoints and the ``[[conflict:reason]]`` tag dispatch
        # need it even when the worker is disabled), then conditionally
        # builds + registers the worker. The cascade-cleanup hook on
        # ``MemoryStore.delete`` keeps ``memory_conflicts`` rows from
        # dangling when a user deletes a memory through the Memory
        # drawer.
        self._memory_conflict_store = None
        self._memory_conflict_worker = None
        self._memory_conflict_rate_limiter = None
        if self._memory_store is not None and self._chat_db is not None:
            try:
                from app.core.memory.memory_conflict_store import (
                    MemoryConflictStore,
                )

                self._memory_conflict_store = MemoryConflictStore(
                    self._chat_db,
                )
                self._memory_store.add_delete_listener(
                    self._memory_conflict_store.delete_for_memory,
                )
            except Exception:
                log.warning(
                    "MemoryConflictStore init failed", exc_info=True,
                )
                self._memory_conflict_store = None
        if (
            self._idle_scheduler is not None
            and self._memory_conflict_store is not None
            and self._fact_check_cancel is not None
            and bool(
                getattr(settings.agent, "conflict_detector_enabled", True)
            )
        ):
            try:
                from app.core.memory.fact_check_rate_limiter import (
                    FactCheckRateLimiter,
                )
                from app.core.memory.memory_conflict_worker import (
                    MemoryConflictWorker,
                )

                self._memory_conflict_rate_limiter = FactCheckRateLimiter(
                    self._chat_db,
                    per_hour_cap=int(
                        getattr(
                            settings.agent,
                            "conflict_detector_per_hour_cap",
                            6,
                        )
                    ),
                    per_day_cap=int(
                        getattr(
                            settings.agent,
                            "conflict_detector_per_day_cap",
                            30,
                        )
                    ),
                    state_key="conflict_detector.rate_state",
                )
                self._memory_conflict_worker = MemoryConflictWorker(
                    memory_store=self._memory_store,
                    conflict_store=self._memory_conflict_store,
                    ollama=self._ollama,
                    chat_model=self._effective_worker_model,
                    rate_limiter=self._memory_conflict_rate_limiter,
                    cancel_event=self._fact_check_cancel,
                    agent_settings=settings.agent,
                    memory_settings=self._memory_settings,
                    notify_memory_updated=self._notify_memory_updated,
                )
                self._idle_scheduler.register(self._memory_conflict_worker)
            except Exception:
                log.warning(
                    "MemoryConflictWorker init failed", exc_info=True,
                )
                self._memory_conflict_worker = None
                self._memory_conflict_rate_limiter = None

        # K29 — opinion-injection rate limiter (LLM YES/NO gate on
        # borderline-heuristic stance contradictions). Independent
        # ``state_key`` so the budget can't be exhausted by the F5
        # conflict detector or the K2 belief worker. Lives off the
        # same ``FactCheckRateLimiter`` plumbing all three share.
        # Off-by-default if the chat_db isn't available (in-memory
        # transient configurations); the detector silently falls
        # back to Path C (definite-only) in that case via the
        # caller's ``llm_gate=None`` branch.
        if self._chat_db is not None:
            try:
                from app.core.memory.fact_check_rate_limiter import (
                    FactCheckRateLimiter,
                )

                self._opinion_injection_rate_limiter = FactCheckRateLimiter(
                    self._chat_db,
                    per_hour_cap=int(
                        getattr(
                            self._memory_settings,
                            "opinion_injection_per_hour_cap",
                            6,
                        )
                    ),
                    per_day_cap=int(
                        getattr(
                            self._memory_settings,
                            "opinion_injection_per_day_cap",
                            30,
                        )
                    ),
                    state_key="opinion_injection.rate_state",
                )
            except Exception:
                log.warning(
                    "OpinionInjection rate limiter init failed",
                    exc_info=True,
                )
                self._opinion_injection_rate_limiter = None

        # K2 — theory-of-mind / belief tracking. Always builds the store
        # (the [[predict:...]] tag dispatch + REST endpoints need it
        # even when the worker is disabled), then conditionally builds
        # the gap detector and the inference worker. Inner-life
        # provider is registered against the prompt assembler below
        # once the detector exists.
        self._belief_store = None
        self._belief_worker = None
        self._belief_rate_limiter = None
        self._belief_gap_detector = None
        # Cached gap list produced by the post-turn detector for the
        # NEXT turn's inner-life provider. Cleared after each render.
        self._pending_belief_gaps: list[Any] = []
        # K17 — one-shot clarification-repair slot. Filled by
        # ``post_turn_mixin._post_turn_inner_life`` when the detector
        # fires; consumed and cleared by
        # ``inner_life_providers_mixin._render_clarification_block``
        # on the next turn so the cue appears exactly once.
        self._pending_clarification: Any = None
        # K8 — one-shot affect-rupture slot. Same shape as above:
        # post-turn detector fills, next-turn provider clears.
        self._pending_rupture: Any = None
        # K23 — misattunement detector state. Unlike K8/K17 the
        # detector runs provider-time (same-turn reaction), so we
        # only need a cooldown counter -- no pending-result slot.
        # Decremented by ``_render_misattunement_block`` each call;
        # armed to ``misattunement_cooldown_turns`` when ``detect()``
        # returns a hit. ``_last_misattunement_*`` fields are
        # diagnostic-only (read by the MCP debug tool); no behaviour
        # depends on them.
        self._misattunement_cooldown: int = 0
        self._misattunement_force_next: bool = False
        self._last_misattunement_trigger: str | None = None
        self._last_misattunement_fire_turn: int | None = None
        # K29 — opinion-injection detector state. Same provider-time
        # shape as K23 (same-turn reaction), with two extra guards
        # against contrarianism: a per-session cap and an LLM
        # rate-limiter for the borderline-heuristic path. The
        # rate-limiter is constructed lazily below once the chat_db
        # is known; the per-session count resets on session boundary
        # via ``switch_session`` / ``clear_conversation_memory``.
        # ``_last_opinion_injection`` carries the most recent
        # :class:`OpinionInjectionResult` for the MCP debug tool;
        # behaviour does not depend on it.
        self._opinion_injection_cooldown: int = 0
        self._opinion_injection_session_count: int = 0
        self._opinion_injection_force_next: bool = False
        self._last_opinion_injection: Any = None
        self._opinion_injection_rate_limiter = None
        # K28 — "What I've been turning over" between-session cue.
        # ``_pending_turning_over_seconds`` is armed by the post-turn
        # engagement tracker when a typed turn lands after a gap of
        # at least ``memory.turning_over_min_gap_minutes``. The next
        # prompt assembly's provider reads + clears the slot and
        # runs the picker. ``_turning_over_force_next`` is the MCP
        # debug bypass (set by ``force_turning_over``); cleared
        # whether the picker fires or not so the bypass is strictly
        # one-turn. ``_last_turning_over`` carries the most recent
        # :class:`TurningOverResult` for the MCP debug tool.
        self._pending_turning_over_seconds: float | None = None
        self._turning_over_force_next: bool = False
        self._last_turning_over: Any = None
        # K30 — self-noticing cues (agreement-streak / flat-affect /
        # repeated-thought). Three sub-detectors fan into one
        # ``self_noticing`` inner-life block. Agreement-streak is
        # stateless (the provider queries ``chat_db.get_messages`` per
        # turn, K23-style); the other two each own a small ring on
        # the controller because ``AffectState`` has no per-turn
        # ring buffer and there's no shared "recent assistant
        # vectors" accessor on ``RagStore``. ``_repeated_thought_*``
        # is the one-shot carry-forward flag set in ``post_turn``
        # and consumed by the next provider call. Force flags
        # mirror the K23 / K29 one-shot bypass shape so the MCP
        # debug tools can drop a cue into the next prompt without
        # waiting for the streak to genuinely fire.
        self_noticing_window = max(
            1, int(getattr(settings.agent, "self_noticing_window", 6))
        )
        self._self_noticing_affect_samples: deque[
            tuple[float, float, str | None]
        ] = deque(maxlen=max(12, self_noticing_window * 2))
        self._self_noticing_aiko_vecs: deque[Any] = deque(maxlen=3)
        self._self_noticing_force_agreement: bool = False
        self._self_noticing_force_flat_affect: bool = False
        self._self_noticing_force_repeated_thought: bool = False
        self._self_noticing_agreement_cooldown: int = 0
        self._self_noticing_flat_affect_cooldown: int = 0
        self._repeated_thought_fired_last_turn: bool = False
        self._repeated_thought_last_cosine: float = 0.0
        self._repeated_thought_last_matched_index: int = -1
        # Diagnostic-only — most-recent verdicts from the three
        # sub-detectors. Read by ``get_self_noticing_state`` over MCP;
        # no behaviour depends on them.
        self._last_self_noticing_agreement: Any = None
        self._last_self_noticing_flat_affect: Any = None
        # K27 — daily personality colour MCP debug flags. The canonical
        # roll is performed by :class:`DayColorWorker` (registered on
        # the idle scheduler above) and by the lazy fallback in
        # :meth:`_render_day_color_block` for the first-turn-after-
        # midnight case. These flags only exist to let MCP debug tools
        # override the next provider call without waiting for natural
        # cadence:
        #
        # * ``_day_color_force_next``: name of a palette colour to
        #   render on the next call regardless of kv_meta state. Does
        #   NOT touch ``kv_meta`` (so the persisted roll survives).
        #   Consumed one-shot.
        # * ``_day_color_force_reroll``: when True, the next provider
        #   call rolls a fresh colour and writes it to ``kv_meta``
        #   (useful for repro without shifting the OS clock).
        #   Consumed one-shot.
        self._day_color_force_next: str | None = None
        self._day_color_force_reroll: bool = False
        # K15 -- vulnerability budget MCP debug flags. The persisted
        # bucket lives in ``kv_meta`` (``aiko.vulnerability_budget``)
        # and is read+decayed lazily on every provider call; these
        # flags only exist so MCP debug tools can override the next
        # render or wipe the persisted state without crafting real
        # self-tags:
        #
        # * ``_vulnerability_budget_force_spent``: when set, the
        #   next provider call renders the cue as if ``state.spent``
        #   equalled this value. Does NOT touch ``kv_meta`` (so the
        #   real persisted bucket survives the test). Consumed
        #   one-shot.
        # * ``_vulnerability_budget_force_reset``: when True, the
        #   next provider call writes a fresh
        #   ``BudgetState(spent=0)`` to ``kv_meta``. Consumed
        #   one-shot.
        self._vulnerability_budget_force_spent: float | None = None
        self._vulnerability_budget_force_reset: bool = False
        if (
            self._chat_db is not None
            and bool(getattr(settings.agent, "belief_tracking_enabled", True))
        ):
            try:
                from app.core.relationship.belief_store import BeliefStore

                self._belief_store = BeliefStore(self._chat_db)
            except Exception:
                log.warning("BeliefStore init failed", exc_info=True)
                self._belief_store = None
        if self._belief_store is not None:
            try:
                from app.core.relationship.belief_gap_detector import BeliefGapDetector

                self._belief_gap_detector = BeliefGapDetector(
                    belief_store=self._belief_store,
                    belief_settings=self._memory_settings,
                )
            except Exception:
                log.warning("BeliefGapDetector init failed", exc_info=True)
                self._belief_gap_detector = None
        if (
            self._idle_scheduler is not None
            and self._belief_store is not None
            and self._fact_check_cancel is not None
            and self._embedder is not None
            and bool(getattr(settings.agent, "belief_worker_enabled", True))
        ):
            try:
                from app.core.relationship.belief_worker import BeliefInferenceWorker
                from app.core.memory.fact_check_rate_limiter import (
                    FactCheckRateLimiter,
                )

                self._belief_rate_limiter = FactCheckRateLimiter(
                    self._chat_db,
                    per_hour_cap=int(
                        getattr(
                            settings.agent,
                            "belief_worker_per_hour_cap",
                            4,
                        )
                    ),
                    per_day_cap=int(
                        getattr(
                            settings.agent,
                            "belief_worker_per_day_cap",
                            20,
                        )
                    ),
                    state_key="belief_worker.rate_state",
                )
                self._belief_worker = BeliefInferenceWorker(
                    belief_store=self._belief_store,
                    chat_db=self._chat_db,
                    embedder=self._embedder,
                    ollama=self._ollama,
                    chat_model=self._effective_worker_model,
                    rate_limiter=self._belief_rate_limiter,
                    cancel_event=self._fact_check_cancel,
                    agent_settings=settings.agent,
                    belief_settings=self._memory_settings,
                    session_id_provider=lambda: self._session_id,
                    user_id_provider=lambda: self._user_id,
                    user_names_provider=lambda: [self.user_display_name]
                    if self.user_display_name
                    else [],
                    assistant_name_provider=lambda: "Aiko",
                    notify_belief_added=self._notify_belief_added,
                    notify_belief_updated=self._notify_belief_updated,
                )
                self._idle_scheduler.register(self._belief_worker)
            except Exception:
                log.warning("BeliefInferenceWorker init failed", exc_info=True)
                self._belief_worker = None
                self._belief_rate_limiter = None

        # K6 — surprise / novelty detector. Pure in-process helper:
        # one embed + a tiny in-memory ring per turn, no DB writes,
        # no background worker. Registered as a per-turn inner-life
        # provider below (taking ``user_text``), same shape as the
        # F2 knowledge-gap block. Requires an Embedder; if RAG is
        # disabled the detector still works (it just can't warm
        # from past sessions and starts every install cold).
        self._novelty_detector = None
        if (
            self._embedder is not None
            and bool(getattr(settings.agent, "novelty_detection_enabled", True))
        ):
            try:
                from app.core.conversation.novelty_detector import NoveltyDetector

                self._novelty_detector = NoveltyDetector(
                    embedder=self._embedder,
                    rag_store=self._rag_store,
                    user_id=self._user_id,
                    memory_settings=self._memory_settings,
                )
            except Exception:
                log.warning("NoveltyDetector init failed", exc_info=True)
                self._novelty_detector = None

        # K18 (topic stagnation) — sibling of K6 that consumes the
        # per-turn distance the novelty detector exposes via
        # ``last_distance``/``last_band``. No embedder, no rag_store,
        # no rate-cap; the per-turn cost is a deque append + a mean.
        # Disabling K6 doesn't disable K18 explicitly here, but the
        # provider returns "" silently when ``last_distance`` is
        # always None (which it will be without the K6 detector
        # populating it), so the cue stays quiet.
        self._topic_stagnation_detector = None
        if bool(
            getattr(settings.agent, "topic_stagnation_enabled", True)
        ):
            try:
                from app.core.conversation.topic_stagnation import (
                    TopicStagnationDetector,
                )

                self._topic_stagnation_detector = TopicStagnationDetector(
                    memory_settings=self._memory_settings,
                )
            except Exception:
                log.warning(
                    "TopicStagnationDetector init failed", exc_info=True,
                )
                self._topic_stagnation_detector = None

        # Anti-rut layer: AikoStylePatternTracker watches Aiko's *own*
        # recent assistant turns for opener / question / length ruts
        # and surfaces a soft "Heads-up" inner-life cue when one of
        # the bands trips. Sibling architecture to K6/K18; cheap pure
        # rolling-window detector (no embedder, no LLM). Per-band
        # cooldowns plus the in-prompt cue let the rut self-correct
        # over a few turns instead of recurring forever.
        self._aiko_style_tracker = None
        if bool(
            getattr(settings.agent, "style_tracker_enabled", True)
        ):
            try:
                from app.core.persona.aiko_style_tracker import (
                    AikoStylePatternTracker,
                )

                self._aiko_style_tracker = AikoStylePatternTracker(
                    agent_settings=settings.agent,
                )
            except Exception:
                log.warning(
                    "AikoStylePatternTracker init failed", exc_info=True,
                )
                self._aiko_style_tracker = None

        # K13 stylometric mirror: tracks Jacob's writing style across
        # recent user turns. Persisted via a tiny JSON-blob table so
        # the rolling window survives restart; warmed lazily on first
        # invocation if the persisted blob is missing or empty (one
        # cheap scan over the latest user messages from chat_db).
        self._style_signal_analyzer = None
        self._style_signal_store = None
        self._style_signal_warmed = False
        if bool(
            getattr(settings.agent, "style_signal_enabled", True)
        ):
            try:
                from app.core.persona.style_signal import (
                    StyleSignalAnalyzer,
                    StyleSignalStore,
                )

                self._style_signal_analyzer = StyleSignalAnalyzer(
                    agent_settings=settings.agent,
                )
                self._style_signal_store = StyleSignalStore(self._chat_db)
                # Restore from persistence eagerly (cheap one-row read);
                # cross-session warm from chat history happens lazily on
                # the first post-turn record so a brand-new install
                # warms naturally instead of doing a full DB scan at
                # boot.
                try:
                    blob = self._style_signal_store.load(self._user_id)
                    if blob:
                        self._style_signal_analyzer.from_dict(blob)
                except Exception:
                    log.debug(
                        "style_signal initial load failed", exc_info=True,
                    )
            except Exception:
                log.warning(
                    "StyleSignalAnalyzer init failed", exc_info=True,
                )
                self._style_signal_analyzer = None
                self._style_signal_store = None

        # K20: metacognitive calibration store. Holds per-user
        # CalibrationState (global score + bounded topic slots) so
        # decay survives restart. Constructed unconditionally
        # (read-side bonus stays available even when the detector's
        # write side is disabled) -- production code reads the state
        # baseline from the configured ``calibration_baseline``.
        self._calibration_store = None
        # Cache slots for the K20 softening detector + topic centroid.
        # ``_last_assistant_vec`` is set by K22's wire-in when it
        # embeds the just-emitted reply; ``_prior_assistant_vec`` is
        # carried forward by K20's wire-in to the next turn so the
        # softening detector can compare the next user message
        # against the claim that triggered the pushback.
        self._last_assistant_vec = None
        self._prior_assistant_vec = None
        try:
            from app.core.affect.calibration_store import CalibrationStore

            self._calibration_store = CalibrationStore(
                self._chat_db,
                baseline=float(
                    getattr(
                        settings.memory, "calibration_baseline", 0.80,
                    )
                ),
            )
        except Exception:
            log.warning(
                "CalibrationStore init failed", exc_info=True,
            )
            self._calibration_store = None

        # K24: sensory anchoring cadence. Per-controller state
        # holder for the "small physical beat available" cue. No
        # persistence -- the in-memory cooldown counter resets on
        # restart, worst case = one extra beat in the first quiet
        # window post-boot. Gated by ``agent.sensory_anchor_enabled``;
        # provider short-circuits to ``""`` when the cadence is None.
        self._sensory_anchor_cadence = None
        if bool(
            getattr(settings.agent, "sensory_anchor_enabled", True)
        ):
            try:
                from app.core.conversation.sensory_anchor import SensoryAnchorCadence

                self._sensory_anchor_cadence = SensoryAnchorCadence(
                    max_recent=int(
                        getattr(
                            settings.memory,
                            "sensory_anchor_max_recent_items",
                            4,
                        )
                    ),
                )
            except Exception:
                log.warning(
                    "SensoryAnchorCadence init failed", exc_info=True,
                )
                self._sensory_anchor_cadence = None

        # K14: implicit engagement tracker. Reuses the K13 rolling word-
        # count window via ``recent_word_counts()`` so we don't pay a
        # second buffer. ``None`` when the master toggle is off; the
        # post-turn pipeline gates on the attribute being non-None.
        self._engagement_tracker = None
        if bool(
            getattr(settings.agent, "engagement_tracker_enabled", True)
        ):
            try:
                from app.core.affect.engagement_tracker import EngagementTracker

                word_count_provider = None
                analyzer = self._style_signal_analyzer
                if analyzer is not None:
                    word_count_provider = analyzer.recent_word_counts
                self._engagement_tracker = EngagementTracker(
                    agent_settings=settings.agent,
                    word_count_window_provider=word_count_provider,
                )
            except Exception:
                log.warning(
                    "EngagementTracker init failed", exc_info=True,
                )
                self._engagement_tracker = None
        # K14 per-turn state: read by ``_post_turn_inner_life`` to
        # compute reply latency, the typed-proactive eligibility
        # predicate to skip nudging an abandoned conversation, and the
        # absence-curiosity inner-life provider. Bookended by the
        # ``chat_once_streaming`` entry (stashes ``_last_turn_mode``)
        # and the post-turn pipeline (stashes label + absence).
        self._last_turn_mode: str = "typed"
        self._last_engagement_label: str = "neutral"
        self._pending_absence_seconds: float | None = None

        self._proactive = ProactiveDirector(
            self._chat_client,
            self._chat_db,
            self._prompt_assembler,
            model=self._effective_chat_model,
            speak=self._tts.enqueue,
            is_busy=lambda: self._turn_in_progress,
            is_live_mode=lambda: self._live_voice_session_active,
            cooldown_seconds=float(
                getattr(settings.agent, "proactive_cooldown_seconds", 120.0),
            ),
            cooldown_seconds_typed=float(
                getattr(settings.agent, "proactive_cooldown_seconds_typed", 600.0),
            ),
            is_typed_eligible=self._is_typed_proactive_eligible,
            context_window=self._context_window,
            notify_message=self._notify_message,
            prepared_nudge_store=self._prepared_nudge_store,
            user_id=self._user_id,
            user_display_name_provider=lambda: self.user_display_name,
            arc_store=self._arc_store,
        )

        # ── Runtime state ────────────────────────────────────────────────
        self._vad_level_threshold = settings.audio.vad_level_threshold
        self._vad_silence_seconds = settings.audio.vad_silence_seconds
        # Push-to-talk / input mode bookkeeping moved to the client.
        # The server only ever sees the resulting PCM stream.
        self._live_no_speech_streak = 0
        self._live_voice_session_active = False
        self._turn_in_progress = False

        # ── Typed-mode proactive timer + presence gate ──────────────
        # The typed-mode ``ProactiveDirector`` path fires opportunistic
        # "pick up the thread" nudges after a long quiet period (4 min
        # default). It's gated on user presence so we never poke
        # someone who alt-tabbed away. Two complementary signals fold
        # client-side into one boolean:
        #   * Browser: ``document.visibilityState === "visible"``.
        #   * Tauri:  ``tauri://focus`` / ``tauri://blur`` events.
        # Default ``True`` so a freshly-loaded UI that hasn't sent a
        # presence frame yet still works.
        self._typed_silence_timer: threading.Timer | None = None
        self._typed_silence_lock = threading.Lock()
        self._user_present: bool = True
        # Wall-clock (monotonic) when the timer was last armed AND the
        # silence budget at that moment. Used to re-arm with a smaller
        # remainder when presence flips ``False -> True`` mid-budget.
        self._typed_silence_armed_at: float | None = None
        self._typed_silence_armed_budget: float | None = None
        # Activity awareness (Phase 4): the foreground app the user is
        # currently in. ``None`` covers "couldn't determine", "user is
        # in our own window", and "feature disabled". Browser users
        # never set this. The setter is gated server-side on
        # ``activity_awareness_enabled`` so a buggy client emitting
        # events while the toggle is off can't leak the data.
        self._user_active_app: str | None = None

        self._remember_history = settings.assistant.remember_history
        self._state = SessionState(
            mic_enabled=settings.audio.enable_microphone,
            session_type="chat",
        )
        self._decision_trace: deque[dict[str, str]] = deque(maxlen=500)

        # ── Metrics ──────────────────────────────────────────────────────
        self._last_metrics: dict[str, Any] = self._zero_metrics()
        self._metrics_history: deque[dict[str, Any]] = deque(maxlen=10)
        self._compactions_total = 0
        # TTS timing: the moment chat_once_streaming finishes the LLM stream
        # is the natural "TTS may begin" mark; ``_tts_turn_start_at`` captures
        # that. We update ``_last_metrics["tts_ms"]`` when the TTS queue
        # signals "end" for a session that started after the LLM was done.
        self._tts_turn_start_at: float | None = None
        self._tts_turn_first_start_at: float | None = None

        # ── Listeners ────────────────────────────────────────────────────
        self._message_listeners: list[Callable[[str, str], None]] = []
        self._tts_state_listeners: list[Callable[..., None]] = []
        self._tts_amplitude_listeners: list[Callable[[float], None]] = []
        self._metrics_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._tts.set_amplitude_listener(self._on_tts_amplitude)
        self._models_cache: list[str] | None = None
        self._models_cache_time = 0.0
        self._cache_ttl = 60.0

        # ── MCP debug server ─────────────────────────────────────────────
        self._mcp_server_runner = None
        if settings.mcp_server.enabled:
            try:
                from app.mcp.runner import McpServerRunner
                from app.mcp.server import create_mcp_server
                mcp_srv = create_mcp_server(self, port=settings.mcp_server.port)
                self._mcp_server_runner = McpServerRunner(
                    mcp_srv, port=settings.mcp_server.port,
                )
                self._mcp_server_runner.start()
            except Exception:
                log.warning("Failed to start embedded MCP server", exc_info=True)

        # ── Phase 2a + 2b: resume opener + dream worker ─────────────────
        # Both ride the listening-window executor so init never blocks
        # on an LLM round-trip. The dream pass writes a salience-boosted
        # ``reflection`` memory; the resume pass then has a fresher
        # candidate to weave when it primes the welcome-back line.
        try:
            self._maybe_schedule_dream_pass()
        except Exception:
            log.debug("dream pass schedule failed", exc_info=True)
        try:
            self._maybe_schedule_resume_opener()
        except Exception:
            log.debug("resume opener schedule failed", exc_info=True)

        # Phase B2 — register the internal listener that turns
        # backchannel hints into low-priority motion broadcasts. Done
        # after every dependency is wired so the callback can use
        # ``self._avatar`` / ``self._avatar_motion_listeners``
        # (registered above).
        self.add_backchannel_listener(self._emit_backchannel_motion)

        # K1 follow-up — first-run onboarding goal seed. Two entry
        # paths converge on ``_seed_onboarding_goal_if_first_time``:
        #
        # 1. **Backfill** (this call here): if the user already has a
        #    display name set (returning user / migrated profile) and
        #    the ``goals.onboarding_goal_seeded`` kv_meta row is
        #    absent, drop the curated goal in now. Idempotent — on
        #    every subsequent boot the kv_meta gate skips.
        # 2. **Identity listener** (registered below): the first
        #    time ``update_user_display_name`` lands a real name,
        #    fire the seed automatically. The ``needs_onboarding``
        #    gate inside the method means the listener is a no-op
        #    until the name is actually set.
        try:
            self._seed_onboarding_goal_if_first_time()
        except Exception:
            log.debug(
                "onboarding-goal backfill failed", exc_info=True,
            )
        self.add_identity_listener(
            lambda _new_name: self._seed_onboarding_goal_if_first_time(),
        )

        # ── Brain orchestration (chunk 5 of phase 1) ─────────────────
        # Wire the task subsystem last so ``_init_task_orchestration``
        # can read every dependency it needs (``_chat_db``, ``_tts``,
        # ``_last_user_activity_at``, ``_settings.agent``) plus the
        # ``self._prompt_assembler`` we'll hook the cue provider into
        # below. The mixin is a clean no-op when
        # ``agent.tasks_enabled`` is False — the subsystem stays
        # dormant and ``self._brain_loop`` stays ``None``.
        try:
            self._init_task_orchestration()
        except Exception:
            log.exception("task-orchestration init failed")
        # The initial ``rebuild_tool_registry()`` above ran before the
        # orchestrator existed, so the filesystem task tools
        # (``list_file_roots`` / ``start_file_search`` / …) were gated
        # out (their gate is ``_task_orchestrator is not None``). Now
        # that orchestration is wired, rebuild once more so those tools
        # actually land in the registry the LLM sees. Cheap + idempotent.
        if getattr(self, "_task_orchestrator", None) is not None:
            try:
                self.rebuild_tool_registry()
            except Exception:
                log.warning(
                    "tool registry rebuild after orchestration init failed",
                    exc_info=True,
                )
        # Install the T6 task-cues provider on the prompt assembler.
        # Best-effort: a broken provider call lands as an empty
        # block (the assembler swallows provider exceptions), but a
        # missing assembler (very early shutdown / partial init)
        # would crash here so we guard the install too.
        if getattr(self, "_prompt_assembler", None) is not None:
            try:
                self._prompt_assembler.set_inner_life_providers(
                    task_cues=lambda: self.drain_task_cues_for_render(
                        turn_id=None,
                    ),
                    running_tasks=self._render_running_tasks_block,
                )
            except Exception:
                log.debug(
                    "task-orchestration provider install on prompt assembler failed",
                    exc_info=True,
                )

    # ── State ─────────────────────────────────────────────────────────

    @property
    def state(self) -> SessionState:
        return self._state

    def update_sources(self, *, mic: bool) -> None:
        self._state.mic_enabled = bool(mic)

    @property
    def session_key(self) -> str:
        return f"{self._user_id}:{self._session_id}" if self._user_id else self._session_id

    @property
    def user_display_name(self) -> str:
        """Configured user display name (or ``"friend"`` fallback).

        Single read site for every renderer, transcript formatter, and
        worker LLM prompt. Refreshes implicitly on next read after the
        identity is updated via ``update_user_display_name``.
        """
        from app.core.infra.settings import resolve_user_display_name
        return resolve_user_display_name(self._settings)

    @property
    def needs_onboarding(self) -> bool:
        """True when no display name has been configured yet."""
        from app.core.infra.settings import is_onboarding_needed
        return is_onboarding_needed(self._settings)

    def update_user_display_name(self, name: str) -> str:
        """Persist the user display name to ``config/user.json``.

        Validated to 1-32 chars after strip. Empty input is rejected
        (the caller -- REST handler -- returns 400). Returns the
        normalized stored value. Broadcasts ``identity_changed`` so the
        UI and any registered listeners see the new name without a
        reload.
        """
        cleaned = (name or "").strip()[:32]
        if not cleaned:
            raise ValueError("user_display_name must be non-empty after trim")
        self._settings.assistant.user_display_name = cleaned
        try:
            persist_user_overrides({"assistant": {"user_display_name": cleaned}})
        except Exception:
            log.warning(
                "failed to persist user_display_name to user.json",
                exc_info=True,
            )
        for listener in list(getattr(self, "_identity_listeners", []) or []):
            try:
                listener(cleaned)
            except Exception:
                log.debug("identity listener raised", exc_info=True)
        return cleaned

    def _seed_onboarding_goal_if_first_time(
        self, *, force: bool = False,
    ):
        """K1 follow-up: seed the curated "get to know {user_name}" goal.

        Idempotent via the ``goals.onboarding_goal_seeded`` row in
        ``kv_meta`` — the second call (and every call after) is a
        no-op unless ``force=True``. Gated additionally on
        ``not needs_onboarding`` so a user who hasn't typed their
        name yet doesn't get a goal that says "Get to know friend";
        the identity-listener path will fire it the moment they do.

        Called from two places:

        - ``SessionController.__init__`` (backfill for existing
          users coming back after the feature ships).
        - The identity listener registered against
          ``update_user_display_name`` — fires automatically on
          first name set.

        Defensive: returns ``None`` on any failure, never raises.
        Logged via :mod:`app.onboarding_goal` so the call is
        traceable end-to-end without a fresh logger here.
        """
        if not force and self.needs_onboarding:
            log.debug(
                "onboarding-goal: needs_onboarding=True; deferring seed",
            )
            return None
        if self._goal_store is None or self._memory_store is None:
            log.debug(
                "onboarding-goal: stores not initialised; deferring seed",
            )
            return None
        try:
            from app.core.goals.onboarding_goal import seed_onboarding_goal

            return seed_onboarding_goal(
                goal_store=self._goal_store,
                memory_store=self._memory_store,
                chat_db=self._chat_db,
                user_display_name=self.user_display_name,
                force=force,
            )
        except Exception:
            log.warning("onboarding-goal seed raised", exc_info=True)
            return None

    def add_identity_listener(self, callback: Callable[[str], None]) -> None:
        """Register a callback fired after ``update_user_display_name``.

        Workers / renderers that cache the name in pre-built prompt
        strings subscribe here to invalidate or rebuild on rename.
        """
        listeners = getattr(self, "_identity_listeners", None)
        if listeners is None:
            listeners = []
            self._identity_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def switch_session(self, session_id: str) -> None:
        # Drop any pending voice merge buffer; the new session starts
        # without an in-flight phrase A waiting for a continuation.
        self._clear_merge_buffer()
        with self._vocal_tone_lock:
            self._last_vocal_tone = None
        normalized = (session_id or "").strip()
        if not normalized:
            return
        self._session_id = normalized
        # K29 — reset the per-session opinion-injection count so the
        # cap applies to the new conversation, not the previous one.
        # Cooldown survives so a fresh switch doesn't accidentally
        # re-fire on the same beat that the prior session ended on.
        self._opinion_injection_session_count = 0
        # K28 — wipe any stashed turning-over slot so the new session
        # doesn't inherit a "this is a comeback" cue from the prior
        # one. The force-next bypass and last-fire diagnostic also
        # clear so MCP debug state matches the visible session.
        self._pending_turning_over_seconds = None
        self._turning_over_force_next = False
        self._last_turning_over = None
        # Best-effort: a write failure (read-only volume, locked file)
        # must not break the in-memory switch — the user just lands
        # back on whatever was previously persisted on next launch.
        try:
            persist_user_overrides({"session": {"last_active_id": normalized}})
        except Exception:
            log.debug("failed to persist last_active_id", exc_info=True)

    def new_session(self) -> str:
        new_id = str(uuid.uuid4())[:8]
        self.switch_session(new_id)
        return new_id

    def _resolve_initial_session_id(self, *, default: str = "main") -> str:
        """Pick the session id to land on at startup.

        Priority (first match wins):

        1. ``user.json``'s ``session.last_active_id`` if it's a non-empty
           string. Honoured even when the underlying session has no
           messages yet — this lets a "New session" → tab-close →
           reopen sequence keep the user on their fresh empty session.
        2. The most recently active session in the chat DB. Saves users
           who never had a persisted preference (first-run, downgrade
           from a build without persistence) from the cold "main"
           default if they've already chatted before.
        3. ``default`` (``"main"``).

        Pure read — no writes — so failures here just fall through.
        """
        try:
            saved = (
                read_user_overrides()
                .get("session", {})
                .get("last_active_id", "")
            )
            if isinstance(saved, str) and saved.strip():
                return saved.strip()
        except Exception:
            log.debug("read_user_overrides failed during startup", exc_info=True)
        try:
            rows = self._chat_db.list_sessions()
            if rows:
                most_recent = rows[0].get("session_id", "")
                # ``list_sessions`` returns the full ``user_id:session_id``
                # composite key; strip the user prefix so the value is
                # consistent with what ``_session_id`` stores everywhere
                # else (the session_key property re-prepends it).
                if isinstance(most_recent, str) and ":" in most_recent:
                    most_recent = most_recent.split(":", 1)[1]
                if most_recent.strip():
                    return most_recent.strip()
        except Exception:
            log.debug("list_sessions failed during startup", exc_info=True)
        return default

    def clear_conversation_memory(self) -> None:
        self._clear_merge_buffer()
        self._chat_db.clear_messages(self.session_key, full_reset=True)
        # K29 — wiping the conversation also resets per-session
        # counters; the cap is about *this conversation*, not the
        # process lifetime.
        self._opinion_injection_session_count = 0
        self._opinion_injection_cooldown = 0
        self._opinion_injection_force_next = False
        self._last_opinion_injection = None
        # K28 — same logic: a full clear should leave no stashed
        # turning-over slot or force-next bypass.
        self._pending_turning_over_seconds = None
        self._turning_over_force_next = False
        self._last_turning_over = None

    def _clear_merge_buffer(self, session_key: str | None = None) -> None:
        """Drop the voice merge buffer (one specific session, or all).

        Called on session change, on full clear, on shutdown, and
        whenever the merge window naturally closes (TTS-start, merge
        branch consumed it, barge-in flow took over).
        """
        with self._merge_lock:
            if session_key is None:
                self._merge_buffer.clear()
            else:
                self._merge_buffer.pop(session_key, None)

    def _wrap_tts_chunk_for_merge(
        self,
        inner: Callable[[str, str], None] | None,
        merge_key: str,
    ) -> Callable[[str, str], None]:
        """Return a TTS-chunk callback that closes the merge window on
        the first invocation and then forwards every chunk to ``inner``.

        Once the first audio chunk is enqueued the user has crossed the
        "Aiko is now speaking" boundary; any subsequent partial speech
        falls back to the existing barge-in flow rather than the merge
        flow. Setting ``tts_started=True`` makes ``feed_stt_partial`` skip
        the early-abort path even if the buffer is still in the dict.
        """
        first_chunk_seen = False

        def _wrapped(prepared_text: str, reaction: str) -> None:
            nonlocal first_chunk_seen
            if not first_chunk_seen:
                first_chunk_seen = True
                with self._merge_lock:
                    buf = self._merge_buffer.get(merge_key)
                    if buf is not None:
                        buf.tts_started = True
                # Once TTS has started the merge window is closed; drop
                # the buffer so we don't keep a reference to a runner
                # whose stream is past the abort-friendly point.
                self._clear_merge_buffer(merge_key)
            if inner is not None:
                inner(prepared_text, reaction)

        return _wrapped

    # ── Settings getters / setters ───────────────────────────────────

    @property
    def chat_model(self) -> str:
        return self._settings.ollama.chat_model

    @property
    def effective_chat_model(self) -> str:
        return self._effective_chat_model

    @property
    def context_window_size(self) -> int:
        return self._context_window

    @property
    def context_window_source(self) -> str:
        """Where ``context_window`` came from: ``config|client|fallback``.

        ``config`` means an explicit ``chat_llm.context_window`` (or
        legacy ``ollama.context_window``) override won. ``client``
        means the active ``ChatClient`` answered ``get_context_length``
        with a positive value — either Ollama's ``/api/show`` for
        local models or the static OpenAI-compat lookup table for
        known cloud models. ``fallback`` is the hardcoded 8192
        last-resort when neither path produced an answer.
        """
        return getattr(self, "_context_source", "fallback")

    @property
    def context_tokens_used(self) -> int:
        try:
            metrics = self._last_metrics
            return int(metrics.get("prompt_tokens", 0) or 0)
        except Exception:
            return 0

    def _resolve_context_window(
        self, override: int | None, model: str,
    ) -> tuple[int, str]:
        """Pick the context window and record the source.

        Order of preference:
        1. Explicit config override (``chat_llm.context_window`` /
           ``ollama.context_window``).
        2. Active client's ``get_context_length(model)`` — Ollama's
           ``/api/show`` for local models, the static lookup table
           in ``OpenAICompatibleClient`` for known cloud models.
        3. Hardcoded ``8192`` last-resort fallback.
        """
        if override:
            try:
                value = int(override)
                if value > 0:
                    return value, "config"
            except (TypeError, ValueError):
                pass
        try:
            detected = self._chat_client.get_context_length(model)
        except Exception:
            detected = None
        if detected and detected > 0:
            return int(detected), "client"
        return 8192, "fallback"

    def set_chat_model(self, model_name: str) -> None:
        normalized = (model_name or "").strip()
        if not normalized:
            return
        # Write the new model to the field that actually owns it:
        # ``ollama.chat_model`` for the pure-Ollama setup, and
        # ``chat_llm.model`` for the remote / OpenAI-compatible
        # setup. Cross-writing both (the pre-PR2 behaviour) used to
        # overwrite the WORKER model name on every chat-model change
        # — when chat moved to ``gpt-5-mini``, ``ollama.chat_model``
        # also became ``gpt-5-mini``, and on next boot the
        # background workers tried to hit local Ollama with the
        # remote model name (HTTP 404).
        if (self._chat_provider or "ollama").strip().lower() == "ollama":
            self._settings.ollama.chat_model = normalized
        else:
            self._settings.chat_llm.model = normalized
        self._effective_chat_model = normalized
        # The worker model only follows the chat model when the
        # worker client IS the chat client (pure-Ollama OR
        # ``workers_use_local=False``). When workers run on a
        # separate local Ollama instance, the worker model stays
        # pinned to whatever ``ollama.chat_model`` was at startup —
        # it's a different model on a different backend.
        if self._worker_client is self._chat_client:
            self._effective_worker_model = normalized
        # Re-resolve the context window for the new model. Honour the explicit
        # config override if any; otherwise re-query /api/show.
        chat_llm = self._settings.chat_llm
        ctx_override = chat_llm.context_window or getattr(
            self._settings.ollama, "context_window", None,
        )
        self._context_window, self._context_source = self._resolve_context_window(
            ctx_override, normalized,
        )
        self._turn_runner.update_runtime(
            model=normalized, context_window=self._context_window,
        )
        # Cascade the WORKER model (not the chat model) to active
        # worker instances; the proactive director is on the chat
        # path so it gets the chat model.
        worker_model = self._effective_worker_model
        self._summary_worker._model = worker_model  # type: ignore[attr-defined]
        self._proactive.update_runtime(model=normalized)
        if self._memory_extractor is not None:
            try:
                self._memory_extractor.update_model(worker_model)
            except Exception:
                log.debug("memory extractor model update failed", exc_info=True)
        if self._dialogue_act_tagger is not None:
            try:
                self._dialogue_act_tagger.update_runtime(model=worker_model)
            except Exception:
                log.debug(
                    "dialogue_act tagger model update failed", exc_info=True,
                )

    def reconfigure_chat_llm(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Rebuild ``self._chat_client`` from a partial ``chat_llm`` patch.

        ``payload`` is a dict subset of :class:`ChatLlmSettings` fields
        (any combination of ``provider``, ``provider_preset``,
        ``model``, ``base_url``, ``api_key``, ``api_key_env``,
        ``max_tokens``, ``temperature``, ``context_window``,
        ``keep_alive``, ``workers_use_local``, ``extra_headers``).

        Side effects:
        1. The in-memory :class:`ChatLlmSettings` is mutated in place.
        2. ``persist_user_overrides({"chat_llm": ...})`` writes the
           new values to ``user.json`` so the change survives a restart.
        3. ``self._chat_client`` is rebuilt via :func:`_build_chat_client`.
        4. ``self._worker_client`` (and the ``self._ollama`` alias) is
           rebound: if the new provider is non-Ollama and
           ``workers_use_local`` is True, a fresh local Ollama client
           is created; otherwise the worker client points at the
           chat client.
        5. TurnRunner + ProactiveDirector are pointed at the new
           client; the model + context window cache are reset.
        6. Worker clients **are not** swapped on existing worker
           instances — the rename in this method is best-effort
           against new turns only. A restart is required to flip
           background workers between Ollama and a remote provider.
           Documented in the "Restart required" notice on the UI.

        Returns the masked snapshot (with ``has_api_key`` instead of
        the raw key) so the REST caller can echo it back to the
        client.
        """
        chat_llm = self._settings.chat_llm

        def _set(field: str, value: Any) -> None:
            if hasattr(chat_llm, field):
                setattr(chat_llm, field, value)

        if "provider" in payload:
            raw = str(payload["provider"] or "").strip().lower()
            if raw in {"ollama", "openai_compatible"}:
                _set("provider", raw)
        if "provider_preset" in payload:
            _set("provider_preset", str(payload["provider_preset"] or "").strip().lower())
        if "model" in payload:
            _set("model", str(payload["model"] or "").strip())
        if "base_url" in payload:
            _set("base_url", str(payload["base_url"] or "").strip())
        if "api_key" in payload:
            # Empty string is a valid value here — it means "clear the
            # stored key". Don't strip-then-falsy-collapse.
            _set("api_key", str(payload["api_key"] or "").strip())
        if "api_key_env" in payload:
            _set("api_key_env", str(payload["api_key_env"] or "").strip())
        if "max_tokens" in payload:
            try:
                _set("max_tokens", max(0, int(payload["max_tokens"])))
            except (TypeError, ValueError):
                pass
        if "temperature" in payload:
            try:
                _set("temperature", float(payload["temperature"]))
            except (TypeError, ValueError):
                pass
        if "context_window" in payload:
            raw = payload["context_window"]
            try:
                _set(
                    "context_window",
                    int(raw) if raw not in (None, "", 0) else None,
                )
            except (TypeError, ValueError):
                pass
        if "keep_alive" in payload:
            _set("keep_alive", str(payload["keep_alive"] or "").strip() or "30m")
        if "workers_use_local" in payload:
            _set("workers_use_local", bool(payload["workers_use_local"]))
        if "extra_headers" in payload:
            raw_headers = payload.get("extra_headers") or {}
            if isinstance(raw_headers, dict):
                _set("extra_headers", {
                    str(k).strip(): str(v).strip()
                    for k, v in raw_headers.items()
                    if str(k).strip() and v is not None
                })

        # Persist (drops api_key entirely if the user cleared it).
        try:
            persist_user_overrides({"chat_llm": {
                "provider": chat_llm.provider,
                "provider_preset": chat_llm.provider_preset,
                "model": chat_llm.model,
                "base_url": chat_llm.base_url,
                "api_key": chat_llm.api_key,
                "api_key_env": chat_llm.api_key_env,
                "max_tokens": chat_llm.max_tokens,
                "temperature": chat_llm.temperature,
                "context_window": chat_llm.context_window,
                "keep_alive": chat_llm.keep_alive,
                "workers_use_local": chat_llm.workers_use_local,
                "extra_headers": dict(chat_llm.extra_headers or {}),
            }})
        except Exception:
            log.warning("persist chat_llm overrides failed", exc_info=True)

        # Rebuild clients.
        self._chat_client = _build_chat_client(
            chat_llm=chat_llm,
            ollama_settings=self._settings.ollama,
            role="chat",
        )
        if (
            (chat_llm.provider or "ollama").strip().lower() != "ollama"
            and bool(chat_llm.workers_use_local)
        ):
            self._worker_client = OllamaClient(
                self._settings.ollama,
                base_url=self._settings.ollama.base_url,
                keep_alive=chat_llm.keep_alive,
            )
        else:
            self._worker_client = self._chat_client
        self._ollama = self._worker_client  # back-compat alias
        self._chat_provider = (chat_llm.provider or "ollama").strip().lower()

        # Recompute the worker model based on the new client topology:
        # when the worker client is a separate local Ollama, the
        # worker model is pinned to ``ollama.chat_model``; otherwise
        # it tracks the chat model. ``set_chat_model`` below picks
        # this up via ``self._effective_worker_model``.
        if self._worker_client is self._chat_client:
            self._effective_worker_model = (
                chat_llm.model.strip()
                or self._settings.ollama.chat_model.strip()
                or "llama3.1:8b"
            )
        else:
            self._effective_worker_model = (
                (self._settings.ollama.chat_model or "").strip()
                or "llama3.1:8b"
            )

        # Re-resolve model + context window. ``set_chat_model`` does
        # the right cascade (TurnRunner / ProactiveDirector / workers).
        new_model = (
            chat_llm.model.strip()
            or self._settings.ollama.chat_model.strip()
            or "llama3.1:8b"
        )
        # Drop the model-listing cache so the next /api/models lands fresh.
        self._models_cache = None
        # Point TurnRunner + ProactiveDirector at the new client.
        # ``set_chat_model`` below cascades the model/context update.
        try:
            self._turn_runner.update_runtime(client=self._chat_client)
        except Exception:
            log.debug("turn_runner update_runtime(client=) failed", exc_info=True)
        try:
            self._proactive.update_runtime(client=self._chat_client)
        except Exception:
            log.debug("proactive update_runtime(client=) failed", exc_info=True)
        self.set_chat_model(new_model)
        log.info(
            "chat_llm reconfigured: provider=%s model=%s base_url=%s "
            "workers_use_local=%s has_api_key=%s",
            chat_llm.provider,
            self._effective_chat_model,
            chat_llm.base_url or "(default)",
            "1" if chat_llm.workers_use_local else "0",
            "1" if (chat_llm.api_key or "").strip() else "0",
        )
        # PR 2: mirror the just-applied legacy state back into the
        # catalogue so ``llm.routes.main_chat`` stays in sync. Cheap
        # (mutates in-memory dataclasses), idempotent, and lets the
        # new REST surface read either ``chat_llm`` or the catalogue
        # interchangeably.
        try:
            self._sync_llm_routes_from_legacy()
            self._persist_llm_settings()
        except Exception:
            log.debug("sync llm.routes from legacy failed", exc_info=True)
        return self._chat_llm_public_snapshot()

    def _chat_llm_public_snapshot(self) -> dict[str, Any]:
        """Return a serialisable view of ``chat_llm`` with the API key masked.

        Used by ``GET /api/settings`` and the response to PATCH /
        PUT credentials. The raw key is replaced by a boolean
        ``has_api_key`` flag; the UI shows ``••••••••`` when true and
        empty when false.
        """
        cfg = self._settings.chat_llm
        return {
            "provider": cfg.provider,
            "provider_preset": cfg.provider_preset,
            "model": cfg.model,
            "base_url": cfg.base_url,
            "has_api_key": bool((cfg.api_key or "").strip()),
            "api_key_env": cfg.api_key_env,
            "max_tokens": int(cfg.max_tokens),
            "temperature": (
                float(cfg.temperature) if cfg.temperature is not None else None
            ),
            "context_window": cfg.context_window,
            "keep_alive": cfg.keep_alive,
            "workers_use_local": bool(cfg.workers_use_local),
            "extra_headers": dict(cfg.extra_headers or {}),
        }

    # ── PR 2: provider catalogue + role-assignment API ──────────────
    #
    # The catalogue lives on ``self._settings.llm`` and is kept in sync
    # with the legacy ``chat_llm`` + ``ollama`` blocks via the
    # mirror-write helpers below. The legacy blocks remain the
    # in-memory primary for now (the ``_chat_client`` / ``_worker_client``
    # construction paths still read from them) — this keeps the diff
    # contained and lets external scripts / MCP keep reading
    # ``chat_llm`` unchanged. Phase 3 may flip the direction.

    def _mask_provider(self, provider: LlmProvider) -> dict[str, Any]:
        """Return a JSON-serialisable view of ``provider`` with the
        ``api_key`` masked behind a ``has_api_key`` flag."""
        return {
            "id": provider.id,
            "name": provider.name,
            "kind": provider.kind,
            "base_url": provider.base_url,
            "has_api_key": bool((provider.api_key or "").strip()),
            "api_key_env": provider.api_key_env,
            "extra_headers": dict(provider.extra_headers or {}),
            "timeout_seconds": int(provider.timeout_seconds or 300),
            "keep_alive": provider.keep_alive,
        }

    def list_providers(self) -> list[dict[str, Any]]:
        """Return the catalogue with credentials masked."""
        return [self._mask_provider(p) for p in self._settings.llm.providers]

    def list_routes(self) -> dict[str, dict[str, Any]]:
        """Return the role-assignment table."""
        out: dict[str, dict[str, Any]] = {}
        for role, route in self._settings.llm.routes.items():
            out[role] = {
                "provider_id": route.provider_id,
                "model": route.model,
                "context_window": route.context_window,
                "max_tokens": int(route.max_tokens or 512),
                "temperature": route.temperature,
            }
        return out

    def _find_llm_provider(self, provider_id: str) -> LlmProvider | None:
        for entry in self._settings.llm.providers:
            if entry.id == provider_id:
                return entry
        return None

    def _generate_provider_id(self, template_id: str | None) -> str:
        """Pick a unique id for a new provider.

        Uses ``template_id`` as a seed when supplied; appends a suffix
        when the natural id is already taken so two "openai" entries
        can coexist (e.g. a "personal" key and a "team" key).
        """
        base = (template_id or "custom").strip().lower()
        existing = {p.id for p in self._settings.llm.providers}
        if base not in existing:
            return base
        for i in range(2, 100):
            candidate = f"{base}_{i}"
            if candidate not in existing:
                return candidate
        return f"{base}_{uuid.uuid4().hex[:8]}"

    def add_provider(
        self,
        *,
        template_id: str | None = None,
        draft: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a new provider to the catalogue.

        ``template_id`` (optional) seeds the entry from a row of
        :func:`_PROVIDER_PRESETS` (``"openai"``, ``"gemini"``, …). The
        ``draft`` dict can override any field. Returns the masked
        snapshot of the inserted entry.
        """
        seed: dict[str, Any] = {}
        if template_id:
            for preset in _PROVIDER_PRESETS:
                if preset.get("id") == template_id:
                    seed = {
                        "kind": preset.get("provider", "ollama"),
                        "name": preset.get("label", template_id),
                        "base_url": preset.get("base_url", ""),
                        "api_key_env": preset.get("env_hint", ""),
                    }
                    break
        payload = dict(draft or {})
        for k, v in seed.items():
            payload.setdefault(k, v)
        # Translate the legacy "provider" key (used in presets) to the
        # new "kind" field.
        if "kind" not in payload and "provider" in payload:
            payload["kind"] = payload.pop("provider")
        provider_id = (
            str(payload.get("id", "") or "").strip()
            or self._generate_provider_id(template_id)
        )
        kind = str(payload.get("kind", "ollama") or "ollama").strip().lower()
        if kind not in {"ollama", "openai_compatible"}:
            kind = "ollama"
        name = str(payload.get("name", "") or "").strip() or provider_id
        base_url = str(payload.get("base_url", "") or "").strip()
        api_key = str(payload.get("api_key", "") or "").strip()
        api_key_env = str(payload.get("api_key_env", "") or "").strip()
        headers_raw = payload.get("extra_headers") or {}
        if isinstance(headers_raw, dict):
            extra_headers = {
                str(k).strip(): str(v).strip()
                for k, v in headers_raw.items()
                if str(k).strip() and v is not None
            }
        else:
            extra_headers = {}
        try:
            timeout = max(1, int(payload.get("timeout_seconds", 300)))
        except (TypeError, ValueError):
            timeout = 300
        keep_alive = str(payload.get("keep_alive", "30m") or "30m").strip() or "30m"
        new_provider = LlmProvider(
            id=provider_id,
            name=name,
            kind=kind,
            base_url=base_url,
            api_key=api_key,
            api_key_env=api_key_env,
            extra_headers=extra_headers,
            timeout_seconds=timeout,
            keep_alive=keep_alive,
        )
        if self._find_llm_provider(provider_id) is not None:
            raise ValueError(
                f"provider id {provider_id!r} already exists; "
                "edit the existing entry or pick a different id"
            )
        self._settings.llm.providers.append(new_provider)
        self._persist_llm_settings()
        log.info(
            "llm: added provider id=%s kind=%s base_url=%s",
            new_provider.id,
            new_provider.kind,
            new_provider.base_url,
        )
        return self._mask_provider(new_provider)

    def update_provider(
        self,
        provider_id: str,
        draft: dict[str, Any],
    ) -> dict[str, Any]:
        """Edit non-credential fields on an existing provider.

        Use :meth:`update_provider_credentials` for the api_key /
        api_key_env path (separate to keep credentials out of logs).
        """
        provider = self._find_llm_provider(provider_id)
        if provider is None:
            raise KeyError(f"unknown provider id={provider_id!r}")
        if "name" in draft:
            provider.name = str(draft["name"] or "").strip() or provider.name
        if "kind" in draft:
            kind = str(draft["kind"] or "").strip().lower()
            if kind in {"ollama", "openai_compatible"}:
                provider.kind = kind
        if "base_url" in draft:
            provider.base_url = str(draft["base_url"] or "").strip()
        if "extra_headers" in draft:
            raw_headers = draft.get("extra_headers") or {}
            if isinstance(raw_headers, dict):
                provider.extra_headers = {
                    str(k).strip(): str(v).strip()
                    for k, v in raw_headers.items()
                    if str(k).strip() and v is not None
                }
        if "timeout_seconds" in draft:
            try:
                provider.timeout_seconds = max(1, int(draft["timeout_seconds"]))
            except (TypeError, ValueError):
                pass
        if "keep_alive" in draft:
            provider.keep_alive = (
                str(draft["keep_alive"] or "").strip() or "30m"
            )
        # Anything changed -> drop the cached client so future
        # ``cache.get`` rebuilds with the new fields.
        self._client_cache.invalidate(provider_id)
        # If the main_chat route still points at this provider, mirror
        # the changes back to the legacy ``chat_llm`` block so the
        # active session reflects them.
        main_route = self._settings.llm.routes.get(LLM_ROLE_MAIN_CHAT)
        if main_route is not None and main_route.provider_id == provider_id:
            self._mirror_route_to_chat_llm(provider, main_route)
            # Rebuild the active chat client so the next turn picks up
            # the new base_url / extra_headers immediately.
            self._chat_client = _build_chat_client(
                chat_llm=self._settings.chat_llm,
                ollama_settings=self._settings.ollama,
                role="chat",
            )
            try:
                self._turn_runner.update_runtime(client=self._chat_client)
                self._proactive.update_runtime(client=self._chat_client)
            except Exception:
                log.debug("update_runtime(client=) after provider edit failed", exc_info=True)
        self._persist_llm_settings()
        log.info("llm: updated provider id=%s", provider_id)
        return self._mask_provider(provider)

    def update_provider_credentials(
        self,
        provider_id: str,
        creds: dict[str, Any],
    ) -> dict[str, Any]:
        """Replace the api_key / api_key_env on an existing provider."""
        provider = self._find_llm_provider(provider_id)
        if provider is None:
            raise KeyError(f"unknown provider id={provider_id!r}")
        if "api_key" in creds:
            provider.api_key = str(creds["api_key"] or "").strip()
        if "api_key_env" in creds:
            provider.api_key_env = str(creds["api_key_env"] or "").strip()
        # Credentials changed -> invalidate the cached client so the
        # next get() rebuilds with the new bearer header.
        self._client_cache.invalidate(provider_id)
        # If main_chat references this provider, mirror to chat_llm and
        # rebuild the in-flight chat client.
        main_route = self._settings.llm.routes.get(LLM_ROLE_MAIN_CHAT)
        if main_route is not None and main_route.provider_id == provider_id:
            self._settings.chat_llm.api_key = provider.api_key
            self._settings.chat_llm.api_key_env = provider.api_key_env
            self._chat_client = _build_chat_client(
                chat_llm=self._settings.chat_llm,
                ollama_settings=self._settings.ollama,
                role="chat",
            )
            try:
                self._turn_runner.update_runtime(client=self._chat_client)
                self._proactive.update_runtime(client=self._chat_client)
            except Exception:
                log.debug("update_runtime(client=) after credentials edit failed", exc_info=True)
        self._persist_llm_settings()
        log.info(
            "llm: updated credentials provider=%s has_api_key=%s",
            provider_id,
            "1" if (provider.api_key or "").strip() else "0",
        )
        return self._mask_provider(provider)

    def remove_provider(self, provider_id: str) -> None:
        """Delete a provider. Fails with ``ValueError`` when any route
        still references it (the UI catches the 409 and asks the user
        to retarget the route first)."""
        if self._find_llm_provider(provider_id) is None:
            raise KeyError(f"unknown provider id={provider_id!r}")
        referenced_by = [
            role
            for role, route in self._settings.llm.routes.items()
            if route.provider_id == provider_id
        ]
        if referenced_by:
            raise ValueError(
                f"provider id={provider_id!r} is still referenced by "
                f"route(s) {sorted(referenced_by)!r}; retarget them first"
            )
        self._settings.llm.providers = [
            p for p in self._settings.llm.providers
            if p.id != provider_id
        ]
        self._client_cache.invalidate(provider_id)
        self._persist_llm_settings()
        log.info("llm: removed provider id=%s", provider_id)

    def update_route(
        self,
        role: str,
        draft: dict[str, Any],
    ) -> dict[str, Any]:
        """Set ``llm.routes[role]`` from a partial draft.

        For ``main_chat`` this is the catalogue-aware equivalent of
        :meth:`reconfigure_chat_llm`: it mutates the route, mirrors
        the matching fields back to the legacy ``chat_llm`` block,
        and rebuilds the chat client + cascades to TurnRunner /
        ProactiveDirector / SummaryWorker via ``set_chat_model``. For
        ``worker_default`` the route + cache update is recorded but
        the in-flight workers still read from the legacy
        ``ollama`` + ``chat_llm.workers_use_local`` config — Phase 3
        will swap that.
        """
        role_name = (role or "").strip()
        if not role_name:
            raise ValueError("role must be a non-empty string")
        current = self._settings.llm.routes.get(role_name)
        if current is None:
            # Allow creation of new roles (Phase 3 prep).
            current = LlmRoute(provider_id="", model="")
        if "provider_id" in draft:
            current.provider_id = str(draft["provider_id"] or "").strip()
        if "model" in draft:
            current.model = str(draft["model"] or "").strip()
        if "context_window" in draft:
            raw = draft["context_window"]
            try:
                current.context_window = (
                    int(raw) if raw not in (None, "", 0) else None
                )
            except (TypeError, ValueError):
                current.context_window = None
        if "max_tokens" in draft:
            try:
                current.max_tokens = max(0, int(draft["max_tokens"] or 0)) or 512
            except (TypeError, ValueError):
                pass
        if "temperature" in draft:
            raw = draft["temperature"]
            try:
                current.temperature = (
                    float(raw) if raw not in (None, "") else None
                )
            except (TypeError, ValueError):
                current.temperature = None
        provider = self._find_llm_provider(current.provider_id)
        if provider is None:
            raise KeyError(
                f"route {role_name!r} references unknown "
                f"provider_id={current.provider_id!r}"
            )
        self._settings.llm.routes[role_name] = current
        if role_name == LLM_ROLE_MAIN_CHAT:
            # Mirror to legacy chat_llm + rebuild client (uses the
            # existing reconfigure_chat_llm path so all the cascades
            # fire correctly).
            chat_payload = self._route_to_chat_llm_payload(provider, current)
            self.reconfigure_chat_llm(chat_payload)
        else:
            # Non-chat role: persist the catalogue snapshot. Workers
            # don't pick up the new client mid-flight; restart required.
            self._persist_llm_settings()
        log.info(
            "llm: updated route %s -> provider=%s model=%s context=%s",
            role_name,
            current.provider_id,
            current.model,
            current.context_window,
        )
        return {
            "provider_id": current.provider_id,
            "model": current.model,
            "context_window": current.context_window,
            "max_tokens": int(current.max_tokens or 512),
            "temperature": current.temperature,
        }

    def test_provider(
        self,
        provider_id: str,
        *,
        override_model: str | None = None,
        override_context_window: int | None = None,
    ) -> dict[str, Any]:
        """Run a one-token probe chat against ``provider``.

        Returns the same shape as the existing
        ``POST /api/llm/test-connection`` response so the UI can
        reuse the same banner. The probe is built from the provider's
        own credentials (never touches the saved key on a different
        entry). ``override_model`` lets the caller test a model id
        the user is typing in the combobox before committing to save.
        """
        provider = self._find_llm_provider(provider_id)
        if provider is None:
            raise KeyError(f"unknown provider id={provider_id!r}")
        # Borrow the existing test-connection plumbing. We synthesise
        # a one-off ``ChatLlmSettings`` instance from the provider +
        # the overrides so the test path stays identical to the
        # legacy ``POST /api/llm/test-connection``.
        from app.core.infra.settings import ChatLlmSettings

        candidate_model = (override_model or "").strip()
        if not candidate_model:
            main_route = self._settings.llm.routes.get(LLM_ROLE_MAIN_CHAT)
            if main_route is not None and main_route.provider_id == provider_id:
                candidate_model = main_route.model
        probe_settings = ChatLlmSettings(
            provider=provider.kind,
            model=candidate_model,
            base_url=provider.base_url,
            api_key=provider.api_key,
            api_key_env=provider.api_key_env,
            context_window=override_context_window,
            extra_headers=dict(provider.extra_headers or {}),
            max_tokens=8,  # enough for a one-token probe
            keep_alive=provider.keep_alive,
        )
        start = time.time()
        try:
            probe = _build_chat_client(
                chat_llm=probe_settings,
                ollama_settings=self._settings.ollama,
                role="test",
            )
            try:
                resp = probe.chat(
                    [{"role": "user", "content": "Reply 'ok'."}],
                    model=candidate_model,
                    options={"num_predict": 4, "temperature": 0},
                )
            finally:
                close = getattr(probe, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass
            latency_ms = int((time.time() - start) * 1000)
            usage = getattr(resp, "usage", None)
            completion_tokens = int(
                getattr(usage, "completion_tokens", 0) or 0
            )
            return {
                "success": True,
                "latency_ms": latency_ms,
                "completion_tokens": completion_tokens,
                "model": candidate_model,
            }
        except Exception as exc:
            return {
                "success": False,
                "error_code": exc.__class__.__name__,
                "error_message": str(exc) or "Provider rejected the request.",
                "model": candidate_model,
            }

    def client_cache_stats(self) -> dict[str, Any]:
        """Diagnostic snapshot of the shared client cache."""
        return self._client_cache.stats()

    # ── PR 2: catalogue <-> legacy mirror helpers ───────────────────

    def _mirror_route_to_chat_llm(
        self, provider: LlmProvider, route: LlmRoute,
    ) -> None:
        """Write a (provider, route) pair into the legacy ``chat_llm`` block.

        Keeps the legacy block in sync with the new catalogue so
        downstream code that still reads ``chat_llm.*`` (and external
        scripts) keeps working unchanged.
        """
        cfg = self._settings.chat_llm
        cfg.provider = provider.kind
        cfg.model = route.model
        cfg.base_url = provider.base_url
        cfg.api_key = provider.api_key
        cfg.api_key_env = provider.api_key_env
        cfg.extra_headers = dict(provider.extra_headers or {})
        cfg.keep_alive = provider.keep_alive or "30m"
        cfg.context_window = route.context_window
        cfg.max_tokens = int(route.max_tokens or 512)
        cfg.temperature = route.temperature
        # Set the UI hint to the provider id when it matches a known
        # preset (purely cosmetic — used to highlight the card).
        cfg.provider_preset = (
            provider.id if provider.id in {p["id"] for p in _PROVIDER_PRESETS} else ""
        )

    def _route_to_chat_llm_payload(
        self, provider: LlmProvider, route: LlmRoute,
    ) -> dict[str, Any]:
        """Translate a (provider, route) pair into a ``reconfigure_chat_llm``
        payload so we can reuse all the legacy cascade plumbing."""
        return {
            "provider": provider.kind,
            "provider_preset": (
                provider.id if provider.id in {p["id"] for p in _PROVIDER_PRESETS} else ""
            ),
            "model": route.model,
            "base_url": provider.base_url,
            "api_key": provider.api_key,
            "api_key_env": provider.api_key_env,
            "max_tokens": int(route.max_tokens or 512),
            "temperature": route.temperature,
            "context_window": route.context_window,
            "keep_alive": provider.keep_alive,
            "extra_headers": dict(provider.extra_headers or {}),
            # ``workers_use_local`` lives outside the catalogue for
            # now (a per-role concern that the new ``routes`` table
            # supersedes); keep the existing value to avoid
            # accidentally flipping it.
            "workers_use_local": bool(self._settings.chat_llm.workers_use_local),
        }

    def _sync_llm_routes_from_legacy(self) -> None:
        """Mirror ``chat_llm`` + ``ollama`` back into ``llm.routes``.

        Called at the end of :meth:`reconfigure_chat_llm` so a legacy
        PATCH against ``/api/settings`` (e.g. from an old client) still
        leaves ``llm.routes.main_chat`` consistent with the new state.
        Also runs at end of ``__init__`` so a fresh boot lands with
        the two snapshots in sync even when the migration produced a
        slightly stale shape.
        """
        chat_llm = self._settings.chat_llm
        ollama = self._settings.ollama
        # Make sure a local_ollama provider exists (it must — the
        # migration synthesises one, but a hand-edited user.json
        # could have removed it).
        local_provider = self._find_llm_provider("local_ollama")
        if local_provider is None:
            local_provider = LlmProvider(
                id="local_ollama",
                name="Local Ollama",
                kind="ollama",
                base_url=(ollama.base_url or "").strip() or "http://127.0.0.1:11434",
                api_key="",
                api_key_env="",
                extra_headers={},
                timeout_seconds=int(getattr(ollama, "timeout", 300)) or 300,
                keep_alive="30m",
            )
            self._settings.llm.providers.append(local_provider)
        else:
            # Keep base_url + timeout in sync with the legacy block.
            local_provider.base_url = (ollama.base_url or "").strip() or local_provider.base_url
            local_provider.timeout_seconds = int(getattr(ollama, "timeout", 300)) or local_provider.timeout_seconds
        # Resolve which provider main_chat points at.
        provider_id_for_chat = "local_ollama"
        if (chat_llm.provider or "").strip().lower() != "ollama" or (
            chat_llm.base_url and not _urls_match(chat_llm.base_url, local_provider.base_url)
        ):
            # Find or create a separate provider entry that matches
            # the legacy chat_llm block.
            preset_id = (chat_llm.provider_preset or "").strip().lower()
            target_id = preset_id or "chat_migrated"
            if target_id == "local_ollama":
                target_id = "chat_migrated"
            existing = self._find_llm_provider(target_id)
            if existing is None:
                kind = (chat_llm.provider or "openai_compatible").strip().lower()
                if kind not in {"ollama", "openai_compatible"}:
                    kind = "openai_compatible"
                existing = LlmProvider(
                    id=target_id,
                    name=preset_id.title() if preset_id else "Chat provider",
                    kind=kind,
                    base_url=(chat_llm.base_url or "").strip(),
                    api_key=chat_llm.api_key or "",
                    api_key_env=chat_llm.api_key_env or "",
                    extra_headers=dict(chat_llm.extra_headers or {}),
                    timeout_seconds=int(getattr(ollama, "timeout", 300)) or 300,
                    keep_alive=chat_llm.keep_alive or "30m",
                )
                self._settings.llm.providers.append(existing)
            else:
                kind = (chat_llm.provider or existing.kind).strip().lower()
                if kind in {"ollama", "openai_compatible"}:
                    existing.kind = kind
                existing.base_url = (chat_llm.base_url or existing.base_url).strip()
                existing.api_key = chat_llm.api_key or existing.api_key
                existing.api_key_env = chat_llm.api_key_env or existing.api_key_env
                existing.extra_headers = dict(chat_llm.extra_headers or existing.extra_headers or {})
                existing.keep_alive = chat_llm.keep_alive or existing.keep_alive
            provider_id_for_chat = target_id
        self._settings.llm.routes[LLM_ROLE_MAIN_CHAT] = LlmRoute(
            provider_id=provider_id_for_chat,
            model=(chat_llm.model or "").strip(),
            context_window=chat_llm.context_window,
            max_tokens=int(chat_llm.max_tokens or 512),
            temperature=chat_llm.temperature,
        )
        self._settings.llm.routes[LLM_ROLE_WORKER_DEFAULT] = LlmRoute(
            provider_id="local_ollama",
            model=(ollama.chat_model or "").strip(),
            context_window=ollama.context_window,
            max_tokens=512,
            temperature=None,
        )

    def _persist_llm_settings(self) -> None:
        """Write the catalogue + routes to ``user.json``.

        Mirrors :func:`persist_user_overrides` for the ``chat_llm``
        block. Credentials are NOT masked here — they live on disk in
        ``user.json`` the same way the legacy ``chat_llm.api_key``
        does. ``user.json`` has file-system permissions and is
        gitignored; the masking only happens on the REST + WS layer.
        """
        providers_payload: list[dict[str, Any]] = []
        for p in self._settings.llm.providers:
            providers_payload.append({
                "id": p.id,
                "name": p.name,
                "kind": p.kind,
                "base_url": p.base_url,
                "api_key": p.api_key,
                "api_key_env": p.api_key_env,
                "extra_headers": dict(p.extra_headers or {}),
                "timeout_seconds": int(p.timeout_seconds or 300),
                "keep_alive": p.keep_alive,
            })
        routes_payload: dict[str, dict[str, Any]] = {}
        for role, r in self._settings.llm.routes.items():
            routes_payload[role] = {
                "provider_id": r.provider_id,
                "model": r.model,
                "context_window": r.context_window,
                "max_tokens": int(r.max_tokens or 512),
                "temperature": r.temperature,
            }
        try:
            persist_user_overrides({
                "llm": {
                    "providers": providers_payload,
                    "routes": routes_payload,
                },
            })
        except Exception:
            log.warning("persist llm overrides failed", exc_info=True)

    @staticmethod
    def provider_presets() -> list[dict[str, Any]]:
        """Return the curated preset catalogue.

        Static method — the catalogue is process-wide. Exposed via
        ``GET /api/llm/presets``.
        """
        return [dict(p) for p in _PROVIDER_PRESETS]

    @property
    def remember_history(self) -> bool:
        return self._remember_history

    def set_remember_history(self, value: bool) -> None:
        self._remember_history = bool(value)

    @property
    def active_session_type(self) -> str:
        return "chat"

    # ── Audio: client-fed mic + speaker streams ─────────────────────

    @property
    def mic_source(self) -> ClientMicSource:
        """The active mic source. WS layer pipes binary frames into it."""
        return self._microphone

    def feed_audio_start(
        self,
        sample_rate: int,
        channels: int,
        dsp_flags: int = 0,
    ) -> None:
        """Handle a ``0x02 mic_start`` frame from the active voice owner."""
        try:
            self._microphone.feed_start(sample_rate, channels, dsp_flags)
        except Exception:
            log.debug("mic feed_start failed", exc_info=True)

    def feed_audio_frame(
        self,
        sample_rate: int,
        channels: int,
        pcm_int16_le: bytes,
    ) -> None:
        """Handle a ``0x01 mic_pcm`` frame from the active voice owner."""
        try:
            self._microphone.feed_pcm(sample_rate, channels, pcm_int16_le)
        except Exception:
            log.debug("mic feed_pcm failed", exc_info=True)

    def feed_audio_end(self) -> None:
        """Signal end of the current mic stream (owner released / disconnected)."""
        try:
            self._microphone.feed_end()
        except Exception:
            log.debug("mic feed_end failed", exc_info=True)

    def set_audio_frame_listener(
        self,
        listener: Callable[[str, int, int, bytes], None] | None,
        *,
        end_listener: Callable[[str], None] | None = None,
    ) -> None:
        """Install a sink for outbound TTS / earcon PCM.

        The web server registers a callback that broadcasts the bytes
        as ``0x10 tts_pcm`` / ``0x11 earcon_pcm`` frames to every
        connected client. ``stream`` is ``"tts"`` or ``"earcon"`` so
        the hub picks the right frame type.
        """
        self._audio_frame_listener = listener
        self._audio_frame_end_listener = end_listener

    def _emit_audio_frame(
        self,
        stream: str,
        sample_rate: int,
        channels: int,
        pcm: bytes,
    ) -> None:
        listener = self._audio_frame_listener
        if listener is None:
            return
        try:
            listener(stream, int(sample_rate), int(channels), pcm)
        except Exception:
            log.debug("audio frame listener raised", exc_info=True)

    def _emit_audio_frame_end(self, stream: str) -> None:
        end_listener = self._audio_frame_end_listener
        if end_listener is None:
            return
        try:
            end_listener(stream)
        except Exception:
            log.debug("audio frame end listener raised", exc_info=True)

    def barge_in_enabled(self) -> bool:
        return bool(getattr(self._settings.audio, "barge_in_enabled", False))

    def set_barge_in_enabled(self, enabled: bool) -> None:
        self._settings.audio.barge_in_enabled = bool(enabled)

    @property
    def vad_level_threshold(self) -> float:
        return float(self._vad_level_threshold)

    def set_vad_level_threshold(self, value: float) -> None:
        self._vad_level_threshold = float(value)

    @property
    def vad_silence_seconds(self) -> float:
        return float(self._vad_silence_seconds)

    def set_vad_silence_seconds(self, value: float) -> None:
        self._vad_silence_seconds = float(value)

    @property
    def stt_model(self) -> str:
        return str(self._settings.stt.model or "large-v1").strip() or "large-v1"

    def set_stt_model(self, model_name: str) -> bool:
        normalized = (model_name or "").strip()
        if not normalized:
            return False
        if normalized == self.stt_model:
            return True
        self._settings.stt.model = normalized
        candidate = RealtimeSttService(self._settings.stt, self._settings.audio)
        if not candidate.is_available:
            log.warning("Failed to load STT model: %s", normalized)
            return False
        self._realtime_stt = candidate
        return True

    # ── TTS API ──────────────────────────────────────────────────────

    @property
    def tts_provider(self) -> str:
        return (self._settings.tts.provider or "pocket-tts").strip().lower() or "pocket-tts"

    def list_tts_providers(self) -> list[str]:
        return ["pocket-tts"]

    @property
    def tts_voice(self) -> str:
        return self._settings.tts.voice or ""

    def list_tts_voices(self) -> list[str]:
        list_voices = getattr(self._tts_engine, "list_voices", None)
        if callable(list_voices):
            try:
                voices = list_voices()
                if voices:
                    return list(voices)
            except Exception:
                pass
        return []

    def set_tts_voice(self, voice: str) -> None:
        normalized = (voice or "").strip()
        if not normalized:
            return
        self._settings.tts.voice = normalized
        set_voice = getattr(self._tts_engine, "set_voice", None)
        if callable(set_voice):
            try:
                set_voice(normalized)
            except Exception:
                log.debug("tts engine rejected voice switch", exc_info=True)

    def get_tts_model_status(self) -> tuple[str, str]:
        getter = getattr(self._tts_engine, "model_status", None)
        if callable(getter):
            try:
                state, details = getter()
                return str(state), str(details)
            except Exception:
                pass
        return ("unknown", "")

    def stop_tts(self) -> None:
        self._tts.stop()

    def is_tts_playing(self) -> bool:
        return self._tts.is_active()

    def speak_text(self, text: str) -> bool:
        if not bool(getattr(self._settings.tts, "enabled", True)):
            return False
        prepared = prepare_tts_text(text or "")
        if not prepared:
            return False
        reaction = infer_tts_reaction(prepared)
        self._tts.enqueue(prepared, reaction=reaction)
        return True

    def set_tts_provider(self, provider: str) -> None:
        normalized = (provider or "").strip().lower() or "pocket-tts"
        if normalized == self.tts_provider:
            return
        try:
            self._tts.stop()
        except Exception:
            pass
        self._settings.tts.provider = normalized
        self._tts_engine = self._build_tts_service(self._settings)
        # Rewire the PCM listener so the new engine still pushes
        # audio to whichever WS hub callback is currently installed.
        self._tts_engine.set_pcm_listener(
            lambda rate, ch, pcm: self._emit_audio_frame("tts", rate, ch, pcm),
            end_listener=lambda: self._emit_audio_frame_end("tts"),
        )
        self._tts = TtsQueue(
            self._tts_engine,
            enabled=bool(self._settings.tts.enabled),
            state_listener=self._on_tts_state,
            amplitude_listener=self._on_tts_amplitude,
            earcon_player=self._earcons,
        )
        # Phase 5b: re-bind the ProsodyDispatcher to the new queue.
        prosody = getattr(self, "_prosody", None)
        if prosody is not None:
            try:
                prosody._enqueue = self._tts.enqueue  # noqa: SLF001
            except Exception:
                log.debug("prosody rebind failed", exc_info=True)
        self._apply_assistant_preferences()
        self._trace("tts.provider", f"Switched TTS provider to {normalized}")

    def prewarm_tts(self) -> None:
        warmup_sync = getattr(self._tts_engine, "warmup_sync", None)
        if callable(warmup_sync):
            try:
                warmup_sync()
            except Exception:
                log.debug("tts warmup_sync failed", exc_info=True)
            return
        warmup_async = getattr(self._tts_engine, "warmup_async", None)
        if callable(warmup_async):
            try:
                warmup_async()
            except Exception:
                log.debug("tts warmup_async failed", exc_info=True)

    def prewarm_runtime(self, on_status: Callable[[str], None] | None = None) -> None:
        def report(message: str) -> None:
            if on_status:
                on_status(message)

        effective = self._effective_chat_model
        cloud_model = effective.endswith("-cloud") or effective.endswith(":cloud")
        provider = self._chat_provider or "ollama"
        # For remote OpenAI-compatible providers we skip the local
        # "model not found" guard (we can't enumerate every Gemini /
        # OpenAI model reliably, and even when we can it costs an
        # extra request that doesn't actually warm anything). We do
        # still optionally probe ``/v1/models`` so a wrong base_url
        # surfaces with a clear error before the first real turn.
        if provider == "openai_compatible":
            report(f"Checking {provider} endpoint...")
            try:
                # Best-effort: ``list_models`` returns ``[]`` on failure
                # rather than raising, so the boot stays healthy.
                self._chat_client.list_models()
            except Exception:
                log.debug("openai-compat list_models probe failed", exc_info=True)
            report(f"Using remote model: {effective} (no local warmup)")
        else:
            report("Checking Ollama availability...")
            try:
                models = self._chat_client.list_models()
            except Exception as exc:
                raise RuntimeError(f"Failed to reach Ollama server: {exc}") from exc
            if not cloud_model and effective not in models:
                raise RuntimeError(
                    f"Chat model not found in Ollama: {effective}. "
                    f"Pull it with: ollama pull {effective}",
                )
            if cloud_model:
                report(f"Using Ollama Cloud model: {effective} (no local warmup)")
            else:
                report(f"Warming chat model: {effective}")
                try:
                    # Pass ``num_ctx`` explicitly so the FIRST load fits
                    # the configured context window. Ollama allocates
                    # the kv-cache on first call; if the warmup ping
                    # omits ``num_ctx`` the model loads at its built-in
                    # default (often 256k for big models) and a later
                    # call with the right size triggers an expensive
                    # reload.
                    self._chat_client.chat(
                        [{"role": "user", "content": "Reply with OK."}],
                        model=effective,
                        options={"num_ctx": self._context_window},
                        surface="model_warmup",
                    )
                except Exception as exc:
                    log.warning("chat model warmup failed: %s", exc)

        # Pre-warm the worker model and the embedder even when the
        # chat client is remote. The original warmup path only knew
        # about the chat model, which on a remote chat provider
        # (openai_compatible) skips the whole Ollama branch — and
        # leaves the local worker model + embedder cold. The first
        # turn then pays the cold-load cost on the embed call (and
        # any background worker firing in parallel competes for the
        # same Ollama instance). For a worker like
        # ``qwen3-coder:30b`` the cold load alone is tens of
        # seconds; the embedder is several seconds. Both are easy
        # wins on boot.
        self._prewarm_local_worker_model(report)
        self._prewarm_embedder(report)

        report("Warming TTS models...")
        self.prewarm_tts()
        report("Warmup complete")

    def _prewarm_local_worker_model(self, report: Callable[[str], None]) -> None:
        """Warm the background-worker Ollama model when it's not the
        same client as chat.

        Skip cases:

        * ``_worker_client is _chat_client`` — pure-Ollama mode, the
          chat warmup at the top of :meth:`prewarm_runtime` already
          loaded this model. Touching it again is wasted work.
        * Worker client is not an :class:`OllamaClient` instance —
          ``workers_use_local=False`` keeps workers on the remote
          chat client; nothing local to warm.
        * Effective worker model is empty — config edge case, log
          and skip.
        * Worker model ends in ``:cloud`` / ``-cloud`` — Ollama Cloud
          loads server-side; the warmup ping is wasted.

        Failures here are logged and swallowed (the worker call on
        first real use will surface the actual error to the user).
        """
        if self._worker_client is self._chat_client:
            return
        if not isinstance(self._worker_client, OllamaClient):
            return
        model = (self._effective_worker_model or "").strip()
        if not model:
            return
        if model.endswith("-cloud") or model.endswith(":cloud"):
            report(f"Using Ollama Cloud worker model: {model} (no local warmup)")
            return
        report(f"Warming worker model: {model}")
        # Source ``num_ctx`` from ``ollama.context_window`` — the same
        # field :class:`OllamaClient._default_options` falls back to.
        # Passing it explicitly here is belt-and-braces: the kv-cache
        # MUST be sized correctly on the FIRST call, otherwise Ollama
        # loads the model at its built-in default (often 256k tokens)
        # and a subsequent worker call with a smaller ``num_ctx``
        # triggers a full model reload — exactly the pathology you
        # see in ``ollama ps`` as a CPU/GPU split.
        worker_options: dict[str, object] = {}
        worker_ctx = getattr(self._settings.ollama, "context_window", None)
        if isinstance(worker_ctx, int) and worker_ctx > 0:
            worker_options["num_ctx"] = int(worker_ctx)
        try:
            self._worker_client.chat(
                [{"role": "user", "content": "Reply with OK."}],
                model=model,
                options=worker_options or None,
                surface="model_warmup",
            )
        except Exception as exc:
            log.warning("worker model warmup failed: %s", exc)

    def _prewarm_embedder(self, report: Callable[[str], None]) -> None:
        """Warm the embedding model into the Ollama loaded-models slot.

        Single-character prompt; the cheapest possible ``/embeddings``
        round-trip. Result is discarded — we only care that Ollama
        has the embedder hot when RAG retrieval fires on the first
        real turn.

        Failures are logged and swallowed: a cold embedder is slow
        but not fatal (RAG silently degrades when the embedder
        raises), so a boot-time warmup miss should not block the
        rest of startup.
        """
        embedder = getattr(self, "_embedder", None)
        if embedder is None:
            return
        model = (getattr(embedder, "model", "") or "").strip()
        if not model:
            return
        report(f"Warming embedder: {model}")
        try:
            embedder.embed(".")
        except Exception as exc:
            log.warning("embedder warmup failed: %s", exc)

    # ── Greetings + proactive ────────────────────────────────────────

    def build_startup_greeting(self) -> str:
        return "Welcome back. Audio is ready."

    def generate_proactive_message(self) -> str | None:
        # The new ProactiveDirector speaks directly via TTS. Returning ``None``
        # tells LiveWorker not to also queue something itself.
        self._proactive.notify_silence(self.session_key)
        return None

    def set_live_voice_session_active(self, active: bool) -> None:
        was_active = self._live_voice_session_active
        self._live_voice_session_active = bool(active)
        self._state.session_type = "live" if active else "chat"
        # Voice mode dominates: drop any pending typed timer so a
        # stale typed nudge can't fire while the user is on the mic.
        # When voice mode ends we don't auto-arm — typing is required
        # to get back into "we just had a typed turn" state.
        if active and not was_active:
            self._disarm_typed_silence_timer()

    # ── Typed-mode proactive: presence gate + silence timer ─────────

    def _is_typed_proactive_eligible(self) -> bool:
        """Predicate handed to :class:`ProactiveDirector`.

        Folds *all* gating concerns into one boolean so the director
        never has to know about settings, live mode, or presence.
        Voice mode dominance lives here: when the user is on the mic
        the typed path is forcefully disabled regardless of presence
        signals (which are typed-mode only — see ``set_user_present``).

        The presence gate is conditional on
        ``agent.proactive_typed_when_away``: with it ``False`` (the
        default) hidden / blurred windows silence the timer; with it
        ``True`` the timer fires regardless. The flag exists so users
        who want Aiko to chime in even when they've alt-tabbed away
        can opt in without having to disable the proactive subsystem
        entirely.
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "proactive_typed_enabled", True)):
            return False
        if self._live_voice_session_active:
            return False
        if self._turn_in_progress:
            return False
        if not self._user_present and not bool(
            getattr(agent, "proactive_typed_when_away", False)
        ):
            return False
        # K14: skip the typed nudge when the last turn read as
        # ``"abandoned"`` (steep latency *and* curt message). The
        # absence-curiosity inner-life cue on the *next* user turn
        # handles this case more gracefully than a proactive ping
        # would; firing here would compound the "Aiko is talking past
        # me" signal. Cleared by the next non-abandoned scoring.
        if bool(getattr(agent, "engagement_proactive_gate", True)):
            if getattr(self, "_last_engagement_label", "neutral") == "abandoned":
                return False
        return True

    def _arm_typed_silence_timer(self) -> None:
        """Schedule a one-shot fire after ``proactive_silence_seconds_typed``.

        Cancels any in-flight timer so we don't race two of them past
        the cooldown gate inside ``ProactiveDirector``. Stores both the
        wall-clock (monotonic) arm time and the budget so a presence
        flip can re-arm with the remaining budget instead of starting
        a fresh full window.
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "proactive_typed_enabled", True)):
            return
        budget = float(getattr(agent, "proactive_silence_seconds_typed", 240.0))
        if budget <= 0.0:
            return
        with self._typed_silence_lock:
            if self._typed_silence_timer is not None:
                try:
                    self._typed_silence_timer.cancel()
                except Exception:
                    log.debug("typed timer cancel raised", exc_info=True)
            timer = threading.Timer(budget, self._on_typed_silence_fire)
            timer.name = "typed-silence-timer"
            timer.daemon = True
            self._typed_silence_timer = timer
            self._typed_silence_armed_at = time.monotonic()
            self._typed_silence_armed_budget = budget
            timer.start()

    def _disarm_typed_silence_timer(self) -> None:
        """Cancel + clear the current typed-silence timer (no fire)."""
        with self._typed_silence_lock:
            if self._typed_silence_timer is not None:
                try:
                    self._typed_silence_timer.cancel()
                except Exception:
                    log.debug("typed timer cancel raised", exc_info=True)
            self._typed_silence_timer = None
            self._typed_silence_armed_at = None
            self._typed_silence_armed_budget = None

    def _on_typed_silence_fire(self) -> None:
        """Timer body: hand off to the director if we're still eligible.

        Re-checked under ``_is_typed_proactive_eligible`` rather than
        trusting the moment we armed. The director enforces its own
        cooldown and inflight guards, so this is purely "should we
        even ask?".
        """
        with self._typed_silence_lock:
            self._typed_silence_timer = None
            self._typed_silence_armed_at = None
            self._typed_silence_armed_budget = None
        try:
            self._proactive.notify_typed_silence(self.session_key)
        except Exception:
            log.debug("notify_typed_silence raised", exc_info=True)

    def set_user_present(self, present: bool) -> None:
        """Public: client-side presence change (tab visibility / window focus).

        Three-state semantics:
        - True after False: re-arm with the remaining silence budget
          if a typed turn is still "owed" a fire (i.e. we had armed a
          timer that got cancelled by the False flip).
        - False after True: cancel the pending timer; if it had been
          running a while, remember the elapsed so the next True flip
          re-arms with what's left.
        - Same value as before: no-op (idempotent — a debounced UI
          may legitimately resend the same value).

        Voice mode does NOT call this path. The voice-mode
        ``LiveSession._maybe_proactive`` continues to fire on its own
        45 s threshold; users wearing the mic may legitimately be
        away from the screen but still present in conversation.
        """
        new_value = bool(present)
        with self._typed_silence_lock:
            if self._user_present == new_value:
                return
            self._user_present = new_value
            armed_at = self._typed_silence_armed_at
            armed_budget = self._typed_silence_armed_budget
            timer = self._typed_silence_timer
        if not new_value:
            if timer is not None:
                # Snapshot how much budget had elapsed so the next
                # True flip re-arms with the remainder rather than
                # giving the user a fresh 4-min grace every alt-tab.
                if armed_at is not None and armed_budget is not None:
                    elapsed = time.monotonic() - armed_at
                    remaining = max(0.0, armed_budget - elapsed)
                else:
                    remaining = 0.0
                with self._typed_silence_lock:
                    if self._typed_silence_timer is not None:
                        try:
                            self._typed_silence_timer.cancel()
                        except Exception:
                            log.debug("typed timer cancel raised", exc_info=True)
                    self._typed_silence_timer = None
                    self._typed_silence_armed_at = None
                    # Stash the remaining budget under the same field
                    # so a subsequent True flip can re-arm with it.
                    self._typed_silence_armed_budget = remaining
            return
        # Flipped to present. If a timer is already running, leave it
        # alone (it was armed before we ever went away). If we have a
        # leftover ``_typed_silence_armed_budget`` from the away leg,
        # re-arm with that budget so the user gets the same total
        # quiet window they would have had if they hadn't alt-tabbed.
        with self._typed_silence_lock:
            if self._typed_silence_timer is not None:
                return
            remaining = self._typed_silence_armed_budget
            self._typed_silence_armed_budget = None
        if remaining is None or remaining <= 0.0:
            return
        agent = self._settings.agent
        if not bool(getattr(agent, "proactive_typed_enabled", True)):
            return
        with self._typed_silence_lock:
            timer = threading.Timer(
                float(remaining), self._on_typed_silence_fire,
            )
            timer.name = "typed-silence-timer"
            timer.daemon = True
            self._typed_silence_timer = timer
            self._typed_silence_armed_at = time.monotonic()
            self._typed_silence_armed_budget = float(remaining)
            timer.start()

    def set_user_active_app(self, app: str | None) -> None:
        """Public: update the foreground app the user is in.

        Server-side privacy gate: when ``activity_awareness_enabled``
        is ``False`` the value is silently dropped. This means a
        buggy or rogue client emitting ``user_activity`` events while
        the user has disabled the feature in settings cannot leak
        which apps the user is in.

        Empty string / blank coerces to ``None`` (no block in
        prompt) so a client that wants to clear the cached value
        without disabling the feature can send ``""``.
        """
        if not bool(getattr(self._settings.agent, "activity_awareness_enabled", False)):
            self._user_active_app = None
            return
        if app is None:
            self._user_active_app = None
            return
        cleaned = str(app).strip()
        self._user_active_app = cleaned or None

    # ── Listeners ────────────────────────────────────────────────────

    # ── Scheduler ───────────────────────────────────────────────────

    @property
    def scheduler(self) -> SpeakingWindowScheduler:
        return self._scheduler

    def notify_user_speech_started(self) -> None:
        """Called by LiveSession when fresh user audio lands mid-window.

        Background workers cooperatively cancel so the LLM channel is free
        for the actual reply.
        """
        try:
            self._scheduler.on_user_speech()
        except Exception:
            log.debug("scheduler.on_user_speech failed", exc_info=True)

    # ── Avatar, desktop, circadian, overlay/outfit/motion emits ─────
    # Methods now live in app/core/session/avatar_mixin.py.

    # ── RAG / documents ─────────────────────────────────────────────

    @property
    def rag_store(self):
        return getattr(self, "_rag_store", None)

    @property
    def document_ingestor(self):
        return getattr(self, "_document_ingestor", None)

    # ── Tools ───────────────────────────────────────────────────────

    @property
    def tool_registry(self):
        return getattr(self, "_tool_registry", None)

    def available_tool_names(self) -> list[str]:
        registry = getattr(self, "_tool_registry", None)
        if registry is None:
            return []
        try:
            return registry.names()
        except Exception:
            return []

    def rebuild_tool_registry(self) -> None:
        """Rebuild the tool registry after settings change.

        Reads the current ``settings.tools`` block, constructs a fresh
        registry, and hands it to the active :class:`TurnRunner`.
        """
        try:
            from app.llm.tools import build_default_registry, ToolRegistry
        except Exception:
            log.warning("tool registry import failed", exc_info=True)
            self._tool_registry = None
            if hasattr(self, "_turn_runner"):
                self._turn_runner.set_tool_registry(None)
            return

        tools_cfg = getattr(self._settings, "tools", None)
        if tools_cfg is None or not getattr(tools_cfg, "enabled", True):
            self._tool_registry = ToolRegistry()
            self._turn_runner.set_tool_registry(self._tool_registry)
            return

        registry = ToolRegistry()
        try:
            from app.llm.tools.builtins import GetTimeTool, RecallTool, WebSearchTool
            if getattr(tools_cfg, "get_time", True):
                registry.register(GetTimeTool())
            if getattr(tools_cfg, "recall", True) and getattr(self, "_rag_retriever", None) is not None:
                registry.register(RecallTool(self._rag_retriever))
            if getattr(tools_cfg, "web_search", True):
                try:
                    registry.register(WebSearchTool())
                except Exception:
                    log.info("web_search tool unavailable (duckduckgo-search missing?)")
            if (
                getattr(tools_cfg, "world", True)
                and getattr(self, "_world_store", None) is not None
            ):
                try:
                    from app.llm.tools.world import build_world_tools

                    for tool in build_world_tools(self):
                        registry.register(tool)
                except Exception:
                    log.warning("world tools failed to register", exc_info=True)
            # K1: goal tools (list_goals / add_goal / update_goal_progress
            # / archive_goal). Gated on ``tools.goals`` (default True)
            # and skipped silently when the goal store didn't wire
            # (no embedder / memory disabled).
            if (
                getattr(tools_cfg, "goals", True)
                and getattr(self, "_goal_store", None) is not None
            ):
                try:
                    from app.llm.tools.goals import build_goal_tools

                    for tool in build_goal_tools(self):
                        registry.register(tool)
                except Exception:
                    log.warning("goal tools failed to register", exc_info=True)
            # Chunk 10: filesystem task tools — ``start_file_search``
            # and ``cancel_file_task``. Gated on ``tools.file_tasks``
            # (default True) and skipped silently when the task
            # subsystem itself is off (``agent.tasks_enabled=False``
            # leaves ``_task_orchestrator`` as ``None``).
            if (
                getattr(tools_cfg, "file_tasks", True)
                and getattr(self, "_task_orchestrator", None) is not None
            ):
                try:
                    from app.llm.tools.file_tasks import build_file_task_tools

                    for tool in build_file_task_tools(self):
                        registry.register(tool)
                except Exception:
                    log.warning(
                        "file task tools failed to register", exc_info=True
                    )
        except Exception:
            log.warning("tool registry build failed", exc_info=True)
        self._tool_registry = registry
        if hasattr(self, "_turn_runner"):
            self._turn_runner.set_tool_registry(registry)
        log.info("tool registry rebuilt: %s", registry.names())

    # ── Memory accessors ────────────────────────────────────────────
    # Methods now live in app/core/session/memory_facade_mixin.py.

    # ── World, shared moments, axes, get_together_summary ──────────
    # Methods now live in app/core/session/world_mixin.py.

    def add_message_listener(
        self, callback: Callable[..., None],
    ) -> None:
        if callback and callback not in self._message_listeners:
            self._message_listeners.append(callback)

    def _notify_message(
        self, speaker: str, text: str, message_id: int | None = None,
    ) -> None:
        """Fan a chat line out to listeners.

        ``message_id`` is the persisted SQLite ``messages.id`` when the
        caller has it (proactive turns pass it so the client can enable
        reactions on the new bubble); it stays ``None`` for callers that
        don't (the streamed-turn path carries the id on ``turn_done``
        instead). Listeners may accept two or three positional args; the
        two-arg ones are called without the id for back-compat.
        """
        for listener in list(self._message_listeners):
            try:
                try:
                    listener(speaker, text, message_id)
                except TypeError:
                    # Legacy two-arg listener — call without the id.
                    listener(speaker, text)
            except Exception:
                log.debug("message listener raised", exc_info=True)

    def add_tool_event_listener(
        self, callback: Callable[[str, dict[str, Any]], None],
    ) -> None:
        listeners = getattr(self, "_tool_event_listeners", None)
        if listeners is None:
            listeners = []
            self._tool_event_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def _notify_tool_event(self, event: str, payload: dict[str, Any]) -> None:
        listeners = getattr(self, "_tool_event_listeners", None) or []
        for listener in list(listeners):
            try:
                listener(event, payload)
            except Exception:
                log.debug("tool event listener raised", exc_info=True)

    def add_tts_state_listener(self, callback: Callable[..., None]) -> None:
        if callback and callback not in self._tts_state_listeners:
            self._tts_state_listeners.append(callback)

    def add_metrics_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        """Subscribe to retroactive metrics updates (e.g. tts_ms back-fill)."""
        if callback and callback not in self._metrics_listeners:
            self._metrics_listeners.append(callback)

    def _notify_metrics_updated(self) -> None:
        snapshot = dict(self._last_metrics)
        for listener in list(self._metrics_listeners):
            try:
                listener(snapshot)
            except Exception:
                log.debug("metrics listener raised", exc_info=True)

    def _on_tts_state(self, event: str, payload: dict[str, Any]) -> None:
        # Carry the last assistant reaction over to the next turn so the
        # mood doesn't reset to "neutral" every time. Phase E mood-carryover.
        if event == "start":
            reaction = (payload or {}).get("reaction")
            try:
                self._prompt_assembler.set_last_reaction(reaction)
            except Exception:
                log.debug("set_last_reaction failed", exc_info=True)
            # First "start" after the LLM finished marks audible-from time;
            # subsequent chunk starts in the same turn don't reset it.
            if (
                self._tts_turn_start_at is not None
                and self._tts_turn_first_start_at is None
            ):
                self._tts_turn_first_start_at = time.monotonic()
            # Open the speaking window so background workers can drain.
            try:
                self._scheduler.on_tts_state("start")
            except Exception:
                log.debug("scheduler.on_tts_state(start) failed", exc_info=True)
        elif event == "end":
            # Queue is drained for this turn. Compute total tts_ms (LLM done
            # → audio fully played) and back-fill the last metrics record.
            if self._tts_turn_start_at is not None:
                tts_ms = round(
                    (time.monotonic() - self._tts_turn_start_at) * 1000.0, 1,
                )
                # ``total_ms`` was capture+stt+llm at the time of the LLM
                # turn; add the freshly-measured TTS span on top.
                base_total = float(self._last_metrics.get("total_ms", 0.0) or 0.0)
                base_total -= float(self._last_metrics.get("tts_ms", 0.0) or 0.0)
                self._last_metrics["tts_ms"] = tts_ms
                self._last_metrics["total_ms"] = round(base_total + tts_ms, 1)
                # Mirror into the history tail so averages reflect tts_ms too.
                if self._metrics_history:
                    self._metrics_history[-1]["tts_ms"] = tts_ms
                    self._metrics_history[-1]["total_ms"] = self._last_metrics["total_ms"]
                self._tts_turn_start_at = None
                self._tts_turn_first_start_at = None
                # Re-broadcast metrics so the badge picks up the final tts_ms.
                self._notify_metrics_updated()
            # Close the scheduler window cooperatively.
            try:
                self._scheduler.on_tts_state("end")
            except Exception:
                log.debug("scheduler.on_tts_state(end) failed", exc_info=True)
        for listener in list(self._tts_state_listeners):
            try:
                listener(event, **payload)
            except Exception:
                log.debug("tts state listener raised", exc_info=True)

    def add_tts_amplitude_listener(self, callback: Callable[[float], None]) -> None:
        if callback and callback not in self._tts_amplitude_listeners:
            self._tts_amplitude_listeners.append(callback)

    def _on_tts_amplitude(self, level: float) -> None:
        for listener in list(self._tts_amplitude_listeners):
            try:
                listener(float(level))
            except Exception:
                log.debug("tts amplitude listener raised", exc_info=True)

    # ── Models listing ───────────────────────────────────────────────

    def list_chat_models(
        self,
        *,
        refresh: bool = False,
        provider: str | None = None,
    ) -> list[str]:
        """Return the model identifiers visible to the active chat client.

        ``provider`` (optional) lets the UI preview a non-active
        provider's model list without committing to it — used by the
        ChatProviderSection drawer to populate the model dropdown the
        instant a user picks a different preset. When None, returns the
        cached / fresh list from ``self._chat_client``.

        Best-effort: the underlying ``list_models`` returns ``[]`` on
        failure, and we always prepend the currently configured model
        so the dropdown shows a working selection even when the
        provider's listing endpoint is down.
        """
        # Provider preview: build a throwaway client with the requested
        # provider, no api_key (the listing endpoint is usually
        # open). This is intentionally lossy — auth-gated providers
        # will just return [] and the UI falls back to a free-text
        # input. The throwaway never touches the real client state.
        if provider:
            target = provider.strip().lower()
            if target and target != (self._chat_provider or "ollama"):
                try:
                    from app.core.infra.settings import ChatLlmSettings

                    probe = _build_chat_client(
                        chat_llm=ChatLlmSettings(provider=target),
                        ollama_settings=self._settings.ollama,
                        role="probe",
                    )
                    return probe.list_models()
                except Exception:
                    log.debug(
                        "list_chat_models provider preview failed: %s",
                        target, exc_info=True,
                    )
                    return []
        now = time.monotonic()
        if not refresh and self._models_cache is not None and (now - self._models_cache_time) < self._cache_ttl:
            return list(self._models_cache)
        try:
            models = self._chat_client.list_models()
        except Exception:
            models = []
        current = self.chat_model
        if current and current not in models:
            models.insert(0, current)
        self._models_cache = list(models)
        self._models_cache_time = now
        return models

    # ── Decision trace + emergency stop (legacy stubs) ──────────────

    def get_decision_trace(self, max_entries: int = 300) -> list[dict[str, str]]:
        items = list(self._decision_trace)
        if max_entries >= len(items):
            return items
        return items[-max_entries:]

    def clear_decision_trace(self) -> None:
        self._decision_trace.clear()

    # ── Metrics ─────────────────────────────────────────────────────

    @staticmethod
    def _zero_metrics() -> dict[str, Any]:
        return {
            "mode": "idle",
            "capture_ms": 0.0,
            "stt_ms": 0.0,
            "llm_ms": 0.0,
            "tts_ms": 0.0,
            "total_ms": 0.0,
            # Token totals (combined streaming + tool-pass).
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            # Ollama timing breakdown (full-precision).
            "total_duration_ms": 0.0,
            "eval_duration_ms": 0.0,
            "prompt_eval_duration_ms": 0.0,
            "tokens_per_second": 0.0,
            # Context fill.
            "context_window": 0,
            "context_source": "fallback",
            "prompt_pct": 0.0,
            # Prompt-assembly telemetry.
            "system_tokens": 0,
            "summary_tokens": 0,
            "rag_tokens": 0,
            "history_tokens": 0,
            "user_tokens": 0,
            "tool_tokens": 0,
            "history_messages_kept": 0,
            "history_dropped_count": 0,
            "summary_active": False,
            "summary_messages": 0,
            # Compaction state.
            "compaction_triggered": False,
            "compactions_total": 0,
            # Phase 1c: time-to-first-stream-delta + filler injection.
            "first_token_ms": 0.0,
            "filler_emitted": False,
            # P1 (perf backlog): per-turn embed budget. Surfaced via
            # ``get_last_response_detail`` so MCP can grep regressions
            # over time. Zero on the idle frame.
            "embed_calls": 0,
            "embed_ms": 0.0,
            # P2 (perf backlog): prompt-build phase telemetry. Per-
            # provider wall time so a regression in a single provider
            # can be attributed without instrumenting it by hand.
            "provider_ms": {},
            "rag_lookup_ms": 0.0,
            "assemble_ms": 0.0,
        }

    def get_last_metrics(self) -> dict[str, Any]:
        return dict(self._last_metrics)

    def get_average_metrics(self) -> dict[str, float | str | int]:
        if not self._metrics_history:
            return {
                "window": 0,
                "capture_ms": 0.0, "stt_ms": 0.0, "llm_ms": 0.0,
                "tts_ms": 0.0, "total_ms": 0.0,
                "prompt_tokens": 0.0, "completion_tokens": 0.0,
                "tokens_per_second": 0.0, "prompt_pct": 0.0,
            }

        def avg(key: str) -> float:
            values = [float(item.get(key, 0.0) or 0.0) for item in self._metrics_history]
            return round(sum(values) / max(1, len(values)), 1)

        return {
            "window": len(self._metrics_history),
            "capture_ms": avg("capture_ms"),
            "stt_ms": avg("stt_ms"),
            "llm_ms": avg("llm_ms"),
            "tts_ms": avg("tts_ms"),
            "total_ms": avg("total_ms"),
            "prompt_tokens": avg("prompt_tokens"),
            "completion_tokens": avg("completion_tokens"),
            "tokens_per_second": avg("tokens_per_second"),
            "prompt_pct": round(avg("prompt_pct"), 4),
        }

    def reset_latency_metrics(self) -> None:
        self._last_metrics = self._zero_metrics()
        self._metrics_history.clear()

    def get_conversation_memory(self, max_entries: int = 200) -> list[dict[str, str]]:
        rows = self._chat_db.get_messages(self.session_key, limit=max_entries)
        return [
            {"role": r.role, "content": r.content, "created_at": r.created_at}
            for r in rows
        ]

    # ── The chat loop ────────────────────────────────────────────────

    def chat_once(self, user_text: str) -> str:
        return self.chat_once_streaming(user_text=user_text, mode="typed")

    def chat_once_streaming(
        self,
        *,
        user_text: str,
        on_token: Callable[[str], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        mode: str = "typed",
        capture_ms: float = 0.0,
        stt_ms: float = 0.0,
        user_vocal_tone: str | None = None,
        _resume_message_id: int | None = None,
    ) -> str:
        _ = user_vocal_tone  # not used in v1; reserved for prosody hints
        cleaned = sanitize_user_text(user_text or "")
        if not cleaned:
            return ""
        # K14: stash the turn's mode so ``_post_turn_inner_life`` can
        # route the engagement signal correctly (voice: latency feeds
        # closeness drift; typed: latency feeds absence-curiosity).
        # ``mode`` defaults to ``"typed"`` upstream so we never see an
        # empty string here, but normalise defensively.
        self._last_turn_mode = (mode or "typed").strip().lower() or "typed"
        # Stash the live turn's user text so a file task spawned mid-turn
        # (``start_file_read`` / ``start_file_search``) can record it as
        # the ``origin_prompt`` on the task metadata — used by the
        # reply-on-complete turn to remind Aiko what the user asked for.
        # Best-effort and opportunistic; only read during the same turn.
        self._active_turn_user_text = cleaned
        # Schema v8: refresh the activity timestamp so the idle worker
        # scheduler defers background sweeps while the user is actively
        # chatting (typed turns also count; voice paths touch the gate
        # through the Live-mode short-circuit in :meth:`_is_user_idle`).
        self._touch_user_activity()

        if on_generation_status:
            on_generation_status("AI is generating response...")

        # If chat history is disabled, replay the message into a transient key
        # so we never persist it across restarts.
        session_key = self.session_key if self._remember_history else f"{self.session_key}:noremember"

        # ── Voice merge bookkeeping ────────────────────────────────────
        # For live-mode turns we install a ``_MergeBuffer`` so that:
        #   1. ``feed_stt_partial`` can detect a continuation (phrase B
        #      starting before TTS began) and abort this turn early.
        #   2. ``process_live_capture`` can merge phrase B's text into
        #      the existing user row and call back into us with
        #      ``_resume_message_id`` set.
        # The buffer key is ``self.session_key`` (the user-facing one),
        # not the ``:noremember`` variant, because the capture-side
        # callers don't know about the noremember mode.
        merge_key = self.session_key
        user_message_id: int
        if _resume_message_id is not None:
            user_message_id = int(_resume_message_id)
            log.info(
                "voice merge: resuming turn user_msg_id=%d merged_chars=%d",
                user_message_id, len(cleaned),
            )
        else:
            user_message_id = self._chat_db.add_message(
                session_id=session_key,
                role="user",
                content=cleaned,
                token_count=estimate_tokens(cleaned),
            )

        if mode == "live":
            with self._merge_lock:
                self._merge_buffer[merge_key] = _MergeBuffer(
                    session_key=merge_key,
                    turn_runner=self._turn_runner,
                    user_text=cleaned,
                    user_message_id=user_message_id,
                    tts_started=False,
                    awaiting_phrase_b=False,
                )
        else:
            # Typed turn: drop any stale buffer that might have been left
            # by a prior live phrase that hasn't completed cleanly. Also
            # clear the vocal-tone snapshot — paralinguistics from the
            # previous voice phrase don't apply to a typed message.
            self._clear_merge_buffer(merge_key)
            with self._vocal_tone_lock:
                self._last_vocal_tone = None
            # The user is typing, so cancel any pending typed-silence
            # timer (we no longer need to nudge them — they're back).
            # Re-armed at the end of the turn if ``mode == "typed"``.
            self._disarm_typed_silence_timer()

        self._turn_in_progress = True
        # F1.6 — abort any in-flight background fact-check distil call.
        # The IdleFactChecker passes this event into ``chat_stream`` so
        # the worker yields the model back to the user immediately and
        # the queued claim goes back to the head of the queue (see
        # :class:`IdleFactChecker`).
        fact_check_cancel = getattr(self, "_fact_check_cancel", None)
        if fact_check_cancel is not None:
            try:
                fact_check_cancel.set()
            except Exception:
                pass
        t0 = time.perf_counter()
        try:
            tts_chunk_cb = None
            on_earcon_cb = None
            if bool(self._settings.tts.enabled):
                prosody = getattr(self, "_prosody", None)
                tts_chunk_cb = (
                    prosody.dispatch if prosody is not None else self._tts.enqueue
                )
                # Phase 1c: route stage-direction earcons (``[[laugh]]``,
                # ``[[sigh]]`` etc.) into the same TTS queue so they
                # play *between* spoken chunks at the right moment.
                tts_queue = getattr(self, "_tts", None)
                if tts_queue is not None and hasattr(tts_queue, "enqueue_earcon"):
                    on_earcon_cb = tts_queue.enqueue_earcon

            wrapped_tts_cb = self._wrap_tts_chunk_for_merge(
                tts_chunk_cb, merge_key,
            ) if mode == "live" and tts_chunk_cb is not None else tts_chunk_cb

            # Clear the K31 per-turn gesture accumulator before the
            # streamed reply lands so a previous turn's gesture can
            # never leak onto this turn's bubble.
            self._current_turn_gestures.clear()
            result = self._turn_runner.run(
                session_key,
                cleaned,
                on_token=on_token,
                on_tts_chunk=wrapped_tts_cb,
                on_earcon=on_earcon_cb,
                on_overlay=self._emit_avatar_overlay,
                on_outfit=self._emit_avatar_outfit,
                on_motion=self._emit_avatar_motion,
                on_touch=self._emit_avatar_touch,
                stop_requested=stop_requested,
                resume_user_message_id=user_message_id,
            )
        finally:
            self._turn_in_progress = False
            # F1.6 — release the fact-check cancel signal so the next
            # idle-scheduler tick can resume distilling claims.
            if fact_check_cancel is not None:
                try:
                    fact_check_cancel.clear()
                except Exception:
                    pass
            # The merge window is meaningful only while this turn is the
            # in-flight one. When the turn returns we drop the buffer so a
            # late partial can't fire ``request_stop()`` on a runner that's
            # already moved on. The TTS-start hook usually clears it
            # earlier; this is the belt-and-braces case for short or
            # tool-only turns that produced no TTS.
            self._clear_merge_buffer(merge_key)

        llm_ms = (time.perf_counter() - t0) * 1000.0
        total_ms = capture_ms + stt_ms + llm_ms
        # Mark the TTS-timing window now; ``_on_tts_state("end", ...)`` will
        # close it and back-fill ``tts_ms`` / ``total_ms`` on the last metric.
        self._tts_turn_start_at = time.monotonic()
        self._tts_turn_first_start_at = None

        self._compactions_total += int(getattr(result, "compactions_run", 0) or 0)
        usage = result.usage
        telemetry = result.telemetry

        # Post-turn inner-life (cheap, no LLM on the hot path): updates
        # affect state, broadcasts mood_state WS, and submits the
        # ReflectionWorker job to the speaking window scheduler.
        try:
            self._post_turn_inner_life(
                user_text=cleaned,
                reaction=getattr(result, "reaction", "neutral") or "neutral",
                assistant_text=getattr(result, "text", "") or "",
                raw_assistant_text=getattr(result, "raw_text", "") or "",
                user_message_id=user_message_id,
                assistant_message_id=getattr(result, "assistant_message_id", None),
            )
        except Exception:
            log.debug("post-turn inner life failed", exc_info=True)

        prompt_pct = 0.0
        if self._context_window > 0 and usage.prompt_tokens > 0:
            prompt_pct = round(usage.prompt_tokens / float(self._context_window), 4)

        metrics: dict[str, Any] = {
            "mode": mode,
            "capture_ms": round(capture_ms, 1),
            "stt_ms": round(stt_ms, 1),
            "llm_ms": round(llm_ms, 1),
            "tts_ms": 0.0,
            "total_ms": round(total_ms, 1),
            "prompt_tokens": int(usage.prompt_tokens),
            "completion_tokens": int(usage.completion_tokens),
            "total_tokens": int(usage.total_tokens),
            "total_duration_ms": round(usage.total_duration_ms, 1),
            "eval_duration_ms": round(usage.eval_duration_ms, 1),
            "prompt_eval_duration_ms": round(usage.prompt_eval_duration_ms, 1),
            "tokens_per_second": float(usage.tokens_per_second),
            "context_window": int(self._context_window),
            "context_source": str(self._context_source),
            "prompt_pct": prompt_pct,
            "compactions_total": int(self._compactions_total),
            "first_token_ms": round(float(getattr(result, "first_token_ms", None) or 0.0), 1),
            "filler_emitted": bool(getattr(result, "filler_emitted", False)),
            # K32: the SQLite ``messages.id`` of the assistant row just
            # persisted, so the frontend can stamp the live bubble's
            # ``backendId`` and enable the reaction tray without waiting
            # for a history reload. ``None`` for empty / aborted turns
            # (no row was written).
            "assistant_message_id": (
                int(result.assistant_message_id)
                if getattr(result, "assistant_message_id", None) is not None
                else None
            ),
        }
        if telemetry is not None:
            tdict = telemetry.as_dict()
            metrics.update({
                "system_tokens": tdict["system_tokens"],
                "summary_tokens": tdict["summary_tokens"],
                "rag_tokens": tdict["rag_tokens"],
                "history_tokens": tdict["history_tokens"],
                "user_tokens": tdict["user_tokens"],
                "tool_tokens": tdict["tool_tokens"],
                "history_messages_kept": tdict["history_messages_kept"],
                "history_dropped_count": tdict["history_messages_dropped"],
                "summary_active": tdict["summary_active"],
                "summary_messages": tdict["summary_messages"],
                "compaction_triggered": tdict["compaction_triggered"],
                # P1: per-turn embed budget.
                "embed_calls": tdict["embed_calls"],
                "embed_ms": tdict["embed_ms"],
                # P2: prompt-build phase telemetry.
                "provider_ms": tdict["provider_ms"],
                "rag_lookup_ms": tdict["rag_lookup_ms"],
                "assemble_ms": tdict["assemble_ms"],
            })
        self._set_last_metrics(metrics)

        # Arm the typed-silence timer so a long quiet period after this
        # turn can fire a typed proactive nudge. Only after typed turns —
        # voice turns are handled by ``LiveSession._maybe_proactive`` on
        # its own timing loop.
        if mode == "typed":
            try:
                self._arm_typed_silence_timer()
            except Exception:
                log.debug("typed silence arm failed", exc_info=True)

        return result.text

    def _set_last_metrics(
        self, metrics: dict[str, Any],
    ) -> None:
        self._last_metrics = dict(metrics)
        self._metrics_history.append(dict(metrics))

    # ── Inner-life block providers (Phase 2b, 2e, 3a, ...) ──────────

    # Moved to app/core/session/inner_life_providers_mixin.py.
    # ── Phase 2a + 2b: bootstrap-time inner-life ────────────────────────

    def _maybe_schedule_dream_pass(self) -> None:
        """Bootstrap-time check: when the gap since the last assistant
        message exceeds ``dream_worker_min_hours_since_last`` and we
        have an LLM + embedder + memory store, schedule a one-shot
        :class:`DreamWorker.maybe_run` job on the listening-window
        executor. Runs *before* the resume opener so the resume weaver
        can pick up the freshly-written dream memory as a candidate.
        """
        worker = getattr(self, "_dream_worker", None)
        memory = getattr(self, "_memory_store", None)
        executor = getattr(self, "_listening_window_executor", None)
        if worker is None or memory is None:
            return
        threshold = float(
            getattr(
                self._settings.agent,
                "dream_worker_min_hours_since_last",
                6.0,
            ),
        )
        if threshold <= 0.0:
            return
        gap_h = self._last_assistant_age_hours()
        if gap_h is None or gap_h < threshold:
            return

        def _job() -> None:
            try:
                rolling = ""
                try:
                    row = self._chat_db.get_latest_summary(self.session_key)
                    rolling = (row.summary if row is not None else "") or ""
                except Exception:
                    rolling = ""
                callbacks = self._top_inner_life_contents("callback", limit=3)
                self_memories = self._top_inner_life_contents("self", limit=3)
                affect = None
                try:
                    affect = self._affect_store.get(self._user_id)
                except Exception:
                    affect = None
                worker.maybe_run(
                    user_id=self._user_id,
                    session_key=self.session_key,
                    hours_since_last=gap_h,
                    rolling_summary=rolling,
                    recent_callbacks=callbacks,
                    recent_self_memories=self_memories,
                    affect=affect,
                )
            except Exception:
                log.debug("dream worker job failed", exc_info=True)

        try:
            if executor is not None:
                executor.submit(_job)
            else:
                _job()
        except Exception:
            log.debug("dream worker submit failed", exc_info=True)

    def _top_inner_life_contents(
        self, kind: str, *, limit: int = 3,
    ) -> list[str]:
        """Return up to ``limit`` content strings of the top-salience
        memories of the requested kind. Used by the dream pass to seed
        the prompt with recent threads / self-thoughts.
        """
        store = getattr(self, "_memory_store", None)
        if store is None:
            return []
        try:
            top = store.list_top(limit=max(limit * 4, 12))
        except Exception:
            return []
        out: list[str] = []
        for mem in top:
            if (mem.kind or "").lower() != kind:
                continue
            content = (mem.content or "").strip()
            if not content:
                continue
            out.append(content)
            if len(out) >= limit:
                break
        return out

    def _compute_user_reply_latency_seconds(
        self, *, user_message_id: int | None,
    ) -> float | None:
        """K14: seconds between the prior assistant reply and this user
        message, or ``None`` when the gap can't be measured.

        Reasons we return ``None``: no ``user_message_id`` (live merge
        path that resumed an existing row), no prior assistant message
        in the session, or unparseable timestamps. The caller treats
        ``None`` as "no signal this turn" so a cold-start session
        doesn't fire a phantom engagement delta.
        """
        if user_message_id is None:
            return None
        try:
            rows = self._chat_db.get_messages(self.session_key)
        except Exception:
            return None
        if not rows:
            return None
        from datetime import datetime, timezone

        prev_assistant_at: str | None = None
        user_created_at: str | None = None
        for row in rows:
            if int(getattr(row, "id", -1)) == int(user_message_id):
                user_created_at = getattr(row, "created_at", None)
                break
            if (row.role or "").lower() == "assistant":
                prev_assistant_at = getattr(row, "created_at", None)
        if not user_created_at or not prev_assistant_at:
            return None
        try:
            u_ts = datetime.fromisoformat(
                str(user_created_at).replace("Z", "+00:00"),
            )
            a_ts = datetime.fromisoformat(
                str(prev_assistant_at).replace("Z", "+00:00"),
            )
        except Exception:
            return None
        if u_ts.tzinfo is None:
            u_ts = u_ts.replace(tzinfo=timezone.utc)
        if a_ts.tzinfo is None:
            a_ts = a_ts.replace(tzinfo=timezone.utc)
        return max(0.0, (u_ts - a_ts).total_seconds())

    def _last_assistant_age_hours(self) -> float | None:
        """Return how many hours ago the last assistant message was
        written, or ``None`` when there's no history at all (so the
        caller can skip the resume opener for fresh installs)."""
        try:
            messages = self._chat_db.get_messages(self.session_key)
        except Exception:
            return None
        last_assistant_at: str | None = None
        for row in reversed(messages):
            if (row.role or "").lower() == "assistant":
                last_assistant_at = getattr(row, "created_at", None)
                break
        if not last_assistant_at:
            return None
        try:
            from datetime import datetime, timezone

            ts = datetime.fromisoformat(
                str(last_assistant_at).replace("Z", "+00:00"),
            )
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            return max(0.0, (now - ts).total_seconds() / 3600.0)
        except Exception:
            return None

    def _maybe_schedule_resume_opener(self) -> None:
        """Bootstrap-time check: when the gap since the last assistant
        message exceeds ``resume_opener_min_hours`` and we have a
        weaver + nudge store, schedule a one-shot resume-opener job
        on the listening-window executor.
        """
        weaver = getattr(self, "_narrative_weaver", None)
        store = getattr(self, "_prepared_nudge_store", None)
        executor = getattr(self, "_listening_window_executor", None)
        if weaver is None or store is None:
            return
        threshold = float(
            getattr(self._settings.agent, "resume_opener_min_hours", 4.0),
        )
        if threshold <= 0.0:
            return
        gap_h = self._last_assistant_age_hours()
        if gap_h is None or gap_h < threshold:
            return
        # Don't replace a fresh prepared nudge that's already there
        # (e.g. one the speaking-window weaver primed yesterday).
        existing = store.get_fresh(self._user_id)
        if existing is not None and existing.source_kind == "resume":
            return

        ttl = float(
            getattr(self._settings.agent, "resume_opener_ttl_seconds", 1800.0),
        )

        def _job() -> None:
            try:
                rolling = ""
                try:
                    row = self._chat_db.get_latest_summary(self.session_key)
                    rolling = (row.summary if row is not None else "") or ""
                except Exception:
                    rolling = ""
                weaver.prepare_resume_opener(
                    self._user_id,
                    rolling_summary=rolling,
                    hours_since_last=gap_h,
                    ttl_seconds=ttl,
                )
            except Exception:
                log.debug("resume opener job failed", exc_info=True)

        try:
            if executor is not None:
                executor.submit(_job)
            else:
                # Fallback: run inline. Only happens when the listening
                # executor failed to spin up (very rare).
                _job()
        except Exception:
            log.debug("resume opener submit failed", exc_info=True)

    # Moved to app/core/session/inner_life_providers_mixin.py.
    # Moved to app/core/session/post_turn_mixin.py.
    # Moved to app/core/session/inner_life_providers_mixin.py.
    # Moved to app/core/session/speaking_window_jobs_mixin.py.

    def _lookup_prefetched_rag_block(self, user_text: str) -> str | None:
        """Phase 1b: PromptAssembler hook into the speculative pre-fetcher.

        Returns ``None`` on a miss so the assembler falls through to the
        live retriever. Allows up to ~250ms of waiting on an in-flight
        fetch to soak up the embedding latency that the partial just paid.
        """
        prefetcher = getattr(self, "_rag_prefetcher", None)
        if prefetcher is None:
            return None
        try:
            return prefetcher.lookup(user_text, wait_pending_seconds=0.25)
        except Exception:
            log.debug("rag prefetch lookup raised", exc_info=True)
            return None

    def _recent_turn_texts(self, *, limit: int = 3) -> list[str]:
        """Return the last ``limit`` non-empty message texts for query expansion.

        Mirrors :meth:`PromptAssembler.assemble_with_budget`'s slicing so
        prefetched RAG queries hit the same cache key as the live one.
        """
        try:
            rows = self._chat_db.get_messages(self.session_key, limit=limit)
        except Exception:
            return []
        out: list[str] = []
        for row in rows[-limit:]:
            text = (getattr(row, "content", "") or "").strip()
            if text:
                out.append(text)
        return out

    def _submit_prompt_prebuild(self) -> None:
        """Schedule a static-slice prompt prebuild on the listening executor.

        Coalesces concurrent requests via ``_prebuild_in_flight`` so a
        burst of partials doesn't queue redundant work. Safe to call from
        the capture loop thread; runs entirely off-thread.
        """
        executor = getattr(self, "_listening_window_executor", None)
        assembler = getattr(self, "_prompt_assembler", None)
        if executor is None or assembler is None:
            return
        if self._prebuild_in_flight:
            return
        self._prebuild_in_flight = True

        def _run() -> None:
            try:
                assembler.prebuild_static_slices(self.session_key)
            except Exception:
                log.debug("prompt prebuild raised", exc_info=True)
            finally:
                self._prebuild_in_flight = False

        try:
            executor.submit(_run)
        except RuntimeError:
            # Executor shut down — drop silently.
            self._prebuild_in_flight = False

    # ── Mood listeners (WS broadcast) ───────────────────────────────

    def add_mood_state_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        if callback and callback not in self._mood_listeners:
            self._mood_listeners.append(callback)

    # ── STT partials + backchannel (Phase 1a) ───────────────────────

    def add_stt_partial_listener(self, callback: Callable[[str], None]) -> None:
        if callback and callback not in self._stt_partial_listeners:
            self._stt_partial_listeners.append(callback)

    def add_backchannel_listener(
        self, callback: Callable[[BackchannelHint, str], None],
    ) -> None:
        if callback and callback not in self._backchannel_listeners:
            self._backchannel_listeners.append(callback)

    def feed_stt_partial(
        self,
        partial_text: str,
        *,
        final: bool = False,
    ) -> BackchannelHint | None:
        """Hot-path entry point for partial STT text (every ~200ms).

        Forwards the partial to all subscribed listeners, then runs the
        regex backchannel classifier through the rate-limit gate. If a new
        hint fires, broadcasts it to backchannel listeners. Returns the
        hint (or ``None``) so callers can also use it locally.

        ``final=True`` signals "the WAV has just been committed and we're
        about to call ``transcribe(wav)``". The prefetcher gets the most
        recent partial as a high-priority submission so the RAG retrieval
        runs in parallel with Whisper. Backchannel hints are skipped in
        the final path (the user is already done talking).
        """
        text = (partial_text or "").strip()
        for listener in list(self._stt_partial_listeners):
            try:
                listener(text)
            except Exception:
                log.debug("stt partial listener raised", exc_info=True)
        if not text:
            return None
        # Notify the scheduler so any in-flight background job knows fresh
        # user audio is landing — they can pre-empt and free the LLM
        # channel before the user finishes speaking. (Skip on final: the
        # WAV is already committed; nothing in-flight should be cancelled
        # at this point because we want any prefetch to *complete*.)
        if not final:
            try:
                self._scheduler.on_user_speech()
            except Exception:
                log.debug("scheduler.on_user_speech failed", exc_info=True)
            # Voice merge early-abort: a partial fired during the
            # in-flight LLM turn (TTS hasn't started yet). Tell the
            # runner to stop so its tokens don't waste any more compute,
            # and flag the buffer so ``process_live_capture`` knows to
            # take the merge branch when phrase B's WAV transcribes.
            # Guarded on the partial length so the very first ASR
            # twitch ("uh", "h-") doesn't pre-emptively kill phrase A.
            buf_runner = None
            with self._merge_lock:
                buf = self._merge_buffer.get(self.session_key)
                if (
                    buf is not None
                    and not buf.tts_started
                    and not buf.awaiting_phrase_b
                    and len(text) >= 12
                ):
                    buf.awaiting_phrase_b = True
                    buf_runner = buf.turn_runner
            if buf_runner is not None:
                log.info(
                    "voice merge: aborting in-flight turn on partial "
                    "speech-start (chars=%d)", len(text),
                )
                try:
                    buf_runner.request_stop()
                except Exception:
                    log.debug("turn_runner.request_stop raised", exc_info=True)
        # Phase 1b / listening window: speculatively pre-fetch RAG hits
        # for this partial. The prefetcher is debounced + dedup'd, but on
        # the ``final`` path we want it to run immediately if possible —
        # transcribe(wav) will block for ~100-500 ms and we want the RAG
        # retrieval to finish in that window.
        prefetcher = getattr(self, "_rag_prefetcher", None)
        if prefetcher is not None:
            try:
                recent_turns = self._recent_turn_texts(limit=3)
                prefetcher.submit(
                    text,
                    recent_turns=recent_turns,
                    exclude_session_id=self.session_key,
                )
            except Exception:
                log.debug("rag prefetch submit failed", exc_info=True)
        # Phase 3 of listening_window_prefetch: pre-build the static prompt
        # slices for the eventual turn. This is RAM/SQLite-cheap (5-20 ms),
        # but we hop to a small executor so the capture loop thread never
        # blocks. The first prebuild during a phrase populates the cache;
        # ``assemble_with_budget`` consults it on commit.
        self._submit_prompt_prebuild()
        # Final path skips the rest: backchannel hints don't make sense
        # once the user has stopped talking.
        if final:
            return None
        try:
            hint = self._backchannel_gate.consider(text, now=time.monotonic())
        except Exception:
            log.debug("backchannel gate raised", exc_info=True)
            hint = None
        if hint is None:
            return None
        for listener in list(self._backchannel_listeners):
            try:
                listener(hint, text)
            except Exception:
                log.debug("backchannel listener raised", exc_info=True)
        return hint

    def reset_backchannel_state(self) -> None:
        """Clear gate state at session boundaries so fresh hints can fire."""
        self._backchannel_gate.reset()

    def _notify_mood_state(self, payload: dict[str, Any]) -> None:
        for listener in list(self._mood_listeners):
            try:
                listener(payload)
            except Exception:
                log.debug("mood state listener raised", exc_info=True)

    # Moved to app/core/session/post_turn_mixin.py.

    # ── Voice capture ────────────────────────────────────────────────

    def record_and_chat(
        self,
        seconds: float = 5.0,
        on_token: Callable[[str], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
    ) -> tuple[str, str]:
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")
        if not self._realtime_stt.is_available:
            raise RuntimeError(
                "RealtimeSTT is not available. Install with: pip install realtimestt",
            )
        capture_started = time.perf_counter()
        text = self._realtime_stt.record_until_silence(
            max_seconds=max(3.0, min(seconds, 30.0)),
            silence_seconds=float(self._vad_silence_seconds),
            mic_source=self._microphone,
        )
        capture_ms = (time.perf_counter() - capture_started) * 1000.0
        if not text:
            raise RuntimeError("No speech was detected from microphone audio.")
        text = sanitize_user_text(text)
        if not text:
            raise RuntimeError("No clear speech was detected from microphone audio.")
        self._trace("stt.mic", f"record transcribe ({len(text)} chars)")
        response = self.chat_once_streaming(
            user_text=text,
            on_token=on_token,
            on_generation_status=on_generation_status,
            mode="record",
            capture_ms=capture_ms,
        )
        return text, response

    def listen_once_and_chat(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        max_listen_seconds: float = 18.0,
        on_token: Callable[[str], None] | None = None,
        on_audio_level: Callable[[float], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
    ) -> tuple[str, str] | None:
        captured = self.capture_live_phrase(
            stop_requested=stop_requested,
            max_listen_seconds=max_listen_seconds,
            on_audio_level=on_audio_level,
            on_generation_status=on_generation_status,
        )
        if captured is None:
            return None
        wav_path, capture_ms = captured
        return self.process_live_capture(
            wav_path=wav_path,
            capture_ms=capture_ms,
            stop_requested=stop_requested,
            on_token=on_token,
            on_generation_status=on_generation_status,
        )

    def capture_live_phrase(
        self,
        *,
        stop_requested: Callable[[], bool] | None = None,
        max_listen_seconds: float = 18.0,
        on_audio_level: Callable[[float], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
    ) -> tuple[Path, float] | None:
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")
        if not self._realtime_stt.is_available:
            raise RuntimeError(
                "RealtimeSTT is not available. Install with: pip install realtimestt",
            )

        live_level_threshold = max(0.004, float(self._vad_level_threshold) * 0.4)
        if self._live_no_speech_streak > 0:
            relax = min(0.7, 0.18 * float(self._live_no_speech_streak))
            live_level_threshold = max(0.002, live_level_threshold * (1.0 - relax))
        end_threshold = max(0.004, float(self._vad_level_threshold) * 0.4)

        # Tiered endpointing: when enabled, the loop's own
        # silence_seconds_to_stop becomes the *hard* turn boundary
        # (`turn_silence_seconds`). The endpoint_check we pass below can
        # break out earlier on a sentence-final partial, or extend the
        # window on a hesitation marker. Legacy mode keeps the original
        # `vad_silence_seconds + 0.4` clamp.
        endpointing_cfg = self._settings.endpointing
        if endpointing_cfg.enabled:
            silence_seconds = max(
                0.4, float(endpointing_cfg.turn_silence_seconds)
            )
        else:
            silence_seconds = min(
                6.0, max(1.5, float(self._vad_silence_seconds) + 0.4)
            )
        use_webrtc = self._live_no_speech_streak < 3

        # Snapshot the recorder's current text on speech-start so we only
        # consider the suffix produced by THIS capture as the partial
        # transcript. Avoids carry-over from previous phrases that the
        # recorder may still be decoding.
        partial_baseline = [""]
        extension_count = [0]
        last_partial_chars = [0]
        # Debounce / dedup state for the listening-window prefetch hook
        # (Phase 1 of the listening_window_prefetch plan). We feed
        # ``feed_stt_partial`` periodically — every ~400 ms once the
        # partial has grown by >= 6 chars — so the existing
        # ``RagPrefetcher`` machinery actually runs during live voice
        # mode without one submission per chunk.
        last_fed_partial = [""]
        last_fed_at = [0.0]
        # The most recent partial we observed in this phrase, stashed so
        # ``process_live_capture`` can fire a final prefetch right before
        # ``transcribe(wav)``.
        last_seen_partial = [""]

        def _on_speech_start() -> None:
            if endpointing_cfg.enabled and endpointing_cfg.use_partial_transcript:
                try:
                    partial_baseline[0] = self._realtime_stt.text() or ""
                except Exception:
                    partial_baseline[0] = ""
            # Reset listening-window state for this phrase.
            last_fed_partial[0] = ""
            last_fed_at[0] = 0.0
            last_seen_partial[0] = ""

        def _maybe_feed_partial(partial: str) -> None:
            """Debounced bridge from the capture loop to feed_stt_partial.

            Triggers everything wired to ``feed_stt_partial``: scheduler
            cancel of background LLM workers, RAG prefetch, backchannel
            classifier, frontend partial broadcast.
            """
            if not partial or len(partial) < 12:
                return
            now = time.monotonic()
            # 400 ms debounce; require >= 6 new chars since last feed so
            # tiny edits to the partial don't refire.
            if (now - last_fed_at[0]) < 0.4:
                return
            if abs(len(partial) - len(last_fed_partial[0])) < 6 and partial == last_fed_partial[0]:
                return
            last_fed_partial[0] = partial
            last_fed_at[0] = now
            try:
                self.feed_stt_partial(partial)
            except Exception:
                log.debug("feed_stt_partial from capture loop raised", exc_info=True)

        # Throttle the periodic partial read inside _on_chunk so we don't
        # call ``stt.text()`` on every chunk. ``feed_stt_partial`` itself
        # is also debounced in ``_maybe_feed_partial``; this just bounds
        # how often we *try*.
        last_chunk_partial_check = [0.0]

        def _on_chunk(chunk_arr: Any) -> None:
            if not (endpointing_cfg.enabled and endpointing_cfg.use_partial_transcript):
                return
            try:
                self._realtime_stt.feed_audio(chunk_arr)
            except Exception:
                pass
            # Periodically read the partial during continuous speech so the
            # listening-window prefetch fires even when there are no silence
            # boundaries to trigger ``_endpoint_check``. Every ~500 ms is
            # enough — RAG retrieval needs roughly that long anyway.
            now = time.monotonic()
            if now - last_chunk_partial_check[0] < 0.5:
                return
            last_chunk_partial_check[0] = now
            partial = _read_partial()
            if partial:
                last_seen_partial[0] = partial
                _maybe_feed_partial(partial)

        def _read_partial() -> str:
            try:
                full = self._realtime_stt.text() or ""
            except Exception:
                return ""
            base = partial_baseline[0]
            if base and full.startswith(base):
                return full[len(base):]
            return full

        def _endpoint_check(silence_s: float, _spoken: int) -> str:
            if not endpointing_cfg.enabled:
                return "wait"
            # Lazy partial fetch: only call text() when we're at or past
            # the earliest decision tier (fast_close). Below that we know
            # decide() returns "wait" anyway.
            min_tier = min(
                float(endpointing_cfg.fast_close_silence_seconds),
                float(endpointing_cfg.phrase_silence_seconds),
            )
            partial = ""
            if (
                silence_s >= min_tier
                and endpointing_cfg.use_partial_transcript
            ):
                partial = _read_partial()
            if partial:
                last_seen_partial[0] = partial
                # Bridge to listening-window machinery (debounced inside).
                _maybe_feed_partial(partial)
            decision = _endpointing.decide(silence_s, partial, endpointing_cfg)
            if decision == "extend":
                extension_count[0] += 1
            # Throttle DEBUG noise: only emit when we've actually crossed a
            # tier OR when we have a non-trivial decision. The decide()
            # call itself is cheap; the log line carries the trace.
            if silence_s >= min_tier or decision != "wait":
                last_partial_chars[0] = len(partial)
                log.debug(
                    "endpoint decide: silence_s=%.2f partial_chars=%d "
                    "hesitation=%s sentence_final=%s decision=%s extensions=%d",
                    silence_s,
                    len(partial),
                    "1" if _endpointing.is_hesitation_marker(partial) else "0",
                    "1" if _endpointing.is_sentence_final(partial) else "0",
                    decision,
                    extension_count[0],
                )
            return decision

        if on_generation_status:
            on_generation_status("listening")
        capture_started = time.perf_counter()
        # Hold the STT recorder context open just for the duration of the
        # capture so feed_audio + text() work for partial-driven endpointing.
        # We close it before returning so the subsequent transcribe(wav)
        # call in process_live_capture gets a fresh context and doesn't
        # double-feed the same audio.
        wants_partial = (
            endpointing_cfg.enabled and endpointing_cfg.use_partial_transcript
        )
        if wants_partial:
            try:
                self._realtime_stt.start_context()
            except Exception:
                log.debug("STT start_context failed; partial endpointing disabled", exc_info=True)
                wants_partial = False
        try:
            wav_path = self._microphone.capture_phrase_to_wav(
                max_seconds=max_listen_seconds,
                max_wait_for_speech_start_seconds=12.0,
                use_webrtc_vad=use_webrtc,
                silence_seconds_to_stop=silence_seconds,
                level_threshold=live_level_threshold,
                end_level_threshold=end_threshold,
                min_speech_seconds_before_stop=1.5,
                speech_start_grace_seconds=0.8,
                max_seconds_after_speech_start=18.0,
                stop_requested=stop_requested,
                on_speech_start=_on_speech_start,
                on_audio_level=on_audio_level,
                on_silence_level=self._on_mic_silence_level,
                on_chunk=_on_chunk if wants_partial else None,
                endpoint_check=_endpoint_check if endpointing_cfg.enabled else None,
            )
        finally:
            if wants_partial:
                try:
                    self._realtime_stt.stop_context()
                except Exception:
                    log.debug("STT stop_context failed", exc_info=True)
        capture_ms = (time.perf_counter() - capture_started) * 1000.0
        if wav_path is None:
            self._live_no_speech_streak += 1
            if on_generation_status:
                on_generation_status(f"listening (retry {self._live_no_speech_streak})")
            # No phrase captured: clear any stale partial so we don't fire
            # a final prefetch with text that was abandoned.
            self._last_live_partial.pop(self.session_key, None)
            self._last_listen_extensions = 0
            return None
        # Stash the most recent partial for the STT-processing-window
        # prefetch in :meth:`process_live_capture`.
        if last_seen_partial[0]:
            self._last_live_partial[self.session_key] = last_seen_partial[0]
        else:
            self._last_live_partial.pop(self.session_key, None)
        self._last_listen_extensions = int(extension_count[0])
        if extension_count[0] > 0:
            log.info(
                "live phrase: extensions=%d capture_ms=%.0f",
                extension_count[0], capture_ms,
            )
        return wav_path, capture_ms

    def capture_ptt_phrase(
        self,
        *,
        ptt_active_getter: Callable[[], bool],
        stop_requested: Callable[[], bool] | None = None,
        on_audio_level: Callable[[float], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
        max_seconds: float = 30.0,
    ) -> tuple[Path, float] | None:
        if not self._state.mic_enabled:
            raise RuntimeError("Microphone source is disabled. Enable it and try again.")
        if not self._realtime_stt.is_available:
            raise RuntimeError(
                "RealtimeSTT is not available. Install with: pip install realtimestt",
            )
        if on_generation_status:
            on_generation_status("push-to-talk")
        return self._microphone.capture_while_ptt_active(
            ptt_active_getter=ptt_active_getter,
            stop_requested=stop_requested,
            on_audio_level=on_audio_level,
            max_seconds=max_seconds,
        )

    def process_live_capture(
        self,
        *,
        wav_path: Path,
        capture_ms: float,
        stop_requested: Callable[[], bool] | None = None,
        on_token: Callable[[str], None] | None = None,
        on_generation_status: Callable[[str], None] | None = None,
    ) -> tuple[str, str] | None:
        if not self._realtime_stt.is_available:
            return None
        try:
            self._earcons.play("listening")
        except Exception:
            pass
        # Listening-window prefetch (Phase 2): fire one final RAG prefetch
        # using the most recent partial we observed during capture, right
        # before Whisper blocks the thread. The prefetcher runs on its own
        # background executor so this is non-blocking; by the time
        # transcribe(wav) returns, retrieval is usually cached.
        last_partial = self._last_live_partial.pop(self.session_key, "")
        if last_partial:
            try:
                self.feed_stt_partial(last_partial, final=True)
            except Exception:
                log.debug("final feed_stt_partial failed", exc_info=True)
        # Phase 1a: vocal-tone analysis. Runs before Whisper so the
        # ~3-5 ms FFT/RMS pass piggybacks on the same I/O cache and
        # the result is available for the prompt builder by the time
        # ``chat_once_streaming`` runs. Failures are swallowed — the
        # block provider just returns "" and nothing else cares.
        try:
            from app.core.affect.vocal_tone import analyse_wav

            tone = analyse_wav(wav_path)
            with self._vocal_tone_lock:
                self._last_vocal_tone = tone
            if tone.confident:
                log.info(
                    "vocal tone: energy=%s pitch=%s pace=%s arousal_hint=%+.2f",
                    tone.energy, tone.pitch, tone.pace, tone.arousal_hint,
                )
        except Exception:
            log.debug("vocal tone analysis failed", exc_info=True)
            with self._vocal_tone_lock:
                self._last_vocal_tone = None
        try:
            if on_generation_status:
                on_generation_status("transcribing")
            stt_started = time.perf_counter()
            text = self._realtime_stt.transcribe(wav_path)
            stt_ms = (time.perf_counter() - stt_started) * 1000.0
        finally:
            try:
                Path(wav_path).unlink(missing_ok=True)
            except Exception:
                pass

        if not text:
            self._live_no_speech_streak += 1
            if on_generation_status:
                on_generation_status("did not catch that, listening")
            return None
        text = sanitize_user_text(text)
        if not text:
            self._live_no_speech_streak += 1
            if on_generation_status:
                on_generation_status("did not catch that, listening")
            return None
        self._live_no_speech_streak = 0
        self._trace("stt.mic", f"live transcribe ({len(text)} chars)")

        # ── Voice merge branch ────────────────────────────────────────
        # If ``feed_stt_partial`` aborted the previous turn and TTS still
        # hasn't started, fold this phrase's text into the existing user
        # row and restart the turn with the combined text instead of
        # firing a brand-new ``role="user"`` message. The merge buffer
        # is consumed (popped) so a third phrase starts a fresh turn
        # unless ``chat_once_streaming`` re-installs a buffer (which it
        # always does for live mode, enabling N-way merge).
        merge_text: str | None = None
        merge_user_message_id: int | None = None
        with self._merge_lock:
            buf = self._merge_buffer.get(self.session_key)
            if (
                buf is not None
                and buf.awaiting_phrase_b
                and not buf.tts_started
            ):
                merged = (buf.user_text + " " + text).strip()
                merge_text = merged
                merge_user_message_id = buf.user_message_id
                # Pop here to avoid a partial fired between this line and
                # ``chat_once_streaming`` re-installing the buffer
                # racing on stale state.
                self._merge_buffer.pop(self.session_key, None)
        if merge_text is not None and merge_user_message_id is not None:
            try:
                self._chat_db.update_message_content(
                    merge_user_message_id, merge_text,
                )
            except Exception:
                log.exception(
                    "voice merge: update_message_content failed; "
                    "falling back to fresh turn",
                )
                merge_text = None
                merge_user_message_id = None
        # Chunk 11: route both the merge and the fresh-turn branches
        # through the brain queue via ``enqueue_user_message``. The
        # merge decision above already resolved which case we're in
        # (DB row updated in place + ``_resume_message_id`` set, or a
        # fresh turn). ``enqueue_user_message`` blocks on a Future
        # until the brain-loop handler finishes the LLM stream so
        # ``process_live_capture`` keeps its existing synchronous
        # contract (the caller in ``live_session.py`` runs
        # ``_wait_for_tts_drain`` immediately after we return). When
        # the task subsystem is off / not wired, the helper degrades
        # to a direct ``chat_once_streaming`` call so the legacy
        # behaviour is byte-identical.
        if merge_text is not None and merge_user_message_id is not None:
            log.info(
                "voice merge: restarting turn with combined text "
                "(user_msg_id=%d combined_chars=%d)",
                merge_user_message_id, len(merge_text),
            )
            response = self.enqueue_user_message(
                text=merge_text,
                mode="voice",
                wait_for_reply=True,
                timeout=None,
                on_token=on_token,
                on_generation_status=on_generation_status,
                stop_requested=stop_requested,
                resume_message_id=merge_user_message_id,
                capture_ms=capture_ms,
                stt_ms=stt_ms,
            )
            return merge_text, response or ""

        response = self.enqueue_user_message(
            text=text,
            mode="voice",
            wait_for_reply=True,
            timeout=None,
            on_token=on_token,
            on_generation_status=on_generation_status,
            stop_requested=stop_requested,
            capture_ms=capture_ms,
            stt_ms=stt_ms,
        )
        return text, response or ""

    def run_stt_diagnostic(
        self,
        *,
        seconds: float = 5.0,
        vad_filter: bool = True,
        initial_prompt: str = "",
    ) -> dict[str, object]:
        if not self._state.mic_enabled:
            return {"ok": False, "reason": "mic-disabled", "message": "Microphone source is disabled."}
        if not self._realtime_stt.is_available:
            return {"ok": False, "reason": "stt-missing", "message": "RealtimeSTT not installed."}
        try:
            text = self._realtime_stt.record_until_silence(
                max_seconds=max(3.0, min(seconds, 30.0)),
                silence_seconds=float(self._vad_silence_seconds),
                mic_source=self._microphone,
            )
        except Exception as exc:
            return {"ok": False, "reason": "exception", "message": str(exc)}
        return {
            "ok": True,
            "stt_model": self.stt_model,
            "transcription": (text or "").strip(),
            "vad_filter": bool(vad_filter),
            "initial_prompt": initial_prompt or "",
        }

    # ── Internals ───────────────────────────────────────────────────

    def _apply_assistant_preferences(self) -> None:
        length_scale = getattr(self._settings.assistant, "tts_length_scale", 1.0) or 1.0
        set_length = getattr(self._tts_engine, "set_length_scale", None)
        if callable(set_length):
            try:
                set_length(length_scale)
            except Exception:
                log.debug("tts engine rejected length scale", exc_info=True)
        # Layer 1c gate: opt-in per-reaction temperature deltas.
        # Default OFF -- Pocket-TTS is sensitive enough to temperature
        # excursions that even small per-reaction deltas can introduce
        # pitch / timbre artefacts on the active voice. The user
        # opts in via ``agent.tts_runtime_temp_enabled`` once a
        # voice has been validated.
        runtime_temp_enabled = bool(
            getattr(self._settings.agent, "tts_runtime_temp_enabled", False),
        )
        set_runtime_temp = getattr(
            self._tts_engine, "set_runtime_temp_enabled", None,
        )
        if callable(set_runtime_temp):
            try:
                set_runtime_temp(runtime_temp_enabled)
            except Exception:
                log.debug(
                    "tts engine rejected runtime temp toggle",
                    exc_info=True,
                )
        # Layer 5 gate: opt-in per-reaction speed jitter.
        # Default OFF -- Pocket-TTS scales playback ``sample_rate`` to
        # change speed, which couples speed and pitch. With per-
        # reaction sub-caps active, that pitch couples to the affect
        # channel and the user perceives "her voice keeps changing"
        # between sentences. The user opts in via
        # ``agent.tts_runtime_speed_enabled`` once a voice has been
        # validated.
        runtime_speed_enabled = bool(
            getattr(
                self._settings.agent, "tts_runtime_speed_enabled", False,
            ),
        )
        set_runtime_speed = getattr(
            self._tts_engine, "set_runtime_speed_enabled", None,
        )
        if callable(set_runtime_speed):
            try:
                set_runtime_speed(runtime_speed_enabled)
            except Exception:
                log.debug(
                    "tts engine rejected runtime speed toggle",
                    exc_info=True,
                )

    def _trace(self, stage: str, message: str) -> None:
        from datetime import datetime, timezone
        self._decision_trace.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "message": message,
        })
        if "error" in stage.lower():
            try:
                log_event(stage, message)
            except Exception:
                pass

    @staticmethod
    def _build_tts_service(settings: AppSettings) -> Any:
        # Lean v1 ships only pocket-tts (matches the active user.json config).
        # Playback now flows through ``set_pcm_listener`` -> WS hub
        # -> connected clients; the engine no longer holds a device handle.
        from app.tts.pocket_tts_service import PocketTtsService
        return PocketTtsService(settings.tts)

    # ── IdleWorkerScheduler activity gate (schema v8 / G1) ──────────

    def _touch_user_activity(self) -> None:
        """Mark "the user just did something". Resets the idle gate.

        Called from the turn lifecycle and from incoming WS / REST
        traffic. The :class:`IdleWorkerScheduler` consults
        :meth:`_is_user_idle` before running a worker; a recent touch
        defers background work so it doesn't compete with the active
        conversation.
        """
        self._last_user_activity_at = time.monotonic()

    def _is_user_idle(self) -> bool:
        """Return True when it's safe to run a background worker.

        Three rules:
          * Live mode (voice) is **always** considered busy. The
            speaking window already runs the speaking-window scheduler;
            stacking idle workers on top would compete for CPU.
          * A turn currently in progress -> not idle.
          * Less than ``idle_worker_quiet_threshold_seconds`` since the
            last user activity -> not idle.
        """
        try:
            if getattr(self, "_live_mode_enabled", False):
                return False
            if getattr(self, "_turn_in_progress", False):
                return False
        except Exception:
            return True
        threshold = float(
            self._memory_settings.idle_worker_quiet_threshold_seconds
        )
        elapsed = time.monotonic() - float(
            getattr(self, "_last_user_activity_at", 0.0) or 0.0
        )
        return elapsed >= threshold

    # ── Shutdown ────────────────────────────────────────────────────

    def shutdown(self) -> None:
        # Clear the voice merge buffer first so a tail-end partial that
        # races shutdown can't try to call ``request_stop()`` on a
        # half-torn-down ``TurnRunner``.
        try:
            self._clear_merge_buffer()
        except Exception:
            log.debug("merge buffer clear on shutdown failed", exc_info=True)
        try:
            self._disarm_typed_silence_timer()
        except Exception:
            log.debug("typed silence timer cancel on shutdown failed", exc_info=True)
        # Brain orchestration first: stop the loop + escalation timers
        # before downstream components disappear. The mixin is
        # exception-safe internally; the outer guard is just for the
        # case where ``_init_task_orchestration`` raised partway
        # through and left the mixin in a half-built state.
        try:
            self._shutdown_task_orchestration()
        except Exception:
            log.debug(
                "task-orchestration shutdown failed", exc_info=True
            )
        if self._mcp_server_runner is not None:
            try:
                self._mcp_server_runner.stop()
            except Exception:
                log.debug("mcp stop failed", exc_info=True)
        try:
            self._scheduler.stop()
        except Exception:
            log.debug("scheduler stop failed", exc_info=True)
        if getattr(self, "_rag_prefetcher", None) is not None:
            try:
                self._rag_prefetcher.shutdown()
            except Exception:
                log.debug("rag prefetcher shutdown failed", exc_info=True)
        if getattr(self, "_listening_window_executor", None) is not None:
            try:
                self._listening_window_executor.shutdown(
                    wait=False, cancel_futures=True,
                )
            except Exception:
                log.debug("listening window executor shutdown failed", exc_info=True)
        try:
            self._tts.stop()
        except Exception:
            pass
        if getattr(self, "_client_cache", None) is not None:
            try:
                self._client_cache.shutdown()
            except Exception:
                log.debug("client cache shutdown failed", exc_info=True)
        if getattr(self, "_idle_scheduler", None) is not None:
            try:
                self._idle_scheduler.stop(timeout=1.5)
            except Exception:
                log.debug("idle worker scheduler stop failed", exc_info=True)
        if getattr(self, "_message_indexer", None) is not None:
            try:
                self._message_indexer.stop()
            except Exception:
                log.debug("message indexer stop failed", exc_info=True)
        try:
            self._summary_worker.stop()
        except Exception:
            pass
        if self._memory_store is not None:
            try:
                self._memory_store.close()
            except Exception:
                log.debug("memory store close failed", exc_info=True)
        if self._embedder is not None:
            try:
                self._embedder.close()
            except Exception:
                log.debug("embedder close failed", exc_info=True)
        try:
            t = threading.Thread(target=self._realtime_stt.stop_context, daemon=True)
            t.start()
            t.join(timeout=2.0)
        except Exception:
            pass


