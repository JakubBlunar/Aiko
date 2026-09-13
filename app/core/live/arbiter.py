"""Validate a Live policy proposal. Nothing here executes an action."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Iterable

from app.core.live.proposal import LIVE_POLICY_INTENTS, LivePolicyProposal
from app.core.live.resolver import FORBIDDEN_RIG_MARKERS

# Nonverbal intents may be chosen without an urge. Speech still needs one
# (or the critical-user reflex / talk-about path outside this check).
NONVERBAL_OK_EMPTY_URGE = frozenset({
    "noop",
    "wait",
    "attend",
    "acknowledge_user",
    "react_affectively",
    "remain_present",
    "yield_floor",
})
SPEECH_INTENTS = frozenset({"backchannel_user", "request_main_speech"})


@dataclass(frozen=True, slots=True)
class LiveArbiterResult:
    accepted: bool
    reason: str
    proposal: LivePolicyProposal | None = None
    talk_about: bool = False

    def to_payload(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "reason": self.reason,
            "talk_about": self.talk_about,
            "proposal": (
                self.proposal.to_payload() if self.proposal is not None else None
            ),
        }


def _blob(proposal: LivePolicyProposal) -> str:
    parts = [
        proposal.intent,
        proposal.reason_code,
        proposal.selected_urge_id,
        " ".join(proposal.context_refs),
        " ".join(proposal.wake_on),
        json_dump(proposal.arguments),
    ]
    return " ".join(parts)


def json_dump(value: Any) -> str:
    try:
        return json.dumps(value, default=str)
    except Exception:
        return str(value)


def _has_rig_identifiers(proposal: LivePolicyProposal) -> bool:
    blob = _blob(proposal)
    return any(marker in blob for marker in FORBIDDEN_RIG_MARKERS)


def arbitrate_live_proposal(
    proposal: LivePolicyProposal,
    *,
    snapshot_generation: int,
    known_urge_ids: Iterable[str] = (),
    known_context_refs: Iterable[str] = (),
    user_intent: bool = False,
) -> LiveArbiterResult:
    """Authorize a parsed proposal. Never reads ``confidence``.

    Generation is a server token: the caller stamps it and drops late
    proposals. This function does not reject a wrong model echo.
    Unknown urge ids fail. Nonverbal intents may omit an urge; speech
    intents still need one. Unknown context_refs are dropped, not a
    reject. Rig identifiers are forbidden.
    Unprompted ``request_main_speech`` is accepted as a talk-about
    record only — the caller must not enqueue TurnRunner.
    """
    del snapshot_generation
    if "confidence" in proposal.arguments:
        # Model stuffed it into arguments; still not an authorization input.
        proposal = LivePolicyProposal(
            snapshot_generation=proposal.snapshot_generation,
            selected_urge_id=proposal.selected_urge_id,
            intent=proposal.intent,
            arguments={
                key: value
                for key, value in proposal.arguments.items()
                if key != "confidence"
            },
            reason_code=proposal.reason_code,
            context_refs=proposal.context_refs,
            reconsider_after_ms=proposal.reconsider_after_ms,
            wake_on=proposal.wake_on,
        )
    if proposal.intent not in LIVE_POLICY_INTENTS:
        return LiveArbiterResult(False, "unknown_intent", proposal)
    if _has_rig_identifiers(proposal):
        return LiveArbiterResult(False, "rig_identifier", proposal)
    urge_ids = {str(item) for item in known_urge_ids if str(item)}
    if proposal.selected_urge_id:
        if urge_ids and proposal.selected_urge_id not in urge_ids:
            return LiveArbiterResult(False, "unknown_urge", proposal)
    elif proposal.intent not in NONVERBAL_OK_EMPTY_URGE:
        return LiveArbiterResult(False, "missing_urge", proposal)
    refs = {str(item) for item in known_context_refs if str(item)}
    if refs and proposal.context_refs:
        kept = tuple(ref for ref in proposal.context_refs if ref in refs)
        if kept != proposal.context_refs:
            # Untrusted 4B invents situation:… / prose citations. Drop them;
            # do not reject an otherwise legal wait/attend.
            proposal = replace(proposal, context_refs=kept)
    talk_about = (
        proposal.intent == "request_main_speech" and not user_intent
    )
    return LiveArbiterResult(True, "ok", proposal, talk_about=talk_about)


__all__ = [
    "LiveArbiterResult",
    "NONVERBAL_OK_EMPTY_URGE",
    "SPEECH_INTENTS",
    "arbitrate_live_proposal",
]
