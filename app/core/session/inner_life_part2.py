from __future__ import annotations

import json
import logging
from typing import Any

from app.core.infra import timephrase
from app.core.proactive.cue_accounting import (
    REASON_CADENCE_BLOCK,
    REASON_CROSS_LANE,
    REASON_IMPORTANCE_FLOOR,
    REASON_NO_OPENING,
    REASON_NO_STOCK,
    REASON_TOPIC_MISS,
    note_decline,
)
from app.core.session.debug_overrides import DebugOverridesHostMixin


log = logging.getLogger("app.session")


def _parse_dt_utc(value):
    """Parse an ISO timestamp into a tz-aware UTC datetime, or ``None``.

    Naive timestamps are assumed UTC. Used by the H21 dream lookup to age
    ``[dream]`` reflections against a wall-clock lookback window.
    """
    if not value or not isinstance(value, str):
        return None
    from datetime import datetime, timezone

    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Assemblies an armed gap slot may go unspent before the moment is
# written off. More than one because the pickers can come up empty on a
# turn and be fruitful on the next; not many more because a welcome-back
# goes stale, and holding the slot costs a picker run each time.
_GAP_SLOT_ATTEMPTS = 3

_DREAM_PREFIX = "[dream] "


def _strip_dream_prefix(content: str) -> str:
    """Reflection text without the ``[dream]`` marker, for subject matching.

    The marker is storage bookkeeping. Leaving it on the cue's subject
    would put a word in there that Aiko can never say.
    """
    text = str(content or "").strip()
    if text.lower().startswith(_DREAM_PREFIX):
        return text[len(_DREAM_PREFIX):].strip()
    return text


def _hobby_book_state(host: Any, slug: str) -> dict[str, Any] | None:
    """Room-paperback ``item.state`` so a reading hobby can cite chapters."""
    store = getattr(host, "_world_store", None)
    if store is None or not hasattr(store, "list_items"):
        return None
    try:
        items = store.list_items()
    except Exception:
        return None
    book = next(
        (i for i in items if (getattr(i, "slug", "") or "") == slug),
        None,
    )
    if book is None:
        return None
    state = dict(getattr(book, "state", None) or {})
    if not str(state.get("title") or "").strip():
        name = str(getattr(book, "name", "") or "").strip()
        if name:
            state["title"] = name
    return state


