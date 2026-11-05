"""Memory consolidator: cluster + merge near-cosine memories (Phase 4b).

The :class:`app.core.memory.memory_store.MemoryStore` already drops *exact* near-
duplicates (>= 0.92 cosine) on insert. Over weeks, however, drift gives us
clusters of *related* memories ("Jacob plays guitar", "Jacob has been
practicing scales", "Jacob finally nailed that arpeggio") that aren't
duplicates but absolutely belong together as a single richer fact. This
worker periodically scans the store, groups near-cosine clusters above a
*looser* threshold, and either:

  * picks the top-salience exemplar and bumps it (no LLM), OR
  * (when ``use_llm_merge=True``) asks the chat model for ONE consolidated
    sentence summarising the whole cluster.

In either case the source notes are archived with their original wording
and the surviving memory inherits a salience boost. Results are
counted in :meth:`stats` and exposed via MCP for quick "did the
consolidator do anything useful last night?" inspection.

Schema (already present in chat_database):

    CREATE TABLE consolidator_state (
        user_id TEXT PRIMARY KEY,
        last_cluster_index INTEGER NOT NULL DEFAULT 0,
        last_run_at TEXT
    );

The ``last_cluster_index`` is currently unused (the new logic always picks
fresh chunks by recency); it stays in the schema as a hook for a future
sliding-window strategy.

Threading model: this worker runs on the SpeakingWindowScheduler thread;
:meth:`maybe_run` is the entry point and accepts an optional ``stop_flag``
so the scheduler can cancel mid-cluster.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Iterable

import numpy as np

from app.core.session.session_text_utils import resolve_user_name
from app.core.infra import timephrase
from app.core.memory.memory_integrity import compatible_memories
from app.core.memory.memory_origin import origin_annotation
from app.core.memory.conflict_heuristics import classify_pair, HEURISTIC_NO

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.core.voice.speaking_window_scheduler import StopFlag
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.memory_consolidator")


@dataclass(slots=True)
class ConsolidationResult:
    chunks_scanned: int = 0
    clusters_found: int = 0
    merges_applied: int = 0
    deletions: int = 0
    archived: int = 0
    llm_calls: int = 0
    failed_llm: int = 0
    elapsed_seconds: float = 0.0


def _build_merge_prompt(user_display_name: str = "the user") -> str:
    """Merge-prompt factory templated on the user's display name."""
    name = user_display_name or "the user"
    return (
        "You are Aiko's memory librarian. You'll receive 2-6 short notes "
        f"that say overlapping things ({name} is the user). Write ONE consolidated "
        "note (<= 30 words) that preserves the strongest specifics and "
        "drops the redundant phrasing. Preserve each note's actual subject and "
        "source role; Aiko's words or public knowledge are not user experiences.\n"
        "\n"
        "Rules:\n"
        "- Output only the consolidated note. No prose, no bullets, no quotes.\n"
        "- Stay concrete. Don't invent facts not present in the inputs.\n"
        "- If the inputs disagree, return an empty answer; do not resolve a conflict here.\n"
        "- Each input ends with how long ago it was recorded. Use that to "
        "pick the fresher claim, and never carry a stale one forward as "
        "though it were current.\n"
        "- " + timephrase.STORED_TEXT_TIME_RULE
    )


# Back-compat constant. New code should call ``_build_merge_prompt(name)``.
_MERGE_PROMPT = _build_merge_prompt()


