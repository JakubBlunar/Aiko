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
        pick = self._pick_cluster(now=now)
        if pick is None:
            return {"skipped": True, "reason": "no_cluster"}

        if not self._rate_limiter.allow(now):
            log.info(
                "knowledge skip: rate limited (cluster=%s topic=%r)",
                pick.cluster_key, _preview(pick.topic),
            )
            return {"skipped": True, "reason": "rate_limited"}

        # Whatever happens below (write, dedupe, no results, privacy),
        # the cluster goes on cooldown so the next tick moves to a
        # different interest rather than grinding this one.
        self._stamp_cooldown(pick.cluster_key, now=now)

        log.info(
            "knowledge start: cluster=%s size=%d topic=%r",
            pick.cluster_key, pick.size, _preview(pick.topic),
        )

        safe_query = self._scrub(pick.topic)
        if safe_query is None:
            log.info(
                "knowledge skip: privacy gate dropped topic cluster=%s",
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

    # ── cluster selection ────────────────────────────────────────────

    def _pick_cluster(self, *, now: datetime) -> _ClusterPick | None:
        """Densest under-researched interest cluster not in cooldown."""
        try:
            topic_graph = self._topic_graph_provider()
        except Exception:
            log.debug("knowledge: topic_graph_provider raised", exc_info=True)
            return None
        if topic_graph is None:
            return None
        try:
            from app.core.conversation.topic_graph import (
                build_topic_graph_snapshot,
            )

            snapshot = build_topic_graph_snapshot(
                topic_graph, self._memory_store,
            )
        except Exception:
            log.debug("knowledge: snapshot build raised", exc_info=True)
            return None
        if not snapshot.get("enabled") or not snapshot.get("clusters"):
            return None

        max_per_cluster = max(
            0,
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
        # Snapshot clusters are already sorted by size desc.
        for cluster in snapshot["clusters"]:
            topic = (cluster.get("summary") or "").strip()
            if len(topic) < _MIN_TOPIC_CHARS:
                continue
            kind_counts = cluster.get("kind_counts") or {}
            if int(kind_counts.get("knowledge", 0)) >= max_per_cluster:
                continue
            key = _cluster_key(topic)
            last = _parse_iso(cooldowns.get(key))
            if last is not None and now - last < cooldown_window:
                continue
            return _ClusterPick(
                cluster_key=key,
                topic=topic,
                cluster_id=int(cluster.get("cluster_id", 0)),
                size=int(cluster.get("size", 0)),
            )
        return None

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

    def _stamp_cooldown(self, cluster_key: str, *, now: datetime) -> None:
        cooldowns = self._load_cooldowns()
        cooldowns[cluster_key] = now.isoformat()
        # Prune obviously-stale entries so the blob can't grow without
        # bound (anything older than 30 days is well past any cooldown).
        cutoff = now - timedelta(days=30)
        pruned = {
            k: v
            for k, v in cooldowns.items()
            if (_parse_iso(v) or now) >= cutoff
        }
        try:
            self._kv_set(_KV_CLUSTER_COOLDOWNS, json.dumps(pruned))
        except Exception:
            log.debug("knowledge cooldown persist failed", exc_info=True)

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
