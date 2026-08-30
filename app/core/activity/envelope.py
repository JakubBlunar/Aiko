"""Parse a versioned activity envelope. Unknown ``v`` is dropped."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CURRENT_VERSION = 1
_KNOWN_TOP = frozenset(
    {"v", "at", "source", "tier", "subject", "signal", "payload"},
)


@dataclass(slots=True)
class ActivitySubject:
    app: str | None = None
    title: str | None = None
    surface_id: str | None = None


@dataclass(slots=True)
class ActivityEnvelope:
    v: int
    at: str
    source: str
    tier: str
    subject: ActivitySubject
    signal_kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_payload_json(self) -> dict[str, Any]:
        """Payload plus any unknown v1 top-level keys (forward compatible)."""
        out = dict(self.payload)
        for key, value in self.extras.items():
            out.setdefault(key, value)
        return out


def parse_envelope(raw: Any) -> ActivityEnvelope | None:
    """Return a typed envelope, or ``None`` if it cannot be stored safely.

    Unknown ``v`` is dropped (we cannot redact what we do not understand).
    Missing / malformed required fields are dropped the same way.
    """
    if not isinstance(raw, dict):
        return None
    try:
        version = int(raw.get("v"))
    except (TypeError, ValueError):
        return None
    if version != CURRENT_VERSION:
        return None
    source = str(raw.get("source") or "").strip()
    if not source:
        return None
    at = str(raw.get("at") or "").strip()
    if not at:
        return None
    tier = str(raw.get("tier") or "cheap").strip() or "cheap"
    subject_raw = raw.get("subject") if isinstance(raw.get("subject"), dict) else {}
    signal_raw = raw.get("signal") if isinstance(raw.get("signal"), dict) else {}
    kind = str(signal_raw.get("kind") or "").strip()
    if not kind:
        return None
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    extras = {k: v for k, v in raw.items() if k not in _KNOWN_TOP}
    app = subject_raw.get("app")
    title = subject_raw.get("title")
    surface_id = subject_raw.get("surface_id")
    return ActivityEnvelope(
        v=version,
        at=at,
        source=source,
        tier=tier,
        subject=ActivitySubject(
            app=_optional_str(app),
            title=_optional_str(title),
            surface_id=_optional_str(surface_id),
        ),
        signal_kind=kind,
        payload=dict(payload),
        extras=extras,
    )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
