"""Read provider token details without guessing an unreported reasoning split."""

from app.llm.chat_client import ChatUsage


def read_reasoning_tokens(usage: ChatUsage, payload: dict, *, responses: bool = False) -> None:
    details = payload.get("output_tokens_details" if responses else "completion_tokens_details")
    if not isinstance(details, dict) or "reasoning_tokens" not in details:
        return
    raw = details["reasoning_tokens"]
    if type(raw) is int and raw >= 0:
        usage.reasoning_tokens = min(raw, usage.completion_tokens)


def responses_usage(
    usage_dict: object, *, total_ms: float, done_reason: str | None,
) -> ChatUsage:
    """Build a ChatUsage from a Responses ``usage`` block.

    Responses names the token fields ``input_tokens`` /
    ``output_tokens`` (vs chat-completions' ``prompt_tokens`` /
    ``completion_tokens``) and reports cache hits under
    ``input_tokens_details.cached_tokens``. ``done_reason`` is already
    mapped by :func:`_parse_responses_output`, so it's stored as-is.
    """
    usage = ChatUsage(total_duration_ms=float(total_ms))
    if isinstance(usage_dict, dict):
        usage.prompt_tokens = int(usage_dict.get("input_tokens", 0) or 0)
        usage.completion_tokens = int(usage_dict.get("output_tokens", 0) or 0)
        details = usage_dict.get("input_tokens_details")
        if isinstance(details, dict):
            usage.cached_tokens = int(details.get("cached_tokens", 0) or 0)
        read_reasoning_tokens(usage, usage_dict, responses=True)
    usage.done_reason = done_reason
    return usage


def fill_wall_clock_eval_duration(
    usage: ChatUsage,
    first_token_ms: float | None,
    elapsed_ms: float,
) -> None:
    """Derive generation time from the stream when the provider won't say.

    Ollama reports ``eval_duration`` natively; no OpenAI-compatible
    endpoint does. ``ChatUsage.tokens_per_second`` divides by that field,
    so every cloud turn used to render as ``0 tok/s`` in the UI and log a
    flat ``eval_ms=0``.

    The stream itself already knows enough: the span between the first
    content delta and the last byte IS the generation phase, with
    time-to-first-token (queueing plus prompt eval) excluded. It carries
    network jitter that Ollama's server-side number does not, hence the
    ``eval_duration_estimated`` flag rather than pretending the two are
    the same measurement.

    Only fills a field the provider left empty, so a future endpoint that
    starts reporting real timings silently wins.
    """
    if usage.eval_duration_ms > 0 or first_token_ms is None:
        return
    usage.eval_duration_ms = max(0.0, elapsed_ms - first_token_ms)
    usage.eval_duration_estimated = True

