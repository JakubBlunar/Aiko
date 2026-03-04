from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class PromptContext:
    user_text: str
    screen_text: str | None = None
    system_audio_text: str | None = None


def build_messages(context: PromptContext) -> list[dict[str, str]]:
    system = (
        "You are a friendly English conversation partner helping the user improve fluency and response speed. "
        "Keep replies concise, natural, and easy to continue. "
        "Do not force grammar corrections unless the user asks."
    )

    additional: list[str] = []
    if context.screen_text:
        additional.append(f"Screen context: {context.screen_text}")
    if context.system_audio_text:
        additional.append(f"System audio context: {context.system_audio_text}")

    user_content = context.user_text.strip()
    if additional:
        user_content = f"{user_content}\n\n" + "\n".join(additional)

    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
