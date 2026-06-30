from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.infra.settings_basic import FileWriteSettings, VisionSettings


@dataclass(slots=True)
class AgentSettings:
    """Lean v1 conversation agent knobs.

    Proactive nudges are driven by
    :class:`app.core.proactive.proactive_director.ProactiveDirector`.

    The ``summary_*`` knobs and ``max_prompt_tokens_pct`` together control
    context compaction (rolling summary + on-overflow squish) handled by
    :class:`app.core.proactive.summary_worker.SummaryWorker` and
    :class:`app.core.session.turn_runner.TurnRunner`.
    """

    proactive_silence_seconds: float = 45.0
    proactive_cooldown_seconds: float = 120.0
    # ── Typed-mode proactive (Aiko speaks first in typed chat) ────────
    # Independent timing knobs from the voice-mode ones above so the
    # two cadences can differ. Defaults intentionally long (4 min
    # silence, 10 min cooldown) so a heads-down typed session never
    # gets nag-y. Gated client-side by browser visibility / Tauri
    # window focus — see ``SessionController._user_present``.
    proactive_typed_enabled: bool = True
    proactive_silence_seconds_typed: float = 240.0  # 4 min
    proactive_cooldown_seconds_typed: float = 600.0  # 10 min
    # When ``False`` (default) the typed-mode proactive director respects
    # ``_user_present``: every connected window hidden / blurred -> no
    # autonomous chime. Flip to ``True`` to opt in to "Aiko can chat
    # in even when I'm not at the window" — the silence timer fires
    # regardless of whether any client window is visible. Voice-mode
    # proactive ignores presence on purpose (mic users are present in
    # conversation even when away from the screen) so this flag does
    # not affect it.
    proactive_typed_when_away: bool = False
    # When ``True`` a typed-mode proactive line is ALSO spoken via TTS
    # (same enqueue path as voice-mode proactive). Default ``False``:
    # typed proactive is text-only because the nudge can land minutes
    # later when the user may be away from the speakers, and an unprompted
    # spoken line then is more startling than helpful. Voice-mode
    # proactive always speaks regardless of this flag.
    proactive_typed_tts_enabled: bool = False
    # ── World-notice proactive (Aiko reaches out about her room) ──────
    # Master switch for the WorldNoticeWorker, which primes a proactive
    # nudge when the user has left something in Aiko's room or after a
    # long quiet stretch. The actual cadence / cooldown / daily-cap live
    # in ``MemorySettings.world_notice_*`` alongside the other idle
    # workers; this just turns the whole behaviour on or off.
    world_notice_enabled: bool = True
    # ── Activity awareness (desktop opt-in) ───────────────────────────
    # When enabled and running inside the Tauri desktop shell, the
    # foreground application name is forwarded over WebSocket so Aiko
    # can naturally reference what the user is doing. App name only —
    # never window titles or URLs (see ``docs/presence-and-activity``
    # for the privacy posture). Off by default; browser shells render
    # the toggle but can never produce a non-null active app.
    activity_awareness_enabled: bool = False
    # ── Weather + season sync (H11, opt-in) ───────────────────────────
    # Master switch for the passive ambient weather feed: when on (and a
    # home location is resolved under ``weather.*``), a low-frequency
    # worker pulls current conditions, surfaces a terse "shared sky"
    # prompt cue, tints the persona-window backdrop, and can nudge the
    # K27 daily colour + seasonal room decor. Coarse city-granularity
    # location only, never GPS (see ``docs/weather-sync.md``). Off by
    # default. The on-demand weather *tools* are gated separately by
    # ``tools.weather``.
    weather_sync_enabled: bool = False
    # ── Shared moments + relationship depth (schema v7) ───────────────
    # ``shared_moments_enabled`` is the master switch for the entire
    # subsystem (inline tag extraction, LLM detector, Together tab,
    # anniversary block). With it off, ``[[moment:]]`` tags still get
    # stripped from chat (the strip pattern lives upstream) but they're
    # not persisted.
    # ``shared_moments_llm_enabled`` toggles only the LLM Track 2
    # detector — turning it off keeps Aiko-curated tags and manual UI
    # button working.
    # The LLM detector is gated by ``shared_moments_min_turn_gap``
    # (cadence) AND ``shared_moments_cooldown_seconds`` (wall-clock) so
    # back-to-back warm exchanges produce at most one moment per window.
    shared_moments_enabled: bool = True
    shared_moments_llm_enabled: bool = True
    shared_moments_min_turn_gap: int = 5
    shared_moments_cooldown_seconds: float = 300.0
    # Anniversary surfacing renders a single "On your mind today — a
    # year ago today, …" line in the system prompt when a shared moment
    # matches one of the 1mo/3mo/6mo/1yr/Nyr windows. Independent of
    # ``shared_moments_enabled`` so you can keep moments off but
    # surface anniversaries from a historical archive (or vice versa).
    anniversary_surfacing_enabled: bool = True
    # Relationship axes: 4 floats (closeness, humor, trust, comfort)
    # that drift per turn from reactions, moments, milestones. Cheap
    # (one SQL upsert). The prompt block is terse and only renders
    # when an axis exceeds the notable threshold (default 0.5).
    relationship_axes_enabled: bool = True
    # J8: milestone-celebration cue. When a relationship milestone crosses
    # (100 turns / 1 week / 1 month / 100 days / 6 months / 1 year), a
    # one-shot warm acknowledgement is surfaced into the next turn's
    # prompt (stage-aware register via J4). Off = the milestone is still
    # recorded as a memory but never actively acknowledged.
    milestone_celebration_enabled: bool = True
    # J5: reconnection ritual. On the first reply after a long absence
    # (>= reconnection_base_gap_hours, closeness-scaled so a closer
    # relationship notices a gap sooner), surface a one-shot warm
    # re-anchoring cue that colours the opener. Distinct from the K57
    # lonely episode (felt) and K28/K36 (next-turn "what I was up to").
    reconnection_enabled: bool = True
    reconnection_base_gap_hours: float = 24.0
    # K-time4: session-elapsed & mid-session gap awareness. A cheap derived
    # signal off the recent-message timestamps, distinct from the
    # cross-session gap family (J5 reconnection / K14 absence_curiosity).
    # Two independent sub-cues fold into one block:
    #   * elapsed  — how long the *current continuous sitting* has run (a
    #     run of messages with no gap > session_clock_break_minutes),
    #     banded at long / very-long; one-shot per band per sitting.
    #   * pause    — a notable *mid-session* pause (the delta before the
    #     latest message) in [gap_min, gap_max) minutes; the upper bound
    #     sits at the absence_curiosity floor (30 min) so it never
    #     double-fires with the gap-return family.
    # Tonal guard (in the rendered cue): observe, don't police.
    session_clock_enabled: bool = True
    session_clock_long_minutes: float = 60.0
    session_clock_very_long_minutes: float = 150.0
    session_clock_break_minutes: float = 30.0
    session_clock_gap_min_minutes: float = 10.0
    session_clock_gap_max_minutes: float = 30.0
    # J10: appreciation beats. Rare, specific unprompted gratitude anchored
    # to a recent positive shared moment. Gated by closeness + a long
    # wall-clock cooldown so it stays a treat, never a tic.
    appreciation_beats_enabled: bool = True
    appreciation_min_closeness: float = 0.25
    appreciation_cooldown_hours: float = 72.0
    appreciation_max_anchor_age_days: float = 21.0
    # J9: reciprocal vulnerability. Rare cue authorising Aiko to open up
    # about something she's sitting with, so the user gets to be the
    # supportive one. Stage (familiar+) + trust gated, paced by the K15
    # budget, and hard-suppressed when the user is in a low-mood window.
    reciprocal_vulnerability_enabled: bool = True
    reciprocal_vulnerability_cooldown_hours: float = 96.0
    reciprocal_vulnerability_min_trust: float = 0.2
    # J6: conflict-repair memory. When a K8 rupture resolves (the user's
    # valence recovers within a turn window), record a durable
    # ``repair``-vibe shared moment so Aiko can reference "we sorted this
    # out" instead of re-litigating. Cooldown stops one rough patch from
    # spawning several rows.
    conflict_repair_enabled: bool = True
    conflict_repair_watch_turns: int = 5
    conflict_repair_recovery_epsilon: float = 0.05
    conflict_repair_min_recovery_rise: float = 0.10
    conflict_repair_cooldown_hours: float = 12.0
    # ── F1 personality backlog: background fact-checker ───────────────
    # Master switch. When off, the queue still persists but the
    # IdleFactChecker worker never runs (so any pending claims simply
    # sit there harmlessly until the flag is flipped back on or the
    # underlying memory is deleted).
    fact_checker_enabled: bool = True
    # Hourly + daily rate caps. Token-bucket persisted to ``kv_meta``.
    # The defaults give the worker a generous budget while still
    # keeping a chatty session from burning unbounded web queries.
    fact_checker_per_hour_cap: int = 10
    fact_checker_per_day_cap: int = 50
    # ── G2 personality backlog: schedule learner ──────────────────────
    # Master switch for :class:`app.core.infra.schedule_learner.ScheduleLearner`,
    # the IdleWorker that buckets ``messages.created_at`` into a
    # ``usual_hours`` user-profile field. Cheap to run; safe to leave on.
    schedule_learner_enabled: bool = True
    # Minimum number of user messages in the rolling window before the
    # worker writes anything. Below this threshold the field stays
    # untouched so a fresh DB doesn't claim a confident schedule.
    schedule_learner_min_samples: int = 5
    # Rolling window the bucketing scan considers. 30 days keeps the
    # picture current without being noisy after a single anomalous day.
    schedule_learner_window_days: int = 30
    # ── K3 personality backlog: routine / ritual awareness ────────────
    # Master switch for the second pass inside ``ScheduleLearner`` that
    # detects named recurring slots ("Sunday-morning chats") and writes
    # them into the ``routines`` user-profile field. Disabling this
    # leaves the G2 ``usual_hours`` write intact; only the K3 pass is
    # skipped. Cheap (no LLM, no embedder), safe to leave on.
    routine_detection_enabled: bool = True
    # ── G3 personality backlog: idle curiosity worker ─────────────────
    # Master switch for
    # :class:`app.core.proactive.idle_curiosity_worker.IdleCuriosityWorker`. When
    # disabled, ``open_question`` memories simply never get web-searched.
    idle_curiosity_enabled: bool = True
    # Hourly + daily caps on web searches the curiosity worker is
    # allowed to issue. Strictly tighter than the fact-checker so a
    # multi-week absence (with a backlog of open questions) cannot dump
    # a wall of "I was reading about" beats on the user when they
    # return. Token-bucket persisted to ``kv_meta`` under a separate key.
    idle_curiosity_per_hour_cap: int = 2
    idle_curiosity_per_day_cap: int = 6
    # ── F9 personality backlog: interest-driven knowledge worker ──────
    # Master switch for
    # :class:`app.core.proactive.idle_knowledge_worker.IdleKnowledgeWorker`.
    # When disabled the worker never registers its idle tick and the
    # ``knowledge`` memory pool only grows from manual/other writers.
    knowledge_enrichment_enabled: bool = True
    # Hourly + daily caps on web searches the knowledge worker may
    # issue. Strictly tighter than the curiosity worker — F9 researches
    # the user's *standing* interests (which change slowly), so a slow
    # drip is the right cadence and a long absence must never dump a
    # wall of "I was reading about…" facts. Own token-bucket persisted
    # to ``kv_meta`` under ``"idle_knowledge.rate_state"`` so it never
    # shares counters with F1 / G3.
    knowledge_enrichment_per_hour_cap: int = 1
    knowledge_enrichment_per_day_cap: int = 4
    # When on, the knowledge worker runs a small worker-LLM "research
    # planner" before searching: it reads the chosen cluster's member
    # memories, decides whether there's an evergreen, impersonal subject
    # worth researching at all (skipping relationship/feeling/plan-only
    # clusters), and emits up to N neutral search queries. The extra
    # queries are queued so a later pass deepens the same interest from a
    # different angle. When off, the worker falls back to the legacy path
    # (privacy-scrub the cluster summary and search that verbatim).
    knowledge_topic_extraction_enabled: bool = True
    # F10f: master switch for the knowledge-gap *notice* — the self-aware
    # "I keep circling X but never dug into it" beat. Independent of F9
    # ``knowledge_enrichment_enabled`` (which silently *researches* the same
    # clusters): this one only controls whether Aiko ever voices the gap.
    # Off → the KnowledgeGapNoticeWorker never registers and the inner-life
    # provider stays empty. Cadence / thresholds live under MemorySettings.
    knowledge_gap_notice_enabled: bool = True
    # K64a: master switch for associative wandering. When on, the
    # AssociativeWanderWorker drifts across the topic graph during quiet
    # windows, connects two *distant* clusters via the worker LLM, and the
    # inner-life provider surfaces the connection only when the live turn is
    # on one of the two topics ("funny, this reminds me of ..."). Off → the
    # worker never registers and the provider stays empty. Cadence /
    # thresholds live under MemorySettings.
    associative_wander_enabled: bool = True
    # K64b: master switch for interest drift. When on, the
    # InterestDriftWorker tracks each topic cluster's mass over time and the
    # inner-life provider surfaces a slow "I've been drawn to X lately" /
    # "X has gone quiet" register shift when the live turn is on a drifting
    # topic. Off → the worker never registers and the provider stays empty.
    # Cadence / thresholds live under MemorySettings.
    interest_drift_enabled: bool = True
    # K64c: master switch for the curiosity gradient. When on, the
    # CuriosityGradientWorker finds a thin topic cluster on the rim of a
    # dense one (the under-explored edge of familiar territory) and the
    # inner-life provider surfaces a genuinely-curious-question cue when the
    # live turn is on either topic. Off → the worker never registers and the
    # provider stays empty. Cadence / thresholds live under MemorySettings.
    curiosity_gradient_enabled: bool = True
    # K67: master switch for the dormant-interest re-opener. When on, the
    # DormantInterestWorker notices a topic cluster that was once a genuine,
    # high-mass user interest and has since gone quiet for weeks, and the
    # inner-life provider surfaces a rare, warm "we haven't talked about X in
    # ages — still into that?" re-opener on a natural conversational lull.
    # Off → the worker never registers and the provider stays empty. Cadence
    # / thresholds live under MemorySettings.
    dormant_interest_enabled: bool = True
    # K64d: master switch for knowledge-map self-reflection. When on, the
    # KnowledgeMapReflectionWorker periodically reads the *shape* of the topic
    # graph (richest territories + under-explored ones), runs a worker-LLM
    # meta-thought, and writes one [mindmap] kind="reflection" memory that
    # surfaces through the existing RAG / K28 turning-over path. Off → the
    # worker never registers. Cadence / thresholds live under MemorySettings.
    knowledge_map_reflection_enabled: bool = True
    # F10h: master switch for the per-cluster affect ("topic temperature")
    # cue. When on, a turn that lands on a warm / tender topic cluster gets
    # a one-line tonal nudge so Aiko meets it with the right register. Off
    # → the provider stays empty. Computed live from shared-moment vibes
    # (no worker); thresholds / cooldown live under MemorySettings.
    topic_temperature_enabled: bool = True
    # H8: master switch for "topic mood origin" — when a cluster first reads
    # warm / tender (F10h), stamp the shared moment that gave it that feel so
    # Aiko can name the origin ("ever since you told me about X") instead of
    # just the feeling. Rides on top of topic_temperature; off → no origin
    # clause is ever appended (the bare warm / tender cue still fires).
    topic_mood_origin_enabled: bool = True
    # F10i: master switch for the per-topic confidence self-model. When on,
    # a turn that lands on a *thin* topic cluster nudges Aiko to admit she
    # doesn't know much yet (rather than bluff), and a *rich* one nudges her
    # to stop over-hedging. Off → the provider stays empty. Computed live
    # from cluster size + learned-fact coverage (no worker); thresholds /
    # cooldown live under MemorySettings.
    topic_confidence_enabled: bool = True
    # K66: master switch for the earned-familiarity register cue. When on,
    # a turn that lands on a *high-mass* topic cluster (one the pair has
    # returned to many times) nudges Aiko to let that shared history show as
    # register — lean on shared shorthand, skip the 101-level recap, assume
    # the context you both already have — never as a stated fact. Orthogonal
    # to F10i topic_confidence (which reads *knowledge richness*, not shared
    # history depth). Off → the provider stays empty. Computed live from
    # cluster mass (no worker); thresholds / cooldown live under
    # MemorySettings.
    earned_familiarity_enabled: bool = True
    # K-time3: master switch for the upcoming-horizon block. When on, a
    # cheap forward sweep over ``future_plan`` memories due within the
    # horizon window renders one terse "coming up" cue with the relative
    # times **already resolved** (so Aiko never recomputes a future date).
    # Off → the provider stays empty. Window / cap / cooldown live under
    # MemorySettings.
    upcoming_horizon_enabled: bool = True
    # ── K61 personality backlog: knowledge-grounding steer ────────────
    # Master switch for the ``knowledge_grounding`` inner-life block
    # (:meth:`InnerLifeProvidersMixin._render_knowledge_grounding_block`).
    # When on, informational turns that have matching learned facts get
    # a one-line cue nudging Aiko to commit to specifics instead of
    # survey-hedging. Disabling leaves the F8 retrieval surfacing intact
    # (the facts still appear in the RAG block); only the steer is gone.
    knowledge_grounding_enabled: bool = True
    # ── F5 personality backlog: conflicting-memory detector ──────────
    # Master switch for
    # :class:`app.core.memory.memory_conflict_worker.MemoryConflictWorker`.
    # When disabled the worker never registers its idle tick and the
    # Conflicts sub-tab in the Memory drawer is hidden.
    conflict_detector_enabled: bool = True
    # Hourly + daily caps on LLM verification calls the worker is
    # allowed to issue. The hybrid heuristic gate keeps most pairs
    # below this cap; only borderline (e.g. numerical-mismatch) pairs
    # consume budget. The token-bucket is persisted to ``kv_meta`` via
    # a dedicated :class:`FactCheckRateLimiter` with
    # ``state_key='conflict_detector.rate_state'`` so an idle pass
    # from the F1 fact-checker can't starve the F5 budget (and vice
    # versa).
    conflict_detector_per_hour_cap: int = 6
    conflict_detector_per_day_cap: int = 30
    # ── K35 personality backlog: memory consolidation worker ─────────
    # Master switch for
    # :class:`app.core.memory.memory_consolidation_worker.MemoryConsolidationWorker`.
    # When disabled the worker never registers its idle tick. The
    # per-hour / per-day caps bound the worker-LLM merge calls (one per
    # cluster), persisted to ``kv_meta`` via a dedicated
    # :class:`FactCheckRateLimiter` with
    # ``state_key='memory_consolidation.rate_state'`` so the merge
    # budget is independent of F1 / F5 / G3. Clusters that can't get a
    # token fall back to the deterministic "keep the strongest member
    # verbatim" path, so a starved budget never blocks consolidation.
    memory_consolidation_enabled: bool = True
    memory_consolidation_per_hour_cap: int = 6
    memory_consolidation_per_day_cap: int = 30
    # F10j: scope the F5 conflict detector + K35 consolidation sweeps to
    # within topic-graph clusters. Turns each worker's O(n^2) all-pairs
    # cosine into a per-cluster O(k^2) sweep (cheaper as the store grows,
    # and the surviving pairs are topically adjacent — exactly where
    # contradictions / near-dupes live). Off → both workers fall back to
    # the full all-pairs sweep. No effect until the topic graph is warm /
    # persistent (degrades to the full sweep).
    cluster_scoped_memory_hygiene_enabled: bool = True
    # ── K2 personality backlog: theory-of-mind / belief tracking ─────
    # Master switch for the whole K2 surface (worker + gap detector +
    # tag parser + REST + UI). When disabled the worker never runs,
    # the gap detector is short-circuited, and the Beliefs sub-tab in
    # the Memory drawer is hidden. Self-tag emissions
    # (``[[predict:...]]``) are still stripped from chat so they
    # never leak to the user, but the parsed payload is dropped.
    belief_tracking_enabled: bool = True
    # Master switch for the background inference worker only. With
    # ``belief_tracking_enabled=True`` but
    # ``belief_worker_enabled=False`` Aiko's self-tag fast path still
    # writes beliefs and the gap detector still surfaces mismatches;
    # only the autonomous inference pass is suppressed.
    belief_worker_enabled: bool = True
    # K65b master switch: fold the K9 interest map into the belief
    # worker's extraction prompt (prioritise the densest clusters +
    # re-check active beliefs sitting on them). When off the worker
    # mines the flat last-N user turns exactly as it did pre-K65b. The
    # ``memory.belief_worker_interest_top_n`` / ``_reconsider_max`` knobs
    # tune the behaviour; this switch turns it off wholesale (and on a
    # cold store with no labelled clusters the worker is byte-identical
    # to the legacy path regardless).
    belief_interest_bias_enabled: bool = True
    # Hourly + daily caps on LLM extraction calls the worker is
    # allowed to issue. Lower-cap by default than the F1 fact-checker
    # because belief inference is a "nice-to-have" mining job, not a
    # correctness gate. Dedicated
    # :class:`FactCheckRateLimiter` with
    # ``state_key='belief_worker.rate_state'``.
    belief_worker_per_hour_cap: int = 8
    belief_worker_per_day_cap: int = 40
    # ── Phase 3c (reworked): context-aware promise extraction worker ──
    # Master switch for
    # :class:`app.core.memory.promise_worker.PromiseExtractionWorker`,
    # the sole writer of ``kind="promise"`` memories. When disabled the
    # worker is never registered and no promises are auto-extracted
    # (the ``[[remember:...]]`` self-tag path is unaffected).
    promise_worker_enabled: bool = True
    # Hourly + daily caps on LLM extraction calls. Generous by default
    # because the worker runs frequently but each call is bounded by
    # these caps -- the real spend ceiling. Dedicated
    # :class:`FactCheckRateLimiter` with
    # ``state_key='promise_worker.rate_state'``.
    promise_worker_per_hour_cap: int = 10
    promise_worker_per_day_cap: int = 60
    # ── K6 personality backlog: surprise / novelty detector ──────────
    # Master switch for :class:`app.core.conversation.novelty_detector.NoveltyDetector`.
    # When disabled the detector is never instantiated and the
    # ``novelty`` inner-life provider is left unregistered, so the
    # prompt-assembler short-circuits the block with zero cost on the
    # hot path. The detector itself is purely in-process (one
    # Embedder.embed call per turn + a tiny ring buffer); there's no
    # rate-cap because the per-turn cost is the same as RAG retrieval.
    novelty_detection_enabled: bool = True
    # ── K18 personality backlog: topic stagnation detector ────────────
    # Master switch for
    # :class:`app.core.conversation.topic_stagnation.TopicStagnationDetector`.
    # The detector is a pure streak counter over the per-turn distance
    # K6 already computes (no extra embedding) so it's effectively
    # free; this knob exists to silence the cue when a tester wants
    # to focus on K6 alone. Leaving it on with conservative
    # thresholds is the intended default.
    topic_stagnation_enabled: bool = True
    # F10k: semantic topic tracking for K6/K18. When on, the novelty
    # detector maps each measured turn to its best topic-graph cluster
    # and the K6/K18 cues gain a private "(shift from X to Y)" /
    # "(circling back to X)" / "(the X thread)" context clause, and a
    # return to a previously-visited cluster reads differently from a
    # brand-new topic. Off → the detectors run exactly as before (no
    # cluster lookups). Toggling requires a restart (the provider is
    # bound at detector construction).
    topic_tracking_enabled: bool = True
    # ── K9 personality backlog: topic graph + curiosity seeds ─────────
    # Master switch for the in-process topic graph wrapper around
    # :attr:`MemoryStore._mirror`. Disabling skips both the seed
    # worker's "have we discussed this already?" filter AND the
    # eventual Memory-tab cluster panel; the rest of the app keeps
    # functioning unchanged. Cheap on its own (rebuilds from the
    # existing in-memory mirror; no embedding work).
    topic_graph_enabled: bool = True
    # Schema v20: persist the topic graph (clusters + centroids +
    # assignments) and maintain it incrementally instead of recomputing
    # the whole O(n^2) clustering on every read. When ``True`` (default)
    # the graph warm-starts from SQLite on boot, assigns each new memory
    # to the nearest cluster on the fly, and only batch-refits during
    # quiet windows (see the two knobs below). When ``False`` it falls
    # back to the legacy in-memory, rebuild-on-read behaviour.
    topic_graph_persistent_enabled: bool = True
    # How often the TopicGraphRebuildWorker runs a full batch refit
    # (default daily). Pressure can trigger it sooner -- see the pending
    # threshold below. Clamped to a 60s floor.
    topic_graph_rebuild_interval_seconds: float = 86_400.0
    # Pending-pressure trigger: once this many incrementally-added
    # memories have failed to join any existing cluster, the refit runs
    # on the next idle tick regardless of the interval, so a burst of new
    # topics (e.g. a web-knowledge enrichment run) is folded in promptly.
    topic_graph_refit_pending_threshold: int = 25
    # F10a: LLM-labelled topic clusters. A background worker
    # (:class:`app.core.conversation.topic_label_worker.ClusterLabelWorker`)
    # names each cluster ("weekend hiking plans") via a worker-LLM pass,
    # cached in ``kv_meta`` keyed by the cluster representative so it is
    # not recomputed every build. Entirely off the chat path (zero
    # per-turn cost). The label surfaces in the topic-graph snapshot
    # (Memory drawer) and feeds the F10e interest-map prompt block.
    topic_label_enabled: bool = True
    # How often the label worker runs a pass (default 30 min). Clamped to
    # a 60s floor in the parser.
    topic_label_interval_seconds: float = 1800.0
    # Max clusters (re)labelled per worker tick. Bounds worker-LLM spend
    # on a large or churned corpus; the rest are picked up next tick.
    topic_label_max_per_run: int = 4
    # Token cap for each label generation (a label is a 2-5 word phrase).
    topic_label_max_tokens: int = 32
    # F10g: per-cluster rolling digest memory. When True (persistent topic
    # graph), a worker writes one high-salience ``kind="topic_digest"``
    # memory per dense cluster -- a worker-LLM one-paragraph "what I know
    # about X" summary -- refreshed only on material size drift. Lives in
    # the normal pool (decays / pinnable / Memory tab) but is excluded
    # from clustering. Entirely off the chat path.
    topic_digest_enabled: bool = True
    # How often the digest worker runs a pass (default 1 h). 60s floor.
    topic_digest_interval_seconds: float = 3600.0
    # Max clusters (re)digested per worker tick (largest-first). Bounds the
    # worker-LLM spend; the rest are picked up next tick. Floor 1.
    topic_digest_max_per_run: int = 3
    # Token cap per digest generation (a 2-4 sentence paragraph). Floor 32.
    topic_digest_max_tokens: int = 256
    # A cluster needs at least this many members before it earns a stored
    # digest (small clusters are cheap to read raw). Floor 2.
    topic_digest_min_cluster_size: int = 6
    # When True, the F10c expansion path surfaces a cluster's digest as the
    # coarse "what I know about X" line and caps raw sibling enumeration to
    # ``rag_digest_sibling_cap`` (keeps a 40-member cluster from dumping 40
    # lines). No-op when no digest exists for the anchor cluster.
    topic_digest_surface_in_rag: bool = True
    # F10b: cluster-aware RAG diversity. When True (and a persistent topic
    # graph is wired), the retriever's final top-k selection caps how many
    # hits may come from a single topic cluster, so one dense cluster (e.g.
    # a big "get to know the user" knot) can't monopolise every slot and
    # crowd out other relevant context. Backfill guarantees the top-k is
    # still filled when only one topic is genuinely relevant -- diversity
    # is preferred, never enforced at the cost of dropping context.
    rag_cluster_diversity_enabled: bool = True
    # Max memory hits the retriever will take from one cluster before
    # deferring the rest (only applied while diversity is enabled and the
    # top-k still has room from other clusters). Clamped to a floor of 1
    # in the parser.
    rag_max_per_cluster: int = 3
    # F10c: topic multi-hop expansion. When a turn's strongest memory hit
    # (score >= ``rag_expand_trigger_score``) belongs to a topic cluster,
    # the retriever appends up to ``rag_expand_max`` sibling members of that
    # cluster whose cosine to the query clears ``rag_expand_min_sim`` --
    # beyond the top-k -- so Aiko gets the surrounding context, not just the
    # single closest line. Needs both the persistent topic graph and the
    # memory store wired; no-op otherwise. This *does* change prompt content
    # (a separate "Related notes from the same topic" section), so it is
    # gated and bounded. Set ``rag_topic_expansion_enabled=False`` to revert
    # to pure top-k retrieval.
    rag_topic_expansion_enabled: bool = True
    # Max sibling memories topic expansion appends per turn. Clamped to a
    # floor of 0 in the parser (0 disables expansion as surely as the flag).
    rag_expand_max: int = 2
    # The turn's strongest memory hit must score at least this for expansion
    # to fire (avoids rounding out weak/incidental cluster touches). Scores
    # include the small memory prior, so this sits a touch above the bare
    # cosine ``score_threshold``.
    rag_expand_trigger_score: float = 0.55
    # Minimum cosine (query vs sibling memory) for a cluster member to be
    # pulled in by expansion. Keeps the appended notes genuinely on-topic.
    rag_expand_min_sim: float = 0.45
    # F10g: when an anchor cluster has a stored digest and
    # ``topic_digest_surface_in_rag`` is on, the digest line replaces bulk
    # sibling enumeration and at most this many raw siblings still follow
    # (the digest is the gist; a couple of specifics drill in). Floor 0.
    rag_digest_sibling_cap: int = 1
    # K-time2 direct recall: when a query names a clearly retrospective
    # time window ("yesterday", "last Tuesday", "back in March"), also
    # pull the actual messages from that window straight out of SQLite so
    # verbatim "what did we say then" recall isn't limited to the semantic
    # top-N. ``rag_direct_recall_enabled`` is the master switch;
    # ``rag_direct_recall_max_messages`` caps how many lines are injected
    # per turn (floor 0 = disabled).
    rag_direct_recall_enabled: bool = True
    rag_direct_recall_max_messages: int = 6
    # F10e: "interest map" prompt block. A terse T1 (semi-stable) inner-
    # life line listing the top few labelled topic clusters by size --
    # "the things you and the user keep coming back to" -- so Aiko carries
    # a sense of her recurring threads without any per-turn LLM cost. Built
    # from the live topic-graph cluster map (label + member count only, no
    # mirror join). Each topic shows the F10a clean label once the label
    # worker has named it, falling back to the heuristic representative
    # summary otherwise. No-op in the non-persistent topic-graph mode.
    # Dropped under aggressive context pressure.
    interest_map_enabled: bool = True
    # How many topic clusters the interest-map block lists (largest first).
    # Clamped to a floor of 1 in the parser.
    interest_map_max_clusters: int = 5
    # Minimum cluster size for a topic to count as a recurring "interest"
    # worth surfacing. Raised to the topic graph's own min_cluster_size if
    # set lower; floor of 1 in the parser.
    interest_map_min_size: int = 4
    # Master switch for
    # :class:`app.core.proactive.curiosity_seed_worker.CuriositySeedWorker`.
    # When ``False`` the worker never registers its idle tick and
    # the seed surfacing path (inner-life bullet + NarrativeWeaver
    # candidate) silently produces empty output. Default ON because
    # the worker is the headline behaviour change of K9.
    curiosity_seed_enabled: bool = True
    # Cap on how many active (un-consumed) seeds the worker keeps
    # alive at once. ``is_ready`` short-circuits when the count is
    # at the cap so a fast-talking session can't pile up forty
    # never-mentioned seeds. Two seeds is a normal active steady
    # state; six is the headroom for "user only chats on weekends".
    curiosity_seed_max_active: int = 6
    # Cap on how many candidates the worker writes per successful
    # tick. The LLM proposes up to 5; this is the post-filter cap on
    # how many of the survivors actually become memories. Keeping
    # it at 2 keeps the inner-life bullet list readable.
    curiosity_seed_max_per_run: int = 2
    # Novelty floor against existing seeds: a candidate whose cosine
    # to ANY active seed >= this is rejected (would be a near-
    # duplicate). Lower = more eager to write; higher = stricter.
    # 0.85 lines up with the dedupe threshold used by the rest of
    # the memory store.
    curiosity_seed_min_novelty: float = 0.85
    # Cosine match threshold for the post-turn auto-resolve hook.
    # When (current user_text + assistant_text) cosines this high
    # against a seed embedding the seed is marked consumed and
    # demoted to archive tier. Lower than the graph filter on
    # purpose -- partial / oblique mentions should still count, the
    # alternative is a seed that hangs around forever once the
    # conversation drifts past it.
    curiosity_seed_resolve_threshold: float = 0.50
    # ── K11 pre-thought / counterfactual cache ───────────────────────
    # Master switch for
    # :class:`app.core.proactive.pre_thought_worker.PreThoughtWorker`.
    # When ``False`` the worker never registers its idle tick. The
    # cached ``pre_thought`` memories already written stay in the store
    # and keep surfacing through RAG until they decay out.
    pre_thought_enabled: bool = True
    # Cap on how many active pre-thoughts the worker keeps alive at
    # once. ``is_ready`` short-circuits when the count is at the cap so
    # a long idle stretch can't pile up dozens of speculative drafts;
    # ``run`` also prunes the oldest beyond this cap after writing.
    pre_thought_max_active: int = 12
    # How many candidate questions the first-stage LLM call proposes
    # per tick (the worker drafts replies for up to ``max_per_run`` of
    # the survivors).
    pre_thought_candidates: int = 4
    # Cap on how many drafted pre-thoughts the worker writes per
    # successful tick (one second-stage draft LLM call each).
    pre_thought_max_per_run: int = 2
    # Novelty floor against existing pre-thoughts: a candidate question
    # whose cosine to ANY active pre-thought question >= this is
    # rejected as a near-duplicate. Mirrors ``curiosity_seed_min_novelty``.
    pre_thought_min_novelty: float = 0.85
    # Per-hour / per-day budget on the worker's LLM calls (a tick can
    # spend 1 question call + up to ``max_per_run`` draft calls). The
    # worker runs on the local worker model, so the caps are generous —
    # they only exist to stop a misconfigured fast cadence from running
    # the local box hot.
    pre_thought_per_hour_cap: int = 6
    pre_thought_per_day_cap: int = 40
    # ── K21 fresh-eyes thread re-summary ─────────────────────────────
    # Master switch for
    # :class:`app.core.proactive.thread_resummary_worker.ThreadResummaryWorker`.
    # When ``False`` the worker never registers its idle tick and the
    # prompt never carries a "where this thread is now" block.
    thread_resummary_enabled: bool = True
    # Floor on conversation length before a fresh-eyes note is worth
    # drafting at all (a 3-message thread doesn't need re-synthesis).
    thread_resummary_min_messages: int = 12
    # Re-draft once this many new messages have landed since the note's
    # ``messages_at`` watermark (the "~50 turns" trigger from the
    # backlog).
    thread_resummary_message_interval: int = 50
    # Re-draft when the existing note is older than this many hours even
    # if the message-interval trigger hasn't fired (the "daily,
    # whichever comes first" trigger).
    thread_resummary_max_age_hours: float = 24.0
    # Per-hour / per-day budget on the worker's LLM calls (one call per
    # successful tick). Runs on the local worker model; the caps only
    # stop a misconfigured fast cadence from running the box hot.
    thread_resummary_per_hour_cap: int = 6
    thread_resummary_per_day_cap: int = 24
    # ── K52 wants ledger — desire with pressure ──────────────────────
    # Master switch for the wants ledger: the feeder worker, the
    # prompt provider, and the post-turn acted-on detection all gate
    # on this. Default ON — the ledger is the structural half of the
    # "will" family.
    wants_ledger_enabled: bool = True
    # How fast a want's pressure grows per wall-clock day. At 0.25 a
    # fresh want (initial 0.15) crosses the imperative threshold
    # (0.7) in roughly 2.2 days of being ignored.
    wants_growth_per_day: float = 0.25
    # Pressure at which the prompt cue flips from the soft "spend one
    # when a lull lands" list to the imperative "bring it up THIS
    # conversation" directive.
    wants_imperative_threshold: float = 0.7
    # Maximum live wants. At the cap the feeder refuses new wants
    # (expiry and acting are the only exits) so pressure ordering
    # stays honest.
    wants_cap: int = 8
    # Wants never acted on expire after this many days — an itch
    # that old has faded, and dropping it keeps the ledger from
    # becoming a guilt list.
    wants_max_age_days: float = 14.0
    # After a want is acted on, its source_ref is blocked from
    # re-entry for this many days so the feeder doesn't immediately
    # re-add the same topic.
    wants_reentry_cooldown_days: float = 5.0
    # Feeder worker cadence (idle scheduler). Hourly matches the
    # other kv-backed maintenance workers.
    wants_worker_interval_seconds: float = 3600.0
    # ── K53 initiative turns — deterministic floor-taking ────────────
    # Master switch for the per-turn initiative directive ("this turn
    # is yours"). Default ON — the scheduled directive is the
    # highest-leverage piece of the will family.
    initiative_turns_enabled: bool = True
    # Base cadence in turns between directives, before arc / axes
    # modulation (light arcs -2, cold axes +2/+4, floor 3).
    initiative_base_period: int = 8
    # Turns at the start of a session before the first directive can
    # fire — turn 1 is never a floor-grab.
    initiative_warmup_turns: int = 3
    # User messages at or above this many characters skip the
    # directive silently (the escape hatch); the counter does not
    # reset, so the next short turn fires instead.
    initiative_substantial_chars: int = 240
    # ── K55 thread ownership — she defends what she opened ───────────
    # Master switch. When a K53 directive / K52 imperative fires, the
    # turn is stamped as Aiko's thread; a short pivot away in the
    # next user reply grants exactly one "circle back" cue.
    thread_ownership_enabled: bool = True
    # Replies at or above this many characters count as engaged when
    # no embedding comparison is available (length-only fallback).
    thread_engaged_chars: int = 80
    # Cosine threshold between the user reply and the opened-topic
    # embedding at or above which the reply counts as engaged
    # regardless of length ("yeah I loved it" is an answer).
    thread_min_topical_similarity: float = 0.30
    # ── K54 topic appetite — she's allowed to be bored ────────────────
    # Master switch for the once-per-conversation "tapped out on this
    # topic, here's my offer instead" permission slip.
    topic_appetite_enabled: bool = True
    # Assistant replies below this many characters count as
    # ack-and-ask (not substantive) when measuring her contribution.
    appetite_short_reply_chars: int = 160
    # Share of recent assistant replies that must be short before
    # she reads as disengaged (boredom needs BOTH a looped topic and
    # her only nodding along).
    appetite_short_share_threshold: float = 0.6
    # Number of recent assistant replies examined for the share.
    appetite_window: int = 6
    # Minimum K52 want pressure required as the offer — negotiating
    # the topic without something to offer is just rudeness.
    appetite_min_want_pressure: float = 0.35
    # Both relationship axes (closeness AND comfort) must be at or
    # above this — the topic tug-of-war is an earned-intimacy move.
    appetite_min_axes: float = 0.15
    # ── K57 directed emotion episodes — feelings at the user ─────────
    # Master switch for the episode store (lonely / miffed / warm_glow
    # / smug / playful_jealous / hurt with cause + decay + thaw).
    emotion_episodes_enabled: bool = True
    # Live episodes kept at once; the strongest wins the prompt.
    emotion_episode_cap: int = 3
    # Base absence (hours) before a gap can register as loneliness;
    # shortened by up to 30% as closeness grows.
    emotion_lonely_threshold_hours: float = 5.0
    # Intensity at or above which the episode cue switches from
    # "let it tint the register" to "this is the register".
    emotion_high_band: float = 0.5
    # ── K59 tease economy — "you'll pay for that one" ────────────────
    # Master switch for the payback ledger (bank on K29 pushback /
    # light offences, collect later as a callback tease).
    tease_economy_enabled: bool = True
    # Most debts kept at once; the oldest is evicted by a newcomer.
    tease_cap: int = 5
    # Unrepaid debts expire after this many days — an old grudge
    # stops being funny.
    tease_expiry_days: float = 14.0
    # Wall-clock hours between collection offers — the running bit
    # must never tip into needling.
    tease_collect_cooldown_hours: float = 12.0
    # Humor axis floor for collection (the bit needs an established
    # teasing register to land).
    tease_min_humor: float = 0.2
    # A debt must age this long before it can be collected — an
    # immediate callback isn't a callback.
    tease_min_age_hours: float = 1.0
    # ── K60 tsundere expression mask ─────────────────────────────────
    # User-facing flavour dial: "off" (default) / "tsundere_light"
    # (masks lonely + warm_glow, frequent dere-slips) /
    # "tsundere_full" (also masks the thaw beat, rarer slips).
    expression_mask: str = "off"
    # Wall-clock days between dere-slips in light mode (full mode
    # uses 2.5x this value).
    mask_slip_cooldown_days: float = 2.0
    # Cosine threshold consumed by
    # :meth:`app.core.conversation.topic_graph.TopicGraph.is_close_to_any_cluster`
    # when the seed worker filters LLM candidates. Anything cosine-
    # close to any existing memory at or above this is rejected as
    # "we've already covered that." Default 0.65 sits between the
    # 0.55 single-link clustering threshold and the 0.85 dedupe
    # threshold so the filter catches "same topic, different angle"
    # without rejecting "adjacent but new" candidates.
    topic_graph_filter_threshold: float = 0.65
    # ── K1 personality backlog: Aiko's long-term goals ────────────────
    # Master switch for the K1 system: goal store + worker + persona +
    # tools + RAG bonus. Flipping ``False`` keeps the SQLite rows
    # intact (so goals survive between toggles), unregisters the
    # ``GoalWorker`` idle tick, silences the "Aiko's quiet long-term
    # goals" inner-life block via the renderer's gate, and stops the
    # ``[[goal:...]]`` self-tag from persisting new rows. The four
    # agent tools (``add_goal`` / ``update_goal_progress`` /
    # ``archive_goal`` / ``list_goals``) are independently gated by
    # ``tools.goals`` below — disabling the master switch leaves the
    # tools wired but they raise immediately because the store skips
    # initialisation. Default ON because the worker only bootstraps
    # once per cold install (single LLM call) and the reflection tick
    # is rate-capped to ``goal_worker_per_*_cap`` below.
    goals_enabled: bool = True
    # Cold-start bootstrap controls whether the ``GoalWorker`` is
    # allowed to fire its initial "propose ~3 goals from persona +
    # rolling summary" LLM call when the store is empty. Flip ``False``
    # if you'd rather seed goals manually via the Memory tab and never
    # let the worker propose its own. The reflection path is
    # unaffected -- once at least one active goal exists, the
    # bootstrap branch is never entered. Default ON so a fresh install
    # arrives with a small set of goals already in place.
    goal_worker_bootstrap_enabled: bool = True
    # Hourly + daily caps on LLM calls the GoalWorker may issue, both
    # the bootstrap pass and per-goal reflection ticks combined.
    # Dedicated :class:`app.core.memory.fact_check_rate_limiter.FactCheckRateLimiter`
    # with ``state_key='goal_worker.rate_state'``. The hourly cap of
    # 3 lines up with the worker's hourly tick cadence with two extra
    # slots for manual ``force_run`` calls; the daily cap of 12 lets
    # Aiko reflect on each of the five active goals twice a day with
    # headroom for the bootstrap pass on day one. Set both to 0 to
    # disable autonomous calls entirely without unregistering the
    # worker (e.g. when you want only the ``[[goal:...]]`` self-tag
    # and the in-turn tools to write goals).
    goal_worker_per_hour_cap: int = 3
    goal_worker_per_day_cap: int = 12
    # ── K16. Unified ambient grounding line ───────────────────────────
    # The grounding line is one paragraph at the top of the system
    # prompt that fuses the seven "ambient" inner-life signals
    # (circadian, world, activity-awareness, affect/mood,
    # relationship-pulse, user_state, ambient_noise) into a single
    # continuous-awareness paragraph. The companion-feel hypothesis is
    # that the LLM treats one paragraph as continuous awareness rather
    # than seven separate facts to recite.
    #
    # Three modes (the canonical reference; mirrored verbatim in
    # docs/personality-backlog/shipped.md and AGENTS.md):
    #
    # ``off`` (default): no grounding line; the seven granular blocks
    #   render as today. Safe rollback target. Use this until you've
    #   verified ``replace`` reads well in your sessions.
    # ``replace``: the grounding line replaces all eight ambient
    #   blocks (the seven listed above plus mood_hint). Cleanest test
    #   of the hypothesis. Most aggressive.
    # ``split``: the grounding line replaces situational signals
    #   (circadian, world, activity, ambient_noise) but keeps
    #   {affect, mood_hint, relationship, user_state} as standalone
    #   blocks. Use when you want to keep the trend phrasing
    #   (affect "lately you've been..."; relationship phase line)
    #   that the fused line cannot represent without dilution.
    #
    # Suppression matrix (which blocks render in which mode):
    #
    #   block            off    split    replace
    #   grounding_line   empty  shown    shown
    #   circadian        shown  dropped  dropped
    #   world            shown  dropped  dropped
    #   activity         shown  dropped  dropped
    #   ambient_noise    shown  dropped  dropped
    #   affect           shown  shown    dropped
    #   mood_hint        shown  shown    dropped
    #   relationship     shown  shown    dropped
    #   user_state       shown  shown    dropped
    #   anniversary, profile bullets, pajama, knowledge_gaps,
    #   belief_gaps, novelty, stagnation, agenda, axes, petname,
    #   vocal_tone, catchphrase, narrative, arc -- ALWAYS shown,
    #   never affected by this mode.
    #
    # Verifying the flip took effect:
    #   - MCP ``get_last_response_detail`` shows
    #     ``provider_ms.grounding_line`` non-zero in ``replace``/``split``,
    #     missing or zero in ``off``.
    #   - DEBUG ``prompt built:`` log line: ``providers=`` count drops
    #     by the number of suppressed granular blocks.
    #
    # Invalid values (anything other than off/replace/split) clamp to
    # ``off`` with a debug log so a typo in the config never breaks the
    # prompt.
    grounding_line_mode: str = "off"
    # ── K-time1. Wall-clock prefixes on chat history ──────────────────
    # When True (the default), every message in the chat history sent to
    # the LLM is prefixed with a short relative-age tag like ``[2 min
    # ago] ...`` / ``[just now] ...`` / ``[yesterday 18:45] ...``. The
    # current user message Aiko is replying to is appended separately
    # and never gets a prefix.
    #
    # Why this exists: without per-message timestamps the LLM has no
    # clock against the conversation -- e.g. {user} saying "I'm
    # planning to visit my grandparents in half an hour" 2 minutes ago
    # gets pattern-matched as a completed past event, and Aiko asks
    # "did you make it back?". The prefix gives the LLM an explicit
    # clock so future plans stay future and recent moments read as
    # recent. The accompanying persona block teaches Aiko how to use
    # the prefix (and not to quote it back).
    #
    # Token cost: ~4-6 tokens per kept history message. Negligible
    # against the configured ``ollama.context_window`` budget.
    #
    # Turn OFF if you want a byte-identical history to the pre-K-time1
    # behaviour (e.g. for A/B comparison, or if your LLM treats the
    # bracketed metadata as part of the dialogue).
    history_age_prefix_enabled: bool = True
    # K51 -- cue-register rotation. When ON, inner-life cue blocks that
    # open with the literal "Heads-up:" get the prefix rotated across a
    # few register shapes ("Heads-up:" / "Quiet note:" / "Noticing:" /
    # bare) at prompt-assembly time, deterministic per turn, so the
    # model never reads the same coach template several times in one
    # prompt. OFF = byte-identical legacy cues (the shared-prefix lint
    # still runs).
    cue_register_rotation_enabled: bool = True
    # Rolling summary background worker.
    summary_idle_seconds: float = 15.0  # quiet time before summarising
    summary_min_unsummarized_messages: int = 6  # minimum new msgs to trigger
    summary_target_tokens: int = 600  # cap on the summary the LLM produces
    # When the *next* prompt would exceed this fraction of the context window,
    # schedule a background compaction immediately (don't wait for idle).
    max_prompt_tokens_pct: float = 0.8

    # ── Speaking-window scheduler (Phase 2a) ──────────────────────────
    # The scheduler drains LLM-driven background jobs (reflection, profile
    # updates, agenda grooming, narrative weaving, etc.) while Aiko is
    # speaking the previous reply. Hot-path stays cheap; the workers feel
    # "free" because they hide under TTS playback.
    scheduler_idle_seconds: float = 20.0  # quiet time before idle drain
    scheduler_speaking_window_grace_ms: int = 200  # soft-close grace
    scheduler_max_job_seconds: float = 8.0  # advisory per-job cap

    # ── Inner-life workers (Phase 2c onward) ──────────────────────────
    # ReflectionWorker fires after every turn unless skipped by emotional-delta
    # throttling. Set to a higher number to throttle more aggressively.
    reflection_min_seconds_between: float = 8.0
    reflection_emotional_delta_threshold: float = 0.05
    # User-profile worker runs every N user turns; lowered when each pass is
    # richer (covers all fields per pass).
    user_profile_min_turns: int = 6
    # Agenda groomer runs every N user turns when there are >= 1 agenda items.
    agenda_groom_every_n_turns: int = 8
    # Conversation-arc worker (cheap LLM, runs each turn at low priority).
    arc_update_every_n_turns: int = 1
    # Self-image pulse: once per UTC day in the first speaking window after
    # midnight. ``enabled=False`` skips entirely.
    self_image_pulse_enabled: bool = True
    # K65d: seed the self-image pulse from the K9 interest map. When on
    # (default) the daily rewrite is handed a "lately you've been spending
    # time on: X, Y, Z" line (the densest topic clusters) so her
    # self-narrative can legitimately reflect what she's been engaging with
    # ("lately I've been drawn to …"). Off → the pulse uses only her
    # top-salience self/reflection memories as before. No effect on a cold /
    # unlabelled store (the provider returns nothing).
    self_image_interest_seed_enabled: bool = True
    # ``num_predict`` ceiling for the self-image LLM call. The prompt asks
    # for a 60–120 word paragraph (~160 tokens), but reasoning models like
    # qwen3.x can leak chain-of-thought into the response and eat budget
    # before the actual paragraph starts. The default leaves headroom for
    # that without being so large that a runaway response is unbounded.
    # Bump this if you keep seeing ``surface=self_image_worker`` truncation
    # warnings in the log.
    self_image_max_tokens: int = 320
    # Prepared-nudge job runs in late speaking windows; cap how stale a
    # prepared nudge can be before ProactiveDirector re-synthesises.
    prepared_nudge_ttl_seconds: float = 600.0

    # ── Filler injection (Phase 1c) ───────────────────────────────────
    # If the LLM hasn't produced a first stream delta within this many
    # ms, the TurnRunner emits a short filler ("Hmm,", "Let me think,")
    # via TTS so Aiko isn't silent. Set ``filler_enabled`` to false to
    # disable globally.
    filler_enabled: bool = True
    filler_first_token_ms: int = 800

    # ── P14: heuristic tool-pass gate ─────────────────────────────────
    # When true (default), turns with no tool-shaped signal skip the
    # forced ``chat_with_tools`` decision pass entirely — the largest
    # avoidable time-to-first-token contributor when tools are enabled.
    # Continuity signals (finished-task block, active tasks, previous
    # turn dispatched a tool) always run the pass. Set to false to
    # restore the old always-run behaviour (the kill-switch if tool
    # recall ever regresses). See
    # [`app/core/session/tool_pass_gate.py`](../session/tool_pass_gate.py).
    tool_pass_gate_enabled: bool = True

    # ── Skills framework: progressive tool disclosure ─────────────────
    # When true, the brain exposes only the matched tool families plus the
    # always-on core (``brain_core_skills``) on a tool-shaped turn, instead
    # of the whole registry. Off (default) = today's behaviour: every
    # registered tool every gated turn. ``world`` is in the core so Aiko's
    # spontaneous room actions (sip tea, shift posture) survive on turns
    # whose text named no item. See docs/skills-framework.md.
    skill_router_enabled: bool = False
    brain_core_skills: tuple[str, ...] = ("time", "recall", "world")
    # Worker-lane router: narrows the workflow planner's skill menu to the
    # group(s) relevant to the goal before each plan. Off (default) = full
    # menu, today's behaviour.
    workflow_skill_router_enabled: bool = False

    # ── Memory consolidation (Phase 4b) ───────────────────────────────
    # MemoryConsolidator merges near-cosine clusters in the SQLite store
    # so we don't drown in tiny redundant fact-rows. Runs in chunks during
    # the speaking window so a single pass never exceeds ``chunk_size``
    # memories. ``enabled=false`` short-circuits.
    consolidator_enabled: bool = True
    consolidator_min_hours_between: float = 18.0
    consolidator_chunk_size: int = 40
    consolidator_similarity_threshold: float = 0.84
    consolidator_min_cluster_size: int = 2
    consolidator_use_llm_merge: bool = True

    # Weekly relationship-pulse: a single LLM pass that summarises
    # how the relationship has been going and writes it as a salience-
    # boosted "self_tagged" memory. Runs at most once per ``min_hours``.
    relationship_pulse_enabled: bool = True
    relationship_pulse_min_hours: float = 168.0  # ~7 days
    relationship_pulse_min_turns: int = 30
    # ``num_predict`` ceiling for the weekly pulse. The prompt asks for
    # 1–2 sentences (≤50 words ~ 70 tokens), but qwen3.x-style models
    # can leak hidden reasoning before the answer starts. 256 leaves
    # comfortable headroom; bump it if you still see truncation warnings
    # tagged ``surface=relationship_pulse``.
    relationship_pulse_max_tokens: int = 256

    # ── Cadence / prosody (Phase 5b) ──────────────────────────────────
    # ProsodyDispatcher inserts per-sentence reactions, occasional micro
    # prefixes ("Mm.", "Oh,") and gentle pause-style punctuation tweaks.
    # All hints are text-only — engines that ignore punctuation are safe.
    cadence_enabled: bool = True
    # Layer 4 (expressive speech): auto-sprinkle ``breath`` / ``soft_sigh``
    # earcons on the first sentence of a melancholy / wistful / sad
    # turn. Cooldown-gated inside the cadence layer so a long
    # heart-to-heart conversation doesn't wheeze. Set to false to
    # silence all auto-sprinkle behaviour; the LLM can still emit
    # ``[[breath]]`` / ``[[chuckle]]`` etc. inline regardless.
    earcon_auto_sprinkle: bool = True
    # Layer 1c (expressive speech): opt-in gate for runtime per-reaction
    # ``model.temp`` mutation. Pocket-TTS is sensitive to temperature
    # excursions away from its tuned baseline -- empirically a delta
    # of even ±0.05 can introduce pitch / timbre artefacts on some
    # voices. Default OFF so the engine always uses the configured
    # ``tts.pocket_tts_temp`` baseline; flip on once you've validated
    # the deltas in :data:`app.tts.pocket_tts_service._REACTION_TEMP_DELTA`
    # sound right on the active voice file.
    tts_runtime_temp_enabled: bool = False
    # Layer 5 (expressive speech): opt-in gate for per-reaction speed
    # jitter. Pocket-TTS implements speed by scaling the playback
    # ``sample_rate``, which couples speed and pitch (a 10% faster
    # sentence is also ~1.6 semitones higher). With per-reaction
    # sub-caps active, that pitch couples to the affect channel and
    # the user perceives "her voice keeps changing" between sentences
    # -- even if each individual band is small. Default OFF so every
    # sentence plays at the engine's tuned 1.0× baseline; flip on once
    # you've listened to the active voice through
    # ``tools/tts_speed_ab.py`` at the proposed band. The user's
    # static pacing slider (``assistant.tts_length_scale``) is honoured
    # regardless of this gate -- it's a deliberate global knob, not
    # per-sentence affect drift.
    tts_runtime_speed_enabled: bool = False

    # ── Aiko style-pattern tracker (response-variability anti-rut) ────
    # Watches Aiko's own recent assistant turns for opener / question /
    # length ruts and surfaces a soft "Heads-up" inner-life cue when
    # one of the bands trips. Sibling architecture to the K6 / K18
    # detectors above; the persona's "Style patterns I'm in" section
    # pairs with the cues this tracker emits. Defaults are calibrated
    # to the diagnostic captured against ~120 assistant messages:
    # opener concentration ~39%, question-end rate ~87%, avg ~52
    # words / 4.9 sentences. Tune via these knobs without code changes.
    style_tracker_enabled: bool = True
    style_tracker_window: int = 12
    style_tracker_warmup: int = 6
    style_tracker_opener_count_threshold: int = 4
    style_tracker_opener_topk_share: float = 0.60
    style_tracker_question_rate_threshold: float = 0.75
    style_tracker_avg_questions_threshold: float = 1.5
    style_tracker_length_avg_threshold: float = 50.0
    style_tracker_cue_cooldown_turns: int = 5

    # ── K47: question/share balance (stop interviewing) ───────────────
    # Proactive complement to the reactive style-tracker question
    # saturation cue. A rolling per-session ratio of Aiko's replies that
    # contain a question; once it exceeds ``ratio_threshold`` over a full
    # ``window``, the question-pushing inner-life providers
    # (curiosity_seeds / forward_curiosity / follow_up / knowledge_gaps +
    # the narrative open_question nudge) are suppressed for the next
    # ``suppress_turns`` turns and a share-first cue is injected BEFORE
    # the LLM call. See
    # [`app/core/conversation/question_balance.py`](../conversation/question_balance.py).
    question_balance_enabled: bool = True
    question_balance_ratio_threshold: float = 0.55
    question_balance_window: int = 10
    question_balance_suppress_turns: int = 2

    # ── K48: tease rhythm (banter as a budget) ────────────────────────
    # Classify tease-shaped assistant turns over a rolling window, read
    # whether the previous tease landed (K32 laugh reaction vs. a
    # short/curt reply), and surface an "ease off" or "one more step is
    # safe" cue. Escalation is gated by the ``humor`` relationship axis
    # so early-relationship Aiko stays gentle. See
    # [`app/core/conversation/tease_rhythm.py`](../conversation/tease_rhythm.py).
    tease_rhythm_enabled: bool = True
    tease_rhythm_window: int = 6
    tease_rhythm_consecutive_cap: int = 3
    tease_rhythm_green_light_humor: float = 0.2
    tease_rhythm_cooldown_turns: int = 3

    # ── K13: stylometric mirror (Jacob-side stylometry) ───────────────
    # Tracks Jacob's writing style across recent user turns and emits
    # a one-line "How Jacob writes lately: terse, casual, asks back
    # often" directive so Aiko's register stays calibrated even when
    # the recent history window doesn't cover yesterday. Five axes:
    # terseness / formality / emoji / slang / question rate. Pure
    # rolling-window analyzer (no embedder, no LLM); persisted via a
    # tiny ``user_style_signal`` JSON-blob table so the window
    # survives restart. Unlike the K6/K18/anti-rut cues this block is
    # ALWAYS rendered (including aggressive mode) because it shapes
    # register, which is the first thing aggressive mode wants to
    # preserve. See [`app/core/persona/style_signal.py`](style_signal.py).
    style_signal_enabled: bool = True
    style_signal_window: int = 30
    style_signal_warmup_min: int = 8
    style_signal_terse_threshold: float = 0.55
    style_signal_formal_threshold: float = 0.55
    style_signal_emoji_threshold: float = 0.05
    style_signal_slang_threshold: float = 0.15
    style_signal_question_threshold: float = 0.40

    # ── K14: implicit engagement signals (latency + length) ──────────
    # Per-turn detector that scores Jacob's reply latency + message
    # length against rolling baselines and routes the signal to two
    # consumers:
    #   * voice mode → ``closeness_delta`` folded into the
    #     relationship-axes updater (snappy replies nudge closeness up;
    #     long voice gaps + curt messages nudge it down)
    #   * typed mode → ``absence_seconds`` band feeds a one-shot
    #     "absence-curiosity" inner-life cue on the NEXT user turn,
    #     and a label of ``"abandoned"`` suppresses the typed
    #     proactive nudge (mirrors the K4 vent gate).
    # Typed latency is deliberately NOT fed into closeness drift -- per
    # the project's design note, a typed pause is thinking time, not
    # disengagement. The latency window is voice-only; the length
    # window is shared with the K13 stylometric mirror via its
    # ``recent_word_counts()`` method (no duplicate buffer).
    # See [`app/core/affect/engagement_tracker.py`](engagement_tracker.py).
    engagement_tracker_enabled: bool = True
    engagement_window: int = 12
    engagement_warmup_min: int = 6
    engagement_latency_z_strong_drop: float = 1.5
    engagement_length_z_strong_drop: float = -1.0
    engagement_closeness_delta_max: float = 0.04
    engagement_absence_curiosity_enabled: bool = True
    engagement_absence_curiosity_min_seconds: float = 1800.0
    # When ``True`` (default), the typed-proactive eligibility check
    # treats an ``"abandoned"`` engagement label as a hard reason to
    # skip the silence-break nudge. Set to ``False`` to ignore the
    # engagement label on the proactive path (the typed nudge then
    # falls back to the legacy cooldown / presence / vent gates only).
    engagement_proactive_gate: bool = True

    # ── K5: mood shell tilt ──────────────────────────────────────────
    # Per-turn one-line emotional directive derived from the live
    # :class:`AffectState` (valence + arousal) and
    # :class:`RelationshipAxesState` (closeness/humor/trust/comfort).
    # NOT a topic suggestion -- a tonal register cue that colours
    # delivery only (pacing, word choice, sentence length, warmth).
    # Returns ``""`` on the common turn; only fires when affect is
    # off-baseline AND/OR a relationship axis crosses
    # ``mood_shell_axis_threshold`` (default 0.5, mirrors the existing
    # ``relationship_axes._NOTABLE_THRESHOLD``). Part of the K16
    # ``replace`` suppression set (the unified grounding line folds
    # the same surface area). See [`app/core/affect/mood_shell.py`](mood_shell.py).
    mood_shell_enabled: bool = True
    mood_shell_axis_threshold: float = 0.5

    # ── K17: clarification-repair detector ────────────────────────────
    # Per-turn regex classifier that fires when Jacob signals he was
    # misunderstood ("no that's not what I meant", "huh?", "wait
    # what"). The post-turn flow stashes a one-shot result and the
    # next-turn inner-life provider renders a "Heads-up: you missed
    # his last point" cue so Aiko re-reads, owns it, and answers
    # what was actually asked. No LLM cold path; the regex hot path
    # is the whole detector. Two bands -- ``strong`` (explicit
    # correction) vs ``mild`` (soft confusion). See
    # [`app/core/conversation/clarification_detector.py`](clarification_detector.py).
    clarification_repair_enabled: bool = True

    # ── K8: affect rupture-and-repair ─────────────────────────────────
    # Per-turn detector that fires when {user_name}'s valence drops
    # by more than ``rupture_valence_drop_threshold`` between the
    # pre-turn affect snapshot and the post-turn AffectUpdater
    # result, *and* Aiko's just-emitted reaction wasn't already an
    # empathetic one (concerned/gentle/sad/calm -- those would
    # trigger false positives because Aiko was responding to
    # existing bad news, not causing it). The post-turn flow
    # stashes a one-shot result on the controller; the next turn's
    # inner-life provider renders a "Heads-up: their mood just
    # dipped right after your last reply" cue so Aiko softens and
    # checks in once. See
    # [`app/core/affect/affect_rupture_detector.py`](affect_rupture_detector.py).
    rupture_repair_enabled: bool = True
    rupture_valence_drop_threshold: float = 0.12

    # ── K37: emotional contagion ──────────────────────────────────────
    # Aiko's affect tilts a small, capped amount toward the user's
    # estimated affect each turn (separate from how it reacts to her own
    # ``[[reaction:...]]``). ``contagion_strength`` is the fraction of
    # the valence/arousal gap closed per turn; ``contagion_max_per_turn``
    # is the hard per-axis ceiling on that move, so a big mismatch can
    # only ever pull her this far in one turn. See
    # [`app/core/affect/affect_state.py`](affect_state.py)
    # (``estimate_user_affect`` + ``_apply_user_contagion``).
    contagion_enabled: bool = True
    contagion_strength: float = 0.15
    contagion_max_per_turn: float = 0.05

    # ── K45: mood inertia (instant face, lagging heart) ───────────────
    # Master switch for the one-shot "your face jumped to X but
    # underneath you're still Y — let the words catch up" cue armed
    # post-turn when the fresh ``[[reaction:...]]`` tag's implied
    # affect target strongly outruns the smoothed AffectState.
    # Thresholds + cooldown live on ``MemorySettings.mood_inertia_*``;
    # the avatar-side damping flag is ``AvatarSettings
    # .mood_inertia_damping``. See
    # [`app/core/affect/mood_inertia.py`](mood_inertia.py).
    mood_inertia_enabled: bool = True

    # ── K23: subtle misattunement detection ──────────────────────────
    # Per-turn detector that fires ``mild_disengagement`` when {user}
    # goes very short or pivots topics right after a substantial Aiko
    # reply. Sits in the gap between K17 (explicit "that's not what I
    # meant" regex) and K14 (multi-turn engagement aggregate). The
    # cue lands on the SAME turn that's about to reply -- pulling
    # back IS the next response.
    #
    # Two trigger paths, both gated by the cooldown:
    #
    # 1. ``shrink``: ``prev_aiko_words >= shrink_min_prev_words``
    #    AND ``this_user_words <= shrink_max_user_words``. A one-word
    #    reply after a 60-word answer reads as "you went quiet".
    # 2. ``pivot``: K6 :class:`NoveltyDetector` band is
    #    ``strong_novelty`` AND ``this_user_words <=
    #    pivot_max_user_words``. A short pivot away without engaging
    #    Aiko's last point.
    #
    # Cooldown lives on :class:`SessionController` and counts down
    # one per turn regardless of trigger state. Default ``3`` keeps
    # the cue from stacking across consecutive disengaged turns
    # (the conditions can persist when {user} is genuinely busy).
    #
    # See
    # [`app/core/affect/misattunement_detector.py`](../affect/misattunement_detector.py).
    misattunement_detection_enabled: bool = True
    misattunement_shrink_min_prev_words: int = 30
    misattunement_shrink_max_user_words: int = 8
    misattunement_pivot_max_user_words: int = 8
    misattunement_cooldown_turns: int = 3

    # ── K69: implicit-need reading (vent vs fix vs reassure) ──────────
    # Master switch for the per-turn response-mode classifier. When on, a
    # cheap pure heuristic over the live user message (cue words + the
    # K14 affect read + the K4 arc) picks witness / problem_solve /
    # reassure / celebrate (or stays silent on a neutral turn) and renders
    # a one-line steer so the reply *mode* matches the need, not the
    # literal words. No LLM on the hot path. The confidence floor lives in
    # ``memory.implicit_need_min_confidence``. Off -> the provider stays
    # empty.
    implicit_need_enabled: bool = True

    # ── K30: self-noticing cues (agreement / flat-affect / repeated) ──
    # K20 metacognitive calibration tracks {user}'s trust in Aiko;
    # K30 is the symmetric loop -- Aiko notices HER own patterns.
    # One master switch fans into three sub-detectors that can be
    # toggled independently while tuning:
    #
    # * ``self_noticing_agreement_streak_enabled`` -- per-provider
    #   call regex over the last ``self_noticing_window`` rendered
    #   assistant replies (SQLite round-trip, K23-style). Fires when
    #   the agreement-token share crosses
    #   ``self_noticing_agreement_threshold`` AND pushback count
    #   sits at or below ``self_noticing_max_pushback``.
    # * ``self_noticing_flat_affect_enabled`` -- reads a small
    #   in-memory ``(valence, arousal, reaction)`` ring populated
    #   post-turn (there's no ring on ``AffectState`` itself). Fires
    #   when both scalar ranges sit at or below their thresholds AND
    #   no reaction outside ``LOW_BAND_REACTIONS`` fired in the
    #   window.
    # * ``self_noticing_repeated_thought_enabled`` -- post-turn
    #   cosine pass on Aiko's just-finished reply against a tiny
    #   embedding ring (last 3 assistant vectors, reusing K22's
    #   synchronous ``turn_vec`` -- no extra embed call). Fires
    #   when ``max_cosine >= self_noticing_repeated_cosine_threshold``;
    #   the cue surfaces on the NEXT turn (one-shot carry-forward
    #   flag), matching v1's detect-and-log discipline.
    #
    # ``self_noticing_cooldown_turns`` arms after the streak
    # detectors fire so the same Heads-up doesn't re-stack for the
    # next several turns. Repeated-thought has no multi-turn
    # cooldown -- the carry-forward flag is naturally one-shot.
    # See
    # [`app/core/affect/self_pattern_detector.py`](../affect/self_pattern_detector.py).
    self_noticing_enabled: bool = True
    self_noticing_agreement_streak_enabled: bool = True
    self_noticing_flat_affect_enabled: bool = True
    self_noticing_repeated_thought_enabled: bool = True
    self_noticing_window: int = 6
    self_noticing_warmup: int = 4
    self_noticing_agreement_threshold: float = 0.80
    self_noticing_max_pushback: int = 0
    self_noticing_flat_valence_range: float = 0.10
    self_noticing_flat_arousal_range: float = 0.10
    self_noticing_repeated_cosine_threshold: float = 0.85
    self_noticing_cooldown_turns: int = 5

    # ── K27: daily personality colour (Aiko's day) ────────────────────
    # Master switch for the slow ambient colour rolled once per local
    # day from the 10-entry palette in
    # [`app/core/affect/day_color.py`](../affect/day_color.py).
    # When off, the inner-life block short-circuits to ``""`` and the
    # :class:`DayColorWorker` skips its tick -- no roll, no read.
    #
    # K27 sits between two adjacent layers:
    #
    # * K5 mood-shell tilt is *reactive* and decays toward baseline;
    #   K27 is the slow under-current K5 reacts on top of.
    # * K30 self-noticing flat-affect detects when Aiko's session
    #   has gone flat; K27 gives her a non-flat starting point so
    #   the K30 measurement actually means "she's slipped" rather
    #   than "she has no colour to begin with".
    #
    # The :class:`DayColorWorker` is the canonical path (runs every
    # ``day_color_check_interval_seconds`` and only writes when the
    # local date has rolled over). The provider has a cheap lazy
    # fallback for the first-turn-after-midnight case when the
    # idle-worker hasn't fired yet.
    day_color_enabled: bool = True
    # Cadence of the idle-worker tick. Defaults to 1h (3600s) -- the
    # tick is cheap (one kv_get + one date compare) so a tighter
    # cadence has negligible cost. Floored at 60s in ``_parse_agent``
    # so a buggy override can't spin the scheduler.
    day_color_check_interval_seconds: int = 3600

    # ── K68: embodied vitality ────────────────────────────────────────
    # Master switch for the slow-moving body-energy layer. When on, a
    # single ``energy`` scalar in [0, 1] (kv_meta ``aiko.vitality``)
    # relaxes toward the circadian baseline over wall-clock idle time, is
    # spent by long / emotionally heavy turns, and -- the headline --
    # **livens up when the conversation is interesting** (engaged user,
    # high arousal, novel topic). It feeds the avatar's gesture/breath
    # amplitude and gates a soft low/high-energy register cue at the
    # extremes. A mechanic, not persona text. Thresholds / rates live
    # under MemorySettings. Off -> the provider stays empty, no spend, no
    # broadcast.
    vitality_enabled: bool = True
    # Cadence of the :class:`VitalityWorker` idle tick (recovers energy
    # toward baseline + broadcasts so she visibly droops while left
    # alone). Cheap tick (one kv read + one float relax). Floored at 60s
    # in ``_parse_agent``.
    vitality_check_interval_seconds: int = 900
    # Off-rhythm-day exceptions. When on, once per local day Aiko rolls a
    # rhythm (early-bird / night-owl / fully-flipped / sluggish / wired)
    # that reshapes the circadian resting curve for that day -- so she's
    # occasionally drowsy at noon and wired at 3am instead of running the
    # exact same energy shape every day. Probability + stability live in
    # ``memory.vitality_rhythm_exception_chance``; the rhythm rides the
    # same kv lazy-roll as K27 day colour. Requires ``vitality_enabled``.
    # Off -> every day uses the plain circadian baseline.
    vitality_rhythm_enabled: bool = True

    # ── H3: mood-drift narrator ───────────────────────────────────────
    # Slow, read-only awareness of how the user's mood + the relationship
    # axes have drifted over days/weeks. A daily sampler
    # (:class:`MoodDriftSampleWorker`) records one (valence + four axes)
    # point per local day into a small kv ring; the ``_render_mood_drift_
    # block`` provider detects a sustained low / recovery / single-axis
    # drift and surfaces ONE gentle reflective cue, then stays quiet until
    # a *different* finding appears. Off → no sampling, no cue.
    mood_drift_enabled: bool = True
    # Sampler cadence. Cheap (one kv_get + a date compare on the no-op
    # tick; two SQLite reads + one kv_set once per day). Floored at 60s
    # in ``_parse_agent``.
    mood_drift_check_interval_seconds: int = 3600
    # Minimum days between two surfaced notes. Guards against two
    # different findings firing back-to-back; the per-finding signature
    # watermark already stops the *same* finding repeating.
    mood_drift_cooldown_days: float = 4.0

    # ── K15: self-disclosure / vulnerability budget ───────────────────
    # Master switch for the rolling token-bucket that paces Aiko's
    # personal disclosures (``[[remember:self:...]]`` tags). When off,
    # the post-turn spend hook is a no-op and the provider returns
    # ``""`` -- no kv_meta writes, no prompt cue.
    #
    # K15 sits between two adjacent layers:
    #
    # * K27 day_color is the slow weather (stable for the day).
    # * The relationship-axes / shared-moments system tracks
    #   closeness + trust which K15 reads at provider time to size
    #   the bucket capacity.
    #
    # Soft enforcement only: the cue surfaces in the prompt but
    # never blocks the reply or suppresses the underlying memory
    # write. The persona block teaches Aiko to read the cue but
    # explicitly allows real moments to override -- the budget is
    # pacing, not a rule.
    vulnerability_budget_enabled: bool = True
    # Capacity floor when closeness + trust are both deeply negative
    # (or at first-boot defaults). Min 1 so the bucket math always
    # has a non-zero divisor.
    vulnerability_budget_min_capacity: int = 1
    # Capacity ceiling when closeness + trust are both at +1. 12 is
    # roughly "four tier-3 disclosures or twelve tier-1 surface
    # taste lines in one session before the cue starts firing".
    vulnerability_budget_max_capacity: int = 12
    # Bucket regeneration rate in tokens / hour. Default 0.5 means
    # a full max-cap bucket (12 tokens) refills in ~24h; a single
    # tier-3 spend (6 tokens) regenerates in ~12h. Tuned so a real
    # soft moment from yesterday is mostly recovered today.
    vulnerability_budget_regen_per_hour: float = 0.5
    # Per-tier costs. Tier 1 = surface preference, tier 2 = mild
    # admission, tier 3 = genuine softness. The 1 / 3 / 6 ladder
    # means three tier-1 lines cost the same as one tier-2, and
    # two tier-2 lines cost the same as one tier-3.
    vulnerability_budget_tier1_cost: int = 1
    vulnerability_budget_tier2_cost: int = 3
    vulnerability_budget_tier3_cost: int = 6

    # ── J11: affection-style learning ─────────────────────────────────
    # Learn which way of expressing care (touch / teasing / appreciation
    # / words / giving space) reliably lands for this user and tilt the
    # expression mix toward it. Primary signal is passive engagement
    # (K14) attributed to the kind Aiko expressed last turn; K32
    # reactions are an optional confirmation booster. Never announced,
    # never collapses a channel (the bias multiplier is floored).
    affection_style_enabled: bool = True
    # Per-turn nudge applied to a kind's share from one passive
    # engagement observation. Small so the weighting moves over many
    # turns, not one.
    affection_style_learning_rate: float = 0.04
    # Extra nudge from an explicit K32 reaction confirmation. Slightly
    # larger than the passive rate (a deliberate click is stronger
    # evidence than an inferred engagement band).
    affection_style_reaction_weight: float = 0.06
    # Minimum share any single kind keeps after renormalisation. With
    # five kinds the uniform share is 0.2; a 0.05 floor means even a
    # never-rewarded channel keeps a quarter of its uniform odds.
    affection_style_floor: float = 0.05
    # Half-life (days) of the slow decay toward uniform run by the idle
    # worker. ~30 days means a learned preference fades over a month of
    # the opposite signal — long enough to be stable, short enough to
    # track a real change in the relationship.
    affection_style_decay_half_life_days: float = 30.0
    # How hard the learned weight tilts a gate. 0 disables biasing
    # entirely (learning still runs + stays observable); the multiplier
    # is 1 + strength * (weight/uniform - 1), clamped to the band below.
    affection_style_bias_strength: float = 0.5
    affection_style_bias_floor: float = 0.6
    affection_style_bias_ceil: float = 1.5
    # Idle-worker decay cadence (seconds). Default 6h.
    affection_style_decay_interval_seconds: int = 21600

    # ── K74: humor-style calibration ──────────────────────────────────
    # Learn which KIND of funny (pun / deadpan / absurdist /
    # self-deprecating / playful-roast) lands for this user. Mirrors J11
    # exactly: passive engagement (K14) attributed to the humor kind Aiko
    # used last turn, K32 laugh/eyeroll reactions as a sparse confirmation
    # booster, floored weights, slow decay toward uniform. NOT rendered as
    # a standalone block — the learned top register only flavours the
    # *existing* K48 tease-rhythm cue (when humour is already in play).
    humor_style_enabled: bool = True
    humor_style_learning_rate: float = 0.04
    humor_style_reaction_weight: float = 0.06
    humor_style_floor: float = 0.05
    humor_style_decay_half_life_days: float = 30.0
    # Top register must sit this many × the uniform share before the
    # register hint rides the tease cue (a genuinely emerged preference).
    humor_style_hint_min_rel: float = 1.25
    # Idle-worker decay cadence (seconds). Default 6h.
    humor_style_decay_interval_seconds: int = 21600

    # ── J12: intimacy pacing & boundary calibration ───────────────────
    # The consent dial. A float in [0, 1] (reserved <-> warm <->
    # affectionate) that HARD-CAPS forwardness regardless of stage or
    # the learned signal. The cap is always on (it's a boundary
    # control, not a behaviour toggle): it scales the K15 disclosure
    # budget, gates the J9 reciprocal-vulnerability beat, and renders a
    # register cue. Default 0.7 ("warm") is behaviour-neutral — the cap
    # only bites for an intimate-stage bond.
    intimacy_ceiling: float = 0.7
    # Master switch for the LEARNED half (the user-pace EMA + the
    # "follow him, don't lead" cue). Off leaves the consent dial fully
    # functional; only the learned-pacing behaviour stops.
    intimacy_pacing_enabled: bool = True
    # EMA rate for blending a new per-message / per-reaction forwardness
    # score into the stored user_pace. Higher than the affection-style
    # rate because pacing evidence is sparser (only affectionate /
    # cooling messages move it).
    intimacy_pacing_learning_rate: float = 0.15
    # Half-life (days) of the slow decay of user_pace back toward the
    # neutral 0.5 midpoint. ~14 days: a forward (or cold) stretch fades
    # over a couple of weeks of neutral conversation.
    intimacy_pacing_decay_half_life_days: float = 14.0
    # How hard Aiko follows the user's own pace within the ceiling
    # (0 = ignore the learned signal entirely, 1 = match it fully). The
    # "slightly follow, never lead by much" knob.
    intimacy_pacing_follow_strength: float = 0.5

    # ── K31 + K32: soft physicality (touch + reactions) ───────────────
    # Master switch for the K31 ``[[touch:KIND]]`` tag family. When
    # off, the streaming parser silently drops touch tags before they
    # reach the avatar or the bubble badge; ``TouchService`` is still
    # constructed (so the persisted state survives a settings flap)
    # but ``try_dispatch`` always returns ``dispatched=False,
    # reason="disabled"``.
    touch_enabled: bool = True
    # Per-kind override map, e.g.
    # ``{"hug": {"cooldown_seconds": 300, "daily_cap": 6}}``. Lets
    # users adjust the cadence without code changes; unknown fields
    # or unknown kinds are silently ignored. Falls back to the
    # taxonomy defaults in :data:`app.core.touch.touch_gestures`.
    touch_per_kind_overrides: dict[str, Any] = field(default_factory=dict)

    # ── K10 persona regression (on-demand golden-turn eval) ───────────
    # Master switch for the persona-drift harness. When off,
    # ``run_persona_regression()`` is a no-op returning an empty snapshot
    # and the Diagnostics panel shows a disabled state. Purely on-demand
    # (MCP tool / "Run check" button / pytest); no background spend.
    persona_regression_enabled: bool = True
    # JSONL fixture of canonical "golden turns" to replay. Relative to
    # the working directory; ships beside the persona sheet.
    persona_regression_fixture_path: str = "data/persona/golden_turns.jsonl"

    # ── Brain orchestration: long-running tasks (schema v16) ──────────
    # Master switch for the whole task subsystem. Off disables the
    # ``start_*`` tools, the ``TaskOrchestrator`` rejects spawns, and
    # the cue / escalation paths stay silent. See
    # :mod:`app.core.tasks` and ``docs/brain-orchestration.md``.
    tasks_enabled: bool = True
    # Max concurrent ``running`` + ``awaiting_input`` rows per user.
    # ``TaskOrchestrator.start_task`` rejects with
    # ``reason=per_user_cap`` past this. Tuning up = more parallel
    # tasks per user (and more memory + WS chatter). Tuning down =
    # tighter back-pressure on long-running work.
    tasks_per_user_cap: int = 8
    # When True, non-terminal task rows surviving a restart get
    # surfaced to Aiko as a one-line cue on her next turn ("the X
    # task stopped when we last talked -- want me to retry?"). Off
    # silently demotes interrupted rows without prompting Aiko.
    # Implemented by ``recover_interrupted_tasks`` in
    # ``app/core/tasks/recovery.py``.
    tasks_resume_on_boot: bool = True
    # When True, ``InnerLifeProvidersMixin._render_running_tasks_block``
    # renders a T6 block listing live tasks for the active user. Off
    # hides the block entirely (Aiko has no inner-prompt awareness of
    # her own running work; only the TaskStrip in the UI does).
    tasks_running_block_enabled: bool = True
    # ``BrainLoop`` deferred-event poll interval in milliseconds.
    # Smaller = deferred items retry sooner when the free-to-speak
    # gate clears (lower latency on the no-interrupt invariant), but
    # the consumer thread wakes more often on idle. Clamped to
    # ``[10, 5000]``. Default 100 = a tenth of a second.
    brain_loop_deferred_grace_ms: int = 100
    # Wall-clock age (in seconds) above which a parked cue is
    # silently dropped on the next dequeue / sweep. Protects against
    # awkward stale-context messages ("the YouTube tab I opened 3
    # hours ago is still going") if the user vanished. Clamped to
    # ``[60, 86400]``. Default ``1800`` = 30 minutes.
    task_cue_max_age_seconds: int = 1800
    # Hard cap on cues rendered into a single turn's prompt T6 block.
    # Excess cues stay in the DB / WS strip so the user sees them,
    # but get dropped from the prompt to keep T6 cheap (the most
    # volatile tier, no cache hits). Clamped to ``[1, 20]``.
    task_cue_max_aggregated: int = 5
    # ── Duration-hybrid task replies (fold-fast + reply-on-complete) ──
    # Master switch for the reply-on-complete behaviour. When True the
    # ``start_file_*`` tools fold a fast result into the same turn and
    # flag slower tasks ``reply_when_done`` so their result is rendered
    # in full (not a terse bullet) when it surfaces. Off = legacy
    # behaviour (terse cue only, no inline fast fold).
    task_reply_on_complete_enabled: bool = True
    # How long a ``start_file_read`` / ``start_file_search`` call blocks
    # waiting for the handler to finish so the result can be folded into
    # the SAME reply (the "fast" half of the duration hybrid). Tasks
    # that don't finish in this window fall back to the reply-on-complete
    # path. Clamped to ``[0, 30]``; 0 disables the inline fast path.
    task_inline_grace_seconds: float = 3.0
    # ── C6: worker-model task-report decision ──────────────────────────
    # Master switch for the worker-LLM decision that runs when a
    # reportable background task finishes. Decides surface_now / park /
    # drop and drafts a short "angle" framing hint the chat model uses to
    # compose the report. When False the legacy binary park+arm path runs
    # for every ``notify_aiko=True`` task (behaviour before C6).
    task_report_decision_enabled: bool = True
    # How the decision treats user-requested tasks (the always-report
    # floor). ``shadow`` keeps the hard floor (park+arm immediately) and
    # only logs the verdict the worker WOULD have produced, plus enriches
    # the cue with the drafted angle — use this to evaluate the worker
    # before trusting it. ``enforce`` makes the verdict authoritative for
    # floor tasks too. Unknown values fall back to ``shadow``.
    task_report_decision_floor_mode: str = "shadow"
    # Whether to enrich parked report cues with the worker-drafted angle
    # hint (rendered as a private ``(angle: …)`` suffix in the T6 cue
    # block; the chat model phrases the actual report). Applies to both
    # the shadow-floor and discretionary tiers.
    task_report_angle_enabled: bool = True
    # Configured roots for the read-only filesystem task handlers
    # (``file_search`` / ``file_read``). Each entry is a dict with
    # ``label`` (human-readable id used in path prefixes like
    # ``"Documents:notes.md"``), ``path`` (absolute or relative to
    # the app root), and an optional ``read_only`` flag reserved
    # for phase 2. Empty default = no filesystem access; the
    # handlers run but every resolve returns ``no_match``. Validate
    # at boot via :func:`app.core.tasks.sandbox.validate_roots`;
    # missing / wrong-type roots get a WARNING but stay in the
    # list so a temporarily-unmounted external drive doesn't auto-
    # disappear from the config. See ``docs/brain-orchestration.md``.
    task_file_allowed_roots: tuple[dict[str, Any], ...] = ()
    # When ``False``, the built-in workflow file skills (``file_search`` /
    # ``read_file`` / ``write_file``) are not offered to the planner.
    # Intended for users who handle files exclusively through a filesystem
    # MCP server: removes the built-in-vs-MCP overlap (two path
    # conventions for the same directory) that makes the planner hand a
    # label/relative path to an MCP file tool and get "path outside
    # allowed directories". Default ``True`` (built-ins on).
    builtin_file_skills_enabled: bool = True
    # ── Chunk 12: file_read handler safety caps ────────────────────────
    # ``FileReadHandler`` is the first phase-1 handler that emits a
    # ``TaskInputNeeded`` (multi-root disambiguation: a bare path that
    # matches in more than one configured root). It also opens and
    # reads file contents, so a small set of safety caps gate what
    # actually reaches the LLM as a tool result.
    #
    # ``task_file_read_max_bytes`` — hard cap on bytes read off disk
    # per call. Files larger than this are truncated at the byte
    # boundary and the result row sets ``truncated=True``. Default
    # 256 KiB — big enough for a Markdown doc, small enough that a
    # rogue 4 GB log can't OOM Aiko's process.
    task_file_read_max_bytes: int = 262144
    # ``task_file_read_max_lines`` — secondary cap applied after the
    # byte read so a 256 KiB single-line minified blob can still be
    # rejected. Default 2000 lines.
    task_file_read_max_lines: int = 2000
    # ``task_file_read_allowed_extensions`` — case-insensitive
    # extension allow-list. Empty tuple = "allow everything that
    # passes the magic-byte text check". When non-empty, anything
    # outside the list is rejected up-front (the magic-byte check
    # still runs as a secondary filter). Defaults to a sensible
    # text-only catalogue so the LLM can't accidentally read a PDF
    # or a database file.
    task_file_read_allowed_extensions: tuple[str, ...] = (
        ".txt", ".md", ".rst", ".log",
        ".py", ".js", ".ts", ".tsx", ".jsx",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
        ".html", ".css", ".xml",
        ".csv", ".tsv",
        ".sh", ".bat", ".ps1",
        ".sql",
        ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".java", ".kt",
        ".rb", ".lua",
    )
    # ── External MCP-server clients ────────────────────────────────────
    # Master switch for connecting to the external MCP servers configured
    # under ``mcp_clients.servers``. When off (or no servers configured),
    # the manager never starts and no MCP tools are registered. MCP tools
    # are surfaced only to the background-worker (workflow planner) lane,
    # so this is only meaningful when ``workflow_enabled`` is also true.
    mcp_clients_enabled: bool = True
    # ── Nested goal workflows ──────────────────────────────────────────
    # Master switch for the ``GoalWorkflowHandler`` + ``start_workflow``
    # brain tool. When off, ``start_workflow`` is not registered and the
    # workflow handler is never built; the fast-lane file tools still work.
    workflow_enabled: bool = True
    # Hard cap on planner iterations (plan->act->observe cycles) before a
    # workflow force-finishes. Bounds runaway loops. Clamped ``[1, 30]``.
    workflow_max_iterations: int = 6
    # Hard cap on child tasks a single workflow may spawn. Clamped
    # ``[1, 50]``.
    workflow_max_children: int = 8
    # Max number of workflows that may run concurrently per user.
    # ``start_workflow`` refuses past this. Clamped ``[1, 8]``.
    workflow_max_concurrent: int = 2
    # Char budget for the planner blackboard (short observations folded
    # back each iteration). Clamped ``[500, 20000]``.
    workflow_planner_history_budget_chars: int = 4000
    # Separate, larger char budget for the final aggregated reply
    # (fuller child content, not the 200-char observations). Clamped
    # ``[1000, 40000]``.
    workflow_reply_budget_chars: int = 6000
    # Max seconds the planner waits on a single child task to reach a
    # terminal state before treating it as timed out. Clamped ``[5, 600]``.
    workflow_child_wait_timeout_seconds: int = 120
    # ``num_predict`` for the planner's JSON decision call. Small — the
    # planner only emits a tiny ``{action, args, reason}`` object.
    # Clamped ``[64, 2048]``.
    workflow_planner_max_tokens: int = 512
    # Circuit breaker: stop a workflow after this many child steps fail /
    # time out *in a row* (a success resets the counter). Catches the
    # "service unavailable" loop the exact-(skill,args) repeat guard
    # misses — e.g. a browser workflow when Chrome / the extension isn't
    # running, where every varied call fails. Clamped ``[1, 20]``.
    workflow_max_consecutive_failures: int = 2
    # Wall-clock budget for a whole workflow loop, in seconds. The loop
    # force-finishes (partial) once exceeded so piled-up slow timeouts
    # can't run for many minutes. ``0`` disables. Clamped ``[0, 3600]``.
    workflow_max_wall_seconds: int = 300
    # Cap on the per-task capability-gap log (missing_capability entries).
    # Clamped ``[1, 500]``.
    workflow_capability_gap_log_max: int = 50
    # ── Task approvals (reusable across destructive capabilities) ───────
    # Generic approval policy shared by every destructive task
    # capability (today: ``file_write``; later: shell exec / http post /
    # send email). ``task_approval_mode`` is the global default —
    # ``"ask"`` gates every destructive action behind a TaskStrip
    # approval prompt, ``"auto"`` performs without asking. Per-capability
    # overrides live in ``task_approval_overrides`` (e.g.
    # ``{"file_write": "auto"}`` to stop asking for writes only). A
    # session "approve all" click (handled in-memory by the controller)
    # rides on top of both — it never persists. See
    # :mod:`app.core.tasks.approval` + ``docs/task-approvals.md``.
    task_approval_mode: str = "ask"
    task_approval_overrides: dict[str, str] = field(default_factory=dict)
    # ── file_write capability resource config ───────────────────────────
    # Nested per-capability block (master switch + byte cap + extension
    # allow-list). The destructive-write APPROVAL is governed by the
    # generic ``task_approval_*`` fields above, not here.
    file_write: FileWriteSettings = field(default_factory=FileWriteSettings)
    # ── vision (describe_image) capability resource config ───────────────
    # Reuses the worker model; ``model`` empty = inherit the effective
    # worker model. Master switch gates the describe_image workflow skill.
    vision: VisionSettings = field(default_factory=VisionSettings)
    # ── Worker-LLM priority gate ────────────────────────────────────────
    # Master switch for the priority gate in front of the shared worker
    # Ollama client. Off = pass-through proxies (zero behaviour change).
    worker_llm_gate_enabled: bool = True
    # Concurrency bound on the worker model. Default 1 (a 30B on one GPU
    # serialises anyway). Clamped ``[1, 8]``.
    worker_llm_max_concurrency: int = 1
    # Optional per-consumer tier overrides: maps the proxy name
    # (``"conversation"`` / ``"maintenance"`` / ``"task"``) to a tier
    # name, letting any consumer be nudged up/down without code.
    worker_llm_priority_overrides: dict[str, str] = field(default_factory=dict)
    # Master switch for the K32 user-reaction tray. When off, the
    # REST endpoints reject with 503 and the inner-life cue stays
    # silent. The frontend hides the hover tray when the connection
    # advertises the feature as disabled.
    user_reactions_enabled: bool = True
    # When True, every K32 reaction click also bumps relationship
    # axes via :meth:`RelationshipAxesUpdater.apply_user_reaction`.
    # Off lets you keep the cue + persistence without moving the
    # axes (useful for debugging or for users who don't want the
    # relationship signal to ride on a UI affordance).
    user_reactions_axes_enabled: bool = True
    # Cumulative absolute axis-movement cap per axis per UTC day,
    # from reactions only. Tuned so 4-5 reactions in a session feels
    # meaningful without grinding closeness to +1 from clicks alone.
    # Implementation in
    # :func:`app.core.relationship.user_reactions.apply_daily_cap`.
    user_reactions_daily_axis_cap: float = 0.15
    # Master switch for the persona-mode action banner (the small
    # transient surface near the avatar in the Tauri overlay window
    # that shows what Aiko just did + the reaction tray). Off hides
    # the banner entirely in the persona webview; the underlying
    # avatar animation still plays.
    persona_touch_banner_enabled: bool = True
    # Visible duration (seconds) of the persona banner. Clamped to
    # ``[1, 120]`` in ``_parse_agent`` so a typo can't pin the
    # banner permanently. Default 20s -- long enough for a glance
    # + a reaction click, short enough not to clutter the overlay.
    persona_touch_banner_duration_seconds: int = 20
    # Chunk 15 (brain orchestration): master switch for the
    # ``PersonaTaskBanner`` -- the persona-window mirror of the
    # ``TaskStrip`` chip in the main chat. Surfaces an
    # ``awaiting_input`` task as a transient pill near the avatar
    # so the user can click an option (or type a free-text answer)
    # without switching back to the chat window. The banner never
    # cancels the underlying task on dismiss; it only hides the
    # surface so the chat-channel answer path still works. Off
    # hides the banner entirely; the strip in the chat window is
    # unaffected.
    persona_task_banner_enabled: bool = True

    # ── Brain orchestration phase 2 (schema v17): lifecycle safety ────
    # Sweep interval for the in-process heartbeat zombie detector
    # (:class:`HeartbeatChecker`). The detector wakes every N seconds,
    # asks the task store for ``status='running'`` rows whose
    # ``heartbeat_at`` is older than :attr:`task_stalled_seconds`, and
    # either logs a WARNING or moves them to ``failed`` depending on
    # :attr:`task_stalled_action`. Clamped to ``[5, 3600]`` in
    # :func:`_parse_agent` so a typo can't either spin the CPU or
    # silently disable the sweep.
    task_heartbeat_check_interval_seconds: int = 30
    # Wall-clock age above which a ``running`` row is considered
    # stalled. The orchestrator bumps ``heartbeat_at`` on every emit
    # so a healthy handler comfortably stays under this threshold.
    # Tune up for long-running, low-emit handlers (e.g. a research
    # task that spends 10 minutes inside one network call); tune down
    # for the agent-y workloads where 5-minute silence is itself a
    # failure signal. Clamped to ``[60, 86400]``.
    task_stalled_seconds: int = 300
    # What :class:`HeartbeatChecker` does with stalled rows. ``"warn"``
    # logs a WARNING + appends an ``EVENT_HEARTBEAT_STALLED`` event
    # but leaves the row running; ``"fail"`` additionally promotes
    # the row to ``failed`` with a "stalled" error. Default is the
    # conservative ``"warn"`` so an aggressive threshold can't kill
    # legitimate slow handlers. See
    # :class:`app.core.tasks.task_heartbeat.HeartbeatChecker`.
    task_stalled_action: str = "warn"
    # Cascade-cancel toggle. When True (the default),
    # :meth:`TaskOrchestrator.cancel` recursively cancels every
    # active child in the task tree. Off keeps the legacy phase-1
    # behaviour (cancel only the named row; children keep running
    # until they emit a terminal outcome themselves).
    task_cascade_cancel_children: bool = True
    # Wall-clock retention window for terminal task rows. The
    # :class:`TaskCleanupWorker` deletes terminal rows whose
    # ``completed_at`` is older than this. Cascade-deletes the
    # associated event log + input history. Clamped to
    # ``[1, 3650]`` so the cleanup never accidentally targets
    # rows that just finished, and never proposes "retain forever".
    task_cleanup_retention_days: int = 30
    # How often the cleanup worker runs (idle scheduler tick gating
    # applies on top). Default 6h. Clamped to ``[600, 604800]``
    # (10 minutes to a week).
    task_cleanup_interval_seconds: int = 21600

    # ── K29: opinion injection (push back when she has a stance) ──────
    # Master switch for the per-turn detector that fires a one-line
    # cue when {user_name}'s latest message contradicts one of Aiko's
    # stored ``kind="self"`` stance memories. The whole feature exists
    # to make the persona's "have opinions, disagree when you
    # disagree" claim actually fire against LLM RLHF agreeability --
    # without flipping into contrarianism.
    #
    # Anti-contrarianism is layered: only opinion-shaped stance
    # memories qualify (predicate filter), only ``definite`` heuristic
    # verdicts and (when budget allows) borderline+LLM-YES verdicts
    # fire, and a hard per-session cap bounds the worst case. See
    # [`app/core/affect/opinion_injection_detector.py`](../affect/opinion_injection_detector.py).
    #
    # ``require_definite=True`` is the strictest no-LLM-cost
    # configuration (Path C in the design plan); leave at ``False``
    # (Path B, the default) for the heuristic + LLM-gated borderline
    # behaviour.
    opinion_injection_enabled: bool = True
    opinion_injection_require_definite: bool = False

    # ── K46: stance persistence ───────────────────────────────────────
    # Master switch for the "don't cave on taste pushback" cue. When on
    # and Aiko has recently stated a taste/opinion (a K29 cue fired in
    # the last ``memory.stance_persistence_window`` turns), a *mild*
    # pushback from the user surfaces a "hold your take" cue and shields
    # the K20 calibration from a factual-trust hit (a taste disagreement
    # shouldn't teach Aiko her facts are suspect). A *strong* correction
    # is left to K20 untouched. Off → neither the cue nor the shield run.
    stance_persistence_enabled: bool = True

    # ── K63: long-arc callbacks ───────────────────────────────────────
    # Master switch for the rare "weeks ago you said…" reach. When on
    # (default) the inner-life provider may, on an eligible turn (past its
    # per-session cap + wall-clock cooldown), surface an old, topically-
    # linked memory as a tentative callback cue. The age / cosine / cap /
    # cooldown knobs live under ``memory.long_arc_callback_*``. Off → the
    # provider never runs (no embed, no search).
    long_arc_callback_enabled: bool = True

    # ── K28: "What I've been turning over" ────────────────────────────
    # Master switch for the between-session reflection-surfacing cue.
    # Off → no turning-over block ever lands in the prompt. On (default)
    # → the post-turn pipeline arms ``_pending_turning_over_seconds``
    # whenever a typed turn lands after a gap of at least
    # ``memory.turning_over_min_gap_minutes`` (default 90 min), and the
    # next prompt assembly runs the picker
    # (:mod:`app.core.session.inner_life.turning_over`). The picker is
    # silent when no recent ``reflection`` memory clears the topical
    # match, so the cue stays rare even with the switch on. See
    # [`app/core/session/inner_life/turning_over.py`](../session/inner_life/turning_over.py).
    turning_over_enabled: bool = True

    # ── K36: "things I did while you were away" ───────────────────────
    # Master switch for the idle-activity producer + its surfacing cue.
    # Off → the IdleAwayActivityWorker never registers and the
    # away-activities prompt block never lands. On (default) → the worker
    # gives Aiko a small autonomous room life during quiet windows
    # (sip the tea, read a book, move the cat, …) and the first turn
    # after a long typed gap may surface one casual line about it. The
    # cadence + gap knobs live on ``MemorySettings.away_activities_*``.
    away_activities_enabled: bool = True

    # H21: sleep & overnight rhythm. Off → the sleep-return cue never
    # lands. On (default) → the first turn after a long typed gap that
    # plausibly spanned an overnight sleep may surface one casual line
    # about having dozed off (optionally weaving in a recent ``[dream]``
    # reflection so the dream has a behavioural home). The gap + dream
    # lookback knobs live on ``MemorySettings.sleep_return_*``.
    sleep_return_enabled: bool = True

    # ── Intentional-placement hold ────────────────────────────────────
    # When the brain (move_to / change_posture tools) or the user (World
    # tab) intentionally sets Aiko's location / posture / activity, a
    # watermark is stamped (``world.intentional_state_at``). For this many
    # seconds afterwards the autonomous movers — the away-activity worker's
    # location beats, the garden visit worker, and the circadian
    # "where you find her" default — DEFER and leave her where she chose
    # to be. So if she decides mid-conversation to stay in the garden, no
    # worker drags her back to the desk. Default 2h; 0 disables the hold
    # (workers always free to move her). Posture/activity-only beats that
    # don't relocate her are unaffected.
    world_intentional_hold_seconds: float = 7200.0

    # ── H16: circadian "where you find her" default ───────────────────
    # Master switch for the :class:`CircadianSettleWorker` — the gentlest
    # mover, which drifts Aiko to a believable time-of-day resting spot
    # (bed at night, desk mid-morning, beanbag late afternoon) but ONLY
    # after her room state has been static for a while and never over a
    # deliberate placement or a garden visit. Cadence knobs live on
    # ``MemorySettings.circadian_settle_*``.
    circadian_settle_enabled: bool = True

    # ── H17: idle beats feed the idea machine ─────────────────────────
    # When on (default), an idle away-beat occasionally produces a small
    # forward-looking "seed" (a thought sparked by what she was doing),
    # surfaced ONCE via a watermark-gated inner-life cue so Aiko phrases
    # "while I was reading earlier I started wondering ..." herself. Off →
    # beats stay purely cosmetic. Cadence on ``MemorySettings.idle_seed_*``.
    idle_seed_enabled: bool = True

    # ── H19: hobbies & ongoing personal projects ──────────────────────
    # Master switch for the :class:`HobbyWorker` — a single multi-day
    # "current hobby" (reading a series, learning guitar, …) that
    # progresses across days, occasionally yields a takeaway seed (via the
    # H17 cue), and rotates when it's run long enough. The standing "what
    # she's been up to lately" line is rendered by ``_render_hobby_block``.
    # Cadence on ``MemorySettings.hobby_*``.
    hobby_worker_enabled: bool = True

    # ── H20: a room that evolves (depleting + accruing micro-state) ────
    # Master switch for the :class:`RoomEvolutionWorker` — a slow pass that
    # drifts the seeded room items so the space accrues a history (tea pot
    # empties + gets a fresh flavour, cookies refill, the book gains
    # progress and flips to a new one on finishing). Cadence on
    # ``MemorySettings.room_evolution_*``.
    room_evolution_enabled: bool = True

    # ── H15: needs-driven, richer garden + outdoor life ───────────────
    # Master switch for the :class:`GardenVisitWorker`. Off → Aiko never
    # autonomously wanders out to tend the garden (the manual world tools
    # still work). On (default) → she visits on a need-driven trigger
    # (drought-stressed or ripe plants pull a visit forward) with a varied
    # visit (jittered duration, occasional "sit outside" relax beat) and
    # leaves a trace in the away-activities journal so she can mention it.
    # Cadence + need knobs on ``MemorySettings.garden_*``.
    garden_visits_enabled: bool = True

    # ── H22: light outings ("I stepped out for a bit") ────────────────
    # Master switch for the rare ``outing`` away-beat. Off → she never
    # narrates a short trip out. On (default) → during daylight quiet
    # windows, paced by its own long cooldown + small daily cap, an idle
    # beat may narrate a brief trip out and back (and feed H17 with a small
    # detail she brought home). The v0 of H5. Cadence on
    # ``MemorySettings.outing_*``.
    outings_enabled: bool = True

    # ── H9: away-diary worker ─────────────────────────────────────────
    # Master switch for the :class:`DiaryWorker` — Aiko's idle journal.
    # Off → the worker never registers. On (default) → during quiet
    # windows with NO UI client connected, Aiko reflects on the recent
    # conversation and writes one short ``diary`` memory (surfaced in the
    # Diary tab). While a window is open the live ``[[diary:...]]`` tag
    # owns the channel instead, so the two never double-write. Cadence
    # knobs live on ``MemorySettings.diary_worker_*``.
    diary_worker_enabled: bool = True

    # ── K34: "forward curiosity" ──────────────────────────────────────
    # Master switch for the forward-question producer + its surfacing
    # cue. Off → the ForwardCuriosityWorker never registers and the
    # forward-curiosity prompt block never lands. On (default) → during
    # quiet windows Aiko drafts a genuine "I've been wondering ..."
    # question about the user's life (from their future_plan / callback
    # memories, biased by K3 routines) and the first turn after a long
    # typed gap may surface one. Cadence + gap knobs live on
    # ``MemorySettings.forward_curiosity_*``.
    forward_curiosity_enabled: bool = True

    # FollowUpWorker master switch. When a user-mentioned future_plan's
    # event time passes, the worker drafts a private "you can ask how it
    # went" cue into the ``aiko.follow_up_cues`` kv ring and the
    # ``_render_follow_up_block`` provider surfaces it on the next turn.
    # Off = no proactive follow-up cue (the retrieval-tag path still
    # lets Aiko ask retrospectively when the memory surfaces).
    follow_up_enabled: bool = True

    # ── K70: longitudinal growth witness ──────────────────────────────
    # Master switch for the rare "you've grown since we met" beat. When
    # ON, a slow idle worker compares an older baseline window of the H3
    # mood-drift daily ring against a recent window and, only when a real
    # durable POSITIVE shift clears a high bar, drafts one private cue
    # into ``aiko.growth_witness``; ``_render_growth_witness_block``
    # surfaces it on a later turn so Aiko reflects it back in her own
    # words. Depends on H3 sampling (``mood_drift_enabled``) for its
    # data — with no ring it silently no-ops. Cadence + cooldown below;
    # detection thresholds live on ``MemorySettings.growth_witness_*``.
    # Off → the provider stays empty.
    growth_witness_enabled: bool = True
    # How often the worker checks for a durable shift during quiet
    # windows (default every 6h; clamped to >= 60s).
    growth_witness_check_interval_seconds: int = 21600
    # Wall-clock cooldown between drafted cues. Deliberately multi-week so
    # a growth observation lands as genuine insight, not flattery on a
    # loop. A *different* finding still has to clear the signature gate.
    growth_witness_cooldown_days: float = 14.0

    # ── K71: self-callback (her own continuity over time) ─────────────
    # Master switch for the symmetric self-side of K63. When ON, a slow
    # idle worker mines Aiko's own aged ``self`` / ``reflection`` memories
    # for a past feeling / stated intention worth revisiting and drafts
    # one private cue into ``aiko.self_callback``; the provider surfaces
    # it on a later turn so Aiko closes the loop in her own words ("a
    # while back I told you I'd been restless -- that's eased now"). The
    # resolution read is left to the model. Cadence + cooldown below;
    # age floor on ``MemorySettings.self_callback_min_age_days``. Off →
    # the provider stays empty.
    self_callback_enabled: bool = True
    # How often the worker checks during quiet windows (default 6h;
    # clamped to >= 60s).
    self_callback_check_interval_seconds: int = 21600
    # Wall-clock cooldown between drafted cues (per-memory signature
    # de-dup is structural, so this just paces *how often* she circles
    # back on herself at all).
    self_callback_cooldown_days: float = 10.0
    # Use the worker model to select + classify the candidate (more robust
    # than the regex feeling/intention prefilter; rejects biographical
    # facts the regex false-positives). Falls back to the pure heuristic
    # when off or no worker client. ~monthly cadence -> negligible cost.
    self_callback_llm_enabled: bool = True

    # ── K43: promise follow-through ───────────────────────────────────
    # Master switch for the promise lifecycle + follow-through cue. When
    # ON, assistant-side ``kind="promise"`` memories carry an
    # open → surfaced → fulfilled | dropped state machine: the
    # PromiseFollowthroughWorker arms a one-shot "you said you'd look
    # into X — close the loop (or own that you haven't)" cue during
    # quiet windows, the post-turn hook auto-fulfils promises Aiko's
    # reply delivered on, and finished background tasks auto-fulfil
    # matching promises. Off → no cue, no lifecycle writes. Cadence +
    # age knobs live on ``MemorySettings.promise_followthrough_*``.
    promise_followthrough_enabled: bool = True

    # ── K38: self-correction cue ──────────────────────────────────────
    # Master switch for the next-turn self-correction cue. When ON, a
    # post-turn lexical detector checks whether Aiko's just-finished
    # reply contradicted one of her own high-confidence fact/preference
    # memories and, if so, arms a one-shot cue so she owns the slip on
    # her next turn. Thresholds + cooldown live on
    # ``MemorySettings.self_correction_*``.
    self_correction_enabled: bool = True

    # ── K25: memory confidence time-decay ─────────────────────────────
    # Master switch for the ``(distant)`` suffix the RAG retriever
    # stamps on age-decayed memory rows. The three numeric knobs that
    # govern the decay formula and threshold live on
    # :class:`MemorySettings` (``confidence_decay_horizon_days``,
    # ``confidence_decay_floor``, ``confidence_decay_distant_threshold``)
    # because they describe a memory-store concept; only the on/off
    # gate lives here so it sits alongside the rest of the per-feature
    # master switches. Flipping ``False`` disables the ``(distant)``
    # suffix entirely — ``_confidence_penalty`` still reads stored
    # confidence for the score offset, K7 ``(faded)`` still fires,
    # ``(uncertain)`` still fires.
    confidence_time_decay_enabled: bool = True

    # ── K22: callback / inside-joke detector ──────────────────────────
    # Master switch for the post-turn cosine pass that detects when
    # Aiko's reply semantically reaches back to an older eligible
    # memory and stamps ``metadata.callback_count``. Off → no rows
    # gain new callback stamps. The retriever's read-side bonus on
    # rows already stamped stays on either way, so flipping this off
    # freezes the loop without losing earned weight. Knob detail
    # lives on :class:`MemorySettings` (``callback_*`` fields). See
    # [`app/core/conversation/callback_detector.py`](callback_detector.py).
    callback_detector_enabled: bool = True

    # ── K20: metacognitive calibration detector ────────────────────────
    # Master switch for the post-turn classifier that detects
    # Jacob's calibration signal toward Aiko's claims (pushback /
    # softening / affirmation) and writes per-user
    # CalibrationState. Off → no new calibration updates; the
    # inner-life provider also goes silent because
    # ``_render_calibration_block`` short-circuits on this flag. Knob
    # detail lives on :class:`MemorySettings` (``calibration_*``
    # fields). See [`app/core/affect/calibration_detector.py`](calibration_detector.py).
    calibration_detection_enabled: bool = True

    # ── K24: sensory anchoring layer ──────────────────────────────────
    # Master switch for the adaptive per-arc cadence that
    # occasionally surfaces a "small physical beat available" cue.
    # Off → ``_render_sensory_anchor_block`` short-circuits and no
    # beats are ever offered to Aiko. Knob detail lives on
    # :class:`MemorySettings` (``sensory_anchor_*`` fields). See
    # [`app/core/conversation/sensory_anchor.py`](sensory_anchor.py).
    sensory_anchor_enabled: bool = True

    # ── Resume opener (Phase 2a) ──────────────────────────────────────
    # When the time since the last assistant turn exceeds this many
    # hours, controller bootstrap schedules a one-shot NarrativeWeaver
    # pass that primes a "welcome back" line into PreparedNudgeStore.
    # ProactiveDirector consumes it on first silence; on the typed path
    # the prompt assembler folds it into the system block so the LLM
    # opens naturally. Set to 0 to disable the opener entirely.
    resume_opener_min_hours: float = 4.0
    # TTL applied to the resume nudge so it survives until the user
    # actually starts a session — longer than the speaking-window TTL.
    resume_opener_ttl_seconds: float = 1800.0  # 30 min

    # ── Dream worker (Phase 2b) ───────────────────────────────────────
    # Bootstrap-time reflection that fires once per app start when the
    # gap since the last assistant turn exceeds this threshold. Writes
    # a salience-boosted ``reflection`` memory tagged ``[dream]`` so the
    # resume opener can prefer it. Set ``enabled=false`` to disable.
    dream_worker_enabled: bool = True
    dream_worker_min_hours_since_last: float = 6.0
    # K65e: ground the dream in the day's hot K9 cluster. When on (default)
    # the dream seed gains a "threads that kept coming up lately: …" line of
    # the most recently-active established clusters (within
    # ``dream_hot_cluster_recency_days``) so "I kept turning over your X"
    # lands on a real, recent topic instead of generic summary material.
    # Off → the dream seeds from summary + callbacks + self memories only.
    # No effect on a cold / non-persistent graph.
    dream_hot_cluster_enabled: bool = True
    # A cluster counts as part of "the day's" activity when its newest
    # member is no older than this many days. Keeps the dream anchored to
    # genuinely recent threads, not a months-old interest.
    dream_hot_cluster_recency_days: float = 3.0

    # ── Catchphrase miner (Phase 2c) ──────────────────────────────────
    # Walks the recent history and promotes 3-7-word phrases that recur
    # ≥ N times across both user and assistant turns. Surfaced through
    # the "Aiko's running jokes with <user>:" inner-life block.
    catchphrase_miner_enabled: bool = True
    catchphrase_miner_min_seconds_between: float = 600.0
    catchphrase_miner_min_new_user_turns: int = 6
    catchphrase_miner_min_total_count: int = 3
    # Phase 4c: CuriosityWorker — emits a one-line "next-turn"
    # follow-up question when the recent conversation has gone shallow.
    curiosity_worker_enabled: bool = True
    curiosity_worker_min_turns_between: int = 3
    curiosity_worker_min_seconds_between: float = 60.0
    curiosity_worker_max_user_word_count: int = 8
    # K65c: cluster-aware re-anchor. When on (default) the worker, on an
    # eligible shallow/idle turn, anchors its follow-up on a *known-but-
    # quiet* K9 interest (an established cluster the user hasn't touched in
    # a while) instead of echoing their literal last words. Falls back to
    # the legacy literal-words prompt when no quiet interest is available
    # (cold / non-persistent graph). Off → pure legacy behaviour.
    curiosity_worker_cluster_anchor_enabled: bool = True
    # A cluster counts as "quiet" once its newest member is at least this
    # many days old. Higher → only reach back to long-dormant interests.
    curiosity_worker_quiet_days: float = 7.0
    # ── F2.1 personality backlog: knowledge-gap memory-match resolver ─
    # Companion to F1's web-search resolver. F1 closes a gap by going
    # to look the answer up; this worker closes it by noticing the
    # answer is already in the memory store (e.g. a ``preference`` row
    # written by the post-summary extractor after the user answered the
    # question in chat). Without this the same gap re-injects into the
    # prompt every session for weeks because nothing else marks it
    # resolved. See :class:`app.core.conversation.idle_gap_resolver.IdleGapResolver`.
    gap_resolver_enabled: bool = True
    # Cadence in seconds. The work is pure cosine over the in-memory
    # mirror, so it's cheap; 10 minutes is a "show up shortly after a
    # gap was minted" cadence without spamming logs on quiet stretches.
    gap_resolver_interval_seconds: int = 600
    # Cosine threshold for "this memory answers this gap." Slightly
    # stricter than the curiosity-seed resolve threshold (0.50) because
    # closing a gap is a stronger claim than consuming a seed: a false
    # positive here means a real open question gets buried, where a
    # seed false positive just means we skip a topic that came up once.
    gap_resolver_threshold: float = 0.55
    # Max gaps the worker resolves per tick. The journal cap is 20 and
    # the typical steady state is a handful of opens, so 5 per tick
    # drains a normal backlog within minutes without spiking CPU.
    gap_resolver_per_tick: int = 5
    # Cosine threshold for the post-turn user-answer resolver in
    # :meth:`PostTurnMixin._resolve_knowledge_gaps`. Mirrors the
    # ``curiosity_seed_resolve_threshold`` shape: the same combined
    # ``user_text + assistant_text`` embedding is reused, and any open
    # gap scoring at-or-above this is closed with
    # ``resolved_by="user_answer"`` in metadata. Lower than the worker
    # threshold because the post-turn check has stronger context (the
    # user *just* spoke about the topic) so false positives are rarer.
    gap_user_answer_resolve_threshold: float = 0.50


