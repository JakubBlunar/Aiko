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


# J8: friendly phrasings for each relationship milestone label (the labels
# come from ``app.core.relationship.relationship._MILESTONES``). ``{name}``
# is filled with the user's display name by ``_render_milestone_block``.
# An unknown label falls back to a humanised default in the renderer.
_MILESTONE_PHRASES: dict[str, str] = {
    "first_hundred_turns": "you and {name} have talked a hundred times now",
    "first_week_together": "it's been a week since you and {name} started talking",
    "first_month_together": "it's been a month since you and {name} first met",
    "hundred_days_together": "it's been a hundred days with {name}",
    "six_months_together": "it's been half a year with {name}",
    "first_year_together": "it's been a whole year with {name}",
}


# J10: shared-moment vibes that read as appreciation-worthy. Excludes
# ``comfort`` / ``vulnerable`` (hard-time moments don't suit a cheerful
# "thanks for that") and ``general`` (too vague to be specific).
_APPRECIATION_VIBES: frozenset[str] = frozenset({
    "warm", "playful", "tender", "proud", "silly",
    "milestone", "gift", "victory", "creative",
})
# kv_meta watermarks for the J10 appreciation cooldown + anti-repeat.
_KV_APPRECIATION_AT = "appreciation.last_surfaced_at"
_KV_APPRECIATION_ANCHOR = "appreciation.last_anchor_id"
# kv_meta watermark for the J9 reciprocal-vulnerability cooldown.
_KV_RECIP_VULN_AT = "reciprocal_vulnerability.last_surfaced_at"


