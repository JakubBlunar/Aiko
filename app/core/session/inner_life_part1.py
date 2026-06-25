from __future__ import annotations

import logging
from typing import Any
from app.core.session.inner_life_shared import (
    _circadian,
    _format_running_task_line,
)


log = logging.getLogger("app.session")


class InnerLifePart1Mixin:
    """Inner-life prompt-block providers (part 1 of 4)."""

    # P22: floor for the shared recent-history fetch. K30 self-noticing
    # and K54 topic-appetite read ``max(window*4, 20)`` rows (24 at the
    # default window of 6); K23 misattunement reads 6. Fetching at least
    # this many on the first caller lets all three share one read in the
    # default config -- a smaller-window caller just tail-reads the cached
    # rows, a larger-window caller refetches and updates the memo.
    _INNER_LIFE_RECENT_MIN = 24

    def _inner_life_recent_messages(self, limit: int) -> list[Any]:
        """Shared per-assembly recent-history read (P22).

        Several inner-life providers each need the last few chat rows
        within a single ``assemble_with_budget`` pass. Routing them
        through this memo collapses their overlapping ``get_messages``
        queries into one read per assembly. Correctness comes from
        keying the cache on the assembler's ``_assembly_seq`` (bumped at
        the top of every assembly) plus the active ``session_key``, so a
        new turn -- or a session switch -- always misses and refetches.

        Returns the chat rows in the database's native (oldest-first)
        order, same as a direct ``chat_db.get_messages`` call, so callers
        keep using ``reversed(rows)`` to walk newest-first.
        """
        db = getattr(self, "_chat_db", None)
        if db is None:
            return []
        want = max(int(limit), self._INNER_LIFE_RECENT_MIN)
        assembler = getattr(self, "_prompt_assembler", None)
        seq = getattr(assembler, "_assembly_seq", None)
        token = (self.session_key, seq)
        cache = getattr(self, "_inner_life_msg_cache", None)
        # Only trust the memo inside a known assembly (seq present) so a
        # provider call outside an assembly never serves a stale window.
        if seq is not None and cache is not None:
            cached_token, cached_window, cached_rows = cache
            if cached_token == token and cached_window >= want:
                return cached_rows
        rows = db.get_messages(self.session_key, limit=want)
        if seq is not None:
            self._inner_life_msg_cache = (token, want, rows)
        return rows

    def _render_affect_block(self) -> str:
        """Hot-path: read affect_state and format the ambient block."""
        try:
            from app.core.affect.affect_state import render_ambient_block
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
        source_kind = (nudge.source_kind or "").strip().lower()
        # K47: while the question/share gate is armed, drop the
        # open_question nudge specifically — it's the one narrative source
        # that hands the LLM a ready-made question to ask.
        if source_kind == "open_question" and self._question_balance_suppressed():
            return ""
        label = self._NARRATIVE_LABELS.get(
            source_kind,
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

    def _render_day_color_block(self) -> str:
        """K27: render today's daily personality colour cue.

        One-line prompt cue ("Your day's colour today: pensive --
        slower replies, more 'hmm'..."), drawn once per local day
        from the 10-entry palette in
        :mod:`app.core.affect.day_color`. The full reasoning lives
        in the persona block; this provider just plumbs whichever
        colour is current into the system prompt next to the
        circadian cue.

        Three layers in order:

        1. **Master switch** -- ``agent.day_color_enabled`` short-
           circuits to ``""`` so the feature can be turned off
           without redeploying.
        2. **MCP debug shortcuts** -- the
           ``_day_color_force_next`` / ``_day_color_force_reroll``
           one-shot flags armed by the
           :func:`force_day_color` / :func:`reroll_day_color`
           MCP tools take precedence over the stored value so a
           tester can poke the system without shifting the OS
           clock.
        3. **Lazy fallback + render** -- read ``kv_meta``; if
           today's colour isn't set (first turn after midnight,
           idle-worker hasn't ticked yet), roll a fresh one via
           :func:`day_color.roll_for_today` and write it. Then
           render whichever colour is current.

        Best-effort: any failure path returns ``""`` so a corrupt
        ``kv_meta`` row or a missing ``chat_db`` reference doesn't
        cascade into the rest of the prompt assembly. Mirrors the
        K30 / K23 / K28 swallow-and-log convention.
        """
        agent_settings = self._settings.agent
        if not bool(getattr(agent_settings, "day_color_enabled", True)):
            return ""

        try:
            from datetime import datetime

            from app.core.affect import day_color
            from app.core.affect.day_color_worker import (
                KV_DAY_COLOR,
                KV_DAY_COLOR_SET_AT,
            )

            now = datetime.now().astimezone()

            forced = getattr(self, "_day_color_force_next", None)
            if forced:
                # One-shot override: render the requested colour
                # without touching kv_meta so the persisted roll
                # survives the test.
                self._day_color_force_next = None
                chosen = day_color.get_color_by_name(forced)
                if chosen is not None:
                    return day_color.render_inner_life_block(chosen)
                # Unknown colour name -- fall through to the normal
                # path rather than rendering a confusing empty cue.

            force_reroll = bool(
                getattr(self, "_day_color_force_reroll", False)
            )

            chat_db = getattr(self, "_chat_db", None)
            if chat_db is None:
                return ""

            try:
                stored_at = chat_db.kv_get(KV_DAY_COLOR_SET_AT)
            except Exception:
                log.debug("day_color kv_get(set_at) failed", exc_info=True)
                stored_at = None

            if force_reroll or day_color.is_stale(stored_at, now):
                # Lazy fallback path -- the idle-worker hasn't fired
                # since the local-date rollover (or a tester just
                # armed force_reroll). Roll + write + log so the
                # next provider call hits the stable-read path.
                self._day_color_force_reroll = False
                try:
                    chosen = day_color.roll_for_today(now=now)
                    chat_db.kv_set(KV_DAY_COLOR, chosen.name)
                    chat_db.kv_set(KV_DAY_COLOR_SET_AT, now.isoformat())
                    log.info(
                        "day_color lazy-roll: name=%s set_at=%s",
                        chosen.name, now.isoformat(),
                    )
                    return day_color.render_inner_life_block(chosen)
                except Exception:
                    log.debug(
                        "day_color lazy-roll failed", exc_info=True,
                    )
                    return ""

            # Stable-read path -- today's colour is already set.
            try:
                stored_name = chat_db.kv_get(KV_DAY_COLOR)
            except Exception:
                log.debug("day_color kv_get(name) failed", exc_info=True)
                return ""
            chosen = day_color.get_color_by_name(stored_name)
            return day_color.render_inner_life_block(chosen) if chosen else ""
        except Exception:
            log.debug("day_color block render failed", exc_info=True)
            return ""

    def _render_vulnerability_budget_block(self) -> str:
        """K15: render the self-disclosure / vulnerability budget cue.

        One-line prompt nudge that paces how often Aiko opens up
        personally. Reads the persisted token-bucket from
        ``kv_meta`` (key ``aiko.vulnerability_budget``), applies
        rolling decay against wall-clock elapsed time, computes the
        bucket capacity from the live closeness + trust axes, and
        renders the cue based on the spent/capacity ratio.

        Three layers in order:

        1. **Master switch** -- ``agent.vulnerability_budget_enabled``
           short-circuits to ``""`` so the feature can be turned off
           without redeploying. Same shape as K27 / K30.
        2. **MCP debug shortcuts** -- the
           ``_vulnerability_budget_force_spent`` /
           ``_vulnerability_budget_force_reset`` one-shot flags
           armed by the :func:`spend_vulnerability` /
           :func:`reset_vulnerability_budget` MCP tools take
           precedence. ``force_spent`` renders the cue with the
           forced spent value without touching kv_meta (so the
           real persisted bucket survives the test);
           ``force_reset`` writes a fresh ``BudgetState(spent=0)``
           to kv_meta. Both are consumed one-shot.
        3. **Read + decay + persist + render** -- read kv_meta,
           deserialise, apply decay (math: ``new_spent = max(0,
           spent - regen_per_hour * elapsed_hours)``), write the
           decayed state back so the next call doesn't re-apply
           the same elapsed window, compute the capacity from
           axes, and render the cue.

        Best-effort: any failure path returns ``""``. Mirrors the
        K30 / K27 swallow-and-log convention -- a corrupt kv_meta
        row, a missing axes store on a brand-new install, or a
        broken settings field must never cascade into the rest of
        the prompt assembly.
        """
        agent_settings = self._settings.agent
        if not bool(
            getattr(agent_settings, "vulnerability_budget_enabled", True)
        ):
            return ""

        try:
            from datetime import datetime, timezone

            from app.core.affect import vulnerability_budget as _vb

            chat_db = getattr(self, "_chat_db", None)
            if chat_db is None:
                return ""

            min_cap = int(
                getattr(
                    agent_settings,
                    "vulnerability_budget_min_capacity",
                    1,
                )
            )
            max_cap = int(
                getattr(
                    agent_settings,
                    "vulnerability_budget_max_capacity",
                    12,
                )
            )
            regen = float(
                getattr(
                    agent_settings,
                    "vulnerability_budget_regen_per_hour",
                    0.5,
                )
            )
            now = datetime.now(timezone.utc)

            # 2. MCP force_reset shortcut -- wipe state, then fall
            # through to the read path so the cue still renders
            # (capacity > 0, spent = 0 -> silent, which is the
            # expected post-reset render).
            if bool(getattr(self, "_vulnerability_budget_force_reset", False)):
                self._vulnerability_budget_force_reset = False
                try:
                    fresh = _vb.BudgetState(
                        spent=0.0, last_decay_at=now.isoformat(),
                    )
                    chat_db.kv_set(_vb.KV_BUDGET_STATE, _vb.serialize(fresh))
                except Exception:
                    log.debug(
                        "K15 force_reset kv_set failed", exc_info=True,
                    )

            # 2. MCP force_spent shortcut -- render the cue against
            # the forced ``spent`` value WITHOUT touching kv_meta so
            # the real persisted bucket survives the test. Consumed
            # one-shot.
            forced_spent = getattr(
                self, "_vulnerability_budget_force_spent", None,
            )
            if forced_spent is not None:
                self._vulnerability_budget_force_spent = None
                # Use min(capacity, max_cap) so the forced render
                # still respects the axes-derived ceiling (low
                # closeness + forced spent should still trigger the
                # low-ceiling cue).
                try:
                    forced_state = _vb.BudgetState(
                        spent=float(forced_spent),
                        last_decay_at=now.isoformat(),
                    )
                except (TypeError, ValueError):
                    log.debug(
                        "K15 force_spent: invalid value %r", forced_spent,
                    )
                else:
                    capacity = self._k15_compute_capacity(
                        min_cap=min_cap, max_cap=max_cap,
                    )
                    return _vb.render_inner_life_block(
                        forced_state,
                        capacity,
                        user_display_name=self.user_display_name,
                    )

            # 3. Read + decay + persist + render.
            try:
                stored = chat_db.kv_get(_vb.KV_BUDGET_STATE)
            except Exception:
                log.debug(
                    "K15 kv_get(budget) failed", exc_info=True,
                )
                stored = None
            state = _vb.deserialize(stored)
            decayed = _vb.apply_decay(
                state, now,
                regen_per_hour=regen, max_capacity=max_cap,
            )
            # Persist the decayed timestamp so the next call doesn't
            # re-apply the same elapsed window. Skip the write when
            # nothing changed (rare: both ``spent`` and
            # ``last_decay_at`` identical) so a healthy budget on a
            # fast turn doesn't keep churning the kv_meta row.
            if (
                decayed.spent != state.spent
                or decayed.last_decay_at != state.last_decay_at
            ):
                try:
                    chat_db.kv_set(
                        _vb.KV_BUDGET_STATE, _vb.serialize(decayed),
                    )
                except Exception:
                    log.debug(
                        "K15 kv_set(decayed) failed", exc_info=True,
                    )

            capacity = self._k15_compute_capacity(
                min_cap=min_cap, max_cap=max_cap,
            )
            return _vb.render_inner_life_block(
                decayed,
                capacity,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug(
                "vulnerability_budget block render failed", exc_info=True,
            )
            return ""

    def _k15_compute_capacity(self, *, min_cap: int, max_cap: int) -> int:
        """Capacity helper -- read closeness + trust, interpolate.

        Extracted so the force_spent path and the normal render
        path share the same axes-reading code. Defaults to neutral
        (0, 0) when the axes store is unavailable or raises, which
        maps to the midpoint capacity (~6 on the default 1..12
        ladder).
        """
        from app.core.affect import vulnerability_budget as _vb

        closeness: float | None = None
        trust: float | None = None
        store = getattr(self, "_relationship_axes_store", None)
        if store is not None:
            try:
                axes = store.get(self._user_id)
                closeness = float(axes.closeness)
                trust = float(axes.trust)
            except Exception:
                log.debug(
                    "K15 axes lookup failed -- using neutral baseline",
                    exc_info=True,
                )
        return _vb.compute_capacity(
            closeness, trust,
            min_cap=min_cap, max_cap=max_cap,
        )

    # ── K31 + K32: soft physicality providers ─────────────────────────

    def _render_user_reactions_block(self) -> str:
        """K32: arm the "Jacob just hearted that line" inner-life cue.

        Drains :data:`_pending_user_reactions` -- the queue that
        :meth:`world_mixin.apply_user_reaction` appends to whenever
        the user taps a reaction button on an Aiko bubble. Renders
        a one-line cue and clears the queue so the same reaction
        can't re-fire the cue on later turns.

        Best-effort: master switch off -> ``""``; empty queue ->
        ``""``; any exception in the rendering path swallowed with
        a DEBUG log.
        """
        agent_settings = getattr(self._settings, "agent", None)
        if agent_settings is not None and not bool(
            getattr(agent_settings, "user_reactions_enabled", True),
        ):
            return ""
        queue = getattr(self, "_pending_user_reactions", None)
        if queue is None or not len(queue):
            return ""
        try:
            from app.core.relationship.user_reactions import (
                render_user_reactions_block,
            )

            pending = list(queue)
            # Drain only after we've copied -- a render exception would
            # otherwise lose the cue.
            queue.clear()
            return render_user_reactions_block(
                pending,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug(
                "K32 user_reactions block render failed", exc_info=True,
            )
            return ""

    # B7: ``_render_touch_state_block`` (the K31 low-physical-budget cue)
    # was removed — touch gating is gone, so there is no budget to
    # surface and the provider is no longer wired in the controller.

    def _render_running_tasks_block(self) -> str:
        """Brain-orchestration chunk 6: list tasks currently in flight.

        Renders one terse multi-line block so Aiko has live awareness
        of what she has running in the background. Sibling of the
        ``task_cues`` block — that one announces *deltas* (results
        just landed / blocked on input), this one announces *state*
        (still working).

        Reads :meth:`TaskOrchestrator.list_running` for the active
        user (filters to ``status in (running, awaiting_input)`` —
        ``paused`` rows survive recovery but aren't actively
        working, so they don't belong in the "currently doing"
        cluster).

        Empty string under any of these conditions:

        * Master switch ``agent.tasks_running_block_enabled`` is
          ``False`` (the off-switch).
        * Master switch ``agent.tasks_enabled`` is ``False`` (the
          orchestrator never built, so there's nothing to list).
        * The orchestrator is missing (early boot or stub host).
        * No active rows for the current user.

        Best-effort exception handling — any failure path returns
        ``""`` and logs at DEBUG. Matches the swallow-and-log
        convention used by every other ``_render_*`` provider.
        """
        agent_settings = getattr(self._settings, "agent", None)
        if agent_settings is None:
            return ""
        if not bool(getattr(agent_settings, "tasks_running_block_enabled", True)):
            return ""
        if not bool(getattr(agent_settings, "tasks_enabled", True)):
            return ""
        orchestrator = getattr(self, "_task_orchestrator", None)
        if orchestrator is None:
            return ""
        try:
            from app.core.tasks import STATUS_AWAITING_INPUT, STATUS_RUNNING

            user_id = getattr(self, "_user_id", None)
            rows = orchestrator.list_running(user_id=user_id)
            active = [
                r for r in rows
                if r.status in (STATUS_RUNNING, STATUS_AWAITING_INPUT)
            ]
            if not active:
                return ""
            # Cap at 5 lines — same aggregation budget the cue
            # block uses. A user with 10+ running tasks is already
            # in a degenerate state; the LLM only needs the most
            # recent handful for orientation.
            cap = 5
            head = active[:cap]
            user_name = self.user_display_name
            lines: list[str] = []
            lines.append(f"Tasks running for {user_name} right now:")
            for row in head:
                lines.append(_format_running_task_line(row))
            if len(active) > cap:
                lines.append(f"...and {len(active) - cap} more")
            return "\n".join(lines)
        except Exception:
            log.debug(
                "running-tasks block render failed", exc_info=True,
            )
            return ""

    def _cadence_context(self) -> Any:
        """Phase 5b: build a CadenceContext from the live affect/circadian."""
        from app.core.voice.cadence import CadenceContext

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
        # Layer 1b: same tracker also exposes a small dB volume
        # nudge (0.0 in quiet rooms, up to +1.5 dB in very-noisy
        # rooms). Plumbed into the gain pipeline by
        # ``analyze_sentence`` / ``ProsodyDispatcher._apply``.
        tracker = getattr(self, "_ambient_noise", None)
        if tracker is not None:
            try:
                ctx.ambient_noise_speed = float(tracker.tts_speed_multiplier())
            except Exception:
                log.debug("cadence ambient-noise lookup failed", exc_info=True)
            try:
                ctx.ambient_volume_db_offset = float(
                    tracker.tts_volume_db_offset()
                )
            except Exception:
                log.debug(
                    "cadence ambient-volume lookup failed", exc_info=True,
                )
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

            from app.core.relationship.relationship import render_petname_block

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

    def _render_goals_block(self) -> str:
        """K1: "Aiko's quiet long-term goals." block.

        Lists up to ``agent.goals_max_rendered`` (default 3) active
        goals as a bullet list, with a single sub-bullet showing the
        most recent reflection note on the goal that was last
        touched. Tonal nudge at the end tells Aiko these are her own
        slow-burn anchors, not user-facing TODOs (the agenda block
        carries those).

        Empty when the goals feature is disabled, the store is
        missing, or no active goals exist. The block is owned by the
        assembler's ``_StaticSlices`` cache, so render cost is paid
        once per listening window even when 3+ goals are live.
        """
        if not bool(getattr(self._settings.agent, "goals_enabled", True)):
            return ""
        store = getattr(self, "_goal_store", None)
        if store is None:
            return ""
        try:
            active = store.list_active()
        except Exception:
            log.debug("goal_store list_active raised", exc_info=True)
            return ""
        if not active:
            return ""
        max_rendered = max(
            1,
            int(
                getattr(
                    self._settings.agent,
                    "goals_max_rendered",
                    3,
                )
            ),
        )
        # Pick the most-recently-reflected goal for the progress sub-bullet.
        # ``last_reflected_at`` is ISO-8601 UTC so lexicographic compare
        # is equivalent to chronological order; missing values sort to
        # the empty string and never win.
        recent_progress_goal_id: int | None = None
        recent_progress_text: str = ""
        recent_progress_at: str = ""
        for goal in active:
            meta = goal.metadata or {}
            note = (meta.get("last_progress_note") or "").strip()
            if not note:
                continue
            last_reflected_at = str(meta.get("last_reflected_at") or "")
            if last_reflected_at > recent_progress_at:
                recent_progress_at = last_reflected_at
                recent_progress_goal_id = int(goal.id)
                recent_progress_text = note
        lines: list[str] = [
            f"Aiko's quiet long-term goals ({self.user_display_name} hasn't asked her about these — these are her own):"
        ]
        for goal in active[:max_rendered]:
            meta = goal.metadata or {}
            summary = str(meta.get("summary") or goal.content or "").strip()
            if not summary:
                continue
            lines.append(f"- {summary}")
            if (
                recent_progress_goal_id == int(goal.id)
                and recent_progress_text
            ):
                # Trim the progress note to one short line so the block
                # stays tight (the worker capped it at 280 chars already
                # but we slice further so two newlines don't sneak in).
                short_note = " ".join(recent_progress_text.split())[:200]
                lines.append(f"  (recent: {short_note})")
        if len(lines) == 1:
            # Defensive: a goal row whose summary fell through the
            # validation would leave us with just the header.
            return ""
        return "\n".join(lines)

    def _render_interest_map_block(self) -> str:
        """F10e: "interest map" — the topic threads we keep coming back to.

        Lists the top ``agent.interest_map_max_clusters`` labelled topic
        clusters (largest first) as a single terse line so Aiko carries a
        sense of her recurring threads with the user. Built from the topic
        graph's live cluster map (label + member count only — no join back
        to the memory mirror), so render cost is negligible even with a
        large corpus, and it is owned by the assembler's ``_StaticSlices``
        cache so it is paid once per listening window.

        Each topic shows the F10a clean label once the
        :class:`~app.core.conversation.topic_label_worker.ClusterLabelWorker`
        has named it, falling back to the heuristic representative summary
        otherwise (the densest clusters -- the ones shown -- are labelled
        first, so the line converges on clean names quickly). Empty when
        the feature is disabled, the topic graph is missing / non-
        persistent, or no cluster clears the size floor.
        """
        if not bool(
            getattr(self._settings.agent, "interest_map_enabled", True)
        ):
            return ""
        graph = getattr(self, "_topic_graph", None)
        if graph is None:
            return ""
        top_n = max(
            1,
            int(getattr(self._settings.agent, "interest_map_max_clusters", 5)),
        )
        min_size = max(
            1,
            int(getattr(self._settings.agent, "interest_map_min_size", 4)),
        )
        try:
            entries = graph.interest_map(top_n=top_n, min_size=min_size)
        except Exception:
            log.debug("interest_map raised", exc_info=True)
            return ""
        if not entries:
            return ""
        labels = ", ".join(e.label for e in entries)
        return (
            f"Topics you and {self.user_display_name} keep coming back to: "
            f"{labels}. These are the threads of your time together — let "
            "them colour what you notice or bring up, but don't recite the "
            "list."
        )

    def _question_balance_suppressed(self) -> bool:
        """K47: True when the question/share gate is currently muting the
        question-pushing cues. Read by the question-pushing providers as
        an early-return guard; never mutates state (the countdown is
        decremented post-turn, so a same-turn re-render is consistent)."""
        if not bool(
            getattr(self._settings.agent, "question_balance_enabled", True)
        ):
            return False
        return int(
            getattr(self, "_question_balance_suppress_remaining", 0)
        ) > 0

    def _render_question_balance_block(self) -> str:
        """K47: share-first cue, surfaced while the question/share gate is
        armed. Pairs with the suppression of the question-pushing
        providers so the turn reads as "offer something of yours" rather
        than another interview question."""
        if not self._question_balance_suppressed():
            return ""
        from app.core.conversation.question_balance import (
            render_share_first_cue,
        )

        return render_share_first_cue(self.user_display_name)

    def _render_tease_rhythm_block(self) -> str:
        """K48: surface the pending banter-rhythm cue (ease off / one
        more step is safe). One-shot — consumes the slot armed by the
        post-turn hook so a re-render in the same assembly is
        consistent. An MCP force flag bypasses the slot for testing."""
        if not bool(
            getattr(self._settings.agent, "tease_rhythm_enabled", True)
        ):
            return ""
        from app.core.conversation.tease_rhythm import render_cue

        forced = getattr(self, "_tease_rhythm_force", None)
        if forced:
            self._tease_rhythm_force = None
            return render_cue(forced, user_name=self.user_display_name)

        cue = getattr(self, "_pending_tease_cue", None)
        self._pending_tease_cue = None
        if not cue:
            return ""
        return render_cue(cue, user_name=self.user_display_name)


