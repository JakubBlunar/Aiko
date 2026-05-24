"""Build the message list sent to Ollama on every turn.

Inputs (all optional):
  - persona file (data/persona/aiko_companion.txt)
  - long-term memory block from :class:`MemoryRetriever` (cross-session)
  - latest summary row (covers everything before the recent window)
  - last N messages from chat_database.messages
  - the new user input

Output: ``list[dict]`` ready for ``OllamaClient.chat_stream``.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from app.core.chat_database import ChatDatabase, MessageRow
from app.llm.token_utils import estimate_messages_tokens, estimate_tokens

if TYPE_CHECKING:
    from app.core.memory_retriever import MemoryRetriever


log = logging.getLogger("app.prompt_assembler")

DEFAULT_PERSONA_PATH = Path("data/persona/aiko_companion.txt")

# Reserve a buffer between (estimated tokens used) and (model's context window)
# so we never send a request that bumps against the limit and gets truncated
# server-side.
_SAFETY_TOKENS = 256


class PromptAssembler:
    def __init__(
        self,
        db: ChatDatabase,
        *,
        persona_path: Path | str = DEFAULT_PERSONA_PATH,
        recent_window: int = 20,
        memory_retriever: "MemoryRetriever | None" = None,
    ) -> None:
        self._db = db
        self._persona_path = Path(persona_path)
        self._recent_window = max(2, int(recent_window))
        self._persona_cache: tuple[float, str] | None = None
        self._memory_retriever = memory_retriever

    def set_memory_retriever(self, retriever: "MemoryRetriever | None") -> None:
        self._memory_retriever = retriever

    # ── public API ────────────────────────────────────────────────────────

    def reload_persona(self) -> None:
        """Force re-read on next ``build()`` call."""
        self._persona_cache = None

    def build(
        self,
        session_key: str,
        user_text: str,
        *,
        context_window: int,
        response_budget: int,
    ) -> list[dict[str, Any]]:
        """Compose the full message list for the next LLM call.

        ``context_window`` and ``response_budget`` come from settings; they
        determine how aggressively to trim history. ``user_text`` is the new
        turn from the human and is appended at the end.
        """
        persona = self._load_persona()
        summary = self._db.get_latest_summary(session_key)

        memory_block = ""
        if self._memory_retriever is not None:
            try:
                memory_block = self._memory_retriever.block_for(user_text)
            except Exception:
                log.debug("memory retrieval failed", exc_info=True)
                memory_block = ""

        system_parts: list[str] = []
        if persona:
            system_parts.append(persona)
        if memory_block:
            system_parts.append(memory_block)
        if summary and summary.summary.strip():
            system_parts.append(
                "Earlier conversation (summary):\n" + summary.summary.strip()
            )

        system_prompt = "\n\n---\n\n".join(p for p in system_parts if p)

        history_msgs = self._db.get_messages(session_key, limit=self._recent_window)
        # history is oldest-first already
        messages: list[dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        budget = max(512, int(context_window) - int(response_budget) - _SAFETY_TOKENS)
        history_dicts = self._fit_history(history_msgs, system_prompt, budget)
        messages.extend(history_dicts)

        cleaned_user = (user_text or "").strip()
        if cleaned_user:
            messages.append({"role": "user", "content": cleaned_user})

        log.debug(
            "prompt built: ctx=%d budget=%d sys=%d hist=%d total~%d",
            context_window,
            budget,
            estimate_tokens(system_prompt),
            len(history_dicts),
            self._estimate(messages),
        )
        return messages

    # ── helpers ───────────────────────────────────────────────────────────

    def _load_persona(self) -> str:
        path = self._persona_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        if self._persona_cache is not None and self._persona_cache[0] == mtime:
            return self._persona_cache[1]
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("persona file %s unreadable: %s", path, exc)
            text = ""
        self._persona_cache = (mtime, text)
        return text

    @staticmethod
    def _fit_history(
        history: list[MessageRow],
        system_prompt: str,
        budget_tokens: int,
    ) -> list[dict[str, Any]]:
        sys_tokens = estimate_tokens(system_prompt) + 4
        remaining = max(256, budget_tokens - sys_tokens)
        kept: list[dict[str, Any]] = []
        running = 0
        for row in reversed(history):
            content = (row.content or "").strip()
            if not content:
                continue
            cost = estimate_tokens(content) + 4
            if running + cost > remaining:
                break
            role = "assistant" if row.role == "assistant" else "user"
            kept.append({"role": role, "content": content})
            running += cost
        kept.reverse()
        return kept

    @staticmethod
    def _estimate(messages: list[dict[str, Any]]) -> int:
        # Reuse the LangChain-shaped estimator on duck-typed dicts.
        class _Shim:
            def __init__(self, content: str) -> None:
                self.content = content

        return estimate_messages_tokens([_Shim(m.get("content", "")) for m in messages])
