"""Chat-turn mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
synchronous chat loop (``chat_once`` / ``chat_once_streaming``), the
per-turn metrics packing (``_set_last_metrics``), and the bootstrap-time
scheduling helpers that prime the next turn (dream-pass scheduling,
resume-opener, RAG prefetch lookup, prompt prebuild). State ownership
stays on ``SessionController.__init__``; these methods only read/write
``self.*``.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for any moved method must patch
``app.core.session.chat_turn_mixin.<symbol>`` instead."""
from __future__ import annotations

import logging
from typing import Any
from collections.abc import Callable
from app.core.session.merge_buffer import _MergeBuffer
from app.llm.token_utils import estimate_tokens
import json
from app.core.session.session_text_utils import sanitize_user_text
import time
from app.core.infra import timephrase


log = logging.getLogger("app.session")

# P16: post-turn cascade wall time above which the timing line escalates
# from DEBUG to INFO. The cascade is meant to be pure-Python + SQLite;
# anything approaching a second means something in it is doing real work
# on the turn thread.
_POST_TURN_SLOW_MS = 400.0

# P44: how far the per-block breakdown may be rescaled toward the
# provider's real prompt-token count. A healthy estimator lands within a
# few percent; anything beyond this band means the char heuristic is so
# far off that "correcting" it would be inventing numbers, so the raw
# estimate is shown instead and the discrepancy stays visible.
_MIN_BREAKDOWN_SCALE = 0.5
_MAX_BREAKDOWN_SCALE = 2.0

# H25: stands in for the user's words when they share a file and say
# nothing. Deliberately reads as a note about what happened rather than
# as speech, because they didn't speak.
ATTACHMENT_ONLY_TEXT = "(shared this without a caption)"


def _estimate_scale(*, estimate: int, actual: int) -> float:
    """Factor mapping estimated prompt tokens onto the provider's count.

    Returns ``1.0`` (no rescale) when either side is missing or the ratio
    falls outside the sane band.
    """
    if estimate <= 0 or actual <= 0:
        return 1.0
    scale = actual / float(estimate)
    if scale < _MIN_BREAKDOWN_SCALE or scale > _MAX_BREAKDOWN_SCALE:
        return 1.0
    return scale


