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

import json
import logging
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
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
    ChatTurnMixin,
    DetectorsInitMixin,
    IdleWorkersInitMixin,
    InnerLifeProvidersMixin,
    LifecycleMixin,
    ListenersMetricsMixin,
    LlmClientsMixin,
    LlmSettingsMixin,
    MemoryFacadeMixin,
    PersonaRegressionMixin,
    PostTurnMixin,
    ProactivePresenceMixin,
    SearchProviderMixin,
    SpeakingWindowJobsMixin,
    SpeakingWorkersInitMixin,
    TaskOrchestrationMixin,
    ToolsRegistryMixin,
    VoiceCaptureMixin,
    VoiceMixin,
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
)
from app.core.voice.speaking_window_scheduler import SpeakingWindowScheduler
from app.core.proactive.summary_worker import SummaryWorker
from app.core.voice.tts_queue import TtsQueue
from app.core.session.turn_runner import TurnRunner
from app.core.session.merge_buffer import _MergeBuffer
from app.core.session.session_state import SessionState
from app.llm.chat_client import ChatClient
from app.llm.embedder import Embedder
from app.llm.factory import ClientCache, build_client_for_route
from app.llm.llm_gate import (
    CONVERSATION_WORKER,
    MAINTENANCE_WORKER,
    TASK,
    GatedChatClient,
    LlmPriorityGate,
    tier_from_name,
)
from app.llm.ollama_client import OllamaClient
from app.llm.openai_compatible_client import OpenAICompatibleClient
from app.llm.token_utils import estimate_tokens
from app.stt import endpointing as _endpointing
from app.stt.realtime_stt_service import RealtimeSttService


log = logging.getLogger("app.session")


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


