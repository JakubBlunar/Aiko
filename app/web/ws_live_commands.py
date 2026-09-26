"""WebSocket commands for the Live shadow impulse bus.

Kept out of ``server.py`` so that file does not grow further. All calls
go through public SessionController methods (private-reach budget is 0).
"""
from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger("app.web")
live_log = logging.getLogger("app.live")


def live_impulse_owner_id(hub: Any) -> str | None:
    """Voice owner, else audio owner. None if nobody is elected."""
    if hub is None:
        return None
    return getattr(hub, "voice_owner_id", None) or getattr(hub, "audio_owner_id", None)


def client_owns_live_impulses(hub: Any, client_id: str) -> bool:
    owner = live_impulse_owner_id(hub)
    token = str(client_id or "")
    if not token or hub is None:
        return True
    if not owner:
        return False
    return token == str(owner)


def handle_live_ws_command(
    session: Any,
    msg_type: str,
    msg: dict[str, Any],
    *,
    client_id: str = "",
    hub: Any = None,
) -> bool:
    """Handle Live-related client frames. Return True if consumed."""
    if msg_type == "delivery_receipt":
        owner = str(getattr(hub, "audio_owner_id", None) or "")
        try:
            session.delivery_ledger().receipt(
                str(msg.get("delivery_id") or ""), str(msg.get("state") or ""),
                client_id, owner,
            )
        except Exception:
            log.debug("delivery receipt failed", exc_info=True)
        return True
    if msg_type == "playback_drained":
        if hub is not None and client_id != str(getattr(hub, "audio_owner_id", None) or ""):
            return True
        try:
            session.notify_playback_drained()
        except Exception:
            log.debug("playback_drained notify failed", exc_info=True)
        return True
    owner = live_impulse_owner_id(hub) if hub is not None else None
    owns = client_owns_live_impulses(hub, client_id)
    if msg_type == "composing":
        if hub is not None and not owns:
            live_log.debug(
                "live impulse dropped: kind=user.typing client=%s owner=%s",
                str(client_id or "-")[:8],
                str(owner or "-")[:8],
            )
            return True
        active = bool(msg.get("active", False))
        surface = str(msg.get("surface") or "chat").strip() or "chat"
        if surface not in {"chat", "persona"}:
            surface = "chat"
        try:
            session.publish_live_impulse(
                kind="user.typing_started" if active else "user.typing_stopped",
                source=f"web.{surface}",
                coalesce_key=f"user.typing.{surface}",
                privacy="local_state",
                priority="attention",
                ttl_ms=4000,
                payload={"active": active, "surface": surface},
            )
        except Exception:
            log.debug("composing impulse failed", exc_info=True)
        return True
    if msg_type in {"voice_start", "voice_stop", "stop"}:
        if hub is not None and not owns and msg_type != "voice_start":
            live_log.debug(
                "live impulse dropped: kind=user.%s client=%s owner=%s",
                msg_type,
                str(client_id or "-")[:8],
                str(owner or "-")[:8],
            )
            return False
        try:
            session.publish_live_impulse(
                kind=f"user.{msg_type}",
                source="web.control",
                coalesce_key=f"control.{msg_type}",
                privacy="local_state",
                priority="control",
                ttl_ms=8000,
                payload={"type": msg_type},
            )
        except Exception:
            log.debug("control impulse failed", exc_info=True)
        return False
    if msg_type in {"switch_session", "new_session"}:
        try:
            session.publish_live_impulse(
                kind="user.session_changed",
                source="web.control",
                coalesce_key="control.session",
                privacy="local_state",
                priority="control",
                ttl_ms=8000,
                payload={"type": msg_type},
            )
        except Exception:
            log.debug("session impulse failed", exc_info=True)
        return False
    return False


def offer_text_delivery(session: Any, hub: Any, message_id: Any) -> str:
    getter = getattr(session, "delivery_ledger", None)
    if not callable(getter) or message_id is None:
        return ""
    token = getter().offer_text(int(message_id), str(getattr(hub, "audio_owner_id", None) or "*"))
    return token if isinstance(token, str) else ""
