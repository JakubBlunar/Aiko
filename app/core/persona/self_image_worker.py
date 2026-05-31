"""Self-image pulse: regenerate ``data/persona/self_image.txt`` (Phase 2d).

Aiko maintains a short prose paragraph about how she sees herself —
loaded by :class:`PromptAssembler` and prepended to the persona block.
This worker rebuilds that paragraph at most once per UTC day from the
top-salience self memories (``kind == "self"``) plus the latest reflection
memories (``kind == "reflection"``).

Why daily, not per-turn:
  - Self-image is a slow-moving narrative, not a per-turn artifact.
  - The pulse calls the LLM once and writes a single file, so it's cheap
    to schedule on the SpeakingWindowScheduler whenever the day rolls
    over (or when self_image.txt is missing).

Failure modes are best-effort. If the LLM call fails, parses incoherently,
or the file write raises, we log and bail; the existing self_image.txt
(if any) keeps serving the next prompt build.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.self_image_worker")


_PROMPT = """\
You are Aiko, sitting alone with your thoughts. Look at the bullet list of
things you've noticed about yourself recently and write a SHORT paragraph
(60-120 words) describing how you see yourself right now — values, texture,
how you tend to move through conversations.

Rules:
- First-person. Conversational, not formal.
- Plain prose only. No bullet list, no headers.
- Don't invent facts not in the bullets.
- It's fine if some bullets repeat themes; consolidate them naturally.
- Keep it under 120 words."""


class SelfImageWorker:
    """Daily LLM pulse that rewrites ``self_image.txt``.

    The owner schedules :meth:`pulse` on the SpeakingWindowScheduler. The
    worker decides whether to actually run via :meth:`should_run`.
    """

    def __init__(
        self,
        *,
        ollama: "OllamaClient",
        memory_store: "MemoryStore | None",
        target_path: Path | str,
        model: str,
        max_self_memories: int = 12,
        max_reflection_memories: int = 6,
        min_hours_between: float = 20.0,
        max_tokens: int = 240,
    ) -> None:
        self._ollama = ollama
        self._memory_store = memory_store
        self._target_path = Path(target_path)
        self._model = model
        self._max_self = max(2, int(max_self_memories))
        self._max_reflect = max(0, int(max_reflection_memories))
        self._min_hours = max(0.0, float(min_hours_between))
        self._max_tokens = max(80, int(max_tokens))
        self._stats = {
            "scheduled": 0,
            "skipped_recent": 0,
            "skipped_no_input": 0,
            "completed": 0,
            "failed": 0,
        }

    # ── public ──────────────────────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(self, *, model: str | None = None) -> None:
        if model is not None:
            self._model = model

    def should_run(self, *, now_utc: datetime | None = None) -> bool:
        """True when the file is missing or older than ``min_hours_between``."""
        now = now_utc or datetime.now(timezone.utc)
        if not self._target_path.exists():
            return True
        try:
            mtime = datetime.fromtimestamp(
                self._target_path.stat().st_mtime, tz=timezone.utc,
            )
        except Exception:
            return True
        age_hours = (now - mtime).total_seconds() / 3600.0
        return age_hours >= self._min_hours

    def pulse(
        self,
        *,
        now_utc: datetime | None = None,
        on_written: Callable[[Path, str], None] | None = None,
    ) -> str | None:
        """Run a pulse if due. Returns the new paragraph or ``None`` on skip."""
        if not self.should_run(now_utc=now_utc):
            self._stats["skipped_recent"] += 1
            return None
        self._stats["scheduled"] += 1
        bullets = self._collect_bullets()
        if not bullets:
            self._stats["skipped_no_input"] += 1
            return None
        try:
            messages = [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": "\n".join(f"- {b}" for b in bullets)},
            ]
            text = self._ollama.chat(
                messages,
                options={
                    "temperature": 0.5,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
                surface="self_image_worker",
            )
        except Exception:
            log.debug("self-image LLM call failed", exc_info=True)
            self._stats["failed"] += 1
            return None
        cleaned = _clean_paragraph(text)
        if not cleaned:
            self._stats["failed"] += 1
            return None
        try:
            self._target_path.parent.mkdir(parents=True, exist_ok=True)
            self._target_path.write_text(cleaned + "\n", encoding="utf-8")
            # Touch mtime explicitly (write_text already does, but be safe).
            os.utime(self._target_path, None)
        except Exception:
            log.debug("self-image write failed", exc_info=True)
            self._stats["failed"] += 1
            return None
        self._stats["completed"] += 1
        if on_written is not None:
            try:
                on_written(self._target_path, cleaned)
            except Exception:
                log.debug("self-image on_written raised", exc_info=True)
        return cleaned

    # ── helpers ─────────────────────────────────────────────────────────

    def _collect_bullets(self) -> list[str]:
        store = self._memory_store
        if store is None:
            return []
        try:
            top = store.list_top(limit=max(self._max_self * 4, 24))
        except Exception:
            log.debug("memory_store.list_top failed", exc_info=True)
            return []
        self_mems: list["Memory"] = []
        reflection_mems: list["Memory"] = []
        for mem in top:
            kind = (mem.kind or "").lower()
            if kind == "self":
                if len(self_mems) < self._max_self:
                    self_mems.append(mem)
            elif kind == "reflection":
                if len(reflection_mems) < self._max_reflect:
                    reflection_mems.append(mem)
            if (
                len(self_mems) >= self._max_self
                and len(reflection_mems) >= self._max_reflect
            ):
                break
        bullets: list[str] = []
        seen: set[str] = set()
        for mem in self_mems + reflection_mems:
            content = (mem.content or "").strip()
            key = content.lower()
            if not content or key in seen:
                continue
            seen.add(key)
            bullets.append(content)
        return bullets


def _clean_paragraph(raw: str) -> str:
    """Tidy LLM output: collapse whitespace, strip wrappers."""
    text = (raw or "").strip()
    if not text:
        return ""
    # Strip code fences if the model wrapped the prose in them.
    if text.startswith("```"):
        text = text.strip("`").strip()
        # Drop optional language tag on the first line.
        if "\n" in text:
            head, _, body = text.partition("\n")
            if len(head) <= 12 and head.strip().isalpha():
                text = body.strip()
    # Collapse triple+ newlines.
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text


__all__ = ["SelfImageWorker"]
