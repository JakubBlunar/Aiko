"""K64b — Interest-drift worker ("I've been weirdly into X lately").

Second member of the K64 *freedom of thought* family. Where K64a
(:class:`~app.core.proactive.associative_wander_worker.AssociativeWanderWorker`)
notices a connection between two *distant* topics, K64b notices that Aiko's
own attention is **shifting** over time: a topic cluster that's been gaining
mass is a budding interest; one that's gone stagnant is a fading one. It's
the slow under-current sibling of K27 day-colour — not an announcement, a
register.

This worker is the silent producer. On an idle tick it:

  * reads each labelled topic cluster's current mass via the cheap
    :meth:`~app.core.conversation.topic_graph.TopicGraph.interest_map`
    (``(label, size)`` rows — no member join),
  * appends ``(now, size)`` to a small per-topic mass time-series in
    ``kv_meta`` (``aiko.interest_mass``), keyed by a stable hash of the
    label so it survives cluster renumbering,
  * once a topic has enough samples, classifies its drift over the window
    (:func:`classify_drift` — pure size-delta math, **no LLM**): fast recent
    growth → ``rising``; a sizable cluster that's gone flat → ``fading``,
  * skips any topic noticed within its per-topic cooldown window,
  * appends ``{at, topic, topic_key, direction, from_size, to_size}`` to a
    small kv_meta journal ring (``aiko.interest_drifts``).

The consumer is
:meth:`InnerLifeProvidersMixin._render_interest_drift_block`, which surfaces
a drafted drift **only when the live turn is actually on that topic**
(lexical overlap with the user's message) so the beat lands in context —
"funny, I've found myself drawn to this more lately" — rather than as a
non-sequitur. It never speaks or fires a proactive nudge; the cue is a
private prompt hint, phrased by the chat model itself.

No LLM: the cue is just a topic + a direction, so the worker is a cheap kv
pass. Rarity is the point (interests drift *slowly*), so it's paced by a
long interval, a small daily cap, and a long per-topic cooldown. Every
failure path is swallowed and logged at debug — the worst case is a missed
beat, never a broken insert or a crashed tick.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

# Reuse the stable per-topic key + the cheap "is the live turn on this
# topic?" gate the F10f notice worker already ships.
from app.core.proactive.knowledge_gap_notice_worker import (
    topic_key,
    topic_relevant,
)

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import TopicGraph


log = logging.getLogger("app.interest_drift_worker")


# kv_meta keys this worker owns, plus the shared journal key the surfacing
# provider reads.
INTEREST_DRIFT_JOURNAL_KEY = "aiko.interest_drifts"
_KV_MASS_SERIES = "aiko.interest_mass"
_KV_TOPIC_COOLDOWNS = "interest_drift.topic_cooldowns"

# Minimum absolute member gain across the window for a topic to count as
# "rising" — guards against a single new memory reading as a budding
# interest on a tiny cluster.
_RISE_MIN_DELTA = 3


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


def classify_drift(
    sizes: list[int],
    *,
    rise_ratio: float,
    fade_max_growth_ratio: float,
    min_size: int,
) -> str | None:
    """Classify a topic's mass trajectory over the sample window.

    ``sizes`` is the per-snapshot member count, oldest -> newest. Pure +
    deterministic so the geometry can be pinned in tests without a live
    graph:

      * ``rising`` — the cluster grew by at least ``_RISE_MIN_DELTA`` members
        AND by at least ``rise_ratio`` of its starting mass over the window
        (a genuinely budding interest, not noise).
      * ``fading`` — the cluster is still sizable (>= ``min_size``) but its
        growth over the window is at or below ``fade_max_growth_ratio`` (it
        was a real interest and has gone stagnant — attention cooled).
      * ``None`` — neutral (too few samples, too small, or steady-state).

    Rising is checked first, so a fast-growing large cluster never reads as
    fading.
    """
    if len(sizes) < 2:
        return None
    start = int(sizes[0])
    end = int(sizes[-1])
    if end < int(min_size):
        return None
    delta = end - start
    growth = (delta / start) if start > 0 else float("inf")
    if delta >= _RISE_MIN_DELTA and growth >= float(rise_ratio):
        return "rising"
    if growth <= float(fade_max_growth_ratio):
        return "fading"
    return None


@dataclass(frozen=True)
class DriftCandidate:
    """One topic whose attention-mass has drifted, + the evidence."""

    topic: str
    key: str
    direction: str  # "rising" | "fading"
    from_size: int
    to_size: int


# ── journal helpers (shared with the surfacing provider) ────────────────


def load_drifts(
    kv_get: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Return the interest-drift journal ring (oldest -> newest)."""
    try:
        raw = kv_get(INTEREST_DRIFT_JOURNAL_KEY)
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


