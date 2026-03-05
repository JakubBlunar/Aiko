from __future__ import annotations

import re

from app.core.tooling.runtime.action_runtime import ActionExecutionResult


_ACTION_COMPLETION_CLAIM_PATTERN = re.compile(
    r"\b(i|we)\s+(just\s+)?(clicked|pressed|typed|wrote|opened|selected|filled|entered|focused|navigated)\b",
    flags=re.IGNORECASE,
)


def normalize_action_narration(text: str, action_result: ActionExecutionResult) -> str:
    reply = str(text or "").strip()
    if not reply:
        return ""

    claimed_completion = bool(_ACTION_COMPLETION_CLAIM_PATTERN.search(reply))
    if not claimed_completion:
        return reply

    if action_result.requires_confirmation:
        return "I can do that. I have not executed it yet. Please confirm the action plan below."

    if not action_result.executed:
        return "I can try that, but it did not execute yet. See the action status below."

    return reply


def build_post_action_followup(
    action_result: ActionExecutionResult,
    *,
    require_confirmation: bool,
) -> str:
    if not require_confirmation:
        return ""
    if action_result.requires_confirmation:
        return ""
    if action_result.executed and not action_result.dry_run and not action_result.blocked:
        return "Done. I executed that plan. Tell me the next step you want me to take."
    return ""