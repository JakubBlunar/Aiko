from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib


@dataclass(slots=True)
class ReadingSessionConfig:
    memory_enabled: bool
    max_scroll_steps: int
    max_quotes: int
    max_quote_chars: int
    trusted_window_titles: list[str]


class ReadingSessionManager:
    def __init__(self, config: ReadingSessionConfig) -> None:
        self._memory_enabled = bool(config.memory_enabled)
        self._max_scroll_steps = max(1, int(config.max_scroll_steps))
        self._max_quotes = max(1, int(config.max_quotes))
        self._max_quote_chars = max(120, int(config.max_quote_chars))
        self._trusted_window_titles = [
            str(item).strip().lower() for item in (config.trusted_window_titles or []) if str(item).strip()
        ]

        self._active = False
        self._window_title = ""
        self._chunks: list[str] = []
        self._chunk_hashes: set[str] = set()
        self._scroll_steps = 0
        self._last_summary = ""

    @property
    def memory_enabled(self) -> bool:
        return self._memory_enabled

    @property
    def active(self) -> bool:
        return self._active

    @property
    def max_scroll_steps(self) -> int:
        return self._max_scroll_steps

    @property
    def scroll_steps(self) -> int:
        return self._scroll_steps

    @property
    def chunk_hash_count(self) -> int:
        return len(self._chunk_hashes)

    @staticmethod
    def is_reading_intent(user_text: str) -> bool:
        lowered = str(user_text or "").strip().lower()
        if not lowered:
            return False
        tokens = (
            "read this",
            "read article",
            "read the article",
            "what do you see",
            "what did you see",
            "read on screen",
            "summarize this page",
        )
        return any(token in lowered for token in tokens)

    @staticmethod
    def is_continue_reading_request(user_text: str) -> bool:
        lowered = str(user_text or "").strip().lower()
        if not lowered:
            return False
        tokens = (
            "continue reading",
            "read more",
            "next part",
            "scroll more",
            "keep reading",
        )
        return any(token in lowered for token in tokens)

    @staticmethod
    def is_reading_evidence_request(user_text: str) -> bool:
        lowered = str(user_text or "").strip().lower()
        if not lowered:
            return False
        tokens = ("what did you see", "show snippet", "quote", "evidence", "summary")
        return any(token in lowered for token in tokens)

    def is_trusted_window(self, foreground_window_title: str) -> bool:
        title = str(foreground_window_title or "").strip().lower()
        if not title:
            return False
        if not self._trusted_window_titles:
            return True
        return any(token in title for token in self._trusted_window_titles)

    def stop(self, trace: Callable[[str, str], None]) -> bool:
        was_active = bool(self._active)
        self._active = False
        self._window_title = ""
        self._chunks.clear()
        self._chunk_hashes.clear()
        self._scroll_steps = 0
        self._last_summary = ""
        trace("reading.stop", "Reading session cleared by user request.")
        return was_active

    def get_status(self) -> dict[str, bool | int | str]:
        return {
            "active": bool(self._active),
            "window": str(self._window_title or ""),
            "chunks": int(len(self._chunks)),
            "scroll_steps": int(self._scroll_steps),
            "max_scroll_steps": int(self._max_scroll_steps),
        }

    def update(
        self,
        *,
        user_text: str,
        screen_text: str | None,
        foreground_window_title: str,
        trace: Callable[[str, str], None],
    ) -> None:
        if not self._memory_enabled or not screen_text:
            return

        reading_intent = self.is_reading_intent(user_text)
        continue_request = self.is_continue_reading_request(user_text)
        if not (self._active or reading_intent or continue_request):
            return

        if not self.is_trusted_window(foreground_window_title):
            trace(
                "reading.blocked",
                f"Untrusted foreground window for reading: {foreground_window_title or '[unknown]'}",
            )
            return

        if not self._active:
            self._active = True
            self._window_title = str(foreground_window_title or "")
            self._chunks.clear()
            self._chunk_hashes.clear()
            self._scroll_steps = 0
            self._last_summary = ""
            trace("reading.start", f"window={self._window_title or '[unknown]'}")

        normalized = "\n".join(line.strip() for line in str(screen_text).splitlines() if line.strip())
        if not normalized:
            return
        digest = hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()
        if digest in self._chunk_hashes:
            trace("reading.chunk", "Skipped duplicate OCR chunk.")
            return

        self._chunk_hashes.add(digest)
        self._chunks.append(normalized[:2400])
        if len(self._chunks) > 10:
            self._chunks = self._chunks[-10:]
        trace("reading.chunk", f"chunks={len(self._chunks)}")

    def build_context_for_prompt(self) -> str:
        if not self._active or not self._chunks:
            return ""
        recent = self._chunks[-2:]
        combined = "\n\n".join(recent)
        return (
            "Active reading session context (recent captures):\n"
            f"{combined[:1800]}"
        )

    def can_continue_after_approval(self) -> bool:
        if not self._memory_enabled or not self._active:
            return False
        return self._scroll_steps < self._max_scroll_steps

    def increment_scroll_step(self) -> None:
        self._scroll_steps += 1

    def build_evidence_block(self, trace: Callable[[str, str], None]) -> str:
        if not self._active or not self._chunks:
            return ""

        quotes: list[str] = []
        used = 0
        for chunk in reversed(self._chunks):
            for line in chunk.splitlines():
                text = line.strip()
                if len(text) < 35:
                    continue
                if text in quotes:
                    continue
                trimmed = text[:160]
                extra = len(trimmed)
                if used + extra > self._max_quote_chars:
                    continue
                quotes.append(trimmed)
                used += extra
                if len(quotes) >= self._max_quotes:
                    break
            if len(quotes) >= self._max_quotes:
                break

        if not quotes:
            return ""

        bullets = "\n".join(f"- \"{q}\"" for q in quotes)
        summary = (
            f"Summary: I captured {len(quotes)} excerpt(s) from "
            f"{self._window_title or 'the active window'} across {len(self._chunks)} screen chunk(s)."
        )
        self._last_summary = summary
        trace("reading.summary", summary)
        return f"Reading evidence:\n{bullets}\n{summary}"