class ChatTurnMixin:
    """Chat loop + per-turn metrics + next-turn scheduling helpers."""

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
        attachments: "list[dict] | None" = None,
    ) -> str:
        _ = user_vocal_tone  # not used in v1; reserved for prosody hints
        cleaned = sanitize_user_text(user_text or "")
        # D2 Part B — normalise the turn's attachments and stash them so
        # the ``attachments`` inner-life provider can render the turn
        # hint during prompt assembly. Reset every turn (empty list) so
        # a previous turn's attachments never leak forward.
        self._active_turn_attachments = list(attachments or [])
        if not cleaned:
            # H25: holding a photo up without a caption is a real message,
            # so an attachment alone is enough to run a turn. It still
            # needs *some* user text: an empty content row would read as
            # a blank turn in every downstream consumer (history, RAG,
            # the summariser). A short stand-in keeps the transcript
            # honest a month later, when the thumbnail is the only other
            # clue about what happened here.
            if not self._active_turn_attachments:
                return ""
            cleaned = ATTACHMENT_ONLY_TEXT
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
        # G4: snapshot which cues have material waiting, BEFORE prompt
        # assembly. It has to be here rather than during assembly: the T6
        # providers *consume* the state arming is read from -- turning_over
        # clears its pending slot, the journal-backed cues advance their
        # watermark -- so a snapshot taken later would report almost
        # nothing as armed and the reach ratio would look perfect exactly
        # when the machinery was busiest.
        self._snapshot_armed_cues()

        if on_generation_status:
            on_generation_status("AI is generating response...")

        # If chat history is disabled, replay the message into a transient key
        # so we never persist it across restarts.
        session_key = (
            self.session_key if self._remember_history else f"{self.session_key}:noremember"
        )

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
            attachments_json: str | None = None
            if self._active_turn_attachments:
                try:
                    attachments_json = json.dumps(self._active_turn_attachments)
                except (TypeError, ValueError):
                    attachments_json = None
            user_message_id = self._chat_db.add_message(
                session_id=session_key,
                role="user",
                content=cleaned,
                token_count=estimate_tokens(cleaned),
                attachments=attachments_json,
            )

        # The transcript just moved; make the restore pointer follow it so
        # the next launch reopens the conversation he was actually in.
        self._touch_last_active_session()

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

            # H25: if the user shared an image, look at it *now* — before
            # the prompt is assembled — so the description can go into
            # this turn's prompt and she reacts to the picture instead of
            # promising to. Blocks for a few seconds, which is why the
            # spoken filler goes out first. Pixels reach the local worker
            # only; the chat route sees her words about the image.
            self._maybe_describe_turn_images(on_tts_chunk=wrapped_tts_cb)

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
        #
        # P16 measurement: this cascade had zero instrumentation, and
        # ``embedder.end_turn()`` has already fired by the time we get
        # here -- so its 1-4 embeds don't even show up in ``embed_calls``.
        # ``post_turn_ms`` makes the cost visible before anyone tries the
        # (Large) fast/slow-lane split. Note the timer wraps the ``except``
        # too: a cascade that fails slowly is still time the user waited.
        post_turn_started_at = time.perf_counter()
        try:
            self._post_turn_inner_life(
                user_text=cleaned,
                reaction=getattr(result, "reaction", "neutral") or "neutral",
                assistant_text=getattr(result, "text", "") or "",
                raw_assistant_text=getattr(result, "raw_text", "") or "",
                user_message_id=user_message_id,
                assistant_message_id=getattr(result, "assistant_message_id", None),
                # G4 needs this turn's block sizes. Passed explicitly
                # because ``_last_system_prompt`` is not stamped until
                # after this call returns, so reading it from there would
                # measure the PREVIOUS assembly.
                telemetry=telemetry,
            )
        except Exception:
            log.debug("post-turn inner life failed", exc_info=True)
        post_turn_ms = (time.perf_counter() - post_turn_started_at) * 1000.0
        # The cascade is supposed to be cheap; a slow one is latency the
        # user feels after the reply finished, so it escalates to INFO
        # rather than hiding at DEBUG with the routine case.
        log.log(
            logging.INFO if post_turn_ms >= _POST_TURN_SLOW_MS else logging.DEBUG,
            "post-turn done: post_turn_ms=%.1f mode=%s",
            post_turn_ms,
            mode,
        )

        # Context occupancy uses the largest single Ollama call's prompt
        # tokens (stamped on telemetry), NOT the merged tool+stream sum in
        # ``usage.prompt_tokens`` (which double-counts the system prompt on
        # tool turns and would falsely read ~2x). Fall back to the merged
        # figure when telemetry is absent (banter turns: single == merged).
        context_tokens = 0
        if telemetry is not None:
            context_tokens = int(getattr(telemetry, "context_prompt_tokens", 0) or 0)
        if context_tokens <= 0:
            context_tokens = int(usage.prompt_tokens)

        prompt_pct = 0.0
        if self._context_window > 0 and context_tokens > 0:
            prompt_pct = round(context_tokens / float(self._context_window), 4)

        metrics: dict[str, Any] = {
            "mode": mode,
            "capture_ms": round(capture_ms, 1),
            "stt_ms": round(stt_ms, 1),
            "llm_ms": round(llm_ms, 1),
            "tts_ms": 0.0,
            "total_ms": round(total_ms, 1),
            "prompt_tokens": int(usage.prompt_tokens),
            "context_tokens": int(context_tokens),
            "completion_tokens": int(usage.completion_tokens),
            "total_tokens": int(usage.total_tokens),
            # P44: prompt-cache hit rate. Parsed by the OpenAI-compatible
            # client since the beginning but never surfaced past the
            # "turn done:" log line, which left ``docs/prompt-caching.md``
            # claiming ``get_last_response_detail`` exposed it when it
            # did not. 0 on Ollama, which reports no such signal.
            "cached_tokens": int(usage.cached_tokens),
            "cached_pct": round(float(usage.cached_tokens_pct), 1),
            # True when tok/s came from wall clock rather than the
            # provider's own generation timer -- see
            # ``_fill_wall_clock_eval_duration``.
            "eval_estimated": bool(
                getattr(usage, "eval_duration_estimated", False),
            ),
            "total_duration_ms": round(usage.total_duration_ms, 1),
            "eval_duration_ms": round(usage.eval_duration_ms, 1),
            "prompt_eval_duration_ms": round(usage.prompt_eval_duration_ms, 1),
            "tokens_per_second": float(usage.tokens_per_second),
            "context_window": int(self._context_window),
            "context_source": str(self._context_source),
            "prompt_pct": prompt_pct,
            "compactions_total": int(self._compactions_total),
            "first_token_ms": round(float(getattr(result, "first_token_ms", None) or 0.0), 1),
            # P16: wall time of the post-turn inner-life cascade. Not part
            # of ``llm_ms`` / ``total_ms`` above -- those were measured
            # before it ran -- so read it as "extra latency the user waited
            # after the reply finished streaming".
            "post_turn_ms": round(post_turn_ms, 1),
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
            # P44: the breakdown rows are char-heuristic estimates while
            # the bar above them is the provider's real prompt_tokens, so
            # the two are computed different ways and cannot agree --
            # visibly so since the switch to a cloud model whose
            # tokenizer the estimator was never calibrated against.
            # Rescaling the rows onto the real total keeps their relative
            # shape (which is the informative part) while making the
            # column sum truthful.
            #
            # The RAW estimates are what the P44 JSONL records, in
            # ``TurnRunner._emit_prompt_cache_record``. Recording these
            # scaled values instead would force est_error_pct to zero by
            # construction and destroy the very measurement that tells us
            # how far off the estimator is.
            scale = _estimate_scale(
                estimate=int(tdict["prompt_tokens_estimate"]),
                actual=int(context_tokens),
            )
            metrics.update({
                "system_tokens": round(tdict["system_tokens"] * scale),
                "summary_tokens": round(tdict["summary_tokens"] * scale),
                "rag_tokens": round(tdict["rag_tokens"] * scale),
                "history_tokens": round(tdict["history_tokens"] * scale),
                "user_tokens": round(tdict["user_tokens"] * scale),
                # The tool pass is a separate LLM call with its own
                # prompt, so its two rows are outside the identity the
                # scale factor enforces and stay as measured.
                "tool_tokens": tdict["tool_tokens"],
                "tool_schema_tokens": tdict["tool_schema_tokens"],
                "breakdown_scale": round(scale, 3),
                # P44 prefix divergence, for the Diagnostics panel.
                "prefix_diverged": tdict["prefix_diverged"],
                "prefix_tier": tdict["prefix_tier"],
                "prefix_lost_chars": tdict["prefix_lost_chars"],
                "prefix_lost_pct": tdict["prefix_lost_pct"],
                "prefix_changed": tdict["prefix_changed"],
                "history_diverged_at": tdict["history_diverged_at"],
                "history_slid": tdict["history_slid"],
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
                # P14: tool-pass gate decision + pass cost.
                "tool_gate_event": tdict["tool_gate_event"],
                "tool_pass_ms": tdict["tool_pass_ms"],
                # L26: per-turn concept trace (what the L5/L4 blocks
                # surfaced into this turn's prompt).
                "concepts_surfaced": tdict["concepts_surfaced"],
                "coactivation_surfaced": tdict["coactivation_surfaced"],
            })
            # Stash the assembled system prompt out-of-band (not in the
            # broadcast metrics — it can be several KB). Fetched on demand
            # via ``get_last_system_prompt`` for the Diagnostics panel / MCP.
            self._last_system_prompt = {
                "prompt": str(getattr(telemetry, "system_prompt", "") or ""),
                "system_tokens": int(tdict["system_tokens"]),
                "context_tokens": int(context_tokens),
                "mode": mode,
                "captured_at": time.time(),
                # P31a: per-block char costs travel with the prompt they
                # describe, for the same reason the prompt itself is
                # out-of-band -- too big for the per-turn WS broadcast.
                "block_chars": dict(getattr(telemetry, "block_chars", {}) or {}),
            }
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
                hot_clusters = self._dream_hot_clusters()
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
                    hot_clusters=hot_clusters,
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
            # Filter by kind in the store (before the sort) rather than
            # after: the unfiltered top-N shape returned nothing as soon as
            # other kinds outranked the requested one.
            top = store.list_top(limit=max(1, int(limit)), kind=kind)
        except Exception:
            return []
        out: list[str] = []
        for mem in top:
            content = (mem.content or "").strip()
            if not content:
                continue
            out.append(content)
            if len(out) >= limit:
                break
        return out

    def _dream_hot_clusters(self, *, limit: int = 2) -> list[str]:
        """K65e: labels of the day's most-active established K9 clusters.

        Reads ``topic_graph.cluster_activity`` and keeps clusters whose
        newest member is within ``dream_hot_cluster_recency_days`` days,
        ordered most-recent first. Returns ``[]`` when disabled, the graph
        is absent / non-persistent, or nothing has been touched recently.
        """
        if not bool(
            getattr(self._settings.agent, "dream_hot_cluster_enabled", True)
        ):
            return []
        graph = getattr(self, "_topic_graph", None)
        if graph is None:
            return []
        recency = float(
            getattr(self._settings.agent, "dream_hot_cluster_recency_days", 3.0)
        )
        try:
            rows = graph.cluster_activity(top_n=8, min_size=3)
        except Exception:
            log.debug("dream hot-cluster lookup failed", exc_info=True)
            return []
        recent = []
        for row in rows or []:
            label = str(getattr(row, "label", "") or "").strip()
            if not label:
                continue
            days = getattr(row, "days_since", None)
            if days is None or float(days) > recency:
                continue
            recent.append((float(days), label))
        recent.sort(key=lambda t: t[0])
        return [label for _d, label in recent[:limit]]

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
            now = timephrase.utcnow()
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
