"""Helper methods for :class:`app.core.session.prompt_assembler.PromptAssembler`.

Split out to keep the assembler file under the size budget. These methods
are composed onto PromptAssembler via inheritance, so they run with full
access to ``self``. Patch ``app.core.session.prompt_assembler_helpers_mixin``
for any module-level symbol they look up.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from app.core.conversation import cue_register
from app.core.infra import timephrase as _timephrase
from app.core.infra.chat_database import ChatDatabase, MessageRow, SummaryRow
from app.llm.token_utils import estimate_messages_tokens, estimate_tokens

if TYPE_CHECKING:
    from app.core.memory.memory_retriever import MemoryRetriever
    from app.core.rag.rag_retriever import RagRetriever

log = logging.getLogger("app.prompt_assembler")

from app.core.session.prompt_support import (
    DEFAULT_PERSONA_PATH,
    DEFAULT_SELF_IMAGE_PATH,
    _SAFETY_TOKENS,
    _MESSAGE_OVERHEAD,
    build_speech_grammar_addendum,
    _SPEECH_GRAMMAR_ADDENDUM,
    _TOUCH_GRAMMAR_ADDENDUM,
    _build_overlay_grammar_addendum,
    _build_outfit_grammar_addendum,
    _build_motion_grammar_addendum,
    _safe_provider,
    _timed_phase,
    PromptTelemetry,
    _StaticSlices,
)


class PromptAssemblerHelpersMixin:
    """Config setters, persona/self-image loaders, slice-cache machinery,
    history fitting, and the small per-turn block renderers."""

    def set_memory_retriever(self, retriever: "MemoryRetriever | None") -> None:
        self._memory_retriever = retriever

    def set_rag_retriever(self, retriever: "RagRetriever | None") -> None:
        self._rag_retriever = retriever

    def set_rag_prefetch_lookup(
        self,
        lookup: Callable[[str], str | None] | None,
    ) -> None:
        """Optional Phase-1b cache: if it returns a non-empty block, we'll
        skip the live retrieval and reuse the speculative pre-fetch."""
        self._rag_prefetch_lookup = lookup

    def set_user_display_name_provider(
        self,
        provider: Callable[[], str] | None,
    ) -> None:
        """Wire the user-display-name resolver.

        Called lazily by the assembler each time a prompt block needs the
        name so a rename via ``identity_changed`` takes effect on the
        very next turn without a re-init.
        """
        self._user_display_name_provider = provider

    def _resolve_user_display_name(self) -> str:
        provider = self._user_display_name_provider
        if provider is None:
            return "the user"
        try:
            name = (provider() or "").strip()
        except Exception:
            return "the user"
        return name or "the user"

    def set_pinned_self_memories_provider(
        self,
        provider: Callable[[], list[str]] | None,
    ) -> None:
        """Phase 2d: callable returning Aiko's top self-memories as bullets.

        Folded into the self-image block on every prompt build (cheap mirror
        read; ms-level). Setting it to ``None`` disables the bullets.
        """
        self._pinned_self_memories_provider = provider

    def set_inner_life_providers(
        self,
        *,
        affect: Callable[[], str] | None = None,
        vitality: Callable[[], str] | None = None,
        circadian: Callable[[], str] | None = None,
        day_color: Callable[[], str] | None = None,
        profile: Callable[[], str] | None = None,
        user_state: Callable[[], str] | None = None,
        relationship: Callable[[], str] | None = None,
        arc: Callable[[], str] | None = None,
        narrative: Callable[[], str] | None = None,
        agenda: Callable[[], str] | None = None,
        goals: Callable[[], str] | None = None,
        interest_map: Callable[[], str] | None = None,
        vocal_tone: Callable[[], str] | None = None,
        catchphrase: Callable[[], str] | None = None,
        petname: Callable[[], str] | None = None,
        ambient_noise: Callable[[], str] | None = None,
        avatar_capabilities: Callable[[], dict[str, bool] | None] | None = None,
        pajama: Callable[[], str] | None = None,
        motion_names: Callable[[], list[str]] | None = None,
        world: Callable[[], str] | None = None,
        activity: Callable[[], str] | None = None,
        weather: Callable[[], str] | None = None,
        hobby: Callable[[], str] | None = None,
        anniversary: Callable[[], str] | None = None,
        milestone: Callable[[], str] | None = None,
        axes: Callable[[], str] | None = None,
        knowledge_gaps: Callable[[str], str] | None = None,
        knowledge_gap_notice: Callable[[str], str] | None = None,
        associative_wander: Callable[[str], str] | None = None,
        long_arc_callback: Callable[[str], str] | None = None,
        interest_drift: Callable[[str], str] | None = None,
        dormant_interest: Callable[[], str] | None = None,
        curiosity_gradient: Callable[[str], str] | None = None,
        topic_temperature: Callable[[str], str] | None = None,
        topic_confidence: Callable[[str], str] | None = None,
        earned_familiarity: Callable[[str], str] | None = None,
        user_expertise: Callable[[str], str] | None = None,
        knowledge_grounding: Callable[[str], str] | None = None,
        belief_gaps: Callable[[], str] | None = None,
        clarification: Callable[[], str] | None = None,
        calibration: Callable[[], str] | None = None,
        sensory_anchor: Callable[[], str] | None = None,
        rupture: Callable[[], str] | None = None,
        mood_inertia: Callable[[], str] | None = None,
        mood_drift: Callable[[], str] | None = None,
        self_correction: Callable[[], str] | None = None,
        promise_followthrough: Callable[[], str] | None = None,
        misattunement: Callable[[str], str] | None = None,
        implicit_need: Callable[[str], str] | None = None,
        opinion_injection: Callable[[str], str] | None = None,
        stance_persistence: Callable[[str], str] | None = None,
        absence_curiosity: Callable[[], str] | None = None,
        reconnection: Callable[[], str] | None = None,
        session_clock: Callable[[], str] | None = None,
        appreciation: Callable[[], str] | None = None,
        reciprocal_vulnerability: Callable[[str], str] | None = None,
        turning_over: Callable[[], str] | None = None,
        sleep_return: Callable[[], str] | None = None,
        away_activities: Callable[[], str] | None = None,
        forward_curiosity: Callable[[], str] | None = None,
        follow_up: Callable[[], str] | None = None,
        growth_witness: Callable[[], str] | None = None,
        self_callback: Callable[[], str] | None = None,
        wellbeing_concern: Callable[[], str] | None = None,
        shared_ritual: Callable[[], str] | None = None,
        upcoming_horizon: Callable[[], str] | None = None,
        mood_shell: Callable[[], str] | None = None,
        intimacy_pacing: Callable[[], str] | None = None,
        novelty: Callable[[str], str] | None = None,
        stagnation: Callable[[str], str] | None = None,
        style_pattern: Callable[[], str] | None = None,
        question_balance: Callable[[], str] | None = None,
        tease_rhythm: Callable[[], str] | None = None,
        style_signal: Callable[[], str] | None = None,
        self_noticing: Callable[[], str] | None = None,
        vulnerability_budget: Callable[[], str] | None = None,
        curiosity_seeds: Callable[[], str] | None = None,
        idle_seeds: Callable[[], str] | None = None,
        wants: Callable[[], str] | None = None,
        initiative: Callable[[str], str] | None = None,
        thread_ownership: Callable[[str], str] | None = None,
        topic_appetite: Callable[[], str] | None = None,
        emotion_episode: Callable[[str], str] | None = None,
        tease_ledger: Callable[[], str] | None = None,
        grounding_line: Callable[[], str] | None = None,
        user_reactions: Callable[[], str] | None = None,
        touch_state: Callable[[], str] | None = None,
        attachments: Callable[[], str] | None = None,
        task_cues: Callable[[], str] | None = None,
        running_tasks: Callable[[], str] | None = None,
    ) -> None:
        """Register optional inner-life block providers.

        Each provider returns a short, prompt-ready string (or empty to
        skip). Workers register themselves via this hook so the assembler
        doesn't need to know about every concrete table.
        """
        if affect is not None:
            self._affect_provider = affect
        if vitality is not None:
            self._vitality_provider = vitality
        if circadian is not None:
            self._circadian_provider = circadian
        if day_color is not None:
            self._day_color_provider = day_color
        if profile is not None:
            self._profile_provider = profile
        if user_state is not None:
            self._user_state_provider = user_state
        if relationship is not None:
            self._relationship_provider = relationship
        if arc is not None:
            self._arc_provider = arc
        if narrative is not None:
            self._narrative_provider = narrative
        if agenda is not None:
            self._agenda_provider = agenda
        if goals is not None:
            self._goals_provider = goals
        if interest_map is not None:
            self._interest_map_provider = interest_map
        if vocal_tone is not None:
            self._vocal_tone_provider = vocal_tone
        if catchphrase is not None:
            self._catchphrase_provider = catchphrase
        if petname is not None:
            self._petname_provider = petname
        if ambient_noise is not None:
            self._ambient_noise_provider = ambient_noise
        if avatar_capabilities is not None:
            self._avatar_capabilities_provider = avatar_capabilities
        if pajama is not None:
            self._pajama_provider = pajama
        if motion_names is not None:
            self._motion_names_provider = motion_names
        if world is not None:
            self._world_provider = world
        if activity is not None:
            self._activity_provider = activity
        if weather is not None:
            self._weather_block_provider = weather
        if hobby is not None:
            self._hobby_provider = hobby
        if anniversary is not None:
            self._anniversary_provider = anniversary
        if milestone is not None:
            self._milestone_provider = milestone
        if axes is not None:
            self._axes_provider = axes
        if knowledge_gaps is not None:
            self._knowledge_gaps_provider = knowledge_gaps
        if knowledge_gap_notice is not None:
            self._knowledge_gap_notice_provider = knowledge_gap_notice
        if associative_wander is not None:
            self._associative_wander_provider = associative_wander
        if long_arc_callback is not None:
            self._long_arc_callback_provider = long_arc_callback
        if interest_drift is not None:
            self._interest_drift_provider = interest_drift
        if dormant_interest is not None:
            self._dormant_interest_provider = dormant_interest
        if curiosity_gradient is not None:
            self._curiosity_gradient_provider = curiosity_gradient
        if topic_temperature is not None:
            self._topic_temperature_provider = topic_temperature
        if topic_confidence is not None:
            self._topic_confidence_provider = topic_confidence
        if earned_familiarity is not None:
            self._earned_familiarity_provider = earned_familiarity
        if user_expertise is not None:
            self._user_expertise_provider = user_expertise
        if knowledge_grounding is not None:
            self._knowledge_grounding_provider = knowledge_grounding
        if belief_gaps is not None:
            self._belief_gaps_provider = belief_gaps
        if clarification is not None:
            self._clarification_provider = clarification
        if calibration is not None:
            self._calibration_provider = calibration
        if sensory_anchor is not None:
            self._sensory_anchor_provider = sensory_anchor
        if rupture is not None:
            self._rupture_provider = rupture
        if mood_inertia is not None:
            self._mood_inertia_provider = mood_inertia
        if mood_drift is not None:
            self._mood_drift_provider = mood_drift
        if self_correction is not None:
            self._self_correction_provider = self_correction
        if promise_followthrough is not None:
            self._promise_followthrough_provider = promise_followthrough
        if misattunement is not None:
            self._misattunement_provider = misattunement
        if implicit_need is not None:
            self._implicit_need_provider = implicit_need
        if opinion_injection is not None:
            self._opinion_injection_provider = opinion_injection
        if stance_persistence is not None:
            self._stance_persistence_provider = stance_persistence
        if absence_curiosity is not None:
            self._absence_curiosity_provider = absence_curiosity
        if reconnection is not None:
            self._reconnection_provider = reconnection
        if session_clock is not None:
            self._session_clock_provider = session_clock
        if appreciation is not None:
            self._appreciation_provider = appreciation
        if reciprocal_vulnerability is not None:
            self._reciprocal_vulnerability_provider = reciprocal_vulnerability
        if turning_over is not None:
            self._turning_over_provider = turning_over
        if sleep_return is not None:
            self._sleep_return_provider = sleep_return
        if away_activities is not None:
            self._away_activities_provider = away_activities
        if forward_curiosity is not None:
            self._forward_curiosity_provider = forward_curiosity
        if follow_up is not None:
            self._follow_up_provider = follow_up
        if growth_witness is not None:
            self._growth_witness_provider = growth_witness
        if self_callback is not None:
            self._self_callback_provider = self_callback
        if wellbeing_concern is not None:
            self._wellbeing_concern_provider = wellbeing_concern
        if shared_ritual is not None:
            self._shared_ritual_provider = shared_ritual
        if upcoming_horizon is not None:
            self._upcoming_horizon_provider = upcoming_horizon
        if mood_shell is not None:
            self._mood_shell_provider = mood_shell
        if intimacy_pacing is not None:
            self._intimacy_pacing_provider = intimacy_pacing
        if novelty is not None:
            self._novelty_provider = novelty
        if stagnation is not None:
            self._stagnation_provider = stagnation
        if style_pattern is not None:
            self._style_pattern_provider = style_pattern
        if question_balance is not None:
            self._question_balance_provider = question_balance
        if tease_rhythm is not None:
            self._tease_rhythm_provider = tease_rhythm
        if style_signal is not None:
            self._style_signal_provider = style_signal
        if self_noticing is not None:
            self._self_noticing_provider = self_noticing
        if vulnerability_budget is not None:
            self._vulnerability_budget_provider = vulnerability_budget
        if curiosity_seeds is not None:
            self._curiosity_seeds_provider = curiosity_seeds
        if idle_seeds is not None:
            self._idle_seeds_provider = idle_seeds
        if wants is not None:
            self._wants_provider = wants
        if initiative is not None:
            self._initiative_provider = initiative
        if thread_ownership is not None:
            self._thread_ownership_provider = thread_ownership
        if topic_appetite is not None:
            self._topic_appetite_provider = topic_appetite
        if emotion_episode is not None:
            self._emotion_episode_provider = emotion_episode
        if tease_ledger is not None:
            self._tease_ledger_provider = tease_ledger
        if grounding_line is not None:
            self._grounding_line_provider = grounding_line
        if user_reactions is not None:
            self._user_reactions_provider = user_reactions
        if touch_state is not None:
            self._touch_state_provider = touch_state
        if attachments is not None:
            self._attachments_provider = attachments
        if task_cues is not None:
            self._task_cues_provider = task_cues
        if running_tasks is not None:
            self._running_tasks_provider = running_tasks

    def set_last_reaction(self, reaction: str | None) -> None:
        if not reaction:
            self._last_reaction = None
            return
        cleaned = str(reaction).strip().lower()
        if cleaned in ("", "neutral"):
            self._last_reaction = None
        else:
            self._last_reaction = cleaned

    def set_grounding_line_mode(self, mode: str) -> None:
        """K16: configure how the unified grounding line interacts with
        the granular ambient blocks.

        Accepts ``"off"`` / ``"replace"`` / ``"split"`` (case-
        insensitive); anything else clamps to ``"off"`` so a typo
        upstream never wedges the prompt. See
        :attr:`AgentSettings.grounding_line_mode` for the full mode
        table and suppression matrix. Idempotent — call again on
        settings reload to flip modes live.
        """
        cleaned = str(mode or "").strip().lower()
        if cleaned not in ("off", "replace", "split"):
            cleaned = "off"
        self._grounding_line_mode = cleaned

    def reload_persona(self) -> None:
        """Force re-read on next ``build()`` call."""
        self._persona_cache = None

    @property
    def last_slice_cache_event(self) -> str:
        """``"hit"`` / ``"miss"`` / ``"skip"`` from the most recent build.

        ``skip`` means no static-slice cache was consulted (e.g., aggressive
        rebuild after compaction). The value is set as a side effect of
        :meth:`assemble_with_budget`; callers should read it immediately
        after the call.
        """
        return self._last_slice_cache_event

    def reset_slice_cache(self, session_key: str | None = None) -> None:
        """Drop the listening-window slice cache for ``session_key``.

        Called by :class:`SessionController` whenever long-lived state
        the slices depend on changes (e.g., persona reload, session
        switch, model change). Pass ``None`` to clear all sessions.
        """
        if session_key is None:
            self._slice_cache.clear()
        else:
            self._slice_cache.pop(session_key, None)

    def prebuild_static_slices(
        self, session_key: str, *, aggressive: bool = False,
    ) -> _StaticSlices:
        """Build everything the prompt needs except the user message and RAG.

        Safe to call from any thread. Result is stashed in a per-session
        cache; :meth:`assemble_with_budget` will reuse it if the cache key
        still matches at commit. Cheap (5-20 ms total: persona/self-image
        disk reads, two SQLite queries, ~8 inner-life provider callbacks)
        and idempotent — calling it more than once during the same phrase
        just refreshes the cache.
        """
        slices = self._build_static_slices(session_key, aggressive=aggressive)
        self._slice_cache[session_key] = slices
        return slices

    def _build_static_slices(
        self, session_key: str, *, aggressive: bool,
    ) -> _StaticSlices:
        return self._build_static_slices_with_history(
            session_key,
            aggressive=aggressive,
            history_msgs=None,
            summary=None,
            already_summarized=None,
        )

    def _build_static_slices_with_history(
        self,
        session_key: str,
        *,
        aggressive: bool,
        history_msgs: list[MessageRow] | None,
        summary: SummaryRow | None,
        already_summarized: int | None,
    ) -> _StaticSlices:
        """Static slice builder with optional pre-fetched history/summary.

        ``assemble_with_budget``'s cache-miss path already paid for the
        SQLite reads to compute the live cache key; it passes them in
        here so we don't double-read. Pass ``None`` for any value to
        fetch fresh.
        """
        persona = self._load_persona()
        self_image_block = self._load_self_image()
        if summary is None and already_summarized is None:
            summary = self._db.get_latest_summary(session_key)
            already_summarized = (
                int(summary.messages_summarized)
                if (summary and summary.summary.strip())
                else 0
            )
        elif already_summarized is None:
            already_summarized = (
                int(summary.messages_summarized)
                if (summary and summary.summary.strip())
                else 0
            )
        recent_window = (
            self._recent_window if not aggressive else max(2, self._recent_window // 2)
        )
        if history_msgs is None:
            history_msgs = self._db.get_messages(session_key, limit=recent_window)
            if already_summarized > 0:
                history_msgs = [
                    row for row in history_msgs
                    if getattr(row, "id", 0) and int(row.id) > already_summarized
                ]
        thread_note = self._thread_note_block(session_key)
        ambient = self._ambient_block()
        mood_hint = self._mood_carryover_hint()
        circadian_block = _safe_provider(self._circadian_provider)
        affect_block = _safe_provider(self._affect_provider)
        profile_block = _safe_provider(self._profile_provider)
        user_state_block = _safe_provider(self._user_state_provider)
        relationship_block = _safe_provider(self._relationship_provider)
        arc_block = _safe_provider(self._arc_provider)
        agenda_block = "" if aggressive else _safe_provider(self._agenda_provider)
        goals_block = "" if aggressive else _safe_provider(self._goals_provider)
        interest_map_block = (
            "" if aggressive else _safe_provider(self._interest_map_provider)
        )
        cache_key = self._compute_static_cache_key(
            session_key, history_msgs, recent_window, aggressive,
        )
        return _StaticSlices(
            cache_key=cache_key,
            persona=persona,
            self_image_block=self_image_block,
            summary_row=summary,
            already_summarized=already_summarized,
            thread_note=thread_note,
            history_msgs=history_msgs,
            ambient=ambient,
            mood_hint=mood_hint,
            affect_block=affect_block,
            circadian_block=circadian_block,
            profile_block=profile_block,
            user_state_block=user_state_block,
            relationship_block=relationship_block,
            arc_block=arc_block,
            agenda_block=agenda_block,
            goals_block=goals_block,
            interest_map_block=interest_map_block,
            built_at=time.monotonic(),
        )

    def _fast_slice_signature(
        self,
        session_key: str,
        recent_window: int,
        aggressive: bool,
    ) -> tuple:
        """P3: cheap slice-cache invalidator.

        Combines the DB head signature (``MAX(id)`` / ``COUNT(*)`` /
        latest-summary watermark — two scalar queries) with the same
        persona/self-image mtime, last-reaction, window and aggressive
        inputs the full ``_compute_static_cache_key`` uses. A conservative
        superset: when this tuple is unchanged the full key is guaranteed
        unchanged too, so the hit path can skip ``get_messages`` +
        ``get_latest_summary``.
        """
        head = self._db.get_history_head(session_key)
        try:
            persona_mtime = self._persona_path.stat().st_mtime
        except OSError:
            persona_mtime = 0.0
        self_image_mtime = 0.0
        if self._self_image_path is not None:
            try:
                self_image_mtime = self._self_image_path.stat().st_mtime
            except OSError:
                self_image_mtime = 0.0
        return (
            session_key,
            head[0],
            head[1],
            head[2],
            persona_mtime,
            self_image_mtime,
            self._last_reaction or "",
            recent_window,
            bool(aggressive),
        )

    def _compute_static_cache_key(
        self,
        session_key: str,
        history_msgs: list[MessageRow],
        recent_window: int,
        aggressive: bool,
    ) -> tuple:
        try:
            persona_mtime = self._persona_path.stat().st_mtime
        except OSError:
            persona_mtime = 0.0
        self_image_mtime = 0.0
        if self._self_image_path is not None:
            try:
                self_image_mtime = self._self_image_path.stat().st_mtime
            except OSError:
                self_image_mtime = 0.0
        history_max_id = 0
        if history_msgs:
            history_max_id = max(int(getattr(m, "id", 0) or 0) for m in history_msgs)
        return (
            session_key,
            history_max_id,
            len(history_msgs),
            persona_mtime,
            self_image_mtime,
            self._last_reaction or "",
            recent_window,
            bool(aggressive),
        )

    def _mood_carryover_hint(self) -> str:
        """Mention Aiko's most recent emotional reaction so she keeps a
        through-line across turns. Skip when neutral / unset.
        """
        reaction = self._last_reaction
        if not reaction:
            return ""
        return (
            f"Your last reaction was '{reaction}'. Carry that mood naturally "
            f"into this turn unless the new context obviously calls for a "
            f"different one."
        )

    def _thread_note_block(self, session_key: str) -> str:
        """K21 fresh-eyes note: Aiko's recently-refreshed read of where
        this thread stands now. Complements (does not replace) the
        rolling summary; rendered as its own small T2 block. Empty until
        the :class:`ThreadResummaryWorker` has drafted one for the
        session.
        """
        try:
            row = self._db.get_thread_note(session_key)
        except Exception:
            return ""
        if row is None:
            return ""
        note = (row.note or "").strip()
        if not note:
            return ""
        return "Where this conversation stands now:\n" + note

    @staticmethod
    def _ambient_block() -> str:
        """Light "what time is it" hint so Aiko can naturally pick up on the
        time of day without us having to tell her every turn. Phrased as a
        cue, not a directive -- the persona is responsible for tone.
        """
        try:
            now = datetime.now().astimezone()
        except Exception:
            return ""
        hour = now.hour
        if hour < 5:
            pod = "late night"
        elif hour < 9:
            pod = "early morning"
        elif hour < 12:
            pod = "morning"
        elif hour < 14:
            pod = "midday"
        elif hour < 18:
            pod = "afternoon"
        elif hour < 22:
            pod = "evening"
        else:
            pod = "late night"
        # Use platform-safe format strings (Windows %-d / Unix %-d differ).
        # K-time6: include the year + a compact ISO date so the anchor is
        # unambiguous for the residual cases where the model still does its
        # own arithmetic (cross-year spans, "how long ago was X").
        date_part = now.strftime("%A, %B %d, %Y").replace(" 0", " ")
        time_part = now.strftime("%I:%M %p").lstrip("0")
        iso_date = now.strftime("%Y-%m-%d")
        return (
            f"Right now it's {date_part}, {pod} ({time_part}) [{iso_date}]. "
            f"Use this naturally if it's relevant; don't announce the time "
            f"unprompted."
        )

    def build_eval_messages(
        self,
        user_text: str,
        *,
        full_context: bool,
        session_key: str = "persona_regression",
        context_window: int = 8192,
        response_budget: int = 512,
    ) -> list[dict[str, Any]]:
        """K10: build the message list for one persona-regression golden turn.

        ``full_context=False`` (minimal scope): persona sheet + the
        always-on speech + touch grammar addenda + the golden user line.
        Deterministic-ish; isolates persona-sheet drift from the rest of
        the prompt.

        ``full_context=True`` (full scope): the live assembled system
        prompt (persona + inner-life blocks + RAG retrieved for the
        golden line), with history discarded and the golden line appended
        as the user turn. Catches memory contamination + prompt rot.
        Note: this drives the real RAG path and perturbs ``_slice_cache``
        / ``RagRetriever.last_surfaced_memory_ids``, so callers must run
        it only while the session is idle.
        """
        if full_context:
            messages, _telemetry = self.assemble_with_budget(
                session_key,
                user_text,
                context_window=context_window,
                response_budget=response_budget,
            )
            system_parts = [
                m
                for m in messages
                if isinstance(m, dict) and m.get("role") == "system"
            ]
            return [*system_parts, {"role": "user", "content": user_text}]

        persona = self._load_persona()
        grammar = build_speech_grammar_addendum(self._resolve_user_display_name())
        system_text = "\n\n".join(
            part for part in (persona, grammar, _TOUCH_GRAMMAR_ADDENDUM) if part
        )
        return [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_text},
        ]

    def _load_persona(self) -> str:
        path = self._persona_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        if self._persona_cache is not None and self._persona_cache[0] == mtime:
            raw = self._persona_cache[1]
        else:
            try:
                raw = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                log.warning("persona file %s unreadable: %s", path, exc)
                raw = ""
            self._persona_cache = (mtime, raw)
        if not raw:
            return ""
        # Phase 4d: render the {user_name} placeholder per-call so a rename
        # via onboarding takes effect without invalidating the mtime cache.
        # If the persona file has stray ``{`` braces (e.g. literal JSON) the
        # ``.format()`` call would raise -- fall back to the raw text.
        try:
            return raw.format(user_name=self._resolve_user_display_name())
        except Exception:
            log.debug(
                "persona templating failed; falling back to raw text",
                exc_info=True,
            )
            return raw

    def _load_self_image(self) -> str:
        """Compose the self-image block (Phase 2d).

        Two pieces, joined with a blank line:
          - prose paragraph from ``data/persona/self_image.txt`` (rebuilt
            once per UTC day by SelfImageWorker; mtime-cached here)
          - "Self-memories you hold:" bullets from the pinned provider

        Either piece may be empty; the result is empty only when both are.
        """
        prose = self._load_self_image_file()
        pinned = self._render_pinned_self_memories_block()
        parts = [p for p in (prose, pinned) if p]
        return "\n\n".join(parts)

    def _load_self_image_file(self) -> str:
        """Read + mtime-cache the prose self-image file."""
        path = self._self_image_path
        if path is None:
            return ""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        if self._self_image_cache is not None and self._self_image_cache[0] == mtime:
            return self._self_image_cache[1]
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            text = ""
        if text:
            text = "Lately:\n" + text
        self._self_image_cache = (mtime, text)
        return text

    def _render_pinned_self_memories_block(self) -> str:
        """Format up to N pinned self-memories as a bulleted block."""
        provider = self._pinned_self_memories_provider
        if provider is None:
            return ""
        try:
            items = provider() or []
        except Exception:
            log.debug("pinned-self-memory provider raised", exc_info=True)
            return ""
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in items:
            txt = (item or "").strip()
            key = txt.lower()
            if not txt or key in seen:
                continue
            seen.add(key)
            cleaned.append(txt)
        if not cleaned:
            return ""
        return "Self-memories you hold:\n" + "\n".join(f"- {c}" for c in cleaned)

    @staticmethod
    def _format_age(created_at_iso: str, now: datetime) -> str:
        """Render the wall-clock age of a chat-history message.

        K-time1 helper. Returns short relative-age phrases meant to be
        wrapped in brackets and prepended to history-message content:

        - < 60s          -> ``just now``
        - 1-59 min       -> ``N min ago``
        - same calendar day -> ``today HH:MM``
        - previous day   -> ``yesterday HH:MM``
        - 2-6 days old   -> ``DayName HH:MM`` (e.g. ``Wednesday 18:45``)
        - older          -> ``Mon DD HH:MM`` (e.g. ``May 28 18:45``)

        Returns ``""`` if ``created_at_iso`` can't be parsed (defensive
        — caller should treat the empty string as "skip the prefix").
        ``now`` must be a timezone-aware datetime.

        K-time5: the banding logic now lives in
        :func:`app.core.infra.timephrase.age_prefix` (single canonical
        implementation). This stays a thin static method so existing
        callers + ``test_prompt_assembler`` keep working.
        """
        return _timephrase.age_prefix(created_at_iso, now)

    @staticmethod
    def _fit_history(
        history: list[MessageRow],
        budget_tokens: int,
        *,
        prefix_enabled: bool = False,
        now: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int, int, int]:
        """Greedy newest-first packer.

        Returns ``(messages, history_tokens, kept_count, dropped_count)``.
        ``dropped_count`` counts messages that were available in ``history``
        but didn't fit within ``budget_tokens``.

        When ``prefix_enabled`` is True (K-time1), every kept message's
        content is prefixed with ``[<relative age>] `` so the LLM has a
        per-message wall-clock anchor. The prefix is included in the
        token-cost accounting so the budget stays honest.
        """
        remaining = max(128, int(budget_tokens))
        kept: list[dict[str, Any]] = []
        running = 0
        dropped = 0
        anchor = now if now is not None else datetime.now(timezone.utc)
        for row in reversed(history):
            content = (row.content or "").strip()
            if not content:
                continue
            if prefix_enabled:
                age = PromptAssemblerHelpersMixin._format_age(row.created_at, anchor)
                if age:
                    content = f"[{age}] {content}"
            cost = estimate_tokens(content) + _MESSAGE_OVERHEAD
            if running + cost > remaining:
                dropped += 1
                continue
            role = "assistant" if row.role == "assistant" else "user"
            kept.append({"role": role, "content": content})
            running += cost
        kept.reverse()
        return kept, running, len(kept), dropped

    @staticmethod
    def _estimate(messages: list[dict[str, Any]]) -> int:
        # Reuse the LangChain-shaped estimator on duck-typed dicts.
        class _Shim:
            def __init__(self, content: str) -> None:
                self.content = content

        return estimate_messages_tokens([_Shim(m.get("content", "")) for m in messages])

