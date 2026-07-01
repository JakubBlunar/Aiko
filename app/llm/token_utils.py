"""Lightweight token estimation for Ollama models (no tokenizer dependency).

The estimate is a character-count heuristic (``chars / chars_per_token``).
The ratio is *adaptively calibrated* at runtime: after a real LLM round-trip
we know the true prompt-token count, so :func:`observe_actual_usage` folds the
observed ``chars / tokens`` ratio into a slow EMA. This keeps the estimate
honest across models with very different tokenizers (a code-heavy Qwen prompt
tokenises very differently from an English-prose Llama prompt) without pulling
in a real tokenizer dependency.
"""
from __future__ import annotations

import threading

# Conservative default chars-per-token ratio for English text with
# Qwen/Llama-style tokenizers. Actual ratio varies by model (3.2-4.5); 3.5
# errs on the safe side and is the cold-start value before any calibration.
_DEFAULT_CHARS_PER_TOKEN = 3.5
_MESSAGE_OVERHEAD = 4  # per-message framing tokens (role tag, separators)

# Clamp the calibrated ratio to a sane band so one pathological observation
# (e.g. a mostly-whitespace prompt, or a bad usage report) can never push the
# estimator into a wildly wrong regime.
_MIN_CHARS_PER_TOKEN = 2.5
_MAX_CHARS_PER_TOKEN = 5.0

# EMA smoothing factor for calibration. Small so the ratio drifts slowly and
# a single outlier turn barely moves it.
_CALIBRATION_ALPHA = 0.05

# Only calibrate off prompts large enough that per-message framing overhead and
# rounding noise are negligible relative to the signal.
_MIN_CALIBRATION_CHARS = 400
_MIN_CALIBRATION_TOKENS = 50

_lock = threading.Lock()
_chars_per_token = _DEFAULT_CHARS_PER_TOKEN
_calibration_samples = 0


def _ratio() -> float:
    # Cheap unlocked read — a float assignment is atomic under the GIL and a
    # slightly stale ratio on a concurrent turn is harmless.
    return _chars_per_token


def chars_per_token() -> float:
    """Return the current (possibly calibrated) chars-per-token ratio."""
    return _ratio()


def estimate_tokens(text: str) -> int:
    """Estimate token count for a string using a character-based heuristic."""
    if not text:
        return 0
    return max(1, int(len(text) / _ratio()))


def estimate_messages_tokens(messages: list) -> int:
    """Estimate total tokens across a list of LangChain message objects."""
    total = 0
    for msg in messages:
        content = getattr(msg, "content", "") or ""
        total += estimate_tokens(content) + _MESSAGE_OVERHEAD
    return total


def observe_actual_usage(prompt_chars: int, actual_prompt_tokens: int) -> None:
    """Fold a real (chars, prompt_tokens) observation into the EMA ratio.

    Called from the turn thread after a completed LLM round-trip. No-ops on
    tiny prompts or non-positive counts so a degenerate sample can't skew the
    estimator. Thread-safe.
    """
    if (
        prompt_chars < _MIN_CALIBRATION_CHARS
        or actual_prompt_tokens < _MIN_CALIBRATION_TOKENS
    ):
        return
    observed = prompt_chars / float(actual_prompt_tokens)
    if observed < _MIN_CHARS_PER_TOKEN or observed > _MAX_CHARS_PER_TOKEN:
        # Reject implausible ratios rather than clamp-and-learn from them.
        return
    global _chars_per_token, _calibration_samples
    with _lock:
        blended = (
            (1.0 - _CALIBRATION_ALPHA) * _chars_per_token
            + _CALIBRATION_ALPHA * observed
        )
        _chars_per_token = min(
            _MAX_CHARS_PER_TOKEN, max(_MIN_CHARS_PER_TOKEN, blended),
        )
        _calibration_samples += 1


def calibration_state() -> dict[str, float | int]:
    """Snapshot of the calibration state (for MCP debug / tests)."""
    return {
        "chars_per_token": round(_chars_per_token, 4),
        "default_chars_per_token": _DEFAULT_CHARS_PER_TOKEN,
        "samples": _calibration_samples,
        "min": _MIN_CHARS_PER_TOKEN,
        "max": _MAX_CHARS_PER_TOKEN,
    }


def reset_calibration() -> None:
    """Reset the calibrated ratio to the cold-start default (tests / MCP)."""
    global _chars_per_token, _calibration_samples
    with _lock:
        _chars_per_token = _DEFAULT_CHARS_PER_TOKEN
        _calibration_samples = 0
