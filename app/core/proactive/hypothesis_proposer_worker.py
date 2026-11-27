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
``hypothesis_max_open`` is a ceiling on live rows, because nothing prunes
this table by decay the way L3 prunes concepts — an untested guess is
exactly as plausible next month as today, just staler. TTL expiry runs at
the top of every tick and only touches rows that were never answered.

It used to be a *hard* ceiling, and that was the whole lane's undoing.
The cap has two exits and the one meant to carry the traffic — being
asked — needs a topical match, so a shelf stocked with subjects that stop
coming up has only the fortnightly TTL left. What that produced was not a
steady trickle of wondering but a duty cycle: twelve inventions, then
total silence until they aged out together, reported each tick as a
healthy-looking ``skipped: max_open``. Measured mid-August: seven days
silent with seven to go, nine of the twelve never asked once.

So a full shelf now *replaces* instead of refusing, giving up its stalest
never-asked row when a novel guess is waiting (see
:meth:`~app.core.concepts.hypothesis_store.HypothesisStore.stalest_evictable`
for what is protected from that, and ``hypothesis_evict_min_age_hours``
for the age bar that keeps replacement from becoming churn). Eviction is
lazy: nothing is given up until a candidate has cleared both novelty
gates, so a barren pass costs the shelf nothing.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Sequence

import numpy as np

from app.core.concepts.hypothesis_store import (
    Hypothesis,
    SUBJECT_WORLD,
)
from app.core.infra import timephrase
from app.core.proactive.idle_worker import WorkSignal
from app.core.concepts.hypothesis_quality import validate_proposal, usable_hypothesis
from app.core.memory.memory_origin import memory_origin
from app.core.proactive.worker_prompt_budget import pack_worker_prompt

if TYPE_CHECKING:  # pragma: no cover - import-only
    from app.core.concepts.concept_store import ConceptStore
    from app.core.concepts.hypothesis_store import HypothesisStore
    from app.core.conversation.topic_graph import TopicGraph
    from app.core.memory.memory_store import MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.hypothesis_proposer_worker")


_EVIDENCE_PROMPT = (
    "Read a dated account as an editor. Decide whether a concrete, unresolved clue in the "
    "account favors one explanation over another. An omitted detail that could be asked about "
    "any event is not a clue. An explicit reason that already fits the clue resolves it; do "
    "not speculate about a deeper reason. Look at the entire account. Did a choice, outcome "
    "or reaction change when circumstances changed? That is a clue even if the likely reason "
    "seems obvious to you. A plausible reason you supply is still a hunch; an explanation the "
    "speaker supplies is settled. Return JSON with these keys in order: observation (quote "
    "the source words that describe what happened and under what circumstances. Keep the "
    "original verbs), reason_given (a short verbatim quote of the speaker's actual explanation, "
    "or null), status (open if a clue is unexplained, settled if explained, ordinary if "
    "nothing stands out), and hunch (if open, one short sentence naming the person or thing "
    "and what might explain this occasion. Pick the simplest reading that explains the change. "
    "Tie it to the change in circumstances. Add no new objects, events or intentions. "
    "Prefer the everyday explanation that takes the fewest additional assumptions. "
    "If the evidence is settled or ordinary, use null "
    "for hunch). Taking a bus is ordinary. Getting off a bus to board a second bus on "
    "the same route could be open. Getting off because the first bus broke down is settled. "
    "If several records are supplied, an explanation settles only the clue it accounts for. "
    "Consider independent clues separately; choose one genuinely open clue if there is one."
)