class InnerLifePart2Mixin(DebugOverridesHostMixin):
    """Inner-life prompt-block providers (part 2 of 4)."""

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

    def _render_knowledge_grounding_block(self, user_text: str) -> str:
        """K61: on informational turns, commit to learned specifics.

        When the live turn is a question AND Aiko has facts she's
        actually learned (F9 ``knowledge`` rows, G3
        ``curiosity_finding`` rows) topically close to what was asked,
        surface up to ``knowledge_grounding_max_items`` of them and
        nudge her to name the real things instead of survey-hedging
        ("there are many...", "it depends") or lecturing. Pure local
        work: one regex (K4 dialogue act), one embed of ``user_text``,
        and a cosine scan over the two memory kinds. No LLM, no extra
        brain-path turn. Empty when the master switch is off, the turn
        isn't informational, there are no learned facts, or nothing
        clears the similarity threshold.
        """
        if not bool(
            getattr(
                self._settings.agent, "knowledge_grounding_enabled", True,
            )
        ):
            return ""
        text = (user_text or "").strip()
        if len(text) < 8:
            return ""
        # K4 informational gate -- regex only, no LLM on the hot path.
        try:
            from app.core.conversation.dialogue_act_tagger import tag_regex

            if tag_regex(text).act != "question":
                return ""
        except Exception:
            log.debug("knowledge-grounding: dialogue-act tag failed", exc_info=True)
            return ""

        store = getattr(self, "_memory_store", None)
        embedder = getattr(self, "_embedder", None)
        if store is None or embedder is None:
            return ""
        try:
            rows = list(store.iter_by_kind("knowledge")) + list(
                store.iter_by_kind("curiosity_finding")
            )
        except Exception:
            log.debug("knowledge-grounding: kind snapshot failed", exc_info=True)
            return ""
        if not rows:
            return ""
        try:
            qvec = embedder.embed(text)
        except Exception:
            log.debug("knowledge-grounding: embed failed", exc_info=True)
            return ""

        from app.llm.embedder import cosine_similarity

        mem_settings = self._memory_settings
        threshold = float(
            getattr(
                mem_settings, "knowledge_grounding_min_similarity", 0.45,
            )
        )
        max_items = max(
            1,
            int(
                getattr(
                    mem_settings, "knowledge_grounding_max_items", 2,
                )
            ),
        )
        scored: list[tuple[float, str]] = []
        for mem in rows:
            emb = getattr(mem, "embedding", None)
            if emb is None or getattr(emb, "size", 0) == 0:
                continue
            try:
                sim = float(cosine_similarity(qvec, emb))
            except Exception:
                continue
            if sim < threshold:
                continue
            content = (getattr(mem, "content", "") or "").strip()
            if content:
                scored.append((sim, content))
        if not scored:
            return ""
        scored.sort(key=lambda t: t[0], reverse=True)

        bullets: list[str] = []
        seen: set[str] = set()
        for _sim, content in scored:
            key = content.lower()
            if key in seen:
                continue
            seen.add(key)
            snippet = (
                content
                if len(content) <= 160
                else content[:159].rstrip() + "\u2026"
            )
            bullets.append(f"- {snippet}")
            if len(bullets) >= max_items:
                break

        log.info(
            "knowledge-grounding fire: candidates=%d surfaced=%d top=%.3f",
            len(scored),
            len(bullets),
            scored[0][0],
        )
        return (
            "You actually know specifics here -- commit to them. Name the "
            "real things below in your own voice; skip the survey hedges "
            "(\"there are lots of...\", \"it depends\") and don't lecture:\n"
            + "\n".join(bullets)
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

    def _render_trusted_beliefs_block(self) -> str:
        """K2: what she has actually learned he thinks and feels.

        The counterpart to :meth:`_render_belief_gaps_block`, and the
        reason that one was doing all the work alone. Until this existed,
        a belief reached the prompt only by being *wrong* -- she could
        voice a theory of mind exclusively at the moment it failed, and a
        store full of correct reads about him contributed nothing to how
        she spoke. On the install where that was measured the gap block
        had rendered on 3 turns out of 851, so for practical purposes the
        whole layer was inert.

        Only ``confirmed`` rows appear (see ``BeliefStore.list_trusted``):
        an ``active`` belief is a single uncorroborated extraction, and
        speaking those back is how a companion earns a reputation for
        confidently inventing things. The cue is deliberately phrased as
        standing context rather than as an instruction to bring anything
        up -- it is background she talks *from*, not a prompt to perform
        insight. It therefore offers **no stance** and is deliberately
        absent from ``_OFFERS`` in ``app/core/conversation/stance.py``,
        alongside the other standing-context blocks (``profile_block``,
        ``axes_block``): it never asks for the floor, so there is nothing
        for the K92 arbiter to arbitrate.

        Reads the store on each assembly rather than caching in
        ``_StaticSlices``: one indexed ``SELECT ... LIMIT n`` is the same
        order of cost as the ``day_color`` kv read that also runs
        uncached, and the block has to reflect a promotion the worker
        made mid-conversation.
        """
        agent = self._settings.agent
        if not bool(getattr(agent, "belief_tracking_enabled", True)):
            return ""
        if not bool(getattr(agent, "belief_trusted_block_enabled", True)):
            return ""
        limit = max(0, int(getattr(agent, "belief_trusted_block_max", 4)))
        if limit <= 0:
            return ""
        store = getattr(self, "_belief_store", None)
        if store is None:
            return ""
        try:
            rows = store.list_trusted(user_id=self._user_id, limit=limit)
        except Exception:
            log.debug("trusted beliefs read failed", exc_info=True)
            return ""
        if not rows:
            return ""
        try:
            from app.core.relationship.belief_store import KIND_MOOD

            lines = [
                "- "
                + (
                    f"{self.user_display_name} is {b.predicted_state} "
                    f"about {b.topic}"
                    if b.kind == KIND_MOOD
                    else f"{b.topic} is {b.predicted_state}, to him"
                )
                for b in rows
            ]
        except Exception:
            log.debug("trusted beliefs render failed", exc_info=True)
            return ""
        return (
            f"What you've come to understand about {self.user_display_name} "
            "(corroborated more than once — you can just assume these "
            "rather than checking):\n"
            + "\n".join(lines)
            + "\nThis is background you speak from, not a list to bring up. "
            "Don't recite it, don't ask him to confirm it, and drop any of "
            "it the moment he says otherwise."
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

            state = store.get(self._user_id)
            state = calibration_detector.decay(
                state,
                now=timephrase.utcnow(),
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
        the slot is cleared once the cue reaches the prompt, so it
        appears exactly once.

        Alone in the gap family in staying off the cue pool, and for a
        reason that is about the cue rather than about effort: what it
        renders is a register instruction built from the duration, with
        no subject in it. Consumption matching asks "did she say this
        thing" and there is no thing -- a warm welcome-back can be
        letter-perfect without reusing a single word of the cue. Pooling
        it would manufacture misses.

        Empty string when the master switch is off, when no absence
        result is pending, or when the slot holds something unusable.
        The clear moved below those checks so it happens on the path
        that renders rather than on the path that reads -- the two
        coincide today, since an unparseable duration is not worth
        retrying either way, and the point is that a gate added later
        cannot quietly spend the return.
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
        try:
            seconds_f = float(seconds)
        except (TypeError, ValueError):
            seconds_f = 0.0
        self._pending_absence_seconds = None
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

    def _spend_gap_slot(self, attr: str, *, fired: bool) -> None:
        """Clear an armed gap slot, or leave it armed for another look.

        The slot used to clear the instant it was read, which spent the
        return-from-gap opportunity on turns where nothing reached the
        prompt at all -- the picker found no candidate, the overnight
        check failed. Those are exactly the turns worth trying again:
        nothing was said, so nothing was used up.

        Bounded by ``_GAP_SLOT_ATTEMPTS`` rather than held until it
        fires. The retry that matters is for a cue Aiko *saw* and passed
        on, and that one lives in the pool with its own budget; this only
        covers the narrower case of a picker that came up empty, which
        stops being worth re-running once the return is no longer recent.
        """
        attempts = getattr(self, "_gap_slot_attempts", None)
        if attempts is None:
            attempts = {}
            self._gap_slot_attempts = attempts
        spent = attempts.get(attr, 0) + 1
        if fired or spent >= _GAP_SLOT_ATTEMPTS:
            attempts.pop(attr, None)
            setattr(self, attr, None)
            return
        attempts[attr] = spent

    def _render_turning_over_block(self) -> str:
        """K28: surface one recent reflection on the first turn after a gap.

        Sibling of :meth:`_render_absence_curiosity_block` -- both
        ride the typed-gap signal armed by the post-turn engagement
        tracker, but they answer different questions: K14
        ``absence_curiosity`` frames the welcome-back; K28
        ``turning_over`` surfaces what Aiko's been thinking about
        in the meantime. The two stack on the 90 min - 4h overlap.

        Two ways to fire. A released pool row -- a reflection she was
        shown last time and did not pick up -- is preferred, because it
        is a cue with unfinished business rather than a new one. Failing
        that, the slot ``self._pending_turning_over_seconds`` (armed by
        ``post_turn_mixin`` when ``engagement.latency_seconds >=
        memory.turning_over_min_gap_minutes * 60``) runs the picker
        (:func:`app.core.session.inner_life.turning_over.pick_turning_over`)
        and what it chooses is logged to the pool as surfaced, so that
        whether Aiko actually used it becomes a question with an answer.
        The slot survives a fruitless picker for a couple of assemblies
        (see :meth:`_spend_gap_slot`). Falls silent when:

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
            self._debug_overrides.take("turning_over_force_next", False)
        )

        # A reflection she was shown and did not pick up comes back
        # before a fresh one is chosen -- it was worth sharing a turn ago
        # and still is. The pool decides how many chances it gets, so
        # this cannot loop.
        row = self.take_pool_cue("turning_over")
        if row is not None:
            self._pending_turning_over_seconds = None
            self._gap_cue_surfaced = True
            log.info("turning-over retry: cue=%d", row.id)
            return row.text

        seconds = getattr(self, "_pending_turning_over_seconds", None)
        if not force_next and seconds is None:
            # Deliberately unreported: ``take_pool_cue`` above has already
            # recorded a reason on every path that reaches here, and
            # ``note_decline`` is first-writer-wins. Reaching this bail at
            # all means the slot is empty, and an empty slot can only be
            # armed by pool stock -- which is what that call just judged.
            return ""

        block = self._turning_over_from_reflections(
            seconds, force_next=force_next,
        )
        self._spend_gap_slot("_pending_turning_over_seconds", fired=bool(block))
        return block

    def _turning_over_from_reflections(
        self, seconds: Any, *, force_next: bool,
    ) -> str:
        """Pick and render a reflection, or ``""`` when none qualifies.

        Split from the provider so the slot's lifetime is decided in one
        place: every path out of here is either a fire or a miss, and the
        caller spends the slot accordingly.
        """
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


        memory_settings = self._memory_settings
        try:
            result = _to.pick_turning_over(
                reflections=reflections,
                active_goal_vecs=goal_vecs,
                recent_user_vecs=msg_vecs,
                now=timephrase.utcnow(),
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

        try:
            block = _to.render_inner_life_block(
                result,
                user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("turning-over render failed", exc_info=True)
            return ""
        if not block:
            return ""

        # Stash diagnostics for the MCP debug tool.
        self._last_turning_over = result
        # K36 one-of guard: mark that a gap cue surfaced this assembly so
        # ``away_activities`` defers to this (reflection-based) cue.
        self._gap_cue_surfaced = True
        # Log it as surfaced-not-yet-used. The subject is the reflection
        # itself: there is no topic label to match on, which is why the
        # policy asks for three shared words rather than one.
        self.record_surfaced_cue(
            "turning_over",
            _strip_dream_prefix(result.content),
            block,
            payload={"memory_id": int(result.memory_id)},
        )

        log.info(
            "turning-over fire: memory_id=%d age_h=%.1f topical=%.3f "
            "source=%s dream=%s",
            result.memory_id,
            result.age_hours,
            result.topical_score,
            result.topical_source or "-",
            result.dream,
        )
        return block

    def _render_sleep_return_block(self) -> str:
        """H21: narrate having dozed off on return from an overnight gap.

        The behavioural anchor for the dream system. Runs first in the
        gap-cue family (immediately after K28 ``turning_over``, before K36
        ``away_activities`` / K34 ``forward_curiosity``) so an overnight
        return reads as "I actually fell asleep …" rather than "I tidied
        the desk while you were away". When a recent ``[dream]`` reflection
        exists, it's woven into the line so the dream finally has a cause.

        Two ways to fire, the pool first: a line she was shown and did
        not use comes back before a new one is composed. Otherwise the
        slot ``self._pending_sleep_return_seconds`` (armed in
        ``post_turn_helpers_mixin._maybe_arm_sleep_return_slot`` on a typed
        gap >= ``memory.sleep_return_min_gap_hours``) opens the door and
        the finer return-hour-aware overnight gate
        (:func:`sleep_return.looks_like_overnight`) decides. A gap that
        doesn't read as a sleep returns "" WITHOUT touching
        ``_gap_cue_surfaced``, so the ordinary away / forward cues still
        get their turn -- and now without burning the slot either, since
        nothing was spent. When it does fire it sets ``_gap_cue_surfaced``
        so the rest of the family defers, and logs the line to the pool.

        Defers to ``turning_over`` (which runs first and owns the
        ``_gap_cue_surfaced`` reset). MCP debug:
        ``force_sleep_return_surface`` arms ``_sleep_return_force_next`` to
        bypass the slot + overnight gates.
        """
        if not bool(
            getattr(self._settings.agent, "sleep_return_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("sleep_return_force_next", False)
        )

        # One-of guard: turning_over already surfaced a gap cue this
        # assembly. Stand down (unless explicitly forced).
        if not force_next and getattr(self, "_gap_cue_surfaced", False):
            return ""

        row = self.take_pool_cue("sleep_return")
        if row is not None:
            self._pending_sleep_return_seconds = None
            self._gap_cue_surfaced = True
            log.info("sleep-return retry: cue=%d", row.id)
            return row.text

        seconds = getattr(self, "_pending_sleep_return_seconds", None)
        if not force_next and seconds is None:
            # Already reported by ``take_pool_cue``; see ``turning_over``.
            # This is the bail behind 6 of the 7 impossible mutex rows on
            # the live ledger, and the reason was never missing -- the
            # structural attribution was overwriting it.
            return ""

        block = self._sleep_return_line(seconds, force_next=force_next)
        self._spend_gap_slot("_pending_sleep_return_seconds", fired=bool(block))
        return block

    def _sleep_return_line(self, seconds: Any, *, force_next: bool) -> str:
        """Compose the dozed-off line, or ``""`` when the gap isn't one.

        Split from the provider so the slot's lifetime is decided in one
        place -- the overnight gate rejecting a gap is the commonest way
        this returns nothing, and it used to cost the slot regardless.
        """
        from app.core.world import sleep_return as _sr

        ms = self._memory_settings
        min_gap_h = float(
            getattr(ms, "sleep_return_min_gap_hours", _sr.DEFAULT_MIN_GAP_HOURS)
        )
        overnight_h = float(
            getattr(
                ms, "sleep_return_overnight_hours", _sr.DEFAULT_OVERNIGHT_HOURS
            )
        )

        now_local = timephrase.now()
        if force_next:
            try:
                gap_hours = float(seconds) / 3600.0 if seconds is not None else overnight_h
            except (TypeError, ValueError):
                gap_hours = overnight_h
        else:
            try:
                gap_hours = float(seconds) / 3600.0
            except (TypeError, ValueError):
                return ""
            if not _sr.looks_like_overnight(
                gap_hours,
                now_local.hour,
                min_gap_hours=min_gap_h,
                overnight_hours=overnight_h,
            ):
                log.debug(
                    "sleep-return silent: gap=%.1fh hour=%d not overnight",
                    gap_hours, now_local.hour,
                )
                return ""

        # Where she dozed off — her current room location if it reads as a
        # restful spot, else the cozy default. Best-effort; never fatal.
        spot_slug: str | None = None
        world_store = getattr(self, "_world_store", None)
        if world_store is not None:
            try:
                state = world_store.get_state()
                loc_id = getattr(state, "location_id", None)
                if loc_id is not None:
                    loc = world_store.get_location_by_id(int(loc_id))
                    if loc is not None:
                        spot_slug = getattr(loc, "slug", None)
            except Exception:
                log.debug("sleep-return: world state read failed", exc_info=True)
        spot_phrase = _sr.sleep_spot_phrase(spot_slug)

        # Optional dream linkage — newest ``[dream]`` reflection within the
        # lookback window gets woven into the cue.
        dream_gist = self._recent_dream_gist(now_local, ms)

        name = self.user_display_name
        block = _sr.render_sleep_line(
            spot_phrase,
            user_display_name=name,
            dream_gist=dream_gist,
        )
        if not block:
            return ""

        self._gap_cue_surfaced = True
        self._last_sleep_return = {
            "gap_hours": round(gap_hours, 2),
            "return_hour": now_local.hour,
            "spot": spot_phrase,
            "spot_slug": spot_slug,
            "dream": bool(dream_gist),
        }
        # The dream is the part worth chasing; the spot is what's left
        # when there wasn't one.
        self.record_surfaced_cue(
            "sleep_return",
            dream_gist or spot_phrase,
            block,
            payload={"dream": bool(dream_gist), "spot_slug": spot_slug},
        )
        log.info(
            "sleep-return fire: gap=%.1fh hour=%d spot=%s dream=%s",
            gap_hours, now_local.hour, spot_slug or "-", bool(dream_gist),
        )
        return block

    def _recent_dream_gist(self, now_local: Any, memory_settings: Any) -> str | None:
        """Newest ``[dream]`` reflection content within the lookback window.

        Dreams are stored by the :class:`DreamWorker` as ``kind="reflection"``
        rows whose content is prefixed ``[dream] ``. Returns the cleaned gist
        (prefix stripped, truncated) or ``None`` when no recent dream exists.
        """
        from datetime import datetime

        from app.core.world import sleep_return as _sr

        memory_store = getattr(self, "_memory_store", None)
        if memory_store is None:
            return None
        lookback_h = float(
            getattr(
                memory_settings,
                "sleep_return_dream_lookback_hours",
                _sr.DEFAULT_DREAM_LOOKBACK_HOURS,
            )
        )
        if lookback_h <= 0:
            return None
        try:
            reflections = list(memory_store.iter_by_kind("reflection"))
        except Exception:
            log.debug("sleep-return: reflection snapshot failed", exc_info=True)
            return None

        prefix = "[dream] "
        now_utc = timephrase.utcnow()
        best_dt: datetime | None = None
        best_content: str | None = None
        for mem in reflections:
            content = str(getattr(mem, "content", "") or "")
            if not content.lower().startswith(prefix):
                continue
            created = _parse_dt_utc(getattr(mem, "created_at", None))
            if created is None:
                continue
            age_h = (now_utc - created).total_seconds() / 3600.0
            if age_h < 0 or age_h > lookback_h:
                continue
            if best_dt is None or created > best_dt:
                best_dt = created
                best_content = content[len(prefix):].strip()

        if not best_content:
            return None
        # Keep the cue short — first sentence / 160 chars.
        gist = best_content.replace("\n", " ").strip()
        if len(gist) > 160:
            gist = gist[:157].rstrip() + "…"
        return gist or None

    def _render_caught_mid_activity_block(self) -> str:
        """H26: she is in the middle of something when he comes back.

        The K36 sibling of this block reports a *finished* beat ("while
        you were away, you re-potted the basil"). This one fires on the
        narrower case where the beat is still running: the room already
        shows her at it, so the honest opening is not a report but an
        interruption — "oh — hang on, let me put this down".

        It runs before ``away_activities`` and takes the shared gap-cue
        slot, because the two would otherwise contradict each other in
        the same prompt: one saying she finished a thing, the other that
        she is still doing it. Being caught wins — it is what the world
        state actually says right now.

        Surfacing marks the beat interrupted, which hands it to the
        worker as a thread to pick back up later. That is the half that
        makes this more than a different opening line: she gets to
        return to it, so the afternoon has continuity rather than a
        series of announcements.
        """
        if not bool(
            getattr(self._settings.agent, "away_activities_enabled", True)
        ):
            return ""
        force_next = bool(
            self._debug_overrides.take("caught_mid_activity_force_next", False)
        )
        if not force_next and getattr(self, "_gap_cue_surfaced", False):
            return ""

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""
        try:
            from app.core.world import in_progress_beat
        except Exception:
            log.debug("in_progress_beat import failed", exc_info=True)
            return ""

        # No open beat is not a missed chance, it is nothing to say -- and
        # it is by far this cue's most common outcome, since a return
        # landing inside a running beat is rare. Reported so the ratio has
        # a denominator: ``no_stock`` is in ``INELIGIBLE_REASONS``, so
        # these turns drop out of ``eligible`` rather than reading as a
        # cue that keeps losing.
        beat = in_progress_beat.load(chat_db.kv_get)
        if beat is None or not beat.activity:
            note_decline(self, "caught_mid_activity", REASON_NO_STOCK)
            return ""
        now = timephrase.utcnow()
        if not force_next and not beat.is_open_at(now):
            note_decline(self, "caught_mid_activity", REASON_NO_STOCK)
            return ""

        # Unlike the other gap cues this one has no minimum-absence bar.
        # The question it answers is "is she mid-something right now",
        # and that is equally true whether he stepped out for an hour or
        # closed the laptop yesterday.
        in_progress_beat.mark_interrupted(chat_db.kv_set, beat, now)
        used_id = getattr(beat, "used_item_id", None)
        store = getattr(self, "_world_store", None)
        if store is not None and used_id is not None:
            try:
                here = store.get_state().location_id
                put = getattr(store, "put_down", None)
                item = put(int(used_id), location_id=here) if callable(put) else None
                if item is not None:
                    notify = getattr(self, "_notify_world", None)
                    if callable(notify):
                        notify({"item": item.to_dict()})
            except Exception:
                log.debug("caught-mid-activity put_down failed", exc_info=True)
        self._gap_cue_surfaced = True
        self._spend_gap_slot("_pending_away_activities_seconds", fired=True)

        minutes = beat.minutes_in(now)
        elapsed = (
            f" You're about {minutes} minutes into it."
            if 3 <= minutes <= 180 else ""
        )
        what = (beat.summary or "").strip() or beat.activity
        block = (
            f"You are in the middle of {what} right now — you "
            f"didn't finish it before {self.user_display_name} showed "
            f"up.{elapsed} Let that show in how you arrive: you might "
            "need a second to set it down, and it's the kind of thing "
            "you'd mention in passing rather than announce. Don't "
            "narrate it as a completed task, and don't make him wait "
            "on it — he's the reason you're putting it down."
        )
        self.record_surfaced_cue(
            "caught_mid_activity",
            beat.activity,
            block,
            payload={"key": beat.key, "started_at": beat.started_at},
        )
        log.info(
            "caught-mid-activity fire: key=%s minutes_in=%d",
            beat.key, minutes,
        )
        return block

    # ── H26 debug surface ────────────────────────────────────────────

    def in_progress_beat_state(self) -> dict[str, Any]:
        """What she is in the middle of right now, if anything.

        Public because the MCP debug tools would otherwise reach through
        the controller for the kv handle and the ratio — see
        ``tests/test_private_reach_guard.py``.
        """
        from app.core.world import in_progress_beat

        chat_db = getattr(self, "_chat_db", None)
        state: dict[str, Any] = {
            "in_progress_ratio": float(
                getattr(
                    self._memory_settings,
                    "away_activities_in_progress_ratio",
                    0.3,
                )
            ),
            "force_next": bool(
                self.debug_overrides.peek(
                    "caught_mid_activity_force_next", False
                )
            ),
            "beat": None,
        }
        beat = (
            in_progress_beat.load(chat_db.kv_get)
            if chat_db is not None else None
        )
        if beat is None:
            return state
        now = timephrase.utcnow()
        state["beat"] = {
            "key": beat.key,
            "activity": beat.activity,
            "posture": beat.posture,
            "summary": beat.summary,
            "started_at": beat.started_at,
            "expected_end_at": beat.expected_end_at,
            "minutes_in": beat.minutes_in(now),
            "minutes_left": beat.minutes_left(now),
            "open": beat.is_open_at(now),
            "interrupted_at": beat.interrupted_at or None,
        }
        return state

    def force_caught_mid_activity(
        self, activity_key: str = "",
    ) -> dict[str, Any]:
        """Leave one beat running and arm the return cue (debug).

        Two steps because the interesting state is otherwise mostly
        waiting: an open beat is a minority outcome *and* its window has
        to still be open when the next turn assembles.
        """
        from app.core.world import in_progress_beat

        worker = getattr(self, "_away_activity_worker", None)
        if worker is None:
            return {"error": "worker not registered (no WorldStore?)"}
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is not None:
            # An already-open beat would make the worker defer instead of
            # starting the one being asked for.
            in_progress_beat.clear(chat_db.kv_set)
        if activity_key.strip():
            worker.force_activity(activity_key.strip())
        worker.force_in_progress()
        result = worker.run()
        self.debug_overrides.arm("caught_mid_activity_force_next")
        beat = (
            in_progress_beat.load(chat_db.kv_get)
            if chat_db is not None else None
        )
        return {
            "ran": True,
            "result": result,
            "left_open": beat is not None,
            "activity": getattr(beat, "activity", ""),
            "expected_end_at": getattr(beat, "expected_end_at", ""),
            "armed": True,
        }

    def _render_away_activities_block(self) -> str:
        """K36: surface one "while you were away I …" line after a gap.

        Consumer side of the :class:`IdleAwayActivityWorker` producer.
        Same typed-gap arming as K28 ``turning_over`` (via
        ``post_turn_mixin._maybe_arm_away_activities_slot``), but reads
        the worker's kv journal instead of the reflection corpus.

        Pool first: a beat she was shown and let pass comes back before a
        new one is taken off the journal. Otherwise it reads
        ``self._pending_away_activities_seconds``, re-checks the gap,
        reads the journal ring, and surfaces the newest entry that's
        newer than the ``away_activity.last_surfaced_at`` watermark.

        The watermark still advances on render, and deliberately: its job
        is "don't offer this beat off the journal twice", which is not the
        job the pool took over. The pool answers the different question of
        whether she *used* the beat, and holds the retry if she didn't --
        so a miss now comes back as a released row rather than as a
        second reading of the same journal entry.

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
            self._debug_overrides.take("away_activities_force_next", False)
        )

        # One-of guard: turning_over already surfaced a gap cue this
        # assembly. Stand down (unless explicitly forced).
        if not force_next and getattr(self, "_gap_cue_surfaced", False):
            return ""

        row = self.take_pool_cue("away_activities")
        if row is not None:
            self._pending_away_activities_seconds = None
            self._gap_cue_surfaced = True
            log.info("away-activities retry: cue=%d", row.id)
            return row.text

        seconds = getattr(self, "_pending_away_activities_seconds", None)
        if not force_next and seconds is None:
            # Already reported by ``take_pool_cue``; see ``turning_over``.
            return ""

        block = self._away_activities_from_journal(
            seconds, force_next=force_next,
        )
        self._spend_gap_slot(
            "_pending_away_activities_seconds", fired=bool(block),
        )
        return block

    def _away_activities_from_journal(
        self, seconds: Any, *, force_next: bool,
    ) -> str:
        """Render the newest unseen journal beat, or ``""``.

        Split from the provider so the slot's lifetime is decided in one
        place: an empty journal or an already-surfaced beat is a miss, and
        used to cost the return-from-gap opportunity anyway.
        """
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
        block = (
            f"While {name} was away, you {summary}. If it fits naturally, "
            "you can mention it in passing — drop it if it doesn't."
        )
        # The summary clause is the subject: "repotted the basil" is what
        # she'd actually say, with the framing around it hers to choose.
        self.record_surfaced_cue(
            "away_activities",
            summary,
            block,
            payload={"at": at, "key": str(newest.get("key") or "")},
        )
        log.info("away-activities fire: at=%s key=%s", at, newest.get("key"))
        return block

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
            self._debug_overrides.take("forward_curiosity_force_next", False)
        )

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

        pool = getattr(self, "_cue_store", None)
        if pool is not None:
            # No relevance gate: the gap slot above already decided this is
            # the moment, and the question is about the user's life rather
            # than the live topic.
            row = self.take_pool_cue("forward_curiosity", force=force_next)
            if row is None:
                log.debug("forward_curiosity silent: empty pool")
                return ""
            self._gap_cue_surfaced = True
            log.info(
                "forward-curiosity fire: source=%s",
                row.payload.get("source"),
            )
            return row.text

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
        log.info(
            "forward-curiosity fire: at=%s source=%s", at, newest.get("source"),
        )
        return f"You've been wondering {question}."

    def _render_concept_hypothesis_block(self, user_text: str) -> str:
        """L30b: surface one untested hunch so Aiko can actually ask.

        Consumer side of the
        :class:`~app.core.proactive.concept_hypothesis_worker.ConceptHypothesisWorker`.
        The worker queues the belief most worth resolving; this decides
        whether *this* turn is a moment to raise it.

        **Dual-mode, and the only cue that is.** Every other pooled cue is
        either topic-gated or gap-armed, and a belief probe genuinely
        wants both. The natural moment is while the subject is already up
        ("you were just saying you walk to think --"), which no gap slot
        can detect; but a lull is also a real opening, and holding out for
        topical luck would leave hunches queued indefinitely. So:

        1. **Topic path**, tried first and by far the better one. Lexical
           overlap between the belief's label and the live message, no gap
           involved. Does *not* touch ``_gap_cue_surfaced`` -- it is not
           spending the gap slot, matching ``knowledge_gap_notice``.
        2. **Gap path**, only if the topic path found nothing. Defers to
           every other gap cue via ``_gap_cue_surfaced`` (this type is
           last in ``GAP_CUE_ORDER``: raising a belief about someone out
           of silence is the heaviest thing she can open with), needs the
           armed slot and the minimum gap, and adds a bar the topic path
           does not have -- ``concept_hypothesis_gap_min_importance``. Out
           of a lull, only a hunch that *matters* is worth the weight.

        **K47 governs this one**, unlike the L30a musing lane. A musing is
        a thought and costs the user nothing; this block exists to produce
        a question, so it belongs under the question budget.

        Cross-lane guard: skips any belief the L30a lane already mused
        about this assembly (``_last_hypothesis_lane_concept_ids``, plus
        ``_last_hypothesis_lane_hypothesis_ids`` for Phase B's invented
        rows), so a single turn never carries both "I half-wonder whether
        X" at T3 and "ask about X" at T6.

        The cue is a private hint, never spoken verbatim -- Aiko phrases
        the question herself. MCP debug:
        ``force_concept_hypothesis_surface`` arms
        ``_concept_hypothesis_force_next`` to bypass the topic, slot, gap
        and importance gates (the pool must still have a cue).
        """
        if not bool(
            getattr(
                self._settings.agent, "concept_hypothesis_ask_enabled", True,
            )
        ):
            return ""
        if self._question_balance_suppressed():
            return ""

        pool = getattr(self, "_cue_store", None)
        if pool is None:
            note_decline(self, "concept_hypothesis", REASON_NO_STOCK)
            return ""

        force_next = bool(
            self._debug_overrides.take("concept_hypothesis_force_next", False)
        )

        # One-shot, and read here rather than inside the gap branch: if
        # the topic path fires we have spent this cue type for the turn,
        # and leaving the slot armed would let a stale lull open a second
        # probe on the next assembly.
        seconds = getattr(self, "_pending_concept_hypothesis_seconds", None)
        self._pending_concept_hypothesis_seconds = None

        try:
            from app.core.proactive.concept_hypothesis_worker import (
                cue_importance,
            )
            from app.core.proactive.knowledge_gap_notice_worker import (
                topic_relevant,
            )
        except Exception:
            log.debug("concept_hypothesis import failed", exc_info=True)
            return ""

        # Two sets because the ids live in two namespaces: a grounded cue
        # carries a concept id, an invented one a hypothesis id, and
        # matching them against a single set would let a concept id
        # accidentally suppress a hypothesis that happened to share it.
        claimed = {
            "concept": set(
                getattr(self, "_last_hypothesis_lane_concept_ids", ()) or ()
            ),
            "hypothesis": set(
                getattr(self, "_last_hypothesis_lane_hypothesis_ids", ()) or ()
            ),
        }

        def _unclaimed(payload: dict[str, Any]) -> bool:
            target_type = str(payload.get("target_type") or "concept")
            try:
                target_id = int(payload.get("target_id") or 0)
            except (TypeError, ValueError):
                return True
            return target_id not in claimed.get(target_type, set())

        text = (user_text or "").strip()
        topic_missed = False
        if text or force_next:
            row = self.take_pool_cue(
                "concept_hypothesis",
                relevant=lambda payload: _unclaimed(payload)
                and topic_relevant(
                    str(payload.get("label") or ""),
                    text,
                    options=self._topic_gate_options(),
                ),
                force=force_next,
                # This provider does its own accounting: it is the only
                # dual-mode cue, so a topic miss here is a fallthrough to
                # the gap path rather than the turn's decision.
                note_as=None,
                user_text=text,
            )
            if row is not None:
                log.info(
                    "concept-hypothesis fire: path=topic target=%s label=%r",
                    row.payload.get("target_id"),
                    str(row.payload.get("label") or "")[:80],
                )
                return row.text
            # Not noted yet: the gap path below is a real second chance,
            # and a fallthrough is not a decision. Carried so that if the
            # gap path turns out to be unavailable, the turn is reported
            # as what it was -- nothing on the shelf about what he said.
            topic_missed = bool(text)

        # ── gap path ──────────────────────────────────────────────────
        if not force_next and getattr(self, "_gap_cue_surfaced", False):
            # No note: the attribution reads the gap mutex itself and
            # reports which cue won, which is strictly more than we know.
            return ""
        if not force_next:
            min_gap_s = (
                float(
                    getattr(
                        self._memory_settings,
                        "concept_hypothesis_min_gap_hours",
                        4.0,
                    )
                )
                * 3600.0
            )
            try:
                seconds_f = float(seconds) if seconds is not None else -1.0
            except (TypeError, ValueError):
                seconds_f = -1.0
            if seconds_f < min_gap_s:
                note_decline(
                    self, "concept_hypothesis",
                    REASON_TOPIC_MISS if topic_missed
                    else REASON_CADENCE_BLOCK,
                )
                return ""

        min_importance = float(
            getattr(
                self._memory_settings,
                "concept_hypothesis_gap_min_importance",
                0.55,
            )
        )
        # Which bar each rejected candidate failed, so a dry gap path can
        # say whether the shelf was too light or already spoken for. H7
        # spent months unable to tell those apart.
        saw_light = False
        saw_claimed = False
        saw_unreadable = 0

        def _weighty(payload: dict[str, Any]) -> bool:
            nonlocal saw_light, saw_claimed, saw_unreadable
            if not _unclaimed(payload):
                saw_claimed = True
                return False
            weight = cue_importance(payload)
            if weight is None:
                # Not silently zero: "no stakes recorded" and "the least
                # important thing on the shelf" are different facts, and
                # collapsing them is what hid H7 for months.
                saw_unreadable += 1
                return False
            if weight >= min_importance:
                return True
            saw_light = True
            return False

        row = self.take_pool_cue(
            "concept_hypothesis",
            relevant=None if force_next else _weighty,
            force=force_next,
            note_as=None,
        )
        if row is None:
            if saw_unreadable:
                log.warning(
                    "concept_hypothesis: %d queued cue(s) carry neither an "
                    "importance nor a resolvable kind and can never clear "
                    "the gap bar",
                    saw_unreadable,
                )
            if saw_light:
                reason = REASON_IMPORTANCE_FLOOR
            elif saw_claimed:
                reason = REASON_CROSS_LANE
            elif self._cadence_blocked("concept_hypothesis"):
                reason = REASON_CADENCE_BLOCK
            else:
                reason = REASON_NO_STOCK
            note_decline(self, "concept_hypothesis", reason)
            log.debug("concept_hypothesis silent: %s", reason)
            return ""
        self._gap_cue_surfaced = True
        log.info(
            "concept-hypothesis fire: path=gap target=%s importance=%s "
            "label=%r",
            row.payload.get("target_id"),
            row.payload.get("importance"),
            str(row.payload.get("label") or "")[:80],
        )
        return row.text

    def _render_hobby_block(self) -> str:
        """H19: standing "what she's been up to lately" line.

        Reads the :class:`HobbyWorker`'s ``aiko.current_hobby`` kv blob and
        renders one terse line giving Aiko continuity of intent — a real
        answer to "what have you been up to?" that progresses across days.
        Empty when the worker hasn't started a hobby yet. The actual
        takeaways ("I'm three chapters in and ugh, the betrayal") surface
        separately through the H17 idle-seed cue.
        """
        if not bool(
            getattr(self._settings.agent, "hobby_worker_enabled", True)
        ):
            return ""

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.proactive.hobby_worker import load_hobby
            from app.core.world.hobby import prompt_progress, render_hobby_line
            from app.core.world.room_evolution import BOOK_SLUG
        except Exception:
            log.debug("hobby block import failed", exc_info=True)
            return ""

        state = load_hobby(chat_db.kv_get)
        if not state:
            return ""

        label = str(state.get("label") or "").strip()
        artifact = str(state.get("artifact") or "").strip()
        kind = str(state.get("kind") or "").strip()
        book_state = None
        if kind == "reading":
            book_state = _hobby_book_state(self, BOOK_SLUG)
            if book_state:
                titled = str(
                    book_state.get("title") or ""
                ).strip()
                if titled:
                    artifact = titled
        if not label and not artifact:
            return ""
        progress, unit = prompt_progress(state, book_state)
        line = render_hobby_line(
            label, progress, unit, artifact=artifact, kind=kind,
        )
        return (
            f"Lately, in your own time, you've been {line}. Bring it up only "
            "if it comes up naturally — don't force it."
        )

    def _render_idle_seed_block(self) -> str:
        """H17: surface one "while I was <doing X> I started wondering ..." cue.

        Consumer side of the :class:`IdleAwayActivityWorker` seed producer
        (``_maybe_emit_seed`` → the ``aiko.idle_seeds`` kv ring). Folds the
        newest unseen seed into the prompt as one optional, private hint so
        Aiko phrases the line herself — the seed is NEVER spoken verbatim.

        Unlike the gap-return cue family (turning_over / away_activities /
        forward_curiosity), this is NOT gap-gated and does NOT touch
        ``_gap_cue_surfaced``: a thought from her own idle life can come up
        mid-conversation. Bounded instead by the producer (rare + daily-
        capped) and a wall-clock surfacing cooldown so it never spams.

        One-shot per seed via the ``idle_seed.surfaced_at`` watermark; the
        ``idle_seed.surfaced_clock`` stamp enforces the cooldown. MCP debug:
        ``force_idle_seed_surface`` arms ``_idle_seed_force_next`` to bypass
        both gates (the ring still has to be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "idle_seed_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("idle_seed_force_next", False)
        )

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.world.idle_activity_worker import load_idle_seeds
        except Exception:
            log.debug("idle_seed import failed", exc_info=True)
            return ""

        ring = load_idle_seeds(chat_db.kv_get)
        if not ring:
            log.debug("idle_seed silent: empty ring")
            return ""

        newest = ring[-1]
        at = str(newest.get("at") or "")
        seed = str(newest.get("seed") or "").strip()
        if not seed:
            return ""

        watermark_key = "idle_seed.surfaced_at"
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                log.debug("idle_seed silent: already surfaced %s", at)
                return ""

            # Wall-clock surfacing cooldown — don't fold a seed into the
            # prompt more often than ``idle_seed_surface_cooldown_seconds``.
            from datetime import datetime, timezone

            cooldown_s = float(
                getattr(
                    self._memory_settings,
                    "idle_seed_surface_cooldown_seconds",
                    1800,
                )
            )
            if cooldown_s > 0:
                try:
                    raw_clock = chat_db.kv_get("idle_seed.surfaced_clock")
                except Exception:
                    raw_clock = None
                if raw_clock:
                    try:
                        last = datetime.fromisoformat(str(raw_clock))
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        elapsed = (
                            timephrase.utcnow() - last
                        ).total_seconds()
                        if elapsed < cooldown_s:
                            log.debug(
                                "idle_seed silent: cooldown %.0fs < %.0fs",
                                elapsed,
                                cooldown_s,
                            )
                            return ""
                    except Exception:
                        pass

        # Advance the per-seed watermark + the surfacing clock.
        try:
            from datetime import datetime, timezone

            chat_db.kv_set(watermark_key, at)
            chat_db.kv_set(
                "idle_seed.surfaced_clock",
                timephrase.utcnow().isoformat(timespec="seconds"),
            )
        except Exception:
            log.debug("idle_seed watermark write failed", exc_info=True)

        activity = str(newest.get("activity") or "").replace("_", " ").strip()
        log.info("idle-seed fire: at=%s activity=%s", at, activity)
        if activity:
            lead = f"Earlier, while you were {activity}, "
        else:
            lead = "Earlier, during some quiet time, "
        return (
            f"{lead}a thought crossed your mind: {seed} "
            "If it fits naturally you can bring it up — no need to force it."
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

        force_next = bool(
            self._debug_overrides.take("follow_up_force_next", False)
        )

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

    def _render_growth_witness_block(self) -> str:
        """K70: surface one rare "you've grown since we met" cue.

        Consumer side of the :class:`GrowthWitnessWorker` producer. The
        worker drafts a finding (lighter mood / more comfortable / more
        open) into the ``aiko.growth_witness`` kv ring at most once every
        couple of weeks; this provider folds the newest unseen finding
        into the prompt as one optional, private cue Aiko phrases herself
        — NEVER spoken verbatim.

        Watermark-only (``growth_witness.last_surfaced_at``), independent
        of the gap-return cue family: a durable longitudinal observation
        is rare enough that surfacing it on the next turn after drafting
        is fine, and the cue copy tells Aiko to wait for a warm moment.
        MCP debug: ``force_growth_witness_surface`` arms
        ``_growth_witness_force_next`` to bypass the watermark (the ring
        still has to be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "growth_witness_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("growth_witness_force_next", False)
        )

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.relationship import growth_witness as _gw
        except Exception:
            log.debug("growth_witness import failed", exc_info=True)
            return ""

        ring = _gw.load_findings(chat_db.kv_get)
        if not ring:
            return ""

        newest = ring[-1]
        at = str(newest.get("at") or "")
        kind = str(newest.get("kind") or "").strip()
        if not kind:
            return ""

        watermark_key = "growth_witness.last_surfaced_at"
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                return ""

        line = _gw.render_inner_life_block(
            kind,
            user_display_name=self.user_display_name,
            span_days=int(newest.get("span_days") or 0),
            detail=str(newest.get("detail") or ""),
        )
        if not line:
            return ""

        # Advance the watermark so this cue doesn't resurface (only once
        # we know it actually rendered).
        try:
            chat_db.kv_set(watermark_key, at)
        except Exception:
            log.debug("growth_witness watermark write failed", exc_info=True)

        log.info("growth-witness fire: at=%s kind=%s", at, kind)
        return line

    def _render_aspiration_momentum_block(self) -> str:
        """L14: surface one occasional "check in on where they're heading" cue.

        Consumer side of the :class:`AspirationMomentumWorker` producer. The
        worker drafts a cue (an active aspiration gone stale enough to be worth
        revisiting) into the ``aiko.aspiration_momentum`` kv ring; this provider
        folds the newest unseen cue into the prompt as one optional, private
        hint Aiko phrases herself — NEVER spoken verbatim, never a nudge.

        Watermark-only (``aspiration_momentum.last_surfaced_at``), sibling of
        the growth_witness / self_callback cue family. MCP debug:
        ``force_aspiration_momentum_surface`` arms
        ``_aspiration_momentum_force_next`` to bypass the watermark (the ring
        still has to be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "aspiration_momentum_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("aspiration_momentum_force_next", False)
        )

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.proactive import aspiration_momentum as _am
        except Exception:
            log.debug("aspiration_momentum import failed", exc_info=True)
            return ""

        ring = _am.load_cues(chat_db.kv_get)
        if not ring:
            return ""

        newest = ring[-1]
        at = str(newest.get("at") or "")
        label = str(newest.get("label") or "").strip()
        subject = str(newest.get("subject") or "user").strip() or "user"
        if not label:
            return ""

        watermark_key = "aspiration_momentum.last_surfaced_at"
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                return ""

        line = _am.render_inner_life_block(
            subject,
            label,
            user_display_name=self.user_display_name,
        )
        if not line:
            return ""

        try:
            chat_db.kv_set(watermark_key, at)
        except Exception:
            log.debug(
                "aspiration_momentum watermark write failed", exc_info=True
            )

        log.info("aspiration-momentum fire: at=%s subject=%s", at, subject)
        return line

    def _render_tension_block(self) -> str:
        """L12: surface one rare, gentle "a friction worth sitting with" cue.

        Consumer side of the :class:`TensionCueWorker` producer. The worker
        drafts a cue (a confident, off-cooldown active tension concept) into the
        ``aiko.tension_cue`` kv ring; this provider folds the newest unseen cue
        into the prompt as one optional, private observation Aiko phrases
        herself -- NEVER spoken verbatim, never a confrontation.

        Since H10 this is no longer a tension's only surface: the T3 flex lane
        renders one when the turn calls for it. So the cue steps aside for a
        tension that lane has already claimed this turn -- the point of the
        cue is that a friction gets raised *once*, carefully, and raising the
        same one twice in a single assembly is the nagging the whole design
        is built to avoid. The two surfaces are complements: T3 answers "this
        friction is live in what he just said", the cue answers "this one has
        been sitting unsaid for a while".

        Watermark-only (``tension_cue.last_surfaced_at``), sibling of the
        aspiration_momentum / self_callback cue family. MCP debug:
        ``force_tension_surface`` arms ``_tension_force_next`` to bypass the
        watermark (the ring still has to be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "tension_cue_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("tension_force_next", False)
        )

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        try:
            from app.core.proactive import tension_cue as _tc
        except Exception:
            log.debug("tension_cue import failed", exc_info=True)
            return ""

        ring = _tc.load_cues(chat_db.kv_get)
        if not ring:
            return ""

        newest = ring[-1]
        at = str(newest.get("at") or "")
        label = str(newest.get("label") or "").strip()
        subject = str(newest.get("subject") or "user").strip() or "user"
        if not label:
            return ""

        try:
            concept_id = int(newest.get("concept_id") or 0)
        except (TypeError, ValueError):
            concept_id = 0

        # H10: the T3 concept lane assembles first and may already be
        # carrying this exact friction. Left standing rather than consumed:
        # the cue keeps its place in the ring and its watermark, so it gets
        # said on a turn where it is the only voice raising it.
        claimed = getattr(self, "_last_context_concept_ids", None) or ()
        if concept_id > 0 and concept_id in claimed:
            log.debug(
                "tension-cue yield: concept=%d already in relevant_context",
                concept_id,
            )
            return ""

        watermark_key = _tc.KV_LAST_SURFACED_AT
        if not force_next:
            try:
                last_surfaced = chat_db.kv_get(watermark_key)
            except Exception:
                last_surfaced = None
            if last_surfaced and str(last_surfaced) == at:
                return ""

        line = _tc.render_inner_life_block(
            subject,
            label,
            user_display_name=self.user_display_name,
        )
        if not line:
            return ""

        try:
            chat_db.kv_set(watermark_key, at)
        except Exception:
            log.debug("tension_cue watermark write failed", exc_info=True)

        # The six-day per-tension cooldown is spent here, not at draft time:
        # it paces how often she raises a given friction *out loud*, and a
        # cue that never rendered was never raised. Stamping it in the
        # producer silenced tensions she had not actually mentioned.
        if concept_id > 0:
            try:
                chat_db.kv_set(
                    _tc.per_concept_cooldown_key(concept_id), at
                )
            except Exception:
                log.debug(
                    "tension_cue cooldown write failed", exc_info=True
                )

        log.info(
            "tension-cue fire: at=%s subject=%s concept=%d",
            at, subject, concept_id,
        )
        return line

    def _render_self_callback_block(self) -> str:
        """K71: surface one rare "close the loop on my own past" cue.

        Consumer side of the :class:`SelfCallbackWorker` producer. The
        worker queues an aged feeling / intention of Aiko's own into the
        pool; this claims one so she revisits it in her own words (the
        resolution read -- eased? followed through? -- is the model's,
        using her current affect in context). NEVER spoken verbatim.

        Independent of the gap-return cue family. MCP debug:
        ``force_self_callback_surface`` arms ``_self_callback_force_next``,
        which now bypasses the type's surfacing cadence rather than a
        watermark.

        The cadence is what replaced that watermark, and it is a better
        fit for what this cue is. A watermark says "each drafted callback
        gets exactly one showing", which quietly meant a callback Aiko
        read straight past was gone for good -- indistinguishable from one
        she acted on. The pool keeps it for another try while
        ``surface_cooldown_hours`` keeps the *type* as rare as it always
        was.
        """
        if not bool(
            getattr(self._settings.agent, "self_callback_enabled", True)
        ):
            return ""
        force_next = bool(
            self._debug_overrides.take("self_callback_force_next", False)
        )
        row = self.take_pool_cue("self_callback", force=force_next)
        if row is None:
            return ""
        log.info(
            "self-callback fire: cue=%d id=%s",
            row.id, row.payload.get("memory_id"),
        )
        return row.text

    def _render_second_thought_block(self, user_text: str = "") -> str:
        """K96: surface one loose end the post-reply think pass wrote down.

        Consumer side of :class:`SecondThoughtWorker`. The pass runs after
        a reply has already gone out, notices what she under-answered or
        walked past, and queues it; this hands it back to her on a later
        turn so a thought can outlive the turn that produced it.

        ``user_text`` is passed through to rank the shelf rather than to
        gate it, and the distinction is the design. A hard relevance
        predicate (``long_arc_callback``'s shape) is right for a
        month-old memory, where reopening it against an unrelated message
        is worse than dropping it. It is wrong here: "going back to what
        you said earlier -- I brushed past that" is a move that *works*
        when the conversation has drifted, and is in fact the only time it
        is worth making. So relevance decides WHICH loose end she is
        handed, and ``surface_cooldown_hours`` decides how often she is
        handed one at all.
        """
        if not bool(
            getattr(self._settings.agent, "second_thought_enabled", False)
        ):
            return ""
        force_next = bool(
            self._debug_overrides.take("second_thought_force_next", False)
        )
        row = self.take_pool_cue(
            "second_thought", force=force_next, user_text=user_text or "",
        )
        if row is None:
            return ""
        log.info(
            "second-thought fire: cue=%d subject=%r",
            row.id, row.payload.get("subject", ""),
        )
        return row.text

    def _render_wellbeing_concern_block(self) -> str:
        """K72: surface one rare, gentle "you doing okay?" concern cue.

        Consumer side of the :class:`WellbeingConcernWorker` producer. The
        worker reads multi-day behavioral signal (small-hours activity,
        explicit "haven't slept / eaten" mentions, a heavy H3 low stretch)
        and, when a real worrying pattern clears a high bar, queues one
        cue. This provider claims it as one optional, private line Aiko
        phrases herself -- offered as care, one soft check-in, dropped the
        instant he deflects. NEVER spoken verbatim.

        Rarity is the type's ``surface_cooldown_hours`` (a week), and it
        gates opening a *new* concern only: a concern she was shown and
        never voiced is still owed, so its retry is not made to wait out
        the week. MCP debug: ``force_wellbeing_concern_surface`` arms
        ``wellbeing_concern_force_next`` (the pool must hold a cue).
        """
        if not bool(
            getattr(self._settings.agent, "wellbeing_concern_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("wellbeing_concern_force_next", False)
        )
        row = self.take_pool_cue("wellbeing_concern", force=force_next)
        if row is None:
            return ""
        log.info(
            "wellbeing-concern fire: cue=%s kind=%s",
            row.id, row.payload.get("kind"),
        )
        return row.text

    def _render_shared_ritual_block(self) -> str:
        """K73: surface one warm "this has become our thing" cue.

        Consumer side of the :class:`SharedRitualWorker` producer. The
        worker names dyadic ``(cadence, shape)`` rituals into
        ``aiko.shared_rituals`` and queues an offer for the strongest one
        still unnamed; this provider claims it as a warm, optional
        acknowledgment Aiko phrases herself. NEVER spoken verbatim.

        Spacing is the type's ``surface_cooldown_hours`` (three days), so
        a burst of newly-qualified rituals cannot announce back-to-back.
        MCP debug: ``force_shared_ritual_surface`` arms
        ``shared_ritual_force_next`` (the pool must hold a cue).
        """
        if not bool(
            getattr(self._settings.agent, "shared_ritual_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("shared_ritual_force_next", False)
        )
        row = self.take_pool_cue("shared_ritual", force=force_next)
        if row is None:
            return ""
        log.info(
            "shared-ritual fire: cue=%s key=%s label=%r",
            row.id, row.payload.get("key"), row.subject,
        )
        return row.text

    @staticmethod
    def _parse_iso_or_none(value: object):
        """Best-effort ISO-8601 parse to an aware datetime, else ``None``."""
        from datetime import datetime, timezone

        if not isinstance(value, str) or not value.strip():
            return None
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _render_upcoming_horizon_block(self) -> str:
        """K-time3: surface a "coming up" heads-up with pre-resolved times.

        A cheap forward sweep over ``future_plan`` memories whose
        ``event_time`` falls within ``memory.upcoming_horizon_days`` of now,
        rendered as one terse cue with the relative phrasing **already
        worked out** by :mod:`app.core.infra.timephrase` — so the chat model
        never recomputes a future date (the thing LLMs reliably get wrong).
        This is the missing *forward sweep*: ``rag_retriever`` only tags a
        future plan with its resolved time if semantic RAG happens to surface
        it; here it surfaces by time, not relevance.

        Anti-nag: the cue re-surfaces the moment the upcoming set *changes*
        (a new plan appears, or one slides out of the window), but otherwise
        sits out a per-turn cooldown (``upcoming_horizon_cooldown_turns``) so
        an unchanged calendar isn't recited every turn. Computed live (no
        worker / kv): a single mirror scan + a couple of ISO parses.

        MCP debug: ``force_upcoming_horizon_surface`` arms
        ``_upcoming_horizon_force_next`` to bypass the cooldown + signature
        gate (the window must still hold at least one plan).
        """
        if not bool(
            getattr(self._settings.agent, "upcoming_horizon_enabled", True)
        ):
            return ""
        store = getattr(self, "_memory_store", None)
        if store is None:
            return ""

        force = bool(
            self._debug_overrides.take("upcoming_horizon_force_next", False)
        )

        try:
            from app.core.conversation.upcoming_horizon import (
                build_signature,
                render_block,
                select_upcoming,
            )
            from app.core.infra import timephrase
        except Exception:
            log.debug("upcoming_horizon import failed", exc_info=True)
            return ""

        mem_settings = self._memory_settings
        horizon_days = int(
            getattr(mem_settings, "upcoming_horizon_days", 7)
        )
        max_items = int(
            getattr(mem_settings, "upcoming_horizon_max_items", 3)
        )

        now = timephrase.now()
        try:
            candidates = store.list_by_temporal_type("future_plan")
        except Exception:
            log.debug("upcoming_horizon: list future_plan failed", exc_info=True)
            return ""

        events = select_upcoming(
            candidates, now, horizon_days=horizon_days, max_items=max_items,
        )
        if not events:
            # Nothing on the horizon: forget the last signature so a plan
            # that appears later always reads as "new" and surfaces fresh.
            self._upcoming_horizon_sig = ""
            return ""

        sig = build_signature(events)
        last_sig = getattr(self, "_upcoming_horizon_sig", "")
        cooldown = int(getattr(self, "_upcoming_horizon_cooldown", 0) or 0)
        if not force and sig == last_sig and cooldown > 0:
            self._upcoming_horizon_cooldown = cooldown - 1
            return ""

        line = render_block(events, now, self.user_display_name)
        if not line:
            return ""

        self._upcoming_horizon_sig = sig
        self._upcoming_horizon_cooldown = max(
            0, int(getattr(mem_settings, "upcoming_horizon_cooldown_turns", 6))
        )
        log.info(
            "upcoming-horizon fire: count=%d cooldown=%d sig=%s",
            len(events),
            self._upcoming_horizon_cooldown,
            sig[:80],
        )
        return line

    def _render_knowledge_gap_notice_block(self, user_text: str) -> str:
        """F10f: surface one "I keep circling X but never dug in" cue.

        Consumer side of the
        :class:`~app.core.proactive.knowledge_gap_notice_worker.KnowledgeGapNoticeWorker`
        producer. The worker drafts dense-but-unresearched topics into the
        ``aiko.knowledge_gap_notices`` kv ring during quiet windows; this
        provider surfaces one **only when the live turn is actually on that
        topic** (lexical overlap with ``user_text``), so the beat lands in
        context — "oh, this again; honestly I still don't know much about
        it" — rather than as a standalone non-sequitur.

        The pool retires a notice once Aiko owns the gap out loud, and
        after at most ``max_surfacings`` turns of her ignoring it (the
        worker's per-topic cooldown also stops it being re-drafted). The
        cue is a private prompt hint, NEVER spoken verbatim — Aiko phrases
        the admission herself. Independent of the gap-return cue family
        (does not touch ``_gap_cue_surfaced``); it's tied to the live topic,
        not to a long-absence return. MCP debug: ``force_knowledge_gap_notice_surface``
        arms ``_knowledge_gap_notice_force_next`` to bypass the
        topic-relevance gate (there must still be a cue to serve).

        Falls back to the older ``aiko.knowledge_gap_notices`` ring when no
        cue pool is wired, which is also the only mode where a cue is
        retired on render rather than on Aiko actually using it.
        """
        if not bool(
            getattr(self._settings.agent, "knowledge_gap_notice_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("knowledge_gap_notice_force_next", False)
        )

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        text = (user_text or "").strip()
        if not text and not force_next:
            return ""

        try:
            from app.core.proactive.knowledge_gap_notice_worker import (
                load_notices,
                render_notice_cue,
                topic_relevant,
            )
        except Exception:
            log.debug("knowledge_gap_notice import failed", exc_info=True)
            return ""

        pool = getattr(self, "_cue_store", None)
        if pool is not None:
            row = self.take_pool_cue(
                "knowledge_gap_notice",
                relevant=lambda payload: topic_relevant(
                    str(payload.get("topic") or ""),
                    text,
                    options=self._topic_gate_options(),
                ),
                force=force_next,
                user_text=text,
            )
            if row is None:
                return ""
            log.info("knowledge-gap-notice fire: topic=%r", row.subject[:80])
            return row.text

        ring = load_notices(chat_db.kv_get)
        if not ring:
            return ""

        surfaced_key = "knowledge_gap_notice.surfaced_keys"
        try:
            raw = chat_db.kv_get(surfaced_key)
            surfaced = set(json.loads(raw)) if raw else set()
        except Exception:
            surfaced = set()

        chosen: dict | None = None
        for entry in reversed(ring):  # newest first
            key = str(entry.get("cluster_key") or "")
            topic = str(entry.get("topic") or "").strip()
            if not topic:
                continue
            if not force_next:
                if key and key in surfaced:
                    continue
                if not topic_relevant(
                    topic, text, options=self._topic_gate_options(),
                ):
                    continue
            chosen = entry
            break
        if chosen is None:
            return ""

        key = str(chosen.get("cluster_key") or "")
        topic = str(chosen.get("topic") or "").strip()
        if key:
            surfaced.add(key)
            try:
                # Cap the surfaced set so it can't grow unbounded.
                trimmed = list(surfaced)[-64:]
                chat_db.kv_set(surfaced_key, json.dumps(trimmed))
            except Exception:
                log.debug(
                    "knowledge_gap_notice surfaced write failed", exc_info=True
                )

        log.info("knowledge-gap-notice fire: topic=%r key=%s", topic[:80], key)
        return render_notice_cue(topic)

    def _render_associative_wander_block(self, user_text: str) -> str:
        """K64a: surface one "funny, this reminds me of ..." connection.

        Consumer side of the
        :class:`~app.core.proactive.associative_wander_worker.AssociativeWanderWorker`
        producer. The worker drifts across the topic graph during quiet
        windows and drafts a genuine connection between two *distant*
        clusters into the ``aiko.associative_wanders`` kv ring; this
        provider surfaces one **only when the live turn is actually on one
        of the two topics** (lexical overlap with ``user_text``), so the
        drift lands in context — "oh, this reminds me of ..." — rather than
        as a non-sequitur.

        One-shot per pair: a surfaced ``pair_key`` is recorded in
        ``associative_wander.surfaced_keys`` and never resurfaces (the
        worker's per-pair cooldown also stops it being re-drafted). The cue
        is a private prompt hint, NEVER spoken verbatim — Aiko decides
        whether the connection fits and phrases it herself. Independent of
        the gap-return cue family (does not touch ``_gap_cue_surfaced``);
        it's tied to the live topic. MCP debug:
        ``force_associative_wander_surface`` arms
        ``_associative_wander_force_next`` to bypass the topic-relevance +
        surfaced gates (the ring must still be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "associative_wander_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("associative_wander_force_next", False)
        )

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        text = (user_text or "").strip()
        if not text and not force_next:
            return ""

        try:
            from app.core.proactive.associative_wander_worker import (
                distant_half,
                load_wanders,
                render_wander_cue,
                wander_relevant,
            )
        except Exception:
            log.debug("associative_wander import failed", exc_info=True)
            return ""

        pool = getattr(self, "_cue_store", None)
        if pool is not None:
            row = self.take_pool_cue(
                "associative_wander",
                relevant=lambda payload: wander_relevant(
                    payload, text, options=self._topic_gate_options(),
                ),
                force=force_next,
                user_text=text,
            )
            if row is None:
                return ""
            # Post-turn asks "did she pivot onto the far topic", and which
            # topic that is depends on the turn we are in right now -- so
            # it is recorded here rather than at production time.
            row.payload["match_subject"] = distant_half(
                row.payload, text, options=self._topic_gate_options(),
            )
            log.info(
                "associative-wander fire: a=%r b=%r",
                str(row.payload.get("topic_a") or "")[:60],
                str(row.payload.get("topic_b") or "")[:60],
            )
            return row.text

        ring = load_wanders(chat_db.kv_get)
        if not ring:
            return ""

        surfaced_key = "associative_wander.surfaced_keys"
        try:
            raw = chat_db.kv_get(surfaced_key)
            surfaced = set(json.loads(raw)) if raw else set()
        except Exception:
            surfaced = set()

        chosen: dict | None = None
        for entry in reversed(ring):  # newest first
            key = str(entry.get("pair_key") or "")
            connection = str(entry.get("connection") or "").strip()
            if not connection:
                continue
            if not force_next:
                if key and key in surfaced:
                    continue
                if not wander_relevant(
                    entry, text, options=self._topic_gate_options(),
                ):
                    continue
            chosen = entry
            break
        if chosen is None:
            return ""

        key = str(chosen.get("pair_key") or "")
        topic_a = str(chosen.get("topic_a") or "").strip()
        topic_b = str(chosen.get("topic_b") or "").strip()
        connection = str(chosen.get("connection") or "").strip()
        if key:
            surfaced.add(key)
            try:
                trimmed = list(surfaced)[-64:]
                chat_db.kv_set(surfaced_key, json.dumps(trimmed))
            except Exception:
                log.debug(
                    "associative_wander surfaced write failed", exc_info=True
                )

        log.info(
            "associative-wander fire: a=%r b=%r key=%s",
            topic_a[:60], topic_b[:60], key,
        )
        return render_wander_cue(chosen)

    def _render_interest_drift_block(self, user_text: str) -> str:
        """K64b: surface one "I've been drawn to X lately" register shift.

        Consumer side of the
        :class:`~app.core.proactive.interest_drift_worker.InterestDriftWorker`
        producer. The worker tracks each topic cluster's mass over time and
        drafts a drift (``rising`` / ``fading``) into the
        ``aiko.interest_drifts`` kv ring during quiet windows; this provider
        surfaces one **only when the live turn is actually on that topic**
        (lexical overlap with ``user_text``), so the slow self-aware beat
        lands in context — "funny, I've found myself drawn to this more
        lately" — rather than as a non-sequitur.

        One-shot per topic: a surfaced ``topic_key`` is recorded in
        ``interest_drift.surfaced_keys`` and never resurfaces (the worker's
        per-topic cooldown also stops it being re-drafted). The cue is a
        private prompt hint, NEVER spoken verbatim — it's a register, not a
        line. Independent of the gap-return cue family (does not touch
        ``_gap_cue_surfaced``); tied to the live topic. MCP debug:
        ``force_interest_drift_surface`` arms ``_interest_drift_force_next``
        to bypass the topic-relevance + surfaced gates (the ring must still
        be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "interest_drift_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("interest_drift_force_next", False)
        )

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        text = (user_text or "").strip()
        if not text and not force_next:
            return ""

        try:
            from app.core.proactive.interest_drift_worker import (
                drift_relevant,
                load_drifts,
                render_drift_cue,
            )
        except Exception:
            log.debug("interest_drift import failed", exc_info=True)
            return ""

        if getattr(self, "_cue_store", None) is not None:
            row = self.take_pool_cue(
                "interest_drift",
                relevant=lambda payload: drift_relevant(
                    payload, text, options=self._topic_gate_options(),
                ),
                force=force_next,
                user_text=text,
            )
            if row is None:
                return ""
            log.info(
                "interest-drift fire: topic=%r dir=%s",
                row.subject[:60], row.payload.get("direction"),
            )
            return row.text

        ring = load_drifts(chat_db.kv_get)
        if not ring:
            return ""

        surfaced_key = "interest_drift.surfaced_keys"
        try:
            raw = chat_db.kv_get(surfaced_key)
            surfaced = set(json.loads(raw)) if raw else set()
        except Exception:
            surfaced = set()

        chosen: dict | None = None
        for entry in reversed(ring):  # newest first
            key = str(entry.get("topic_key") or "")
            topic = str(entry.get("topic") or "").strip()
            if not topic:
                continue
            if not force_next:
                if key and key in surfaced:
                    continue
                if not drift_relevant(
                    entry, text, options=self._topic_gate_options(),
                ):
                    continue
            chosen = entry
            break
        if chosen is None:
            return ""

        key = str(chosen.get("topic_key") or "")
        topic = str(chosen.get("topic") or "").strip()
        direction = str(chosen.get("direction") or "").strip()
        if key:
            surfaced.add(key)
            try:
                trimmed = list(surfaced)[-64:]
                chat_db.kv_set(surfaced_key, json.dumps(trimmed))
            except Exception:
                log.debug(
                    "interest_drift surfaced write failed", exc_info=True
                )

        log.info(
            "interest-drift fire: topic=%r dir=%s key=%s",
            topic[:60], direction, key,
        )
        return render_drift_cue(chosen)

    def _render_dormant_interest_block(self) -> str:
        """K67: gently re-open a once-loved topic that's gone quiet.

        Consumer side of the
        :class:`~app.core.proactive.dormant_interest_worker.DormantInterestWorker`
        producer. The worker finds a topic cluster that was once a genuine,
        high-mass user interest and has since gone silent for weeks, and
        drafts it into the ``aiko.dormant_interests`` kv ring during quiet
        windows. This provider surfaces one **only on a natural conversational
        lull** (the K18 ``TopicStagnationDetector`` standing reading dips below
        the mild-stagnation threshold) — the dormant interest by definition
        isn't the live topic, so unlike the K64b drift cue this reaches for
        something *off* the current thread, which is exactly why it waits for a
        lull rather than topic-relevance.

        Rare and warm by construction: the pool serves each cue at most
        ``max_surfacings`` times and retires it once she reaches for it,
        plus a long wall-clock surfacing cooldown across ALL topics
        (``dormant_interest.surfaced_clock``) so even with several
        re-openers queued the beat stays occasional. The cue is a private
        prompt hint, NEVER spoken verbatim — the chat model phrases the actual
        re-opener. MCP debug: ``force_dormant_interest_surface`` arms
        ``_dormant_interest_force_next`` to bypass the lull + cooldown +
        surfaced gates (there must still be a cue to serve).

        Reads the cue pool when one is wired, and falls back to the older
        ``aiko.dormant_interests`` ring otherwise, so an install (or a test)
        without a pool keeps its cues. Only the pool path can tell whether
        Aiko actually took the re-opener; the ring path retires a cue on
        render, as it always did.
        """
        if not bool(
            getattr(self._settings.agent, "dormant_interest_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("dormant_interest_force_next", False)
        )

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        # Natural-lull gate (same standing reading K54 consumes). When the
        # window hasn't filled or the conversation is still moving, hold —
        # a re-opener only lands on a real quiet beat.
        if not force_next:
            from app.core.conversation.topic_stagnation import in_standing_lull

            if not in_standing_lull(
                getattr(self, "_topic_stagnation_detector", None),
                self._memory_settings,
            ):
                # Not ``cadence_block``: a clock resolves itself by
                # waiting, whereas a lull may never arrive. Reporting both
                # as one reason is what left this cue's 1-surfacing-in-96
                # unexplained -- it read as an over-long cooldown when the
                # question was whether the room ever goes quiet.
                note_decline(self, "dormant_interest", REASON_NO_OPENING)
                return ""

        try:
            from app.core.proactive.dormant_interest_worker import (
                load_dormant,
                render_dormant_cue,
            )
        except Exception:
            log.debug("dormant_interest import failed", exc_info=True)
            return ""

        pool = getattr(self, "_cue_store", None)
        ring = [] if pool is not None else load_dormant(chat_db.kv_get)
        if pool is None and not ring:
            return ""

        # Wall-clock surfacing cooldown across all topics — keeps the beat
        # occasional even when several re-openers are queued.
        clock_key = "dormant_interest.surfaced_clock"
        if not force_next:

            cooldown_h = float(
                getattr(
                    self._memory_settings,
                    "dormant_interest_surface_cooldown_hours",
                    24.0,
                )
            )
            if cooldown_h > 0:
                last = _parse_dt_utc(chat_db.kv_get(clock_key))
                if last is not None:
                    elapsed_h = (
                        timephrase.utcnow() - last
                    ).total_seconds() / 3600.0
                    if elapsed_h < cooldown_h:
                        note_decline(
                            self, "dormant_interest", REASON_CADENCE_BLOCK
                        )
                        return ""

        if pool is not None:
            row = self.take_pool_cue("dormant_interest", force=force_next)
            if row is None:
                return ""
            topic = row.subject
            text = row.text
        else:
            surfaced_key = "dormant_interest.surfaced_keys"
            try:
                raw = chat_db.kv_get(surfaced_key)
                surfaced = set(json.loads(raw)) if raw else set()
            except Exception:
                surfaced = set()

            chosen: dict | None = None
            for entry in reversed(ring):  # newest first
                key = str(entry.get("topic_key") or "")
                topic = str(entry.get("topic") or "").strip()
                if not topic:
                    continue
                if not force_next and key and key in surfaced:
                    continue
                chosen = entry
                break
            if chosen is None:
                return ""

            key = str(chosen.get("topic_key") or "")
            topic = str(chosen.get("topic") or "").strip()
            if key:
                surfaced.add(key)
                try:
                    trimmed = list(surfaced)[-64:]
                    chat_db.kv_set(surfaced_key, json.dumps(trimmed))
                except Exception:
                    log.debug(
                        "dormant_interest surfaced write failed", exc_info=True
                    )
            text = render_dormant_cue(topic)

        try:
            chat_db.kv_set(
                clock_key,
                timephrase.utcnow().isoformat(timespec="seconds"),
            )
        except Exception:
            log.debug("dormant_interest clock write failed", exc_info=True)

        log.info("dormant-interest fire: topic=%r", topic[:60])
        return text

    def _render_curiosity_gradient_block(self, user_text: str) -> str:
        """K64c: surface one "I keep brushing past X, I'm curious" edge.

        Consumer side of the
        :class:`~app.core.proactive.curiosity_gradient_worker.CuriosityGradientWorker`
        producer. The worker finds a *thin* topic cluster sitting right next
        to a *dense* one (the under-explored edge of familiar territory) and
        drafts it into the ``aiko.curiosity_gradients`` kv ring during quiet
        windows; this provider surfaces one **only when the live turn is on
        either topic** (lexical overlap with ``user_text``), so the curious
        beat lands in context — "we talk about X all the time, but I realise
        I've never actually asked you about Y".

        One-shot per edge: a surfaced ``edge_key`` is recorded in
        ``curiosity_gradient.surfaced_keys`` and never resurfaces (the
        worker's per-edge cooldown also stops it being re-drafted). The cue
        is a private prompt hint, NEVER spoken verbatim — the chat model
        phrases the actual question. Independent of the gap-return cue family
        (does not touch ``_gap_cue_surfaced``); tied to the live topic. MCP
        debug: ``force_curiosity_gradient_surface`` arms
        ``_curiosity_gradient_force_next`` to bypass the topic-relevance +
        surfaced gates (the ring must still be non-empty).
        """
        if not bool(
            getattr(self._settings.agent, "curiosity_gradient_enabled", True)
        ):
            return ""

        force_next = bool(
            self._debug_overrides.take("curiosity_gradient_force_next", False)
        )

        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None or not hasattr(chat_db, "kv_get"):
            return ""

        text = (user_text or "").strip()
        if not text and not force_next:
            return ""

        try:
            from app.core.proactive.curiosity_gradient_worker import (
                gradient_relevant,
                load_gradients,
                render_gradient_cue,
            )
        except Exception:
            log.debug("curiosity_gradient import failed", exc_info=True)
            return ""

        pool = getattr(self, "_cue_store", None)
        if pool is not None:
            row = self.take_pool_cue(
                "curiosity_gradient",
                relevant=lambda payload: gradient_relevant(
                    payload, text, options=self._topic_gate_options(),
                ),
                force=force_next,
                user_text=text,
            )
            if row is None:
                return ""
            log.info(
                "curiosity-gradient fire: dense=%r thin=%r",
                str(row.payload.get("dense_topic") or "")[:60],
                str(row.payload.get("thin_topic") or "")[:60],
            )
            return row.text

        ring = load_gradients(chat_db.kv_get)
        if not ring:
            return ""

        surfaced_key = "curiosity_gradient.surfaced_keys"
        try:
            raw = chat_db.kv_get(surfaced_key)
            surfaced = set(json.loads(raw)) if raw else set()
        except Exception:
            surfaced = set()

        chosen: dict | None = None
        for entry in reversed(ring):  # newest first
            key = str(entry.get("edge_key") or "")
            thin = str(entry.get("thin_topic") or "").strip()
            if not thin:
                continue
            if not force_next:
                if key and key in surfaced:
                    continue
                if not gradient_relevant(
                    entry, text, options=self._topic_gate_options(),
                ):
                    continue
            chosen = entry
            break
        if chosen is None:
            return ""

        key = str(chosen.get("edge_key") or "")
        dense = str(chosen.get("dense_topic") or "").strip()
        thin = str(chosen.get("thin_topic") or "").strip()
        if key:
            surfaced.add(key)
            try:
                trimmed = list(surfaced)[-64:]
                chat_db.kv_set(surfaced_key, json.dumps(trimmed))
            except Exception:
                log.debug(
                    "curiosity_gradient surfaced write failed", exc_info=True
                )

        log.info(
            "curiosity-gradient fire: dense=%r thin=%r key=%s",
            dense[:60], thin[:60], key,
        )
        return render_gradient_cue(chosen)

    def _render_topic_temperature_block(self, user_text: str) -> str:
        """F10h: nudge tone when the live turn lands on a *charged* topic.

        Maps ``user_text`` to its nearest topic cluster
        (``TopicGraph.best_clusters_for`` — centroid dot products over the
        live embedding), gathers the ``vibe`` tags of that cluster's
        ``shared_moment`` members, and scores a per-cluster emotional
        temperature
        (:func:`~app.core.conversation.topic_temperature.score_cluster`).
        When the cluster reads **warm** (good moments live here) or
        **tender** (vulnerable / patched-up ground), it surfaces one
        private Heads-up line so Aiko meets the topic with the right
        register instead of flat. A topic-scoped sibling of the
        relationship-axes block.

        Computed live (no worker / kv): shared moments are few, and the
        per-turn cost is one embed (usually a cache hit, since novelty /
        knowledge-grounding embed the same ``user_text``) plus a handful
        of centroid dots and a member walk over the *one* matched cluster.
        Paced by a global turn cooldown so a charged topic isn't re-nudged
        every turn. MCP debug: ``force_topic_temperature_surface`` arms
        ``_topic_temperature_force_next`` to bypass the cooldown + the
        similarity / charge thresholds (the cluster must still have at
        least one vibed shared moment).
        """
        if not bool(
            getattr(self._settings.agent, "topic_temperature_enabled", True)
        ):
            return ""
        text = (user_text or "").strip()
        if len(text) < 8:
            return ""
        graph = getattr(self, "_topic_graph", None)
        embedder = getattr(self, "_embedder", None)
        store = getattr(self, "_memory_store", None)
        if graph is None or embedder is None or store is None:
            return ""
        if not bool(getattr(graph, "persistent", False)):
            return ""

        force = bool(
            self._debug_overrides.take("topic_temperature_force_next", False)
        )

        cooldown = int(getattr(self, "_topic_temperature_cooldown", 0) or 0)
        if cooldown > 0 and not force:
            self._topic_temperature_cooldown = cooldown - 1
            return ""

        mem_settings = self._memory_settings
        min_sim = float(
            getattr(mem_settings, "topic_temperature_min_sim", 0.45)
        )
        threshold = float(
            getattr(mem_settings, "topic_temperature_threshold", 0.5)
        )

        try:
            qvec = embedder.embed(text)
        except Exception:
            log.debug("topic-temperature: embed failed", exc_info=True)
            return ""
        try:
            matches = graph.best_clusters_for(
                qvec, top_n=1, min_sim=(0.0 if force else min_sim),
            )
        except Exception:
            log.debug("topic-temperature: best_clusters_for failed", exc_info=True)
            return ""
        if not matches:
            return ""
        cid, label, _sim = matches[0]

        try:
            member_ids = graph.cluster_member_ids(cid)
        except Exception:
            log.debug("topic-temperature: member walk failed", exc_info=True)
            return ""
        from app.core.conversation.topic_temperature import (
            MomentCandidate,
            render_block,
            score_cluster,
        )

        vibes: list[str] = []
        candidates: list[MomentCandidate] = []
        for mid in member_ids:
            mem = store.get(mid)
            if mem is None or getattr(mem, "kind", "") != "shared_moment":
                continue
            meta = getattr(mem, "metadata", None) or {}
            if not isinstance(meta, dict):
                continue
            vibe = meta.get("vibe")
            if not vibe:
                continue
            vibes.append(str(vibe))
            # H8: keep the moment's summary so we can later name the
            # origin of the topic's feel.
            what = str(
                meta.get("what") or getattr(mem, "content", "") or ""
            ).strip()
            candidates.append(
                MomentCandidate(
                    moment_id=int(getattr(mem, "id", 0) or 0),
                    vibe=str(vibe),
                    what=what,
                    when=str(meta.get("when") or ""),
                    created_at=str(getattr(mem, "created_at", "") or ""),
                )
            )
        if not vibes:
            return ""

        temp = score_cluster(vibes, threshold=(0.0 if force else threshold))
        if temp.dominant is None:
            return ""
        # H8: stamp / read the per-cluster mood origin so Aiko can name
        # what gave the topic its feel ("ever since you told me about X").
        origin_what = self._topic_mood_origin(cid, temp.dominant, candidates)
        line = render_block(
            temp,
            label or "this topic",
            self.user_display_name,
            origin_what=origin_what,
        )
        if not line:
            return ""

        self._topic_temperature_cooldown = max(
            0, int(getattr(mem_settings, "topic_temperature_cooldown_turns", 6))
        )
        self._topic_temperature_last = {
            "cluster_id": int(cid),
            "label": label,
            "warmth": temp.warmth,
            "tenderness": temp.tenderness,
            "dominant": temp.dominant,
            "moment_count": temp.moment_count,
            "origin_what": origin_what,
        }
        log.info(
            "topic-temperature fire: cluster=%s dominant=%s warmth=%.2f "
            "tender=%.2f moments=%d",
            cid,
            temp.dominant,
            temp.warmth,
            temp.tenderness,
            temp.moment_count,
        )
        return line

    def _topic_mood_origin(
        self, cluster_id: int, dominant: str, candidates: list,
    ) -> str | None:
        """H8: persist + return the origin moment for a charged cluster.

        Keyed by ``cluster_id`` in the ``aiko.topic_mood_origin`` kv side-
        table, the origin is the shared moment that *gave* the topic its
        feel (``topic_temperature.pick_origin``). Stamped the first time a
        cluster reaches a pole, and re-stamped if the pole later flips
        (e.g. a warm topic turns tender). Returns the stored summary so
        ``render_block`` can append the "ever since…" clause, or ``None``
        when the feature is off / no candidate carries the pole. All paths
        are best-effort (swallow + log on failure) so origin bookkeeping
        never breaks the tonal cue.
        """
        if not bool(
            getattr(self._settings.agent, "topic_mood_origin_enabled", True)
        ):
            return None
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return None
        import json as _json
        from datetime import datetime as _dt, timezone as _tz

        from app.core.conversation.topic_temperature import (
            KV_MOOD_ORIGIN,
            ORIGIN_WHAT_MAXLEN,
            pick_origin,
        )

        try:
            raw = chat_db.kv_get(KV_MOOD_ORIGIN)
            origin_map = _json.loads(raw) if raw else {}
            if not isinstance(origin_map, dict):
                origin_map = {}
        except Exception:
            log.debug("topic-mood-origin: kv_get/parse failed", exc_info=True)
            origin_map = {}

        key = str(int(cluster_id))
        entry = origin_map.get(key)
        if not isinstance(entry, dict):
            entry = None

        if entry is None or entry.get("pole") != dominant:
            cand = pick_origin(candidates, dominant)
            if cand is not None and cand.what:
                entry = {
                    "pole": dominant,
                    "what": cand.what[:ORIGIN_WHAT_MAXLEN],
                    "when": cand.when,
                    "moment_id": cand.moment_id,
                    "stamped_at": _dt.now(_tz.utc).isoformat(),
                }
                origin_map[key] = entry
                try:
                    chat_db.kv_set(KV_MOOD_ORIGIN, _json.dumps(origin_map))
                    log.info(
                        "topic-mood-origin stamped: cluster=%s pole=%s "
                        "moment=%s",
                        cluster_id,
                        dominant,
                        cand.moment_id,
                    )
                except Exception:
                    log.debug(
                        "topic-mood-origin: kv_set failed", exc_info=True
                    )

        if entry and entry.get("pole") == dominant:
            what = entry.get("what")
            return str(what) if what else None
        return None

    def _render_topic_confidence_block(self, user_text: str) -> str:
        """F10i: calibrate how confidently Aiko speaks about the live topic.

        Maps ``user_text`` to its nearest topic cluster
        (``TopicGraph.best_clusters_for``), reads that cluster's
        ``(size, learned_count)`` (``TopicGraph.cluster_knowledge_stats``),
        scores a per-topic confidence
        (:func:`~app.core.conversation.topic_confidence.score_confidence`),
        and surfaces a one-line register nudge on the extremes: **thin**
        ground → it's okay to admit she doesn't know much and ask rather
        than bluff; **familiar** ground → trust what she knows, stop
        over-hedging. The silent middle is the common case. A topic-scoped
        sibling of K20 metacognitive calibration.

        Distinct from F10f (which owns the *dense-but-unresearched* "I keep
        circling X" beat — those clusters score mid/high here, so they
        never read as thin) and from K61 knowledge-grounding (which pushes
        *specific facts* on informational turns — the familiar band here is
        only an anti-over-hedge register cue, no content). Computed live in
        the provider (no worker / kv); same cheap shape as F10h. MCP debug:
        ``force_topic_confidence_surface`` arms ``_topic_confidence_force_next``
        to bypass the cooldown + min-sim and force a band on the matched
        cluster.
        """
        if not bool(
            getattr(self._settings.agent, "topic_confidence_enabled", True)
        ):
            return ""
        text = (user_text or "").strip()
        if len(text) < 8:
            return ""
        graph = getattr(self, "_topic_graph", None)
        embedder = getattr(self, "_embedder", None)
        if graph is None or embedder is None:
            return ""
        if not bool(getattr(graph, "persistent", False)):
            return ""

        force = bool(
            self._debug_overrides.take("topic_confidence_force_next", False)
        )

        cooldown = int(getattr(self, "_topic_confidence_cooldown", 0) or 0)
        if cooldown > 0 and not force:
            self._topic_confidence_cooldown = cooldown - 1
            return ""

        mem_settings = self._memory_settings
        min_sim = float(
            getattr(mem_settings, "topic_confidence_min_sim", 0.45)
        )
        thin = float(
            getattr(mem_settings, "topic_confidence_thin_threshold", 0.25)
        )
        familiar = float(
            getattr(mem_settings, "topic_confidence_familiar_threshold", 0.7)
        )
        if force:
            # Force a band on whatever cluster matches: split at 0.5.
            min_sim, thin, familiar = 0.0, 0.5, 0.5

        try:
            qvec = embedder.embed(text)
        except Exception:
            log.debug("topic-confidence: embed failed", exc_info=True)
            return ""
        try:
            matches = graph.best_clusters_for(qvec, top_n=1, min_sim=min_sim)
        except Exception:
            log.debug("topic-confidence: best_clusters_for failed", exc_info=True)
            return ""
        if not matches:
            return ""
        cid, label, _sim = matches[0]

        try:
            stats = graph.cluster_knowledge_stats(cid)
        except Exception:
            log.debug("topic-confidence: stats failed", exc_info=True)
            return ""
        if stats is None:
            return ""
        size, learned = stats

        from app.core.conversation.topic_confidence import (
            render_block,
            score_confidence,
        )

        conf = score_confidence(
            size, learned, thin_threshold=thin, familiar_threshold=familiar,
        )
        if conf.band is None:
            return ""
        line = render_block(conf, label or "this topic", self.user_display_name)
        if not line:
            return ""

        self._topic_confidence_cooldown = max(
            0, int(getattr(mem_settings, "topic_confidence_cooldown_turns", 6))
        )
        self._topic_confidence_last = {
            "cluster_id": int(cid),
            "label": label,
            "size": conf.size,
            "learned_count": conf.learned_count,
            "confidence": conf.confidence,
            "band": conf.band,
        }
        log.info(
            "topic-confidence fire: cluster=%s band=%s confidence=%.2f "
            "size=%d learned=%d",
            cid,
            conf.band,
            conf.confidence,
            conf.size,
            conf.learned_count,
        )
        return line

    def _render_earned_familiarity_block(self, user_text: str) -> str:
        """K66: let *deep shared history* on a topic show as register.

        Maps ``user_text`` to its nearest topic cluster
        (``TopicGraph.best_clusters_for``), reads that cluster's **mass**
        (member count, via ``cluster_member_ids``), and when the territory
        is well-worn (``size >= earned_familiarity_deep_threshold``) surfaces
        one private register nudge: lean on the shorthand you've built, skip
        the 101-level recap, assume the shared context — never count the
        history out loud.

        Orthogonal to F10h topic_temperature (emotional charge) and F10i
        topic_confidence (knowledge richness). The signal here is pure
        shared-history *depth*, deliberately NOT knowledge-weighted, so it
        fires on the big-but-unstudied conversational clusters F10i leaves
        silent. The two can co-occur on a genuinely rich+deep cluster, but
        the long K66 cooldown keeps that rare. Computed live in the provider
        (no worker / kv); same cheap shape as F10h/F10i. MCP debug:
        ``force_earned_familiarity_surface`` arms
        ``_earned_familiarity_force_next`` to bypass the cooldown + min-sim
        and force the deep band on the matched cluster.
        """
        if not bool(
            getattr(self._settings.agent, "earned_familiarity_enabled", True)
        ):
            return ""
        text = (user_text or "").strip()
        if len(text) < 8:
            return ""
        graph = getattr(self, "_topic_graph", None)
        embedder = getattr(self, "_embedder", None)
        if graph is None or embedder is None:
            return ""
        if not bool(getattr(graph, "persistent", False)):
            return ""

        force = bool(
            self._debug_overrides.take("earned_familiarity_force_next", False)
        )

        cooldown = int(getattr(self, "_earned_familiarity_cooldown", 0) or 0)
        if cooldown > 0 and not force:
            self._earned_familiarity_cooldown = cooldown - 1
            return ""

        mem_settings = self._memory_settings
        min_sim = float(
            getattr(mem_settings, "earned_familiarity_min_sim", 0.45)
        )
        deep_threshold = int(
            getattr(mem_settings, "earned_familiarity_deep_threshold", 14)
        )
        if force:
            # Force the deep band on whatever cluster matches.
            min_sim, deep_threshold = 0.0, 1

        try:
            qvec = embedder.embed(text)
        except Exception:
            log.debug("earned-familiarity: embed failed", exc_info=True)
            return ""
        try:
            matches = graph.best_clusters_for(qvec, top_n=1, min_sim=min_sim)
        except Exception:
            log.debug(
                "earned-familiarity: best_clusters_for failed", exc_info=True
            )
            return ""
        if not matches:
            return ""
        cid, label, _sim = matches[0]

        try:
            member_ids = graph.cluster_member_ids(cid)
        except Exception:
            log.debug("earned-familiarity: member walk failed", exc_info=True)
            return ""
        size = len(member_ids or ())

        from app.core.conversation.earned_familiarity import (
            render_block,
            score_familiarity,
        )

        read = score_familiarity(size, deep_threshold=deep_threshold)
        if read.band is None:
            return ""
        line = render_block(read, label or "this topic", self.user_display_name)
        if not line:
            return ""

        self._earned_familiarity_cooldown = max(
            0, int(getattr(mem_settings, "earned_familiarity_cooldown_turns", 12))
        )
        self._earned_familiarity_last = {
            "cluster_id": int(cid),
            "label": label,
            "size": read.size,
            "band": read.band,
        }
        log.info(
            "earned-familiarity fire: cluster=%s size=%d label=%r",
            cid,
            read.size,
            (label or "")[:60],
        )
        return line

    def _render_user_expertise_block(self, user_text: str) -> str:
        """K75: steer explanation depth to the user's competence on the topic.

        Maps ``user_text`` to its nearest topic cluster
        (``TopicGraph.best_clusters_for``), reads the running per-cluster
        competence estimate from the ``aiko.user_expertise`` kv map (learned
        post-turn from the user's own language — see the K75 block in
        ``post_turn_mixin``), bands it, and when the read is confidently
        ``expert`` or ``novice`` surfaces one private depth steer (skip the
        101 / scaffold gently). The quiet ``familiar`` middle renders nothing.

        Orthogonal to K66 earned_familiarity (shared-history depth *between*
        them) and F10i topic_confidence (how much Aiko *knows*): this is the
        *user's* competence. Cooldown-gated to stay rare. MCP debug:
        ``force_user_expertise_surface`` arms ``_user_expertise_force_next``
        to bypass the cooldown + min-sim + min-samples on the matched cluster.
        """
        if not bool(
            getattr(self._settings.agent, "user_expertise_enabled", True)
        ):
            return ""
        text = (user_text or "").strip()
        if len(text) < 8:
            return ""
        graph = getattr(self, "_topic_graph", None)
        embedder = getattr(self, "_embedder", None)
        if graph is None or embedder is None:
            return ""
        if not bool(getattr(graph, "persistent", False)):
            return ""

        force = bool(
            self._debug_overrides.take("user_expertise_force_next", False)
        )

        cooldown = int(getattr(self, "_user_expertise_cooldown", 0) or 0)
        if cooldown > 0 and not force:
            self._user_expertise_cooldown = cooldown - 1
            return ""

        from app.core.conversation import user_expertise as _ue

        mem = self._memory_settings
        min_sim = float(getattr(mem, "user_expertise_min_sim", 0.45))
        min_samples = int(getattr(mem, "user_expertise_min_samples", 4))
        novice_threshold = float(
            getattr(mem, "user_expertise_novice_threshold", -0.35)
        )
        expert_threshold = float(
            getattr(mem, "user_expertise_expert_threshold", 0.35)
        )
        if force:
            min_sim, min_samples = 0.0, 1

        try:
            qvec = embedder.embed(text)
        except Exception:
            log.debug("user-expertise: embed failed", exc_info=True)
            return ""
        try:
            matches = graph.best_clusters_for(qvec, top_n=1, min_sim=min_sim)
        except Exception:
            log.debug("user-expertise: best_clusters_for failed", exc_info=True)
            return ""
        if not matches:
            return ""
        cid, label, _sim = matches[0]

        state_map = _ue.load_map(self._chat_db.kv_get)
        state = state_map.get(str(int(cid)))
        band = _ue.band_for(
            state,
            novice_threshold=novice_threshold,
            expert_threshold=expert_threshold,
            min_samples=min_samples,
        )
        if band not in (_ue.BAND_NOVICE, _ue.BAND_EXPERT):
            return ""
        line = _ue.render_block(band, label or "this topic", self.user_display_name)
        if not line:
            return ""

        self._user_expertise_cooldown = max(
            0, int(getattr(mem, "user_expertise_cooldown_turns", 12))
        )
        self._user_expertise_last = {
            "cluster_id": int(cid),
            "label": label,
            "band": band,
            "score": round(float(state.score), 3) if state else None,
            "samples": int(state.samples) if state else 0,
        }
        log.info(
            "user-expertise fire: cluster=%s band=%s label=%r",
            cid,
            band,
            (label or "")[:60],
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
        # A promise that named a time and missed it gets said out loud as
        # late. Without this the cue reported only how long ago she said
        # it, so "by lunch" and "sometime" produced the same line and the
        # one obligation she could actually have broken went unmentioned.
        late_text = ""
        overdue_hours = pending.get("overdue_hours")
        if overdue_hours is not None:
            try:
                late_text = (
                    f" That was due {lifecycle.humanize_age(float(overdue_hours))}"
                    ", so it's late."
                )
            except (TypeError, ValueError):
                late_text = ""
        log.info(
            "promise-followthrough fire: memory_id=%s age=%s overdue_h=%s "
            "what=%r",
            pending.get("memory_id"),
            age_text,
            overdue_hours if overdue_hours is not None else "-",
            what[:80],
        )
        return (
            f"Heads-up: {age_text} you told {self.user_display_name} you'd "
            f"{what} — you haven't closed that loop.{late_text} If it fits "
            "this turn, mention what you found, or own that you haven't "
            "gotten to it yet. One casual line, not a production — and don't "
            "pretend you did it if you didn't."
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
        if self._debug_overrides.take("mood_inertia_force", False):
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
        """K38: surface an owed self-correction.

        The post-turn detector
        (:meth:`PostTurnMixin._maybe_arm_self_correction`) queues a cue
        when Aiko's last reply contradicted one of her own
        high-confidence ``fact`` / ``preference`` memories; this claims
        it so she owns the slip naturally on this turn. Independent of
        the gap-return cue family -- does NOT read or set
        ``_gap_cue_surfaced``. Survives ``aggressive=True`` (an owed
        correction must still land).

        The cue comes from the pool, so post-turn matching decides
        whether she actually took the correction. That verdict is worth
        more here than for most types: a correction she read past is a
        wrong belief still standing, which is the one failure the whole
        feature exists to prevent, and the old one-shot slot could not
        tell it apart from success. The retry budget is deliberately one
        turn -- the line opens with "a moment ago you said", which is
        true next turn and a fiction by the turn after.
        """
        if not bool(
            getattr(self._settings.agent, "self_correction_enabled", True)
        ):
            return ""
        row = self.take_pool_cue("self_correction")
        return row.text if row is not None else ""

    def _render_dropped_topic_block(self) -> str:
        """K82: surface an owed circle-back to a skipped ask.

        The post-turn detector
        (:meth:`PostTurnHelpersMixin._maybe_arm_dropped_topic`) queues a
        cue when Aiko's last reply covered only one of two separable
        asks; this claims it so she circles back once, lightly, on this
        turn. Independent of the gap-return cue family -- does NOT read
        or set ``_gap_cue_surfaced``. Survives ``aggressive=True`` (an
        owed circle-back must still land).

        The cue comes from the pool, so post-turn matching decides
        whether she actually took it. The retry budget is deliberately
        one extra turn -- the line opens with "last turn they also
        asked", which is true next turn and a fiction by the turn after.
        """
        if not bool(
            getattr(self._settings.agent, "dropped_topic_enabled", True)
        ):
            return ""
        row = self.take_pool_cue("dropped_topic")
        return row.text if row is not None else ""

    def _render_user_correction_block(self) -> str:
        """F13: surface an owed acknowledgment of a user correction.

        The off-turn
        :class:`~app.core.memory.user_correction_worker.UserCorrectionWorker`
        queues a cue once it has confirmed the user corrected a stored fact
        and superseded it; this claims it so Aiko owns the slip once,
        naturally, on this turn. Sibling of the K38 self-correction block:
        one-shot, pool-backed (so post-turn matching decides whether she
        actually took it), and survives ``aggressive=True`` -- an owed
        correction must still land even when the prompt is trimmed.
        """
        if not bool(
            getattr(self._settings.agent, "user_correction_enabled", True)
        ):
            return ""
        row = self.take_pool_cue("user_correction")
        return row.text if row is not None else ""

    def _render_fact_reversal_block(self) -> str:
        """F14: surface an owed acknowledgment of a self-discovered reversal.

        The F1
        :class:`~app.core.memory.idle_fact_checker.IdleFactChecker` queues a
        cue once its own web research contradicts and rewrites a claim Aiko
        had already surfaced to the user; this claims it so she owns the
        reversal once, naturally, on this turn. Sibling of the F13
        user-correction block: gated on its master toggle and left standing
        under ``aggressive`` trims, since owning a self-found mistake must
        still land when the prompt is tight.
        """
        if not bool(
            getattr(self._settings.agent, "fact_reversal_enabled", True)
        ):
            return ""
        row = self.take_pool_cue("fact_reversal")
        return row.text if row is not None else ""

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
            self._debug_overrides.take("misattunement_force_next", False)
        )
        if force_next:
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
            recent = self._inner_life_recent_messages(6)
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

    def _render_implicit_need_block(self, user_text: str) -> str:
        """K69: steer the response *mode* the live message is asking for.

        Provider-time (not a post-turn stash) so the steer lands on the
        SAME turn that's about to reply -- answering the need IS this
        reply. A pure heuristic ([`implicit_need.classify`](
        ../conversation/implicit_need.py)) over the live ``user_text``,
        corroborated by three cheap, already-available signals gathered
        here:

        * the K4 conversation arc (``_arc_store`` -- a weak prior),
        * the live K14 user-state read (``_user_state_estimator.estimate``
          -- same-turn perceived mood / energy), and
        * voice-mode paralinguistics (``_last_vocal_tone`` tags).

        Silent on the common ``neutral`` turn. Best-effort: any failure
        path returns ``""`` so a classifier hiccup never disturbs prompt
        assembly. Stashes ``_last_implicit_need`` for the MCP debug tool;
        ``_implicit_need_force_mode`` (one-shot) lets a tester pin a mode.
        """
        if not bool(
            getattr(self._settings.agent, "implicit_need_enabled", True)
        ):
            return ""
        text = (user_text or "").strip()
        if not text:
            return ""

        try:
            from app.core.conversation import implicit_need as _need
        except Exception:
            log.debug("implicit_need import failed", exc_info=True)
            return ""

        # MCP-debug bypass: force a specific mode for one turn.
        force_mode = self._debug_overrides.take("implicit_need_force_mode")
        if force_mode is not None:
            forced = _need.NeedResult(
                str(force_mode), 99.0, {}, ("forced",),
            )
            self._last_implicit_need = {
                "mode": forced.mode, "confidence": 99.0, "forced": True,
            }
            return _need.render_inner_life_block(
                forced, user_display_name=self.user_display_name,
            )

        # Arc (weak prior) -- best-effort, lags by one turn.
        arc: str | None = None
        arc_store = getattr(self, "_arc_store", None)
        if arc_store is not None:
            try:
                arc = arc_store.get_or_default(self._user_id).arc
            except Exception:
                arc = None

        # Live user-state read on THIS message (same-turn estimate).
        perceived_mood: str | None = None
        perceived_energy: str | None = None
        estimator = getattr(self, "_user_state_estimator", None)
        if estimator is not None:
            try:
                now = estimator.estimate(self._user_id, user_text=text)
                perceived_mood = getattr(now, "perceived_mood", None)
                perceived_energy = getattr(now, "perceived_energy", None)
            except Exception:
                log.debug("implicit_need: user_state estimate failed", exc_info=True)

        # Voice-mode paralinguistic tags (if present).
        vocal_tags: tuple[str, ...] = ()
        try:
            lock = getattr(self, "_vocal_tone_lock", None)
            if lock is not None:
                with lock:
                    tone = getattr(self, "_last_vocal_tone", None)
            else:
                tone = getattr(self, "_last_vocal_tone", None)
            if tone is not None and getattr(tone, "tags", None):
                vocal_tags = tuple(tone.tags)
        except Exception:
            vocal_tags = ()

        try:
            result = _need.classify(
                text,
                arc=arc,
                perceived_mood=perceived_mood,
                perceived_energy=perceived_energy,
                vocal_tags=vocal_tags,
                min_confidence=float(
                    getattr(
                        self._memory_settings,
                        "implicit_need_min_confidence",
                        2.0,
                    )
                ),
            )
        except Exception:
            log.debug("implicit_need classify raised", exc_info=True)
            return ""

        self._last_implicit_need = {
            "mode": result.mode,
            "confidence": result.confidence,
            "scores": result.scores,
            "reasons": list(result.reasons[:6]),
            "forced": False,
        }

        if result.mode == _need.MODE_NEUTRAL:
            return ""

        log.info(
            "implicit-need: mode=%s confidence=%.2f arc=%s mood=%s",
            result.mode,
            result.confidence,
            arc or "-",
            perceived_mood or "-",
        )
        try:
            return _need.render_inner_life_block(
                result, user_display_name=self.user_display_name,
            )
        except Exception:
            log.debug("implicit_need render failed", exc_info=True)
            return ""


