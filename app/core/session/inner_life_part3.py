from __future__ import annotations

import logging
from typing import Any
from app.core.infra import timephrase
from app.core.session.debug_overrides import DebugOverridesHostMixin


log = logging.getLogger("app.session")


class InnerLifePart3Mixin(DebugOverridesHostMixin):
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
          biographical facts. L28's concept candidates (see
          :meth:`_stance_concept_candidates`) are exempt -- their kind
          establishes them as stances.
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
            self._debug_overrides.take("opinion_injection_force_next", False)
        )

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
        # L28: her durable stances join the same pool. A concept can be the
        # only place a recurring taste lives -- synthesis is what happens to
        # the ones that come up often -- so without this the opinions she
        # holds most firmly were the ones she couldn't notice being pushed
        # on. Appended, so a self-memory and a concept saying the same thing
        # compete on cosine and the sharper wording wins.
        self_memories.extend(self._stance_concept_candidates())
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
                # Carried across the turn boundary so the deferred cue still
                # knows not to tell her she wrote a concept.
                "stance_origin": result.stance_origin,
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
        # The quote is the subject: ``what`` is the same sentence on
        # every pushback, so keying on it would have each debt
        # supersede the last.
        try:
            quote = " ".join((user_text or "").split())[:120]
            self._bank_tease_debt(
                what="they pushed back hard on a take of yours",
                context=f'they said "{quote}"' if quote else "",
                source="opinion_pushback",
                subject=quote,
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

    def _stance_concept_candidates(self) -> list[Any]:
        """L28: her ``stance`` diet, adapted into K29 stance candidates.

        The diet is ``value`` / ``taste`` / ``pursuit`` at ``subject=aiko``
        -- values alone would make every opinion of hers a principle, which
        is the register K29's cue text works hardest to avoid. Taste and
        pursuit are what let a stance be a preference she simply has.

        Read through ``for_consumer`` rather than ``relevant``, unlike the
        L18c boundary-clash sibling: the detector runs its own cosine pass
        over the whole candidate pool and picks one winner, so pre-ranking
        by similarity here would just be a second, differently-tuned
        version of the same sort. The diet budget is what keeps the pool
        bounded.

        Returns ``[]`` on every failure -- cold view, no diet, read error --
        which lands K29 exactly on its pre-L28 behaviour.
        """
        try:
            from app.core.affect.opinion_injection_detector import StanceConcept
            from app.core.concepts.concept_view import concept_view_from
        except Exception:
            log.debug("opinion-injection: concept import failed", exc_info=True)
            return []
        try:
            view = concept_view_from(self)
        except Exception:
            log.debug("opinion-injection: concept view failed", exc_info=True)
            return []
        if view is None or not getattr(view, "enabled", False):
            return []
        try:
            rows = view.for_consumer("stance")
        except Exception:
            log.debug("opinion-injection: stance concept read failed", exc_info=True)
            return []
        out: list[Any] = []
        for concept in rows:
            candidate = StanceConcept.from_concept(concept)
            if candidate is not None:
                out.append(candidate)
        return out

    def _render_boundary_clash_block(self, user_text: str) -> str:
        """L18c: surface a soft cue when the live turn nears an active boundary.

        Sibling of :meth:`_render_opinion_injection_block` -- a provider-time
        detector that fires on the same turn the user message arrives. Simpler
        than K29: the raw material is active ``boundary`` concepts (read via
        ``ConceptView``), the gate is cosine-only (no hot-path LLM), and
        ``classify_pair`` only sharpens the cue's register (approach vs push).

        Guardrails mirror K29:

        * Master switch (``agent.boundary_clash_enabled``) flips it off.
        * Cooldown counter (``memory.boundary_clash_cooldown_turns``,
          default 5) decremented every call, armed on fire.
        * Per-session cap (``memory.boundary_clash_per_session_cap``,
          default 3) -- a standing boundary is background guidance, so the
          sharp in-the-moment cue never nags.
        * Cosine + word-count gates live in the detector module.
        """
        if not bool(
            getattr(self._settings.agent, "boundary_clash_enabled", True)
        ):
            return ""
        try:
            from app.core.affect import boundary_clash_detector
            from app.core.concepts.concept_view import concept_view_from
        except Exception:
            log.debug("boundary-clash import failed", exc_info=True)
            return ""

        # Decrement cooldown first so a quiet turn always whittles the
        # counter down even when nothing trips the trigger.
        current_cooldown = max(0, int(getattr(self, "_boundary_clash_cooldown", 0)))
        if current_cooldown > 0:
            self._boundary_clash_cooldown = current_cooldown - 1
        if self._boundary_clash_cooldown > 0:
            return ""

        session_cap = max(
            0,
            int(
                getattr(
                    self._memory_settings,
                    "boundary_clash_per_session_cap",
                    3,
                )
            ),
        )
        session_count = int(getattr(self, "_boundary_clash_session_count", 0))
        if session_cap > 0 and session_count >= session_cap:
            return ""

        view = concept_view_from(self)
        embedder = getattr(self, "_embedder", None)
        if view is None or not view.enabled or embedder is None:
            return ""

        try:
            user_vec = embedder.embed(user_text or "")
        except Exception:
            log.debug("boundary-clash: embedder failed", exc_info=True)
            return ""

        # All active boundaries, nearest first; the detector applies the
        # cosine floor so pass min_sim=0.0 here (subject is left unset so
        # user / relationship / aiko lines all compete).
        try:
            pairs = view.relevant(user_vec, kind="boundary", k=8, min_sim=0.0)
        except Exception:
            log.debug("boundary-clash: concept read failed", exc_info=True)
            return ""
        if not pairs:
            return ""

        candidates = [
            boundary_clash_detector.BoundaryCandidate(
                concept_id=int(getattr(c, "concept_id", 0)),
                subject=str(getattr(c, "subject", "") or "user"),
                label=str(getattr(c, "label", "") or ""),
                cosine=float(sim),
            )
            for (c, sim) in pairs
        ]

        memory_settings = self._memory_settings
        try:
            result = boundary_clash_detector.detect(
                user_text or "",
                candidates=candidates,
                min_cosine=float(
                    getattr(
                        memory_settings,
                        "boundary_clash_min_cosine",
                        boundary_clash_detector.DEFAULT_MIN_COSINE,
                    )
                ),
                min_user_words=int(
                    getattr(
                        memory_settings,
                        "boundary_clash_min_user_words",
                        boundary_clash_detector.DEFAULT_MIN_USER_WORDS,
                    )
                ),
            )
        except Exception:
            log.debug("boundary-clash detector raised", exc_info=True)
            return ""

        if result is None:
            return ""

        cooldown_turns = max(
            0,
            int(
                getattr(
                    self._memory_settings,
                    "boundary_clash_cooldown_turns",
                    5,
                )
            ),
        )
        self._boundary_clash_cooldown = cooldown_turns
        self._boundary_clash_session_count = session_count + 1
        self._last_boundary_clash = result

        log.info(
            "boundary-clash fire: trigger=%s cosine=%.3f concept_id=%d "
            "subject=%s heuristic=%s cooldown_set=%d session_count=%d",
            result.trigger,
            result.cosine,
            result.concept_id,
            result.subject,
            result.heuristic_label,
            cooldown_turns,
            self._boundary_clash_session_count,
        )

        try:
            return boundary_clash_detector.render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("boundary-clash render failed", exc_info=True)
            return ""

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
            stance_origin=str(
                pending.get("stance_origin", "")
                or opinion_injection_detector.ORIGIN_MEMORY
            ),
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
                subject=quote,
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
            self._debug_overrides.take("stance_persistence_force_next", False)
        )

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

    def _render_long_arc_callback_block(self, user_text: str) -> str:
        """K63: rarely surface a "weeks ago you said…" long-arc callback.

        Where K22 catches short-horizon callbacks, this reaches *weeks or
        months* back to connect the live turn to something the user told
        Aiko long ago — the strongest "she actually knows me" beat a
        companion can produce, so it's paced hard for rarity:

        * Master switch (``agent.long_arc_callback_enabled``).
        * Per-session cap (``memory.long_arc_callback_per_session_cap``,
          default 1), checked *before* the embed so the search only runs
          on a genuinely eligible turn. Spacing between callbacks is the
          type's ``surface_cooldown_hours``.
        * Short turns (< ``memory.long_arc_callback_min_user_words``) are
          skipped — too little topic to anchor a callback.
        * The aged retrieval lane keeps only memories at least
          ``min_age_days`` old whose cosine clears ``min_cosine`` (a higher
          bar than normal RAG), and a don't-repeat ring suppresses anything
          recently surfaced. Leans on K25 hedging via the tentative cue.

        Because the pick is made against the live message, the pool row
        is written here rather than by a producer — the surface-time
        ledger the gap-return cues use. That is what stops an ignored
        callback from being spent: it is released back to ``pending``
        after the turn and re-offered while they are still on the same
        thread (:func:`long_arc_callback.still_relevant`), instead of
        being burned into the don't-repeat ring having said nothing.
        A retry is exempt from the per-session cap and the cadence for
        the same reason — it is one offer being finished, not a second
        one being opened.

        MCP debug: ``force_long_arc_callback`` arms a one-shot
        ``_long_arc_callback_force_next`` that bypasses the cap + cadence +
        min-words gates (but NOT the age / cosine / kind gates — a forced
        bypass on a turn with no old topical memory still silently
        expires).
        """
        if not bool(
            getattr(self._settings.agent, "long_arc_callback_enabled", True)
        ):
            return ""
        retriever = getattr(self, "_rag_retriever", None)
        if retriever is None or not hasattr(
            retriever, "aged_callback_candidate"
        ):
            return ""
        try:
            from app.core.conversation import long_arc_callback as lac
        except Exception:
            log.debug("long-arc-callback import failed", exc_info=True)
            return ""

        force_next = bool(
            self._debug_overrides.take("long_arc_callback_force_next", False)
        )

        mem = self._memory_settings

        now = timephrase.utcnow()
        kv_get = self._chat_db.kv_get
        kv_set = self._chat_db.kv_set

        retry = self.take_pool_cue(
            "long_arc_callback",
            relevant=lambda payload: lac.still_relevant(payload, user_text),
            force=force_next,
        )
        if retry is not None:
            log.info(
                "long-arc-callback retry: cue=%s mem=%s",
                retry.id, retry.payload.get("memory_id"),
            )
            return retry.text

        if not force_next:
            cap = max(
                0, int(getattr(mem, "long_arc_callback_per_session_cap", 1))
            )
            if int(getattr(self, "_long_arc_callback_session_count", 0)) >= cap:
                return ""
            if self._cadence_blocked("long_arc_callback"):
                return ""
            min_words = max(
                0, int(getattr(mem, "long_arc_callback_min_user_words", 5))
            )
            if len((user_text or "").split()) < min_words:
                return ""

        try:
            candidates = retriever.aged_callback_candidate(
                user_text or "",
                min_age_days=int(
                    getattr(mem, "long_arc_callback_min_age_days", 21)
                ),
                min_cosine=float(
                    getattr(mem, "long_arc_callback_min_cosine", 0.55)
                ),
                allowed_kinds=lac.ALLOWED_KINDS,
                top_k=lac.CANDIDATE_TOP_K,
            )
        except Exception:
            log.debug("long-arc-callback aged search raised", exc_info=True)
            return ""
        if not candidates:
            return ""

        recent_ids = lac.load_recent_ids(kv_get)
        pick = lac.select(candidates, exclude_ids=recent_ids)
        if pick is None:
            return ""

        try:
            block = lac.render_block(
                pick, user_display_name=self.user_display_name, now=now,
            )
        except Exception:
            log.debug("long-arc-callback render failed", exc_info=True)
            return ""
        if not block:
            return ""

        # Arm the rarity gates only on an actual fire. The cadence rides
        # on the ledger row's ``last_surfaced_at``, so writing it is what
        # spaces the next callback.
        row = self.record_surfaced_cue(
            "long_arc_callback",
            lac.snippet(pick.content),
            block,
            payload={
                "memory_id": pick.memory_id,
                "kind": pick.kind,
                "snippet": lac.snippet(pick.content),
                "cosine": round(pick.cosine, 3),
                "age_days": round(pick.age_days, 1),
            },
        )
        lac.append_recent_id(kv_get, kv_set, pick.memory_id)
        self._long_arc_callback_session_count = (
            int(getattr(self, "_long_arc_callback_session_count", 0)) + 1
        )
        self._last_long_arc_callback = {
            "memory_id": pick.memory_id,
            "kind": pick.kind,
            "cosine": round(pick.cosine, 3),
            "age_days": round(pick.age_days, 1),
            "forced": force_next,
            "cue_id": getattr(row, "id", 0),
        }
        log.info(
            "long-arc-callback fire: mem=%d kind=%s cosine=%.3f age_days=%.1f "
            "forced=%s cue=%s",
            pick.memory_id,
            pick.kind,
            pick.cosine,
            pick.age_days,
            force_next,
            getattr(row, "id", 0),
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
            self._debug_overrides.take("self_noticing_force_agreement", False)
        )
        if agreement_force:
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
            self._debug_overrides.take("self_noticing_force_flat_affect", False)
        )
        if flat_force:
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
            self._debug_overrides.take(
                "self_noticing_force_repeated_thought", False,
            )
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
            # The override is already consumed by the take() above; this is
            # the other half of the pair.
            self._repeated_thought_fired_last_turn = False
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
        """K9: surface up to two pending "quiet curiosity" seeds.

        Reads the cue pool; no per-turn LLM, no embedder. Two rather
        than one because a seed is a topic, not a sentence, and a pair
        gives the model somewhere to go when the first does not fit --
        the only pooled block that surfaces more than a single cue.
        Fairness comes from the pool's ordering, which puts a seed she
        has never seen ahead of one she has already passed on.

        Both are marked surfaced, so both are judged post-turn and both
        burn a surfacing whether or not she takes either. That is the
        honest accounting: they were in the prompt.
        """
        if not bool(
            getattr(self._settings.agent, "curiosity_seed_enabled", True)
        ):
            return ""
        if self._question_balance_suppressed():
            return ""
        rendered: list[str] = []
        for _ in range(2):
            row = self.take_pool_cue("curiosity_seed")
            if row is None:
                break
            topic = (row.subject or "").strip()
            if not topic:
                continue
            if len(topic) > 120:
                topic = topic[:119].rstrip(",;: ") + "…"
            rendered.append(f"- {topic}")
        if not rendered:
            return ""
        # Bare topics under the persona's own header. The "only if a soft
        # pivot lands naturally" qualifier that used to be baked into this
        # line is in that hoisted section, which arrives in T6 alongside
        # this block whenever it renders.
        return "Quiet curiosity:\n" + "\n".join(rendered)

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

            force = bool(
                self._debug_overrides.take("initiative_force_next", False)
            )

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
        """K55 / K89: evaluate the reply to a thread Aiko opened.

        Runs while ``_owned_thread`` is set (stamped post-turn when a
        K53 directive / K52 imperative fired). An engaged reply clears
        it silently; a short pivot spends part of the thread's stake
        and renders a return cue; a substantial reply about something
        else retires it without a word. K89 lets a thread survive its
        first evaluation, so the slot is re-armed from the outcome
        rather than always cleared -- the thread is worth two returns
        at most and dies of age, stake or a cooling cosine before that.

        A blank ``user_text`` (proactive turn) skips the evaluation
        without touching the thread -- the cue should judge a real
        reply, not a silence.
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
        # Clear the slot before anything can raise, so a sick embedder
        # drops the thread rather than re-evaluating it every turn. It
        # is re-armed below only when ``advance`` says the thread lives.
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
            outcome = _town.advance(
                thread,
                verdict,
                max_returns=int(getattr(agent, "thread_max_returns", 2)),
                stake_decay=float(
                    getattr(agent, "thread_stake_decay", 0.35)
                ),
                min_stake=float(getattr(agent, "thread_min_stake", 0.25)),
                max_age_minutes=float(
                    getattr(agent, "thread_max_age_minutes", 45.0)
                ),
                cooling_margin=float(
                    getattr(agent, "thread_cooling_margin", 0.05)
                ),
            )
            log.info(
                "thread-ownership: verdict=%s cosine=%s chars=%d "
                "source=%s outcome=%s returns=%d topic=%s",
                verdict.verdict,
                f"{verdict.cosine:.3f}" if verdict.cosine is not None
                else "n/a",
                verdict.reply_chars,
                thread.source,
                outcome.reason,
                thread.returns_used,
                thread.topic[:60],
            )
            self._owned_thread = outcome.thread
            if not outcome.cue:
                return ""
            # K57: a brushed-off thread is a light miffed trigger —
            # comedy-weight, not a real sulk (the post-turn drain
            # applies it). Only on the FIRST brush-off: K89's second
            # return would otherwise stack a second sulk on top of the
            # gentler nudge, which is the opposite of gentler.
            if thread.returns_used == 0:
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
                    log.debug(
                        "thread-pivot miffed queue failed", exc_info=True,
                    )
            return _town.render_return_block(
                thread.topic,
                user_display_name=self.user_display_name,
                attempt=thread.returns_used + 1,
                last=outcome.thread is None,
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

            from app.core.conversation import wants_ledger as _wl

            agent = self._settings.agent
            now = timephrase.utcnow()
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
            if self._debug_overrides.take("wants_force_imperative", False):
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

            from app.core.affect import emotion_episodes as _ee

            now = timephrase.utcnow()
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
                    self._debug_overrides.take("mask_force_slip_next", False)
                )
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
        last offer → claim the oldest ripe debt from the pool. MCP
        ``force_tease_collection`` arms a one-shot bypass of the humor
        and cooldown gates.

        Everything downstream of the claim is the pool's: the row is
        marked ``surfaced`` by ``take_pool_cue``, post-turn matching
        decides whether she collected, and a miss releases it to come
        round again. The two gates left here are the two the pool cannot
        express -- an axis floor, and a cooldown that J11 moves.
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "tease_economy_enabled", True)):
            return ""
        try:
            force = bool(
                self._debug_overrides.take("tease_collection_force_next", False)
            )
            if not force and not self._tease_collection_open(agent):
                return ""
            row = self.take_pool_cue("tease_ledger", force=force)
            if row is None:
                return ""
            log.info(
                "tease collection offered: subject=%s source=%s",
                row.subject[:80], row.payload.get("source", "-"),
            )
            return row.text
        except Exception:
            log.debug("tease collection render failed", exc_info=True)
            return ""

    def _tease_collection_open(self, agent: Any) -> bool:
        """The two K59 gates the cue policy has no field for.

        The humor floor is a relationship-axis read, which the pool
        knows nothing about. The cooldown *could* have been
        ``surface_cooldown_hours`` were it a constant, but J11 divides
        it by the affection-style bias -- if teasing is the care
        language this user responds to the interval shortens, and it
        lengthens when the jabs land flat -- so it is spent here
        instead, against the same ``last_surfaced_at`` the policy
        version reads.
        """
        humor = 0.0
        axes_store = getattr(self, "_relationship_axes_store", None)
        if axes_store is not None:
            try:
                humor = float(axes_store.get(self._user_id).humor)
            except Exception:
                humor = 0.0
        if humor < float(getattr(agent, "tease_min_humor", 0.2)):
            return False
        cooldown_h = float(
            getattr(agent, "tease_collect_cooldown_hours", 12.0)
        ) / max(0.1, self._affection_style_bias("teasing"))
        if cooldown_h <= 0.0:
            return True
        store = self._cue_pool_store()
        if store is None:
            return True
        last = timephrase.parse_iso(store.last_surfaced_at("tease_ledger"))
        if last is None:
            return True
        elapsed_h = (timephrase.utcnow() - last).total_seconds() / 3600.0
        return elapsed_h >= cooldown_h

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
                self._debug_overrides.take("topic_appetite_force_next", False)
            )

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

    def _render_taste_lean_block(self) -> str:
        """K81: a rare, lull-gated permission slip to steer toward a topic
        Aiko genuinely enjoys.

        Shaped like the K54 appetite slip: fires at most once per
        conversation, only on a standing lull (the K18
        ``TopicStagnationDetector.last_mean`` reading) with relationship
        warmth earned, and only when she holds an active ``taste`` concept
        confident enough to lean on. It is framed as enthusiasm ("you're
        allowed to steer toward what you love"), never a filter on what he
        may raise. L42 now supplies the counterweight: a current concentration
        or fixation finding suppresses this optional steer so learned taste
        cannot deepen a rut. Every input is best-effort: a cold store / missing
        signal reads as its blocking value so the slip stays silent.

        K85a widened the read past ``taste``. Two taste rows have ever
        been mined, so on the numbers this block was silent almost
        always -- not because the gates were tight but because there was
        nothing behind them. See :meth:`_widened_lean`.
        """
        if not bool(
            getattr(self._settings.agent, "taste_steer_enabled", True)
        ):
            return ""
        if bool(getattr(self, "_taste_lean_fired", False)):
            return ""
        try:
            from app.core.concepts.concept_view import concept_view_from

            if not self._lean_gate_open("taste_lean_force_next", "taste"):
                return ""

            view = concept_view_from(self)
            if view is None or not view.enabled:
                return ""
            # Only an aiko taste is hers to lean on; take the single
            # strongest one above a modest confidence bar.
            min_conf = float(
                getattr(
                    self._memory_settings, "taste_steer_min_confidence", 0.6
                )
            )
            tastes = view.core(
                subject="aiko", kind="taste",
                min_confidence=min_conf, limit=1,
            )
            kind = "taste"
            if not tastes:
                tastes = self._widened_lean(view, min_conf)
                kind = str(getattr(tastes[0], "kind", "")) if tastes else ""
            if not tastes:
                return ""
            label = (getattr(tastes[0], "label", "") or "").strip()
            if not label:
                return ""
        except Exception:
            log.debug("taste-lean block render failed", exc_info=True)
            return ""

        self._taste_lean_fired = True
        log.info("taste-lean fire: kind=%s label=%s", kind, label[:60])
        return self._render_lean_copy(kind, label)

    def _lean_gate_open(self, force_key: str, label: str) -> bool:
        """The shared pacing gate for the T6 lean slips (K81 / K85e).

        Three conditions, all of which read as blocking when their input
        is cold: no L42 concentration / fixation finding (so a lean can
        never deepen a rut she is already in), a standing K18 lull, and
        relationship warmth earned on at least one axis. The debug
        override skips all three.
        """
        if bool(self._debug_overrides.take(force_key, False)):
            return True

        from app.core.concepts.surfacing_conduct import load_conduct_snapshot

        chat_db = getattr(self, "_chat_db", None)
        conduct = (
            load_conduct_snapshot(chat_db.kv_get)
            if chat_db is not None
            and bool(
                getattr(
                    self._settings.agent, "surfacing_conduct_enabled", True,
                )
            )
            else []
        )
        if any(
            row.get("shape") in {"concentration", "fixation"}
            for row in conduct
        ):
            log.debug("%s-lean suppressed by L42 conduct finding", label)
            return False

        detector = getattr(self, "_topic_stagnation_detector", None)
        lull_mean = getattr(detector, "last_mean", None)
        lull_threshold = float(
            getattr(self._memory_settings, "stagnation_mild_threshold", 0.18)
        )
        if lull_mean is None or float(lull_mean) < lull_threshold:
            return False

        # Warmth earned: a lean toward something of hers is a familiarity
        # she has to have earned, so a cold bond reads as no-fire.
        closeness = comfort = None
        axes_store = getattr(self, "_relationship_axes_store", None)
        if axes_store is not None:
            try:
                axes = axes_store.get(self._user_id)
                closeness = float(axes.closeness)
                comfort = float(axes.comfort)
            except Exception:
                closeness = comfort = None
        min_axes = float(
            getattr(self._settings.agent, "appetite_min_axes", 0.15)
        )
        return not (
            closeness is None
            or comfort is None
            or (closeness < min_axes and comfort < min_axes)
        )

    def _render_pursuit_lean_block(self) -> str:
        """K85e: a lull slip for a subject that is hers, not theirs.

        Same pacing as the K81 taste lean and the same L42 counterweight,
        and it shares taste's once-per-conversation latch -- there is one
        permission slip here with two possible sources, and firing both
        on the same quiet turn would read as a woman with an agenda.

        It runs *first* of the two because it is the one the whole K85
        family exists for. Taste is bond-scoped by construction ("topics
        you enjoy getting into with him"), so leaning on it still points
        the lull back at him; a pursuit is the only thing in the store
        that doesn't.

        Deliberately not a question and not an announcement. The failure
        mode for this block is a hobbyhorse -- her turning a quiet moment
        into a monologue about her tomatoes -- so the copy asks for the
        small concrete thing that happened and then stops.
        """
        if not bool(
            getattr(self._settings.agent, "pursuit_lean_enabled", True)
        ):
            return ""
        if bool(getattr(self, "_taste_lean_fired", False)):
            return ""
        try:
            from app.core.concepts.concept_view import concept_view_from

            if not self._lean_gate_open("pursuit_lean_force_next", "pursuit"):
                return ""
            view = concept_view_from(self)
            if view is None or not view.enabled:
                return ""
            min_conf = float(
                getattr(
                    self._memory_settings, "taste_steer_min_confidence", 0.6
                )
            )
            rows = view.core(
                subject="aiko", kind="pursuit",
                min_confidence=min_conf, limit=1,
            )
            if not rows:
                return ""
            label = (getattr(rows[0], "label", "") or "").strip()
            if not label:
                return ""
        except Exception:
            log.debug("pursuit-lean block render failed", exc_info=True)
            return ""

        self._taste_lean_fired = True
        log.info("pursuit-lean fire: label=%s", label[:60])
        return (
            "Something you've been up to:\n"
            f"Things have gone a little quiet, and this is true of you: "
            f"{label}. If there's room, say a small concrete thing about it "
            "-- what you actually did with it lately, or the bit of it "
            "you're stuck on. Offer it and leave it there; don't build it "
            "into a topic, don't ask him to be interested, and let it drop "
            "the moment he'd rather talk about something else."
        )

    def _widened_lean(self, view: Any, min_conf: float) -> list:
        """K85a: fall back to her other self-concepts when taste is empty.

        Taste is the natural fit -- it is literally "a topic she enjoys" --
        but only two rows have ever been mined, so the block almost never
        fires. The other ``subject="aiko"`` kinds are not starved at all:
        there are around a hundred active value / aspiration / identity
        rows. What most of them are *not* is hers, in the sense this block
        needs: three quarters name the user or describe the bond, and
        leaning on one of those just points the lull back at him. Hence
        the filter.

        This is a stopgap and worth naming as one. A value is not a topic,
        so the copy has to change shape (see :meth:`_render_lean_copy`),
        and even the surviving labels skew toward how she reasons rather
        than toward anything she could raise over tea. The real supply
        line is the ``pursuit`` kind.
        """
        if not bool(
            getattr(self._settings.agent, "taste_steer_widen_enabled", True)
        ):
            return []
        from app.core.concepts.own_subject import own_subjects

        for kind in ("aspiration", "value", "identity"):
            rows = view.core(
                subject="aiko", kind=kind, min_confidence=min_conf,
            )
            mine = own_subjects(rows, self.user_display_name)
            if mine:
                return mine[:1]
        return []

    def _render_lean_copy(self, kind: str, label: str) -> str:
        """The cue for one lean. A taste is a topic; the rest are not.

        Steering the conversation toward "I value distinguishing between
        confidence and importance in my reasoning" would produce a
        lecture, so the widened kinds get asked for something different:
        say the position out loud and let it stand as hers.
        """
        name = self.user_display_name
        tail = (
            "This is yours: it colours how much you light up, never a rule "
            "about what he has to want, so let it go the moment he'd rather "
            "be elsewhere."
        )
        if kind == "taste":
            return (
                "Leaning toward what you love:\n"
                f"Things have gone a little quiet. One of the topics you "
                f"genuinely enjoy getting into with {name} is right here to "
                f"lean on: {label}. If it fits, steer gently toward it -- "
                f"offer something concrete you'd love to dig into together. "
                f"{tail}"
            )
        return (
            "Something of yours to put on the table:\n"
            f"Things have gone a little quiet, and this is true of you: "
            f"{label} Say the plain version of it out loud if there's room "
            "-- not as a topic to steer him onto and not as a confession, "
            f"just a thing you think, offered so he has something to react "
            f"to. Don't explain the machinery behind it. {tail}"
        )

    def _render_concept_learning_block(self, user_text: str = "") -> str:
        """L17e: rare permission to say out loud that she changed her mind.

        The one place the learning history touches the conversation. It is
        deliberately the narrowest surface in the L17 stack:

        - It reads **only** the bounded snapshot the drift worker left
          behind. No trajectory scan, no graph walk, no embedding, no LLM
          call on the turn path.
        - It hands the chat model **old, new and because** and nothing
          else. No confidence, no salience, no shape, no ids, no event
          types -- the machinery is not hers to narrate, and a belief
          revision said in the vocabulary of scores stops being one.
        - It fires at most once per conversation, once per change, and
          once per long global cooldown, behind trust plus either a lull
          or genuine live relevance.

        The framing asks for a fallible statement rather than a question,
        so it cannot become the reassurance-seeking move K47 balances
        against.
        """
        agent = self._settings.agent
        if (
            not bool(getattr(agent, "concepts_enabled", False))
            or not bool(
                getattr(agent, "concept_learning_reflection_enabled", True)
            )
            or bool(getattr(self, "_learning_reflection_fired", False))
        ):
            return ""
        try:
            import json

            from app.core.concepts.concept_drift_worker import (
                DRIFT_PENDING_KEY,
            )

            chat_db = getattr(self, "_chat_db", None)
            if chat_db is None:
                return ""
            try:
                pending = json.loads(chat_db.kv_get(DRIFT_PENDING_KEY) or "[]")
            except (TypeError, ValueError):
                return ""
            if not isinstance(pending, list) or not pending:
                return ""

            force = bool(
                self._debug_overrides.take(
                    "concept_learning_force_next", False
                )
            )
            settings = self._memory_settings
            fired_key = "concept.drift.last_reflection_fp"
            seen = str(chat_db.kv_get(fired_key) or "")
            item = next(
                (
                    row
                    for row in pending
                    if isinstance(row, dict)
                    and str(row.get("fingerprint", "")) != seen
                    and str(row.get("new", "")).strip()
                ),
                None,
            )
            if item is None:
                return ""

            if not force:
                if not self._learning_reflection_allowed(item, user_text):
                    return ""
                last_raw = chat_db.kv_get("concept.drift.last_reflection")
                last = timephrase.parse_iso(last_raw) if last_raw else None
                cooldown = max(
                    1.0,
                    float(
                        getattr(
                            settings, "concept_reflection_cooldown_days", 30.0
                        )
                    ),
                )
                if last is not None and (
                    timephrase.utcnow() - last
                ).total_seconds() < cooldown * 86400:
                    return ""

            old = str(item.get("old", "")).strip()
            new = str(item.get("new", "")).strip()
            because = str(item.get("because", "")).strip()
            chat_db.kv_set(
                "concept.drift.last_reflection",
                timephrase.utcnow().isoformat(),
            )
            chat_db.kv_set(fired_key, str(item.get("fingerprint", "")))
            self._learning_reflection_fired = True

            shift = (
                f"You used to read it as: {old}. You'd put it differently "
                f"now: {new}."
                if old
                else f"Something you've settled into thinking: {new}."
            )
            grounds = f" What moved you: {because}." if because else ""
            return (
                "Something you understand differently now:\n"
                f"{shift}{grounds} If it genuinely fits the conversation, "
                "you may say once, briefly, that your read on this has "
                "changed -- in your own words, as a fallible personal "
                "shift, not a report. State it rather than asking whether "
                "it's right, and never mention memory, tracking, "
                "confidence, analysis, or any machinery behind it. If it "
                "doesn't fit naturally, say nothing."
            )
        except Exception:
            log.debug("concept-learning block render failed", exc_info=True)
            return ""

    def _learning_reflection_allowed(
        self, item: dict[str, Any], user_text: str
    ) -> bool:
        """Trust plus either a lull or genuine live relevance.

        A belief revision is an intimate thing to volunteer, so it needs
        warmth to land; and it needs an opening, which is either that
        nothing else is going on or that the conversation is already on
        the subject.
        """
        settings = self._memory_settings
        if float(item.get("salience", 0.0) or 0.0) < float(
            getattr(settings, "concept_reflection_min_salience", 0.6)
        ):
            return False
        axes_store = getattr(self, "_relationship_axes_store", None)
        if axes_store is None:
            return False
        axes = axes_store.get(self._user_id)
        min_axes = float(
            getattr(settings, "concept_reflection_min_axes", 0.3)
        )
        if float(axes.trust) < min_axes:
            return False
        if max(float(axes.closeness), float(axes.comfort)) < min_axes:
            return False

        # Live relevance, checked lexically so the turn path stays free of
        # embeddings: is the conversation already about this belief?
        from app.core.concepts.concept_drift import label_tokens

        turn = label_tokens(user_text)
        subject = label_tokens(item.get("new", "")) | label_tokens(
            item.get("old", "")
        )
        if turn and subject and len(turn & subject) >= 2:
            return True

        detector = getattr(self, "_topic_stagnation_detector", None)
        lull = getattr(detector, "last_mean", None)
        threshold = float(
            getattr(settings, "stagnation_mild_threshold", 0.18)
        )
        return lull is not None and float(lull) >= threshold

    def _render_conduct_notice_block(self) -> str:
        """L42: rare permission to acknowledge a relationship habit naturally."""
        agent = self._settings.agent
        if (
            not bool(getattr(agent, "surfacing_conduct_enabled", True))
            or not bool(
                getattr(agent, "surfacing_conduct_notice_enabled", True)
            )
            or bool(getattr(self, "_conduct_notice_fired", False))
        ):
            return ""
        try:
            from app.core.concepts.concept_view import concept_view_from
            from app.core.concepts.surfacing_conduct import load_conduct_snapshot

            force = bool(
                self._debug_overrides.take("conduct_notice_force_next", False)
            )
            chat_db = getattr(self, "_chat_db", None)
            if chat_db is None or not load_conduct_snapshot(chat_db.kv_get):
                return ""
            if not force:
                detector = getattr(self, "_topic_stagnation_detector", None)
                lull = getattr(detector, "last_mean", None)
                threshold = float(
                    getattr(
                        self._memory_settings,
                        "stagnation_mild_threshold",
                        0.18,
                    )
                )
                if lull is None or float(lull) < threshold:
                    return ""
                axes_store = getattr(self, "_relationship_axes_store", None)
                if axes_store is None:
                    return ""
                axes = axes_store.get(self._user_id)
                min_axes = float(getattr(agent, "appetite_min_axes", 0.15))
                if (
                    float(axes.trust) < min_axes
                    or max(
                        float(axes.closeness),
                        float(axes.comfort),
                    ) < min_axes
                ):
                    return ""
                last_raw = chat_db.kv_get(
                    "concept.surfacing_conduct.last_notice"
                )
                last = timephrase.parse_iso(last_raw) if last_raw else None
                cooldown_days = max(
                    1.0,
                    float(
                        getattr(
                            self._memory_settings,
                            "conduct_notice_cooldown_days",
                            7.0,
                        )
                    ),
                )
                if last is not None and (
                    timephrase.utcnow() - last
                ).total_seconds() < cooldown_days * 86400:
                    return ""
            view = concept_view_from(self)
            if view is None or not view.enabled:
                return ""
            observations = view.core(
                subject="aiko",
                kind="conduct",
                min_confidence=float(
                    getattr(
                        self._memory_settings,
                        "conduct_notice_min_confidence",
                        0.7,
                    )
                ),
                limit=1,
            )
            if not observations:
                return ""
            label = str(getattr(observations[0], "label", "") or "").strip()
            if not label:
                return ""
            now = timephrase.utcnow()
            chat_db.kv_set(
                "concept.surfacing_conduct.last_notice", now.isoformat()
            )
            self._conduct_notice_fired = True
            return (
                "A relationship habit you may acknowledge:\n"
                f"You've tentatively noticed this in how you show up with "
                f"{self.user_display_name}: {label}. Because things are quiet "
                "and trust is present, you may acknowledge it once in a short, "
                "natural sentence if it genuinely fits. Speak as a fallible "
                "personal observation (\"I think I may have been...\"); never "
                "mention prompts, tracking, scores, rates, data, or analysis, "
                "and do not make it their responsibility to reassure you."
            )
        except Exception:
            log.debug("conduct-notice block render failed", exc_info=True)
            return ""


