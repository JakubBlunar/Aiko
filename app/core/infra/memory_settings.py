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
    # Promotion / demotion / cleanup gates used by
    # :class:`MemoryPromotionWorker`.
    scratchpad_ttl_days: int = 14
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
    # Wall-clock cooldown between callbacks (hours). Long for rarity.
    long_arc_callback_cooldown_hours: float = 6.0
    # At most this many callbacks per session, regardless of cooldown.
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
    # Max drafts per local day. Small — at most a couple of "this reminds me
    # of ..." beats are available to surface in a day.
    associative_wander_daily_cap: int = 2
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
    # Max drift cues drafted per local day. Small — drift is a rare,
    # slow-burn signal.
    interest_drift_daily_cap: int = 3
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
    # Max re-openers drafted per local day. Tiny — re-opening a dropped
    # thread is a rare, warm beat, never a sweep.
    dormant_interest_daily_cap: int = 2
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
    # Max curiosity-edge cues drafted per local day. Small — rarity matters.
    curiosity_gradient_daily_cap: int = 3
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
    forward_curiosity_daily_cap: int = 4
    forward_curiosity_min_gap_hours: float = 4.0
    forward_curiosity_journal_max: int = 8
    # FollowUpWorker cue ring size (``aiko.follow_up_cues``). Bounds the
    # number of drafted "ask how their plan went" cues kept around.
    follow_up_journal_max: int = 8
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



def parse_memory_settings(memory_raw: dict[str, Any]) -> "MemorySettings":
    return MemorySettings(
            enabled=bool(memory_raw.get("enabled", True)),
            top_k=max(0, int(memory_raw.get("top_k", 6))),
            score_threshold=max(0.0, min(1.0, float(memory_raw.get("score_threshold", 0.4)))),
            max_memories=max(50, int(memory_raw.get("max_memories", 5000))),
            dedupe_threshold=max(0.5, min(0.999, float(memory_raw.get("dedupe_threshold", 0.92)))),
            extractor_enabled=bool(memory_raw.get("extractor_enabled", True)),
            self_tagged_salience=max(0.0, min(1.0, float(memory_raw.get("self_tagged_salience", 0.7)))),
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
            scratchpad_ttl_days=max(
                1, int(memory_raw.get("scratchpad_ttl_days", 14))
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
            long_arc_callback_cooldown_hours=max(
                0.0,
                float(memory_raw.get("long_arc_callback_cooldown_hours", 6.0)),
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
            associative_wander_daily_cap=max(
                0,
                int(memory_raw.get("associative_wander_daily_cap", 2)),
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
            interest_drift_daily_cap=max(
                0,
                int(memory_raw.get("interest_drift_daily_cap", 3)),
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
            dormant_interest_daily_cap=max(
                0,
                int(memory_raw.get("dormant_interest_daily_cap", 2)),
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
            curiosity_gradient_daily_cap=max(
                0,
                int(memory_raw.get("curiosity_gradient_daily_cap", 3)),
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
            forward_curiosity_daily_cap=max(
                0,
                int(memory_raw.get("forward_curiosity_daily_cap", 4)),
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
    )

