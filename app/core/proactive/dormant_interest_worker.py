"""K67 — Dormant-interest re-opener ("we haven't talked about X in ages").

The symmetric sibling of K64b
(:class:`~app.core.proactive.interest_drift_worker.InterestDriftWorker`): where
K64b notices *Aiko's own* attention shifting and K34 asks about the *user's*
upcoming plans, K67 fills the missing beat — a topic cluster that was once a
genuine, **high-mass user interest** and has since gone *silent* for a long
stretch (no new members in weeks). An established thread that quietly dropped
off ("you used to talk about your band all the time — still playing, or did
that fizzle?").

This worker is the silent producer. On an idle tick it:

  * reads each labelled topic cluster's mass + recency via
    :meth:`~app.core.conversation.topic_graph.TopicGraph.cluster_activity`
    (``(label, size, last_active, days_since)`` rows),
  * keeps only clusters that were a real interest (``size >= min_size``) AND
    have gone quiet (``days_since >= dormant_days``) — :func:`classify_dormant`,
    pure size/age math, **no LLM**,
  * skips any topic re-opened within its (long) per-topic cooldown window,
  * appends ``{at, topic, topic_key, days_since, size}`` to a small kv_meta
    journal ring (``aiko.dormant_interests``).

The consumer is
:meth:`InnerLifeProvidersMixin._render_dormant_interest_block`, which surfaces a
drafted re-opener **only on a natural conversational lull** (the K18
``TopicStagnationDetector`` standing reading), one-shot per topic, with a long
wall-clock surfacing cooldown so it stays rare and warm — never an
interrogation. It never speaks or fires a proactive nudge; the cue is a private
prompt hint phrased by the chat model itself.

No LLM: the cue is just a topic + a dormancy age, so the worker is a cheap kv
pass. Rarity is the point. Every failure path is swallowed and logged at debug
— the worst case is a missed beat, never a broken insert or a crashed tick.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

# Reuse the stable per-topic key the F10f notice worker already ships, so a
# dormant topic's identity survives cluster renumbering.
from app.core.proactive.knowledge_gap_notice_worker import topic_key

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import TopicGraph


log = logging.getLogger("app.dormant_interest_worker")


# kv_meta keys this worker owns, plus the shared journal key the surfacing
# provider reads.
DORMANT_INTEREST_JOURNAL_KEY = "aiko.dormant_interests"
_KV_TOPIC_COOLDOWNS = "dormant_interest.topic_cooldowns"


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


def classify_dormant(
    size: int,
    days_since: float | None,
    *,
    min_size: int,
    dormant_days: float,
) -> bool:
    """True when a cluster is a once-high-mass interest gone silent.

    Pure + deterministic so the geometry can be pinned in tests without a
    live graph:

      * ``size >= min_size`` — the cluster was a genuine interest, not a
        one-off mention (its accumulated members are its peak mass; a dormant
        cluster has stopped growing, so current size ≈ peak).
      * ``days_since >= dormant_days`` — its newest member is at least this
        old, i.e. the territory has gone quiet for a real stretch.

    ``days_since is None`` (no resolvable member timestamp) reads as *not*
    dormant — we never re-open a topic we can't date.
    """
    if days_since is None:
        return False
    if int(size) < int(min_size):
        return False
    return float(days_since) >= float(dormant_days)


@dataclass(frozen=True)
class DormantCandidate:
    """One once-high-mass interest that's gone quiet, + the evidence."""

    topic: str
    key: str
    days_since: float
    size: int


# ── journal helpers (shared with the surfacing provider) ────────────────


