"""Build the message list sent to Ollama on every turn.

Inputs (all optional):
  - persona file (data/persona/aiko_companion.txt)
  - long-term memory block from :class:`MemoryRetriever` (cross-session)
  - latest summary row (covers everything before the recent window)
  - last N messages from chat_database.messages
  - the new user input

Output: ``list[dict]`` ready for ``OllamaClient.chat_stream`` plus a typed
:class:`PromptTelemetry` describing how the budget was spent. The new
:meth:`PromptAssembler.assemble_with_budget` is the canonical entry point;
``build()`` is kept as a thin alias that returns only the messages for callers
that don't need telemetry.
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
from app.core.infra.chat_database import ChatDatabase, MessageRow, SummaryRow
from app.llm.token_utils import estimate_messages_tokens, estimate_tokens

if TYPE_CHECKING:
    from app.core.memory.memory_retriever import MemoryRetriever
    from app.core.rag.rag_retriever import RagRetriever

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
from app.core.session.prompt_assembler_helpers_mixin import (
    PromptAssemblerHelpersMixin,
)

log = logging.getLogger("app.prompt_assembler")


# ── Prompt-cache prefix-stability ladder ─────────────────────────────
#
# OpenAI (and any vendor that ships compatible prompt caching) hashes
# the request's token stream and returns "longest prefix that matches a
# previous request in the last 5-10 min". Cache matching ends at the
# FIRST differing token. To keep the cache hit-rate high we lay
# ``system_parts`` out from most-stable (T0) to most-volatile (T6),
# strictly. WITHIN each tier the relative order is preserved so the
# existing behavioural cluster comments stay correct (e.g. "K28
# turning_over lands right after K14 absence_curiosity" still holds —
# both blocks are T6, the comment governs in-tier order).
#
# Contract: when adding a new prompt block, pick the tier that matches
# its volatility and append it to that list. Never inline a per-turn
# block above a stable one; that single byte change invalidates every
# token after it (including history messages and the user message),
# costing ~10x on the input side. See ``docs/prompt-caching.md``.
#
# This constant is purely documentation/audit — the actual ordering is
# enforced by the explicit ``if block: system_parts.append(block)``
# cascade in :meth:`PromptAssembler.assemble_with_budget`. Tests in
# ``tests/test_prompt_assembler.py::PromptCachePrefixOrderingTests``
# lock the cross-tier invariants in place.
_PROMPT_BLOCK_TIERS: dict[str, tuple[str, ...]] = {
    # T0 — stable across sessions. Persona file edit / config flip is
    # the only thing that should ever invalidate this.
    "T0_stable": (
        "persona",
        "speech_grammar_addendum",
        "overlay_grammar_block",
        "outfit_grammar_block",
        "motion_grammar_block",
        "touch_grammar_addendum",
        "self_image_block",
        "narrative_block",
        "profile_block",
        "petname_block",
        "catchphrase_block",
    ),
    # T1 — per-arc / per-day. Changes a few times a day at most.
    "T1_semi_stable": (
        "relationship_block",
        "axes_block",
        "arc_block",
        "agenda_block",
        "goals_block",
        "interest_map_block",
        "day_color_block",
        "anniversary_block",
        "milestone_block",
    ),
    # T2 — compaction only. Only mutates when a SummaryWorker run
    # collapses old history into a new summary row.
    "T2_summary": (
        "summary_text",
        "thread_note_text",
    ),
    # T3 — per-turn but topic-stable. Same memories often surface on
    # consecutive turns on the same thread.
    "T3_rag": (
        "memory_block",
    ),
    # T4 — ambient awareness. Hourly to per-turn changes.
    "T4_ambient": (
        "grounding_block",
        "ambient",
        "circadian_block",
        "pajama_block",
        "ambient_noise_block",
        "world_block",
        "activity_block",
        "sensory_anchor_block",
    ),
    # T5 — per-turn affect / style. Updates after every reply.
    "T5_affect_style": (
        "affect_block",
        "mood_hint",
        "mood_inertia_block",
        "mood_shell_block",
        "emotion_episode_block",
        "style_signal_block",
        "user_state_block",
        "vocal_tone_block",
    ),
    # T6 — live ``user_text``-dependent detectors. The freshest cues
    # the LLM reads before the user message. Almost always change
    # turn-to-turn.
    "T6_detectors": (
        "belief_gaps_block",
        "clarification_block",
        "calibration_block",
        "rupture_block",
        "self_correction_block",
        "promise_followthrough_block",
        "misattunement_block",
        "opinion_injection_block",
        "reconnection_block",
        "absence_curiosity_block",
        "turning_over_block",
        "away_activities_block",
        "forward_curiosity_block",
        "follow_up_block",
        "appreciation_block",
        "reciprocal_vulnerability_block",
        "novelty_block",
        "stagnation_block",
        "style_pattern_block",
        "question_balance_block",
        "tease_rhythm_block",
        "self_noticing_block",
        "vulnerability_budget_block",
        "touch_state_block",
        "user_reactions_block",
        # D2 Part B — in-chat attachment turn hint (per-turn; what the
        # user attached to THIS message). NOT dropped under aggressive.
        "attachments_block",
        # Brain orchestration chunk 6 — running-tasks state block.
        # Sibling of ``task_cues_block``: this block announces what's
        # *still working*, the cue block announces *deltas*
        # (results landed, blocked on input). State comes before
        # delta so the prompt reads "you're doing A and B; A just
        # finished" rather than the reverse. NOT dropped under
        # ``aggressive`` — when the user asks "are you still working
        # on X?" they expect Aiko to know, even on a tight budget.
        "running_tasks_block",
        # Brain orchestration chunk 5 — parked task cues (results,
        # input-needed questions). T6 because the cue list is
        # turn-specific (drained on each assembly) and clusters with
        # the other "live read" blocks. NOT dropped under
        # ``aggressive`` — a parked task waiting for an answer is
        # exactly what tight prompts need to keep surfaced.
        "task_cues_block",
        "initiative_block",
        "thread_ownership_block",
        "wants_block",
        "topic_appetite_block",
        "tease_ledger_block",
        "curiosity_seeds_block",
        "knowledge_gaps_block",
        # K61 (F8/F9): informational-turn steer toward learned
        # specifics. Query-aware (consumes ``user_text``), so it lands
        # in T6 with the other detectors. Clusters right after the
        # knowledge-gap line — both are "what Aiko knows / wonders
        # about" surfaces.
        "knowledge_grounding_block",
        # F10f: "I keep circling X but never dug in" — clusters with the
        # knowledge-gap / grounding surfaces (all "what Aiko does /
        # doesn't know about a recurring topic").
        "knowledge_gap_notice_block",
        # F10h: per-cluster affect ("topic temperature") — a topical
        # tonal cue, sits with the other topic-graph-derived surfaces.
        "topic_temperature_block",
        # F10i: per-topic confidence self-model — hedge / earned
        # familiarity, last of the topic-graph-derived cues.
        "topic_confidence_block",
        # K64a: associative wandering — "funny, this reminds me of ..."
        # connects the live topic to a distant cluster. Sits with the
        # other topic-graph-derived surfaces (it's drawn from the graph).
        "associative_wander_block",
        # K64b: interest drift — "I've been drawn to X lately" / fading.
        # Slow self-aware register shift from per-cluster mass over time;
        # sits with the other topic-graph-derived surfaces.
        "interest_drift_block",
    ),
}


class PromptAssembler(PromptAssemblerHelpersMixin):
    def __init__(
        self,
        db: ChatDatabase,
        *,
        persona_path: Path | str = DEFAULT_PERSONA_PATH,
        recent_window: int = 20,
        memory_retriever: "MemoryRetriever | None" = None,
        rag_retriever: "RagRetriever | None" = None,
        self_image_path: Path | str | None = None,
        history_age_prefix_enabled: bool = True,
        cue_register_rotation_enabled: bool = True,
    ) -> None:
        self._db = db
        self._persona_path = Path(persona_path)
        self._recent_window = max(2, int(recent_window))
        self._persona_cache: tuple[float, str] | None = None
        self._memory_retriever = memory_retriever
        self._rag_retriever = rag_retriever
        # K-time1 toggle. When True, every history message in the LLM
        # prompt is prefixed with a relative-age tag (``[2 min ago]``,
        # ``[just now]``, ``[yesterday 18:45]``). Set False for a
        # byte-identical history to the pre-K-time1 behaviour.
        self._history_age_prefix_enabled = bool(history_age_prefix_enabled)
        # K51 toggle. When True, cue blocks that open with the literal
        # ``Heads-up:`` get their prefix rotated across a few register
        # shapes at assembly time (deterministic per turn) so the
        # model never reads the same coach template thrice in one
        # prompt. False = byte-identical legacy cues.
        self._cue_rotation_enabled = bool(cue_register_rotation_enabled)
        # Carry-over hint: the most recent assistant reaction. Lets the LLM
        # keep an emotional through-line across turns without us writing it
        # explicitly into the persona.
        self._last_reaction: str | None = None
        # Listening-window slice cache (Phase 3 of the
        # listening_window_prefetch plan). Single-entry-per-session is
        # sufficient — there's only ever one active conversation per
        # process. Last hit/miss is exposed via ``last_slice_cache_event``
        # so :class:`TurnRunner` can fold it into the "turn done:" log.
        self._slice_cache: dict[str, _StaticSlices] = {}
        # P3: cheap per-session invalidation signature stored alongside the
        # slice cache. When it still matches, the hit path skips the
        # ``get_messages`` + ``get_latest_summary`` reads entirely.
        self._slice_head_sig: dict[str, tuple] = {}
        self._last_slice_cache_event: str = "skip"
        # P22: monotonically-incrementing assembly sequence. Bumped at the
        # top of every ``assemble_with_budget`` call so inner-life providers
        # that each need the recent-history window (K23 misattunement, K30
        # self-noticing, K54 topic-appetite) can share a single
        # ``get_messages`` read keyed on this seq instead of each issuing
        # their own overlapping query. See
        # ``SessionController._inner_life_recent_messages``.
        self._assembly_seq: int = 0

        # Phase-2/3/4 block providers. Each callable returns a short text
        # snippet (or ``""`` to skip) that gets folded into the system
        # prompt. They run on the hot path so must be cheap (<1ms each):
        # SQL reads + dict lookups, no LLM. Set via ``set_inner_life_providers``.
        self._affect_provider: Callable[[], str] | None = None
        self._circadian_provider: Callable[[], str] | None = None
        # K27 -- daily personality colour. Slow ambient cue rolled
        # once per local day, kept standalone (not folded into the
        # K16 unified grounding line). Returns a one-line phrase
        # like "Your day's colour today: pensive -- slower replies,
        # more 'hmm'...". The provider has a lazy fallback so the
        # very first turn after midnight always has a fresh colour,
        # even when the idle-worker hasn't fired yet.
        self._day_color_provider: Callable[[], str] | None = None
        self._profile_provider: Callable[[], str] | None = None
        self._user_state_provider: Callable[[], str] | None = None
        self._relationship_provider: Callable[[], str] | None = None
        self._arc_provider: Callable[[], str] | None = None
        self._narrative_provider: Callable[[], str] | None = None
        self._agenda_provider: Callable[[], str] | None = None
        # Aiko's room: compact ambient block describing her current
        # location + nearby items. See WorldStore.render_block.
        self._world_provider: Callable[[], str] | None = None
        # Activity awareness (Phase 4c): the foreground app the user
        # is in, surfaced as "<user> is currently working in <App>."
        # Always empty string when the feature is disabled or no app
        # was captured. Desktop-only opt-in; browser users never set
        # the underlying state. Dropped in aggressive mode.
        self._activity_provider: Callable[[], str] | None = None
        # Schema v7: "On your mind today — a year ago today, …" line that
        # surfaces a single shared_moment matching one of the calendar
        # anniversary windows. Always empty when no match for today;
        # rate-limited per moment by the provider itself. Dropped in
        # aggressive mode.
        self._anniversary_provider: Callable[[], str] | None = None
        # J8: one-shot milestone-celebration cue. Armed post-turn when a
        # relationship milestone (100 turns / 1 week / 1 month / …) crosses,
        # consumed by the provider on the next turn. Empty almost always;
        # deferred (not consumed) in aggressive mode so a milestone never
        # gets silently swallowed under context pressure.
        self._milestone_provider: Callable[[], str] | None = None
        # Schema v7: terse relationship-axes line. Only renders when an
        # axis exceeds the notability threshold (default 0.5). Dropped
        # in aggressive mode.
        self._axes_provider: Callable[[], str] | None = None
        # F2 personality backlog: knowledge-gap "Things you've been
        # wondering about with <user>" line. Unlike the other inner-life
        # providers this one takes the current ``user_text`` so it can
        # pick the most-relevant open gap by cosine similarity. Returns
        # empty when no gap is sufficiently relevant. Dropped in
        # aggressive mode.
        self._knowledge_gaps_provider: Callable[[str], str] | None = None
        # F10f: surface a "I keep circling X but never dug in" cue when the
        # live turn is on a dense-but-unresearched topic. Query-aware
        # (consumes ``user_text``); dropped in aggressive mode.
        self._knowledge_gap_notice_provider: Callable[[str], str] | None = None
        # F10h: per-cluster affect ("topic temperature"). Nudges tone when
        # the live turn lands on a warm / tender cluster. Query-aware
        # (consumes ``user_text``); dropped in aggressive mode.
        self._topic_temperature_provider: Callable[[str], str] | None = None
        # F10i: per-topic confidence self-model. Hedge on a thin cluster,
        # speak with earned familiarity on a rich one. Query-aware
        # (consumes ``user_text``); dropped in aggressive mode.
        self._topic_confidence_provider: Callable[[str], str] | None = None
        # K64a: associative wandering — surface a "funny, this reminds me
        # of ..." connection between the live topic and a distant cluster.
        # Query-aware (consumes ``user_text``); dropped in aggressive mode.
        self._associative_wander_provider: Callable[[str], str] | None = None
        # K64b: interest drift — surface a slow "I've been drawn to X
        # lately" / "X has gone quiet" register shift when the live turn is
        # on a topic whose mass has drifted. Query-aware; dropped aggressive.
        self._interest_drift_provider: Callable[[str], str] | None = None
        # K61: informational-turn knowledge-grounding steer.
        self._knowledge_grounding_provider: Callable[[str], str] | None = (
            None
        )
        # K2 personality backlog: belief-gap "you had Jacob pegged as X
        # but it actually reads Y" lines. Source rows come from the
        # post-turn :class:`BeliefGapDetector`; the provider only
        # renders cached gaps from the previous turn -- it does no
        # work itself, so we can safely call it on every turn. Dropped
        # in aggressive mode.
        self._belief_gaps_provider: Callable[[], str] | None = None
        # K17 — clarification-repair one-shot. The post-turn detector
        # stashes a result on the controller; this provider renders
        # it on the very next turn and clears the slot.
        self._clarification_provider: Callable[[], str] | None = None
        # K20 — metacognitive calibration. Reads the persisted
        # per-user CalibrationState and renders a one-line hedge cue
        # when global / topic scores have dropped below threshold.
        # Not one-shot: the state persists across turns and decays
        # toward baseline lazily on each read.
        self._calibration_provider: Callable[[], str] | None = None
        # K24 — sensory anchoring layer. Adaptive per-arc cadence;
        # the provider returns a one-line "small physical beat
        # available" cue when the cooldown is clear AND the dice
        # cooperate. NOT one-shot from the assembler's POV: state
        # lives on the controller's :class:`SensoryAnchorCadence`.
        self._sensory_anchor_provider: Callable[[], str] | None = None
        # K8 — affect-rupture one-shot. Sibling of the clarification
        # provider above; same one-shot contract.
        self._rupture_provider: Callable[[], str] | None = None
        # K45 — mood-inertia one-shot. Post-turn detector stashes a
        # rendered cue when the fresh reaction tag strongly outran the
        # smoothed felt state; this provider surfaces it once on the
        # next turn and clears the slot. T5 (reaction-shaping family,
        # sits right after the mood carryover hint).
        self._mood_inertia_provider: Callable[[], str] | None = None
        # K38 — self-correction one-shot. Post-turn detector stashes a
        # SelfCorrectionHit when Aiko's reply contradicted one of her own
        # high-confidence fact/preference memories; this provider renders
        # the cue once on the next turn and clears the slot.
        self._self_correction_provider: Callable[[], str] | None = None
        # K43 — promise follow-through one-shot. The idle worker arms a
        # pending cue in kv_meta when an assistant-side promise has sat
        # open past the age gate; this provider renders it once ("close
        # the loop — or own that you haven't") and clears the slot.
        self._promise_followthrough_provider: Callable[[], str] | None = None
        # K23 — subtle misattunement detector. Per-turn detector that
        # fires ``mild_disengagement`` when {user} goes very short or
        # pivots topics right after a substantial Aiko reply. Unlike
        # the K8/K17 one-shots, this provider takes ``user_text`` and
        # runs the detector itself on every call -- cooldown lives on
        # the controller, not in a pending slot.
        self._misattunement_provider: Callable[[str], str] | None = None
        # K29 — opinion injection detector. Per-turn provider that
        # fires a one-line cue when {user_name}'s latest message
        # contradicts one of Aiko's stored ``kind="self"`` stance
        # memories. Sibling of the K23 misattunement provider: same
        # provider-time shape, takes ``user_text``, runs the
        # detector itself on every call -- cooldown + per-session
        # cap + LLM rate-limiter all live on the controller. The
        # detector is deliberately conservative (only ``definite``
        # heuristic verdicts fire immediately; ``borderline`` runs
        # through an LLM YES/NO gate) so an Aiko with a few stored
        # stances doesn't slip into contrarianism.
        self._opinion_injection_provider: Callable[[str], str] | None = None
        # K14 typed-mode absence-curiosity one-shot. Same shape as the
        # K8 rupture provider: post-turn engagement tracker stashes a
        # pending absence duration on the controller when a typed gap
        # lands in the configured band; this provider renders the cue
        # on the very next turn and clears the slot.
        self._absence_curiosity_provider: Callable[[], str] | None = None
        # J5 reconnection ritual. Unlike the rest of the gap family this
        # one self-computes the gap at assembly time (so it can colour the
        # FIRST reply back), closeness-scaled, one-shot per return.
        self._reconnection_provider: Callable[[], str] | None = None
        # J10 appreciation beat. Rare, specific gratitude anchored to a
        # recent positive shared moment; closeness + long cooldown gated.
        self._appreciation_provider: Callable[[], str] | None = None
        # J9 reciprocal vulnerability. user_text provider (live low-mood
        # gate); stage + trust + K15-budget gated, long cooldown.
        self._reciprocal_vulnerability_provider: (
            Callable[[str], str] | None
        ) = None
        # K28 "What I've been turning over" one-shot. Sibling of the
        # K14 absence_curiosity provider: same post-turn-armed slot
        # mechanic but a longer gap threshold (default 90 min) and
        # a different render (surfaces one recent reflection rather
        # than welcoming the user back). Stacks with K14 on the
        # 90 min - 4h overlap; the welcome-back line precedes the
        # "and I was thinking about X" content in the system prompt
        # so the two cues read naturally together.
        self._turning_over_provider: Callable[[], str] | None = None
        # K36 "things I did while you were away" one-shot. Consumer of the
        # IdleAwayActivityWorker journal; same post-turn-armed slot as K28
        # turning_over but reads the kv journal ring. Defers to
        # turning_over via the shared _gap_cue_surfaced flag so only one
        # gap cue fires per return.
        self._away_activities_provider: Callable[[], str] | None = None
        # K34 "forward curiosity" one-shot. Consumer of the
        # ForwardCuriosityWorker question ring; runs LAST of the three
        # gap-return cues so it defers to turning_over + away_activities
        # via the shared _gap_cue_surfaced flag.
        self._forward_curiosity_provider: Callable[[], str] | None = None
        # Follow-up cue: surfaces one "you could ask how their plan went"
        # hint after a user-mentioned future_plan's event time passes.
        # Independent of the gap-return cue family (does not touch
        # _gap_cue_surfaced).
        self._follow_up_provider: Callable[[], str] | None = None
        # K5 mood-shell tilt. NOT one-shot -- derived fresh every turn
        # from current affect / relationship axes / pending moments,
        # so the renderer is stateless from the assembler's POV.
        # Returns ``""`` when nothing notable crosses the gate. Part
        # of the K16 ``replace`` suppression set because it folds
        # affect colour into a single tonal line.
        self._mood_shell_provider: Callable[[], str] | None = None
        # K1 personality backlog: "Aiko's quiet long-term goals" inner-life
        # bullet listing up to ``goals_max_rendered`` active goals plus the
        # most recent reflection note when one fits. Cheap mirror walk via
        # :class:`GoalStore`; no per-turn LLM. The block clusters with
        # ``agenda_block`` and the other "things Aiko is carrying" cues
        # in the system prompt. Dropped in aggressive mode along with
        # ``agenda`` so the budget stays focused on the user's message
        # under tight contexts. Empty until the worker bootstrap (or a
        # user / self-tag write) lands the first goal.
        self._goals_provider: Callable[[], str] | None = None
        # F10e: "interest map" -- terse T1 line listing the top few
        # labelled topic clusters ("the things we keep coming back to").
        # Derived from the topic graph's live cluster map (label + size),
        # no per-turn LLM cost. Clusters with ``goals_block`` /
        # ``agenda_block`` in the "things Aiko is carrying" T1 group;
        # dropped in aggressive mode alongside them. Empty until the F10a
        # label worker has named at least one dense cluster.
        self._interest_map_provider: Callable[[], str] | None = None
        # K6 personality backlog: surprise/novelty signal. Takes the
        # current ``user_text`` (like the F2 knowledge-gap provider)
        # because the detector compares the live turn embedding to a
        # rolling centroid. Returns ``""`` on silent/warmup/cooldown
        # turns; banded "Heads-up: ..." lines otherwise. Dropped in
        # aggressive mode.
        self._novelty_provider: Callable[[str], str] | None = None
        # K18 personality backlog: topic stagnation signal. Sibling
        # of the novelty provider above; consumes the per-turn
        # distance K6 just computed (no extra embedding) and emits
        # a "Heads-up: you've been circling..." line when the rolling
        # mean distance stays low for a window. Order in
        # ``set_inner_life_providers`` matters -- novelty must run
        # first so K18 can read its ``last_distance`` / ``last_band``
        # off the K6 detector. Dropped in aggressive mode.
        self._stagnation_provider: Callable[[str], str] | None = None
        # Anti-rut layer: AikoStylePatternTracker watches Aiko's *own*
        # recent assistant turns and emits an opener / question /
        # length "Heads-up" cue when one of the bands trips. Provider
        # takes no args (the post-turn pipeline already pushed the
        # stripped reply into the tracker on the previous turn).
        # Sibling of K6/K18 in voice and shape; clusters next to them
        # in the system prompt. Dropped in aggressive mode like the
        # other style cues.
        self._style_pattern_provider: Callable[[], str] | None = None
        # K47 question/share balance: proactive share-first cue surfaced
        # while the question/share gate is armed (Aiko's recent replies
        # have leaned too hard on questions). Pairs with the suppression
        # of the question-pushing providers (curiosity_seeds /
        # forward_curiosity / follow_up / knowledge_gaps + narrative
        # open_question). No args; the post-turn pipeline owns the
        # counter. Dropped in aggressive mode like the other cues.
        self._question_balance_provider: Callable[[], str] | None = None
        # K48 tease rhythm: one-shot banter-budget cue (ease off / one
        # more step is safe), armed post-turn from the tease classifier +
        # K32 landing signal + humor axis. Clusters with the other
        # Aiko-side noticing cues; dropped in aggressive mode.
        self._tease_rhythm_provider: Callable[[], str] | None = None
        # K13 stylometric mirror: tracks Jacob's *own* writing style
        # (terseness / formality / emoji / slang / question rate)
        # across recent user turns and surfaces a one-line "How
        # Jacob writes lately: ..." directive so Aiko's register
        # stays calibrated across days. Pair of the anti-rut tracker
        # (which is the Aiko-side half). No args; the post-turn
        # pipeline updates the analyzer state. Unlike K6/K18 this
        # block is *always* rendered (including aggressive mode)
        # because register is the first thing the budget should
        # preserve. Returns "" during warmup or when every axis is
        # default.
        self._style_signal_provider: Callable[[], str] | None = None
        # K30 personality backlog: "self-noticing cues" — one block
        # that fans the three agreement-streak / flat-affect /
        # repeated-thought sub-detectors. Same register and cadence
        # as ``style_pattern`` (Aiko-side patterns I'm in), dropped
        # in aggressive mode for the same reason. Takes no args;
        # post-turn pushes affect samples + assistant embeddings
        # into rings on :class:`SessionController` which the
        # provider reads. Returns "" when none of the three
        # sub-detectors fire (the steady state).
        self._self_noticing_provider: Callable[[], str] | None = None
        # K15 personality backlog: "Self-disclosure / vulnerability
        # budget" inner-life cue. One-line nudge ("you've shared a
        # lot of softness with Jacob recently -- let yourself stay
        # surface this turn") that paces how often Aiko opens up
        # personally. Takes no args; the provider on
        # :class:`SessionController` reads ``kv_meta``, applies
        # rolling decay, and renders the cue based on the
        # spent/capacity ratio. NOT suppressed under
        # ``aggressive=True`` because the cue is one line and the
        # whole point is to make the over-cap warning persist
        # through tight-budget turns when long replies would
        # otherwise compound the over-disclosure. NOT included in
        # the K16 grounding-line suppression matrix because it's
        # a budget cue, not an ambient grounding block.
        self._vulnerability_budget_provider: Callable[[], str] | None = None
        # K32 personality backlog: arms the "Jacob just hearted that
        # line" inner-life cue when the user tapped a reaction button
        # on one of Aiko's recent bubbles since her last turn. Drains
        # ``SessionController._pending_user_reactions`` and renders a
        # single-line nudge. Provider stays silent when the queue is
        # empty so the budget never wastes a turn on a no-op cue.
        # NOT suppressed under ``aggressive=True`` (one line, one-shot).
        self._user_reactions_provider: Callable[[], str] | None = None
        # K31 personality backlog: "physical budget" cue. Renders only
        # when Aiko has been physical with the user a lot today
        # (intimate-gesture stack hit or any kind's daily cap was
        # hit). Sibling of K15 vulnerability budget. Provider returns
        # ``""`` on the common case; not suppressed under aggressive
        # because it's a sub-line cue.
        self._touch_state_provider: Callable[[], str] | None = None
        # D2 Part B — in-chat attachment turn hint. When the user
        # attached image / text files to the message being processed
        # this turn, the provider renders a one-line list of
        # ``Attachments:<file> (image|text)`` paths + tells Aiko to act
        # on them via ``start_workflow`` (describe_image / read_file).
        # Reads per-turn session state set at the top of
        # ``chat_once_streaming``; silent when nothing is attached. NOT
        # suppressed under ``aggressive`` — a fresh attachment is
        # exactly what the user wants acted on.
        self._attachments_provider: Callable[[], str] | None = None
        # Brain-orchestration chunk 6 — running-tasks state block.
        # Sibling of ``_task_cues_provider`` below: this provider
        # renders what's *still working* (state), the other renders
        # *deltas* (results / blocked tasks). Reads
        # :meth:`TaskOrchestrator.list_running` directly; the format
        # is "Tasks running for {user_name} right now:" plus one
        # bullet per active task (handler + status + progress%).
        # Cap at 5 bullets so a task-bomb can't blow the budget.
        # Empty when no tasks are running or the master switch
        # ``agent.tasks_running_block_enabled`` is off (the common
        # path on most turns). NOT suppressed under ``aggressive`` —
        # "are you still working on X?" needs an honest answer even
        # on a tight budget.
        self._running_tasks_provider: Callable[[], str] | None = None
        # Brain-orchestration chunk 5 — parked task cues. Provider
        # drains :class:`TaskCueStore` on each assembly and renders a
        # T6 block with success / failure / question sub-headers (see
        # :func:`app.core.tasks.cue_render.render_cue_block`).
        # ``SessionController`` installs this provider during init via
        # :meth:`TaskOrchestrationMixin.drain_task_cues_for_render`,
        # which also cancels any pending escalation timer so a cue
        # that just surfaced doesn't double-fire as a proactive. The
        # provider returns ``""`` when nothing is parked, so the
        # common path costs one dict lookup. NOT suppressed under
        # ``aggressive`` — a parked task waiting on an answer is
        # exactly what a tight budget needs to keep visible.
        self._task_cues_provider: Callable[[], str] | None = None
        # K9 personality backlog: "Quiet curiosity" inner-life bullet
        # listing 1-2 active curiosity seeds (topics Aiko has been
        # quietly wondering about that haven't come up yet). Cheap
        # mirror walk + provider-side rotation; no per-turn LLM. The
        # block is suppressed in aggressive mode along with novelty /
        # stagnation so the budget stays focused on the user's
        # message. Empty when no active seeds exist.
        self._curiosity_seeds_provider: Callable[[], str] | None = None
        # K52 wants ledger. Pressure-banded "things Aiko wants from
        # the conversation" cue — soft list below the imperative
        # threshold, a single directive paragraph above it. Dropped
        # in aggressive mode alongside curiosity seeds.
        self._wants_provider: Callable[[], str] | None = None
        # K53 initiative turns. Takes the live ``user_text`` (for the
        # substantial-message escape hatch) and returns the one-turn
        # "this turn is yours" directive on cadence, else "".
        self._initiative_provider: Callable[[str], str] | None = None
        # K55 thread ownership. Takes the live ``user_text`` (the
        # reply being evaluated against the opened-thread embedding)
        # and returns the one-shot "circle back" cue on a pivot,
        # else "".
        self._thread_ownership_provider: Callable[[str], str] | None = None
        # K54 topic appetite. Once-per-conversation "tapped out on
        # this topic, here's my offer" permission slip. Must run
        # after the K18 stagnation provider (reads its standing
        # ``last_mean``) and after the K52 wants provider has matured
        # the ledger this turn.
        self._topic_appetite_provider: Callable[[], str] | None = None
        # K57 directed emotion episodes. Takes the live ``user_text``
        # (acknowledgment detection resolves a live episode) and
        # returns the strongest episode's register cue — or the
        # one-shot thaw cue right after a resolution.
        self._emotion_episode_provider: Callable[[str], str] | None = None
        # K59 tease economy. Rare collection-opportunity permission
        # slip from the payback ledger (humor-gated, cooldown-gated).
        self._tease_ledger_provider: Callable[[], str] | None = None
        # K16 unified ambient grounding line. One paragraph that fuses
        # circadian/world/activity/affect/relationship/user_state/
        # ambient_noise into a single continuous-awareness sentence at
        # the top of the system prompt. Provider returns ``""`` when
        # ``agent.grounding_line_mode == "off"``; the suppression of the
        # underlying granular blocks lives inline in
        # ``assemble_with_budget`` based on the per-turn mode argument.
        self._grounding_line_provider: Callable[[], str] | None = None
        # K16 mode selector: ``"off"`` / ``"replace"`` / ``"split"``.
        # Stored on the assembler (rather than threaded through
        # ``assemble_with_budget``) so :class:`TurnRunner` doesn't need
        # to thread a per-turn arg. ``SessionController`` sets the mode
        # once at boot via :meth:`set_grounding_line_mode` and again on
        # any settings reload. Suppression of granular blocks lives
        # inline in :meth:`assemble_with_budget` keyed off this value.
        self._grounding_line_mode: str = "off"
        # Per-turn dynamic blocks: not part of ``_StaticSlices`` because
        # they change every utterance. ``vocal_tone`` is set immediately
        # before the live turn dispatch by ``SessionController`` after
        # analysing the captured WAV; ``catchphrase`` is set by the
        # speaking-window mining job.
        self._vocal_tone_provider: Callable[[], str] | None = None
        self._catchphrase_provider: Callable[[], str] | None = None
        self._petname_provider: Callable[[], str] | None = None
        self._ambient_noise_provider: Callable[[], str] | None = None
        # Alexia bundle: capability lookup → drives the dynamic overlay
        # grammar block, plus a pajama hint provider for the
        # quiet-conversation cue when auto-outfit resolves to pajamas.
        self._avatar_capabilities_provider: (
            Callable[[], dict[str, bool] | None] | None
        ) = None
        self._pajama_provider: Callable[[], str] | None = None
        # Phase: motion grammar — provider returns the list of motion
        # filename stems (e.g. ``["wave", "nod", "dh"]``) registered in
        # the loaded rig, in declaration order. Crossed with the
        # ``_MOTION_GRAMMAR_DESCRIPTIONS`` registry to decide which
        # ``[[motion:X]]`` lines to advertise.
        self._motion_names_provider: Callable[[], list[str]] | None = None
        self._self_image_path = (
            Path(self_image_path) if self_image_path is not None else None
        )
        self._self_image_cache: tuple[float, str] | None = None
        # Phase 1b: optional cache lookup that returns a pre-fetched RAG
        # block (formatted) for the current ``user_text``. Wired by
        # SessionController.
        self._rag_prefetch_lookup: Callable[[str], str | None] | None = None
        # Phase 2d: optional callable -> list[str] of top self-memories,
        # rendered as bullets after the prose self-image block.
        self._pinned_self_memories_provider: (
            Callable[[], list[str]] | None
        ) = None
        # First-run identity: callable returning the user's display name.
        # Threaded down to the RAG block formatters so the "What you know
        # about <name>" header reflects whatever the user typed into the
        # onboarding modal. Defaults to a generic placeholder when the
        # caller didn't wire it, which is fine for tests / fixtures.
        self._user_display_name_provider: Callable[[], str] | None = None


    # ── public API ────────────────────────────────────────────────────────


    def build(
        self,
        session_key: str,
        user_text: str,
        *,
        context_window: int,
        response_budget: int,
    ) -> list[dict[str, Any]]:
        """Backward-compatible thin wrapper over :meth:`assemble_with_budget`.

        Returns just the ``messages`` list. Callers that need the budget
        accounting should use :meth:`assemble_with_budget` instead.
        """
        messages, _telemetry = self.assemble_with_budget(
            session_key, user_text,
            context_window=context_window,
            response_budget=response_budget,
        )
        return messages

    def assemble_with_budget(
        self,
        session_key: str,
        user_text: str,
        *,
        context_window: int,
        response_budget: int,
        aggressive: bool = False,
    ) -> tuple[list[dict[str, Any]], PromptTelemetry]:
        """Compose the full message list and return per-block telemetry.

        ``aggressive=True`` is used by :class:`TurnRunner` after a synchronous
        compaction when the previous assembly overflowed. It shrinks the
        recent-message window and drops the RAG block (the rolling summary
        already encodes long-term context).
        """
        # P2 (perf backlog): per-phase wall time. Captured via
        # ``_safe_provider(timing_sink=…)`` and ``_timed_phase`` context
        # managers below; folded into ``PromptTelemetry.provider_ms`` at
        # the end so MCP / get_last_response_detail can attribute "this
        # turn was slow because of <provider>" without a custom log
        # dive.
        provider_ms: dict[str, float] = {}
        rag_lookup_ms = 0.0
        assemble_started_at = time.perf_counter()
        # P22: new assembly -> invalidate the providers' shared
        # recent-history memo. Wraps at a large bound so it never grows
        # unboundedly across a long-lived process.
        self._assembly_seq = (self._assembly_seq + 1) & 0x7FFFFFFF
        # Listening-window cache hit (Phase 3): if the slices we built
        # speculatively while the user was speaking still match the
        # session's static state, skip the persona/self-image disk reads,
        # the two SQLite queries, and the eight inner-life providers.
        # Otherwise build fresh and stash the result for next turn.
        recent_window = (
            self._recent_window if not aggressive else max(2, self._recent_window // 2)
        )
        # P3: compute the cheap head signature once. On a hit it lets us
        # skip the ``get_messages`` + ``get_latest_summary`` round-trips;
        # on a miss we still fall through to the full validation below.
        try:
            fast_key = self._fast_slice_signature(
                session_key, recent_window, aggressive,
            )
        except Exception:
            fast_key = None

        cached = self._slice_cache.get(session_key)
        slice_event = "miss"
        if cached is not None and fast_key is not None and (
            self._slice_head_sig.get(session_key) == fast_key
        ):
            # Cheap path: nothing the static slices depend on has moved
            # since they were built, so trust the cache without touching
            # the recent-message window or summary row.
            slices = cached
            slice_event = "hit"
        elif cached is not None:
            try:
                # We rebuild history_msgs to recompute the live cache key —
                # this is the same SQL the cache would have run, so when
                # the cache is stale we still pay only one query (we then
                # reuse `live_history` for the build below).
                live_history = self._db.get_messages(session_key, limit=recent_window)
                live_summary = self._db.get_latest_summary(session_key)
                live_already = (
                    int(live_summary.messages_summarized)
                    if (live_summary and live_summary.summary.strip())
                    else 0
                )
                if live_already > 0:
                    live_history = [
                        row for row in live_history
                        if getattr(row, "id", 0) and int(row.id) > live_already
                    ]
                live_key = self._compute_static_cache_key(
                    session_key, live_history, recent_window, aggressive,
                )
            except Exception:
                live_key = None
                live_history = None
                live_summary = None
                live_already = 0
            if live_key is not None and live_key == cached.cache_key:
                slices = cached
                slice_event = "hit"
            else:
                self._slice_cache.pop(session_key, None)
                slices = self._build_static_slices_with_history(
                    session_key,
                    aggressive=aggressive,
                    history_msgs=live_history,
                    summary=live_summary,
                    already_summarized=live_already,
                )
                self._slice_cache[session_key] = slices
            # Refresh the cheap signature so the next turn can fast-path
            # (covers both the confirmed hit and the rebuild).
            if fast_key is not None:
                self._slice_head_sig[session_key] = fast_key
        else:
            slices = self._build_static_slices(session_key, aggressive=aggressive)
            self._slice_cache[session_key] = slices
            if fast_key is not None:
                self._slice_head_sig[session_key] = fast_key
        self._last_slice_cache_event = slice_event

        persona = slices.persona
        self_image_block = slices.self_image_block
        summary = slices.summary_row
        already_summarized = slices.already_summarized
        history_msgs = slices.history_msgs
        ambient = slices.ambient
        mood_hint = slices.mood_hint
        affect_block = slices.affect_block
        circadian_block = slices.circadian_block
        # K27 -- daily personality colour. NOT cached in _StaticSlices
        # because the provider mutates state on two paths: (1) the
        # lazy fallback writes a fresh roll to kv_meta on the first
        # turn after a local-date rollover, and (2) the MCP
        # ``force_day_color`` / ``reroll_day_color`` shortcuts consume
        # one-shot flags. Built on every assembly so those state
        # transitions happen exactly once. Cheap: one kv_get + one
        # date compare on the steady-state path.
        day_color_block = ""
        if self._day_color_provider is not None:
            with _timed_phase(provider_ms, "day_color"):
                try:
                    day_color_block = self._day_color_provider() or ""
                except Exception:
                    log.debug("day_color provider raised", exc_info=True)
                    day_color_block = ""
        profile_block = slices.profile_block
        user_state_block = slices.user_state_block
        relationship_block = slices.relationship_block
        arc_block = slices.arc_block
        agenda_block = slices.agenda_block
        goals_block = slices.goals_block
        interest_map_block = slices.interest_map_block

        memory_block = ""
        rag_prefetch_event = "skip"
        rag_phase_start = time.perf_counter()
        if not aggressive:
            # Phase 1b: try the speculative pre-fetch cache first. On a hit
            # we skip the embed + multi-source retrieval entirely, saving
            # ~80-300ms on the hot path. Misses fall through to live
            # retrieval below.
            if self._rag_prefetch_lookup is not None:
                try:
                    cached_block = self._rag_prefetch_lookup(user_text)
                except Exception:
                    log.debug("rag prefetch lookup raised", exc_info=True)
                    cached_block = None
                if cached_block:
                    memory_block = cached_block
                    rag_prefetch_event = "hit"
                else:
                    rag_prefetch_event = "miss"
            # Prefer RAG (memories + messages + documents merged) when available.
            # Falls back to legacy single-source MemoryRetriever otherwise so we
            # stay functional on environments without LanceDB (probe failure).
            if not memory_block and self._rag_retriever is not None:
                try:
                    recent_turns = [
                        (row.content or "").strip()
                        for row in history_msgs[-3:]
                        if (row.content or "").strip()
                    ]
                    memory_block = self._rag_retriever.block_for(
                        user_text,
                        recent_turns=recent_turns,
                        exclude_session_id=session_key,
                        user_display_name=self._resolve_user_display_name(),
                    )
                except Exception:
                    log.debug("rag retrieval failed", exc_info=True)
                    memory_block = ""
            if not memory_block and self._memory_retriever is not None:
                try:
                    memory_block = self._memory_retriever.block_for(
                        user_text,
                        user_display_name=self._resolve_user_display_name(),
                    )
                except Exception:
                    log.debug("memory retrieval failed", exc_info=True)
                    memory_block = ""
        # P2: capture wall time of the RAG phase (prefetch lookup + live
        # retrieval + legacy fallback). On ``aggressive=True`` builds the
        # whole block is skipped, so ``rag_lookup_ms`` reads ~0.
        rag_lookup_ms = (time.perf_counter() - rag_phase_start) * 1000.0

        summary_text = ""
        if summary and summary.summary.strip():
            summary_text = "Earlier conversation (summary):\n" + summary.summary.strip()
        # K21: fresh-eyes thread note (cached in static slices, session-stable
        # until the ThreadResummaryWorker re-drafts). Sits in T2 right after
        # the rolling summary so the cache prefix stays intact.
        thread_note_text = slices.thread_note

        # Per-turn dynamic blocks read fresh on every assemble (NOT cached
        # in static slices). Vocal-tone is captured by the live-capture
        # path; catchphrase / pet-name / ambient noise come from cheap
        # store reads. Narrative reads from the prepared-nudge store,
        # whose contents change between turns even when ``history_max_id``
        # doesn't move (NarrativeWeaver runs every N turns, ProactiveDirector
        # consumes nudges) — caching it would surface stale text.
        vocal_tone_block = _safe_provider(
            self._vocal_tone_provider,
            timing_sink=provider_ms, timing_name="vocal_tone",
        )
        catchphrase_block = _safe_provider(
            self._catchphrase_provider,
            timing_sink=provider_ms, timing_name="catchphrase",
        )
        petname_block = _safe_provider(
            self._petname_provider,
            timing_sink=provider_ms, timing_name="petname",
        )
        ambient_noise_block = _safe_provider(
            self._ambient_noise_provider,
            timing_sink=provider_ms, timing_name="ambient_noise",
        )
        pajama_block = _safe_provider(
            self._pajama_provider,
            timing_sink=provider_ms, timing_name="pajama",
        )
        narrative_block = _safe_provider(
            self._narrative_provider,
            timing_sink=provider_ms, timing_name="narrative",
        )
        # Aiko's room: read fresh every turn so cookie consumption / state
        # changes from agent tools surface immediately in the next prompt.
        # Dropped in aggressive mode to free tokens for history.
        world_block = "" if aggressive else _safe_provider(
            self._world_provider,
            timing_sink=provider_ms, timing_name="world",
        )
        # Activity awareness: read fresh so a user who alt-tabs to a
        # different app between turns is reflected in the next prompt.
        # Dropped in aggressive mode for the same reason as world.
        activity_block = "" if aggressive else _safe_provider(
            self._activity_provider,
            timing_sink=provider_ms, timing_name="activity",
        )
        # Schema v7: shared-moment anniversary line + relationship-axes
        # summary. Both empty strings most turns; the anniversary
        # provider also stamps the chosen moment so it won't fire
        # repeatedly inside the rate-limit window. Dropped in
        # aggressive mode.
        anniversary_block = "" if aggressive else _safe_provider(
            self._anniversary_provider,
            timing_sink=provider_ms, timing_name="anniversary",
        )
        # J8: milestone-celebration one-shot. Not called in aggressive mode
        # so the slot is deferred rather than consumed under context
        # pressure (a milestone is a once-per-threshold beat we don't want
        # to lose).
        milestone_block = "" if aggressive else _safe_provider(
            self._milestone_provider,
            timing_sink=provider_ms, timing_name="milestone",
        )
        axes_block = "" if aggressive else _safe_provider(
            self._axes_provider,
            timing_sink=provider_ms, timing_name="axes",
        )
        # F2: knowledge-gap "wondering about" line. Query-aware (so the
        # block picks the gap closest to what the user is talking about
        # right now) which is why it's not a zero-arg provider.
        knowledge_gaps_block = ""
        if not aggressive and self._knowledge_gaps_provider is not None:
            with _timed_phase(provider_ms, "knowledge_gaps"):
                try:
                    knowledge_gaps_block = self._knowledge_gaps_provider(user_text) or ""
                except Exception:
                    log.debug("knowledge gaps provider raised", exc_info=True)
                    knowledge_gaps_block = ""

        # F10f: surface a self-aware "I keep circling X but never dug in"
        # cue when the live turn is on a dense-but-unresearched topic.
        # Query-aware (the provider gates on user_text topic relevance),
        # dropped in aggressive mode like the F2 gap block above.
        knowledge_gap_notice_block = ""
        if not aggressive and self._knowledge_gap_notice_provider is not None:
            with _timed_phase(provider_ms, "knowledge_gap_notice"):
                try:
                    knowledge_gap_notice_block = (
                        self._knowledge_gap_notice_provider(user_text) or ""
                    )
                except Exception:
                    log.debug(
                        "knowledge gap notice provider raised", exc_info=True
                    )
                    knowledge_gap_notice_block = ""

        # F10h: per-cluster affect — nudge tone when the live turn lands
        # on a warm / tender cluster. Query-aware (the provider embeds
        # user_text + matches a cluster), dropped in aggressive mode.
        topic_temperature_block = ""
        if not aggressive and self._topic_temperature_provider is not None:
            with _timed_phase(provider_ms, "topic_temperature"):
                try:
                    topic_temperature_block = (
                        self._topic_temperature_provider(user_text) or ""
                    )
                except Exception:
                    log.debug(
                        "topic temperature provider raised", exc_info=True
                    )
                    topic_temperature_block = ""

        # F10i: per-topic confidence — hedge on a thin cluster, speak with
        # earned familiarity on a rich one. Query-aware, dropped in
        # aggressive mode.
        topic_confidence_block = ""
        if not aggressive and self._topic_confidence_provider is not None:
            with _timed_phase(provider_ms, "topic_confidence"):
                try:
                    topic_confidence_block = (
                        self._topic_confidence_provider(user_text) or ""
                    )
                except Exception:
                    log.debug(
                        "topic confidence provider raised", exc_info=True
                    )
                    topic_confidence_block = ""

        # K64a: associative wandering — surface a "funny, this reminds me
        # of ..." connection between the live topic and a distant cluster.
        # Query-aware (the provider gates on user_text topic relevance),
        # dropped in aggressive mode.
        associative_wander_block = ""
        if not aggressive and self._associative_wander_provider is not None:
            with _timed_phase(provider_ms, "associative_wander"):
                try:
                    associative_wander_block = (
                        self._associative_wander_provider(user_text) or ""
                    )
                except Exception:
                    log.debug(
                        "associative wander provider raised", exc_info=True
                    )
                    associative_wander_block = ""

        # K64b: interest drift — surface a slow "I've been drawn to X
        # lately" / "X has gone quiet" register shift when the live turn is
        # on a drifting topic. Query-aware, dropped in aggressive mode.
        interest_drift_block = ""
        if not aggressive and self._interest_drift_provider is not None:
            with _timed_phase(provider_ms, "interest_drift"):
                try:
                    interest_drift_block = (
                        self._interest_drift_provider(user_text) or ""
                    )
                except Exception:
                    log.debug(
                        "interest drift provider raised", exc_info=True
                    )
                    interest_drift_block = ""

        # K61: on informational turns, steer Aiko toward learned
        # specifics (F9 ``knowledge`` / G3 ``curiosity_finding`` rows)
        # instead of survey-hedging. Query-aware, local-only (one
        # regex + one embed + a cosine scan), dropped under aggressive.
        knowledge_grounding_block = ""
        if (
            not aggressive
            and self._knowledge_grounding_provider is not None
        ):
            with _timed_phase(provider_ms, "knowledge_grounding"):
                try:
                    knowledge_grounding_block = (
                        self._knowledge_grounding_provider(user_text) or ""
                    )
                except Exception:
                    log.debug(
                        "knowledge grounding provider raised",
                        exc_info=True,
                    )
                    knowledge_grounding_block = ""

        belief_gaps_block = ""
        if not aggressive and self._belief_gaps_provider is not None:
            with _timed_phase(provider_ms, "belief_gaps"):
                try:
                    belief_gaps_block = self._belief_gaps_provider() or ""
                except Exception:
                    log.debug("belief gaps provider raised", exc_info=True)
                    belief_gaps_block = ""

        # K17 — clarification-repair one-shot. Same shape as the K2
        # belief-gap provider: stateless from the assembler's POV, the
        # post-turn detector stashes a result and the inner-life
        # provider clears the slot on the read here. Resolved before
        # rendering so the system_parts ordering stays explicit.
        # NOT gated on aggressive mode -- a "you missed his point"
        # cue is exactly the kind of thing aggressive mode wants to
        # keep, since it directly steers the next reply.
        clarification_block = ""
        if self._clarification_provider is not None:
            with _timed_phase(provider_ms, "clarification"):
                try:
                    clarification_block = self._clarification_provider() or ""
                except Exception:
                    log.debug("clarification provider raised", exc_info=True)
                    clarification_block = ""

        # K20 — metacognitive calibration. Sibling of the
        # clarification provider above; renders a hedge cue when the
        # per-user CalibrationState has dropped below threshold.
        # NOT gated on aggressive mode -- a "Jacob's been double-
        # checking you" cue is steering-critical (it tilts the
        # whole turn's register) and exactly the kind of thing
        # aggressive mode wants to keep.
        calibration_block = ""
        if self._calibration_provider is not None:
            with _timed_phase(provider_ms, "calibration"):
                try:
                    calibration_block = self._calibration_provider() or ""
                except Exception:
                    log.debug("calibration provider raised", exc_info=True)
                    calibration_block = ""

        # K24 — sensory anchoring layer. Adaptive per-arc cadence
        # that occasionally surfaces a "small physical beat
        # available" cue so Aiko can substitute a sensory detail
        # for an emotional statement. Resolved here so the
        # ``system_parts`` ordering stays explicit; placement is
        # *after* world_block / activity_block in the assembled
        # prompt (the body beat is texture on top of the room
        # location). Gated on aggressive mode: when context is
        # tight the body beat is a graceful skip.
        sensory_anchor_block = ""
        if self._sensory_anchor_provider is not None and not aggressive:
            with _timed_phase(provider_ms, "sensory_anchor"):
                try:
                    sensory_anchor_block = (
                        self._sensory_anchor_provider() or ""
                    )
                except Exception:
                    log.debug(
                        "sensory_anchor provider raised", exc_info=True,
                    )
                    sensory_anchor_block = ""

        # K8 — affect-rupture one-shot. Sibling of the clarification
        # provider; same one-shot contract, same not-gated-on-
        # aggressive policy (a "their mood just dipped" cue is
        # critical signal Aiko needs to soften the next reply).
        rupture_block = ""
        if self._rupture_provider is not None:
            with _timed_phase(provider_ms, "rupture"):
                try:
                    rupture_block = self._rupture_provider() or ""
                except Exception:
                    log.debug("rupture provider raised", exc_info=True)
                    rupture_block = ""

        # K45 — mood-inertia one-shot. Same one-shot contract as the
        # rupture provider and same not-gated-on-aggressive policy:
        # the provider clears the pending slot when it renders, so
        # dropping the block after the read would silently lose the
        # "let the words catch up" beat.
        mood_inertia_block = ""
        if self._mood_inertia_provider is not None:
            with _timed_phase(provider_ms, "mood_inertia"):
                try:
                    mood_inertia_block = self._mood_inertia_provider() or ""
                except Exception:
                    log.debug("mood-inertia provider raised", exc_info=True)
                    mood_inertia_block = ""

        # K38 — self-correction one-shot. Sibling of the rupture
        # provider; same one-shot contract and same not-gated-on-
        # aggressive policy (an owed correction must land even when the
        # prompt is trimmed).
        self_correction_block = ""
        if self._self_correction_provider is not None:
            with _timed_phase(provider_ms, "self_correction"):
                try:
                    self_correction_block = self._self_correction_provider() or ""
                except Exception:
                    log.debug("self-correction provider raised", exc_info=True)
                    self_correction_block = ""

        # K43 — promise follow-through one-shot. Same one-shot contract
        # as rupture/self-correction (provider consumes a pending slot)
        # and the same not-gated-on-aggressive policy: the provider
        # clears the kv slot when it renders, so dropping the block
        # after the read would silently lose an owed beat.
        promise_followthrough_block = ""
        if self._promise_followthrough_provider is not None:
            with _timed_phase(provider_ms, "promise_followthrough"):
                try:
                    promise_followthrough_block = (
                        self._promise_followthrough_provider() or ""
                    )
                except Exception:
                    log.debug(
                        "promise-followthrough provider raised", exc_info=True,
                    )
                    promise_followthrough_block = ""

        # K23 — subtle misattunement detector. Per-turn detector
        # reading the last assistant reply length, this user message
        # length, and K6's last_band. Empty on most turns (cooldown +
        # narrow trigger gates). Not gated on aggressive mode -- the
        # "pull back, lighter" instruction is exactly the kind of
        # steering an aggressive turn benefits from.
        misattunement_block = ""
        if (
            getattr(self, "_misattunement_provider", None) is not None
        ):
            with _timed_phase(provider_ms, "misattunement"):
                try:
                    misattunement_block = self._misattunement_provider(user_text) or ""
                except Exception:
                    log.debug("misattunement provider raised", exc_info=True)
                    misattunement_block = ""

        # K29 — opinion injection detector. Per-turn detector that
        # checks the live user message against Aiko's stored
        # ``kind="self"`` stance memories and fires a "you've got a
        # different read on this" cue when a real contradiction
        # lands. Empty on most turns (no stance touched + heuristic
        # gate + cooldown + per-session cap). Not gated on
        # aggressive mode -- a steering signal to disagree where it
        # fits is exactly what tight budgets need.
        opinion_injection_block = ""
        if (
            getattr(self, "_opinion_injection_provider", None) is not None
        ):
            with _timed_phase(provider_ms, "opinion_injection"):
                try:
                    opinion_injection_block = (
                        self._opinion_injection_provider(user_text) or ""
                    )
                except Exception:
                    log.debug(
                        "opinion-injection provider raised", exc_info=True
                    )
                    opinion_injection_block = ""

        # J5 reconnection ritual. Self-computes the gap at assembly time
        # (so it can colour the FIRST reply back after a long absence),
        # closeness-scaled, one-shot per return. Leads the gap cluster.
        # NOT gated on aggressive mode -- a returning user's welcome is
        # exactly the wrong thing to drop under context pressure, and the
        # block is short.
        reconnection_block = ""
        if getattr(self, "_reconnection_provider", None) is not None:
            with _timed_phase(provider_ms, "reconnection"):
                try:
                    reconnection_block = self._reconnection_provider() or ""
                except Exception:
                    log.debug("reconnection provider raised", exc_info=True)
                    reconnection_block = ""

        # K14 typed-mode absence-curiosity one-shot. Empty on most
        # turns (only fires when the post-turn tracker stashed an
        # absence_seconds in the configured band). NOT gated on
        # aggressive mode -- this cue is the entire point of K14
        # typed-mode, and dropping it would silently break the
        # behaviour Jacob signed off on.
        absence_curiosity_block = ""
        if (
            getattr(self, "_absence_curiosity_provider", None) is not None
        ):
            with _timed_phase(provider_ms, "absence_curiosity"):
                try:
                    absence_curiosity_block = (
                        self._absence_curiosity_provider() or ""
                    )
                except Exception:
                    log.debug(
                        "absence curiosity provider raised", exc_info=True,
                    )
                    absence_curiosity_block = ""

        # K28 "What I've been turning over" one-shot. Sibling of the
        # K14 block above: same post-turn-armed mechanic but a longer
        # gap threshold (90 min default) and a different render -- the
        # provider runs a picker over recent reflection memories and
        # returns a "Turning over: ..." cue or empty. NOT gated on
        # aggressive mode (same rationale as K14: the cue IS the
        # entire feature, dropping it silently would defeat the point).
        # NOT in the K16 ``replace`` suppression set: the fused
        # grounding line never carries reflection content, so K28 is
        # purely additive on top.
        turning_over_block = ""
        if (
            getattr(self, "_turning_over_provider", None) is not None
        ):
            with _timed_phase(provider_ms, "turning_over"):
                try:
                    turning_over_block = (
                        self._turning_over_provider() or ""
                    )
                except Exception:
                    log.debug(
                        "turning_over provider raised", exc_info=True,
                    )
                    turning_over_block = ""

        # K36 "things I did while you were away" one-shot. Runs AFTER
        # turning_over so it can read the just-set _gap_cue_surfaced flag
        # and defer (only one of the two gap cues surfaces per return).
        away_activities_block = ""
        if (
            getattr(self, "_away_activities_provider", None) is not None
        ):
            with _timed_phase(provider_ms, "away_activities"):
                try:
                    away_activities_block = (
                        self._away_activities_provider() or ""
                    )
                except Exception:
                    log.debug(
                        "away_activities provider raised", exc_info=True,
                    )
                    away_activities_block = ""

        # K34 "forward curiosity" one-shot. Runs AFTER turning_over and
        # away_activities so it reads their _gap_cue_surfaced flag and
        # defers (only one of the three gap cues surfaces per return).
        forward_curiosity_block = ""
        if (
            getattr(self, "_forward_curiosity_provider", None) is not None
        ):
            with _timed_phase(provider_ms, "forward_curiosity"):
                try:
                    forward_curiosity_block = (
                        self._forward_curiosity_provider() or ""
                    )
                except Exception:
                    log.debug(
                        "forward_curiosity provider raised", exc_info=True,
                    )
                    forward_curiosity_block = ""

        # Follow-up cue: "their plan just happened, you can ask how it
        # went". Time-anchored (not gap-gated), independent of the
        # _gap_cue_surfaced family.
        follow_up_block = ""
        if getattr(self, "_follow_up_provider", None) is not None:
            with _timed_phase(provider_ms, "follow_up"):
                try:
                    follow_up_block = self._follow_up_provider() or ""
                except Exception:
                    log.debug("follow_up provider raised", exc_info=True)
                    follow_up_block = ""

        # J10 appreciation beat. Rare (long cooldown), specific, anchored
        # to a recent positive shared moment. Volatile one-shot so it
        # lives in T6 with the gap family rather than T1 — when it fires
        # it shouldn't bust the stable prefix.
        appreciation_block = ""
        if getattr(self, "_appreciation_provider", None) is not None:
            with _timed_phase(provider_ms, "appreciation"):
                try:
                    appreciation_block = self._appreciation_provider() or ""
                except Exception:
                    log.debug("appreciation provider raised", exc_info=True)
                    appreciation_block = ""

        # J9 reciprocal vulnerability. user_text provider so the low-mood
        # suppression reads the live message. Rare (long cooldown),
        # stage/trust/K15-budget gated.
        reciprocal_vulnerability_block = ""
        if getattr(self, "_reciprocal_vulnerability_provider", None) is not None:
            with _timed_phase(provider_ms, "reciprocal_vulnerability"):
                try:
                    reciprocal_vulnerability_block = (
                        self._reciprocal_vulnerability_provider(user_text) or ""
                    )
                except Exception:
                    log.debug(
                        "reciprocal_vulnerability provider raised",
                        exc_info=True,
                    )
                    reciprocal_vulnerability_block = ""

        # K5 mood-shell tilt. Stateless: derives a one-line emotional
        # directive from current affect + relationship axes + pending
        # moments every turn, returns "" when nothing is notable. NOT
        # gated on aggressive mode (a tonal cue is exactly what
        # aggressive mode wants to keep). Part of the K16 ``replace``
        # suppression set so a unified grounding line doesn't fight
        # with the mood-shell line.
        mood_shell_block = ""
        if getattr(self, "_mood_shell_provider", None) is not None:
            with _timed_phase(provider_ms, "mood_shell"):
                try:
                    mood_shell_block = self._mood_shell_provider() or ""
                except Exception:
                    log.debug("mood shell provider raised", exc_info=True)
                    mood_shell_block = ""

        # K57: directed emotion episode. Takes ``user_text`` for the
        # acknowledgment pass; NOT gated on aggressive mode (a live
        # episode is a register directive — exactly what a tight
        # budget should keep — and the provider consumes one-shot
        # thaw state, so skipping the call could lose the melt beat).
        emotion_episode_block = ""
        if self._emotion_episode_provider is not None:
            with _timed_phase(provider_ms, "emotion_episode"):
                try:
                    emotion_episode_block = (
                        self._emotion_episode_provider(user_text) or ""
                    )
                except Exception:
                    log.debug(
                        "emotion episode provider raised", exc_info=True,
                    )
                    emotion_episode_block = ""

        # K6: per-turn surprise/novelty signal. Same shape as the F2
        # knowledge-gap provider (takes ``user_text``), since the
        # detector scores the live utterance against a rolling
        # centroid. Returns "" on silent / warmup / cooldown turns,
        # which is the common case.
        novelty_block = ""
        if not aggressive and self._novelty_provider is not None:
            with _timed_phase(provider_ms, "novelty"):
                try:
                    novelty_block = self._novelty_provider(user_text) or ""
                except Exception:
                    log.debug("novelty provider raised", exc_info=True)
                    novelty_block = ""

        # K18: topic-stagnation signal. Sibling of K6 above and runs
        # immediately after, so the stagnation detector can read the
        # just-populated ``last_distance``/``last_band`` off the
        # ``NoveltyDetector`` to decide whether to fire (and whether
        # to enter post-novelty suppression). Same provider shape
        # (takes ``user_text`` for symmetry, though the streak
        # detector itself doesn't read it). Returns "" on the common
        # silent / warmup / cooldown / suppressed turn.
        stagnation_block = ""
        if not aggressive and self._stagnation_provider is not None:
            with _timed_phase(provider_ms, "stagnation"):
                try:
                    stagnation_block = self._stagnation_provider(user_text) or ""
                except Exception:
                    log.debug("stagnation provider raised", exc_info=True)
                    stagnation_block = ""

        # Anti-rut layer: AikoStylePatternTracker. Watches Aiko's
        # *own* recent assistant turns and emits an opener / question
        # / length "Heads-up" cue when she ruts. No args -- the
        # post-turn pipeline already fed the previous reply's
        # stripped text into the tracker. Same dropping discipline
        # as K6/K18 (aggressive mode skips it).
        style_pattern_block = ""
        if not aggressive and self._style_pattern_provider is not None:
            with _timed_phase(provider_ms, "style_pattern"):
                try:
                    style_pattern_block = self._style_pattern_provider() or ""
                except Exception:
                    log.debug("style_pattern provider raised", exc_info=True)
                    style_pattern_block = ""

        # K47: question/share balance share-first cue. Sibling of the
        # anti-rut style_pattern cue; surfaces while the proactive
        # question/share gate is armed (paired with suppression of the
        # question-pushing providers). Dropped in aggressive mode.
        question_balance_block = ""
        if not aggressive and self._question_balance_provider is not None:
            with _timed_phase(provider_ms, "question_balance"):
                try:
                    question_balance_block = (
                        self._question_balance_provider() or ""
                    )
                except Exception:
                    log.debug(
                        "question_balance provider raised", exc_info=True
                    )
                    question_balance_block = ""

        # K48: tease-rhythm banter-budget cue. Sibling of the anti-rut /
        # share-first cues; one-shot, armed post-turn. Dropped in
        # aggressive mode.
        tease_rhythm_block = ""
        if not aggressive and self._tease_rhythm_provider is not None:
            with _timed_phase(provider_ms, "tease_rhythm"):
                try:
                    tease_rhythm_block = self._tease_rhythm_provider() or ""
                except Exception:
                    log.debug("tease_rhythm provider raised", exc_info=True)
                    tease_rhythm_block = ""

        # K30: self-noticing cues. Sibling of ``style_pattern`` (same
        # persona block, same anti-narration rules) -- fans three
        # sub-detectors (agreement streak / flat affect / repeated
        # thought) into one Heads-up cluster. Sits right after the
        # opener / question / length cues so all of Aiko's "patterns
        # I'm in" beats render together. Dropped in aggressive mode
        # like the rest of the rut cluster -- the budget gets the
        # user's message back when context is tight.
        self_noticing_block = ""
        if not aggressive and self._self_noticing_provider is not None:
            with _timed_phase(provider_ms, "self_noticing"):
                try:
                    self_noticing_block = self._self_noticing_provider() or ""
                except Exception:
                    log.debug("self_noticing provider raised", exc_info=True)
                    self_noticing_block = ""

        # K15: self-disclosure / vulnerability budget cue. One-line
        # pacing nudge ("you've shared a lot of softness recently --
        # let yourself stay surface this turn unless a moment really
        # earns it"). Soft enforcement: the persona block teaches
        # Aiko to read the cue but allows real moments to override.
        # NOT dropped under ``aggressive=True`` -- a tight budget is
        # exactly when an over-cap warning matters most (long replies
        # compound over-disclosure). NOT in the K16 grounding-line
        # suppression matrix because it's a pacing cue, not an
        # ambient grounding block.
        vulnerability_budget_block = ""
        if self._vulnerability_budget_provider is not None:
            with _timed_phase(provider_ms, "vulnerability_budget"):
                try:
                    vulnerability_budget_block = (
                        self._vulnerability_budget_provider() or ""
                    )
                except Exception:
                    log.debug(
                        "vulnerability_budget provider raised", exc_info=True,
                    )
                    vulnerability_budget_block = ""

        # K32: one-shot "Jacob just hearted that line" cue armed by
        # the REST handler when the user taps a reaction emoji. The
        # provider drains the queue once it has rendered the cue --
        # the same reaction can't re-fire on the next turn. NOT
        # suppressed under ``aggressive`` (one line, one-shot).
        user_reactions_block = ""
        if self._user_reactions_provider is not None:
            with _timed_phase(provider_ms, "user_reactions"):
                try:
                    user_reactions_block = (
                        self._user_reactions_provider() or ""
                    )
                except Exception:
                    log.debug(
                        "user_reactions provider raised", exc_info=True,
                    )
                    user_reactions_block = ""

        # Brain-orchestration chunk 6: running-tasks state block.
        # Sibling of the task_cues_block below. State first
        # (what's still working), deltas after (what just finished
        # or is blocked). Empty string is the common case (no
        # tasks running) — typical turn pays a single dict lookup.
        running_tasks_block = ""
        if self._running_tasks_provider is not None:
            with _timed_phase(provider_ms, "running_tasks"):
                try:
                    running_tasks_block = (
                        self._running_tasks_provider() or ""
                    )
                except Exception:
                    log.debug(
                        "running_tasks provider failed", exc_info=True,
                    )
                    running_tasks_block = ""

        # Brain-orchestration chunk 5: parked task cues. The
        # provider drains :class:`TaskCueStore` (cancelling any
        # pending escalation in the process so a cue that surfaces
        # naturally doesn't also escalate) and returns a multi-line
        # T6 block. Empty string is the common case (no tasks
        # parked) — every other turn this is a one-line provider
        # cost. The provider itself owns the rendering, so
        # ``assemble_with_budget`` just shuttles the string through.
        task_cues_block = ""
        if self._task_cues_provider is not None:
            with _timed_phase(provider_ms, "task_cues"):
                try:
                    task_cues_block = self._task_cues_provider() or ""
                except Exception:
                    log.debug(
                        "task_cues provider failed", exc_info=True,
                    )
                    task_cues_block = ""

        # K31: physical-budget cue. Renders only when Aiko has been
        # physical with the user a lot today (intimate-gesture stack
        # hit or any kind's daily cap was hit). Sibling of K15.
        touch_state_block = ""
        if self._touch_state_provider is not None:
            with _timed_phase(provider_ms, "touch_state"):
                try:
                    touch_state_block = self._touch_state_provider() or ""
                except Exception:
                    log.debug(
                        "touch_state provider raised", exc_info=True,
                    )
                    touch_state_block = ""

        # D2 Part B — in-chat attachment turn hint. One line listing the
        # files the user attached to this turn's message + a nudge to
        # act on them via ``start_workflow``. Silent when nothing's
        # attached.
        attachments_block = ""
        if self._attachments_provider is not None:
            with _timed_phase(provider_ms, "attachments"):
                try:
                    attachments_block = self._attachments_provider() or ""
                except Exception:
                    log.debug(
                        "attachments provider raised", exc_info=True,
                    )
                    attachments_block = ""

        # K13: stylometric mirror. One short "How Jacob writes lately"
        # line that shapes Aiko's register across days. Unlike the
        # K6/K18/anti-rut cues this block is intentionally NOT gated
        # on ``aggressive`` -- if the budget is tight, register is
        # still the first thing we want to preserve. The provider
        # already returns "" during warmup or when every axis is
        # default, so the line costs zero on the common new-user /
        # neutral-register turn.
        style_signal_block = ""
        if self._style_signal_provider is not None:
            with _timed_phase(provider_ms, "style_signal"):
                try:
                    style_signal_block = self._style_signal_provider() or ""
                except Exception:
                    log.debug("style_signal provider raised", exc_info=True)
                    style_signal_block = ""

        # K9: "Quiet curiosity" bullet — topics Aiko has been quietly
        # wondering about that haven't come up yet. Sits between the
        # stagnation cue and the knowledge-gap cue so the three
        # inner-life surfaces ("we've been circling", "I'm wondering",
        # "I'm curious about") cluster together. Empty on cold-start
        # / when the seed worker hasn't written anything yet.
        # Dropped in aggressive mode -- the budget should focus on
        # the user's message, not on cued asides.
        curiosity_seeds_block = ""
        if not aggressive and self._curiosity_seeds_provider is not None:
            curiosity_seeds_block = _safe_provider(
                self._curiosity_seeds_provider,
                timing_sink=provider_ms,
                timing_name="curiosity_seeds",
            )

        # K52: wants ledger. Same aggressive-mode posture as the
        # curiosity seeds — a tight budget should focus on the
        # user's message, not on Aiko's agenda.
        wants_block = ""
        if not aggressive and self._wants_provider is not None:
            wants_block = _safe_provider(
                self._wants_provider,
                timing_sink=provider_ms,
                timing_name="wants",
            )

        # K54: topic appetite. Same aggressive-mode posture as the
        # wants block (it's a permission slip, not a directive), and
        # it must run after the wants provider so the ledger is
        # matured for this turn, and after the K18 stagnation
        # provider so ``last_mean`` reflects the live conversation.
        topic_appetite_block = ""
        if not aggressive and self._topic_appetite_provider is not None:
            topic_appetite_block = _safe_provider(
                self._topic_appetite_provider,
                timing_sink=provider_ms,
                timing_name="topic_appetite",
            )

        # K59: tease-ledger collection opportunity. Same posture as
        # wants / topic_appetite — a permission slip, dropped under
        # aggressive (the provider only stamps offered/cooldown when
        # it actually fires, so a skipped call loses nothing).
        tease_ledger_block = ""
        if not aggressive and self._tease_ledger_provider is not None:
            tease_ledger_block = _safe_provider(
                self._tease_ledger_provider,
                timing_sink=provider_ms,
                timing_name="tease_ledger",
            )

        # K53: initiative directive. NOT gated on aggressive mode —
        # the director's counter advances on every evaluated turn, so
        # dropping the call here would silently lose a scheduled
        # beat; the provider itself fires rarely (cadence + gates).
        initiative_block = ""
        if self._initiative_provider is not None:
            with _timed_phase(provider_ms, "initiative"):
                try:
                    initiative_block = (
                        self._initiative_provider(user_text) or ""
                    )
                except Exception:
                    log.debug("initiative provider raised", exc_info=True)
                    initiative_block = ""

        # K55: thread-ownership evaluation. NOT gated on aggressive
        # mode — the provider consumes one-shot state (the opened
        # thread), so skipping the call would silently lose the
        # single granted return; it renders rarely (only on a pivot
        # away from a thread Aiko opened in the previous turn).
        thread_ownership_block = ""
        if self._thread_ownership_provider is not None:
            with _timed_phase(provider_ms, "thread_ownership"):
                try:
                    thread_ownership_block = (
                        self._thread_ownership_provider(user_text) or ""
                    )
                except Exception:
                    log.debug(
                        "thread ownership provider raised", exc_info=True,
                    )
                    thread_ownership_block = ""

        # K16: unified ambient grounding line. Always built (the
        # provider itself short-circuits to ``""`` when the mode is
        # ``off``); the suppression of the granular ambient blocks
        # happens just below this block. Timing lands in the same
        # ``provider_ms`` dict as every other provider so MCP
        # ``get_last_response_detail`` can attribute the cost.
        grounding_block = ""
        if not aggressive and self._grounding_line_provider is not None:
            grounding_block = _safe_provider(
                self._grounding_line_provider,
                timing_sink=provider_ms, timing_name="grounding_line",
            )
        # Suppression matrix (see AgentSettings.grounding_line_mode):
        #   off     -> grounding_block already "" via the provider; no
        #              granular suppression.
        #   split   -> drop {circadian, ambient_noise, world, activity}.
        #   replace -> drop {circadian, ambient_noise, affect, mood
        #              hint, relationship, user_state, world, activity}.
        # Anniversary, profile, pajama, knowledge_gaps, belief_gaps,
        # novelty, stagnation, agenda, axes, petname, vocal_tone,
        # catchphrase, narrative, arc, day_color are NEVER suppressed —
        # they each carry data that fusing dilutes. K27 day_color is
        # explicitly a trend/phase block (slow daily under-current),
        # not a situational block, so it survives both modes.
        grounding_mode = self._grounding_line_mode
        if grounding_block and grounding_mode in ("split", "replace"):
            circadian_block = ""
            ambient_noise_block = ""
            world_block = ""
            activity_block = ""
            if grounding_mode == "replace":
                affect_block = ""
                mood_hint = ""
                relationship_block = ""
                user_state_block = ""
                # K5 mood-shell folds affect colour into a single tonal
                # line, which is exactly what the K16 unified grounding
                # line replaces. Drop it in ``replace`` mode so the two
                # don't double-up; keep it in ``split`` because the
                # mood-shell line lives in the "trend / phase" cluster
                # that ``split`` preserves.
                mood_shell_block = ""

        # Alexia bundle: capability lookup is *not* a string provider —
        # it returns the raw flags so we can build the overlay /
        # outfit grammar dynamically per-prompt. Defensive: swallow
        # any provider error.
        capabilities: dict[str, bool] | None = None
        if self._avatar_capabilities_provider is not None:
            try:
                capabilities = self._avatar_capabilities_provider()
            except Exception:
                log.debug("avatar capabilities provider raised", exc_info=True)
                capabilities = None
        overlay_grammar_block = _build_overlay_grammar_addendum(capabilities)
        outfit_grammar_block = _build_outfit_grammar_addendum(capabilities)
        motion_names: list[str] = []
        if self._motion_names_provider is not None:
            try:
                motion_names = list(self._motion_names_provider() or [])
            except Exception:
                log.debug("motion names provider raised", exc_info=True)
                motion_names = []
        motion_grammar_block = _build_motion_grammar_addendum(motion_names)

        # K51 — cue-register rotation. Every producer emits the literal
        # ``Heads-up: ...`` (single audit point); here, at the last
        # moment before layout, the prefix is rotated across a few
        # register shapes keyed on a per-turn seed + running ordinal so
        # two cues in one prompt never share a shape. The seed is
        # derived from (user_text, history length) — deterministic
        # across the tool pass and streaming pass of the same turn, no
        # clock, no RNG. All rotated blocks live in the uncached T5/T6
        # tail, so this has zero prompt-cache impact. The shared-prefix
        # lint runs regardless of the toggle to catch future template
        # regressions.
        cue_block_names = (
            "mood_inertia_block",
            "clarification_block",
            "calibration_block",
            "rupture_block",
            "self_correction_block",
            "promise_followthrough_block",
            "misattunement_block",
            "opinion_injection_block",
            "novelty_block",
            "stagnation_block",
            "style_pattern_block",
            "question_balance_block",
            "tease_rhythm_block",
            "self_noticing_block",
            "user_reactions_block",
        )
        cue_blocks = {
            "mood_inertia_block": mood_inertia_block,
            "clarification_block": clarification_block,
            "calibration_block": calibration_block,
            "rupture_block": rupture_block,
            "self_correction_block": self_correction_block,
            "promise_followthrough_block": promise_followthrough_block,
            "misattunement_block": misattunement_block,
            "opinion_injection_block": opinion_injection_block,
            "novelty_block": novelty_block,
            "stagnation_block": stagnation_block,
            "style_pattern_block": style_pattern_block,
            "question_balance_block": question_balance_block,
            "tease_rhythm_block": tease_rhythm_block,
            "self_noticing_block": self_noticing_block,
            "user_reactions_block": user_reactions_block,
        }
        if self._cue_rotation_enabled:
            seed = cue_register.turn_seed(user_text, len(history_msgs))
            ordinal = 0
            for name in cue_block_names:
                block = cue_blocks[name]
                if not block:
                    continue
                lines = cue_register.count_cue_lines(block)
                if lines == 0:
                    continue
                cue_blocks[name] = cue_register.rotate_cue_prefix(
                    block, seed=seed, ordinal=ordinal,
                )
                ordinal += lines
            mood_inertia_block = cue_blocks["mood_inertia_block"]
            clarification_block = cue_blocks["clarification_block"]
            calibration_block = cue_blocks["calibration_block"]
            rupture_block = cue_blocks["rupture_block"]
            self_correction_block = cue_blocks["self_correction_block"]
            promise_followthrough_block = cue_blocks[
                "promise_followthrough_block"
            ]
            misattunement_block = cue_blocks["misattunement_block"]
            opinion_injection_block = cue_blocks["opinion_injection_block"]
            novelty_block = cue_blocks["novelty_block"]
            stagnation_block = cue_blocks["stagnation_block"]
            style_pattern_block = cue_blocks["style_pattern_block"]
            question_balance_block = cue_blocks["question_balance_block"]
            tease_rhythm_block = cue_blocks["tease_rhythm_block"]
            self_noticing_block = cue_blocks["self_noticing_block"]
            user_reactions_block = cue_blocks["user_reactions_block"]
        offenders = cue_register.lint_shared_prefixes(
            list(cue_blocks.values()),
        )
        for prefix, count in offenders:
            log.info("cue-lint: prefix=%r count=%d", prefix, count)

        # Layout follows the prompt-cache prefix-stability ladder (see
        # ``_PROMPT_BLOCK_TIERS`` near the top of the file and
        # ``docs/prompt-caching.md``). Strictly: T0 stable -> T6
        # per-turn detectors. Within each tier the existing behavioural
        # cluster comments still apply (e.g. K28 turning_over must
        # follow K14 absence_curiosity — both T6).
        system_parts: list[str] = []

        # ── T0: STABLE ────────────────────────────────────────────────
        # Persona + grammar addenda + self_image + narrative + profile +
        # petname + catchphrase. THIS IS THE CACHE PREFIX — adding any
        # per-turn content above this point invalidates the prefix and
        # collapses the OpenAI cache hit-rate to ~0.
        if persona:
            system_parts.append(persona)
            # Phase 1c: speech grammar addendum sits immediately after the
            # persona so the model picks up the [[laugh]] / [[sigh]] /
            # [[gasp]] / [[hum]] grammar without us editing the
            # user-customisable persona file. Constant cost; under 60
            # tokens.
            system_parts.append(
                build_speech_grammar_addendum(self._resolve_user_display_name()),
            )
            if overlay_grammar_block:
                system_parts.append(overlay_grammar_block)
            if outfit_grammar_block:
                system_parts.append(outfit_grammar_block)
            if motion_grammar_block:
                system_parts.append(motion_grammar_block)
            # K31 soft physicality: ``[[touch:KIND]]`` grammar. Lands
            # in the same cluster as the other tag grammars so the
            # LLM reads the full stage-direction vocabulary together.
            # Backend gates (axes / cooldown / daily cap) silently
            # drop unsupported requests, so it's safe to advertise
            # every kind unconditionally.
            system_parts.append(_TOUCH_GRAMMAR_ADDENDUM)
        if self_image_block:
            system_parts.append(self_image_block)
        if narrative_block:
            system_parts.append(narrative_block)
        if profile_block:
            system_parts.append(profile_block)
        if petname_block:
            system_parts.append(petname_block)
        # K9 catchphrases live in T0: they're an extracted-from-history
        # stable user fact ("Jacob says 'lol no'") that only mutates
        # when the catchphrase miner runs (hourly idle worker). Sits
        # next to petname because both encode "how this person talks".
        if catchphrase_block:
            system_parts.append(catchphrase_block)

        # ── T1: SEMI-STABLE (per-arc / per-day) ──────────────────────
        # Relationship / axes / arc / agenda / goals / day_color /
        # anniversary. Changes a few times a day at most.
        if relationship_block:
            system_parts.append(relationship_block)
        # Anniversary + axes sit right after the relationship block so
        # the three "how do we know each other" pieces cluster together
        # in the system prompt.
        if anniversary_block:
            system_parts.append(anniversary_block)
        if milestone_block:
            system_parts.append(milestone_block)
        if axes_block:
            system_parts.append(axes_block)
        if arc_block:
            system_parts.append(arc_block)
        if agenda_block:
            system_parts.append(agenda_block)
        if goals_block:
            # K1: "Aiko's quiet long-term goals." One short bullet
            # block listing 1-3 active goals plus the latest progress
            # note when there's room. Lands immediately after agenda
            # so the "things Aiko is carrying" cluster reads as
            # "follow-ups for you (agenda) -> what she's been
            # working on herself (goals)". Empty until the worker
            # bootstrap or a manual write seeds the ring; dropped
            # in aggressive mode alongside agenda.
            system_parts.append(goals_block)
        if interest_map_block:
            # F10e: "interest map" -- the topic threads Aiko and the user
            # keep returning to, derived from the topic graph's labelled
            # clusters (no per-turn LLM cost). Lands right after goals so
            # the "what she's carrying" cluster reads agenda -> goals ->
            # the broader interests that frame them. Dropped in aggressive
            # mode alongside agenda / goals; empty until the F10a label
            # worker has named at least one dense cluster.
            system_parts.append(interest_map_block)
        if day_color_block:
            # K27 -- daily personality colour. Trend/phase block (slow
            # daily under-current), not a situational block, so it
            # survives K16 ``split``/``replace``. Lives in T1 because
            # the kv_meta row only flips at local midnight.
            system_parts.append(day_color_block)

        # ── T2: SUMMARY (compaction-only) ────────────────────────────
        # Only mutates when SummaryWorker collapses old history into a
        # new summary row. Stable across consecutive turns until the
        # next compaction event, so it caches for the whole arc.
        if summary_text:
            system_parts.append(summary_text)
        if thread_note_text:
            system_parts.append(thread_note_text)

        # ── T3: RAG MEMORY ────────────────────────────────────────────
        # Per-turn retrieval but topic-stable: the same surfaced
        # memories often repeat on consecutive turns within one
        # thread, so this layer caches well in practice even though
        # it's nominally "rebuilt each turn".
        if memory_block:
            system_parts.append(memory_block)

        # ── T4: AMBIENT AWARENESS ────────────────────────────────────
        # Hourly to per-turn changes (clock, posture, foreground app).
        # K16: unified ambient grounding line. Lands at the head of the
        # ambient cluster so the LLM reads "where we are right now"
        # before the granular ambient cues. Empty in mode ``off``;
        # non-empty in ``replace`` and ``split`` (the granular blocks
        # are then suppressed by the matrix applied above when this
        # block rendered). The mode gate is defensive: even if a
        # provider misbehaves and returns text while the mode is
        # ``off``, the assembler refuses to append.
        if grounding_block and grounding_mode in ("replace", "split"):
            system_parts.append(grounding_block)
        if ambient:
            system_parts.append(ambient)
        if circadian_block:
            system_parts.append(circadian_block)
        if pajama_block:
            # Pajama-aware cue lands right next to the circadian block
            # so the LLM sees both pieces of "what time is it / what
            # are you wearing" in one neighbourhood.
            system_parts.append(pajama_block)
        if ambient_noise_block:
            system_parts.append(ambient_noise_block)
        if world_block:
            system_parts.append(world_block)
        # Activity block lands right after world so the two
        # "ambient awareness" cues (where Aiko is, what the user is
        # doing) sit next to each other in the system prompt.
        if activity_block:
            system_parts.append(activity_block)
        if sensory_anchor_block:
            # K24: sensory anchor sits right after the ambient
            # awareness cluster (world + activity). The body beat
            # is texture on top of the room location -- it tells
            # Aiko "you could touch the {item}" while the world
            # block grounds where she is. Intentionally NOT added
            # to the K16 grounding-line suppression matrix above:
            # the fused grounding paragraph never mentions specific
            # items + verb classes, so the cue is always additive
            # rather than redundant.
            system_parts.append(sensory_anchor_block)

        # ── T5: AFFECT / STYLE (per-turn) ────────────────────────────
        # AffectState updates after every reply, so this whole cluster
        # is uncached on every turn — but it sits AFTER the stable
        # prefix so the cache covers everything up to here.
        if affect_block:
            system_parts.append(affect_block)
        if mood_hint:
            system_parts.append(mood_hint)
        if mood_inertia_block:
            # K45: mood-inertia sits directly after the carryover hint —
            # both are reaction-shaping beats ("carry the mood" vs
            # "your face outran the feeling, let the words catch up").
            system_parts.append(mood_inertia_block)
        if mood_shell_block:
            # K5 mood-shell tilt: one-line emotional directive (e.g.
            # "Lean affectionate and steady; let warmth show.") sits
            # right after affect/mood because it derives from the
            # axes + affect colour the assistant just read. Empty on
            # most turns (silenced unless something crosses the gate)
            # and dropped in K16 ``replace`` mode (the unified
            # grounding line subsumes it).
            system_parts.append(mood_shell_block)
        if emotion_episode_block:
            # K57: the strongest directed-emotion episode (or its
            # one-shot thaw). Sits right after the mood-shell line —
            # it's the *objectful* sibling of that objectless tonal
            # tilt, and the strongest register directive in T5.
            system_parts.append(emotion_episode_block)
        if style_signal_block:
            # K13: "How Jacob writes lately: terse, casual, asks back
            # often." NOT gated on aggressive (register shaping is a
            # budget priority). Empty during warmup or when every
            # axis is default. Sits in T5 because the style tracker
            # updates after every user turn (axes drift per message).
            system_parts.append(style_signal_block)
        if user_state_block:
            system_parts.append(user_state_block)
        if vocal_tone_block:
            system_parts.append(vocal_tone_block)

        # ── T6: DETECTORS (per-turn, live ``user_text``-dependent) ───
        # The freshest cues the LLM reads before the user message.
        # Almost always change turn-to-turn. WITHIN this tier the
        # existing relative ordering preserves the behavioural
        # clusters (noticing cues / pacing cues / reaction cluster).
        if belief_gaps_block:
            # K2: surface up to two "your read on X doesn't match the
            # room" lines right alongside the knowledge-gap block.
            # Same "things on Aiko's mind" cluster -- belief gaps are
            # the affective sibling of knowledge gaps.
            system_parts.append(belief_gaps_block)
        if clarification_block:
            # K17: clarification-repair beats every other noticing cue
            # because it's the loudest signal in the room ("you missed
            # the point"). Goes right after belief_gaps so all the
            # "noticing cues" cluster together and lands above novelty
            # / stagnation / style_pattern -- if she missed the point
            # she should re-read first, react second.
            system_parts.append(clarification_block)
        if calibration_block:
            # K20: metacognitive calibration sits right after K17 in
            # the noticing-Jacob cluster. K17 = "you misread him";
            # K20 = "he doesn't trust your claim". Both steer the
            # next reply (clarification asks Aiko to re-read; K20
            # asks her to hedge), so they belong together.
            system_parts.append(calibration_block)
        if rupture_block:
            # K8: affect-rupture sits right after K17 so the "noticing
            # cues" cluster together at the top of the reaction-
            # shaping section. If both fire on the same turn (a
            # confused user whose mood also dropped), the
            # clarification cue tells Aiko what to fix while the
            # rupture cue tells her how to soften.
            system_parts.append(rupture_block)
        if self_correction_block:
            # K38: self-correction sits right after the rupture cue.
            # Both are one-shot post-turn detectors that steer this
            # turn's opening: rupture = "soften, his mood dipped",
            # self-correction = "own the slip you just made". Survives
            # aggressive mode -- an owed correction must land.
            system_parts.append(self_correction_block)
        if promise_followthrough_block:
            # K43: promise follow-through sits right after the
            # self-correction cue — both are "own what you owe" beats
            # (slip you made vs loop you left open). One-shot: the
            # provider already cleared its kv slot, so this block must
            # land whenever it's non-empty.
            system_parts.append(promise_followthrough_block)
        if misattunement_block:
            # K23: subtle-misattunement sits in the same noticing-Jacob
            # cluster as K17/K20/K8. K17 = "you misread him"; K20 =
            # "he doesn't trust your claim"; K8 = "his mood dipped";
            # K23 = "he went quiet on you / pivoted away". All four
            # steer the next reply (re-read / hedge / soften / pull
            # back) and benefit from being in the same paragraph of
            # the prompt. NOT in the K16 suppression set -- the
            # fused grounding line never carries misattunement
            # signal, so K23 is purely additive on top.
            system_parts.append(misattunement_block)
        if opinion_injection_block:
            # K29: stance-contradiction sits in the same "live read on
            # the user's turn" cluster as K8 / K17 / K23 -- they all
            # steer the next reply based on something that just
            # happened (mood dip / misread / pull-back / stored take
            # contradicts). Lands right after misattunement so the
            # "pull back" + "share your take" cues never appear in
            # opposite orders. NOT in the K16 suppression set: the
            # fused grounding line never carries stance signal so
            # K29 is purely additive on top.
            system_parts.append(opinion_injection_block)
        if reconnection_block:
            # J5: leads the gap cluster — the warm "good to see you, it's
            # been a while" re-anchoring beat that colours the first reply
            # back after a long absence. Sits before the K14 welcome-back
            # curiosity so the greeting reads before the "what were you up
            # to" follow-ons.
            system_parts.append(reconnection_block)
        if absence_curiosity_block:
            # K14 typed-mode: "Jacob was away for a few hours before
            # this message" sits right next to the other reaction-
            # shaping cues so the welcome-back framing lives in the
            # same cluster as the rupture / clarification beats. Same
            # one-shot policy: appears exactly once per qualifying
            # gap.
            system_parts.append(absence_curiosity_block)
        if turning_over_block:
            # K28: "Turning over: between sessions you've been thinking
            # about ..." lands *immediately after* the K14 welcome-back
            # line. Order matters: the welcome-back framing must
            # precede the "and I was thinking about X" content for the
            # combined cue to read naturally on a 90 min - 4h gap
            # (where both K14 and K28 fire). One-shot, same as K14.
            system_parts.append(turning_over_block)
        if away_activities_block:
            # K36: "While you were away you ..." sits right after the K28
            # turning_over block — both are gap-return cues, but the
            # provider's _gap_cue_surfaced guard ensures only one of the
            # two actually renders per return. One-shot.
            system_parts.append(away_activities_block)
        if forward_curiosity_block:
            # K34: "You've been wondering ..." sits right after the K36
            # away-activities block — the third gap-return cue. The
            # provider's _gap_cue_surfaced guard ensures only one of the
            # three renders per return. One-shot.
            system_parts.append(forward_curiosity_block)
        if follow_up_block:
            # FollowUpWorker cue: "their plan just happened, you can ask
            # how it went". Time-anchored, independent of the gap-cue
            # family, watermark-gated one-shot.
            system_parts.append(follow_up_block)
        if appreciation_block:
            # J10: rare specific gratitude anchored to a recent positive
            # shared moment. Cooldown + anti-repeat watermarked one-shot.
            system_parts.append(appreciation_block)
        if reciprocal_vulnerability_block:
            # J9: rare cue letting Aiko open up so the user can be the
            # supportive one. Suppressed when the user reads low-mood.
            system_parts.append(reciprocal_vulnerability_block)
        if novelty_block:
            # K6: surface the "Heads-up: Jacob just brought up
            # something new" line right after belief_gaps so reaction
            # cues cluster together and land before the knowledge_gap
            # "wondering about" bullet -- reacting beats wondering.
            system_parts.append(novelty_block)
        if stagnation_block:
            # K18: sibling of K6 -- "Heads-up: you've been circling
            # the same topic for a bit" sits immediately next to the
            # surprise cue so reaction-shaping context clusters
            # together. Empty on the common turn (suppressed by
            # warmup, cooldown, post-novelty window, or above-
            # threshold mean).
            system_parts.append(stagnation_block)
        if style_pattern_block:
            # Anti-rut layer: a "Heads-up: your last few replies have
            # all opened with..." / "...all ended on a question" /
            # "...have been running long" line. Sits next to the
            # K6/K18 cues since it's the same shape (a noticing cue
            # Aiko reads and silently corrects on this turn). Empty
            # on the common no-rut turn.
            system_parts.append(style_pattern_block)
        if question_balance_block:
            # K47: share-first cue. Sits right after the anti-rut
            # style_pattern cue (same "notice and self-correct" shape)
            # and is paired with the suppression of the question-pushing
            # providers, so the turn reads as "offer something of yours"
            # instead of another interview question. Empty on the common
            # turn (gate not armed).
            system_parts.append(question_balance_block)
        if tease_rhythm_block:
            # K48: banter-budget cue — "ease off" or "the roast landed,
            # one gentle step further is safe". Same noticing-cue family
            # as style_pattern / question_balance; one-shot, empty on the
            # common no-tease turn.
            system_parts.append(tease_rhythm_block)
        if self_noticing_block:
            # K30: self-noticing cluster — agreement-streak,
            # flat-affect, and/or repeated-thought Heads-ups (1-3
            # lines depending on which sub-detectors fired). Sits
            # right after the K30 anti-rut block so the "patterns
            # I'm in" cluster reads together and the persona block
            # can teach Aiko them as one register-shift family.
            # Empty on the common no-streak turn.
            system_parts.append(self_noticing_block)
        if vulnerability_budget_block:
            # K15: self-disclosure / vulnerability budget cue. Sits
            # right after the K30 self-noticing cluster so the
            # "register I'm in / how much have I shared" pair reads
            # as one self-aware family — both teach Aiko to pace
            # herself, just on different axes (rut vs. depth).
            # Silent on the common turn (budget under 50% spent).
            system_parts.append(vulnerability_budget_block)
        if touch_state_block:
            # K31: physical-budget cue, sibling of K15. Lands right
            # after K15 so the two pacing cues cluster together --
            # disclosure depth + physical contact frequency read as
            # one "how much have I leaned in" family. Silent on the
            # common turn (no intimate-gesture stack today).
            system_parts.append(touch_state_block)
        if user_reactions_block:
            # K32: "Jacob just hearted that line" one-shot. Sits
            # after the touch_state cue so the reciprocity beat
            # (Aiko reached out, Jacob reacted) reads in order in
            # the prompt context. Drained by the provider on
            # render so it never re-fires on the next turn.
            system_parts.append(user_reactions_block)
        if attachments_block:
            # D2 Part B: in-chat attachment turn hint. Lands right
            # before the running/parked task blocks so the "the user
            # just handed me these files -> hand them to start_workflow"
            # beat reads adjacent to the task machinery it triggers.
            # Per-turn (reflects only what was attached to THIS message).
            system_parts.append(attachments_block)
        if running_tasks_block:
            # Brain-orchestration chunk 6: running-tasks state
            # block. Lands BEFORE task_cues_block so the prompt
            # reads "you're currently doing A and B; B just
            # finished" rather than the reverse. Read directly
            # from :class:`TaskOrchestrator` (no draining) so the
            # same task can stay surfaced across many turns until
            # it actually terminates. Capped at 5 bullets by the
            # provider so a task-bomb can't balloon the block.
            system_parts.append(running_tasks_block)
        if task_cues_block:
            # Brain-orchestration chunk 5: parked task cues land in
            # the T6 cluster right after the K32 reaction one-shot.
            # Both are "live read on what just happened" beats —
            # K32 says "Jacob reacted", this says "your background
            # task finished / is blocked". The cue store enforces
            # its own aggregation cap (default 5) so the block
            # never balloons. Drained on every assembly so an old
            # cue surfacing once doesn't re-fire next turn — the
            # escalation manager owns the "if she stayed silent,
            # nudge" path instead.
            system_parts.append(task_cues_block)
        if initiative_block:
            # K53: the one-turn floor-taking directive. Leads the
            # "things on Aiko's mind" cluster — it's the strongest
            # imperative in the prompt and the blocks below it
            # (wants / curiosity / gaps) supply its material.
            system_parts.append(initiative_block)
        if thread_ownership_block:
            # K55: the one-shot circle-back cue. Sits directly under
            # the initiative directive — same family (Aiko holding
            # her own thread), and the two never fire on the same
            # turn (the thread was opened BY the last directive, so
            # the cadence counter just reset).
            system_parts.append(thread_ownership_block)
        if wants_block:
            # K52: wants ledger — leads the "things on Aiko's mind"
            # cluster because it carries the strongest directive
            # (the imperative band outranks the soft curiosity /
            # knowledge-gap permission slips below it).
            system_parts.append(wants_block)
        if topic_appetite_block:
            # K54: the once-per-conversation "tapped out" negotiation
            # slip. Lands right under the wants block — its offer IS
            # the strongest want, so the two read as one thought.
            system_parts.append(topic_appetite_block)
        if tease_ledger_block:
            # K59: the rare "collect on an old one" permission slip.
            # Clusters with the wants/appetite "things on Aiko's
            # mind" family, just before the softer curiosity cues.
            system_parts.append(tease_ledger_block)
        if curiosity_seeds_block:
            # K9: "Quiet curiosity" — at-most-two topics Aiko has
            # been wondering about that haven't come up yet. Sits
            # right after the stagnation cue and before the
            # knowledge-gap "wondering about" line so the three
            # "things on Aiko's mind" surfaces cluster together.
            # Empty until the seed worker has written something.
            system_parts.append(curiosity_seeds_block)
        if knowledge_gaps_block:
            # F2: surface one "wondering about" bullet right after
            # agenda. Keeps the "things on Aiko's mind" cluster
            # together in the system prompt.
            system_parts.append(knowledge_gaps_block)
        if knowledge_grounding_block:
            # K61: informational-turn steer toward learned specifics.
            # Sits right after the knowledge-gap line so the "what Aiko
            # knows / wonders about" surfaces cluster together.
            system_parts.append(knowledge_grounding_block)
        if knowledge_gap_notice_block:
            # F10f: "I keep circling X but never dug in" — clusters with
            # the knowledge-gap / grounding surfaces (all "what Aiko does
            # / doesn't know about a recurring topic").
            system_parts.append(knowledge_gap_notice_block)
        if topic_temperature_block:
            # F10h: per-cluster affect — tonal nudge for a warm / tender
            # topic, sits with the other topic-graph-derived cues.
            system_parts.append(topic_temperature_block)
        if topic_confidence_block:
            # F10i: per-topic confidence — hedge / earned-familiarity
            # register cue, last of the topic-graph-derived surfaces.
            system_parts.append(topic_confidence_block)
        if associative_wander_block:
            # K64a: associative wandering — "funny, this reminds me of ..."
            # connection cue, sits with the other topic-graph-derived
            # surfaces (it is drawn from the same cluster geometry).
            system_parts.append(associative_wander_block)
        if interest_drift_block:
            # K64b: interest drift — slow "drawn to X lately" / "X gone
            # quiet" register shift, last of the topic-graph-derived cues.
            system_parts.append(interest_drift_block)

        system_prompt = "\n\n---\n\n".join(p for p in system_parts if p)

        # Pre-build per-block telemetry. Per-block estimates use the same
        # heuristic as ``estimate_tokens`` so the sum is internally consistent
        # with ``prompt_tokens_estimate``.
        persona_tokens = estimate_tokens(persona) if persona else 0
        ambient_tokens = estimate_tokens(ambient) if ambient else 0
        mood_tokens = estimate_tokens(mood_hint) if mood_hint else 0
        rag_tokens = estimate_tokens(memory_block) if memory_block else 0
        summary_tokens = estimate_tokens(summary_text) if summary_text else 0
        affect_tokens = estimate_tokens(affect_block) if affect_block else 0
        circadian_tokens = estimate_tokens(circadian_block) if circadian_block else 0
        profile_tokens = estimate_tokens(profile_block) if profile_block else 0
        user_state_tokens = estimate_tokens(user_state_block) if user_state_block else 0
        relationship_tokens = estimate_tokens(relationship_block) if relationship_block else 0
        arc_tokens = estimate_tokens(arc_block) if arc_block else 0
        narrative_tokens = estimate_tokens(narrative_block) if narrative_block else 0
        agenda_tokens = estimate_tokens(agenda_block) if agenda_block else 0
        world_tokens = estimate_tokens(world_block) if world_block else 0
        self_image_tokens = estimate_tokens(self_image_block) if self_image_block else 0
        system_tokens = estimate_tokens(system_prompt) + (_MESSAGE_OVERHEAD if system_prompt else 0)

        cleaned_user = (user_text or "").strip()
        user_tokens = (
            estimate_tokens(cleaned_user) + _MESSAGE_OVERHEAD if cleaned_user else 0
        )

        # Budget for history = context_window - response_budget - safety -
        # everything we already commit to (system block + the user message).
        budget_tokens = max(
            512,
            int(context_window) - int(response_budget) - _SAFETY_TOKENS,
        )
        history_budget = max(
            128, budget_tokens - system_tokens - user_tokens,
        )
        history_dicts, history_tokens, kept_count, dropped_count = self._fit_history(
            history_msgs,
            history_budget,
            prefix_enabled=self._history_age_prefix_enabled,
        )

        # In aggressive mode every block has been shrunk; if we still don't
        # fit, drop more from the head of history until we do.
        if aggressive:
            while history_dicts and (
                system_tokens + user_tokens + history_tokens > budget_tokens
            ):
                dropped = history_dicts.pop(0)
                cost = estimate_tokens(dropped.get("content", "")) + _MESSAGE_OVERHEAD
                history_tokens = max(0, history_tokens - cost)
                kept_count = max(0, kept_count - 1)
                dropped_count += 1

        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history_dicts)
        if cleaned_user:
            messages.append({"role": "user", "content": cleaned_user})

        prompt_tokens_estimate = system_tokens + history_tokens + user_tokens
        compaction_triggered = (
            prompt_tokens_estimate > budget_tokens
            or (history_msgs and not history_dicts and not aggressive)
        )

        telemetry = PromptTelemetry(
            context_window=int(context_window),
            budget_tokens=budget_tokens,
            persona_tokens=persona_tokens,
            ambient_tokens=ambient_tokens,
            mood_tokens=mood_tokens,
            rag_tokens=rag_tokens,
            summary_tokens=summary_tokens,
            system_tokens=system_tokens,
            history_tokens=history_tokens,
            user_tokens=user_tokens,
            tool_tokens=0,
            affect_tokens=affect_tokens,
            circadian_tokens=circadian_tokens,
            profile_tokens=profile_tokens,
            user_state_tokens=user_state_tokens,
            relationship_tokens=relationship_tokens,
            arc_tokens=arc_tokens,
            narrative_tokens=narrative_tokens,
            agenda_tokens=agenda_tokens,
            world_tokens=world_tokens,
            self_image_tokens=self_image_tokens,
            prompt_tokens_estimate=prompt_tokens_estimate,
            history_messages_kept=kept_count,
            history_messages_dropped=dropped_count,
            summary_active=bool(summary_text),
            summary_messages=int(already_summarized),
            compaction_triggered=bool(compaction_triggered),
            rag_prefetch_event=rag_prefetch_event,
            slice_cache_event=slice_event,
            # P2: per-phase wall time. ``provider_ms`` is rounded for
            # log readability but the dict only contains entries for
            # providers that actually ran this build.
            provider_ms={k: round(v, 2) for k, v in provider_ms.items()},
            rag_lookup_ms=round(rag_lookup_ms, 2),
            assemble_ms=round(
                (time.perf_counter() - assemble_started_at) * 1000.0, 2,
            ),
            # P1 fields are stamped post-assemble by ``TurnRunner`` from
            # the embedder's per-turn counters; we leave them at the
            # default 0 here so a stand-alone assemble call (e.g. tests
            # without a turn boundary) still produces a valid telemetry.
        )

        # Per plan: tweaking-only headline for the prompt build. Stays
        # at DEBUG so default-INFO logs aren't flooded; bump
        # `app.core.session.prompt_assembler` to DEBUG when tracing retrieval/budget.
        # Field names align with AGENTS.md "Standard line shape".
        # P2: ``inner_blocks`` was previously a hard-coded count of 10
        # static slots that never picked up novelty / belief_gaps /
        # routines / etc.; now it's the live provider count derived
        # from the timing dict, so adding a new provider doesn't
        # silently leave it out of the headline. ``provider_ms_total``
        # rolls up the wall time of every provider that actually ran;
        # ``slowest_provider`` calls out the worst offender so a
        # regression in a single provider lights up immediately.
        provider_count = len(provider_ms)
        provider_ms_total = sum(provider_ms.values())
        if provider_ms:
            slowest_name, slowest_ms = max(
                provider_ms.items(), key=lambda kv: kv[1],
            )
            slowest_field = f"{slowest_name}:{slowest_ms:.1f}"
        else:
            slowest_field = "-"
        log.debug(
            "prompt built: ctx=%d budget=%d est_tokens=%d "
            "sys=%d hist=%d user=%d rag_tokens=%d "
            "history_msgs_in=%d history_msgs_out=%d "
            "providers=%d provider_ms_total=%.1f slowest_provider=%s "
            "rag_lookup_ms=%.1f assemble_ms=%.1f "
            "summary_active=%s compaction=%s aggressive=%s",
            context_window,
            budget_tokens,
            prompt_tokens_estimate,
            system_tokens,
            history_tokens,
            user_tokens,
            telemetry.rag_tokens,
            kept_count,
            dropped_count,
            provider_count,
            provider_ms_total,
            slowest_field,
            telemetry.rag_lookup_ms,
            telemetry.assemble_ms,
            "1" if telemetry.summary_active else "0",
            "1" if telemetry.compaction_triggered else "0",
            "1" if aggressive else "0",
        )
        return messages, telemetry

    # ── helpers ───────────────────────────────────────────────────────────


