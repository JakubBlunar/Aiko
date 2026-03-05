from __future__ import annotations

from dataclasses import dataclass

from app.llm.prompt_builder import PromptContext, build_messages


@dataclass(slots=True)
class TurnInput:
    user_text: str
    user_vocal_tone: str | None = None
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


class TurnManager:
    def build_chat_messages(self, turn_input: TurnInput) -> list[dict[str, str]]:
        context = PromptContext(
            user_text=turn_input.user_text,
            user_vocal_tone=turn_input.user_vocal_tone,
            screen_text=turn_input.screen_text,
            persona_background=turn_input.persona_background,
            persona_user_notes=turn_input.persona_user_notes,
            persona_response_style=turn_input.persona_response_style,
            memory_messages=turn_input.memory_messages,
            memory_summary=turn_input.memory_summary,
            assistant_strategy=turn_input.assistant_strategy,
            active_goal=turn_input.active_goal,
            goal_description=turn_input.goal_description,
            available_capabilities=turn_input.available_capabilities,
        )
        return build_messages(context)
