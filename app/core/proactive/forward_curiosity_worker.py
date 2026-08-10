"""K34 — Forward curiosity worker ("I've been wondering ...").

The gap-return family already has two members: K28 ``turning_over``
surfaces what Aiko has been *thinking* about between sessions, and K36
``away_activities`` surfaces what she's been *doing*. K34 is the
forward-looking third sibling: it drafts a genuine question Aiko *wants
to ask the user* about their life ("did the espresso machine arrive?",
"how did your sister's move go?") and surfaces one on the first turn
back after a long typed absence.

This worker is the silent producer. During a quiet window it:

  * gathers candidate topics from the user's own ``future_plan``
    memories (upcoming things they mentioned) and recent ``callback``
    rows, with what she holds about him as phrasing context (concepts
    first, the K3 routine / usual-hours profile as floor). Whether a
    plan is still *pending* is not decided here: ``MemoryDecayWorker``
    retires plans that have stopped being plans, so the temporal type
    is the authority and everything in this pool is fair game,
  * picks one that hasn't been drafted recently,
  * composes a short, natural forward question (deterministic template,
    optionally rephrased by the local worker LLM with a safe fallback),
  * appends ``{at, question, source, source_id}`` to a small kv_meta
    journal ring (``aiko.forward_curiosity``).

K87 gave it a third pool. ``future_plan`` and ``callback`` are both his
life by construction, so a worker fed only by them could only ever break
a long silence with a question about him -- and this is the cue that
fires on exactly the turn where she has the most room to lead. The
``wondering`` source draws her own subject notes (the curiosity worker's
subject mode) and drafts a *statement* rather than a question, with
``agent.curiosity_subject_quota`` deciding how the two split.

L28 gave it a fourth. All three pools above are memory rows, so a plan,
a callback or a note was the only thing she could be curious *about*:
she could ask how an event went but never wonder whether a direction he
is on still holds. The ``concept`` source reads her ``forward_curiosity``
concept diet, keys candidates as ``concept:{id}`` so the existing dedupe
paths cover it unchanged, and lands on whichever side of the K87 quota
the concept's subject says it belongs to.

The consumer is :meth:`InnerLifeProvidersMixin._render_forward_curiosity_block`,
which on the first turn after a >= ``forward_curiosity_min_gap_hours``
gap folds the newest unseen question into the prompt as one optional,
casual "you've been wondering ..." line. This worker never speaks or
fires a proactive nudge.

Distinct from the existing curiosity systems: G3 ``IdleCuriosityWorker``
answers Aiko's *own* open questions via web search; K9
``CuriositySeedWorker`` proposes brand-new lateral topics; the
speaking-window ``CuriosityWorker`` drafts next-turn follow-ups; and
``FollowUpWorker`` fires time-window proactive nudges near an event's
``event_time``. K34 alone drafts forward questions about the *user's*
life and surfaces them passively on gap-return.

Paced by its own wall-clock cooldown and, above that, by how many unasked
questions are already in the cue pool: a stocked worker reports no demand
*and* declines to draft when ``run()`` is called anyway, since the
scheduler still admits a zero-pressure worker on its plain interval.
Every failure path is swallowed and logged at debug — the worst case is a
missed beat, never a broken insert or a crashed tick.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.cue_producer import CueProducer, StoreProvider
from app.core.proactive.curiosity_subject import (
    MODE_PERSON,
    MODE_SUBJECT,
    is_person_directed,
    wants_subject,
)
from app.core.proactive.idle_worker import WorkSignal
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.concepts.concept_view import ConceptView
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.core.infra.user_profile import UserProfileStore
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.forward_curiosity_worker")


# kv_meta keys this worker owns (namespaced under ``forward_curiosity.``),
# plus the shared journal key the surfacing provider reads.
FORWARD_CURIOSITY_JOURNAL_KEY = "aiko.forward_curiosity"
_KV_LAST_FIRED_AT = "forward_curiosity.last_fired_at"

# How many of the most recent ring entries to scan when de-duping a
# candidate by source id. Bounds the "don't re-draft the same plan"
# check to a small recent window.
_DEDUP_LOOKBACK = 16


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


@dataclass(frozen=True)
class QuestionCandidate:
    """One topic Aiko could raise on her return, + its provenance.

    ``source`` also decides the *shape* of what gets drafted:
    ``future_plan`` and ``callback`` are things in his life and produce
    a question, K87's ``wondering`` is a subject of her own and produces
    a statement she can open with, and L28's ``concept`` is a standing
    thing rather than an event, so it produces a "does that still fit?"
    rather than a "how did it go?".

    ``hers`` is the K87 quota axis and is deliberately *not* derived from
    ``source``: a concept lands on either side depending on whose it is.
    """

    source: str  # "future_plan" | "callback" | "wondering" | "concept"
    source_id: str
    topic: str
    # When the source memory was written. ``topic`` is that memory's raw
    # content, so any "tonight" in it belongs to this moment, not to
    # whenever the cue eventually surfaces (K-time10).
    source_at: str = ""
    #: A subject of hers to offer, rather than a question about him.
    hers: bool = False


# ── journal helpers (shared with the surfacing provider) ────────────────


def load_questions(
    kv_get: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Return the forward-curiosity journal ring (oldest -> newest)."""
    try:
        raw = kv_get(FORWARD_CURIOSITY_JOURNAL_KEY)
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


