"""Shared context accounting for worker prompts with worker-owned item order."""
from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from app.llm.token_utils import estimate_tokens


Item = TypeVar("Item")
_UNKNOWN_WINDOW_FALLBACK = 4096


@dataclass(frozen=True, slots=True)
class PackedWorkerPrompt:
    messages: list[dict[str, str]]
    item_count: int
    input_chars: int
    input_tokens: int
    input_limit: int
    context_window: int
    output_reserved: int


def pack_worker_prompt(
    items: Sequence[Item],
    render: Callable[[Sequence[Item]], list[dict[str, str]]],
    *,
    context_window: int | None,
    output_tokens: int,
    surface: str,
    log: logging.Logger,
    min_items: int = 1,
    report: bool = True,
) -> PackedWorkerPrompt | None:
    """Fit the longest prefix in the caller's chosen order; never drop fixed text."""
    window = int(context_window or 0)
    if window <= 0:
        if report:
            log.warning("%s: unknown context window; using %d", surface, _UNKNOWN_WINDOW_FALLBACK)
        window = _UNKNOWN_WINDOW_FALLBACK
    reserve = max(0, int(output_tokens))
    margin = max(64, min(1024, window // 20))
    limit = window - reserve - margin

    def build(count: int) -> tuple[list[dict[str, str]], int]:
        messages = render(items[:count])
        tokens = sum(estimate_tokens(message.get("content", "")) + 4 for message in messages)
        return messages, tokens

    minimum = max(0, int(min_items))
    if len(items) < minimum:
        return None
    base_messages, base_tokens = build(minimum)
    if base_tokens > limit:
        if report:
            log.warning(
                "%s: prompt cannot fit minimum items=%d input=%d limit=%d "
                "window=%d output_reserved=%d",
                surface, minimum, base_tokens, limit, window, reserve,
            )
        return None

    best_count, best_messages, best_tokens = minimum, base_messages, base_tokens
    low, high = minimum + 1, len(items)
    while low <= high:
        middle = (low + high) // 2
        messages, tokens = build(middle)
        if tokens <= limit:
            best_count, best_messages, best_tokens = middle, messages, tokens
            low = middle + 1
        else:
            high = middle - 1
    packed = PackedWorkerPrompt(
        best_messages, best_count,
        sum(len(message.get("content", "")) for message in best_messages),
        best_tokens, limit, window, reserve,
    )
    if report:
        report_worker_prompt(packed, len(items), surface=surface, log=log)
    return packed


def report_worker_prompt(
    packed: PackedWorkerPrompt, item_count: int, *, surface: str,
    log: logging.Logger,
) -> None:
    """Report the selected pack once after a worker's optional-context trials."""
    dropped = item_count - packed.item_count
    if dropped:
        log.warning(
            "%s: dropped %d of %d items chars=%d input=%d limit=%d "
            "window=%d output_reserved=%d",
            surface, dropped, item_count, packed.input_chars, packed.input_tokens,
            packed.input_limit, packed.context_window, packed.output_reserved,
        )
    else:
        log.info(
            "%s: prompt chars=%d input=%d limit=%d window=%d output_reserved=%d",
            surface, packed.input_chars, packed.input_tokens, packed.input_limit,
            packed.context_window, packed.output_reserved,
        )
