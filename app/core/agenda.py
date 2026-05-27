"""Agenda — small persistent goal list for Aiko (Phase 4a).

The user accumulates loose intentions ("I want to learn rust", "we
should plan that trip", "I'll fix the deploy script"). Some come from
the user, some Aiko proposes. Promises (Phase 3c) are short-lived
commitments; the agenda is the *medium-term* roster of things-in-flight.

Schema (already in chat_database):

    CREATE TABLE agenda (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        goal TEXT NOT NULL,
        source_session TEXT,
        created_at TEXT NOT NULL,
        due_at TEXT,
        status TEXT NOT NULL DEFAULT 'open',  -- open | done | dropped | snoozed
        importance REAL NOT NULL DEFAULT 0.5,
        last_groomed_at TEXT
    );

Two extraction paths:

  Track 1 — inline ``[[agenda:goal text]]`` tags in assistant output.
    TurnRunner doesn't need to know about this; the SessionController
    parses the same ``raw_text`` that fed the [[remember:...]] tags.

  Track 2 — LLM grooming pass on the speaking-window scheduler. Reviews
    the recent conversation against the open agenda and emits a JSON
    diff (new / completed / promoted / dropped). Throttled by user-turn
    cadence.

The hot-path read is :meth:`AgendaStore.render_block` which returns up
to a few open items sorted by importance.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.session_text_utils import resolve_user_name, speaker_label

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.agenda")


_AGENDA_TAG_RE = re.compile(
    r"\[\[agenda(?::(?P<importance>[0-9.]+))?:(?P<body>[^\]]+?)\]\]",
    flags=re.IGNORECASE,
)


@dataclass(slots=True)
class AgendaItem:
    id: int
    user_id: str
    goal: str
    source_session: str | None
    created_at: str
    due_at: str | None
    status: str
    importance: float
    last_groomed_at: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "goal": self.goal,
            "status": self.status,
            "importance": round(float(self.importance), 3),
            "created_at": self.created_at,
            "due_at": self.due_at,
            "last_groomed_at": self.last_groomed_at,
        }


_VALID_STATUSES = {"open", "done", "dropped", "snoozed"}


class AgendaStore:
    """SQLite CRUD on ``agenda`` (per-user, status-indexed)."""

    def __init__(self, db: "ChatDatabase") -> None:
        self._db = db

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    # ── reads ──────────────────────────────────────────────────────────

    def list_open(self, user_id: str, *, limit: int = 5) -> list[AgendaItem]:
        if not user_id:
            return []
        rows = self._db.execute_fetchall(
            "SELECT id, user_id, goal, source_session, created_at, due_at, "
            "status, importance, last_groomed_at FROM agenda "
            "WHERE user_id = ? AND status = 'open' "
            "ORDER BY importance DESC, created_at DESC LIMIT ?",
            (user_id, max(1, int(limit))),
        )
        return [_row_to_item(r) for r in rows]

    def list_all(self, user_id: str, *, limit: int = 50) -> list[AgendaItem]:
        if not user_id:
            return []
        rows = self._db.execute_fetchall(
            "SELECT id, user_id, goal, source_session, created_at, due_at, "
            "status, importance, last_groomed_at FROM agenda "
            "WHERE user_id = ? ORDER BY status='open' DESC, importance DESC, "
            "created_at DESC LIMIT ?",
            (user_id, max(1, int(limit))),
        )
        return [_row_to_item(r) for r in rows]

    def render_block(
        self,
        user_id: str,
        *,
        max_items: int = 4,
        user_display_name: str = "the user",
    ) -> str:
        items = self.list_open(user_id, limit=max_items)
        if not items:
            return ""
        lines = [f"- {item.goal}" for item in items]
        return (
            f"Active goals you're tracking with {user_display_name}:\n"
            + "\n".join(lines)
        )

    # ── writes ─────────────────────────────────────────────────────────

    def add(
        self,
        user_id: str,
        *,
        goal: str,
        source_session: str | None = None,
        importance: float = 0.5,
        due_at: str | None = None,
    ) -> AgendaItem | None:
        goal_clean = (goal or "").strip()
        if not user_id or not goal_clean or len(goal_clean) < 3:
            return None
        # Dedupe against an existing open item with the same lowercased goal.
        existing = self._find_by_goal(user_id, goal_clean)
        if existing is not None:
            # Bump importance toward the new value (favor higher).
            new_importance = max(existing.importance, float(importance))
            if abs(new_importance - existing.importance) > 1e-6:
                self.update(existing.id, importance=new_importance)
            return existing
        importance_clipped = max(0.0, min(1.0, float(importance)))
        now = self._now()
        new_id = self._db.execute_commit(
            "INSERT INTO agenda (user_id, goal, source_session, created_at, "
            "due_at, status, importance) VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (user_id, goal_clean[:240], source_session, now, due_at, importance_clipped),
        )
        return AgendaItem(
            id=new_id,
            user_id=user_id,
            goal=goal_clean[:240],
            source_session=source_session,
            created_at=now,
            due_at=due_at,
            status="open",
            importance=importance_clipped,
            last_groomed_at=None,
        )

    def update(
        self,
        agenda_id: int,
        *,
        status: str | None = None,
        importance: float | None = None,
        goal: str | None = None,
        due_at: str | None = None,
        last_groomed_at: str | None = None,
    ) -> bool:
        if agenda_id <= 0:
            return False
        sets: list[str] = []
        params: list[object] = []
        if status is not None:
            if status not in _VALID_STATUSES:
                return False
            sets.append("status = ?")
            params.append(status)
        if importance is not None:
            sets.append("importance = ?")
            params.append(max(0.0, min(1.0, float(importance))))
        if goal is not None:
            goal_clean = (goal or "").strip()
            if goal_clean:
                sets.append("goal = ?")
                params.append(goal_clean[:240])
        if due_at is not None:
            sets.append("due_at = ?")
            params.append(due_at)
        if last_groomed_at is not None:
            sets.append("last_groomed_at = ?")
            params.append(last_groomed_at)
        if not sets:
            return False
        params.append(int(agenda_id))
        self._db.execute_commit(
            f"UPDATE agenda SET {', '.join(sets)} WHERE id = ?",
            tuple(params),
        )
        return True

    def mark_done(self, agenda_id: int) -> bool:
        return self.update(agenda_id, status="done", last_groomed_at=self._now())

    def mark_dropped(self, agenda_id: int) -> bool:
        return self.update(agenda_id, status="dropped", last_groomed_at=self._now())

    def get(self, agenda_id: int) -> AgendaItem | None:
        row = self._db.execute_fetchone(
            "SELECT id, user_id, goal, source_session, created_at, due_at, "
            "status, importance, last_groomed_at FROM agenda WHERE id = ?",
            (int(agenda_id),),
        )
        return _row_to_item(row) if row else None

    def _find_by_goal(self, user_id: str, goal: str) -> AgendaItem | None:
        norm = goal.strip().lower()
        rows = self._db.execute_fetchall(
            "SELECT id, user_id, goal, source_session, created_at, due_at, "
            "status, importance, last_groomed_at FROM agenda "
            "WHERE user_id = ? AND status = 'open'",
            (user_id,),
        )
        for r in rows:
            if str(r[2] or "").strip().lower() == norm:
                return _row_to_item(r)
        return None


def _row_to_item(row: tuple[Any, ...] | None) -> AgendaItem:
    if row is None:
        # Caller is expected to handle None; keep this tolerant for tests.
        return AgendaItem(  # pragma: no cover
            id=0, user_id="", goal="", source_session=None,
            created_at="", due_at=None, status="open",
            importance=0.0, last_groomed_at=None,
        )
    return AgendaItem(
        id=int(row[0]),
        user_id=str(row[1] or ""),
        goal=str(row[2] or ""),
        source_session=str(row[3]) if row[3] else None,
        created_at=str(row[4] or ""),
        due_at=str(row[5]) if row[5] else None,
        status=str(row[6] or "open"),
        importance=float(row[7] or 0.5),
        last_groomed_at=str(row[8]) if row[8] else None,
    )


# ── inline tag extraction ────────────────────────────────────────────────


def extract_inline_tags(raw_text: str) -> list[tuple[str, float]]:
    """Pull ``[[agenda:goal]]`` and ``[[agenda:0.7:goal]]`` out of text."""
    out: list[tuple[str, float]] = []
    seen: set[str] = set()
    for m in _AGENDA_TAG_RE.finditer(raw_text or ""):
        body = (m.group("body") or "").strip()
        if not body:
            continue
        key = body.lower()
        if key in seen:
            continue
        seen.add(key)
        importance = 0.5
        try:
            raw_imp = m.group("importance")
            if raw_imp:
                importance = max(0.0, min(1.0, float(raw_imp)))
        except Exception:
            importance = 0.5
        out.append((body[:240], importance))
    return out


# ── LLM grooming pass ────────────────────────────────────────────────────


_GROOM_PROMPT = """\
You are Aiko's tidy-up routine. You will receive (1) Aiko's current open
agenda and (2) the most recent slice of conversation. Diff them and emit:

