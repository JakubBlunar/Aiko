from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class MemorySettings:
    """Long-term memory: cross-session vector store of durable facts.

    Populated by background extraction after each summary, plus any
    ``[[remember:...]]`` tags Aiko emits inline.

    Schema v8 added tiered memory: ``scratchpad`` (fast decay, gets
    promoted to ``long_term`` when used or revived; deleted if never
    used), ``long_term`` (the default home), ``archive`` (decay ~ 0).
    The ``MemoryPromotionWorker`` shuffles rows between tiers on a
    configurable cadence; the ``MemoryDecayWorker`` applies
    wall-clock-driven decay so an intermittently-running desktop app
    still applies the right amount of decay on resume.
    """

    enabled: bool = True
    top_k: int = 6
    score_threshold: float = 0.4
    max_memories: int = 5000  # long_term cap
    dedupe_threshold: float = 0.92
    # The narrower second dedupe gate: a fact restated within
    # ``restate_window_hours`` at this similarity is the same fact again,
    # not a new one. Lower than ``dedupe_threshold`` on purpose, and only
    # safe because the window and a matching kind / temporal type come
    # with it. Set the window to 0 to disable.
    restate_threshold: float = 0.85
    restate_window_hours: float = 6.0
    extractor_enabled: bool = True
    self_tagged_salience: float = 0.7

    # ── Schema v8: tier + decay + revival ────────────────────────────
    tiers_enabled: bool = True
    # Per-tier salience decay per day (applied proportionally to
    # elapsed wall-clock time -- running every hour applies 1/24 per
    # call). ``archive`` defaults to 0 so cold history doesn't fade.
    decay_rate_scratchpad: float = 0.05
    decay_rate_long_term: float = 0.02
    decay_rate_archive: float = 0.0
    # Revival mechanic. When Aiko's reply mentions enough keywords from
    # a surfaced memory, ``revival_score`` is bumped by
    # ``revival_per_hit``. Each decay tick applies a small rebate
    # proportional to revival_score (``revival_coefficient * elapsed``)
    # and then walks revival_score itself back down by
    # ``revival_decay_per_day * elapsed``. ``min_word_overlap`` controls
    # how strict the citation detection is.
    revival_coefficient: float = 0.05
    revival_per_hit: float = 0.15
    revival_decay_per_day: float = 0.02
    revival_min_word_overlap: int = 3
    # F12 -- semantic revival. The keyword test above only credits
    # memories Aiko happens to *quote*, and since paraphrasing is the
    # whole reason to hand a memory to a language model, nearly all of its
    # errors are misses. When the lexical test misses, fall back to cosine
    # between the reply and the stored memory (both vectors already in
    # hand -- no extra embed call).
    #
    # The floor is high and the credit is small on purpose. Surfaced
    # memories were selected for topical similarity to the turn in the
    # first place, and the reply is about that same turn, so cosine here
    # partly measures "was on topic" rather than "she used it". Treating a
    # semantic hit as equal to a quote would let topical coincidence earn
    # full retention credit. ``semantic_revival_per_hit`` is deliberately
    # below ``scratchpad_ttl_min_revival`` so one semantic hit earns
    # salience and progress toward promotion WITHOUT rescuing a memory
    # from scratchpad TTL cleanup; two do. The raw cosine is recorded in
    # the L37 ledger so the floor can be re-derived from real
    # distributions rather than left at this guess.
    semantic_revival_enabled: bool = True
    semantic_revival_min_cosine: float = 0.62
    semantic_revival_per_hit: float = 0.05
    # Promotion / demotion / cleanup gates used by
    # :class:`MemoryPromotionWorker`.
    scratchpad_ttl_days: int = 14
    # Minimum ``revival_score`` that spares an unused scratchpad memory
    # from TTL deletion. This replaced an exact ``revival_score == 0.0``
    # test, which was both brittle (float equality against a value that
    # decays back down) and, once F12's semantic fallback existed, far too
    # generous: any trace of topical similarity would have exempted a
    # memory from cleanup forever, quietly turning scratchpad TTL off.
    # Sits above ``semantic_revival_per_hit`` and at or below
    # ``revival_per_hit``, so a quoted memory is rescued exactly as before
    # and a merely-on-topic one is not.
    scratchpad_ttl_min_revival: float = 0.10
    scratchpad_promote_min_age_days: int = 7
    scratchpad_promote_min_use_count: int = 3
    scratchpad_promote_min_revival: float = 0.3
    archive_demote_idle_days: int = 180
    # Per-tier caps (long_term cap reuses ``max_memories`` above).
    scratchpad_cap: int = 1000
    archive_cap: int = 10000
    # Safety clamp on wall-clock catch-up: even if the app was offline
    # for months, decay won't try to apply more than this many days'
    # worth at once. Keeps the per-call magnitude bounded.
    decay_max_catchup_days: float = 30.0
    # ── K7 personality backlog: forgetting protocol ───────────────────
    # Master switch for the ``(faded)`` suffix appended by
    # :func:`app.core.rag.rag_retriever._is_faded_memory`. Flipping ``False``
    # disables every fade hedge — including the archive-tier suffix that
    # was the original K7 implementation — so users who'd rather Aiko
    # speak from memory without ever hedging "I think you said this
    # once, ages ago…" get a single clean kill switch. Default ON
    # because the persona rule already gates the hedge on "only when
    # the memory is actually load-bearing for your reply", so the
    # cosmetic cost of leaving it on is small.
    fade_hedge_enabled: bool = True
    # Salience floor for a long_term row to register as faded. Together
    # with ``faded_idle_days`` below, this picks up the
    # "decayed-in-place" window between freshly written and demoted-to-
    # archive. With the long_term decay rate of 0.02/day a fresh
    # salience-0.5 row hits the 0.20 threshold around day 15; combined
    # with the 30-day idle floor, only rows that genuinely haven't
    # surfaced in over a month qualify. Higher → only the very faded
    # rows hedge; lower → more aggressive hedging on lukewarm memories.
    # Archive-tier rows ignore this threshold and always fade (when
    # ``fade_hedge_enabled`` is on).
    faded_salience_threshold: float = 0.20
    # Minimum days since ``last_used_at`` (or ``created_at`` if a row
    # has never been touched) before a low-salience long_term row picks
    # up the ``(faded)`` suffix. The strict ``>`` semantics means a row
    # idle for exactly 30 days does NOT fade — that one-day buffer
    # prevents a row Aiko mentioned a month ago to the day from
    # flipping to hedged on the anniversary. Higher → only very stale
    # rows fade; lower → more aggressive hedging.
    faded_idle_days: int = 30
    # ── K25: memory confidence time-decay ─────────────────────────────
    # Read-side time-decay on memory confidence. Pure derived value at
    # ``format_block`` time — no schema change, no decay-writer. Each
    # retrieval recomputes ``effective_confidence = stored * max(floor,
    # 1 - days_since_created / horizon_days)``. Pinned rows bypass
    # (return stored as-is) since a pin reads as "the user explicitly
    # trusts this row". When ``effective_confidence`` falls below
    # ``confidence_decay_distant_threshold``, the retriever stamps the
    # row with ``(distant)`` — a third suffix distinct from
    # ``(uncertain)`` (low stored value) and ``(faded)`` (K7 tier +
    # idle). The persona maps each tag to a different verbal hedge:
    # ``(distant)`` → "a while back", "don't quote me" (time-flavoured),
    # ``(uncertain)`` → "I think", "if I'm remembering right"
    # (source-doubt), ``(faded)`` → "ages ago", "I might be wrong"
    # (cold-history). See
    # [`app/core/rag/rag_retriever.py`](../rag/rag_retriever.py)
    # ``_is_distant_memory``. Master switch lives on
    # :class:`AgentSettings` as ``confidence_time_decay_enabled``.
    #
    # Tuning rules:
    # * ``horizon_days`` — days at which the multiplier reaches
    #   ``floor``. Higher → slower decay, the hedge fires later in a
    #   memory's life.
    # * ``floor`` — minimum decay multiplier. Below ~0.1 the floor
    #   stops mattering (an old row's effective value is already
    #   below the threshold anyway); above ~0.5 the hedge effectively
    #   never fires on default-confidence rows.
    # * ``distant_threshold`` — effective confidence value below
    #   which the suffix fires. Mirrors the existing 0.5 cutoff used
    #   for ``(uncertain)``. Lower → only very-decayed claims hedge;
    #   higher → more hedging.
    confidence_decay_horizon_days: int = 365
    confidence_decay_floor: float = 0.3
    confidence_decay_distant_threshold: float = 0.5
    # ── K29 personality backlog: opinion injection numeric knobs ─────
    # The five numbers governing the K29 detector + caller plumbing.
    # The on/off / require-definite gates live on :class:`AgentSettings`
    # alongside the rest of the master switches; the rest of the
    # tunables describe a memory/retrieval concept so they sit here.
    #
    # * ``min_cosine`` — top-cosine floor between the live user
    #   message and a stance memory's embedding. Default ``0.55``
    #   matches K22 callback / K6 strong_novelty. Lower → easier
    #   topical match; higher → only near-exact topical brushes.
    # * ``min_user_words`` — short messages ("ok", "yeah", "lol")
    #   are K23 territory and never claim a contradiction. Default
    #   ``4`` words.
    # * ``cooldown_turns`` — turns between fires. Longer than K23
    #   (3 turns) because a stance disagreement is a heavier
    #   conversational beat than a soft-drift cue. Default ``5``.
    # * ``per_session_cap`` — hard cap per session. Five
    #   contradictions in a single session almost certainly means
    #   the detector is misfiring; the cap silently suppresses
    #   the rest. Default ``3``.
    # * ``per_hour_cap`` / ``per_day_cap`` — LLM-gate budgets for
    #   the borderline path. The detector only spends an LLM call
    #   when the heuristic says ``borderline`` and the limiter has
    #   tokens. Matches the F5 conflict-detector defaults.
    opinion_injection_min_cosine: float = 0.55
    opinion_injection_min_user_words: int = 4
    opinion_injection_cooldown_turns: int = 5
    opinion_injection_per_session_cap: int = 3
    opinion_injection_per_hour_cap: int = 6
    opinion_injection_per_day_cap: int = 30

    # ── L18c: boundary-vs-conversation clash cue ──────────────────────
    # Gating for the per-turn "this turn nears an active boundary" T6 cue
    # (master switch: ``agent.boundary_clash_enabled``). Cosine-only, no
    # LLM:
    # * ``min_cosine`` — the live turn's embedding must clear this against
    #   an active boundary concept's label. Set a touch above the K29
    #   opinion floor (0.55): a boundary is a broader behavioural line, so
    #   a slightly firmer topical match keeps the cue from over-firing.
    # * ``min_user_words`` — short quips ("lol", "ok") can't credibly
    #   approach a boundary; skip them (mirrors K29).
    # * ``cooldown_turns`` / ``per_session_cap`` — a standing boundary is
    #   background guidance, so the sharp in-the-moment cue is rate-limited
    #   the same way K29 is (never nag).
    boundary_clash_min_cosine: float = 0.58
    boundary_clash_min_user_words: int = 4
    boundary_clash_cooldown_turns: int = 5
    boundary_clash_per_session_cap: int = 3

    # ── K46: stance persistence ──────────────────────────────────────
    # How many turns after Aiko states a taste/opinion (a K29 cue
    # actually fired) the stance stays "warm" — i.e. a mild pushback in
    # this window is read as taste disagreement (hold the take, shield
    # the K20 calibration from a factual-trust hit) rather than a
    # correction. Decremented once per turn.
    stance_persistence_window: int = 3

    # ── K63: long-arc callbacks ("weeks ago you said…") ──────────────
    # An eligible callback memory must be at least this many days old
    # (keeps it firmly "long arc" — K22 covers fresher callbacks).
    long_arc_callback_min_age_days: int = 21
    # Topical bar: cosine of the live turn vs. the old memory. Higher
    # than the normal RAG ``score_threshold`` so a callback is a real
    # link, not a loose association.
    long_arc_callback_min_cosine: float = 0.55
    # At most this many *new* callbacks per session. The wall-clock
    # spacing that used to sit here is the type's
    # ``CuePolicy.surface_cooldown_hours``, so that an ignored callback
    # can be re-offered without the retry waiting out the gap.
    long_arc_callback_per_session_cap: int = 1
    # Skip turns shorter than this many words (too little topic to anchor
    # a callback; also avoids an embed/search on trivial replies).
    long_arc_callback_min_user_words: int = 5

    # ── K28 personality backlog: turning-over picker ─────────────────
    # The "What I've been turning over" cue (see ``AgentSettings.
    # turning_over_enabled`` for the master switch) only arms when
    # the gap between Aiko's last reply and the current user message
    # is at least this long. The default (90 min) sits inside K14's
    # absence-curiosity band [30 min, 4h) by design -- the two cues
    # stack: K14 frames the welcome-back, K28 adds "...and I was
    # thinking about X". Clamped to ``>= 5`` so a misconfiguration
    # can't make the cue fire on every typed turn. Voice-mode turns
    # never arm K28 (same gating as K14).
    turning_over_min_gap_minutes: float = 90.0
    # Picker age window for candidate reflections (the picker only
    # considers rows with ``min_age_hours <= age <= max_age_hours``).
    # Lower bound prevents a reflection written 5 minutes ago from
    # surfacing as "I've been turning this over"; upper bound keeps
    # the cue tied to the most recent between-session window. The
    # parser clamps ``max`` to ``>= min + 1h`` so the window is
    # always non-empty.
    turning_over_min_age_hours: float = 24.0
    turning_over_max_age_hours: float = 72.0
    # Cosine similarity floor for the candidate reflection against
    # the union of active-goal vectors and recent user-message
    # vectors. Below this, the candidate is dropped as "not relevant
    # to the current thread". 0.30 is conservative -- the picker
    # would rather stay silent than surface an off-topic reflection.
    # Clamped to ``[0, 1]``.
    turning_over_min_topical_similarity: float = 0.30
    # How many recent user-message vectors to pull from the RAG
    # store as the "thread" pool. 0 disables the thread pool
    # (picker would then only match against active goals). Default
    # 12 mirrors K6's :data:`NoveltyDetector.window`.
    turning_over_recent_msgs_window: int = 12

    # ── K22 personality backlog: callback / inside-joke detector ─────
    # Post-turn cosine pass between Aiko's reply and older eligible
    # memories. Hits stamp ``metadata.callback_count`` and bump
    # ``salience`` + ``revival_score`` so the retriever's read-side
    # bonus (``_RAG_CALLBACK_BONUS``) prefers memories Aiko has
    # actually managed to weave back into a reply over equally-
    # relevant siblings that have never been cited. The reinforcement
    # is invisible to the LLM by design — see :mod:`app.core.conversation.callback_detector`.
    #
    # Minimum days since ``created_at`` before a memory is eligible to
    # be counted as a callback target. Lower than this and the row is
    # treated as "still part of the current thread", not a callback.
    # Default 3 days roughly maps to "this isn't the same session and
    # the memory has had time to settle". Higher → only very-old
    # rows qualify; lower → easier callbacks.
    callback_age_floor_days: int = 3
    # Cosine similarity floor for the assistant-reply embedding vs a
    # candidate memory's embedding. ``0.55`` is the same conservative
    # threshold K6 uses for ``strong_novelty`` — high enough that
    # generic word overlap doesn't trip it but loose enough that
    # paraphrased callbacks still register. Clamped to ``[0, 1]``.
    callback_similarity_threshold: float = 0.55
    # Maximum number of memories stamped as called-back on a single
    # turn. One reply rarely references more than a handful of beats,
    # so the cap prevents a single high-similarity sentence from
    # blanket-bumping every near-duplicate row.
    callback_max_hits_per_turn: int = 3
    # Per-row cooldown in hours. A memory called back less than this
    # ago stays silent on subsequent matches so back-to-back replies
    # on a similar topic don't spam the same row. Higher → callbacks
    # cluster less; lower → faster compounding on a recent thread.
    callback_cooldown_hours: int = 24
    # Salience bump applied to each called-back row at record time.
    # The store clamps the result to ``[0, 1]`` so already-pinned /
    # high-salience rows simply stay at the ceiling. Higher → louder
    # compounding via the retriever's salience-aware base score;
    # lower → only the read-side ``_RAG_CALLBACK_BONUS`` drives the
    # preference.
    callback_salience_bump: float = 0.05
    # Revival-score bump applied to each called-back row at record
    # time. The store clamps to ``[0, 1]``. Acts as a tier-promotion
    # signal: a long_term row that keeps getting called back will
    # have its revival_score nudge it toward salience=1.0 over the
    # promotion worker's next sweeps.
    callback_revival_bump: float = 0.10
    # ── K20 personality backlog: metacognitive calibration ───────────
    # Tracks Jacob's calibration signal toward Aiko's claims (pushback /
    # softening rephrase / affirmation) into a per-user
    # CalibrationState (global scalar + bounded ring of topic slots).
    # Surfaced as a one-line hedge cue on the next turn when the
    # global score sits below ``calibration_global_low_threshold`` or
    # a topic slot sits below ``calibration_topic_low_threshold``.
    # K20 deliberately does NOT touch RAG retrieval scores -- F3
    # already owns per-memory accuracy hedging. K20 is the per-user /
    # per-topic register tilt on top of it. See
    # :mod:`app.core.affect.calibration_detector` and
    # :mod:`app.core.affect.calibration_store`.
    #
    # Baseline score the global + topic slots decay toward in the
    # absence of new signals. ``0.80`` reads as "neutral-positive"
    # (Aiko speaks confidently by default); lowering it makes Aiko
    # more reflexively hedgy.
    calibration_baseline: float = 0.80
    # Render thresholds for the inner-life cue. The global cue fires
    # only when ``global_score < calibration_global_low_threshold``;
    # the topic cue (which wins on tie) fires when any topic slot is
    # below ``calibration_topic_low_threshold``. Lower → cue is
    # rarer; higher → cue fires more readily.
    calibration_global_low_threshold: float = 0.55
    calibration_topic_low_threshold: float = 0.50
    # Exponential half-life in days for the drift toward baseline.
    # Topic slots decay slower (multiplier in
    # ``calibration_detector.decay``) so a learned topic stance
    # outlives a general bad day. Higher → calibration persists
    # longer; lower → faster recovery to baseline.
    calibration_half_life_days: float = 5.0
    # Cosine similarity floor between an incoming assistant_vec and
    # an existing topic centroid for the slot to absorb the signal
    # (rather than allocating a new slot). Higher → narrower topics,
    # more slots; lower → broader topics, fewer slots.
    calibration_topic_merge_threshold: float = 0.78
    # Cosine similarity floor between user_vec and the prior
    # assistant_vec for the softening detector to fire (the
    # hedge-token regex must also match -- both conditions are AND).
    # Higher → only near-paraphrases fire; lower → looser cosine
    # gate (raises false positives, the regex stays the safety net).
    calibration_softening_threshold: float = 0.70
    # Hard cap on the topic-slot ring. Eviction prefers the slot
    # whose ``abs(score - baseline)`` is smallest AND whose
    # ``last_signal_at`` is oldest. Higher → finer topic resolution
    # at the cost of memory + storage; lower → coarser, more global
    # behaviour.
    calibration_max_topic_slots: int = 8
    # ── K24 personality backlog: sensory anchoring layer ─────────────
    # Adaptive per-arc cadence layer that occasionally surfaces a
    # "small physical beat available" cue so Aiko substitutes a
    # sensory detail for an emotional statement. State is in-memory
    # on the controller (no DB, no persistence). See
    # :mod:`app.core.conversation.sensory_anchor`.
    #
    # Global minimum cooldown between beats; the per-arc cooldown
    # adds on top via ``max(arc_min, min_turn_gap)`` so this is a
    # *floor*, not a ceiling. Raise to make beats rarer overall;
    # the per-arc table still drives the band shape.
    sensory_anchor_min_turn_gap: int = 4
    # Multiplier on the per-arc probability. ``1.0`` = ship as
    # designed; ``< 1.0`` = rarer (e.g. ``0.5`` halves every band);
    # ``> 1.0`` = more often (e.g. ``2.0`` would push ``support``'s
    # 0.45 probability up against the 1.0 clamp). Clamped
    # ``[0.0, 2.0]`` so a buggy user.json can't accidentally
    # silence the feature entirely or push the dice into "always
    # fire" territory.
    sensory_anchor_probability_scale: float = 1.0
    # No-repeat ring size. After firing on the tea pot, the same
    # slug stays out of the candidate pool until ``max_recent``
    # other items have fired (or the deque overflows). Lower →
    # more repetition tolerance; higher → more variety required.
    sensory_anchor_max_recent_items: int = 4
    # Hard cap on how many room items the selector considers per
    # tick. The world is small today (~10 items per location), but
    # this protects future "100-item garden" scenarios from a
    # quadratic blow-up in the weighted sample step.
    sensory_anchor_max_window_items: int = 6
    # ── Background workers (schema v8) ───────────────────────────────
    # Worker intervals in seconds. Both workers are idempotent: running
    # more often is safe but wastes a little CPU. Drop to ~60 for
    # active testing. Lowered from 3600 -> 1800 since idle workers no
    # longer block the brain and there's ample local-LLM headroom.
    promotion_worker_interval_seconds: int = 1800
    decay_worker_interval_seconds: int = 1800
    # F1 personality backlog: how often the IdleFactChecker drains the
    # claim queue. Defaults to 5 minutes so a steady drip of newly
    # written memories gets verified over a session. The worker still
    # respects the per-hour/per-day rate caps in :class:`AgentSettings`.
    fact_checker_interval_seconds: int = 300
    # G2: schedule learner cadence. The bucket scan is cheap and the
    # picture changes slowly, so once a day is plenty.
    schedule_learner_interval_seconds: int = 86400
    # ── K3: routine / ritual awareness thresholds ────────────────────
    # The K3 pass piggybacks on the G2 cadence (same worker, same
    # window). These knobs only control whether a (weekday, bucket)
    # cell qualifies as a named ritual.
    #
    # Minimum number of *distinct ISO weeks* the slot must light up
    # before it's considered recurrent. 3 is the smallest value that
    # actually reads as "happens regularly" (twice could be a
    # coincidence; once is just one moment). Lower this for active
    # testing, never below 1.
    routine_min_touches: int = 3
    # Proportional floor: the slot must light up in at least this
    # share of weeks across the rolling window. With a 30-day window
    # the denominator is 5 weeks, so 0.30 means "covered 2 of 5".
    # This stops a long window from minting a "routine" off three
    # weeks at the start of the window when the user has since drifted
    # to other slots.
    routine_min_share: float = 0.30
    # Cap on how many named routines the worker writes into the
    # ``routines`` profile field. The 240-char ``ProfileEntry`` cap is
    # the hard upper bound; this knob is the soft one that keeps the
    # rendered phrase from growing into a list. Top-N by recurrence
    # density.
    routine_max_active: int = 5
    # G3: idle curiosity worker cadence. Each tick web-searches at most
    # one open question, so a 30-minute interval combined with the
    # rate-cap gives the worker room to chip away at a backlog without
    # hammering the search engine.
    idle_curiosity_interval_seconds: int = 1800
    # F9: knowledge-enrichment worker cadence. Each successful tick
    # web-searches one topic cluster and distils up to two facts, so an
    # hour between runs (combined with the tight hour/day search caps)
    # keeps the knowledge pool growing as a slow drip rather than a
    # firehose.
    knowledge_enrichment_interval_seconds: int = 3600
    # F9: per-cluster cooldown. After researching (or trying to
    # research) an interest cluster, the worker won't touch the same
    # topic again for this many hours, so it rotates across interests
    # instead of grinding the densest one. Keyed on a hash of the
    # cluster summary in ``kv_meta``.
    knowledge_cluster_cooldown_hours: int = 72
    # F9: per-cluster knowledge ceiling. A cluster that already has this
    # many ``knowledge`` rows is considered "researched enough" and
    # skipped, so the worker spreads its budget across the user's
    # breadth of interests rather than over-mining one.
    knowledge_enrichment_max_per_cluster: int = 3
    # F9 (research planner): how many candidate clusters a single tick may
    # try before giving up. When the top-scored cluster is judged
    # "unresearchable" by the planner (purely personal/relationship
    # material) the worker advances to the next-best cluster in the SAME
    # tick rather than burning the tick on a junk query.
    knowledge_enrichment_max_clusters_per_run: int = 3
    # F9 (research planner): max impersonal search queries the planner may
    # emit per cluster. The worker researches one per tick and queues the
    # rest, so a single cluster is mined from several angles over time.
    knowledge_research_queries_per_cluster: int = 3
    # F9 (research planner): cooldown applied to a cluster the planner
    # deems unresearchable. Much longer than the normal per-cluster
    # cooldown so a personal-only cluster doesn't re-burn a planner call
    # every few days.
    knowledge_unresearchable_cooldown_hours: int = 336
    # ── F10f: knowledge-gap notice worker (self-aware "I don't know X") ──
    # How often the KnowledgeGapNoticeWorker may draft a cue during quiet
    # windows. Hourly is plenty — the cue surfaces only when the user
    # raises the topic, so over-drafting just fills the small ring.
    knowledge_gap_notice_interval_seconds: int = 3600
    # A cluster must have at least this many members to count as a gap Aiko
    # "keeps coming back to" — small clusters aren't a recurring theme worth
    # admitting ignorance about.
    knowledge_gap_notice_min_size: int = 5
    # Upper bound on a cluster's ``knowledge``-row fraction for it to still
    # read as a gap. At/below this the topic is "barely researched"; above
    # it Aiko already knows enough that the admit-the-gap beat would be a
    # lie. Default 0.15 ≈ "fewer than ~1 in 6 members are learned facts".
    knowledge_gap_notice_max_knowledge_fraction: float = 0.15
    # Per-topic cooldown: once a gap is drafted (and likely voiced) for a
    # topic, don't re-draft it for this long, so Aiko doesn't keep harping
    # on "I still don't know much about your job". Keyed on a stable hash
    # of the label in ``kv_meta`` (survives cluster renumbering).
    knowledge_gap_notice_topic_cooldown_hours: int = 72
    # Size of the kv journal ring of drafted notices. Tiny — the provider
    # surfaces the newest topic-relevant unseen entry.
    knowledge_gap_notice_journal_max: int = 6
    # ── K64a: associative wandering (connect two distant topics) ─────────
    # How often the AssociativeWanderWorker may draft a connection during
    # quiet windows. Deliberately long (90 min default): a person who keeps
    # announcing connections is exhausting, so rarity is the feature.
    associative_wander_interval_seconds: int = 5400
    # Global cooldown between drafts (independent of the per-tick interval),
    # so even a long idle stretch can't produce a flurry of connections.
    associative_wander_cooldown_seconds: int = 7200
    # Size of the kv journal ring of drafted connections.
    associative_wander_journal_max: int = 6
    # A cluster must have at least this many members to be worth connecting
    # — a one-off topic isn't a real strand of thought.
    associative_wander_min_size: int = 4
    # Upper bound on the centroid cosine of the two clusters for the pair to
    # count as "distant". At/below this the topics are genuinely far apart
    # (the interesting kind of connection); above it they're neighbours and
    # the link would be obvious. 0.25 ≈ "clearly different topics".
    associative_wander_max_pair_cosine: float = 0.25
    # Per-pair cooldown: once a connection between two topics is drafted,
    # don't re-connect the same pair for this long (a week default), so Aiko
    # doesn't keep re-noticing the same link. Keyed on a stable hash of the
    # unordered label pair in ``kv_meta`` (survives cluster renumbering).
    associative_wander_pair_cooldown_hours: int = 168
    # How many member content snippets to pull from each cluster as substance
    # for the worker-LLM connection prompt. 0 → labels only.
    associative_wander_member_samples: int = 3
    # ── K64b: interest drift (budding / fading interests over time) ──────
    # How often the InterestDriftWorker snapshots cluster mass + may draft a
    # drift cue. Long (6h default): interests drift slowly, and each tick
    # just adds one sample to the per-topic mass time-series.
    interest_drift_interval_seconds: int = 21600
    # Size of the kv journal ring of drafted drift cues.
    interest_drift_journal_max: int = 6
    # A cluster must have at least this many members to track / count as a
    # real interest (rising or fading).
    interest_drift_min_size: int = 4
    # Cap on how many of the largest clusters get a mass sample per tick —
    # bounds the kv time-series size.
    interest_drift_max_clusters: int = 40
    # How many mass snapshots to keep per topic (the drift window). At the
    # 6h default that's two days of history.
    interest_drift_window_samples: int = 8
    # Minimum snapshots before a topic's drift is classified at all (cold
    # topics stay silent until the window warms).
    interest_drift_min_samples: int = 3
    # Fractional growth across the window for a topic to read as "rising"
    # (0.5 ≈ "grew 50% since the window start"), combined with an absolute
    # floor of a few new members so a tiny cluster can't trip it.
    interest_drift_rise_ratio: float = 0.5
    # Upper bound on window growth for a sizable cluster to read as
    # "fading" (0.05 ≈ "barely grew — attention has cooled").
    interest_drift_fade_max_growth_ratio: float = 0.05
    # Per-topic cooldown: once a drift is noticed for a topic, don't
    # re-notice it for this long. Keyed on a stable hash of the label.
    interest_drift_topic_cooldown_hours: int = 72
    # ── K67: dormant-interest re-opener ("haven't talked about X in ages") ─
    # How often the DormantInterestWorker scans cluster activity + may draft
    # a re-opener. Long (6h default): a dropped interest is a slow signal.
    dormant_interest_interval_seconds: int = 21600
    # Size of the kv journal ring of drafted re-openers.
    dormant_interest_journal_max: int = 6
    # A cluster must have at least this many members to count as a genuine
    # past interest worth re-opening (its accumulated members ≈ peak mass).
    dormant_interest_min_size: int = 6
    # Cap on how many of the largest clusters get scanned per tick.
    dormant_interest_max_clusters: int = 40
    # A cluster counts as dormant once its newest member is at least this
    # many days old (no new activity for a real stretch). 21 ≈ three weeks.
    dormant_interest_dormant_days: float = 21.0
    # Per-topic cooldown: once a topic is drafted as a re-opener, don't
    # re-draft it for this long (14 days) so the ring doesn't fill with the
    # same dead thread. Keyed on a stable hash of the label.
    dormant_interest_topic_cooldown_hours: int = 336
    # Provider-side wall-clock surfacing cooldown: at most one re-opener may
    # surface across ALL topics in this window (24h), so even with several
    # dormant interests queued the beat stays rare.
    dormant_interest_surface_cooldown_hours: float = 24.0
    # ── K64c: curiosity gradient (thin edge of a dense topic) ────────────
    # How often the CuriosityGradientWorker may draft a curiosity-edge cue.
    curiosity_gradient_interval_seconds: int = 5400
    # Size of the kv journal ring of drafted curiosity edges.
    curiosity_gradient_journal_max: int = 6
    # A cluster must have at least this many members to be the *dense* anchor
    # of an edge (the familiar territory Aiko's been spending time around).
    curiosity_gradient_dense_min_size: int = 8
    # Member-count band for the *thin* cluster (the under-explored edge):
    # big enough to be a real topic, small enough to be unexplored.
    curiosity_gradient_thin_min_size: int = 2
    curiosity_gradient_thin_max_size: int = 4
    # Centroid-cosine band for a thin cluster to count as "adjacent" to its
    # nearest dense cluster: at/above the min it's genuinely on the rim of
    # the familiar topic; at/below the max it isn't a near-duplicate of it.
    curiosity_gradient_adjacency_min_cosine: float = 0.40
    curiosity_gradient_adjacency_max_cosine: float = 0.90
    # Per-edge cooldown: once a curiosity edge is noticed, don't re-notice
    # it for this long. Keyed on a stable hash of the unordered label pair.
    curiosity_gradient_edge_cooldown_hours: int = 96
    # ── K64d: knowledge-map self-reflection (shape of what I know) ───────
    # How often the KnowledgeMapReflectionWorker may run. Daily by default —
    # this is the rarest, most introspective K64 beat. Floored at 60s.
    knowledge_map_reflection_interval_seconds: int = 86400
    # Wall-clock cooldown between map-shape reflections, independent of the
    # scheduler interval (a force-run still bypasses it). Hours.
    knowledge_map_reflection_cooldown_hours: int = 20
    # Need at least this many labelled clusters before there's a "shape"
    # worth reflecting on — otherwise the worker skips (no_context).
    knowledge_map_reflection_min_clusters: int = 4
    # How many of the richest (largest) clusters to feed the LLM as the
    # "well-trodden territory" half of the prompt.
    knowledge_map_reflection_rich_top_n: int = 5
    # How many under-researched (dense-but-unlearned) clusters to feed as the
    # "blank in the learned sense" half. 0 disables the gap half entirely.
    knowledge_map_reflection_gap_top_n: int = 3
    # L28: how many concepts to hang off each rich territory ("you believe:
    # …"), read through ConceptView.for_cluster. 0 disables the annotation
    # and restores the pre-L28 size/recency-only payload.
    knowledge_map_reflection_concepts_per_cluster: int = 2
    # num_predict cap for the worker-LLM meta-thought (it's one short note).
    knowledge_map_reflection_max_tokens: int = 120
    # Salience of the written [mindmap] reflection memory. Mid-range — it's a
    # scratchpad-tier reflection that earns persistence only via retrieval.
    knowledge_map_reflection_salience: float = 0.5
    # ── F10h: topic temperature (per-cluster affect) ─────────────────────
    # Minimum centroid cosine for the live turn to count as "on" a topic
    # cluster before its temperature is even considered. Keeps the tonal
    # nudge from firing on a loose, incidental brush with a cluster.
    topic_temperature_min_sim: float = 0.45
    # A cluster's dominant pole (warmth or tenderness, both in [0, 1]) must
    # reach this for the cue to surface. Higher → only strongly-charged
    # topics nudge tone.
    topic_temperature_threshold: float = 0.5
    # Global cooldown (in turns) after a temperature cue fires, so a
    # charged topic isn't re-nudged every single turn it comes up.
    topic_temperature_cooldown_turns: int = 6
    # ── F10i: per-topic confidence self-model ────────────────────────────
    # Minimum centroid cosine for the live turn to count as "on" a topic
    # cluster before its confidence is judged (mirrors the temperature gate).
    topic_confidence_min_sim: float = 0.45
    # Confidence (in [0, 1]) at/below which the topic reads as *thin* ground
    # → hedge / ask. Genuinely small clusters; F10f owns dense-but-thin.
    topic_confidence_thin_threshold: float = 0.25
    # Confidence at/above which the topic reads as *familiar* ground →
    # stop over-hedging. Rich clusters with real learned-fact coverage.
    topic_confidence_familiar_threshold: float = 0.7
    # Global cooldown (in turns) after a confidence cue fires.
    topic_confidence_cooldown_turns: int = 6
    # ── K66: earned familiarity ("well-trodden ground between us") ───────
    # Minimum centroid cosine for the live turn to count as "on" a topic
    # cluster before its shared-history depth is judged (mirrors the
    # temperature / confidence gates).
    earned_familiarity_min_sim: float = 0.45
    # Cluster mass (member count) at/above which a topic reads as deep,
    # well-worn shared ground -> the shorthand / skip-the-recap register
    # cue fires. Set above F10i's effective size band so K66 fires on the
    # big-but-unstudied *conversational* clusters F10i (knowledge-weighted)
    # leaves silent. Distinct signal from topic_confidence on purpose.
    earned_familiarity_deep_threshold: int = 14
    # Global cooldown (in turns) after an earned-familiarity cue fires.
    # Longer than its siblings: deep familiarity is a slow-moving register,
    # not a per-charged-topic beat, so it should surface rarely.
    earned_familiarity_cooldown_turns: int = 12
    # ── K75: user-expertise calibration (per-cluster competence) ─────────
    # Minimum centroid cosine for the live turn to count as "on" a cluster
    # before we learn from / steer on it (mirrors the K66 gate). Doubles as
    # the noise filter: only topically-substantive turns feed the estimate.
    user_expertise_min_sim: float = 0.45
    # EMA learning rate blending each signal-bearing message into the
    # per-cluster competence score (converges over ~4-8 messages).
    user_expertise_learning_rate: float = 0.25
    # Signal-bearing messages a cluster needs before any band is trusted.
    user_expertise_min_samples: int = 4
    # Score thresholds (score in [-1, +1]) for the confident bands; the
    # middle stays "familiar" and renders no steer.
    user_expertise_novice_threshold: float = -0.35
    user_expertise_expert_threshold: float = 0.35
    # Global cooldown (in turns) after a depth-steer cue fires.
    user_expertise_cooldown_turns: int = 12
    # ── K68: embodied vitality (body energy) ─────────────────────────────
    # Half-life (hours) of the relaxation toward the circadian baseline.
    # After this many idle hours the gap to baseline halves. Short enough
    # that a livened-up evening settles back overnight; long enough that a
    # within-session boost persists across a conversation.
    vitality_recover_half_life_hours: float = 2.0
    # Energy at/below which the LOW register cue fires (sleepy, smaller /
    # slower body language) and the avatar droops.
    vitality_low_threshold: float = 0.30
    # Energy at/above which the HIGH register cue fires (lit-up, more
    # animated).
    vitality_high_threshold: float = 0.70
    # Avatar gesture/breath amplitude multiplier at energy 0 (floor) and
    # energy 1 (ceil). Multiplied onto the user's avatar.expressiveness
    # setting, so a tired Aiko visibly shrinks without overwriting the
    # slider. floor < 1 < ceil keeps a normal-energy day near 1.0x.
    vitality_expressiveness_floor: float = 0.7
    vitality_expressiveness_ceil: float = 1.2
    # ── spend (per turn) ──
    # Chars of Aiko's reply per one "length cost unit"; a longer, more
    # effortful reply spends more energy. 1200 chars ~ one unit.
    vitality_cost_chars_per_unit: float = 1200.0
    # Energy spent per length unit and per unit of K57 emotion intensity.
    vitality_cost_length_unit: float = 0.04
    vitality_cost_emotion_gain: float = 0.06
    # Hard cap on total per-turn spend so one turn can't crater the bucket.
    vitality_cost_max: float = 0.12
    # ── boost (the liven-up) ──
    # Energy gained when K14 reads the user as ``engaged``.
    vitality_boost_engaged: float = 0.05
    # Arousal (her own activation) above this adds (arousal - thr) * gain.
    vitality_boost_arousal_threshold: float = 0.55
    vitality_boost_arousal_gain: float = 0.22
    # Energy gained on a K6 strong-novelty / mild-shift topic.
    vitality_boost_strong_novelty: float = 0.04
    vitality_boost_mild_novelty: float = 0.02
    # Hard cap on total per-turn boost so one great turn can't slam to full.
    vitality_boost_max: float = 0.15
    # ── proactivity feedback ──
    # At energy 0 a tired Aiko stretches her proactive silence threshold
    # by this factor (initiates less); at energy 1 it shrinks by it. 0.0
    # disables the proactivity feedback. e.g. 0.4 -> up to +40% longer
    # silence when exhausted, -40% when lit up.
    vitality_proactive_factor: float = 0.4
    # ── K68 rhythm exceptions (off-rhythm days) ──
    # Probability that a given local day rolls an *off-rhythm* baseline
    # (early-bird / night-owl / flipped / sluggish / wired) instead of the
    # plain circadian curve. Drawn once per day, stable all day. 0.0 ->
    # always normal (feature effectively off via the agent master switch);
    # clamped to [0, 1]. At the default ~1-in-3 days is off-rhythm, with a
    # full day/night flip being the rarest slice of that.
    vitality_rhythm_exception_chance: float = 0.3
    # ── K69: implicit-need reading ──
    # Score floor for the per-turn response-mode classifier: a winning
    # mode below this stays silent (``neutral``). At the default 2.0 a
    # single soft cue (weight 1.0) isn't enough -- it takes one strong
    # marker or two corroborating signals to steer, which keeps the cue
    # rare and the restraint honest. Floored at 0.5.
    implicit_need_min_confidence: float = 2.0
    # ── K-time3: upcoming-horizon block (pre-resolved future times) ──────
    # How far ahead the forward sweep looks for ``future_plan`` events
    # (in days). Within this window the resolved phrasing stays specific
    # ("tomorrow morning 09:00", "on Friday 18:00"); beyond it the cue
    # stays silent.
    upcoming_horizon_days: int = 7
    # Maximum number of upcoming events listed in the cue, soonest-first.
    upcoming_horizon_max_items: int = 3
    # Cooldown (in turns) before the *same* set of upcoming plans is
    # re-surfaced — a new or freshly-passed plan re-surfaces immediately
    # (the set's signature changes). Keeps the heads-up from nagging every
    # turn while still resurfacing periodically for an imminent event.
    upcoming_horizon_cooldown_turns: int = 6
    # K61: minimum cosine similarity for a learned fact to count as
    # "relevant to what the user just asked" in the knowledge-grounding
    # inner-life block. Higher → the steer fires only on a tight
    # topical match (fewer, more on-point cues). Lower → fires more
    # readily (risk of nudging Aiko to "commit to specifics" that only
    # loosely relate).
    knowledge_grounding_min_similarity: float = 0.45
    # K61: how many learned facts the grounding cue lists inline. Kept
    # tiny so the block stays a steer ("you actually know this — name
    # it"), not a data dump.
    knowledge_grounding_max_items: int = 2
    # K9: curiosity-seed worker cadence. One LLM call + a handful of
    # embeddings per tick, so an hour between successful runs is
    # plenty -- the worker also ``is_ready=False``s when the seed
    # store is at ``curiosity_seed_max_active`` so the cadence is a
    # ceiling, not a floor.
    curiosity_seed_interval_seconds: int = 3600
    # L2 concept synthesis worker. Runs regularly (default 30 min) but
    # does a small bounded batch per run using kv_meta dirty-tracking, so
    # it triggers reliably under intermittent uptime and never does one
    # long pass when data has accumulated. Steady-state runs are near
    # no-ops (0 LLM calls) when nothing is dirty. ``max_clusters_per_run``
    # bounds how many dirty topic clusters get full-content synthesis each
    # run (the rest of the map is included as cheap labels for
    # cross-cluster reasoning); ``max_aiko_memories`` caps the aiko-self
    # input; ``dirty_size_delta`` is the min cluster/population size drift
    # that re-marks something dirty (avoids churn on +/-1 wobble).
    concept_synthesis_interval_seconds: int = 1800
    concept_synthesis_max_clusters_per_run: int = 5
    concept_synthesis_max_aiko_memories: int = 40
    concept_synthesis_dirty_size_delta: int = 3
    # Answer-token budget per proposer LLM call. Sized generously: a
    # reasoning-capable maintenance model can spend a large, variable preamble
    # on visible chain-of-thought before the JSON, and several proposers emit
    # multiple concepts with full rationales in one object, so a low cap
    # truncates the array mid-object and the whole batch fails to parse (the
    # salvage pass recovers complete objects, but a roomy budget avoids losing
    # the tail in the first place). It's an idle worker, so the extra tokens
    # cost latency we don't feel.
    concept_synthesis_max_tokens: int = 4096
    # L13 affective concepts. The post-turn per-cluster affect sampler folds
    # this turn's affect into the live topic cluster's rolling EWMA (one map
    # per subject). ``affect_sampler_min_sim`` / ``affect_sampler_top_n`` gate
    # + bound the cluster match; ``affect_sampler_learning_rate`` is the EWMA
    # alpha; the map is bounded by ``cluster_affect_map_cap`` +
    # ``cluster_affect_max_age_days``. ``concept_synthesis_affect_min_samples``
    # is how many affect-bearing turns a cluster (or self-theme) must accrue
    # before it is offered to the affective proposers.
    concept_synthesis_affect_min_samples: int = 3
    affect_sampler_min_sim: float = 0.4
    affect_sampler_top_n: int = 1
    affect_sampler_learning_rate: float = 0.2
    cluster_affect_map_cap: int = 200
    cluster_affect_max_age_days: float = 120.0
    # L7 relationship rituals. The ritual pass groups ``shared_moment``
    # memories by single-link cosine (``ritual_group_similarity``) into
    # recurring clusters of ``>= ritual_group_min_size`` members, offering up
    # to ``max_ritual_groups`` of them to the proposer. The whole pass is
    # skipped until at least ``ritual_min_moments`` shared moments exist.
    # The similarity floor is on the **mean-centered** scale (see
    # ``ritual_grouping.center_vectors``): raw shared-moment cosines average
    # 0.608, so the old 0.6 linked 95% of all pairs and single-link handed
    # back the whole corpus as one group.
    concept_synthesis_ritual_min_moments: int = 6
    concept_synthesis_ritual_group_min_size: int = 3
    concept_synthesis_ritual_group_similarity: float = 0.45
    concept_synthesis_max_ritual_groups: int = 3
    # L8 narrative arcs. The narrative pass loads each subject-dominant topic
    # cluster's member memories in temporal order and offers up to
    # ``max_narrative_clusters_per_run`` of them (per subject) as candidate
    # arcs, each capped at ``max_narrative_memories`` steps. A candidate (and a
    # NEW arc) needs at least ``narrative_min_chain`` ordered steps to count as
    # a story rather than an anecdote.
    concept_synthesis_narrative_min_chain: int = 3
    concept_synthesis_max_narrative_clusters_per_run: int = 3
    concept_synthesis_max_narrative_memories: int = 40
    # L29a shared arcs -- the "both of us" narrative, cut out of the
    # ``shared_moment`` stream rather than sourced from topic clusters. An
    # episode grows while the next moment is within ``shared_arc_similarity``
    # of its running centroid AND within ``shared_arc_gap_days`` of its last
    # member; it must reach ``shared_arc_min_chain`` moments and then have
    # been quiet for ``shared_arc_quiet_days`` (a project still in motion is
    # not a closed arc). Up to ``max_shared_arc_episodes`` are offered per run.
    # The similarity floor lives on the *mean-centered* scale the grouper
    # compares on, so it is not comparable to the ritual threshold: raw shared
    # moments all point the same way (measured mean pairwise cosine 0.608), and
    # only the residual after the corpus mean is projected out says anything
    # about topic.
    concept_synthesis_shared_arc_min_chain: int = 3
    concept_synthesis_shared_arc_similarity: float = 0.45
    concept_synthesis_shared_arc_gap_days: float = 10.0
    concept_synthesis_shared_arc_quiet_days: float = 3.0
    concept_synthesis_max_shared_arc_episodes: int = 3
    # L14 aspiration synthesis (the open-ended sibling of narrative). Same
    # shape as the narrative knobs, plus ``aspiration_min_span_days``: the
    # ordered evidence of a candidate must cover at least this many days before
    # it is offered as a *trajectory* -- a direction has to persist over time,
    # not just accumulate in one sitting.
    concept_synthesis_aspiration_min_chain: int = 3
    concept_synthesis_aspiration_min_span_days: float = 14.0
    concept_synthesis_max_aspiration_clusters_per_run: int = 3
    concept_synthesis_max_aspiration_memories: int = 40
    # L18 boundary synthesis: cap on explicit-anchor memories offered to the
    # boundary proposer per run (per subject). Boundaries are mined from a
    # hybrid of topic clusters (bounded by ``concept_synthesis_max_clusters_per_run``)
    # and these deliberate remembered notes; this bounds the anchor batch.
    concept_synthesis_max_boundary_memories: int = 24
    # L23: cap on explicit-anchor memories offered to the communication-style
    # proposer per run (per subject). Style lines are mined from a hybrid of
    # topic clusters and deliberate remembered notes; this bounds the anchor
    # batch (same shape as the boundary cap).
    concept_synthesis_max_comm_style_memories: int = 24
    # L12: cap on active BASE concepts offered to the tension (meta) proposer
    # per run (per subject); for the relationship lens each side gets roughly
    # half. Concept cardinality is small by design (tens), so this rarely bites
    # -- it only bounds the prompt when the graph is unusually rich.
    concept_synthesis_max_tension_concepts: int = 24
    # L20: cap on active BASE concepts offered to the generalization (meta)
    # proposer per run (per subject). Same shape/rationale as the tension cap.
    concept_synthesis_max_generalization_concepts: int = 24
    # K81 taste synthesis thresholds. The taste pass reads the L37 surfacing
    # ledger's per-cluster engaged rate over ``taste_affinity_window_days``
    # (a window, not lifetime, so taste tracks how the relationship works now),
    # trusts a cluster only once it has ``taste_min_settled`` settled surfacings
    # (warmup floor -- a cold ledger yields no taste), keeps clusters whose
    # engaged rate clears the affinity bar (a *rate*, so a rare topic that
    # always lands beats a frequent flat one), and offers at most
    # ``concept_synthesis_max_taste_clusters`` per run.
    #
    # The bar is **relative to her own baseline**:
    # ``max(taste_min_affinity, baseline * taste_affinity_baseline_multiple)``,
    # where the baseline is the pooled engaged rate across the same snapshot.
    # An absolute bar was the original design and it was measured unreachable
    # (L28m): across 39 warmed clusters the best engaged rate was 0.32 and the
    # median 0.20 against a 0.5 bar, so the pass could never mint a taste. The
    # labels are not balanced classes -- the same argument
    # ``engagement_baseline`` makes for L38 standing -- so "lands better than
    # her average" is the honest reading of enjoyment, and the absolute value
    # survives only as a floor that stops a relationship where nothing lands
    # from minting taste out of noise.
    taste_affinity_window_days: int = 90
    taste_min_settled: int = 4
    taste_min_affinity: float = 0.15
    taste_affinity_baseline_multiple: float = 1.4
    concept_synthesis_max_taste_clusters: int = 6
    # K85c pursuit synthesis. The pass is a no-op until ``pursuit_min_notes``
    # ``pursuit_note`` rows exist -- below the gate's three-source floor there
    # is nothing that could promote -- and offers at most
    # ``concept_synthesis_max_pursuit_memories`` notes per run, chronologically
    # rather than by salience: recurrence is the signal, and a salience sort
    # would hide exactly the dull repetition that proves it.
    pursuit_min_notes: int = 6
    concept_synthesis_max_pursuit_memories: int = 40
    # L42 weekly surfacing-conduct self-model.
    conduct_window_days: int = 90
    conduct_cadence_seconds: int = 604800
    conduct_max_user_vectors: int = 1000
    conduct_user_topic_min_similarity: float = 0.45
    conduct_min_settled_rows: int = 50
    conduct_min_user_turns: int = 20
    conduct_concentration_min_settled: int = 8
    conduct_concentration_min_share: float = 0.30
    conduct_concentration_min_excess: float = 0.12
    conduct_concentration_min_ratio: float = 2.0
    conduct_neglect_min_confidence: float = 0.75
    conduct_neglect_min_age_days: float = 14.0
    conduct_neglect_max_surfaced: int = 1
    conduct_neglect_min_candidates: int = 3
    conduct_fixation_min_surfaced: int = 12
    conduct_fixation_min_settled: int = 6
    conduct_fixation_min_ratio: float = 3.0
    conduct_fixation_min_rate_gap: float = 0.05
    conduct_snapshot_cap: int = 6
    conduct_notice_min_confidence: float = 0.7
    conduct_notice_cooldown_days: float = 7.0
    # L20 surfacing: "prefer the abstraction". When a generalization parent is
    # among the turn's concept candidates at >= ``parent_min_confidence``, its
    # child concepts are dropped from the pool so Aiko speaks the through-line
    # instead of reciting the specifics beneath it. Disable to render parent +
    # children side by side.
    generalization_suppress_children_enabled: bool = True
    generalization_parent_min_confidence: float = 0.7
    # L12 tension-cue worker (the proactive "a friction worth sitting with"
    # producer). ``interval_seconds`` is the idle cadence; ``min_confidence``
    # gates which tensions qualify; ``journal_max`` bounds the cue ring. The
    # per-tension cooldown lives under AgentSettings
    # (``tension_cue_cooldown_days``) next to the enable flags.
    tension_cue_interval_seconds: float = 28800.0
    tension_cue_min_confidence: float = 0.6
    tension_cue_journal_max: int = 4
    # L14 aspiration-momentum worker (the proactive check-in producer). It
    # drafts a private cue over an active aspiration that has gone stale enough
    # to be worth revisiting. ``interval_seconds`` is the idle cadence;
    # ``cooldown_days`` is the per-concept cooldown that rotates check-ins
    # across the active set; ``min_confidence`` gates which aspirations qualify;
    # ``staleness_min_days`` is how long since last reinforcement before one is
    # worth a check-in; ``journal_max`` bounds the cue ring.
    aspiration_momentum_interval_seconds: float = 21600.0
    aspiration_momentum_cooldown_days: float = 10.0
    aspiration_momentum_min_confidence: float = 0.6
    aspiration_momentum_staleness_min_days: float = 7.0
    aspiration_momentum_journal_max: int = 4
    # Shared engagement clock (app/core/infra/engagement_clock.py): a
    # monotonic "active-conversation time" counter so decay tracks time
    # actually spent engaging, not calendar time (away/quiet stretches
    # cost ~nothing). ``seconds_per_day`` is the calibration -- how much
    # active conversation equals one "decay-day" in the existing per-day
    # rate domains (default gentle: ~1 active hour = 1 decay-day).
    # ``idle_cap_seconds`` bounds the credit for one turn (so returning
    # from a long absence adds only one capped turn's worth);
    # ``min_turn_seconds`` is the floor credited per completed turn.
    engagement_clock_enabled: bool = True
    engagement_seconds_per_day: float = 3600.0
    engagement_idle_cap_seconds: float = 300.0
    engagement_min_turn_seconds: float = 15.0
    # When on, the memory decay worker drives ``elapsed_days`` from the
    # engagement clock instead of wall-clock (with the same
    # ``decay_max_catchup_days`` clamp as a safety net). Off => today's
    # wall-clock behaviour exactly.
    memory_decay_use_engagement_clock: bool = True
    # L3 concept lifecycle engine. The single writer of a concept's
    # confidence / plasticity / status. Runs often + cheap (no LLM) in a
    # rolling round-robin batch (``batch_size`` stalest concepts per tick)
    # so a growing concept set never blocks the idle scheduler. Also gated
    # by ``agent.concepts_enabled``. Decay is engagement-driven (via the
    # shared clock), keyed off the per-concept ``last_lifecycle_engagement``
    # anchor, and status transitions read confidence (not wall-clock idle):
    # ``active -> dormant`` below ``dormant_confidence_floor``,
    # ``dormant -> retired`` below ``retire_confidence_floor``. Promotion
    # is gated by distinct sources + confidence over the promote threshold;
    # the stability-age floor (``promote_min_age_days``) defaults to 0 (off)
    # since those are the meaningful signals. When *age* is used (a non-zero
    # ``promote_min_age_days``, or the ``candidate_ttl_days`` cleanup) it is
    # measured in *engaged* days -- via the per-concept
    # ``first_evidence_engagement`` anchor -- so a concept matures with real
    # interaction, not wall-clock idling (wall-clock fallback when the
    # engagement clock is off). Half-life is in *engaged* days;
    # ``decay_max_catchup_days`` clamps decay's engaged-days per tick (age
    # itself is unclamped -- it only gates).
    concept_lifecycle_enabled: bool = True
    concept_lifecycle_interval_seconds: int = 300
    concept_lifecycle_batch_size: int = 100
    concept_promote_min_sources: int = 2
    # Stability *delay* before a candidate may promote, in *engaged*
    # (active-conversation) days when the engagement clock is on (wall-clock
    # fallback otherwise). Defaults to 0.0 => **off**: promotion is gated by
    # distinct sources + confidence alone, which are the meaningful signals
    # (a concept that's well-evidenced and confident shouldn't have to wait,
    # and post-promotion evidence only refines its confidence). Raise it
    # (e.g. 2.0 ~= 2h of active convo) to re-introduce a maturation delay.
    concept_promote_min_age_days: float = 0.0
    concept_promote_min_confidence: float = 0.6
    # 7.5 engaged days, damped per kind to an effective 11-14 (see
    # ``effective_halflife``), so a belief nothing re-observes reaches the
    # dormant floor from 0.8 in 13-16 engaged days. It was 45.0, which
    # worked out to 80-97 engaged days -- against roughly 3.4 engaged days
    # accumulated per week of real use, that is a year and a half of
    # conversation to clear one unearned concept, so nothing ever cleared
    # and 71% of the graph was never-reinforced (L22).
    concept_confidence_halflife_days: float = 7.5
    concept_decay_max_catchup_days: float = 3.0
    concept_dormant_confidence_floor: float = 0.35
    concept_retire_confidence_floor: float = 0.15
    # L46: the second route out of ``dormant``, beside the confidence floor
    # above. Wall-clock days since a dormant belief was last reinforced,
    # after which it retires -- see ``_is_stale_dormant`` for why *this* one
    # is wall-clock while every other age floor here is engagement-driven.
    #
    # It exists because the floor alone barely fired: eight concepts had ever
    # retired, against 251 sitting dormant at ~0.45 average confidence with
    # 222 of them unreinforced for over a month. The L22 sweep demotes
    # never-reinforced actives while their confidence is still high, and from
    # there decay needs ~19 engaged days -- five or six calendar weeks -- to
    # reach 0.15, so the floor was always weeks behind a conclusion the
    # evidence already supported. ``retired`` is revivable and dormant rows
    # never surface, so arriving early costs nothing. 0 disables the route.
    concept_dormant_ttl_days: float = 30.0
    concept_candidate_ttl_days: float = 21.0
    concept_identity_plasticity: float = 0.3
    # L16 plasticity governor. Plasticity is the per-concept learning rate
    # the L3 engine damps *every* confidence move by (accrual, decay, L9
    # disproof, L15 revision), so a sticky core trait resists change in
    # both directions. Per-kind defaults live on the ``ConceptKind``
    # registry (identity uses ``concept_identity_plasticity`` above);
    # ``concept_default_plasticity`` is the fallback band for any kind
    # that registers no default.
    concept_default_plasticity: float = 0.5
    # L16 relationship modulation + plasticity-drift + re-check slowdown.
    # (1) Modulation: at eval time, a kind that opts in (only ``boundary``
    # today, via its registry ``plasticity_modulation``) has its *effective*
    # plasticity raised by the live trust + relationship-duration signal --
    # loosening a boundary as the bond deepens, capped at the kind ceiling and
    # never touching the stored base. ``duration_days_full`` is the days-known
    # at which the duration term saturates; ``shift_event_delta`` is the band a
    # lift must cross (vs. the last ``influences`` edge) before a
    # ``plasticity_shift`` event is emitted. (2) Drift: a settled *active*
    # concept's stored plasticity is nudged one-way down toward ``drift_floor``
    # at ``drift_rate``, scaled by confidence + engaged age (stickier with
    # time). (3) Re-check slowdown: a sticky (low effective-plasticity) concept
    # is probed for contradictions on a plasticity-scaled stride
    # (``stride = 1 + round(stride_k * (1 - eff_plast))``) so core beliefs are
    # re-examined less often. Each piece is independently switchable; all on.
    concept_plasticity_modulation_enabled: bool = True
    concept_plasticity_duration_days_full: float = 180.0
    concept_plasticity_shift_event_delta: float = 0.1
    concept_plasticity_drift_enabled: bool = True
    concept_plasticity_drift_rate: float = 0.05
    concept_plasticity_drift_floor: float = 0.15
    concept_plasticity_recheck_slowdown_enabled: bool = True
    concept_plasticity_recheck_stride_k: float = 3.0
    # L17a concept trajectory: a concept that decays without ever crossing a
    # status threshold emits no lifecycle event, so its slide is invisible to
    # the event log. The lifecycle sweep drops a ``confidence_sample`` row
    # whenever a quiet concept has moved ``sample_band`` away (either
    # direction) from the confidence at its last recorded event -- banded
    # rather than per-tick so the timeline stays readable.
    concept_confidence_sample_enabled: bool = True
    concept_confidence_sample_band: float = 0.1
    # L23 cognitive surfacing -- habituation (repetition suppression). A concept
    # surfaced recently is damped by a ``[floor, 1]`` multiplier that recovers
    # over ``window_turns`` user-turns (turn clock = ``relationship.total_turns``,
    # state in ``kv_meta`` under ``concept.surfacing_habituation``). The flex
    # (turn-relevant) lane uses ``_floor`` (strong suppression so it steps
    # aside); the always-on core lane uses the gentler ``_core_floor`` and only
    # *rotates* which core concepts show when more qualify than the cap -- a core
    # belief is never suppressed out of contention. ``_state_cap`` bounds the
    # persisted map. Salience + spreading-activation land in later L23 passes.
    concept_surfacing_habituation_enabled: bool = True
    concept_surfacing_habituation_window_turns: int = 4
    concept_surfacing_habituation_floor: float = 0.35
    concept_surfacing_core_habituation_floor: float = 0.8
    concept_surfacing_state_cap: int = 300
    # L38 earned standing -- relationship-local performance prior for
    # flex/activation concept surfacing. Recomputed off-turn from L37 and
    # cached in kv_meta; cold/missing evidence remains neutral.
    concept_surfacing_standing_enabled: bool = True
    concept_surfacing_standing_window_days: int = 90
    concept_surfacing_standing_min_settled: int = 4
    concept_surfacing_standing_prior_strength: float = 10.0
    concept_surfacing_standing_floor: float = 0.35
    concept_surfacing_standing_ceiling: float = 1.0
    concept_surfacing_standing_refresh_seconds: int = 3600
    concept_surfacing_standing_state_cap: int = 1000
    # L27 core-lane rationale clause. When on, an always-on *pinned* concept
    # may carry a compact "why" clause (its stored rationale, trimmed to
    # ``rationale_max_chars`` on a word boundary) after its grounding, so the
    # ever-present beliefs read as grounded. Applies to pinned concepts only,
    # so the turn-relevant fill stays token-lean.
    concept_surfacing_core_rationale_enabled: bool = True
    concept_surfacing_rationale_max_chars: int = 180
    # L28 user-profile composition. The profile block leads with the
    # ``subject=user`` identity + value concepts (upstream source of truth)
    # before the SQLite profile fields. ``profile_concept_max_lines`` caps how
    # many concept bullets lead the block; ``profile_concept_min_confidence``
    # is the confidence bar a concept must clear to appear there. Set the cap
    # to 0 to disable the concept lead entirely (pure SQLite profile).
    #
    # L39 lowered the cap from 10 to 4 on measured numbers rather than taste
    # (L28m). This is the one concept surface with **no rotation at all** --
    # it sits in the T0 cache prefix, so giving it a habituation read would
    # make it a third volatile T0 block and break the prefix ladder. At 10
    # lines it was ~620 tokens of identical always-on assertion every turn
    # (concept labels are full sentences, ~60 tokens each) drawn from 170
    # eligible rows, and because the profile claims *first*, those 10 also
    # pre-empted two thirds of the 15-slot core lane -- which does rotate,
    # and which now carries the openness reserve. Releasing six of them is
    # the cheap version of the repetition fix: the same beliefs still reach
    # the prompt, through the lane that rests them.
    profile_concept_max_lines: int = 4
    profile_concept_min_confidence: float = 0.5
    # L23 cognitive surfacing -- emotional / recent-change salience. A concept
    # with a sharp recent lifecycle event (contradicted, plasticity_shift,
    # revived, promoted) gets an intrusion bump on the turn-relevant lane, so a
    # freshly-changed belief surfaces even at moderate cosine, fading over the
    # per-kind ``salience_halflife_days``. ``_event_scan`` bounds how many recent
    # events are scanned per turn to build the per-concept charge map. Only kinds
    # with a non-zero ``salience`` weight (boundary, affective) are affected.
    concept_surfacing_salience_enabled: bool = True
    concept_surfacing_salience_event_scan: int = 120
    # L23 cognitive surfacing -- spreading activation (associative priming). The
    # turn's hot topic clusters + the directly-relevant concepts "seed" the
    # graph; their shared-cluster neighbours (and, once meta concepts exist,
    # concept->concept references) are pulled into the candidate pool with an
    # additive activation boost, so a concept associated with what's being
    # discussed can surface even at low direct cosine. ``_seed_cap`` bounds how
    # many seed concepts expand; ``_max`` caps the activated neighbours added.
    concept_surfacing_activation_enabled: bool = True
    concept_surfacing_activation_seed_cap: int = 4
    concept_surfacing_activation_max: int = 4
    # L32 concept importance -- the second strength axis, distinct from
    # confidence. Each concept gets a derived [0, 1] stake (its kind's prior,
    # lifted by the emotional charge of the topics it is grounded in) which
    # multiplies the turn-relevant score, so an important-but-shakier belief
    # can outrank a trivial-but-certain one. Never stored and never applied
    # to the T0 profile lane -- see ``app/core/concepts/concept_importance.py``.
    # ``_strength`` is the whole tilt: 0.0 reproduces the pre-L32 ranking
    # exactly, 0.4 spans roughly x0.92 (taste) to x1.16 (boundary).
    # ``_affect_lift`` caps how far a fully-charged topic can carry a concept
    # above its kind prior; ``_affect_min_samples`` is the per-cluster
    # evidence bar below which an affect reading is ignored as noise.
    concept_importance_enabled: bool = True
    concept_importance_strength: float = 0.4
    concept_importance_affect_lift: float = 0.5
    concept_importance_affect_min_samples: int = 3
    # How many cosine neighbours the flex lane fetches per rendered slot.
    # Importance can only re-rank what cosine brought back, so this has to
    # be wider than the render cap or an important concept just outside the
    # top few never gets a chance to be promoted. ``nearest`` scores every
    # active concept in one matmul and slices, so depth is nearly free.
    concept_surfacing_overfetch: int = 5
    # L21 cold-start / anti-premature guard. Nothing is proposed or
    # surfaced while the topic graph is too sparse to support an
    # abstraction: synthesis is skipped (a manual ``force`` run still
    # works) and the lifecycle engine promotes candidates against a
    # *stricter* bar until the graph matures. ``min_clusters`` is the
    # cluster-count floor; ``min_history_days`` is how much calendar
    # history must exist first (belt-and-braces with the cluster floor).
    # ``promote_young_*`` are the tightened promotion thresholds applied
    # only while immature.
    concept_min_clusters: int = 6
    concept_min_history_days: float = 3.0
    concept_promote_young_min_sources: int = 3
    concept_promote_young_min_confidence: float = 0.72
    # L5 surfacing. The T1 concept_block renders at most
    # ``surface_max_items`` active user-identity concepts, and only those
    # whose confidence clears ``surface_min_confidence`` (kept above the
    # lifecycle dormant floor so nothing shaky is ever asserted).
    # L5 concept surfacing moved to the unified context budget below
    # (``context_budget_concept_*``): turn-relevance scored + budgeted,
    # replacing the old top-N-by-confidence ``concept_surface_*`` knobs.
    # ── Unified context budget (the T3 ``relevant_context`` region) ─────
    # One turn-relevance-scored region that surfaces a variable mix of
    # memories + topic clusters + concepts under a single shared token
    # budget, reserved *before* history is packed. The budget is a fraction
    # of the context window, absolute-capped, and clamped so it never eats
    # the protected history floor -- so it auto-scales from a 64k local
    # model up to a big cloud window. Per-source floors/caps/weights/
    # min-relevance tune the mix. Replaces the old memory_block (30% clip),
    # interest_map_block (top-5-by-size), and concept_block
    # (top-3-by-confidence). See ``docs/context-budget.md``.
    context_budget_enabled: bool = True
    context_budget_fraction: float = 0.15
    context_budget_max_tokens: int = 4096
    context_budget_min_tokens: int = 256
    context_budget_history_floor_tokens: int = 1024
    context_budget_memory_pool_k: int = 18
    context_budget_memory_floor: int = 1
    context_budget_memory_cap: int = 8
    context_budget_memory_weight: float = 1.0
    context_budget_memory_min_relevance: float = 0.0
    context_budget_cluster_floor: int = 0
    context_budget_cluster_cap: int = 3
    context_budget_cluster_weight: float = 0.9
    context_budget_cluster_min_relevance: float = 0.30
    context_budget_concept_floor: int = 0
    context_budget_concept_cap: int = 3
    context_budget_concept_weight: float = 1.1
    context_budget_concept_min_relevance: float = 0.30
    # L27 always-on *core* lane: up to ``core_cap`` high-confidence concepts
    # are pinned into the region every turn regardless of turn relevance (who
    # the user is, what they + Aiko value, how she wants to behave). Which
    # kinds participate is declared per-kind in the ``ConceptKind`` registry
    # (``core_always_on`` + an optional per-kind ``core_min_confidence`` bar);
    # ``core_min_confidence`` here is the *global fallback* bar for kinds that
    # don't set their own. The lane is balanced across kinds + subjects so no
    # one kind crowds out the others, bypasses the concept cap + min-relevance,
    # and enriches on top of the turn-relevant picks. Set ``core_cap = 0`` to
    # disable it. (Legacy ``context_budget_identity_*`` keys still parse.)
    context_budget_core_cap: int = 2
    context_budget_core_min_confidence: float = 0.75
    # The **openness reserve** on that lane. Only ``core_always_on`` kinds are
    # eligible for the core lane, and today those are identity, value,
    # boundary and generalization -- two anchors and two guides, with no
    # ``generative`` kind among them. So however wide ``core_cap`` is set, not
    # one pinned concept can be an aspiration, a taste, a pursuit or a
    # tension: the lane is structurally incapable of carrying something that
    # could move. This keeps ``openness_slots`` of the cap for the strongest
    # generative-role concept instead, drawn from the kinds the lane cannot
    # otherwise reach. An unfillable slot falls back to the ordinary lane, so
    # the reserve never costs a pin it cannot use. ``0`` restores the
    # guides-and-anchors-only lane exactly.
    concept_core_openness_slots: int = 2
    # The bar a reserved pick must clear. Pinning a half-formed aspiration
    # into every single turn is worse than pinning nothing, so this sits well
    # above the lifecycle's dormant floor without demanding the very high
    # settledness the guide kinds are held to (value 0.85, boundary 0.8) --
    # the point of the reserve is that something unfinished gets a seat.
    concept_core_openness_min_confidence: float = 0.5
    # The **generative floor** on the per-turn flex lane. That lane is tilted
    # rather than closed: any kind can reach it, but ``surface_score`` ends in
    # ``importance_factor``, which at the default strength is x1.16 for
    # boundary against x0.92 for taste -- a ~26% head start on every
    # comparison. When the tilt wins completely and a turn's pick contains no
    # generative concept at all, this swaps the weakest selected *guide* for
    # the strongest available generative one. A floor rather than a lower
    # ``concept_importance_strength`` on purpose: the tilt is usually right,
    # and this only fires in the one case where it isn't. ``0`` disables.
    concept_flex_generative_floor: int = 1
    # ── L45 gate tuning ────────────────────────────────────────────────
    # Most thresholds in this file were set by hand, against one person's
    # graph, and several turned out to be unreachable on it: the taste bar sat
    # above what any topic cluster could score, so the pass minted nothing for
    # five weeks. A constant cannot be right for two relationships with
    # different concept populations, so the L45 worker measures the live
    # distribution and solves each calibratable gate against a *declared
    # intent* ("admit roughly this share", "leave a pool this many times the
    # cap") instead. Learned values and the statistics behind them live in
    # ``data/tuning/concept_gates.json``; anything set explicitly in
    # ``config/user.json`` always wins, and no background pass ever edits that
    # file. See ``app/core/concepts/gate_tuning.py`` for the registry and
    # which gates are cleared to move.
    concept_gate_tuning_enabled: bool = True
    # The scheduler heartbeat, *not* how often the work happens. The idle
    # scheduler admits an over-budget worker only once it is three heartbeats
    # overdue, so a 24h heartbeat on a machine that sleeps overnight risks a
    # three-day gap; a 6h heartbeat with a daily internal cadence key gets the
    # same once-a-day work with an 18h worst case.
    concept_gate_tuning_heartbeat_seconds: int = 21600
    concept_gate_tuning_cadence_seconds: int = 86400
    # Pairs drawn for the similarity-distribution sample. Exhaustive
    # comparison is quadratic (~420k pairs at 900 actives) while all the
    # cosine gates need is the distribution's *shape*, which a random sample
    # estimates fine -- and successive runs draw fresh pairs, so the picture
    # sharpens without any one run paying for it. ``0`` skips the sample.
    concept_gate_tuning_cosine_pairs: int = 4000
    # ── Worker concept diets ───────────────────────────────────────────
    # What a *background worker* gets to think with. Each consumer declares a
    # diet (kinds, subject, appetite) in ``app/core/concepts/concept_diets.py``
    # and the budget below sizes it: ``min(fraction * worker_ctx, max_tokens)``
    # scaled by the diet's weight, floored at ``min_tokens``.
    #
    # The fraction and the cap are not redundant. A concept renders at roughly
    # 15-20 tokens, so on a 64k worker route even 6% is hundreds of concepts --
    # likely more than the store holds, which would make every diet quietly
    # mean "all of them". The fraction is what protects a *small* worker
    # window; the cap is what actually sizes the section on a large one.
    concept_diet_token_fraction: float = 0.06
    concept_diet_max_tokens: int = 600
    concept_diet_min_tokens: int = 150
    # L30a hypothesis lane: the *tentative* register. Every other concept
    # lane reads ``status="active"`` only, which hides a candidate rather
    # than merely hedging it -- yet that is exactly what a mind holds as
    # "I think X might be true, but I'm not sure." This is its own budget
    # source (rendered last) so open questions can never crowd out the
    # beliefs Aiko has actually earned. The cap was 1 by design -- two
    # simultaneous "I'm wondering whether..." lines read as an interview.
    # Phase B raised it to 2 *only* because the lane now reads two
    # origins and offers at most one candidate per origin
    # (``hypothesis_lane.one_per_origin``): a grounded open question and
    # an invented one are different registers, so the pair does not read
    # as an interview the way two grounded questions would. Set it back to
    # 1 and the two origins compete for one slot, which is a coherent
    # (quieter) configuration rather than a broken one.
    hypothesis_surfacing_enabled: bool = True
    context_budget_hypothesis_floor: int = 0
    context_budget_hypothesis_cap: int = 2
    context_budget_hypothesis_weight: float = 0.7
    context_budget_hypothesis_min_relevance: float = 0.35
    # Eligibility, both calibrated against a real 261-candidate pool
    # rather than guessed -- see ``app/core/concepts/concept_hypothesis.py``
    # for the measurements. ``min_unsettled`` sits just above 0.20, which
    # is precisely where a twice-grounded, fully-confident belief scores,
    # so the ~84 candidates that are merely waiting out the promotion age
    # floor stay out of the register. ``min_sources`` drops candidates with
    # no evidence edge at all: they score *highest* on unsettledness
    # exactly because nothing supports them, so without this floor the lane
    # leads with bare LLM hunches.
    hypothesis_min_unsettled: float = 0.22
    hypothesis_min_sources: int = 1
    # L30b: cadence + gating for turning one of those open questions into
    # an actual ask. The worker keeps the shelf stocked during quiet
    # windows; the cue policy governs how often one may surface.
    concept_hypothesis_interval_seconds: int = 1800
    concept_hypothesis_max_per_run: int = 1
    # Gap-path threshold, mirroring ``forward_curiosity_min_gap_hours``.
    concept_hypothesis_min_gap_hours: float = 4.0
    # ...and the extra bar the gap path alone must clear. Raising a belief
    # about someone out of a *lull* is much heavier than raising it while
    # already on the subject, so only hunches that matter earn that. 0.55
    # sits just above the neutral 0.5 an unaffected concept scores, so the
    # gap path needs a positive importance signal rather than merely the
    # absence of a negative one.
    concept_hypothesis_gap_min_importance: float = 0.55
    # L30c: how hard a denial hits. Deliberately the same 0.25 the L9
    # detector uses rather than something harsher -- "not really" is very
    # often a *correction* carrying better information ("it's more that I
    # hate driving"), and the adjudicator's separate CORRECT verdict keeps
    # that near-miss refinable instead of killing it.
    concept_hypothesis_deny_penalty: float = 0.25
    # Cosine floor for the *semantic* half of the echo gate that decides
    # whether a reply is about the belief at all. Only consulted for long
    # replies -- anything short goes straight to the adjudicator, since
    # "yeah, kind of" is the archetypal answer to a hunch and shares no
    # words with it. Low on purpose: this gate separates "answering me"
    # from "talking about something else", and being strict here silently
    # discards real answers.
    concept_hypothesis_answer_threshold: float = 0.45
    # L30 Phase B: the invented layer. These govern the ``hypotheses``
    # table, which is *not* the concept graph and does not share its
    # tuning -- see ``docs/hypotheses.md``.
    #
    # Cadence is deliberately slower than the ask worker's: inventing a
    # guess is a real LLM call and the shelf it stocks is small.
    hypothesis_invention_interval_seconds: int = 5400
    hypothesis_invention_max_per_run: int = 2
    # Target stock of live (open or supported) rows. This is the cap L32
    # warned the concept graph could grow past unchecked; here it is a
    # hard number because nothing prunes inventions by decay -- an
    # untested guess is not less plausible next month, just staler.
    hypothesis_max_open: int = 12
    # Cosine at or above which a proposal is "she already wondered this".
    # Higher than the concept dedupe bar (0.86) on purpose: rejecting a
    # guess costs one wasted proposal, while over-rejecting makes the
    # layer sterile, so the two errors are not symmetric and this one
    # leans toward letting near-neighbours through.
    hypothesis_min_novelty: float = 0.88
    # ...and the separate bar against the *concept* graph. Lower, because
    # the failure it prevents is worse: "speculating" about something she
    # already believes is not a duplicate wondering, it is Aiko forgetting
    # what she knows, out loud.
    hypothesis_concept_novelty: float = 0.82
    # TTL for a row that was never put to the user. Two weeks of quiet is
    # long enough to call a guess stale without discarding one that simply
    # had no fitting moment yet.
    hypothesis_ttl_hours: float = 336.0
    # Graduation bar. Two *independent* confirmations for something
    # invented from nothing, so one polite "yeah, sure" cannot turn a
    # fancy into part of what Aiko knows about the user.
    hypothesis_graduate_min_support: int = 2
    hypothesis_graduate_min_credence: float = 0.7
    # How much one answer moves credence. Symmetric, unlike the concept
    # side: there is no evidence graph underneath to make a confirmation
    # cheaper than a denial, so both are worth the same step.
    hypothesis_credence_step: float = 0.2
    # L9 living beliefs. Counter-evidence lowers an active identity
    # concept's confidence and can step it into a revivable
    # ``contradicted`` status (distinct from a faded ``dormant``). The L3
    # lifecycle worker stays the single writer; a read-only
    # ``ConceptContradictionDetector`` reuses the F5 three-tier gate
    # (cosine band -> ``classify_pair`` -> LLM YES/NO for borderline) to
    # find memories that disprove a belief. Checks ride L3's rolling
    # batch: at most ``contradiction_batch_size`` *active* concepts are
    # checked per tick (rotating via ``last_lifecycle_at``), each pulling
    # up to ``max_candidates`` near memories inside the
    # ``[similarity_min, similarity_max)`` cosine band. A confirmed
    # contradiction applies a plasticity-damped ``penalty``; once
    # confidence falls below ``contradicted_confidence_floor`` the concept
    # flips to ``contradicted``. LLM spend is bounded separately by the
    # agent-side ``concept_contradiction_per_hour/day_cap`` limiter.
    concept_contradiction_enabled: bool = True
    concept_contradiction_similarity_min: float = 0.6
    concept_contradiction_similarity_max: float = 0.95
    concept_contradiction_penalty: float = 0.25
    concept_contradicted_confidence_floor: float = 0.4
    concept_contradiction_batch_size: int = 20
    concept_contradiction_max_candidates: int = 6
    # L15 belief revision. When a belief flips to ``contradicted`` (L9),
    # the doubt flows back down to the memories that supported it: the L3
    # worker runs the read-mostly ``ConceptBeliefReviser`` over the
    # concept's ``evidence`` memories and arbitrates, per memory, one of
    # (a) inaccurate -> lower confidence, (b) superseded -> reclassify to
    # ``past_event`` + a fresh ``relevance_until``, (c) fine -> no write.
    # The cheap ``classify_pair`` gate keeps the LLM off compatible
    # memories; the 3-way arbitration is bounded per tick
    # (``batch_size`` concepts x ``max_evidence`` memories) and by the
    # agent-side ``concept_belief_revision_per_hour/day_cap`` LLM limiter.
    # ``confidence_penalty`` is the damped (a) step, floored at
    # ``confidence_floor`` (a concept never zeroes an observation);
    # ``superseded_relevance_days`` is the (b) grace window before the
    # stale fact slides out of normal RAG. L3 stays the single writer of
    # concept state; the reviser only writes memory state (like F1 / F5).
    concept_belief_revision_enabled: bool = True
    concept_belief_revision_batch_size: int = 5
    concept_belief_revision_max_evidence: int = 6
    concept_belief_revision_confidence_penalty: float = 0.2
    concept_belief_revision_confidence_floor: float = 0.2
    concept_belief_revision_superseded_relevance_days: float = 7.0
    # L2 near-duplicate consolidation. Creation-time dedup keeps anything at
    # / above the dedup cosine from splitting into two rows; this idle worker
    # is the retroactive fix for paraphrase twins that land just *below* that
    # bar and accumulate in the ``active`` set. Each tick stacks the active
    # set once and finds every same-``(subject, kind)`` pair over
    # ``merge_cosine``; a pair over ``auto_merge_cosine`` is fused outright,
    # and below it the pair is a *candidate* an LLM adjudicates (same
    # belief? paraphrase / subset), where only a ``same`` verdict merges
    # (folding the weaker row's evidence into the stronger via
    # ``ConceptStore.merge_into``). Never mutates confidence / plasticity /
    # status -- the stronger row always survives, so the L3 engine stays the
    # single writer. LLM spend is bounded by the agent-side
    # ``concept_consolidation_per_hour/day_cap`` limiter.
    concept_consolidation_enabled: bool = True
    concept_consolidation_interval_seconds: int = 900
    # Now a cap on *pairs acted on per run*, not on seeds scanned: discovery
    # became a single global scan (L46), so the whole backlog is visible
    # every tick and this is what keeps one tick from trying to work all of
    # it. Worst-first ordering means the cut falls on the least-similar
    # pairs, which are the ones that can wait.
    concept_consolidation_batch_size: int = 40
    # Was 0.88, which admitted only 12 candidate pairs across a month of
    # real use -- barely wider than the (never-firing) creation-time bar,
    # so the retroactive fix had almost nothing to fix. 0.84 sees ~10x
    # that. Widening is cheap here precisely because the LLM adjudicates:
    # a false candidate costs one bounded call and a negative-cache entry,
    # whereas a missed twin is a permanent extra row. Below ~0.82 the
    # pairs stop being restatements and start being different subjects
    # sharing a sentence template, which is noise the adjudicator would
    # have to reject over and over.
    concept_consolidation_merge_cosine: float = 0.84
    # L46: the bar above which a pair merges with no LLM adjudication at
    # all. **Disabled by default** (1.0 = adjudicate all the way up), which
    # is the opposite of how L46 was planned, so the reasoning matters.
    #
    # The plan was to set this to ``concept_dedupe.DEDUPE_COS`` (0.86) on
    # the argument that the creation-time guard fuses at that cosine
    # without asking anyone, so a pair reaching it *after* birth is the
    # same belief by the same measurement. That argument is wrong, and
    # asymmetrically so: at creation a false positive merely reinforces an
    # existing row, while here it *destroys* a distinct belief. Hand-reading
    # all 18 above-bar pairs in the live graph found 2 genuine twins and 14
    # template collisions -- including the highest-cosine pair of the set at
    # 0.900 ("reflecting on relationship depth energizes Jacob" against
    # "playful anticipation and lighthearted connection energize Jacob").
    # Token overlap does not separate the two groups either: the twins span
    # Jaccard 0.14-0.52, straddling the collisions' 0.07-0.27. On this data
    # the embedding is reading the sentence template, and no cheap proxy
    # rescues it -- only the adjudicator can tell them apart.
    #
    # Kept as a knob rather than deleted because a graph whose labels are
    # less templated could reasonably enable it. Raise it deliberately, and
    # dry-run before trusting it.
    concept_consolidation_auto_merge_cosine: float = 1.0
    # ── L31: what a concept may accept as evidence ────────────────────
    # Creation is gated (a new concept must clear its kind's min_sources /
    # min_chain / directional bars) but *reinforcement* was not gated at
    # all: ``resolve_reinforces`` checked only that the id the LLM named
    # appeared in the list of 40 it was shown, and every cited source was
    # then attached with no similarity check. Two shapes grew out of that,
    # and they need two different bars -- see
    # ``concept_evidence_admission`` for the full measurement.
    #
    # (1) Contamination, which the cosine floor catches. One
    # ``aspiration/user`` row ("deepening emotional and physical intimacy
    # with Aiko...") reached 97 sources including "Jacob really enjoyed
    # Chainsaw Man's opening song" and "organizing the snack stash" --
    # evidence for something else that happened to be the nearest label on
    # the shown list. Over all 6091 live evidence edges the cosine between
    # a source and the label it supports runs p1 0.324, p5 0.384, p50
    # 0.574, so 0.35 refuses 2.2% of the existing stock while catching
    # every piece hand-read as wrong on that row (0.243, 0.311, 0.328)
    # against its genuine evidence at 0.60-0.68. 0.40 refuses 6.7% and
    # 0.45 refuses 15.1%, which is where real spread starts going too.
    # 0.0 disables the check.
    concept_evidence_admission_cosine: float = 0.35
    # (2) Accretion, which only the ceiling catches. One
    # ``ritual/relationship`` row ended up citing 145 of the 158
    # ``shared_moment`` memories in the graph -- 92% -- and none of it is
    # off-topic: a label that vague really is near everything affectionate,
    # and its lowest-cosine evidence still sits at 0.385. 24 is the 99th
    # percentile of ``distinct_source_count`` (p50 4, p90 10, p95 13), so
    # it binds on about one concept in a hundred, and it is deliberately
    # far above where it would *matter*: ``confidence_target`` saturates at
    # its 0.97 cap by 8 distinct sources, so everything past the eighth
    # already bought nothing. No concept can lose confidence or fail a
    # promotion floor by being capped. 0 disables the cap.
    #
    # Both bars are forward-only. They refuse *new* sources and never
    # remove an edge a concept already holds, so rows that grew before the
    # gate existed keep their history and simply stop growing.
    concept_evidence_max_sources: int = 24
    # L25 edge referential integrity. Concept edges (evidence /
    # contradicts) point at memory rows that get deleted, pruned, and
    # merged. Most deletes are reconciled synchronously by the reconciler's
    # delete-listener hook; ``MemoryStore.prune`` batch-deletes *without*
    # firing listeners, so this idle worker is the defence-in-depth that
    # garbage-collects orphaned edges (memory endpoint no longer exists)
    # and recomputes the affected concepts' edge-derived evidence counts.
    # It runs infrequently over a bounded batch and does no LLM work. L3
    # stays the single writer of confidence / plasticity / status.
    concept_edge_integrity_enabled: bool = True
    concept_edge_integrity_interval_seconds: float = 3600.0
    concept_edge_integrity_batch_size: int = 200
    # ── L17: concept evolution (drift classification + relabelling) ───
    # Master switch for
    # :class:`app.core.concepts.concept_drift_worker.ConceptDriftWorker`,
    # which is the single writer of ``label`` / ``rationale`` (the direct
    # counterpart to L3 owning confidence / plasticity / status) and the
    # only producer of ``concept_learning_events``.
    concept_drift_enabled: bool = True
    concept_drift_interval_seconds: int = 3600
    # Per-run bounds. ``max_concepts`` caps the trajectory reads;
    # ``trace_anchor`` / ``trace_recent`` size the two-ended timeline
    # window (origin + recent movement, so a wall of L17a
    # ``confidence_sample`` rows can't hide a structural move).
    concept_drift_max_concepts: int = 120
    concept_drift_trace_anchor: int = 20
    concept_drift_trace_recent: int = 60
    # Cold-start sweep. The forward pass is watermark-driven, and the
    # watermark advances to the newest event id whether or not every moved
    # concept fitted in ``max_concepts`` -- correct in steady state, but on
    # a store that accumulated history before this worker existed it would
    # classify one page and mark the rest as accounted for. The sweep walks
    # the concept id space once on its own cursor, then retires itself.
    # Its findings cap is larger because it is reading months of movement
    # rather than one interval's worth.
    concept_drift_sweep_enabled: bool = True
    concept_drift_sweep_page: int = 60
    concept_drift_sweep_max_findings: int = 24
    # L17b classifier thresholds. ``min_salience`` is the bar a change
    # must clear to be worth remembering at all; ``min_age_days`` refuses
    # to call anything about a belief younger than this "evolution";
    # ``min_confidence_delta`` is the noise floor for movement that did
    # not change the wording or the status.
    concept_drift_min_salience: float = 0.35
    concept_drift_min_age_days: float = 3.0
    concept_drift_min_confidence_delta: float = 0.15
    concept_drift_max_findings: int = 12
    # Succession band. The upper bound must stay at/below the synthesis
    # dedupe cosine (0.86): at or above it the two beliefs would never
    # have become separate rows, so a pair up there is a consolidation
    # problem rather than an evolution. ``min_overlap`` is the Jaccard
    # floor on shared evidence -- the structural half of the argument,
    # since two labels can be near in embedding space by coincidence but
    # two beliefs resting on the same remembered moments cannot.
    concept_drift_succession_min_cosine: float = 0.55
    concept_drift_succession_max_cosine: float = 0.86
    concept_drift_succession_min_overlap: float = 0.25
    concept_drift_succession_window_days: float = 120.0
    # Relabelling. When a proposal folds into an existing concept but says
    # it better, the drift worker rewrites the stored wording in place so
    # the concept stays current, and the timeline keeps every wording it
    # has ever held. ``min_cosine`` guards the identity vector: below it
    # the "rewording" is a different claim and must stay a separate
    # concept. ``cooldown_days`` plus the previously-held-label guard
    # (read free off the timeline's label snapshots) stop phrasing churn.
    concept_relabel_enabled: bool = True
    concept_relabel_min_cosine: float = 0.80
    concept_relabel_cooldown_days: float = 21.0
    concept_relabel_max_per_run: int = 3
    concept_relabel_scan_limit: int = 40
    concept_drift_relabel_min_tokens: int = 1
    # L17e: how salient a change must be to be offered to the rare T6
    # reflection, and how many are held in the pending snapshot.
    concept_reflection_min_salience: float = 0.6
    concept_drift_pending_cap: int = 3
    # A belief revision is an intimate thing to volunteer, so the T6 slip
    # needs warmth to land and an opening to land in, and it should stay
    # genuinely rare -- a month between them, on top of the per-change
    # watermark and the once-per-conversation limit.
    concept_reflection_min_axes: float = 0.3
    concept_reflection_cooldown_days: float = 30.0
    # ── L17f: the evolution diary ─────────────────────────────────────
    # One short first-person paragraph per period about how Aiko's
    # understanding moved, composed only from the ``because`` clauses L17b
    # already wrote. ``min_events`` is the anti-filler floor: below it the
    # period writes nothing AND leaves its events pending, so two thin
    # weeks can still add up to one entry worth reading. The cooldown is
    # spent even when the model returns nothing, so an unproductive period
    # costs a period rather than looping on the same material.
    evolution_diary_interval_seconds: int = 86400
    evolution_diary_min_events: int = 3
    evolution_diary_min_salience: float = 0.45
    evolution_diary_cooldown_days: float = 7.0
    # ── L17d: self-correction meta-concepts ───────────────────────────
    # A rule about how Aiko works, learned from several of her own
    # corrections landing for the same reason. The floor counts distinct
    # *beliefs*, not events: three corrections to one belief is her
    # wobbling on one thing, while the same reason arriving from three
    # different beliefs is a habit. ``min_span_days`` keeps a single
    # afternoon's mood from reading as a tendency, and the cooldown is the
    # anti-oscillation lever -- she should not be able to rewrite her
    # working strategy weekly however much history accrues.
    # (Prefixed ``concept_`` because the bare ``self_correction_*`` names
    # already belong to K38's in-reply "I got that wrong" cue.)
    concept_self_correction_evidence_floor: int = 3
    concept_self_correction_min_span_days: float = 7.0
    concept_self_correction_min_salience: float = 0.5
    concept_self_correction_similarity: float = 0.55
    concept_self_correction_cooldown_days: float = 14.0
    concept_self_correction_max_events: int = 200
    concept_self_correction_max_rules: int = 2
    # L4 cluster co-activation. Which topic clusters "light up together"
    # (share a conversation session, by default). ``min_pair_support`` is
    # how many buckets two clusters must co-occur in before the pair
    # counts; ``min_strength`` is the Jaccard floor (co-occurrence /
    # union) that keeps a pair; ``max_modes`` / ``max_reps_per_mode`` cap
    # the returned connected-component groups; ``quiet_min_days`` is how
    # stale a cluster must be to be offered as the "meanwhile Y has gone
    # quiet" contrast in the L5 block.
    coactivation_min_pair_support: int = 2
    coactivation_min_strength: float = 0.25
    coactivation_max_modes: int = 4
    coactivation_max_reps_per_mode: int = 4
    coactivation_quiet_min_days: float = 10.0
    # K11: pre-thought / counterfactual worker cadence. A tick is one
    # question-generation LLM call plus up to ``pre_thought_max_per_run``
    # in-persona draft calls, so an hour between successful runs is
    # plenty; the worker also ``is_ready=False``s when the pre-thought
    # store is at ``pre_thought_max_active``, making the cadence a
    # ceiling not a floor.
    pre_thought_interval_seconds: int = 3600
    # K21: fresh-eyes thread re-summary worker cadence. The is_ready
    # gate already enforces the real triggers (message-interval / age),
    # so this is just how often the idle scheduler bothers to check —
    # hourly is plenty.
    thread_resummary_interval_seconds: int = 3600
    # WorldNoticeWorker cadence + pacing. The worker checks for a freshly
    # user-given item (kv watermark) or a long-enough quiet stretch and
    # primes a single proactive "I noticed my room" nudge. Runs often
    # (default 5 min) because it's cheap and quiet-gated, but a
    # per-fire cooldown (default 1h) plus a daily cap keep the actual
    # nudges rare so she stays subtle rather than chatty. ``ttl`` bounds
    # how long a primed nudge stays fresh before the proactive director
    # drops it unspoken.
    world_notice_interval_seconds: int = 300
    world_notice_cooldown_seconds: int = 3600
    world_notice_daily_cap: int = 4
    world_notice_ttl_seconds: int = 1800
    # K36 IdleAwayActivityWorker cadence + pacing. The worker runs during
    # quiet windows (default every 20 min) and, paced by a per-fire
    # cooldown (default 90 min) + daily cap, performs one small room
    # activity, mutating the world + journaling it. ``min_gap_hours`` is
    # the typed-absence threshold the surfacing provider gates on (only
    # mention "while you were away" after a real gap). ``journal_max``
    # bounds the kv ring of recent activities.
    away_activities_interval_seconds: int = 1200
    away_activities_cooldown_seconds: int = 5400
    away_activities_daily_cap: int = 6
    away_activities_min_gap_hours: float = 4.0
    away_activities_journal_max: int = 8
    # H21 — sleep & overnight rhythm. ``min_gap_hours`` is the shortest
    # absence that can read as a sleep when she returns in the morning band;
    # ``overnight_hours`` is the gap that reads as a sleep at any hour;
    # ``dream_lookback_hours`` bounds how recent a ``[dream]`` reflection must
    # be to get woven into the return cue. Longer gap floor than the ordinary
    # away cue (4h) so a long afternoon out never reads as "I fell asleep".
    sleep_return_min_gap_hours: float = 5.0
    sleep_return_overnight_hours: float = 9.0
    sleep_return_dream_lookback_hours: float = 18.0
    # H14 — fraction of idle beats the worker LLM composes from scratch
    # (open-vocab activity grounded in the live room) instead of the
    # curated weighted templates. 0.0 disables; 1.0 always LLM-composes.
    away_activities_llm_ratio: float = 0.5
    # K91 — episodes. ``episode_ratio`` is the chance an eligible firing
    # plays out as a chain instead of one beat; ``min_gap_seconds`` is how
    # long she must have been left alone for a chain to read as plausible
    # (default 3h, twice the beat cooldown); ``max_beats`` caps the chain
    # so a quiet day doesn't turn into a montage. An episode still costs
    # one beat against the daily cap and one rephrase generation.
    away_activities_episode_ratio: float = 0.35
    away_activities_episode_max_beats: int = 3
    away_activities_episode_min_gap_seconds: int = 10800
    # H17 — idle beats feed the idea machine. ``ratio`` is the fraction of
    # beats that also produce a conversational seed (LLM-composed; needs a
    # worker model). ``daily_cap`` bounds seeds/day; ``max_ring`` bounds the
    # kv ring; ``surface_cooldown`` is the wall-clock floor between surfacing
    # one seed as an inner-life cue.
    idle_seed_ratio: float = 0.25
    idle_seed_daily_cap: int = 3
    idle_seed_max_ring: int = 6
    idle_seed_surface_cooldown_seconds: int = 1800
    # H19 — hobby worker cadence. ``interval`` is the idle-tick cadence;
    # ``advance_min_hours`` paces actual progress so it doesn't climb every
    # tick; ``milestone_every`` advances per takeaway seed; ``max_advances``
    # is when the hobby rotates out (0 disables rotation).
    hobby_worker_interval_seconds: int = 3600
    hobby_advance_min_hours: float = 6.0
    hobby_milestone_every: int = 3
    hobby_max_advances: int = 12
    # H20 — room-evolution cadence. ``interval`` is the idle-tick cadence;
    # ``min_hours`` is the wall-clock floor between actual drifts so the
    # room changes gradually rather than every tick.
    room_evolution_interval_seconds: int = 21600
    room_evolution_min_hours: float = 8.0
    # H15 — needs-driven, richer garden + outdoor life. ``need_dry_days``
    # is the ``days_dry`` threshold at which a plant counts as
    # drought-stressed (pulls a visit forward); ``need_visit_floor_hours``
    # is the minimum gap between two need-driven visits so a thirsty plant
    # can't make her pace the garden every tick. ``relax_ratio`` is the
    # chance a non-need visit is a "sit outside" beat (tea on the pavers,
    # read in the sun) instead of watering chores. ``visit_min/max_minutes``
    # jitter how long she lingers. ``journal_max`` bounds the away-journal
    # ring the garden visit shares with the K36 surfacing provider.
    garden_need_dry_days: float = 2.0
    garden_need_visit_floor_hours: float = 0.75
    garden_relax_ratio: float = 0.3
    garden_visit_min_minutes: float = 4.0
    garden_visit_max_minutes: float = 10.0
    garden_journal_max: int = 8
    # H22 — light outings ("I stepped out for a bit"). A rare away-beat
    # gated to daylight + its own ``cooldown_hours`` + ``daily_cap`` that
    # narrates a short trip out and back (and feeds H17 through the shared
    # idle-seed path). Long cooldown + small cap keep it special.
    outing_cooldown_hours: float = 6.0
    outing_daily_cap: int = 2
    # H16 circadian-settle worker cadence. ``interval`` is how often the
    # scheduler may consider it; ``settle_after`` is how long Aiko's room
    # state must have been static before it drifts her to the time-of-day
    # resting default (so it never fights the livelier away-activity beats).
    circadian_settle_interval_seconds: int = 3600
    circadian_settle_after_seconds: int = 7200
    # H9 away-diary worker cadence. ``interval`` is how often the
    # scheduler may consider it; ``cooldown`` is the wall-clock floor
    # between actual entries (3h default — a diary written too often
    # stops meaning anything); ``daily_cap`` bounds entries per local
    # day; ``min_context_chars`` is the minimum recent-transcript length
    # before there's anything worth reflecting on.
    diary_worker_interval_seconds: int = 1800
    diary_worker_cooldown_seconds: int = 10800
    diary_worker_daily_cap: int = 3
    diary_worker_min_context_chars: int = 80
    # K34 ForwardCuriosityWorker cadence + pacing. The worker runs during
    # quiet windows (default every 30 min) and, paced by a per-fire
    # cooldown (default 1h) + daily cap, drafts one forward question into
    # the ``aiko.forward_curiosity`` kv ring. ``min_gap_hours`` is the
    # typed-absence threshold the surfacing provider gates on (only
    # surface "I've been wondering" after a real gap). ``journal_max``
    # bounds the kv ring of drafted questions.
    forward_curiosity_interval_seconds: int = 900
    forward_curiosity_cooldown_seconds: int = 3600
    forward_curiosity_min_gap_hours: float = 4.0
    forward_curiosity_journal_max: int = 8
    # FollowUpWorker cue ring size (``aiko.follow_up_cues``). Bounds the
    # number of drafted "ask how their plan went" cues kept around.
    follow_up_journal_max: int = 8
    # K70 growth-witness detection thresholds. The worker compares the
    # oldest third of the H3 mood-drift ring against the newest third;
    # ``min_samples`` is the floor before any finding fires (a real
    # multi-week history), ``min_valence_delta`` / ``min_axis_delta`` are
    # how far mood / a relationship axis (comfort, trust) must have risen
    # to read as durable growth, and ``journal_max`` bounds the
    # ``aiko.growth_witness`` cue ring.
    growth_witness_min_samples: int = 10
    growth_witness_min_valence_delta: float = 0.25
    growth_witness_min_axis_delta: float = 0.30
    growth_witness_journal_max: int = 4
    # K71 self-callback. ``min_age_days`` is how old one of Aiko's own
    # self / reflection memories must be before it reads as "a while back"
    # (and stays distinct from K28's recent 24-72h reflections);
    # ``journal_max`` bounds the ``aiko.self_callback`` cue ring.
    self_callback_min_age_days: int = 14
    self_callback_journal_max: int = 4
    # K72 wellbeing concern. ``window_days`` is the multi-day lookback for
    # behavioral signal; ``late_night_min`` distinct small-hours days and
    # ``neglect_min_days`` distinct days with an explicit "haven't slept /
    # eaten" mention each trigger a concern. ``rough_run`` / ``rough_
    # threshold`` gate the H3 low-stretch fallback (longer + deeper than
    # H3 sustained_low so K72 reads as care, not mood narration).
    # ``journal_max`` bounds the ``aiko.wellbeing_concern`` cue ring.
    wellbeing_concern_window_days: int = 7
    wellbeing_concern_late_night_min: int = 3
    wellbeing_concern_neglect_min_days: int = 2
    wellbeing_concern_rough_run: int = 5
    wellbeing_concern_rough_threshold: float = -0.25
    wellbeing_concern_journal_max: int = 4
    # K73 shared-ritual formation. ``window_days`` is the multi-week
    # lookback; a ``(weekday, bucket, shape)`` slot becomes a ritual once
    # it recurs in ``min_weeks`` distinct ISO weeks AND ``min_share`` of
    # the window's weeks. ``min_messages`` is the floor before any naming;
    # ``max_active`` caps the stored list.
    shared_ritual_window_days: int = 56
    shared_ritual_min_weeks: int = 3
    shared_ritual_min_share: float = 0.34
    shared_ritual_max_active: int = 6
    shared_ritual_min_messages: int = 30
    # K26 voice adoption. A catchphrase that started as *his* becomes
    # adoptable once it has been in the registry ``min_age_days``; at most
    # one adoption per ``min_days_between``, ``max_adopted`` active at a
    # time, ``max_rendered`` named in the prompt block. The defaults are
    # deliberately slow — the beat only works if it's invisible per
    # session and obvious over months.
    voice_adoption_min_age_days: float = 14.0
    voice_adoption_min_days_between: float = 10.0
    voice_adoption_max_adopted: int = 3
    voice_adoption_max_rendered: int = 2
    # K76 flashbulb encoding. At memory-write time the live AffectState
    # arousal + any active K57 episode intensity fold into a [0,1] charge;
    # ``flashbulb_max_boost`` is the most salience a fully-charged moment
    # adds, ``arousal_weight`` / ``episode_weight`` weight the two inputs,
    # ``arousal_neutral`` is the resting arousal below which nothing
    # counts. Off → memory salience is encoding-affect-blind (legacy).
    flashbulb_enabled: bool = True
    flashbulb_max_boost: float = 0.35
    flashbulb_arousal_weight: float = 0.6
    flashbulb_episode_weight: float = 0.7
    flashbulb_arousal_neutral: float = 0.4
    # K43 PromiseFollowthroughWorker cadence + pacing. The worker runs
    # during quiet windows (default every 30 min). ``min_age_hours`` is
    # how long an assistant promise must sit open before the cue arms
    # (closing the loop 5 minutes later reads robotic, not attentive).
    # ``cooldown_hours`` paces consecutive cues so a backlog of old
    # promises doesn't turn every turn into loop-closing.
    # ``drop_after_days`` ages out promises nobody followed up on (a
    # 3-week-old "I'll check" resurfacing is weirder than letting it
    # go). ``fulfil_min_overlap`` is the content-word overlap a reply /
    # finished task must share with the promise body to count as
    # fulfilled.
    promise_followthrough_interval_seconds: int = 900
    promise_followthrough_min_age_hours: float = 4.0
    promise_followthrough_cooldown_hours: float = 6.0
    promise_followthrough_drop_after_days: float = 14.0
    promise_fulfil_min_overlap: int = 3
    # ── K38: self-correction cue thresholds ───────────────────────────
    # ``min_confidence`` is the floor a fact/preference memory must clear
    # to count as a durable claim worth correcting toward. ``min_overlap``
    # is the number of shared content words a reply sentence and a memory
    # must have before the contradiction heuristic runs (lexical
    # shortlist). ``max_candidates`` caps the candidate pool per turn.
    # ``cooldown_turns`` is the per-fire suppression window so a single
    # slip doesn't nag every turn.
    self_correction_min_confidence: float = 0.6
    self_correction_min_overlap: int = 2
    self_correction_max_candidates: int = 50
    self_correction_cooldown_turns: int = 3
    # F13 user-correction detector + worker. ``min_confidence`` is the
    # floor a memory must clear to be a correction target (lower than K38's
    # 0.6: a surfaced note the user bothered to correct is worth catching
    # even at middling confidence). ``min_overlap`` / ``max_candidates``
    # bound the pattern-gate candidate pool the same way K38's do.
    # ``interval_seconds`` / ``max_per_run`` pace the off-turn worker;
    # ``concept_penalty`` is the plasticity-damped confidence step applied
    # to a concept whose evidence was corrected; ``confidence`` is what the
    # corrected fact is written at.
    user_correction_min_confidence: float = 0.4
    user_correction_min_overlap: int = 2
    user_correction_max_candidates: int = 50
    user_correction_interval_seconds: int = 45
    user_correction_max_per_run: int = 8
    user_correction_concept_penalty: float = 0.25
    user_correction_confidence: float = 0.9
    # F14 fact-reversal: minimum absolute confidence drop the F1 fact-
    # checker's ``contradict`` verdict must carry before it counts as a
    # genuine reversal worth owning aloud, rather than routine drift (a
    # 0.7 -> 0.65 nudge). Clamped to [0, 0.3]; default 0.25.
    fact_reversal_min_delta: float = 0.25
    # K45 mood inertia: effective-mismatch score (whiplash bonus
    # included) at or above which the one-shot cue arms (floor 0.1),
    # and how many post-turn assessments to skip after a fire so one
    # big mood swing doesn't nag on consecutive turns.
    mood_inertia_mismatch_threshold: float = 0.45
    mood_inertia_cooldown_turns: int = 3
    # Output-token ceiling for the memory extractor's JSON ANSWER (the
    # array we parse) — NOT the reasoning trace. The old hardcoded 512
    # truncated the ``"memories": [...]`` array mid-object on longer
    # transcripts; 1024 comfortably fits the capped answer (≤5 memories,
    # each ≤~120 chars). When ``memory_extractor_think`` is on, the client
    # adds ``ollama.think_num_predict_headroom`` on top of this so the
    # hidden trace gets its own budget and never starves the answer; this
    # value stays the answer budget either way.
    memory_extractor_max_tokens: int = 1024
    # Run the extractor with the model's reasoning trace enabled. The
    # extractor's judgement ("is this durable? what's the right tense /
    # event_time?") is exactly the kind of multi-step call that gets
    # flaky on reasoning models when think is off, so default it ON.
    # Ollama returns the trace in ``message.thinking`` (separate from
    # the JSON ``message.content`` we parse) — it never pollutes the
    # output, it only costs latency + tokens from the budget above.
    memory_extractor_think: bool = True
    # K1: cap on simultaneously-active long-term goals Aiko carries.
    # When :meth:`GoalStore.add_goal` would push past the cap, the
    # oldest un-pinned active goal is archived (its progress history
    # is preserved). Five lines up with the "carrying ~5 things" feel
    # the persona block suggests; bumping past ~7 makes the prompt
    # bullet list noisy and the worker spread thin across too many
    # reflection candidates. Pinned goals do not count against the
    # cap; archived goals never do.
    goal_max_active: int = 5
    # K1: per-goal cap on retained reflection (``goal_progress``)
    # rows. Once the cap is hit the oldest progress row on that goal
    # is pruned each time a new one is appended. The most recent
    # entry is also mirrored into the parent goal's
    # ``metadata.last_progress_note`` so the prompt block stays cheap
    # to render. 12 is roughly two weeks of one-reflection-per-day
    # cadence; lower it for a tighter context budget, raise it for a
    # richer audit trail in the Memory tab.
    goal_max_progress_per_goal: int = 12
    # K1: goal worker tick cadence. The worker's
    # ``is_ready`` predicate fires no more than once per this
    # interval, and the reflection path picks the oldest-touched
    # active goal each turn. One hour gives every active goal a
    # daily-ish reflection at the default ``goal_max_active=5``
    # without ever queueing two ticks in a row. Lower it for a
    # tester loop (e.g. 60 seconds to watch the reflection arrive
    # within a minute); raise it for a calmer cadence.
    goal_reflection_interval_seconds: int = 3600
    # F5: conflicting-memory detector cadence. The all-pairs cosine
    # scan is cheap (NumPy on the in-memory mirror) but the heuristic
    # gate + occasional LLM call adds up, so once an hour is plenty.
    conflict_detector_interval_seconds: int = 1800
    # Cosine similarity band used to short-circuit the candidate
    # filter. Pairs below ``min`` are topically distant (no point
    # checking for contradiction); pairs >= ``max`` are dedupe-likely
    # (the row would already have been merged at write time). The
    # default 0.80-0.92 was chosen so paraphrases sit just above and
    # related-but-distinct claims sit in-band.
    conflict_detector_similarity_min: float = 0.80
    conflict_detector_similarity_max: float = 0.92
    # When the F3 confidence delta between the two halves of a
    # confirmed conflict is at least this big, the worker auto-demotes
    # the loser instead of asking the user. Higher = more cautious
    # auto-resolution; lower = more eager. 0.30 means
    # MemoryExtractor-default (0.7) vs F1-verified (0.95) auto-resolves
    # but two MemoryExtractor rows (both 0.7) always surface to the
    # Conflicts tab.
    conflict_detector_auto_resolve_delta: float = 0.30
    # Caps on the candidate corpus and pair count per tick. The all-
    # pairs loop is O(n^2) on the corpus; ``max_corpus`` keeps that
    # bounded for tens of thousands of memories. ``max_pairs_per_run``
    # caps the heuristic+LLM work per tick so a hot streak of
    # contradictions doesn't burn the per-day LLM budget on one run.
    conflict_detector_max_corpus: int = 1000
    conflict_detector_max_pairs_per_run: int = 50
    # ── K35 personality backlog: memory consolidation worker ─────────
    # Nightly-ish cadence (default 6h so it gets several chances to land
    # in a quiet window per day; caps keep the cost bounded regardless).
    consolidation_interval_seconds: int = 21600
    # Only scratchpad rows created within this many days are scanned —
    # the noisy auto-extracted backlog, not durable long_term anchors.
    consolidation_lookback_days: int = 30
    # Cosine at/above which two same-kind, non-contradicting rows are
    # treated as near-duplicates and fused. Sits just under the 0.92
    # insert-dedupe so it catches the band that escaped write-time
    # merge.
    consolidation_similarity_threshold: float = 0.90
    # O(n^2) corpus cap + per-run cluster cap. ``max_clusters_per_run``
    # bounds the worker-LLM merge calls per tick; ``min_cluster_size``
    # is the smallest group worth merging (2 = a single duplicate pair).
    consolidation_max_corpus: int = 1000
    consolidation_max_clusters_per_run: int = 20
    consolidation_min_cluster_size: int = 2
    # ── K2 personality backlog: theory-of-mind / belief tracking ─────
    # Background inference worker cadence. The worker spends one LLM
    # call per tick to extract beliefs from the last
    # ``belief_worker_lookback_turns`` user turns; once an hour leaves
    # plenty of room between calls without making the model feel
    # forgetful.
    belief_worker_interval_seconds: int = 1200
    # How many recent **user** messages the worker passes to the LLM
    # per extraction. Larger windows give a richer signal but cost
    # more tokens; 12 is enough to span a few conversational beats.
    belief_worker_lookback_turns: int = 12
    # ── K65b: bias the belief worker toward high-mass interests ───────
    # How many of the densest K9 topic clusters (by member count) are
    # folded into the extraction prompt as a "topics the user keeps
    # returning to -- prioritise theory-of-mind here" hint. 0 disables
    # the interest hint without touching the master switch.
    belief_worker_interest_top_n: int = 5
    # On each tick the worker may also nominate up to this many *active*
    # beliefs whose topic sits on one of those high-mass interests for a
    # "still true?" re-check, folded into the SAME LLM call (zero extra
    # spend). Keeps long-lived beliefs on durable interests fresh
    # instead of letting them rot until the 90-day stale sweep.
    belief_worker_reconsider_max: int = 3
    # ── Phase 3c (reworked): context-aware promise extraction worker ──
    # Cadence + context budgets for
    # :class:`app.core.memory.promise_worker.PromiseExtractionWorker`.
    # Frequent by default (every 10 min) because real spend is bounded
    # by the per-hour / per-day caps, not the interval.
    promise_worker_interval_seconds: int = 600
    # How many recent turns (both user and assistant) the worker reads.
    # Promises come from both sides, so unlike the belief worker this
    # keeps assistant lines too.
    promise_worker_lookback_turns: int = 12
    # Max promises persisted per run -- a single noisy window can't
    # flood the store; the next tick picks up anything dropped.
    promise_worker_max_per_run: int = 5
    # Per-message + overall transcript char budgets for the snapshot.
    # Generous so the LLM has enough surrounding context to resolve
    # pronouns/objects into self-contained promises; only truncate to
    # protect the worker-LLM token budget.
    promise_worker_max_msg_chars: int = 2000
    promise_worker_max_transcript_chars: int = 8000
    # Gap-detector thresholds. The mood pass surfaces a gap when
    # ``|val_pred - val_obs|`` exceeds ``belief_gap_valence_threshold``,
    # ``|aro_pred - aro_obs|`` exceeds ``belief_gap_arousal_threshold``,
    # or the recomputed valence band crosses into opposing territory.
    # Tuned conservatively so a small affect drift can't pelt Aiko
    # with "am I reading this wrong?" beats every turn.
    belief_gap_valence_threshold: float = 0.30
    belief_gap_arousal_threshold: float = 0.25
    # Window the mood-gap pass considers. Predictions older than this
    # are skipped on the mood pass (they age out via the stale sweep
    # instead). Opinion beliefs have no recency window because a long-
    # held belief can still be contradicted by a fresh message.
    belief_recent_window_hours: int = 24
    # Active beliefs untouched (no check, no update) for this many
    # days are bulk-flipped to ``stale`` on the gap detector's first
    # sweep of the tick. Stale rows stay in the table as audit
    # history but are dropped from future detector passes.
    belief_stale_after_days: int = 90
    # Hard ceiling on ``active`` beliefs per user. The worker prunes
    # the lowest-confidence + oldest active rows down to this cap on
    # every tick so a runaway extraction can't flood the store.
    # Confirmed / contradicted / stale audit rows are kept regardless.
    belief_max_active_per_user: int = 200
    # ── K6 personality backlog: surprise / novelty detector ──────────
    # Size of the rolling centroid window. The detector keeps the
    # last N user-message embeddings (cross-session per user) in an
    # in-memory ring; the centroid is their re-normalised mean.
    # Bigger windows smooth more aggressively, smaller ones react
    # faster to topic pivots. 12 spans a few conversational beats
    # without being so long that a real shift gets averaged away.
    novelty_window: int = 12
    # Minimum ring size before the detector starts emitting a band.
    # Below this we just collect vectors and stay silent so a cold
    # start (or a brand-new install) doesn't fire "this is novel" on
    # the first three turns of every session.
    novelty_warmup_min: int = 3
    # Distance band thresholds. ``distance = 1.0 - cosine`` against
    # the centroid (vectors are unit-norm, so distance lives in
    # ``[0, 2]`` but practical values cluster well below 1.0).
    # Tuned conservatively so small lexical variations (greetings,
    # filler) stay below ``mild`` and only real topic pivots cross
    # ``strong``. Set ``strong < mild`` and the detector falls back
    # to single-threshold behaviour.
    novelty_mild_threshold: float = 0.35
    novelty_strong_threshold: float = 0.55
    # Turns to suppress further novelty signals after a hit. Prevents
    # "you keep saying surprising things" piles when a user runs
    # through several genuinely-new topics in a row. The current turn
    # still contributes to the centroid so the baseline keeps moving.
    novelty_cooldown_turns: int = 2
    # ── K18: topic-stagnation detector thresholds ────────────────────
    # The K18 detector is a pure streak counter over the K6 distance
    # stream -- no embeddings, no rag_store, no per-user state. These
    # knobs only control when a sustained low-divergence streak counts
    # as a "lull". Defaults are conservative on purpose; calibration
    # is best done live and the persona explicitly tells Aiko that
    # *not* hearing the cue is also a signal.
    #
    # Number of distance samples to average before scoring. 6 covers
    # roughly a conversational beat (greeting, two follow-ups, two
    # answers, a recap) so a single tight exchange doesn't fire by
    # itself.
    stagnation_window: int = 6
    # Mean-distance band thresholds. Note the inversion vs K6: lower
    # mean = MORE stagnant, so ``strong < mild``. A 6-turn mean
    # below 0.18 reads as "we've been on this for a bit"; below 0.10
    # reads as "we've been *very* on this". Set ``strong > mild`` and
    # the detector falls back to a single-threshold behaviour using
    # the tighter value.
    stagnation_mild_threshold: float = 0.18
    stagnation_strong_threshold: float = 0.10
    # Turns to suppress further stagnation signals after a hit. The
    # window is longer than K6's because lulls are by nature
    # drawn-out; refiring on consecutive turns is almost never
    # useful, even when the mean stays below threshold.
    stagnation_cooldown_turns: int = 4
    # Turns to keep K18 quiet after a K6 hit. Right after novelty
    # fires the centroid is mid-shift, so distances are noisy for a
    # few turns; waiting a beat avoids the "you just pivoted, but
    # also you've been on this forever" weirdness.
    stagnation_post_novelty_suppression_turns: int = 3
    # F10k: minimum cluster-centroid cosine for the novelty detector to
    # treat a turn as confidently "on" a topic-graph cluster. Below this
    # the turn has no cluster identity and the prior cluster is kept
    # (a transient miss must not read as a topic change). Clamped [0, 1].
    topic_tracking_min_sim: float = 0.30
    # IdleWorkerScheduler tick + quiet gate. Lowering ``wake_seconds``
    # makes workers fire sooner after a quiet period starts but
    # increases idle CPU; ``quiet_threshold`` is how long since the
    # last user activity before the scheduler considers itself idle.
    idle_worker_wake_seconds: float = 60.0
    idle_worker_quiet_threshold_seconds: int = 30
    # P8: per-tick wall-time budget in milliseconds. The scheduler runs
    # as many due workers as fit into this budget per wake-up so the
    # natural typing/speaking gap between turns drains backlog instead
    # of one worker at a time. Anti-starvation always lets the
    # most-overdue worker fire even if its EMA estimate exceeds the
    # remaining budget. Set to a small value (e.g. 500) to approximate
    # the old one-per-tick behaviour; ``max_per_tick`` (0 = unlimited)
    # is a hard cap if you want to clamp tick log volume on heavy
    # backlogs.
    idle_worker_tick_budget_ms: int = 3000
    idle_worker_max_per_tick: int = 0
    # P36: demand-driven scheduling. Workers report a cheap "how much
    # work is pending" pressure via ``demand()``; the scheduler ranks by
    # urgency instead of by age, and splits the per-tick budget into two
    # lanes. ``idle_worker_tick_budget_ms`` above becomes the LLM lane
    # (it was always really a GPU-contention limit, sized for one local
    # Ollama serving both chat and workers); ``compute_budget_ms`` is
    # the lane for workers that touch no LLM and therefore have no GPU
    # to protect.
    #
    # ``pressure_enabled = false`` restores the pre-P36 path exactly:
    # one budget, oldest-first ranking, no probes.
    idle_worker_pressure_enabled: bool = True
    idle_worker_compute_budget_ms: int = 6000
    # Minimum blended pressure/staleness for admission ahead of the
    # heartbeat. Raise to make Aiko lazier about speculative work.
    idle_worker_urgency_threshold: float = 0.35
    # Anti-thrash floor as a fraction of each worker's own interval,
    # floored at one scheduler tick. One ratio serves intervals spanning
    # three orders of magnitude: at wake=15/ratio=0.1 a 30s worker
    # floors at 15s while an 86400s one floors at 2.4h.
    idle_worker_min_interval_ratio: float = 0.1
    # How far the budget may grow as the user's absence deepens.
    # ``1.0`` disables depth scaling.
    idle_worker_depth_max_multiplier: float = 10.0
    # ``auto`` derives chat-vs-worker GPU contention from the route
    # topology. Force ``none`` / ``queueing`` / ``swapping`` when the
    # topology lies -- e.g. a "remote" endpoint that is really your own
    # GPU box, which would otherwise read as split backends and remove
    # the protection.
    idle_worker_contention_override: str = "auto"



