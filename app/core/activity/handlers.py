"""Per-source redaction. Unknown sources are dropped, never persisted.

The safety property that lets UIA ship later as a handler: an envelope
we cannot redact does not hit disk. Server re-applies the title
allowlist even if the client already stripped.
"""
from __future__ import annotations

import re
from typing import Any, Protocol

from app.core.activity.envelope import ActivityEnvelope


KNOWN_SOURCES = ("foreground", "idle", "lock")

_URL_RE = re.compile(r"(?i)(?:https?://|www\.|file://)")
_TITLE_MAX = 200


class SourceHandler(Protocol):
    source: str

    def redact(
        self, envelope: ActivityEnvelope, settings: Any,
    ) -> ActivityEnvelope | None:
        """Return a safe envelope, or ``None`` to drop."""
        ...


def redact(envelope: ActivityEnvelope, settings: Any) -> ActivityEnvelope | None:
    handler = _HANDLERS.get(envelope.source)
    if handler is None:
        return None
    return handler.redact(envelope, settings)


def title_allowlist_from_settings(settings: Any) -> tuple[str, ...]:
    agent = getattr(settings, "agent", None)
    raw = getattr(agent, "activity_title_allowlist", None) if agent is not None else None
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        name = _normalise_app(str(item))
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return tuple(out)


def app_on_allowlist(app: str | None, allowlist: tuple[str, ...]) -> bool:
    if not app or not allowlist:
        return False
    needle = _normalise_app(app)
    if not needle:
        return False
    lower = needle.lower()
    return any(_normalise_app(entry).lower() == lower for entry in allowlist)


def strip_title(title: str | None) -> str | None:
    if not title:
        return None
    text = title.strip()
    if not text or _URL_RE.search(text):
        return None
    if len(text) > _TITLE_MAX:
        text = text[:_TITLE_MAX].rstrip()
    return text or None


def _normalise_app(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 4 and text[-4:].lower() == ".exe":
        text = text[:-4].strip()
    return text


class _ForegroundHandler:
    source = "foreground"

    def redact(
        self, envelope: ActivityEnvelope, settings: Any,
    ) -> ActivityEnvelope | None:
        allowlist = title_allowlist_from_settings(settings)
        app = _normalise_app(envelope.subject.app or "") or None
        title = envelope.subject.title
        if not app_on_allowlist(app, allowlist):
            title = None
        envelope.subject.app = app
        envelope.subject.title = strip_title(title)
        envelope.subject.surface_id = (envelope.subject.surface_id or None)
        envelope.payload.pop("process_path", None)
        envelope.payload.pop("pid", None)
        return envelope


class _IdleHandler:
    source = "idle"

    def redact(
        self, envelope: ActivityEnvelope, settings: Any,
    ) -> ActivityEnvelope | None:
        envelope.subject.app = None
        envelope.subject.title = None
        envelope.payload.pop("process_path", None)
        return envelope


class _LockHandler:
    source = "lock"

    def redact(
        self, envelope: ActivityEnvelope, settings: Any,
    ) -> ActivityEnvelope | None:
        envelope.subject.app = None
        envelope.subject.title = None
        envelope.payload.pop("process_path", None)
        return envelope


_HANDLERS: dict[str, SourceHandler] = {
    handler.source: handler
    for handler in (_ForegroundHandler(), _IdleHandler(), _LockHandler())
}