{
  "complete": [<id>, ...],            // agenda ids that look done now
  "drop":     [<id>, ...],            // agenda ids no longer relevant
  "promote":  [{"id": <id>, "importance": <0..1>}, ...],
  "add":      [{"goal": "<short text>", "importance": <0..1>}, ...]
}

Rules:
- "complete" / "drop" only include items you have positive evidence for.
- "add" should NOT duplicate the existing agenda.
- Output ONLY the JSON object. No prose."""


@dataclass(slots=True)
class GroomDiff:
    complete: list[int]
    drop: list[int]
    promote: list[tuple[int, float]]
    add: list[tuple[str, float]]


_JSON_BLOCK_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _parse_groom_diff(raw: str) -> GroomDiff:
    text = (raw or "").strip()
    out = GroomDiff(complete=[], drop=[], promote=[], add=[])
    if not text:
        return out
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidate = fenced.group(1) if fenced else None
    if candidate is None:
        m = _JSON_BLOCK_RE.search(text)
        candidate = m.group(0) if m else None
    if not candidate:
        return out
    try:
        data = json.loads(candidate)
    except Exception:
        log.debug("agenda groom JSON parse failed", exc_info=True)
        return out
    if not isinstance(data, dict):
        return out
    for cid in data.get("complete") or []:
        try:
            out.complete.append(int(cid))
        except Exception:
            continue
    for did in data.get("drop") or []:
        try:
            out.drop.append(int(did))
        except Exception:
            continue
    for entry in data.get("promote") or []:
        if not isinstance(entry, dict):
            continue
        try:
            iid = int(entry.get("id"))
            imp = float(entry.get("importance", 0.5))
        except Exception:
            continue
        out.promote.append((iid, max(0.0, min(1.0, imp))))
    for entry in data.get("add") or []:
        if not isinstance(entry, dict):
            continue
        goal = str(entry.get("goal") or "").strip()
        if len(goal) < 4:
            continue
        try:
            imp = float(entry.get("importance", 0.5))
        except Exception:
            imp = 0.5
        out.add.append((goal[:240], max(0.0, min(1.0, imp))))
    return out


class AgendaWorker:
    """Speaking-window LLM grooming over the open agenda."""

    def __init__(
        self,
        *,
        ollama: "OllamaClient",
        store: AgendaStore,
        model: str,
        every_n_turns: int = 8,
        max_history_chars: int = 2500,
        max_tokens: int = 220,
        user_display_name_provider: "Callable[[], str] | None" = None,
    ) -> None:
        self._ollama = ollama
        self._store = store
        self._model = model
        self._every_n = max(1, int(every_n_turns))
        self._max_history_chars = max(500, int(max_history_chars))
        self._max_tokens = max(80, int(max_tokens))
        self._user_display_name_provider = user_display_name_provider
        self._user_turns_seen = 0
        self._user_turns_at_last_groom = 0
        self._stats = {
            "scheduled": 0,
            "skipped_throttled": 0,
            "skipped_no_history": 0,
            "completed": 0,
            "failed": 0,
            "completes": 0,
            "drops": 0,
            "promotes": 0,
            "adds": 0,
        }

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(self, *, model: str | None = None) -> None:
        if model is not None:
            self._model = model

    def notify_user_turn(self) -> None:
        self._user_turns_seen += 1

    def should_run(self, user_id: str) -> bool:
        if (
            self._user_turns_seen - self._user_turns_at_last_groom
            < self._every_n
        ):
            return False
        # Only groom when there's something to groom.
        return bool(self._store.list_open(user_id, limit=1))

    def maybe_run(
        self,
        user_id: str,
        *,
        history_provider: Callable[[], Iterable[tuple[str, str]]],
    ) -> GroomDiff | None:
        if not self.should_run(user_id):
            self._stats["skipped_throttled"] += 1
            return None
        self._user_turns_at_last_groom = self._user_turns_seen
        self._stats["scheduled"] += 1
        try:
            history = list(history_provider() or [])
        except Exception:
            log.debug("history provider failed", exc_info=True)
            history = []
        if not history:
            self._stats["skipped_no_history"] += 1
            return None
        items = self._store.list_open(user_id, limit=20)
        block = _format_groom_block(
            items,
            history,
            max_chars=self._max_history_chars,
            user_display_name=resolve_user_name(
                self._user_display_name_provider,
            ),
        )
        if not block:
            self._stats["skipped_no_history"] += 1
            return None
        try:
            messages = [
                {"role": "system", "content": _GROOM_PROMPT},
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
            log.debug("agenda groom LLM call failed", exc_info=True)
            self._stats["failed"] += 1
            return None
        diff = _parse_groom_diff(raw)
        self._apply_diff(user_id, diff)
        self._stats["completed"] += 1
        return diff

    # ── apply ───────────────────────────────────────────────────────────

    def _apply_diff(self, user_id: str, diff: GroomDiff) -> None:
        for iid in diff.complete:
            if self._store.mark_done(iid):
                self._stats["completes"] += 1
        for iid in diff.drop:
            if self._store.mark_dropped(iid):
                self._stats["drops"] += 1
        for iid, imp in diff.promote:
            if self._store.update(iid, importance=imp):
                self._stats["promotes"] += 1
        for goal, imp in diff.add:
            added = self._store.add(user_id, goal=goal, importance=imp)
            if added is not None:
                self._stats["adds"] += 1


def _format_groom_block(
    items: list[AgendaItem],
    history: list[tuple[str, str]],
    *,
    max_chars: int,
    user_display_name: str = "Jacob",
) -> str:
    if not history:
        return ""
    if items:
        agenda_lines = [
            f"- id={i.id} importance={i.importance:.2f}: {i.goal}"
            for i in items
        ]
        agenda_block = "Current open agenda:\n" + "\n".join(agenda_lines)
    else:
        agenda_block = "Current open agenda:\n(empty)"
    msg_lines: list[str] = []
    total = 0
    for role, content in reversed(history):
        text = (content or "").strip()
        if not text:
            continue
        speaker = speaker_label(role, user_display_name)
        line = f"{speaker}: {text}"
        if total + len(line) > max_chars and msg_lines:
            break
        msg_lines.append(line)
        total += len(line) + 1
    msg_lines.reverse()
    convo_block = "Recent conversation:\n" + "\n".join(msg_lines)
    return f"{agenda_block}\n\n{convo_block}"


__all__ = [
    "AgendaItem",
    "AgendaStore",
    "AgendaWorker",
    "GroomDiff",
    "extract_inline_tags",
    "_parse_groom_diff",
]
