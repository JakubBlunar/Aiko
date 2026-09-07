"""Speaking-window worker that tracks the conversation's present situation."""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable

from app.core.conversation.conversation_situation import (
    ConversationSituationState,
    ConversationSituationStore,
    parse_extraction,
    reduce_situation,
)
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase, MessageRow
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.conversation_situation")

_SYSTEM_PROMPT = """\
You are Aiko's present-situation observer. Read a short, age-tagged transcript,
the authoritative structured-world snapshot, and the previous observation.
Track what is genuinely happening in the conversation now.

The situation vocabulary is open-ended. Do not force it into a catalogue.
Capture a shared activity/place only when the recent transcript establishes
that Aiko and the user are participating together. A remembered ritual or a
possible plan is not current evidence. The world snapshot wins: never claim
that Aiko is already somewhere or doing something contradicted by it.

Return ONE JSON object:
{
  "operation": "keep|replace|clear",
  "summary": "short durable description with no relative time words",
  "shared": true|false,
  "shared_activity": "open-ended activity or empty string",
  "place_ref": "place named in the conversation or empty string",
  "aiko_activity": "what Aiko is doing in this situation or empty string",
  "evidence_message_ids": [integer ids from the supplied transcript],
  "explicit_end": true|false
}

Use replace when recent evidence establishes or materially changes the present
situation. Use keep when the previous observation still fits and nothing
material changed. Use clear when it no longer describes the present; set
explicit_end=true only when the transcript or authoritative world state
directly ends/contradicts it. Never include a confidence field.
"""


