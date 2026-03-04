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

    def recent_messages(self, max_messages: int = 12) -> list[dict[str, str]]:
        if not self._path.exists():
            return []

        lines = self._path.read_text(encoding="utf-8").splitlines()
        selected = lines[-max(1, max_messages) :]

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

        lines = self._path.read_text(encoding="utf-8").splitlines()
        selected = lines[-max(1, max_entries) :]

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
