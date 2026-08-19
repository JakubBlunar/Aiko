"""Background extractor that mines durable facts out of a chat transcript.

Triggered by :class:`SummaryWorker` right after a successful
``save_summary``: at that point the conversation is paused, the GPU is free,
and there's a fresh batch of unsummarized turns whose long-term-relevant
content we want to capture.

The extractor runs ONE ``chat_json`` call against the same chat model the
user is talking to (no separate judge model -- avoids extra model swaps and
GPU thrashing). The model is asked for a JSON list of memories:

    {"memories": [
        {"content": "...", "kind": "preference", "salience": 0.7,
         "temporal_type": "durable", "event_time": null}, ...
    ]}

Each candidate is validated, embedded, and pushed into :class:`MemoryStore`,
which dedupes against already-stored near-duplicates.

Schema v10 — the prompt now carries the current date so the extractor can
resolve relative phrases ("yesterday", "tonight at 8", "next Monday") into
absolute ISO-8601 ``event_time`` and a ``temporal_type`` classification.
``relevance_until`` is derived server-side from the type so the LLM only
needs to think about *what* the memory is, not *how long* it stays fresh.

The extractor keeps a **watermark** per session (H31). It rides on
:class:`SummaryWorker`, which advances its own watermark and fires once
every ``summary_min_unsummarized_messages`` new messages; the extractor
used to read the trailing ``max_window_messages`` regardless, so every turn
was mined about five times over and each re-read was an independent chance
to duplicate the claim or mis-assign its owner. Now only messages after the
watermark are offered for extraction, with the rows before it rendered as
labelled read-only context so a pronoun at the boundary still resolves.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable

from app.core.infra.chat_database import ChatDatabase, MessageRow
from app.core.memory.memory_store import (
    VALID_KINDS,
    VALID_PROVENANCE,
    VALID_TEMPORAL_TYPES,
    MemoryStore,
    _DEFAULT_PROVENANCE,
    _DEFAULT_TEMPORAL_TYPE,
    derive_relevance_until,
)
from app.core.session.session_text_utils import resolve_user_name, speaker_labels
from app.llm.chat_client import content_looks_complete
from app.llm.embedder import Embedder
from app.llm.ollama_client import OllamaClient
from app.core.infra import timephrase


log = logging.getLogger("app.memory_extractor")


# The relevance-window table and its derivation now live next to the
# writer in ``memory_store``, because ``MemoryStore.add`` can change a
# row's temporal type and therefore has to be able to recompute the
# expiry that type implies (H40). Re-exported under the old private names
# so existing importers keep working.
_derive_relevance_until = derive_relevance_until


def _parse_iso(value: str | None) -> datetime | None:
    """Best-effort ISO-8601 -> aware datetime, with tz-naive promotion to UTC."""
    if not value:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    # Normalize trailing ``Z`` (which fromisoformat doesn't accept on
    # Python 3.10) to ``+00:00`` so we don't lose zone info.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_system_prompt(
    user_display_name: str = "the user",
    *,
    today: datetime | None = None,
    max_memories: int = 5,
) -> str:
    """System prompt for the memory extractor, name- and date-templated.

    Resolved at run time so a rename via the onboarding modal takes
    effect on the next sweep without restarting the worker. The
    ``today`` anchor is what lets the LLM resolve relative phrases
    ("yesterday", "tonight", "next Monday") to absolute ISO-8601 in
    ``event_time``; without it, the model has no way to know what
    "yesterday" means.
    """
    name = user_display_name or "the user"
    if today is None:
        today = timephrase.utcnow().astimezone()
    today_human = today.strftime("%A, %B %d, %Y, %H:%M %Z").strip()
    today_iso = today.isoformat()
    valid_types = ", ".join(VALID_TEMPORAL_TYPES)
    valid_provenance = ", ".join(VALID_PROVENANCE)
    return (
        f"You analyse a chat transcript between a user named {name} and his AI "
        "companion Aiko. Your job is to extract DURABLE memories that would "
        "still be relevant later, plus any time-bound events worth "
        "remembering with their absolute timestamp.\n"
        "\n"
        f"Today is {today_human} ({today_iso}). Every transcript line is "
        "prefixed with when it was said. Resolve a relative phrase "
        "('yesterday', 'tonight at 8', 'next Monday', 'in two weeks') "
        "against THAT LINE's timestamp rather than against today, and "
        "write the result as an absolute ISO-8601 timestamp in the "
        "``event_time`` field. This sweep can run hours after the words "
        "were said and can cover more than one day, so the two anchors "
        "are often different days: a 'tomorrow' said on Monday evening is "
        "Tuesday, whichever day you are reading it on. If only a date is "
        "given with no clock time, use noon local, except for something "
        "that already happened on the current day — there, use the "
        "line's own timestamp, because noon may not have come yet. If the "
        "wording is vague ('soon', 'eventually'), leave ``event_time`` "
        "null rather than guessing; a plan with no time is still useful, "
        "a plan with an invented one is not.\n"
        "\n"
        "Do not write a weekday name or a calendar date into ``content`` "
        "unless the user stated it outright. Say what happened and let "
        "``event_time`` carry when; a date you worked out yourself is the "
        "one part of this that is silently wrong when your arithmetic "
        "slips, and it then outlives every correction.\n"
        "\n"
        "Two kinds of memories are allowed:\n"
        f"  1. Facts about {name}: real preferences, opinions, ongoing projects, "
        "     important events (past or future), relationships, recurring jokes. "
        f"     One short sentence in THIRD person ('{name} ...').\n"
        "  2. Aiko's notes about herself: a stance, a taste, a decision Aiko "
        "     made about her own personality that she wants to keep next time. "
        "     One short sentence in FIRST person ('I ...').\n"
        "\n"
        "WHO A CLAIM BELONGS TO. Half of this transcript is Aiko talking, so "
        "every claim you extract needs an owner and the owner is whoever the "
        "claim is *about*, not whoever happens to be nearby. A hobby, plan, "
        "taste, feeling or intention that Aiko voiced about herself is "
        "category 2 or nothing at all — never restate it as a fact about "
        f"{name}. If Aiko says she has started collecting something, "
        f"{name} has not started collecting anything. When you cannot tell "
        "from the transcript which of the two it belongs to, drop it.\n"
        "\n"
        "Each memory ALSO carries a ``temporal_type`` that classifies how "
        "it relates to time:\n"
        "  - 'durable': timeless fact ('Jacob lives in Prague').\n"
        "  - 'preference': taste / identity ('Jacob is vegetarian').\n"
        "  - 'ongoing': active project or state with a soft expiry "
        "    ('Jacob is learning Japanese').\n"
        "  - 'past_event': already happened — should be referenced "
        "    retrospectively ('Jacob worked on the dashboard yesterday'). "
        "    Set ``event_time`` to when it happened if known.\n"
        "  - 'future_plan': mentioned as upcoming ('Jacob is going to the "
        "    gym tonight at 8'). Set ``event_time`` to when it is supposed "
        "    to happen whenever the wording pins it down; leave it null if "
        "    it genuinely does not ('sometime next month').\n"
        "  The past / future split is the one that matters most, because "
        "  the two are handled by different machinery downstream. Anything "
        "  that has NOT happened yet is 'future_plan', however casually it "
        "  came up — a delivery, an appointment, an intention for later "
        "  today. Filing one of those as 'past_event' tells Aiko it is "
        "  done, and she will talk as though it were.\n"
        "\n"
        "Each memory ALSO carries a ``provenance`` that records HOW it was "
        "learned:\n"
        "  - 'stated': the user said it outright, in so many words "
        f"    ('{name} said he is vegetarian').\n"
        "  - 'inferred': you concluded it by reading between the lines or "
        "    adding up several things he said, but he never said it "
        f"    directly ('{name} seems to prefer working late').\n"
        "  Default to 'inferred' whenever you are not sure he said it "
        "  outright. Claiming he stated something he only implied is the "
        "  worse error, so lean 'inferred'. Aiko's own first-person "
        "  'self' notes are always 'inferred'.\n"
        "\n"
        "Rules:\n"
        "- Skip throwaway chitchat, single-turn moods, weather, jokes that are "
        "  not recurring.\n"
        "- Skip anything already in the existing memory list.\n"
        "- If nothing is worth remembering, return an empty array.\n"
        f"- Return AT MOST {max(1, int(max_memories))} memories — only the most "
        "  durable / important ones. Fewer is better than padding.\n"
        "- Keep each 'content' to a single short sentence under ~120 "
        "  characters. Do not write paragraphs.\n"
        "- Any private reasoning you do is separate and unlimited; this "
        f"  length budget applies ONLY to the final JSON answer (at most "
        f"  {max(1, int(max_memories))} memories, each under ~120 chars). "
        "  Keep the answer compact.\n"
        "- 'kind' must be one of: fact, preference, event, relationship, self. "
        "  Use 'self' only for Aiko's first-person notes.\n"
        f"- 'temporal_type' must be one of: {valid_types}. Default to "
        "  'durable' when unsure — but only when the memory really has no "
        "  time to it. If it has one, pick the side of now it falls on.\n"
        f"- 'provenance' must be one of: {valid_provenance}. Default to "
        "  'inferred' when unsure.\n"
        "- 'salience' is 0..1 -- how much this should drive future conversation.\n"
        "- Phrase the content with proper tense based on temporal_type. "
        "  past_event: past tense ('Jacob finished the dashboard'). "
        "  future_plan: future tense ('Jacob plans to go to the gym at 20:00'). "
        "  Never leave a raw 'yesterday' / 'today' / 'tonight' / 'tomorrow' "
        "  in content: it re-anchors to whenever the note is next read, so "
        "  'the courier comes tomorrow' is still saying that a week later. "
        "  The event_time field carries the moment; content carries what "
        "  happened or is meant to.\n"
        "\n"
        'Reply with JSON only, exactly: {"memories": [{"content": "...", '
        '"kind": "...", "salience": 0.5, "temporal_type": "...", '
        '"provenance": "...", "event_time": "ISO-8601 or null"}]}'
    )


def _salvage_memories(text: str) -> list[dict]:
    """Recover complete memory objects from a truncated JSON response.

    When the model hits its ``num_predict`` cap the ``"memories": [...]``
    array is cut off inside the last object, so :func:`json.loads` on the
    whole blob fails. This walks the characters after the array's opening
    ``[``, tracks brace depth (string-/escape-aware), and parses each
    fully-closed ``{...}`` object on its own. The trailing incomplete
    object is silently dropped — everything before it is preserved.

    Returns the list of successfully-parsed dicts (possibly empty).
    """
    if not text:
        return []
    # Anchor on the memories array; fall back to the first '[' if the
    # key spelling drifted. Without an array there's nothing to salvage.
    key_pos = text.find('"memories"')
    bracket = text.find("[", key_pos if key_pos >= 0 else 0)
    if bracket < 0:
        return []
    out: list[dict] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i in range(bracket + 1, len(text)):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                fragment = text[start : i + 1]
                try:
                    obj = json.loads(fragment)
                    if isinstance(obj, dict):
                        out.append(obj)
                except Exception:
                    pass
                start = -1
        elif ch == "]" and depth == 0:
            break
    return out


_FIRST_PERSON_OPENERS: frozenset[str] = frozenset({
    "i", "i'm", "i've", "i'll", "i'd", "my", "mine", "myself",
})


def _opens_first_person(content: str) -> bool:
    """Does the sentence start by talking about the speaker herself?"""
    words = re.findall(r"[A-Za-z']+", content or "")
    return bool(words) and words[0].lower() in _FIRST_PERSON_OPENERS


def _opens_about_user(content: str, user_name: str) -> bool:
    """Does the sentence start by naming the user?

    Returns ``False`` when the display name is unset and
    :func:`resolve_user_name` handed back its ``"the user"`` fallback. The
    first token of that fallback is ``"the"``, which would match the
    opening of any number of ordinary sentences -- "The garden is my
    favourite place" is not a claim about anybody's user.
    """
    name = (user_name or "").strip().lower()
    if not name or name == "the user":
        return False
    words = re.findall(r"[A-Za-z']+", content or "")
    return bool(words) and words[0].lower() == name.split()[0]


def _fallback_kind(content: str) -> str:
    """The kind to use when the model's label is missing or unrecognised.

    This used to be a flat ``"fact"``, which is not a neutral default: in
    this schema ``fact`` means "a fact about the user", so a first-person
    note from Aiko that lost its label became a claim about the user's life.
    A dropped field is a plausible way for the bottle-cap failure to happen
    without the model ever asserting anything wrong, so the fallback reads
    the sentence instead of assuming a side.
    """
    return "self" if _opens_first_person(content) else "fact"


# Back-compat constant for callers that imported the module-level prompt
# directly. New code should call ``_build_system_prompt(name)`` to pick
# up the configured display name and the current date.
_SYSTEM_PROMPT = _build_system_prompt()


class MemoryExtractor:
    def __init__(
        self,
        db: ChatDatabase,
        store: MemoryStore,
        embedder: Embedder,
        ollama: OllamaClient,
        *,
        model: str,
        min_window_messages: int = 4,
        max_window_messages: int = 30,
        context_messages: int = 10,
        max_new_per_run: int = 5,
        max_tokens: int = 1024,
        think: bool = True,
        timeout_seconds: float = 120.0,
        user_display_name_provider: "Callable[[], str] | None" = None,
    ) -> None:
        self._db = db
        self._store = store
        self._embedder = embedder
        self._ollama = ollama
        self._model = model
        # ``_min_window`` now counts *unmined* messages, not the size of the
        # window read. Below it the run is skipped without advancing, so the
        # material accumulates rather than being mined two rows at a time
        # (which is what an overflow squish, whose bar is 2, would cause).
        self._min_window = max(2, int(min_window_messages))
        self._max_window = max(self._min_window, int(max_window_messages))
        # Rows immediately before the watermark, rendered as labelled
        # read-only context. Non-zero because a claim's first mention and
        # the pronoun that carries it often land in different runs: "he
        # finally finished it" is unextractable without the turn before.
        self._context_messages = max(0, int(context_messages))
        self._max_new_per_run = max(1, int(max_new_per_run))
        # Output token ceiling for the JSON ANSWER (the array we parse),
        # NOT the reasoning trace. With ``think`` on, the OllamaClient
        # adds ``ollama.think_num_predict_headroom`` on top of this so the
        # trace gets its own budget; this stays the answer budget. The
        # salvage parser recovers anything that still clips at the tail.
        self._max_tokens = max(256, int(max_tokens))
        # Reasoning trace on/off. Default ON: the extractor's
        # classify-and-date judgement is unreliable on reasoning models
        # when think is suppressed. Ollama keeps the trace in
        # ``message.thinking``, so ``chat_json`` still returns clean JSON.
        self._think = bool(think)
        self._timeout = float(timeout_seconds)
        self._lock = threading.Lock()
        self._on_added_listeners: list = []
        # Identity: optional callable evaluated at each run so renames
        # propagate without re-creating the worker.
        self._user_display_name_provider = user_display_name_provider

    def _resolve_user_name(self) -> str:
        return resolve_user_name(self._user_display_name_provider)

    # ── public API ────────────────────────────────────────────────────────

    def update_model(self, model: str) -> None:
        if model:
            self._model = model

    def add_listener(self, callback) -> None:
        """Register ``callback(memory)`` invoked once per inserted memory."""
        self._on_added_listeners.append(callback)

    def extract_for_session(self, session_key: str) -> int:
        """Run extraction on the recent window of ``session_key``.

        Returns the number of new memories inserted. Existing duplicates
        bump salience but are not counted as new.
        """
        # One extraction at a time -- the chat model is shared with the
        # foreground turn, so we don't want to fight for GPU.
        if not self._lock.acquire(blocking=False):
            log.debug("extractor already running, skipping")
            return 0
        try:
            return self._do_extract(session_key)
        finally:
            self._lock.release()

    # ── internals ─────────────────────────────────────────────────────────

    # ── watermark ─────────────────────────────────────────────────────────

    @staticmethod
    def _watermark_key(session_key: str) -> str:
        return f"memory.extractor.watermark:{session_key}"

    def _read_watermark(self, session_key: str) -> int | None:
        """Highest message id already offered for extraction, or ``None``.

        ``None`` means "never run against this session" and is handled as a
        seed rather than as zero: treating it as zero would mine a
        thousand-message history from the beginning on first launch.
        """
        try:
            raw = self._db.kv_get(self._watermark_key(session_key))
        except Exception:
            log.debug("extractor watermark read failed", exc_info=True)
            return None
        if raw is None:
            return None
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return None

    def _write_watermark(self, session_key: str, message_id: int) -> None:
        try:
            self._db.kv_set(self._watermark_key(session_key), str(int(message_id)))
        except Exception:
            # A lost write costs one duplicate batch on the next run, which
            # the existing-memories block and the restatement gate absorb.
            # Never worth failing an otherwise good extraction over.
            log.warning("extractor watermark write failed", exc_info=True)

    def _select_window(
        self, session_key: str,
    ) -> "tuple[list[MessageRow], list[MessageRow]] | None":
        """Split the session into (already-mined context, new material).

        Returns ``None`` when there is not enough new material to be worth a
        model call. The first element is context only and must never be
        offered for extraction.
        """
        watermark = self._read_watermark(session_key)
        if watermark is None:
            # First run on this session. Seed from the trailing window so an
            # install with existing history mines its recent past once and
            # then walks forward, instead of re-mining months of transcript.
            fresh = self._db.get_messages(session_key, limit=self._max_window)
            if len(fresh) < self._min_window:
                log.debug(
                    "extract skipped: only %d messages (need %d)",
                    len(fresh), self._min_window,
                )
                return None
            return [], fresh
        fresh = self._db.get_messages_after(
            session_key, after_id=watermark, limit=self._max_window,
        )
        if len(fresh) < self._min_window:
            log.debug(
                "extract skipped: %d unmined messages past id %d (need %d)",
                len(fresh), watermark, self._min_window,
            )
            return None
        context: list[MessageRow] = []
        if self._context_messages:
            context = self._db.get_messages_before(
                session_key,
                before_id=fresh[0].id,
                limit=self._context_messages,
            )
        return context, fresh

    # ── work ──────────────────────────────────────────────────────────────

    def _do_extract(self, session_key: str) -> int:
        window = self._select_window(session_key)
        if window is None:
            return 0
        context_rows, rows = window

        existing = self._format_existing()
        parts: list[str] = []
        if existing:
            parts.append(existing)
        if context_rows:
            parts.append(
                "Earlier turns, ALREADY mined on a previous pass. They are "
                "here only so references in the new turns resolve. Do NOT "
                "extract memories from these lines:\n"
                + self._format_transcript(context_rows)
            )
        parts.append(
            "New turns since the last pass (most recent last). Extract ONLY "
            "from these lines:\n"
            + self._format_transcript(rows)
        )
        parts.append("Return the JSON now.")
        user_prompt = "\n\n".join(parts)

        now = timephrase.utcnow().astimezone()
        messages = [
            {
                "role": "system",
                "content": _build_system_prompt(
                    self._resolve_user_name(),
                    today=now,
                    max_memories=self._max_new_per_run,
                ),
            },
            {"role": "user", "content": user_prompt},
        ]

        t0 = time.monotonic()
        try:
            content, usage = self._ollama.chat_json(
                messages,
                model=self._model,
                timeout_seconds=self._timeout,
                options={"temperature": 0.2, "num_predict": self._max_tokens},
                format_json=True,
                think=self._think,
                surface="memory_extractor",
            )
        except Exception as exc:
            # No watermark advance: these turns were never actually read, so
            # they stay unmined and the next run picks them up again. The
            # alternative -- advancing on every attempt -- would turn a
            # transient Ollama timeout into permanently unmined turns.
            log.warning("memory extractor LLM call failed: %s", exc)
            return 0

        # done_reason="length" now reflects the THINKING + ANSWER total,
        # not just the JSON we parse. With think on, a complete answer
        # followed by a long reasoning trace legitimately hits the cap and
        # is NOT a problem. Only flag a real truncation when the answer
        # itself looks incomplete (content_looks_complete treats a closed
        # ``}`` / ``]`` as done); otherwise the cap only clipped reasoning,
        # which doesn't affect what we store. The client-level log carries
        # the answer~=/thinking~= token split for tuning.
        if (
            getattr(usage, "done_reason", None) == "length"
            and not content_looks_complete(content)
        ):
            log.warning(
                "memory extractor ANSWER truncated at num_predict=%d "
                "(completion=%d tokens incl. reasoning); salvaging complete "
                "objects. Raise memory.memory_extractor_max_tokens if this "
                "is frequent.",
                self._max_tokens, getattr(usage, "completion_tokens", 0),
            )

        # Raw-response trace (mirrors turn_runner's ``llm raw response:``).
        # The extractor's INFO line only reports COUNTS, so when a run
        # logs "no new memories" you can't tell whether the model judged
        # the transcript empty, returned a wrong-shaped object, or buried
        # the JSON behind a reasoning trace (thinking models emit a lot of
        # completion tokens that get stripped to an empty array). Enable
        # with set_log_level("app.memory_extractor", "DEBUG").
        log.debug("extractor raw response: %r", (content or "")[:1000])

        candidates, understood = self._parse_answer(content)
        if understood:
            # The model read these turns and gave an answer we could act on,
            # so they are mined -- including when the answer was "nothing
            # here", which is a verdict and not a failure. An unparseable
            # answer is the one case that leaves the watermark alone.
            self._write_watermark(session_key, rows[-1].id)
        if not candidates:
            # Distinguish "model returned an empty array" (genuinely nothing
            # durable — common on casual/short transcripts) from "model
            # emitted output we couldn't turn into candidates" (wrong shape,
            # stripped thinking trace, all-filtered). The stripped-content
            # length is the tell: a few chars means an empty/near-empty body,
            # a large body that still yields zero candidates means parsing or
            # validation dropped everything (see the DEBUG raw line above).
            stripped_len = len((content or "").strip())
            log.info(
                "extractor: no new memories (%d new msgs, %d context, %.0f ms, "
                "%d/%d tokens, response_chars=%d, parsed=%s)",
                len(rows), len(context_rows), (time.monotonic() - t0) * 1000.0,
                usage.prompt_tokens, usage.completion_tokens, stripped_len,
                understood,
            )
            return 0

        # Cap per run so a chatty model can't flood the store.
        if len(candidates) > self._max_new_per_run:
            candidates = candidates[: self._max_new_per_run]

        inserted = 0
        for cand in candidates:
            content_text = cand["content"]
            try:
                emb = self._embedder.embed(content_text)
            except Exception as exc:
                log.debug("embed failed for memory candidate: %s", exc)
                continue
            # v10: derive ``relevance_until`` server-side from the
            # candidate's ``temporal_type``. The LLM only needs to
            # classify the memory; we own the freshness window so a
            # buggy model can't poison RAG with permanent past_events.
            event_time_dt = _parse_iso(cand.get("event_time"))
            event_time_iso = event_time_dt.isoformat() if event_time_dt else None
            relevance_until = _derive_relevance_until(
                cand["temporal_type"],
                event_time=event_time_dt,
                created_at=now,
            )
            memory = self._store.add(
                content=content_text,
                kind=cand["kind"],
                embedding=emb,
                salience=cand["salience"],
                source_session=session_key,
                source_message_id=None,
                # Schema v8: LLM-distilled observations are speculative.
                # Land them in scratchpad so the promotion worker can
                # either confirm them via retrieval / revival or sweep
                # them away after the TTL.
                tier="scratchpad",
                temporal_type=cand["temporal_type"],
                # F16: the extractor distils memories from a transcript, so
                # each is testimony or inference per the LLM's classification
                # (defaulting to ``inferred`` -- see ``_validate_entries``).
                provenance=cand.get("provenance", _DEFAULT_PROVENANCE),
                event_time=event_time_iso,
                relevance_until=relevance_until,
            )
            if memory is not None:
                inserted += 1
                self._notify(memory)

        log.info(
            "extractor: %d new memories inserted (%d candidates, %.0f ms)",
            inserted, len(candidates), (time.monotonic() - t0) * 1000.0,
        )
        return inserted

    def _format_transcript(self, rows: list[MessageRow]) -> str:
        # K-time10: age-prefixed, so "see you tomorrow" said on Friday
        # resolves to Saturday rather than to whenever this batch happens
        # to run. The system prompt asks for absolute ``event_time``
        # values; without per-line stamps the model was being asked to do
        # that arithmetic with only one of the two operands.
        user_name = resolve_user_name(self._user_display_name_provider)
        return timephrase.format_transcript(
            rows, role_labels=speaker_labels(user_name),
        )

    def _format_existing(self) -> str:
        """The "do NOT re-emit these" block: salient rows *and* fresh ones.

        This used to be ``list_top(20)`` alone, which is ranked by salience
        and so systematically excluded the rows most at risk of being
        re-emitted. Everything the extractor writes lands in ``scratchpad``
        at whatever salience the model guessed, frequently 0.0; a row
        written four minutes ago is therefore nowhere near the top twenty,
        and the model was asked not to duplicate itself while being shown
        only the memories it was least likely to duplicate. The recent half
        of the block is what closes that gap, and it is the guard the
        watermark leans on for claims that straddle a window boundary.
        """
        seen: set[int] = set()
        merged: list = []
        for mem in list(self._store.list_recent(limit=12)) + list(
            self._store.list_top(limit=20)
        ):
            mem_id = getattr(mem, "id", None)
            if mem_id is not None:
                if mem_id in seen:
                    continue
                seen.add(mem_id)
            merged.append(mem)
        if not merged:
            return ""
        # Age-tagged (K-time10): the dedupe judgement is partly temporal.
        # "Jacob has a headache" from six weeks ago is not the same claim
        # as one from this morning, and an undated list invited the model
        # to treat them as duplicates.
        return timephrase.format_memory_block(
            merged, header="Existing memories (do NOT re-emit these):",
        )

    def _parse_response(self, raw: str) -> list[dict]:
        """Validate the model's JSON and return a list of candidate dicts."""
        return self._parse_answer(raw)[0]

    def _parse_answer(self, raw: str) -> tuple[list[dict], bool]:
        """As :meth:`_parse_response`, plus "did we understand the answer".

        The two are different questions and the watermark needs the second
        one. An empty candidate list is ambiguous on its own: it is what a
        transcript of pure chitchat produces, and also what a wrong-shaped
        or unreadable response produces. Advancing past unmined turns
        because the model emitted garbage would lose them silently, so the
        boolean says whether the model actually answered — ``True`` for a
        well-formed empty array, ``False`` for anything we could not read.
        """
        text = (raw or "").strip()
        if not text:
            return [], False
        # Handle code-fenced JSON (the model sometimes ignores format=json).
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            # The single most common failure is a length-truncated
            # response: the ``"memories": [...]`` array is cut mid-object
            # so the whole blob won't parse. Salvage the complete leading
            # objects instead of dropping the entire batch.
            salvaged = _salvage_memories(text)
            if salvaged:
                log.info(
                    "extractor: salvaged %d complete memory object(s) from "
                    "a truncated/invalid response", len(salvaged),
                )
                # Salvage counts as understood: whatever was cut off was cut
                # from the *answer*, not from the reading of the transcript,
                # and re-running would re-answer the same turns.
                return self._validate_entries(salvaged), True
            log.warning("extractor: response was not valid JSON: %r", text[:200])
            return [], False
        if not isinstance(parsed, dict):
            return [], False
        memories = parsed.get("memories")
        if not isinstance(memories, list):
            return [], False
        return self._validate_entries(memories), True

    def _validate_entries(self, memories: list) -> list[dict]:
        """Normalise + filter a list of raw memory dicts into candidates."""
        user_name = self._resolve_user_name()
        out: list[dict] = []
        for entry in memories:
            if not isinstance(entry, dict):
                continue
            content = str(entry.get("content") or "").strip()
            if not content or len(content) < 6:
                continue
            kind = str(entry.get("kind") or "").strip().lower()
            if kind not in VALID_KINDS:
                kind = _fallback_kind(content)
            elif kind == "self" and _opens_about_user(content, user_name):
                # The model chose the self bucket for a sentence about the
                # user. Reading the sentence is more reliable than reading
                # the label, and the label is the field that decides whose
                # life the claim describes.
                kind = "fact"
            try:
                salience = float(entry.get("salience", 0.5))
            except (TypeError, ValueError):
                salience = 0.5
            salience = max(0.0, min(1.0, salience))
            # v10: temporal_type defaults to ``durable`` for unknown /
            # missing values so legacy outputs and noisy LLMs don't
            # crash the insert. ``event_time`` is left as a raw string
            # here; ``_parse_iso`` in the caller validates the format
            # and falls back to ``None`` on bad data.
            temporal_type = str(entry.get("temporal_type") or _DEFAULT_TEMPORAL_TYPE)
            temporal_type = temporal_type.strip().lower()
            if temporal_type not in VALID_TEMPORAL_TYPES:
                temporal_type = _DEFAULT_TEMPORAL_TYPE
            # F16 (v30): provenance defaults to ``inferred`` for unknown /
            # missing values -- over-claiming testimony is the failure this
            # fixes, so anything the LLM leaves off lands on the safe side.
            provenance = str(entry.get("provenance") or _DEFAULT_PROVENANCE)
            provenance = provenance.strip().lower()
            if provenance not in VALID_PROVENANCE:
                provenance = _DEFAULT_PROVENANCE
            event_time_raw = entry.get("event_time")
            event_time = (
                str(event_time_raw).strip()
                if isinstance(event_time_raw, str) and event_time_raw.strip()
                else None
            )
            out.append(
                {
                    "content": content,
                    "kind": kind,
                    "salience": salience,
                    "temporal_type": temporal_type,
                    "provenance": provenance,
                    "event_time": event_time,
                }
            )
        return out

    def _notify(self, memory) -> None:
        for cb in list(self._on_added_listeners):
            try:
                cb(memory)
            except Exception:
                log.debug("memory listener raised", exc_info=True)
