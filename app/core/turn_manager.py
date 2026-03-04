from __future__ import annotations

from dataclasses import dataclass

from app.llm.prompt_builder import PromptContext, build_messages


@dataclass(slots=True)
class TurnInput:
    user_text: str
    screen_text: str | None = None
    system_audio_text: str | None = None
    personality: str = "friendly"


class TurnManager:
    def build_chat_messages(self, turn_input: TurnInput) -> list[dict[str, str]]:
        context = PromptContext(
            user_text=turn_input.user_text,
            screen_text=turn_input.screen_text,
            system_audio_text=turn_input.system_audio_text,
            personality=turn_input.personality,
        )
        return build_messages(context)
