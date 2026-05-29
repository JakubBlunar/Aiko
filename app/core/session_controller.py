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
from app.core.affect_state import AffectStore, AffectUpdater
from app.core.backchannel_classifier import BackchannelGate, BackchannelHint
from app.core.chat_database import ChatDatabase
from app.core import circadian as _circadian
from app.core.crash_logging import log_event
from app.core.memory_extractor import MemoryExtractor
from app.core.memory_retriever import MemoryRetriever
from app.core.memory_store import MemoryStore
from app.core.avatar_profile import AvatarProfile, AvatarProfileError, from_disk as _avatar_from_disk
from app.core.proactive_director import ProactiveDirector
from app.core.prompt_assembler import PromptAssembler
from app.core.session import AvatarMixin, MemoryFacadeMixin, WorldMixin
from app.core.world_store import WorldStore
from app.core.session_text_utils import (
    infer_tts_reaction,
    prepare_tts_text,
    sanitize_user_text,
)
from app.core.settings import (
    AppSettings,
    persist_user_overrides,
    read_user_overrides,
)
from app.core.speaking_window_scheduler import SpeakingWindowScheduler
from app.core.summary_worker import SummaryWorker
from app.core.tts_queue import TtsQueue
from app.core.turn_runner import TurnRunner
from app.llm.embedder import Embedder
from app.llm.ollama_client import OllamaClient
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
)


def _resolve_env_var_name(*, base_url: str, explicit: str = "") -> str:
    if explicit:
        return explicit
    host = (base_url or "").lower()
    for needle, env_name in _PROVIDER_ENV_HINTS:
        if needle in host:
            return env_name
    return ""


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

    repo_root = Path(__file__).resolve().parents[2]
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


