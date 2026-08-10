"""K64d — Knowledge-map self-reflection ("the shape of what I know").

Final member of the K64 *freedom of thought* family. K64a/b/c each notice
something *local* about the topic graph (a connection, a drifting topic, an
under-explored edge). K64d steps back and looks at the **whole shape**: which
territories of Aiko's mind are rich and well-trodden, which are thin or
blank. It's the lowest-frequency, most introspective member — a periodic
"huh, I realise most of what I hold is about X, and I've barely got anything
on Y" meta-thought.

Unlike a/b/c (which are cue producers surfaced one-shot through a dedicated
inner-life block), K64d **reuses the existing reflection machinery**: it runs
a worker-LLM pass seeded by the graph's *shape* (rich clusters + under-
explored ones) instead of raw recent memories, and writes ONE
``kind="reflection"`` memory prefixed ``[mindmap]``. That memory then flows
through the same paths every other reflection does — the RAG retriever, the
K28 ``turning_over`` between-session surfacing, the NarrativeWeaver — so
there's no new prompt-assembler wiring, no new provider, and the meta-thought
surfaces naturally in Aiko's own words when it's relevant.

Paced hard: a ~daily interval plus a wall-clock cooldown, so the map-shape
reflection is a rare, considered beat rather than a recurring announcement.
Every failure path is swallowed and logged at debug — the worst case is a
missed beat, never a broken insert or a crashed tick.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.concepts.concept_diets import diet_for
from app.core.proactive.idle_worker import WorkSignal
from app.core.infra import timephrase

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.core.concepts.concept_view import ConceptView
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.llm.chat_client import ChatClient
    from app.llm.embedder import Embedder


log = logging.getLogger("app.knowledge_map_reflection_worker")


# Content prefix that marks a map-shape reflection, mirroring DreamWorker's
# ``[dream] `` discriminator. Round-trips cleanly through SQLite + the
# reflection-kind machinery without a schema bump.
MINDMAP_PREFIX = "[mindmap] "

_KV_LAST_FIRED_AT = "knowledge_map_reflection.last_fired_at"


@dataclass(slots=True)
class RichTerritory:
    """One well-trodden region of the map, as the reflection sees it.

    ``label`` / ``size`` / ``days_since`` come from the topic graph (how
    much, how recently); ``concepts`` is the L28 annotation -- what Aiko
    *believes* about that territory, read through
    :meth:`ConceptView.for_cluster` off ``representative_id`` and scoped to
    the ``knowledge_map_reflection`` diet. Empty when
    the concept layer is off, cold, or has nothing edged to the cluster,
    which is the normal early state and renders exactly as before.
    """

    label: str
    size: int
    days_since: float | None = None
    representative_id: int = 0
    concepts: list[str] = field(default_factory=list)


def _utcnow() -> datetime:
    return timephrase.utcnow()


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


def _build_system_prompt() -> str:
    return (
        "You are Aiko in a quiet, introspective stretch — not talking to "
        "anyone, just looking inward at the shape of what's been on your "
        "mind lately: which subjects you hold a lot about, and which ones "
        "you've barely got anything on.\n"
        "\n"
        "Compose ONE short reflection (<= 40 words, first person, a plain "
        "sentence or two) about the *shape* of what you know — a real "
        "noticing, e.g. 'most of what I'm carrying lately circles X, and I "
        "realise I've got almost nothing on Y even though it keeps brushing "
        "past.' Each topic notes how recently it's been active — feel free "
        "to notice when a territory has gone quiet for a while, or when one "
        "is suddenly hot. NOT a greeting, NOT a question, NOT a to-do — just "
        "a private note to yourself.\n"
        "\n"
        # K-time10: this lands in ``memories`` as a "[mindmap] "
        # reflection and gets recalled weeks later, when "lately" no
        # longer means what it meant at writing time.
        + timephrase.STORED_TEXT_TIME_RULE
        + "\n"
        "\n"
        "Output ONLY the sentence(s). No quotes, no JSON, no preamble."
    )


def recency_phrase(days_since: float | None) -> str:
    """Bucket a cluster's days-since-last-touch into a short recency tag.

    K-time9 follow-up: lets the reflection seed distinguish "recently hot"
    territory from one that "went quiet months ago". Empty string when no
    timestamp resolved (the LLM then just sees size, as before).
    """
    if days_since is None:
        return ""
    if days_since <= 7:
        return "hot this week"
    if days_since <= 30:
        return "active recently"
    if days_since <= 90:
        return "cooled off, weeks since"
    if days_since <= 180:
        return "quiet for a couple months"
    return "gone quiet, months since"


def clean_reflection_output(raw: str) -> str:
    """Best-effort clean of the LLM's one-line reflection."""
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = text.strip("`").strip()
        if "\n" in text:
            head, _, body = text.partition("\n")
            if len(head) <= 12 and head.strip().isalpha():
                text = body.strip()
    text = text.strip("\"'` \t\n")
    if len(text) > 320:
        text = text[:320].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


