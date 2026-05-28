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
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from app.core.chat_database import ChatDatabase, MessageRow
from app.core.memory_store import (
    VALID_KINDS,
    VALID_TEMPORAL_TYPES,
    MemoryStore,
    _DEFAULT_TEMPORAL_TYPE,
)
from app.core.session_text_utils import resolve_user_name, speaker_label
from app.llm.embedder import Embedder
from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.memory_extractor")


# Server-side relevance windows per temporal_type. The LLM only emits
# the *type* (and optionally event_time); we derive when retrieval
# should stop surfacing the row in normal RAG. ``None`` here means
# "no expiry" (pure timeless memories — preferences and durable
# facts).
_RELEVANCE_WINDOW: dict[str, timedelta | None] = {
    "durable": None,
    "preference": None,
    "ongoing": timedelta(days=30),
    "past_event": timedelta(days=7),
    # ``future_plan`` uses a special derivation: relevance_until =
    # event_time + 1 day (so we still have a window after the event
    # to ask "how was it?"). The extractor falls back to created_at +
    # 2 days when event_time is missing.
    "future_plan": timedelta(days=2),
}


def _derive_relevance_until(
    temporal_type: str,
    *,
    event_time: datetime | None,
    created_at: datetime,
) -> str | None:
    """Compute the v10 ``relevance_until`` from the candidate's type.

    ``past_event`` / ``ongoing`` measure from ``created_at`` (when we
    learned about it). ``future_plan`` measures from ``event_time`` +
    1 day so retrieval stops surfacing the plan after the day-after
    window closes; the decay worker reclassifies the row to
    ``past_event`` shortly after ``event_time`` passes anyway.
    ``durable`` and ``preference`` return ``None`` (no expiry).
    """
    if temporal_type == "future_plan":
        anchor = event_time if event_time is not None else created_at
        return (anchor + timedelta(days=1)).isoformat()
    window = _RELEVANCE_WINDOW.get(temporal_type)
    if window is None:
        return None
    return (created_at + window).isoformat()


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
        today = datetime.now(timezone.utc).astimezone()
    today_human = today.strftime("%A, %B %d, %Y, %H:%M %Z").strip()
    today_iso = today.isoformat()
    valid_types = ", ".join(VALID_TEMPORAL_TYPES)
    return (
        f"You analyse a chat transcript between a user named {name} and his AI "
        "companion Aiko. Your job is to extract DURABLE memories that would "
        "still be relevant later, plus any time-bound events worth "
        "remembering with their absolute timestamp.\n"
        "\n"
        f"Today is {today_human} ({today_iso}). Use this anchor to resolve "
        "relative phrases the user says ('yesterday', 'tonight at 8', "
        "'next Monday', 'in two weeks') into absolute ISO-8601 timestamps "
        "in the ``event_time`` field. If the user gives only a date with no "
        "clock time, set the time to noon local. If the user is vague "
        "('soon', 'eventually'), leave ``event_time`` null.\n"
        "\n"
        "Two kinds of memories are allowed:\n"
        f"  1. Facts about {name}: real preferences, opinions, ongoing projects, "
        "     important events (past or future), relationships, recurring jokes. "
        f"     One short sentence in THIRD person ('{name} ...').\n"
        "  2. Aiko's notes about herself: a stance, a taste, a decision Aiko "
        "     made about her own personality that she wants to keep next time. "
        "     One short sentence in FIRST person ('I ...').\n"
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
        "    gym tonight at 8'). REQUIRED to set ``event_time`` to when "
        "    it's supposed to happen.\n"
        "\n"
        "Rules:\n"
        "- Skip throwaway chitchat, single-turn moods, weather, jokes that are "
        "  not recurring.\n"
        "- Skip anything already in the existing memory list.\n"
        "- If nothing is worth remembering, return an empty array.\n"
        "- 'kind' must be one of: fact, preference, event, relationship, self. "
        "  Use 'self' only for Aiko's first-person notes.\n"
        f"- 'temporal_type' must be one of: {valid_types}. Default to "
        "  'durable' when unsure.\n"
        "- 'salience' is 0..1 -- how much this should drive future conversation.\n"
        "- Phrase the content with proper tense based on temporal_type. "
        "  past_event: past tense ('Jacob finished the dashboard'). "
        "  future_plan: future tense ('Jacob plans to go to the gym at 20:00 tonight'). "
        "  Avoid leaving raw 'yesterday'/'tonight' words in content — they "
        "  go stale immediately. The event_time field carries the precise "
        "  moment.\n"
        "\n"
        'Reply with JSON only, exactly: {"memories": [{"content": "...", '
        '"kind": "...", "salience": 0.5, "temporal_type": "...", '
        '"event_time": "ISO-8601 or null"}]}'
    )


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
        max_new_per_run: int = 5,
        timeout_seconds: float = 60.0,
        user_display_name_provider: "Callable[[], str] | None" = None,
    ) -> None:
        self._db = db
        self._store = store
        self._embedder = embedder
        self._ollama = ollama
        self._model = model
        self._min_window = max(2, int(min_window_messages))
        self._max_window = max(self._min_window, int(max_window_messages))
        self._max_new_per_run = max(1, int(max_new_per_run))
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

    def _do_extract(self, session_key: str) -> int:
        rows = self._db.get_messages(session_key, limit=self._max_window)
        if len(rows) < self._min_window:
            log.debug(
                "extract skipped: only %d messages (need %d)",
                len(rows), self._min_window,
            )
            return 0

        transcript = self._format_transcript(rows)
        existing = self._format_existing()
        user_prompt = (
            (existing + "\n\n" if existing else "")
            + "Transcript (most recent last):\n"
            + transcript
            + "\n\nReturn the JSON now."
        )

        now = datetime.now(timezone.utc).astimezone()
        messages = [
            {
                "role": "system",
                "content": _build_system_prompt(
                    self._resolve_user_name(),
                    today=now,
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
                options={"temperature": 0.2, "num_predict": 512},
                format_json=True,
            )
        except Exception as exc:
            log.warning("memory extractor LLM call failed: %s", exc)
            return 0

        candidates = self._parse_response(content)
        if not candidates:
            log.info(
                "extractor: no new memories (transcript %d msgs, %.0f ms, %d/%d tokens)",
                len(rows), (time.monotonic() - t0) * 1000.0,
                usage.prompt_tokens, usage.completion_tokens,
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
        user_name = resolve_user_name(self._user_display_name_provider)
        parts: list[str] = []
        for row in rows:
            speaker = speaker_label(row.role, user_name)
            content = (row.content or "").strip()
            if not content:
                continue
            parts.append(f"{speaker}: {content}")
        return "\n".join(parts)

    def _format_existing(self) -> str:
        # Just enough recent + salient memories to discourage duplicates.
        recent = self._store.list_top(limit=20)
        if not recent:
            return ""
        lines = ["Existing memories (do NOT re-emit these):"]
        for mem in recent:
            lines.append(f"- {mem.content}")
        return "\n".join(lines)

    def _parse_response(self, raw: str) -> list[dict]:
        """Validate the model's JSON and return a list of candidate dicts."""
        text = (raw or "").strip()
        if not text:
            return []
        # Handle code-fenced JSON (the model sometimes ignores format=json).
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            log.warning("extractor: response was not valid JSON: %r", text[:200])
            return []
        if not isinstance(parsed, dict):
            return []
        memories = parsed.get("memories")
        if not isinstance(memories, list):
            return []
        out: list[dict] = []
        for entry in memories:
            if not isinstance(entry, dict):
                continue
            content = str(entry.get("content") or "").strip()
            if not content or len(content) < 6:
                continue
            kind = str(entry.get("kind") or "fact").strip().lower()
            if kind not in VALID_KINDS:
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
