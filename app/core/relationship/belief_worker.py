"""Belief inference worker (K2 personality backlog).

Periodic background worker that mines Aiko's recent conversation for
fresh theory-of-mind beliefs about the user -- mood predictions
("Jacob is excited about the tokyo trip") and topical opinions
("Jacob thinks Rust is overhyped") -- and upserts them into the
:class:`app.core.relationship.belief_store.BeliefStore`.

Pipeline (one tick = one ``run`` call):

1. Snapshot the last ``belief_worker_lookback_turns`` (default 12)
   user messages from the active session via
   :meth:`ChatDatabase.get_messages`, each prefixed with the user's
   name so the extractor knows whose "I"/"you" it's reading.
2. Optionally privacy-scrub the lookback transcript via
   :func:`fact_check_privacy.scrub_claim_for_search` (only when
   ``belief_worker_scrub_transcript`` is on -- off by default because
   the extractor runs on the trusted LOCAL model and the scrubber would
   strip the pronouns/names the theory-of-mind pass depends on).
3. Spend one LLM call through the dedicated
   :class:`FactCheckRateLimiter`
   (``state_key='belief_worker.rate_state'``) asking for a JSON
   **object** ``{"beliefs": [...]}`` of belief tuples ``{kind, topic,
   predicted_state, confidence}``. Object, not bare array, because
   ``format: "json"`` gives no choice -- see
   :mod:`app.llm.json_answers`.
4. For each accepted tuple: compute a topic embedding via the
   provided :class:`Embedder`, then call
   :meth:`BeliefStore.upsert`. The store handles its own
   (user_id, kind, topic) dedupe + fuzzy-topic merge.
5. Cap the user's active-belief count at
   ``belief_max_active_per_user`` via
   :meth:`BeliefStore.prune_to_cap`.

**The shape contract on the two text fields.** ``topic`` and
``predicted_state`` are not free text: every consumer reads them back
inside a sentence (see
:func:`~app.core.relationship.belief_gap_detector.render_inner_life_block`
— "You had {name} pegged as {state} about {topic}"), and ``topic`` is
additionally embedded for retrieval and fuzzy merge. So a state has to
complete "{name} is ___" in the present tense, and a topic has to be a
subject that can come up again. Neither may carry a date: the row
already has ``observed_at``, and a dated claim about a past evening is
one nothing can ever confirm or contradict, so it occupies the active
set until the cap prunes it. The prompt states both frames literally and
:func:`state_fault` / :func:`topic_fault` enforce them, because the only
thing the old "2-80 char state phrase" instruction bounded was length.

The self-tag fast path (``[[predict:...]]``) wins over the worker:
:meth:`BeliefStore.upsert` for an existing ``self_tag`` row simply
refreshes its state; the worker writes ``source='worker'`` for
brand-new beliefs only. Higher-confidence self-tag rows are not
overwritten by lower-confidence worker rows because ``upsert``
always overwrites with the latest value -- so we apply the
self-tag wins guard inside the worker loop (skip upserting if a
self-tagged active belief already exists for the topic).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.relationship.belief_store import (
    BeliefStore,
    KIND_MOOD,
    KIND_OPINION,
    SOURCE_SELF_TAG,
    SOURCE_WORKER,
    VALID_KINDS,
)
from app.core.memory.fact_check_privacy import scrub_claim_for_search
from app.core.proactive.idle_worker import WorkSignal, pressure_from_count
from app.core.infra import timephrase
from app.llm.json_answers import parse_json_array_answer

if TYPE_CHECKING:
    from app.core.concepts.concept_view import ConceptView
    from app.core.infra.chat_database import ChatDatabase
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.infra.settings import AgentSettings
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.belief_worker")


# Cap on how much of any text we render in a single log line. Mirrors
# the F1 / F5 / G3 worker convention.
_LOG_PREVIEW_CHARS = 200


# Cap on the LLM response so a malformed answer can't run away with
# the budget. ~6 beliefs * ~50 chars each = ~300 tokens.
_EXTRACT_MAX_TOKENS = 350


# Maximum entries we accept from one extraction pass, regardless of
# what the model returns. Tuned so a single noisy turn can't flood
# the store; the next tick picks up anything we dropped.
_MAX_BELIEFS_PER_RUN = 6


# L28: how many durable concept labels reach the extraction prompt. The
# diet's token budget governs how much is *read*; this is how much is
# worth spending prompt on. A prior only has to point the extractor
# somewhere -- a long list of them starts competing with the transcript
# that is supposed to decide.
_MAX_CONCEPT_HINTS = 5


# Ask for an OBJECT wrapping the array, matching the memory extractor.
# Ollama's ``format: "json"`` constrains generation to a JSON object, so a
# prompt asking for a bare array cannot be satisfied: the model reasons to
# the right answer and the grammar then emits ``{}``. See
# :mod:`app.llm.json_answers`.
def _build_system_prompt(user_name: str = "the user") -> str:
    """Belief-extraction system prompt, naming the user in the frames.

    The two frames quoted below are literal: they are what
    :func:`~app.core.relationship.belief_gap_detector.render_inner_life_block`
    actually renders, and ``predicted_state`` is dropped into the blank.
    Quoting them is what turns the shape requirement into something the
    model can check by reading its own answer back. Asked instead for
    "a 2-80 char state phrase" it produced ``experienced mild evening
    frustration and low energy on august 12 2026`` — inside the length
    budget, and unusable in either frame.
    """
    name = (user_name or "").strip() or "the user"
    return (
        f"You read a short chat transcript and infer what {name} "
        "believes or feels about specific topics. Reply with ONE JSON "
        'object (no prose, no markdown) shaped {"beliefs": [...]} '
        "holding zero or more belief objects. Each object in that array "
        "has these fields:\n"
        "  - kind: 'mood' (how they feel about the topic) or 'opinion' "
        "(what they think about the topic).\n"
        "  - topic: 2-60 char lowercase subject phrase, no quotes. A "
        "lasting subject that could come up again next month ('the "
        "tokyo trip', 'rust', 'his workload') -- never an occasion or a "
        "date ('tuesday evening', 'the meeting on the 14th'). A topic "
        "nobody can raise again is a belief nobody can ever check.\n"
        "  - predicted_state: 2-80 char phrase. It gets read back "
        "INSIDE a sentence, so write only the part that completes the "
        "blank:\n"
        f"      mood    -> \"{name} is ___ about <topic>\"\n"
        "      opinion -> \"<topic> is ___\"\n"
        "    So an adjective or a noun phrase, never a new clause: it "
        "must not start with a verb or with 'he' / 'she' / 'it', and it "
        "must not repeat the subject or the topic. 'frustrated and low "
        "on energy', not 'experienced mild evening frustration'; "
        "'overhyped', not 'thinks rust is overhyped'; 'restorative', "
        "not 'finds the quiet evenings restorative'; 'a clever idea', "
        "not 'he said it was a clever idea'. Read your phrase back "
        "inside the frame before you answer -- if it does not make one "
        "grammatical sentence, rewrite it. Keep it under about eight "
        "words.\n"
        "  - confidence: 0.0-1.0 decimal -- how sure you are.\n"
        "A belief is a standing read on a person, so skip anything that "
        "is really a scheduled plan or a single event ('meeting on "
        "friday', 'went to bed early'). Those are commitments and "
        "history; other parts of the system record them, and stored "
        "here they can never be confirmed or contradicted.\n"
        "Be conservative: skip the turn if the transcript doesn't "
        "actually let you predict anything. Never invent beliefs from "
        'thin air. Return {"beliefs": []} when there\'s nothing to '
        "report. Output the JSON object and nothing else."
    )


# ``{{`` / ``}}`` because this template goes through ``str.format`` — a
# literal ``{"beliefs"`` would be read as a replacement field and raise
# ``KeyError`` on every run.
_USER_TEMPLATE = (
    "Transcript (most recent user turns):\n{transcript}\n\n"
    'Return one JSON object shaped {{"beliefs": [...]}}.'
)


# Field names that mark a belief object, so a single unwrapped item is
# still understood. See :mod:`app.llm.json_answers`.
_BELIEF_ITEM_KEYS = ("kind", "topic", "predicted_state")


def _utcnow() -> datetime:
    return timephrase.utcnow()


def _now_iso() -> str:
    return _utcnow().isoformat()


def _preview(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= _LOG_PREVIEW_CHARS:
        return s
    return s[: _LOG_PREVIEW_CHARS - 1] + "\u2026"


def _coerce_labels(raw: Any) -> list[str]:
    """Normalise an interest-map payload to an ordered list of labels.

    Accepts anything iterable whose items are bare label strings,
    ``(label, size)`` tuples, or objects exposing a ``.label`` attribute
    (e.g. :class:`app.core.conversation.topic_graph.InterestEntry`).
    Blank labels are dropped; order + de-dupe are preserved so the
    caller's "densest first" ordering survives.
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    try:
        items = list(raw)
    except TypeError:
        return []
    for item in items:
        label: Any = None
        if isinstance(item, str):
            label = item
        elif hasattr(item, "label"):
            label = item.label
        elif isinstance(item, (tuple, list)) and item:
            label = item[0]
        text = str(label or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


# Tiny tokenizer for the reconsider-candidate overlap. Words of length
# >= 3 only, so trivial joiners ("the", "a", "of") don't manufacture a
# match between an interest label and a belief topic.
_TOPIC_WORD_RE = re.compile(r"[a-z0-9]{3,}")


def _topic_words(text: str | None) -> set[str]:
    return set(_TOPIC_WORD_RE.findall((text or "").lower()))


# A date or a weekday written into a topic or a state, which the row's
# own ``observed_at`` already records. In a field that is re-read as a
# present-tense claim this is not redundancy, it is a category error: it
# turns "how he feels about X" into "what happened on the 12th", which
# nothing can later agree or disagree with.
_DATE_RE = re.compile(
    r"\b(?:"
    r"jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
    r"|january|february|march|april|june|july|august|september"
    r"|october|november|december"
    r"|mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|yesterday|tomorrow|tonight"
    r")\b"
    r"|\b(?:19|20)\d{2}\b"
    r"|\b\d{1,2}(?:st|nd|rd|th)\b",
    re.I,
)


# Openers that cannot follow "is". A state phrase completes a frame, so
# it has to be a complement — an adjective ("frustrated"), a noun phrase
# ("a clever idea"), a participle ("looking forward to it"). A *finite
# verb* cannot: it makes the state a second predicate, and the render
# comes out as "is finds deep emotional recharge" or "is experienced mild
# evening frustration". A pronoun opener fails the same way ("is he hopes
# for deeper trust") and additionally re-states the subject the frame
# already supplies.
#
# Curated from what the store actually contained rather than derived,
# because telling a finite verb from a participle needs POS tagging we
# do not have here, and a guess in either direction is worse than a list:
# too eager and the worker stores nothing, too shy and the frame breaks.
# Every entry below appeared in a real row. Keep it that way — add a word
# when a row shows up carrying it, not on suspicion.
_PAST_OPENERS: frozenset[str] = frozenset(
    {
        "experienced", "felt", "had", "was", "were", "got", "went",
        "became", "expressed", "showed", "reported", "described",
        "mentioned", "said", "told", "asked", "spent", "took", "made",
        "started", "finished", "decided", "struggled", "seemed",
    }
)

_PRESENT_OPENERS: frozenset[str] = frozenset(
    {
        "finds", "likes", "loves", "hates", "values", "views", "hopes",
        "prefers", "wants", "needs", "thinks", "believes", "feels",
        "seeks", "enjoys", "considers", "sees", "treats", "keeps",
        "tends", "plans", "expects", "trusts", "appreciates", "dislikes",
        "find", "like", "value", "view", "hope", "prefer", "want",
        "need", "think", "believe", "feel", "seek", "enjoy", "consider",
    }
)

_PRONOUN_OPENERS: frozenset[str] = frozenset(
    {"he", "she", "they", "it", "i", "you", "we", "his", "her", "their"}
)


# How many words a state phrase may run to before it is a sentence
# rather than a complement. The prompt asks for "under about eight"; the
# gate allows a little slack so a legitimately long-but-well-shaped
# phrase ("quietly proud of how the refactor landed") survives.
_MAX_STATE_WORDS = 12


_WORD_RE = re.compile(r"[a-z0-9']+", re.I)


def state_fault(state: str) -> str:
    """Name why ``state`` cannot complete the frame, or ``""`` if it can.

    The mirror of :func:`~app.core.memory.promise_worker._is_low_quality`,
    and there for the same reason: a prompt is guidance, and the store is
    what everything downstream reads. A model that ignores the shape
    instruction should cost us one dropped tuple on one tick, not a
    permanent row that renders as "You had Jacob pegged as experienced
    mild evening frustration and low energy on august 12 2026 about
    daily routine and motivation".

    Returns a short reason string suitable for a log line, so the drops
    are countable rather than invisible.
    """
    text = (state or "").strip()
    if not text:
        return "empty"
    words = _WORD_RE.findall(text.lower())
    if not words:
        return "empty"
    if _DATE_RE.search(text):
        return "dated"
    head = words[0]
    if head in _PAST_OPENERS:
        return f"past_tense:{head}"
    if head in _PRESENT_OPENERS:
        return f"finite_verb:{head}"
    if head in _PRONOUN_OPENERS:
        return f"restates_subject:{head}"
    if len(words) > _MAX_STATE_WORDS:
        return f"sentence:{len(words)}w"
    return ""


def topic_fault(topic: str) -> str:
    """Name why ``topic`` is not a subject anyone can raise again."""
    text = (topic or "").strip()
    if not text:
        return "empty"
    if _DATE_RE.search(text):
        return "dated"
    return ""


@dataclass(slots=True)
class _BeliefTuple:
    """One belief returned by the LLM extractor."""

    kind: str
    topic: str
    predicted_state: str
    confidence: float


class BeliefInferenceWorker:
    """IdleWorker that mines recent turns for theory-of-mind beliefs."""

    name = "belief_worker"

    def __init__(
        self,
        *,
        belief_store: BeliefStore,
        chat_db: "ChatDatabase",
        embedder: "Embedder",
        ollama: "OllamaClient",
        chat_model: str,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        belief_settings: Any,
        # The **scoped** ``user_id:session_id`` key, not the bare session
        # id: that is what ``messages.session_id`` actually holds, and
        # every chat-db read here keys on it exactly. Wired to the bare id
        # this worker finds no transcript on any run, forever, while
        # reporting the benign-looking "no recent user turns".
        session_key_provider: Callable[[], str | None],
        user_id_provider: Callable[[], str],
        user_names_provider: Callable[[], list[str]] | None = None,
        assistant_name_provider: Callable[[], str | None] | None = None,
        notify_belief_added: Callable[[dict[str, Any]], None] | None = None,
        notify_belief_updated: Callable[[dict[str, Any]], None] | None = None,
        interest_map_provider: Callable[[], Any] | None = None,
        view_provider: Callable[[], "ConceptView | None"] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._belief_store = belief_store
        self._chat_db = chat_db
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._belief_settings = belief_settings
        self._session_key_provider = session_key_provider
        self._user_id_provider = user_id_provider
        self._user_names_provider = user_names_provider
        self._assistant_name_provider = assistant_name_provider
        self._notify_belief_added = notify_belief_added
        self._notify_belief_updated = notify_belief_updated
        # K65b: returns the K9 interest map (densest topic clusters). Any
        # iterable of items exposing ``.label`` / ``.size`` or ``(label,
        # size)`` tuples / bare label strings is accepted; ``None`` /
        # missing keeps the legacy flat-transcript behaviour.
        self._interest_map_provider = interest_map_provider
        # L28: late-bound ConceptView, read once per run for the durable
        # prior. ``None`` leaves the prompt exactly as K65b built it.
        self._view_provider = view_provider
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._belief_settings,
                "belief_worker_interval_seconds",
                3600,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        """Both enable flags, plus budget for the extraction call."""
        return self._fresh_turns(now, last_run_at) is not None

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Pressure from transcript that has not been mined yet.

        This worker has no backlog to count — it re-reads the last
        dozen turns and asks a model what the user believes. Run it
        against an unchanged transcript and it spends a generation to
        re-derive tuples it already has, so the honest signal is *new
        material*: how many messages have landed since the last run,
        against the lookback window the extraction actually reads.

        A session with no new turns reports zero and rides the
        heartbeat, which is what re-mines the window occasionally in
        case a later turn recontextualises an earlier one.
        """
        fresh = self._fresh_turns(now, last_run_at)
        if fresh is None:
            return WorkSignal(pressure=0.0, reason="disabled or no budget")
        new_messages, window = fresh
        if new_messages <= 0:
            return WorkSignal(pressure=0.0, reason="no new turns")
        return WorkSignal(
            pressure=pressure_from_count(new_messages, saturation=window),
            reason=f"{new_messages} new messages",
            needs_llm=True,
        )

    def _fresh_turns(
        self, now: datetime, last_run_at: datetime | None,
    ) -> tuple[int, int] | None:
        """``(new_messages, window)`` if a run could extract, else ``None``.

        ``snapshot`` rather than ``allow``: ``run`` spends the token,
        a probe never may.
        """
        if not bool(
            getattr(self._agent_settings, "belief_tracking_enabled", True)
        ):
            return None
        if not bool(
            getattr(self._agent_settings, "belief_worker_enabled", True)
        ):
            return None
        lookback_turns = int(
            getattr(
                self._belief_settings, "belief_worker_lookback_turns", 12,
            )
        )
        if lookback_turns <= 0:
            return None
        try:
            snapshot = self._rate_limiter.snapshot(now)
            if snapshot["hour_used"] >= snapshot["hour_cap"]:
                return None
            if snapshot["day_used"] >= snapshot["day_cap"]:
                return None
            session_key = (
                self._session_key_provider()
                if self._session_key_provider else None
            )
            if not session_key:
                return None
            fresh = self._chat_db.count_messages_since(
                session_key, last_run_at,
            )
        except Exception:
            log.debug("belief-worker demand probe failed", exc_info=True)
            return None
        return fresh, max(1, lookback_turns * 2)

    def run(self) -> dict[str, Any]:
        if not bool(
            getattr(self._agent_settings, "belief_tracking_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled_tracking"}
        if not bool(
            getattr(self._agent_settings, "belief_worker_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled_worker"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        session_key = (
            self._session_key_provider() if self._session_key_provider else None
        )
        if not session_key:
            return {"skipped": True, "reason": "no_session"}

        lookback_turns = int(
            getattr(
                self._belief_settings,
                "belief_worker_lookback_turns",
                12,
            )
        )
        if lookback_turns <= 0:
            return {"skipped": True, "reason": "lookback_zero"}

        now = self._clock()
        transcript = self._snapshot_transcript(
            session_key=session_key, lookback_turns=lookback_turns,
        )
        if not transcript:
            # An empty window is normal; a session key that matches *no*
            # message ever is a wiring fault, and the two look identical
            # from here. Separating them costs one COUNT on a path that
            # already decided to do nothing, and the un-separated version
            # hid a mis-scoped key for three months.
            if self._session_is_unknown(session_key):
                log.warning(
                    "belief-worker: session key %r matches no messages at "
                    "all -- expected the scoped 'user_id:session_id' form. "
                    "The worker cannot mine anything until this is fixed.",
                    session_key,
                )
                return {"skipped": True, "reason": "unknown_session"}
            log.info(
                "belief-worker skip: no recent user turns session=%s",
                session_key,
            )
            return {"skipped": True, "reason": "no_user_turns"}

        # Rate-limit gate.
        if not self._rate_limiter.allow(now):
            log.info(
                "belief-worker skip: rate-limited session=%s",
                session_key,
            )
            return {"skipped": True, "reason": "rate_limited"}

        user_names = self._user_names_provider() if self._user_names_provider else None
        assistant_name = (
            self._assistant_name_provider() if self._assistant_name_provider else None
        )
        # The belief extractor runs on the LOCAL maintenance model, which
        # the privacy threat model treats as trusted. Scrubbing the
        # transcript with the outbound web-search PII gate would strip the
        # first/second-person pronouns + names the theory-of-mind pass
        # relies on, so it's off by default. Opt in only when the worker
        # model is routed to an untrusted endpoint.
        if bool(
            getattr(self._agent_settings, "belief_worker_scrub_transcript", False)
        ):
            scrubbed = scrub_claim_for_search(
                transcript,
                user_names=user_names,
                assistant_name=assistant_name,
            )
            if not scrubbed:
                log.info(
                    "belief-worker skip: privacy-blocked transcript session=%s "
                    "raw_chars=%d",
                    session_key,
                    len(transcript),
                )
                return {"skipped": True, "reason": "privacy_blocked"}
        else:
            scrubbed = transcript

        log.info(
            "belief-worker start: session=%s lookback_turns=%d "
            "raw_chars=%d scrubbed_chars=%d preview=%r",
            session_key,
            lookback_turns,
            len(transcript),
            len(scrubbed),
            _preview(scrubbed),
        )

        # K65b: bias extraction toward the user's densest K9 interests and
        # fold a few "still true?" re-checks into the *same* LLM call. Both
        # are best-effort and degrade to the legacy flat prompt on a cold
        # store (no labelled clusters / no active beliefs).
        interest_labels = self._interest_labels()
        interest_hint = ""
        reconsider_block = ""
        reconsider_count = 0
        if interest_labels:
            clean_labels = self._scrub_terms(
                interest_labels, user_names, assistant_name,
            )
            if clean_labels:
                interest_hint = ", ".join(clean_labels)
            reconsider_topics = self._reconsider_topics(
                user_id=self._user_id_provider(), labels=interest_labels,
            )
            clean_reconsider = self._scrub_terms(
                reconsider_topics, user_names, assistant_name,
            )
            reconsider_count = len(clean_reconsider)
            if clean_reconsider:
                reconsider_block = "; ".join(clean_reconsider)
            if interest_hint or reconsider_block:
                log.info(
                    "belief-worker interest-bias: interests=%d reconsider=%d",
                    len(interest_labels),
                    reconsider_count,
                )

        # L28: the durable layer as a third prior. K2 beliefs stay
        # transient -- nothing here touches the store, its confidence
        # semantics or its lifecycle; the concepts only shape what the
        # extractor goes looking for.
        concept_hint = self._concept_hint(user_names, assistant_name)
        if concept_hint:
            log.info(
                "belief-worker concept-bias: chars=%d", len(concept_hint),
            )

        t0 = time.monotonic()
        dropped_shape: list[str] = []
        tuples = self._extract_with_llm(
            scrubbed,
            interest_hint=interest_hint,
            reconsider_block=reconsider_block,
            concept_hint=concept_hint,
            dropped=dropped_shape,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if tuples is None:
            log.info(
                "belief-worker llm-unparseable elapsed_ms=%.0f session=%s",
                elapsed_ms,
                session_key,
            )
            return {
                "skipped": True,
                "reason": "llm_unparseable",
                "llm_ms": round(elapsed_ms, 1),
            }

        log.info(
            "belief-worker llm done: tuples=%d elapsed_ms=%.0f",
            len(tuples),
            elapsed_ms,
        )

        upserted = 0
        skipped_self_tag = 0
        dropped_invalid = 0
        user_id = self._user_id_provider()
        for t in tuples[:_MAX_BELIEFS_PER_RUN]:
            if self._cancel_event.is_set():
                break
            if t.kind not in VALID_KINDS:
                dropped_invalid += 1
                continue
            # Self-tag wins guard: if Aiko already self-tagged a belief
            # for this exact (kind, topic) and it's active, leave it
            # alone -- her deliberate guess outranks the worker's
            # inference.
            existing = self._belief_store.list_recent(
                user_id=user_id,
                kind=t.kind,
                limit=1,
            )
            if existing:
                row = existing[0]
                if (
                    row.topic == t.topic.lower()
                    and row.status == "active"
                    and row.source == SOURCE_SELF_TAG
                    and row.confidence >= t.confidence
                ):
                    skipped_self_tag += 1
                    continue
            embedding = None
            try:
                embedding = self._embedder.embed(t.topic)
            except Exception:
                log.debug(
                    "belief-worker: embedder raised for topic=%r",
                    t.topic,
                    exc_info=True,
                )
            belief = self._belief_store.upsert(
                user_id=user_id,
                kind=t.kind,
                topic=t.topic,
                predicted_state=t.predicted_state,
                confidence=float(t.confidence),
                source=SOURCE_WORKER,
                topic_embedding=embedding,
                observed_at=now.isoformat(),
            )
            if belief is None:
                dropped_invalid += 1
                continue
            upserted += 1
            log.info(
                "belief-worker upsert: id=%s kind=%s topic=%r state=%r "
                "confidence=%.2f",
                belief.id,
                belief.kind,
                belief.topic,
                belief.predicted_state,
                belief.confidence,
            )
            if self._notify_belief_added is not None:
                try:
                    self._notify_belief_added(belief.to_payload())
                except Exception:
                    log.debug(
                        "belief-worker: notify_belief_added raised",
                        exc_info=True,
                    )

        # Prune any per-user excess. Cap is a hard ceiling on
        # ``active`` rows; we don't touch confirmed / contradicted /
        # stale audit history.
        cap = int(
            getattr(
                self._belief_settings,
                "belief_max_active_per_user",
                200,
            )
        )
        pruned = self._belief_store.prune_to_cap(user_id=user_id, cap=cap)
        if pruned:
            log.info(
                "belief-worker pruned %d rows to cap=%d for user=%s",
                pruned,
                cap,
                user_id,
            )

        result = {
            "tuples_returned": len(tuples),
            "upserted": upserted,
            "skipped_self_tag": skipped_self_tag,
            "dropped_invalid": dropped_invalid,
            "dropped_shape": len(dropped_shape),
            "pruned": pruned,
            "llm_ms": round(elapsed_ms, 1),
        }
        if dropped_shape:
            # Reasons, not just a count: "the model keeps writing dates"
            # and "the model keeps writing clauses" want different fixes,
            # and the prompt is where either is actually fixed.
            result["shape_faults"] = sorted(
                {f.split(":")[0] for f in dropped_shape}
            )
        log.info("belief-worker done: %s", result)
        return result

    # ── K65b: interest-map bias helpers ──────────────────────────────

    def _interest_labels(self) -> list[str]:
        """Top high-mass K9 cluster labels, or ``[]`` when disabled/cold."""
        if not bool(
            getattr(self._agent_settings, "belief_interest_bias_enabled", True)
        ):
            return []
        if self._interest_map_provider is None:
            return []
        top_n = int(
            getattr(self._belief_settings, "belief_worker_interest_top_n", 5)
        )
        if top_n <= 0:
            return []
        try:
            raw = self._interest_map_provider()
        except Exception:
            log.debug(
                "belief-worker: interest_map_provider raised", exc_info=True
            )
            return []
        return _coerce_labels(raw)[:top_n]

    def _reconsider_topics(
        self, *, user_id: str, labels: list[str]
    ) -> list[str]:
        """Stalest believed topics that sit on a top interest.

        ``list_believed`` is ``observed_at DESC`` so we walk it in reverse
        (oldest-observed first) and keep the first ``reconsider_max`` whose
        topic shares a content word with any high-mass interest label.
        Corroborated beliefs are included: they are the ones Aiko actually
        says out loud, so they are the ones worth re-checking.
        """
        max_n = int(
            getattr(self._belief_settings, "belief_worker_reconsider_max", 3)
        )
        if max_n <= 0 or not labels:
            return []
        try:
            active = self._belief_store.list_believed(user_id=user_id, limit=200)
        except Exception:
            log.debug("belief-worker: list_believed raised", exc_info=True)
            return []
        if not active:
            return []
        label_words = _topic_words(" ".join(labels))
        if not label_words:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for belief in reversed(active):
            topic = (getattr(belief, "topic", "") or "").strip()
            key = topic.lower()
            if not topic or key in seen:
                continue
            if _topic_words(topic) & label_words:
                out.append(topic)
                seen.add(key)
                if len(out) >= max_n:
                    break
        return out

    def _concept_hint(
        self,
        user_names: list[str] | None,
        assistant_name: str | None,
    ) -> str:
        """L28: what she durably holds about him, as an extraction prior.

        The two K65b hints are both *topic* signals -- which subjects he
        keeps returning to and which earlier beliefs are due a re-check.
        Neither says anything about what he is *like*, so the extractor
        infers a passing mood with no model of the person it is inferring
        about, and the same transcript reads the same way whether he is
        someone who withdraws under pressure or someone who gets louder.

        Scrubbed on the same path as the interest labels, because a
        concept label is free text about a named person and the worker may
        be routed to an untrusted endpoint.

        Deliberately one-directional: this reads concepts and writes
        nothing back. K2's transient half stays transient -- a belief is a
        prediction about right now, and promoting one on the strength of
        agreeing with a durable concept is how a layer starts confirming
        itself.
        """
        if self._view_provider is None:
            return ""
        try:
            view = self._view_provider()
        except Exception:
            log.debug("belief-worker: view_provider raised", exc_info=True)
            return ""
        if view is None or not getattr(view, "enabled", False):
            return ""
        try:
            rows = view.for_consumer("belief_inference")
        except Exception:
            log.debug("belief-worker: for_consumer failed", exc_info=True)
            return ""
        labels = [
            " ".join(str(getattr(c, "label", "") or "").split())
            for c in rows[:_MAX_CONCEPT_HINTS]
        ]
        clean = self._scrub_terms(
            [line for line in labels if line], user_names, assistant_name,
        )
        return "; ".join(clean)

    def _scrub_terms(
        self,
        terms: list[str],
        user_names: list[str] | None,
        assistant_name: str | None,
    ) -> list[str]:
        """Privacy-scrub each short term; drop any that scrub to empty."""
        out: list[str] = []
        for term in terms:
            s = (term or "").strip()
            if not s:
                continue
            scrubbed = scrub_claim_for_search(
                s, user_names=user_names, assistant_name=assistant_name,
            )
            if scrubbed and scrubbed.strip():
                out.append(scrubbed.strip())
        return out

    # ── transcript snapshot ──────────────────────────────────────────

    def _resolve_user_name(self) -> str:
        """First configured user name, or ``"the user"`` fallback."""
        names = (
            self._user_names_provider() if self._user_names_provider else None
        )
        if names:
            first = (str(names[0]) or "").strip()
            if first:
                return first
        return "the user"

    def _session_is_unknown(self, session_key: str) -> bool:
        """True when the key names a session the message store never saw."""
        try:
            return int(self._chat_db.get_message_count(session_key)) <= 0
        except Exception:
            log.debug("belief-worker session probe failed", exc_info=True)
            return False

    def _snapshot_transcript(
        self,
        *,
        session_key: str,
        lookback_turns: int,
    ) -> str:
        """Join the last N user messages into one speaker-attributed block.

        Assistant turns are intentionally omitted: the worker mines
        user beliefs, not Aiko's own speech. Each line is prefixed with
        the user's name so the extractor knows first-person "I"/"me" is
        the user and "you" addresses Aiko -- the deictic anchor that
        makes "what does the user believe" resolvable. We cap each user
        message at 600 chars so a long rant can't blow the budget.
        """
        rows = self._chat_db.get_messages(session_key, limit=lookback_turns * 2)
        user_msgs = [r for r in rows if r.role == "user"]
        if not user_msgs:
            return ""
        user_msgs = user_msgs[-lookback_turns:]
        user_name = self._resolve_user_name()
        # K-time10: age-prefixed. A belief is a claim about what the user
        # is like *now*, and the lookback window can span days -- without
        # stamps, "he's been stressed all week" said last Tuesday reads as
        # though it were said this morning.
        now = self._clock()
        chunks: list[str] = []
        for row in user_msgs:
            text = (row.content or "").strip()
            if not text:
                continue
            if len(text) > 600:
                text = text[:597] + "\u2026"
            age = timephrase.age_prefix(getattr(row, "created_at", None), now)
            stamp = f"[{age}] " if age else ""
            chunks.append(f"{stamp}{user_name}: {text}")
        return "\n".join(chunks)

    # ── LLM extractor ────────────────────────────────────────────────

    def _extract_with_llm(
        self,
        scrubbed_transcript: str,
        *,
        interest_hint: str = "",
        reconsider_block: str = "",
        concept_hint: str = "",
        dropped: list[str] | None = None,
    ) -> list[_BeliefTuple] | None:
        sections = [_USER_TEMPLATE.format(transcript=scrubbed_transcript)]
        if interest_hint:
            sections.append(
                "Topics this user keeps returning to (prioritise beliefs "
                f"about these when the transcript supports it): {interest_hint}."
            )
        if concept_hint:
            sections.append(
                "What you durably hold about this user (a prior on what to "
                "look for, not evidence -- the transcript decides, and it "
                "may well contradict one of these): "
                f"{concept_hint}."
            )
        if reconsider_block:
            sections.append(
                "Also re-check whether these earlier beliefs still hold: if a "
                "transcript turn speaks to one, return an updated belief for "
                f"that topic; otherwise ignore it: {reconsider_block}."
            )
        user_content = "\n\n".join(sections)
        # Two time instructions doing opposite jobs, deliberately.
        #
        # K-time8: the anchor is for *reading* — it lets the model resolve
        # "he was stressed yesterday" in the transcript instead of
        # guessing. The rule is for *writing*, and it has to be the
        # live-state half of the contract: this worker fills a topic and
        # a state phrase that are both re-read as claims about the
        # present. Pasting `STORED_TEXT_TIME_RULE` here instead (which
        # this worker did) instructs the model to write the concrete day
        # and put finished events in the past tense, which is how a mood
        # read became "experienced mild evening frustration and low
        # energy on august 12 2026" — a row that no later turn can
        # confirm or contradict, so it sits in the active set until the
        # cap prunes it.
        system_content = (
            f"{timephrase.today_anchor(self._clock())}\n\n"
            f"{_build_system_prompt(self._resolve_user_name())}\n\n"
            f"{timephrase.LIVE_STATE_TIME_RULE}"
        )
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "belief-worker extract prompt: model=%s prompt_chars=%d "
                "user_payload=%r",
                self._chat_model,
                len(user_content) + len(system_content),
                _preview(user_content),
            )
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": _EXTRACT_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                # Inferring what the user believes/feels from recent
                # messages is a theory-of-mind judgement; reasoning lifts
                # quality. num_predict stays the answer budget.
                think=True,
                surface="belief_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("belief-worker extract call raised", exc_info=True)
            return None
        if self._cancel_event.is_set():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            # An empty answer is a failed call, not "no beliefs": the
            # reasoning trace consumed the whole num_predict budget before
            # the answer began (the client already retried once without
            # it). Reporting [] would make the failure indistinguishable
            # from a quiet transcript.
            log.info("belief-worker llm-empty-answer: no answer tokens at all")
            return None
        log.debug(
            "belief-worker extract raw: chars=%d preview=%r",
            len(raw),
            _preview(raw),
        )
        return self._parse_tuples(raw, dropped=dropped)

    @staticmethod
    def _parse_tuples(
        raw: str, *, dropped: list[str] | None = None,
    ) -> list[_BeliefTuple] | None:
        """Parse the LLM's JSON answer into typed tuples.

        Returns ``None`` only when the response is fundamentally
        un-parseable. An empty list returns ``[]`` -- a perfectly valid
        "nothing to report" turn. Shape tolerance lives in
        :func:`app.llm.json_answers.parse_json_array_answer`.

        Tuples that survive parsing are then shape-gated by
        :func:`state_fault` / :func:`topic_fault`, because the length
        limits below only ever bounded how *much* the model wrote, never
        whether what it wrote fits the sentence it will be read back in.
        Each rejection's reason is appended to ``dropped`` so the run
        summary can report it: a gate that quietly ate most of the
        model's output would be indistinguishable from a quiet
        transcript, and "the extractor reports success while storing
        nothing" is a failure this worker family has had before.
        """
        _dropped = dropped if dropped is not None else []
        parsed = parse_json_array_answer(
            raw, key="beliefs", item_hint_keys=_BELIEF_ITEM_KEYS,
        )
        if parsed is None:
            return None
        out: list[_BeliefTuple] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind", "")).strip().lower()
            topic = str(item.get("topic", "")).strip().lower()
            state = str(item.get("predicted_state", "")).strip()
            try:
                confidence = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                continue
            if kind not in (KIND_MOOD, KIND_OPINION):
                continue
            if not topic or len(topic) > 60:
                continue
            if not state or len(state) > 120:
                continue
            fault = topic_fault(topic) or state_fault(state)
            if fault:
                _dropped.append(fault)
                log.info(
                    "belief-worker dropped unusable shape: reason=%s "
                    "topic=%r state=%r",
                    fault,
                    topic,
                    _preview(state),
                )
                continue
            confidence = max(0.0, min(1.0, confidence))
            out.append(
                _BeliefTuple(
                    kind=kind,
                    topic=topic,
                    predicted_state=state,
                    confidence=confidence,
                )
            )
        return out
