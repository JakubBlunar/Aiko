"""Voice I/O mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
client-side audio I/O surface (mic source, frame feed, listeners),
VAD/STT settings, the TTS provider/voice surface + runtime prewarm,
and the STT-partial / backchannel / mood-state listener wiring. State
ownership stays on ``SessionController.__init__``.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for any moved method must patch
``app.core.session.voice_mixin.<symbol>`` instead."""
from __future__ import annotations

import logging
import threading
from typing import Any
from app.core.conversation.backchannel_classifier import BackchannelHint
from collections.abc import Callable
from app.audio.client_mic_source import ClientMicSource
from app.llm.ollama_client import OllamaClient
from app.stt.realtime_stt_service import RealtimeSttService
from app.core.voice.tts_queue import TtsQueue
from app.core.session.session_text_utils import infer_tts_reaction
from app.core.session.session_text_utils import prepare_tts_text
import time


log = logging.getLogger("app.session")


class VoiceMixin:
    """Audio I/O + VAD/STT settings + TTS/prewarm + STT partials/backchannel."""

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
        # Tear down the outgoing recorder's subprocesses before swapping
        # in the new one — otherwise the old transcription/reader children
        # are orphaned and spin on a broken pipe, flooding the log. Run in
        # a daemon thread so a slow join can't block the settings change;
        # shutdown() sets the shared event synchronously so the children
        # stop regardless.
        old = self._realtime_stt
        self._realtime_stt = candidate
        if old is not None:
            try:
                threading.Thread(target=old.shutdown, daemon=True).start()
            except Exception:
                log.debug("old STT recorder shutdown failed", exc_info=True)
        return True

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
        if self._worker_client_inner is self._chat_client:
            return
        if not isinstance(self._worker_client_inner, OllamaClient):
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

    def add_mood_state_listener(
        self, callback: Callable[[dict[str, Any]], None],
    ) -> None:
        if callback and callback not in self._mood_listeners:
            self._mood_listeners.append(callback)

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