# Brain-orchestration chunk 6: helper for the running-tasks block.
# Pulled out so the rendering rules can be tested in isolation
# (``tests/test_running_tasks_provider.py``) without spinning up a
# full :class:`SessionController`. Pure function — takes a TaskRow,
# returns the formatted bullet line.
def _format_running_task_line(row: Any) -> str:
    """Format one :class:`TaskRow` as a bullet for the running-tasks block.

    Shape: ``- {label} ({status}[, {N}%][, "{last_message}"])``

    * ``label`` is ``row.title`` when set, else ``row.handler_name``.
      Long titles are truncated to 40 chars (with an ellipsis) so a
      title bomb can't blow past the block budget.
    * ``status`` is the raw status string (``running`` /
      ``awaiting_input``). Stays lowercase — Aiko's persona block
      teaches her to read these casually, not formally.
    * ``N%`` only appears when ``row.progress`` is a finite float in
      ``[0, 1]``; clamped + rounded to whole percent.
    * ``last_message`` appears when set, truncated to 60 chars. The
      ``awaiting_input`` cue's question text lives in the parallel
      task-cues block, so we don't repeat it here — the
      ``last_message`` is the handler's progress narration ("scanning
      directory tree", etc.).
    """
    handler = str(getattr(row, "handler_name", "") or "task")
    title = str(getattr(row, "title", "") or "").strip()
    label = title or handler
    if len(label) > 40:
        label = label[:39].rstrip() + "…"
    status = str(getattr(row, "status", "") or "running")
    parts: list[str] = [status]
    progress = getattr(row, "progress", None)
    if progress is not None:
        try:
            p = float(progress)
        except (TypeError, ValueError):
            p = None  # type: ignore[assignment]
        else:
            if p < 0.0:
                p = 0.0
            elif p > 1.0:
                p = 1.0
            parts.append(f"{int(round(p * 100))}%")
    last = getattr(row, "last_message", None)
    if last:
        text = str(last).strip()
        if text:
            if len(text) > 60:
                text = text[:59].rstrip() + "…"
            parts.append(f'"{text}"')
    return f"- {label} ({', '.join(parts)})"


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

    def _render_knowledge_gaps_block(self, user_text: str) -> str:
        """F2: surface the open knowledge gap most relevant to ``user_text``.

        Returns at most one bullet. Empty string when there are no open
        gaps or the best similarity match is below the threshold (so we
        don't surface a totally unrelated wondering on every turn). The
        block ends without a trailing newline so the assembler can stitch
        it next to its siblings.
        """
        if self._question_balance_suppressed():
            return ""
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

    def _render_turning_over_block(self) -> str:
        """K28: surface one recent reflection on the first turn after a gap.

        Sibling of :meth:`_render_absence_curiosity_block` -- both
        ride the typed-gap signal armed by the post-turn engagement
        tracker, but they answer different questions: K14
        ``absence_curiosity`` frames the welcome-back; K28
        ``turning_over`` surfaces what Aiko's been thinking about
        in the meantime. The two stack on the 90 min - 4h overlap.

        One-shot contract: reads ``self._pending_turning_over_seconds``
        (armed by ``post_turn_mixin`` when ``engagement.latency_seconds
        >= memory.turning_over_min_gap_minutes * 60``), clears the
        slot, and runs the picker
        (:func:`app.core.session.inner_life.turning_over.pick_turning_over`).
        Falls silent when:

        * the master switch is off,
        * the slot was never armed (no recent qualifying gap),
        * the threshold double-check fails (defensive against
          settings changes between turns), OR
        * the picker returns ``None`` (no reflection clears the age
          window + topical-similarity gate).

        MCP debug: ``force_turning_over`` arms
        ``_turning_over_force_next`` so the next provider call
        ignores both the pending-slot gate AND the threshold
        double-check. The picker still runs, so a forced bypass
        on an empty reflection corpus still silently expires.
        """
        # K36 one-of guard: reset the shared "a gap cue already fired
        # this assembly" flag at the top of the turn (this provider runs
        # before ``away_activities`` in the T6 cluster). Set it True only
        # when this block actually fires, so ``away_activities`` defers
        # and at most one of the two surfaces per return.
        self._gap_cue_surfaced = False

        if not bool(
            getattr(self._settings.agent, "turning_over_enabled", True)
        ):
            return ""

        # MCP-debug bypass: ``force_next`` ignores the pending-slot
        # gate for this one call. Cleared whether we fire or not.
        force_next = bool(
            getattr(self, "_turning_over_force_next", False)
        )
        if force_next:
            self._turning_over_force_next = False

        seconds = getattr(self, "_pending_turning_over_seconds", None)
        if not force_next and seconds is None:
            return ""
        self._pending_turning_over_seconds = None

        # Defensive threshold double-check: the post-turn arm has
        # already gated on the same threshold, but settings can flip
        # between turns and the slot might carry a stale value.
        if not force_next and seconds is not None:
            try:
                seconds_f = float(seconds)
            except (TypeError, ValueError):
                return ""
            min_gap_s = (
                float(
                    getattr(
                        self._memory_settings,
                        "turning_over_min_gap_minutes",
                        90.0,
                    )
                )
                * 60.0
            )
            if seconds_f < min_gap_s:
                return ""

        memory_store = getattr(self, "_memory_store", None)
        if memory_store is None:
            return ""

        try:
            reflections = list(memory_store.iter_by_kind("reflection"))
        except Exception:
            log.debug(
                "turning-over: reflection snapshot failed", exc_info=True,
            )
            return ""
        if not reflections:
            log.debug("turning-over silent: no reflection rows")
            return ""

        # Active-goal vectors. Empty when no GoalStore is wired or no
        # active goals exist; the picker handles empty pools.
        goal_vecs: list = []
        goal_store = getattr(self, "_goal_store", None)
        if goal_store is not None:
            try:
                goal_vecs = list(goal_store.active_goal_vectors())
            except Exception:
                log.debug(
                    "turning-over: goal vectors raised", exc_info=True,
                )
                goal_vecs = []

        # Recent user-message vectors from the RAG store. Same shape
        # K6 uses to warm its novelty ring buffer.
        msg_vecs: list = []
        rag_store = getattr(self, "_rag_store", None)
        msgs_window = int(
            getattr(
                self._memory_settings,
                "turning_over_recent_msgs_window",
                12,
            )
        )
        if rag_store is not None and msgs_window > 0:
            try:
                msg_vecs = list(
                    rag_store.list_recent_user_vectors(
                        user_id_prefix=getattr(self, "_user_id", "") or "",
                        limit=msgs_window,
                    )
                )
            except Exception:
                log.debug(
                    "turning-over: recent_user_vectors raised", exc_info=True,
                )
                msg_vecs = []

        try:
            from app.core.session.inner_life import turning_over as _to
        except Exception:
            log.debug("turning-over import failed", exc_info=True)
            return ""

        from datetime import datetime, timezone

        memory_settings = self._memory_settings
        try:
            result = _to.pick_turning_over(
                reflections=reflections,
                active_goal_vecs=goal_vecs,
                recent_user_vecs=msg_vecs,
                now=datetime.now(timezone.utc),
                min_age_hours=float(
                    getattr(
                        memory_settings,
                        "turning_over_min_age_hours",
                        _to.DEFAULT_MIN_AGE_HOURS,
                    )
                ),
                max_age_hours=float(
                    getattr(
                        memory_settings,
                        "turning_over_max_age_hours",
                        _to.DEFAULT_MAX_AGE_HOURS,
                    )
                ),
                min_topical_similarity=float(
                    getattr(
                        memory_settings,
                        "turning_over_min_topical_similarity",
                        _to.DEFAULT_MIN_TOPICAL_SIMILARITY,
                    )
                ),
            )
        except Exception:
            log.debug("turning-over picker raised", exc_info=True)
            return ""

        if result is None:
            log.debug(
                "turning-over silent: no candidate cleared the gates "
                "(reflections=%d goals=%d msgs=%d)",
                len(reflections), len(goal_vecs), len(msg_vecs),
            )
            return ""

        # Stash diagnostics for the MCP debug tool.
        self._last_turning_over = result
        # K36 one-of guard: mark that a gap cue surfaced this assembly so
        # ``away_activities`` defers to this (reflection-based) cue.
        self._gap_cue_surfaced = True

        log.info(
            "turning-over fire: memory_id=%d age_h=%.1f topical=%.3f "
            "source=%s dream=%s",
            result.memory_id,
            result.age_hours,
            result.topical_score,
            result.topical_source or "-",
            result.dream,
        )

        try:
            return _to.render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("turning-over render failed", exc_info=True)
            return ""

    def _render_away_activities_block(self) -> str:
        """K36: surface one "while you were away I …" line after a gap.

        Consumer side of the :class:`IdleAwayActivityWorker` producer.
        Same typed-gap arming as K28 ``turning_over`` (via
        ``post_turn_mixin._maybe_arm_away_activities_slot``), but reads
        the worker's kv journal instead of the reflection corpus.

        One-shot contract: reads + clears
        ``self._pending_away_activities_seconds``, re-checks the gap,
        reads the journal ring, and surfaces the newest entry that's
        newer than the ``away_activity.last_surfaced_at`` watermark. The
        watermark advances so the same beat never resurfaces.

        Defers to ``turning_over`` via the shared ``_gap_cue_surfaced``
        flag so at most one of the two gap cues fires per return —
        ``turning_over`` runs first and wins when it has a reflection to
        share; this fills in otherwise.

        MCP debug: ``force_away_activities_surface`` arms
        ``_away_activities_force_next`` to bypass the slot + watermark
        gates (the journal still has to be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "away_activities_enabled", True)
        ):
            return ""

        force_next = bool(
            getattr(self, "_away_activities_force_next", False)
        )
        if force_next:
            self._away_activities_force_next = False

        # One-of guard: turning_over already surfaced a gap cue this
        # assembly. Stand down (unless explicitly forced).
        if not force_next and getattr(self, "_gap_cue_surfaced", False):
            return ""

        seconds = getattr(self, "_pending_away_activities_seconds", None)
        if not force_next and seconds is None:
            return ""
        self._pending_away_activities_seconds = None

        if not force_next and seconds is not None:
            try:
                seconds_f = float(seconds)
            except (TypeError, ValueError):
                return ""
            min_gap_s = (
                float(
                    getattr(
                        self._memory_settings,
                        "away_activities_min_gap_hours",
                        4.0,
                    )
                )
                * 3600.0
            )
            if seconds_f < min_gap_s:
                return ""

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.world.idle_activity_worker import load_journal
        except Exception:
            log.debug("away_activities import failed", exc_info=True)
            return ""

        journal = load_journal(chat_db.kv_get)
        if not journal:
            log.debug("away_activities silent: empty journal")
            return ""

        newest = journal[-1]
        at = str(newest.get("at") or "")
        summary = str(newest.get("summary") or "").strip()
        if not summary:
            return ""

        watermark_key = "away_activity.last_surfaced_at"
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                log.debug("away_activities silent: already surfaced %s", at)
                return ""

        # Advance the watermark so this beat doesn't resurface.
        try:
            chat_db.kv_set(watermark_key, at)
        except Exception:
            log.debug("away_activities watermark write failed", exc_info=True)

        name = self.user_display_name
        # Mark the gap-cue slot consumed so the K34 forward-curiosity
        # provider (which runs after this one) defers — at most one of
        # {turning_over, away_activities, forward_curiosity} surfaces
        # per return.
        self._gap_cue_surfaced = True
        log.info("away-activities fire: at=%s key=%s", at, newest.get("key"))
        return (
            f"While {name} was away, you {summary}. If it fits naturally, "
            "you can mention it in passing — drop it if it doesn't."
        )

    def _render_forward_curiosity_block(self) -> str:
        """K34: surface one "you've been wondering ..." line after a gap.

        Consumer side of the :class:`ForwardCuriosityWorker` producer.
        Same typed-gap arming as K28 ``turning_over`` / K36
        ``away_activities`` (via
        ``post_turn_mixin._maybe_arm_forward_curiosity_slot``), but reads
        the worker's kv question ring.

        One-shot contract: reads + clears
        ``self._pending_forward_curiosity_seconds``, re-checks the gap,
        reads the ring, and surfaces the newest entry that's newer than
        the ``forward_curiosity.last_surfaced_at`` watermark. The
        watermark advances so the same question never resurfaces.

        Runs LAST of the three gap-return cues, so it defers to both
        ``turning_over`` and ``away_activities`` via the shared
        ``_gap_cue_surfaced`` flag — at most one of the three fires per
        return.

        MCP debug: ``force_forward_curiosity_surface`` arms
        ``_forward_curiosity_force_next`` to bypass the slot + watermark
        + one-of gates (the ring still has to be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "forward_curiosity_enabled", True)
        ):
            return ""
        if self._question_balance_suppressed():
            return ""

        force_next = bool(
            getattr(self, "_forward_curiosity_force_next", False)
        )
        if force_next:
            self._forward_curiosity_force_next = False

        # One-of guard: a higher-priority gap cue already surfaced this
        # assembly. Stand down (unless explicitly forced).
        if not force_next and getattr(self, "_gap_cue_surfaced", False):
            return ""

        seconds = getattr(self, "_pending_forward_curiosity_seconds", None)
        if not force_next and seconds is None:
            return ""
        self._pending_forward_curiosity_seconds = None

        if not force_next and seconds is not None:
            try:
                seconds_f = float(seconds)
            except (TypeError, ValueError):
                return ""
            min_gap_s = (
                float(
                    getattr(
                        self._memory_settings,
                        "forward_curiosity_min_gap_hours",
                        4.0,
                    )
                )
                * 3600.0
            )
            if seconds_f < min_gap_s:
                return ""

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.proactive.forward_curiosity_worker import (
                load_questions,
            )
        except Exception:
            log.debug("forward_curiosity import failed", exc_info=True)
            return ""

        ring = load_questions(chat_db.kv_get)
        if not ring:
            log.debug("forward_curiosity silent: empty ring")
            return ""

        newest = ring[-1]
        at = str(newest.get("at") or "")
        question = str(newest.get("question") or "").strip()
        if not question:
            return ""

        watermark_key = "forward_curiosity.last_surfaced_at"
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                log.debug(
                    "forward_curiosity silent: already surfaced %s", at,
                )
                return ""

        # Advance the watermark so this question doesn't resurface.
        try:
            chat_db.kv_set(watermark_key, at)
        except Exception:
            log.debug(
                "forward_curiosity watermark write failed", exc_info=True,
            )

        self._gap_cue_surfaced = True
        log.info("forward-curiosity fire: at=%s source=%s", at, newest.get("source"))
        return (
            f"You've been wondering {question}. If it comes up naturally, "
            "you can ask — drop it if it doesn't fit."
        )

    def _render_follow_up_block(self) -> str:
        """Surface one "you could ask how their plan went" cue.

        Consumer side of the :class:`FollowUpWorker` producer. The worker
        drafts a cue into the ``aiko.follow_up_cues`` kv ring when a
        user-mentioned ``future_plan`` event time has just passed; this
        provider folds the newest unseen cue into the prompt as one
        optional, private hint. Aiko phrases the actual check-in herself
        — the cue is NEVER spoken verbatim (the bug that leaked the
        directive into chat).

        Independent of the gap-return cue family — does NOT read or set
        ``_gap_cue_surfaced``: a concrete, time-anchored "their plan just
        happened" beat is worth a line even alongside a generic gap cue,
        and it must surface on the very next turn after the event passed,
        not only on a long-gap return.

        One-shot via the ``follow_up.last_surfaced_at`` watermark so the
        same cue never resurfaces. MCP debug: ``force_follow_up_surface``
        arms ``_follow_up_force_next`` to bypass the watermark (the ring
        still has to be non-empty).
        """
        if not bool(getattr(self._settings.agent, "follow_up_enabled", True)):
            return ""
        if self._question_balance_suppressed():
            return ""

        force_next = bool(getattr(self, "_follow_up_force_next", False))
        if force_next:
            self._follow_up_force_next = False

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.proactive.follow_up_worker import load_follow_up_cues
        except Exception:
            log.debug("follow_up import failed", exc_info=True)
            return ""

        ring = load_follow_up_cues(chat_db.kv_get)
        if not ring:
            return ""

        newest = ring[-1]
        at = str(newest.get("at") or "")
        plan = str(newest.get("plan") or "").strip()
        if not plan:
            return ""

        watermark_key = "follow_up.last_surfaced_at"
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                return ""

        # Advance the watermark so this cue doesn't resurface.
        try:
            chat_db.kv_set(watermark_key, at)
        except Exception:
            log.debug("follow_up watermark write failed", exc_info=True)

        clock = str(newest.get("clock") or "").strip()
        question = str(newest.get("question") or "").strip()
        when = f" (around {clock})" if clock else ""
        line = (
            f"Earlier{when} {plan} — that time has passed now. If it fits "
            "the flow, you can gently ask how it went; no need to open with "
            "it, and let it go if the moment isn't right."
        )
        if question:
            line += f' Something like: "{question}"'
        log.info(
            "follow-up cue fire: at=%s source=%s", at, newest.get("source_id"),
        )
        return line

    def _render_promise_followthrough_block(self) -> str:
        """K43: surface one "close the loop on what you said you'd do" cue.

        Consumer side of the :class:`PromiseFollowthroughWorker`
        producer. The worker arms a one-shot pending payload in kv_meta
        (``promise_followthrough.pending``) during a quiet window; this
        provider renders it once and clears the slot. Persisting the
        slot in kv (not on the controller) means an armed cue survives
        an app restart instead of orphaning a ``surfaced`` promise row.

        The cue covers both outcomes on purpose — share what you found
        *or* own that you haven't gotten to it — because the worker
        can't know whether Aiko actually has anything. If the promise
        was fulfilled or deleted between arming and rendering, the cue
        drops silently (slot still cleared).

        Independent of the gap-return cue family — does NOT touch
        ``_gap_cue_surfaced``; an owed loop-close is worth a line even
        mid-session.
        """
        if not bool(
            getattr(
                self._settings.agent, "promise_followthrough_enabled", True,
            )
        ):
            return ""
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""
        try:
            from app.core.memory import promise_lifecycle as lifecycle
            from app.core.proactive.promise_followthrough_worker import (
                clear_pending,
                load_pending,
            )
        except Exception:
            log.debug("promise_followthrough import failed", exc_info=True)
            return ""

        pending = load_pending(chat_db.kv_get)
        if pending is None:
            return ""
        # One-shot: consume the slot whatever happens next.
        clear_pending(chat_db.kv_set)

        what = str(pending.get("what") or "").strip()
        if not what:
            return ""

        # Re-validate against the live row: a promise fulfilled (post-turn
        # resolution / finished task) or deleted between arming and now
        # no longer owes anything.
        memory_store = getattr(self, "_memory_store", None)
        try:
            mem = (
                memory_store.get(int(pending.get("memory_id") or 0))
                if memory_store is not None
                else None
            )
        except Exception:
            mem = None
        if mem is None or lifecycle.promise_status(mem) not in (
            lifecycle.ACTIVE_STATUSES
        ):
            log.debug(
                "promise_followthrough silent: row gone or resolved (id=%s)",
                pending.get("memory_id"),
            )
            return ""

        try:
            age_text = lifecycle.humanize_age(
                float(pending.get("age_hours") or 0.0),
            )
        except (TypeError, ValueError):
            age_text = "a while ago"
        log.info(
            "promise-followthrough fire: memory_id=%s age=%s what=%r",
            pending.get("memory_id"),
            age_text,
            what[:80],
        )
        return (
            f"Heads-up: {age_text} you told {self.user_display_name} you'd "
            f"{what} — you haven't closed that loop. If it fits this turn, "
            "mention what you found, or own that you haven't gotten to it "
            "yet. One casual line, not a production — and don't pretend you "
            "did it if you didn't."
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

    def _render_mood_inertia_block(self) -> str:
        """K45: surface a one-shot mood-inertia cue.

        Same one-shot contract as :meth:`_render_rupture_block` — the
        post-turn detector (:meth:`PostTurnMixin._maybe_arm_mood_inertia`)
        stashes a rendered cue on the controller when the fresh reaction
        tag strongly outran the smoothed felt state; we surface it once
        and clear the slot. The MCP ``force_mood_inertia`` flag bypasses
        the detector with a synthetic cue built from the live state.
        """
        if not bool(
            getattr(self._settings.agent, "mood_inertia_enabled", True)
        ):
            return ""
        if getattr(self, "_mood_inertia_force", False):
            self._mood_inertia_force = False
            try:
                from app.core.affect import mood_inertia

                state = self._affect_store.get(self._user_id)
                ring = list(getattr(self, "_mood_inertia_reactions", []) or [])
                reaction = ring[-1] if ring else "excited"
                forced = mood_inertia.InertiaResult(
                    mismatch=1.0, raw_mismatch=1.0,
                    whiplash=False, band="strong",
                )
                return mood_inertia.render_cue(
                    forced, reaction, state.valence, state.arousal,
                )
            except Exception:
                log.debug("forced mood-inertia render failed", exc_info=True)
                return ""
        cue = getattr(self, "_pending_mood_inertia", None)
        if not cue:
            return ""
        self._pending_mood_inertia = None
        return str(cue)

    def _render_self_correction_block(self) -> str:
        """K38: surface a one-shot self-correction cue.

        The post-turn detector
        (:meth:`PostTurnMixin._maybe_arm_self_correction`) stashes a
        :class:`SelfCorrectionHit` on the controller when Aiko's last
        reply contradicted one of her own high-confidence
        ``fact`` / ``preference`` memories. We render it once and clear
        the slot so she owns the slip naturally on this turn. Independent
        of the gap-return cue family -- does NOT read or set
        ``_gap_cue_surfaced``. Survives ``aggressive=True`` (an owed
        correction must still land).
        """
        if not bool(
            getattr(self._settings.agent, "self_correction_enabled", True)
        ):
            return ""
        hit = getattr(self, "_pending_self_correction", None)
        if hit is None:
            return ""
        self._pending_self_correction = None
        try:
            snippet = (hit.reply_snippet or "").strip()
            memory = (hit.memory_content or "").strip()
            if not snippet or not memory:
                return ""
            return (
                f'Heads-up: a moment ago you said "{snippet}", but you\'d '
                f"noted {memory}. If it still fits, own the correction "
                "naturally and once -- 'oh wait, I think I had that "
                "backwards' -- never a grovel, and drop it if it no longer "
                "matters."
            )
        except Exception:
            log.debug("self-correction render failed", exc_info=True)
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
        if not bool(
            getattr(self._settings.agent, "opinion_injection_enabled", True)
        ):
            return ""
        try:
            from app.core.affect import opinion_injection_detector
        except Exception:
            log.debug("opinion-injection import failed", exc_info=True)
            return ""

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

        # Optional LLM gate for the borderline path. ``llm_gate=None``
        # cleanly skips the LLM branch and degrades to Path C
        # (definite-only). The detector itself owns the heuristic
        # call; this lambda only fires when classify_pair returns
        # ``borderline``.
        llm_gate = None
        rate_limiter = getattr(self, "_opinion_injection_rate_limiter", None)
        ollama_client = getattr(self, "_ollama", None)
        if (
            rate_limiter is not None
            and ollama_client is not None
            and not bool(
                getattr(
                    self._settings.agent,
                    "opinion_injection_require_definite",
                    False,
                )
            )
        ):
            def _gate(user_t: str, stance_t: str) -> str | None:
                try:
                    if not rate_limiter.allow():
                        return None
                except Exception:
                    log.debug(
                        "opinion-injection: rate_limiter raised", exc_info=True
                    )
                    return None
                return self._opinion_injection_llm_verdict(
                    user_t, stance_t,
                )

            llm_gate = _gate

        memory_settings = self._memory_settings
        agent_settings = self._settings.agent
        try:
            result = opinion_injection_detector.detect(
                user_text or "",
                user_vec=user_vec,
                self_memories=self_memories,
                llm_gate=llm_gate,
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
                require_definite=bool(
                    getattr(
                        agent_settings,
                        "opinion_injection_require_definite",
                        False,
                    )
                ),
            )
        except Exception:
            log.debug("opinion-injection detector raised", exc_info=True)
            return ""

        if result is None:
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
            return opinion_injection_detector.render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("opinion-injection render failed", exc_info=True)
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
                recent_rows = self._chat_db.get_messages(
                    self.session_key, limit=max(window * 4, 20),
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
                # Wall-clock cooldown between offers.
                cooldown_h = float(
                    getattr(agent, "tease_collect_cooldown_hours", 12.0)
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
                rows = self._chat_db.get_messages(
                    self.session_key, limit=max(window * 4, 20),
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

        # Long cooldown — this beat is rare by design.
        if not force:
            cooldown_h = float(
                getattr(self._settings.agent, "appreciation_cooldown_hours", 72.0)
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

