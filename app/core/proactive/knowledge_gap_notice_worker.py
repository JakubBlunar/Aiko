"""F10f — Knowledge-gap notice worker ("I keep circling X but never dug in").

The F9 :class:`~app.core.proactive.idle_knowledge_worker.IdleKnowledgeWorker`
already *acts* on knowledge gaps silently: it scores dense, low-``knowledge``-
coverage topic clusters and quietly researches them into ``kind="knowledge"``
rows. What F10f adds is the *self-aware* half — letting Aiko notice and own
the gap out loud ("honestly, this keeps coming up and I still don't know much
about it") instead of bluffing.

This worker is the silent producer. During a quiet window it:

  * reads the topic graph's
    :meth:`~app.core.conversation.topic_graph.TopicGraph.knowledge_gap_clusters`
    — dense clusters thin on learned knowledge,
  * skips any topic noticed within the per-topic cooldown window
    (a ``kv_meta`` map keyed by a stable hash of the label, so re-noticing
    dedup survives graph rebuilds that renumber cluster ids),
  * appends ``{at, topic, cluster_key, size, knowledge_count}`` to a small
    kv_meta journal ring (``aiko.knowledge_gap_notices``).

The consumer is
:meth:`InnerLifeProvidersMixin._render_knowledge_gap_notice_block`, which
surfaces a drafted notice **only when the live turn is actually on that
topic** (lexical overlap with the user's message) so the beat lands in
context rather than as a non-sequitur. It never speaks or fires a proactive
nudge — the cue is a private prompt hint, phrased by the chat model itself.

No LLM: the cue is just a topic + stats, so the worker is a cheap kv pass.
Every failure path is swallowed and logged at debug — the worst case is a
missed beat, never a broken insert or a crashed tick.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import TopicGraph


log = logging.getLogger("app.knowledge_gap_notice_worker")


# kv_meta keys this worker owns, plus the shared journal key the surfacing
# provider reads.
KNOWLEDGE_GAP_JOURNAL_KEY = "aiko.knowledge_gap_notices"
_KV_TOPIC_COOLDOWNS = "knowledge_gap_notice.topic_cooldowns"

_WORD_RE = re.compile(r"[a-z0-9]+")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
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


def topic_key(label: str) -> str:
    """Stable short hash of a topic label, robust to cluster renumbering.

    Lower-cased, whitespace-collapsed, hashed — so the same topic maps to
    the same key across graph rebuilds even when its ``cluster_id`` changes.
    """
    flat = " ".join((label or "").lower().split())
    return hashlib.sha1(flat.encode("utf-8")).hexdigest()[:16]


# ── journal helpers (shared with the surfacing provider) ────────────────


def load_notices(
    kv_get: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Return the knowledge-gap-notice journal ring (oldest -> newest)."""
    try:
        raw = kv_get(KNOWLEDGE_GAP_JOURNAL_KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        blob = json.loads(raw)
    except Exception:
        return []
    if not isinstance(blob, list):
        return []
    return [e for e in blob if isinstance(e, dict)]


def append_notice(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    ring = load_notices(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(KNOWLEDGE_GAP_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("knowledge_gap_notice journal write failed", exc_info=True)


class KnowledgeGapNoticeWorker:
    """IdleWorker that drafts "I don't really know much about X" cues."""

    name = "knowledge_gap_notice"

    def __init__(
        self,
        *,
        topic_graph_provider: Callable[[], "TopicGraph | None"],
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        enabled_provider: Callable[[], bool] | None = None,
        interval_seconds: float = 3600.0,
        min_size: int = 5,
        max_knowledge_fraction: float = 0.15,
        topic_cooldown_hours: float = 72.0,
        journal_max: int = 6,
    ) -> None:
        self._topic_graph_provider = topic_graph_provider
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._enabled_provider = enabled_provider
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._min_size = max(2, int(min_size))
        self._max_knowledge_fraction = max(0.0, float(max_knowledge_fraction))
        self._topic_cooldown_hours = max(0.0, float(topic_cooldown_hours))
        self._journal_max = max(1, int(journal_max))
        # MCP debug: force the next run() to draft even if on cooldown.
        self._force_next = False

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

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
        force = self._force_next
        self._force_next = False
        if not self._enabled():
            return {"drafted": 0, "disabled": True}

        graph = self._safe_graph()
        if graph is None:
            return {"drafted": 0, "no_graph": True}

        try:
            candidates = graph.knowledge_gap_clusters(
                min_size=self._min_size,
                max_knowledge_fraction=self._max_knowledge_fraction,
                top_n=5,
            )
        except Exception:
            log.debug("knowledge_gap_clusters failed", exc_info=True)
            return {"drafted": 0, "no_candidate": True}
        if not candidates:
            return {"drafted": 0, "no_candidate": True}

        now = _utcnow()
        cooldowns = self._load_cooldowns()
        chosen = None
        for cand in candidates:
            key = topic_key(cand.label)
            if not force and self._on_cooldown(cooldowns, key, now):
                continue
            chosen = (cand, key)
            break
        if chosen is None:
            return {"drafted": 0, "all_on_cooldown": True}

        cand, key = chosen
        append_notice(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "topic": cand.label[:200],
                "cluster_key": key,
                "size": int(cand.size),
                "knowledge_count": int(cand.knowledge_count),
            },
            max_entries=self._journal_max,
        )
        cooldowns[key] = now.isoformat(timespec="seconds")
        self._save_cooldowns(cooldowns, now)
        log.info(
            "knowledge-gap-notice drafted: topic=%r size=%d knowledge=%d frac=%.2f",
            cand.label[:80],
            cand.size,
            cand.knowledge_count,
            cand.knowledge_fraction,
        )
        return {
            "drafted": 1,
            "topic": cand.label,
            "cluster_key": key,
            "size": cand.size,
            "knowledge_count": cand.knowledge_count,
        }

    # ── MCP debug ─────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm the next ``run()`` to bypass the per-topic cooldown."""
        self._force_next = True

    # ── gates / helpers ───────────────────────────────────────────────

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def _safe_graph(self) -> "TopicGraph | None":
        try:
            return self._topic_graph_provider()
        except Exception:
            return None

    def _on_cooldown(
        self, cooldowns: dict[str, str], key: str, now: datetime,
    ) -> bool:
        if self._topic_cooldown_hours <= 0:
            return False
        last = _parse_iso(cooldowns.get(key))
        if last is None:
            return False
        elapsed_h = (now - last).total_seconds() / 3600.0
        return elapsed_h < self._topic_cooldown_hours

    def _load_cooldowns(self) -> dict[str, str]:
        try:
            raw = self._kv_get(_KV_TOPIC_COOLDOWNS)
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            blob = json.loads(raw)
        except Exception:
            return {}
        return {str(k): str(v) for k, v in blob.items()} if isinstance(blob, dict) else {}

    def _save_cooldowns(self, cooldowns: dict[str, str], now: datetime) -> None:
        # Prune entries older than 2x the cooldown so the map can't grow
        # unbounded across many topics.
        if self._topic_cooldown_hours > 0:
            horizon = self._topic_cooldown_hours * 2.0
            pruned: dict[str, str] = {}
            for k, v in cooldowns.items():
                last = _parse_iso(v)
                if last is None:
                    continue
                if (now - last).total_seconds() / 3600.0 <= horizon:
                    pruned[k] = v
            cooldowns = pruned
        try:
            self._kv_set(_KV_TOPIC_COOLDOWNS, json.dumps(cooldowns))
        except Exception:
            log.debug("knowledge_gap_notice cooldown write failed", exc_info=True)


def topic_relevant(topic: str, user_text: str) -> bool:
    """True when the live turn looks like it's on ``topic``.

    Cheap lexical gate: at least one significant (>= 3-char) content word
    of the topic label appears in the user's message. Topic labels are
    short (a 2-5 word phrase), so a single shared content word is a
    reasonable "we're talking about this right now" signal — enough to
    keep the gap notice in context without an embedding round-trip.
    """
    topic_words = {w for w in _WORD_RE.findall((topic or "").lower()) if len(w) >= 3}
    if not topic_words:
        return False
    user_words = {w for w in _WORD_RE.findall((user_text or "").lower()) if len(w) >= 3}
    return bool(topic_words & user_words)


__all__ = [
    "KnowledgeGapNoticeWorker",
    "KNOWLEDGE_GAP_JOURNAL_KEY",
    "load_notices",
    "append_notice",
    "topic_key",
    "topic_relevant",
]