# ``_PROVIDER_PRESETS`` now lives in app/core/session/llm_presets.py
# (consumed by llm_settings_mixin). ``GET /api/llm/presets`` reaches it
# via ``SessionController.provider_presets()``.


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
            reasoning_effort=getattr(chat_llm, "reasoning_effort", "") or "",
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
    SearchProviderMixin,
    ChatTurnMixin,
    VoiceCaptureMixin,
    VoiceMixin,
    ProactivePresenceMixin,
    ListenersMetricsMixin,
    ToolsRegistryMixin,
    LifecycleMixin,
    LlmClientsMixin,
    LlmSettingsMixin,
    SpeakingWorkersInitMixin,
    IdleWorkersInitMixin,
    DetectorsInitMixin,
    PersonaRegressionMixin,
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

        # ── Secret storage ───────────────────────────────────────────────
        # Move any plaintext API keys out of ``user.json`` into the OS
        # keychain, and hydrate in-memory keys from the keychain when the
        # on-disk config has none. MUST run before the client builds below
        # so the freshly-hydrated keys reach ``_build_chat_client`` /
        # ``ClientCache``. Inert under pytest + when no keychain backend
        # is present (the plaintext-config path is preserved verbatim).
        self._init_secret_storage()

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
            # to set two URLs. The worker model + context window come from
            # the ``worker_default`` route (P13), falling back to the
            # legacy ``ollama.*`` block.
            raw_worker_client: ChatClient = self._build_worker_ollama_client(
                chat_llm.keep_alive
            )
        else:
            # Either pure Ollama (one client serves both roles) or the
            # user explicitly opted workers into the remote provider.
            raw_worker_client = self._chat_client
        # Phase 6: wrap the raw worker client in the priority gate and
        # expose the conversation / maintenance / workflow proxy views.
        # Sets self._worker_client, self._ollama, self._maintenance_client,
        # self._workflow_client, self._worker_llm_gate.
        self._worker_client: ChatClient
        self._install_worker_clients(raw_worker_client)
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
        if self._worker_client_inner is self._chat_client:
            self._effective_worker_model = self._effective_chat_model
        else:
            # Route-first (P13): the worker_default route owns the model.
            self._effective_worker_model, _ = self._worker_route_model_ctx()

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
            "mood_inertia_damping": bool(settings.avatar.mood_inertia_damping),
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
        # D2 Part B — attachments the user added to the message being
        # processed THIS turn. Set at the top of ``chat_once_streaming``
        # and read by the ``attachments`` inner-life provider; reset to
        # empty on every turn so a stale value never re-surfaces.
        self._active_turn_attachments: list[dict] = []
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
        # One-shot notices accumulated during boot (before any WS client is
        # connected) and delivered in the ``hello`` payload to the first
        # client that connects. Currently carries the destructive
        # LanceDB-rebuild warning (I7).
        self._startup_notices: list[dict[str, Any]] = []
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
                        self._capture_embedding_swap_notice(self._rag_store)
                    except Exception:
                        log.debug("embedding-swap notice capture failed", exc_info=True)
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
                            cluster_diversity_enabled=bool(
                                getattr(
                                    self._settings.agent,
                                    "rag_cluster_diversity_enabled",
                                    True,
                                )
                            ),
                            max_per_cluster=int(
                                getattr(
                                    self._settings.agent,
                                    "rag_max_per_cluster",
                                    3,
                                )
                            ),
                            topic_expansion_enabled=bool(
                                getattr(
                                    self._settings.agent,
                                    "rag_topic_expansion_enabled",
                                    True,
                                )
                            ),
                            expand_max=int(
                                getattr(
                                    self._settings.agent,
                                    "rag_expand_max",
                                    2,
                                )
                            ),
                            expand_trigger_score=float(
                                getattr(
                                    self._settings.agent,
                                    "rag_expand_trigger_score",
                                    0.55,
                                )
                            ),
                            expand_min_sim=float(
                                getattr(
                                    self._settings.agent,
                                    "rag_expand_min_sim",
                                    0.45,
                                )
                            ),
                            topic_digest_surface_enabled=bool(
                                getattr(
                                    self._settings.agent,
                                    "topic_digest_surface_in_rag",
                                    True,
                                )
                            ),
                            digest_sibling_cap=int(
                                getattr(
                                    self._settings.agent,
                                    "rag_digest_sibling_cap",
                                    1,
                                )
                            ),
                            direct_recall_enabled=bool(
                                getattr(
                                    self._settings.agent,
                                    "rag_direct_recall_enabled",
                                    True,
                                )
                            ),
                            direct_recall_max_messages=int(
                                getattr(
                                    self._settings.agent,
                                    "rag_direct_recall_max_messages",
                                    6,
                                )
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
        self._thread_note_listeners: list[Callable[[dict[str, Any]], None]] = []
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
            cue_register_rotation_enabled=bool(
                getattr(self._settings.agent, "cue_register_rotation_enabled", True)
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
        self._init_speaking_workers(settings, self_image_path)

        # ── Speaking-window scheduler (Phase 2a) ─────────────────────
        # Drains LLM-driven background jobs while Aiko is mid-TTS so the
        # hot path stays cheap. Workers register themselves with this and
        # submit jobs from `_post_turn` rather than running their own daemon
        # threads. The scheduler is created up-front so workers can take a
        # reference at construction time.
        self._init_speaking_window(settings)

        # Schema v10 — follow-up worker rides the same idle scheduler as
        # the decay/promotion workers. It is a silent *cue* producer (the
        # K34 ForwardCuriosityWorker pattern): when a user-mentioned
        # future_plan's event time passes it drafts a hint into the
        # ``aiko.follow_up_cues`` kv ring, optionally phrasing a natural
        # retrospective question on the local worker LLM. The
        # ``_render_follow_up_block`` provider surfaces it on the next
        # turn so Aiko asks in her own voice — it is NEVER spoken
        # verbatim (the bug that leaked the directive into chat). Failures
        # only drop the cue path; the retrieval annotations still work.
        self._init_idle_workers(settings)
        # K17 — one-shot clarification-repair slot. Filled by
        # ``post_turn_mixin._post_turn_inner_life`` when the detector
        # fires; consumed and cleared by
        # ``inner_life_providers_mixin._render_clarification_block``
        # on the next turn so the cue appears exactly once.
        self._init_detectors_and_state(settings)

        self._init_runtime_and_hooks(settings)

    # ── State ─────────────────────────────────────────────────────────

    # Session-lifecycle methods (identity, session switch/clear, model
    # getters, accessors, shutdown) live in lifecycle_mixin.py.














    # ── Settings getters / setters ───────────────────────────────────






    # ── P13b: declarative worker-model cascade ──────────────────────
    # Every background worker that holds its own model name + talks to
    # ``self._worker_client``. ``set_chat_model`` cascades the worker
    # model to all of them (vs. the old hand-coded 3). Missing attrs
    # and workers without a model knob are skipped harmlessly, so the
    # list can stay generous as new workers land.
    _WORKER_MODEL_CONSUMERS: tuple[str, ...] = (
        "_summary_worker",
        "_memory_extractor",
        "_dialogue_act_tagger",
        "_reflection_worker",
        "_dream_worker",
        "_curiosity_worker",
        "_promise_worker",
        "_self_image_worker",
        "_relationship_pulse",
        "_idle_fact_checker",
        "_curiosity_seed_worker",
        "_goal_worker",
        "_memory_conflict_worker",
        "_belief_worker",
        "_moment_detector",
    )

    # Worker/workflow LLM client build + cascade + set_chat_model methods
    # live in llm_clients_mixin.py.









    # Web-search provider methods (_get_search_provider,
    # _register_search_consumer, _search_public_snapshot,
    # reconfigure_search) now live in
    # app/core/session/search_provider_mixin.py.

    # LLM settings / provider catalogue / routes / secrets methods live in
    # llm_settings_mixin.py. _build_chat_client stays defined in this
    # module (re-exported); the mixin forwards to it lazily.


    # ── PR 2: provider catalogue + role-assignment API ──────────────
    #
    # The catalogue lives on ``self._settings.llm`` and is kept in sync
    # with the legacy ``chat_llm`` + ``ollama`` blocks via the
    # mirror-write helpers below. The legacy blocks remain the
    # in-memory primary for now (the ``_chat_client`` / ``_worker_client``
    # construction paths still read from them) — this keeps the diff
    # contained and lets external scripts / MCP keep reading
    # ``chat_llm`` unchanged. Phase 3 may flip the direction.













    # ── PR 2: catalogue <-> legacy mirror helpers ───────────────────




    # ── Secret storage (OS keychain) ────────────────────────────────
    #
    # API keys never touch ``user.json`` as plaintext once a keychain
    # backend is available. The in-memory dataclasses (``provider.api_key``
    # / ``chat_llm.api_key``) keep holding the resolved key for the life
    # of the process, so every existing read / mask / cache-key path is
    # untouched -- only *persistence* is redirected to the keychain.









    # ── Audio: client-fed mic + speaker streams ─────────────────────

    # Voice I/O + VAD/STT + TTS/prewarm + STT-partial/backchannel methods
    # live in voice_mixin.py.















    # ── TTS API ──────────────────────────────────────────────────────















    # ── Greetings + proactive ────────────────────────────────────────

    # Proactive + presence methods live in proactive_presence_mixin.py.



    # ── Typed-mode proactive: presence gate + silence timer ─────────







    # ── Listeners ────────────────────────────────────────────────────

    # ── Scheduler ───────────────────────────────────────────────────



    # ── Avatar, desktop, circadian, overlay/outfit/motion emits ─────
    # Methods now live in app/core/session/avatar_mixin.py.

    # ── RAG / documents ─────────────────────────────────────────────



    # ── Tools ───────────────────────────────────────────────────────

    # Tool-registry methods live in tools_registry_mixin.py.



    # ── Memory accessors ────────────────────────────────────────────
    # Methods now live in app/core/session/memory_facade_mixin.py.

    # ── World, shared moments, axes, get_together_summary ──────────
    # Methods now live in app/core/session/world_mixin.py.

    # WS listeners + metrics + decision-trace methods live in
    # listeners_metrics_mixin.py.










    # ── Models listing ───────────────────────────────────────────────


    # ── Decision trace + emergency stop (legacy stubs) ──────────────



    # ── Metrics ─────────────────────────────────────────────────────






    # ── The chat loop ────────────────────────────────────────────────

    # Chat-turn methods (chat_once / chat_once_streaming / metrics /
    # next-turn scheduling helpers) live in chat_turn_mixin.py.



    # ── Inner-life block providers (Phase 2b, 2e, 3a, ...) ──────────

    # Moved to app/core/session/inner_life_providers_mixin.py.
    # ── Phase 2a + 2b: bootstrap-time inner-life ────────────────────────






    # Moved to app/core/session/inner_life_providers_mixin.py.
    # Moved to app/core/session/post_turn_mixin.py.
    # Moved to app/core/session/inner_life_providers_mixin.py.
    # Moved to app/core/session/speaking_window_jobs_mixin.py.




    # ── Mood listeners (WS broadcast) ───────────────────────────────


    # ── STT partials + backchannel (Phase 1a) ───────────────────────






    # Moved to app/core/session/post_turn_mixin.py.

    # ── Voice capture ────────────────────────────────────────────────

    # Voice-capture pipeline methods (record_and_chat /
    # capture_live_phrase / process_live_capture / ...) live in
    # voice_capture_mixin.py.






    # ── Internals ───────────────────────────────────────────────────




    # ── IdleWorkerScheduler activity gate (schema v8 / G1) ──────────



    # ── Shutdown ────────────────────────────────────────────────────



