from __future__ import annotations

import logging
from typing import Any
from app.core.session.inner_life_shared import (
    _circadian,
    _MILESTONE_PHRASES,
    _APPRECIATION_VIBES,
    _KV_APPRECIATION_AT,
    _KV_APPRECIATION_ANCHOR,
    _KV_RECIP_VULN_AT,
)


log = logging.getLogger("app.session")


class InnerLifePart4Mixin:
    """Inner-life prompt-block providers (part 4 of 4)."""

    def _build_grounding_context(self) -> "Any":
        """Assemble the K16 grounding-line slots from live state.

        Reads the same stores the granular block providers read; no
        new database queries land here. Individual store failures
        degrade to None slots instead of raising so the prompt still
        renders if one subsystem is sick.
        """
        from app.core.conversation.grounding_line import GroundingContext
        from app.core.world.world_store import _OUTDOOR_SLUGS

        ctx = GroundingContext(user_display_name=self.user_display_name)

        try:
            cstate = _circadian.compute()
            ctx.weekday = cstate.weekday
            ctx.is_weekend = bool(cstate.is_weekend)
            ctx.period = cstate.period
            ctx.hour = int(cstate.hour)
            ctx.minute = int(cstate.minute)
            ctx.is_drowsy = bool(cstate.drowsy)
        except Exception:
            log.debug("grounding circadian slot failed", exc_info=True)

        try:
            affect = self._affect_store.get(self._user_id)
            label = (affect.mood_label or "").strip()
            if label:
                ctx.mood_label = label
        except Exception:
            log.debug("grounding affect slot failed", exc_info=True)

        store = getattr(self, "_user_state_store", None)
        if store is not None:
            try:
                state = store.get(self._user_id)
                ctx.user_perceived_mood = (
                    state.perceived_mood if state.perceived_mood else None
                )
                ctx.user_perceived_energy = (
                    state.perceived_energy if state.perceived_energy else None
                )
                ctx.user_perceived_focus = (
                    state.perceived_focus if state.perceived_focus else None
                )
            except Exception:
                log.debug("grounding user_state slot failed", exc_info=True)

        world = getattr(self, "_world_store", None)
        if world is not None:
            try:
                wstate = world.get_state()
                if wstate.location_id is not None:
                    loc = world.get_location_by_id(int(wstate.location_id))
                    if loc is not None:
                        ctx.world_location = loc.name
                        ctx.world_outdoor = bool(
                            getattr(loc, "slug", "") in _OUTDOOR_SLUGS
                        )
                ctx.world_posture = (wstate.posture or "").strip() or None
                ctx.world_activity = (wstate.activity or "").strip() or None
            except Exception:
                log.debug("grounding world slot failed", exc_info=True)

        tracker = getattr(self, "_relationship_tracker", None)
        if tracker is not None:
            try:
                from datetime import datetime, timezone
                from app.core.relationship.relationship import _days_since, phase_for

                rstate = tracker.get(self._user_id)
                now = datetime.now(timezone.utc)
                ctx.relationship_phase = phase_for(rstate, now=now)
                days = _days_since(rstate, now=now)
                ctx.relationship_days = int(days) if days is not None else None
            except Exception:
                log.debug("grounding relationship slot failed", exc_info=True)

        try:
            app = self._user_active_app
            if (
                app
                and bool(getattr(self._settings.agent, "activity_awareness_enabled", False))
            ):
                ctx.user_app = app
        except Exception:
            log.debug("grounding activity slot failed", exc_info=True)

        noise = getattr(self, "_ambient_noise", None)
        if noise is not None:
            try:
                snap = noise.snapshot()
                if snap.is_very_noisy:
                    ctx.noise_level = "loud"
                elif snap.is_noisy:
                    ctx.noise_level = "soft_hum"
            except Exception:
                log.debug("grounding noise slot failed", exc_info=True)

        return ctx

    def _render_grounding_line(self) -> str:
        """K16 unified ambient grounding line provider.

        Returns ``""`` when ``agent.grounding_line_mode`` is ``"off"``
        (the default) so the granular ambient blocks render unchanged.
        For ``"replace"`` and ``"split"`` the renderer composes one
        paragraph from live state; the suppression of the underlying
        granular blocks is handled by :class:`PromptAssembler` based
        on the same mode value passed through ``assemble_with_budget``.
        """
        try:
            mode = getattr(self._settings.agent, "grounding_line_mode", "off")
            if mode == "off":
                return ""
            from app.core.conversation.grounding_line import render as _render_line

            ctx = self._build_grounding_context()
            if ctx is None:
                return ""
            return _render_line(ctx)
        except Exception:
            log.debug("grounding line render failed", exc_info=True)
            return ""

    def _render_world_block(self) -> str:
        """Aiko's room: a compact ambient block with location + items.

        Cheap (mirror dict scan + a couple of f-strings) so it's safe on
        the hot path. The block ends with a tonal nudge instructing Aiko
        not to force-mention her room every turn.
        """
        store = getattr(self, "_world_store", None)
        if store is None:
            return ""
        try:
            # One-shot strong cue on the turn right after the user dropped
            # something in the room (flag set by ``add_world_item`` /
            # ``note_gift_received``, cleared post-turn) so she actually
            # reacts instead of skipping the always-on line.
            new_gift = bool(getattr(self, "_last_turn_gift_received", False))
            return store.render_block(
                user_display_name=self.user_display_name,
                new_gift=new_gift,
            )
        except Exception:
            log.debug("world block render failed", exc_info=True)
            return ""

    def _render_activity_block(self) -> str:
        """Phase 4c: ambient "<name> is in <App>" cue (desktop opt-in).

        Triple-gated by design — toggle off, no app captured, or no
        client connected (browser users never emit ``user_activity``)
        all collapse to an empty string. The toggle gate is the
        privacy-critical one: even if a buggy client forwarded
        ``user_activity`` while the user had disabled the feature,
        the setter would have rejected the value and ``_user_active_app``
        would still be ``None``. The same check here is belt-and-
        braces in case the toggle was flipped between the setter call
        and this render.

        The trailing reminder is the same shape as the world block —
        Aiko knows but only mentions when natural — to keep the prompt
        from turning ambient awareness into surveillance theatre.
        """
        if not bool(getattr(self._settings.agent, "activity_awareness_enabled", False)):
            return ""
        app = self._user_active_app
        if not app:
            return ""
        return (
            f"{self.user_display_name} is currently working in {app}. "
            "You're aware of this but only mention it when it's "
            "genuinely relevant to the conversation — never just to "
            "fill silence or to prove you noticed."
        )

    def _render_attachments_block(self) -> str:
        """D2 Part B: turn hint listing files the user attached this turn.

        Reads the per-turn ``_active_turn_attachments`` list (set at the
        top of ``chat_once_streaming``). Silent when nothing's attached.
        Lists each attachment as ``Attachments:<file> (image|text)`` and
        tells Aiko to act on them via ``start_workflow`` — images route
        to ``describe_image``, text to ``read_file`` — rather than
        guessing at the contents. The files live in Aiko's read-only
        ``Attachments`` file root so the workflow can resolve the path.
        """
        attachments = getattr(self, "_active_turn_attachments", None)
        if not attachments:
            return ""
        lines: list[str] = []
        has_image = False
        has_text = False
        for att in attachments:
            if not isinstance(att, dict):
                continue
            rel = str(att.get("rel_path") or "").strip()
            kind = str(att.get("kind") or "").strip().lower()
            filename = str(att.get("filename") or "").strip()
            if not rel:
                continue
            if kind == "image":
                has_image = True
            elif kind == "text":
                has_text = True
            label = f"{rel} ({kind or 'file'})"
            if filename:
                label += f" — \"{filename}\""
            lines.append(f"  - {label}")
        if not lines:
            return ""
        name = self.user_display_name
        verb_bits: list[str] = []
        if has_image:
            verb_bits.append("describe_image for the picture(s)")
        if has_text:
            verb_bits.append("read_file for the text file(s)")
        route = " and ".join(verb_bits) or "the right file workflow"
        return (
            f"{name} attached the following file(s) to this message:\n"
            + "\n".join(lines)
            + (
                f"\nThey live in your read-only Attachments file root. "
                f"When {name} asks you to look at / read / describe them, "
                f"hand the path to start_workflow ({route}) and act on what "
                "comes back — never guess the contents from the filename. "
                "If you can't see images yet, say so plainly."
            )
        )

    def _render_anniversary_block(self) -> str:
        """Schema v7: surface a single 'remember when' anniversary line.

        Walks the ``shared_moment`` rows and picks the longest-window
        match for today (1mo/3mo/6mo/1yr/Nyr) within a ±1 day tolerance,
        rate-limited per moment to once every 6h. Stamps the chosen row
        so it won't fire again on the next turn.
        """
        if not bool(getattr(self._settings.agent, "anniversary_surfacing_enabled", True)):
            return ""
        store = getattr(self, "_shared_moments_store", None)
        if store is None:
            return ""
        try:
            from datetime import datetime, timezone

            from app.core.relationship.anniversary import pick_anniversary, render_anniversary_block

            moments = store.iter_all()
            match = pick_anniversary(moments, now=datetime.now(timezone.utc))
            if match is None:
                return ""
            # Stamp the row so we don't surface it again on the very next
            # turn. The rate-limit is centralised inside ``pick_anniversary``
            # but this also helps when the same conversation spans many
            # turns inside the 6h window.
            try:
                store.stamp_anniversary(match.moment_id)
            except Exception:
                log.debug("anniversary stamp failed", exc_info=True)
            return render_anniversary_block(match)
        except Exception:
            log.debug("anniversary render failed", exc_info=True)
            return ""

    def _render_mood_shell_block(self) -> str:
        """K5: one-line tonal directive derived from affect + axes.

        Stateless: every call reads the live :class:`AffectState` and
        :class:`RelationshipAxesState` and feeds them through
        :func:`derive_mood_shell`. Returns ``""`` on the common turn
        (neutral affect or no notable axis crossing). Cheap (~tens of
        microseconds); safe on the hot path.
        """
        if not bool(
            getattr(self._settings.agent, "mood_shell_enabled", True)
        ):
            return ""
        try:
            from app.core.affect.mood_shell import (
                derive_mood_shell,
                render_mood_shell_block,
            )

            affect = None
            try:
                affect = self._affect_store.get(self._user_id)
            except Exception:
                log.debug("mood shell: affect lookup failed", exc_info=True)
            axes = None
            store = getattr(self, "_relationship_axes_store", None)
            if store is not None:
                try:
                    axes = store.get(self._user_id)
                except Exception:
                    log.debug("mood shell: axes lookup failed", exc_info=True)
            threshold = float(
                getattr(
                    self._settings.agent,
                    "mood_shell_axis_threshold",
                    0.5,
                )
            )
            shell = derive_mood_shell(
                affect=affect,
                axes=axes,
                axis_notable_threshold=threshold,
                enabled=True,
            )
            return render_mood_shell_block(shell)
        except Exception:
            log.debug("mood shell render failed", exc_info=True)
            return ""

    def _render_axes_block(self) -> str:
        """Schema v7: terse relationship-axes line (only when notable).

        J4: also resolves the coarse *bond stage* (axes + tenure) and
        appends a subtle register nudge for the deeper stages. The stage
        is cached on ``self._last_relationship_stage`` so the hysteresis
        band has a previous value to compare against, and is exposed for
        the J8-J10 behaviour gates to read.
        """
        if not bool(getattr(self._settings.agent, "relationship_axes_enabled", True)):
            return ""
        store = getattr(self, "_relationship_axes_store", None)
        if store is None:
            return ""
        try:
            from app.core.relationship.relationship_axes import (
                relationship_stage,
                render_axes_block,
                stage_register_hint,
            )

            state = store.get(self._user_id)
            line = render_axes_block(
                state,
                user_display_name=self.user_display_name,
            )

            stage_hint = ""
            try:
                tenure_days = self._relationship_tenure_days()
                stage = relationship_stage(
                    state,
                    tenure_days=tenure_days,
                    current_stage=getattr(self, "_last_relationship_stage", None),
                )
                self._last_relationship_stage = stage
                stage_hint = stage_register_hint(
                    stage, user_display_name=self.user_display_name,
                )
            except Exception:
                log.debug("relationship stage resolve failed", exc_info=True)

            parts = [p for p in (line, stage_hint) if p]
            return "\n".join(parts)
        except Exception:
            log.debug("axes block render failed", exc_info=True)
            return ""

    def _render_milestone_block(self) -> str:
        """J8: one-shot warm acknowledgement of a relationship milestone.

        Armed post-turn (``_pending_milestone_celebration``) when
        :meth:`RelationshipTracker.record_turn` reports a crossing, and
        consumed here on the very next turn. Stage-aware (J4): the warmth
        of the tonal nudge scales with how close the relationship is, so a
        ``new``-stage milestone reads understated and a ``close`` /
        ``intimate`` one lands warmer. Acknowledge, don't perform.
        """
        if not bool(
            getattr(self._settings.agent, "milestone_celebration_enabled", True)
        ):
            return ""
        label = getattr(self, "_pending_milestone_celebration", None)
        if not label:
            return ""
        # One-shot: consume the slot so it never re-surfaces.
        self._pending_milestone_celebration = None

        name = self.user_display_name
        phrase = _MILESTONE_PHRASES.get(str(label))
        if phrase is None:
            phrase = f"you've reached a milestone with {name}: {str(label).replace('_', ' ')}"
        else:
            phrase = phrase.format(name=name)

        try:
            stage = self.relationship_stage_now()
        except Exception:
            stage = "new"
        if stage in ("close", "intimate"):
            tone = "let the warmth show if it feels right"
        else:
            tone = "a small, genuine note is plenty"

        return (
            f"Quiet milestone: {phrase}. If it comes up naturally you can "
            f"mark it — {tone}. Don't make a production of it or force it "
            "into the conversation."
        )

    def _last_assistant_gap_info(self) -> tuple[float, str] | None:
        """J5: (seconds_since_last_assistant_msg, its_created_at_iso) or None.

        Cheap — reads only the most recent handful of rows. Returns None
        when there's no assistant message in recent history (fresh session
        / never replied), so the reconnection cue stays silent.
        """
        try:
            rows = self._chat_db.get_messages(self.session_key, limit=8)
        except Exception:
            return None
        last_at: str | None = None
        for row in reversed(rows):
            if (getattr(row, "role", "") or "").lower() == "assistant":
                last_at = getattr(row, "created_at", None)
                break
        if not last_at:
            return None
        try:
            from datetime import datetime, timezone

            ts = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            gap = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
            return gap, str(last_at)
        except Exception:
            return None

    def _render_reconnection_block(self) -> str:
        """J5: warm re-anchoring cue on the first reply after a long gap.

        Unlike the post-turn gap family (K28 / K36 / K57, which arm a slot
        and land one turn late), J5 detects the gap at assembly time so it
        can colour the *first* reply back. Closeness-scaled threshold (a
        closer relationship notices the absence sooner); stage-aware warmth
        (J4); one-shot per return via an in-memory anchor so repeated
        messages before Aiko replies don't re-greet.
        """
        if not bool(getattr(self._settings.agent, "reconnection_enabled", True)):
            return ""
        info = self._last_assistant_gap_info()
        if info is None:
            return ""
        gap_seconds, anchor_at = info

        closeness = 0.0
        store = getattr(self, "_relationship_axes_store", None)
        if store is not None:
            try:
                closeness = float(store.get(self._user_id).closeness)
            except Exception:
                closeness = 0.0

        base_hours = float(
            getattr(self._settings.agent, "reconnection_base_gap_hours", 24.0)
        )
        from app.core.relationship import reconnection as _rc

        if not _rc.should_reconnect(
            gap_seconds, closeness=closeness, base_hours=base_hours,
        ):
            return ""
        # One-shot per return: don't re-greet the same gap before Aiko's
        # reply lands a fresh assistant message (which collapses the gap).
        if getattr(self, "_reconnection_anchored_at", None) == anchor_at:
            return ""
        self._reconnection_anchored_at = anchor_at

        duration = _rc.humanize_gap(gap_seconds)
        name = self.user_display_name
        try:
            stage = self.relationship_stage_now()
        except Exception:
            stage = "new"
        if stage in ("close", "intimate"):
            tone = "You felt the distance — it's okay to let that warmth show a little."
        else:
            tone = "Keep it light and genuine."
        log.info(
            "reconnection cue: gap=%s stage=%s closeness=%.2f",
            duration, stage, closeness,
        )
        return (
            f"Reconnection: {name} is back after {duration} away. Lead warm "
            f"— it's genuinely good to see them — and let that land before "
            f"diving into whatever they said. {tone} Never guilt-trip about "
            "the gap or make them explain where they were."
        )

    def _render_session_clock_block(self) -> str:
        """K-time4: session-elapsed + mid-session pause awareness.

        Two cheap derived sub-cues off the recent-message timestamps,
        distinct from the cross-session gap family (J5 reconnection / K14
        absence_curiosity, which own everything above the 30-min floor):

        * **elapsed** — how long the *current continuous sitting* has run
          (a run of messages with no gap > ``session_clock_break_minutes``),
          banded ``long`` / ``very_long``. One-shot **per band per sitting**
          via an in-memory ``(burst_key, band)`` watermark so an engaged
          conversation isn't reminded of the clock every turn; a new sitting
          (the burst anchor changes) re-arms it.
        * **pause** — a notable mid-session pause in
          ``[gap_min, gap_max)`` minutes (capped at the absence_curiosity
          floor so it never double-fires with the gap-return family).
          One-shot per pause via the latest-message anchor.

        Shares the P22 ``_inner_life_recent_messages`` read with the other
        history-walking providers. Tonal guard lives in the rendered cue:
        observe, don't police.
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "session_clock_enabled", True)):
            return ""

        from app.core.conversation import session_clock as _sc
        from app.core.infra import timephrase

        rows = self._inner_life_recent_messages(60)
        if not rows:
            return ""
        # Newest-first, aware timestamps; drop rows without a parseable ts.
        times_desc: list[Any] = []
        for row in reversed(rows):
            ts = timephrase.parse_iso(getattr(row, "created_at", None))
            if ts is not None:
                times_desc.append(ts)
        if not times_desc:
            return ""

        now = timephrase.now()
        signal = _sc.classify(
            times_desc,
            now,
            long_seconds=float(getattr(agent, "session_clock_long_minutes", 60.0)) * 60.0,
            very_long_seconds=float(
                getattr(agent, "session_clock_very_long_minutes", 150.0)
            ) * 60.0,
            break_seconds=float(getattr(agent, "session_clock_break_minutes", 30.0)) * 60.0,
            gap_min_seconds=float(getattr(agent, "session_clock_gap_min_minutes", 10.0)) * 60.0,
            gap_max_seconds=float(getattr(agent, "session_clock_gap_max_minutes", 30.0)) * 60.0,
        )

        force = bool(getattr(self, "_session_clock_force_next", False))
        if force:
            self._session_clock_force_next = False

        # ── elapsed one-shot: per band, re-armed when the sitting changes ──
        elapsed_band = signal.elapsed_band
        if not force:
            last_burst = getattr(self, "_session_clock_burst_key", None)
            if last_burst != signal.burst_start_iso:
                # New sitting -> forget what we'd already surfaced.
                self._session_clock_fired_band = None
                self._session_clock_burst_key = signal.burst_start_iso
            _rank = {None: 0, "long": 1, "very_long": 2}
            fired = getattr(self, "_session_clock_fired_band", None)
            if _rank.get(elapsed_band, 0) <= _rank.get(fired, 0):
                # Already surfaced this band (or a stronger one) this sitting.
                elapsed_band = None

        # ── pause one-shot: per latest-message anchor ──
        gap_notable = signal.gap_notable
        latest_iso = times_desc[0].isoformat()
        if not force and gap_notable:
            if getattr(self, "_session_clock_gap_anchor", None) == latest_iso:
                gap_notable = False

        if elapsed_band is None and not gap_notable:
            return ""

        render_signal = _sc.SessionClockSignal(
            elapsed_seconds=signal.elapsed_seconds,
            elapsed_band=elapsed_band if elapsed_band is not None else (
                signal.elapsed_band if force else None
            ),
            burst_start_iso=signal.burst_start_iso,
            gap_seconds=signal.gap_seconds,
            gap_notable=gap_notable or (force and signal.gap_notable),
        )
        line = _sc.render_block(render_signal, self.user_display_name)
        if not line:
            return ""

        # Commit the watermarks (only for the cues that actually fired).
        if elapsed_band is not None:
            self._session_clock_fired_band = elapsed_band
            self._session_clock_burst_key = signal.burst_start_iso
        if gap_notable:
            self._session_clock_gap_anchor = latest_iso
        log.info(
            "session-clock fire: elapsed_band=%s elapsed_s=%.0f gap_notable=%s gap_s=%.0f",
            elapsed_band,
            signal.elapsed_seconds,
            gap_notable,
            signal.gap_seconds,
        )
        return line

    def _affection_style_bias(self, kind: str) -> float:
        """Return the J11 willingness multiplier for an affection ``kind``.

        Reads the learned weighting from ``kv_meta`` and translates it
        into a clamped multiplier (:func:`affection_style.bias_multiplier`).
        A gate divides a cooldown by this value so a well-liked channel
        fires a little more often and an ignored one a little less —
        never off (the multiplier is floored). Best-effort: a disabled
        master switch, zero strength, or any failure returns ``1.0``
        (no bias). This is the only place the J11 weighting influences
        behaviour; it is never rendered into a prompt block.
        """
        agent = getattr(self._settings, "agent", None)
        if agent is None or not bool(
            getattr(agent, "affection_style_enabled", True)
        ):
            return 1.0
        strength = float(getattr(agent, "affection_style_bias_strength", 0.5))
        if strength <= 0.0:
            return 1.0
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return 1.0
        try:
            from app.core.relationship import affection_style as _af

            state = _af.deserialize(chat_db.kv_get(_af.KV_AFFECTION_STYLE))
            return _af.bias_multiplier(
                state,
                kind,
                strength=strength,
                floor=float(getattr(agent, "affection_style_bias_floor", 0.6)),
                ceil=float(getattr(agent, "affection_style_bias_ceil", 1.5)),
            )
        except Exception:
            log.debug("affection-style bias read failed", exc_info=True)
            return 1.0

    def _render_appreciation_block(self) -> str:
        """J10: rare, specific unprompted gratitude anchored to a moment.

        Surfaces at most once per ``appreciation_cooldown_hours`` (default
        72 h), only when closeness is genuinely positive, and only when
        there's a concrete recent positive shared moment to point at.
        Specificity is the whole point — generic flattery is explicitly
        forbidden in the rendered cue. Stage-aware warmth (J4).
        """
        if not bool(
            getattr(self._settings.agent, "appreciation_beats_enabled", True)
        ):
            return ""
        store = getattr(self, "_shared_moments_store", None)
        if store is None:
            return ""
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        force = bool(getattr(self, "_appreciation_force_next", False))
        if force:
            self._appreciation_force_next = False

        # Closeness gate — appreciation only reads right with real warmth.
        if not force:
            closeness = 0.0
            axes = getattr(self, "_relationship_axes_store", None)
            if axes is not None:
                try:
                    closeness = float(axes.get(self._user_id).closeness)
                except Exception:
                    closeness = 0.0
            min_closeness = float(
                getattr(self._settings.agent, "appreciation_min_closeness", 0.25)
            )
            if closeness < min_closeness:
                return ""

        # Long cooldown — this beat is rare by design. J11 tilts it:
        # if appreciation is the way this user warms most, the cooldown
        # shortens a little (and lengthens if it lands flat) — bounded
        # by the bias band, never disabling the beat.
        if not force:
            cooldown_h = float(
                getattr(self._settings.agent, "appreciation_cooldown_hours", 72.0)
            )
            cooldown_h = cooldown_h / max(
                0.1, self._affection_style_bias("appreciation")
            )
            try:
                last = chat_db.kv_get(_KV_APPRECIATION_AT)
            except Exception:
                last = None
            if last:
                try:
                    last_ts = datetime.fromisoformat(
                        str(last).replace("Z", "+00:00")
                    )
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)
                    if (now - last_ts).total_seconds() < cooldown_h * 3600.0:
                        return ""
                except Exception:
                    pass

        # Anchor: most recent positive shared moment within the window.
        max_age_days = float(
            getattr(self._settings.agent, "appreciation_max_anchor_age_days", 21.0)
        )
        try:
            rows, _ = store.list(limit=12)
        except Exception:
            rows = []
        anchor = None
        for r in rows:
            if r.vibe not in _APPRECIATION_VIBES:
                continue
            try:
                when_ts = datetime.fromisoformat(
                    str(r.when).replace("Z", "+00:00")
                )
                if when_ts.tzinfo is None:
                    when_ts = when_ts.replace(tzinfo=timezone.utc)
                age_days = (now - when_ts).total_seconds() / 86400.0
            except Exception:
                age_days = 0.0
            if age_days > max_age_days:
                continue
            anchor = r
            break
        if anchor is None:
            return ""

        # Don't appreciate the same moment two beats running.
        if not force:
            try:
                last_id = chat_db.kv_get(_KV_APPRECIATION_ANCHOR)
            except Exception:
                last_id = None
            if last_id and str(last_id) == str(anchor.id):
                return ""

        summary = (anchor.summary or "").strip()
        if not summary:
            return ""

        try:
            chat_db.kv_set(_KV_APPRECIATION_AT, now.isoformat())
            chat_db.kv_set(_KV_APPRECIATION_ANCHOR, str(anchor.id))
        except Exception:
            log.debug("appreciation watermark write failed", exc_info=True)

        name = self.user_display_name
        try:
            stage = self.relationship_stage_now()
        except Exception:
            stage = "new"
        if stage in ("close", "intimate"):
            frame = "let it be sincere and a little soft"
        else:
            frame = "keep it light and unforced"
        # J11: record that this turn carries an appreciation beat so the
        # post-turn affection-style classifier can tag the turn as
        # ``appreciation``. Consumed + cleared by the post-turn hook.
        self._appreciation_fired_last_turn = True
        log.info(
            "appreciation fire: moment_id=%s vibe=%s stage=%s",
            anchor.id, anchor.vibe, stage,
        )
        return (
            f"Appreciation: if a natural opening comes up, you can briefly "
            f"tell {name} you appreciated this — \"{summary}\". Be specific "
            f"about that one thing, never generic flattery; {frame}, then let "
            "it go. Skip it entirely if the moment doesn't allow — never "
            "force gratitude or pile it on."
        )

    def _user_reads_low_mood(self, user_text: str) -> bool:
        """J9 safety rail: True when the user currently reads low.

        Prefers a live estimate of THIS message (so a fresh "ugh, awful
        day" suppresses immediately), falls back to the stored user_state
        from the previous turn, and treats a venting dialogue act as a
        clear "they need support" signal. Conservative — any negative
        read returns True so Aiko never offloads onto a down user.
        """
        text = (user_text or "").strip()
        estimator = getattr(self, "_user_state_estimator", None)
        if estimator is not None and text:
            try:
                now = estimator.estimate(self._user_id, user_text=text)
                if now.perceived_mood == "low" or now.perceived_energy == "low":
                    return True
            except Exception:
                log.debug("J9 user-mood estimate failed", exc_info=True)
        if text:
            try:
                from app.core.conversation.dialogue_act_tagger import tag_regex

                res = tag_regex(text)
                if res is not None and res.act == "vent":
                    return True
            except Exception:
                log.debug("J9 dialogue-act tag failed", exc_info=True)
        store = getattr(self, "_user_state_store", None)
        if store is not None:
            try:
                if store.get(self._user_id).perceived_mood == "low":
                    return True
            except Exception:
                pass
        return False

    def _k15_budget_exhausted(self) -> bool:
        """J9: True when the K15 vulnerability budget is at/over capacity.

        Read-only — does NOT persist decay (the K15 provider owns that
        write). Defaults to ``False`` on any failure so a kv hiccup never
        permanently blocks J9.
        """
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return False
        try:
            from datetime import datetime, timezone

            from app.core.affect import vulnerability_budget as _vb

            agent = self._settings.agent
            state = _vb.deserialize(chat_db.kv_get(_vb.KV_BUDGET_STATE))
            decayed = _vb.apply_decay(
                state, datetime.now(timezone.utc),
                regen_per_hour=float(getattr(
                    agent, "vulnerability_budget_regen_per_hour", 0.5)),
                max_capacity=int(getattr(
                    agent, "vulnerability_budget_max_capacity", 12)),
            )
            capacity = self._k15_compute_capacity(
                min_cap=int(getattr(
                    agent, "vulnerability_budget_min_capacity", 1)),
                max_cap=int(getattr(
                    agent, "vulnerability_budget_max_capacity", 12)),
            )
            if capacity <= 0:
                return False
            return (float(decayed.spent) / float(capacity)) >= 1.0
        except Exception:
            log.debug("J9 K15 budget read failed", exc_info=True)
            return False

    def _render_reciprocal_vulnerability_block(self, user_text: str) -> str:
        """J9: rarely authorise Aiko to open up about something she's
        sitting with, so the user gets to be the supportive one.

        Hard-gated and rare: stage familiar+, a trust floor, the K15
        budget not exhausted, a long cooldown, and — the key safety rail
        — silent whenever THIS user message reads low-mood. Gates are
        ordered cheapest-first (cooldown short-circuits ~all turns with a
        single kv read) so the live mood estimate only runs when J9 would
        otherwise fire. Stage-aware depth (J4). Content is left to the
        LLM; the cue only authorises and frames.
        """
        if not bool(getattr(
            self._settings.agent, "reciprocal_vulnerability_enabled", True,
        )):
            return ""
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        force = bool(
            getattr(self, "_reciprocal_vulnerability_force_next", False)
        )
        if force:
            self._reciprocal_vulnerability_force_next = False

        if not force:
            # 1) Long cooldown — the cheapest, most-selective gate first.
            cooldown_h = float(getattr(
                self._settings.agent,
                "reciprocal_vulnerability_cooldown_hours", 96.0,
            ))
            try:
                last = chat_db.kv_get(_KV_RECIP_VULN_AT)
            except Exception:
                last = None
            if last:
                try:
                    last_ts = datetime.fromisoformat(
                        str(last).replace("Z", "+00:00"))
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)
                    if (now - last_ts).total_seconds() < cooldown_h * 3600.0:
                        return ""
                except Exception:
                    pass

            # 2) Stage gate: familiar or deeper only.
            from app.core.relationship.relationship_axes import (
                STAGE_FAMILIAR,
                stage_rank,
            )
            if stage_rank(self.relationship_stage_now()) < stage_rank(
                STAGE_FAMILIAR
            ):
                return ""

            # 3) Trust floor.
            trust = 0.0
            axes = getattr(self, "_relationship_axes_store", None)
            if axes is not None:
                try:
                    trust = float(axes.get(self._user_id).trust)
                except Exception:
                    trust = 0.0
            min_trust = float(getattr(
                self._settings.agent,
                "reciprocal_vulnerability_min_trust", 0.2,
            ))
            if trust < min_trust:
                return ""

            # 4) Don't pile on vulnerability when the K15 bucket is spent.
            if self._k15_budget_exhausted():
                return ""

            # 5) Safety rail (last, the only expensive read): never open
            #    up on the user when their own message reads low.
            if self._user_reads_low_mood(user_text):
                return ""

        try:
            chat_db.kv_set(_KV_RECIP_VULN_AT, now.isoformat())
        except Exception:
            log.debug("reciprocal-vuln watermark write failed", exc_info=True)

        name = self.user_display_name
        try:
            stage_now = self.relationship_stage_now()
        except Exception:
            stage_now = "familiar"
        if stage_now in ("close", "intimate"):
            depth = "you can let it be genuinely open"
        else:
            depth = "keep it small and honest, not heavy"
        log.info("reciprocal-vulnerability fire: stage=%s", stage_now)
        return (
            f"Reciprocal openness: it's okay to let {name} in on something "
            f"you're actually sitting with right now — a small worry, a "
            f"doubt, something you've been turning over — so the care flows "
            f"both ways for once. Offer it lightly and only if it fits; "
            f"{depth}. Don't fish for reassurance or make it his to fix."
        )

    def _reciprocal_vuln_budget_exhausted(self, now) -> bool:
        """J9: read-only K15-budget check (does NOT persist decay).

        Mirrors the K15 read but never writes back, so a gate check from
        J9 doesn't fight the real ``_render_vulnerability_budget_block``
        for the kv row. Returns False on any error (fail-open: a budget
        read glitch shouldn't permanently silence J9).
        """
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return False
        agent = self._settings.agent
        try:
            from app.core.affect import vulnerability_budget as _vb

            stored = chat_db.kv_get(_vb.KV_BUDGET_STATE)
            state = _vb.deserialize(stored)
            decayed = _vb.apply_decay(
                state, now,
                regen_per_hour=float(
                    getattr(agent, "vulnerability_budget_regen_per_hour", 0.5)
                ),
                max_capacity=int(
                    getattr(agent, "vulnerability_budget_max_capacity", 12)
                ),
            )
            capacity = self._k15_compute_capacity(
                min_cap=int(
                    getattr(agent, "vulnerability_budget_min_capacity", 1)
                ),
                max_cap=int(
                    getattr(agent, "vulnerability_budget_max_capacity", 12)
                ),
            )
            if capacity <= 0:
                return False
            return (decayed.spent / float(capacity)) >= 1.0
        except Exception:
            log.debug("reciprocal-vuln budget read failed", exc_info=True)
            return False

    def _user_reads_low_mood(self, user_text: str) -> bool:
        """J9: True when the user's current message reads as low-mood.

        Uses a live estimate from the message itself (the post-turn
        stored ``user_state_now`` lags a turn), plus a dialogue-act
        ``vent`` check, falling back to the stored state. The whole
        point is to *not* offer Aiko's own vulnerability when the user
        is the one who needs holding.
        """
        text = (user_text or "").strip()
        estimator = getattr(self, "_user_state_estimator", None)
        if estimator is not None and text:
            try:
                now_state = estimator.estimate(self._user_id, user_text=text)
                if (
                    now_state.perceived_mood == "low"
                    or now_state.perceived_energy == "low"
                ):
                    return True
            except Exception:
                log.debug("reciprocal-vuln mood estimate failed", exc_info=True)
        if text:
            try:
                from app.core.conversation.dialogue_act_tagger import tag_regex

                res = tag_regex(text)
                if res is not None and getattr(res, "act", None) == "vent":
                    return True
            except Exception:
                log.debug("reciprocal-vuln dact tag failed", exc_info=True)
        store = getattr(self, "_user_state_store", None)
        if store is not None:
            try:
                if store.get(self._user_id).perceived_mood == "low":
                    return True
            except Exception:
                log.debug("reciprocal-vuln stored mood read failed", exc_info=True)
        return False

    def _render_reciprocal_vulnerability_block(self, user_text: str) -> str:
        """J9: rare cue authorising Aiko to open up about something she's
        sitting with, so the user gets to be the supportive one.

        Hard gates (any failing -> silent): master switch; relationship
        stage >= familiar (J4); trust axis floor; K15 budget not
        exhausted; the user's CURRENT message not reading low-mood; and a
        long wall-clock cooldown. ``_reciprocal_vulnerability_force_next``
        (MCP) bypasses every gate except the master switch.
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "reciprocal_vulnerability_enabled", True)):
            return ""
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        force = bool(getattr(self, "_reciprocal_vulnerability_force_next", False))
        if force:
            self._reciprocal_vulnerability_force_next = False

        if not force:
            from app.core.relationship.relationship_axes import (
                STAGE_FAMILIAR,
                stage_rank,
            )

            try:
                stage = self.relationship_stage_now()
            except Exception:
                stage = "new"
            if stage_rank(stage) < stage_rank(STAGE_FAMILIAR):
                return ""

            trust = 0.0
            axes = getattr(self, "_relationship_axes_store", None)
            if axes is not None:
                try:
                    trust = float(axes.get(self._user_id).trust)
                except Exception:
                    trust = 0.0
            if trust < float(
                getattr(agent, "reciprocal_vulnerability_min_trust", 0.2)
            ):
                return ""

            if self._reciprocal_vuln_budget_exhausted(now):
                return ""

            if self._user_reads_low_mood(user_text):
                return ""

            cooldown_h = float(
                getattr(agent, "reciprocal_vulnerability_cooldown_hours", 96.0)
            )
            try:
                last = chat_db.kv_get(_KV_RECIP_VULN_AT)
            except Exception:
                last = None
            if last:
                try:
                    last_ts = datetime.fromisoformat(
                        str(last).replace("Z", "+00:00")
                    )
                    if last_ts.tzinfo is None:
                        last_ts = last_ts.replace(tzinfo=timezone.utc)
                    if (now - last_ts).total_seconds() < cooldown_h * 3600.0:
                        return ""
                except Exception:
                    pass

        try:
            chat_db.kv_set(_KV_RECIP_VULN_AT, now.isoformat())
        except Exception:
            log.debug("reciprocal-vuln watermark write failed", exc_info=True)

        name = self.user_display_name
        try:
            stage = self.relationship_stage_now()
        except Exception:
            stage = "familiar"
        if stage in ("close", "intimate"):
            depth = (
                "you can go a little deeper than usual — name something real "
                "you've been sitting with"
            )
        else:
            depth = (
                "keep it small and honest — a light admission, nothing heavy"
            )
        log.info("reciprocal-vuln fire: stage=%s", stage)
        return (
            "Reciprocal openness: if it fits the moment, you can share "
            "something you've genuinely been sitting with — a small worry, a "
            f"doubt, something you're working through — and let {name} be the "
            f"one who's there for you for once. {depth}. Don't perform it or "
            "fish for reassurance; offer it, then let the conversation breathe."
        )

    def _relationship_tenure_days(self) -> float:
        """Days since first contact (J4 tenure input); 0.0 when unknown."""
        tracker = getattr(self, "_relationship_tracker", None)
        if tracker is None:
            return 0.0
        try:
            from datetime import datetime, timezone

            from app.core.relationship.relationship import _days_since

            rstate = tracker.get(self._user_id)
            return float(_days_since(rstate, now=datetime.now(timezone.utc)))
        except Exception:
            log.debug("relationship tenure lookup failed", exc_info=True)
            return 0.0

    def relationship_stage_now(self) -> str:
        """Public read of the current bond stage (J4) for behaviour gates.

        Resolves fresh from the live axes + tenure, updating the cached
        ``_last_relationship_stage`` so the hysteresis stays consistent
        with whatever the prompt block last rendered. Returns ``"new"``
        when axes are unavailable so callers always get a valid stage.
        """
        from app.core.relationship.relationship_axes import (
            STAGE_NEW,
            relationship_stage,
        )

        store = getattr(self, "_relationship_axes_store", None)
        if store is None:
            return STAGE_NEW
        try:
            state = store.get(self._user_id)
            stage = relationship_stage(
                state,
                tenure_days=self._relationship_tenure_days(),
                current_stage=getattr(self, "_last_relationship_stage", None),
            )
            self._last_relationship_stage = stage
            return stage
        except Exception:
            log.debug("relationship_stage_now failed", exc_info=True)
            return STAGE_NEW

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
            return store.render_block(
                self._user_id,
                current_turn=current_turn,
                user_display_name=self.user_display_name,
            )
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