def parse_memory_settings(memory_raw: dict[str, Any]) -> "MemorySettings":
    return MemorySettings(
            enabled=bool(memory_raw.get("enabled", True)),
            top_k=max(0, int(memory_raw.get("top_k", 6))),
            score_threshold=max(0.0, min(1.0, float(memory_raw.get("score_threshold", 0.4)))),
            max_memories=max(50, int(memory_raw.get("max_memories", 5000))),
            dedupe_threshold=max(0.5, min(0.999, float(memory_raw.get("dedupe_threshold", 0.92)))),
            restate_threshold=max(
                0.5, min(0.999, float(memory_raw.get("restate_threshold", 0.85))),
            ),
            restate_window_hours=max(
                0.0, float(memory_raw.get("restate_window_hours", 6.0)),
            ),
            extractor_enabled=bool(memory_raw.get("extractor_enabled", True)),
            self_tagged_salience=max(
                0.0, min(1.0, float(memory_raw.get("self_tagged_salience", 0.7)))
            ),
            tiers_enabled=bool(memory_raw.get("tiers_enabled", True)),
            decay_rate_scratchpad=max(
                0.0, min(1.0, float(memory_raw.get("decay_rate_scratchpad", 0.05)))
            ),
            decay_rate_long_term=max(
                0.0, min(1.0, float(memory_raw.get("decay_rate_long_term", 0.02)))
            ),
            decay_rate_archive=max(
                0.0, min(1.0, float(memory_raw.get("decay_rate_archive", 0.0)))
            ),
            revival_coefficient=max(
                0.0, min(1.0, float(memory_raw.get("revival_coefficient", 0.05)))
            ),
            revival_per_hit=max(
                0.0, min(1.0, float(memory_raw.get("revival_per_hit", 0.15)))
            ),
            revival_decay_per_day=max(
                0.0, min(1.0, float(memory_raw.get("revival_decay_per_day", 0.02)))
            ),
            revival_min_word_overlap=max(
                1, int(memory_raw.get("revival_min_word_overlap", 3))
            ),
            semantic_revival_enabled=bool(
                memory_raw.get("semantic_revival_enabled", True)
            ),
            semantic_revival_min_cosine=max(
                0.0,
                min(1.0,
                    float(memory_raw.get("semantic_revival_min_cosine", 0.62))),
            ),
            semantic_revival_per_hit=max(
                0.0,
                min(1.0, float(memory_raw.get("semantic_revival_per_hit", 0.05))),
            ),
            scratchpad_ttl_days=max(
                1, int(memory_raw.get("scratchpad_ttl_days", 14))
            ),
            scratchpad_ttl_min_revival=max(
                0.0,
                min(1.0,
                    float(memory_raw.get("scratchpad_ttl_min_revival", 0.10))),
            ),
            scratchpad_promote_min_age_days=max(
                0, int(memory_raw.get("scratchpad_promote_min_age_days", 7))
            ),
            scratchpad_promote_min_use_count=max(
                0, int(memory_raw.get("scratchpad_promote_min_use_count", 3))
            ),
            scratchpad_promote_min_revival=max(
                0.0,
                min(1.0, float(memory_raw.get("scratchpad_promote_min_revival", 0.3))),
            ),
            archive_demote_idle_days=max(
                1, int(memory_raw.get("archive_demote_idle_days", 180))
            ),
            scratchpad_cap=max(50, int(memory_raw.get("scratchpad_cap", 1000))),
            archive_cap=max(50, int(memory_raw.get("archive_cap", 10000))),
            fade_hedge_enabled=bool(
                memory_raw.get("fade_hedge_enabled", True),
            ),
            faded_salience_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("faded_salience_threshold", 0.20)),
                ),
            ),
            faded_idle_days=max(
                1, int(memory_raw.get("faded_idle_days", 30)),
            ),
            confidence_decay_horizon_days=max(
                1, int(memory_raw.get("confidence_decay_horizon_days", 365)),
            ),
            confidence_decay_floor=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("confidence_decay_floor", 0.3)),
                ),
            ),
            confidence_decay_distant_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "confidence_decay_distant_threshold", 0.5,
                        )
                    ),
                ),
            ),
            opinion_injection_min_cosine=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("opinion_injection_min_cosine", 0.55)
                    ),
                ),
            ),
            opinion_injection_min_user_words=max(
                0,
                int(memory_raw.get("opinion_injection_min_user_words", 4)),
            ),
            opinion_injection_cooldown_turns=max(
                0,
                int(memory_raw.get("opinion_injection_cooldown_turns", 5)),
            ),
            opinion_injection_per_session_cap=max(
                0,
                int(memory_raw.get("opinion_injection_per_session_cap", 3)),
            ),
            opinion_injection_per_hour_cap=max(
                0,
                int(memory_raw.get("opinion_injection_per_hour_cap", 6)),
            ),
            opinion_injection_per_day_cap=max(
                0,
                int(memory_raw.get("opinion_injection_per_day_cap", 30)),
            ),
            # ── L18c: boundary-vs-conversation clash cue ───────────────
            boundary_clash_min_cosine=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("boundary_clash_min_cosine", 0.58)),
                ),
            ),
            boundary_clash_min_user_words=max(
                0,
                int(memory_raw.get("boundary_clash_min_user_words", 4)),
            ),
            boundary_clash_cooldown_turns=max(
                0,
                int(memory_raw.get("boundary_clash_cooldown_turns", 5)),
            ),
            boundary_clash_per_session_cap=max(
                0,
                int(memory_raw.get("boundary_clash_per_session_cap", 3)),
            ),
            # ── K46: stance persistence ───────────────────────────────
            stance_persistence_window=max(
                0,
                int(memory_raw.get("stance_persistence_window", 3)),
            ),
            # ── K63: long-arc callbacks ────────────────────────────────
            long_arc_callback_min_age_days=max(
                1,
                int(memory_raw.get("long_arc_callback_min_age_days", 21)),
            ),
            long_arc_callback_min_cosine=max(
                0.0,
                min(1.0, float(memory_raw.get("long_arc_callback_min_cosine", 0.55))),
            ),
            long_arc_callback_per_session_cap=max(
                0,
                int(memory_raw.get("long_arc_callback_per_session_cap", 1)),
            ),
            long_arc_callback_min_user_words=max(
                0,
                int(memory_raw.get("long_arc_callback_min_user_words", 5)),
            ),
            # ── K28: turning-over picker ──────────────────────────────
            # ``turning_over_min_gap_minutes`` clamped to >= 5 so a
            # misconfigured value can't make the cue fire on every
            # typed turn.
            turning_over_min_gap_minutes=max(
                5.0,
                float(
                    memory_raw.get("turning_over_min_gap_minutes", 90.0)
                ),
            ),
            # ``min_age_hours`` clamped to >= 1; ``max_age_hours``
            # clamped to >= min_age + 1 so the picker window is always
            # non-empty even with a hostile config.
            turning_over_min_age_hours=max(
                1.0,
                float(
                    memory_raw.get("turning_over_min_age_hours", 24.0)
                ),
            ),
            turning_over_max_age_hours=max(
                max(
                    1.0,
                    float(
                        memory_raw.get("turning_over_min_age_hours", 24.0)
                    ),
                )
                + 1.0,
                float(
                    memory_raw.get("turning_over_max_age_hours", 72.0)
                ),
            ),
            turning_over_min_topical_similarity=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "turning_over_min_topical_similarity", 0.30,
                        )
                    ),
                ),
            ),
            turning_over_recent_msgs_window=max(
                0,
                int(
                    memory_raw.get("turning_over_recent_msgs_window", 12)
                ),
            ),
            callback_age_floor_days=max(
                1, int(memory_raw.get("callback_age_floor_days", 3)),
            ),
            callback_similarity_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("callback_similarity_threshold", 0.55)
                    ),
                ),
            ),
            callback_max_hits_per_turn=max(
                1, int(memory_raw.get("callback_max_hits_per_turn", 3)),
            ),
            callback_cooldown_hours=max(
                1, int(memory_raw.get("callback_cooldown_hours", 24)),
            ),
            callback_salience_bump=max(
                0.0,
                min(
                    0.5,
                    float(memory_raw.get("callback_salience_bump", 0.05)),
                ),
            ),
            callback_revival_bump=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("callback_revival_bump", 0.10)),
                ),
            ),
            calibration_baseline=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("calibration_baseline", 0.80)),
                ),
            ),
            calibration_global_low_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "calibration_global_low_threshold", 0.55,
                        )
                    ),
                ),
            ),
            calibration_topic_low_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "calibration_topic_low_threshold", 0.50,
                        )
                    ),
                ),
            ),
            calibration_half_life_days=max(
                0.1,
                float(memory_raw.get("calibration_half_life_days", 5.0)),
            ),
            calibration_topic_merge_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "calibration_topic_merge_threshold", 0.78,
                        )
                    ),
                ),
            ),
            calibration_softening_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "calibration_softening_threshold", 0.70,
                        )
                    ),
                ),
            ),
            calibration_max_topic_slots=max(
                1, int(memory_raw.get("calibration_max_topic_slots", 8)),
            ),
            sensory_anchor_min_turn_gap=max(
                1, int(memory_raw.get("sensory_anchor_min_turn_gap", 4)),
            ),
            sensory_anchor_probability_scale=max(
                0.0,
                min(
                    2.0,
                    float(
                        memory_raw.get(
                            "sensory_anchor_probability_scale", 1.0,
                        )
                    ),
                ),
            ),
            sensory_anchor_max_recent_items=max(
                1,
                int(memory_raw.get("sensory_anchor_max_recent_items", 4)),
            ),
            sensory_anchor_max_window_items=max(
                1,
                int(memory_raw.get("sensory_anchor_max_window_items", 6)),
            ),
            decay_max_catchup_days=max(
                1.0, float(memory_raw.get("decay_max_catchup_days", 30.0))
            ),
            promotion_worker_interval_seconds=max(
                10,
                int(memory_raw.get("promotion_worker_interval_seconds", 1800)),
            ),
            decay_worker_interval_seconds=max(
                10, int(memory_raw.get("decay_worker_interval_seconds", 1800))
            ),
            fact_checker_interval_seconds=max(
                30,
                int(memory_raw.get("fact_checker_interval_seconds", 300)),
            ),
            schedule_learner_interval_seconds=max(
                60,
                int(
                    memory_raw.get("schedule_learner_interval_seconds", 86400)
                ),
            ),
            routine_min_touches=max(
                1,
                int(memory_raw.get("routine_min_touches", 3)),
            ),
            routine_min_share=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("routine_min_share", 0.30)),
                ),
            ),
            routine_max_active=max(
                1,
                int(memory_raw.get("routine_max_active", 5)),
            ),
            idle_curiosity_interval_seconds=max(
                60,
                int(memory_raw.get("idle_curiosity_interval_seconds", 1800)),
            ),
            knowledge_enrichment_interval_seconds=max(
                60,
                int(
                    memory_raw.get(
                        "knowledge_enrichment_interval_seconds", 3600,
                    )
                ),
            ),
            knowledge_cluster_cooldown_hours=max(
                0,
                int(memory_raw.get("knowledge_cluster_cooldown_hours", 72)),
            ),
            knowledge_enrichment_max_per_cluster=max(
                0,
                int(
                    memory_raw.get("knowledge_enrichment_max_per_cluster", 3)
                ),
            ),
            knowledge_enrichment_max_clusters_per_run=max(
                1,
                int(
                    memory_raw.get(
                        "knowledge_enrichment_max_clusters_per_run", 3,
                    )
                ),
            ),
            knowledge_research_queries_per_cluster=max(
                1,
                int(
                    memory_raw.get(
                        "knowledge_research_queries_per_cluster", 3,
                    )
                ),
            ),
            knowledge_unresearchable_cooldown_hours=max(
                0,
                int(
                    memory_raw.get(
                        "knowledge_unresearchable_cooldown_hours", 336,
                    )
                ),
            ),
            knowledge_gap_notice_interval_seconds=max(
                60,
                int(
                    memory_raw.get(
                        "knowledge_gap_notice_interval_seconds", 3600,
                    )
                ),
            ),
            knowledge_gap_notice_min_size=max(
                2,
                int(memory_raw.get("knowledge_gap_notice_min_size", 5)),
            ),
            knowledge_gap_notice_max_knowledge_fraction=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "knowledge_gap_notice_max_knowledge_fraction",
                            0.15,
                        )
                    ),
                ),
            ),
            knowledge_gap_notice_topic_cooldown_hours=max(
                0,
                int(
                    memory_raw.get(
                        "knowledge_gap_notice_topic_cooldown_hours", 72,
                    )
                ),
            ),
            knowledge_gap_notice_journal_max=max(
                1,
                int(memory_raw.get("knowledge_gap_notice_journal_max", 6)),
            ),
            associative_wander_interval_seconds=max(
                60,
                int(
                    memory_raw.get(
                        "associative_wander_interval_seconds", 5400,
                    )
                ),
            ),
            associative_wander_cooldown_seconds=max(
                0,
                int(
                    memory_raw.get(
                        "associative_wander_cooldown_seconds", 7200,
                    )
                ),
            ),
            associative_wander_journal_max=max(
                1,
                int(memory_raw.get("associative_wander_journal_max", 6)),
            ),
            associative_wander_min_size=max(
                2,
                int(memory_raw.get("associative_wander_min_size", 4)),
            ),
            associative_wander_max_pair_cosine=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "associative_wander_max_pair_cosine", 0.25,
                        )
                    ),
                ),
            ),
            associative_wander_pair_cooldown_hours=max(
                0,
                int(
                    memory_raw.get(
                        "associative_wander_pair_cooldown_hours", 168,
                    )
                ),
            ),
            associative_wander_member_samples=max(
                0,
                int(memory_raw.get("associative_wander_member_samples", 3)),
            ),
            interest_drift_interval_seconds=max(
                60,
                int(
                    memory_raw.get("interest_drift_interval_seconds", 21600)
                ),
            ),
            interest_drift_journal_max=max(
                1,
                int(memory_raw.get("interest_drift_journal_max", 6)),
            ),
            interest_drift_min_size=max(
                2,
                int(memory_raw.get("interest_drift_min_size", 4)),
            ),
            interest_drift_max_clusters=max(
                1,
                int(memory_raw.get("interest_drift_max_clusters", 40)),
            ),
            interest_drift_window_samples=max(
                2,
                int(memory_raw.get("interest_drift_window_samples", 8)),
            ),
            interest_drift_min_samples=max(
                2,
                int(memory_raw.get("interest_drift_min_samples", 3)),
            ),
            interest_drift_rise_ratio=max(
                0.0,
                float(memory_raw.get("interest_drift_rise_ratio", 0.5)),
            ),
            interest_drift_fade_max_growth_ratio=max(
                0.0,
                float(
                    memory_raw.get(
                        "interest_drift_fade_max_growth_ratio", 0.05,
                    )
                ),
            ),
            interest_drift_topic_cooldown_hours=max(
                0,
                int(
                    memory_raw.get(
                        "interest_drift_topic_cooldown_hours", 72,
                    )
                ),
            ),
            dormant_interest_interval_seconds=max(
                60,
                int(
                    memory_raw.get("dormant_interest_interval_seconds", 21600)
                ),
            ),
            dormant_interest_journal_max=max(
                1,
                int(memory_raw.get("dormant_interest_journal_max", 6)),
            ),
            dormant_interest_min_size=max(
                2,
                int(memory_raw.get("dormant_interest_min_size", 6)),
            ),
            dormant_interest_max_clusters=max(
                1,
                int(memory_raw.get("dormant_interest_max_clusters", 40)),
            ),
            dormant_interest_dormant_days=max(
                0.0,
                float(memory_raw.get("dormant_interest_dormant_days", 21.0)),
            ),
            dormant_interest_topic_cooldown_hours=max(
                0,
                int(
                    memory_raw.get(
                        "dormant_interest_topic_cooldown_hours", 336,
                    )
                ),
            ),
            dormant_interest_surface_cooldown_hours=max(
                0.0,
                float(
                    memory_raw.get(
                        "dormant_interest_surface_cooldown_hours", 24.0,
                    )
                ),
            ),
            curiosity_gradient_interval_seconds=max(
                60,
                int(
                    memory_raw.get(
                        "curiosity_gradient_interval_seconds", 5400,
                    )
                ),
            ),
            curiosity_gradient_journal_max=max(
                1,
                int(memory_raw.get("curiosity_gradient_journal_max", 6)),
            ),
            curiosity_gradient_dense_min_size=max(
                2,
                int(memory_raw.get("curiosity_gradient_dense_min_size", 8)),
            ),
            curiosity_gradient_thin_min_size=max(
                1,
                int(memory_raw.get("curiosity_gradient_thin_min_size", 2)),
            ),
            curiosity_gradient_thin_max_size=max(
                1,
                int(memory_raw.get("curiosity_gradient_thin_max_size", 4)),
            ),
            curiosity_gradient_adjacency_min_cosine=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "curiosity_gradient_adjacency_min_cosine", 0.40,
                        )
                    ),
                ),
            ),
            curiosity_gradient_adjacency_max_cosine=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "curiosity_gradient_adjacency_max_cosine", 0.90,
                        )
                    ),
                ),
            ),
            curiosity_gradient_edge_cooldown_hours=max(
                0,
                int(
                    memory_raw.get(
                        "curiosity_gradient_edge_cooldown_hours", 96,
                    )
                ),
            ),
            knowledge_map_reflection_interval_seconds=max(
                60,
                int(
                    memory_raw.get(
                        "knowledge_map_reflection_interval_seconds", 86400,
                    )
                ),
            ),
            knowledge_map_reflection_cooldown_hours=max(
                0,
                int(
                    memory_raw.get(
                        "knowledge_map_reflection_cooldown_hours", 20,
                    )
                ),
            ),
            knowledge_map_reflection_min_clusters=max(
                2,
                int(
                    memory_raw.get(
                        "knowledge_map_reflection_min_clusters", 4,
                    )
                ),
            ),
            knowledge_map_reflection_rich_top_n=max(
                1,
                int(
                    memory_raw.get(
                        "knowledge_map_reflection_rich_top_n", 5,
                    )
                ),
            ),
            knowledge_map_reflection_gap_top_n=max(
                0,
                int(
                    memory_raw.get(
                        "knowledge_map_reflection_gap_top_n", 3,
                    )
                ),
            ),
            knowledge_map_reflection_concepts_per_cluster=max(
                0,
                int(
                    memory_raw.get(
                        "knowledge_map_reflection_concepts_per_cluster", 2,
                    )
                ),
            ),
            knowledge_map_reflection_max_tokens=max(
                40,
                int(
                    memory_raw.get(
                        "knowledge_map_reflection_max_tokens", 120,
                    )
                ),
            ),
            knowledge_map_reflection_salience=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "knowledge_map_reflection_salience", 0.5,
                        )
                    ),
                ),
            ),
            topic_temperature_min_sim=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("topic_temperature_min_sim", 0.45)),
                ),
            ),
            topic_temperature_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("topic_temperature_threshold", 0.5)),
                ),
            ),
            topic_temperature_cooldown_turns=max(
                0,
                int(memory_raw.get("topic_temperature_cooldown_turns", 6)),
            ),
            topic_confidence_min_sim=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("topic_confidence_min_sim", 0.45)),
                ),
            ),
            topic_confidence_thin_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("topic_confidence_thin_threshold", 0.25)
                    ),
                ),
            ),
            topic_confidence_familiar_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "topic_confidence_familiar_threshold", 0.7
                        )
                    ),
                ),
            ),
            topic_confidence_cooldown_turns=max(
                0,
                int(memory_raw.get("topic_confidence_cooldown_turns", 6)),
            ),
            earned_familiarity_min_sim=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("earned_familiarity_min_sim", 0.45)),
                ),
            ),
            earned_familiarity_deep_threshold=max(
                1,
                int(memory_raw.get("earned_familiarity_deep_threshold", 14)),
            ),
            earned_familiarity_cooldown_turns=max(
                0,
                int(memory_raw.get("earned_familiarity_cooldown_turns", 12)),
            ),
            user_expertise_min_sim=max(
                0.0,
                min(1.0, float(memory_raw.get("user_expertise_min_sim", 0.45))),
            ),
            user_expertise_learning_rate=max(
                0.01,
                min(
                    1.0,
                    float(memory_raw.get("user_expertise_learning_rate", 0.25)),
                ),
            ),
            user_expertise_min_samples=max(
                1,
                int(memory_raw.get("user_expertise_min_samples", 4)),
            ),
            user_expertise_novice_threshold=max(
                -1.0,
                min(
                    0.0,
                    float(
                        memory_raw.get(
                            "user_expertise_novice_threshold", -0.35
                        )
                    ),
                ),
            ),
            user_expertise_expert_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "user_expertise_expert_threshold", 0.35
                        )
                    ),
                ),
            ),
            user_expertise_cooldown_turns=max(
                0,
                int(memory_raw.get("user_expertise_cooldown_turns", 12)),
            ),
            vitality_recover_half_life_hours=max(
                0.01,
                float(
                    memory_raw.get("vitality_recover_half_life_hours", 2.0)
                ),
            ),
            vitality_low_threshold=max(
                0.0,
                min(1.0, float(memory_raw.get("vitality_low_threshold", 0.30))),
            ),
            vitality_high_threshold=max(
                0.0,
                min(
                    1.0, float(memory_raw.get("vitality_high_threshold", 0.70))
                ),
            ),
            vitality_expressiveness_floor=max(
                0.0,
                min(
                    2.0,
                    float(
                        memory_raw.get("vitality_expressiveness_floor", 0.7)
                    ),
                ),
            ),
            vitality_expressiveness_ceil=max(
                0.0,
                min(
                    3.0,
                    float(memory_raw.get("vitality_expressiveness_ceil", 1.2)),
                ),
            ),
            vitality_cost_chars_per_unit=max(
                1.0,
                float(memory_raw.get("vitality_cost_chars_per_unit", 1200.0)),
            ),
            vitality_cost_length_unit=max(
                0.0,
                float(memory_raw.get("vitality_cost_length_unit", 0.04)),
            ),
            vitality_cost_emotion_gain=max(
                0.0,
                float(memory_raw.get("vitality_cost_emotion_gain", 0.06)),
            ),
            vitality_cost_max=max(
                0.0,
                min(1.0, float(memory_raw.get("vitality_cost_max", 0.12))),
            ),
            vitality_boost_engaged=max(
                0.0,
                float(memory_raw.get("vitality_boost_engaged", 0.05)),
            ),
            vitality_boost_arousal_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "vitality_boost_arousal_threshold", 0.55
                        )
                    ),
                ),
            ),
            vitality_boost_arousal_gain=max(
                0.0,
                float(memory_raw.get("vitality_boost_arousal_gain", 0.22)),
            ),
            vitality_boost_strong_novelty=max(
                0.0,
                float(memory_raw.get("vitality_boost_strong_novelty", 0.04)),
            ),
            vitality_boost_mild_novelty=max(
                0.0,
                float(memory_raw.get("vitality_boost_mild_novelty", 0.02)),
            ),
            vitality_boost_max=max(
                0.0,
                min(1.0, float(memory_raw.get("vitality_boost_max", 0.15))),
            ),
            vitality_proactive_factor=max(
                0.0,
                min(1.0, float(memory_raw.get("vitality_proactive_factor", 0.4))),
            ),
            vitality_rhythm_exception_chance=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("vitality_rhythm_exception_chance", 0.3)
                    ),
                ),
            ),
            implicit_need_min_confidence=max(
                0.5,
                float(memory_raw.get("implicit_need_min_confidence", 2.0)),
            ),
            upcoming_horizon_days=max(
                1,
                int(memory_raw.get("upcoming_horizon_days", 7)),
            ),
            upcoming_horizon_max_items=max(
                1,
                int(memory_raw.get("upcoming_horizon_max_items", 3)),
            ),
            upcoming_horizon_cooldown_turns=max(
                0,
                int(memory_raw.get("upcoming_horizon_cooldown_turns", 6)),
            ),
            knowledge_grounding_min_similarity=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "knowledge_grounding_min_similarity", 0.45,
                        )
                    ),
                ),
            ),
            knowledge_grounding_max_items=max(
                1,
                int(memory_raw.get("knowledge_grounding_max_items", 2)),
            ),
            curiosity_seed_interval_seconds=max(
                60,
                int(memory_raw.get("curiosity_seed_interval_seconds", 3600)),
            ),
            concept_synthesis_interval_seconds=max(
                60,
                int(memory_raw.get("concept_synthesis_interval_seconds", 1800)),
            ),
            concept_synthesis_max_clusters_per_run=max(
                1,
                int(
                    memory_raw.get("concept_synthesis_max_clusters_per_run", 5)
                ),
            ),
            concept_synthesis_max_aiko_memories=max(
                1,
                int(memory_raw.get("concept_synthesis_max_aiko_memories", 40)),
            ),
            concept_synthesis_dirty_size_delta=max(
                1,
                int(memory_raw.get("concept_synthesis_dirty_size_delta", 3)),
            ),
            concept_synthesis_max_tokens=max(
                256,
                int(memory_raw.get("concept_synthesis_max_tokens", 4096)),
            ),
            concept_synthesis_affect_min_samples=max(
                1,
                int(
                    memory_raw.get("concept_synthesis_affect_min_samples", 3)
                ),
            ),
            affect_sampler_min_sim=max(
                0.0,
                min(1.0, float(memory_raw.get("affect_sampler_min_sim", 0.4))),
            ),
            affect_sampler_top_n=max(
                1, int(memory_raw.get("affect_sampler_top_n", 1))
            ),
            affect_sampler_learning_rate=max(
                0.01,
                min(
                    1.0,
                    float(memory_raw.get("affect_sampler_learning_rate", 0.2)),
                ),
            ),
            cluster_affect_map_cap=max(
                1, int(memory_raw.get("cluster_affect_map_cap", 200))
            ),
            cluster_affect_max_age_days=max(
                1.0,
                float(memory_raw.get("cluster_affect_max_age_days", 120.0)),
            ),
            concept_synthesis_ritual_min_moments=max(
                2,
                int(memory_raw.get("concept_synthesis_ritual_min_moments", 6)),
            ),
            concept_synthesis_ritual_group_min_size=max(
                2,
                int(
                    memory_raw.get(
                        "concept_synthesis_ritual_group_min_size", 3
                    )
                ),
            ),
            concept_synthesis_ritual_group_similarity=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "concept_synthesis_ritual_group_similarity", 0.45
                        )
                    ),
                ),
            ),
            concept_synthesis_max_ritual_groups=max(
                1,
                int(memory_raw.get("concept_synthesis_max_ritual_groups", 3)),
            ),
            concept_synthesis_narrative_min_chain=max(
                2,
                int(
                    memory_raw.get("concept_synthesis_narrative_min_chain", 3)
                ),
            ),
            concept_synthesis_max_narrative_clusters_per_run=max(
                1,
                int(
                    memory_raw.get(
                        "concept_synthesis_max_narrative_clusters_per_run", 3
                    )
                ),
            ),
            concept_synthesis_max_narrative_memories=max(
                2,
                int(
                    memory_raw.get(
                        "concept_synthesis_max_narrative_memories", 40
                    )
                ),
            ),
            concept_synthesis_shared_arc_min_chain=max(
                2,
                int(
                    memory_raw.get(
                        "concept_synthesis_shared_arc_min_chain", 3
                    )
                ),
            ),
            concept_synthesis_shared_arc_similarity=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "concept_synthesis_shared_arc_similarity", 0.45
                        )
                    ),
                ),
            ),
            concept_synthesis_shared_arc_gap_days=max(
                0.5,
                float(
                    memory_raw.get(
                        "concept_synthesis_shared_arc_gap_days", 10.0
                    )
                ),
            ),
            concept_synthesis_shared_arc_quiet_days=max(
                0.0,
                float(
                    memory_raw.get(
                        "concept_synthesis_shared_arc_quiet_days", 3.0
                    )
                ),
            ),
            concept_synthesis_max_shared_arc_episodes=max(
                1,
                int(
                    memory_raw.get(
                        "concept_synthesis_max_shared_arc_episodes", 3
                    )
                ),
            ),
            concept_synthesis_aspiration_min_chain=max(
                2,
                int(
                    memory_raw.get("concept_synthesis_aspiration_min_chain", 3)
                ),
            ),
            concept_synthesis_aspiration_min_span_days=max(
                0.0,
                float(
                    memory_raw.get(
                        "concept_synthesis_aspiration_min_span_days", 14.0
                    )
                ),
            ),
            concept_synthesis_max_aspiration_clusters_per_run=max(
                1,
                int(
                    memory_raw.get(
                        "concept_synthesis_max_aspiration_clusters_per_run", 3
                    )
                ),
            ),
            concept_synthesis_max_aspiration_memories=max(
                2,
                int(
                    memory_raw.get(
                        "concept_synthesis_max_aspiration_memories", 40
                    )
                ),
            ),
            concept_synthesis_max_boundary_memories=max(
                1,
                int(
                    memory_raw.get(
                        "concept_synthesis_max_boundary_memories", 24
                    )
                ),
            ),
            concept_synthesis_max_comm_style_memories=max(
                1,
                int(
                    memory_raw.get(
                        "concept_synthesis_max_comm_style_memories", 24
                    )
                ),
            ),
            concept_synthesis_max_tension_concepts=max(
                2,
                int(
                    memory_raw.get(
                        "concept_synthesis_max_tension_concepts", 24
                    )
                ),
            ),
            concept_synthesis_max_generalization_concepts=max(
                2,
                int(
                    memory_raw.get(
                        "concept_synthesis_max_generalization_concepts", 24
                    )
                ),
            ),
            taste_affinity_window_days=max(
                1,
                int(memory_raw.get("taste_affinity_window_days", 90)),
            ),
            taste_min_settled=max(
                1,
                int(memory_raw.get("taste_min_settled", 4)),
            ),
            taste_min_affinity=max(
                0.0,
                min(1.0, float(memory_raw.get("taste_min_affinity", 0.15))),
            ),
            taste_affinity_baseline_multiple=max(
                1.0,
                float(
                    memory_raw.get("taste_affinity_baseline_multiple", 1.4)
                ),
            ),
            concept_synthesis_max_taste_clusters=max(
                1,
                int(
                    memory_raw.get("concept_synthesis_max_taste_clusters", 6)
                ),
            ),
            pursuit_min_notes=max(
                1, int(memory_raw.get("pursuit_min_notes", 6)),
            ),
            concept_synthesis_max_pursuit_memories=max(
                1,
                int(
                    memory_raw.get(
                        "concept_synthesis_max_pursuit_memories", 40,
                    )
                ),
            ),
            conduct_window_days=max(
                21, int(memory_raw.get("conduct_window_days", 90)),
            ),
            conduct_cadence_seconds=max(
                86400, int(memory_raw.get("conduct_cadence_seconds", 604800)),
            ),
            conduct_max_user_vectors=max(
                20, int(memory_raw.get("conduct_max_user_vectors", 1000)),
            ),
            conduct_user_topic_min_similarity=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "conduct_user_topic_min_similarity", 0.45
                        )
                    ),
                ),
            ),
            conduct_min_settled_rows=max(
                1, int(memory_raw.get("conduct_min_settled_rows", 50)),
            ),
            conduct_min_user_turns=max(
                1, int(memory_raw.get("conduct_min_user_turns", 20)),
            ),
            conduct_concentration_min_settled=max(
                1,
                int(memory_raw.get("conduct_concentration_min_settled", 8)),
            ),
            conduct_concentration_min_share=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("conduct_concentration_min_share", 0.30)
                    ),
                ),
            ),
            conduct_concentration_min_excess=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "conduct_concentration_min_excess", 0.12
                        )
                    ),
                ),
            ),
            conduct_concentration_min_ratio=max(
                1.0,
                float(memory_raw.get("conduct_concentration_min_ratio", 2.0)),
            ),
            conduct_neglect_min_confidence=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("conduct_neglect_min_confidence", 0.75)
                    ),
                ),
            ),
            conduct_neglect_min_age_days=max(
                0.0,
                float(memory_raw.get("conduct_neglect_min_age_days", 14.0)),
            ),
            conduct_neglect_max_surfaced=max(
                0, int(memory_raw.get("conduct_neglect_max_surfaced", 1)),
            ),
            conduct_neglect_min_candidates=max(
                2, int(memory_raw.get("conduct_neglect_min_candidates", 3)),
            ),
            conduct_fixation_min_surfaced=max(
                2, int(memory_raw.get("conduct_fixation_min_surfaced", 12)),
            ),
            conduct_fixation_min_settled=max(
                1, int(memory_raw.get("conduct_fixation_min_settled", 6)),
            ),
            conduct_fixation_min_ratio=max(
                1.0,
                float(memory_raw.get("conduct_fixation_min_ratio", 3.0)),
            ),
            conduct_fixation_min_rate_gap=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("conduct_fixation_min_rate_gap", 0.05)),
                ),
            ),
            conduct_snapshot_cap=max(
                1, int(memory_raw.get("conduct_snapshot_cap", 6)),
            ),
            conduct_notice_min_confidence=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("conduct_notice_min_confidence", 0.7)
                    ),
                ),
            ),
            conduct_notice_cooldown_days=max(
                1.0,
                float(memory_raw.get("conduct_notice_cooldown_days", 7.0)),
            ),
            generalization_suppress_children_enabled=bool(
                memory_raw.get(
                    "generalization_suppress_children_enabled", True
                )
            ),
            generalization_parent_min_confidence=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "generalization_parent_min_confidence", 0.7
                        )
                    ),
                ),
            ),
            tension_cue_interval_seconds=max(
                60.0,
                float(
                    memory_raw.get("tension_cue_interval_seconds", 28800.0)
                ),
            ),
            tension_cue_min_confidence=float(
                memory_raw.get("tension_cue_min_confidence", 0.6)
            ),
            tension_cue_journal_max=max(
                1,
                int(memory_raw.get("tension_cue_journal_max", 4)),
            ),
            aspiration_momentum_interval_seconds=max(
                60.0,
                float(
                    memory_raw.get(
                        "aspiration_momentum_interval_seconds", 21600.0
                    )
                ),
            ),
            aspiration_momentum_cooldown_days=max(
                0.0,
                float(
                    memory_raw.get("aspiration_momentum_cooldown_days", 10.0)
                ),
            ),
            aspiration_momentum_min_confidence=max(
                0.0,
                float(
                    memory_raw.get("aspiration_momentum_min_confidence", 0.6)
                ),
            ),
            aspiration_momentum_staleness_min_days=max(
                0.0,
                float(
                    memory_raw.get(
                        "aspiration_momentum_staleness_min_days", 7.0
                    )
                ),
            ),
            aspiration_momentum_journal_max=max(
                1,
                int(memory_raw.get("aspiration_momentum_journal_max", 4)),
            ),
            engagement_clock_enabled=bool(
                memory_raw.get("engagement_clock_enabled", True)
            ),
            engagement_seconds_per_day=max(
                1.0,
                float(memory_raw.get("engagement_seconds_per_day", 3600.0)),
            ),
            engagement_idle_cap_seconds=max(
                1.0,
                float(memory_raw.get("engagement_idle_cap_seconds", 300.0)),
            ),
            engagement_min_turn_seconds=max(
                0.0,
                float(memory_raw.get("engagement_min_turn_seconds", 15.0)),
            ),
            memory_decay_use_engagement_clock=bool(
                memory_raw.get("memory_decay_use_engagement_clock", True)
            ),
            concept_lifecycle_enabled=bool(
                memory_raw.get("concept_lifecycle_enabled", True)
            ),
            concept_lifecycle_interval_seconds=max(
                30,
                int(memory_raw.get("concept_lifecycle_interval_seconds", 300)),
            ),
            concept_lifecycle_batch_size=max(
                1,
                int(memory_raw.get("concept_lifecycle_batch_size", 100)),
            ),
            concept_promote_min_sources=max(
                1,
                int(memory_raw.get("concept_promote_min_sources", 2)),
            ),
            concept_promote_min_age_days=max(
                0.0,
                float(memory_raw.get("concept_promote_min_age_days", 0.0)),
            ),
            concept_promote_min_confidence=max(
                0.0,
                float(memory_raw.get("concept_promote_min_confidence", 0.6)),
            ),
            concept_confidence_halflife_days=max(
                0.1,
                float(memory_raw.get("concept_confidence_halflife_days", 7.5)),
            ),
            concept_decay_max_catchup_days=max(
                0.1,
                float(memory_raw.get("concept_decay_max_catchup_days", 3.0)),
            ),
            concept_dormant_confidence_floor=max(
                0.0,
                float(memory_raw.get("concept_dormant_confidence_floor", 0.35)),
            ),
            concept_retire_confidence_floor=max(
                0.0,
                float(memory_raw.get("concept_retire_confidence_floor", 0.15)),
            ),
            concept_dormant_ttl_days=max(
                0.0,
                float(memory_raw.get("concept_dormant_ttl_days", 30.0)),
            ),
            concept_candidate_ttl_days=max(
                0.0,
                float(memory_raw.get("concept_candidate_ttl_days", 21.0)),
            ),
            concept_identity_plasticity=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("concept_identity_plasticity", 0.3)),
                ),
            ),
            concept_default_plasticity=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("concept_default_plasticity", 0.5)),
                ),
            ),
            concept_plasticity_modulation_enabled=bool(
                memory_raw.get("concept_plasticity_modulation_enabled", True)
            ),
            concept_plasticity_duration_days_full=max(
                1.0,
                float(
                    memory_raw.get("concept_plasticity_duration_days_full", 180.0)
                ),
            ),
            concept_plasticity_shift_event_delta=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get("concept_plasticity_shift_event_delta", 0.1)
                    ),
                ),
            ),
            concept_plasticity_drift_enabled=bool(
                memory_raw.get("concept_plasticity_drift_enabled", True)
            ),
            concept_plasticity_drift_rate=min(
                1.0,
                max(0.0, float(memory_raw.get("concept_plasticity_drift_rate", 0.05))),
            ),
            concept_plasticity_drift_floor=min(
                1.0,
                max(0.0, float(memory_raw.get("concept_plasticity_drift_floor", 0.15))),
            ),
            concept_plasticity_recheck_slowdown_enabled=bool(
                memory_raw.get(
                    "concept_plasticity_recheck_slowdown_enabled", True
                )
            ),
            concept_plasticity_recheck_stride_k=max(
                0.0,
                float(memory_raw.get("concept_plasticity_recheck_stride_k", 3.0)),
            ),
            concept_confidence_sample_enabled=bool(
                memory_raw.get("concept_confidence_sample_enabled", True)
            ),
            concept_confidence_sample_band=min(
                1.0,
                max(
                    0.01,
                    float(
                        memory_raw.get("concept_confidence_sample_band", 0.1)
                    ),
                ),
            ),
            concept_surfacing_habituation_enabled=bool(
                memory_raw.get("concept_surfacing_habituation_enabled", True)
            ),
            concept_surfacing_habituation_window_turns=max(
                0,
                int(
                    memory_raw.get(
                        "concept_surfacing_habituation_window_turns", 4
                    )
                ),
            ),
            concept_surfacing_habituation_floor=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get("concept_surfacing_habituation_floor", 0.35)
                    ),
                ),
            ),
            concept_surfacing_core_habituation_floor=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_surfacing_core_habituation_floor", 0.8
                        )
                    ),
                ),
            ),
            concept_surfacing_state_cap=max(
                0,
                int(memory_raw.get("concept_surfacing_state_cap", 300)),
            ),
            concept_surfacing_standing_enabled=bool(
                memory_raw.get("concept_surfacing_standing_enabled", True)
            ),
            concept_surfacing_standing_window_days=max(
                1,
                int(
                    memory_raw.get(
                        "concept_surfacing_standing_window_days", 90
                    )
                ),
            ),
            concept_surfacing_standing_min_settled=max(
                1,
                int(
                    memory_raw.get(
                        "concept_surfacing_standing_min_settled", 4
                    )
                ),
            ),
            concept_surfacing_standing_prior_strength=min(
                100.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_surfacing_standing_prior_strength", 10.0
                        )
                    ),
                ),
            ),
            concept_surfacing_standing_floor=min(
                0.5,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_surfacing_standing_floor", 0.35
                        )
                    ),
                ),
            ),
            concept_surfacing_standing_ceiling=min(
                1.0,
                max(
                    0.5,
                    float(
                        memory_raw.get(
                            "concept_surfacing_standing_ceiling", 1.0
                        )
                    ),
                ),
            ),
            concept_surfacing_standing_refresh_seconds=max(
                60,
                int(
                    memory_raw.get(
                        "concept_surfacing_standing_refresh_seconds", 3600
                    )
                ),
            ),
            concept_surfacing_standing_state_cap=max(
                100,
                int(
                    memory_raw.get(
                        "concept_surfacing_standing_state_cap", 1000
                    )
                ),
            ),
            concept_surfacing_core_rationale_enabled=bool(
                memory_raw.get(
                    "concept_surfacing_core_rationale_enabled", True
                )
            ),
            concept_surfacing_rationale_max_chars=max(
                0,
                int(
                    memory_raw.get(
                        "concept_surfacing_rationale_max_chars", 120
                    )
                ),
            ),
            profile_concept_max_lines=max(
                0, int(memory_raw.get("profile_concept_max_lines", 4)),
            ),
            profile_concept_min_confidence=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("profile_concept_min_confidence", 0.5)
                    ),
                ),
            ),
            concept_surfacing_salience_enabled=bool(
                memory_raw.get("concept_surfacing_salience_enabled", True)
            ),
            concept_surfacing_salience_event_scan=max(
                0,
                int(
                    memory_raw.get("concept_surfacing_salience_event_scan", 120)
                ),
            ),
            concept_surfacing_activation_enabled=bool(
                memory_raw.get("concept_surfacing_activation_enabled", True)
            ),
            concept_surfacing_activation_seed_cap=max(
                0,
                int(
                    memory_raw.get("concept_surfacing_activation_seed_cap", 4)
                ),
            ),
            concept_surfacing_activation_max=max(
                0,
                int(memory_raw.get("concept_surfacing_activation_max", 4)),
            ),
            concept_importance_enabled=bool(
                memory_raw.get("concept_importance_enabled", True)
            ),
            # Clamped to [0, 1]: the multiplier is 1 + strength * (imp - 0.5),
            # so strength > 1 could drive a score negative at imp = 0.
            concept_importance_strength=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("concept_importance_strength", 0.4)),
                ),
            ),
            concept_importance_affect_lift=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get("concept_importance_affect_lift", 0.5)
                    ),
                ),
            ),
            concept_importance_affect_min_samples=max(
                1,
                int(
                    memory_raw.get(
                        "concept_importance_affect_min_samples", 3
                    )
                ),
            ),
            concept_surfacing_overfetch=max(
                1,
                int(memory_raw.get("concept_surfacing_overfetch", 5)),
            ),
            concept_min_clusters=max(
                0,
                int(memory_raw.get("concept_min_clusters", 6)),
            ),
            concept_min_history_days=max(
                0.0,
                float(memory_raw.get("concept_min_history_days", 3.0)),
            ),
            concept_promote_young_min_sources=max(
                1,
                int(memory_raw.get("concept_promote_young_min_sources", 3)),
            ),
            concept_promote_young_min_confidence=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_promote_young_min_confidence", 0.72
                        )
                    ),
                ),
            ),
            context_budget_enabled=bool(
                memory_raw.get("context_budget_enabled", True)
            ),
            context_budget_fraction=min(
                0.8,
                max(0.0, float(memory_raw.get("context_budget_fraction", 0.15))),
            ),
            context_budget_max_tokens=max(
                0, int(memory_raw.get("context_budget_max_tokens", 4096))
            ),
            context_budget_min_tokens=max(
                0, int(memory_raw.get("context_budget_min_tokens", 256))
            ),
            context_budget_history_floor_tokens=max(
                0,
                int(memory_raw.get("context_budget_history_floor_tokens", 1024)),
            ),
            context_budget_memory_pool_k=max(
                0, int(memory_raw.get("context_budget_memory_pool_k", 18))
            ),
            context_budget_memory_floor=max(
                0, int(memory_raw.get("context_budget_memory_floor", 1))
            ),
            context_budget_memory_cap=max(
                0, int(memory_raw.get("context_budget_memory_cap", 8))
            ),
            context_budget_memory_weight=max(
                0.0, float(memory_raw.get("context_budget_memory_weight", 1.0))
            ),
            context_budget_memory_min_relevance=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get("context_budget_memory_min_relevance", 0.0)
                    ),
                ),
            ),
            context_budget_cluster_floor=max(
                0, int(memory_raw.get("context_budget_cluster_floor", 0))
            ),
            context_budget_cluster_cap=max(
                0, int(memory_raw.get("context_budget_cluster_cap", 3))
            ),
            context_budget_cluster_weight=max(
                0.0, float(memory_raw.get("context_budget_cluster_weight", 0.9))
            ),
            context_budget_cluster_min_relevance=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "context_budget_cluster_min_relevance", 0.30
                        )
                    ),
                ),
            ),
            context_budget_concept_floor=max(
                0, int(memory_raw.get("context_budget_concept_floor", 0))
            ),
            context_budget_concept_cap=max(
                0, int(memory_raw.get("context_budget_concept_cap", 3))
            ),
            context_budget_concept_weight=max(
                0.0, float(memory_raw.get("context_budget_concept_weight", 1.1))
            ),
            context_budget_concept_min_relevance=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "context_budget_concept_min_relevance", 0.30
                        )
                    ),
                ),
            ),
            context_budget_core_cap=max(
                0,
                int(
                    memory_raw.get(
                        "context_budget_core_cap",
                        # Back-compat: the lane was ``identity`` pre-L27.
                        memory_raw.get("context_budget_identity_cap", 2),
                    )
                ),
            ),
            context_budget_core_min_confidence=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "context_budget_core_min_confidence",
                            memory_raw.get(
                                "context_budget_identity_min_confidence", 0.75
                            ),
                        )
                    ),
                ),
            ),
            concept_core_openness_slots=max(
                0, int(memory_raw.get("concept_core_openness_slots", 2))
            ),
            concept_core_openness_min_confidence=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_core_openness_min_confidence", 0.5
                        )
                    ),
                ),
            ),
            concept_flex_generative_floor=max(
                0, int(memory_raw.get("concept_flex_generative_floor", 1))
            ),
            concept_gate_tuning_enabled=bool(
                memory_raw.get("concept_gate_tuning_enabled", True)
            ),
            # Floored well above the scheduler's wake interval: a heartbeat
            # shorter than that just burns ticks on a ``demand()`` probe that
            # answers "not yet" until the daily cadence comes round.
            concept_gate_tuning_heartbeat_seconds=max(
                600,
                int(
                    memory_raw.get(
                        "concept_gate_tuning_heartbeat_seconds", 21600
                    )
                ),
            ),
            concept_gate_tuning_cadence_seconds=max(
                3600,
                int(
                    memory_raw.get(
                        "concept_gate_tuning_cadence_seconds", 86400
                    )
                ),
            ),
            # Capped as well as floored: the sample is the one part of the run
            # that can grow without bound, and the scheduler stops admitting a
            # worker whose average duration outgrows its lane budget.
            concept_gate_tuning_cosine_pairs=min(
                50_000,
                max(
                    0,
                    int(
                        memory_raw.get(
                            "concept_gate_tuning_cosine_pairs", 4000
                        )
                    ),
                ),
            ),
            # Clamped to [0, 0.8] like ``context_budget_fraction``: a worker
            # whose prompt is four-fifths concepts has no room left for the
            # thing it was actually asked to reason about.
            concept_diet_token_fraction=min(
                0.8,
                max(
                    0.0,
                    float(memory_raw.get("concept_diet_token_fraction", 0.06)),
                ),
            ),
            concept_diet_max_tokens=max(
                0, int(memory_raw.get("concept_diet_max_tokens", 600))
            ),
            concept_diet_min_tokens=max(
                0, int(memory_raw.get("concept_diet_min_tokens", 150))
            ),
            hypothesis_surfacing_enabled=bool(
                memory_raw.get("hypothesis_surfacing_enabled", True)
            ),
            context_budget_hypothesis_floor=max(
                0,
                int(memory_raw.get("context_budget_hypothesis_floor", 0)),
            ),
            context_budget_hypothesis_cap=max(
                0, int(memory_raw.get("context_budget_hypothesis_cap", 2))
            ),
            context_budget_hypothesis_weight=max(
                0.0,
                float(memory_raw.get("context_budget_hypothesis_weight", 0.7)),
            ),
            context_budget_hypothesis_min_relevance=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "context_budget_hypothesis_min_relevance", 0.35
                        )
                    ),
                ),
            ),
            hypothesis_min_unsettled=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("hypothesis_min_unsettled", 0.22)),
                ),
            ),
            hypothesis_min_sources=max(
                0, int(memory_raw.get("hypothesis_min_sources", 1))
            ),
            concept_hypothesis_interval_seconds=max(
                60,
                int(
                    memory_raw.get("concept_hypothesis_interval_seconds", 1800)
                ),
            ),
            concept_hypothesis_max_per_run=max(
                1, int(memory_raw.get("concept_hypothesis_max_per_run", 1))
            ),
            concept_hypothesis_min_gap_hours=max(
                0.0,
                float(memory_raw.get("concept_hypothesis_min_gap_hours", 4.0)),
            ),
            concept_hypothesis_gap_min_importance=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_hypothesis_gap_min_importance", 0.55
                        )
                    ),
                ),
            ),
            concept_hypothesis_deny_penalty=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get("concept_hypothesis_deny_penalty", 0.25)
                    ),
                ),
            ),
            concept_hypothesis_answer_threshold=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_hypothesis_answer_threshold", 0.45
                        )
                    ),
                ),
            ),
            hypothesis_invention_interval_seconds=max(
                60,
                int(
                    memory_raw.get(
                        "hypothesis_invention_interval_seconds", 5400
                    )
                ),
            ),
            hypothesis_invention_max_per_run=max(
                1, int(memory_raw.get("hypothesis_invention_max_per_run", 2))
            ),
            hypothesis_max_open=max(
                0, int(memory_raw.get("hypothesis_max_open", 12))
            ),
            hypothesis_min_novelty=min(
                1.0,
                max(0.0, float(memory_raw.get("hypothesis_min_novelty", 0.88))),
            ),
            hypothesis_concept_novelty=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("hypothesis_concept_novelty", 0.82)),
                ),
            ),
            hypothesis_ttl_hours=max(
                0.0, float(memory_raw.get("hypothesis_ttl_hours", 336.0))
            ),
            hypothesis_graduate_min_support=max(
                1, int(memory_raw.get("hypothesis_graduate_min_support", 2))
            ),
            hypothesis_graduate_min_credence=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get("hypothesis_graduate_min_credence", 0.7)
                    ),
                ),
            ),
            hypothesis_credence_step=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("hypothesis_credence_step", 0.2)),
                ),
            ),
            concept_contradiction_enabled=bool(
                memory_raw.get("concept_contradiction_enabled", True)
            ),
            concept_contradiction_similarity_min=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_contradiction_similarity_min", 0.6
                        )
                    ),
                ),
            ),
            concept_contradiction_similarity_max=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_contradiction_similarity_max", 0.95
                        )
                    ),
                ),
            ),
            concept_contradiction_penalty=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("concept_contradiction_penalty", 0.25)),
                ),
            ),
            concept_contradicted_confidence_floor=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_contradicted_confidence_floor", 0.4
                        )
                    ),
                ),
            ),
            concept_contradiction_batch_size=max(
                1,
                int(memory_raw.get("concept_contradiction_batch_size", 20)),
            ),
            concept_contradiction_max_candidates=max(
                1,
                int(memory_raw.get("concept_contradiction_max_candidates", 6)),
            ),
            concept_belief_revision_enabled=bool(
                memory_raw.get("concept_belief_revision_enabled", True)
            ),
            concept_belief_revision_batch_size=max(
                1,
                int(memory_raw.get("concept_belief_revision_batch_size", 5)),
            ),
            concept_belief_revision_max_evidence=max(
                1,
                int(memory_raw.get("concept_belief_revision_max_evidence", 6)),
            ),
            concept_belief_revision_confidence_penalty=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_belief_revision_confidence_penalty", 0.2
                        )
                    ),
                ),
            ),
            concept_belief_revision_confidence_floor=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_belief_revision_confidence_floor", 0.2
                        )
                    ),
                ),
            ),
            concept_belief_revision_superseded_relevance_days=max(
                0.0,
                float(
                    memory_raw.get(
                        "concept_belief_revision_superseded_relevance_days",
                        7.0,
                    )
                ),
            ),
            concept_consolidation_enabled=bool(
                memory_raw.get("concept_consolidation_enabled", True)
            ),
            concept_consolidation_interval_seconds=max(
                30,
                int(
                    memory_raw.get(
                        "concept_consolidation_interval_seconds", 900
                    )
                ),
            ),
            concept_consolidation_batch_size=max(
                1,
                int(memory_raw.get("concept_consolidation_batch_size", 40)),
            ),
            concept_consolidation_merge_cosine=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_consolidation_merge_cosine", 0.84
                        )
                    ),
                ),
            ),
            # Floored at the merge cosine, not at 0: an auto-merge bar
            # *below* the candidate bar would fuse every pair the scan
            # finds without ever adjudicating one, silently turning off the
            # judgement this worker exists to apply.
            concept_consolidation_auto_merge_cosine=min(
                1.0,
                max(
                    min(
                        1.0,
                        max(
                            0.0,
                            float(
                                memory_raw.get(
                                    "concept_consolidation_merge_cosine",
                                    0.84,
                                )
                            ),
                        ),
                    ),
                    float(
                        memory_raw.get(
                            "concept_consolidation_auto_merge_cosine", 1.0
                        )
                    ),
                ),
            ),
            concept_evidence_admission_cosine=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_evidence_admission_cosine", 0.35
                        )
                    ),
                ),
            ),
            concept_evidence_max_sources=max(
                0,
                int(memory_raw.get("concept_evidence_max_sources", 24)),
            ),
            concept_edge_integrity_enabled=bool(
                memory_raw.get("concept_edge_integrity_enabled", True)
            ),
            concept_edge_integrity_interval_seconds=max(
                1.0,
                float(
                    memory_raw.get(
                        "concept_edge_integrity_interval_seconds", 3600.0
                    )
                ),
            ),
            concept_edge_integrity_batch_size=max(
                1,
                int(memory_raw.get("concept_edge_integrity_batch_size", 200)),
            ),
            concept_drift_enabled=bool(
                memory_raw.get("concept_drift_enabled", True)
            ),
            concept_drift_interval_seconds=max(
                60,
                int(memory_raw.get("concept_drift_interval_seconds", 3600)),
            ),
            concept_drift_max_concepts=max(
                1, int(memory_raw.get("concept_drift_max_concepts", 120))
            ),
            concept_drift_trace_anchor=max(
                0, int(memory_raw.get("concept_drift_trace_anchor", 20))
            ),
            concept_drift_trace_recent=max(
                1, int(memory_raw.get("concept_drift_trace_recent", 60))
            ),
            concept_drift_sweep_enabled=bool(
                memory_raw.get("concept_drift_sweep_enabled", True)
            ),
            concept_drift_sweep_page=max(
                1, int(memory_raw.get("concept_drift_sweep_page", 60))
            ),
            concept_drift_sweep_max_findings=max(
                1,
                int(memory_raw.get("concept_drift_sweep_max_findings", 24)),
            ),
            concept_drift_min_salience=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("concept_drift_min_salience", 0.35)),
                ),
            ),
            concept_drift_min_age_days=max(
                0.0,
                float(memory_raw.get("concept_drift_min_age_days", 3.0)),
            ),
            concept_drift_min_confidence_delta=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_drift_min_confidence_delta", 0.15
                        )
                    ),
                ),
            ),
            concept_drift_max_findings=max(
                1, int(memory_raw.get("concept_drift_max_findings", 12))
            ),
            concept_drift_succession_min_cosine=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_drift_succession_min_cosine", 0.55
                        )
                    ),
                ),
            ),
            # Clamped at the synthesis dedupe cosine: above it the pair
            # would never have become two rows in the first place.
            concept_drift_succession_max_cosine=min(
                0.86,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_drift_succession_max_cosine", 0.86
                        )
                    ),
                ),
            ),
            concept_drift_succession_min_overlap=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_drift_succession_min_overlap", 0.25
                        )
                    ),
                ),
            ),
            concept_drift_succession_window_days=max(
                1.0,
                float(
                    memory_raw.get(
                        "concept_drift_succession_window_days", 120.0
                    )
                ),
            ),
            concept_relabel_enabled=bool(
                memory_raw.get("concept_relabel_enabled", True)
            ),
            concept_relabel_min_cosine=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("concept_relabel_min_cosine", 0.80)),
                ),
            ),
            concept_relabel_cooldown_days=max(
                0.0,
                float(memory_raw.get("concept_relabel_cooldown_days", 21.0)),
            ),
            concept_relabel_max_per_run=max(
                1, int(memory_raw.get("concept_relabel_max_per_run", 3))
            ),
            concept_relabel_scan_limit=max(
                1, int(memory_raw.get("concept_relabel_scan_limit", 40))
            ),
            concept_drift_relabel_min_tokens=max(
                1, int(memory_raw.get("concept_drift_relabel_min_tokens", 1))
            ),
            concept_reflection_min_salience=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get("concept_reflection_min_salience", 0.6)
                    ),
                ),
            ),
            concept_drift_pending_cap=max(
                1, int(memory_raw.get("concept_drift_pending_cap", 3))
            ),
            concept_reflection_min_axes=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("concept_reflection_min_axes", 0.3)),
                ),
            ),
            concept_reflection_cooldown_days=max(
                1.0,
                float(
                    memory_raw.get("concept_reflection_cooldown_days", 30.0)
                ),
            ),
            evolution_diary_interval_seconds=max(
                60,
                int(
                    memory_raw.get("evolution_diary_interval_seconds", 86400)
                ),
            ),
            evolution_diary_min_events=max(
                1, int(memory_raw.get("evolution_diary_min_events", 3))
            ),
            evolution_diary_min_salience=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get("evolution_diary_min_salience", 0.45)
                    ),
                ),
            ),
            evolution_diary_cooldown_days=max(
                0.0,
                float(memory_raw.get("evolution_diary_cooldown_days", 7.0)),
            ),
            concept_self_correction_evidence_floor=max(
                2,
                int(
                    memory_raw.get(
                        "concept_self_correction_evidence_floor", 3
                    )
                ),
            ),
            concept_self_correction_min_span_days=max(
                0.0,
                float(
                    memory_raw.get(
                        "concept_self_correction_min_span_days", 7.0
                    )
                ),
            ),
            concept_self_correction_min_salience=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_self_correction_min_salience", 0.5
                        )
                    ),
                ),
            ),
            concept_self_correction_similarity=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get(
                            "concept_self_correction_similarity", 0.55
                        )
                    ),
                ),
            ),
            concept_self_correction_cooldown_days=max(
                0.0,
                float(
                    memory_raw.get(
                        "concept_self_correction_cooldown_days", 14.0
                    )
                ),
            ),
            concept_self_correction_max_events=max(
                10,
                int(memory_raw.get("concept_self_correction_max_events", 200)),
            ),
            concept_self_correction_max_rules=max(
                1,
                int(memory_raw.get("concept_self_correction_max_rules", 2)),
            ),
            coactivation_min_pair_support=max(
                1,
                int(memory_raw.get("coactivation_min_pair_support", 2)),
            ),
            coactivation_min_strength=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("coactivation_min_strength", 0.25)),
                ),
            ),
            coactivation_max_modes=max(
                1,
                int(memory_raw.get("coactivation_max_modes", 4)),
            ),
            coactivation_max_reps_per_mode=max(
                2,
                int(memory_raw.get("coactivation_max_reps_per_mode", 4)),
            ),
            coactivation_quiet_min_days=max(
                0.0,
                float(memory_raw.get("coactivation_quiet_min_days", 10.0)),
            ),
            pre_thought_interval_seconds=max(
                60,
                int(memory_raw.get("pre_thought_interval_seconds", 3600)),
            ),
            thread_resummary_interval_seconds=max(
                60,
                int(memory_raw.get("thread_resummary_interval_seconds", 3600)),
            ),
            world_notice_interval_seconds=max(
                30,
                int(memory_raw.get("world_notice_interval_seconds", 300)),
            ),
            world_notice_cooldown_seconds=max(
                0,
                int(memory_raw.get("world_notice_cooldown_seconds", 3600)),
            ),
            world_notice_daily_cap=max(
                0,
                int(memory_raw.get("world_notice_daily_cap", 4)),
            ),
            world_notice_ttl_seconds=max(
                60,
                int(memory_raw.get("world_notice_ttl_seconds", 1800)),
            ),
            away_activities_interval_seconds=max(
                30,
                int(memory_raw.get("away_activities_interval_seconds", 1200)),
            ),
            away_activities_cooldown_seconds=max(
                0,
                int(memory_raw.get("away_activities_cooldown_seconds", 5400)),
            ),
            away_activities_daily_cap=max(
                0,
                int(memory_raw.get("away_activities_daily_cap", 6)),
            ),
            away_activities_min_gap_hours=max(
                0.0,
                float(memory_raw.get("away_activities_min_gap_hours", 4.0)),
            ),
            sleep_return_min_gap_hours=max(
                0.0,
                float(memory_raw.get("sleep_return_min_gap_hours", 5.0)),
            ),
            sleep_return_overnight_hours=max(
                0.0,
                float(memory_raw.get("sleep_return_overnight_hours", 9.0)),
            ),
            sleep_return_dream_lookback_hours=max(
                0.0,
                float(memory_raw.get("sleep_return_dream_lookback_hours", 18.0)),
            ),
            away_activities_journal_max=max(
                1,
                int(memory_raw.get("away_activities_journal_max", 8)),
            ),
            away_activities_llm_ratio=min(
                1.0,
                max(0.0, float(memory_raw.get("away_activities_llm_ratio", 0.5))),
            ),
            away_activities_episode_ratio=min(
                1.0,
                max(
                    0.0,
                    float(
                        memory_raw.get("away_activities_episode_ratio", 0.35)
                    ),
                ),
            ),
            away_activities_episode_max_beats=max(
                1,
                min(
                    4,
                    int(
                        memory_raw.get("away_activities_episode_max_beats", 3)
                    ),
                ),
            ),
            away_activities_episode_min_gap_seconds=max(
                0,
                int(
                    memory_raw.get(
                        "away_activities_episode_min_gap_seconds", 10800
                    )
                ),
            ),
            idle_seed_ratio=min(
                1.0,
                max(0.0, float(memory_raw.get("idle_seed_ratio", 0.25))),
            ),
            idle_seed_daily_cap=max(
                0, int(memory_raw.get("idle_seed_daily_cap", 3)),
            ),
            idle_seed_max_ring=max(
                1, int(memory_raw.get("idle_seed_max_ring", 6)),
            ),
            idle_seed_surface_cooldown_seconds=max(
                0,
                int(memory_raw.get("idle_seed_surface_cooldown_seconds", 1800)),
            ),
            hobby_worker_interval_seconds=max(
                60, int(memory_raw.get("hobby_worker_interval_seconds", 3600)),
            ),
            hobby_advance_min_hours=max(
                0.0, float(memory_raw.get("hobby_advance_min_hours", 6.0)),
            ),
            hobby_milestone_every=max(
                0, int(memory_raw.get("hobby_milestone_every", 3)),
            ),
            hobby_max_advances=max(
                0, int(memory_raw.get("hobby_max_advances", 12)),
            ),
            room_evolution_interval_seconds=max(
                60,
                int(memory_raw.get("room_evolution_interval_seconds", 21600)),
            ),
            room_evolution_min_hours=max(
                0.0, float(memory_raw.get("room_evolution_min_hours", 8.0)),
            ),
            garden_need_dry_days=max(
                0.0, float(memory_raw.get("garden_need_dry_days", 2.0)),
            ),
            garden_need_visit_floor_hours=max(
                0.0,
                float(memory_raw.get("garden_need_visit_floor_hours", 0.75)),
            ),
            garden_relax_ratio=min(
                1.0,
                max(0.0, float(memory_raw.get("garden_relax_ratio", 0.3))),
            ),
            garden_visit_min_minutes=max(
                0.5, float(memory_raw.get("garden_visit_min_minutes", 4.0)),
            ),
            garden_visit_max_minutes=max(
                0.5, float(memory_raw.get("garden_visit_max_minutes", 10.0)),
            ),
            garden_journal_max=max(
                1, int(memory_raw.get("garden_journal_max", 8)),
            ),
            outing_cooldown_hours=max(
                0.0, float(memory_raw.get("outing_cooldown_hours", 6.0)),
            ),
            outing_daily_cap=max(
                0, int(memory_raw.get("outing_daily_cap", 2)),
            ),
            circadian_settle_interval_seconds=max(
                60,
                int(memory_raw.get("circadian_settle_interval_seconds", 3600)),
            ),
            circadian_settle_after_seconds=max(
                0,
                int(memory_raw.get("circadian_settle_after_seconds", 7200)),
            ),
            diary_worker_interval_seconds=max(
                30,
                int(memory_raw.get("diary_worker_interval_seconds", 1800)),
            ),
            diary_worker_cooldown_seconds=max(
                0,
                int(memory_raw.get("diary_worker_cooldown_seconds", 10800)),
            ),
            diary_worker_daily_cap=max(
                0,
                int(memory_raw.get("diary_worker_daily_cap", 3)),
            ),
            diary_worker_min_context_chars=max(
                0,
                int(memory_raw.get("diary_worker_min_context_chars", 80)),
            ),
            forward_curiosity_interval_seconds=max(
                30,
                int(memory_raw.get("forward_curiosity_interval_seconds", 900)),
            ),
            forward_curiosity_cooldown_seconds=max(
                0,
                int(memory_raw.get("forward_curiosity_cooldown_seconds", 3600)),
            ),
            forward_curiosity_min_gap_hours=max(
                0.0,
                float(memory_raw.get("forward_curiosity_min_gap_hours", 4.0)),
            ),
            forward_curiosity_journal_max=max(
                1,
                int(memory_raw.get("forward_curiosity_journal_max", 8)),
            ),
            follow_up_journal_max=max(
                1,
                int(memory_raw.get("follow_up_journal_max", 8)),
            ),
            growth_witness_min_samples=max(
                2,
                int(memory_raw.get("growth_witness_min_samples", 10)),
            ),
            growth_witness_min_valence_delta=max(
                0.0,
                float(
                    memory_raw.get("growth_witness_min_valence_delta", 0.25)
                ),
            ),
            growth_witness_min_axis_delta=max(
                0.0,
                float(memory_raw.get("growth_witness_min_axis_delta", 0.30)),
            ),
            growth_witness_journal_max=max(
                1,
                int(memory_raw.get("growth_witness_journal_max", 4)),
            ),
            self_callback_min_age_days=max(
                1,
                int(memory_raw.get("self_callback_min_age_days", 14)),
            ),
            self_callback_journal_max=max(
                1,
                int(memory_raw.get("self_callback_journal_max", 4)),
            ),
            wellbeing_concern_window_days=max(
                1,
                int(memory_raw.get("wellbeing_concern_window_days", 7)),
            ),
            wellbeing_concern_late_night_min=max(
                1,
                int(memory_raw.get("wellbeing_concern_late_night_min", 3)),
            ),
            wellbeing_concern_neglect_min_days=max(
                1,
                int(memory_raw.get("wellbeing_concern_neglect_min_days", 2)),
            ),
            wellbeing_concern_rough_run=max(
                1,
                int(memory_raw.get("wellbeing_concern_rough_run", 5)),
            ),
            wellbeing_concern_rough_threshold=float(
                memory_raw.get("wellbeing_concern_rough_threshold", -0.25)
            ),
            wellbeing_concern_journal_max=max(
                1,
                int(memory_raw.get("wellbeing_concern_journal_max", 4)),
            ),
            shared_ritual_window_days=max(
                7,
                int(memory_raw.get("shared_ritual_window_days", 56)),
            ),
            shared_ritual_min_weeks=max(
                1,
                int(memory_raw.get("shared_ritual_min_weeks", 3)),
            ),
            shared_ritual_min_share=max(
                0.0,
                min(1.0, float(memory_raw.get("shared_ritual_min_share", 0.34))),
            ),
            shared_ritual_max_active=max(
                1,
                int(memory_raw.get("shared_ritual_max_active", 6)),
            ),
            shared_ritual_min_messages=max(
                1,
                int(memory_raw.get("shared_ritual_min_messages", 30)),
            ),
            voice_adoption_min_age_days=max(
                0.0,
                float(memory_raw.get("voice_adoption_min_age_days", 14.0)),
            ),
            voice_adoption_min_days_between=max(
                0.0,
                float(
                    memory_raw.get("voice_adoption_min_days_between", 10.0)
                ),
            ),
            voice_adoption_max_adopted=max(
                1,
                int(memory_raw.get("voice_adoption_max_adopted", 3)),
            ),
            voice_adoption_max_rendered=max(
                1,
                int(memory_raw.get("voice_adoption_max_rendered", 2)),
            ),
            flashbulb_enabled=bool(
                memory_raw.get("flashbulb_enabled", True),
            ),
            flashbulb_max_boost=max(
                0.0,
                min(1.0, float(memory_raw.get("flashbulb_max_boost", 0.35))),
            ),
            flashbulb_arousal_weight=max(
                0.0,
                float(memory_raw.get("flashbulb_arousal_weight", 0.6)),
            ),
            flashbulb_episode_weight=max(
                0.0,
                float(memory_raw.get("flashbulb_episode_weight", 0.7)),
            ),
            flashbulb_arousal_neutral=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("flashbulb_arousal_neutral", 0.4)),
                ),
            ),
            promise_followthrough_interval_seconds=max(
                30,
                int(
                    memory_raw.get(
                        "promise_followthrough_interval_seconds", 900,
                    )
                ),
            ),
            promise_followthrough_min_age_hours=max(
                0.0,
                float(
                    memory_raw.get("promise_followthrough_min_age_hours", 4.0)
                ),
            ),
            promise_followthrough_cooldown_hours=max(
                0.0,
                float(
                    memory_raw.get("promise_followthrough_cooldown_hours", 6.0)
                ),
            ),
            promise_followthrough_drop_after_days=max(
                1.0,
                float(
                    memory_raw.get(
                        "promise_followthrough_drop_after_days", 14.0,
                    )
                ),
            ),
            promise_fulfil_min_overlap=max(
                1,
                int(memory_raw.get("promise_fulfil_min_overlap", 3)),
            ),
            self_correction_min_confidence=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("self_correction_min_confidence", 0.6)),
                ),
            ),
            self_correction_min_overlap=max(
                1,
                int(memory_raw.get("self_correction_min_overlap", 2)),
            ),
            self_correction_max_candidates=max(
                1,
                int(memory_raw.get("self_correction_max_candidates", 50)),
            ),
            self_correction_cooldown_turns=max(
                0,
                int(memory_raw.get("self_correction_cooldown_turns", 3)),
            ),
            user_correction_min_confidence=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("user_correction_min_confidence", 0.4)),
                ),
            ),
            user_correction_min_overlap=max(
                1,
                int(memory_raw.get("user_correction_min_overlap", 2)),
            ),
            user_correction_max_candidates=max(
                1,
                int(memory_raw.get("user_correction_max_candidates", 50)),
            ),
            user_correction_interval_seconds=max(
                1,
                int(memory_raw.get("user_correction_interval_seconds", 45)),
            ),
            user_correction_max_per_run=max(
                1,
                int(memory_raw.get("user_correction_max_per_run", 8)),
            ),
            user_correction_concept_penalty=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("user_correction_concept_penalty", 0.25)),
                ),
            ),
            user_correction_confidence=min(
                1.0,
                max(
                    0.0,
                    float(memory_raw.get("user_correction_confidence", 0.9)),
                ),
            ),
            fact_reversal_min_delta=min(
                0.3,
                max(
                    0.0,
                    float(memory_raw.get("fact_reversal_min_delta", 0.25)),
                ),
            ),
            mood_inertia_mismatch_threshold=max(
                0.1,
                float(
                    memory_raw.get("mood_inertia_mismatch_threshold", 0.45)
                ),
            ),
            mood_inertia_cooldown_turns=max(
                0,
                int(memory_raw.get("mood_inertia_cooldown_turns", 3)),
            ),
            memory_extractor_max_tokens=max(
                256,
                int(memory_raw.get("memory_extractor_max_tokens", 1024)),
            ),
            memory_extractor_think=bool(
                memory_raw.get("memory_extractor_think", True)
            ),
            goal_max_active=max(
                1, int(memory_raw.get("goal_max_active", 5)),
            ),
            goal_max_progress_per_goal=max(
                1, int(memory_raw.get("goal_max_progress_per_goal", 12)),
            ),
            goal_reflection_interval_seconds=max(
                60,
                int(memory_raw.get("goal_reflection_interval_seconds", 3600)),
            ),
            conflict_detector_interval_seconds=max(
                60,
                int(
                    memory_raw.get("conflict_detector_interval_seconds", 1800),
                ),
            ),
            conflict_detector_similarity_min=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "conflict_detector_similarity_min", 0.80
                        ),
                    ),
                ),
            ),
            conflict_detector_similarity_max=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "conflict_detector_similarity_max", 0.92
                        ),
                    ),
                ),
            ),
            conflict_detector_auto_resolve_delta=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "conflict_detector_auto_resolve_delta", 0.30
                        ),
                    ),
                ),
            ),
            conflict_detector_max_corpus=max(
                10,
                int(memory_raw.get("conflict_detector_max_corpus", 1000)),
            ),
            conflict_detector_max_pairs_per_run=max(
                1,
                int(
                    memory_raw.get(
                        "conflict_detector_max_pairs_per_run", 50,
                    ),
                ),
            ),
            consolidation_interval_seconds=max(
                60,
                int(memory_raw.get("consolidation_interval_seconds", 21600)),
            ),
            consolidation_lookback_days=max(
                0,
                int(memory_raw.get("consolidation_lookback_days", 30)),
            ),
            consolidation_similarity_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get(
                            "consolidation_similarity_threshold", 0.90
                        ),
                    ),
                ),
            ),
            consolidation_max_corpus=max(
                10,
                int(memory_raw.get("consolidation_max_corpus", 1000)),
            ),
            consolidation_max_clusters_per_run=max(
                1,
                int(
                    memory_raw.get("consolidation_max_clusters_per_run", 20),
                ),
            ),
            consolidation_min_cluster_size=max(
                2,
                int(memory_raw.get("consolidation_min_cluster_size", 2)),
            ),
            belief_worker_interval_seconds=max(
                60,
                int(memory_raw.get("belief_worker_interval_seconds", 1200)),
            ),
            belief_worker_lookback_turns=max(
                1,
                int(memory_raw.get("belief_worker_lookback_turns", 12)),
            ),
            belief_worker_interest_top_n=max(
                0,
                int(memory_raw.get("belief_worker_interest_top_n", 5)),
            ),
            belief_worker_reconsider_max=max(
                0,
                int(memory_raw.get("belief_worker_reconsider_max", 3)),
            ),
            promise_worker_interval_seconds=max(
                60,
                int(memory_raw.get("promise_worker_interval_seconds", 600)),
            ),
            promise_worker_lookback_turns=max(
                1,
                int(memory_raw.get("promise_worker_lookback_turns", 12)),
            ),
            promise_worker_max_per_run=max(
                1,
                int(memory_raw.get("promise_worker_max_per_run", 5)),
            ),
            promise_worker_max_msg_chars=max(
                200,
                int(memory_raw.get("promise_worker_max_msg_chars", 2000)),
            ),
            promise_worker_max_transcript_chars=max(
                500,
                int(
                    memory_raw.get(
                        "promise_worker_max_transcript_chars", 8000
                    )
                ),
            ),
            belief_gap_valence_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("belief_gap_valence_threshold", 0.30)),
                ),
            ),
            belief_gap_arousal_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("belief_gap_arousal_threshold", 0.25)),
                ),
            ),
            belief_recent_window_hours=max(
                1,
                int(memory_raw.get("belief_recent_window_hours", 24)),
            ),
            belief_stale_after_days=max(
                1,
                int(memory_raw.get("belief_stale_after_days", 90)),
            ),
            belief_max_active_per_user=max(
                10,
                int(memory_raw.get("belief_max_active_per_user", 200)),
            ),
            novelty_window=max(
                2,
                int(memory_raw.get("novelty_window", 12)),
            ),
            novelty_warmup_min=max(
                2,
                int(memory_raw.get("novelty_warmup_min", 3)),
            ),
            novelty_mild_threshold=max(
                0.0,
                min(
                    2.0,
                    float(memory_raw.get("novelty_mild_threshold", 0.35)),
                ),
            ),
            novelty_strong_threshold=max(
                0.0,
                min(
                    2.0,
                    float(memory_raw.get("novelty_strong_threshold", 0.55)),
                ),
            ),
            novelty_cooldown_turns=max(
                0,
                int(memory_raw.get("novelty_cooldown_turns", 2)),
            ),
            stagnation_window=max(
                2,
                int(memory_raw.get("stagnation_window", 6)),
            ),
            stagnation_mild_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("stagnation_mild_threshold", 0.18)),
                ),
            ),
            stagnation_strong_threshold=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("stagnation_strong_threshold", 0.10)),
                ),
            ),
            stagnation_cooldown_turns=max(
                0,
                int(memory_raw.get("stagnation_cooldown_turns", 4)),
            ),
            stagnation_post_novelty_suppression_turns=max(
                0,
                int(
                    memory_raw.get(
                        "stagnation_post_novelty_suppression_turns", 3,
                    )
                ),
            ),
            topic_tracking_min_sim=max(
                0.0,
                min(
                    1.0,
                    float(memory_raw.get("topic_tracking_min_sim", 0.30)),
                ),
            ),
            idle_worker_wake_seconds=max(
                1.0, float(memory_raw.get("idle_worker_wake_seconds", 60.0))
            ),
            idle_worker_quiet_threshold_seconds=max(
                0,
                int(memory_raw.get("idle_worker_quiet_threshold_seconds", 30)),
            ),
            idle_worker_tick_budget_ms=max(
                0,
                int(memory_raw.get("idle_worker_tick_budget_ms", 3000)),
            ),
            idle_worker_max_per_tick=max(
                0,
                int(memory_raw.get("idle_worker_max_per_tick", 0)),
            ),
            idle_worker_pressure_enabled=bool(
                memory_raw.get("idle_worker_pressure_enabled", True),
            ),
            idle_worker_compute_budget_ms=max(
                0,
                int(memory_raw.get("idle_worker_compute_budget_ms", 6000)),
            ),
            idle_worker_urgency_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("idle_worker_urgency_threshold", 0.35)
                    ),
                ),
            ),
            idle_worker_min_interval_ratio=max(
                0.0,
                min(
                    1.0,
                    float(
                        memory_raw.get("idle_worker_min_interval_ratio", 0.1)
                    ),
                ),
            ),
            idle_worker_depth_max_multiplier=max(
                1.0,
                float(
                    memory_raw.get("idle_worker_depth_max_multiplier", 10.0)
                ),
            ),
            idle_worker_contention_override=str(
                memory_raw.get("idle_worker_contention_override", "auto")
                or "auto"
            ).strip().lower(),
    )

