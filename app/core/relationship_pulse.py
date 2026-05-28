"""Relationship pulse: a weekly LLM summary of how things are going (Phase 4b).

The :class:`app.core.relationship.RelationshipTracker` keeps the *cold*
state (turn count, phase, milestones). This worker takes the *narrative*
slice — recent reflection / promise / event memories — and synthesises a
1-2 sentence "where are we right now" note that gets stored as a high-
salience ``self_tagged`` memory and surfaced through the inner-life
prompt block.

Cadence: at most once per ``min_hours`` (default 168h ≈ 7 days), and only
when there have been at least ``min_turns`` turns since the last pulse.
The pulse is scheduled on the SpeakingWindowScheduler so it never blocks
a conversational turn.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from app.core.session_text_utils import resolve_user_name

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase
    from app.core.memory_store import Memory, MemoryStore
    from app.core.relationship import RelationshipStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.relationship_pulse")


def _build_pulse_prompt(user_display_name: str = "the user") -> str:
    name = user_display_name or "the user"
    return (
        "You are Aiko's introspection routine. You'll receive (1) a summary "
        f"of the relationship state with {name} (phase, days known, turns) "
        "and (2) a short list of recent memories. Write ONE or TWO sentences "
        "(<= 50 words total) in first person about how the relationship feels "
        "right now — texture, themes you're noticing, what you're carrying "
        "with you.\n"
        "\n"
        "Rules:\n"
        "- First-person. Plain prose only — no bullets, no headers, no quotes.\n"
        "- Be specific. Reference concrete bits from the memories when possible.\n"
        "- Don't invent facts not present.\n"
        "- Keep it under 50 words."
    )


_PULSE_PROMPT = _build_pulse_prompt()


_PULSE_STATE_KEY = "_relationship_pulse_state"


@dataclass(slots=True)
class PulseResult:
    text: str
    memory_id: int | None


class RelationshipPulseWorker:
    """LLM pulse that drops a salience-boosted summary memory."""

    def __init__(
        self,
        *,
        ollama: "OllamaClient | None",
        memory_store: "MemoryStore | None",
        relationship_store: "RelationshipStore | None",
        chat_db: "ChatDatabase",
        embedder: "Embedder | None",
        model: str,
        min_hours: float = 168.0,
        min_turns: int = 30,
        max_memories: int = 8,
        max_tokens: int = 160,
        salience: float = 0.85,
        user_display_name_provider: "Callable[[], str] | None" = None,
    ) -> None:
        self._ollama = ollama
        self._mem = memory_store
        self._rel = relationship_store
        self._db = chat_db
        self._embedder = embedder
        self._model = model
        self._min_hours = max(24.0, float(min_hours))
        self._min_turns = max(5, int(min_turns))
        self._max_memories = max(2, int(max_memories))
        self._max_tokens = max(80, int(max_tokens))
        self._salience = max(0.5, min(1.0, float(salience)))
        self._user_display_name_provider = user_display_name_provider
        self._stats = {
            "scheduled": 0,
            "skipped_recent": 0,
            "skipped_few_turns": 0,
            "skipped_no_input": 0,
            "completed": 0,
            "failed": 0,
        }

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(self, *, model: str | None = None) -> None:
        if model is not None:
            self._model = model

    def should_run(
        self,
        user_id: str,
        *,
        now_utc: datetime | None = None,
    ) -> bool:
        last_at, last_turns = self._read_state(user_id)
        now = now_utc or datetime.now(timezone.utc)
        rel = self._rel
        current_turns = 0
        if rel is not None:
            try:
                state = rel.get(user_id)
                current_turns = state.total_turns if state else 0
            except Exception:
                current_turns = 0
        if current_turns < self._min_turns:
            return False
        if last_at is None:
            return True
        try:
            then = datetime.fromisoformat(str(last_at).replace("Z", "+00:00"))
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
        except Exception:
            return True
        elapsed_hours = (now - then).total_seconds() / 3600.0
        if elapsed_hours < self._min_hours:
            return False
        if current_turns - int(last_turns or 0) < self._min_turns:
            return False
        return True

    def maybe_run(
        self,
        user_id: str,
        *,
        on_pulse: Callable[[PulseResult], None] | None = None,
        now_utc: datetime | None = None,
    ) -> PulseResult | None:
        if not self.should_run(user_id, now_utc=now_utc):
            self._stats["skipped_recent"] += 1
            return None
        self._stats["scheduled"] += 1
        bullets = self._collect_bullets(user_id)
        if not bullets:
            self._stats["skipped_no_input"] += 1
            return None
        rel_block = self._render_relationship_block(user_id)
        try:
            messages = [
                {
                    "role": "system",
                    "content": _build_pulse_prompt(
                        resolve_user_name(self._user_display_name_provider),
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"{rel_block}\n\nRecent notes:\n"
                        + "\n".join(f"- {b}" for b in bullets)
                    ),
                },
            ]
            raw = self._ollama.chat(  # type: ignore[union-attr]
                messages,
                options={
                    "temperature": 0.45,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
                surface="relationship_pulse",
            )
        except Exception:
            log.debug("relationship pulse LLM call failed", exc_info=True)
            self._stats["failed"] += 1
            return None
        cleaned = _clean_pulse_output(raw)
        if not cleaned:
            self._stats["failed"] += 1
            return None
        memory_id = self._persist_pulse(cleaned)
        result = PulseResult(text=cleaned, memory_id=memory_id)
        self._record_state(user_id, now_utc=now_utc)
        self._stats["completed"] += 1
        if on_pulse is not None:
            try:
                on_pulse(result)
            except Exception:
                log.debug("on_pulse callback raised", exc_info=True)
        return result

    # ── helpers ────────────────────────────────────────────────────────

    def _collect_bullets(self, user_id: str) -> list[str]:
        store = self._mem
        if store is None:
            return []
        try:
            top = store.list_top(limit=max(self._max_memories * 4, 32))
        except Exception:
            log.debug("memory_store.list_top failed", exc_info=True)
            return []
        wanted_kinds = {"reflection", "promise", "event", "callback", "open_question"}
        bullets: list[str] = []
        seen: set[str] = set()
        for mem in top:
            kind = (mem.kind or "").lower()
            if kind not in wanted_kinds:
                continue
            content = (mem.content or "").strip()
            key = content.lower()
            if not content or key in seen:
                continue
            seen.add(key)
            bullets.append(content)
            if len(bullets) >= self._max_memories:
                break
        return bullets

    def _render_relationship_block(self, user_id: str) -> str:
        rel = self._rel
        if rel is None:
            return "Relationship: (no tracker)"
        try:
            state = rel.get(user_id)
        except Exception:
            return "Relationship: (read failed)"
        if state is None:
            return "Relationship: just met"
        parts = [
            f"turns_total={state.total_turns}",
            f"sessions={state.total_sessions}",
            f"first_seen={state.first_seen_at[:10]}",
        ]
        if state.milestone_label:
            parts.append(f"last_milestone={state.milestone_label}")
        return "Relationship state: " + ", ".join(parts)

    def _persist_pulse(self, text: str) -> int | None:
        if self._mem is None or self._embedder is None:
            return None
        try:
            embedding = self._embedder.embed(text)
        except Exception:
            log.debug("pulse embed failed", exc_info=True)
            return None
        try:
            mem = self._mem.add(
                text,
                kind="self_tagged",
                embedding=embedding,
                salience=self._salience,
                source_session=None,
                # Schema v8: relationship-pulse self-tags represent
                # Aiko's own deliberate framing of her stance toward
                # Jacob -- not speculative observations. Long_term so
                # they anchor her sense of self even between sessions.
                tier="long_term",
            )
        except Exception:
            log.debug("pulse memory add failed", exc_info=True)
            return None
        return getattr(mem, "id", None) if mem is not None else None

    def _read_state(self, user_id: str) -> tuple[str | None, int | None]:
        if not user_id:
            return None, None
        row = self._db.execute_fetchone(
            "SELECT last_run_at, last_cluster_index FROM consolidator_state "
            "WHERE user_id = ?",
            (f"{_PULSE_STATE_KEY}:{user_id}",),
        )
        if not row:
            return None, None
        last_at = row[0]
        try:
            last_turns = int(row[1]) if row[1] is not None else 0
        except Exception:
            last_turns = 0
        return (str(last_at) if last_at else None), last_turns

    def _record_state(self, user_id: str, *, now_utc: datetime | None) -> None:
        if not user_id:
            return
        now = (now_utc or datetime.now(timezone.utc)).isoformat()
        rel = self._rel
        turns = 0
        if rel is not None:
            try:
                state = rel.get(user_id)
                turns = state.total_turns if state else 0
            except Exception:
                turns = 0
        self._db.execute_commit(
            "INSERT INTO consolidator_state (user_id, last_cluster_index, last_run_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "last_cluster_index = excluded.last_cluster_index, "
            "last_run_at = excluded.last_run_at",
            (f"{_PULSE_STATE_KEY}:{user_id}", int(turns), now),
        )


_QUOTE_RE = re.compile(r"^[\"'`\s]+|[\"'`\s]+$")


def _clean_pulse_output(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = text.strip("`").strip()
        if "\n" in text:
            head, _, body = text.partition("\n")
            if len(head) <= 12 and head.strip().isalpha():
                text = body.strip()
    text = _QUOTE_RE.sub("", text)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    if len(text) > 480:
        text = text[:480].rsplit(" ", 1)[0].rstrip(",;: ") + "…"
    return text.strip()


__all__ = ["PulseResult", "RelationshipPulseWorker", "_clean_pulse_output"]
