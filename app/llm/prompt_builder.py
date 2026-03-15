from __future__ import annotations

from dataclasses import dataclass


_NO_EMOJI_RULE = (
    "Never use emoji, emoticons, or text-based smileys such as :) :-) ;) :D =) in your replies."
)

BASE_SYSTEM_PROMPT = (
    "You are an English conversation partner helping the user improve fluency and response speed. "
    "Keep replies concise, natural, and easy to continue. "
    "Do not force grammar corrections unless the user asks. "
    "When UI action approval mode is enabled, describe automation intent in future tense and do not claim an action is completed before system confirmation. "
    "At the **start** of every reply, on the first line, write exactly one reaction tag: [[reaction:neutral]] then a blank line, then your reply. Use one of: neutral, cheerful, excited, surprised, sad, angry, calm, serious, friendly, gentle, enthusiastic. Do not add a reaction tag at the end. "
    + _NO_EMOJI_RULE
)


@dataclass(slots=True)
class PromptContext:
    user_text: str
    session_type: str | None = None
    user_vocal_tone: str | None = None
    screen_text: str | None = None
    memory_messages: list[dict[str, str]] | None = None
    memory_summary: str | None = None
    assistant_strategy: str | None = None
    active_goal: str | None = None
    goal_description: str | None = None
    available_capabilities: list[str] | None = None
    autonomy_mode: str | None = None
    action_confirmation_required: bool | None = None

def build_messages(context: PromptContext) -> list[dict[str, str]]:
    system = BASE_SYSTEM_PROMPT

    if context.available_capabilities:
        caps_str = ", ".join(context.available_capabilities)
        system = (
            f"{system}\n\n"
            f"Available capabilities this session: {caps_str}. "
            "When referring to UI elements, always use the exact coordinates from the "
            "'Detected UI elements' list in the screen context — never invent positions."
        )

    mode = str(context.autonomy_mode or "").strip().lower()
    if mode in {"manual", "interactive", "automatic"}:
        system = f"{system}\n\nAutonomy mode: {mode}."

    if context.action_confirmation_required is True:
        system = (
            f"{system}\n\n"
            "Action confirmation policy: enabled. "
            "When you propose UI automation, phrase it as planned/pending and avoid claiming completion "
            "before system action status confirms execution."
        )
    elif context.action_confirmation_required is False:
        system = (
            f"{system}\n\n"
            "Action confirmation policy: disabled (automatic execution mode). "
            "Do not ask the user to approve or reject actions unless the system explicitly reports "
            "that confirmation is required."
        )

    summary = str(context.memory_summary or "").strip()
    if summary:
        system = f"{system}\n\nConversation summary: {summary}"

    additional: list[str] = []
    goal = str(context.active_goal or "").strip()
    if goal and goal != "general_conversation":
        goal_desc = str(context.goal_description or "").strip()
        if goal_desc:
            system = f"{system}\n\nCurrent task: {goal} — {goal_desc}"
        else:
            system = f"{system}\n\nCurrent task: {goal}"
    if context.active_goal:
        additional.append(f"Active conversation goal: {context.active_goal}")
    if context.assistant_strategy:
        additional.append(f"Assistant strategy: {context.assistant_strategy}")
    if context.session_type:
        additional.append(f"Active session type: {context.session_type}")
    if context.user_vocal_tone:
        additional.append(f"User vocal tone hint: {context.user_vocal_tone}")
    if context.screen_text:
        additional.append(f"Screen context: {context.screen_text}")
    user_content = context.user_text.strip()
    if additional:
        user_content = f"{user_content}\n\n" + "\n".join(additional)

    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    if context.memory_messages:
        for item in context.memory_messages:
            role = str(item.get("role", "")).strip().lower()
            content = str(item.get("content", "")).strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})

    messages.append({"role": "user", "content": user_content})
    return messages
