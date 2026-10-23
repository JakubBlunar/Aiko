"""Synthetic stream failures must not become completed companion turns."""

import json
import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

from app.core.infra.settings import load_settings
from app.llm.chat_client import ChatUsage
from app.llm.openai_compatible_client import OpenAICompatibleClient, _parse_responses_output
from app.llm.turn_outcome import ProviderOutcomeError
from tests.test_openai_compatible_client import _sse_response
from tests.test_turn_runner_mood_fallback import _build_runner


def _event(delta=None, *, finish=None, **fields):
    return "data: " + json.dumps({
        "choices": [{"delta": delta or {}, "finish_reason": finish}], **fields,
    }, ensure_ascii=False)


def _client():
    return OpenAICompatibleClient(
        load_settings().ollama, base_url="https://api.venice.ai/api/v1",
        model="deepseek-v4-1-flash-uncensored", reasoning_effort="low",
        api_style="chat_completions", api_key="synthetic-test-key",
    )


@pytest.mark.parametrize("body", [
    {"choices": [{"message": {"refusal": "Provider limitation"}, "finish_reason": "stop"}]},
    {"choices": [{"message": {"content": "partial"}, "finish_reason": "content_filter"}]},
    {"error": {"message": "Synthetic failure"}},
])
def test_explicit_nonstreaming_provider_outcomes_are_typed(body):
    with pytest.raises(ProviderOutcomeError):
        OpenAICompatibleClient._extract_choice(body)


@pytest.mark.parametrize("events, expected", [
    ([], "incomplete"),
    ([{"type": "response.refusal.delta", "delta": "Provider limitation"}], "provider_blocked"),
    ([{"type": "response.failed"}], "provider_error"),
    ([{"type": "response.incomplete", "response": {
        "status": "incomplete", "incomplete_details": {"reason": "content_filter"},
    }}], "provider_blocked"),
    ([{"type": "response.incomplete", "response": {
        "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
    }}], "budget_exhausted"),
])
def test_responses_failures_are_typed_and_clear_previous_usage(events, expected):
    client = _client()
    client._api_style = "responses"
    client.last_usage = ChatUsage(completion_tokens=999)
    with patch("app.llm.openai_compatible_client.requests.post", return_value=_sse_response([
        "data: " + json.dumps(event) for event in events
    ])):
        with pytest.raises(ProviderOutcomeError) as error:
            list(client.chat_stream([]))
    assert error.value.outcome == expected
    assert client.last_usage.completion_tokens == 0


