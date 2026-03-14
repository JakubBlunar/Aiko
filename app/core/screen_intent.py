"""Screen-related intent detection (e.g. "what do you see", "read this")."""


def is_screen_intent(user_text: str) -> bool:
    """Return True if the user message suggests they want the assistant to look at the screen."""
    normalized = (user_text or "").lower()
    triggers = (
        "screen",
        "on my screen",
        "look at",
        "what do you see",
        "what can you see",
        "see this",
        "read this",
        "from the screen",
    )
    return any(token in normalized for token in triggers)
