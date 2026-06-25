"""K64c — Curiosity-gradient worker ("I keep brushing past X, I'm curious").

Third member of the K64 *freedom of thought* family. K64a connects two
*distant* topics; K64b notices a topic's mass *drifting* over time; K64c
notices the **boundary** of what Aiko knows: a *thin* topic cluster sitting
right next to a *dense* one. That thin cluster is the under-explored **edge**
of familiar territory — she's been near the topic a lot but never actually
dug into the bit just adjacent to it — and that's exactly where genuine
curiosity lives ("we talk about hiking gear all the time, but I realise I've
never asked you about trail navigation").

This worker is the silent producer. On an idle tick it:

  * reads the topic graph's labelled clusters (centroid + size + label),
  * for each *thin* cluster (member count in
    ``[thin_min_size, thin_max_size]``) finds its nearest *dense* cluster
    (size >= ``dense_min_size``) by centroid cosine,
  * keeps it as a curiosity edge when that cosine is in
    ``[adjacency_min_cosine, adjacency_max_cosine]`` (genuinely adjacent —
    close enough to be the edge of the dense topic, but not a near-duplicate
    of it),
  * skips any edge noticed within its per-edge cooldown window,
  * appends ``{at, dense_topic, thin_topic, edge_key, cosine}`` to a small
    kv_meta journal ring (``aiko.curiosity_gradients``).

The consumer is
:meth:`InnerLifeProvidersMixin._render_curiosity_gradient_block`, which
surfaces a drafted edge **only when the live turn is on either topic**
(lexical overlap with the user's message) so the curious beat lands in
context. It never speaks or fires a proactive nudge; the cue is a private
prompt hint, and the chat model phrases the actual curious question itself.

No LLM: the signal is pure cluster geometry (sizes + centroid cosines), so
the worker is a cheap pass. Rarity is the point, so it's paced by a long
interval, a small daily cap, and a long per-edge cooldown. Every failure
path is swallowed and logged at debug — the worst case is a missed beat,
never a broken insert or a crashed tick.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.associative_wander_worker import pair_key
from app.core.proactive.idle_worker import default_is_ready

# Reuse the cheap lexical "is the live turn on this topic?" gate.
from app.core.proactive.knowledge_gap_notice_worker import topic_relevant

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import TopicCluster, TopicGraph


log = logging.getLogger("app.curiosity_gradient_worker")


# kv_meta keys this worker owns, plus the shared journal key the surfacing
# provider reads.
CURIOSITY_GRADIENT_JOURNAL_KEY = "aiko.curiosity_gradients"
_KV_EDGE_COOLDOWNS = "curiosity_gradient.edge_cooldowns"
_KV_DAY = "curiosity_gradient.day"
_KV_DAY_COUNT = "curiosity_gradient.day_count"


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


def _centroid_cosine(a: Any, b: Any) -> float | None:
    """Cosine of two centroid vectors, or ``None`` if either is unusable."""
    try:
        import numpy as np

        va = np.asarray(a, dtype=np.float32)
        vb = np.asarray(b, dtype=np.float32)
        if va.size == 0 or vb.size == 0 or va.shape != vb.shape:
            return None
        na = float(np.linalg.norm(va))
        nb = float(np.linalg.norm(vb))
        if na <= 0.0 or nb <= 0.0:
            return None
        return float(np.dot(va, vb) / (na * nb))
    except Exception:
        return None


@dataclass(frozen=True)
class GradientEdge:
    """One curiosity edge: a thin cluster on the rim of a dense one."""

    dense_cluster_id: int
    thin_cluster_id: int
    dense_label: str
    thin_label: str
    cosine: float
    key: str


def find_gradient_edges(
    clusters: list["TopicCluster"],
    *,
    dense_min_size: int,
    thin_min_size: int,
    thin_max_size: int,
    adjacency_min: float,
    adjacency_max: float,
) -> list[GradientEdge]:
    """All curiosity edges, strongest (most adjacent) first.

    A thin cluster (members in ``[thin_min_size, thin_max_size]``, non-blank
    label) qualifies when its *nearest* dense cluster (size >=
    ``dense_min_size``, non-blank label) has a centroid cosine in
    ``[adjacency_min, adjacency_max]`` — close enough to be the under-
    explored edge of a familiar topic, but not a near-duplicate of it. Pure
    + sortable so the geometry can be pinned in tests without a live graph.
    """
    dense: list["TopicCluster"] = []
    thin: list["TopicCluster"] = []
    for c in clusters:
        label = (getattr(c, "summary", "") or "").strip()
        if not label or getattr(c, "centroid", None) is None:
            continue
        try:
            size = int(getattr(c, "size", 0))
        except (TypeError, ValueError):
            continue
        if size >= int(dense_min_size):
            dense.append(c)
        elif int(thin_min_size) <= size <= int(thin_max_size):
            thin.append(c)

    edges: list[GradientEdge] = []
    for t in thin:
        best: "TopicCluster | None" = None
        best_cos = -2.0
        for d in dense:
            if int(d.cluster_id) == int(t.cluster_id):
                continue
            cos = _centroid_cosine(t.centroid, d.centroid)
            if cos is None:
                continue
            if cos > best_cos:
                best_cos = cos
                best = d
        if best is None:
            continue
        if not (float(adjacency_min) <= best_cos <= float(adjacency_max)):
            continue
        dense_label = (best.summary or "").strip()
        thin_label = (t.summary or "").strip()
        edges.append(
            GradientEdge(
                dense_cluster_id=int(best.cluster_id),
                thin_cluster_id=int(t.cluster_id),
                dense_label=dense_label,
                thin_label=thin_label,
                cosine=best_cos,
                key=pair_key(dense_label, thin_label),
            )
        )
    edges.sort(key=lambda e: e.cosine, reverse=True)
    return edges


# ── journal helpers (shared with the surfacing provider) ────────────────


def load_gradients(
    kv_get: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Return the curiosity-gradient journal ring (oldest -> newest)."""
    try:
        raw = kv_get(CURIOSITY_GRADIENT_JOURNAL_KEY)
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


