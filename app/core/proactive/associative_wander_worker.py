"""K64a — Associative wandering worker ("funny, this reminds me of ...").

The first member of the K64 *freedom of thought* family. Where the rest of
Aiko's interior life is reactive (extract / fact-check / consolidate / answer
the user), this is the genuinely *drifting* part: during a quiet window the
worker traverses the K9 topic graph, picks two **distant** clusters (low
centroid cosine — topics that are not neighbours), and asks the worker LLM
for a real, non-forced connection between them ("her hiking memories and her
Rust debugging both share a 'follow the trail patiently' feeling").

This worker is the silent producer. On an idle tick it:

  * reads the topic graph's labelled clusters (size + centroid + label),
  * forms candidate pairs whose centroid cosine is at or below
    ``max_pair_cosine`` (distant, not neighbours), skipping any pair on its
    per-pair cooldown,
  * pulls a few member snippets from each cluster as substance,
  * asks the worker LLM for ONE genuine connecting observation (or nothing,
    if the two really don't connect),
  * appends ``{at, topic_a, topic_b, pair_key, connection}`` to a small
    kv_meta journal ring (``aiko.associative_wanders``).

The consumer is
:meth:`InnerLifeProvidersMixin._render_associative_wander_block`, which
surfaces a drafted connection **only when the live turn is actually on one
of the two topics** (lexical overlap with the user's message) so the drift
lands in context — "oh, funny, this reminds me of ..." — rather than as a
non-sequitur. It never speaks or fires a proactive nudge; the cue is a
private prompt hint, phrased by the chat model itself, in her own words.

Rarity is the whole point (a person who keeps announcing connections is
exhausting), so the worker is paced hard: a long draft interval, a small
daily cap, and a long *per-pair* cooldown so the same two topics aren't
re-connected for a week. Every failure path is swallowed and logged at
debug — the worst case is a missed beat, never a broken insert or a crashed
tick.
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

# Reuse the cheap lexical "is the live turn on this topic?" gate the F10f
# notice worker already ships — same shape, no need for a second copy.
from app.core.proactive.knowledge_gap_notice_worker import topic_relevant

if TYPE_CHECKING:
    from app.core.conversation.topic_graph import TopicCluster, TopicGraph
    from app.core.memory.memory_store import MemoryStore
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.associative_wander_worker")


# kv_meta keys this worker owns, plus the shared journal key the surfacing
# provider reads.
ASSOCIATIVE_WANDER_JOURNAL_KEY = "aiko.associative_wanders"
_KV_PAIR_COOLDOWNS = "associative_wander.pair_cooldowns"
_KV_LAST_FIRED_AT = "associative_wander.last_fired_at"
_KV_DAY = "associative_wander.day"
_KV_DAY_COUNT = "associative_wander.day_count"

# Cap how much of any text we render in a single log line / feed the LLM.
_LOG_PREVIEW_CHARS = 120
_MEMBER_SNIPPET_CHARS = 140


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


def _preview(text: str | None) -> str:
    if not text:
        return "<empty>"
    flat = " ".join(str(text).split())
    if len(flat) > _LOG_PREVIEW_CHARS:
        return flat[: _LOG_PREVIEW_CHARS - 1] + "…"
    return flat


def pair_key(label_a: str, label_b: str) -> str:
    """Stable short hash of an unordered topic pair, robust to renumbering.

    Lower-cased, whitespace-collapsed, sorted, joined, hashed — so the same
    two topics map to the same key regardless of order or which cluster ids
    the graph happens to assign on a given rebuild.
    """
    a = " ".join((label_a or "").lower().split())
    b = " ".join((label_b or "").lower().split())
    lo, hi = sorted((a, b))
    return hashlib.sha1(f"{lo}\x1f{hi}".encode("utf-8")).hexdigest()[:16]


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
class WanderPair:
    """One candidate pair of distant clusters to connect."""

    cluster_id_a: int
    cluster_id_b: int
    label_a: str
    label_b: str
    cosine: float
    key: str


def find_distant_pairs(
    clusters: list["TopicCluster"],
    *,
    max_cosine: float,
    min_size: int,
) -> list[WanderPair]:
    """All eligible distant pairs, most-distant first.

    A pair qualifies when both clusters clear ``min_size``, both have a
    non-blank label, and their centroid cosine is at or below
    ``max_cosine`` (i.e. they are genuinely far apart in topic space — the
    interesting kind of connection, not two neighbours). Pure + sortable so
    the worker can rank candidates and the tests can pin the geometry
    without a live graph.
    """
    eligible: list["TopicCluster"] = []
    for c in clusters:
        try:
            if int(getattr(c, "size", 0)) < min_size:
                continue
        except (TypeError, ValueError):
            continue
        label = (getattr(c, "summary", "") or "").strip()
        if not label:
            continue
        if getattr(c, "centroid", None) is None:
            continue
        eligible.append(c)

    pairs: list[WanderPair] = []
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            ca, cb = eligible[i], eligible[j]
            cos = _centroid_cosine(ca.centroid, cb.centroid)
            if cos is None or cos > max_cosine:
                continue
            label_a = (ca.summary or "").strip()
            label_b = (cb.summary or "").strip()
            pairs.append(
                WanderPair(
                    cluster_id_a=int(ca.cluster_id),
                    cluster_id_b=int(cb.cluster_id),
                    label_a=label_a,
                    label_b=label_b,
                    cosine=cos,
                    key=pair_key(label_a, label_b),
                )
            )
    pairs.sort(key=lambda p: p.cosine)
    return pairs


# ── journal helpers (shared with the surfacing provider) ────────────────


def load_wanders(
    kv_get: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Return the associative-wander journal ring (oldest -> newest)."""
    try:
        raw = kv_get(ASSOCIATIVE_WANDER_JOURNAL_KEY)
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