def append_question(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    ring = load_questions(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(FORWARD_CURIOSITY_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("forward_curiosity journal write failed", exc_info=True)


def is_hers(entry: dict[str, Any]) -> bool:
    """Is this journal entry a subject of hers, or a question about him?

    Two sources produce hers now -- K87's ``wondering`` notes and L28's
    ``subject=aiko`` concepts -- and every consumer of the ring has to
    make the same distinction: the cue's framing, the wants ledger's
    ``share`` vs ``ask``, and the quota history. One predicate so they
    cannot disagree.

    ``source == "wondering"`` is still sufficient on its own because ring
    entries written before the flag existed do not carry it.
    """
    if bool(entry.get("hers")):
        return True
    return str(entry.get("source") or "") == "wondering"


def render_question_cue(entry: dict[str, Any]) -> str:
    """The prompt line for one forward question.

    Written into ``cue_pool`` at production time. The "ask it if it fits"
    handling that used to trail this now arrives with the hoisted persona
    section, so it is only in the prompt on the turns the cue fires.

    Entries of hers get a different line. "You've been wondering how the
    interview went" reads as a prompt to ask; the same frame around a
    subject of hers would too, and the whole point of the subject side is
    that it is hers to offer rather than his to answer.
    """
    question = str(entry.get("question") or "").strip()
    if not question:
        return ""
    # K-time10: the question was drafted from a memory's raw wording, so
    # a "tonight" in it means the evening that memory was written --
    # possibly months back. Resolved here, at render, so the journal
    # entry keeps the original phrasing.
    question = timephrase.resolve_deictics(question, entry.get("source_at"))
    if is_hers(entry):
        return (
            f"Something of your own you've been chewing on: {question} "
            "Offer it if there's room -- it's yours to say, not his to "
            "answer."
        )
    return f"You've been wondering {question}."


class ForwardCuriosityWorker:
    """IdleWorker that drafts forward questions about the user's life."""

    name = "forward_curiosity"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        user_id_provider: Callable[[], str],
        user_display_name_provider: Callable[[], str],
        user_profile_store: "UserProfileStore | None" = None,
        view_provider: Callable[[], "ConceptView | None"] | None = None,
        enabled_provider: Callable[[], bool] | None = None,
        cue_store_provider: StoreProvider | None = None,
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        interval_seconds: float = 1800.0,
        cooldown_seconds: float = 3600.0,
        journal_max: int = 8,
        subject_quota_provider: Callable[[], float] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._user_id_provider = user_id_provider
        self._user_display_name_provider = user_display_name_provider
        self._user_profile_store = user_profile_store
        self._view_provider = view_provider
        self._enabled_provider = enabled_provider
        self._cues = CueProducer("forward_curiosity", cue_store_provider)
        self._ollama = ollama
        self._model = model
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._journal_max = max(1, int(journal_max))
        self._subject_quota_provider = subject_quota_provider or (lambda: 0.4)
        self._rng = rng or random.Random()
        # MCP debug: arm a specific source_id for the next run().
        self._forced_source_id: str | None = None

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
        # Hard vetoes only: the feature flag and the wall-clock rate
        # limiter, which stays a veto because composing a question can
        # call a model.
        return self._enabled() and self._cooldown_elapsed(_utcnow())

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> WorkSignal | None:
        """Pressure from the shortfall of unasked questions.

        The gap slot arms the *opportunity* to ask one and this worker
        supplies the *content*, so what it must never be is empty when a
        long absence ends. Stock is the only thing worth scheduling on.
        """
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        return self._cues.demand(needs_llm=self._ollama is not None)

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"drafted": 0, "disabled": True}
        now = _utcnow()
        if not self._cooldown_elapsed(now):
            return {"drafted": 0, "skipped_cooldown": True}
        # Stock is a veto here and not only a demand signal. ``demand()``
        # reporting zero pressure stops the scheduler *preferring* this
        # worker, but it is still admitted on its plain interval, so
        # without this the shelf grew to seven times its target and every
        # hour added one more near-identical question.
        stocked = self._cues.stock()
        if stocked >= self._cues.inventory_target:
            return {"drafted": 0, "skipped_stocked": True, "stocked": stocked}

        candidate = self._pick_candidate()
        if candidate is None:
            return {"drafted": 0, "no_candidate": True}

        user_name = self._resolve(self._user_display_name_provider) or "they"
        question = self._compose_question(user_name, candidate)
        if not question:
            return {"drafted": 0, "no_question": True}

        entry = {
            "at": now.isoformat(timespec="seconds"),
            "question": question,
            "source": candidate.source,
            "source_id": candidate.source_id,
            "source_at": candidate.source_at,
            "hers": candidate.hers,
        }
        append_question(
            self._kv_get, self._kv_set, entry, max_entries=self._journal_max,
        )
        # The subject is the topic she is curious about, not the phrasing
        # of the question -- the question is one wording of many, and post
        # turn has to recognise the subject however she ends up asking.
        cue_id = self._cues.publish(
            candidate.topic,
            render_question_cue(entry),
            payload={**entry, "topic": candidate.topic},
        )
        self._mark_fired(now)
        log.info(
            "forward_curiosity drafted: source=%s source_id=%s cue=%s",
            candidate.source,
            candidate.source_id,
            cue_id,
        )
        return {
            "drafted": 1,
            "source": candidate.source,
            "source_id": candidate.source_id,
            "question": question,
            "cue_id": cue_id,
        }

    # ── candidate selection ──────────────────────────────────────────

    def _pick_candidate(self) -> QuestionCandidate | None:
        from app.core.proactive.cue_store import normalise_subject

        # The ring only remembers the last few drafts; the pool remembers
        # every source it ever drafted from, which is what stops a plan
        # coming back around the moment it rotates out of the ring.
        already = self._recent_source_ids() | self._cues.claimed_sources()
        # The pool remembers subjects the ring has long since rotated out,
        # and across the terminal states too -- a question that was asked
        # and answered is not worth drafting again either.
        pooled = self._cues.spoken_for()
        candidates: list[QuestionCandidate] = []

        # Upcoming things the user mentioned (espresso machine, sister's
        # move, interview). These are the strongest forward-question
        # source: a concrete event with a "how did it go?" shape.
        for mem in self._safe_list_temporal("future_plan"):
            sid = str(getattr(mem, "id", "") or "")
            topic = (getattr(mem, "content", "") or "").strip()
            if not sid or not topic:
                continue
            if self._merged_away(mem) or self._lineage(mem) & already:
                continue
            if normalise_subject(topic) in pooled:
                continue
            candidates.append(
                QuestionCandidate(
                    source="future_plan",
                    source_id=sid,
                    topic=topic,
                    source_at=str(getattr(mem, "created_at", "") or ""),
                )
            )

        # Callbacks — things Aiko earlier flagged as worth circling back
        # to. Slightly weaker but still user-centred.
        for mem in self._safe_iter_kind("callback"):
            sid = str(getattr(mem, "id", "") or "")
            topic = (getattr(mem, "content", "") or "").strip()
            if not sid or not topic:
                continue
            if self._merged_away(mem) or self._lineage(mem) & already:
                continue
            if normalise_subject(topic) in pooled:
                continue
            candidates.append(
                QuestionCandidate(
                    source="callback",
                    source_id=sid,
                    topic=topic,
                    source_at=str(getattr(mem, "created_at", "") or ""),
                )
            )

        # K87: her own subject wonderings, written by the curiosity
        # worker's subject mode. The two pools above are both his life
        # by construction, so without this the worker could only ever
        # come back from a long absence with a question about him.
        user_name = self._resolve(self._user_display_name_provider)
        for mem in self._safe_iter_kind("open_question"):
            sid = str(getattr(mem, "id", "") or "")
            topic = (getattr(mem, "content", "") or "").strip()
            if not sid or not topic or sid in already:
                continue
            if is_person_directed(topic, user_name):
                continue
            if normalise_subject(topic) in pooled:
                continue
            candidates.append(
                QuestionCandidate(
                    source="wondering",
                    source_id=f"oq:{sid}",
                    topic=topic,
                    hers=True,
                    source_at=str(getattr(mem, "created_at", "") or ""),
                )
            )

        # L28: the concept layer. The three pools above are all memory
        # rows, which means a plan, a callback or a note was the only
        # thing she could be curious *about* -- so she could ask how an
        # event went, but never wonder whether a direction he is on still
        # holds, or bring up a taste of her own that nobody wrote a note
        # about.
        candidates.extend(self._concept_candidates(already, pooled))

        if not candidates:
            return None

        # MCP-forced source_id wins if it's among the live candidates.
        forced = self._forced_source_id
        self._forced_source_id = None
        if forced:
            for cand in candidates:
                if cand.source_id == forced:
                    return cand

        return self._rng.choice(self._quota_pool(candidates))

    def _concept_candidates(
        self, already: set[str], pooled: set[str],
    ) -> list[QuestionCandidate]:
        """Standing things she could wonder about, from her concept diet.

        Two things make this pool cheaper than a fourth source looks.

        **The lineage key already has a convention.** K87 namespaces its
        ids as ``oq:{sid}``, so a concept is ``concept:{cid}`` and flows
        through ``_recent_source_ids``, ``claimed_sources`` and
        ``spoken_for`` with no new dedupe axis. The memory-specific
        ``_merged_away`` / ``_lineage`` guards have no analogue and need
        none: the view only returns ``status == "active"`` rows, so a
        merged or retired concept is already gone.

        **The quota split already fits.** ``subject=aiko`` concepts join
        the subject side and everything else the person side, so K87's
        ``curiosity_subject_quota`` keeps governing the ratio.
        """
        from app.core.proactive.cue_store import normalise_subject

        if self._view_provider is None:
            return []
        try:
            view = self._view_provider()
        except Exception:
            log.debug("forward_curiosity view_provider raised", exc_info=True)
            return []
        if view is None or not getattr(view, "enabled", False):
            return []
        try:
            rows = view.for_consumer(self.name)
        except Exception:
            log.debug("forward_curiosity for_consumer failed", exc_info=True)
            return []
        out: list[QuestionCandidate] = []
        for concept in rows:
            cid = int(getattr(concept, "concept_id", 0) or 0)
            label = " ".join(str(getattr(concept, "label", "") or "").split())
            if cid <= 0 or not label:
                continue
            sid = f"concept:{cid}"
            if sid in already or normalise_subject(label) in pooled:
                continue
            out.append(
                QuestionCandidate(
                    source="concept",
                    source_id=sid,
                    topic=label,
                    hers=str(getattr(concept, "subject", "")) == "aiko",
                    # A concept has no single authoring moment the way a
                    # memory row does -- it is an abstraction over many --
                    # so there is no age for the deictic resolver to
                    # anchor on and nothing to hand it.
                )
            )
        return out

    def _quota_pool(
        self, candidates: list[QuestionCandidate],
    ) -> list[QuestionCandidate]:
        """Narrow the draw to one side of the K87 quota when it is owed.

        The recent journal is the history, so the ratio is measured over
        what actually got drafted rather than over what happened to be
        available. When the quota is satisfied the full pool is returned
        and the choice stays uniform, which keeps the person side from
        starving in the other direction.
        """
        quota = min(
            1.0,
            max(0.0, float(self._subject_quota_provider() or 0.0)),
        )
        subject = [c for c in candidates if c.hers]
        person = [c for c in candidates if not c.hers]
        if not subject or not person:
            return candidates
        history = [
            MODE_SUBJECT if is_hers(e) else MODE_PERSON
            for e in load_questions(self._kv_get)[-_DEDUP_LOOKBACK:]
        ]
        return subject if wants_subject(history, quota=quota) else person

    # ── question composition ─────────────────────────────────────────

    def _compose_question(
        self, user_name: str, candidate: QuestionCandidate,
    ) -> str:
        if candidate.source == "wondering":
            return self._compose_wondering(candidate)
        if candidate.source == "concept":
            return self._compose_concept(user_name, candidate)
        fallback = self._fallback_question(candidate.topic)
        if self._ollama is None or not self._model:
            return fallback
        hint = self._context_hint()
        hint_clause = f" What you know of them: {hint}." if hint else ""
        # K-time10: the note's age changes the question that fits. "How's
        # the interview prep going?" for a note from this morning becomes
        # "how did the interview go?" for one from three weeks ago.
        age = ""
        if timephrase.parse_iso(candidate.source_at) is not None:
            age = " (noted {})".format(
                timephrase.humanize_past(candidate.source_at, timephrase.utcnow())
            )
        prompt = (
            f"You are Aiko. Between conversations you've been wondering "
            f"about something in {user_name}'s life. Here's the note you "
            f"have: \"{candidate.topic}\"{age}.{hint_clause} Draft the gist "
            "of ONE warm, natural follow-up question you'd want to ask "
            f"{user_name} about it next time it fits — first person, no "
            "greeting, no preamble, ONE short question, no emoji. Keep it "
            "light and genuine, not an interrogation."
        )
        system = (
            timephrase.today_anchor()
            + " Phrase the question against the note's age -- ask "
            "retrospectively about something long past -- and do not use "
            "'today' / 'tonight' / 'tomorrow' in it, because the question "
            "waits in a queue before it is asked.\n\n"
            'Reply with JSON only: {"question": "<one short first-person '
            'question>"}.'
        )
        return self._ask_llm(system, prompt, fallback)

    def _compose_concept(
        self, user_name: str, candidate: QuestionCandidate,
    ) -> str:
        """L28: what she wants to know about a standing thing.

        The other two shapes both assume an event -- "how did it go?" for
        a plan, "here's what I've been chewing on" for a note. A concept
        is neither: it is a reading she has been carrying, so the question
        that fits is whether it still fits, and the interesting answer is
        the part that has moved.

        Split by whose it is, because the two are different acts. About
        him it is a check on her own model of him. About herself it is an
        offer, and the rest of the pipeline (the cue's framing, the wants
        ledger's ``share``) already reads ``hers`` that way.
        """
        topic = (candidate.topic or "").strip()
        if not topic:
            return ""
        if candidate.hers:
            fallback = f"where you've landed on {self._clip_topic(topic)}"
            prompt = (
                "You are Aiko. Here is something that is true of you: "
                f"\"{topic}\". It is a standing part of you rather than "
                "something that just happened, so what you have been "
                "turning over is where you are with it now -- what has "
                "grown, what you would say about it today. Draft ONE "
                "short first-person line you could open with about that "
                "-- no greeting, no preamble, no question mark, no emoji. "
                "Say something with content in it, not that the subject "
                "exists."
            )
        else:
            fallback = f"whether that's still true of {self._clip_topic(topic)}"
            prompt = (
                "You are Aiko. Here is something you hold about "
                f"{user_name}: \"{topic}\". It is a standing read of them "
                "rather than an event, so a 'how did it go?' does not "
                "fit -- what you actually want to know is whether it "
                "still holds, and what it looks like now. Draft the gist "
                "of ONE warm, natural question you'd want to ask "
                f"{user_name} about that next time it fits -- first "
                "person, no greeting, no preamble, ONE short question, no "
                "emoji. Curious, not an audit."
            )
        if self._ollama is None or not self._model:
            return fallback
        shape = (
            "line" if candidate.hers else "question"
        )
        system = (
            timephrase.today_anchor()
            + " This is about a standing thing with no date attached, so "
            "do not use 'today' / 'tonight' / 'tomorrow' -- it waits in a "
            "queue before it is said.\n\n"
            'Reply with JSON only: {"question": "<one short first-person '
            f'{shape}>"}}.'
        )
        return self._ask_llm(system, prompt, fallback)

    def _ask_llm(self, system: str, prompt: str, fallback: str) -> str:
        """One ``{"question": ...}`` draft, or ``fallback``.

        Shared by every composer that calls a model: the failure handling
        is the interesting part and it should not be written twice.
        """
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0.8, "num_predict": 80},
                format_json=True,
                surface="forward_curiosity",
            )
        except Exception:
            log.debug("forward_curiosity LLM compose failed", exc_info=True)
            return fallback
        try:
            blob = json.loads(content or "{}")
            line = str(blob.get("question") or "").strip()
        except Exception:
            line = ""
        return line or fallback

    @staticmethod
    def _clip_topic(topic: str, limit: int = 100) -> str:
        text = " ".join(str(topic).split())
        if len(text) <= limit:
            return text
        return text[: limit - 3].rsplit(" ", 1)[0] + "…"

    def _compose_wondering(self, candidate: QuestionCandidate) -> str:
        """K87: turn a subject note into something she can open with.

        The note is already a one-line instruction to her future self
        ("Maybe bring up that cold brew tastes better on the second
        day"), so the deterministic fallback here is simply stripping
        that prefix -- a model call would be spending tokens to
        paraphrase a sentence that already says the thing.
        """
        topic = (candidate.topic or "").strip()
        for prefix in ("maybe bring up that ", "maybe bring up "):
            if topic.lower().startswith(prefix):
                topic = topic[len(prefix):]
                break
        topic = topic.strip()
        if not topic:
            return ""
        if len(topic) > 140:
            topic = topic[:137].rsplit(" ", 1)[0] + "…"
        if topic[-1] not in ".!?":
            topic += "."
        return topic[0].upper() + topic[1:]

    def _fallback_question(self, topic: str) -> str:
        snippet = (topic or "").strip()
        if len(snippet) > 100:
            snippet = snippet[:97].rsplit(" ", 1)[0] + "…"
        return f"how {snippet} is going" if snippet else ""

    def _context_hint(self) -> str:
        """Context for the question, concepts first and profile as floor.

        The hint used to be two flat K3 profile strings (``routines``,
        ``usual_hours``), which tell a model when he is usually around and
        nothing about what he is like -- so every question came out shaped
        by the clock. L28's compose-first stance puts the concept layer
        ahead of the derived fields: what moves him, what he is heading
        toward, what he enjoys. The profile fields stay as the floor,
        because a cold concept layer should leave the hint no worse than
        it was.
        """
        parts = list(self._concept_hint_lines())
        routines = self._profile_routines()
        if routines:
            parts.append(routines)
        return "; ".join(parts)

    def _concept_hint_lines(self) -> list[str]:
        """Up to three of his concepts from the diet, strongest first.

        Deliberately short. This is background for phrasing one question,
        not a briefing: the diet's own budget governs how much is read,
        and only the leading few are worth spending prompt on.
        """
        if self._view_provider is None:
            return []
        try:
            view = self._view_provider()
        except Exception:
            log.debug("forward_curiosity view_provider raised", exc_info=True)
            return []
        if view is None or not getattr(view, "enabled", False):
            return []
        try:
            rows = view.for_consumer(self.name, subject="user")
        except Exception:
            log.debug("forward_curiosity hint read failed", exc_info=True)
            return []
        out: list[str] = []
        for concept in rows[:3]:
            label = " ".join(str(getattr(concept, "label", "") or "").split())
            if label:
                out.append(self._clip_topic(label, 120))
        return out

    def _profile_routines(self) -> str:
        store = self._user_profile_store
        if store is None:
            return ""
        try:
            user_id = self._resolve(self._user_id_provider)
            if not user_id:
                return ""
            fields = store.fields(user_id)
        except Exception:
            return ""
        parts: list[str] = []
        for key in ("routines", "usual_hours"):
            entry = fields.get(key)
            value = (getattr(entry, "value", "") or "").strip() if entry else ""
            if value:
                parts.append(value)
        return "; ".join(parts)

    # ── gates ────────────────────────────────────────────────────────

    def _recent_source_ids(self) -> set[str]:
        ring = load_questions(self._kv_get)
        recent = ring[-_DEDUP_LOOKBACK:] if ring else []
        return {
            str(e.get("source_id"))
            for e in recent
            if e.get("source_id")
        }

    @staticmethod
    def _merged_away(mem: "Memory") -> bool:
        """Did K35 fold this row into another one?

        Such a row is a duplicate the consolidation worker already
        resolved; the surviving primary is in the same candidate list and
        speaks for the whole group.
        """
        meta = getattr(mem, "metadata", None) or {}
        return bool(meta.get("consolidated_into"))

    @staticmethod
    def _lineage(mem: "Memory") -> set[str]:
        """This row's id plus the ids it absorbed.

        A question drafted from one of the absorbed rows before the merge
        happened is the same question, so the survivor inherits their
        claims along with their content.
        """
        ids = {str(getattr(mem, "id", "") or "")}
        meta = getattr(mem, "metadata", None) or {}
        absorbed = meta.get("source_ids")
        if isinstance(absorbed, list):
            ids.update(str(i) for i in absorbed if i is not None)
        return {i for i in ids if i}

    def _cooldown_elapsed(self, now: datetime) -> bool:
        if self._cooldown_seconds <= 0:
            return True
        last = _parse_iso(self._kv_get_safe(_KV_LAST_FIRED_AT))
        if last is None:
            return True
        return (now - last).total_seconds() >= self._cooldown_seconds

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def _mark_fired(self, now: datetime) -> None:
        self._kv_set_safe(_KV_LAST_FIRED_AT, now.isoformat(timespec="seconds"))

    # ── helpers ──────────────────────────────────────────────────────

    def force_source(self, source_id: str | None) -> None:
        """Arm a specific source_id for the next ``run()`` (MCP debug)."""
        self._forced_source_id = source_id

    def _safe_list_temporal(self, temporal_type: str) -> list["Memory"]:
        try:
            return self._memory_store.list_by_temporal_type(temporal_type)
        except Exception:
            log.debug(
                "forward_curiosity list %s failed", temporal_type,
                exc_info=True,
            )
            return []

    def _safe_iter_kind(self, kind: str) -> list["Memory"]:
        try:
            return self._memory_store.iter_by_kind(kind)
        except Exception:
            log.debug(
                "forward_curiosity iter %s failed", kind, exc_info=True,
            )
            return []

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
                "forward_curiosity kv_set failed key=%s", key, exc_info=True,
            )

    def _resolve(self, provider: Callable[[], str]) -> str:
        try:
            return str(provider() or "").strip()
        except Exception:
            return ""


__all__ = [
    "ForwardCuriosityWorker",
    "QuestionCandidate",
    "FORWARD_CURIOSITY_JOURNAL_KEY",
    "is_hers",
    "load_questions",
    "append_question",
    "render_question_cue",
]
