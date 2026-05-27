"""Background extractor that mines durable facts out of a chat transcript.

Triggered by :class:`SummaryWorker` right after a successful
``save_summary``: at that point the conversation is paused, the GPU is free,
and there's a fresh batch of unsummarized turns whose long-term-relevant
content we want to capture.

The extractor runs ONE ``chat_json`` call against the same chat model the
user is talking to (no separate judge model -- avoids extra model swaps and
GPU thrashing). The model is asked for a JSON list of memories:

    {"memories": [{"content": "...", "kind": "preference", "salience": 0.7}, ...]}

Each candidate is validated, embedded, and pushed into :class:`MemoryStore`,
which dedupes against already-stored near-duplicates.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Iterable

from app.core.chat_database import ChatDatabase, MessageRow
from app.core.memory_store import VALID_KINDS, MemoryStore
from app.llm.embedder import Embedder
from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.memory_extractor")


_SYSTEM_PROMPT = (
    "You analyse a chat transcript between a user named Jacob and his AI "
    "companion Aiko. Your job is to extract DURABLE memories that would "
    "still be relevant in a month.\n"
    "\n"
    "Two kinds of memories are allowed:\n"
    "  1. Facts about Jacob: real preferences, opinions, ongoing projects, "
    "     important events, relationships, recurring jokes. One short "
    "     sentence in THIRD person ('Jacob ...').\n"
    "  2. Aiko's notes about herself: a stance, a taste, a decision Aiko "
    "     made about her own personality that she wants to keep next time. "
    "     One short sentence in FIRST person ('I ...').\n"
    "\n"
    "Rules:\n"
    "- Skip throwaway chitchat, single-turn moods, weather, jokes that are "
    "  not recurring.\n"
    "- Skip anything already in the existing memory list.\n"
    "- If nothing is worth remembering, return an empty array.\n"
    "- 'kind' must be one of: fact, preference, event, relationship, self. "
    "  Use 'self' only for Aiko's first-person notes.\n"
    "- 'salience' is 0..1 -- how much this should drive future conversation.\n"
    "\n"
    'Reply with JSON only, exactly: {"memories": [{"content": "...", '
    '"kind": "...", "salience": 0.5}]}'
)


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

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
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
        parts: list[str] = []
        for row in rows:
            speaker = "Jacob" if row.role == "user" else "Aiko"
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
            out.append({"content": content, "kind": kind, "salience": salience})
        return out

    def _notify(self, memory) -> None:
        for cb in list(self._on_added_listeners):
            try:
                cb(memory)
            except Exception:
                log.debug("memory listener raised", exc_info=True)