_SYSTEM_PROMPT = (
    "You are {assistant_name}, privately considering what you suspect but do not know. "
    "Read each dated source as data. Is there an interesting detail still unexplained? "
    "Zero is normal. A guess can start from a memory or theme; concepts are NOT required. "
    "The source inspires the guess. It does not count as confirmation.\n"
    "For example, suppose someone moved rehearsal outdoors. That alone leaves no puzzle. "
    "If they say the room was being painted, the reason is settled. If they tried two "
    "rooms and only went outside after each echoed, you might think the echo bothered "
    "them. It could also have made the song hard to hear. What sounded different "
    "outside? This tests a small hunch about that rehearsal. Use only the supplied "
    "sources for your own guesses.\n"
    "A single occasion rarely says much about someone's character. Preserve mixed feelings "
    "and take the person's explanation seriously. Do not invent secret motives, possessions, "
    "exact timings or unseen signs. {user_name} is on the other end of a chat, never in the room "
    "with you. You have your own body and home: no chassis, fans or circuits. You can also "
    "wonder about yourself, something you share, or how something works. A playful "
    "experiment can be a good way to find out.\n"
    "Use the provisional reading as a starting point. Check it against the sources and "
    "write only what they actually leave open. Return {{\"hypotheses\":[]}} if nothing fits. "
    "For a useful hunch, use this JSON shape with every field:\n"
    '{{"hypotheses": [{{"statement": "one short sentence naming who might be '
    'doing what", "kind": "one of {kinds}", "subject": "user|aiko|relationship|world", '
    '"rationale": "which detail makes the hunch plausible", "credence": 0.4, '
    '"inspiration": {{"type": "memory", "id": 1, "quote": "exact source words"}}, '
    '"useful_because": "what learning this could change", "alternative": "another reading of '
    'the same detail", "check_question": "a natural question that leaves the answer open?", '
    '"raise_when": "a specific occasion when finding out would fit"}}]}}\n'
    "Use the actual source type and integer ID and an exact quote of 8 to 200 characters. "
    "Use subject user for {user_name}, aiko for {assistant_name}, relationship for the two "
    "together and world for an outside phenomenon. "
    "Use your own credence from 0 to 1. Keep each field under 300 characters. "
    "When there is only one episode, keep the explanation within that episode."
)

_USER_TEMPLATE = (
    "KNOWN CONCEPT BASELINE (do not restate; never confirmation of a guess):\n{concepts}\n\n"
    "DATED INSPIRATIONS (context, not proof):\n{inspirations}\n\n"
    "EARLIER GUESSES (avoid paraphrases):\n{open_hypotheses}\n\n"
    "Your provisional thought: {hunch}\n"
    "File this specific thought if the sources warrant keeping it. Keep the possibility "
    "audible in the statement itself, for example with 'might have'. Record what else "
    "could explain the same observation. For check_question, speak to the person who had "
    "the experience: e.g. 'What felt different when you tried the other version?' For your "
    "own experience: 'What would I listen for on another try?' Adapt to the actual scene. "
    "Keep unobserved actions, new objects, and your favored answer out of the question. "
    "Let them supply the reason. Do not substitute a more dramatic interpretation. "
    "You can still return an empty list."
)
_MIN_N = 0
_MAX_N = 2
_MAX_TOKENS = 1600
_MAX_HUNCH_CHARS = 300
_MAX_CONCEPTS = 12
_MAX_MEMORIES = 8
_MAX_CLUSTERS = 8
_MAX_OPEN_LIST = 10
_MAX_STATEMENT_CHARS = 300
_MAX_RATIONALE_CHARS = 400
_ABSTENTION_TTL = timedelta(hours=6)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)

#: Vocabulary that only makes sense if Aiko is a machine.
#:
#: The persona gives her a body, an apartment and an energy level that
#: moves through the day, so a self-hypothesis phrased in hardware is not
#: a bold guess -- it is a different character. This worker is the one
#: place in the stack with no persona context and the loosest brief
#: ("reaching is the point"), run at temperature 0.95, and the
#: combination lands here reliably: of the first six ``subject=aiko``
#: rows, five described hardware she does not have -- a chassis that
#: feels phantom warmth, cooling fans that make her metaphors better,
#: processing latency that drops when he is in the room.
#:
#: Deliberately narrow, and only ever applied to ``subject=aiko``. Every
#: entry is a physical machine part or machine telemetry with no
#: ordinary reading for a person; the ambiguous software vocabulary
#: ("weights", "parameters", "codebase") is left to the prompt, because
#: "she weights her words" is a sentence we want.
_MACHINE_SELF_RE = re.compile(
    r"\b(?:"
    r"chassis|circuit(?:ry|s)?|transistors?|motherboard|silicon|"
    r"cooling (?:fan|system|vent)s?|heat ?sinks?|"
    r"cpu|gpu|ram|processors?|server (?:rack|farm)s?|"
    r"processing (?:latency|power|load)|clock speed|firmware|"
    r"memory (?:subsystem|consolidation)|uptime|bandwidth"
    r")\b",
    re.IGNORECASE,
)


