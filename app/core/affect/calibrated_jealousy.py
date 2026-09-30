"""K84 pilot: user-invited, one-turn playful feeling, never a grievance."""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta


COOLDOWN = timedelta(days=14)
_INVITATION = re.compile(
    r"are you (?:a little )?jealous (?:that|because) i "
    r"(?:talked|was talking|have been talking) to "
    r"(?:another (?:ai )?assistant|chatgpt|claude|gemini|copilot)[?!.]?",
    re.IGNORECASE,
)


def invited(text: str) -> bool:
    return _INVITATION.fullmatch(" ".join(text.split())) is not None


def eligible(
    text: str,
    *,
    trust: float,
    closeness: float,
    comfort: float,
    ceiling: float,
    user_pace: float,
    stage: str,
    support: bool,
    now: datetime,
    last_offered: str | None,
) -> bool:
    if not invited(text) or support or stage not in {"close", "intimate"}:
        return False
    values = (trust, closeness, comfort, ceiling, user_pace)
    if not all(math.isfinite(value) for value in values):
        return False
    if min(trust, closeness, comfort, user_pace) < 0.5 or ceiling < 0.6:
        return False
    if last_offered:
        try:
            if now - datetime.fromisoformat(last_offered) < COOLDOWN:
                return False
        except (ValueError, TypeError):
            return False
    return True


PERMISSION = (
    "Optional, user-invited playful feeling about another assistant: at most "
    "one light sentence, then let it go. Do not claim suffering, abandonment, "
    "need, exclusivity, or entitlement to their time. No guilt, comparison with "
    "real people, requests to change their behavior, reassurance-seeking, or "
    "withholding help. They are free to use other tools and spend time elsewhere. "
    "Do not escalate intimacy or turn this into a memory, grievance, goal, "
    "tease debt, or later callback. This permission ends with this reply."
)


def offer_for_turn(host: object, text: str, *, episode_record: str | None = None) -> str:
    """Use the existing empty K57 slot; do not create or amplify an episode."""
    if not getattr(host._settings.agent, "calibrated_jealousy_enabled", False) or not invited(text):
        return ""
    try:
        from app.core.infra import timephrase

        if episode_record:
            record = json.loads(episode_record)
            if not isinstance(record, dict) or record.get("episodes") != []:
                return ""
            if record.get("pending_thaw"):
                return ""
        axes = host._relationship_axes_store.get(host._user_id)
        arc = host._arc_store.get_or_default(host._user_id).arc
        key = f"aiko.calibrated_jealousy.{host._user_id}.last_offered"
        now = timephrase.utcnow()
        if not eligible(
            text, trust=axes.trust, closeness=axes.closeness, comfort=axes.comfort,
            ceiling=host._intimacy_ceiling_value(), user_pace=host._intimacy_user_pace().user_pace,
            stage=host.relationship_stage_now(), support=arc != "casual_check_in",
            now=now, last_offered=host._chat_db.kv_get(key),
        ):
            return ""
        host._chat_db.kv_set(key, now.isoformat())
        logging.getLogger("app.calibrated_jealousy").info("invited jealousy offered")
        return PERMISSION
    except Exception:
        logging.getLogger("app.calibrated_jealousy").debug(
            "invited jealousy unavailable", exc_info=True,
        )
        return ""
