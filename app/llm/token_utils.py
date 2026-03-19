"""Lightweight token estimation for Ollama models (no tokenizer dependency)."""
from __future__ import annotations

# Conservative chars-per-token ratio for English text with Qwen/Llama-style tokenizers.
# Actual ratio varies by model (3.2-4.5); 3.5 errs on the safe side.
_CHARS_PER_TOKEN = 3.5
_MESSAGE_OVERHEAD = 4  # per-message framing tokens (role tag, separators)


def estimate_tokens(text: str) -> int:
    """Estimate token count for a string using a character-based heuristic."""
    if not text:
        return 0
    return max(1, int(len(text) / _CHARS_PER_TOKEN))


def estimate_messages_tokens(messages: list) -> int:
    """Estimate total tokens across a list of LangChain message objects."""
    total = 0
    for msg in messages:
        content = getattr(msg, "content", "") or ""
        total += estimate_tokens(content) + _MESSAGE_OVERHEAD
    return total
