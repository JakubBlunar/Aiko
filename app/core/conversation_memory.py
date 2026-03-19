from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


DEFAULT_MEMORY_PATH = Path(__file__).resolve().parents[2] / "data" / "conversation_memory.jsonl"


@dataclass(slots=True)
class MemoryEntry:
    role: str
    content: str
    timestamp: str


class ConversationMemoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_MEMORY_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, *, role: str, content: str) -> None:
        role = (role or "").strip().lower()
        content = (content or "").strip()
        if role not in {"user", "assistant"} or not content:
            return

        entry = MemoryEntry(
            role=role,
            content=content,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")

    @staticmethod
    def _tail_lines(path: Path, n: int) -> list[str]:
        """Read the last *n* lines from a JSONL file without loading the whole file."""
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if size == 0:
            return []
        buf_size = min(size, max(4096, n * 512))
        with path.open("rb") as fh:
            fh.seek(max(0, size - buf_size))
            tail = fh.read().decode("utf-8", errors="replace")
        lines = tail.splitlines()
        if fh.tell() > buf_size:
            lines = lines[1:]
        return lines[-n:]

    def recent_messages(self, max_messages: int = 12) -> list[dict[str, str]]:
        if not self._path.exists():
            return []

        selected = self._tail_lines(self._path, max(1, max_messages))

        output: list[dict[str, str]] = []
        for line in selected:
            try:
                obj: dict[str, Any] = json.loads(line)
            except Exception:
                continue
            role = str(obj.get("role", "")).strip().lower()
            content = str(obj.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                output.append({"role": role, "content": content})
        return output

    def recent_entries(self, max_entries: int = 200) -> list[MemoryEntry]:
        if not self._path.exists():
            return []

        selected = self._tail_lines(self._path, max(1, max_entries))

        output: list[MemoryEntry] = []
        for line in selected:
            try:
                obj: dict[str, Any] = json.loads(line)
            except Exception:
                continue

            role = str(obj.get("role", "")).strip().lower()
            content = str(obj.get("content", "")).strip()
            timestamp = str(obj.get("timestamp", "")).strip()
            if role in {"user", "assistant"} and content:
                output.append(MemoryEntry(role=role, content=content, timestamp=timestamp))
        return output

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()
