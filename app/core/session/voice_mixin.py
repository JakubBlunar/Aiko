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
        previous_loaded = bool(getattr(self._realtime_stt, "is_loaded", False))
        self._settings.stt.model = normalized
        candidate = RealtimeSttService(self._settings.stt, self._settings.audio)
        if not candidate.is_available:
            log.warning("STT unavailable, cannot switch model: %s", normalized)
            return False
        # P27: construction no longer validates the model name, because it
        # no longer loads anything. Only force the load here when the
        # outgoing service already had weights resident — i.e. voice is in
        # use, so the swap has to be verified now rather than failing on
        # the user's next utterance. Editing the setting with voice cold
        # stays free.
        if previous_loaded and not candidate.prewarm():
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
        """Providers that could actually be selected right now.

        Availability is probed from the filesystem, never by importing --
        Chatterbox pins ``torch==2.6.0`` against this app's ``2.10.0``, so
        "try it and see" is not an option that exists.
        """
        from app.tts import registry

        names = registry.usable_names()
        # The current selection stays listed even if it just became
        # unusable, so the UI can show what is configured rather than
        # silently appearing to be set to something else.
        current = self.tts_provider
        if current and current not in names:
            names.append(current)
        return names

    def describe_tts_providers(self) -> list[dict]:
        """The full catalogue with availability and reasons, for the UI.

        Unavailable engines are returned rather than filtered out: a
        greyed-out row saying "run this command to install" is far more
        use than an option that never appears.
        """
        from app.tts import registry

        return registry.describe()

    def get_tts_device(self) -> str:
        """The device the *current* provider is configured to use."""
        from app.tts import registry

        provider = self.tts_provider
        return registry.resolve_device(
            provider, self._settings.tts.for_provider(provider).device
        )

    def set_tts_device(self, device: str) -> str:
        """Set the current provider's device, rebuilding if it changed.

        Per-provider rather than global because the right answer differs
        by engine: pocket-tts is CPU-only, Nano is real-time on CPU and
        spending VRAM on it buys nothing, Turbo cannot reach real time
        without a GPU.
        """
        from app.core.infra.settings import TtsProviderSettings
        from app.tts import registry

        provider = self.tts_provider
        want = (device or "auto").strip().lower()
        if want not in {"auto", "cpu", "cuda"}:
            return self.get_tts_device()
        current = self._settings.tts.for_provider(provider)
        if want == current.device:
            return self.get_tts_device()
        self._settings.tts.providers[provider] = TtsProviderSettings(
            voice=current.voice, device=want,
        )
        resolved = registry.resolve_device(provider, want)
        try:
            self._tts.stop()
        except Exception:
            pass
        self._rebuild_tts_engine()
        self._trace("tts.device", f"{provider} now on {resolved}")
        return resolved

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
        from app.core.infra.settings import TtsProviderSettings

        provider = self.tts_provider
        current = self._settings.tts.for_provider(provider)
        # Recorded against the provider as well as flat, so a round trip
        # pocket-tts -> chatterbox -> pocket-tts comes back to the voice
        # that was set rather than to the default. A voice is not portable
        # between engines -- one wants a .safetensors embedding, the other
        # a reference clip -- so one shared field could only ever be
        # correct for whichever engine was selected last.
        self._settings.tts.providers[provider] = TtsProviderSettings(
            voice=normalized, device=current.device,
        )
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

    def set_tts_enabled(self, enabled: bool) -> bool:
        """Flip TTS on/off at runtime, loading or freeing the weights (P28).

        The old path only called ``TtsQueue.set_enabled``, which stops
        playback but leaves the model resident — so "turn TTS off to free
        memory" did nothing. Now:

        * **off** — release the voice weights (the PyTorch runtime itself
          stays imported; only a restart with ``tts.enabled=false``
          avoids that).
        * **on** — upgrade a :class:`NullTtsService` to the real engine, or
          re-load a released model. The load runs on the engine's own
          daemon thread, so this returns immediately and the first
          utterance waits on it.

        Returns the resulting enabled state.
        """
        want = bool(enabled)
        self._settings.tts.enabled = want
        try:
            self._tts.set_enabled(want)
        except Exception:
            log.debug("tts queue enable toggle failed", exc_info=True)
        engine = self._tts_engine
        if not want:
            release = getattr(engine, "release_model", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    log.debug("tts model release failed", exc_info=True)
            return False
        if getattr(engine, "is_null_engine", False):
            # Boot happened with TTS off, so there is no real engine yet.
            # Rebuilding also rewires the PCM listener + queue + prosody.
            self._rebuild_tts_engine()
            return True
        load = getattr(engine, "load_model_now", None)
        if callable(load):
            try:
                load()
            except Exception:
                log.debug("tts model reload failed", exc_info=True)
        return True

    def _rebuild_tts_engine(self) -> None:
        """Construct a fresh engine and rewire everything hanging off it.

        Shared by :meth:`set_tts_provider` (different engine wanted) and
        :meth:`set_tts_enabled` (no real engine exists yet). Releases the
        outgoing engine's weights first — P40: the swap used to drop the
        old reference without shutting it down.
        """
        old = getattr(self, "_tts_engine", None)
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
        if old is not None and old is not self._tts_engine:
            release = getattr(old, "release_model", None)
            if callable(release):
                try:
                    release()
                except Exception:
                    log.debug("outgoing tts engine release failed", exc_info=True)

    def set_tts_provider(self, provider: str) -> None:
        from app.tts import registry

        normalized = (
            (provider or "").strip().lower() or registry.DEFAULT_PROVIDER
        )
        if normalized == self.tts_provider:
            return
        # Checked before the swap rather than after. ``build_with_fallback``
        # would keep her talking either way, but silently landing on
        # pocket-tts while the setting reads "chatterbox-turbo" is the
        # kind of state that wastes an hour of listening for a difference
        # that was never applied.
        usable, reason = registry.availability(normalized)
        if not usable:
            log.warning("refusing TTS provider %s: %s", normalized, reason)
            self._trace("tts.provider", f"{normalized} unavailable: {reason}")
            return
        try:
            self._tts.stop()
        except Exception:
            pass
        self._settings.tts.provider = normalized
        self._rebuild_tts_engine()
        device = self.get_tts_device()
        self._trace(
            "tts.provider",
            f"Switched TTS provider to {normalized} on {device}",
        )

    def prewarm_tts(self) -> None:
        # P28: with TTS off there is nothing to warm, and on the real
        # engine ``warmup_sync`` blocks up to 60s waiting for a load we
        # deliberately never started.
        if not bool(getattr(self._settings.tts, "enabled", True)):
            return
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

    def prewarm_stt(self, *, background: bool = True) -> bool:
        """Start loading the STT model, if it isn't loaded already (P27).

        Deliberately **not** called from :meth:`prewarm_runtime`. Boot is
        exactly the moment we're trying to stop paying for Whisper: a
        text-only session should never load it. This is the voice-enable
        hook instead — the WS ``voice_start`` handler calls it, so the
        multi-second load overlaps with the user releasing the mic button
        rather than landing inside their first sentence.

        Returns True when a load was started or the model is already
        resident; False when STT can't load at all (disabled, or the
        engine isn't installed).
        """
        stt = getattr(self, "_realtime_stt", None)
        if stt is None or not stt.is_available:
            return False
        if getattr(stt, "is_loaded", False):
            return True
        if not background:
            return bool(stt.prewarm())
        # A daemon thread rather than the caller's: on the WS path the
        # caller is the event loop, and a blocking model load there stalls
        # every other client.
        threading.Thread(
            target=self._prewarm_stt_blocking, daemon=True, name="stt-prewarm",
        ).start()
        return True

    def _prewarm_stt_blocking(self) -> None:
        try:
            t0 = time.monotonic()
            ok = self._realtime_stt.prewarm()
            log.info(
                "STT prewarm %s: load_ms=%.0f",
                "ok" if ok else "failed",
                (time.monotonic() - t0) * 1000.0,
            )
        except Exception:
            log.debug("stt prewarm failed", exc_info=True)

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
                # Non-fatal on purpose. A fresh install has Ollama
                # running but nothing pulled yet; raising here would
                # kill boot before the browser can reach the onboarding
                # flow that offers to pull the model. Record it instead
                # and let the UI drive the recovery.
                self._missing_chat_model = effective
                report(
                    f"Chat model not installed: {effective} "
                    "(pull it from the setup screen or run "
                    f"'ollama pull {effective}')",
                )
                log.warning(
                    "chat model %s is not installed on %s; skipping warmup",
                    effective,
                    getattr(self._chat_client, "base_url", "?"),
                )
                self._prewarm_embedder(report)
                report("Warming TTS models...")
                self.prewarm_tts()
                report("Warmup complete (chat model missing)")
                return
            self._missing_chat_model = ""
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
        * Worker client is not an :class:`OllamaClient` instance — the
          ``worker_default`` route points at a remote provider, so
          there's nothing local to warm.
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
        # Source ``num_ctx`` from the worker route — the same value
        # :class:`OllamaClient._default_options` falls back to. Passing
        # it explicitly here is belt-and-braces: the kv-cache MUST be
        # sized correctly on the FIRST call, otherwise Ollama loads the
        # model at its built-in default (often 256k tokens) and a
        # subsequent worker call with a smaller ``num_ctx`` triggers a
        # full model reload — exactly the pathology you see in
        # ``ollama ps`` as a CPU/GPU split.
        worker_options: dict[str, object] = {}
        _, worker_ctx = self._worker_route_model_ctx()
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
