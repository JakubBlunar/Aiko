"""Bounded delivery evidence, distinct from attention or understanding."""
from __future__ import annotations

import threading
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Callable


current_delivery_id: ContextVar[str] = ContextVar("current_delivery_id", default="")


@dataclass
class AudioDelivery:
    token: str
    owner: str
    text: str
    ended: bool = False
    status: str = "unknown"


@dataclass
class ResponseDelivery:
    token: str
    scope: tuple[str, int]
    message_id: int | None = None
    generated: bool = False
    aborted: bool = False
    text_owner: str = ""
    text_presented: bool = False
    acknowledged: bool = False
    audio_incomplete: bool = False
    clips: list[AudioDelivery] = field(default_factory=list)


class DeliveryLedger:
    def __init__(self, scope_provider: Callable[[], tuple[str, int]]) -> None:
        self._scope = scope_provider
        self._lock = threading.RLock()
        self._responses: dict[str, ResponseDelivery] = {}
        self._spoken: tuple[str, str] = ("", "")

    def begin(self, session_key: str) -> str:
        with self._lock:
            scope = self._scope()
            if scope[0] != session_key:
                return ""
            token = uuid.uuid4().hex
            self._responses[token] = ResponseDelivery(token, scope)
            while len(self._responses) > 4:
                self._responses.pop(next(iter(self._responses)))
            return token

    def finish(self, token: str, message_id: int | None, *, aborted: bool = False) -> None:
        with self._lock:
            record = self._responses.get(token)
            if record is not None and record.scope == self._scope():
                record.message_id = message_id
                record.generated = True
                record.aborted = aborted

    def set_spoken_context(self, token: str, text: str) -> None:
        with self._lock:
            self._spoken = (token, " ".join(text.split())[:120])

    def offer_audio(self, owner: str) -> str:
        with self._lock:
            token, text = self._spoken
            record = self._responses.get(token)
            if record is None or record.scope != self._scope() or not owner:
                return ""
            if len(record.clips) >= 32:
                record.audio_incomplete = True
                return ""
            clip = AudioDelivery(uuid.uuid4().hex, owner, text)
            record.clips.append(clip)
            return clip.token

    def end_audio(self, token: str) -> None:
        with self._lock:
            for record in self._responses.values():
                for clip in record.clips:
                    if clip.token == token:
                        clip.ended = True

    def cancel_audio(self) -> None:
        with self._lock:
            for record in self._responses.values():
                if record.scope != self._scope():
                    continue
                for clip in record.clips:
                    if clip.status == "unknown":
                        clip.status = "interrupted"
            self._spoken = ("", "")

    def offer_text(self, message_id: int | None, owner: str) -> str:
        with self._lock:
            for record in reversed(self._responses.values()):
                if message_id is not None and record.message_id == message_id:
                    if record.scope != self._scope() or not owner:
                        return ""
                    record.text_owner = owner
                    return record.token
            return ""

    def receipt(self, token: str, state: str, client_id: str, owner: str) -> bool:
        with self._lock:
            if not client_id or (state != "text_presented" and client_id != owner):
                return False
            for record in self._responses.values():
                if record.scope != self._scope():
                    continue
                if token == record.token and state == "text_presented":
                    if record.text_presented or not record.text_owner:
                        return False
                    if record.text_owner != "*" and (
                        record.text_owner != client_id or client_id != owner
                    ):
                        return False
                    record.text_presented = True
                    return True
                for clip in record.clips:
                    if token != clip.token or clip.owner != client_id or clip.status != "unknown":
                        continue
                    if state == "interrupted" or (state == "played" and clip.ended):
                        clip.status = state
                        return True
            return False

    def acknowledge(self, message_id: int) -> None:
        with self._lock:
            for record in self._responses.values():
                if record.scope == self._scope() and record.message_id == message_id:
                    record.acknowledged = True

    def render(self) -> str:
        with self._lock:
            records = [
                record for record in self._responses.values() if record.scope == self._scope()
            ]
            lines = []
            for record in records[-2:]:
                if not record.generated or (record.message_id is None and not record.clips):
                    continue
                played = sum(clip.status == "played" for clip in record.clips)
                audio_status = (
                    f"audio clips completed {played}/{len(record.clips)}"
                    if record.clips else "audio delivery unknown (no identified clips)"
                )
                interrupted = record.aborted or any(
                    clip.status == "interrupted" for clip in record.clips
                )
                lines.append(
                    f"Message {record.message_id or 'unpersisted'}: generated; "
                    f"text presentation {'reported' if record.text_presented else 'unknown'}; "
                    f"{audio_status}; "
                    f"interrupted={interrupted}; "
                    f"explicit acknowledgement={record.acknowledged}."
                )
                if record.audio_incomplete:
                    lines.append("Additional audio delivery is unknown.")
                lines.extend(
                    f"Clip {index + 1} ({clip.status}): {clip.text}"
                    for index, clip in enumerate(record.clips[:3])
                )
            if not lines:
                return ""
            return (
                "[Recent delivery evidence]\n" + "\n".join(lines)
                + "\nReceipts are not proof of hearing, reading, attention, "
                "understanding or agreement. "
                "Generated material and private research are not established common ground. "
                "Do not assume an interrupted or unconfirmed clip was heard; "
                "do not repeat it automatically."
            )
