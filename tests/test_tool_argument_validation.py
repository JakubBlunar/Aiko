"""Invalid model calls must be repairable without executing their tool."""

from unittest.mock import MagicMock

import pytest

from app.llm.chat_client import ChatResponse, ChatToolCall
from app.llm.ollama_client import OllamaClient
from app.llm.openai_compatible_client import _parse_openai_tool_calls, _parse_responses_output
from app.llm.tool_arguments import parse_tool_arguments
from app.llm.tools.base import ToolRegistry, ToolSchema
from tests.test_turn_runner_tool_pass import _build_runner


def _registry(parameters):
    tool = MagicMock()
    tool.schema.return_value = ToolSchema("synthetic", "Synthetic test tool.", parameters)
    tool.run.return_value = "done"
    registry = ToolRegistry()
    registry.register(tool)
    return registry, tool


@pytest.mark.parametrize("arguments", [
    {}, {"count": "2"}, {"count": True}, {"count": 0}, {"count": 5},
    {"count": 2, "mode": "secret-value"}, {"count": 2, "unknown": "secret-value"},
    {"count": 2, "items": ["secret-value"]},
    {"count": 2, "items": [{"enabled": "true"}]},
])
def test_schema_rejects_before_execution_without_echoing_private_values(arguments):
    registry, tool = _registry({
        "type": "object", "required": ["count"],
        "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 4},
            "mode": {"enum": ["one", "two"]},
            "items": {"type": "array", "items": {
                "type": "object", "properties": {"enabled": {"type": "boolean"}},
            }},
        },
    })
    result = registry.dispatch("synthetic", arguments, call_id="original-id")
    assert not result.ok
    assert result.call_id == "original-id"
    assert "secret-value" not in result.content
    tool.run.assert_not_called()


def test_explicit_extensible_schema_still_accepts_plugin_payloads():
    registry, tool = _registry({
        "type": "object", "properties": {}, "additionalProperties": True,
    })
    assert registry.dispatch("synthetic", {"plugin_field": "value"}).ok
    tool.run.assert_called_once_with({"plugin_field": "value"})
    tool.schema.assert_called_once()


@pytest.mark.parametrize("raw", ["{", "[]", "null", "false", {1: "x"},
                                  {"n": float("nan")}, {"n": float("inf")}])
def test_invalid_json_cannot_turn_into_executable_empty_defaults(raw):
    arguments, error = parse_tool_arguments(raw)
    assert arguments == {}
    assert error


@pytest.mark.parametrize("provider", ["openai", "responses", "ollama"])
def test_each_parser_preserves_invalid_argument_signal_and_original_call_id(provider):
    if provider == "openai":
        calls = _parse_openai_tool_calls([{
            "id": "bad-id", "function": {"name": "synthetic", "arguments": "{"},
        }])
    elif provider == "responses":
        _, calls, _ = _parse_responses_output({"output": [{
            "type": "function_call", "name": "synthetic", "arguments": "{",
            "call_id": "bad-id",
        }]})
    else:
        calls = OllamaClient._parse_tool_calls([{
            "id": "bad-id", "function": {"name": "synthetic", "arguments": "{"},
        }])
    assert len(calls) == 1
    assert calls[0].argument_error
    # Native Ollama does not provide stable call IDs; the runner supplies one.
    if provider != "ollama":
        assert calls[0].call_id == "bad-id"


def test_model_can_repair_invalid_call_and_execute_no_argument_tool_once():
    runner, client, _ = _build_runner()
    registry, tool = _registry({"type": "object", "properties": {}})
    runner._tool_registry = registry
    client.tool_pass_round_limit = None
    client.chat_with_tools.side_effect = [
        ChatResponse("", [ChatToolCall("synthetic", {}, "bad", "Invalid JSON object.")]),
        ChatResponse("", [ChatToolCall("synthetic", {}, "repaired")]),
        ChatResponse(""),
    ]
    messages = [{"role": "user", "content": "Use the synthetic tool."}]
    runner._maybe_run_tool_pass(messages, stop_requested=None, max_rounds=3)
    tool.run.assert_called_once_with({})
    results = [m for m in messages if m["role"] == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in results] == [
        ("bad", "Invalid JSON object."), ("repaired", "done"),
    ]
