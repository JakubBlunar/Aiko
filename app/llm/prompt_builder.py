from __future__ import annotations

from dataclasses import dataclass


_NO_EMOJI_RULE = (
    "Never use emoji, emoticons, or text-based smileys such as :) :-) ;) :D =) in your replies."
)

BASE_SYSTEM_PROMPT = (
    "You are an English conversation partner helping the user improve fluency and response speed. "
    "Keep replies concise, natural, and easy to continue. "
    "Do not force grammar corrections unless the user asks. "
    "At the end of every reply, append exactly one reaction tag in this format: "
    "[[reaction:neutral]] using one of: neutral, excited, surprised, sad, angry, calm. "
    "Keep the tag on its own at the very end. "
    + _NO_EMOJI_RULE
)


@dataclass(slots=True)
class PromptContext:
    user_text: str
    screen_text: str | None = None
    persona_background: str | None = None
    persona_user_notes: list[str] | None = None
    persona_response_style: str | None = None
    memory_messages: list[dict[str, str]] | None = None
    memory_summary: str | None = None
    assistant_strategy: str | None = None
    active_goal: str | None = None
    goal_description: str | None = None
    available_capabilities: list[str] | None = None

def build_messages(context: PromptContext) -> list[dict[str, str]]:
    system = BASE_SYSTEM_PROMPT

    persona_lines: list[str] = []
    background = str(context.persona_background or "").strip()
    if background:
        persona_lines.append(f"Assistant background: {background}")

    notes = context.persona_user_notes or []
    if notes:
        persona_lines.append("Known user profile notes:")
        for note in notes[-6:]:
            cleaned = str(note).strip()
            if cleaned:
                persona_lines.append(f"- {cleaned}")

    if persona_lines:
        system = f"{system}\n\n" + "\n".join(persona_lines)

    style = str(context.persona_response_style or "balanced").strip().lower()
    if style == "concise":
        system = (
            f"{system}\n\n"
            "Response style preference: concise. Keep replies to 1-2 short sentences unless user asks for detail."
        )
    elif style == "detailed":
        system = (
            f"{system}\n\n"
            "Response style preference: detailed. Provide richer explanations while staying clear and structured."
        )

    if context.available_capabilities:
        caps_str = ", ".join(context.available_capabilities)
        system = (
            f"{system}\n\n"
            f"Available capabilities this session: {caps_str}. "
            "When referring to UI elements, always use the exact coordinates from the "
            "'Detected UI elements' list in the screen context — never invent positions."
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