class MemoryConsolidator:
    """Background pass that merges near-cosine memory clusters."""

    def __init__(
        self,
        *,
        ollama: "OllamaClient | None",
        memory_store: "MemoryStore",
        chat_db: "ChatDatabase",
        model: str,
        chunk_size: int = 40,
        similarity_threshold: float = 0.84,
        min_cluster_size: int = 2,
        min_hours_between: float = 18.0,
        use_llm_merge: bool = True,
        max_llm_tokens: int = 80,
        user_display_name_provider: "Callable[[], str] | None" = None,
        repoint_memory_edges: "Callable[[int, int], int] | None" = None,
        embedder=None,
    ) -> None:
        self._ollama = ollama
        self._mem = memory_store
        self._db = chat_db
        self._model = model
        self._embedder = embedder
        self._user_display_name_provider = user_display_name_provider
        # Kept for constructor compatibility. Archived originals retain
        # their concept edges; a rewrite is not equivalent source evidence.
        self._repoint_memory_edges = repoint_memory_edges
        self._chunk_size = max(8, int(chunk_size))
        self._sim = max(0.5, min(0.99, float(similarity_threshold)))
        self._min_cluster = max(2, int(min_cluster_size))
        self._min_hours = max(0.0, float(min_hours_between))
        self._use_llm = bool(use_llm_merge) and (ollama is not None)
        self._max_tokens = max(40, int(max_llm_tokens))
        self._stats = {
            "scheduled": 0,
            "skipped_recent": 0,
            "skipped_no_memories": 0,
            "completed": 0,
            "failed": 0,
            "merges_applied": 0,
            "deletions": 0,
            "archived": 0,
            "llm_calls": 0,
            "failed_llm": 0,
        }

    def _resolve_user_name(self) -> str:
        return resolve_user_name(self._user_display_name_provider)

    # ── public ──────────────────────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(self, *, model: str | None = None) -> None:
        if model is not None:
            self._model = model

    def should_run(self, user_id: str, *, now_utc: datetime | None = None) -> bool:
        """True if no run has happened in the last ``min_hours_between``."""
        last = self._read_last_run(user_id)
        if last is None:
            return True
        now = now_utc or timephrase.utcnow()
        try:
            then = datetime.fromisoformat(last)
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
        except Exception:
            return True
        elapsed_hours = (now - then).total_seconds() / 3600.0
        return elapsed_hours >= self._min_hours

    def maybe_run(
        self,
        user_id: str,
        *,
        stop_flag: "StopFlag | None" = None,
        now_utc: datetime | None = None,
    ) -> ConsolidationResult | None:
        if not self.should_run(user_id, now_utc=now_utc):
            self._stats["skipped_recent"] += 1
            return None
        return self._run_once(user_id, stop_flag=stop_flag, now_utc=now_utc)

    def force_run(
        self,
        user_id: str,
        *,
        stop_flag: "StopFlag | None" = None,
        now_utc: datetime | None = None,
    ) -> ConsolidationResult | None:
        """Run regardless of throttling — used by manual MCP triggers."""
        return self._run_once(user_id, stop_flag=stop_flag, now_utc=now_utc)

    # ── core ───────────────────────────────────────────────────────────

    def _run_once(
        self,
        user_id: str,
        *,
        stop_flag: "StopFlag | None",
        now_utc: datetime | None,
    ) -> ConsolidationResult | None:
        import time

        self._stats["scheduled"] += 1
        t0 = time.monotonic()
        memories = self._collect_recent()
        if not memories:
            self._stats["skipped_no_memories"] += 1
            return None
        result = ConsolidationResult()
        result.chunks_scanned = 1
        # Schema v8: cluster within tier only. Merging a scratchpad
        # rumor into a verified long_term anchor would silently
        # overwrite the anchor's salience + content with speculative
        # text; merging a stale archive row into long_term would
        # resurrect cold history without earning it. Group first, then
        # cluster each bucket.
        by_tier: dict[str, list["Memory"]] = {}
        for mem in memories:
            by_tier.setdefault(getattr(mem, "tier", "long_term"), []).append(mem)
        clusters: list[list["Memory"]] = []
        for tier_rows in by_tier.values():
            clusters.extend(
                _cluster_memories(
                    tier_rows,
                    similarity=self._sim,
                    min_size=self._min_cluster,
                )
            )
        result.clusters_found = len(clusters)
        for cluster in clusters:
            if stop_flag is not None and stop_flag.is_set():
                log.debug("consolidator: stop_flag set mid-run")
                break
            try:
                merged_content = self._merge_cluster(cluster)
            except Exception:
                log.debug("merge_cluster raised", exc_info=True)
                continue
            if not merged_content:
                continue
            survivor, victims = _split_survivor(cluster)
            try:
                archived = self._apply_merge(survivor, victims, merged_content)
            except Exception:
                log.debug("apply_merge raised", exc_info=True)
                continue
            if archived is None:
                continue
            result.merges_applied += 1
            result.archived += archived
        self._record_run(user_id, now_utc=now_utc)
        result.elapsed_seconds = time.monotonic() - t0
        self._stats["completed"] += 1
        self._stats["merges_applied"] += result.merges_applied
        self._stats["deletions"] += result.deletions
        self._stats["archived"] += result.archived
        log.info(
            "consolidator: clusters=%d merges=%d deletions=%d elapsed=%.1fs",
            result.clusters_found,
            result.merges_applied,
            result.deletions,
            result.elapsed_seconds,
        )
        return result

    def _collect_recent(self) -> list["Memory"]:
        try:
            recent = self._mem.list_recent(limit=self._chunk_size)
        except Exception:
            log.debug("list_recent failed", exc_info=True)
            return []
        # Drop pure self/reflection memories from clustering — those are
        # narrative artifacts that we want to keep distinct.
        return [m for m in recent if not m.pinned and m.tier != "archive"
                and not (m.metadata or {}).get("consolidated_into")
                and (m.kind or "fact") not in {"self", "reflection"}]

    # ── merging ────────────────────────────────────────────────────────

    def _merge_cluster(self, cluster: list["Memory"]) -> str | None:
        """Synthesise a single consolidated note for the cluster."""
        if not cluster:
            return None
        if not self._use_llm or self._ollama is None:
            # Fallback: take the most-salient member's content as-is.
            survivor, _ = _split_survivor(cluster)
            return survivor.content
        # K-time10: age-tagged, matching what the K-time9 sibling
        # (``memory_consolidation_worker``) already does. "Take the most
        # recent one" was in the prompt long before the model was given
        # any way to tell which that was.
        bullets = [
            timephrase.format_memory_line(m) + " " + origin_annotation(m)
            for m in cluster
            if (m.content or "").strip()
        ]
        if len(bullets) < 2:
            survivor, _ = _split_survivor(cluster)
            return survivor.content
        try:
            messages = [
                {
                    "role": "system",
                    "content": (
                        timephrase.today_anchor()
                        + "\n\n"
                        + _build_merge_prompt(self._resolve_user_name())
                    ),
                },
                {"role": "user", "content": "\n".join(bullets)},
            ]
            self._stats["llm_calls"] += 1
            raw = self._ollama.chat(
                messages,
                options={
                    "temperature": 0.2,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
                # Reasoning model: num_predict stays the answer budget; the
                # client adds think headroom for the trace (was truncating
                # to an empty answer with think off).
                think=True,
                surface="memory_consolidator",
            )
        except Exception:
            log.debug("merge LLM call failed", exc_info=True)
            self._stats["failed_llm"] += 1
            survivor, _ = _split_survivor(cluster)
            return survivor.content
        cleaned = _clean_merge_output(raw)
        if not cleaned:
            survivor, _ = _split_survivor(cluster)
            return survivor.content
        return cleaned

    def _apply_merge(
        self,
        survivor: "Memory",
        victims: list["Memory"],
        merged_content: str,
    ) -> int | None:
        # Boost salience: average of the cluster + small bonus, clipped.
        if survivor is None:
            return None
        boost = 0.05 + 0.03 * len(victims)
        peak = max(m.salience for m in [survivor, *victims])
        new_salience = min(1.0, max(survivor.salience, peak) + boost)
        # Schema v8: keep the cluster's best revival_score so a
        # frequently-cited victim's earned trust transfers to the
        # consolidated row. Tier is constant within a cluster (we
        # group by tier upstream) but we pass it explicitly so the
        # MemoryStore.update path runs the tier normalization /
        # pinned coercion logic for free.
        new_revival = max(float(m.revival_score) for m in [survivor, *victims])
        cleaned_text = (merged_content or survivor.content).strip()[:4000]
        if timephrase.has_relative_deictic(cleaned_text) and cleaned_text != survivor.content:
            cleaned_text = survivor.content
        embedding = None
        if cleaned_text != survivor.content:
            try:
                if self._embedder is None:
                    cleaned_text = survivor.content
                else:
                    embedding = self._embedder.embed(cleaned_text)
            except Exception:
                log.debug("merge re-embed failed; keeping original text", exc_info=True)
                cleaned_text = survivor.content
        from app.core.concepts.concept_support import memory_revision

        group = [survivor, *victims]
        source_revisions = {str(m.id): memory_revision(m) for m in group}
        try:
            self._mem.update(
                int(survivor.id),
                content=cleaned_text,
                embedding=embedding,
                confidence=(min(m.confidence for m in group)
                            if cleaned_text != survivor.content else survivor.confidence),
                salience=new_salience,
                revival_score=new_revival,
                tier=getattr(survivor, "tier", "long_term"),
                metadata={"source_ids": [m.id for m in group],
                          "source_revisions": source_revisions},
                metadata_merge=True,
            )
        except Exception:
            log.debug("survivor update failed", exc_info=True)
            return None
        archived = 0
        for victim in victims:
            try:
                self._mem.update(int(victim.id), tier="archive", metadata={
                    "consolidated_into": int(survivor.id),
                    "consolidated_at": timephrase.utcnow().isoformat(),
                }, metadata_merge=True)
                archived += 1
            except Exception:
                log.debug("source archive failed", exc_info=True)
        return archived

    # ── state IO ───────────────────────────────────────────────────────

    def _read_last_run(self, user_id: str) -> str | None:
        if not user_id:
            return None
        row = self._db.execute_fetchone(
            "SELECT last_run_at FROM consolidator_state WHERE user_id = ?",
            (user_id,),
        )
        if not row:
            return None
        val = row[0]
        return str(val) if val else None

    def _record_run(self, user_id: str, *, now_utc: datetime | None) -> None:
        if not user_id:
            return
        now = (now_utc or timephrase.utcnow()).isoformat()
        self._db.execute_commit(
            "INSERT INTO consolidator_state (user_id, last_cluster_index, last_run_at) "
            "VALUES (?, 0, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET last_run_at = excluded.last_run_at",
            (user_id, now),
        )


# ── helpers (module-level for testability) ────────────────────────────


def _split_survivor(cluster: list["Memory"]) -> tuple["Memory", list["Memory"]]:
    """Pick the highest-salience / earliest-created member as survivor."""
    if not cluster:
        raise ValueError("empty cluster")
    ranked = sorted(
        cluster,
        key=lambda m: (m.salience, m.use_count, -_safe_ts(m.created_at)),
        reverse=True,
    )
    survivor = ranked[0]
    victims = [m for m in cluster if m.id != survivor.id]
    return survivor, victims


def _safe_ts(iso: str) -> float:
    try:
        if not iso:
            return 0.0
        # Negative-int-friendly: more recent => larger int.
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _cluster_memories(
    memories: Iterable["Memory"],
    *,
    similarity: float,
    min_size: int,
) -> list[list["Memory"]]:
    """Single-link clustering on cosine similarity."""
    items = list(memories)
    if len(items) < min_size:
        return []
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            left = [i for i in range(n) if find(i) == ra]
            right = [i for i in range(n) if find(i) == rb]
            if any(classify_pair(items[i].content, items[j].content).label != HEURISTIC_NO
                   for i in left for j in right):
                return  # A neutral bridge must not fuse contradictory endpoints.
            parent[ra] = rb

    embeddings = [np.asarray(m.embedding, dtype=np.float32) for m in items]
    norms = [float(np.linalg.norm(e)) for e in embeddings]
    for i in range(n):
        if norms[i] <= 0.0:
            continue
        ei = embeddings[i] / norms[i]
        for j in range(i + 1, n):
            if norms[j] <= 0.0:
                continue
            ej = embeddings[j] / norms[j]
            sim = float(np.dot(ei, ej))
            if sim >= similarity and compatible_memories(items[i], items[j], consolidation=True):
                union(i, j)

    groups: dict[int, list["Memory"]] = {}
    for idx, mem in enumerate(items):
        root = find(idx)
        groups.setdefault(root, []).append(mem)
    return [
        sorted(g, key=lambda m: (m.salience, m.use_count), reverse=True)
        for g in groups.values()
        if len(g) >= min_size
    ]


_CODE_FENCE_RE = re.compile(r"```(?:json|text)?\s*(.*?)\s*```", re.DOTALL)


def _clean_merge_output(raw: str) -> str:
    """Strip code fences, surrounding quotes, and over-long output."""
    text = (raw or "").strip()
    if not text:
        return ""
    fenced = _CODE_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    # If the model accidentally returned a list, take the first line.
    if text.startswith(("- ", "* ")):
        first_line = text.splitlines()[0].lstrip("-* ").strip()
        if first_line:
            text = first_line
    if len(text) > 600:
        text = text[:600].rsplit(" ", 1)[0].rstrip(",;: ") + "…"
    text = text.strip().strip("\"'`")
    return "" if text.lower() in {"null", "none", "n/a"} else text


__all__ = [
    "ConsolidationResult",
    "MemoryConsolidator",
    "_cluster_memories",
    "_clean_merge_output",
    "_split_survivor",
]