def append_drift(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    ring = load_drifts(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(INTEREST_DRIFT_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("interest_drift journal write failed", exc_info=True)


def drift_relevant(entry: dict[str, Any], user_text: str) -> bool:
    """True when the live turn is on the drifting topic."""
    return topic_relevant(str(entry.get("topic") or ""), user_text)


class InterestDriftWorker:
    """IdleWorker that notices Aiko's own budding / fading interests."""

    name = "interest_drift"

    def __init__(
        self,
        *,
        topic_graph_provider: Callable[[], "TopicGraph | None"],
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        enabled_provider: Callable[[], bool] | None = None,
        interval_seconds: float = 21600.0,
        daily_cap: int = 3,
        journal_max: int = 6,
        min_size: int = 4,
        max_clusters: int = 40,
        window_samples: int = 8,
        min_samples: int = 3,
        rise_ratio: float = 0.5,
        fade_max_growth_ratio: float = 0.05,
        topic_cooldown_hours: float = 72.0,
    ) -> None:
        self._topic_graph_provider = topic_graph_provider
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._enabled_provider = enabled_provider
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._daily_cap = max(0, int(daily_cap))
        self._journal_max = max(1, int(journal_max))
        self._min_size = max(2, int(min_size))
        self._max_clusters = max(1, int(max_clusters))
        self._window_samples = max(2, int(window_samples))
        self._min_samples = max(2, int(min_samples))
        self._rise_ratio = max(0.0, float(rise_ratio))
        self._fade_max_growth_ratio = max(0.0, float(fade_max_growth_ratio))
        self._topic_cooldown_hours = max(0.0, float(topic_cooldown_hours))
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
            entries = graph.interest_map(
                top_n=self._max_clusters, min_size=self._min_size,
            )
        except Exception:
            log.debug("interest_drift interest_map failed", exc_info=True)
            return {"drafted": 0, "no_graph": True}

        now = _utcnow()
        # 1) Update the per-topic mass time-series with this tick's sizes.
        series = self._load_series()
        seen_keys: set[str] = set()
        for entry in entries:
            label = (getattr(entry, "label", "") or "").strip()
            size = int(getattr(entry, "size", 0) or 0)
            if not label:
                continue
            key = topic_key(label)
            seen_keys.add(key)
            row = series.get(key) or {"label": label, "history": []}
            row["label"] = label
            hist = row.get("history") or []
            hist.append([now.isoformat(timespec="seconds"), size])
            if len(hist) > self._window_samples:
                hist = hist[-self._window_samples:]
            row["history"] = hist
            series[key] = row
        self._prune_series(series, seen_keys)
        self._save_series(series)

        # 2) Classify drift for each topic with enough samples and pick the
        #    strongest candidate that isn't on cooldown.
        if not force and not self._under_daily_cap(now):
            return {"drafted": 0, "skipped_daily_cap": True}

        cooldowns = self._load_cooldowns()
        candidates = self._rank_candidates(series, cooldowns, now, force=force)
        if not candidates:
            return {"drafted": 0, "no_candidate": True}

        chosen = candidates[0]
        append_drift(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "topic": chosen.topic[:200],
                "topic_key": chosen.key,
                "direction": chosen.direction,
                "from_size": chosen.from_size,
                "to_size": chosen.to_size,
            },
            max_entries=self._journal_max,
        )
        cooldowns[chosen.key] = now.isoformat(timespec="seconds")
        self._save_cooldowns(cooldowns, now)
        self._bump_daily(now)
        log.info(
            "interest-drift drafted: topic=%r dir=%s %d->%d",
            chosen.topic[:80], chosen.direction,
            chosen.from_size, chosen.to_size,
        )
        return {
            "drafted": 1,
            "topic": chosen.topic,
            "direction": chosen.direction,
            "from_size": chosen.from_size,
            "to_size": chosen.to_size,
        }

    # ── MCP debug ─────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm the next ``run()`` to bypass the per-topic + daily caps."""
        self._force_next = True

    # ── candidate ranking ──────────────────────────────────────────────

    def _rank_candidates(
        self,
        series: dict[str, dict],
        cooldowns: dict[str, str],
        now: datetime,
        *,
        force: bool,
    ) -> list[DriftCandidate]:
        out: list[DriftCandidate] = []
        for key, row in series.items():
            hist = row.get("history") or []
            if len(hist) < self._min_samples:
                continue
            sizes = [int(s) for _, s in hist if isinstance(s, (int, float))]
            if len(sizes) < self._min_samples:
                continue
            direction = classify_drift(
                sizes,
                rise_ratio=self._rise_ratio,
                fade_max_growth_ratio=self._fade_max_growth_ratio,
                min_size=self._min_size,
            )
            if direction is None:
                continue
            if not force and self._on_cooldown(cooldowns, key, now):
                continue
            label = str(row.get("label") or "").strip()
            if not label:
                continue
            out.append(
                DriftCandidate(
                    topic=label,
                    key=key,
                    direction=direction,
                    from_size=sizes[0],
                    to_size=sizes[-1],
                )
            )
        # Strongest absolute change first (most pronounced drift).
        out.sort(key=lambda c: abs(c.to_size - c.from_size), reverse=True)
        return out

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
        if self._topic_cooldown_hours <= 0:
            return False
        last = _parse_iso(cooldowns.get(key))
        if last is None:
            return False
        elapsed_h = (now - last).total_seconds() / 3600.0
        return elapsed_h < self._topic_cooldown_hours

    def _under_daily_cap(self, now: datetime) -> bool:
        if self._daily_cap <= 0:
            return False
        today = now.astimezone().strftime("%Y-%m-%d")
        if self._kv_get_safe("interest_drift.day") != today:
            return True
        try:
            count = int(self._kv_get_safe("interest_drift.day_count") or "0")
        except (TypeError, ValueError):
            count = 0
        return count < self._daily_cap

    def _bump_daily(self, now: datetime) -> None:
        today = now.astimezone().strftime("%Y-%m-%d")
        if self._kv_get_safe("interest_drift.day") != today:
            self._kv_set_safe("interest_drift.day", today)
            self._kv_set_safe("interest_drift.day_count", "1")
            return
        try:
            count = int(self._kv_get_safe("interest_drift.day_count") or "0")
        except (TypeError, ValueError):
            count = 0
        self._kv_set_safe("interest_drift.day_count", str(count + 1))

    # ── mass time-series persistence ───────────────────────────────────

    def _load_series(self) -> dict[str, dict]:
        try:
            raw = self._kv_get(_KV_MASS_SERIES)
        except Exception:
            return {}
        if not raw:
            return {}
        try:
            blob = json.loads(raw)
        except Exception:
            return {}
        return blob if isinstance(blob, dict) else {}

    def _save_series(self, series: dict[str, dict]) -> None:
        try:
            self._kv_set(_KV_MASS_SERIES, json.dumps(series))
        except Exception:
            log.debug("interest_drift series write failed", exc_info=True)

    def _prune_series(self, series: dict[str, dict], seen: set[str]) -> None:
        # Drop topics not seen this tick whose newest sample is stale, so a
        # renamed / dissolved cluster can't keep its time-series forever.
        if not series:
            return
        stale: list[str] = []
        now = _utcnow()
        for key, row in series.items():
            if key in seen:
                continue
            hist = row.get("history") or []
            last = _parse_iso(hist[-1][0]) if hist else None
            if last is None:
                stale.append(key)
                continue
            # Stale once it's older than the full window would have been.
            horizon_h = (
                self._interval_seconds * self._window_samples / 3600.0
            ) + 1.0
            if (now - last).total_seconds() / 3600.0 > horizon_h:
                stale.append(key)
        for key in stale:
            series.pop(key, None)

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
        return (
            {str(k): str(v) for k, v in blob.items()}
            if isinstance(blob, dict)
            else {}
        )

    def _save_cooldowns(self, cooldowns: dict[str, str], now: datetime) -> None:
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
            log.debug("interest_drift cooldown write failed", exc_info=True)

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
                "interest_drift kv_set failed key=%s", key, exc_info=True,
            )


__all__ = [
    "InterestDriftWorker",
    "DriftCandidate",
    "INTEREST_DRIFT_JOURNAL_KEY",
    "classify_drift",
    "load_drifts",
    "append_drift",
    "drift_relevant",
]
