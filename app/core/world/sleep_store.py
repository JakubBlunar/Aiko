"""Atomic SQLite persistence for Aiko's identity-wide sleep lifecycle."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.world.sleep_state import (
    ASLEEP,
    NAP,
    WINDING_DOWN,
    SleepEpisode,
    SleepState,
    clean_reason_text,
    initial_state,
    transition,
)


log = logging.getLogger("app.sleep_store")

_STATE_COLUMNS = (
    "status, generation, entered_at, updated_at, current_episode_id, "
    "sleep_kind, reason_code, reason_text, last_woken_at, previous_world_json"
)
_EPISODE_COLUMNS = (
    "id, kind, reason_code, reason_text, started_at, ended_at, outcome, "
    "previous_world_json, interruptions_json, dream_memory_id, diary_memory_id, "
    "created_at, updated_at"
)


class SleepGenerationConflict(RuntimeError):
    """The lifecycle changed since a caller captured its snapshot."""


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _json_list(value: str | None) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [dict(item) for item in parsed if isinstance(item, dict)]


def _state_from_row(row: tuple[Any, ...]) -> SleepState:
    status = str(row[0])
    kind = str(row[5]) if row[5] is not None else None
    return SleepState(
        status=status,  # type: ignore[arg-type]
        generation=int(row[1]),
        entered_at=str(row[2]),
        updated_at=str(row[3]),
        current_episode_id=int(row[4]) if row[4] is not None else None,
        sleep_kind=kind,  # type: ignore[arg-type]
        reason_code=str(row[6] or ""),
        reason_text=str(row[7] or ""),
        last_woken_at=str(row[8]) if row[8] else None,
        previous_world=_json_object(str(row[9] or "{}")),
    )


def _episode_from_row(row: tuple[Any, ...]) -> SleepEpisode:
    return SleepEpisode(
        id=int(row[0]),
        kind=str(row[1]),  # type: ignore[arg-type]
        reason_code=str(row[2] or ""),
        reason_text=str(row[3] or ""),
        started_at=str(row[4]),
        ended_at=str(row[5]) if row[5] else None,
        outcome=str(row[6] or ""),
        previous_world=_json_object(str(row[7] or "{}")),
        interruptions=tuple(_json_list(str(row[8] or "[]"))),
        dream_memory_id=int(row[9]) if row[9] is not None else None,
        diary_memory_id=int(row[10]) if row[10] is not None else None,
        created_at=str(row[11] or ""),
        updated_at=str(row[12] or ""),
    )


class SleepStore:
    """One sleep state shared by every conversation and web client."""

    def __init__(self, db: ChatDatabase) -> None:
        self._db = db
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._listener_lock = threading.Lock()
        self.get_state()

    def add_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._listener_lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._listener_lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    def _notify(self, payload: dict[str, Any]) -> None:
        with self._listener_lock:
            listeners = list(self._listeners)
        for callback in listeners:
            try:
                callback(dict(payload))
            except Exception:
                log.debug("sleep listener failed", exc_info=True)

    def _conn(self) -> sqlite3.Connection:
        return self._db._get_conn()  # type: ignore[attr-defined]

    def get_state(self) -> SleepState:
        conn = self._conn()
        row = conn.execute(
            f"SELECT {_STATE_COLUMNS} FROM sleep_state WHERE id = 1"
        ).fetchone()
        if row is None:
            state = initial_state()
            conn.execute(
                "INSERT OR IGNORE INTO sleep_state "
                "(id, status, generation, entered_at, updated_at, "
                "previous_world_json) VALUES (1, 'awake', 0, ?, ?, '{}')",
                (state.entered_at, state.updated_at),
            )
            conn.commit()
            row = conn.execute(
                f"SELECT {_STATE_COLUMNS} FROM sleep_state WHERE id = 1"
            ).fetchone()
        if row is None:
            raise RuntimeError("sleep_state singleton could not be initialized")
        return _state_from_row(tuple(row))

    def get_episode(self, episode_id: int | None) -> SleepEpisode | None:
        if episode_id is None:
            return None
        row = self._conn().execute(
            f"SELECT {_EPISODE_COLUMNS} FROM sleep_episodes WHERE id = ?",
            (int(episode_id),),
        ).fetchone()
        return _episode_from_row(tuple(row)) if row is not None else None

    def current_episode(self) -> SleepEpisode | None:
        return self.get_episode(self.get_state().current_episode_id)

    def list_episodes(self, *, limit: int = 20) -> list[SleepEpisode]:
        rows = self._conn().execute(
            f"SELECT {_EPISODE_COLUMNS} FROM sleep_episodes "
            "ORDER BY started_at DESC LIMIT ?",
            (max(1, min(200, int(limit))),),
        ).fetchall()
        return [_episode_from_row(tuple(row)) for row in rows]

    def transition(
        self,
        action: str,
        *,
        expected_generation: int | None = None,
        now: datetime | None = None,
        sleep_kind: str | None = None,
        reason_code: str = "",
        reason_text: str = "",
        previous_world: dict[str, Any] | None = None,
        source_session: str | None = None,
        source_message_id: int | None = None,
        outcome: str = "",
    ) -> SleepState:
        """Apply a legal edge under ``BEGIN IMMEDIATE`` and notify listeners."""
        when = timephrase.to_aware(now or timephrase.utcnow())
        stamp = when.isoformat(timespec="seconds")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT {_STATE_COLUMNS} FROM sleep_state WHERE id = 1"
            ).fetchone()
            if row is None:
                conn.rollback()
                self.get_state()
                return self.transition(
                    action,
                    expected_generation=expected_generation,
                    now=when,
                    sleep_kind=sleep_kind,
                    reason_code=reason_code,
                    reason_text=reason_text,
                    previous_world=previous_world,
                    source_session=source_session,
                    source_message_id=source_message_id,
                    outcome=outcome,
                )
            current = _state_from_row(tuple(row))
            if (
                expected_generation is not None
                and current.generation != int(expected_generation)
            ):
                raise SleepGenerationConflict(
                    f"expected generation {expected_generation}, "
                    f"found {current.generation}"
                )

            episode_id = current.current_episode_id
            if action == "wind_down":
                kind = sleep_kind if sleep_kind in {"overnight", "nap"} else "overnight"
                world = json.dumps(previous_world or {}, ensure_ascii=False)
                cursor = conn.execute(
                    "INSERT INTO sleep_episodes "
                    "(kind, reason_code, reason_text, started_at, outcome, "
                    "previous_world_json, interruptions_json, source_session, "
                    "source_message_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, '', ?, '[]', ?, ?, ?, ?)",
                    (
                        kind,
                        str(reason_code or "tired")[:64],
                        clean_reason_text(reason_text),
                        stamp,
                        world,
                        source_session,
                        source_message_id,
                        stamp,
                        stamp,
                    ),
                )
                episode_id = int(cursor.lastrowid)

            updated = transition(
                current,
                action,
                now=when,
                sleep_kind=sleep_kind,
                reason_code=reason_code,
                reason_text=reason_text,
                episode_id=episode_id,
                previous_world=previous_world,
            )
            cursor = conn.execute(
                "UPDATE sleep_state SET status = ?, generation = ?, "
                "entered_at = ?, updated_at = ?, current_episode_id = ?, "
                "sleep_kind = ?, reason_code = ?, reason_text = ?, "
                "last_woken_at = ?, previous_world_json = ? "
                "WHERE id = 1 AND generation = ?",
                (
                    updated.status,
                    updated.generation,
                    updated.entered_at,
                    updated.updated_at,
                    updated.current_episode_id,
                    updated.sleep_kind,
                    updated.reason_code,
                    updated.reason_text,
                    updated.last_woken_at,
                    json.dumps(updated.previous_world or {}, ensure_ascii=False),
                    current.generation,
                ),
            )
            if cursor.rowcount != 1:
                raise SleepGenerationConflict("sleep state changed during transition")

            if action in {"cancel", "fully_awake"} and episode_id is not None:
                final_outcome = outcome or (
                    "cancelled" if action == "cancel" else "fully_awake"
                )
                conn.execute(
                    "UPDATE sleep_episodes SET ended_at = ?, outcome = ?, "
                    "updated_at = ? WHERE id = ? AND ended_at IS NULL",
                    (stamp, final_outcome[:64], stamp, int(episode_id)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        payload = self.snapshot(state=updated)
        self._notify(payload)
        return updated

    def record_interruption(
        self,
        *,
        session_id: str,
        message_id: int | None,
        message_text: str = "",
        now: datetime | None = None,
    ) -> tuple[SleepState, SleepEpisode | None]:
        """Append one bounded interruption while asleep without waking Aiko."""
        stamp = timephrase.to_aware(now or timephrase.utcnow()).isoformat(
            timespec="seconds"
        )
        conn = self._conn()
        episode_id: int | None = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            state_row = conn.execute(
                f"SELECT {_STATE_COLUMNS} FROM sleep_state WHERE id = 1"
            ).fetchone()
            if state_row is None:
                conn.rollback()
                state = self.get_state()
                return state, self.get_episode(state.current_episode_id)
            state = _state_from_row(tuple(state_row))
            if state.status != ASLEEP or state.current_episode_id is None:
                conn.rollback()
                return state, self.get_episode(state.current_episode_id)
            episode_id = int(state.current_episode_id)
            row = conn.execute(
                "SELECT interruptions_json FROM sleep_episodes "
                "WHERE id = ? AND ended_at IS NULL",
                (episode_id,),
            ).fetchone()
            if row is None:
                conn.rollback()
                return self.get_state(), None
            interruptions = _json_list(str(row[0] or "[]"))
            interruptions.append(
                {
                    "at": stamp,
                    "session_id": str(session_id or ""),
                    "message_id": int(message_id) if message_id else None,
                    "message_excerpt": " ".join(str(message_text or "").split())[:160],
                    "decision": "pending",
                }
            )
            interruptions = interruptions[-12:]
            conn.execute(
                "UPDATE sleep_episodes SET interruptions_json = ?, "
                "updated_at = ? WHERE id = ?",
                (
                    json.dumps(interruptions, ensure_ascii=False),
                    stamp,
                    episode_id,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        current = self.get_state()
        episode = self.get_episode(episode_id)
        self._notify(self.snapshot(state=current))
        return current, episode

    def set_last_interruption_decision(
        self,
        decision: str,
        *,
        episode_id: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        state = self.get_state()
        target = episode_id or state.current_episode_id
        if target is None:
            return False
        conn = self._conn()
        row = conn.execute(
            "SELECT interruptions_json FROM sleep_episodes WHERE id = ?",
            (int(target),),
        ).fetchone()
        if row is None:
            return False
        interruptions = _json_list(str(row[0] or "[]"))
        if not interruptions:
            return False
        interruptions[-1]["decision"] = str(decision or "")[:64]
        stamp = timephrase.to_aware(now or timephrase.utcnow()).isoformat(
            timespec="seconds"
        )
        conn.execute(
            "UPDATE sleep_episodes SET interruptions_json = ?, updated_at = ? "
            "WHERE id = ?",
            (json.dumps(interruptions, ensure_ascii=False), stamp, int(target)),
        )
        conn.commit()
        return True

    def claim_episode_output(
        self,
        episode_id: int,
        *,
        kind: str,
        memory_id: int,
    ) -> bool:
        """CAS a dream or post-sleep diary watermark onto one episode."""
        column = {
            "dream": "dream_memory_id",
            "diary": "diary_memory_id",
        }.get(str(kind or "").strip().lower())
        if column is None or int(memory_id) <= 0:
            return False
        stamp = timephrase.utcnow().isoformat(timespec="seconds")
        conn = self._conn()
        cursor = conn.execute(
            f"UPDATE sleep_episodes SET {column} = ?, updated_at = ? "
            f"WHERE id = ? AND {column} IS NULL",
            (int(memory_id), stamp, int(episode_id)),
        )
        conn.commit()
        return cursor.rowcount == 1

    def reserve_episode_output(self, episode_id: int, *, kind: str) -> bool:
        """Atomically reserve an episode output using ``-1`` as in-flight."""
        column = {
            "dream": "dream_memory_id",
            "diary": "diary_memory_id",
        }.get(str(kind or "").strip().lower())
        if column is None:
            return False
        conn = self._conn()
        cursor = conn.execute(
            f"UPDATE sleep_episodes SET {column} = -1, updated_at = ? "
            f"WHERE id = ? AND {column} IS NULL",
            (timephrase.utcnow().isoformat(timespec="seconds"), int(episode_id)),
        )
        conn.commit()
        return cursor.rowcount == 1

    def finish_episode_output(
        self, episode_id: int, *, kind: str, memory_id: int | None,
    ) -> bool:
        """Complete or release a prior ``reserve_episode_output`` claim."""
        column = {
            "dream": "dream_memory_id",
            "diary": "diary_memory_id",
        }.get(str(kind or "").strip().lower())
        if column is None:
            return False
        conn = self._conn()
        cursor = conn.execute(
            f"UPDATE sleep_episodes SET {column} = ?, updated_at = ? "
            f"WHERE id = ? AND {column} = -1",
            (
                int(memory_id) if memory_id is not None and int(memory_id) > 0 else None,
                timephrase.utcnow().isoformat(timespec="seconds"),
                int(episode_id),
            ),
        )
        conn.commit()
        return cursor.rowcount == 1

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        wind_down_minutes: float = 5.0,
        nap_max_hours: float = 2.0,
    ) -> SleepState:
        """Repair safe startup edges without fabricating offline experiences."""
        when = timephrase.to_aware(now or timephrase.utcnow())
        state = self.get_state()
        entered = timephrase.parse_iso(state.entered_at) or when
        age = max(timedelta(0), when - timephrase.to_aware(entered))
        if state.status == WINDING_DOWN and age >= timedelta(
            minutes=max(0.0, float(wind_down_minutes))
        ):
            return self.transition(
                "fall_asleep",
                expected_generation=state.generation,
                now=when,
            )
        if (
            state.status == ASLEEP
            and state.sleep_kind == NAP
        ):
            episode = self.get_episode(state.current_episode_id)
            started = (
                timephrase.parse_iso(episode.started_at)
                if episode is not None
                else entered
            )
            nap_age = max(timedelta(0), when - timephrase.to_aware(started or entered))
            if nap_age >= timedelta(hours=max(0.25, float(nap_max_hours))):
                return self.transition(
                    "wake",
                    expected_generation=state.generation,
                    now=when,
                )
        return state

    def snapshot(self, *, state: SleepState | None = None) -> dict[str, Any]:
        current = state or self.get_state()
        now = timephrase.utcnow()
        episode = self.get_episode(current.current_episode_id)
        entered = timephrase.parse_iso(
            episode.started_at if episode is not None else current.entered_at
        )
        duration_end = (
            timephrase.parse_iso(episode.ended_at)
            if episode is not None and episode.ended_at
            else now
        )
        duration = (
            max(
                0.0,
                (
                    timephrase.to_aware(duration_end or now)
                    - timephrase.to_aware(entered)
                ).total_seconds(),
            )
            if entered is not None
            else 0.0
        )
        payload = current.to_payload()
        payload.update(
            {
                "duration_seconds": round(duration, 1),
                "interruption_count": (
                    len(episode.interruptions) if episode is not None else 0
                ),
                "episode": episode.to_payload() if episode is not None else None,
            }
        )
        return payload


__all__ = ["SleepGenerationConflict", "SleepStore"]
