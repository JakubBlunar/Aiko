"""User-profile store + worker (Phase 3a).

Aiko keeps a small structured profile of the user — name, occupation,
hobbies, communication style, current goals — that's distilled from the
conversation by a low-priority LLM job during the TTS speaking window.

Each profile field is stored as a row in ``user_profile``: one ``(user_id,
field, value, confidence, updated_at)`` per fact. The worker runs every
N user turns and re-extracts the small set of fields below from the
recent conversation, merging new observations with existing entries by
confidence-weighted upsert.

Hot path:
  - ``UserProfileStore.render_block(user_id)`` — cheap SQL read; produces
    the prompt-ready text block.
  - ``UserProfileStore.fields(user_id)`` — raw dict for the MCP tool.

Background path (speaking window only):
  - ``UserProfileWorker.maybe_run(...)`` — gates on N turns + minimum-input.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.user_profile")


# Schema-aware list of profile fields. The worker is asked to fill any
# of these it can, omitting the ones it can't infer. Adding a new field:
# bump the worker's prompt + this list. (Schema is generic — no migration.)
PROFILE_FIELDS: tuple[str, ...] = (
    "name",
    "occupation",
    "location",
    "hobbies",
    "communication_style",
    "current_focus",
    "values",
    "goals",
)

_PROMPT = """\
You are Aiko, journaling between turns. Look at the recent conversation
with Jacob and update your mental notes about him.

Respond with ONE JSON object on a single line:
{
  "fields": {
    "<field>": {"value": "<short text, <=80 chars>", "confidence": <0..1>}
    ...
  }
}

