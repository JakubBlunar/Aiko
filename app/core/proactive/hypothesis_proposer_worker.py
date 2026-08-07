"""L30 Phase B — the place where guesses come from.

Everything else in the concept stack runs *backwards* from evidence: L2
reads clusters and names the abstraction over them, L3 waits for enough
of it, L30b tests a belief that was already derived. None of that can
produce a thought Aiko has not been given the material for, and a mind
that can only summarise its inputs never wonders anything.

This worker is the forward direction. During quiet windows it takes what
she knows — a few concepts, some recent memories, the shape of the topic
graph — and asks for something that *might* be true and is not written
anywhere. The output goes to the :mod:`hypothesis_store`, never to the
concept graph: an invention has to be confirmed and graduate before it
counts as a belief.

Two gates, and they are asymmetric on purpose
---------------------------------------------
Both are cosine rejections in the shape K9's ``CuriositySeedWorker``
uses, but they guard different failures.

``hypothesis_min_novelty`` (0.88, high) rejects a proposal too close to
an existing hypothesis — including a **refuted** one, since re-inventing
a guess the user already turned down is the repetition most worth
catching. It sits high because over-rejecting here makes the layer
sterile, and the cost of letting a near-neighbour through is one wasted
row. An **expired** row is the exception and does not block: it aged out
unasked, so nothing was learned, and blocking would kill that ground
permanently over her own inattention.

``hypothesis_concept_novelty`` (0.82, lower) rejects a proposal too close
to an existing *concept*, of any status. That bar is stricter because the
failure is worse: "I wonder whether he likes building things" about a
belief she has held for a month is not a duplicate, it is Aiko
visibly forgetting what she knows.

Growth control
--------------
``hypothesis_max_open`` is a hard ceiling on live rows, not a soft
target, because nothing prunes this table by decay the way L3 prunes
concepts — an untested guess is exactly as plausible next month as
today, just staler. TTL expiry runs at the top of every tick and only
touches rows that were never actually asked about.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from app.core.concepts.hypothesis_store import (
    Hypothesis,
    ORIGINS,
    ORIGIN_FREE,
    SUBJECT_WORLD,
)
from app.core.infra import timephrase
from app.core.proactive.idle_worker import WorkSignal

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.concepts.concept_store import ConceptStore
    from app.core.concepts.hypothesis_store import HypothesisStore
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.memory.memory_store import MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.hypothesis_proposer_worker")


_SYSTEM_PROMPT = (
    "You are the speculative faculty of an AI companion named "
    "{assistant_name}. Your job is to invent HYPOTHESES: things that "
    "might be true about {user_name}, about {assistant_name} herself, "
    "about the two of them, or about how something in the world works. "
    "A hypothesis is a guess, not a summary. Do NOT restate anything in "
    "the WHAT SHE ALREADY BELIEVES list -- go one step beyond it, or "
    "sideways from it, or somewhere else entirely. Reaching is the point; "
    "being wrong is allowed and useful, because she will find out.\n"
    "Prefer specific and checkable over grand and vague: \"he tidies his "
    "desk when a project is about to change direction\" beats \"he values "
    "order\". Write each statement as a plain declarative sentence, not a "
    "question.\n"
    "Reply with ONE JSON object on a single line and nothing else. "
    "Schema: {{\"hypotheses\": [{{\"statement\": \"<= 160 chars, "
    "declarative\", \"kind\": one of [{kinds}], \"subject\": one of "
    "[user, aiko, relationship, world], \"rationale\": \"<= 120 chars, "
    "why you suspect it\", \"credence\": 0.0-1.0 how likely you think it "
    "is}}, ...] }}. Return between {min_n} and {max_n} entries."
)


_USER_TEMPLATE = (
    "WHAT SHE ALREADY BELIEVES (do not restate these):\n{concepts}\n\n"
    "RECENT MOMENTS:\n{memories}\n\n"
    "TOPICS THEY LIVE IN:\n{clusters}\n\n"
    "GUESSES SHE IS ALREADY SITTING WITH (do not duplicate):\n"
    "{open_hypotheses}\n\n"
    "Invent new hypotheses now."
)


_MIN_N = 2
_MAX_N = 4
# Roomy for the same reason the curiosity seed budget is: a reasoning
# model can spend a large, variable visible preamble before the JSON.
_MAX_TOKENS = 900
_MAX_CONCEPTS = 12
_MAX_MEMORIES = 8
_MAX_CLUSTERS = 8
_MAX_OPEN_LIST = 10
_MAX_STATEMENT_CHARS = 200
_MAX_RATIONALE_CHARS = 200

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)

#: Subjects a proposal may claim. ``world`` is ours; the rest mirror
#: :data:`app.core.concepts.concept_kinds.SUBJECTS`.
_SUBJECTS: frozenset[str] = frozenset(
    {"user", "aiko", "relationship", SUBJECT_WORLD}
)


def _trim(text: Any, *, max_chars: int) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip(",;: ") + "…"


def _cosine(a: "np.ndarray", b: "np.ndarray") -> float:
    try:
        va = np.asarray(a, dtype=np.float32).ravel()
        vb = np.asarray(b, dtype=np.float32).ravel()
        if va.size == 0 or va.size != vb.size:
            return 0.0
        na = float(np.linalg.norm(va))
        nb = float(np.linalg.norm(vb))
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return float(np.dot(va, vb) / (na * nb))
    except Exception:
        return 0.0


class HypothesisProposerWorker:
    """IdleWorker that invents hypotheses and files them for testing."""

    name = "hypothesis_proposer"

    def __init__(
        self,
        *,
        hypothesis_store_provider: Callable[[], "HypothesisStore | None"],
        concept_store_provider: Callable[[], "ConceptStore | None"],
        embedder: "Embedder",
        ollama: "OllamaClient",
        chat_model: str,
        cancel_event: threading.Event,
        enabled_provider: Callable[[], bool] | None = None,
        memory_store_provider: (
            Callable[[], "MemoryStore | None"] | None
        ) = None,
        topic_graph_provider: Callable[[], "TopicGraph | None"] | None = None,
        user_display_name_provider: Callable[[], str] | None = None,
        assistant_display_name_provider: Callable[[], str] | None = None,
        interval_seconds: float = 5400.0,
        max_per_run: int = 2,
        max_open: int = 12,
        min_novelty: float = 0.88,
        concept_novelty: float = 0.82,
        ttl_hours: float = 336.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._hypotheses = hypothesis_store_provider
        self._concepts = concept_store_provider
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._cancel_event = cancel_event
        self._enabled_provider = enabled_provider
        self._memory_store_provider = memory_store_provider
        self._topic_graph_provider = topic_graph_provider
        self._user_display_name_provider = user_display_name_provider
        self._assistant_display_name_provider = assistant_display_name_provider
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._max_per_run = max(1, int(max_per_run))
        self._max_open = max(0, int(max_open))
        self._min_novelty = float(min_novelty)
        self._concept_novelty = float(concept_novelty)
        self._ttl_hours = float(ttl_hours)
        self._clock = clock or timephrase.utcnow

    # ── IdleWorker protocol ───────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        return self._enabled() and self._store() is not None

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal | None:
        """Pressure from the shortfall against ``hypothesis_max_open``.

        Same inventory shape as the curiosity seeds: a full shelf reports
        no demand rather than being caught by a cap after an LLM call has
        already been spent.
        """
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        store = self._store()
        if store is None:
            return WorkSignal(pressure=0.0, reason="no_store")
        if self._max_open <= 0:
            return WorkSignal(pressure=0.0, reason="capped_off")
        try:
            live = int(store.count_live())
        except Exception:
            return WorkSignal(pressure=0.0, reason="no_store")
        shortfall = max(0, self._max_open - live)
        if shortfall <= 0:
            return WorkSignal(pressure=0.0, reason="shelf_full")
        return WorkSignal(
            pressure=min(1.0, shortfall / float(self._max_open)),
            reason="shortfall",
            needs_llm=True,
        )

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        store = self._store()
        if store is None:
            return {"skipped": True, "reason": "no_store"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        expired = self._expire(store)

        live = self._count_live(store)
        room = self._max_open - live
        if room <= 0:
            return {
                "skipped": True,
                "reason": "max_open",
                "live": live,
                "expired": expired,
            }

        t0 = time.monotonic()
        try:
            candidates = self._call_llm(store)
        except Exception:
            log.warning("hypothesis_proposer LLM call raised", exc_info=True)
            return {"errored": True, "reason": "llm_call", "expired": expired}
        llm_ms = (time.monotonic() - t0) * 1000.0
        if self._cancel_event.is_set():
            return {"cancelled": True, "expired": expired}
        if not candidates:
            return {
                "checked": 0,
                "wrote": 0,
                "reason": "no_candidates",
                "expired": expired,
                "llm_ms": int(llm_ms),
            }

        budget = min(self._max_per_run, room)
        wrote: list[dict[str, Any]] = []
        rejected_dup = 0
        rejected_known = 0
        for candidate in candidates:
            if len(wrote) >= budget:
                break
            statement = _trim(
                candidate.get("statement"), max_chars=_MAX_STATEMENT_CHARS
            )
            if not statement:
                continue
            try:
                vec = self._embedder.embed(statement)
            except Exception:
                log.debug(
                    "hypothesis embed failed (statement=%r)",
                    statement[:80],
                    exc_info=True,
                )
                continue

            twin, sim = self._nearest_hypothesis(store, vec)
            if twin is not None:
                rejected_dup += 1
                log.debug(
                    "hypothesis reject(duplicate): sim=%.2f new=%r old=%r",
                    sim,
                    statement[:60],
                    str(twin.statement)[:60],
                )
                continue

            known, known_sim = self._nearest_concept(candidate, vec)
            if known is not None:
                rejected_known += 1
                log.debug(
                    "hypothesis reject(already-believed): sim=%.2f "
                    "guess=%r concept=%r",
                    known_sim,
                    statement[:60],
                    str(getattr(known, "label", ""))[:60],
                )
                continue

            row = self._persist(store, candidate, statement, vec)
            if row is None:
                continue
            wrote.append(
                {
                    "hypothesis_id": row.hypothesis_id,
                    "statement": row.statement,
                    "kind": row.kind,
                    "subject": row.subject,
                    "credence": row.credence,
                }
            )

        log.info(
            "hypothesis_proposer run done: wrote=%d candidates=%d "
            "rejected(duplicate=%d already_believed=%d) expired=%d "
            "llm_ms=%.0f",
            len(wrote),
            len(candidates),
            rejected_dup,
            rejected_known,
            expired,
            llm_ms,
        )
        return {
            "checked": len(candidates),
            "wrote": len(wrote),
            "hypotheses": wrote,
            "rejected_duplicate": rejected_dup,
            "rejected_already_believed": rejected_known,
            "expired": expired,
            "llm_ms": int(llm_ms),
        }

    # ── gates ─────────────────────────────────────────────────────────

    def _nearest_hypothesis(
        self, store: "HypothesisStore", vec: Any,
    ) -> tuple[Hypothesis | None, float]:
        """The nearest existing guess, if it is near enough to reject.

        Deliberately over closed rows too, refuted included: the point of
        keeping a refuted row instead of deleting it is that she does not
        re-invent the guess the user already turned down.

        ``expired`` is the one status that does **not** block, and the
        distinction is the whole reason this is not a plain
        ``live_only=False`` scan. An expired row means she never got round
        to asking -- nothing was learned about the guess, and the row is
        closed so it can never be asked now. Letting it block would
        retire that ground permanently on the strength of her own
        inattention, which over months is exactly the sterility
        ``hypothesis_min_novelty`` sits high to avoid. Re-inventing it
        gives the guess a fresh TTL and another chance to be raised.
        """
        from app.core.concepts.hypothesis_store import STATUS_EXPIRED

        try:
            hits = store.nearest(vec, k=5, live_only=False)
        except Exception:
            log.debug("hypothesis nearest failed", exc_info=True)
            return None, 0.0
        blocking = [
            (row, sim)
            for row, sim in hits
            if str(getattr(row, "status", "")) != STATUS_EXPIRED
        ]
        if not blocking:
            return None, 0.0
        row, sim = blocking[0]
        return (row if sim >= self._min_novelty else None), float(sim)

    def _nearest_concept(
        self, candidate: dict[str, Any], vec: Any,
    ) -> tuple[Any, float]:
        """The nearest thing she already believes, if too near.

        Searches across kinds within the subject, for the same reason the
        graduation-side duplicate check does (see
        :mod:`app.core.concepts.concept_dedupe`): the proposer's guessed
        kind carries no authority, so filtering on it would let a belief
        she already holds through on a taxonomy disagreement.

        ``world``-subject guesses skip this entirely — the concept graph
        has no subject for how something works, so there is nothing there
        that could be the same thought.
        """
        subject = str(candidate.get("subject") or "user")
        if subject == SUBJECT_WORLD:
            return None, 0.0
        store = self._concept_store()
        if store is None:
            return None, 0.0
        try:
            hits = store.nearest(
                vec, subject=subject, kind=None, status=None, k=3,
            )
        except Exception:
            log.debug("concept nearest failed", exc_info=True)
            return None, 0.0
        if not hits:
            return None, 0.0
        concept, sim = hits[0]
        return (concept if sim >= self._concept_novelty else None), float(sim)

    def _expire(self, store: "HypothesisStore") -> int:
        try:
            return int(
                store.expire_stale(
                    ttl_hours=self._ttl_hours, now=self._clock()
                )
            )
        except Exception:
            log.debug("hypothesis TTL sweep failed", exc_info=True)
            return 0

    @staticmethod
    def _count_live(store: "HypothesisStore") -> int:
        try:
            return int(store.count_live())
        except Exception:
            return 0

    # ── write ─────────────────────────────────────────────────────────

    def _persist(
        self,
        store: "HypothesisStore",
        candidate: dict[str, Any],
        statement: str,
        vec: Any,
    ) -> Hypothesis | None:
        subject = str(candidate.get("subject") or "user")
        if subject not in _SUBJECTS:
            subject = "user"
        origin = str(candidate.get("origin") or ORIGIN_FREE)
        if origin not in ORIGINS:
            origin = ORIGIN_FREE
        row = Hypothesis(
            statement=statement,
            kind=str(candidate.get("kind") or "identity"),
            subject=subject,
            rationale=_trim(
                candidate.get("rationale"), max_chars=_MAX_RATIONALE_CHARS
            ),
            origin=origin,
            credence=_clamp(candidate.get("credence"), default=0.5),
            embedding=np.asarray(vec, dtype=np.float32).ravel(),
        )
        try:
            store.add(row)
        except Exception:
            log.warning("hypothesis insert failed", exc_info=True)
            return None
        log.info(
            "hypothesis invented: id=%s subject=%s kind=%s credence=%.2f "
            "statement=%r",
            row.hypothesis_id,
            row.subject,
            row.kind,
            row.credence,
            statement[:80],
        )
        return row

    # ── context pack ──────────────────────────────────────────────────

    def _concepts_block(self) -> str:
        store = self._concept_store()
        if store is None:
            return "(nothing settled yet)"
        try:
            rows = store.list_by(status="active")
        except Exception:
            log.debug("concept list_by failed", exc_info=True)
            return "(nothing settled yet)"
        if not rows:
            return "(nothing settled yet)"
        rows = sorted(
            rows,
            key=lambda c: -float(getattr(c, "confidence", 0.0) or 0.0),
        )[:_MAX_CONCEPTS]
        return "\n".join(
            f"- [{getattr(row, 'subject', 'user')}] "
            f"{_trim(getattr(row, 'label', ''), max_chars=120)}"
            for row in rows
        )

    def _memories_block(self) -> str:
        if self._memory_store_provider is None:
            return "(none)"
        try:
            store = self._memory_store_provider()
        except Exception:
            store = None
        if store is None:
            return "(none)"
        try:
            rows = store.list_recent(limit=_MAX_MEMORIES)
        except Exception:
            log.debug("memory recent failed", exc_info=True)
            return "(none)"
        lines = [
            f"- {_trim(getattr(row, 'content', ''), max_chars=140)}"
            for row in (rows or [])
            if getattr(row, "content", "")
        ]
        return "\n".join(lines) if lines else "(none)"

    def _clusters_block(self) -> str:
        if self._topic_graph_provider is None:
            return "(none)"
        try:
            graph = self._topic_graph_provider()
        except Exception:
            graph = None
        if graph is None:
            return "(none)"
        try:
            clusters = graph.topic_clusters()
        except Exception:
            log.debug("topic_clusters failed", exc_info=True)
            return "(none)"
        if not clusters:
            return "(none)"
        ordered = sorted(clusters, key=lambda c: (-c.size, c.cluster_id))
        lines = [
            f"- {_trim(c.summary or '(unnamed)', max_chars=100)}"
            for c in ordered[:_MAX_CLUSTERS]
        ]
        return "\n".join(lines) if lines else "(none)"

    def _open_block(self, store: "HypothesisStore") -> str:
        try:
            rows = store.list_by(live=True)
        except Exception:
            return "(none)"
        if not rows:
            return "(none)"
        return "\n".join(
            f"- {_trim(row.statement, max_chars=120)}"
            for row in rows[:_MAX_OPEN_LIST]
        )

    # ── LLM ───────────────────────────────────────────────────────────

    def _call_llm(self, store: "HypothesisStore") -> list[dict[str, Any]]:
        from app.core.concepts.concept_kinds import CONCEPT_KINDS

        system = _SYSTEM_PROMPT.format(
            assistant_name=self._assistant_name(),
            user_name=self._user_name(),
            kinds=", ".join(sorted(CONCEPT_KINDS)),
            min_n=_MIN_N,
            max_n=_MAX_N,
        )
        user_payload = _USER_TEMPLATE.format(
            concepts=self._concepts_block(),
            memories=self._memories_block(),
            clusters=self._clusters_block(),
            open_hypotheses=self._open_block(store),
        )
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_payload},
                ],
                options={
                    "num_predict": _MAX_TOKENS,
                    # Hotter than the other maintenance passes. Every
                    # other worker wants the most defensible reading of
                    # its inputs; this one wants a reach, and a cautious
                    # guess is a paraphrase of something she already
                    # believes -- which the novelty gate then rejects.
                    "temperature": 0.95,
                },
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="hypothesis_proposer_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("hypothesis_proposer chat_stream raised", exc_info=True)
            return []
        if self._cancel_event.is_set():
            return []
        return self._parse(("".join(chunks)).strip())

    @staticmethod
    def _parse(raw: str) -> list[dict[str, Any]]:
        match = _JSON_OBJECT_RE.search(raw or "")
        if match is None:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, dict):
            return []
        entries = parsed.get("hypotheses")
        if not isinstance(entries, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in entries[:_MAX_N]:
            if not isinstance(entry, dict):
                continue
            statement = str(entry.get("statement") or "").strip()
            if not statement:
                continue
            out.append(
                {
                    "statement": statement,
                    "kind": str(entry.get("kind") or "identity").strip(),
                    "subject": str(entry.get("subject") or "user").strip(),
                    "rationale": str(entry.get("rationale") or "").strip(),
                    "credence": entry.get("credence"),
                    "origin": str(entry.get("origin") or ORIGIN_FREE).strip(),
                }
            )
        return out

    # ── plumbing ──────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def _store(self) -> "HypothesisStore | None":
        try:
            return self._hypotheses()
        except Exception:
            return None

    def _concept_store(self) -> "ConceptStore | None":
        try:
            return self._concepts()
        except Exception:
            return None

    def _user_name(self) -> str:
        return _name(self._user_display_name_provider, "the user")

    def _assistant_name(self) -> str:
        return _name(self._assistant_display_name_provider, "the assistant")


def _name(provider: Callable[[], str] | None, fallback: str) -> str:
    if provider is None:
        return fallback
    try:
        return (provider() or fallback) or fallback
    except Exception:
        return fallback


def _clamp(value: Any, *, default: float) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return default


__all__ = ["HypothesisProposerWorker"]
