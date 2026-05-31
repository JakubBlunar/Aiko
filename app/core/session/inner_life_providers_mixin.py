"""Inner-life prompt-block providers mixin.

Extracted from :mod:`app.core.session.session_controller` to keep the controller
shell readable. Covers every per-turn ``_render_*`` block provider that
the prompt assembler asks for, plus the K16 grounding-context builder,
the small avatar-capability accessors used by the prompt grammar, and
the ``_cadence_context`` helper that feeds the cadence engine.

These are pure read methods that delegate to stores already on
``self`` (``self._affect_store``, ``self._memory_store``, etc.), so
they have no init-order risk: the mixin only ever runs after
``SessionController.__init__`` has finished wiring the host class.

State ownership stays in ``SessionController.__init__``; this mixin
just reads ``self.*``.

NB: tests that previously patched
``app.core.session.session_controller.<symbol>`` for any of the moved methods
must patch ``app.core.session.inner_life_providers_mixin.<symbol>``
instead. The patch must target the module where the symbol is
*looked up*.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.affect import circadian as _circadian


log = logging.getLogger("app.session")


class InnerLifeProvidersMixin:
    """Per-turn prompt-block providers, grounding builder, avatar accessors."""

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
        label = self._NARRATIVE_LABELS.get(
            (nudge.source_kind or "").strip().lower(),
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

    def _render_knowledge_gaps_block(self, user_text: str) -> str:
        """F2: surface the open knowledge gap most relevant to ``user_text``.

        Returns at most one bullet. Empty string when there are no open
        gaps or the best similarity match is below the threshold (so we
        don't surface a totally unrelated wondering on every turn). The
        block ends without a trailing newline so the assembler can stitch
        it next to its siblings.
        """
        store = getattr(self, "_knowledge_gap_store", None)
        if store is None:
            return ""
        try:
            gap = store.pick_relevant(user_text)
        except Exception:
            log.debug("knowledge gap pick_relevant failed", exc_info=True)
            return ""
        if gap is None:
            return ""
        meta = getattr(gap, "metadata", None) or {}
        if not isinstance(meta, dict):
            meta = {}
        topic = str(meta.get("topic") or "").strip()
        question = str(meta.get("question") or "").strip()
        if not question:
            # Defensive: a gap row without question metadata is still
            # worth surfacing via its raw content.
            question = (gap.content or "").strip()
        if not question:
            return ""
        bullet = f"- {topic}: {question}" if topic else f"- {question}"
        return (
            f"Things you've been wondering about with {self.user_display_name}:\n"
            + bullet
        )

    def _render_belief_gaps_block(self) -> str:
        """K2: surface up to two belief-gap lines from the previous turn.

        The gap detector runs in ``_post_turn_inner_life`` and stashes
        any detected mismatches into ``self._pending_belief_gaps``. We
        consume that list here (clearing it after read) so the gap
        only appears in the next turn's prompt -- after that Aiko
        either addressed it or the belief got contradicted/confirmed
        and won't re-surface.
        """
        if not bool(getattr(self._settings.agent, "belief_tracking_enabled", True)):
            return ""
        gaps = getattr(self, "_pending_belief_gaps", None) or []
        if not gaps:
            return ""
        try:
            from app.core.relationship.belief_gap_detector import render_inner_life_block

            block = render_inner_life_block(gaps, max_lines=2)
        except Exception:
            log.debug("belief gaps render failed", exc_info=True)
            block = ""
        # Clear regardless of render success so we don't keep retrying
        # the same broken render on every turn.
        self._pending_belief_gaps = []
        if not block:
            return ""
        return (
            f"Your theory-of-mind read on {self.user_display_name} "
            "doesn't quite match the live signal:\n" + block + "\n"
            "Name the gap once and gently if it fits, then move on. "
            "Don't repeat the question."
        )

    def _render_clarification_block(self) -> str:
        """K17: surface a one-shot clarification-repair cue.

        The detector runs inline from ``_post_turn_inner_life`` and
        stashes any hit into ``self._pending_clarification``. We
        consume the slot here (clearing it after the read) so the
        cue appears in exactly one prompt -- the very next turn
        after the user signalled "you missed it". After that Aiko
        either fixed it (good) or didn't (and the user will re-fire
        the trigger anyway), so a sticky cue would just spam.
        """
        if not bool(
            getattr(self._settings.agent, "clarification_repair_enabled", True)
        ):
            return ""
        result = getattr(self, "_pending_clarification", None)
        if result is None:
            return ""
        # Clear before rendering so a render exception still resets
        # the slot -- sticky cues are worse than missing cues here.
        self._pending_clarification = None
        try:
            from app.core.conversation.clarification_detector import render_inner_life_block

            return render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("clarification render failed", exc_info=True)
            return ""

    def _render_calibration_block(self) -> str:
        """K20: surface a one-line calibration hedge cue.

        Reads the per-user :class:`CalibrationState`, applies lazy
        decay so the snapshot is current, and renders a hedge cue
        when the global score sits below the configured threshold OR
        a topic slot sits below the topic threshold. Topic-specific
        cue wins when both fire.

        Returns ``""`` (empty -- not ``None``) when the master switch
        is off, the store is unavailable, or the state hasn't dropped
        below either threshold. Empty strings are dropped by the
        prompt assembler, so the cue family is silent by default.
        """
        if not bool(
            getattr(
                self._settings.agent,
                "calibration_detection_enabled",
                True,
            )
        ):
            return ""
        store = getattr(self, "_calibration_store", None)
        if store is None:
            return ""
        try:
            from app.core.affect import calibration_detector
            from datetime import datetime, timezone

            state = store.get(self._user_id)
            state = calibration_detector.decay(
                state,
                now=datetime.now(timezone.utc),
                half_life_days=float(
                    getattr(
                        self._memory_settings,
                        "calibration_half_life_days",
                        5.0,
                    )
                ),
                baseline=float(
                    getattr(
                        self._memory_settings,
                        "calibration_baseline",
                        0.80,
                    )
                ),
            )
            block = calibration_detector.render_inner_life_block(
                state,
                user_display_name=self.user_display_name,
                global_threshold=float(
                    getattr(
                        self._memory_settings,
                        "calibration_global_low_threshold",
                        0.55,
                    )
                ),
                topic_threshold=float(
                    getattr(
                        self._memory_settings,
                        "calibration_topic_low_threshold",
                        0.50,
                    )
                ),
            )
            return block or ""
        except Exception:
            log.debug("calibration render failed", exc_info=True)
            return ""

    def _render_sensory_anchor_block(self) -> str:
        """K24: surface a "small physical beat available" cue.

        Reads :class:`RoomState` + nearby items from
        :class:`WorldStore`, the live conversation arc from
        :class:`ArcStore`, and ticks the per-controller
        :class:`SensoryAnchorCadence`. The cadence handles the
        cooldown counter, arc-weighted probability roll,
        posture-kind compatibility filter, and no-repeat ring; we
        just feed it world state.

        Returns ``""`` (empty -- not ``None``) when the master
        switch is off, the cadence is unavailable, the world store
        is missing, or the cadence chooses not to fire (silent
        turn). Empty strings are dropped by the prompt assembler,
        so the cue family is silent by default.
        """
        if not bool(
            getattr(
                self._settings.agent, "sensory_anchor_enabled", True,
            )
        ):
            return ""
        cadence = getattr(self, "_sensory_anchor_cadence", None)
        if cadence is None:
            return ""
        world_store = getattr(self, "_world_store", None)
        if world_store is None:
            return ""
        try:
            from app.core.conversation import sensory_anchor

            room_state = world_store.get_state()
            posture = (room_state.posture or "").strip().lower()
            if not posture:
                return ""
            # Pull room items only -- carried items (location_id
            # IS NULL in the schema) are intentionally excluded so
            # "items she has at her current location" stays clean
            # and the no-repeat ring tracks position-aware beats.
            items = world_store.list_items(
                location_id=room_state.location_id,
            )
            if not items:
                return ""
            arc_state = None
            arc_store = getattr(self, "_arc_store", None)
            if arc_store is not None:
                try:
                    arc_state = arc_store.get_or_default(self._user_id)
                except Exception:
                    log.debug(
                        "sensory_anchor: arc fetch failed", exc_info=True,
                    )
                    arc_state = None
            arc = (
                arc_state.arc if arc_state is not None
                else "casual_check_in"
            )
            beat = cadence.tick(
                posture=posture,
                items=items,
                arc=arc,
                min_turn_gap=int(
                    getattr(
                        self._memory_settings,
                        "sensory_anchor_min_turn_gap",
                        4,
                    )
                ),
                probability_scale=float(
                    getattr(
                        self._memory_settings,
                        "sensory_anchor_probability_scale",
                        1.0,
                    )
                ),
                max_window=int(
                    getattr(
                        self._memory_settings,
                        "sensory_anchor_max_window_items",
                        6,
                    )
                ),
            )
            if beat is None:
                return ""
            return sensory_anchor.render_inner_life_block(
                beat, user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("sensory_anchor render failed", exc_info=True)
            return ""

    def _render_absence_curiosity_block(self) -> str:
        """K14 typed-mode: surface a one-shot absence-curiosity cue.

        Reads ``self._pending_absence_seconds`` (set by the post-turn
        engagement tracker when the typed gap landed in the
        absence-curiosity band) and renders a short line nudging Aiko
        toward warm curiosity about where the user has been. One-shot:
        the slot is cleared on read so the cue appears exactly once.

        Empty string when the master switch is off, when no absence
        result is pending, or when the duration falls outside the
        configured band (defensive double-check against settings that
        flipped between turns).
        """
        if not bool(
            getattr(
                self._settings.agent,
                "engagement_absence_curiosity_enabled",
                True,
            )
        ):
            return ""
        seconds = getattr(self, "_pending_absence_seconds", None)
        if seconds is None:
            return ""
        self._pending_absence_seconds = None
        try:
            seconds_f = float(seconds)
        except (TypeError, ValueError):
            return ""
        if seconds_f <= 0.0:
            return ""

        # Friendly duration string. Bands picked so a 32-min gap reads
        # as "about half an hour", a 95-min gap as "an hour and a
        # half", and a 3h gap as "a few hours" -- all sound natural
        # in conversation, none cite the raw value.
        if seconds_f < 60.0 * 45:
            duration = "about half an hour"
        elif seconds_f < 60.0 * 75:
            duration = "an hour or so"
        elif seconds_f < 60.0 * 105:
            duration = "an hour and a half"
        elif seconds_f < 60.0 * 60 * 2.5:
            duration = "a couple of hours"
        else:
            duration = "a few hours"

        name = self.user_display_name or "the user"
        return (
            f"Absence-curiosity: {name} was away for {duration} before "
            "this message. Welcome them back as if they just stepped "
            "into the room with you -- be lightly curious about what "
            "they were up to if it feels natural, but DON'T announce "
            "the gap or make them feel like they owe you an "
            "explanation. The cue is curiosity, not absence-anxiety."
        )

    def _render_rupture_block(self) -> str:
        """K8: surface a one-shot affect-rupture cue.

        Same one-shot contract as :meth:`_render_clarification_block`
        and :meth:`_render_belief_gaps_block` -- the post-turn
        detector stashes a result on the controller; we render it
        once and clear the slot. Affect-rupture is *not* a sticky
        cue: if Aiko softens and Jacob's mood recovers next turn,
        re-firing would be patronising. If it doesn't recover, the
        next-turn delta will fire the detector again organically.
        """
        if not bool(
            getattr(self._settings.agent, "rupture_repair_enabled", True)
        ):
            return ""
        result = getattr(self, "_pending_rupture", None)
        if result is None:
            return ""
        self._pending_rupture = None
        try:
            from app.core.affect.affect_rupture_detector import render_inner_life_block

            return render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("rupture render failed", exc_info=True)
            return ""

    def _render_misattunement_block(self, user_text: str) -> str:
        """K23: surface a per-turn ``mild_disengagement`` cue.

        Provider-time (not post-turn stash) so the cue lands on the
        SAME turn that's about to reply to the disengaging message --
        pulling back IS the next reply, not the one after. Reads:

        * Last assistant ``MessageRow`` from chat history (for the
          shrink trigger's ``prev_aiko_words`` input).
        * K6 :class:`NoveltyDetector` ``last_band`` / ``last_distance``
          for the pivot trigger. K6's provider always runs *earlier*
          in the assembly chain (its ``novelty`` block lands above
          the ``misattunement`` slot in ``system_parts``), so the
          fields are already populated for this turn.

        Decrements the cooldown counter by 1 on every call regardless
        of trigger state -- otherwise a long-running session of
        regular replies would never let an old fire expire. On a
        hit, arms the cooldown to
        ``agent.misattunement_cooldown_turns``.
        """
        if not bool(
            getattr(self._settings.agent, "misattunement_detection_enabled", True)
        ):
            return ""
        try:
            from app.core.affect import misattunement_detector
        except Exception:
            log.debug("misattunement detector import failed", exc_info=True)
            return ""

        # Decrement cooldown first so a quiet turn always whittles the
        # counter down -- otherwise a session that never trips a
        # trigger would keep a stale armed cooldown forever.
        current_cooldown = max(0, int(getattr(self, "_misattunement_cooldown", 0)))
        if current_cooldown > 0:
            self._misattunement_cooldown = current_cooldown - 1

        # MCP-debug bypass: force_misattunement() sets a one-shot flag
        # that ignores the (newly-decremented) cooldown for this call.
        # Cleared whether we fire or not so the bypass is strictly
        # one-turn.
        force_next = bool(
            getattr(self, "_misattunement_force_next", False)
        )
        if force_next:
            self._misattunement_force_next = False
            cooldown_for_detect = 0
        else:
            cooldown_for_detect = self._misattunement_cooldown

        user_words = len((user_text or "").split())
        if user_words <= 0:
            return ""

        # Last assistant reply word count -- scan the last few rows
        # (oldest-first window) backwards for the most recent
        # ``role == "assistant"``. ``None`` when no prior assistant
        # turn (cold-start session) so the shrink trigger no-ops; the
        # pivot trigger can still fire on K6 alone.
        prev_aiko_words: int | None = None
        try:
            recent = self._chat_db.get_messages(self.session_key, limit=6)
            for row in reversed(recent):
                if row.role == "assistant" and (row.content or "").strip():
                    prev_aiko_words = len(row.content.split())
                    break
        except Exception:
            log.debug("misattunement: chat_db read failed", exc_info=True)
            prev_aiko_words = None

        novelty_band: str | None = None
        novelty_distance: float | None = None
        detector = getattr(self, "_novelty_detector", None)
        if detector is not None:
            try:
                novelty_band = getattr(detector, "last_band", None)
                novelty_distance = getattr(detector, "last_distance", None)
            except Exception:
                log.debug("misattunement: novelty read failed", exc_info=True)

        agent_settings = self._settings.agent
        try:
            result = misattunement_detector.detect(
                prev_aiko_words=prev_aiko_words,
                this_user_words=user_words,
                novelty_band=novelty_band,
                novelty_distance=novelty_distance,
                cooldown_remaining=cooldown_for_detect,
                shrink_min_prev_words=int(
                    getattr(
                        agent_settings,
                        "misattunement_shrink_min_prev_words",
                        misattunement_detector.DEFAULT_SHRINK_MIN_PREV_WORDS,
                    )
                ),
                shrink_max_user_words=int(
                    getattr(
                        agent_settings,
                        "misattunement_shrink_max_user_words",
                        misattunement_detector.DEFAULT_SHRINK_MAX_USER_WORDS,
                    )
                ),
                pivot_max_user_words=int(
                    getattr(
                        agent_settings,
                        "misattunement_pivot_max_user_words",
                        misattunement_detector.DEFAULT_PIVOT_MAX_USER_WORDS,
                    )
                ),
            )
        except Exception:
            log.debug("misattunement detector raised", exc_info=True)
            return ""

        if result is None:
            return ""

        # Arm cooldown for next N turns and stash diagnostics for the
        # MCP debug tool / per-fire log line.
        cooldown_turns = max(
            0,
            int(getattr(agent_settings, "misattunement_cooldown_turns", 3)),
        )
        self._misattunement_cooldown = cooldown_turns
        self._last_misattunement_trigger = result.trigger
        try:
            self._last_misattunement_fire_turn = (
                self._chat_db.get_message_count(self.session_key)
            )
        except Exception:
            self._last_misattunement_fire_turn = None

        log.info(
            "misattunement-detector: trigger=%s prev_aiko=%d this_user=%d "
            "novelty_band=%s cooldown_set=%d",
            result.trigger,
            result.prev_aiko_words,
            result.this_user_words,
            novelty_band or "-",
            cooldown_turns,
        )

        try:
            return misattunement_detector.render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("misattunement render failed", exc_info=True)
            return ""

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

            return render_inner_life_block(result)
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

            return render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
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
            return store.render_block(
                user_display_name=self.user_display_name,
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
        """Schema v7: terse relationship-axes line (only when notable)."""
        if not bool(getattr(self._settings.agent, "relationship_axes_enabled", True)):
            return ""
        store = getattr(self, "_relationship_axes_store", None)
        if store is None:
            return ""
        try:
            from app.core.relationship.relationship_axes import render_axes_block

            state = store.get(self._user_id)
            return render_axes_block(
                state,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("axes block render failed", exc_info=True)
            return ""

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