Allowed fields (omit any you can't infer):
  - name              (his name or what he goes by)
  - occupation        (what he does for work / school)
  - location          (city / region / context only — NOT exact address)
  - hobbies           (1-3 hobbies, comma-separated)
  - communication_style  (e.g. "concise", "playful", "asks follow-ups")
  - current_focus     (what he's been focused on lately)
  - values            (1-3 things he seems to care about)
  - goals             (an active goal he mentioned)

Rules:
- Output ONLY valid JSON. No prose around it.
- A field is OMITTED if you have no evidence — do NOT guess.
- "value" must be a short phrase, NOT a full sentence with quotes.
- "confidence" reflects how sure you are (0.0..1.0).
- It's fine to return {"fields": {}} when nothing new is observable."""


@dataclass(slots=True)
class ProfileEntry:
    user_id: str
    field: str
    value: str
    confidence: float
    updated_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "field": self.field,
            "value": self.value,
            "confidence": round(float(self.confidence), 3),
            "updated_at": self.updated_at,
        }


class UserProfileStore:
    """SQLite CRUD + prompt rendering for the ``user_profile`` table.

    Read paths are intentionally chatty (small queries, no caching) so the
    block is always fresh after the worker writes a row. SQLite handles
    the contention fine at this volume.
    """

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    # ── reads ───────────────────────────────────────────────────────────

    def fields(self, user_id: str) -> dict[str, ProfileEntry]:
        if not user_id:
            return {}
        rows = self._db.execute_fetchall(
            "SELECT user_id, field, value, confidence, updated_at "
            "FROM user_profile WHERE user_id = ? ORDER BY confidence DESC",
            (user_id,),
        )
        out: dict[str, ProfileEntry] = {}
        for row in rows:
            entry = ProfileEntry(
                user_id=str(row[0]),
                field=str(row[1]),
                value=str(row[2]),
                confidence=float(row[3] or 0.0),
                updated_at=str(row[4] or ""),
            )
            out[entry.field] = entry
        return out

    def as_dict(self, user_id: str) -> dict[str, dict[str, object]]:
        return {f: e.to_dict() for f, e in self.fields(user_id).items()}

    def render_block(self, user_id: str, *, min_confidence: float = 0.4) -> str:
        entries = self.fields(user_id)
        if not entries:
            return ""
        lines: list[str] = []
        for entry in entries.values():
            if entry.confidence < min_confidence:
                continue
            value = entry.value.strip()
            if not value:
                continue
            lines.append(f"- {_humanize_field(entry.field)}: {value}")
        if not lines:
            return ""
        return "What you know about Jacob (profile):\n" + "\n".join(lines)

    # ── writes ──────────────────────────────────────────────────────────

    def upsert(
        self,
        user_id: str,
        field: str,
        value: str,
        confidence: float,
        *,
        now_iso: str | None = None,
    ) -> bool:
        """Insert or merge a (user_id, field) row. Returns True on write."""
        if not user_id or not field:
            return False
        value = (value or "").strip()
        if not value or len(value) > 240:
            value = value[:240]
        if not value:
            return False
        confidence = max(0.0, min(1.0, float(confidence)))
        now = now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")
        existing = self.fields(user_id).get(field)
        if existing is not None:
            # Merge: keep the higher-confidence value; if values differ but
            # the new one is much higher, replace; if similar conf, keep
            # the new value (it's fresher) and average the confidences.
            if existing.value.strip().lower() == value.strip().lower():
                merged_conf = max(existing.confidence, confidence)
                self._db.execute_commit(
                    "UPDATE user_profile SET confidence = ?, updated_at = ? "
                    "WHERE user_id = ? AND field = ?",
                    (merged_conf, now, user_id, field),
                )
                return False
            if confidence + 0.05 < existing.confidence:
                return False
            merged_conf = (existing.confidence + confidence) / 2.0 + 0.05
            merged_conf = min(1.0, merged_conf)
            self._db.execute_commit(
                "UPDATE user_profile SET value = ?, confidence = ?, updated_at = ? "
                "WHERE user_id = ? AND field = ?",
                (value, merged_conf, now, user_id, field),
            )
            return True
        self._db.execute_commit(
            "INSERT INTO user_profile (user_id, field, value, confidence, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, field, value, confidence, now),
        )
        return True


def _humanize_field(field: str) -> str:
    return (field or "").replace("_", " ")


_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _parse_profile_payload(raw: str) -> dict[str, tuple[str, float]]:
    """Best-effort parse of the worker's LLM response."""
    text = (raw or "").strip()
    if not text:
        return {}
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        m = _JSON_BLOCK_RE.search(text)
        candidate = m.group(0) if m else None
    if not candidate:
        return {}
    try:
        data = json.loads(candidate)
    except Exception:
        log.debug("profile JSON parse failed", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    fields_raw = data.get("fields") or {}
    if not isinstance(fields_raw, dict):
        return {}
    out: dict[str, tuple[str, float]] = {}
    for field_name, payload in fields_raw.items():
        if field_name not in PROFILE_FIELDS:
            continue
        if isinstance(payload, dict):
            value = str(payload.get("value") or "").strip()
            try:
                conf = float(payload.get("confidence", 0.5))
            except Exception:
                conf = 0.5
        elif isinstance(payload, str):
            value = payload.strip()
            conf = 0.5
        else:
            continue
        if not value or len(value) < 2:
            continue
        out[field_name] = (value[:240], max(0.0, min(1.0, conf)))
    return out


class UserProfileWorker:
    """Runs the profile-update LLM call inside the speaking window.

    Throttled by:
      * minimum number of user turns since the last run (default 6).
      * only fires when at least one user turn has occurred since.
    """

    def __init__(
        self,
        *,
        ollama: "OllamaClient",
        db: "ChatDatabase",
        store: UserProfileStore,
        model: str,
        min_user_turns: int = 6,
        max_history_chars: int = 3500,
        max_tokens: int = 320,
    ) -> None:
        self._ollama = ollama
        self._db = db
        self._store = store
        self._model = model
        self._min_user_turns = max(1, int(min_user_turns))
        self._max_history_chars = max(500, int(max_history_chars))
        self._max_tokens = max(120, int(max_tokens))
        self._user_turns_seen = 0
        self._user_turns_at_last_run = 0
        self._stats = {
            "scheduled": 0,
            "skipped_throttled": 0,
            "skipped_no_input": 0,
            "completed": 0,
            "failed": 0,
            "fields_written": 0,
        }

    # ── public ──────────────────────────────────────────────────────────

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(self, *, model: str | None = None) -> None:
        if model is not None:
            self._model = model

    def notify_user_turn(self) -> None:
        """Hot-path tick: SessionController calls this after each user turn."""
        self._user_turns_seen += 1

    def should_run(self) -> bool:
        return (
            self._user_turns_seen - self._user_turns_at_last_run
            >= self._min_user_turns
        )

    def maybe_run(
        self,
        user_id: str,
        *,
        session_key: str,
        history_provider: Callable[[], Iterable[tuple[str, str]]],
    ) -> dict[str, ProfileEntry] | None:
        """If due, run an extraction pass and persist results.

        ``history_provider`` returns an iterable of ``(role, content)``
        tuples — the worker doesn't reach into ChatDatabase directly so
        callers can scope which messages are visible (e.g., only the
        current session, or last N across sessions).
        """
        if not self.should_run():
            self._stats["skipped_throttled"] += 1
            return None
        # Reserve our turn so a flaky LLM doesn't make us spin.
        self._user_turns_at_last_run = self._user_turns_seen
        self._stats["scheduled"] += 1
        try:
            history = list(history_provider() or [])
        except Exception:
            log.debug("history_provider raised", exc_info=True)
            history = []
        block = _format_history_block(history, max_chars=self._max_history_chars)
        if not block:
            self._stats["skipped_no_input"] += 1
            return None
        try:
            messages = [
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": block},
            ]
            raw = self._ollama.chat(
                messages,
                options={
                    "temperature": 0.2,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
            )
        except Exception:
            log.debug("profile worker LLM call failed", exc_info=True)
            self._stats["failed"] += 1
            return None
        parsed = _parse_profile_payload(raw)
        if not parsed:
            self._stats["completed"] += 1
            return self._store.fields(user_id)
        for field_name, (value, conf) in parsed.items():
            try:
                wrote = self._store.upsert(user_id, field_name, value, conf)
            except Exception:
                log.debug("profile upsert failed", exc_info=True)
                continue
            if wrote:
                self._stats["fields_written"] += 1
        self._stats["completed"] += 1
        return self._store.fields(user_id)


def _format_history_block(
    history: list[tuple[str, str]],
    *,
    max_chars: int,
) -> str:
    """Render recent (role, content) pairs into a tidy block, newest last."""
    if not history:
        return ""
    lines: list[str] = []
    total = 0
    for role, content in reversed(history):
        text = (content or "").strip()
        if not text:
            continue
        speaker = "Jacob" if role == "user" else "You"
        line = f"{speaker}: {text}"
        if total + len(line) > max_chars and lines:
            break
        lines.append(line)
        total += len(line) + 1
    lines.reverse()
    return "\n".join(lines)


__all__ = [
    "UserProfileStore",
    "UserProfileWorker",
    "ProfileEntry",
    "PROFILE_FIELDS",
    "_parse_profile_payload",
]
