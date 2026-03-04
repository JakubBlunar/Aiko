from __future__ import annotations

from dataclasses import dataclass

from app.llm.prompt_builder import PromptContext, build_messages


@dataclass(slots=True)
class TurnInput:
    user_text: str
    screen_text: str | None = None
    system_audio_text: str | None = None
    personality: str = "friendly"
    persona_background: str | None = None
    persona_user_notes: list[str] | None = None
    memory_messages: list[dict[str, str]] | None = None
    assistant_strategy: str | None = None
    active_goal: str | None = None


class TurnManager:
    def build_chat_messages(self, turn_input: TurnInput) -> list[dict[str, str]]:
        context = PromptContext(
            user_text=turn_input.user_text,
            screen_text=turn_input.screen_text,
            system_audio_text=turn_input.system_audio_text,
            personality=turn_input.personality,
            persona_background=turn_input.persona_background,
            persona_user_notes=turn_input.persona_user_notes,
            memory_messages=turn_input.memory_messages,
            assistant_strategy=turn_input.assistant_strategy,
            active_goal=turn_input.active_goal,
        )
        return build_messages(context)
