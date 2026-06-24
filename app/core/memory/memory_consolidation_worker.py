"""Memory consolidation worker (K35 personality backlog).

Auto-extracted scratchpad memories accumulate near-duplicates over
weeks: the insert-time dedupe in :meth:`MemoryStore.add` only fires at
cosine ``>= 0.92`` against the mirror *at that instant*, so two
phrasings of the same fact written days apart, or anything that lands
just below the bar, both survive and quietly inflate RAG noise.

K35 is the periodic cleanup pass. One tick = one :meth:`run`:

1. **Corpus** — scratchpad-tier rows inside a recency window
   (``consolidation_lookback_days``), dropped if pinned / blank / missing
   an embedding, capped at ``consolidation_max_corpus`` (newest first).
2. **Cluster** — all-pairs NumPy cosine (same vectorised trick as F5
   :class:`app.core.memory.memory_conflict_worker.MemoryConflictWorker`).
   For each unprocessed anchor, gather rows at/above
   ``consolidation_similarity_threshold`` that share the anchor's
   ``kind`` AND are NOT flagged as contradicting by
   :func:`app.core.memory.conflict_heuristics.classify_pair` (those are
   F5's job). That star-cluster (size ``>= consolidation_min_cluster_size``)
   is one merge unit; members are marked processed so clusters don't
   chain.
3. **Merge** — pick the primary (highest ``confidence`` -> ``salience``
   -> newest), fuse the cluster's contents into ONE clean sentence via a
   rate-limited worker-LLM call, with the primary's own content as the
   deterministic fallback on any failure.
4. **Commit** — update the primary in place (merged content, re-embedded
   only if the text actually changed, ``salience``/``confidence`` lifted
   to the cluster max, ``tier='long_term'``, ``metadata.source_ids``
   provenance), then **archive** the absorbed duplicates
   (``tier='archive'``, ``metadata.consolidated_into=primary_id``).
   Archiving is reversible and mirrors how F5 demotes a conflict loser.

Distinct from F5: F5 finds *contradicting* pairs in the ``[0.80, 0.92)``
band and demotes the loser; K35 finds tight *near-duplicates* and fuses
them. The contradiction heuristic is reused only as a guard so the two
workers never fight over the same pair.

Every failure path is swallowed and logged — the worst outcome is a
skipped merge, never a corrupt row.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import numpy as np

from app.core.memory.cluster_scope import partition_by_cluster
from app.core.memory.conflict_heuristics import HEURISTIC_NO, classify_pair
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.llm.chat_client import ChatClient
    from app.llm.embedder import Embedder


log = logging.getLogger("app.memory_consolidation_worker")


_LOG_PREVIEW_CHARS = 160
_MERGE_MAX_TOKENS = 120

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)

_SYSTEM_PROMPT = (
    "You merge several near-duplicate notes about the same thing into "
    "ONE clean sentence that keeps every distinct detail and drops the "
    "repetition. Answer with ONE JSON object on a single line and "
    'nothing else. Schema: {"merged": "<one merged sentence>"}. '
    "Keep it factual and concise; do not invent anything not present in "
    "the notes."
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _preview(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= _LOG_PREVIEW_CHARS:
        return s
    return s[: _LOG_PREVIEW_CHARS - 1] + "\u2026"


@dataclass(slots=True)
class _Cluster:
    primary: "Memory"
    others: list["Memory"]

    @property
    def members(self) -> list["Memory"]:
        return [self.primary, *self.others]


class MemoryConsolidationWorker:
    """IdleWorker that fuses near-duplicate scratchpad memories."""

    name = "memory_consolidation"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        embedder: "Embedder",
        ollama: "ChatClient | None",
        chat_model: str | None,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        notify_memory_updated: Any | None = None,
        topic_graph_provider: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._notify_memory_updated = notify_memory_updated
        # F10j: late-bound accessor for the K9 topic graph so the
        # near-duplicate sweep can be scoped to within-cluster groups.
        self._topic_graph_provider = topic_graph_provider
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "consolidation_interval_seconds",
                21600,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not self._enabled():
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        now = self._clock()
        threshold = float(
            getattr(
                self._memory_settings,
                "consolidation_similarity_threshold",
                0.90,
            )
        )
        min_cluster = max(
            2,
            int(
                getattr(
                    self._memory_settings,
                    "consolidation_min_cluster_size",
                    2,
                )
            ),
        )
        max_corpus = int(
            getattr(self._memory_settings, "consolidation_max_corpus", 1000)
        )
        max_clusters = int(
            getattr(
                self._memory_settings,
                "consolidation_max_clusters_per_run",
                20,
            )
        )

        candidates = self._snapshot_candidates(now=now, max_corpus=max_corpus)
        log.info(
            "memory-consolidation start: corpus_size=%d threshold=%.2f "
            "min_cluster=%d max_clusters=%d",
            len(candidates),
            threshold,
            min_cluster,
            max_clusters,
        )
        if len(candidates) < min_cluster:
            return {
                "skipped": True,
                "reason": "corpus_too_small",
                "corpus_size": len(candidates),
            }

        # F10j: scope the near-duplicate sweep to within topic-cluster
        # groups. When the switch is off / the graph is absent or unwarmed,
        # this is a single group == the full candidate list (legacy).
        cluster_scoped = bool(
            getattr(
                self._agent_settings,
                "cluster_scoped_memory_hygiene_enabled",
                True,
            )
        )
        graph = None
        if cluster_scoped and self._topic_graph_provider is not None:
            try:
                graph = self._topic_graph_provider()
            except Exception:
                log.debug(
                    "memory-consolidation: topic_graph_provider raised",
                    exc_info=True,
                )
                graph = None
        groups = partition_by_cluster(
            candidates, graph, enabled=cluster_scoped, min_group=min_cluster,
        )

        clusters: list[_Cluster] = []
        for group in groups:
            if len(clusters) >= max_clusters:
                break
            if len(group) < min_cluster:
                continue
            emb_matrix = np.asarray(
                [m.embedding for m in group], dtype=np.float32,
            )
            if emb_matrix.ndim != 2 or emb_matrix.shape[0] != len(group):
                log.warning(
                    "memory-consolidation: bad embedding matrix shape=%s; "
                    "skipping group",
                    emb_matrix.shape,
                )
                continue
            clusters.extend(
                self._build_clusters(
                    group,
                    emb_matrix,
                    threshold,
                    min_cluster,
                    max_clusters - len(clusters),
                )
            )
        log.info(
            "memory-consolidation: %d cluster(s) found across %d group(s) "
            "(cluster_scoped=%s)",
            len(clusters),
            len(groups),
            bool(cluster_scoped and graph is not None),
        )

        merged = 0
        absorbed_total = 0
        llm_used = 0
        for cluster in clusters:
            if self._cancel_event.is_set():
                log.info("memory-consolidation cancelled mid-merge")
                break
            ok, used_llm = self._merge_cluster(cluster, now)
            if ok:
                merged += 1
                absorbed_total += len(cluster.others)
                if used_llm:
                    llm_used += 1

        result = {
            "corpus_size": len(candidates),
            "groups": len(groups),
            "cluster_scoped": bool(cluster_scoped and graph is not None),
            "clusters": len(clusters),
            "merged": merged,
            "absorbed": absorbed_total,
            "llm_used": llm_used,
        }
        log.info("memory-consolidation done: %s", result)
        return result

    # ── corpus + clustering ──────────────────────────────────────────

    def _snapshot_candidates(
        self, *, now: datetime, max_corpus: int,
    ) -> list["Memory"]:
        """Scratchpad rows in the recency window, newest first."""
        lookback_days = int(
            getattr(self._memory_settings, "consolidation_lookback_days", 30)
        )
        cutoff = now - timedelta(days=max(0, lookback_days))
        try:
            rows = self._memory_store.iter_by_tier("scratchpad")
        except Exception:
            log.debug("memory-consolidation: iter_by_tier raised", exc_info=True)
            return []
        usable: list["Memory"] = []
        for mem in rows:
            if getattr(mem, "pinned", False):
                continue
            if getattr(mem, "embedding", None) is None:
                continue
            if not (mem.content or "").strip():
                continue
            created = _parse_iso(mem.created_at)
            if created is not None and created < cutoff:
                continue
            usable.append(mem)
        usable.sort(key=lambda m: m.created_at or "", reverse=True)
        return usable[: max(1, int(max_corpus))]

    def _build_clusters(
        self,
        candidates: list["Memory"],
        emb_matrix: np.ndarray,
        threshold: float,
        min_cluster: int,
        max_clusters: int,
    ) -> list[_Cluster]:
        processed: set[int] = set()
        clusters: list[_Cluster] = []
        for i, anchor in enumerate(candidates):
            if i in processed:
                continue
            if len(clusters) >= max_clusters:
                break
            sims = emb_matrix[i + 1:] @ emb_matrix[i]
            band_idx = np.nonzero(sims >= threshold)[0]
            member_idx: list[int] = []
            for offset_j in band_idx.tolist():
                j = i + 1 + int(offset_j)
                if j in processed:
                    continue
                cand = candidates[j]
                if cand.kind != anchor.kind:
                    continue
                # Never fuse a contradiction — that's F5's job.
                if classify_pair(anchor.content, cand.content).label != HEURISTIC_NO:
                    continue
                member_idx.append(j)
            if len(member_idx) + 1 < min_cluster:
                continue
            processed.add(i)
            for j in member_idx:
                processed.add(j)
            group = [anchor, *(candidates[j] for j in member_idx)]
            primary = self._pick_primary(group)
            others = [m for m in group if m.id != primary.id]
            clusters.append(_Cluster(primary=primary, others=others))
        return clusters

    @staticmethod
    def _pick_primary(group: list["Memory"]) -> "Memory":
        def key(m: "Memory") -> tuple[float, float, str]:
            return (
                float(getattr(m, "confidence", 0.7)),
                float(getattr(m, "salience", 0.5)),
                m.created_at or "",
            )

        return max(group, key=key)

    # ── merge ────────────────────────────────────────────────────────

    def _merge_cluster(
        self, cluster: _Cluster, now: datetime,
    ) -> tuple[bool, bool]:
        """Fuse one cluster into its primary. Returns (committed, used_llm)."""
        primary = cluster.primary
        group = cluster.members
        when_iso = now.isoformat() if isinstance(now, datetime) else _now_iso()

        merged_text, used_llm = self._compose_merge(group, primary, now)
        merged_text = (merged_text or "").strip() or primary.content
        text_changed = merged_text != primary.content

        source_ids = sorted({int(m.id) for m in group})
        salience_max = max(float(getattr(m, "salience", 0.5)) for m in group)
        confidence_max = max(float(getattr(m, "confidence", 0.7)) for m in group)

        new_embedding = None
        if text_changed:
            try:
                new_embedding = self._embedder.embed(merged_text)
            except Exception:
                log.debug(
                    "memory-consolidation: re-embed failed, keeping primary "
                    "vector + content",
                    exc_info=True,
                )
                # Without a fresh vector the merged text would have a
                # stale embedding; fall back to the primary's own text.
                merged_text = primary.content
                text_changed = False

        try:
            self._memory_store.update(
                primary.id,
                content=merged_text if text_changed else None,
                embedding=new_embedding,
                salience=salience_max,
                confidence=confidence_max,
                tier="long_term",
                metadata={
                    "source_ids": source_ids,
                    "consolidated_at": when_iso,
                },
                metadata_merge=True,
            )
        except Exception:
            log.warning(
                "memory-consolidation: primary update failed id=%s",
                primary.id,
                exc_info=True,
            )
            return (False, used_llm)

        self._notify(primary.id)

        absorbed: list[int] = []
        for mem in cluster.others:
            try:
                self._memory_store.update(
                    mem.id,
                    tier="archive",
                    metadata={
                        "consolidated_into": int(primary.id),
                        "consolidated_at": when_iso,
                    },
                    metadata_merge=True,
                )
            except Exception:
                log.debug(
                    "memory-consolidation: archive failed id=%s",
                    mem.id,
                    exc_info=True,
                )
                continue
            absorbed.append(int(mem.id))
            self._notify(mem.id)

        log.info(
            "memory-consolidation merged: primary=%s absorbed=%s llm=%s "
            "text_changed=%s content=%r",
            primary.id,
            absorbed,
            used_llm,
            text_changed,
            _preview(merged_text),
        )
        return (True, used_llm)

    def _compose_merge(
        self, group: list["Memory"], primary: "Memory", now: datetime,
    ) -> tuple[str, bool]:
        """Return (merged_text, used_llm). Falls back to primary content."""
        fallback = primary.content
        if self._ollama is None or not self._chat_model:
            return (fallback, False)
        if not self._rate_limiter.allow(now):
            log.info(
                "memory-consolidation: LLM merge skipped (rate-limited) "
                "primary=%s",
                primary.id,
            )
            return (fallback, False)
        notes = "\n".join(f"- {(m.content or '').strip()}" for m in group)
        user_content = f"Notes to merge:\n{notes}"
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
                model=self._chat_model,
                options={"temperature": 0.2, "num_predict": _MERGE_MAX_TOKENS},
                format_json=True,
                surface="memory_consolidation",
            )
        except Exception:
            log.debug("memory-consolidation: LLM merge raised", exc_info=True)
            return (fallback, False)
        merged = self._parse_merged(content)
        if not merged:
            return (fallback, False)
        return (merged, True)

    @staticmethod
    def _parse_merged(raw: str | None) -> str:
        match = _JSON_OBJECT_RE.search(raw or "")
        if match is None:
            return ""
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        return str(parsed.get("merged", "")).strip()

    # ── helpers ──────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        return bool(
            getattr(self._agent_settings, "memory_consolidation_enabled", True)
        )

    def _notify(self, memory_id: int) -> None:
        if self._notify_memory_updated is None:
            return
        try:
            self._notify_memory_updated({"memory_id": int(memory_id)})
        except Exception:
            log.debug(
                "memory-consolidation: notify_memory_updated raised",
                exc_info=True,
            )


__all__ = ["MemoryConsolidationWorker"]
