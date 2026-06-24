"""Per-cluster rolling digest worker (F10g personality backlog).

The K9 topic graph carves memory into clusters and F10a names them, but a
cluster's *content* still only reaches the prompt as a pile of individual
member rows. F10c (topic expansion) appends siblings line-by-line, which
rounds out a topic but also means a 40-member cluster can dump 40 lines
into the prompt. F10d gave on-demand member enumeration but never a stored
*summary*.

This worker is the true realisation of the original "cluster-summary"
idea: during quiet windows it writes one high-salience
``kind="topic_digest"`` memory per dense cluster — a worker-LLM
one-paragraph compression of its members ("what I know about X") — and
refreshes it only when the cluster has drifted materially in size (the
same cache-by-representative trick :class:`ClusterLabelWorker` uses).

Design notes:

* **Off the chat path.** Runs on the :class:`IdleWorkerScheduler`
  (maintenance tier), so it never costs a per-turn token and never
  touches the chat prompt cache.
* **Lives in the normal pool.** The digest is a real :class:`Memory`
  (``kind="topic_digest"``), so it decays, can be pinned, and shows in
  the Memory tab. It is, however, **excluded from topic-graph
  clustering** (see ``topic_graph._NON_CLUSTERING_KINDS``) so a digest
  never feeds back into the cluster it summarises.
* **Cached by representative.** Each digest's ``(memory_id, size)`` is
  cached in ``kv_meta`` keyed by the cluster's *representative* memory id
  (``aiko.topic_digest.<rep>``). Cluster ids are reassigned on every
  batch refit, so keying by the (stable) representative lets a digest
  survive a rebuild: the next tick re-uses the existing memory for free
  (no LLM) and only regenerates when the representative is new or the
  cluster drifted materially in size.
* **Cluster→digest map.** After each run the worker rebuilds
  :attr:`cluster_digest_map` (``{cluster_id: memory_id}``) from the live
  clusters and persists it to ``kv_meta`` (``aiko.topic_digest_map``).
  The RAG retriever reads it (via an injected provider) to surface the
  digest as the coarse answer and cap raw sibling expansion. Stale
  entries (between a rebuild and the next tick) degrade gracefully — the
  retriever verifies the looked-up row is still a ``topic_digest``.
* **Bounded spend.** At most ``topic_digest_max_per_run`` clusters get a
  fresh LLM digest per tick (largest-first); the rest wait. The free
  cache-reuse pass is unbounded (dict work).

Only meaningful in the persisted/incremental topic-graph mode
(:attr:`TopicGraph.persistent`); :meth:`is_ready` short-circuits
otherwise.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.infra.settings import AgentSettings
    from app.core.memory.memory_store import MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.topic_digest_worker")


_KV_PREFIX = "aiko.topic_digest."
_KV_MAP_KEY = "aiko.topic_digest_map"

_SYSTEM_PROMPT = (
    "You write a short digest of what an AI companion knows about ONE "
    "topic, given a set of memory snippets that were grouped together "
    "because they are about the same thing. Write 2-4 plain sentences "
    "that compress what these memories collectively say -- the gist, the "
    "specifics that matter, and any throughline. Refer to the user by "
    "name if a name appears. Be concrete; do NOT add facts that are not "
    "in the snippets, do NOT use the words 'memories', 'cluster', "
    "'topic', or 'snippets', and do NOT add a preamble. Reply with ONE "
    'JSON object on a single line and nothing else: {"digest": "<2-4 '
    'sentences>"}.'
)

_USER_TEMPLATE = "MEMORY SNIPPETS:\n{snippets}\n\nReturn the digest JSON now."

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)

_MAX_SNIPPETS = 16
_MAX_SNIPPET_CHARS = 200
_MAX_DIGEST_CHARS = 700
_DIGEST_SALIENCE = 0.8
# Relabel/redigest when the cluster size has changed by more than this
# fraction since the cached digest was generated (membership drifted).
_SIZE_DRIFT_FRACTION = 0.5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _trim(text: str | None, *, max_chars: int) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip(",;: ") + "\u2026"


class TopicDigestWorker:
    """IdleWorker that writes a rolling one-paragraph digest per cluster."""

    name = "topic_digest"

    def __init__(
        self,
        *,
        topic_graph: "TopicGraph",
        memory_store: "MemoryStore",
        embedder: "Embedder",
        ollama: "OllamaClient",
        chat_model: str,
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        notify_memory_added: Callable[[dict], None] | None = None,
        notify_memory_updated: Callable[[dict], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._topic_graph = topic_graph
        self._memory_store = memory_store
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._notify_memory_added = notify_memory_added
        self._notify_memory_updated = notify_memory_updated
        self._clock = clock or _utcnow
        # {cluster_id: digest_memory_id}; rebuilt every run, read by the
        # RAG retriever through an injected provider. Warm-loaded from kv.
        self.cluster_digest_map: dict[int, int] = self._load_map()

    # ── IdleWorker protocol ───────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(self._agent_settings, "topic_digest_interval_seconds", 3600.0)
        )

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        if not bool(getattr(self._agent_settings, "topic_digest_enabled", True)):
            return False
        if not getattr(self._topic_graph, "persistent", False):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at
        )

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._agent_settings, "topic_digest_enabled", True)):
            return {"skipped": True, "reason": "disabled"}
        if not getattr(self._topic_graph, "persistent", False):
            return {"skipped": True, "reason": "not_persistent"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        try:
            clusters = self._topic_graph.topic_clusters()
        except Exception:
            log.debug("topic_digest: topic_clusters raised", exc_info=True)
            return {"errored": True, "reason": "topic_clusters"}
        if not clusters:
            self._publish_map({})
            return {"checked": 0, "written": 0, "reused": 0, "reason": "no_clusters"}

        min_size = max(
            2, int(getattr(self._agent_settings, "topic_digest_min_cluster_size", 6))
        )
        max_per_run = max(
            1, int(getattr(self._agent_settings, "topic_digest_max_per_run", 3))
        )

        dense = [c for c in clusters if int(c.size) >= min_size]
        # Largest clusters first -- the densest topic knots are the most
        # worth a stored summary, and this bounds the per-tick LLM spend.
        dense.sort(key=lambda c: int(c.size), reverse=True)

        new_map: dict[int, int] = {}
        reused = 0
        todo: list[Any] = []
        for cluster in dense:
            rep = int(cluster.representative_id)
            size = int(cluster.size)
            cid = int(cluster.cluster_id)
            cached = self._read_cache(rep)
            mem_id = self._cached_memory_id(cached)
            if (
                mem_id is not None
                and not self._drifted(size, cached.get("size"))
                and self._digest_exists(mem_id)
            ):
                new_map[cid] = mem_id
                reused += 1
                continue
            todo.append((cluster, mem_id))

        written = 0
        for cluster, existing_id in todo[:max_per_run]:
            if self._cancel_event.is_set():
                break
            snippets = self._snippets_block(cluster)
            if not snippets:
                continue
            digest_text = self._call_llm(snippets)
            if not digest_text:
                continue
            mem_id = self._write_digest(cluster, digest_text, existing_id)
            if mem_id is None:
                continue
            self._write_cache(int(cluster.representative_id), mem_id, int(cluster.size))
            new_map[int(cluster.cluster_id)] = mem_id
            written += 1

        self._publish_map(new_map)

        if written or reused:
            log.info(
                "topic_digest run done: dense=%d written=%d reused=%d pending=%d",
                len(dense),
                written,
                reused,
                max(0, len(todo) - written),
            )
        return {
            "checked": len(clusters),
            "dense": len(dense),
            "written": written,
            "reused": reused,
            "pending": max(0, len(todo) - written),
            "mapped": len(new_map),
        }

    # ── cluster→digest map ─────────────────────────────────────────────

    def _publish_map(self, new_map: dict[int, int]) -> None:
        self.cluster_digest_map = dict(new_map)
        try:
            self._kv_set(
                _KV_MAP_KEY,
                json.dumps({str(k): int(v) for k, v in new_map.items()}),
            )
        except Exception:
            log.debug("topic_digest: map persist failed", exc_info=True)

    def _load_map(self) -> dict[int, int]:
        try:
            raw = self._kv_get(_KV_MAP_KEY)
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[int, int] = {}
        for k, v in parsed.items():
            try:
                out[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
        return out

    def digest_for_cluster(self, cluster_id: int) -> int | None:
        """Provider read for the RAG retriever: digest memory id for a cluster."""
        try:
            return self.cluster_digest_map.get(int(cluster_id))
        except (TypeError, ValueError):
            return None

    # ── cache ─────────────────────────────────────────────────────────

    def _read_cache(self, rep: int) -> dict[str, Any]:
        try:
            raw = self._kv_get(_KV_PREFIX + str(rep))
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _write_cache(self, rep: int, memory_id: int, size: int) -> None:
        try:
            self._kv_set(
                _KV_PREFIX + str(rep),
                json.dumps({"memory_id": int(memory_id), "size": int(size)}),
            )
        except Exception:
            log.debug("topic_digest: cache write failed (rep=%s)", rep, exc_info=True)

    @staticmethod
    def _cached_memory_id(cached: dict[str, Any]) -> int | None:
        try:
            mid = int(cached.get("memory_id"))
        except (TypeError, ValueError):
            return None
        return mid if mid > 0 else None

    def _digest_exists(self, memory_id: int) -> bool:
        try:
            mem = self._memory_store.get(int(memory_id))
        except Exception:
            return False
        return mem is not None and str(getattr(mem, "kind", "")) == "topic_digest"

    @staticmethod
    def _drifted(current_size: int, cached_size: Any) -> bool:
        try:
            cached = int(cached_size)
        except (TypeError, ValueError):
            return True
        if cached <= 0:
            return True
        return abs(int(current_size) - cached) / cached > _SIZE_DRIFT_FRACTION

    # ── digest write ────────────────────────────────────────────────────

    def _write_digest(
        self, cluster: Any, digest_text: str, existing_id: int | None
    ) -> int | None:
        try:
            embedding = self._embedder.embed(digest_text)
        except Exception:
            log.warning("topic_digest embed failed", exc_info=True)
            return None
        member_ids = [int(m) for m in list(cluster.member_ids)[:_MAX_SNIPPETS]]
        metadata = {
            "cluster_representative_id": int(cluster.representative_id),
            "member_count": int(cluster.size),
            "refreshed_at": self._clock().isoformat(),
            "source_ids": member_ids,
        }
        # Refresh the existing row in place when we have one (keeps the same
        # memory id so the cluster→digest map and Memory-tab row are stable);
        # otherwise insert a fresh long_term row, skipping dedupe (a digest
        # is intentionally near the topic it summarises).
        if existing_id is not None and self._digest_exists(existing_id):
            try:
                updated = self._memory_store.update(
                    int(existing_id),
                    content=digest_text,
                    embedding=embedding,
                    salience=_DIGEST_SALIENCE,
                    metadata=metadata,
                )
            except Exception:
                log.warning("topic_digest update failed", exc_info=True)
                return None
            if updated is None:
                return None
            if self._notify_memory_updated is not None:
                try:
                    self._notify_memory_updated(updated.to_dict())
                except Exception:
                    log.debug("topic_digest notify updated failed", exc_info=True)
            return int(updated.id)

        try:
            new_mem = self._memory_store.add(
                content=digest_text,
                kind="topic_digest",
                embedding=embedding,
                salience=_DIGEST_SALIENCE,
                tier="long_term",
                skip_dedupe=True,
                metadata=metadata,
            )
        except Exception:
            log.warning("topic_digest write failed", exc_info=True)
            return None
        if new_mem is None:
            return None
        if self._notify_memory_added is not None:
            try:
                self._notify_memory_added(new_mem.to_dict())
            except Exception:
                log.debug("topic_digest notify added failed", exc_info=True)
        return int(new_mem.id)

    # ── prompt + LLM ──────────────────────────────────────────────────

    def _snippets_block(self, cluster: Any) -> str:
        lines: list[str] = []
        for mid in list(cluster.member_ids)[:_MAX_SNIPPETS]:
            try:
                mem = self._memory_store.get(int(mid))
            except Exception:
                mem = None
            if mem is None:
                continue
            # Defensive: never feed a prior digest back into a fresh one.
            if str(getattr(mem, "kind", "")) == "topic_digest":
                continue
            snippet = _trim(getattr(mem, "content", ""), max_chars=_MAX_SNIPPET_CHARS)
            if snippet:
                lines.append(f"- {snippet}")
        return "\n".join(lines)

    def _call_llm(self, snippets: str) -> str:
        max_tokens = max(
            32, int(getattr(self._agent_settings, "topic_digest_max_tokens", 256))
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_TEMPLATE.format(snippets=snippets)},
        ]
        t0 = time.monotonic()
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": max_tokens, "temperature": 0.3},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="topic_digest_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("topic_digest chat_stream raised", exc_info=True)
            return ""
        if self._cancel_event.is_set():
            return ""
        raw = "".join(chunks).strip()
        digest = self._parse_digest(raw)
        log.debug(
            "topic_digest generated: chars=%d llm_ms=%.0f",
            len(digest),
            (time.monotonic() - t0) * 1000.0,
        )
        return digest

    @staticmethod
    def _parse_digest(raw: str) -> str:
        if not raw:
            return ""
        match = _JSON_OBJECT_RE.search(raw)
        if match is None:
            return ""
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        digest = str(parsed.get("digest") or "").strip().strip("\"'")
        if len(digest) < 8:
            return ""
        return _trim(digest, max_chars=_MAX_DIGEST_CHARS)


__all__ = ["TopicDigestWorker"]
