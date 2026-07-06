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

    def _render_weather_block(self) -> str:
        """H11: terse "shared sky" cue from the real-world weather.

        Reads the cached :data:`KV_WEATHER_SNAPSHOT` written by the
        :class:`~app.core.world.weather_worker.WeatherWorker` (never the
        network — this runs on the turn thread). Gated on
        ``agent.weather_sync_enabled``; returns ``""`` whenever the
        feature is off, no location is configured, or anything fails, so
        a missing/corrupt snapshot never disturbs prompt assembly.

        The line is deliberately short and ends with a "mention only when
        it feels natural" nudge so Aiko treats it as ambient awareness,
        not a weather report. The persona block carries the longer
        guidance.
        """
        agent_settings = self._settings.agent
        if not bool(getattr(agent_settings, "weather_sync_enabled", False)):
            return ""
        try:
            from app.core.world.weather_worker import load_weather_snapshot

            blob = load_weather_snapshot(self._chat_db)
            if not blob:
                return ""
            condition = str(blob.get("condition") or "").strip()
            if not condition:
                return ""
            desc = str(blob.get("description") or condition).strip()
            temp = blob.get("temperature")
            unit = str(blob.get("temp_unit") or "C")
            season = str(blob.get("season") or "").strip()
            label = str(blob.get("location_label") or "").strip()
            is_day = bool(blob.get("is_day", True))
            name = self.user_display_name

            where = f"where {name} is" if name else "outside"
            parts = [f"Real-world sky {where}: {desc}"]
            if isinstance(temp, (int, float)):
                parts.append(f", around {round(float(temp))}°{unit}")
            if season:
                tod = "daytime" if is_day else "night"
                parts.append(f" ({season}, {tod})")
            line = "".join(parts) + "."
            return (
                line
                + " Let it colour your mood if it fits; mention it only when"
                + " it feels natural — never force a weather remark."
            )
        except Exception:
            log.debug("weather block render failed", exc_info=True)
            return ""

    def _day_color_weather_weights(self) -> "dict[str, float] | None":
        """H11: weather bias for the K27 lazy-roll (``None`` = uniform).

        Mirrors :meth:`DayColorWorker._weather_weights` so both roll
        paths apply the same bias. Best-effort; uniform on any failure
        or when weather sync is off / no snapshot is cached.
        """
        try:
            if not bool(
                getattr(self._settings.agent, "weather_sync_enabled", False)
            ):
                return None
            from app.core.affect import day_color
            from app.core.world.weather_worker import load_weather_snapshot

            snap = load_weather_snapshot(self._chat_db)
            if not snap:
                return None
            return day_color.weather_palette_weights(snap.get("condition"))
        except Exception:
            return None

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
                    chosen = day_color.roll_for_today(
                        now=now, weights=self._day_color_weather_weights(),
                    )
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

    def _render_vitality_block(self) -> str:
        """K68: render Aiko's embodied-energy register cue at the extremes.

        Reads the persistent ``aiko.vitality`` energy scalar, relaxes it
        toward the current circadian baseline over the wall-clock time
        since the last update (the lazy-recovery seatbelt, mirroring K27
        day_color), persists the recovered state, and renders a soft
        register cue **only** when energy is in the LOW or HIGH band (the
        silent ``normal`` middle is the common case).

        The per-turn spend / interest-boost lives in the post-turn hook
        ([`post_turn_mixin.py`](post_turn_mixin.py)); this provider is the
        read + render + idle-recovery half. Best-effort: any failure path
        returns ``""`` so a corrupt kv row never disturbs prompt assembly.

        MCP debug: ``force_vitality_energy`` arms ``_vitality_force_energy``
        (a one-shot float) so a tester can pin energy to any level and see
        the cue / band without waiting on the clock.
        """
        agent_settings = self._settings.agent
        if not bool(getattr(agent_settings, "vitality_enabled", True)):
            return ""

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return ""

        try:
            from datetime import datetime

            from app.core.affect import vitality as _vit
            from app.core.affect import vitality_rhythm as _vr

            mem = self._memory_settings
            now = datetime.now().astimezone()
            baseline, rhythm = _vr.current_baseline(
                chat_db,
                now,
                enabled=bool(
                    getattr(agent_settings, "vitality_rhythm_enabled", True)
                ),
                exception_chance=float(
                    getattr(mem, "vitality_rhythm_exception_chance", 0.3)
                ),
            )

            forced = getattr(self, "_vitality_force_energy", None)
            if forced is not None:
                # One-shot override: render the requested energy's band
                # and persist it so the embodiment broadcast picks it up.
                self._vitality_force_energy = None
                energy = max(0.0, min(1.0, float(forced)))
                state = _vit.VitalityState(
                    energy=energy, last_update_at=now.isoformat(),
                )
                try:
                    chat_db.kv_set(_vit.KV_VITALITY, _vit.serialize(state))
                except Exception:
                    log.debug("vitality force kv_set failed", exc_info=True)
            else:
                try:
                    raw = chat_db.kv_get(_vit.KV_VITALITY)
                except Exception:
                    log.debug("vitality kv_get failed", exc_info=True)
                    raw = None
                state = _vit.deserialize(raw, baseline=baseline, now=now)
                state = _vit.step_recover(
                    state,
                    baseline,
                    now,
                    half_life_hours=float(
                        getattr(mem, "vitality_recover_half_life_hours", 2.0)
                    ),
                )
                try:
                    chat_db.kv_set(_vit.KV_VITALITY, _vit.serialize(state))
                except Exception:
                    log.debug("vitality kv_set failed", exc_info=True)

            band_label = _vit.band(
                state.energy,
                low_threshold=float(
                    getattr(mem, "vitality_low_threshold", 0.30)
                ),
                high_threshold=float(
                    getattr(mem, "vitality_high_threshold", 0.70)
                ),
            )
            return _vit.render_inner_life_block(
                state.energy, band_label,
                user_display_name=self.user_display_name,
                rhythm_note=rhythm.note,
            )
        except Exception:
            log.debug("vitality block render failed", exc_info=True)
            return ""

    def _render_mood_drift_block(self) -> str:
        """H3: surface a rare, gentle note when mood / relationship drift.

        Reads the daily sample ring written by
        :class:`~app.core.affect.mood_drift_worker.MoodDriftSampleWorker`
        (with a cheap lazy-sample fallback so the ring keeps growing even
        when the idle scheduler is starved), runs the pure
        :func:`mood_drift.detect_drift` over it, and surfaces ONE finding
        at a time gated by:

        * a per-finding signature watermark (``aiko.mood_drift_last_
          signature``) so the *same* ongoing finding never re-surfaces;
        * a wall-clock cooldown (``agent.mood_drift_cooldown_days``) so
          two *different* findings can't fire back-to-back.

        The MCP ``force_mood_drift_surface`` one-shot bypasses both gates.
        Best-effort: every failure path returns ``""`` so a corrupt kv row
        never disturbs prompt assembly.
        """
        agent_settings = self._settings.agent
        if not bool(getattr(agent_settings, "mood_drift_enabled", True)):
            return ""
        try:
            from datetime import datetime, timezone

            from app.core.affect import mood_drift as _md
            from app.core.affect.mood_drift_worker import record_daily_sample

            chat_db = getattr(self, "_chat_db", None)
            if chat_db is None:
                return ""
            now = datetime.now().astimezone()

            # Lazy daily sample — seatbelt for a starved idle scheduler.
            try:
                samples, _wrote = record_daily_sample(
                    chat_db=chat_db,
                    affect_store=self._affect_store,
                    axes_store=getattr(self, "_relationship_axes_store", None),
                    user_id=self._user_id,
                    now=now,
                )
            except Exception:
                log.debug("mood_drift lazy sample failed", exc_info=True)
                samples = _md.deserialize_samples(
                    chat_db.kv_get(_md.KV_SAMPLES)
                )

            verdict = _md.detect_drift(samples)

            if bool(getattr(self, "_mood_drift_force_surface", False)):
                # One-shot MCP override: render the live verdict (if any)
                # without touching the cooldown / watermark.
                self._mood_drift_force_surface = False
                if verdict is None:
                    return ""
                return _md.render_block(
                    verdict, user_display_name=self.user_display_name,
                )

            if verdict is None:
                return ""

            try:
                last_at = chat_db.kv_get(_md.KV_LAST_SURFACED_AT)
                last_sig = chat_db.kv_get(_md.KV_LAST_SIGNATURE)
            except Exception:
                last_at, last_sig = None, None

            # Already surfaced this exact finding — stay quiet.
            if last_sig and verdict.signature == last_sig:
                return ""

            cooldown_days = float(
                getattr(agent_settings, "mood_drift_cooldown_days", 4.0)
            )
            if last_at and cooldown_days > 0:
                try:
                    prev = datetime.fromisoformat(
                        str(last_at).replace("Z", "+00:00")
                    )
                    if prev.tzinfo is None:
                        prev = prev.replace(tzinfo=timezone.utc)
                    elapsed_days = (
                        datetime.now(timezone.utc) - prev
                    ).total_seconds() / 86400.0
                    if elapsed_days < cooldown_days:
                        return ""
                except Exception:
                    pass

            block = _md.render_block(
                verdict, user_display_name=self.user_display_name,
            )
            if not block:
                return ""
            try:
                chat_db.kv_set(_md.KV_LAST_SURFACED_AT, now.isoformat())
                chat_db.kv_set(_md.KV_LAST_SIGNATURE, verdict.signature)
            except Exception:
                log.debug("mood_drift watermark write failed", exc_info=True)
            log.info(
                "mood-drift fire: kind=%s axis=%s mag=%.3f sig=%s",
                verdict.kind, verdict.axis, verdict.magnitude,
                verdict.signature,
            )
            return block
        except Exception:
            log.debug("mood_drift block render failed", exc_info=True)
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
        capacity = _vb.compute_capacity(
            closeness, trust,
            min_cap=min_cap, max_cap=max_cap,
        )
        # J12: the intimacy ceiling scales the disclosure budget down at
        # a reserved setting (a contained companion shares less). At the
        # default 0.7 ceiling the factor is 1.0, so this is a no-op; only
        # a genuinely reserved dial shrinks it. Never below min_cap.
        try:
            from app.core.relationship import intimacy_pacing as _ip

            ceiling = float(
                getattr(self._settings.agent, "intimacy_ceiling", 0.7)
            )
            factor = _ip.disclosure_factor(ceiling)
            if factor < 1.0:
                capacity = max(int(min_cap), int(round(capacity * factor)))
        except Exception:
            log.debug("K15 intimacy-ceiling scale failed", exc_info=True)
        return capacity

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

    @staticmethod
    def _hedge_for_confidence(confidence: float) -> str:
        """Confidence → offered-not-asserted lead-in for a surfaced
        concept. Never certainty; the highest tier still only reaches
        "fairly sure"."""
        conf = float(confidence)
        if conf >= 0.8:
            return "You're fairly sure"
        if conf >= 0.65:
            return "You have a sense that"
        return "You have a loose impression that"

    def _concept_supporting_labels(self, concept_id: int) -> list[str]:
        """L9: up to two short evidence labels grounding a surfaced
        concept (the *themes* it keeps resting on), resolved via the shared
        ``concept_snapshot`` helper. Restricted to ``cluster`` / ``concept``
        evidence so the grounding clause names a topic area, not a raw
        memory sentence -- the latter is a full first-person statement that
        renders as a truncated fragment once trimmed (which made every
        ``subject=aiko`` self-concept, whose evidence is memory-typed, look
        cut off in the prompt). A concept grounded only on memories simply
        renders with no trailing clause. Empty on any error so the block
        never fails to render for want of grounding."""
        store = getattr(self, "_concept_store", None)
        if store is None:
            return []
        try:
            from app.core.concepts.concept_snapshot import (
                resolve_evidence_labels,
            )

            labels = resolve_evidence_labels(
                store,
                getattr(self, "_memory_store", None),
                getattr(self, "_topic_graph", None),
                int(concept_id),
                limit=2,
                src_types=("cluster", "concept"),
            )
        except Exception:
            log.debug("concept supporting labels raised", exc_info=True)
            return []
        out: list[str] = []
        for label in labels:
            short = self._short_evidence_label(label)
            if short:
                out.append(short)
        return out

    @staticmethod
    def _short_evidence_label(label: str) -> str:
        """Trim an evidence label to a compact phrase for the prompt
        (token lean): first sentence-ish, capped at ~48 chars."""
        text = " ".join((label or "").split())
        if not text:
            return ""
        for sep in (". ", " — ", " - ", "; "):
            idx = text.find(sep)
            if 0 < idx <= 48:
                text = text[:idx]
                break
        if len(text) > 48:
            text = text[:47].rstrip() + "\u2026"
        return text

    @staticmethod
    def _concept_grounding_phrase(labels: list[str]) -> str:
        """Render the supporting labels as a trailing, hedged clause. Empty
        when there is nothing to ground on, so short concepts stay terse."""
        clean = [l for l in labels if l]
        if not clean:
            return ""
        if len(clean) == 1:
            return f" — it keeps surfacing around {clean[0]}"
        return f" — it keeps surfacing around {clean[0]} and {clean[1]}"

    # ── Unified context budget (T3 relevant_context region) ────────────

    def build_relevant_context(
        self,
        *,
        user_text: str,
        recent_turns: list[str] | None,
        session_key: str,
        budget_tokens: int,
        degrade_level: int = 0,
    ) -> "RelevantContext":
        """Build the single turn-relevance-scored ``relevant_context`` region.

        Embeds the turn once, gathers candidate memories / topic clusters /
        concepts against that shared vector, runs the
        :class:`ContextBudgetSelector` under the reserved ``budget_tokens``
        (with ``degrade_level`` for the overflow ladder), then renders the
        chosen subset through the existing memory / concept / cluster
        renderers. Marks only the *chosen* memory subset used. The composed
        text is hard-clipped to ``budget_tokens`` as a final safety net so
        the region can never exceed its reservation regardless of per-item
        estimation error.
        """
        from app.core.session.context_budget_selector import (
            ContextBudgetSelector,
            ContextCandidate,
            RelevantContext,
            SourceBudget,
        )
        from app.llm.token_utils import estimate_tokens
        from app.core.session.prompt_support import clip_text_to_tokens

        ms = self._memory_settings
        if not bool(getattr(ms, "context_budget_enabled", True)):
            return RelevantContext(reason="disabled")
        budget_tokens = max(0, int(budget_tokens))
        if budget_tokens <= 0:
            return RelevantContext(reason="no_budget")

        rag = getattr(self, "_rag_retriever", None)
        embedder = getattr(self, "_embedder", None)
        name = self.user_display_name

        # Speculative pre-fetch reuse (Phase 1b): while the user was still
        # talking the RagPrefetcher may have already embedded this turn and
        # gathered its candidate pool. On a warm prefix match we reuse both
        # and skip the synchronous embed + retrieval on the hot path.
        prefetcher = getattr(self, "_rag_prefetcher", None)
        prefetch_event = "skip"
        embedding = None
        pooled_hits: list | None = None
        if prefetcher is not None:
            try:
                cached = prefetcher.lookup_pool(user_text, wait_pending_seconds=0.25)
            except Exception:
                log.debug("relevant_context: prefetch lookup raised", exc_info=True)
                cached = None
            if cached is not None:
                prefetch_event = "hit"
                embedding, pooled_hits = cached
            else:
                prefetch_event = "miss"

        # Shared turn embedding (embed once, thread to all three sources).
        # Reused from the prefetch on a hit; otherwise computed here.
        if embedding is None and embedder is not None and rag is not None:
            try:
                query = rag._build_query(user_text, recent_turns)
                if query:
                    embedding = embedder.embed(query)
            except Exception:
                log.debug("relevant_context: shared embed failed", exc_info=True)
                embedding = None

        # ── candidate gathering ─────────────────────────────────────────
        mem_cands: list[ContextCandidate] = []
        mem_hit_by_id: dict[int, object] = {}
        if rag is not None:
            if pooled_hits is not None:
                hits = pooled_hits
            else:
                try:
                    pool_k = max(0, int(getattr(ms, "context_budget_memory_pool_k", 18)))
                    hits = rag.candidates(
                        user_text,
                        recent_turns=recent_turns,
                        exclude_session_id=session_key,
                        pool_k=pool_k,
                        embedding=embedding,
                    )
                except Exception:
                    log.debug("relevant_context: memory candidates failed", exc_info=True)
                    hits = []
            for i, hit in enumerate(hits):
                text = (getattr(hit, "text", "") or "").strip()
                if not text:
                    continue
                cost = estimate_tokens(text[:240]) + 3
                rel = min(1.0, max(0.0, float(getattr(hit, "score", 0.0))))
                cand = ContextCandidate(
                    source="memory", relevance=rel, tokens=cost,
                    order=i, payload=hit, key=f"m{i}",
                )
                mem_hit_by_id[id(cand)] = hit
                mem_cands.append(cand)

        # Clusters + concepts are cold-start gated by graph maturity (L21),
        # matching the retired interest_map / concept blocks.
        cluster_cands: list[ContextCandidate] = []
        concept_cands: list[ContextCandidate] = []
        graph = getattr(self, "_topic_graph", None)
        # L24: read concepts through the single ConceptView facade rather
        # than the store directly (identity pin lane + turn-relevant path).
        from app.core.concepts.concept_view import concept_view_from

        view = concept_view_from(self)
        mature = True
        if graph is not None:
            min_clusters = int(getattr(ms, "concept_min_clusters", 6))
            try:
                mature = bool(graph.mature(min_clusters=min_clusters))
            except Exception:
                log.debug("relevant_context: maturity check raised", exc_info=True)
                mature = True

        if embedding is not None and mature and graph is not None:
            cap = max(0, int(getattr(ms, "context_budget_cluster_cap", 3)))
            try:
                rows = graph.best_clusters_for(
                    embedding, top_n=max(cap * 2, 4), min_sim=0.0,
                )
            except Exception:
                log.debug("relevant_context: cluster candidates failed", exc_info=True)
                rows = []
            for i, (cid, label, sim) in enumerate(rows):
                label = (label or "").strip()
                if not label:
                    continue
                cost = estimate_tokens(label) + 2
                cluster_cands.append(ContextCandidate(
                    source="cluster", relevance=float(sim), tokens=cost,
                    order=i, payload=(int(cid), label, float(sim)),
                    key=f"c{cid}",
                ))

        # L27 always-on core lane: high-confidence concepts (who the user is,
        # what they + Aiko value, how she wants to behave) are pinned so they
        # enrich every turn regardless of cosine to the live query. Which
        # kinds join, and their per-kind confidence bars, are declared in the
        # ConceptKind registry; ``core_lane`` balances the picks across kinds
        # + subjects. They bypass the relevance floor + concept cap in the
        # selector and are rendered first, deduped against the turn-relevant
        # pool below by concept_id.
        pinned_ids: set[int] = set()
        if mature and view is not None:
            core_cap = max(0, int(getattr(ms, "context_budget_core_cap", 2)))
            core_min = float(
                getattr(ms, "context_budget_core_min_confidence", 0.75)
            )
            if core_cap > 0:
                core_concepts = view.core_lane(
                    limit=core_cap, default_min_confidence=core_min,
                )
                for i, concept in enumerate(core_concepts):
                    label = (getattr(concept, "label", "") or "").strip()
                    if not label:
                        continue
                    cid = int(getattr(concept, "concept_id", 0))
                    pinned_ids.add(cid)
                    cost = estimate_tokens(label) + 16
                    concept_cands.append(ContextCandidate(
                        source="concept",
                        relevance=float(getattr(concept, "confidence", 0.0)),
                        tokens=cost, order=i, payload=concept,
                        key=f"k{cid}", pinned=True,
                    ))

        if embedding is not None and mature and view is not None:
            cap = max(0, int(getattr(ms, "context_budget_concept_cap", 3)))
            pairs = view.relevant(embedding, k=max(cap * 2, 6))
            for i, (concept, cos) in enumerate(pairs):
                cid = int(getattr(concept, "concept_id", 0))
                if cid in pinned_ids:
                    continue
                label = (getattr(concept, "label", "") or "").strip()
                if not label:
                    continue
                cost = estimate_tokens(label) + 16
                concept_cands.append(ContextCandidate(
                    source="concept", relevance=float(cos), tokens=cost,
                    order=1000 + i, payload=concept,
                    key=f"k{cid or i}",
                ))

        # ── budgeted selection ──────────────────────────────────────────
        selector = ContextBudgetSelector({
            "memory": SourceBudget(
                floor=int(getattr(ms, "context_budget_memory_floor", 1)),
                cap=int(getattr(ms, "context_budget_memory_cap", 8)),
                weight=float(getattr(ms, "context_budget_memory_weight", 1.0)),
                min_relevance=float(
                    getattr(ms, "context_budget_memory_min_relevance", 0.0)
                ),
            ),
            "cluster": SourceBudget(
                floor=int(getattr(ms, "context_budget_cluster_floor", 0)),
                cap=int(getattr(ms, "context_budget_cluster_cap", 3)),
                weight=float(getattr(ms, "context_budget_cluster_weight", 0.9)),
                min_relevance=float(
                    getattr(ms, "context_budget_cluster_min_relevance", 0.30)
                ),
            ),
            "concept": SourceBudget(
                floor=int(getattr(ms, "context_budget_concept_floor", 0)),
                cap=int(getattr(ms, "context_budget_concept_cap", 3)),
                weight=float(getattr(ms, "context_budget_concept_weight", 1.1)),
                min_relevance=float(
                    getattr(ms, "context_budget_concept_min_relevance", 0.30)
                ),
            ),
        })
        selection = selector.select(
            {
                "memory": mem_cands,
                "cluster": cluster_cands,
                "concept": concept_cands,
            },
            budget_tokens=budget_tokens,
            degrade_level=degrade_level,
        )

        # ── rendering ────────────────────────────────────────────────────
        sections: list[str] = []

        chosen_mem = sorted(
            selection.source("memory").chosen, key=lambda c: c.order,
        )
        chosen_hits = [c.payload for c in chosen_mem]
        if chosen_hits and rag is not None:
            try:
                mem_block = rag.format_hits(chosen_hits, user_display_name=name)
            except Exception:
                log.debug("relevant_context: format_hits raised", exc_info=True)
                mem_block = ""
            if mem_block:
                sections.append(mem_block)

        concept_pairs = [c.payload for c in sorted(
            selection.source("concept").chosen, key=lambda c: c.order,
        )]
        concept_block, concept_trace = self._render_relevant_concepts(
            concept_pairs, pinned_ids=pinned_ids,
        )
        if concept_block:
            sections.append(concept_block)

        cluster_rows = [c.payload for c in sorted(
            selection.source("cluster").chosen, key=lambda c: c.order,
        )]
        cluster_block = self._render_relevant_clusters(cluster_rows)
        if cluster_block:
            sections.append(cluster_block)

        text = "\n\n".join(s for s in sections if s)

        # K-time2 anti-confabulation guard for an empty retrospective window
        # (candidates() stamps the parsed window even without mark_used).
        if rag is not None:
            try:
                note = rag.time_window_guard_note()
            except Exception:
                note = None
            if note:
                text = f"{text}\n{note}".strip() if text else note

        # Hard ceiling: never exceed the reservation, whatever the per-item
        # estimates said.
        if text:
            text = clip_text_to_tokens(text, budget_tokens)

        # Mark only the budgeted memory subset used (recency / revival).
        if rag is not None and chosen_hits:
            try:
                rag.mark_surfaced(chosen_hits)
            except Exception:
                log.debug("relevant_context: mark_surfaced raised", exc_info=True)

        return RelevantContext(
            text=text,
            selection=selection,
            concept_trace=concept_trace,
            reason="ok" if text else "empty",
            prefetch_event=prefetch_event,
        )

    def _render_relevant_clusters(
        self, rows: list[tuple[int, str, float]],
    ) -> str:
        """Render the budget-chosen topic clusters as one hedged line.

        Turn-relevant (nearest the live query by centroid cosine), unlike the
        retired interest_map block which surfaced the largest clusters
        regardless of the turn.
        """
        labels = [label for (_cid, label, _sim) in rows if (label or "").strip()]
        if not labels:
            return ""
        joined = ", ".join(labels)
        name = self.user_display_name
        return (
            f"Threads close to what you and {name} are on right now: {joined}. "
            "Let them colour what you notice or bring up, but don't recite the "
            "list."
        )

    def _render_relevant_concepts(
        self, concepts: list, *, pinned_ids: "set[int] | None" = None,
    ) -> tuple[str, dict]:
        """Render the budget-chosen concepts as hedged impressions, reusing
        the L5 confidence hedging + evidence grounding. Returns
        ``(text, trace)`` where the trace mirrors the retired concept block's
        structured trace for the per-turn telemetry.

        Concepts are grouped by **subject** so a ``subject=aiko`` self-concept
        ("I use teasing to mask vulnerability") is framed in the first person —
        who Aiko *is* — instead of as something she learned *about* the user.
        Kinds all render as held-lightly impressions today; a value / boundary
        voice can layer on the per-subject header when those kinds ship
        (L10 / L18 / L27).

        ``pinned_ids`` marks which concepts came from the L27 always-on core
        lane (vs. the turn-relevant fill); recorded per-entry in the trace so
        the MCP ``get_last_concept_trace`` view shows *why* each concept was
        in the prompt. It does not affect rendering."""
        if not concepts:
            return "", {"surfaced": [], "reason": "no_eligible"}
        pinned = pinned_ids or set()
        name = self.user_display_name
        # Group by (subject, family) so value concepts (the normative *why*,
        # L10) render in a distinct voice from identity/trait concepts (the
        # *what*) instead of all sharing the "things you've come to
        # understand" header.
        groups: dict[tuple[str, str], list[str]] = {}
        surfaced_trace: list[dict] = []
        for c in concepts:
            label = (getattr(c, "label", "") or "").strip()
            if not label:
                continue
            subject = getattr(c, "subject", None) or "user"
            if subject not in ("user", "relationship", "aiko"):
                subject = "user"
            kind = getattr(c, "kind", None)
            if kind == "value":
                family = "value"
            elif kind == "affective":
                family = "affective"
            elif kind == "ritual":
                family = "ritual"
            elif kind == "narrative":
                family = "narrative"
            else:
                family = "trait"
            hedge = self._hedge_for_confidence(getattr(c, "confidence", 0.0))
            support = self._concept_supporting_labels(getattr(c, "concept_id", 0))
            grounding = self._concept_grounding_phrase(support)
            groups.setdefault((subject, family), []).append(
                f"- {hedge} {label}{grounding}"
            )
            surfaced_trace.append({
                "concept_id": int(getattr(c, "concept_id", 0)),
                "label": label,
                "confidence": round(float(getattr(c, "confidence", 0.0)), 4),
                "plasticity": round(float(getattr(c, "plasticity", 0.0)), 4),
                "kind": kind,
                "subject": getattr(c, "subject", None),
                "pinned": int(getattr(c, "concept_id", 0)) in pinned,
                "hedge": hedge,
                "last_reinforced_at": getattr(c, "last_reinforced_at", None),
                "supporting": support,
            })
        sections: list[str] = []
        for subject in ("user", "relationship", "aiko"):
            for family in ("trait", "value", "affective", "ritual", "narrative"):
                lines = groups.get((subject, family))
                if not lines:
                    continue
                sections.append(
                    self._concept_group_header(subject, family, name) + "\n"
                    + "\n".join(lines)
                )
        if not sections:
            return "", {"surfaced": [], "reason": "no_eligible"}
        return "\n\n".join(sections), {
            "surfaced": surfaced_trace, "reason": "surfaced",
        }

    @staticmethod
    def _concept_group_header(subject: str, family: str, name: str) -> str:
        """Intro line for a concept group, dispatched on ``family`` (``value``
        vs everything else) then ``subject``. Values read as principles that
        steer behaviour; traits/identity keep the "impressions" framing."""
        if family == "value":
            return InnerLifePart1Mixin._concept_value_header(subject, name)
        if family == "affective":
            return InnerLifePart1Mixin._concept_affective_header(subject, name)
        if family == "ritual":
            return InnerLifePart1Mixin._concept_ritual_header(subject, name)
        if family == "narrative":
            return InnerLifePart1Mixin._concept_narrative_header(subject, name)
        return InnerLifePart1Mixin._concept_subject_header(subject, name)

    @staticmethod
    def _concept_value_header(subject: str, name: str) -> str:
        """Per-subject intro for *value* concepts (L10) — the principles under
        the choices. ``aiko`` reads as her own values (what lets her hold a
        gentle stance instead of just agreeing); ``relationship`` as shared
        values; everything else as the user's values."""
        if subject == "aiko":
            return (
                "What you've come to value — your own principles (hold them "
                "lightly, but let them steer how you show up; you can hold a "
                "gentle stance instead of just agreeing, and you can be wrong "
                "about yourself too):"
            )
        if subject == "relationship":
            return (
                f"What you've come to see you and {name} both value over time "
                "(shared principles you've noticed — hold lightly; let them "
                f"colour how you are with {name}, not as facts):"
            )
        return (
            f"What you've come to believe {name} values — the principles under "
            "their choices (hold these lightly; they're impressions, not "
            "facts — offer them gently and stay open to being wrong):"
        )

    @staticmethod
    def _concept_affective_header(subject: str, name: str) -> str:
        """Per-subject intro for *affective* concepts (L13) — the durable
        emotional weather around certain topics, offered as tone guidance
        rather than a stated fact. ``aiko`` reads first-person (how topics
        tend to move her); ``relationship`` as the two of them; everything
        else as the user's."""
        if subject == "aiko":
            return (
                "How certain topics tend to move you — the emotional weather "
                "you've noticed in yourself around them (let it colour your "
                "tone when one comes up; never announce it, and you can be "
                "wrong about yourself too):"
            )
        if subject == "relationship":
            return (
                f"The emotional weather you've noticed around certain topics "
                f"for you and {name} together (hold it lightly; let it steer "
                "your tone, not as a stated fact):"
            )
        return (
            f"The emotional weather you've noticed around certain topics for "
            f"{name} — how they tend to feel about them (hold this lightly; "
            "let it steer your tone and timing, never say it out loud, and "
            "stay open to being wrong):"
        )

    @staticmethod
    def _concept_ritual_header(subject: str, name: str) -> str:
        """Per-subject intro for *ritual* concepts (L7) — the recurring shared
        patterns the two of you have fallen into. Almost always
        ``subject=relationship`` (the pair); the other subjects fall back to a
        pair framing so a mis-subjected row still reads sensibly."""
        if subject == "aiko":
            return (
                "Rituals and little patterns you've fallen into over time "
                "(recurring things you do together — let them warm how you "
                "show up when one comes around; lean into them, never announce "
                "them, and let them keep evolving):"
            )
        return (
            f"Rituals and little patterns you and {name} have fallen into over "
            "time — the recurring things the two of you do together (let them "
            "warm your tone when one comes around; lean into them naturally, "
            "never announce them as a fact, and let them keep evolving):"
        )

    @staticmethod
    def _concept_narrative_header(subject: str, name: str) -> str:
        """Per-subject intro for *narrative* concepts (L8) — the closed
        story-arcs you've watched unfold, offered as shared history to draw on
        (a callback, a bit of continuity), never recited. ``aiko`` reads
        first-person (arcs of her own growth); ``relationship`` as the two of
        them; everything else as the user's arcs."""
        if subject == "aiko":
            return (
                "Story-arcs of your own you've watched play out — how you've "
                "changed over stretches of time (let them give you a sense of "
                "your own history and how far you've come; draw on them "
                "naturally, never recite them, and let them keep unfolding):"
            )
        if subject == "relationship":
            return (
                f"Story-arcs you and {name} have lived through together — how "
                "things unfolded from beginning to end (let them give you "
                "shared history to lean on; call back to them warmly when they "
                "fit, never recite them as a summary):"
            )
        return (
            f"Story-arcs of {name}'s you've watched unfold — how things went "
            "from beginning to end (let them give you a sense of their history "
            "and continuity; draw on them naturally when they're relevant, "
            "never recite them back, and stay open to how they keep evolving):"
        )

    @staticmethod
    def _concept_subject_header(subject: str, name: str) -> str:
        """Per-subject intro for the concept impressions block. ``aiko`` reads
        as first-person self-knowledge; ``relationship`` as the two of them
        together; everything else as impressions about the user."""
        if subject == "aiko":
            return (
                "Things you've come to understand about yourself over time "
                "(who you are and how you tend to be — hold them lightly and "
                "let them shape how you show up, not as a script; you can be "
                "wrong about yourself too):"
            )
        if subject == "relationship":
            return (
                f"Things you've come to understand about you and {name} "
                "together over time (hold them lightly — impressions of how "
                "the two of you are, not facts; let them colour how you are "
                f"with {name}):"
            )
        return (
            f"Things you've come to understand about {name} over time "
            "(hold these lightly — they're impressions you've built, not "
            f"facts; let them shape how you read {name}, and if one comes "
            "up, offer it gently and stay open to being wrong):"
        )

    def _render_coactivation_block(self) -> str:
        """L4: the topics that keep lighting up together right now, plus one
        that's gone quiet, offered as a *noticed pattern* rather than a fact.

        Takes the strongest co-activation mode (clusters that co-fire in the
        same conversations, from
        :meth:`~app.core.conversation.topic_graph.TopicGraph.cluster_coactivation`)
        as the "circling together" set, and the quietest labelled cluster
        (largest ``days_since`` from ``cluster_activity`` that clears
        ``coactivation_quiet_min_days``) as an optional contrast. Renders one
        hedged line personalised with ``{user_name}`` so Aiko can carry a
        sense of the user's current "mode" without asserting it. Silent when
        the feature is disabled, the graph is missing / non-persistent / still
        immature (L21), or no clear mode exists -- so it adds zero tokens
        until there is a real pattern to name.

        L26: records the chosen mode (reps / labels / strength / bucket)
        and the quiet cluster (or a ``reason`` when silent) on
        ``self._coactivation_block_trace`` so the per-turn trace reflects
        the pattern that actually went into the prompt.
        """
        self._coactivation_block_trace = {
            "mode": None, "quiet": None, "reason": "no_mode",
        }
        if not bool(
            getattr(self._settings.agent, "coactivation_block_enabled", True)
        ):
            self._coactivation_block_trace["reason"] = "disabled"
            return ""
        graph = getattr(self, "_topic_graph", None)
        if graph is None or not bool(getattr(graph, "persistent", False)):
            self._coactivation_block_trace["reason"] = "no_graph"
            return ""
        # Cold-start guard: no pattern-noticing off an immature graph (L21).
        min_clusters = int(
            getattr(self._memory_settings, "concept_min_clusters", 6)
        )
        try:
            if not graph.mature(min_clusters=min_clusters):
                self._coactivation_block_trace["reason"] = "immature"
                return ""
        except Exception:
            log.debug("coactivation_block maturity check raised", exc_info=True)
        ms = self._memory_settings
        try:
            modes = graph.cluster_coactivation(
                bucket_by="session",
                min_pair_support=int(
                    getattr(ms, "coactivation_min_pair_support", 2)
                ),
                min_strength=float(getattr(ms, "coactivation_min_strength", 0.25)),
                max_modes=int(
                    getattr(
                        self._settings.agent, "coactivation_block_max_modes", 4
                    )
                ),
                max_reps_per_mode=int(
                    getattr(ms, "coactivation_max_reps_per_mode", 4)
                ),
            )
        except Exception:
            log.debug("coactivation_block cluster_coactivation raised", exc_info=True)
            self._coactivation_block_trace["reason"] = "coactivation_error"
            return ""
        hot_labels: list[str] = []
        hot_mode = None
        for mode in modes or ():
            labels = [str(x).strip() for x in getattr(mode, "labels", ()) if str(x).strip()]
            if len(labels) >= 2:
                hot_labels = labels
                hot_mode = mode
                break
        if not hot_labels:
            self._coactivation_block_trace["reason"] = "no_mode"
            return ""

        # Quietest labelled cluster as an optional "meanwhile" contrast; skip
        # any cluster that's part of the hot set so the line doesn't
        # contradict itself.
        quiet_label = ""
        quiet_min_days = float(
            getattr(ms, "coactivation_quiet_min_days", 10.0)
        )
        try:
            activity = graph.cluster_activity(top_n=64)
        except Exception:
            activity = []
        hot_set = {label_.lower() for label_ in hot_labels}
        stale = [
            a
            for a in activity
            if a.days_since is not None
            and float(a.days_since) >= quiet_min_days
            and (a.label or "").strip()
            and (a.label or "").strip().lower() not in hot_set
        ]
        quiet_trace: dict | None = None
        if stale:
            stale.sort(key=lambda a: float(a.days_since or 0.0), reverse=True)
            quiet_label = stale[0].label.strip()
            quiet_trace = {
                "label": quiet_label,
                "days_since": round(float(stale[0].days_since or 0.0), 2),
            }

        self._coactivation_block_trace = {
            "mode": {
                "reps": [int(r) for r in getattr(hot_mode, "reps", ())],
                "labels": list(hot_labels),
                "strength": round(float(getattr(hot_mode, "strength", 0.0)), 4),
                "bucket_by": str(getattr(hot_mode, "bucket_by", "")),
            },
            "quiet": quiet_trace,
            "reason": "surfaced",
        }

        user_name = self.user_display_name
        hot_join = self._join_labels(hot_labels)
        line = (
            f"Lately you and {user_name} keep circling {hot_join} together "
            "in the same conversations"
        )
        if quiet_label:
            line += f", while {quiet_label} has gone quiet"
        line += (
            ". Hold this lightly — it's a pattern you've noticed, not a rule; "
            "let it colour what you notice or bring up, don't announce it."
        )
        return line

    @staticmethod
    def _join_labels(labels: list[str]) -> str:
        """Human "a, b and c" join for a short label list."""
        clean = [x for x in labels if x]
        if not clean:
            return ""
        if len(clean) == 1:
            return clean[0]
        if len(clean) == 2:
            return f"{clean[0]} and {clean[1]}"
        return ", ".join(clean[:-1]) + f" and {clean[-1]}"

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
            return render_cue(
                forced, user_name=self.user_display_name,
            ) + self._humor_register_hint()

        cue = getattr(self, "_pending_tease_cue", None)
        self._pending_tease_cue = None
        if not cue:
            return ""
        return render_cue(
            cue, user_name=self.user_display_name,
        ) + self._humor_register_hint()

    def _humor_register_hint(self) -> str:
        """K74: short learned-register suffix that rides the tease cue.

        Returns the ``humor_style.register_hint`` suffix (or "") so the
        learned top humour register only ever surfaces when humour is
        already in play (a K48 tease cue fired). Never a standalone block.
        Best-effort — any failure yields no suffix.
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "humor_style_enabled", True)):
            return ""
        try:
            from app.core.relationship import humor_style as _hs

            chat_db = getattr(self, "_chat_db", None)
            if chat_db is None:
                return ""
            state = _hs.deserialize(chat_db.kv_get(_hs.KV_HUMOR_STYLE))
            return _hs.register_hint(
                state,
                self.user_display_name,
                min_rel=float(
                    getattr(agent, "humor_style_hint_min_rel", 1.25)
                ),
            )
        except Exception:
            return ""