def append_wander(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    ring = load_wanders(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(ASSOCIATIVE_WANDER_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("associative_wander journal write failed", exc_info=True)


def wander_relevant(entry: dict[str, Any], user_text: str) -> bool:
    """True when the live turn is on either topic of a drafted connection."""
    a = str(entry.get("topic_a") or "")
    b = str(entry.get("topic_b") or "")
    return topic_relevant(a, user_text) or topic_relevant(b, user_text)


class AssociativeWanderWorker:
    """IdleWorker that drafts genuine connections between distant topics."""

    name = "associative_wander"

    def __init__(
        self,
        *,
        topic_graph_provider: Callable[[], "TopicGraph | None"],
        memory_store: "MemoryStore",
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        enabled_provider: Callable[[], bool] | None = None,
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        interval_seconds: float = 5400.0,
        cooldown_seconds: float = 7200.0,
        daily_cap: int = 2,
        journal_max: int = 6,
        min_size: int = 4,
        max_pair_cosine: float = 0.25,
        pair_cooldown_hours: float = 168.0,
        member_samples: int = 3,
        rng: random.Random | None = None,
    ) -> None:
        self._topic_graph_provider = topic_graph_provider
        self._memory_store = memory_store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._enabled_provider = enabled_provider
        self._ollama = ollama
        self._model = model
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._daily_cap = max(0, int(daily_cap))
        self._journal_max = max(1, int(journal_max))
        self._min_size = max(2, int(min_size))
        self._max_pair_cosine = float(max_pair_cosine)
        self._pair_cooldown_hours = max(0.0, float(pair_cooldown_hours))
        self._member_samples = max(0, int(member_samples))
        self._rng = rng or random.Random()
        # MCP debug: arm the next run() to bypass the per-pair cooldown +
        # the global cooldown / daily cap gates.
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
        if not force and not self._cooldown_elapsed(now):
            return {"drafted": 0, "skipped_cooldown": True}
        if not force and not self._under_daily_cap(now):
            return {"drafted": 0, "skipped_daily_cap": True}

        graph = self._safe_graph()
        if graph is None:
            return {"drafted": 0, "no_graph": True}
        try:
            clusters = graph.topic_clusters()
        except Exception:
            log.debug("associative_wander topic_clusters failed", exc_info=True)
            return {"drafted": 0, "no_graph": True}

        pairs = find_distant_pairs(
            clusters, max_cosine=self._max_pair_cosine, min_size=self._min_size,
        )
        if not pairs:
            return {"drafted": 0, "no_pair": True}

        cooldowns = self._load_cooldowns()
        chosen = self._choose_pair(pairs, cooldowns, now, force=force)
        if chosen is None:
            return {"drafted": 0, "all_on_cooldown": True}

        members_a = self._member_snippets(graph, chosen.cluster_id_a)
        members_b = self._member_snippets(graph, chosen.cluster_id_b)
        connection = self._compose_connection(chosen, members_a, members_b)
        if not connection:
            # Mark the pair on cooldown anyway so a genuinely unconnectable
            # pair isn't retried every tick.
            self._stamp_pair(cooldowns, chosen.key, now)
            log.info(
                "associative-wander no-connection: a=%r b=%r cos=%.3f",
                _preview(chosen.label_a), _preview(chosen.label_b),
                chosen.cosine,
            )
            return {"drafted": 0, "no_connection": True}

        append_wander(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "topic_a": chosen.label_a[:200],
                "topic_b": chosen.label_b[:200],
                "pair_key": chosen.key,
                "connection": connection,
            },
            max_entries=self._journal_max,
        )
        self._stamp_pair(cooldowns, chosen.key, now)
        self._mark_fired(now)
        log.info(
            "associative-wander drafted: a=%r b=%r cos=%.3f connection=%r",
            _preview(chosen.label_a), _preview(chosen.label_b),
            chosen.cosine, _preview(connection),
        )
        return {
            "drafted": 1,
            "topic_a": chosen.label_a,
            "topic_b": chosen.label_b,
            "pair_key": chosen.key,
            "cosine": round(chosen.cosine, 3),
            "connection": connection,
        }

    # ── MCP debug ─────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm the next ``run()`` to bypass cooldown + daily cap + per-pair."""
        self._force_next = True

    # ── pair selection ────────────────────────────────────────────────

    def _choose_pair(
        self,
        pairs: list[WanderPair],
        cooldowns: dict[str, str],
        now: datetime,
        *,
        force: bool,
    ) -> WanderPair | None:
        """Pick a distant pair to connect.

        When forced, take the single most-distant pair (deterministic, ready
        for an end-to-end repro). Otherwise keep only pairs off cooldown and
        pick randomly among the most-distant handful so the drift has variety
        instead of always grabbing the same extreme pair.
        """
        if force:
            return pairs[0]
        available = [
            p for p in pairs if not self._pair_on_cooldown(cooldowns, p.key, now)
        ]
        if not available:
            return None
        # ``pairs`` is sorted most-distant first; sample from the closest of
        # the distant band so the connection is far but not gibberish-far.
        top = available[: max(1, min(len(available), 5))]
        return self._rng.choice(top)

    def _member_snippets(
        self, graph: "TopicGraph", cluster_id: int,
    ) -> list[str]:
        """Up to ``member_samples`` member content snippets for a cluster."""
        if self._member_samples <= 0:
            return []
        try:
            member_ids = graph.cluster_member_ids(cluster_id)
        except Exception:
            return []
        out: list[str] = []
        for mid in member_ids[: self._member_samples]:
            try:
                mem = self._memory_store.get(int(mid))
            except Exception:
                mem = None
            text = (getattr(mem, "content", "") or "").strip() if mem else ""
            if not text:
                continue
            if len(text) > _MEMBER_SNIPPET_CHARS:
                text = text[: _MEMBER_SNIPPET_CHARS - 1].rsplit(" ", 1)[0] + "…"
            out.append(text)
        return out

    # ── connection composition (worker LLM) ───────────────────────────

    def _compose_connection(
        self,
        pair: WanderPair,
        members_a: list[str],
        members_b: list[str],
    ) -> str:
        """Ask the worker LLM for one genuine connection, or ``""``.

        Returns an empty string on any failure / when the model judges the
        two topics don't really connect — never a forced or generic line.
        """
        if self._ollama is None or not self._model:
            return ""
        notes_a = "; ".join(members_a) or "(no extra notes)"
        notes_b = "; ".join(members_b) or "(no extra notes)"
        prompt = (
            "You are Aiko, letting your mind wander between two unrelated "
            "things you've been thinking about.\n"
            f"TOPIC A: {pair.label_a}\n"
            f"  notes: {notes_a}\n"
            f"TOPIC B: {pair.label_b}\n"
            f"  notes: {notes_b}\n\n"
            "Is there a GENUINE, non-obvious connection between them — a "
            "shared feeling, rhythm, shape, or principle that actually rings "
            "true (e.g. 'both reward following a faint trail patiently')? Do "
            "NOT force it. If they don't really connect, say so."
        )
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "Reply with JSON only: "
                            '{"connects": <bool>, "connection": "<one short '
                            "first-person observation linking the two, <= 200 "
                            'chars, no preamble>"}. Set connects=false and '
                            'connection="" when there is no honest connection.'
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0.85, "num_predict": 120},
                format_json=True,
                surface="associative_wander",
            )
        except Exception:
            log.debug("associative_wander LLM compose failed", exc_info=True)
            return ""
        try:
            blob = json.loads(content or "{}")
        except Exception:
            return ""
        if not isinstance(blob, dict):
            return ""
        if blob.get("connects") is False:
            return ""
        line = str(blob.get("connection") or "").strip()
        if len(line) > 240:
            line = line[:237].rsplit(" ", 1)[0] + "…"
        return line

    # ── gates / cooldowns ──────────────────────────────────────────────

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

    def _cooldown_elapsed(self, now: datetime) -> bool:
        if self._cooldown_seconds <= 0:
            return True
        last = _parse_iso(self._kv_get_safe(_KV_LAST_FIRED_AT))
        if last is None:
            return True
        return (now - last).total_seconds() >= self._cooldown_seconds

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

    def _mark_fired(self, now: datetime) -> None:
        self._kv_set_safe(_KV_LAST_FIRED_AT, now.isoformat(timespec="seconds"))
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

    def _pair_on_cooldown(
        self, cooldowns: dict[str, str], key: str, now: datetime,
    ) -> bool:
        if self._pair_cooldown_hours <= 0:
            return False
        last = _parse_iso(cooldowns.get(key))
        if last is None:
            return False
        return (now - last).total_seconds() / 3600.0 < self._pair_cooldown_hours

    def _stamp_pair(
        self, cooldowns: dict[str, str], key: str, now: datetime,
    ) -> None:
        cooldowns[key] = now.isoformat(timespec="seconds")
        # Prune entries past 2x the cooldown so the map can't grow unbounded.
        if self._pair_cooldown_hours > 0:
            horizon = self._pair_cooldown_hours * 2.0
            pruned: dict[str, str] = {}
            for k, v in cooldowns.items():
                last = _parse_iso(v)
                if last is None:
                    continue
                if (now - last).total_seconds() / 3600.0 <= horizon:
                    pruned[k] = v
            cooldowns = pruned
        try:
            self._kv_set(_KV_PAIR_COOLDOWNS, json.dumps(cooldowns))
        except Exception:
            log.debug("associative_wander cooldown write failed", exc_info=True)

    def _load_cooldowns(self) -> dict[str, str]:
        try:
            raw = self._kv_get(_KV_PAIR_COOLDOWNS)
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
                "associative_wander kv_set failed key=%s", key, exc_info=True,
            )


__all__ = [
    "AssociativeWanderWorker",
    "WanderPair",
    "ASSOCIATIVE_WANDER_JOURNAL_KEY",
    "load_wanders",
    "append_wander",
    "wander_relevant",
    "find_distant_pairs",
    "pair_key",
]
