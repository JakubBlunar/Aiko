from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger("app.session")


class InnerLifePart3Mixin:
    """Inner-life prompt-block providers (part 3 of 4)."""

    def _render_opinion_injection_block(self, user_text: str) -> str:
        """K29: surface a per-turn cue when a stored stance contradicts {user}.

        Sibling of :meth:`_render_misattunement_block` -- both are
        provider-time detectors that fire the cue on the same turn the
        user message arrives, not the turn after. The anti-
        contrarianism guardrails are layered:

        * Master switch (``agent.opinion_injection_enabled``) flips
          the whole feature off without a code change.
        * Cooldown counter decremented every call; armed on fire.
          Default 5 turns -- longer than K23's 3 because a stance
          disagreement is a heavier beat than a soft-drift cue.
        * Per-session cap (``memory.opinion_injection_per_session_cap``,
          default 3). Five fires in one session almost certainly
          means the detector is misfiring; the cap silently
          suppresses the rest.
        * Predicate filter on stance memories (lives in the detector
          module). Only opinion-shaped self-tags qualify, not
          biographical facts.
        * Heuristic + LLM gate (lives in the detector module).
          Only ``definite`` contradictions fire immediately;
          ``borderline`` requires an LLM YES verdict via the
          rate-limited ``FactCheckRateLimiter``.

        MCP debug: ``force_opinion_injection`` arms a one-shot
        ``_opinion_injection_force_next`` that bypasses cooldown +
        per-session cap (but NOT the predicate filter / cosine /
        heuristic gates -- a forced bypass on an unrelated message
        still silently expires when no stance contradicts).
        """
        # K46: reset the per-turn "a stance cue fired" flag at the top so
        # every path below leaves it accurate for the post-turn hook.
        self._opinion_injection_cue_emitted = False
        if not bool(
            getattr(self._settings.agent, "opinion_injection_enabled", True)
        ):
            return ""
        try:
            from app.core.affect import opinion_injection_detector
        except Exception:
            log.debug("opinion-injection import failed", exc_info=True)
            return ""

        # P21: a borderline verdict confirmed by the post-turn resolver
        # renders here, exactly one turn after the contradicting message
        # (the stance hasn't changed in those few seconds, so the lag is
        # invisible). One-shot: clear on read. Cooldown / cap / tease were
        # already armed when the verdict landed, so skip the gates below.
        pending_cue = getattr(self, "_opinion_injection_pending_cue", None)
        if pending_cue:
            self._opinion_injection_pending_cue = None
            self._opinion_injection_cue_emitted = True  # K46
            return pending_cue

        # Decrement cooldown first so a quiet turn always whittles
        # the counter down; otherwise a session that never trips a
        # trigger keeps a stale armed cooldown forever.
        current_cooldown = max(
            0, int(getattr(self, "_opinion_injection_cooldown", 0))
        )
        if current_cooldown > 0:
            self._opinion_injection_cooldown = current_cooldown - 1

        # MCP-debug bypass: ``force_next`` ignores cooldown + cap for
        # this one call. Cleared whether we fire or not so the
        # bypass is strictly one-turn.
        force_next = bool(
            getattr(self, "_opinion_injection_force_next", False)
        )
        if force_next:
            self._opinion_injection_force_next = False

        if not force_next:
            if self._opinion_injection_cooldown > 0:
                return ""
            session_cap = max(
                0,
                int(
                    getattr(
                        self._memory_settings,
                        "opinion_injection_per_session_cap",
                        3,
                    )
                ),
            )
            session_count = int(
                getattr(self, "_opinion_injection_session_count", 0)
            )
            if session_cap > 0 and session_count >= session_cap:
                return ""

        memory_store = getattr(self, "_memory_store", None)
        embedder = getattr(self, "_embedder", None)
        if memory_store is None or embedder is None:
            return ""

        try:
            self_memories = list(memory_store.iter_by_kind("self"))
        except Exception:
            log.debug("opinion-injection: self memory snapshot failed", exc_info=True)
            return ""
        if not self_memories:
            return ""

        try:
            user_vec = embedder.embed(user_text or "")
        except Exception:
            log.debug("opinion-injection: embedder failed", exc_info=True)
            return ""

        # P21: the borderline path's LLM verdict used to run *inline* here
        # -- 0.5-8s of added TTFT on every fire, before any token streamed.
        # We now defer it: ``detect`` returns a PENDING borderline candidate
        # without touching the LLM, the post-turn hook
        # (``_resolve_opinion_injection_pending``) runs the rate-limited
        # verdict, and a confirmed cue renders one turn later via the
        # one-shot at the top of this method. ``definite`` hits still fire
        # inline (they never needed the LLM).
        rate_limiter = getattr(self, "_opinion_injection_rate_limiter", None)
        ollama_client = getattr(self, "_ollama", None)
        require_definite = bool(
            getattr(
                self._settings.agent,
                "opinion_injection_require_definite",
                False,
            )
        )
        # Only defer when there's actually a way to resolve the verdict off
        # the hot path; otherwise stay definite-only (Path C).
        can_defer = (
            rate_limiter is not None
            and ollama_client is not None
            and not require_definite
        )

        memory_settings = self._memory_settings
        try:
            result = opinion_injection_detector.detect(
                user_text or "",
                user_vec=user_vec,
                self_memories=self_memories,
                llm_gate=None,
                defer_borderline=can_defer,
                min_cosine=float(
                    getattr(
                        memory_settings,
                        "opinion_injection_min_cosine",
                        opinion_injection_detector.DEFAULT_MIN_COSINE,
                    )
                ),
                min_user_words=int(
                    getattr(
                        memory_settings,
                        "opinion_injection_min_user_words",
                        opinion_injection_detector.DEFAULT_MIN_USER_WORDS,
                    )
                ),
                require_definite=require_definite,
            )
        except Exception:
            log.debug("opinion-injection detector raised", exc_info=True)
            return ""

        if result is None:
            return ""

        # P21: a PENDING (borderline) result means "candidate found, but
        # the verdict costs an LLM call". Stash it for the post-turn
        # resolver and stay silent this turn -- do NOT arm cooldown / cap /
        # tease until the cue actually fires.
        if result.llm_verdict == "PENDING":
            self._opinion_injection_pending_borderline = {
                "user_text": user_text or "",
                "stance_text": result.stance_text,
                "stance_memory_id": result.stance_memory_id,
                "cosine": result.cosine,
                "heuristic_label": result.heuristic_label,
                "heuristic_signals": list(result.heuristic_signals),
            }
            log.debug(
                "opinion-injection: borderline deferred stance_id=%d cosine=%.3f",
                result.stance_memory_id,
                result.cosine,
            )
            return ""

        # Arm cooldown, bump per-session count, stash diagnostics
        # for the MCP debug tool. ``last_opinion_injection`` is the
        # full result dataclass so the tool can show heuristic
        # signals + the matched stance text.
        cooldown_turns = max(
            0,
            int(
                getattr(
                    self._memory_settings,
                    "opinion_injection_cooldown_turns",
                    5,
                )
            ),
        )
        self._opinion_injection_cooldown = cooldown_turns
        self._opinion_injection_session_count = (
            int(getattr(self, "_opinion_injection_session_count", 0)) + 1
        )
        self._last_opinion_injection = result

        log.info(
            "opinion-injection fire: trigger=%s cosine=%.3f stance_id=%d "
            "heuristic=%s signals=%s llm_verdict=%s cooldown_set=%d "
            "session_count=%d",
            result.trigger,
            result.cosine,
            result.stance_memory_id,
            result.heuristic_label,
            ",".join(result.heuristic_signals) or "-",
            result.llm_verdict or "-",
            cooldown_turns,
            self._opinion_injection_session_count,
        )

        # K59: a hard pushback on her stance is prime ledger
        # material — bank the user's claim as a future callback
        # tease ("oh, like the time you swore...? I remember
        # things."). Best-effort; dedupe lives in the pure module.
        try:
            quote = " ".join((user_text or "").split())[:120]
            self._bank_tease_debt(
                what="they pushed back hard on a take of yours",
                context=f'they said "{quote}"' if quote else "",
                source="opinion_pushback",
            )
        except Exception:
            log.debug("opinion-pushback tease bank failed", exc_info=True)

        try:
            block = opinion_injection_detector.render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("opinion-injection render failed", exc_info=True)
            return ""
        self._opinion_injection_cue_emitted = True  # K46
        return block

    def _opinion_injection_llm_verdict(
        self,
        user_text: str,
        stance_text: str,
    ) -> str | None:
        """One-shot YES/NO/UNRELATED gate for borderline-heuristic stances.

        Mirrors the F5 conflict-detector's ``_verify_with_llm`` (same
        Ollama call shape, same JSON schema, same parse path) but
        scoped to the K29 prompt: "does the user's claim contradict
        Aiko's stored stance". Returns the bare verdict string for
        the detector; ``None`` on any error / parse failure / cancel.
        """
        ollama_client = getattr(self, "_ollama", None)
        if ollama_client is None:
            return None
        try:
            from app.core.affect import opinion_injection_llm as _llm
        except Exception:
            log.debug("opinion-injection llm module missing", exc_info=True)
            return None
        return _llm.verify(
            ollama_client,
            model=self._effective_worker_model,
            user_text=user_text,
            stance_text=stance_text,
            cancel_event=getattr(self, "_fact_check_cancel", None),
        )

    def _resolve_opinion_injection_pending(self) -> None:
        """P21: run the deferred K29 borderline verdict off the hot path.

        Drains the ``_opinion_injection_pending_borderline`` slot armed by
        :meth:`_render_opinion_injection_block`, runs the rate-limited
        YES/NO/UNRELATED gate, and -- on a YES -- arms a one-shot
        ``_opinion_injection_pending_cue`` that the *next* turn's provider
        renders. Best-effort: any failure path drops the candidate (no
        cue). Called from ``_post_turn_inner_life``.
        """
        pending = getattr(self, "_opinion_injection_pending_borderline", None)
        if not pending:
            return
        # One-shot: clear regardless of outcome so a dropped verdict never
        # lingers into a later turn.
        self._opinion_injection_pending_borderline = None

        if not bool(
            getattr(self._settings.agent, "opinion_injection_enabled", True)
        ):
            return
        if bool(
            getattr(
                self._settings.agent,
                "opinion_injection_require_definite",
                False,
            )
        ):
            return

        # Per-session cap: a confirmed cue still counts against the cap, so
        # skip the LLM spend entirely once saturated.
        session_cap = max(
            0,
            int(
                getattr(
                    self._memory_settings,
                    "opinion_injection_per_session_cap",
                    3,
                )
            ),
        )
        session_count = int(
            getattr(self, "_opinion_injection_session_count", 0)
        )
        if session_cap > 0 and session_count >= session_cap:
            return

        rate_limiter = getattr(self, "_opinion_injection_rate_limiter", None)
        if rate_limiter is not None:
            try:
                if not rate_limiter.allow():
                    return
            except Exception:
                log.debug(
                    "opinion-injection: rate_limiter raised (resolve)",
                    exc_info=True,
                )
                return

        user_text = str(pending.get("user_text", ""))
        stance_text = str(pending.get("stance_text", ""))
        verdict = self._opinion_injection_llm_verdict(user_text, stance_text)
        if (verdict or "").strip().upper() != "YES":
            log.debug(
                "opinion-injection: borderline resolved verdict=%s (no cue)",
                verdict or "-",
            )
            return

        try:
            from app.core.affect import opinion_injection_detector
        except Exception:
            log.debug("opinion-injection import failed (resolve)", exc_info=True)
            return

        result = opinion_injection_detector.OpinionInjectionResult(
            trigger="contradiction_borderline",
            stance_text=stance_text,
            stance_memory_id=int(pending.get("stance_memory_id", 0) or 0),
            cosine=float(pending.get("cosine", 0.0) or 0.0),
            heuristic_label=str(pending.get("heuristic_label", "")),
            heuristic_signals=list(pending.get("heuristic_signals", []) or []),
            llm_verdict="YES",
        )

        cooldown_turns = max(
            0,
            int(
                getattr(
                    self._memory_settings,
                    "opinion_injection_cooldown_turns",
                    5,
                )
            ),
        )
        self._opinion_injection_cooldown = cooldown_turns
        self._opinion_injection_session_count = session_count + 1
        self._last_opinion_injection = result

        try:
            self._opinion_injection_pending_cue = (
                opinion_injection_detector.render_inner_life_block(
                    result,
                    user_display_name=self.user_display_name,
                )
            )
        except Exception:
            log.debug(
                "opinion-injection render failed (resolve)", exc_info=True
            )
            self._opinion_injection_pending_cue = None
            return

        log.info(
            "opinion-injection fire (deferred): trigger=%s cosine=%.3f "
            "stance_id=%d heuristic=%s llm_verdict=YES cooldown_set=%d "
            "session_count=%d",
            result.trigger,
            result.cosine,
            result.stance_memory_id,
            result.heuristic_label,
            cooldown_turns,
            self._opinion_injection_session_count,
        )

        # K59: bank the hard pushback as future tease material (mirrors
        # the definite path in _render_opinion_injection_block).
        try:
            quote = " ".join(user_text.split())[:120]
            self._bank_tease_debt(
                what="they pushed back hard on a take of yours",
                context=f'they said "{quote}"' if quote else "",
                source="opinion_pushback",
            )
        except Exception:
            log.debug(
                "opinion-pushback tease bank failed (resolve)", exc_info=True
            )

    def _render_stance_persistence_block(self, user_text: str) -> str:
        """K46: surface a "hold your take" cue on mild taste pushback.

        Fires only when Aiko has *recently* stated a taste/opinion (a K29
        cue fired within the last ``memory.stance_persistence_window``
        turns, tracked by ``_stance_recent_window`` which is armed +
        decremented post-turn) AND the live user message reads as a
        *mild* pushback in K20's calibration regex (``pushback_mild``).
        A strong correction ("no, that's wrong") is deliberately left to
        K20 — that's a factual signal even mid-taste-talk.

        The companion write-side shield (skip the K20 calibration drop on
        this same turn) lives in the post-turn hook; both share the
        :func:`app.core.conversation.stance_persistence.evaluate` gate so
        the cue and the shield never disagree.

        MCP debug: ``force_stance_persistence`` arms a one-shot
        ``_stance_persistence_force_next`` that fires the cue regardless
        of the recent-stance window (it still needs a mild-pushback band
        to classify a band for the line).
        """
        if not bool(
            getattr(self._settings.agent, "stance_persistence_enabled", True)
        ):
            return ""
        try:
            from app.core.affect import calibration_detector
            from app.core.conversation import stance_persistence
        except Exception:
            log.debug("stance-persistence import failed", exc_info=True)
            return ""

        force_next = bool(
            getattr(self, "_stance_persistence_force_next", False)
        )
        if force_next:
            self._stance_persistence_force_next = False

        recent_window = int(getattr(self, "_stance_recent_window", 0) or 0)
        recent_stance = recent_window > 0 or force_next
        if not recent_stance:
            return ""

        # Classify the live user turn. Regex-only (no vecs) — strong /
        # mild / affirmation are pure regex; the softening band needs the
        # prior-assistant vector we don't carry here, and K46 only acts on
        # the mild band anyway.
        band: str | None = None
        try:
            signal = calibration_detector.detect(user_text=user_text or "")
            band = signal.kind if signal is not None else None
        except Exception:
            log.debug("stance-persistence band classify raised", exc_info=True)
            band = None

        verdict = stance_persistence.evaluate(
            recent_stance=recent_stance, pushback_band=band,
        )
        if not verdict.hold:
            return ""

        stance_text = str(getattr(self, "_stance_recent_text", "") or "")
        try:
            block = stance_persistence.render_block(
                stance_text, user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("stance-persistence render failed", exc_info=True)
            return ""
        if not block:
            return ""

        self._last_stance_persistence = {
            "band": band,
            "window": recent_window,
            "forced": force_next,
            "stance_text": stance_text,
        }
        log.info(
            "stance-persistence fire: band=%s window=%d forced=%s",
            band,
            recent_window,
            force_next,
        )
        return block

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
            from app.core.conversation.novelty_detector import render_inner_life_block

            # F10k: thread the per-turn cluster-transition signals the
            # detector just computed into the render so the cue can name
            # the topic move (return-to-known vs brand-new).
            return render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
                topic_changed=bool(getattr(detector, "last_cluster_changed", False)),
                topic_returning=bool(
                    getattr(detector, "last_cluster_returning", False)
                ),
                topic_label=str(getattr(detector, "last_cluster_label", "") or ""),
                prev_topic_label=str(
                    getattr(detector, "last_prev_cluster_label", "") or ""
                ),
            )
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
            from app.core.conversation.topic_stagnation import render_inner_life_block

            # F10k: K6 just mapped this turn to its best cluster; name the
            # looped-on topic in the lull cue if it has a clean label.
            topic_label = str(
                getattr(novelty, "last_cluster_label", "") or ""
            ) if novelty is not None else ""
            return render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
                topic_label=topic_label,
            )
        except Exception:
            log.debug("topic stagnation block render failed", exc_info=True)
            return ""

    def _render_style_pattern_block(self) -> str:
        """Anti-rut layer: surface a one-line style nudge for Aiko.

        The :class:`AikoStylePatternTracker` has been fed the previous
        turn's stripped reply by the post-turn pipeline. Here we just
        ask it what it sees -- opener-rut, question-saturation, or
        length-sprawl -- and render the matching cue. Empty string
        when the tracker is disabled, in warmup, in cooldown, or no
        band tripped, which is the common case so the block disappears
        entirely on most turns.
        """
        if not bool(
            getattr(self._settings.agent, "style_tracker_enabled", True)
        ):
            return ""
        tracker = getattr(self, "_aiko_style_tracker", None)
        if tracker is None:
            return ""
        try:
            result = tracker.detect()
        except Exception:
            log.debug("aiko style tracker raised", exc_info=True)
            return ""
        if result is None:
            return ""
        try:
            from app.core.persona.aiko_style_tracker import render_inner_life_block

            return render_inner_life_block(result)
        except Exception:
            log.debug("aiko style block render failed", exc_info=True)
            return ""

    def _render_self_noticing_block(self) -> str:
        """K30: fan three self-noticing sub-detectors into one block.

        Each sub-detector is independently togglable:

        * **Agreement streak** -- regex over the last
          ``self_noticing_window`` rendered assistant replies pulled
          from SQLite per provider call (K23-style; zero new state
          for this sub-detector). Fires when the agreement-token
          share meets the threshold AND the pushback count is at or
          below ``self_noticing_max_pushback``.
        * **Flat affect** -- range scan over the in-memory
          ``_self_noticing_affect_samples`` ring populated post-turn.
          Fires only when both scalar ranges sit at or below their
          thresholds AND no reaction outside ``LOW_BAND_REACTIONS``
          fired in the window.
        * **Repeated thought** -- consumes the one-shot
          ``_repeated_thought_fired_last_turn`` flag armed post-turn
          when Aiko's just-finished reply was a near-duplicate of one
          of her last 3 replies. Cooldown-free because the flag is
          naturally one-shot; the post-turn detector won't re-arm
          unless cosine threshold trips again.

        Returns the joined Heads-up lines (1-3) or ``""`` when none
        of the sub-detectors fire (the common-case empty turn). All
        diagnostic state (last verdict, last cosine, cooldown
        remainders) is stashed on the controller for the MCP debug
        tools; no behaviour depends on those reads.
        """
        agent_settings = self._settings.agent
        if not bool(getattr(agent_settings, "self_noticing_enabled", True)):
            return ""

        try:
            from app.core.affect.self_pattern_detector import (
                detect_agreement_streak,
                detect_flat_affect,
            )
        except Exception:
            log.debug("self-noticing import failed", exc_info=True)
            return ""

        lines: list[str] = []
        window = max(1, int(
            getattr(agent_settings, "self_noticing_window", 6)
        ))
        warmup = max(1, int(
            getattr(agent_settings, "self_noticing_warmup", 4)
        ))

        # --- Agreement streak (SQLite-backed) ----------------------------
        # Decrement cooldown first so a quiet turn always whittles the
        # counter down -- mirrors the K23 / K29 pattern.
        agreement_cd = max(
            0, int(getattr(self, "_self_noticing_agreement_cooldown", 0))
        )
        if agreement_cd > 0:
            self._self_noticing_agreement_cooldown = agreement_cd - 1
        agreement_force = bool(
            getattr(self, "_self_noticing_force_agreement", False)
        )
        if agreement_force:
            self._self_noticing_force_agreement = False
            agreement_cooldown_for_check = 0
        else:
            agreement_cooldown_for_check = (
                self._self_noticing_agreement_cooldown
            )
        if (
            bool(
                getattr(
                    agent_settings,
                    "self_noticing_agreement_streak_enabled",
                    True,
                )
            )
            and agreement_cooldown_for_check == 0
            and self._chat_db is not None
        ):
            try:
                # Pull a generous slice (window*2 rows) and filter to
                # assistant rows -- a chatty stretch can have multiple
                # user rows between Aiko's replies, so a strict
                # ``limit=window`` would miss some of them.
                recent_rows = self._inner_life_recent_messages(
                    max(window * 4, 20),
                )
                recent_assistant: list[str] = []
                for row in reversed(recent_rows):
                    if row.role == "assistant" and (row.content or "").strip():
                        recent_assistant.append(row.content)
                        if len(recent_assistant) >= window:
                            break
            except Exception:
                log.debug(
                    "self-noticing: chat_db read failed", exc_info=True,
                )
                recent_assistant = []
            if recent_assistant:
                try:
                    result = detect_agreement_streak(
                        recent_assistant,
                        min_samples=warmup,
                        agreement_threshold=float(
                            getattr(
                                agent_settings,
                                "self_noticing_agreement_threshold",
                                0.80,
                            )
                        ),
                        max_pushback=int(
                            getattr(
                                agent_settings,
                                "self_noticing_max_pushback",
                                0,
                            )
                        ),
                    )
                    self._last_self_noticing_agreement = result
                    if result.fired or agreement_force:
                        lines.append(
                            "Heads-up: you've been agreeing with everything"
                            " for a stretch -- if you actually have a"
                            " different read on something, say it."
                        )
                        self._self_noticing_agreement_cooldown = int(
                            getattr(
                                agent_settings,
                                "self_noticing_cooldown_turns",
                                5,
                            )
                        )
                        log.info(
                            "self-noticing agreement-streak: share=%.2f "
                            "pushback=%.2f n=%d cooldown=%d",
                            result.agreement_share,
                            result.pushback_share,
                            result.sample_size,
                            self._self_noticing_agreement_cooldown,
                        )
                except Exception:
                    log.debug(
                        "self-noticing agreement detect failed",
                        exc_info=True,
                    )

        # --- Flat affect (in-memory ring) -------------------------------
        flat_cd = max(
            0, int(getattr(self, "_self_noticing_flat_affect_cooldown", 0))
        )
        if flat_cd > 0:
            self._self_noticing_flat_affect_cooldown = flat_cd - 1
        flat_force = bool(
            getattr(self, "_self_noticing_force_flat_affect", False)
        )
        if flat_force:
            self._self_noticing_force_flat_affect = False
            flat_cooldown_for_check = 0
        else:
            flat_cooldown_for_check = (
                self._self_noticing_flat_affect_cooldown
            )
        if (
            bool(
                getattr(
                    agent_settings,
                    "self_noticing_flat_affect_enabled",
                    True,
                )
            )
            and flat_cooldown_for_check == 0
        ):
            ring = getattr(self, "_self_noticing_affect_samples", None)
            if ring is not None and len(ring) > 0:
                # Scan only the most-recent ``window`` samples so a
                # historical mood swing doesn't keep flat-affect from
                # firing on a freshly-flat stretch.
                samples = list(ring)[-window:]
                try:
                    result = detect_flat_affect(
                        samples,
                        min_samples=warmup,
                        valence_range_threshold=float(
                            getattr(
                                agent_settings,
                                "self_noticing_flat_valence_range",
                                0.10,
                            )
                        ),
                        arousal_range_threshold=float(
                            getattr(
                                agent_settings,
                                "self_noticing_flat_arousal_range",
                                0.10,
                            )
                        ),
                    )
                    self._last_self_noticing_flat_affect = result
                    if result.fired or flat_force:
                        lines.append(
                            "Heads-up: your read has been pretty even-keel"
                            " all session -- let yourself land somewhere"
                            " if a moment actually moves you."
                        )
                        self._self_noticing_flat_affect_cooldown = int(
                            getattr(
                                agent_settings,
                                "self_noticing_cooldown_turns",
                                5,
                            )
                        )
                        log.info(
                            "self-noticing flat-affect: val_range=%.3f "
                            "aro_range=%.3f notable=%d n=%d cooldown=%d",
                            result.valence_range,
                            result.arousal_range,
                            result.notable_reaction_count,
                            result.sample_size,
                            self._self_noticing_flat_affect_cooldown,
                        )
                except Exception:
                    log.debug(
                        "self-noticing flat-affect detect failed",
                        exc_info=True,
                    )

        # --- Repeated thought (one-shot carry-forward) ------------------
        repeated_force = bool(
            getattr(self, "_self_noticing_force_repeated_thought", False)
        )
        repeated_flag = bool(
            getattr(self, "_repeated_thought_fired_last_turn", False)
        )
        if (
            bool(
                getattr(
                    agent_settings,
                    "self_noticing_repeated_thought_enabled",
                    True,
                )
            )
            and (repeated_flag or repeated_force)
        ):
            lines.append(
                "Heads-up: your last reply was very close to something you"
                " already said -- find a different angle this turn, or"
                " just don't restate."
            )
            # One-shot consume both flags regardless of which fired.
            self._repeated_thought_fired_last_turn = False
            self._self_noticing_force_repeated_thought = False
            log.info(
                "self-noticing repeated-thought rendered: cosine=%.3f",
                float(
                    getattr(self, "_repeated_thought_last_cosine", 0.0)
                ),
            )

        return "\n".join(lines)

    def _render_style_signal_block(self) -> str:
        """K13: surface the one-line "How <name> writes lately" cue.

        Reads the rolling-window snapshot from
        :class:`StyleSignalAnalyzer` (which the post-turn pipeline
        has been feeding user turns), buckets each axis against the
        configured thresholds, and renders the labels into a single
        short line. Returns ``""`` when the analyzer is disabled, in
        warmup, or when every axis sits in the default mid-band --
        which is the common no-signal case so the block costs zero on
        a neutral-register speaker.
        """
        if not bool(
            getattr(self._settings.agent, "style_signal_enabled", True)
        ):
            return ""
        analyzer = getattr(self, "_style_signal_analyzer", None)
        if analyzer is None:
            return ""
        try:
            signal = analyzer.current_signal()
        except Exception:
            log.debug("style signal analyzer raised", exc_info=True)
            return ""
        if signal is None:
            return ""
        try:
            labels = analyzer.labels_for_signal(signal)
        except Exception:
            log.debug("style signal labels failed", exc_info=True)
            return ""
        if not labels:
            return ""
        try:
            from app.core.persona.style_signal import render_inner_life_block

            return render_inner_life_block(
                signal,
                labels,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("style signal block render failed", exc_info=True)
            return ""

    def _render_curiosity_seeds_block(self) -> str:
        """K9: surface up to two active "quiet curiosity" seeds.

        Reads the in-memory mirror via
        :meth:`MemoryStore.iter_by_kind`; no per-turn LLM, no
        embedder. Picks the oldest unconsumed seeds (oldest first
        gives every seed a fair shot at being mentioned) and renders
        them as a short "Quiet curiosity (only if a soft pivot lands
        naturally):" bullet list. Empty when the worker is disabled,
        when no seeds exist yet, or when every seed is already
        ``consumed_at``.
        """
        if not bool(
            getattr(self._settings.agent, "curiosity_seed_enabled", True)
        ):
            return ""
        if self._question_balance_suppressed():
            return ""
        memory = getattr(self, "_memory_store", None)
        if memory is None:
            return ""
        try:
            seeds = memory.iter_by_kind("curiosity_seed")
        except Exception:
            log.debug("curiosity_seed iter failed", exc_info=True)
            return ""
        if not seeds:
            return ""
        active: list[Any] = []
        for seed in seeds:
            metadata = seed.metadata or {}
            if metadata.get("consumed_at"):
                continue
            if seed.tier == "archive":
                continue
            active.append(seed)
        if not active:
            return ""
        active.sort(key=lambda m: m.created_at or "")
        rendered: list[str] = []
        for seed in active[:2]:
            metadata = seed.metadata or {}
            topic = (metadata.get("topic") or seed.content or "").strip()
            if not topic:
                continue
            if len(topic) > 120:
                topic = topic[:119].rstrip(",;: ") + "…"
            rendered.append(f"- {topic}")
        if not rendered:
            return ""
        header = "Quiet curiosity (only if a soft pivot lands naturally):"
        return header + "\n" + "\n".join(rendered)

    def _render_initiative_block(self, user_text: str) -> str:
        """K53: deterministic floor-taking directive.

        Per-turn provider (takes the live ``user_text`` for the
        substantial-message escape hatch). The
        :class:`InitiativeDirector` counter lives on the controller
        and is recreated lazily; every gate input is best-effort —
        a sick store reads as its neutral value rather than
        blocking the turn. MCP ``force_initiative_turn`` arms
        ``_initiative_force_next`` to bypass everything except the
        support/reflection arc block.
        """
        if not bool(
            getattr(self._settings.agent, "initiative_turns_enabled", True)
        ):
            return ""
        try:
            from app.core.conversation import initiative_director as _idir

            director = getattr(self, "_initiative_director", None)
            if director is None:
                director = _idir.InitiativeDirector()
                self._initiative_director = director
            agent = self._settings.agent

            arc = None
            arc_store = getattr(self, "_arc_store", None)
            if arc_store is not None:
                try:
                    arc_state = arc_store.get_or_default(self._user_id)
                    arc = getattr(arc_state, "arc", None)
                except Exception:
                    arc = None

            closeness = comfort = None
            axes_store = getattr(self, "_relationship_axes_store", None)
            if axes_store is not None:
                try:
                    axes = axes_store.get(self._user_id)
                    closeness = float(axes.closeness)
                    comfort = float(axes.comfort)
                except Exception:
                    closeness = comfort = None

            # K52 tie-in: read the ledger (no mutation — the wants
            # provider owns growth) for both the imperative-active
            # gate and the directive's content.
            want_text = None
            wants_imperative_active = False
            chat_db = getattr(self, "_chat_db", None)
            if chat_db is not None:
                try:
                    from datetime import datetime, timezone

                    from app.core.conversation import wants_ledger as _wl

                    state = _wl.deserialize(
                        chat_db.kv_get(_wl.KV_WANTS_LEDGER)
                    )
                    if state.wants:
                        strongest = max(
                            state.wants, key=lambda w: w.pressure,
                        )
                        want_text = strongest.text
                        threshold = float(
                            getattr(
                                agent, "wants_imperative_threshold", 0.7,
                            )
                        )
                        wants_imperative_active = (
                            strongest.pressure >= threshold
                        )
                except Exception:
                    want_text = None
                    wants_imperative_active = False

            force = bool(getattr(self, "_initiative_force_next", False))
            if force:
                self._initiative_force_next = False

            decision = director.note_turn_and_decide(
                base_period=int(
                    getattr(agent, "initiative_base_period", 8)
                ),
                arc=arc,
                closeness=closeness,
                comfort=comfort,
                misattunement_active=(
                    int(getattr(self, "_misattunement_cooldown", 0)) > 0
                ),
                rupture_active=(
                    getattr(self, "_pending_rupture", None) is not None
                ),
                user_text=user_text or "",
                substantial_chars=int(
                    getattr(agent, "initiative_substantial_chars", 240)
                ),
                warmup_turns=int(
                    getattr(agent, "initiative_warmup_turns", 3)
                ),
                wants_imperative_active=wants_imperative_active,
                force=force,
            )
            log.debug(
                "initiative-director: reason=%s turns=%d period=%d",
                decision.reason,
                director.turns_since_initiative,
                decision.effective_period,
            )
            if not decision.fire:
                return ""
            log.info(
                "initiative-turn fire: period=%d arc=%s want=%s",
                decision.effective_period,
                arc,
                (want_text or "")[:60] or None,
            )
            # K55: this turn opens Aiko's thread — arm the post-turn
            # stamp so the next user reply gets evaluated for a
            # three-words-and-pivot tell.
            self._pending_thread_open = {
                "source": "initiative",
                "topic": want_text or None,
            }
            return _idir.render_block(
                want_text,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("initiative block render failed", exc_info=True)
            return ""

    def _render_thread_ownership_block(self, user_text: str) -> str:
        """K55: evaluate the reply to a thread Aiko opened.

        Runs only while ``_owned_thread`` is set (stamped post-turn
        when a K53 directive / K52 imperative fired). Exactly one
        evaluation per thread: an engaged reply clears it silently, a
        short pivot renders the single return cue and the thread is
        dropped forever. A blank ``user_text`` (proactive turn) skips
        the evaluation without consuming the thread — the cue should
        judge a real reply, not a silence.
        """
        if not bool(
            getattr(self._settings.agent, "thread_ownership_enabled", True)
        ):
            return ""
        thread = getattr(self, "_owned_thread", None)
        if thread is None:
            return ""
        text = (user_text or "").strip()
        if not text:
            return ""
        # One evaluation max — consume the slot before anything can
        # raise so a sick embedder can't make the cue fire twice.
        self._owned_thread = None
        try:
            from app.core.conversation import thread_ownership as _town

            agent = self._settings.agent
            user_vec = None
            embedder = getattr(self, "_embedder", None)
            if embedder is not None:
                try:
                    user_vec = embedder.embed(text)
                except Exception:
                    user_vec = None
            verdict = _town.evaluate_reply(
                thread,
                text,
                user_vec,
                engaged_chars=int(
                    getattr(agent, "thread_engaged_chars", 80)
                ),
                min_topical_similarity=float(
                    getattr(agent, "thread_min_topical_similarity", 0.30)
                ),
            )
            log.info(
                "thread-ownership: verdict=%s cosine=%s chars=%d "
                "source=%s topic=%s",
                verdict.verdict,
                f"{verdict.cosine:.3f}" if verdict.cosine is not None
                else "n/a",
                verdict.reply_chars,
                thread.source,
                thread.topic[:60],
            )
            if verdict.verdict != _town.VERDICT_PIVOT:
                return ""
            # K57: a brushed-off thread is a light miffed trigger —
            # comedy-weight, not a real sulk (the post-turn drain
            # applies it).
            try:
                self._queue_emotion_trigger(
                    emotion="miffed",
                    cause=(
                        "the thread you opened ("
                        + thread.topic[:80]
                        + ") got brushed off"
                    ),
                    intensity=0.25,
                    source="thread_pivot",
                )
            except Exception:
                log.debug("thread-pivot miffed queue failed", exc_info=True)
            return _town.render_return_block(
                thread.topic,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug(
                "thread ownership block render failed", exc_info=True,
            )
            return ""

    def _render_wants_block(self) -> str:
        """K52: surface Aiko's wants ledger with pressure-driven bands.

        Reads + lazily matures the ledger on every turn (growth +
        expiry land on the same pure functions the feeder worker
        uses, then the state is persisted back — mirrors the K15
        read-decay-persist convention). Soft band lists up to two
        wants; once the strongest want crosses
        ``agent.wants_imperative_threshold`` the cue flips to the
        one-want imperative directive. MCP ``force_want_imperative``
        arms ``_wants_force_imperative`` to bypass the threshold once.
        """
        if not bool(
            getattr(self._settings.agent, "wants_ledger_enabled", True)
        ):
            return ""
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return ""
        try:
            from datetime import datetime, timezone

            from app.core.conversation import wants_ledger as _wl

            agent = self._settings.agent
            now = datetime.now(timezone.utc)
            state = _wl.deserialize(chat_db.kv_get(_wl.KV_WANTS_LEDGER))
            if not state.wants and not state.recently_acted:
                return ""
            matured = _wl.apply_growth(
                state, now,
                growth_per_day=float(
                    getattr(agent, "wants_growth_per_day", 0.25)
                ),
                max_age_days=float(
                    getattr(agent, "wants_max_age_days", 14.0)
                ),
                reentry_cooldown_days=float(
                    getattr(agent, "wants_reentry_cooldown_days", 5.0)
                ),
            )
            try:
                chat_db.kv_set(_wl.KV_WANTS_LEDGER, _wl.serialize(matured))
            except Exception:
                log.debug("wants ledger persist failed", exc_info=True)
            threshold = float(
                getattr(agent, "wants_imperative_threshold", 0.7)
            )
            if getattr(self, "_wants_force_imperative", False):
                self._wants_force_imperative = False
                threshold = 0.0
            block = _wl.render_block(
                matured, now,
                user_display_name=self.user_display_name,
                imperative_threshold=threshold,
            )
            if block.startswith("Something you've been wanting"):
                strongest = max(matured.wants, key=lambda w: w.pressure)
                log.info(
                    "wants-ledger imperative fire: id=%s pressure=%.2f "
                    "source=%s",
                    strongest.id, strongest.pressure, strongest.source,
                )
                # K55: an imperative want directive opens Aiko's
                # thread just like a K53 initiative turn does.
                self._pending_thread_open = {
                    "source": "want_imperative",
                    "topic": strongest.text,
                }
            return block
        except Exception:
            log.debug("wants block render failed", exc_info=True)
            return ""

    def _render_emotion_episode_block(self, user_text: str) -> str:
        """K57: render the strongest live directed-emotion episode.

        Per turn: read the kv store, apply wall-clock decay, run
        acknowledgment detection against the live ``user_text``
        (an ack resolves the episode and arms the thaw), persist,
        then render — the one-shot thaw cue outranks a live episode
        because the visible transition is the point. MCP
        ``force_emotion_episode`` writes straight into the kv store,
        so no force flag is needed here.
        """
        if not bool(
            getattr(self._settings.agent, "emotion_episodes_enabled", True)
        ):
            return ""
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return ""
        try:
            from datetime import datetime, timezone

            from app.core.affect import emotion_episodes as _ee

            now = datetime.now(timezone.utc)
            raw = chat_db.kv_get(_ee.KV_EMOTION_EPISODES)
            state = _ee.deserialize(raw)
            if not state.episodes and state.pending_thaw is None:
                return ""
            state = _ee.apply_decay(state, now)

            text = (user_text or "").strip()
            if text:
                for ep in list(state.episodes):
                    if _ee.detect_acknowledgment(ep, text):
                        state = _ee.resolve(
                            state, ep.emotion,
                            reason="they acknowledged it",
                        )
                        log.info(
                            "emotion-episode resolved: emotion=%s "
                            "reason=acknowledged cause=%s",
                            ep.emotion, ep.cause[:80],
                        )

            state, thaw = _ee.consume_thaw(state)
            try:
                chat_db.kv_set(
                    _ee.KV_EMOTION_EPISODES, _ee.serialize(state),
                )
            except Exception:
                log.debug("emotion episode persist failed", exc_info=True)

            # K60 — tsundere expression mask. The felt episode stays
            # truthful in the kv state above; only the expressed cue
            # transforms below. Hard sincerity rail: the mask drops
            # unconditionally on a support arc (deflecting real pain
            # is the one unforgivable tsundere failure mode).
            from app.core.affect import expression_mask as _mask

            mode = _mask.normalize_mode(
                getattr(self._settings.agent, "expression_mask", "off")
            )
            if mode != _mask.MODE_OFF:
                try:
                    arc_store = getattr(self, "_arc_store", None)
                    if arc_store is not None:
                        arc = str(
                            arc_store.get_or_default(self._user_id).arc
                        )
                        if arc == "support":
                            mode = _mask.MODE_OFF
                except Exception:
                    log.debug("mask arc check failed", exc_info=True)

            strength = 1.0
            if mode != _mask.MODE_OFF:
                try:
                    axes_store = getattr(
                        self, "_relationship_axes_store", None,
                    )
                    if axes_store is not None:
                        axes = axes_store.get(self._user_id)
                        strength = _mask.mask_strength(
                            getattr(axes, "closeness", None),
                            getattr(axes, "trust", None),
                        )
                except Exception:
                    strength = 1.0

                # Caught-caring outranks everything: the user just
                # named her warmth, the flustered denial IS the reply.
                if _mask.detect_caught_caring(text):
                    log.info(
                        "mask caught-caring fire: mode=%s strength=%.2f",
                        mode, strength,
                    )
                    return _mask.render_caught_caring_block(
                        user_display_name=self.user_display_name,
                        strength=strength,
                    )

            if thaw is not None:
                log.info(
                    "emotion-episode thaw: emotion=%s reason=%s",
                    thaw[0], thaw[2],
                )
                rendered_thaw = _ee.render_thaw_block(
                    thaw, user_display_name=self.user_display_name,
                )
                if mode == _mask.MODE_FULL:
                    rendered_thaw += (
                        " (Mask: even the thaw comes out grudging -- "
                        "\"...okay, fine. We're good. Stop smiling.\")"
                    )
                return rendered_thaw
            episode = _ee.strongest(state)
            if episode is None:
                return ""
            log.debug(
                "emotion-episode render: emotion=%s intensity=%.2f",
                episode.emotion, episode.intensity,
            )

            if mode != _mask.MODE_OFF and _mask.is_masked(
                episode.emotion, mode,
            ):
                # The slip: rare, earned, wall-clock budgeted. A
                # one-shot MCP flag (force_dere_slip) bypasses both
                # gates for end-to-end repro.
                force_slip = bool(
                    getattr(self, "_mask_force_slip_next", False)
                )
                if force_slip:
                    self._mask_force_slip_next = False
                cooldown_light = float(
                    getattr(
                        self._settings.agent,
                        "mask_slip_cooldown_days",
                        2.0,
                    )
                )
                slip = force_slip or _mask.should_slip(
                    mode=mode,
                    episode_intensity=episode.intensity,
                    last_slip_at=chat_db.kv_get(_mask.KV_LAST_SLIP_AT),
                    now=now,
                    cooldown_days_light=cooldown_light,
                    cooldown_days_full=cooldown_light * 2.5,
                )
                if slip:
                    try:
                        chat_db.kv_set(
                            _mask.KV_LAST_SLIP_AT, now.isoformat(),
                        )
                    except Exception:
                        log.debug("slip stamp failed", exc_info=True)
                log.info(
                    "mask render: emotion=%s mode=%s strength=%.2f "
                    "slip=%s",
                    episode.emotion, mode, strength, slip,
                )
                return _mask.render_masked_block(
                    emotion=episode.emotion,
                    cause=episode.cause,
                    user_display_name=self.user_display_name,
                    strength=strength,
                    slip=slip,
                )

            return _ee.render_block(
                episode,
                user_display_name=self.user_display_name,
                high_band=float(
                    getattr(self._settings.agent, "emotion_high_band", 0.5)
                ),
            )
        except Exception:
            log.debug("emotion episode block render failed", exc_info=True)
            return ""

    def _render_tease_collection_block(self) -> str:
        """K59: rare collection-opportunity cue from the tease ledger.

        Gate walk: master switch → humor-axis floor (the bit needs an
        established teasing register) → wall-clock cooldown since the
        last offer (``aiko.tease_last_offer_at`` kv stamp) → a debt
        old enough to be a *callback* (``tease_min_age_hours``).
        On fire: stamps the row ``offered_at`` (the post-turn settle
        pass checks the reply against it), bumps the cooldown stamp,
        and renders the permission slip. MCP ``force_tease_collection``
        arms a one-shot bypass of the humor + cooldown gates.
        """
        if not bool(
            getattr(self._settings.agent, "tease_economy_enabled", True)
        ):
            return ""
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return ""
        try:
            from datetime import datetime, timezone

            from app.core.relationship import tease_ledger as _tl

            force = bool(getattr(self, "_tease_collection_force_next", False))
            if force:
                self._tease_collection_force_next = False

            agent = self._settings.agent
            now = datetime.now(timezone.utc)

            if not force:
                # Humor-axis floor.
                humor = 0.0
                axes_store = getattr(self, "_relationship_axes_store", None)
                if axes_store is not None:
                    try:
                        humor = float(axes_store.get(self._user_id).humor)
                    except Exception:
                        humor = 0.0
                if humor < float(getattr(agent, "tease_min_humor", 0.2)):
                    return ""
                # Wall-clock cooldown between offers. J11 tilts it: if
                # teasing is the care language this user responds to, the
                # cooldown shortens a little (lengthens if it lands flat),
                # bounded by the bias band — never off.
                cooldown_h = float(
                    getattr(agent, "tease_collect_cooldown_hours", 12.0)
                )
                cooldown_h = cooldown_h / max(
                    0.1, self._affection_style_bias("teasing")
                )
                last_raw = chat_db.kv_get("aiko.tease_last_offer_at")
                if last_raw and cooldown_h > 0.0:
                    last = _tl._parse_iso(str(last_raw))
                    if last is not None:
                        elapsed_h = (now - last).total_seconds() / 3600.0
                        if elapsed_h < cooldown_h:
                            return ""

            state = _tl.expire(
                _tl.deserialize(chat_db.kv_get(_tl.KV_TEASE_LEDGER)),
                now,
                expiry_days=float(
                    getattr(agent, "tease_expiry_days", 14.0)
                ),
            )
            debt = _tl.pick_collectable(
                state,
                now,
                min_age_hours=(
                    0.0 if force
                    else float(getattr(agent, "tease_min_age_hours", 1.0))
                ),
            )
            if debt is None:
                chat_db.kv_set(
                    _tl.KV_TEASE_LEDGER, _tl.serialize(state),
                )
                return ""
            state = _tl.stamp_offered(state, debt.id, now)
            chat_db.kv_set(_tl.KV_TEASE_LEDGER, _tl.serialize(state))
            chat_db.kv_set("aiko.tease_last_offer_at", now.isoformat())
            log.info(
                "tease collection offered: what=%s source=%s age_h=%.1f",
                debt.what[:80],
                debt.source,
                (
                    (now - (_tl._parse_iso(debt.created_at) or now))
                    .total_seconds() / 3600.0
                ),
            )
            return _tl.render_block(
                debt, user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("tease collection render failed", exc_info=True)
            return ""

    def _render_topic_appetite_block(self) -> str:
        """K54: once-per-conversation "tapped out" negotiation slip.

        Combines the K18 standing lull reading
        (``TopicStagnationDetector.last_mean``), Aiko's own recent
        contribution pattern (share of short assistant replies), the
        strongest K52 want (the offer), and the relationship axes.
        Every input is best-effort — a sick store reads as its
        blocking value (no lull / no offer / cold axes) so the cue
        stays silent rather than firing on bad data. MCP
        ``force_topic_appetite`` arms ``_topic_appetite_force_next``
        to bypass everything except the arc block + offer
        requirement.
        """
        if not bool(
            getattr(self._settings.agent, "topic_appetite_enabled", True)
        ):
            return ""
        try:
            from app.core.conversation import topic_appetite as _tap

            agent = self._settings.agent

            arc = None
            arc_store = getattr(self, "_arc_store", None)
            if arc_store is not None:
                try:
                    arc_state = arc_store.get_or_default(self._user_id)
                    arc = getattr(arc_state, "arc", None)
                except Exception:
                    arc = None

            closeness = comfort = None
            axes_store = getattr(self, "_relationship_axes_store", None)
            if axes_store is not None:
                try:
                    axes = axes_store.get(self._user_id)
                    closeness = float(axes.closeness)
                    comfort = float(axes.comfort)
                except Exception:
                    closeness = comfort = None

            detector = getattr(self, "_topic_stagnation_detector", None)
            lull_mean = getattr(detector, "last_mean", None)

            short_share = None
            window = max(2, int(getattr(agent, "appetite_window", 6)))
            try:
                rows = self._inner_life_recent_messages(
                    max(window * 4, 20),
                )
                lengths: list[int] = []
                for row in reversed(rows):
                    if row.role != "assistant":
                        continue
                    content = (row.content or "").strip()
                    if not content:
                        continue
                    lengths.append(len(content))
                    if len(lengths) >= window:
                        break
                if len(lengths) >= window:
                    short_share = _tap.compute_short_reply_share(
                        lengths,
                        short_chars=int(
                            getattr(agent, "appetite_short_reply_chars", 160)
                        ),
                    )
            except Exception:
                short_share = None

            want_text = None
            want_pressure = 0.0
            chat_db = getattr(self, "_chat_db", None)
            if chat_db is not None:
                try:
                    from app.core.conversation import wants_ledger as _wl

                    state = _wl.deserialize(
                        chat_db.kv_get(_wl.KV_WANTS_LEDGER)
                    )
                    if state.wants:
                        strongest = max(
                            state.wants, key=lambda w: w.pressure,
                        )
                        want_text = strongest.text
                        want_pressure = float(strongest.pressure)
                except Exception:
                    want_text = None
                    want_pressure = 0.0

            force = bool(
                getattr(self, "_topic_appetite_force_next", False)
            )
            if force:
                self._topic_appetite_force_next = False

            decision = _tap.decide(
                already_fired=bool(
                    getattr(self, "_topic_appetite_fired", False)
                ),
                arc=arc,
                closeness=closeness,
                comfort=comfort,
                lull_mean=lull_mean,
                short_reply_share=short_share,
                want_text=want_text,
                want_pressure=want_pressure,
                lull_threshold=float(
                    getattr(
                        self._memory_settings,
                        "stagnation_mild_threshold",
                        0.18,
                    )
                ),
                short_share_threshold=float(
                    getattr(agent, "appetite_short_share_threshold", 0.6)
                ),
                min_want_pressure=float(
                    getattr(agent, "appetite_min_want_pressure", 0.35)
                ),
                min_axes=float(getattr(agent, "appetite_min_axes", 0.15)),
                force=force,
            )
            log.debug(
                "topic-appetite: reason=%s lull=%s short_share=%s "
                "pressure=%.2f",
                decision.reason,
                f"{lull_mean:.3f}" if lull_mean is not None else "n/a",
                f"{short_share:.2f}" if short_share is not None else "n/a",
                want_pressure,
            )
            if not decision.fire:
                return ""
            self._topic_appetite_fired = True
            log.info(
                "topic-appetite fire: lull=%s short_share=%s "
                "pressure=%.2f want=%s",
                f"{lull_mean:.3f}" if lull_mean is not None else "n/a",
                f"{short_share:.2f}" if short_share is not None else "n/a",
                want_pressure,
                (want_text or "")[:60],
            )
            return _tap.render_block(
                want_text or "",
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("topic appetite block render failed", exc_info=True)
            return ""


