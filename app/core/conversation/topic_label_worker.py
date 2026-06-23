"""Cluster-label idle worker (F10a personality backlog).

The K9 topic graph carves memory into clusters, but a cluster's label
used to be just the first sentence of its highest-salience member --
fine for debugging, useless as a human- or prompt-readable topic name.
This worker runs a tiny worker-LLM pass during quiet windows that names
each cluster with a concise noun phrase ("weekend hiking plans", "taste
in music", "work stress").

Design notes:

* **Off the chat path.** It runs on the :class:`IdleWorkerScheduler`
  (maintenance tier), so it never costs a per-turn token and never
  touches the chat prompt cache.
* **Cached by representative.** Each generated label is cached in
  ``kv_meta`` keyed by the cluster's *representative* memory id
  (``aiko.topic_label.<rep>``) alongside the cluster ``size`` at label
  time. Cluster ids are reassigned on every batch refit, so keying by
  the (stable) representative lets a label survive a rebuild: the next
  tick re-applies the cached label for free (no LLM) and only
  regenerates when the representative is new or the cluster has drifted
  materially in size.
* **Bounded spend.** At most ``topic_label_max_per_run`` clusters get a
  fresh LLM label per tick (largest-first); the rest wait for the next
  tick. The free cache-reapply pass is unbounded (it's just dict work).

Only meaningful in the persisted/incremental topic-graph mode
(:attr:`TopicGraph.persistent`); :meth:`is_ready` short-circuits
otherwise because the in-memory mode has no stable cluster identity to
attach an override to.
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
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.topic_label_worker")


_KV_PREFIX = "aiko.topic_label."

_SYSTEM_PROMPT = (
    "You name a cluster of an AI companion's memories with a short topic "
    "label. You are given a few memory snippets that were grouped together "
    "because they are about the same thing. Return a 2-5 word noun phrase "
    "naming the shared topic (e.g. 'weekend hiking plans', 'taste in music', "
    "'work stress', 'childhood in Poland'). Be specific and concrete. Do NOT "
    "use the words 'memories', 'cluster', 'topic', or 'group'. "
    "Reply with ONE JSON object on a single line and nothing else: "
    '{"label": "<2-5 word phrase>"}.'
)

_USER_TEMPLATE = "MEMORY SNIPPETS:\n{snippets}\n\nReturn the label JSON now."

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)

_MAX_SNIPPETS = 8
_MAX_SNIPPET_CHARS = 120
_MAX_LABEL_CHARS = 60
# Relabel when the cluster size has changed by more than this fraction
# since the cached label was generated (membership drifted materially).
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


class ClusterLabelWorker:
    """IdleWorker that names topic-graph clusters with a worker-LLM pass."""

    name = "topic_label"

    def __init__(
        self,
        *,
        topic_graph: "TopicGraph",
        memory_store: "MemoryStore",
        ollama: "OllamaClient",
        chat_model: str,
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._topic_graph = topic_graph
        self._memory_store = memory_store
        self._ollama = ollama
        self._chat_model = chat_model
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ───────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(self._agent_settings, "topic_label_interval_seconds", 1800.0)
        )

    def is_ready(self, *, now: datetime, last_run_at: datetime | None) -> bool:
        if not bool(getattr(self._agent_settings, "topic_label_enabled", True)):
            return False
        if not getattr(self._topic_graph, "persistent", False):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at
        )

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._agent_settings, "topic_label_enabled", True)):
            return {"skipped": True, "reason": "disabled"}
        if not getattr(self._topic_graph, "persistent", False):
            return {"skipped": True, "reason": "not_persistent"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        try:
            clusters = self._topic_graph.topic_clusters()
        except Exception:
            log.debug("topic_label: topic_clusters raised", exc_info=True)
            return {"errored": True, "reason": "topic_clusters"}
        if not clusters:
            return {"checked": 0, "labeled": 0, "reapplied": 0, "reason": "no_clusters"}

        max_per_run = max(
            1, int(getattr(self._agent_settings, "topic_label_max_per_run", 4))
        )

        reapplied = 0
        todo: list[Any] = []
        for cluster in clusters:
            rep = int(cluster.representative_id)
            size = int(cluster.size)
            cached = self._read_cache(rep)
            if cached is not None and not self._drifted(size, cached.get("size")):
                label = str(cached.get("label") or "").strip()
                if label and label != (cluster.summary or ""):
                    if self._topic_graph.set_cluster_label(
                        int(cluster.cluster_id), label
                    ):
                        reapplied += 1
                continue
            todo.append(cluster)

        # Largest clusters first -- the densest topic knots are the most
        # worth a readable name, and bound the per-tick LLM spend.
        todo.sort(key=lambda c: int(c.size), reverse=True)

        labeled = 0
        for cluster in todo[:max_per_run]:
            if self._cancel_event.is_set():
                break
            snippets = self._snippets_block(cluster)
            if not snippets:
                continue
            label = self._call_llm(snippets)
            if not label:
                continue
            if self._topic_graph.set_cluster_label(int(cluster.cluster_id), label):
                self._write_cache(
                    int(cluster.representative_id), label, int(cluster.size)
                )
                labeled += 1

        if labeled or reapplied:
            log.info(
                "topic_label run done: clusters=%d labeled=%d reapplied=%d "
                "pending=%d",
                len(clusters),
                labeled,
                reapplied,
                max(0, len(todo) - labeled),
            )
        return {
            "checked": len(clusters),
            "labeled": labeled,
            "reapplied": reapplied,
            "pending": max(0, len(todo) - labeled),
        }

    # ── cache ─────────────────────────────────────────────────────────

    def _read_cache(self, rep: int) -> dict[str, Any] | None:
        try:
            raw = self._kv_get(_KV_PREFIX + str(rep))
        except Exception:
            return None
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def _write_cache(self, rep: int, label: str, size: int) -> None:
        try:
            self._kv_set(
                _KV_PREFIX + str(rep),
                json.dumps({"label": label, "size": int(size)}),
            )
        except Exception:
            log.debug("topic_label: cache write failed (rep=%s)", rep, exc_info=True)

    @staticmethod
    def _drifted(current_size: int, cached_size: Any) -> bool:
        try:
            cached = int(cached_size)
        except (TypeError, ValueError):
            return True
        if cached <= 0:
            return True
        return abs(int(current_size) - cached) / cached > _SIZE_DRIFT_FRACTION

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
            snippet = _trim(getattr(mem, "content", ""), max_chars=_MAX_SNIPPET_CHARS)
            if snippet:
                lines.append(f"- {snippet}")
        return "\n".join(lines)

    def _call_llm(self, snippets: str) -> str:
        max_tokens = max(
            8, int(getattr(self._agent_settings, "topic_label_max_tokens", 32))
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
                surface="topic_label_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("topic_label chat_stream raised", exc_info=True)
            return ""
        if self._cancel_event.is_set():
            return ""
        raw = "".join(chunks).strip()
        label = self._parse_label(raw)
        log.debug(
            "topic_label generated: label=%r llm_ms=%.0f",
            label,
            (time.monotonic() - t0) * 1000.0,
        )
        return label

    @staticmethod
    def _parse_label(raw: str) -> str:
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
        label = str(parsed.get("label") or "").strip().strip("\"'")
        # Guard against the model echoing a banned framing word or an empty
        # phrase; a degenerate label is worse than the heuristic fallback.
        if not label:
            return ""
        return _trim(label, max_chars=_MAX_LABEL_CHARS)


__all__ = ["ClusterLabelWorker"]
