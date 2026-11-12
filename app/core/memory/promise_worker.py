"""Promise extraction worker (reworked Phase 3c).

The original promise extractor had two tracks: a post-turn regex that
captured the bare verb fragment after "I'll" / "I need to" (so
"I'll never know" became the memory "Jacob promised: never know") and
a speaking-window LLM pass that only fired in voice mode. The regex
track wrote context-free fragments straight to ``tier="long_term"`` at
high confidence, which polluted the memory store with unusable rows.

This worker replaces both. It runs on the :class:`IdleWorkerScheduler`
during quiet windows (so it never blocks the brain), reads the last
few turns of conversation for *context*, and asks the worker LLM to
extract **self-contained** promises -- pronouns and vague objects
resolved against the transcript -- as JSON. Output is gated
(idiom stop-list + min content words), deduped against existing open
promises, and written as ``kind="promise"`` memories with the same
lifecycle contract consumed by :mod:`app.core.memory.promise_lifecycle`
/ :class:`PromiseFollowthroughWorker` / :class:`FollowUpWorker`.

Pipeline (one ``run`` call):

1. Snapshot the last ``promise_worker_lookback_turns`` turns (both
   user and assistant lines) via :meth:`ChatDatabase.get_messages`,
   capped by ``promise_worker_max_msg_chars`` /
   ``promise_worker_max_transcript_chars``.
2. Refuse hard-PII windows via :func:`fact_check_privacy.web_safe_probe`
   (a gate only -- the original transcript is what reaches the model).
3. Spend one LLM call through the dedicated
   :class:`FactCheckRateLimiter` (``state_key='promise_worker.rate_state'``)
   asking for a JSON object ``{"promises": [{who, what, deadline}, ...]}``.
   Object, not bare array, because ``format: "json"`` gives the model no
   choice -- see :mod:`app.llm.json_answers`.
4. Quality-gate + dedupe each promise, then persist via
   :meth:`MemoryStore.add`.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.memory.conflict_heuristics import _content_words, _tokenize
from app.core.memory.fact_check_privacy import web_safe_probe
from app.core.memory.promise_extractor import Promise
from app.core.memory.promise_lifecycle import (
    ACTIVE_STATUSES,
    promise_status,
    promise_what,
)
from app.core.proactive.idle_worker import WorkSignal, pressure_from_count
from app.core.infra import timephrase
from app.llm.json_answers import parse_json_array_answer

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory.memory_store import MemoryStore
    from app.llm.chat_client import ChatClient
    from app.llm.embedder import Embedder


log = logging.getLogger("app.promise_worker")


# Cap on how much of any text we render in a single log line.
_LOG_PREVIEW_CHARS = 200

# Cap on the LLM response so a malformed answer can't run away with the
# budget. Five actions plus literal source quotes, IDs and JSON scaffolding.
_EXTRACT_MAX_TOKENS = 800

# A promise body must carry at least this many content words to be
# usable. "resolve them" has one ("resolve"); "fix the deploy script"
# has three ("fix", "deploy", "script"). This is the backstop behind
# the LLM's own self-contained instruction.
_MIN_CONTENT_WORDS = 2

# Pronouns that don't count as a real object. A promise whose only
# "content" words are a verb + a pronoun ("resolve them", "fix it")
# isn't self-contained, so it fails the gate even though it tokenizes
# to two words.
_PRONOUNS: frozenset[str] = frozenset(
    {
        "it",
        "them",
        "they",
        "that",
        "this",
        "those",
        "these",
        "you",
        "him",
        "her",
        "us",
        "we",
        "stuff",
        "thing",
        "things",
        "something",
        "someone",
    }
)

# Idiomatic heads / whole-phrases that read as commitments to the regex
# but are figures of speech. The LLM is told to skip these; this is the
# belt-and-suspenders gate for when it doesn't.
_IDIOM_FIRST_TOKENS: frozenset[str] = frozenset(
    {"never", "bet", "guess", "doubt", "wonder", "suppose", "dunno"}
)
_IDIOM_WHOLE_PHRASES: frozenset[str] = frozenset(
    {
        "never know",
        "see",
        "we will see",
        "see about that",
        "hope so",
        "think so",
        "guess so",
        "bet",
        "find out eventually",
    }
)


_SYSTEM_PROMPT = (
    "You read a short chat transcript between a user and the assistant "
    "(Aiko) and extract concrete promises or commitments either party "
    "made. Reply with ONE JSON object (no prose, no markdown) shaped "
    '{"promises": [...]} holding zero or more promise objects. Each '
    "object in that array has these fields:\n"
    "  - source_message_id: the numeric ID of the message making the commitment.\n"
    "  - source_quote: the shortest exact quote showing the commitment, at most 160 chars.\n"
    "  - who: 'user' (the human committed) or 'assistant' (Aiko "
    "committed).\n"
    "  - what: a SELF-CONTAINED action phrase, 4-160 chars, that names "
    "its object so it stands on its own. Resolve pronouns and vague "
    "references using the transcript -- write 'bring Jacob some tea', "
    "not 'bring you some'; 'fix the deploy script', not 'fix it'.\n"
    "  - deadline: when it is owed by, as an ISO-8601 date "
    "(YYYY-MM-DD) or date-time, resolved against the transcript's own "
    "timestamps. Use null when no time was stated or the wording was "
    "vague ('soon', 'sometime') -- do not invent one.\n"
    "  - deadline_quote: exact timing phrase from the SAME commitment message, or null. "
    "The message timestamp is never a deadline. Keep unstated times unknown.\n"
    "  - commitment_type: follow_up or immediate. Keep only meaningful follow_up "
    "commitments worth remembering beyond the current exchange. Immediate enactment, "
    "affectionate invitations, reassurance and ordinary bedtime actions are not lasting "
    "promises. Preserve a genuine request to follow up, deliver something or remember it.\n"
    "An actual reminder or delivery owed later is follow_up even when no due date "
    "is known or it is due later the same day. Immediate means enacting the action "
    "in this exchange, not merely mentioning a nearby future action.\n"
    "Rules:\n"
    "- A promise is a concrete intent to DO, find out, follow up on, or "
    "remember something specific. Idioms and figures of speech are NOT "
    "promises ('I'll never know', \"we'll see\", 'I bet', 'I guess', "
    "'I hope so').\n"
    "- Skip vague feelings with no action, and skip anything you cannot "
    "make self-contained from the transcript.\n"
    "- The speaker must own the action in the quoted sentence. Advice to the "
    "other person ('you just have to survive Thursday'), commands ('play the "
    "episode'), and looking forward to someone else's cookies are NOT the "
    "speaker's promises. Quote enough to retain I/we and the actual intention. "
    "Do not convert reassurance, a wish or an invitation into an obligation.\n"
    "- An offer ('If you want, I can remind you') is not an accepted promise. "
    "Only extract definite, unconditional follow-up commitments. Preserve "
    "conditional arrangements in ordinary memories instead; this promise ledger "
    "cannot decide whether their conditions have been fulfilled.\n"
    "- Paraphrase to the action; do not echo the literal sentence.\n"
    '- 0-5 items max. Return {"promises": []} when nothing qualifies.\n'
    "- Output the JSON object and nothing else."
)


# ``{{`` / ``}}`` because this template goes through ``str.format`` — a
# literal ``{"promises"`` would be read as a replacement field and raise
# ``KeyError`` on every run.
_USER_TEMPLATE = (
    "Transcript (most recent turns):\n{transcript}\n\n"
    'Return one JSON object shaped {{"promises": [...]}}.'
)


# Field names that identify a promise object, so a model that skips the
# wrapper and returns a single bare ``{"who": ..., "what": ...}`` is still
# understood. See :mod:`app.llm.json_answers`.
_PROMISE_ITEM_KEYS = ("who", "what", "deadline")


def _utcnow() -> datetime:
    return timephrase.utcnow()


def _preview(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= _LOG_PREVIEW_CHARS:
        return s
    return s[: _LOG_PREVIEW_CHARS - 1] + "\u2026"


def resolve_promise_who(
    who_raw: str,
    *,
    user_names: list[str] | None = None,
    assistant_name: str | None = None,
) -> str:
    """Map the model's ``who`` to ``"user"`` / ``"assistant"``, or ``""``.

    ``""`` means *unattributable*, and the caller must drop the promise
    rather than pick a side. Sidedness is not a cosmetic field: only
    assistant-side promises enter follow-through
    (:func:`~app.core.memory.promise_lifecycle.is_assistant_promise`),
    so guessing wrong either has Aiko silently owing nothing for
    something she said, or chasing the user over a commitment that was
    hers. A dropped promise costs one tick — the worker re-reads the same
    window next run — while a misattributed one is permanent and looks
    correct in every log line.

    The names are resolved from config rather than hardcoded, because
    the tolerance for a model that answers with a name instead of a role
    was written as the literal ``"jacob"`` and therefore stopped working
    the moment ``assistant.user_display_name`` was set to anything else.
    """
    token = (who_raw or "").strip().lower()
    if not token:
        return ""
    if token == "assistant":
        return "assistant"
    if token == "user":
        return "user"
    assistant = (assistant_name or "").strip().lower()
    # "aiko" stays accepted alongside the configured name: it is the
    # persona default and the transcript she appears in uses it.
    if token == assistant or token == "aiko":
        return "assistant"
    for name in user_names or []:
        if token == (str(name) or "").strip().lower():
            return "user"
    return ""


_NULL_WORDS: frozenset[str] = frozenset({"", "null", "none", "n/a", "unknown"})


def _read_deadline(
    raw: str | None, *, anchor: datetime | None = None,
) -> tuple[datetime | None, str]:
    """Turn the model's ``deadline`` field into ``(when, display_text)``.

    The prompt asks for ISO, and this is what happens when the answer is
    something else. Across 160 stored promises the field arrived in six
    registers, so the parse is a backstop rather than a formality --
    without it 13 of the 37 promises that named a day named it only to a
    human reader.

    Both halves of the return matter and they fail independently:

    * ``when`` is ``None`` for wording that names no moment. Nothing
      downstream may treat that as "not yet due" -- it means the promise
      has no clock at all, which is the normal case (123 of 160).
    * ``display_text`` is what gets written into the stored sentence. An
      absolute day carrying its weekday, because the alternative is a
      reader doing calendar arithmetic on a raw ``2026-08-19`` and the
      model getting the weekday wrong is one of the ways H40 happened.

    Unparseable wording keeps its raw form only while it would still be
    true next month: ``"before the end of the quarter"`` survives, a bare
    ``"tomorrow"`` does not, because the sentence outlives the day.
    """
    text = (raw or "").strip()
    if text.lower() in _NULL_WORDS:
        return None, ""
    when = timephrase.parse_loose_datetime(text, anchor=anchor, day_end=True)
    if when is None:
        return None, "" if timephrase.has_relative_deictic(text) else text[:60]
    return when, _format_deadline(when, anchor=anchor)


def _format_deadline(when: datetime, *, anchor: datetime | None = None) -> str:
    """Absolute, weekday-bearing rendering of a deadline for stored text.

    The clock is shown only when the deadline actually carries one --
    a day parsed with no stated time lands on its final minute, and
    printing "23:59" would dress a whole-day deadline up as a precise
    one.
    """
    local = when.astimezone()
    stamp = local.strftime("%a %b %d").replace(" 0", " ")
    stamp = f"{stamp}, {local.year}"  # Stored text must remain absolute across years.
    if (local.hour, local.minute) != (23, 59):
        stamp = f"{stamp} {local.strftime('%H:%M')}"
    return stamp


def _is_low_quality(what: str) -> bool:
    """True when a promise body is an idiom or too thin to be usable."""
    norm = (what or "").strip().lower().strip(" \"'.,;:!?")
    if len(norm) < 4:
        return True
    if norm in _IDIOM_WHOLE_PHRASES:
        return True
    tokens = _tokenize(norm)
    if tokens and tokens[0] in _IDIOM_FIRST_TOKENS:
        return True
    # Content words minus pronouns: a verb + a bare pronoun object
    # ("resolve them") isn't a self-contained promise.
    meaningful = _content_words(tokens) - _PRONOUNS
    if len(meaningful) < _MIN_CONTENT_WORDS:
        return True
    return False


class PromiseExtractionWorker:
    """IdleWorker that mines recent turns for concrete promises."""

    name = "promise_worker"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        chat_db: "ChatDatabase",
        embedder: "Embedder",
        ollama: "ChatClient",
        chat_model: str,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        # The **scoped** ``user_id:session_id`` key, not the bare session
        # id: that is what ``messages.session_id`` actually holds, and
        # every chat-db read here keys on it exactly. Wired to the bare id
        # this worker finds no transcript on any run, forever, while
        # reporting the benign-looking "no recent turns".
        session_key_provider: Callable[[], str | None],
        user_display_name_provider: Callable[[], str] | None = None,
        user_names_provider: Callable[[], list[str]] | None = None,
        assistant_name_provider: Callable[[], str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._chat_db = chat_db
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._session_key_provider = session_key_provider
        self._user_display_name_provider = user_display_name_provider
        self._user_names_provider = user_names_provider
        self._assistant_name_provider = assistant_name_provider
        self._clock = clock or _utcnow

    # ?? IdleWorker protocol ??????????????????????????????????????????

    def update_runtime(self, *, model: str | None = None) -> None:
        """Hot-swap the worker LLM model (model-cascade hook)."""
        if model is not None:
            self._chat_model = model

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "promise_worker_interval_seconds",
                600,
            )
        )

    def _enabled(self) -> bool:
        return bool(
            getattr(self._agent_settings, "promise_worker_enabled", True)
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        """Enabled, with budget for the extraction call."""
        return self._fresh_turns(now, last_run_at) is not None

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Pressure from transcript that has not been mined yet.

        Same shape as the belief worker, and for the same reason:
        there is no queue of un-extracted promises to count, only a
        window of recent turns that either has new material in it or
        does not. Re-running against an unchanged window spends a
        generation to rediscover promises already in the store.
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
        """``(new_messages, window)`` if a run could extract, else ``None``."""
        if not self._enabled():
            return None
        lookback_turns = int(
            getattr(self._memory_settings, "promise_worker_lookback_turns", 12)
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
            log.debug("promise-worker demand probe failed", exc_info=True)
            return None
        return fresh, max(1, lookback_turns * 2)

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        session_key = (
            self._session_key_provider() if self._session_key_provider else None
        )
        if not session_key:
            return {"skipped": True, "reason": "no_session"}

        lookback_turns = int(
            getattr(self._memory_settings, "promise_worker_lookback_turns", 12)
        )
        if lookback_turns <= 0:
            return {"skipped": True, "reason": "lookback_zero"}

        now = self._clock()
        source_rows: dict[int, dict[str, Any]] = {}
        transcript = self._snapshot_transcript(
            session_key=session_key, lookback_turns=lookback_turns,
            source_rows=source_rows,
        )
        if not transcript:
            # An empty window is normal; a session key that matches *no*
            # message ever is a wiring fault, and the two look identical
            # from here. Separating them costs one COUNT on a path that
            # already decided to do nothing, and the un-separated version
            # hid a mis-scoped key for 54 days.
            if self._session_is_unknown(session_key):
                log.warning(
                    "promise-worker: session key %r matches no messages at "
                    "all -- expected the scoped 'user_id:session_id' form. "
                    "The worker cannot mine anything until this is fixed.",
                    session_key,
                )
                return {"skipped": True, "reason": "unknown_session"}
            log.info(
                "promise-worker skip: no recent turns session=%s", session_key,
            )
            return {"skipped": True, "reason": "no_turns"}

        from app.core.concepts.concept_support import revision_hash

        window_key = "promise_worker.window:" + revision_hash(session_key)
        window_revision = revision_hash(source_rows)
        if self._chat_db.kv_get(window_key) == window_revision:
            return {"skipped": True, "reason": "unchanged_source_window"}

        if not self._rate_limiter.allow(now):
            log.info(
                "promise-worker skip: rate-limited session=%s", session_key,
            )
            return {"skipped": True, "reason": "rate_limited"}

        # Privacy gate. Unlike the belief worker (which mines coarse
        # topics), the promise worker needs names + pronouns intact so
        # the LLM can resolve "bring you some" -> "bring Jacob some tea".
        # So we only ask the *question* "could this be published?": if the
        # answer is no (hard PII like a URL/email/address, or the window
        # collapses to nothing once personal tokens come out) we skip the
        # run; otherwise the ORIGINAL transcript goes to the local worker
        # LLM. Nothing scrubbed is used, which is why this calls the probe
        # rather than the query builder — the latter logs a REDACT audit
        # line describing an outbound search that never happens here.
        user_names = (
            self._user_names_provider() if self._user_names_provider else None
        )
        assistant_name = (
            self._assistant_name_provider()
            if self._assistant_name_provider
            else None
        )
        # Gate the actual conversation, not our ISO timestamps/message IDs
        # (numeric stamps can resemble phone numbers to the PII detector).
        privacy_text = "\n".join(row["content"] for row in source_rows.values()) or transcript
        publishable = web_safe_probe(
            privacy_text,
            user_names=user_names,
            assistant_name=assistant_name,
        )
        if not publishable:
            log.info(
                "promise-worker skip: privacy-blocked transcript session=%s "
                "raw_chars=%d",
                session_key,
                len(transcript),
            )
            return {"skipped": True, "reason": "privacy_blocked"}

        log.info(
            "promise-worker start: session=%s lookback_turns=%d raw_chars=%d "
            "preview=%r",
            session_key,
            lookback_turns,
            len(transcript),
            _preview(transcript),
        )

        t0 = time.monotonic()
        promises, failure = self._extract_with_llm(transcript, source_rows=source_rows)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if promises is None:
            log.info(
                "promise-worker %s elapsed_ms=%.0f session=%s",
                failure.replace("llm_", "llm-").replace("_", "-"),
                elapsed_ms,
                session_key,
            )
            return {
                "skipped": True,
                "reason": failure,
                "llm_ms": round(elapsed_ms, 1),
            }

        log.info(
            "promise-worker llm done: promises=%d elapsed_ms=%.0f",
            len(promises),
            elapsed_ms,
        )

        max_per_run = max(
            1,
            int(
                getattr(self._memory_settings, "promise_worker_max_per_run", 5)
            ),
        )
        existing = self._existing_promise_word_sets()
        source_memories = list(self._memory_store.iter_by_kind("promise"))
        persisted = 0
        dropped_dup = 0
        dropped_quality = 0
        write_failed = False
        for p in promises[:max_per_run]:
            if self._cancel_event.is_set():
                break
            if _is_low_quality(p.text):
                dropped_quality += 1
                continue
            body_words = _content_words(_tokenize(p.text))
            from app.core.memory.promise_evidence import same_source_actions

            if same_source_actions(p, source_memories, self._is_duplicate):
                dropped_dup += 1
                continue
            scope = (p.who, p.deadline)
            if self._is_duplicate(body_words, existing.get(scope, [])):
                dropped_dup += 1
                continue
            if self._persist(p, session_key=session_key):
                persisted += 1
                # Keep the in-run dedupe set fresh so two near-identical
                # promises in one batch don't both land.
                existing.setdefault(scope, []).append(body_words)
                source_memories = list(self._memory_store.iter_by_kind("promise"))
            else:
                write_failed = True

        if not self._cancel_event.is_set() and not write_failed:
            self._chat_db.kv_set(window_key, window_revision)

        result = {
            "promises_returned": len(promises),
            "persisted": persisted,
            "dropped_duplicate": dropped_dup,
            "dropped_low_quality": dropped_quality,
            "llm_ms": round(elapsed_ms, 1),
        }
        log.info("promise-worker done: %s", result)
        return result

    # ?? transcript snapshot ??????????????????????????????????????????

    def _session_is_unknown(self, session_key: str) -> bool:
        """True when the key names a session the message store never saw."""
        try:
            return int(self._chat_db.get_message_count(session_key)) <= 0
        except Exception:
            log.debug("promise-worker session probe failed", exc_info=True)
            return False

    def _snapshot_transcript(
        self,
        *,
        session_key: str,
        lookback_turns: int,
        source_rows: dict[int, dict[str, Any]] | None = None,
    ) -> str:
        """Join the last N turns (both sides) into one prompt block.

        Unlike the belief worker (user-only), promises come from both
        Aiko and the user, so assistant lines are kept. We render from
        the most recent message backward and stop once the overall
        ``promise_worker_max_transcript_chars`` budget is hit so a long
        history can't blow the worker-LLM token budget.
        """
        max_msg_chars = max(
            200,
            int(
                getattr(
                    self._memory_settings,
                    "promise_worker_max_msg_chars",
                    2000,
                )
            ),
        )
        max_transcript_chars = max(
            500,
            int(
                getattr(
                    self._memory_settings,
                    "promise_worker_max_transcript_chars",
                    8000,
                )
            ),
        )
        user_name = (
            (self._user_display_name_provider() or "").strip()
            if self._user_display_name_provider
            else ""
        ) or "the user"
        try:
            rows = self._chat_db.get_messages(
                session_key, limit=lookback_turns * 2
            )
        except Exception:
            log.debug("promise-worker get_messages failed", exc_info=True)
            return ""
        rows = [r for r in rows if r.role in ("user", "assistant")]
        if not rows:
            return ""
        lines: list[str] = []
        total = 0
        # K-time10: stamp each line with its age. The system prompt asks
        # the model to resolve "tomorrow" into a concrete day, which needs
        # to know the day the word was *said* -- the run-time anchor alone
        # is the wrong operand once the lookback spans more than a day.
        now = self._clock()
        for row in reversed(rows):
            text = (row.content or "").strip()
            if not text:
                continue
            if len(text) > max_msg_chars:
                text = text[: max_msg_chars - 1] + "\u2026"
            created = getattr(row, "created_at", None)
            line = timephrase.format_transcript([{
                "role": row.role, "content": text, "created_at": created,
            }], now, role_labels={"user": user_name, "assistant": "Aiko"})
            mid = getattr(row, "id", None)
            line = f"[message_id={mid}; role={row.role}; said_at={created}] {line}"
            if total + len(line) > max_transcript_chars and lines:
                break
            lines.append(line)
            if source_rows is not None and type(mid) is int:
                from app.core.memory.promise_evidence import source_revision

                source_rows[mid] = {"role": row.role, "content": text, "created_at": created,
                    "revision": source_revision({"role": row.role, "content": row.content,
                                                 "created_at": created})}
            total += len(line) + 1
        lines.reverse()
        return "\n".join(lines)

    # ?? LLM extractor ????????????????????????????????????????????????

    def _extract_with_llm(
        self, transcript: str, *, source_rows: dict[int, dict[str, Any]] | None = None,
    ) -> tuple[list[Promise] | None, str]:
        """Run one extraction call: ``(promises, failure_reason)``.

        ``promises`` is ``None`` exactly when the call failed, and the
        second element then names *how* — the three failures have
        different causes and different fixes, and collapsing them into a
        bare ``None`` (or worse, into ``[]``) is what hid a permanently
        broken extractor.
        """
        user_content = _USER_TEMPLATE.format(transcript=transcript)
        # K-time8: anchor the extractor in wall-clock time so a stated
        # deadline like "tomorrow" / "next Monday" can be resolved to a
        # concrete date instead of left as a stale relative word.
        system_content = (
            _SYSTEM_PROMPT
            + "\n\n"
            + timephrase.today_anchor(self._clock())
            + " Each transcript line is prefixed with when it was said; "
            "resolve a relative deadline ('tomorrow', 'tonight', 'next "
            "Monday') against THAT line's timestamp, not against today, "
            "and write a concrete day/time. "
            + timephrase.STORED_TEXT_TIME_RULE
        )
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "promise-worker extract prompt: model=%s prompt_chars=%d "
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
                # Distinguishing a real commitment ("I'll look into X")
                # from idle phrasing is a judgement call reasoning helps.
                # Headroom for the trace is added client-side, and the
                # client retries once with the trace off if it starves the
                # answer anyway. Measured on qwen3.6:27b: the trace costs
                # ~35s and reached the same verdict a 2.5s no-think call
                # did, so if latency ever matters more than the judgement,
                # this is the flag to flip.
                think=True,
                surface="promise_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("promise-worker extract call raised", exc_info=True)
            return None, "llm_error"
        if self._cancel_event.is_set():
            return None, "cancelled_mid_extract"
        raw = "".join(chunks).strip()
        if not raw:
            # NOT "no promises found". The model produced no answer at
            # all -- historically because the reasoning trace ate the
            # whole num_predict budget. Returning [] here is what let an
            # extractor that had never once worked report success on
            # every run for its entire life.
            return None, "llm_empty_answer"
        log.debug(
            "promise-worker extract raw: chars=%d preview=%r",
            len(raw),
            _preview(raw),
        )
        parsed = self._parse_promises(
            raw,
            user_names=(
                self._user_names_provider()
                if self._user_names_provider else None
            ),
            assistant_name=(
                self._assistant_name_provider()
                if self._assistant_name_provider else None
            ),
            anchor=self._clock(),
            source_rows=source_rows if source_rows is not None else {},
        )
        if parsed is None:
            log.info(
                "promise-worker unparseable answer: chars=%d preview=%r",
                len(raw),
                _preview(raw),
            )
            return None, "llm_unparseable"
        return parsed, ""

    @staticmethod
    def _parse_promises(
        raw: str,
        *,
        user_names: list[str] | None = None,
        assistant_name: str | None = None,
        anchor: datetime | None = None,
        source_rows: dict[int, dict[str, Any]] | None = None,
    ) -> list[Promise] | None:
        """Parse the LLM's JSON answer into typed promises.

        Returns ``None`` only when the response is fundamentally
        un-parseable. An empty list returns ``[]`` -- a valid "nothing to
        report" turn. Shape tolerance lives in
        :func:`app.llm.json_answers.parse_json_array_answer`, because
        ``format: "json"`` guarantees an object and models vary in how
        they wrap the array.

        An item whose ``who`` names neither side is dropped rather than
        assigned one; see :func:`resolve_promise_who`.

        ``anchor`` is the moment the transcript was read, which any
        relative deadline resolves against.
        """
        parsed = parse_json_array_answer(
            raw, key="promises", item_hint_keys=_PROMISE_ITEM_KEYS,
        )
        if parsed is None:
            return None
        out: list[Promise] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            if item.get("commitment_type") == "immediate":
                continue
            who_raw = str(item.get("who") or "").strip().lower()
            who = resolve_promise_who(
                who_raw,
                user_names=user_names,
                assistant_name=assistant_name,
            )
            if not who:
                log.info(
                    "promise-worker dropped unattributable promise: who=%r "
                    "what=%r",
                    who_raw,
                    _preview(str(item.get("what") or "")),
                )
                continue
            what = str(item.get("what") or "").strip()
            if len(what) < 4:
                continue
            mid = item.get("source_message_id")
            quote = str(item.get("source_quote") or "").strip()
            source = source_rows.get(mid) if source_rows is not None and type(mid) is int else None
            item_anchor = anchor
            if source_rows is not None:
                # Only the exact offered message, same speaker and literal span
                # can authorize a source pointer. A plausible model-supplied ID cannot.
                if (source is None or source["role"] != who or len(quote) < 8
                        or " ".join(quote.split()) not in " ".join(source["content"].split())):
                    log.info("promise-worker refused source: missing/mismatched message or quote")
                    continue
                from app.core.memory.promise_evidence import ownership_refusal

                refusal = ownership_refusal(quote, source["content"])
                if refusal:
                    log.info("promise-worker refused ownership: %s", refusal)
                    continue
                item_anchor = timephrase.parse_iso(source["created_at"])
                if timephrase.has_relative_deictic(what):
                    continue  # The prompt must anchor the stored action, not just its deadline.
            deadline_raw = item.get("deadline")
            if (source_rows is not None and item_anchor is None
                    and timephrase.has_relative_deictic(str(deadline_raw or ""))):
                continue
            when, when_text = _read_deadline(
                deadline_raw if isinstance(deadline_raw, str) else None,
                anchor=item_anchor,
            )
            deadline_quote, precision, revision = "", "", ""
            if source is not None:
                from app.core.memory.promise_evidence import supported_deadline, source_revision

                deadline_quote = item.get("deadline_quote")
                when, precision = supported_deadline(deadline_quote, source["content"], item_anchor)
                when_text = _format_deadline(when) if when else ""
                revision = source.get("revision") or source_revision(source)
                deadline_quote = (deadline_quote if isinstance(deadline_quote, str)
                                  and deadline_quote in source["content"] else "")
            out.append(
                Promise(
                    who=who,
                    text=what[:200],
                    source="llm",
                    confidence=0.8,
                    source_turn_id=mid if source is not None else None,
                    raw_match=quote[:2000] if source is not None else "",
                    deadline=when,
                    deadline_text=when_text,
                    deadline_quote=deadline_quote,
                    deadline_precision=precision,
                    source_revision=revision,
                )
            )
        return out

    # ?? dedupe + persistence ?????????????????????????????????????????

    def _existing_promise_word_sets(self) -> dict[tuple[str, datetime | None], list[set[str]]]:
        """Content-word sets of existing still-active promise bodies."""
        from app.core.memory.memory_origin import memory_origin

        out: dict[tuple[str, datetime | None], list[set[str]]] = {}
        try:
            rows = self._memory_store.iter_by_kind("promise")
        except Exception:
            log.debug("promise-worker iter_by_kind failed", exc_info=True)
            return out
        for mem in rows:
            try:
                if promise_status(mem) not in ACTIVE_STATUSES:
                    continue
                words = _content_words(_tokenize(promise_what(mem)))
            except Exception:
                continue
            if words:
                subject = memory_origin(mem).subject
                who = "assistant" if subject == "aiko" else "user" if subject == "user" else ""
                raw_deadline = (mem.metadata or {}).get("promise_deadline")
                deadline = timephrase.parse_iso(raw_deadline)
                if who and not (raw_deadline and deadline is None):
                    out.setdefault((who, deadline), []).append(words)
        return out

    @staticmethod
    def _is_duplicate(
        body_words: set[str], existing: list[set[str]], *, min_overlap: int = 3
    ) -> bool:
        if not body_words:
            return True
        for prior in existing:
            needed = min(int(min_overlap), len(body_words))
            if needed <= 0:
                continue
            if len(body_words & prior) >= needed:
                return True
        return False

    def _persist(self, promise: Promise, *, session_key: str | None) -> bool:
        store = self._memory_store
        embedder = self._embedder
        if store is None or embedder is None:
            return False
        if promise.source == "llm" and (promise.source_turn_id is None or not promise.raw_match):
            return False
        if promise.source_revision:
            from app.core.memory.promise_evidence import source_revision

            row = self._chat_db.get_message_row(promise.source_turn_id)
            current = (source_revision({"role": row.role, "content": row.content,
                                        "created_at": row.created_at}) if row else None)
            if current != promise.source_revision:
                return False
        display_name = (
            (self._user_display_name_provider() or "Jacob")
            if self._user_display_name_provider
            else "Jacob"
        )
        content = promise.to_memory_content(user_display_name=display_name)
        try:
            emb = embedder.embed(content)
        except Exception:
            log.debug("promise-worker embed failed", exc_info=True)
            return False
        metadata: dict[str, Any] = {
            "promise_who": promise.who,
            "promise_status": "open",
            "source_quote": promise.raw_match,
            "source_scope": "validated_message_quote",
            "commitment_source": {"version": 1, "message_id": promise.source_turn_id,
                                  "revision": promise.source_revision},
            "deadline_quote": promise.deadline_quote,
            "deadline_precision": promise.deadline_precision,
        }
        # The deadline lives here and nowhere else. ``event_time`` was the
        # obvious alternative and is the wrong shape: it means "when this
        # happened / happens", the decay worker retires rows by it, and a
        # promise whose date has passed is the one row that must stay
        # visible -- an unkept commitment is the point, not expired
        # bookkeeping. Keeping it out of the temporal columns leaves
        # ``promise_status`` the single authority on a promise's fate.
        if promise.deadline is not None:
            metadata["promise_deadline"] = promise.deadline.isoformat()
        try:
            mem = store.add(
                content=content,
                kind="promise",
                embedding=emb,
                salience=0.6,
                source_session=session_key,
                source_message_id=promise.source_turn_id,
                metadata=metadata,
                tier="long_term",
                confidence=0.85,
            )
        except Exception:
            log.debug("promise-worker insert failed", exc_info=True)
            return False
        if mem is not None:
            log.info(
                "promise-worker upsert: id=%s who=%s deadline=%s content=%r",
                getattr(mem, "id", "?"),
                promise.who,
                promise.deadline.isoformat() if promise.deadline else "-",
                _preview(content),
            )
        return mem is not None

    # ?? debug surface ????????????????????????????????????????????????

    def debug_state(self) -> dict[str, Any]:
        """Snapshot for the MCP ``get_promise_stats`` tool."""
        now = self._clock()
        return {
            "enabled": self._enabled(),
            "interval_seconds": self.interval_seconds,
            "lookback_turns": int(
                getattr(
                    self._memory_settings, "promise_worker_lookback_turns", 12
                )
            ),
            "max_msg_chars": int(
                getattr(
                    self._memory_settings, "promise_worker_max_msg_chars", 2000
                )
            ),
            "max_transcript_chars": int(
                getattr(
                    self._memory_settings,
                    "promise_worker_max_transcript_chars",
                    8000,
                )
            ),
            "rate_limit": self._rate_limiter.snapshot(now),
        }


__all__ = ["PromiseExtractionWorker"]
