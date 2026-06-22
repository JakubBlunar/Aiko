"""Voice-capture pipeline mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
microphone capture / STT / live-phrase pipeline: ``record_and_chat``,
``listen_once_and_chat``, ``capture_live_phrase``, ``capture_ptt_phrase``,
``process_live_capture`` and ``run_stt_diagnostic``. State ownership
stays on ``SessionController.__init__``.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for any moved method must patch
``app.core.session.voice_capture_mixin.<symbol>`` instead."""
from __future__ import annotations

import logging
from typing import Any
from collections.abc import Callable
from pathlib import Path
from app.stt import endpointing as _endpointing
from app.core.session.session_text_utils import sanitize_user_text
import time


log = logging.getLogger("app.session")


class VoiceCaptureMixin:
    """Mic capture + STT + live/PTT phrase pipeline."""

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
