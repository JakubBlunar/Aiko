"""Transcript edits and reply replacement, serialized against normal turns."""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from functools import wraps
from typing import Any, ParamSpec, TypeVar

from app.core.infra.chat_database import MessageRow

log = logging.getLogger("app.session")
P = ParamSpec("P")
R = TypeVar("R")


class HistoryConflict(Exception):
    """The conversation moved or is busy."""


class HistoryMessageNotFound(Exception):
    """The selected message does not belong to this conversation."""


def history_lock(session: Any) -> Any:
    return session.__dict__.setdefault("_history_edit_lock", threading.RLock())


def serialized_history_turn(method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        with history_lock(args[0]):
            return method(*args, **kwargs)

    return wrapped


def notify_history(session: Any, event: dict[str, Any]) -> None:
    for listener in list(session.__dict__.get("_history_listeners", ())):
        try:
            listener(event)
        except Exception:
            log.exception("history listener failed: type=%s", event["type"])


class MessageHistoryMixin:
    def add_history_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        listeners = self.__dict__.setdefault("_history_listeners", [])
        if callback not in listeners:
            listeners.append(callback)

    def _history_message(self, session_key: str, message_id: int) -> MessageRow:
        if session_key != self.session_key:
            raise HistoryConflict("The active conversation changed. Reload and try again.")
        row = self._chat_db.get_message_row(message_id)
        if (
            row is None or row.session_id != self._history_storage_key(session_key)
            or row.role not in {"user", "assistant"}
        ):
            raise HistoryMessageNotFound("Message not found in this conversation.")
        return row

    def _history_storage_key(self, session_key: str) -> str:
        return (
            session_key if self.__dict__.get("_remember_history", True)
            else f"{session_key}:noremember"
        )

    def _check_history_idle(self) -> None:
        if (
            self.__dict__.get("_pending_chat_retry") is not None
            or self.__dict__.get("_turn_in_progress", False)
        ):
            raise HistoryConflict("Wait for the current reply to finish, or stop it first.")

    def _delete_history_rows(self, session_key: str, ids: list[int]) -> list[int]:
        storage_key = self._history_storage_key(session_key)
        deleted = self._chat_db.delete_message_rows(storage_key, ids)
        if not deleted:
            return []
        notify_history(self, {
            "type": "messages_deleted", "session": session_key, "message_ids": deleted,
        })
        assembler = getattr(self, "_prompt_assembler", None)
        if assembler is not None:
            assembler.reset_slice_cache(storage_key)
        prefetcher = getattr(self, "_rag_prefetcher", None)
        if prefetcher is not None:
            prefetcher.reset()
        rag = getattr(self, "_rag_store", None)
        if rag is not None:
            rag.delete_messages(storage_key, deleted)
        log.info("messages deleted: session=%s ids=%s", session_key, deleted)
        return deleted

    def delete_chat_message(self, session_key: str, message_id: int) -> list[int]:
        lock = history_lock(self)
        if not lock.acquire(blocking=False):
            raise HistoryConflict("Wait for the current reply to finish, or stop it first.")
        try:
            self._check_history_idle()
            row = self._history_message(session_key, message_id)
            if row.role == "assistant":
                self.stop_tts()
            return self._delete_history_rows(session_key, [row.id])
        finally:
            lock.release()

    def reserve_chat_retry(self, session_key: str, message_id: int) -> MessageRow:
        lock = history_lock(self)
        if not lock.acquire(blocking=False):
            raise HistoryConflict("Wait for the current reply to finish, or stop it first.")
        try:
            self._check_history_idle()
            row = self._history_message(session_key, message_id)
            self._check_latest_retry(row)
            self._pending_chat_retry = (session_key, row.id)
            return row
        finally:
            lock.release()

    def _check_latest_retry(self, row: MessageRow) -> None:
        if (
            row.role != "user"
            or self._chat_db.get_latest_user_message_id(row.session_id) != row.id
        ):
            raise HistoryConflict("Only the latest user message can be resent.")

    def prepare_chat_retry(self, message_id: int) -> tuple[MessageRow, list[dict]]:
        """Revalidate on the brain thread before removing the old response."""
        pending = self.__dict__.get("_pending_chat_retry")
        if pending is None or pending[1] != message_id:
            raise HistoryConflict("This resend is no longer pending.")
        row = self._history_message(pending[0], message_id)
        self._check_latest_retry(row)
        attachments = json.loads(row.attachments) if row.attachments else []
        if not isinstance(attachments, list) or any(not isinstance(a, dict) for a in attachments):
            raise ValueError("The original message has invalid stored attachments.")
        self.stop_tts()
        self._clear_merge_buffer()
        ids = self._chat_db.get_message_ids_after(row.session_id, row.id)
        self._delete_history_rows(pending[0], ids)
        notify_history(self, {
            "type": "reply_replacing", "session": pending[0], "user_message_id": row.id,
        })
        return row, attachments

    def finish_chat_retry(self, message_id: int) -> None:
        with history_lock(self):
            pending = self.__dict__.get("_pending_chat_retry")
            if pending is not None and pending[1] == message_id:
                self._pending_chat_retry = None
