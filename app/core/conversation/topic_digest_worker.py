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
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import WorkSignal, pressure_from_count
from app.core.session.session_text_utils import resolve_user_name
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.infra.settings import AgentSettings
    from app.core.memory.memory_store import MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.topic_digest_worker")


_KV_PREFIX = "aiko.topic_digest."
_KV_MAP_KEY = "aiko.topic_digest_map"

def _build_system_prompt(
    user_display_name: str = "the user",
    assistant_name: str = "Aiko",
) -> str:
    """System prompt for the digest worker, name-templated at run time.

    Resolved per run so a rename via onboarding propagates without
    re-creating the worker. Naming both parties keeps digests personal
    ("Jacob …" / "Aiko …") instead of drifting into "the user" / "the AI
    companion" when a snippet happens to be phrased impersonally.
    """
    name = user_display_name or "the user"
    aiko = assistant_name or "Aiko"
    return (
        f"You write a short digest of what the AI companion {aiko} knows "
        "about ONE topic, given a set of memory snippets that were grouped "
        "together because they are about the same thing. Write 2-4 plain "
        "sentences that compress what these memories collectively say -- "
        "the gist, the specifics that matter, and any throughline. Refer "
        f"to the user as {name} and to the companion as {aiko} by name -- "
        "never 'the user' or 'the AI companion'. Be concrete; do NOT add "
        "facts that are not in the snippets, do NOT use the words "
        "'memories', 'cluster', 'topic', or 'snippets', and do NOT add a "
        "preamble. Each snippet ends with how long ago it was recorded -- "
        "respect that: something noted months ago is part of the history, "
        "not the present. "
        + timephrase.STORED_TEXT_TIME_RULE
        + ' Reply with ONE JSON object on a single line and nothing '
        'else: {"digest": "<2-4 sentences>"}.'
    )


# Back-compat for importers/tests that referenced the module-level prompt.
# New code should call ``_build_system_prompt(name)`` per run.
_SYSTEM_PROMPT = _build_system_prompt()

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
    return timephrase.utcnow()


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
        user_display_name_provider: Callable[[], str] | None = None,
        assistant_display_name_provider: Callable[[], str] | None = None,
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
        # Identity providers evaluated per run so a rename propagates
        # without re-creating the worker (mirrors MemoryExtractor).
        self._user_display_name_provider = user_display_name_provider
        self._assistant_display_name_provider = assistant_display_name_provider
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
        return bool(getattr(self._topic_graph, "persistent", False))

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Pressure from dense clusters with no digest behind them.

        ``cluster_digest_map`` is rebuilt at the end of every run and
        held in memory, so "which dense clusters are undigested" costs
        one dict lookup each — no ``kv_get`` per cluster the way
        rebuilding ``run``'s ``todo`` list would. Drift (a digested
        cluster that has since grown) is left to the heartbeat, since
        a stale digest still answers the retriever and a missing one
        does not.
        """
        if not bool(getattr(self._agent_settings, "topic_digest_enabled", True)):
            return WorkSignal(pressure=0.0, reason="disabled")
        if not getattr(self._topic_graph, "persistent", False):
            return WorkSignal(pressure=0.0, reason="not persistent")
        try:
            clusters = self._topic_graph.topic_clusters()
        except Exception:
            log.debug("topic_digest: demand probe failed", exc_info=True)
            return None
        min_size = max(
            2, int(getattr(self._agent_settings, "topic_digest_min_cluster_size", 6))
        )
        missing = sum(
            1 for c in clusters
            if int(c.size) >= min_size
            and int(c.cluster_id) not in self.cluster_digest_map
        )
        max_per_run = max(
            1, int(getattr(self._agent_settings, "topic_digest_max_per_run", 3))
        )
        return WorkSignal(
            pressure=pressure_from_count(missing, saturation=max_per_run),
            reason=f"{missing} undigested",
            needs_llm=missing > 0,
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

        # Reap orphaned digests: rows whose cluster was reassigned/dropped
        # on a past refit and no longer map to anything live. Protect the
        # freshly-mapped digests AND those still queued for regeneration
        # this tick (their existing row is about to be refreshed, not
        # abandoned). Only the derived digest is deleted -- member memories
        # are untouched and re-cluster into a new digest later.
        pending_ids = {
            int(mid) for _cluster, mid in todo[max_per_run:] if mid is not None
        }
        reaped = self._reap_orphans(
            protected=set(new_map.values()) | pending_ids
        )

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
            "reaped": reaped,
        }

    def _reap_orphans(self, *, protected: set[int]) -> int:
        """Delete ``topic_digest`` rows not tied to a live cluster.

        No-op unless ``topic_digest_reap_orphans`` is enabled. Pinned
        digests are always kept. Deletion routes through
        :meth:`MemoryStore.delete` so SQLite, the in-process mirror, and
        the LanceDB vector stay in sync.
        """
        if not bool(
            getattr(self._agent_settings, "topic_digest_reap_orphans", True)
        ):
            return 0
        try:
            digests = self._memory_store.iter_by_kind("topic_digest")
        except Exception:
            log.debug("topic_digest: iter_by_kind failed", exc_info=True)
            return 0
        reaped = 0
        for mem in digests:
            try:
                mid = int(mem.id)
            except (TypeError, ValueError, AttributeError):
                continue
            if mid in protected or bool(getattr(mem, "pinned", False)):
                continue
            try:
                if self._memory_store.delete(mid):
                    reaped += 1
            except Exception:
                log.debug(
                    "topic_digest: reap delete failed (id=%s)", mid,
                    exc_info=True,
                )
        if reaped:
            log.info("topic_digest reaped %d orphaned digest(s)", reaped)
        return reaped

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
        now = timephrase.utcnow()
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
                # K-time10: age-tag each snippet. A digest summarises a
                # topic *over time*, and feeding the whole cluster in
                # undated is how "Jacob was ill" from May gets written
                # into a digest as though it were the current state.
                created_at = str(getattr(mem, "created_at", "") or "")
                if timephrase.parse_iso(created_at) is not None:
                    snippet = (
                        f"{snippet} ({timephrase.humanize_past(created_at, now)})"
                    )
                lines.append(f"- {snippet}")
        return "\n".join(lines)

    def _resolve_user_name(self) -> str:
        return resolve_user_name(self._user_display_name_provider)

    def _resolve_assistant_name(self) -> str:
        return resolve_user_name(
            self._assistant_display_name_provider, fallback="Aiko"
        )

    def _call_llm(self, snippets: str) -> str:
        max_tokens = max(
            32, int(getattr(self._agent_settings, "topic_digest_max_tokens", 256))
        )
        # Anchor resolved per call, not at module import: ``_SYSTEM_PROMPT``
        # is built once at load and would freeze the date to whenever the
        # process started.
        system_prompt = (
            timephrase.today_anchor()
            + "\n\n"
            + _build_system_prompt(
                self._resolve_user_name(), self._resolve_assistant_name()
            )
        )
        messages = [
            {"role": "system", "content": system_prompt},
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
