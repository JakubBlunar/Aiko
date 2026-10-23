"""Provider reasoning splits must not inflate answer speed or alter billing."""

import json
from unittest.mock import patch

import pytest

from app.llm.chat_client import ChatUsage
from app.llm.openai_compatible_client import OpenAICompatibleClient, _responses_usage
from app.llm.token_usage import read_reasoning_tokens
from tests.test_openai_compatible_client import _sse_response
from tests.test_openai_stream_integrity import _client, _event


@pytest.mark.parametrize("reasoning", [None, 0, 70])
def test_usage_split_preserves_billable_totals_and_honest_speed(reasoning):
    usage = ChatUsage(prompt_tokens=1000, completion_tokens=100, reasoning_tokens=reasoning,
                      eval_duration_ms=1000)
    assert usage.total_tokens == 1100
    assert usage.answer_tokens == (None if reasoning is None else 100 - reasoning)
    assert usage.tokens_per_second == (100 if reasoning is None else 100 - reasoning)


def test_merge_requires_known_split_for_every_nonempty_call():
    first = ChatUsage(completion_tokens=10, reasoning_tokens=7)
    second = ChatUsage(completion_tokens=20, reasoning_tokens=5)
    merged = first.merge(second)
    assert merged.completion_tokens == 30
    assert merged.reasoning_tokens == 12
    assert merged.answer_tokens == 18
    assert ChatUsage().merge(first).reasoning_tokens == 7
    assert first.merge(ChatUsage(completion_tokens=20)).reasoning_tokens is None


@pytest.mark.parametrize("raw", [-1, True, "5", None, 3.5])
def test_invalid_provider_detail_is_unknown(raw):
    usage = ChatUsage(completion_tokens=10)
    read_reasoning_tokens(usage, {"completion_tokens_details": {"reasoning_tokens": raw}})
    assert usage.reasoning_tokens is None


@pytest.mark.parametrize("responses", [False, True])
def test_nonstreaming_usage_reads_provider_specific_detail(responses):
    if responses:
        usage = _responses_usage({
            "input_tokens": 100, "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 20},
        }, total_ms=100, done_reason="stop")
    else:
        usage = OpenAICompatibleClient._build_usage(usage_dict={
            "prompt_tokens": 100, "completion_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 20},
        }, total_ms=100, finish_reason="stop")
    assert usage.reasoning_tokens == 20
    assert usage.answer_tokens == 10
    assert usage.total_tokens == 130


@pytest.mark.parametrize("responses", [False, True])
@pytest.mark.parametrize("reasoning", [None, 0, 20])
def test_streaming_usage_includes_split_without_guessing(responses, reasoning):
    client = _client()
    if responses:
        client._api_style = "responses"
        usage = {"input_tokens": 100, "output_tokens": 30}
        if reasoning is not None:
            usage["output_tokens_details"] = {"reasoning_tokens": reasoning}
        frames = ["data: " + json.dumps(event) for event in [
            {"type": "response.output_text.delta", "delta": "Hello."},
            {"type": "response.completed", "response": {"status": "completed", "usage": usage}},
        ]]
    else:
        usage = {"prompt_tokens": 100, "completion_tokens": 30}
        if reasoning is not None:
            usage["completion_tokens_details"] = {"reasoning_tokens": reasoning}
        frames = [_event({"content": "Hello."}), _event(finish="stop", usage=usage)]
    with patch("app.llm.openai_compatible_client.requests.post",
               return_value=_sse_response(frames)):
        assert "".join(client.chat_stream([])) == "Hello."
    assert client.last_usage.reasoning_tokens == reasoning
    assert client.last_usage.total_tokens == 130