class ConversationSituationWorker:
    """Periodic worker-model interpretation with deterministic persistence."""

    def __init__(
        self,
        *,
        client: "ChatClient | None",
        chat_db: "ChatDatabase",
        store: ConversationSituationStore,
        model: str | None,
        world_snapshot_provider: Callable[[], dict[str, Any]],
        every_n_user_turns: int = 2,
        max_history_messages: int = 14,
        max_tokens: int = 240,
    ) -> None:
        self._client = client
        self._chat_db = chat_db
        self._store = store
        self._model = model
        self._world_snapshot_provider = world_snapshot_provider
        self._every_n = max(1, int(every_n_user_turns))
        self._max_history = max(4, int(max_history_messages))
        self._max_tokens = max(100, int(max_tokens))
        self._turns_seen: dict[str, int] = defaultdict(int)
        self._turns_at_last_run: dict[str, int] = defaultdict(int)
        self._last_user_message_id: dict[str, int] = {}
        self._last_result: dict[str, dict[str, Any]] = {}
        self._stats = {
            "scheduled": 0,
            "completed": 0,
            "failed": 0,
            "invalid": 0,
            "kept": 0,
            "replaced": 0,
            "cleared": 0,
        }

    def update_runtime(self, *, model: str | None = None) -> None:
        if model is not None:
            self._model = model

    def notify_user_turn(
        self, session_id: str, *, message_id: int | None = None
    ) -> None:
        key = str(session_id)
        if message_id is not None:
            current_id = int(message_id)
            if self._last_user_message_id.get(key) == current_id:
                return
            self._last_user_message_id[key] = current_id
        self._turns_seen[key] += 1

    def should_run(self, session_id: str) -> bool:
        key = str(session_id)
        if self._client is None or not self._model:
            return False
        return (
            self._turns_seen[key] - self._turns_at_last_run[key]
        ) >= self._every_n

    def mark_scheduled(self, session_id: str) -> None:
        key = str(session_id)
        self._turns_at_last_run[key] = self._turns_seen[key]
        self._stats["scheduled"] += 1

    def stats(self, session_id: str | None = None) -> dict[str, Any]:
        report: dict[str, Any] = {
            **self._stats,
            "every_n_user_turns": self._every_n,
        }
        if session_id is not None:
            key = str(session_id)
            report.update(
                {
                    "turns_seen": self._turns_seen[key],
                    "turns_since_run": (
                        self._turns_seen[key] - self._turns_at_last_run[key]
                    ),
                    "last_result": dict(self._last_result.get(key, {})),
                }
            )
        return report

    def run(self, session_id: str) -> ConversationSituationState | None:
        key = str(session_id)
        try:
            rows = self._chat_db.get_messages(key, limit=self._max_history)
            if not rows:
                return self._store.get(key)
            previous = self._store.get(key)
            prompt = self._build_user_prompt(rows, previous)
            raw, _usage = self._client.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            f"{timephrase.today_anchor()}\n\n"
                            f"{_SYSTEM_PROMPT}\n\n"
                            f"{timephrase.STORED_TEXT_TIME_RULE}"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0, "num_predict": self._max_tokens},
                think=False,
                surface="conversation_situation",
            )
            valid_ids = {int(row.id) for row in rows}
            extraction = parse_extraction(raw, valid_message_ids=valid_ids)
            if extraction is None:
                self._stats["invalid"] += 1
                self._last_result[key] = {
                    "result": "invalid_output",
                    "preserved_previous": previous is not None,
                }
                return previous
            source_message_id = max(valid_ids)
            state = reduce_situation(
                previous,
                extraction,
                session_id=key,
                source_message_id=source_message_id,
            )
            if state is not None:
                self._store.upsert(state)
            self._stats["completed"] += 1
            self._stats[
                {
                    "keep": "kept",
                    "replace": "replaced",
                    "clear": "cleared",
                }[extraction.operation]
            ] += 1
            self._last_result[key] = {
                "result": extraction.operation,
                "status": state.status if state is not None else "empty",
                "generation": state.generation if state is not None else 0,
                "source_message_id": source_message_id,
            }
            return state
        except Exception:
            self._stats["failed"] += 1
            try:
                preserved = self._store.get(key)
            except Exception:
                preserved = None
            self._last_result[key] = {
                "result": "failed",
                "preserved_previous": preserved is not None,
            }
            log.debug("conversation situation worker failed", exc_info=True)
            return preserved

    def _build_user_prompt(
        self,
        rows: list["MessageRow"],
        previous: ConversationSituationState | None,
    ) -> str:
        now = timephrase.now()
        tagged_rows = [
            {
                "role": row.role,
                "content": f"[message_id={int(row.id)}] {row.content}",
                "created_at": row.created_at,
            }
            for row in rows
        ]
        transcript = timephrase.format_transcript(tagged_rows, now_dt=now)
        try:
            world = self._world_snapshot_provider() or {}
        except Exception:
            world = {}
        world = self._bounded_world_snapshot(world)
        prior = previous.to_payload() if previous is not None else None
        return (
            "AUTHORITATIVE WORLD SNAPSHOT:\n"
            + json.dumps(world, ensure_ascii=False, separators=(",", ":"))
            + "\n\nPREVIOUS SITUATION:\n"
            + json.dumps(prior, ensure_ascii=False, separators=(",", ":"))
            + "\n\nRECENT TRANSCRIPT:\n"
            + transcript
        )

    @staticmethod
    def _bounded_world_snapshot(world: dict[str, Any]) -> dict[str, Any]:
        state = world.get("state") if isinstance(world, dict) else {}
        state = state if isinstance(state, dict) else {}
        location_id = state.get("location_id")
        scene_id = state.get("scene_id")
        locations = world.get("locations")
        scenes = world.get("scenes")
        current_location = next(
            (
                value
                for value in (locations if isinstance(locations, list) else [])
                if isinstance(value, dict) and value.get("id") == location_id
            ),
            None,
        )
        current_scene = next(
            (
                value
                for value in (scenes if isinstance(scenes, list) else [])
                if isinstance(value, dict) and value.get("id") == scene_id
            ),
            None,
        )
        return {
            "state": state,
            "current_location": current_location,
            "current_scene": current_scene,
        }


__all__ = ["ConversationSituationWorker"]