class SessionController(AvatarMixin, MemoryFacadeMixin, WorldMixin):
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._user_id = (settings.assistant.user_id or "default").strip() or "default"
        # Restore the session the user was last viewing so closing the
        # browser tab (or the whole app) doesn't snap them back to the
        # primordial "main" conversation. Persistence happens in
        # ``switch_session`` — see ``_resolve_initial_session_id`` for
        # the fallback chain.
        self._session_id = self._resolve_initial_session_id(default="main")

        # ── Chat LLM client (Ollama or Ollama Cloud) ──────────────────────
        chat_llm = settings.chat_llm
        chat_provider = (chat_llm.provider or "ollama").strip().lower()
        if chat_provider != "ollama":
            log.warning(
                "chat_llm.provider=%s is not supported in the lean rewrite; "
                "falling back to Ollama. Set the model on settings.ollama.chat_model.",
                chat_provider,
            )
        chat_base_url = (chat_llm.base_url or "").strip() or settings.ollama.base_url
        api_key_explicit = (chat_llm.api_key or "").strip()
        api_key_env_name = _resolve_env_var_name(
            base_url=chat_base_url, explicit=(chat_llm.api_key_env or "").strip(),
        )
        api_key = api_key_explicit or os.environ.get(api_key_env_name, "").strip()
        extra_headers = {
            str(k).strip(): str(v).strip()
            for k, v in dict(chat_llm.extra_headers or {}).items()
            if str(k).strip() and v is not None
        }
        self._ollama = OllamaClient(
            settings.ollama,
            base_url=chat_base_url,
            api_key=api_key or None,
            extra_headers=extra_headers or None,
            keep_alive=chat_llm.keep_alive,
        )
        self._chat_provider = "ollama"

        chat_model_override = (chat_llm.model or "").strip()
        self._effective_chat_model = (
            chat_model_override
            or (settings.ollama.chat_model or "").strip()
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
            Path(__file__).resolve().parents[2] / "data" / "chat_sessions.db"
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
            avatar_root = Path(__file__).resolve().parents[2] / avatar_root
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
                    from app.core.rag_store import auto_open as _rag_auto_open

                    rag_root = (
                        Path(__file__).resolve().parents[2] / "data" / "lancedb"
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
                        from app.core.message_indexer import MessageIndexer

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
                        from app.core.rag_retriever import RagRetriever

                        self._rag_retriever = RagRetriever(
                            self._rag_store,
                            self._embedder,
                            top_k=self._memory_settings.top_k,
                            score_threshold=self._memory_settings.score_threshold,
                            memory_store=self._memory_store,
                        )
                    except Exception:
                        log.warning("RagRetriever failed to init", exc_info=True)
                        self._rag_retriever = None
                # DocumentIngestor: lets users upload notes / PDFs that get
                # indexed into the same RagStore.
                self._document_ingestor = None
                if self._rag_store is not None and self._embedder is not None:
                    try:
                        from app.core.document_ingestor import DocumentIngestor

                        docs_root = (
                            Path(__file__).resolve().parents[2] / "data" / "documents"
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
            from app.core.cadence import ProsodyDispatcher

            self._prosody = ProsodyDispatcher(
                self._tts.enqueue,
                enabled=bool(settings.agent.cadence_enabled),
            )
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
            Path(__file__).resolve().parents[2] / "data" / "persona" / "self_image.txt"
        )
        self._prompt_assembler = PromptAssembler(
            self._chat_db,
            memory_retriever=self._memory_retriever,
            rag_retriever=getattr(self, "_rag_retriever", None),
            self_image_path=self_image_path,
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
                from app.core.rag_prefetcher import RagPrefetcher

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
            from app.core.reflection_worker import ReflectionWorker

            self._reflection_worker = ReflectionWorker(
                ollama=self._ollama,
                memory_store=self._memory_store,
                embedder=self._embedder,
                model=self._effective_chat_model,
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
                from app.core.dream_worker import DreamWorker

                self._dream_worker = DreamWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    model=self._effective_chat_model,
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
                from app.core.catchphrase_miner import CatchphraseMiner

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
            from app.core.ambient_noise import AmbientNoiseTracker

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
                from app.core.curiosity_worker import CuriosityWorker

                self._curiosity_worker = CuriosityWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    model=self._effective_chat_model,
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
            from app.core.relationship import (
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
            from app.core.agenda import AgendaStore, AgendaWorker

            self._agenda_store = AgendaStore(self._chat_db)
            self._agenda_worker = AgendaWorker(
                ollama=self._ollama,
                store=self._agenda_store,
                model=self._effective_chat_model,
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
            from app.core.promise_extractor import PromiseExtractor

            self._promise_extractor = PromiseExtractor(
                ollama=self._ollama,
                memory_store=self._memory_store,
                embedder=self._embedder,
                model=self._effective_chat_model,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("PromiseExtractor init failed", exc_info=True)
            self._promise_extractor = None

        # Phase 3a: structured user profile + per-turn user-state estimator.
        # The store is hot-path-safe (small SQL reads) and the estimator
        # runs after every turn (regex only). The worker is LLM-driven and
        # only fires every N user turns inside the speaking window.
        self._user_profile_store = None
        self._user_profile_worker = None
        self._user_state_store = None
        self._user_state_estimator = None
        try:
            from app.core.user_profile import (
                UserProfileStore, UserProfileWorker,
            )
            from app.core.user_state import UserStateEstimator, UserStateStore

            self._user_profile_store = UserProfileStore(self._chat_db)
            self._user_state_store = UserStateStore(self._chat_db)
            self._user_state_estimator = UserStateEstimator(self._user_state_store)
            self._user_profile_worker = UserProfileWorker(
                ollama=self._ollama,
                db=self._chat_db,
                store=self._user_profile_store,
                model=self._effective_chat_model,
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
                from app.core.self_image_worker import SelfImageWorker

                self._self_image_worker = SelfImageWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    target_path=self_image_path,
                    model=self._effective_chat_model,
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
                from app.core.memory_consolidator import MemoryConsolidator

                self._consolidator = MemoryConsolidator(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    chat_db=self._chat_db,
                    model=self._effective_chat_model,
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
                from app.core.relationship_pulse import RelationshipPulseWorker

                self._relationship_pulse = RelationshipPulseWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    relationship_store=getattr(self, "_relationship_store", None),
                    chat_db=self._chat_db,
                    embedder=self._embedder,
                    model=self._effective_chat_model,
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
                from app.core.shared_moments import SharedMomentsStore

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
                from app.core.knowledge_gap_extractor import KnowledgeGapStore

                self._knowledge_gap_store = KnowledgeGapStore(
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                )
            except Exception:
                log.warning("KnowledgeGapStore init failed", exc_info=True)
                self._knowledge_gap_store = None

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
                from app.core.fact_check_queue import FactCheckQueue

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
                from app.core.shared_moment_extractor import MomentDetector

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
                    model=self._effective_chat_model,
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
                from app.core.relationship_axes import (
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
            profile=self._render_user_profile_block,
            user_state=self._render_user_state_block,
            relationship=self._render_relationship_block,
            agenda=self._render_agenda_block,
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
            novelty=self._render_novelty_block,
            stagnation=self._render_stagnation_block,
            grounding_line=self._render_grounding_line,
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
                    model=self._effective_chat_model,
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
            model=self._effective_chat_model,
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
                from app.core.idle_worker_scheduler import IdleWorkerScheduler
                from app.core.memory_decay_worker import MemoryDecayWorker
                from app.core.memory_promotion_worker import (
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
                        from app.core.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.idle_fact_checker import IdleFactChecker
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
                                chat_model=self._effective_chat_model,
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
                        from app.core.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.idle_curiosity_worker import (
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
                                chat_model=self._effective_chat_model,
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

                # Aiko's living garden — plant stage promotion + visiting
                # the garden during idle daylight windows. Both workers
                # piggyback on the shared scheduler so they share the
                # quiet-window gate; they're a no-op when the WorldStore
                # never loaded. Failures here only drop garden cycling;
                # the manual tools still work.
                if getattr(self, "_world_store", None) is not None:
                    try:
                        from app.core.garden_visit_worker import (
                            GardenVisitWorker,
                        )
                        from app.core.plant_growth_worker import (
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
            self._ollama,
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
            from app.core.conversation_arc import (
                ArcEstimator,
                ArcSmootherWorker,
                ArcStore,
            )

            self._arc_store = ArcStore(self._chat_db)
            self._arc_estimator = ArcEstimator(self._arc_store)
            self._arc_smoother = ArcSmootherWorker(
                ollama=self._ollama,
                store=self._arc_store,
                model=self._effective_chat_model,
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
            from app.core.prepared_nudge import (
                NarrativeWeaver,
                PreparedNudgeStore,
            )

            self._prepared_nudge_store = PreparedNudgeStore(self._chat_db)
            self._narrative_weaver = NarrativeWeaver(
                ollama=self._ollama,
                store=self._prepared_nudge_store,
                memory_store=self._memory_store,
                agenda_store=getattr(self, "_agenda_store", None),
                model=self._effective_chat_model,
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
                from app.core.follow_up_worker import FollowUpWorker

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
                from app.core.schedule_learner import ScheduleLearner

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
                from app.core.memory_conflict_store import (
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
                from app.core.fact_check_rate_limiter import (
                    FactCheckRateLimiter,
                )
                from app.core.memory_conflict_worker import (
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
                    chat_model=self._effective_chat_model,
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
        if (
            self._chat_db is not None
            and bool(getattr(settings.agent, "belief_tracking_enabled", True))
        ):
            try:
                from app.core.belief_store import BeliefStore

                self._belief_store = BeliefStore(self._chat_db)
            except Exception:
                log.warning("BeliefStore init failed", exc_info=True)
                self._belief_store = None
        if self._belief_store is not None:
            try:
                from app.core.belief_gap_detector import BeliefGapDetector

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
                from app.core.belief_worker import BeliefInferenceWorker
                from app.core.fact_check_rate_limiter import (
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
                    chat_model=self._effective_chat_model,
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
                from app.core.novelty_detector import NoveltyDetector

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
                from app.core.topic_stagnation import (
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

        self._proactive = ProactiveDirector(
            self._ollama,
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
        from app.core.settings import resolve_user_display_name
        return resolve_user_display_name(self._settings)

    @property
    def needs_onboarding(self) -> bool:
        """True when no display name has been configured yet."""
        from app.core.settings import is_onboarding_needed
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
        """Where ``context_window`` came from: ``config|ollama_show|fallback``."""
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
        2. ``OllamaClient.get_context_length(model)`` from ``/api/show``.
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
            detected = self._ollama.get_context_length(model)
        except Exception:
            detected = None
        if detected and detected > 0:
            return int(detected), "ollama_show"
        return 8192, "fallback"

    def set_chat_model(self, model_name: str) -> None:
        normalized = (model_name or "").strip()
        if not normalized:
            return
        self._settings.ollama.chat_model = normalized
        self._effective_chat_model = normalized
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
        # Update the cached model on workers too.
        self._summary_worker._model = normalized  # type: ignore[attr-defined]
        self._proactive.update_runtime(model=normalized)
        if self._memory_extractor is not None:
            try:
                self._memory_extractor.update_model(normalized)
            except Exception:
                log.debug("memory extractor model update failed", exc_info=True)

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
        report("Checking Ollama availability...")
        try:
            models = self._ollama.list_models()
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
                self._ollama.chat(
                    [{"role": "user", "content": "Reply with OK."}],
                    model=effective,
                    surface="model_warmup",
                )
            except Exception as exc:
                log.warning("chat model warmup failed: %s", exc)

        report("Warming TTS models...")
        self.prewarm_tts()
        report("Warmup complete")

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

    def add_message_listener(self, callback: Callable[[str, str], None]) -> None:
        if callback and callback not in self._message_listeners:
            self._message_listeners.append(callback)

    def _notify_message(self, speaker: str, text: str) -> None:
        for listener in list(self._message_listeners):
            try:
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

    def list_chat_models(self, *, refresh: bool = False) -> list[str]:
        now = time.monotonic()
        if not refresh and self._models_cache is not None and (now - self._models_cache_time) < self._cache_ttl:
            return list(self._models_cache)
        try:
            models = self._ollama.list_models()
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

            result = self._turn_runner.run(
                session_key,
                cleaned,
                on_token=on_token,
                on_tts_chunk=wrapped_tts_cb,
                on_earcon=on_earcon_cb,
                on_overlay=self._emit_avatar_overlay,
                on_outfit=self._emit_avatar_outfit,
                on_motion=self._emit_avatar_motion,
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

    def _render_affect_block(self) -> str:
        """Hot-path: read affect_state and format the ambient block."""
        try:
            from app.core.affect_state import render_ambient_block
            state = self._affect_store.get(self._user_id)
            return render_ambient_block(state)
        except Exception:
            log.debug("affect block render failed", exc_info=True)
            return ""

    def _render_vocal_tone_block(self) -> str:
        """Phase 1a: per-turn paralinguistic cue from the captured WAV.

        Returns an empty string when no live capture has happened yet
        this turn or when the analyser couldn't get a confident estimate
        (very short utterance, silence, missing audio dependencies). The
        snapshot is left in place after the turn so an immediate retry
        path can still see it; it's cleared explicitly when a fresh
        live phrase commits or by ``_clear_vocal_tone_after_turn``.
        """
        try:
            with self._vocal_tone_lock:
                tone = self._last_vocal_tone
            if tone is None:
                return ""
            return tone.to_prompt_line()
        except Exception:
            log.debug("vocal tone block render failed", exc_info=True)
            return ""

    # Per-source-kind framing for the narrative inner-monologue block.
    # The ``open_question`` slot carries a ``{name}`` placeholder filled
    # in :func:`_render_narrative_block` so the cue reads with whatever
    # name the user typed into the onboarding modal; the rest are
    # name-agnostic.
    _NARRATIVE_LABELS: dict[str, str] = {
        "open_question": "Something you've been wanting to ask {name}",
        "callback": "A loose thread to circle back to",
        "promise": "Something you said you'd do",
        "reflection": "On your mind",
        "agenda": "A goal you're tracking",
        "resume": "Where you left off last time",
        "mixed": "On your mind",
    }

    def _render_narrative_block(self) -> str:
        """Inner-monologue cue surfaced from the prepared-nudge store.

        Reads (without consuming) the same nudge that the live-voice
        ``ProactiveDirector`` would speak during silence, and folds it
        into the system prompt so a *typed* turn has the same
        situational awareness ("oh, and there's that thing I wanted to
        ask…"). The LLM decides whether to actually pick it up — we
        just put it on the table.

        Non-consuming on purpose: typed turns don't pre-empt with the
        nudge text, they only react if the conversation goes that way.
        ``ProactiveDirector`` keeps exclusive ownership of ``consume``.

        Returns ``""`` whenever the store hasn't been initialised, no
        fresh nudge is available, or the nudge has empty text — which
        means the block is silently skipped and contributes 0 prompt
        tokens.
        """
        store = getattr(self, "_prepared_nudge_store", None)
        if store is None:
            return ""
        try:
            nudge = store.get_fresh(self._user_id)
        except Exception:
            log.debug("narrative block: get_fresh raised", exc_info=True)
            return ""
        if nudge is None:
            return ""
        text = (nudge.text or "").strip()
        if not text:
            return ""
        label = self._NARRATIVE_LABELS.get(
            (nudge.source_kind or "").strip().lower(),
            "On your mind",
        )
        if "{name}" in label:
            label = label.format(name=self.user_display_name)
        return f"{label}: {text}"

    def _render_catchphrase_block(self) -> str:
        """Phase 2c: "Aiko's running jokes with <name>" inner-life block.

        Hot-path mirror read; no LLM. Surfaces up to 3 catchphrase
        memories sorted by salience so the LLM keeps using the top
        few naturally.
        """
        store = getattr(self, "_memory_store", None)
        if store is None:
            return ""
        try:
            top = store.list_top(limit=24)
        except Exception:
            return ""
        phrases: list[str] = []
        for mem in top:
            if (mem.kind or "").lower() != "catchphrase":
                continue
            content = (mem.content or "").strip()
            if not content:
                continue
            phrases.append(content)
            if len(phrases) >= 3:
                break
        if not phrases:
            return ""
        bullets = "\n".join(f"- {p}" for p in phrases)
        return (
            f"Aiko's running jokes with {self.user_display_name}:\n" + bullets
        )

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

    def _avatar_capabilities(self) -> dict[str, bool] | None:
        """Hot-path: hand the prompt-assembler the loaded avatar's
        capability flags so it can build the dynamic ``[[overlay:X]]``
        / ``[[outfit:X]]`` grammar blocks. Returns ``None`` when no
        avatar is loaded.
        """
        avatar = self._avatar
        if avatar is None:
            return None
        return dict(avatar.capabilities)

    def _avatar_motion_names(self) -> list[str]:
        """Hot-path: return every motion-file stem the loaded rig
        ships, in declaration order. The prompt-assembler crosses
        these against ``_MOTION_GRAMMAR_DESCRIPTIONS`` to decide
        which ``[[motion:X]]`` lines to advertise.
        """
        avatar = self._avatar
        if avatar is None:
            return []
        names: list[str] = []
        for refs in (avatar.motions or {}).values():
            for ref in refs:
                if ref.name:
                    names.append(ref.name)
        return names

    def _render_pajama_block(self) -> str:
        """Quiet-conversation cue: emitted only when the auto-outfit
        resolves to pajamas. Soft prompt nudge layered on top of the
        regular circadian block to keep the tone matched to her outfit.
        """
        try:
            # Either pajama variant warrants the quieter-tone nudge —
            # the hood doesn't change the vibe, just the silhouette.
            if self.resolve_auto_outfit() in {"pajamas", "pajamas_hooded"}:
                return (
                    "You're in pajamas; the conversation is a quieter "
                    "one — softer cadence, smaller sentences, gentler "
                    "warmth."
                )
        except Exception:
            log.debug("pajama block render failed", exc_info=True)
        return ""

    def _render_circadian_block(self) -> str:
        """Hot-path: pure function over the current local time."""
        try:
            state = self._affect_store.get(self._user_id)
            cstate = _circadian.compute(
                baseline_drift=state.baseline_arousal - 0.4,
                baseline_sociability=state.baseline_valence,
            )
            return cstate.ambient_line()
        except Exception:
            log.debug("circadian block render failed", exc_info=True)
            return ""

    def _cadence_context(self) -> Any:
        """Phase 5b: build a CadenceContext from the live affect/circadian."""
        from app.core.cadence import CadenceContext

        ctx = CadenceContext()
        try:
            state = self._affect_store.get(self._user_id)
            ctx.mood_label = state.mood_label or "content"
            ctx.mood_arousal = float(state.arousal)
            ctx.mood_valence = float(state.valence)
        except Exception:
            log.debug("cadence affect lookup failed", exc_info=True)
        try:
            cstate = _circadian.compute()
            ctx.circadian_period = getattr(cstate, "period", "")
            ctx.circadian_drowsy = bool(getattr(cstate, "drowsy", False))
        except Exception:
            log.debug("cadence circadian lookup failed", exc_info=True)
        # Phase 4b: ambient-noise speed multiplier. Default 1.0 (quiet
        # room); the EMA tracker returns a slightly lower value when
        # the room is loud so spoken cadence slows a hair.
        tracker = getattr(self, "_ambient_noise", None)
        if tracker is not None:
            try:
                ctx.ambient_noise_speed = float(tracker.tts_speed_multiplier())
            except Exception:
                log.debug("cadence ambient-noise lookup failed", exc_info=True)
        return ctx

    def _render_user_profile_block(self) -> str:
        """Phase 3a: bullet block of the high-confidence profile fields."""
        store = getattr(self, "_user_profile_store", None)
        if store is None:
            return ""
        try:
            return store.render_block(
                self._user_id,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("user profile block render failed", exc_info=True)
            return ""

    def _render_user_state_block(self) -> str:
        """Phase 3a: tiny per-turn 'Right now <name>...' line."""
        store = getattr(self, "_user_state_store", None)
        if store is None:
            return ""
        try:
            return store.render_block(
                self._user_id,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("user state block render failed", exc_info=True)
            return ""

    def _render_relationship_block(self) -> str:
        """Phase 3b: short ambient block about how long we've known the user."""
        tracker = getattr(self, "_relationship_tracker", None)
        if tracker is None:
            return ""
        try:
            return tracker.ambient_line(
                self._user_id,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("relationship block render failed", exc_info=True)
            return ""

    def _render_ambient_noise_block(self) -> str:
        """Phase 4b: render the ambient-noise prompt cue (empty if quiet)."""
        tracker = getattr(self, "_ambient_noise", None)
        if tracker is None:
            return ""
        try:
            return tracker.prompt_block()
        except Exception:
            log.debug("ambient noise block render failed", exc_info=True)
            return ""

    def _on_mic_silence_level(self, level: float) -> None:
        """Phase 4b: forwarded from :class:`MicrophoneCapture` for every
        capture chunk classified as silence (no VAD speech, level under
        threshold). Folds into the EMA tracker; safe to call from any
        thread.
        """
        tracker = getattr(self, "_ambient_noise", None)
        if tracker is None:
            return
        try:
            tracker.observe(float(level))
        except Exception:
            log.debug("ambient noise observe failed", exc_info=True)

    def _render_petname_block(self) -> str:
        """Phase 2d: address-style cue keyed off the current relationship
        phase. Empty in the ``new`` phase because the persona already
        covers introductions; non-empty after that.
        """
        tracker = getattr(self, "_relationship_tracker", None)
        if tracker is None:
            return ""
        try:
            from datetime import datetime, timezone

            from app.core.relationship import render_petname_block

            state = tracker.get(self._user_id)
            return render_petname_block(
                state,
                now=datetime.now(timezone.utc),
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("petname block render failed", exc_info=True)
            return ""

    def _render_agenda_block(self) -> str:
        """Phase 4a: open agenda items as a small bullet block."""
        store = getattr(self, "_agenda_store", None)
        if store is None:
            return ""
        try:
            return store.render_block(
                self._user_id,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("agenda block render failed", exc_info=True)
            return ""

    def _render_knowledge_gaps_block(self, user_text: str) -> str:
        """F2: surface the open knowledge gap most relevant to ``user_text``.

        Returns at most one bullet. Empty string when there are no open
        gaps or the best similarity match is below the threshold (so we
        don't surface a totally unrelated wondering on every turn). The
        block ends without a trailing newline so the assembler can stitch
        it next to its siblings.
        """
        store = getattr(self, "_knowledge_gap_store", None)
        if store is None:
            return ""
        try:
            gap = store.pick_relevant(user_text)
        except Exception:
            log.debug("knowledge gap pick_relevant failed", exc_info=True)
            return ""
        if gap is None:
            return ""
        meta = getattr(gap, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        topic = str(meta.get("topic") or "").strip()
        question = str(meta.get("question") or "").strip()
        if not question:
            # Defensive: a gap row without question metadata is still
            # worth surfacing via its raw content.
            question = (gap.content or "").strip()
        if not question:
            return ""
        bullet = f"- {topic}: {question}" if topic else f"- {question}"
        return (
            f"Things you've been wondering about with {self.user_display_name}:\n"
            + bullet
        )

    def _render_belief_gaps_block(self) -> str:
        """K2: surface up to two belief-gap lines from the previous turn.

        The gap detector runs in ``_post_turn_inner_life`` and stashes
        any detected mismatches into ``self._pending_belief_gaps``. We
        consume that list here (clearing it after read) so the gap
        only appears in the next turn's prompt -- after that Aiko
        either addressed it or the belief got contradicted/confirmed
        and won't re-surface.
        """
        if not bool(getattr(self._settings.agent, "belief_tracking_enabled", True)):
            return ""
        gaps = getattr(self, "_pending_belief_gaps", None) or []
        if not gaps:
            return ""
        try:
            from app.core.belief_gap_detector import render_inner_life_block

            block = render_inner_life_block(gaps, max_lines=2)
        except Exception:
            log.debug("belief gaps render failed", exc_info=True)
            block = ""
        # Clear regardless of render success so we don't keep retrying
        # the same broken render on every turn.
        self._pending_belief_gaps = []
        if not block:
            return ""
        return (
            f"Your theory-of-mind read on {self.user_display_name} "
            "doesn't quite match the live signal:\n" + block + "\n"
            "Name the gap once and gently if it fits, then move on. "
            "Don't repeat the question."
        )

    def _render_novelty_block(self, user_text: str) -> str:
        """K6: surface a one-line surprise/novelty signal for this turn.

        The detector embeds ``user_text``, compares it to a rolling
        centroid of recent user-message vectors, and returns a banded
        result (``mild_shift`` or ``strong_novelty``). Empty string
        when the detector is disabled, in warmup/cooldown, or the
        distance is below the mild threshold -- which is the common
        case, so the block disappears entirely on normal turns.
        """
        if not bool(
            getattr(self._settings.agent, "novelty_detection_enabled", True)
        ):
            return ""
        detector = getattr(self, "_novelty_detector", None)
        if detector is None:
            return ""
        try:
            result = detector.detect(user_text)
        except Exception:
            log.debug("novelty detector raised", exc_info=True)
            return ""
        if result is None:
            return ""
        try:
            from app.core.novelty_detector import render_inner_life_block

            return render_inner_life_block(result)
        except Exception:
            log.debug("novelty block render failed", exc_info=True)
            return ""

    def _render_stagnation_block(self, user_text: str) -> str:
        """K18: surface a one-line "we've been on this for a while" cue.

        Sibling of :meth:`_render_novelty_block`; runs *after* it on
        the prompt-assembly path so we can read the just-computed
        ``last_distance`` / ``last_band`` off the K6 detector without
        re-embedding. Empty string when disabled, when K6 didn't
        measure a distance this turn (short text / warmup / embed
        failure), when we're inside the post-novelty suppression
        window, when we're inside a hit cooldown, or when the
        rolling mean stays above the mild threshold -- which is the
        common case, so the block disappears entirely on normal
        turns.
        """
        if not bool(
            getattr(self._settings.agent, "topic_stagnation_enabled", True)
        ):
            return ""
        detector = getattr(self, "_topic_stagnation_detector", None)
        if detector is None:
            return ""
        novelty = getattr(self, "_novelty_detector", None)
        # ``last_distance`` is always reset at the top of each
        # ``NoveltyDetector.detect`` call, so the value we read here
        # belongs unambiguously to this turn (or stays ``None`` if
        # K6 was disabled / didn't measure).
        distance = (
            getattr(novelty, "last_distance", None) if novelty is not None
            else None
        )
        novelty_just_fired = bool(
            getattr(novelty, "last_band", None)
        ) if novelty is not None else False
        try:
            result = detector.detect(
                distance,
                novelty_just_fired=novelty_just_fired,
            )
        except Exception:
            log.debug("topic stagnation detector raised", exc_info=True)
            return ""
        if result is None:
            return ""
        try:
            from app.core.topic_stagnation import render_inner_life_block

            return render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("topic stagnation block render failed", exc_info=True)
            return ""

    def _build_grounding_context(self) -> "Any":
        """Assemble the K16 grounding-line slots from live state.

        Reads the same stores the granular block providers read; no
        new database queries land here. Individual store failures
        degrade to None slots instead of raising so the prompt still
        renders if one subsystem is sick.
        """
        from app.core.grounding_line import GroundingContext
        from app.core.world_store import _OUTDOOR_SLUGS

        ctx = GroundingContext(user_display_name=self.user_display_name)

        try:
            cstate = _circadian.compute()
            ctx.weekday = cstate.weekday
            ctx.is_weekend = bool(cstate.is_weekend)
            ctx.period = cstate.period
            ctx.hour = int(cstate.hour)
            ctx.minute = int(cstate.minute)
            ctx.is_drowsy = bool(cstate.drowsy)
        except Exception:
            log.debug("grounding circadian slot failed", exc_info=True)

        try:
            affect = self._affect_store.get(self._user_id)
            label = (affect.mood_label or "").strip()
            if label:
                ctx.mood_label = label
        except Exception:
            log.debug("grounding affect slot failed", exc_info=True)

        store = getattr(self, "_user_state_store", None)
        if store is not None:
            try:
                state = store.get(self._user_id)
                ctx.user_perceived_mood = (
                    state.perceived_mood if state.perceived_mood else None
                )
                ctx.user_perceived_energy = (
                    state.perceived_energy if state.perceived_energy else None
                )
                ctx.user_perceived_focus = (
                    state.perceived_focus if state.perceived_focus else None
                )
            except Exception:
                log.debug("grounding user_state slot failed", exc_info=True)

        world = getattr(self, "_world_store", None)
        if world is not None:
            try:
                wstate = world.get_state()
                if wstate.location_id is not None:
                    loc = world.get_location_by_id(int(wstate.location_id))
                    if loc is not None:
                        ctx.world_location = loc.name
                        ctx.world_outdoor = bool(
                            getattr(loc, "slug", "") in _OUTDOOR_SLUGS
                        )
                ctx.world_posture = (wstate.posture or "").strip() or None
                ctx.world_activity = (wstate.activity or "").strip() or None
            except Exception:
                log.debug("grounding world slot failed", exc_info=True)

        tracker = getattr(self, "_relationship_tracker", None)
        if tracker is not None:
            try:
                from datetime import datetime, timezone
                from app.core.relationship import _days_since, phase_for

                rstate = tracker.get(self._user_id)
                now = datetime.now(timezone.utc)
                ctx.relationship_phase = phase_for(rstate, now=now)
                days = _days_since(rstate, now=now)
                ctx.relationship_days = int(days) if days is not None else None
            except Exception:
                log.debug("grounding relationship slot failed", exc_info=True)

        try:
            app = self._user_active_app
            if (
                app
                and bool(getattr(self._settings.agent, "activity_awareness_enabled", False))
            ):
                ctx.user_app = app
        except Exception:
            log.debug("grounding activity slot failed", exc_info=True)

        noise = getattr(self, "_ambient_noise", None)
        if noise is not None:
            try:
                snap = noise.snapshot()
                if snap.is_very_noisy:
                    ctx.noise_level = "loud"
                elif snap.is_noisy:
                    ctx.noise_level = "soft_hum"
            except Exception:
                log.debug("grounding noise slot failed", exc_info=True)

        return ctx

    def _render_grounding_line(self) -> str:
        """K16 unified ambient grounding line provider.

        Returns ``""`` when ``agent.grounding_line_mode`` is ``"off"``
        (the default) so the granular ambient blocks render unchanged.
        For ``"replace"`` and ``"split"`` the renderer composes one
        paragraph from live state; the suppression of the underlying
        granular blocks is handled by :class:`PromptAssembler` based
        on the same mode value passed through ``assemble_with_budget``.
        """
        try:
            mode = getattr(self._settings.agent, "grounding_line_mode", "off")
            if mode == "off":
                return ""
            from app.core.grounding_line import render as _render_line

            ctx = self._build_grounding_context()
            if ctx is None:
                return ""
            return _render_line(ctx)
        except Exception:
            log.debug("grounding line render failed", exc_info=True)
            return ""

    def _render_world_block(self) -> str:
        """Aiko's room: a compact ambient block with location + items.

        Cheap (mirror dict scan + a couple of f-strings) so it's safe on
        the hot path. The block ends with a tonal nudge instructing Aiko
        not to force-mention her room every turn.
        """
        store = getattr(self, "_world_store", None)
        if store is None:
            return ""
        try:
            return store.render_block(
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("world block render failed", exc_info=True)
            return ""

    def _render_activity_block(self) -> str:
        """Phase 4c: ambient "<name> is in <App>" cue (desktop opt-in).

        Triple-gated by design — toggle off, no app captured, or no
        client connected (browser users never emit ``user_activity``)
        all collapse to an empty string. The toggle gate is the
        privacy-critical one: even if a buggy client forwarded
        ``user_activity`` while the user had disabled the feature,
        the setter would have rejected the value and ``_user_active_app``
        would still be ``None``. The same check here is belt-and-
        braces in case the toggle was flipped between the setter call
        and this render.

        The trailing reminder is the same shape as the world block —
        Aiko knows but only mentions when natural — to keep the prompt
        from turning ambient awareness into surveillance theatre.
        """
        if not bool(getattr(self._settings.agent, "activity_awareness_enabled", False)):
            return ""
        app = self._user_active_app
        if not app:
            return ""
        return (
            f"{self.user_display_name} is currently working in {app}. "
            "You're aware of this but only mention it when it's "
            "genuinely relevant to the conversation — never just to "
            "fill silence or to prove you noticed."
        )

    def _render_anniversary_block(self) -> str:
        """Schema v7: surface a single 'remember when' anniversary line.

        Walks the ``shared_moment`` rows and picks the longest-window
        match for today (1mo/3mo/6mo/1yr/Nyr) within a ±1 day tolerance,
        rate-limited per moment to once every 6h. Stamps the chosen row
        so it won't fire again on the next turn.
        """
        if not bool(getattr(self._settings.agent, "anniversary_surfacing_enabled", True)):
            return ""
        store = getattr(self, "_shared_moments_store", None)
        if store is None:
            return ""
        try:
            from datetime import datetime, timezone

            from app.core.anniversary import pick_anniversary, render_anniversary_block

            moments = store.iter_all()
            match = pick_anniversary(moments, now=datetime.now(timezone.utc))
            if match is None:
                return ""
            # Stamp the row so we don't surface it again on the very next
            # turn. The rate-limit is centralised inside ``pick_anniversary``
            # but this also helps when the same conversation spans many
            # turns inside the 6h window.
            try:
                store.stamp_anniversary(match.moment_id)
            except Exception:
                log.debug("anniversary stamp failed", exc_info=True)
            return render_anniversary_block(match)
        except Exception:
            log.debug("anniversary render failed", exc_info=True)
            return ""

    def _render_axes_block(self) -> str:
        """Schema v7: terse relationship-axes line (only when notable)."""
        if not bool(getattr(self._settings.agent, "relationship_axes_enabled", True)):
            return ""
        store = getattr(self, "_relationship_axes_store", None)
        if store is None:
            return ""
        try:
            from app.core.relationship_axes import render_axes_block

            state = store.get(self._user_id)
            return render_axes_block(
                state,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("axes block render failed", exc_info=True)
            return ""

    def _render_arc_block(self) -> str:
        """Phase 4c: ambient line about the current conversation arc."""
        store = getattr(self, "_arc_store", None)
        if store is None:
            return ""
        try:
            current_turn = self._chat_db.get_message_count(self.session_key)
        except Exception:
            current_turn = 0
        try:
            return store.render_block(
                self._user_id,
                current_turn=current_turn,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("arc block render failed", exc_info=True)
            return ""

    def _top_pinned_self_memories(self, *, limit: int = 5) -> list[str]:
        """Phase 2d: hot-path provider for pinned self-memory bullets.

        Reads from the ``MemoryStore`` mirror (in-memory dict) and filters
        for ``kind == "self"``. Returns up to ``limit`` items sorted by the
        store's salience+use_count ranking. Hot-path safe.
        """
        store = getattr(self, "_memory_store", None)
        if store is None:
            return []
        try:
            top = store.list_top(limit=max(8, int(limit) * 4))
        except Exception:
            log.debug("list_top failed in pinned self provider", exc_info=True)
            return []
        out: list[str] = []
        for mem in top:
            if (mem.kind or "").lower() != "self":
                continue
            content = (mem.content or "").strip()
            if content:
                out.append(content)
            if len(out) >= int(limit):
                break
        return out

    def _record_milestone_memory(self, label: str) -> None:
        """Persist a milestone as a callback memory so RAG surfaces it."""
        if not label:
            return
        store = getattr(self, "_memory_store", None)
        embedder = getattr(self, "_embedder", None)
        if store is None or embedder is None:
            return
        humanized = label.replace("_", " ")
        content = (
            f"Aiko reached a milestone with {self.user_display_name}: {humanized}. "
            "She might naturally bring this up in conversation."
        )
        try:
            emb = embedder.embed(content)
        except Exception:
            log.debug("milestone embed failed", exc_info=True)
            return
        try:
            mem = store.add(
                content=content,
                kind="callback",
                embedding=emb,
                salience=0.6,
                source_session=self.session_key,
                # Schema v8: relationship milestones are real,
                # confirmed events. Long_term so they survive the
                # scratchpad TTL even if RAG never re-surfaces them.
                tier="long_term",
            )
        except Exception:
            log.debug("milestone memory insert failed", exc_info=True)
            return
        if mem is not None:
            log.info("relationship milestone recorded: %s", label)
            try:
                self._notify_memory_added(mem)
            except Exception:
                pass

    def _maybe_schedule_agenda_groom_job(self) -> None:
        """Phase 4a: enqueue AgendaWorker grooming pass on the speaking window."""
        worker = getattr(self, "_agenda_worker", None)
        if worker is None:
            return
        try:
            if not worker.should_run(self._user_id):
                return
        except Exception:
            log.debug("agenda should_run failed", exc_info=True)
            return

        session_key = self.session_key
        user_id = self._user_id
        history_window = 16

        def _history_provider() -> list[tuple[str, str]]:
            try:
                rows = self._chat_db.get_messages(session_key, limit=history_window)
            except Exception:
                return []
            return [
                (str(r.role or ""), str(r.content or ""))
                for r in rows
                if r.role in ("user", "assistant")
            ]

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                worker.maybe_run(
                    user_id, history_provider=_history_provider,
                )
            except Exception:
                log.debug("agenda groom job raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="agenda_groom",
                priority=70,
                estimated_seconds=4.5,
                callable=_job,
                dedupe_key="agenda_groom",
            ))
        except Exception:
            log.debug("agenda groom submit failed", exc_info=True)

    def _maybe_schedule_promise_llm_job(self) -> None:
        """Phase 3c: enqueue the LLM promise extractor in the speaking window."""
        extractor = getattr(self, "_promise_extractor", None)
        if extractor is None:
            return
        try:
            if not extractor.should_run_llm():
                return
        except Exception:
            log.debug("promise extractor should_run failed", exc_info=True)
            return

        session_key = self.session_key
        history_window = 12

        def _history_provider() -> list[tuple[str, str]]:
            try:
                rows = self._chat_db.get_messages(session_key, limit=history_window)
            except Exception:
                return []
            return [
                (str(r.role or ""), str(r.content or ""))
                for r in rows
                if r.role in ("user", "assistant")
            ]

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                extractor.maybe_run_llm(
                    session_key=session_key,
                    history_provider=_history_provider,
                )
            except Exception:
                log.debug("promise llm job raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="promise_llm",
                priority=65,
                estimated_seconds=3.5,
                callable=_job,
                dedupe_key="promise_llm",
            ))
        except Exception:
            log.debug("promise llm submit failed", exc_info=True)

    def _maybe_schedule_moment_llm_job(
        self,
        *,
        user_text: str,
        assistant_text: str,
        raw_assistant_text: str,
        milestone: str | None,
    ) -> None:
        """Schema v7: enqueue the LLM moment detector when signals warrant.

        Gating is a two-step process: a cheap signal check here (so we
        only spend cycles on candidate turns), and a cadence/cooldown
        check inside :class:`MomentDetector.should_run_llm`. Skipping
        either short-circuits the job.
        """
        detector = getattr(self, "_moment_detector", None)
        if detector is None:
            return

        try:
            from app.core.shared_moment_extractor import detect_moment_reaction_tags

            reaction_signal = bool(
                detect_moment_reaction_tags(raw_assistant_text or "")
            )
        except Exception:
            reaction_signal = False

        gift_signal = bool(self._last_turn_gift_received)
        promise_kept_signal = bool(self._last_turn_promise_kept)
        milestone_signal = bool(milestone)
        now_monotonic = time.monotonic()
        try:
            should = detector.should_run_llm(
                reaction_signal=reaction_signal,
                milestone_signal=milestone_signal,
                gift_signal=gift_signal,
                promise_kept_signal=promise_kept_signal,
                now_monotonic=now_monotonic,
            )
        except Exception:
            log.debug("moment detector should_run failed", exc_info=True)
            return
        if not should:
            return

        session_key = self.session_key
        history_window = 10

        def _history_provider() -> list[tuple[str, str]]:
            try:
                rows = self._chat_db.get_messages(session_key, limit=history_window)
            except Exception:
                return []
            return [
                (str(r.role or ""), str(r.content or ""))
                for r in rows
                if r.role in ("user", "assistant")
            ]

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                detector.maybe_run_llm(
                    history_provider=_history_provider,
                    now_monotonic=time.monotonic(),
                    reaction_signal=reaction_signal,
                    milestone_signal=milestone_signal,
                    gift_signal=gift_signal,
                    promise_kept_signal=promise_kept_signal,
                )
            except Exception:
                log.debug("moment llm job raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="moment_llm",
                priority=75,
                estimated_seconds=3.5,
                callable=_job,
                dedupe_key="moment_llm",
            ))
        except Exception:
            log.debug("moment llm submit failed", exc_info=True)

    def _maybe_schedule_user_profile_job(self) -> None:
        """Phase 3a: enqueue UserProfileWorker via the speaking window."""
        worker = getattr(self, "_user_profile_worker", None)
        if worker is None:
            return
        try:
            if not worker.should_run():
                return
        except Exception:
            log.debug("profile worker should_run failed", exc_info=True)
            return

        session_key = self.session_key
        user_id = self._user_id
        history_window = 24

        def _history_provider() -> list[tuple[str, str]]:
            try:
                rows = self._chat_db.get_messages(session_key, limit=history_window)
            except Exception:
                return []
            return [
                (str(r.role or ""), str(r.content or ""))
                for r in rows
                if r.role in ("user", "assistant")
            ]

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                worker.maybe_run(
                    user_id,
                    session_key=session_key,
                    history_provider=_history_provider,
                )
            except Exception:
                log.debug("user profile job raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="user_profile",
                priority=60,
                estimated_seconds=4.0,
                callable=_job,
                dedupe_key="user_profile",
            ))
        except Exception:
            log.debug("user profile submit failed", exc_info=True)

    def _maybe_schedule_self_image_pulse(self) -> None:
        """Phase 2d: enqueue a daily self-image rebuild during TTS playback."""
        worker = getattr(self, "_self_image_worker", None)
        if worker is None:
            return
        try:
            if not worker.should_run():
                return
        except Exception:
            log.debug("self-image should_run check failed", exc_info=True)
            return

        def _job(_stop_flag: Any) -> None:
            if _stop_flag is not None and _stop_flag.is_set():
                return
            try:
                new_text = worker.pulse()
                if new_text:
                    log.info(
                        "self-image pulse wrote %d chars",
                        len(new_text),
                    )
            except Exception:
                log.debug("self-image pulse raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="self_image_pulse",
                priority=80,  # lowest — daily, not urgent
                estimated_seconds=5.0,
                callable=_job,
                dedupe_key="self_image_pulse",
            ))
        except Exception:
            log.debug("self-image pulse submit failed", exc_info=True)

    def _maybe_schedule_consolidator(self) -> None:
        """Phase 4b: enqueue the memory-consolidator pass."""
        worker = getattr(self, "_consolidator", None)
        if worker is None:
            return
        try:
            if not worker.should_run(self._user_id):
                return
        except Exception:
            log.debug("consolidator should_run failed", exc_info=True)
            return

        user_id = self._user_id

        def _job(stop_flag: Any) -> None:
            try:
                worker.maybe_run(user_id, stop_flag=stop_flag)
            except Exception:
                log.debug("consolidator job raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="memory_consolidator",
                priority=85,  # very low — daily-ish maintenance
                estimated_seconds=6.0,
                callable=_job,
                dedupe_key="memory_consolidator",
            ))
        except Exception:
            log.debug("consolidator submit failed", exc_info=True)

    def _maybe_schedule_arc_smoother(self) -> None:
        """Phase 4c: enqueue ArcSmootherWorker if it's due."""
        worker = getattr(self, "_arc_smoother", None)
        if worker is None:
            return
        try:
            if not worker.should_run():
                return
        except Exception:
            log.debug("arc smoother should_run failed", exc_info=True)
            return

        session_key = self.session_key
        user_id = self._user_id
        history_window = 12

        def _history_provider() -> list[tuple[str, str]]:
            try:
                rows = self._chat_db.get_messages(session_key, limit=history_window)
            except Exception:
                return []
            return [
                (str(r.role or ""), str(r.content or ""))
                for r in rows
                if r.role in ("user", "assistant")
            ]

        def _job(stop_flag: Any) -> None:
            if stop_flag is not None and stop_flag.is_set():
                return
            try:
                current_turn = self._chat_db.get_message_count(session_key)
            except Exception:
                current_turn = 0
            try:
                worker.maybe_run(
                    user_id,
                    history_provider=_history_provider,
                    current_turn=current_turn,
                )
            except Exception:
                log.debug("arc smoother job raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="arc_smoother",
                priority=72,
                estimated_seconds=3.5,
                callable=_job,
                dedupe_key="arc_smoother",
            ))
        except Exception:
            log.debug("arc smoother submit failed", exc_info=True)

    def _maybe_schedule_narrative_weaver(self) -> None:
        """Phase 4c: enqueue NarrativeWeaver to refill prepared_nudge."""
        worker = getattr(self, "_narrative_weaver", None)
        if worker is None:
            return
        try:
            if not worker.should_run(self._user_id):
                return
        except Exception:
            log.debug("narrative weaver should_run failed", exc_info=True)
            return

        user_id = self._user_id

        def _job(stop_flag: Any) -> None:
            if stop_flag is not None and stop_flag.is_set():
                return
            try:
                worker.maybe_run(user_id)
            except Exception:
                log.debug("narrative weaver job raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="narrative_weaver",
                priority=68,
                estimated_seconds=3.0,
                callable=_job,
                dedupe_key="narrative_weaver",
            ))
        except Exception:
            log.debug("narrative weaver submit failed", exc_info=True)

    def _maybe_schedule_relationship_pulse(self) -> None:
        """Phase 4b: enqueue the weekly relationship-pulse summary."""
        worker = getattr(self, "_relationship_pulse", None)
        if worker is None:
            return
        try:
            if not worker.should_run(self._user_id):
                return
        except Exception:
            log.debug("relationship pulse should_run failed", exc_info=True)
            return

        user_id = self._user_id

        def _job(stop_flag: Any) -> None:
            if stop_flag is not None and stop_flag.is_set():
                return
            try:
                worker.maybe_run(user_id)
            except Exception:
                log.debug("relationship pulse job raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="relationship_pulse",
                priority=82,
                estimated_seconds=5.5,
                callable=_job,
                dedupe_key="relationship_pulse",
            ))
        except Exception:
            log.debug("relationship pulse submit failed", exc_info=True)

    def _maybe_schedule_curiosity(
        self,
        *,
        user_text: str,
        assistant_text: str,
    ) -> None:
        """Phase 4c: enqueue a curiosity-follow-up pass.

        Mid-priority (75) so it lands between agenda (lower) and arc
        (higher). Internally throttled to ``min_turns_between`` /
        ``min_seconds_between`` and skips automatically when the arc
        isn't shallow.
        """
        worker = getattr(self, "_curiosity_worker", None)
        if worker is None:
            return
        store = getattr(self, "_arc_store", None)
        arc_label = "casual_check_in"
        if store is not None:
            try:
                state = store.get_or_default(self._user_id)
                arc_label = getattr(state, "arc", arc_label) or arc_label
            except Exception:
                log.debug("curiosity arc lookup failed", exc_info=True)
        session_key = self.session_key
        user_snap = (user_text or "")[:1000]
        asst_snap = (assistant_text or "")[:1000]

        def _job(stop_flag: Any) -> None:
            if stop_flag is not None and stop_flag.is_set():
                return
            try:
                worker.maybe_run(
                    session_key=session_key,
                    user_text=user_snap,
                    assistant_text=asst_snap,
                    arc_label=arc_label,
                    on_memory_added=self._notify_memory_added,
                )
            except Exception:
                log.debug("curiosity worker job raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="curiosity",
                priority=75,
                estimated_seconds=2.5,
                callable=_job,
                dedupe_key="curiosity",
            ))
        except Exception:
            log.debug("curiosity worker submit failed", exc_info=True)

    def _maybe_schedule_catchphrase_miner(self) -> None:
        """Phase 2c: enqueue the recurring-phrase miner.

        Low-priority (90) so it lands after the more reactive workers
        (reflection, narrative weaver). Internally throttled to one
        run per ``catchphrase_miner_min_seconds_between`` window.
        """
        miner = getattr(self, "_catchphrase_miner", None)
        if miner is None:
            return
        session_key = self.session_key

        def _job(stop_flag: Any) -> None:
            if stop_flag is not None and stop_flag.is_set():
                return
            try:
                miner.maybe_run(session_key=session_key)
            except Exception:
                log.debug("catchphrase miner job raised", exc_info=True)

        try:
            from app.core.speaking_window_scheduler import ScheduledJob

            self._scheduler.submit(ScheduledJob(
                name="catchphrase_miner",
                priority=90,
                estimated_seconds=2.5,
                callable=_job,
                dedupe_key="catchphrase_miner",
            ))
        except Exception:
            log.debug("catchphrase miner submit failed", exc_info=True)

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

    # ── Schema v8 revival detection (E2) ────────────────────────────

    # Tiny stopword list scoped to the revival overlap check. We only
    # need to suppress the most common "free" matches so a memory and
    # an assistant reply don't pass the >=3-word threshold purely on
    # filler. Not a full NLP pipeline -- the threshold itself does the
    # heavy lifting.
    _REVIVAL_STOPWORDS: frozenset[str] = frozenset({
        "the", "a", "an", "and", "or", "but", "if", "then", "so", "of",
        "in", "on", "at", "to", "for", "with", "by", "as", "is", "are",
        "was", "were", "be", "been", "being", "do", "does", "did", "have",
        "has", "had", "you", "your", "i", "me", "my", "we", "our", "us",
        "he", "she", "they", "them", "this", "that", "these", "those",
        "it", "its", "from", "about", "into", "than", "what", "when",
        "where", "who", "how", "why", "not", "no", "yes", "ok", "okay",
        "just", "really", "very", "much", "like", "would", "could",
        "should", "will", "can", "may", "might", "also", "too", "any",
        "all", "some", "more", "most", "less", "such", "there", "here",
        "now", "again", "still", "even", "only", "yet",
    })

    @classmethod
    def _revival_tokens(cls, text: str) -> set[str]:
        """Lowercase content-word set used by the keyword overlap check.

        Tokens shorter than 4 chars and items in :attr:`_REVIVAL_STOPWORDS`
        are dropped -- short / common words light up too many incidental
        overlaps to be useful as a revival signal.
        """
        if not text:
            return set()
        import re

        raw = re.findall(r"[A-Za-z][A-Za-z0-9'_-]+", str(text).lower())
        out: set[str] = set()
        for token in raw:
            token = token.strip("'-_")
            if len(token) < 4:
                continue
            if token in cls._REVIVAL_STOPWORDS:
                continue
            out.add(token)
        return out

    def _mark_revived_memories(self, *, assistant_text: str) -> None:
        """Reward memories Aiko actually cited in her reply with revival.

        Reads the most recent surfaced-IDs snapshot from the RAG
        retriever, runs the keyword-overlap check between the reply
        text and each surfaced memory's content, and calls
        :meth:`MemoryStore.mark_revived` on the qualifying ids. Skipped
        entirely when tiers are disabled or no memories surfaced.
        """
        if not assistant_text or not self._memory_settings.tiers_enabled:
            return
        store = self._memory_store
        if store is None:
            return
        retriever = getattr(self, "_rag_retriever", None)
        if retriever is None:
            return
        ids = getattr(retriever, "last_surfaced_memory_ids", None)
        if not ids:
            return
        threshold = max(1, int(self._memory_settings.revival_min_word_overlap))
        reply_tokens = self._revival_tokens(assistant_text)
        if len(reply_tokens) < threshold:
            return
        delta = float(self._memory_settings.revival_per_hit)
        if delta <= 0:
            return
        revived: list[int] = []
        for mem_id in ids:
            mem = store.get(int(mem_id))
            if mem is None:
                continue
            mem_tokens = self._revival_tokens(mem.content)
            if len(reply_tokens & mem_tokens) >= threshold:
                revived.append(int(mem_id))
        if revived:
            try:
                store.mark_revived(revived, delta=delta)
                log.info(
                    "revival: bumped %d memory revival_scores (delta=%.2f)",
                    len(revived), delta,
                )
            except Exception:
                log.debug("mark_revived failed", exc_info=True)

    def _post_turn_inner_life(
        self,
        *,
        user_text: str,
        reaction: str,
        assistant_text: str = "",
        raw_assistant_text: str = "",
    ) -> None:
        """Run all post-turn inner-life updates (cheap, no LLM).

        Currently:
          - AffectUpdater.apply_turn (POST-TURN)
          - mood_state WS broadcast
          - ReflectionWorker scheduling (Phase 2c) — submitted to the
            speaking window so the LLM call hides under TTS playback.

        More post-turn jobs (user-state estimator, promise regex, agenda
        regex) will hang off this method as the relevant phases land.
        """
        try:
            affect_before = self._affect_store.get(self._user_id)
        except Exception:
            log.debug("affect snapshot failed", exc_info=True)
            affect_before = None
        try:
            with self._vocal_tone_lock:
                tone = self._last_vocal_tone
            state = self._affect_updater.apply_turn(
                self._user_id,
                reaction=reaction,
                user_text=user_text,
                user_tone=tone,
            )
        except Exception:
            log.debug("affect updater failed", exc_info=True)
            return
        self._notify_mood_state({
            "label": state.mood_label,
            "intensity": float(state.mood_intensity),
            "valence": float(state.valence),
            "arousal": float(state.arousal),
            "circadian_period": self.current_circadian_period(),
            "resolved_outfit": self.resolve_auto_outfit(),
        })

        # Schema v8: bump revival_score on memories Aiko actually cited.
        # The RAG retriever stashed the surfaced IDs after its mark_used
        # pass; we compare the assistant reply's keyword set against each
        # memory's content and reward overlap above the configured floor.
        try:
            self._mark_revived_memories(assistant_text=assistant_text)
        except Exception:
            log.debug("memory revival mark failed", exc_info=True)

        # Phase 2c: schedule a reflection during TTS playback.
        worker = getattr(self, "_reflection_worker", None)
        if worker is not None:
            session_key = self.session_key
            user_snapshot = (user_text or "")[:1500]
            assistant_snapshot = (assistant_text or "")[:1500]
            reaction_snapshot = reaction or "neutral"
            affect_after = state

            def _job(_stop_flag: Any) -> None:
                # Honor cooperative cancel before the LLM call too.
                if _stop_flag is not None and _stop_flag.is_set():
                    return
                try:
                    worker.maybe_run(
                        session_key=session_key,
                        user_text=user_snapshot,
                        assistant_text=assistant_snapshot,
                        reaction=reaction_snapshot,
                        affect_before=affect_before,
                        affect_after=affect_after,
                        on_memory_added=self._notify_memory_added,
                    )
                except Exception:
                    log.debug("reflection job raised", exc_info=True)

            try:
                from app.core.speaking_window_scheduler import ScheduledJob

                self._scheduler.submit(ScheduledJob(
                    name="reflection",
                    priority=50,  # mid — reactive jobs (cancel) run sooner
                    estimated_seconds=4.0,
                    callable=_job,
                    dedupe_key="reflection",
                ))
            except Exception:
                log.debug("reflection job submit failed", exc_info=True)

        # Phase 2d: opportunistically schedule the daily self-image pulse.
        try:
            self._maybe_schedule_self_image_pulse()
        except Exception:
            log.debug("self-image schedule failed", exc_info=True)

        # Phase 3a: per-turn user-state heuristic (regex only, ~0.5ms).
        estimator = getattr(self, "_user_state_estimator", None)
        if estimator is not None:
            try:
                estimator.apply_turn(self._user_id, user_text=user_text)
            except Exception:
                log.debug("user-state estimator failed", exc_info=True)
        worker = getattr(self, "_user_profile_worker", None)
        if worker is not None:
            try:
                worker.notify_user_turn()
                self._maybe_schedule_user_profile_job()
            except Exception:
                log.debug("user-profile schedule failed", exc_info=True)

        # Phase 3b: bump turn counter + maybe surface a milestone callback.
        tracker = getattr(self, "_relationship_tracker", None)
        milestone: str | None = None
        if tracker is not None:
            try:
                _new_state, milestone = tracker.record_turn(self._user_id)
            except Exception:
                log.debug("relationship record_turn failed", exc_info=True)
                milestone = None
            if milestone:
                self._record_milestone_memory(milestone)
        self._last_turn_milestone = milestone

        # Phase 3c: post-turn promise regex (cheap) + maybe schedule LLM pass.
        extractor = getattr(self, "_promise_extractor", None)
        if extractor is not None:
            try:
                extractor.extract_post_turn(
                    user_text=user_text,
                    assistant_text=assistant_text,
                    session_key=self.session_key,
                )
                extractor.notify_user_turn()
                self._maybe_schedule_promise_llm_job()
            except Exception:
                log.debug("promise extraction failed", exc_info=True)

        # Phase 4a: inline [[agenda:...]] tags in raw assistant output.
        agenda_store = getattr(self, "_agenda_store", None)
        if agenda_store is not None and raw_assistant_text:
            try:
                from app.core.agenda import extract_inline_tags

                for goal_text, importance in extract_inline_tags(raw_assistant_text):
                    agenda_store.add(
                        self._user_id,
                        goal=goal_text,
                        importance=importance,
                        source_session=self.session_key,
                    )
            except Exception:
                log.debug("agenda inline extraction failed", exc_info=True)
        agenda_worker = getattr(self, "_agenda_worker", None)
        if agenda_worker is not None:
            try:
                agenda_worker.notify_user_turn()
                self._maybe_schedule_agenda_groom_job()
            except Exception:
                log.debug("agenda groom schedule failed", exc_info=True)

        # Phase 4c: hot-path arc estimator on the user turn.
        estimator = getattr(self, "_arc_estimator", None)
        smoother = getattr(self, "_arc_smoother", None)
        if estimator is not None:
            try:
                current_turn = self._chat_db.get_message_count(self.session_key)
            except Exception:
                current_turn = 0
            try:
                estimator.apply_turn(
                    self._user_id,
                    user_text=user_text,
                    current_turn=current_turn,
                )
            except Exception:
                log.debug("arc estimator failed", exc_info=True)
        if smoother is not None:
            try:
                smoother.notify_user_turn()
                self._maybe_schedule_arc_smoother()
            except Exception:
                log.debug("arc smoother schedule failed", exc_info=True)

        # Phase 4c: notify narrative weaver and maybe enqueue.
        weaver = getattr(self, "_narrative_weaver", None)
        if weaver is not None:
            try:
                weaver.notify_user_turn()
                self._maybe_schedule_narrative_weaver()
            except Exception:
                log.debug("narrative weaver schedule failed", exc_info=True)

        # Phase 4b: opportunistic maintenance jobs (consolidator + pulse).
        try:
            self._maybe_schedule_consolidator()
        except Exception:
            log.debug("consolidator schedule failed", exc_info=True)
        try:
            self._maybe_schedule_relationship_pulse()
        except Exception:
            log.debug("relationship pulse schedule failed", exc_info=True)
        # Phase 2c (Aiko human-like upgrades): mine recurring phrases.
        try:
            self._maybe_schedule_catchphrase_miner()
        except Exception:
            log.debug("catchphrase miner schedule failed", exc_info=True)
        # Phase 4c: small follow-up question on shallow arcs.
        try:
            self._maybe_schedule_curiosity(
                user_text=user_text,
                assistant_text=assistant_text,
            )
        except Exception:
            log.debug("curiosity worker schedule failed", exc_info=True)

        # Schema v7: shared moments + relationship axes. Order matters —
        # extract inline tags first so the axes updater sees their vibes.
        moment_vibes_this_turn: list[str] = []
        moments_store = getattr(self, "_shared_moments_store", None)
        if (
            moments_store is not None
            and raw_assistant_text
            and bool(getattr(self._settings.agent, "shared_moments_enabled", True))
        ):
            try:
                from app.core.shared_moment_extractor import extract_inline_tags

                for candidate in extract_inline_tags(raw_assistant_text):
                    row = moments_store.add_from_candidate(
                        candidate,
                        source_session=self.session_key,
                    )
                    if row is not None:
                        moment_vibes_this_turn.append(row.vibe)
                        detector = getattr(self, "_moment_detector", None)
                        if detector is not None:
                            try:
                                detector.note_tag_persisted()
                            except Exception:
                                pass
                        self._notify_shared_moment_added(row)
            except Exception:
                log.debug("shared-moment inline extraction failed", exc_info=True)
        self._last_turn_moment_vibes = moment_vibes_this_turn

        # F2: inline [[gap:topic:question]] tags. Same shape as the
        # moments extraction above — pure regex over the raw assistant
        # text, ``prune_overflow`` keeps the cap honoured.
        gap_store = getattr(self, "_knowledge_gap_store", None)
        if gap_store is not None and raw_assistant_text:
            try:
                from app.core.knowledge_gap_extractor import (
                    extract_inline_tags as _extract_gaps,
                )

                for candidate in _extract_gaps(raw_assistant_text):
                    gap = gap_store.add_gap(
                        topic=candidate.topic,
                        question=candidate.question,
                        source_session=self.session_key,
                    )
                    if gap is not None:
                        self._notify_knowledge_gap_added(gap)
            except Exception:
                log.debug("knowledge gap inline extraction failed", exc_info=True)

        # F5: inline [[conflict:reason]] self-tag. Aiko emits this when
        # she notices a memory contradiction mid-turn ("hold on, that
        # doesn't match what you told me last week"). We log the
        # reason for audit and force_run the F5 worker so the
        # conflict surfaces in the next idle window even if it's
        # outside the regular cadence. The cosine band + heuristic
        # gate still filters the candidate pairs -- we don't try to
        # attribute the tag to a specific (a, b) here.
        conflict_worker = getattr(self, "_memory_conflict_worker", None)
        if conflict_worker is not None and raw_assistant_text:
            try:
                from app.core.services.response_text_service import (
                    extract_conflict_tags,
                )

                tags = extract_conflict_tags(raw_assistant_text)
                if tags:
                    log.info(
                        "F5 self-flag: aiko reported %d conflict reason(s): %s",
                        len(tags),
                        [t[:120] for t in tags],
                    )
                    scheduler = getattr(self, "_idle_scheduler", None)
                    if scheduler is not None:
                        try:
                            scheduler.force_run(conflict_worker.name)
                        except Exception:
                            log.debug(
                                "F5 force_run failed", exc_info=True,
                            )
            except Exception:
                log.debug(
                    "conflict-tag inline extraction failed", exc_info=True,
                )

        # K2: inline [[predict:kind:topic:state:confidence]] self-tag.
        # Aiko's theory-of-mind prediction about the user gets parsed
        # here and upserted into the BeliefStore. We optionally embed
        # the topic so the store can fuzzy-merge near-duplicates on
        # the next upsert. The gap detector pass below picks up the
        # fresh row if its mood prediction disagrees with the live
        # affect read.
        belief_store = getattr(self, "_belief_store", None)
        if (
            belief_store is not None
            and raw_assistant_text
            and bool(getattr(self._settings.agent, "belief_tracking_enabled", True))
        ):
            try:
                from app.core.services.response_text_service import (
                    extract_predict_tags,
                )

                tags = extract_predict_tags(raw_assistant_text)
                if tags:
                    log.info(
                        "K2 self-flag: aiko predicted %d belief(s)",
                        len(tags),
                    )
                    embedder = getattr(self, "_embedder", None)
                    for t in tags:
                        embedding = None
                        if embedder is not None:
                            try:
                                embedding = embedder.embed(t.topic)
                            except Exception:
                                log.debug(
                                    "K2 embed topic failed",
                                    exc_info=True,
                                )
                        try:
                            belief = belief_store.upsert(
                                user_id=self._user_id,
                                kind=t.kind,
                                topic=t.topic,
                                predicted_state=t.predicted_state,
                                confidence=t.confidence,
                                source="self_tag",
                                topic_embedding=embedding,
                            )
                            if belief is not None:
                                log.info(
                                    "K2 belief from tag: id=%s kind=%s "
                                    "topic=%r state=%r confidence=%.2f",
                                    belief.id,
                                    belief.kind,
                                    belief.topic,
                                    belief.predicted_state,
                                    belief.confidence,
                                )
                                self._notify_belief_added(belief.to_payload())
                        except Exception:
                            log.debug(
                                "K2 upsert from tag raised", exc_info=True,
                            )
            except Exception:
                log.debug(
                    "predict-tag inline extraction failed", exc_info=True,
                )

        # K2: post-turn gap detector pass. Compares active mood
        # beliefs against the live affect read and active opinion
        # beliefs against the user's most recent message. Surfaced
        # gaps are stashed for the next-turn ``_render_belief_gaps_block``
        # provider to consume.
        gap_detector = getattr(self, "_belief_gap_detector", None)
        if (
            gap_detector is not None
            and bool(getattr(self._settings.agent, "belief_tracking_enabled", True))
        ):
            try:
                affect_store = getattr(self, "_affect_store", None)
                affect = (
                    affect_store.get(self._user_id)
                    if affect_store is not None
                    else None
                )
                gaps = gap_detector.detect(
                    user_id=self._user_id,
                    affect=affect,
                    recent_user_message=user_text,
                )
                if gaps:
                    self._pending_belief_gaps = list(gaps)
                    # Mirror the per-row contradiction flips out to
                    # listeners so the UI's Beliefs sub-tab can
                    # refresh without polling.
                    for g in gaps:
                        try:
                            row = belief_store.get(g.belief_id) if belief_store else None
                            if row is not None:
                                self._notify_belief_updated(row.to_payload())
                        except Exception:
                            log.debug(
                                "K2 notify_belief_updated raised",
                                exc_info=True,
                            )
            except Exception:
                log.debug("belief gap detector raised", exc_info=True)

        # Apply per-turn drift to the relationship axes. Cheap (no LLM).
        axes_updater = getattr(self, "_relationship_axes_updater", None)
        if (
            axes_updater is not None
            and bool(getattr(self._settings.agent, "relationship_axes_enabled", True))
        ):
            try:
                from app.core.shared_moment_extractor import (
                    detect_moment_reaction_tags,
                )

                reaction_tag_set = detect_moment_reaction_tags(raw_assistant_text or "")
                if reaction:
                    reaction_tag_set.add(str(reaction).lower())
                axes_state = axes_updater.apply_turn(
                    self._user_id,
                    reaction_tags=reaction_tag_set,
                    moment_vibes=moment_vibes_this_turn,
                    milestone=milestone,
                    gift_received=bool(self._last_turn_gift_received),
                    promise_kept=bool(self._last_turn_promise_kept),
                    user_text=user_text,
                )
                # Reset per-turn flags now that they've been consumed.
                self._last_turn_gift_received = False
                self._last_turn_promise_kept = False
                self._maybe_notify_axes(axes_state)
            except Exception:
                log.debug("relationship axes update failed", exc_info=True)

        # Schedule the LLM moment detector when a moment-worthy signal
        # fired AND cadence allows. Detector internally throttles further.
        detector = getattr(self, "_moment_detector", None)
        if (
            detector is not None
            and moments_store is not None
            and bool(getattr(self._settings.agent, "shared_moments_enabled", True))
            and bool(getattr(self._settings.agent, "shared_moments_llm_enabled", True))
        ):
            try:
                detector.notify_user_turn()
                self._maybe_schedule_moment_llm_job(
                    user_text=user_text,
                    assistant_text=assistant_text,
                    raw_assistant_text=raw_assistant_text,
                    milestone=milestone,
                )
            except Exception:
                log.debug("moment detector schedule failed", exc_info=True)

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
            from app.core.vocal_tone import analyse_wav

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
        if merge_text is not None and merge_user_message_id is not None:
            log.info(
                "voice merge: restarting turn with combined text "
                "(user_msg_id=%d combined_chars=%d)",
                merge_user_message_id, len(merge_text),
            )
            response = self.chat_once_streaming(
                user_text=merge_text,
                on_token=on_token,
                stop_requested=stop_requested,
                on_generation_status=on_generation_status,
                mode="live",
                capture_ms=capture_ms,
                stt_ms=stt_ms,
                _resume_message_id=merge_user_message_id,
            )
            return merge_text, response

        response = self.chat_once_streaming(
            user_text=text,
            on_token=on_token,
            stop_requested=stop_requested,
            on_generation_status=on_generation_status,
            mode="live",
            capture_ms=capture_ms,
            stt_ms=stt_ms,
        )
        return text, response

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


