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

from app.audio.earcons import EarconPlayer
from app.audio.mic_capture import MicrophoneCapture, list_output_devices
from app.core.affect_state import AffectStore, AffectUpdater
from app.core.backchannel_classifier import BackchannelGate, BackchannelHint
from app.core.chat_database import ChatDatabase
from app.core import circadian as _circadian
from app.core.crash_logging import log_event
from app.core.memory_extractor import MemoryExtractor
from app.core.memory_retriever import MemoryRetriever
from app.core.memory_store import MemoryStore
from app.core.persona_manager import PersonaManager
from app.core.proactive_director import ProactiveDirector
from app.core.prompt_assembler import PromptAssembler
from app.core.session_text_utils import (
    infer_tts_reaction,
    prepare_tts_text,
    sanitize_user_text,
)
from app.core.settings import AppSettings
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


# ── Controller ─────────────────────────────────────────────────────────


class SessionController:
    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self._user_id = (settings.assistant.user_id or "default").strip() or "default"
        self._session_id = "main"

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

        # ── Live2D persona manager ───────────────────────────────────────
        personas_root = Path(__file__).resolve().parents[2] / "data" / "personas"
        self._persona_manager = PersonaManager(personas_root)

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

        # ── Long-term memory (cross-session) ─────────────────────────────
        self._memory_settings = settings.memory
        self._embedder: Embedder | None = None
        self._memory_store: MemoryStore | None = None
        self._memory_retriever: MemoryRetriever | None = None
        self._memory_extractor: MemoryExtractor | None = None
        self._memory_listeners: list[Callable[[Any], None]] = []
        # RAG: LanceDB-backed retrieval substrate. Owned by SessionController
        # so it can be shared with MessageIndexer and DocumentIngestor.
        self._rag_store = None  # type: ignore[var-annotated]
        if self._memory_settings.enabled:
            try:
                self._embedder = Embedder(settings.ollama)
                self._memory_store = MemoryStore(
                    storage_path,
                    max_memories=self._memory_settings.max_memories,
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

        # ── TTS engine + queue ───────────────────────────────────────────
        self._output_device = getattr(settings.audio, "output_device", None)
        self._tts_engine = self._build_tts_service(
            settings, output_device=self._output_device,
        )
        self._tts = TtsQueue(
            self._tts_engine,
            enabled=bool(settings.tts.enabled),
            state_listener=self._on_tts_state,
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
        self._microphone = MicrophoneCapture(settings.audio)
        self._microphone_device = settings.audio.microphone_device
        self._earcons = EarconPlayer(
            enabled=getattr(settings.audio, "earcons_enabled", True),
            output_device=self._output_device,
        )
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
                )
                self._prompt_assembler.set_rag_prefetch_lookup(
                    self._lookup_prefetched_rag_block,
                )
            except Exception:
                log.warning("RagPrefetcher init failed", exc_info=True)
                self._rag_prefetcher = None

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
            )
        except Exception:
            log.warning("ReflectionWorker init failed", exc_info=True)
            self._reflection_worker = None

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
                )
            except Exception:
                log.warning("RelationshipPulseWorker init failed", exc_info=True)
                self._relationship_pulse = None
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
        )
        self._prompt_assembler.set_pinned_self_memories_provider(
            self._top_pinned_self_memories,
        )

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
        # Slow background decay so unused memories drift down over weeks. We
        # also opportunistically prune so the store doesn't unbounded-grow.
        self._memory_decay_stop = threading.Event()
        self._memory_decay_thread: threading.Thread | None = None
        if self._memory_store is not None:
            self._memory_decay_thread = threading.Thread(
                target=self._memory_decay_loop,
                name="MemoryDecay",
                daemon=True,
            )
            self._memory_decay_thread.start()
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
            )
        except Exception:
            log.warning("PreparedNudgeStore/NarrativeWeaver init failed", exc_info=True)
            self._prepared_nudge_store = None
            self._narrative_weaver = None

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
            context_window=self._context_window,
            notify_message=self._notify_message,
            prepared_nudge_store=self._prepared_nudge_store,
            user_id=self._user_id,
        )

        # ── Runtime state ────────────────────────────────────────────────
        self._vad_level_threshold = settings.audio.vad_level_threshold
        self._vad_silence_seconds = settings.audio.vad_silence_seconds
        self._live_input_mode = getattr(settings.audio, "live_input_mode", None) or "voice_detection"
        self._live_ptt_type = getattr(settings.audio, "live_ptt_type", None) or "keyboard"
        self._live_ptt_key = getattr(settings.audio, "live_ptt_key", None)
        self._live_ptt_mouse_button = getattr(settings.audio, "live_ptt_mouse_button", None)
        self._live_ptt_toggle = getattr(settings.audio, "live_ptt_toggle", False)
        self._ptt_active = False
        self._live_no_speech_streak = 0
        self._live_voice_session_active = False
        self._turn_in_progress = False
        self._remember_history = settings.assistant.remember_history
        self._state = SessionState(
            mic_enabled=settings.audio.enable_microphone,
            session_type="chat",
        )
        self._decision_trace: deque[dict[str, str]] = deque(maxlen=500)

        # ── Metrics ──────────────────────────────────────────────────────
        self._last_metrics: dict[str, float | int | str] = self._zero_metrics()
        self._metrics_history: deque[dict[str, float | int | str]] = deque(maxlen=10)
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
        self._input_devices_cache: list[tuple[int, str]] | None = None
        self._input_devices_cache_time = 0.0
        self._output_devices_cache: list[tuple[int, str]] | None = None
        self._output_devices_cache_time = 0.0
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

    # ── State ─────────────────────────────────────────────────────────

    @property
    def state(self) -> SessionState:
        return self._state

    def update_sources(self, *, mic: bool) -> None:
        self._state.mic_enabled = bool(mic)

    @property
    def session_key(self) -> str:
        return f"{self._user_id}:{self._session_id}" if self._user_id else self._session_id

    def switch_session(self, session_id: str) -> None:
        # Drop any pending voice merge buffer; the new session starts
        # without an in-flight phrase A waiting for a continuation.
        self._clear_merge_buffer()
        self._session_id = session_id

    def new_session(self) -> str:
        new_id = str(uuid.uuid4())[:8]
        self.switch_session(new_id)
        return new_id

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

    # ── Audio: VAD / mic / output devices ───────────────────────────

    def list_microphone_devices(self, *, refresh: bool = False) -> list[tuple[int, str]]:
        now = time.monotonic()
        if not refresh and self._input_devices_cache is not None and (now - self._input_devices_cache_time) < self._cache_ttl:
            return list(self._input_devices_cache)
        devices = self._microphone.list_input_devices()
        self._input_devices_cache = list(devices)
        self._input_devices_cache_time = now
        return devices

    def set_microphone_device(self, device_index: int | None) -> None:
        self._microphone_device = device_index
        self._microphone.set_device(device_index)

    @property
    def microphone_device(self) -> int | None:
        return self._microphone_device

    def list_output_devices(self, *, refresh: bool = False) -> list[tuple[int, str]]:
        now = time.monotonic()
        if not refresh and self._output_devices_cache is not None and (now - self._output_devices_cache_time) < self._cache_ttl:
            return list(self._output_devices_cache)
        try:
            devices = list_output_devices()
        except Exception:
            devices = []
        self._output_devices_cache = list(devices)
        self._output_devices_cache_time = now
        return devices

    def set_output_device(self, device_index: int | None) -> None:
        self._output_device = device_index
        rebuild = getattr(self._tts_engine, "set_output_device", None)
        if callable(rebuild):
            try:
                rebuild(device_index)
            except Exception:
                log.debug("tts engine rejected device switch", exc_info=True)
        try:
            self._earcons = EarconPlayer(
                enabled=getattr(self._settings.audio, "earcons_enabled", True),
                output_device=device_index,
            )
        except Exception:
            log.debug("earcons rebuild failed", exc_info=True)

    @property
    def output_device(self) -> int | None:
        return self._output_device

    def barge_in_enabled(self) -> bool:
        return bool(getattr(self._settings.audio, "barge_in_enabled", False))

    def set_barge_in_enabled(self, enabled: bool) -> None:
        self._settings.audio.barge_in_enabled = bool(enabled)

    @property
    def live_input_mode(self) -> str:
        return self._live_input_mode

    def set_live_input_mode(self, mode: str) -> None:
        normalized = (mode or "").strip().lower()
        if normalized:
            self._live_input_mode = normalized

    @property
    def live_ptt_type(self) -> str:
        return self._live_ptt_type

    def set_live_ptt_type(self, ptt_type: str) -> None:
        self._live_ptt_type = (ptt_type or "keyboard").strip().lower() or "keyboard"

    @property
    def live_ptt_key(self) -> str | None:
        return self._live_ptt_key

    def set_live_ptt_key(self, key: str | None) -> None:
        self._live_ptt_key = (key or None) and str(key).strip()

    @property
    def live_ptt_mouse_button(self) -> str | None:
        return self._live_ptt_mouse_button

    def set_live_ptt_mouse_button(self, button: str | None) -> None:
        self._live_ptt_mouse_button = (button or None) and str(button).strip().lower()

    @property
    def live_ptt_toggle(self) -> bool:
        return bool(self._live_ptt_toggle)

    def set_live_ptt_toggle(self, value: bool) -> None:
        self._live_ptt_toggle = bool(value)

    def get_ptt_active(self) -> bool:
        return self._ptt_active

    def set_ptt_active(self, active: bool) -> None:
        self._ptt_active = bool(active)

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
        self._tts_engine = self._build_tts_service(
            self._settings, output_device=self._output_device,
        )
        self._tts = TtsQueue(
            self._tts_engine,
            enabled=bool(self._settings.tts.enabled),
            state_listener=self._on_tts_state,
            amplitude_listener=self._on_tts_amplitude,
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
        self._live_voice_session_active = bool(active)
        self._state.session_type = "live" if active else "chat"

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

    # ── Persona ─────────────────────────────────────────────────────

    @property
    def persona_manager(self) -> PersonaManager:
        return self._persona_manager

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
        except Exception:
            log.warning("tool registry build failed", exc_info=True)
        self._tool_registry = registry
        if hasattr(self, "_turn_runner"):
            self._turn_runner.set_tool_registry(registry)
        log.info("tool registry rebuilt: %s", registry.names())

    # ── Memory accessors ────────────────────────────────────────────

    @property
    def memory_store(self) -> "MemoryStore | None":
        return self._memory_store

    @property
    def memory_extractor(self) -> "MemoryExtractor | None":
        return self._memory_extractor

    def list_memories(
        self,
        *,
        limit: int = 50,
        order: str = "recent",
    ) -> list[dict[str, Any]]:
        store = self._memory_store
        if store is None:
            return []
        if order == "top":
            mems = store.list_top(limit=limit)
        else:
            mems = store.list_recent(limit=limit)
        return [m.to_dict() for m in mems]

    def delete_memory(self, memory_id: int) -> bool:
        if self._memory_store is None:
            return False
        return self._memory_store.delete(int(memory_id))

    def add_memory_listener(self, callback: Callable[[Any], None]) -> None:
        if callback and callback not in self._memory_listeners:
            self._memory_listeners.append(callback)

    def _notify_memory_added(self, memory: Any) -> None:
        for listener in list(self._memory_listeners):
            try:
                listener(memory)
            except Exception:
                log.debug("memory listener raised", exc_info=True)

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
    def _zero_metrics() -> dict[str, float | int | str]:
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
        }

    def get_last_metrics(self) -> dict[str, float | int | str]:
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
            # by a prior live phrase that hasn't completed cleanly.
            self._clear_merge_buffer(merge_key)

        self._turn_in_progress = True
        t0 = time.perf_counter()
        try:
            tts_chunk_cb = None
            if bool(self._settings.tts.enabled):
                prosody = getattr(self, "_prosody", None)
                tts_chunk_cb = (
                    prosody.dispatch if prosody is not None else self._tts.enqueue
                )

            wrapped_tts_cb = self._wrap_tts_chunk_for_merge(
                tts_chunk_cb, merge_key,
            ) if mode == "live" and tts_chunk_cb is not None else tts_chunk_cb

            result = self._turn_runner.run(
                session_key,
                cleaned,
                on_token=on_token,
                on_tts_chunk=wrapped_tts_cb,
                stop_requested=stop_requested,
                resume_user_message_id=user_message_id,
            )
        finally:
            self._turn_in_progress = False
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

        metrics: dict[str, float | int | str | bool] = {
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
            })
        self._set_last_metrics(metrics)
        return result.text

    def _set_last_metrics(
        self, metrics: dict[str, float | int | str | bool],
    ) -> None:
        self._last_metrics = dict(metrics)  # type: ignore[arg-type]
        self._metrics_history.append(dict(metrics))  # type: ignore[arg-type]

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
        return ctx

    def _render_user_profile_block(self) -> str:
        """Phase 3a: bullet block of the high-confidence profile fields."""
        store = getattr(self, "_user_profile_store", None)
        if store is None:
            return ""
        try:
            return store.render_block(self._user_id)
        except Exception:
            log.debug("user profile block render failed", exc_info=True)
            return ""

    def _render_user_state_block(self) -> str:
        """Phase 3a: tiny per-turn 'Right now Jacob...' line."""
        store = getattr(self, "_user_state_store", None)
        if store is None:
            return ""
        try:
            return store.render_block(self._user_id)
        except Exception:
            log.debug("user state block render failed", exc_info=True)
            return ""

    def _render_relationship_block(self) -> str:
        """Phase 3b: short ambient block about how long we've known Jacob."""
        tracker = getattr(self, "_relationship_tracker", None)
        if tracker is None:
            return ""
        try:
            return tracker.ambient_line(self._user_id)
        except Exception:
            log.debug("relationship block render failed", exc_info=True)
            return ""

    def _render_agenda_block(self) -> str:
        """Phase 4a: open agenda items as a small bullet block."""
        store = getattr(self, "_agenda_store", None)
        if store is None:
            return ""
        try:
            return store.render_block(self._user_id)
        except Exception:
            log.debug("agenda block render failed", exc_info=True)
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
            return store.render_block(self._user_id, current_turn=current_turn)
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
            f"Aiko reached a milestone with Jacob: {humanized}. "
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
            state = self._affect_updater.apply_turn(
                self._user_id,
                reaction=reaction,
                user_text=user_text,
            )
        except Exception:
            log.debug("affect updater failed", exc_info=True)
            return
        self._notify_mood_state({
            "label": state.mood_label,
            "intensity": float(state.mood_intensity),
            "valence": float(state.valence),
            "arousal": float(state.arousal),
        })

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
        if tracker is not None:
            try:
                _new_state, milestone = tracker.record_turn(self._user_id)
            except Exception:
                log.debug("relationship record_turn failed", exc_info=True)
                milestone = None
            if milestone:
                self._record_milestone_memory(milestone)

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
    def _build_tts_service(settings: AppSettings, output_device: int | None = None) -> Any:
        # Lean v1 ships only pocket-tts (matches the active user.json config).
        # Kokoro / PyKokoro were removed -- restore them in v2 if needed.
        from app.tts.pocket_tts_service import PocketTtsService
        return PocketTtsService(settings.tts, output_device=output_device)

    # ── Memory decay daemon ─────────────────────────────────────────

    def _memory_decay_loop(self) -> None:
        """Tick once a day to gently decay salience and prune the store.

        Wakes every 60s so shutdown can interrupt promptly. The actual decay
        only fires once 24h have elapsed since the last tick.
        """
        store = self._memory_store
        if store is None:
            return
        interval_seconds = 24 * 60 * 60
        last_tick = time.monotonic()
        while not self._memory_decay_stop.wait(60.0):
            now = time.monotonic()
            if now - last_tick < interval_seconds:
                continue
            last_tick = now
            try:
                # Small daily decrement; matches the 0.02/day default in
                # MemoryStore.decay's signature.
                store.decay(by=0.02)
            except Exception:
                log.debug("memory decay failed", exc_info=True)
            try:
                pruned = store.prune()
                if pruned:
                    log.info("memory decay: pruned %d low-salience memories", pruned)
            except Exception:
                log.debug("memory prune failed", exc_info=True)

    # ── Shutdown ────────────────────────────────────────────────────

    def shutdown(self) -> None:
        # Clear the voice merge buffer first so a tail-end partial that
        # races shutdown can't try to call ``request_stop()`` on a
        # half-torn-down ``TurnRunner``.
        try:
            self._clear_merge_buffer()
        except Exception:
            log.debug("merge buffer clear on shutdown failed", exc_info=True)
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
        try:
            self._memory_decay_stop.set()
            if self._memory_decay_thread is not None:
                self._memory_decay_thread.join(timeout=1.5)
        except Exception:
            log.debug("memory decay stop failed", exc_info=True)
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