@pytest.mark.parametrize("body", [
    {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
    {"status": "completed", "output": [{
        "type": "message", "content": [{"type": "refusal", "refusal": "Provider limitation"}],
    }]},
])
def test_nonstreaming_responses_refusals_remain_provider_outcomes(body):
    with pytest.raises(ProviderOutcomeError) as error:
        _parse_responses_output(body)
    assert error.value.outcome == "provider_blocked"


@pytest.mark.parametrize("tail", [
    ['data: {"error":{"message":"synthetic upstream error"}}', "data: [DONE]"],
    ['data: {"error":"synthetic upstream error"}', "data: [DONE]"],
    [],  # A socket closing without any completion signal is not success.
    [_event(finish="content_filter"), "data: [DONE]"],
    [_event({"refusal": "synthetic provider limitation"}), "data: [DONE]"],
])
def test_failed_stream_never_saves_assistant_or_learns_from_partial_reply(tail):
    client = _client()
    runner = _build_runner(stream_tokens=[])
    runner._ollama = client
    runner._post_turn = MagicMock()
    runner._memory_store = MagicMock()
    frames = [_event({"content": (
        "[[reaction:neutral]] An unfinished reply. "
        "[[remember:self:Synthetic provisional preference]]"
    )})] + tail
    with patch(
        "app.llm.openai_compatible_client.requests.post", return_value=_sse_response(frames),
    ):
        with pytest.raises(requests.RequestException):
            runner.run(session_key="synthetic:failure", user_text="hello")
    assert not any(
        call.kwargs.get("role") == "assistant" for call in runner._db.add_message.call_args_list
    )
    runner._post_turn.assert_not_called()
    runner._memory_store.add.assert_not_called()


@pytest.mark.parametrize("finish", ["length", "stop"])
def test_reasoning_only_completion_is_an_error_and_resets_old_usage(finish):
    client = _client()
    client.last_usage = ChatUsage(prompt_tokens=999, completion_tokens=999)
    frames = [
        _event({"reasoning_content": "synthetic private reasoning"}),
        _event(finish=finish, usage={"prompt_tokens": 10, "completion_tokens": 128}),
        "data: [DONE]",
    ]
    with patch(
        "app.llm.openai_compatible_client.requests.post", return_value=_sse_response(frames),
    ):
        with pytest.raises(requests.RequestException, match="visible"):
            list(client.chat_stream([{"role": "user", "content": "hello"}]))
    assert client.last_usage.prompt_tokens == 10


@pytest.mark.parametrize("raw, expected", [
    ("<think>private [[remember:self:synthetic thought]]</think>Hello.", "Hello."),
    ("Hi. <THINKING>private</THINKING> Bye.", "Hi.  Bye."),
    ("< reasoning >private< / reasoning >Hello.", "Hello."),
    ("Hello.<reflection>private unfinished trace", "Hello."),
    ("I think 2 < 3. <em>Fine.</em>", "I think 2 < 3. <em>Fine.</em>"),
])
def test_thinking_never_leaks_at_any_chunk_boundary(raw, expected):
    for tokens in ([raw[:i], raw[i:]] for i in range(len(raw) + 1)):
        frames = [_event({"content": text}) for text in tokens]
        frames += [_event(finish="stop"), "data: [DONE]"]
        client = _client()
        with patch(
            "app.llm.openai_compatible_client.requests.post", return_value=_sse_response(frames),
        ):
            assert "".join(client.chat_stream([])) == expected


def test_reasoning_field_and_inline_tags_cannot_write_self_memory():
    client = _client()
    runner = _build_runner(stream_tokens=[])
    runner._ollama = client
    runner._memory_store = MagicMock()
    raw = (
        "<think>[[remember:self:A synthetic private thought is not my preference]]</think>"
        "[[reaction:neutral]]Hello."
    )
    frames = [_event({"content": char, "reasoning_content": "private"}) for char in raw]
    frames += [_event(finish="stop"), "data: [DONE]"]
    with patch(
        "app.llm.openai_compatible_client.requests.post", return_value=_sse_response(frames),
    ):
        result = runner.run(session_key="synthetic:private-trace", user_text="hello")
    assert result.text == "Hello."
    assert "remember:self" not in result.raw_text
    runner._memory_store.add.assert_not_called()


@pytest.mark.parametrize("ending", [[_event(finish="stop")], ["data: [DONE]"]])
def test_either_terminal_marker_can_complete_a_visible_reply(ending):
    frames = [_event({"content": "Hello."})] + ending
    with patch(
        "app.llm.openai_compatible_client.requests.post", return_value=_sse_response(frames),
    ):
        assert "".join(_client().chat_stream([])) == "Hello."


def test_explicit_cancellation_does_not_require_completion_or_visible_text():
    stop = threading.Event()
    stop.set()
    with patch("app.llm.openai_compatible_client.requests.post", return_value=_sse_response([])):
        assert list(_client().chat_stream([], stop_event=stop)) == []


@pytest.mark.parametrize("encoding", [None, "ISO-8859-1"])
def test_sse_always_decodes_utf8_even_without_a_charset(encoding):
    response = requests.Response()
    response.status_code = 200
    response.encoding = encoding
    frames = [_event({"content": "Ahoj, \u017dofia. Caf\u00e9?"}), _event(finish="stop")]
    # Real servers can put literal Unicode in SSE, not just JSON escapes.
    response._content = "\n\n".join(frames).encode("utf-8")
    response._content_consumed = True
    with patch("app.llm.openai_compatible_client.requests.post", return_value=response):
        assert "".join(_client().chat_stream([])) == "Ahoj, \u017dofia. Caf\u00e9?"


def test_provider_error_payload_is_not_exposed_in_the_exception():
    frames = ['data: {"error":{"message":"SYNTHETIC_PRIVATE_REQUEST_TEXT"}}']
    with patch(
        "app.llm.openai_compatible_client.requests.post", return_value=_sse_response(frames),
    ):
        with pytest.raises(requests.RequestException) as error:
            list(_client().chat_stream([]))
    assert "SYNTHETIC_PRIVATE_REQUEST_TEXT" not in str(error.value)


def test_private_trace_is_discarded_without_waiting_to_stream_the_whole_answer():
    client = _client()
    frames = [
        _event({"content": "<think>private"}),
        _event({"content": "</think>[[reaction:neutral]]Hello."}),
        _event({"content": " More answer."}), _event(finish="stop"), "data: [DONE]",
    ]
    with patch(
        "app.llm.openai_compatible_client.requests.post", return_value=_sse_response(frames),
    ):
        stream = client.chat_stream([])
        assert next(stream) == "[[reaction:neutral]]Hello."
        assert next(stream) == " More answer."
        assert list(stream) == []