class KnowledgeMapReflectionWorker:
    """IdleWorker that reflects on the overall shape of Aiko's knowledge."""

    name = "knowledge_map_reflection"

    def __init__(
        self,
        *,
        topic_graph_provider: Callable[[], "TopicGraph | None"],
        memory_store: "MemoryStore",
        embedder: "Embedder | None",
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        enabled_provider: Callable[[], bool] | None = None,
        notify_memory_added: Callable[["Memory"], None] | None = None,
        view_provider: Callable[[], "ConceptView | None"] | None = None,
        interval_seconds: float = 86400.0,
        cooldown_hours: float = 20.0,
        min_clusters: int = 4,
        rich_top_n: int = 5,
        gap_top_n: int = 3,
        concepts_per_cluster: int = 2,
        max_tokens: int = 120,
        salience: float = 0.5,
    ) -> None:
        self._topic_graph_provider = topic_graph_provider
        # L28: late-bound ConceptView so each territory can carry what Aiko
        # actually *believes* about it, not just how big and how recent it
        # is. ``None`` (or a provider returning a cold view) leaves the
        # payload exactly as it was.
        self._view_provider = view_provider
        self._memory_store = memory_store
        self._embedder = embedder
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._ollama = ollama
        self._model = model
        self._enabled_provider = enabled_provider
        self._notify_memory_added = notify_memory_added
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._cooldown_hours = max(0.0, float(cooldown_hours))
        self._min_clusters = max(2, int(min_clusters))
        self._rich_top_n = max(1, int(rich_top_n))
        self._gap_top_n = max(0, int(gap_top_n))
        self._concepts_per_cluster = max(0, int(concepts_per_cluster))
        self._max_tokens = max(40, int(max_tokens))
        self._salience = max(0.0, min(1.0, float(salience)))
        # MCP debug: force the next run() to bypass the wall-clock cooldown.
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
        """Enabled, wired, and past the 20-hour cooldown.

        The cooldown is a hard veto: a run inside it returns
        ``skipped_cooldown`` without touching the graph, so admitting
        one achieves nothing the next admission would not.
        """
        return self._blocker(now) is None

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """One reflection per cooldown window, so a flat signal.

        Deliberately does **not** check whether the graph is rich
        enough. That answer lives in ``_read_shape``, which walks
        cluster activity and annotates concepts — run-shaped work, not
        probe-shaped, and this worker's 2.4-hour anti-thrash floor
        already bounds how often a thin graph gets re-examined.
        """
        blocker = self._blocker(now)
        if blocker is not None:
            return WorkSignal(pressure=0.0, reason=blocker)
        return WorkSignal(
            pressure=1.0 if self._force_next else 0.5,
            reason="forced" if self._force_next else "reflection due",
            needs_llm=True,
        )

    def _blocker(
        self, now: datetime, *, force: bool | None = None,
    ) -> str | None:
        """First reason a run would do nothing, or ``None``.

        ``force=None`` peeks at the one-shot instead of consuming it.
        """
        if force is None:
            force = self._force_next
        if not self._enabled():
            return "disabled"
        if self._ollama is None or not self._model:
            return "no_llm"
        if self._embedder is None:
            return "no_embedder"
        if not force and not self._cooldown_elapsed(now):
            return "skipped_cooldown"
        return None

    def run(self) -> dict[str, Any]:
        force = self._force_next
        self._force_next = False
        now = _utcnow()
        blocker = self._blocker(now, force=force)
        if blocker is not None:
            return {"wrote": 0, blocker: True}

        graph = self._safe_graph()
        if graph is None:
            return {"wrote": 0, "no_graph": True}

        rich, gaps = self._read_shape(graph)
        if len(rich) < self._min_clusters:
            return {"wrote": 0, "no_context": True, "rich": len(rich)}

        reflection = self._compose(rich, gaps)
        if not reflection:
            return {"wrote": 0, "no_reflection": True}

        content = MINDMAP_PREFIX + reflection
        try:
            embedding = self._embedder.embed(content)
        except Exception:
            log.debug("knowledge_map_reflection embed failed", exc_info=True)
            return {"wrote": 0, "embed_failed": True}
        try:
            memory = self._memory_store.add(
                content=content,
                kind="reflection",
                embedding=embedding,
                salience=self._salience,
                tier="scratchpad",
                metadata={
                    "source": "knowledge_map",
                    "reflected_at": now.isoformat(timespec="seconds"),
                },
            )
        except Exception:
            log.debug("knowledge_map_reflection write failed", exc_info=True)
            return {"wrote": 0, "write_failed": True}

        # Stamp the cooldown regardless of dedupe so a near-identical
        # reflection doesn't get re-attempted every tick.
        self._mark_fired(now)

        if memory is None:
            log.info("knowledge-map-reflection deduped against existing memory")
            return {"wrote": 0, "deduped": True, "reflection": reflection}

        if self._notify_memory_added is not None:
            try:
                self._notify_memory_added(memory)
            except Exception:
                log.debug(
                    "knowledge_map_reflection notify added failed",
                    exc_info=True,
                )
        log.info(
            "knowledge-map-reflection wrote memory id=%s rich=%d gaps=%d: %r",
            getattr(memory, "id", "?"), len(rich), len(gaps),
            reflection[:120],
        )
        return {
            "wrote": 1,
            "memory_id": int(getattr(memory, "id", 0) or 0),
            "reflection": reflection,
            "rich": len(rich),
            "gaps": len(gaps),
        }

    # ── MCP debug ─────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm the next ``run()`` to bypass the wall-clock cooldown."""
        self._force_next = True

    # ── shape reading ──────────────────────────────────────────────────

    def _read_shape(
        self, graph: "TopicGraph",
    ) -> tuple[list["RichTerritory"], list[tuple[str, int]]]:
        """Return ``(rich, gaps)``.

        ``rich`` = the largest labelled clusters (well-trodden territory),
        each carrying how long since that territory was last active so the
        reflection can read "recently hot vs. went quiet months ago", plus
        the concepts Aiko holds about it (L28);
        ``gaps`` = dense-but-under-researched clusters (familiar in
        conversation, blank in *learned* knowledge). Both tolerate the
        non-persistent / in-memory graph mode (empty). Falls back to the
        recency-free ``interest_map`` if ``cluster_activity`` is unavailable
        (older graph / duck-typed stub).
        """
        rich: list[RichTerritory] = []
        top_n = max(self._rich_top_n, self._min_clusters)
        try:
            activity = getattr(graph, "cluster_activity", None)
            if callable(activity):
                for e in activity(top_n=top_n):
                    label = (getattr(e, "label", "") or "").strip()
                    if label:
                        rich.append(RichTerritory(
                            label=label,
                            size=int(getattr(e, "size", 0) or 0),
                            days_since=getattr(e, "days_since", None),
                            representative_id=int(
                                getattr(e, "representative_id", 0) or 0
                            ),
                        ))
            else:
                for e in graph.interest_map(top_n=top_n):
                    label = (getattr(e, "label", "") or "").strip()
                    if label:
                        rich.append(RichTerritory(
                            label=label,
                            size=int(getattr(e, "size", 0) or 0),
                        ))
        except Exception:
            log.debug("knowledge_map_reflection interest_map failed", exc_info=True)
        self._annotate_concepts(rich)

        gaps: list[tuple[str, int]] = []
        if self._gap_top_n > 0:
            try:
                for g in graph.knowledge_gap_clusters(top_n=self._gap_top_n):
                    label = (getattr(g, "label", "") or "").strip()
                    if label:
                        gaps.append((label, int(getattr(g, "size", 0) or 0)))
            except Exception:
                log.debug(
                    "knowledge_map_reflection knowledge_gap_clusters failed",
                    exc_info=True,
                )
        return rich, gaps

    def _annotate_concepts(self, rich: list["RichTerritory"]) -> None:
        """L28: hang each territory's concepts off it, in place.

        The map says *how much* and *how recently*; the concept layer says
        *what he actually believes about it*. Reading them through
        ``ConceptView.for_cluster`` rather than re-deriving here is the
        point of the contract -- one read path, so this worker can't drift
        from what the rest of the system thinks the graph means.

        Scoped to the ``knowledge_map_reflection`` diet, and spread across
        kinds before it goes deep into one. With only two slots per
        territory, straight confidence ordering reliably spent both on the
        same kind -- and because the stakes priors run highest on
        ``boundary`` and ``value``, a reflection on the whole shape of the
        map arrived as two rails per region. One taste beside one
        conviction is a truer summary of a territory than the two firmest
        things she holds about it.

        Silent no-op when concepts are off, cold, or simply have nothing
        edged to a given cluster, which is the common case early on.
        """
        if self._concepts_per_cluster <= 0 or self._view_provider is None:
            return
        try:
            view = self._view_provider()
        except Exception:
            log.debug("knowledge_map_reflection concept view failed", exc_info=True)
            return
        if view is None or not getattr(view, "enabled", False):
            return
        diet = diet_for("knowledge_map_reflection")
        kinds = diet.kinds if diet is not None else None
        for row in rich:
            if not row.representative_id:
                continue
            try:
                concepts = view.for_cluster(row.representative_id, kinds=kinds)
            except Exception:
                log.debug(
                    "knowledge_map_reflection for_cluster failed", exc_info=True
                )
                continue
            row.concepts = self._territory_labels(concepts)

    def _territory_labels(self, concepts: "Sequence[object]") -> list[str]:
        """Up to ``concepts_per_cluster`` labels, one kind at a time.

        Most-confident within a kind, then round the kinds in order of
        their leading concept -- so the first slot still goes to the
        firmest thing she holds about the territory and only the *second*
        one is forced to say something new.
        """
        buckets: dict[str, list[object]] = defaultdict(list)
        for concept in concepts:
            label = " ".join(str(getattr(concept, "label", "")).split())
            if label:
                buckets[str(getattr(concept, "kind", ""))].append(concept)
        for rows in buckets.values():
            rows.sort(
                key=lambda c: float(getattr(c, "confidence", 0.0) or 0.0),
                reverse=True,
            )

        def _lead(kind: str) -> float:
            return float(getattr(buckets[kind][0], "confidence", 0.0) or 0.0)

        order = sorted(buckets, key=lambda k: (-_lead(k), k))
        labels: list[str] = []
        depth = 0
        while len(labels) < self._concepts_per_cluster:
            drew = False
            for kind in order:
                rows = buckets[kind]
                if depth >= len(rows):
                    continue
                labels.append(
                    " ".join(str(getattr(rows[depth], "label", "")).split())
                )
                drew = True
                if len(labels) >= self._concepts_per_cluster:
                    break
            if not drew:
                break
            depth += 1
        return labels

    def _compose(
        self,
        rich: list["RichTerritory"],
        gaps: list[tuple[str, int]],
    ) -> str:
        rich_lines = "\n".join(
            self._rich_line(row) for row in rich[: self._rich_top_n]
        )
        payload = [
            "The richest territories of what you hold "
            "(topic — how much — how recently active):",
            rich_lines or "  (nothing substantial yet)",
        ]
        if gaps:
            gap_lines = "\n".join(
                f"  - {label}" for label, _ in gaps
            )
            payload.append(
                "Topics that keep coming up but you've barely actually "
                "learned about:\n" + gap_lines
            )
        user_payload = "\n\n".join(payload)
        try:
            raw = self._ollama.chat(
                [
                    {
                        "role": "system",
                        "content": (
                            timephrase.today_anchor()
                            + "\n\n"
                            + _build_system_prompt()
                        ),
                    },
                    {"role": "user", "content": user_payload},
                ],
                options={"temperature": 0.6, "num_predict": self._max_tokens},
                model=self._model,
                surface="knowledge_map_reflection",
            )
        except Exception:
            log.debug("knowledge_map_reflection LLM call failed", exc_info=True)
            return ""
        return clean_reflection_output(raw)

    @staticmethod
    def _rich_line(row: "RichTerritory") -> str:
        phrase = recency_phrase(row.days_since)
        facts = f"{row.size} memories, {phrase}" if phrase else f"{row.size} memories"
        line = f"  - {row.label} ({facts})"
        if row.concepts:
            line += " — you believe: " + "; ".join(row.concepts)
        return line

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

    def _cooldown_elapsed(self, now: datetime) -> bool:
        if self._cooldown_hours <= 0:
            return True
        last = _parse_iso(self._kv_get_safe(_KV_LAST_FIRED_AT))
        if last is None:
            return True
        return (now - last).total_seconds() / 3600.0 >= self._cooldown_hours

    def _mark_fired(self, now: datetime) -> None:
        self._kv_set_safe(_KV_LAST_FIRED_AT, now.isoformat(timespec="seconds"))

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
                "knowledge_map_reflection kv_set failed key=%s", key,
                exc_info=True,
            )


__all__ = [
    "KnowledgeMapReflectionWorker",
    "MINDMAP_PREFIX",
    "clean_reflection_output",
    "recency_phrase",
]