def load_dormant(
    kv_get: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Return the dormant-interest journal ring (oldest -> newest)."""
    try:
        raw = kv_get(DORMANT_INTEREST_JOURNAL_KEY)
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


def append_dormant(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    ring = load_dormant(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(DORMANT_INTEREST_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("dormant_interest journal write failed", exc_info=True)


class DormantInterestWorker:
    """IdleWorker that notices a once-loved topic the user dropped."""

    name = "dormant_interest"

    def __init__(
        self,
        *,
        topic_graph_provider: Callable[[], "TopicGraph | None"],
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        enabled_provider: Callable[[], bool] | None = None,
        interval_seconds: float = 21600.0,
        daily_cap: int = 2,
        journal_max: int = 6,
        min_size: int = 6,
        max_clusters: int = 40,
        dormant_days: float = 21.0,
        topic_cooldown_hours: float = 336.0,
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
        self._dormant_days = max(0.0, float(dormant_days))
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
            entries = graph.cluster_activity(
                top_n=self._max_clusters, min_size=self._min_size,
            )
        except Exception:
            log.debug("dormant_interest cluster_activity failed", exc_info=True)
            return {"drafted": 0, "no_graph": True}

        now = _utcnow()
        if not force and not self._under_daily_cap(now):
            return {"drafted": 0, "skipped_daily_cap": True}

        cooldowns = self._load_cooldowns()
        candidates = self._rank_candidates(
            entries, cooldowns, now, force=force,
        )
        if not candidates:
            return {"drafted": 0, "no_candidate": True}

        chosen = candidates[0]
        append_dormant(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "topic": chosen.topic[:200],
                "topic_key": chosen.key,
                "days_since": round(chosen.days_since, 1),
                "size": chosen.size,
            },
            max_entries=self._journal_max,
        )
        cooldowns[chosen.key] = now.isoformat(timespec="seconds")
        self._save_cooldowns(cooldowns, now)
        self._bump_daily(now)
        log.info(
            "dormant-interest drafted: topic=%r days=%.0f size=%d",
            chosen.topic[:80], chosen.days_since, chosen.size,
        )
        return {
            "drafted": 1,
            "topic": chosen.topic,
            "days_since": chosen.days_since,
            "size": chosen.size,
        }

    # ── MCP debug ─────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm the next ``run()`` to bypass the per-topic + daily caps."""
        self._force_next = True

    # ── candidate ranking ──────────────────────────────────────────────

    def _rank_candidates(
        self,
        entries: list[Any],
        cooldowns: dict[str, str],
        now: datetime,
        *,
        force: bool,
    ) -> list[DormantCandidate]:
        out: list[DormantCandidate] = []
        for entry in entries:
            label = (getattr(entry, "label", "") or "").strip()
            if not label:
                continue
            size = int(getattr(entry, "size", 0) or 0)
            days_since = getattr(entry, "days_since", None)
            if not classify_dormant(
                size,
                days_since,
                min_size=self._min_size,
                dormant_days=self._dormant_days,
            ):
                continue
            key = topic_key(label)
            if not force and self._on_cooldown(cooldowns, key, now):
                continue
            out.append(
                DormantCandidate(
                    topic=label,
                    key=key,
                    days_since=float(days_since),
                    size=size,
                )
            )
        # Most-dormant first — the longest-forgotten interest reads most as
        # "we haven't talked about this in ages".
        out.sort(key=lambda c: c.days_since, reverse=True)
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
        if self._kv_get_safe("dormant_interest.day") != today:
            return True
        try:
            count = int(self._kv_get_safe("dormant_interest.day_count") or "0")
        except (TypeError, ValueError):
            count = 0
        return count < self._daily_cap

    def _bump_daily(self, now: datetime) -> None:
        today = now.astimezone().strftime("%Y-%m-%d")
        if self._kv_get_safe("dormant_interest.day") != today:
            self._kv_set_safe("dormant_interest.day", today)
            self._kv_set_safe("dormant_interest.day_count", "1")
            return
        try:
            count = int(self._kv_get_safe("dormant_interest.day_count") or "0")
        except (TypeError, ValueError):
            count = 0
        self._kv_set_safe("dormant_interest.day_count", str(count + 1))

    # ── per-topic cooldown persistence ─────────────────────────────────

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
            log.debug("dormant_interest cooldown write failed", exc_info=True)

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
                "dormant_interest kv_set failed key=%s", key, exc_info=True,
            )


__all__ = [
    "DormantInterestWorker",
    "DormantCandidate",
    "DORMANT_INTEREST_JOURNAL_KEY",
    "classify_dormant",
    "load_dormant",
    "append_dormant",
]
