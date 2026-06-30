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


log = logging.getLogger("app.session")


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
        if not cleaned:
            return ""
        # D2 Part B — normalise the turn's attachments and stash them so
        # the ``attachments`` inner-life provider can render the turn
        # hint during prompt assembly. Reset every turn (empty list) so
        # a previous turn's attachments never leak forward.
        self._active_turn_attachments = list(attachments or [])
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
                # P14: tool-pass gate decision + pass cost.
                "tool_gate_event": tdict["tool_gate_event"],
                "tool_pass_ms": tdict["tool_pass_ms"],
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
