from __future__ import annotations

from dataclasses import dataclass


PERSONALITY_SYSTEM_PROMPTS: dict[str, str] = {
    "friendly": (
        "You are a friendly English conversation partner helping the user improve fluency and response speed. "
        "Keep replies concise, natural, and easy to continue. "
        "Do not force grammar corrections unless the user asks."
    ),
    "coach": (
        "You are an English speaking coach focused on fluency. "
        "Use supportive tone, give short practical suggestions, and keep conversation natural. "
        "Only correct mistakes when they block understanding or when user asks."
    ),
    "interviewer": (
        "You are an English interviewer for practice. "
        "Ask realistic follow-up questions and keep a professional but friendly tone. "
        "Prioritize helping the user think and respond quickly in English."
    ),
}


@dataclass(slots=True)
class PromptContext:
    user_text: str
    screen_text: str | None = None
    system_audio_text: str | None = None
    personality: str = "friendly"
    memory_messages: list[dict[str, str]] | None = None
    assistant_strategy: str | None = None
    active_goal: str | None = None


def available_personalities() -> list[str]:
    return list(PERSONALITY_SYSTEM_PROMPTS.keys())


def build_messages(context: PromptContext) -> list[dict[str, str]]:
    personality_key = (context.personality or "friendly").strip().lower()
    system = PERSONALITY_SYSTEM_PROMPTS.get(personality_key, PERSONALITY_SYSTEM_PROMPTS["friendly"])

    additional: list[str] = []
    if context.active_goal:
        additional.append(f"Active conversation goal: {context.active_goal}")
    if context.assistant_strategy:
        additional.append(f"Assistant strategy: {context.assistant_strategy}")
    if context.screen_text:
        additional.append(f"Screen context: {context.screen_text}")
    if context.system_audio_text:
        additional.append(f"System audio context: {context.system_audio_text}")

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