def append_gradient(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    ring = load_gradients(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(CURIOSITY_GRADIENT_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("curiosity_gradient journal write failed", exc_info=True)


def gradient_relevant(entry: dict[str, Any], user_text: str) -> bool:
    """True when the live turn is on either side of a curiosity edge."""
    dense = str(entry.get("dense_topic") or "")
    thin = str(entry.get("thin_topic") or "")
    return topic_relevant(dense, user_text) or topic_relevant(thin, user_text)


class CuriosityGradientWorker:
    """IdleWorker that notices under-explored edges of familiar topics."""

    name = "curiosity_gradient"

    def __init__(
        self,
        *,
        topic_graph_provider: Callable[[], "TopicGraph | None"],
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        enabled_provider: Callable[[], bool] | None = None,
        interval_seconds: float = 5400.0,
        daily_cap: int = 3,
        journal_max: int = 6,
        dense_min_size: int = 8,
        thin_min_size: int = 2,
        thin_max_size: int = 4,
        adjacency_min_cosine: float = 0.40,
        adjacency_max_cosine: float = 0.90,
        edge_cooldown_hours: float = 96.0,
    ) -> None:
        self._topic_graph_provider = topic_graph_provider
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._enabled_provider = enabled_provider
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._daily_cap = max(0, int(daily_cap))
        self._journal_max = max(1, int(journal_max))
        self._dense_min_size = max(2, int(dense_min_size))
        self._thin_min_size = max(1, int(thin_min_size))
        self._thin_max_size = max(self._thin_min_size, int(thin_max_size))
        self._adjacency_min_cosine = float(adjacency_min_cosine)
        self._adjacency_max_cosine = float(adjacency_max_cosine)
        self._edge_cooldown_hours = max(0.0, float(edge_cooldown_hours))
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

        now = _utcnow()
        if not force and not self._under_daily_cap(now):
            return {"drafted": 0, "skipped_daily_cap": True}

        graph = self._safe_graph()
        if graph is None:
            return {"drafted": 0, "no_graph": True}
        try:
            clusters = graph.topic_clusters()
        except Exception:
            log.debug("curiosity_gradient topic_clusters failed", exc_info=True)
            return {"drafted": 0, "no_graph": True}

        edges = find_gradient_edges(
            clusters,
            dense_min_size=self._dense_min_size,
            thin_min_size=self._thin_min_size,
            thin_max_size=self._thin_max_size,
            adjacency_min=self._adjacency_min_cosine,
            adjacency_max=self._adjacency_max_cosine,
        )
        if not edges:
            return {"drafted": 0, "no_edge": True}

        cooldowns = self._load_cooldowns()
        chosen = None
        for edge in edges:
            if not force and self._on_cooldown(cooldowns, edge.key, now):
                continue
            chosen = edge
            break
        if chosen is None:
            return {"drafted": 0, "all_on_cooldown": True}

        append_gradient(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "dense_topic": chosen.dense_label[:200],
                "thin_topic": chosen.thin_label[:200],
                "edge_key": chosen.key,
                "cosine": round(chosen.cosine, 3),
            },
            max_entries=self._journal_max,
        )
        cooldowns[chosen.key] = now.isoformat(timespec="seconds")
        self._save_cooldowns(cooldowns, now)
        self._bump_daily(now)
        log.info(
            "curiosity-gradient drafted: dense=%r thin=%r cos=%.3f",
            chosen.dense_label[:60], chosen.thin_label[:60], chosen.cosine,
        )
        return {
            "drafted": 1,
            "dense_topic": chosen.dense_label,
            "thin_topic": chosen.thin_label,
            "edge_key": chosen.key,
            "cosine": round(chosen.cosine, 3),
        }

    # ── MCP debug ─────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm the next ``run()`` to bypass the per-edge + daily caps."""
        self._force_next = True

    # ── gates / helpers ────────────────────────────────────────────────

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
        if self._edge_cooldown_hours <= 0:
            return False
        last = _parse_iso(cooldowns.get(key))
        if last is None:
            return False
        return (now - last).total_seconds() / 3600.0 < self._edge_cooldown_hours

    def _under_daily_cap(self, now: datetime) -> bool:
        if self._daily_cap <= 0:
            return False
        today = now.astimezone().strftime("%Y-%m-%d")
        if self._kv_get_safe(_KV_DAY) != today:
            return True
        try:
            count = int(self._kv_get_safe(_KV_DAY_COUNT) or "0")
        except (TypeError, ValueError):
            count = 0
        return count < self._daily_cap

    def _bump_daily(self, now: datetime) -> None:
        today = now.astimezone().strftime("%Y-%m-%d")
        if self._kv_get_safe(_KV_DAY) != today:
            self._kv_set_safe(_KV_DAY, today)
            self._kv_set_safe(_KV_DAY_COUNT, "1")
            return
        try:
            count = int(self._kv_get_safe(_KV_DAY_COUNT) or "0")
        except (TypeError, ValueError):
            count = 0
        self._kv_set_safe(_KV_DAY_COUNT, str(count + 1))

    def _load_cooldowns(self) -> dict[str, str]:
        try:
            raw = self._kv_get(_KV_EDGE_COOLDOWNS)
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            blob = json.loads(raw)
        except Exception:
            return {}
        return (
            {str(k): str(v) for k, v in blob.items()}
            if isinstance(blob, dict)
            else {}
        )

    def _save_cooldowns(self, cooldowns: dict[str, str], now: datetime) -> None:
        if self._edge_cooldown_hours > 0:
            horizon = self._edge_cooldown_hours * 2.0
            pruned: dict[str, str] = {}
            for k, v in cooldowns.items():
                last = _parse_iso(v)
                if last is None:
                    continue
                if (now - last).total_seconds() / 3600.0 <= horizon:
                    pruned[k] = v
            cooldowns = pruned
        try:
            self._kv_set(_KV_EDGE_COOLDOWNS, json.dumps(cooldowns))
        except Exception:
            log.debug("curiosity_gradient cooldown write failed", exc_info=True)

    def _kv_get_safe(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _kv_set_safe(self, key: str, value: str) -> None:
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug(
                "curiosity_gradient kv_set failed key=%s", key, exc_info=True,
            )


__all__ = [
    "CuriosityGradientWorker",
    "GradientEdge",
    "CURIOSITY_GRADIENT_JOURNAL_KEY",
    "find_gradient_edges",
    "load_gradients",
    "append_gradient",
    "gradient_relevant",
]
