"""Interest-driven knowledge enrichment worker (F9 personality backlog).

Where G3 (:class:`IdleCuriosityWorker`) answers Aiko's own
``open_question`` memories, F9 is the engine that *fills* a real,
queryable knowledge pool from the user's recurring interests. On an
idle tick it reads the K9 topic graph, picks the densest
under-researched interest cluster, web-searches it, distils the
results into one or two distilled, impersonal ``knowledge`` facts
(F8), and writes them with a ``source_url`` (F4). Over weeks this is
what turns "I like a genre" into "I can name things in it".

Design notes:

* **Strictly silent.** F9 never fires a proactive message. The new
  ``knowledge`` rows just quietly make Aiko's next on-topic reply
  sharper (the F8 retrieval boost surfaces them on informational
  turns; the K61 inner-life block tells her to commit to specifics).
* **Off the brain path.** Runs on the idle scheduler against the
  maintenance worker model, so the synchronous chat turn never pays
  for it. The only per-turn cost F8/K61 add is a local RAG search.
* **Privacy first.** A cluster summary is derived from the user's
  conversation, so it can carry names / pronouns. The same scrubber
  that guards F1/G3 runs on the search query; a cluster that won't
  scrub is put on cooldown so it stops consuming ticks.
* **Impersonal only.** The distil prompt is explicit: extract
  evergreen, impersonal facts (names, titles, dates, examples) —
  never anything about a specific person in the conversation. This
  keeps ``knowledge`` cleanly distinct from personal ``fact``/``event``
  memory.
* **No grinding.** A per-cluster wall-clock cooldown plus tight
  hour/day search caps (its own ``FactCheckRateLimiter`` budget, keyed
  on ``"idle_knowledge.rate_state"``) keep it from re-researching the
  same interest or dumping a wall of facts after a long absence.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory.memory_store import MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.idle_knowledge_worker")


# ── prompt template ────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You extract general, evergreen knowledge from web search excerpts "
    "about a topic. Reply with ONE JSON object on a single line and "
    "nothing else. Schema: {\"facts\": [{\"text\": \"<= 200 chars, one "
    "concrete impersonal fact -- names, titles, dates, examples; NEVER "
    "about any specific person in a conversation\", \"confidence\": "
    "<number in [0, 1]>}]}. Return at most 2 facts. Facts must be "
    "evergreen (not news, not time-sensitive). If the excerpts are "
    "off-topic, thin, or contradictory, return an empty list. Never "
    "invent facts the excerpts don't support."
)

_USER_TEMPLATE = (
    "TOPIC: {topic}\n"
    "EXCERPTS:\n{excerpts}"
)

# Research-planner prompt. Runs BEFORE any web search to (a) judge whether
# a conversation-derived interest cluster has an evergreen, impersonal
# subject worth researching at all, and (b) turn it into neutral search
# queries with every personal detail stripped. ``RESEARCH_QUERIES`` is the
# recognisable marker the test stub keys on; keep it in the system text.
_PLAN_SYSTEM_PROMPT = (
    "You convert a person's private conversation interests into neutral, "
    "general web-search queries (RESEARCH_QUERIES) about the GENERAL subject "
    "matter, with ALL personal details removed. Reply with ONE JSON object on "
    "a single line and nothing else. Schema: {\"researchable\": <bool>, "
    "\"queries\": [\"<concise impersonal web search query>\", ...]}. "
    "Set researchable=false and queries=[] when the material is purely about "
    "the people in the conversation -- their relationship, feelings, plans, "
    "schedules, health, or private events with NO general subject anyone "
    "could look up (anniversaries, how someone's day went, inside jokes, who "
    "said what, milestones together). Set researchable=true ONLY when there "
    "is an evergreen subject worth researching: a hobby, craft, place, work "
    "of art, food, sport, technology, science, history, or named entity. "
    "Each query MUST be impersonal: no person's name, no pronouns referring "
    "to the conversation participants, no private specifics -- just the topic "
    "someone would type into a search engine. Return at most {max_queries} "
    "short queries, each a DIFFERENT angle on the subject."
)

_PLAN_USER_TEMPLATE = (
    "INTEREST SUMMARY: {summary}\n"
    "RELATED NOTES:\n{notes}"
)

# Caps on the planner prompt + how many member notes we feed it.
_PLAN_MAX_TOKENS = 600
_PLAN_MAX_NOTES = 10
_PLAN_NOTE_CHARS = 160
_PLAN_QUERY_CHARS = 200

# Coverage-weighted cluster scoring. Blends how much room a cluster still
# has to learn (knowledge headroom), its conversational density (size),
# and freshness (never/long-ago researched) so a single big cluster can't
# monopolise the worker. Weights sum to 1.0.
_SCORE_W_ROOM = 0.45
_SCORE_W_SIZE = 0.35
_SCORE_W_FRESH = 0.20

# kv_meta key holding the per-cluster research queue
# ``{cluster_key: [{"query", "topic", "added_at"}, ...]}``.
_KV_RESEARCH_QUEUE = "aiko.knowledge_worker.research_queue"
# Cap the total queued queries across all clusters so the blob stays small.
_RESEARCH_QUEUE_CAP = 60


# Caps on the prompt so a long search result can't blow up the context.
_MAX_SNIPPET_CHARS = 400
_MAX_EXCERPTS = 4
_DISTIL_MAX_TOKENS = 220
_MAX_FACTS = 2

# Confidence floor for accepting a distilled fact, and the cap on what
# we ever stamp (we never call a scraped fact "verified").
_MIN_FACT_CONFIDENCE = 0.6
_MAX_FACT_CONFIDENCE = 0.9

# Salience for a fresh knowledge row — slightly above the 0.5 default
# so a learned fact has a small leg-up, but below a curiosity_finding
# (0.65) since those were anchored on something Aiko explicitly
# wondered about.
_KNOWLEDGE_SALIENCE = 0.6

# Cap how much of any text we render in a single log line.
_LOG_PREVIEW_CHARS = 200

# kv_meta key holding the per-cluster cooldown map ``{cluster_key: iso}``.
_KV_CLUSTER_COOLDOWNS = "aiko.knowledge_worker.cluster_cooldowns"

# Shortest usable cluster summary (chars). Below this there isn't a
# real topic to research.
_MIN_TOPIC_CHARS = 8

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
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
    return dt


def _cooldown_at(entry: object) -> datetime | None:
    """Extract the 'stamped at' time from a cooldown entry (str or dict)."""
    if isinstance(entry, dict):
        return _parse_iso(entry.get("at"))
    return _parse_iso(entry)


def _preview(text: str | None) -> str:
    if not text:
        return "<empty>"
    flat = " ".join(str(text).split())
    if len(flat) > _LOG_PREVIEW_CHARS:
        return flat[: _LOG_PREVIEW_CHARS - 1] + "…"
    return flat


def _cluster_key(summary: str) -> str:
    """Stable per-topic cooldown key.

    Hashed off the normalised summary text rather than the topic
    graph's ``cluster_id`` (which is not stable across rebuilds) so a
    re-clustered topic keeps its cooldown.
    """
    norm = " ".join((summary or "").lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class KnowledgeFact:
    """One distilled, impersonal fact."""

    text: str
    confidence: float


@dataclass(frozen=True)
class _ClusterPick:
    cluster_key: str
    topic: str
    cluster_id: int
    size: int
    members: tuple[str, ...] = ()


class IdleKnowledgeWorker:
    """IdleWorker that turns topic-graph interests into ``knowledge`` rows."""

    name = "idle_knowledge"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        embedder: "Embedder",
        ollama: "OllamaClient",
        chat_model: str,
        web_search_tool: Any,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        topic_graph_provider: Callable[[], "TopicGraph | None"],
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        user_names_provider: Callable[[], list[str]] | None = None,
        assistant_name_provider: Callable[[], str | None] | None = None,
        notify_memory_added: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        query_reformulator: Callable[[str], str | None] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._web_search = web_search_tool
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._topic_graph_provider = topic_graph_provider
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._user_names_provider = user_names_provider
        self._assistant_name_provider = assistant_name_provider
        self._notify_memory_added = notify_memory_added
        self._clock = clock or _utcnow
        self._query_reformulator = query_reformulator

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "knowledge_enrichment_interval_seconds",
                3600,
            )
        )

    def _enabled(self) -> bool:
        return bool(
            getattr(self._agent_settings, "knowledge_enrichment_enabled", True)
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not self._enabled():
            return False
        if not default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        ):
            return False
        snapshot = self._rate_limiter.snapshot(now)
        if snapshot["hour_used"] >= snapshot["hour_cap"]:
            return False
        if snapshot["day_used"] >= snapshot["day_cap"]:
            return False
        # Cheapest "is there anything to research" check last — it
        # rebuilds the (cached) topic graph, so we only pay it once the
        # interval + budget gates have already passed.
        return self._pick_cluster(now=now) is not None

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}
        now = self._clock()
        candidates = self._score_candidates(now=now)
        if not candidates:
            return {"skipped": True, "reason": "no_cluster"}

        pick, raw_query, extra_queries, from_queue = self._choose_research(
            candidates, now=now,
        )
        if pick is None or raw_query is None:
            return {"skipped": True, "reason": "no_researchable_topic"}

        if not self._rate_limiter.allow(now):
            log.info(
                "knowledge skip: rate limited (cluster=%s query=%r)",
                pick.cluster_key, _preview(raw_query),
            )
            return {"skipped": True, "reason": "rate_limited"}

        # Normal per-cluster cooldown so the next tick rotates to a
        # different interest; queue the planner's extra angles so this
        # cluster is mined further when it comes back up.
        self._stamp_cooldown(pick.cluster_key, now=now)
        if extra_queries:
            self._enqueue_queries(
                pick.cluster_key, pick.topic, extra_queries, now=now,
            )

        log.info(
            "knowledge start: cluster=%s size=%d query=%r queued=%d "
            "from_queue=%s",
            pick.cluster_key, pick.size, _preview(raw_query),
            len(extra_queries), from_queue,
        )

        safe_query = self._scrub(raw_query)
        if safe_query is None:
            log.info(
                "knowledge skip: privacy gate dropped query cluster=%s",
                pick.cluster_key,
            )
            return {"skipped": True, "reason": "privacy_gate"}
        log.info(
            "knowledge scrubbed: cluster=%s safe_query=%r",
            pick.cluster_key, _preview(safe_query),
        )

        search_t0 = time.monotonic()
        try:
            snippets = self._search(safe_query)
        except Exception:
            search_ms = (time.monotonic() - search_t0) * 1000.0
            log.warning(
                "knowledge search failed: cluster=%s elapsed_ms=%.0f",
                pick.cluster_key, search_ms, exc_info=True,
            )
            return {"errored": True, "reason": "search_failed"}
        search_ms = (time.monotonic() - search_t0) * 1000.0
        log.info(
            "knowledge search done: cluster=%s elapsed_ms=%.0f "
            "result_count=%d",
            pick.cluster_key, search_ms, len(snippets),
        )
        if self._cancel_event.is_set():
            return {"cancelled": True}
        if not snippets:
            return {
                "checked": 1, "cluster": pick.cluster_key,
                "outcome": "no_results", "wrote": 0,
            }

        distil_t0 = time.monotonic()
        facts = self._distil(safe_query, snippets)
        distil_ms = (time.monotonic() - distil_t0) * 1000.0
        if facts is None:
            log.info(
                "knowledge distil cancel/parse-fail: cluster=%s "
                "elapsed_ms=%.0f",
                pick.cluster_key, distil_ms,
            )
            return {"cancelled": True}
        log.info(
            "knowledge distil done: cluster=%s elapsed_ms=%.0f facts=%d",
            pick.cluster_key, distil_ms, len(facts),
        )
        if not facts:
            return {
                "checked": 1, "cluster": pick.cluster_key,
                "outcome": "inconclusive", "wrote": 0,
            }

        source_urls = [s["url"] for s in snippets if s.get("url")]
        primary_url = source_urls[0] if source_urls else ""
        wrote: list[int] = []
        deduped = 0
        for fact in facts:
            if self._cancel_event.is_set():
                break
            mem_id = self._write_knowledge(
                fact=fact,
                topic=safe_query,
                source_url=primary_url,
                source_urls=source_urls,
                cluster_key=pick.cluster_key,
                now=now,
            )
            if mem_id is None:
                deduped += 1
            else:
                wrote.append(mem_id)
        log.info(
            "knowledge apply done: cluster=%s wrote=%d deduped=%d "
            "memory_ids=%s",
            pick.cluster_key, len(wrote), deduped, wrote,
        )
        return {
            "checked": 1,
            "cluster": pick.cluster_key,
            "topic": safe_query[:120],
            "outcome": "wrote" if wrote else "deduped",
            "wrote": len(wrote),
            "deduped": deduped,
            "memory_ids": wrote,
        }

    def _choose_research(
        self, candidates: list[_ClusterPick], *, now: datetime,
    ) -> tuple[_ClusterPick | None, str | None, list[str], bool]:
        """Walk ranked candidates until one yields a query to research.

        Returns ``(pick, query, extra_queries, from_queue)``. For each
        candidate (up to ``knowledge_enrichment_max_clusters_per_run``):

        1. Drain a previously-queued subtopic if the cluster has one
           (deepening an interest from a fresh angle without re-planning).
        2. Otherwise run the LLM planner. ``None`` (undetermined) falls back
           to the legacy "search the scrubbed summary" path; ``[]``
           (explicitly unresearchable) puts the cluster on a long cooldown
           and advances to the next candidate; a non-empty list researches
           the first query now and returns the rest to be queued.
        """
        extraction_enabled = self._topic_extraction_enabled()
        max_try = max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "knowledge_enrichment_max_clusters_per_run",
                    3,
                )
            ),
        )
        unresearchable_hours = float(
            getattr(
                self._memory_settings,
                "knowledge_unresearchable_cooldown_hours",
                336,
            )
        )
        for pick in candidates[:max_try]:
            if self._cancel_event.is_set():
                break
            queued = self._pop_queued_query(pick.cluster_key)
            if queued is not None:
                return pick, queued, [], True
            if not extraction_enabled:
                return pick, pick.topic, [], False
            queries = self._plan_research(pick)
            if queries is None:
                # Undetermined -> legacy fallback so a flaky planner never
                # permanently blocks an otherwise valid interest.
                return pick, pick.topic, [], False
            if not queries:
                self._stamp_cooldown(
                    pick.cluster_key, now=now, hours=unresearchable_hours,
                )
                log.info(
                    "knowledge skip: planner judged cluster unresearchable "
                    "cluster=%s topic=%r",
                    pick.cluster_key, _preview(pick.topic),
                )
                continue
            return pick, queries[0], queries[1:], False
        return None, None, [], False

    # ── cluster selection ────────────────────────────────────────────

    def _pick_cluster(self, *, now: datetime) -> _ClusterPick | None:
        """Best under-researched cluster (coverage-weighted), or ``None``.

        Thin wrapper over :meth:`_score_candidates` used by the cheap
        ``is_ready`` gate -- it only needs to know whether *anything* is
        worth a tick.
        """
        candidates = self._score_candidates(now=now)
        return candidates[0] if candidates else None

    def _score_candidates(self, *, now: datetime) -> list[_ClusterPick]:
        """Eligible clusters ranked best-first by a coverage-weighted score.

        Instead of always grabbing the single densest cluster (which lets
        one big interest monopolise the worker), each eligible cluster is
        scored on a blend of knowledge headroom, conversational size, and
        freshness. Clusters already at their per-cluster knowledge ceiling,
        with too-short summaries, or inside their cooldown window are
        dropped entirely.
        """
        try:
            topic_graph = self._topic_graph_provider()
        except Exception:
            log.debug("knowledge: topic_graph_provider raised", exc_info=True)
            return []
        if topic_graph is None:
            return []
        try:
            from app.core.conversation.topic_graph import (
                build_topic_graph_snapshot,
            )

            snapshot = build_topic_graph_snapshot(
                topic_graph, self._memory_store,
            )
        except Exception:
            log.debug("knowledge: snapshot build raised", exc_info=True)
            return []
        if not snapshot.get("enabled") or not snapshot.get("clusters"):
            return []

        max_per_cluster = max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "knowledge_enrichment_max_per_cluster",
                    3,
                )
            ),
        )
        cooldowns = self._load_cooldowns()
        cooldown_window = timedelta(
            hours=max(
                0.0,
                float(
                    getattr(
                        self._memory_settings,
                        "knowledge_cluster_cooldown_hours",
                        72,
                    )
                ),
            )
        )
        clusters = snapshot["clusters"]
        max_size = max(
            (int(c.get("size", 0)) for c in clusters), default=1,
        ) or 1

        scored: list[tuple[float, int, _ClusterPick]] = []
        for cluster in clusters:
            topic = (cluster.get("summary") or "").strip()
            if len(topic) < _MIN_TOPIC_CHARS:
                continue
            kind_counts = cluster.get("kind_counts") or {}
            knowledge_count = int(kind_counts.get("knowledge", 0))
            if knowledge_count >= max_per_cluster:
                continue
            key = _cluster_key(topic)
            entry = cooldowns.get(key)
            if self._cooldown_active(entry, now, cooldown_window):
                continue
            last = _cooldown_at(entry)
            size = int(cluster.get("size", 0))
            room_frac = (max_per_cluster - knowledge_count) / max_per_cluster
            size_frac = size / max_size
            # Never-researched -> fully fresh; otherwise older = fresher,
            # saturating at one cooldown window past the cooldown's end.
            if last is None:
                fresh_frac = 1.0
            else:
                age_h = (now - last).total_seconds() / 3600.0
                window_h = cooldown_window.total_seconds() / 3600.0 or 1.0
                fresh_frac = max(0.0, min(1.0, age_h / (2.0 * window_h)))
            score = (
                _SCORE_W_ROOM * room_frac
                + _SCORE_W_SIZE * size_frac
                + _SCORE_W_FRESH * fresh_frac
            )
            members = tuple(
                str(m.get("content") or "").strip()
                for m in (cluster.get("members") or [])
                if str(m.get("content") or "").strip()
            )
            pick = _ClusterPick(
                cluster_key=key,
                topic=topic,
                cluster_id=int(cluster.get("cluster_id", 0)),
                size=size,
                members=members,
            )
            # Sort by score desc, then size desc as a stable tiebreak.
            scored.append((score, size, pick))

        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [pick for _, _, pick in scored]

    def _load_cooldowns(self) -> dict[str, str]:
        try:
            raw = self._kv_get(_KV_CLUSTER_COOLDOWNS)
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _stamp_cooldown(
        self, cluster_key: str, *, now: datetime, hours: float | None = None,
    ) -> None:
        """Put a cluster on cooldown.

        With no ``hours`` the entry is the legacy ``iso`` string (blocked
        for the standard ``knowledge_cluster_cooldown_hours`` window). With
        ``hours`` set (used for clusters the planner judged unresearchable)
        the entry becomes ``{"at": iso, "until": iso}`` so the longer block
        is honoured independent of the standard window.
        """
        cooldowns = self._load_cooldowns()
        if hours is None:
            cooldowns[cluster_key] = now.isoformat()
        else:
            until = now + timedelta(hours=max(0.0, float(hours)))
            cooldowns[cluster_key] = {
                "at": now.isoformat(),
                "until": until.isoformat(),
            }
        # Prune obviously-stale entries so the blob can't grow without
        # bound (anything older than 30 days is well past any cooldown).
        cutoff = now - timedelta(days=30)
        pruned = {
            k: v
            for k, v in cooldowns.items()
            if (_cooldown_at(v) or now) >= cutoff
        }
        try:
            self._kv_set(_KV_CLUSTER_COOLDOWNS, json.dumps(pruned))
        except Exception:
            log.debug("knowledge cooldown persist failed", exc_info=True)

    @staticmethod
    def _cooldown_active(
        entry: object, now: datetime, window: timedelta,
    ) -> bool:
        """True when ``entry`` still blocks selection at ``now``."""
        if entry is None:
            return False
        if isinstance(entry, dict):
            until = _parse_iso(entry.get("until"))
            return until is not None and now < until
        last = _parse_iso(entry)
        return last is not None and now - last < window

    # ── research queue ───────────────────────────────────────────────

    def _load_queue(self) -> dict[str, list[dict[str, str]]]:
        try:
            raw = self._kv_get(_KV_RESEARCH_QUEUE)
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, list[dict[str, str]]] = {}
        for key, items in data.items():
            if isinstance(items, list):
                out[str(key)] = [i for i in items if isinstance(i, dict)]
        return out

    def _save_queue(self, queue: dict[str, list[dict[str, str]]]) -> None:
        # Drop empty buckets and enforce a global cap (oldest-first) so the
        # blob can't grow without bound.
        flat: list[tuple[str, dict[str, str]]] = []
        for key, items in queue.items():
            for item in items:
                flat.append((key, item))
        if len(flat) > _RESEARCH_QUEUE_CAP:
            flat.sort(key=lambda t: str(t[1].get("added_at", "")))
            flat = flat[-_RESEARCH_QUEUE_CAP:]
        rebuilt: dict[str, list[dict[str, str]]] = {}
        for key, item in flat:
            rebuilt.setdefault(key, []).append(item)
        try:
            self._kv_set(_KV_RESEARCH_QUEUE, json.dumps(rebuilt))
        except Exception:
            log.debug("knowledge queue persist failed", exc_info=True)

    def _enqueue_queries(
        self,
        cluster_key: str,
        topic: str,
        queries: list[str],
        *,
        now: datetime,
    ) -> None:
        if not queries:
            return
        queue = self._load_queue()
        bucket = queue.setdefault(cluster_key, [])
        existing = {str(i.get("query", "")).strip().lower() for i in bucket}
        for query in queries:
            norm = query.strip().lower()
            if not norm or norm in existing:
                continue
            existing.add(norm)
            bucket.append({
                "query": query.strip(),
                "topic": topic[:200],
                "added_at": now.isoformat(),
            })
        self._save_queue(queue)

    def _pop_queued_query(self, cluster_key: str) -> str | None:
        """Pop the next queued subtopic for a cluster (FIFO), or ``None``."""
        queue = self._load_queue()
        bucket = queue.get(cluster_key)
        if not bucket:
            return None
        item = bucket.pop(0)
        if not bucket:
            queue.pop(cluster_key, None)
        self._save_queue(queue)
        query = str(item.get("query", "")).strip()
        return query or None

    # ── research planner ─────────────────────────────────────────────

    def _topic_extraction_enabled(self) -> bool:
        return bool(
            getattr(
                self._agent_settings,
                "knowledge_topic_extraction_enabled",
                True,
            )
        )

    def _plan_research(self, pick: _ClusterPick) -> list[str] | None:
        """Ask the worker LLM for impersonal research queries on a cluster.

        Returns:
          * ``[...]`` -- one or more clean, impersonal queries to research.
          * ``[]``    -- the planner judged the cluster unresearchable
                         (purely personal); the caller should skip it.
          * ``None``  -- the call failed / parse error / cancel; the caller
                         falls back to the legacy "scrub the summary" path
                         so a flaky model never permanently blocks research.
        """
        max_queries = max(
            1,
            int(
                getattr(
                    self._memory_settings,
                    "knowledge_research_queries_per_cluster",
                    3,
                )
            ),
        )
        notes = "\n".join(
            f"- {note[:_PLAN_NOTE_CHARS]}"
            for note in pick.members[:_PLAN_MAX_NOTES]
        ) or "(no additional notes)"
        system = _PLAN_SYSTEM_PROMPT.replace("{max_queries}", str(max_queries))
        user = _PLAN_USER_TEMPLATE.format(summary=pick.topic, notes=notes)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": _PLAN_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                # Planning which search queries to run to fill a knowledge
                # gap is a reasoning task. The distil pass below stays
                # think=False (mechanical summarisation). Headroom added
                # client-side so the query plan isn't starved.
                think=True,
                surface="idle_knowledge_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("knowledge plan call raised", exc_info=True)
            return None
        if self._cancel_event.is_set():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            return None
        return self._parse_plan(raw, max_queries=max_queries)

    @staticmethod
    def _parse_plan(raw: str, *, max_queries: int) -> list[str] | None:
        """Parse the planner JSON. See :meth:`_plan_research` for return contract."""
        match = _JSON_OBJECT_RE.search(raw.strip())
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        raw_queries = parsed.get("queries")
        # No queries key at all -> the model didn't speak the planner
        # schema (e.g. a fallback/old response); signal "undetermined".
        if raw_queries is None:
            return None
        if parsed.get("researchable") is False:
            return []
        if not isinstance(raw_queries, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in raw_queries:
            text = str(item or "").strip()
            if not text:
                continue
            if len(text) > _PLAN_QUERY_CHARS:
                text = text[: _PLAN_QUERY_CHARS - 1].rsplit(" ", 1)[0] + "…"
            norm = text.lower()
            if norm in seen:
                continue
            seen.add(norm)
            out.append(text)
            if len(out) >= max_queries:
                break
        return out

    # ── pieces ───────────────────────────────────────────────────────

    def _scrub(self, topic_text: str) -> str | None:
        """Privacy-scrub the topic into a safe search query.

        Uses the F6 local-LLM reformulation when a reformulator is wired
        (rewrite -> deterministic post-filter); otherwise falls back to
        the deterministic scrub directly.
        """
        from app.core.memory.fact_check_privacy import scrub_claim_for_search

        user_names: list[str] | None = None
        if self._user_names_provider is not None:
            try:
                provided = self._user_names_provider()
                if provided:
                    user_names = list(provided)
            except Exception:
                user_names = None
        assistant_name: str | None = None
        if self._assistant_name_provider is not None:
            try:
                assistant_name = self._assistant_name_provider() or None
            except Exception:
                assistant_name = None
        if self._query_reformulator is not None:
            from app.core.memory.query_reformulation import (
                reformulate_query_for_search,
            )

            return reformulate_query_for_search(
                topic_text,
                reformulate_fn=self._query_reformulator,
                user_names=user_names,
                assistant_name=assistant_name,
            )
        return scrub_claim_for_search(
            topic_text,
            user_names=user_names,
            assistant_name=assistant_name,
        )

    def _search(self, safe_query: str) -> list[dict[str, str]]:
        if self._web_search is None:
            return []
        result_text = self._web_search.run(
            {"query": safe_query, "max_results": _MAX_EXCERPTS},
        )
        try:
            parsed = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            return []
        results = parsed.get("results", []) if isinstance(parsed, dict) else []
        out: list[dict[str, str]] = []
        for item in results[:_MAX_EXCERPTS]:
            if not isinstance(item, dict):
                continue
            snippet = str(item.get("snippet") or item.get("body") or "").strip()
            if not snippet:
                continue
            out.append({
                "title": str(item.get("title", ""))[:120],
                "url": str(item.get("url", ""))[:200],
                "snippet": snippet[:_MAX_SNIPPET_CHARS],
            })
        return out

    def _distil(
        self,
        safe_query: str,
        snippets: list[dict[str, str]],
    ) -> list[KnowledgeFact] | None:
        """Distil up to two impersonal facts; ``None`` on cancel/parse-fail."""
        excerpts_text = "\n".join(
            f"- {s['title']} ({s['url']}): {s['snippet']}"
            for s in snippets[:_MAX_EXCERPTS]
        )
        user_content = _USER_TEMPLATE.format(
            topic=safe_query, excerpts=excerpts_text,
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": _DISTIL_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="idle_knowledge_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("knowledge distil call raised", exc_info=True)
            return None
        if self._cancel_event.is_set():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            return None
        return self._parse_facts(raw)

    @staticmethod
    def _parse_facts(raw: str) -> list[KnowledgeFact] | None:
        match = _JSON_OBJECT_RE.search(raw.strip())
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        raw_facts = parsed.get("facts")
        if not isinstance(raw_facts, list):
            return []
        out: list[KnowledgeFact] = []
        seen: set[str] = set()
        for item in raw_facts:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            if len(text) > 240:
                text = text[:237].rsplit(" ", 1)[0] + "…"
            dedupe_key = text.lower()
            if dedupe_key in seen:
                continue
            try:
                conf = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            conf = max(0.0, min(1.0, conf))
            if conf < _MIN_FACT_CONFIDENCE:
                continue
            seen.add(dedupe_key)
            out.append(KnowledgeFact(text=text, confidence=conf))
            if len(out) >= _MAX_FACTS:
                break
        return out

    # ── memory write ─────────────────────────────────────────────────

    def _write_knowledge(
        self,
        *,
        fact: KnowledgeFact,
        topic: str,
        source_url: str,
        source_urls: list[str],
        cluster_key: str,
        now: datetime,
    ) -> int | None:
        try:
            embedding = self._embedder.embed(fact.text)
        except Exception:
            log.warning("knowledge embed failed", exc_info=True)
            return None
        confidence = min(_MAX_FACT_CONFIDENCE, float(fact.confidence))
        try:
            new_mem = self._memory_store.add(
                content=fact.text,
                kind="knowledge",
                embedding=embedding,
                salience=_KNOWLEDGE_SALIENCE,
                confidence=confidence,
                tier="long_term",
                metadata={
                    "topic": topic[:200],
                    "source_query": topic[:200],
                    "source_url": source_url,
                    "source_urls": source_urls[:5],
                    "cluster_key": cluster_key,
                    "learned_at": now.isoformat(),
                },
            )
        except Exception:
            log.warning("knowledge write failed", exc_info=True)
            return None
        if new_mem is None:
            log.info(
                "knowledge deduped against existing memory (cluster=%s)",
                cluster_key,
            )
            return None
        if self._notify_memory_added is not None:
            try:
                self._notify_memory_added(new_mem.to_dict())
            except Exception:
                log.debug("knowledge notify added failed", exc_info=True)
        return int(new_mem.id)


__all__ = ["IdleKnowledgeWorker", "KnowledgeFact"]