def describes_machinery(statement: str) -> str:
    """The machine term ``statement`` uses to describe Aiko, or ``""``.

    Returns the offending phrase rather than a bool so the reject log
    names it -- a silent count here would be indistinguishable from the
    novelty gate, and the whole point is knowing which failure is
    consuming the run's candidates.
    """
    match = _MACHINE_SELF_RE.search(str(statement or ""))
    return match.group(0) if match else ""

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
        evict_when_full: bool = True,
        evict_min_age_hours: float = 168.0,
        clock: Callable[[], datetime] | None = None,
        context_window: Callable[[], int | None] | None = None,
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
        self._evict_when_full = bool(evict_when_full)
        self._evict_min_age_hours = max(1.0, float(evict_min_age_hours))
        self._clock = clock or timephrase.utcnow
        self._context_window = context_window or (lambda: None)
        self._inspirations: dict[tuple[str, int], dict[str, Any]] = {}
        self._offered_inspirations: dict[tuple[str, int], dict[str, Any]] = {}
        self._abstention: tuple[str, datetime] | None = None
        self._run_lock = threading.Lock()

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

        A full shelf holding stale stock is *not* the same as a full one,
        and reporting both as ``shelf_full`` is how the lane went a week
        without a new guess while every signal said healthy. A shelf that
        can replace reports demand -- low demand, one slot's worth,
        because giving up a guess to make room is worth less than filling
        an empty slot and should lose to any worker with real hunger.
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
            if any(not usable_hypothesis(h) and h.asked_count <= 0
                   for h in store.list_by(live=True)):
                return WorkSignal(pressure=min(1.0, 1.0 / self._max_open),
                                  reason="unreviewed_stock", needs_llm=True)
            if self._evictable(store) is None:
                return WorkSignal(pressure=0.0, reason="shelf_full")
            return WorkSignal(
                pressure=min(1.0, 1.0 / float(self._max_open)),
                reason="shelf_stale",
                needs_llm=True,
            )
        return WorkSignal(
            pressure=min(1.0, shortfall / float(self._max_open)),
            reason="shortfall",
            needs_llm=True,
        )

    def run(self) -> dict[str, Any]:
        # A forced run and the scheduler share this worker's offered-source receipts.
        if not self._run_lock.acquire(blocking=False):
            return {"skipped": True, "reason": "already_running"}
        try:
            return self._run_once()
        finally:
            self._run_lock.release()

    def _run_once(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        store = self._store()
        if store is None:
            return {"skipped": True, "reason": "no_store"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        expired = self._expire(store)
        retired = self._retire_unreviewed(store)

        live = self._count_live(store)
        room = self._max_open - live
        # A full shelf is only a stop if it is also a *fresh* shelf. When
        # it is stale, the run continues on credit: no row is given up
        # until a candidate has passed both novelty gates, so the shelf is
        # never spent on a pass that produces nothing.
        if room <= 0:
            if not self._evict_when_full or self._evictable(store) is None:
                return {
                    "skipped": True,
                    "reason": "max_open",
                    "live": live,
                    "expired": expired,
                }
            log.info(
                "hypothesis shelf full but stale: live=%d replacing up to %d",
                live,
                self._max_per_run,
            )

        t0 = time.monotonic()
        try:
            candidates, outcome = self._call_llm(store)
        except Exception:
            log.warning("hypothesis_proposer LLM call raised", exc_info=True)
            return {"errored": True, "reason": "llm_call", "expired": expired, "wrote": 0}
        llm_ms = (time.monotonic() - t0) * 1000.0
        if self._cancel_event.is_set():
            return {"cancelled": True, "expired": expired}
        if not candidates:
            return {
                "checked": 0,
                "wrote": 0,
                "reason": outcome,
                "expired": expired,
                "llm_ms": int(llm_ms),
            }

        # The cap is enforced per write by ``slots`` below rather than by a
        # budget computed up front, because on a stale shelf a slot can be
        # *bought* mid-loop and ``room`` at this point does not know that.
        budget = self._max_per_run
        slots = room
        evicted: list[int] = []
        wrote: list[dict[str, Any]] = []
        rejected_dup = 0
        rejected_known = 0
        rejected_machine = 0
        rejected_quality = 0
        quality_faults: dict[str, int] = {}
        for candidate in candidates:
            if len(wrote) >= budget:
                break
            quality, fault = self._proposal_quality(candidate)
            if fault:
                rejected_quality += 1
                quality_faults[fault] = quality_faults.get(fault, 0) + 1
                continue
            candidate["quality"] = quality
            statement = _trim(
                candidate.get("statement"), max_chars=_MAX_STATEMENT_CHARS
            )
            if not statement:
                continue

            # Before the embed, because this one costs nothing and the
            # embed does. Self-only: "his GPU ran hot" is a fine guess
            # about him, and only a guess about *her* changes who she is.
            if str(candidate.get("subject") or "") == "aiko":
                term = describes_machinery(statement)
                if term:
                    rejected_machine += 1
                    log.info(
                        "hypothesis reject(machine-self): term=%r guess=%r",
                        term,
                        statement[:80],
                    )
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

            twin, sim = self._nearest_hypothesis(store, vec, protect=evicted)
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

            # Both gates passed, so this guess is worth a slot. Buy one if
            # the shelf owes us none -- deliberately here and not earlier,
            # so a pass whose every candidate was rejected leaves the
            # shelf exactly as it found it.
            if slots <= 0:
                given_up = self._evict_one(store, exclude=evicted)
                if given_up is None:
                    break
                evicted.append(given_up)
                slots += 1

            row = self._persist(store, candidate, statement, vec)
            if row is None:
                continue
            slots -= 1
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
            "rejected(duplicate=%d already_believed=%d machine_self=%d) "
            "expired=%d evicted=%d llm_ms=%.0f",
            len(wrote),
            len(candidates),
            rejected_dup,
            rejected_known,
            rejected_machine,
            expired,
            len(evicted),
            llm_ms,
        )
        out: dict[str, Any] = {
            "checked": len(candidates),
            "wrote": len(wrote),
            "hypotheses": wrote,
            "rejected_duplicate": rejected_dup,
            "rejected_already_believed": rejected_known,
            "rejected_machine_self": rejected_machine,
            "rejected_quality": rejected_quality,
            "quality_faults": quality_faults,
            "retired_unreviewed": retired,
            "expired": expired,
            "llm_ms": int(llm_ms),
        }
        if evicted:
            out["evicted"] = evicted
        return out

    def _proposal_quality(self, candidate):
        return validate_proposal(candidate, self._offered_inspirations)

    def _retire_unreviewed(self, store):
        """The ordinary invention shelf yields before new motivated ideas."""
        from app.core.concepts.hypothesis_store import STATUS_EXPIRED

        retired = 0
        for old in store.list_by(live=True):
            if not usable_hypothesis(old) and old.asked_count <= 0:
                store.close(old, status=STATUS_EXPIRED)
                retired += 1
        return retired

    # ── shelf space ───────────────────────────────────────────────────

    def _evictable(self, store: "HypothesisStore") -> "Hypothesis | None":
        """The row a replacement would take, without taking it."""
        if not self._evict_when_full:
            return None
        try:
            return store.stalest_evictable(
                min_age_hours=self._evict_min_age_hours, now=self._clock(),
            )
        except Exception:
            log.debug("hypothesis evictable lookup failed", exc_info=True)
            return None

    def _evict_one(
        self, store: "HypothesisStore", *, exclude: "list[int]",
    ) -> int | None:
        """Retire the stalest never-asked guess. Returns its id, or None.

        Closed as ``expired`` rather than deleted, which is the same exit
        the TTL uses and carries the same meaning: she never got round to
        asking. That matters downstream -- ``expired`` is the one status
        :meth:`_nearest_hypothesis` lets past, so the ground stays open to
        be wondered again instead of being burned by a slot shortage.
        """
        from app.core.concepts.hypothesis_store import STATUS_EXPIRED

        if not self._evict_when_full:
            return None
        try:
            row = store.stalest_evictable(
                min_age_hours=self._evict_min_age_hours,
                now=self._clock(),
                exclude=exclude,
            )
            if row is None:
                return None
            store.close(row, status=STATUS_EXPIRED)
        except Exception:
            log.debug("hypothesis eviction failed", exc_info=True)
            return None
        log.info(
            "hypothesis evicted to make room: id=%s guess=%r",
            row.hypothesis_id,
            str(row.statement)[:80],
        )
        return int(row.hypothesis_id)

    # ── gates ─────────────────────────────────────────────────────────

    def _nearest_hypothesis(
        self,
        store: "HypothesisStore",
        vec: Any,
        *,
        protect: "Sequence[int]" = (),
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

        ``protect`` re-arms that exemption for rows *this pass* expired to
        make room. Without it a run could give up a guess for a slot and
        spend the slot on a paraphrase of it -- churn that reports as two
        healthy writes. Only within the pass: on the next run the row is
        an ordinary expired one and free to be wondered again.
        """
        from app.core.concepts.hypothesis_store import STATUS_EXPIRED

        try:
            hits = store.nearest(vec, k=5, live_only=False)
        except Exception:
            log.debug("hypothesis nearest failed", exc_info=True)
            return None, 0.0
        just_evicted = {int(i) for i in protect}
        blocking = [
            (row, sim)
            for row, sim in hits
            if str(getattr(row, "status", "")) != STATUS_EXPIRED
            or int(getattr(row, "hypothesis_id", 0) or 0) in just_evicted
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
        quality = candidate["quality"]
        inspiration = quality["inspiration"]
        origin = "memory" if inspiration["type"] == "memory" else "extrapolation"
        row = Hypothesis(
            statement=statement,
            kind=str(candidate.get("kind") or "identity"),
            subject=subject,
            rationale=_trim(
                candidate.get("rationale"), max_chars=_MAX_RATIONALE_CHARS
            ),
            origin=origin, origin_refs=[inspiration["id"]], quality=quality,
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
            rows = store.list_recent(limit=_MAX_MEMORIES * 4)
        except Exception:
            log.debug("memory recent failed", exc_info=True)
            return "(none)"
        lines = []
        for row in rows or []:
            origin = memory_origin(row)
            if origin.mode in {"hypothetical", "question", "summary", "unknown"}:
                continue
            mid = getattr(row, "id", None)
            content = getattr(row, "content", "")
            if type(mid) is not int or not isinstance(content, str) or not content.strip():
                continue
            meta = getattr(row, "metadata", None) or {}
            if meta.get("superseded_by") or meta.get("consolidated_into"):
                continue
            content = " ".join(content.splitlines()).strip()
            if len(content) > 1000:
                continue  # Do not sever qualifications just to fit more sources.
            note = SimpleNamespace(content=content,
                                   created_at=getattr(row, "created_at", None),
                                   temporal_type=getattr(row, "temporal_type", None),
                                   event_time=getattr(row, "event_time", None))
            rendered = timephrase.format_memory_block([note], self._clock())
            receipt = {"type": "memory", "id": mid, "text": content,
                       "created_at": getattr(row, "created_at", None),
                       "temporal_type": note.temporal_type, "event_time": note.event_time,
                       "subject": origin.subject, "mode": origin.mode}
            self._inspirations[("memory", mid)] = receipt
            stamp = getattr(row, "created_at", None) or "date unknown"
            lines.append(f"[memory:{mid}; {origin.subject}/{origin.mode}; {stamp}] {rendered}")
            if len(lines) >= _MAX_MEMORIES:
                break
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
        lines = []
        for c in ordered[:_MAX_CLUSTERS]:
            label = str(c.summary or "").strip()
            if not label:
                continue
            self._inspirations[("topic", int(c.cluster_id))] = {
                "type": "topic", "id": int(c.cluster_id), "text": label[:300],
                "subject": "topic context", "mode": "summary, not an observation",
            }
            lines.append(f"[topic:{int(c.cluster_id)}; theme, not fact] {label[:300]}")
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

    def _call_llm(self, store: "HypothesisStore") -> tuple[list[dict[str, Any]], str]:
        from app.core.concepts.concept_kinds import CONCEPT_KINDS

        self._inspirations = {}
        self._offered_inspirations = {}
        assistant_name, user_name = self._assistant_name(), self._user_name()
        system = (timephrase.today_anchor(self._clock()) + "\n\n"
                  + _SYSTEM_PROMPT.format(assistant_name=assistant_name, user_name=user_name,
                                          kinds=", ".join(sorted(CONCEPT_KINDS)))
                  + "\n\n" + timephrase.STORED_TEXT_TIME_RULE)
        sources = (self._memories_block() + "\n" + self._clusters_block()).splitlines()
        sources = [line for line in sources if line.startswith(("[memory:", "[topic:"))]
        if not sources:
            return [], "no_sources"
        concepts, guesses = self._concepts_block(), self._open_block(store)

        def render(selected, hunch=""):
            return [{"role": "system", "content": system}, {"role": "user", "content":
                _USER_TEMPLATE.format(concepts=concepts, inspirations="\n".join(selected),
                                      open_hypotheses=guesses, hunch=hunch)}]

        packed = None
        context_window = self._context_window()
        for _ in range(3):
            packed = pack_worker_prompt(sources, render, context_window=context_window,
                                        output_tokens=_MAX_TOKENS + _MAX_HUNCH_CHARS,
                                        surface="hypothesis_proposer_worker", log=log)
            if packed is not None:
                break
            if guesses != "(omitted for evidence budget)":
                guesses = "(omitted for evidence budget)"
            else:
                concepts = "(omitted for evidence budget)"
        if packed is None:
            return [], "prompt_budget"
        for source in sources[:packed.item_count]:
            match = re.match(r"\[(memory|topic):(\d+);", source)
            if match is None:
                continue
            key = (match[1], int(match[2]))
            if key in self._inspirations:
                self._offered_inspirations[key] = self._inspirations[key]
        # Key the offered material itself. Age labels and the wall-clock prompt
        # anchor change between runs even when a single source hasn't changed.
        route = getattr(self._ollama, "routing_revision", None)
        material = [self._chat_model, route, context_window, assistant_name, user_name,
                    concepts, guesses, list(self._offered_inspirations.values())]
        encoded = json.dumps(material, sort_keys=True, default=str).encode()
        digest = hashlib.sha256(encoded).hexdigest()
        now = self._clock()
        if self._abstention:
            previous, stamp = self._abstention
            if previous == digest and timedelta(0) <= now - stamp < _ABSTENTION_TTL:
                return [], "unchanged_abstention"
        self._abstention = None
        selected = sources[:packed.item_count]
        source_keys = {f"{kind}:{ident}" for kind, ident in self._offered_inspirations}
        evidence_prompt = timephrase.today_anchor(self._clock()) + "\n" + _EVIDENCE_PROMPT
        if len(source_keys) > 1:
            evidence_prompt += (
                "\nFirst return a reviewed object mapping each supplied source key (for "
                "example memory:1) to ordinary, settled, or open. Then report the observation, "
                "reason_given, status and hunch for the most promising open clue, if any."
            )
        messages = [{"role": "system", "content": evidence_prompt},
                    {"role": "user", "content": "\n".join(selected)}]
        raw, finish = self._generate(messages, route, "hypothesis_evidence", 400, 0.0)
        if finish not in (None, "stop"):
            return [], finish
        try:
            reading = json.loads(raw)
        except ValueError:
            return [], "invalid_response"
        if not isinstance(reading, dict) or not {"observation", "reason_given", "status",
                                                "hunch"}.issubset(reading):
            return [], "invalid_response"
        observation, reason = reading["observation"], reading["reason_given"]
        if not isinstance(observation, str) or not observation.strip():
            return [], "invalid_response"
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            return [], "invalid_response"
        status, hunch = reading["status"], reading["hunch"]
        if len(source_keys) > 1:
            reviewed = reading.get("reviewed", {k: reading[k] for k in source_keys if k in reading})
            if (not isinstance(reviewed, dict) or set(reviewed) != source_keys
                    or any(value not in ("ordinary", "settled", "open")
                           for value in reviewed.values())
                    or (status == "open") != ("open" in reviewed.values())):
                return [], "incomplete_evidence_review"
            if status == "open":
                open_keys = {key for key, value in reviewed.items() if value == "open"}
                selected = [line for line in selected if line[1:].partition(";")[0] in open_keys]
                self._offered_inspirations = {
                    (kind, ident): receipt
                    for (kind, ident), receipt in self._offered_inspirations.items()
                    if f"{kind}:{ident}" in open_keys
                }
        if status == "settled" and reason:
            quote = reason.strip().casefold()
            if not any(quote in row["text"].casefold()
                       for row in self._offered_inspirations.values()):
                return [], "unoffered_explanation"
        if hunch is None and (
            (status == "settled" and reason) or (status == "ordinary" and reason is None)
        ):
            candidates, outcome = [], "abstained"
        elif (status == "open" and isinstance(hunch, str)
              and 0 < len(hunch.strip()) <= _MAX_HUNCH_CHARS):
            raw, finish = self._generate(render(selected, hunch.strip()), route,
                                         "hypothesis_proposer_worker", _MAX_TOKENS, 0.35)
            if finish not in (None, "stop"):
                return [], finish
            candidates, outcome = self._parse(raw)
        else:
            return [], "invalid_response"
        if outcome == "abstained" and finish == "stop":
            self._abstention = (digest, self._clock())
        return candidates, outcome

    def _generate(self, messages, route, surface, tokens, temperature):
        if self._cancel_event.is_set():
            return "", "cancelled"
        if route != getattr(self._ollama, "routing_revision", None):
            return "", "route_changed"
        raw = "".join(self._ollama.chat_stream(
            messages, options={"num_predict": tokens, "temperature": temperature},
            model=self._chat_model, stop_event=self._cancel_event, format_json=True, think=False,
            surface=surface,
        )).strip()
        if self._cancel_event.is_set():
            return "", "cancelled"
        if route != getattr(self._ollama, "routing_revision", None):
            return "", "route_changed"
        finish = getattr(getattr(self._ollama, "last_usage", None), "done_reason", None)
        return raw, finish if finish in (None, "stop") else "incomplete_response"

    @staticmethod
    def _parse(raw: str) -> tuple[list[dict[str, Any]], str]:
        match = _JSON_OBJECT_RE.search(raw or "")
        if match is None:
            return [], "invalid_response"
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return [], "invalid_response"
        if not isinstance(parsed, dict):
            return [], "invalid_response"
        entries = parsed.get("hypotheses")
        # A preamble may accompany proposals. Only an entire, explicitly empty JSON
        # reply is a completed abstention that can stand in for a later model call.
        if match.group(0) == raw.strip() and parsed == {"hypotheses": []}:
            return [], "abstained"
        if not isinstance(entries, list):
            return [], "invalid_response"
        candidates = [entry for entry in entries[:_MAX_N] if isinstance(entry, dict)]
        return candidates, "proposed" if candidates else "invalid_response"

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


__all__ = [
    "describes_machinery","HypothesisProposerWorker"]
