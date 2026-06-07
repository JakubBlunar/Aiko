"""Tests for the tool-decision pass on
:class:`app.core.session.turn_runner.TurnRunner`.

Two contracts are pinned here:

1. **Budget** — the tool-decision pass caps ``num_predict`` at
   ``min(max_tokens, 512)`` and tags the call ``surface="tool_pass"``.

2. **Forced-choice + escape tool** — chatty models (gpt-5-mini at any
   ``reasoning_effort``) lose the implicit "text vs tool" coin-flip on
   ``tool_choice="auto"`` and narrate their intent instead of emitting
   the call. The pass forces ``tool_choice="required"`` and injects a
   synthetic ``respond_directly`` escape tool so the decision becomes
   "which tool" rather than "text vs tool". When the model picks
   ``respond_directly`` (the "no tool needed" choice) nothing is
   dispatched and no dangling tool-call message is left on ``messages``;
   when it picks a real tool, the escape tool is filtered out and only
   the real call is dispatched.
"""
from __future__ import annotations

import types
import unittest
from typing import Any
from unittest.mock import MagicMock

from app.core.session.turn_runner import (
    TurnRunner,
    _RESPOND_DIRECTLY_TOOL,
)
from app.llm.ollama_client import OllamaUsage


def _tool_call(name: str, arguments: dict[str, Any] | None = None,
               call_id: str = "") -> types.SimpleNamespace:
    """Build a duck-typed tool-call (``.name`` / ``.arguments`` /
    ``.call_id``) matching what the chat clients return."""
    return types.SimpleNamespace(
        name=name, arguments=arguments or {}, call_id=call_id,
    )


def _build_runner(
    *,
    max_tokens: int = 512,
    tool_calls: list[Any] | None = None,
) -> tuple[TurnRunner, MagicMock, MagicMock]:
    """Construct a TurnRunner with a stub chat client + tool registry.

    ``tool_calls`` is the list the stub ``chat_with_tools`` returns
    (defaults to empty -> the escape path). Returns the runner, the
    chat-client mock, and the registry mock.
    """
    ollama = MagicMock()
    response = MagicMock()
    response.content = ""
    response.tool_calls = tool_calls or []
    ollama.chat_with_tools = MagicMock(return_value=response)
    ollama.last_usage = OllamaUsage()

    registry = MagicMock()
    registry.to_ollama_tools = MagicMock(
        return_value=[{
            "type": "function",
            "function": {
                "name": "list_file_roots",
                "description": "List configured file roots.",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    )
    registry.dispatch = MagicMock(
        return_value=types.SimpleNamespace(
            name="list_file_roots", content="{}", ok=True,
        ),
    )

    runner = TurnRunner(
        ollama=ollama,
        db=MagicMock(),
        prompt_assembler=MagicMock(),
        model="gpt-5-mini",
        context_window=8192,
        max_tokens=max_tokens,
        temperature=0.7,
        filler_enabled=False,
        tool_registry=registry,
    )
    return runner, ollama, registry


class ToolPassBudgetTests(unittest.TestCase):
    def test_tool_pass_num_predict_capped_at_512(self) -> None:
        # max_tokens=512 -> min(512, 512) == 512.
        runner, ollama, _ = _build_runner(max_tokens=512)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "what files can you see?"},
        ]
        runner._maybe_run_tool_pass(messages, stop_requested=None)
        ollama.chat_with_tools.assert_called_once()
        options = ollama.chat_with_tools.call_args.kwargs["options"]
        self.assertEqual(options["num_predict"], 512)
        # surface tag must mark this as the (benign-truncation) tool pass.
        self.assertEqual(
            ollama.chat_with_tools.call_args.kwargs["surface"],
            "tool_pass",
        )

    def test_tool_pass_num_predict_respects_smaller_max_tokens(self) -> None:
        # A deployment that configured a tiny max_tokens still caps the
        # tool pass at that smaller ceiling (min(max_tokens, 512)).
        runner, ollama, _ = _build_runner(max_tokens=300)
        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "hi"}], stop_requested=None,
        )
        options = ollama.chat_with_tools.call_args.kwargs["options"]
        self.assertEqual(options["num_predict"], 300)


class ForcedChoiceEscapeToolTests(unittest.TestCase):
    def test_forces_tool_choice_required(self) -> None:
        runner, ollama, _ = _build_runner()
        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "hi"}], stop_requested=None,
        )
        self.assertEqual(
            ollama.chat_with_tools.call_args.kwargs["tool_choice"],
            "required",
        )

    def test_injects_respond_directly_escape_tool(self) -> None:
        runner, ollama, _ = _build_runner()
        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "hi"}], stop_requested=None,
        )
        tools = ollama.chat_with_tools.call_args.kwargs["tools"]
        names = [t["function"]["name"] for t in tools]
        # Real tool + the synthetic escape tool, escape tool last.
        self.assertIn("list_file_roots", names)
        self.assertIn(_RESPOND_DIRECTLY_TOOL, names)
        self.assertEqual(names[-1], _RESPOND_DIRECTLY_TOOL)

    def test_escape_only_pick_appends_nothing_and_skips_dispatch(self) -> None:
        # Model picks ONLY respond_directly -> "just answer". No tool is
        # dispatched and no tool-call message is left dangling on messages
        # (a dangling tool_call would 400 the streaming pass).
        runner, _ollama, registry = _build_runner(
            tool_calls=[_tool_call(_RESPOND_DIRECTLY_TOOL, call_id="c0")],
        )
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "hey, how are you?"},
        ]
        runner._maybe_run_tool_pass(messages, stop_requested=None)
        registry.dispatch.assert_not_called()
        self.assertEqual(len(messages), 1)  # untouched

    def test_real_pick_dispatches_and_filters_escape(self) -> None:
        # Model picks a real tool (and may also include the escape tool):
        # only the real tool is dispatched, and messages gain a clean
        # assistant tool_calls msg + matching tool result.
        runner, _ollama, registry = _build_runner(
            tool_calls=[
                _tool_call("list_file_roots", call_id="c1"),
                _tool_call(_RESPOND_DIRECTLY_TOOL, call_id="c2"),
            ],
        )
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "what files can you see?"},
        ]
        # max_rounds=1 so the stub (which always returns the same call)
        # doesn't re-dispatch on a second round.
        runner._maybe_run_tool_pass(
            messages, stop_requested=None, max_rounds=1,
        )
        registry.dispatch.assert_called_once()
        self.assertEqual(
            registry.dispatch.call_args.args[0], "list_file_roots",
        )
        # assistant tool_calls msg must carry ONLY the real call.
        assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
        self.assertEqual(len(assistant_msgs), 1)
        call_names = [
            tc["function"]["name"]
            for tc in assistant_msgs[0]["tool_calls"]
        ]
        self.assertEqual(call_names, ["list_file_roots"])
        # one tool result message, linked by tool_call_id.
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["name"], "list_file_roots")


class FinishedTaskRelaxesForcedChoiceTests(unittest.TestCase):
    """When a finished-task result is already in the system prompt the
    pass relaxes ``tool_choice`` to "auto" so the model narrates the
    result instead of being forced to re-run the task it finished."""

    def test_reply_block_relaxes_to_auto(self) -> None:
        runner, ollama, _ = _build_runner()
        messages = [
            {
                "role": "system",
                "content": (
                    "You just finished what the user asked for — reply now "
                    "using the result below.\n  file read: notes.md:\n    hi"
                ),
            },
            {"role": "user", "content": "what did you find?"},
        ]
        runner._maybe_run_tool_pass(messages, stop_requested=None)
        self.assertEqual(
            ollama.chat_with_tools.call_args.kwargs["tool_choice"], "auto",
        )

    def test_success_cue_header_relaxes_to_auto(self) -> None:
        runner, ollama, _ = _build_runner()
        messages = [
            {
                "role": "system",
                "content": "Tasks that finished since your last message:\n- A — ok",
            },
            {"role": "user", "content": "anything?"},
        ]
        runner._maybe_run_tool_pass(messages, stop_requested=None)
        self.assertEqual(
            ollama.chat_with_tools.call_args.kwargs["tool_choice"], "auto",
        )

    def test_no_task_block_still_forces_required(self) -> None:
        runner, ollama, _ = _build_runner()
        messages = [
            {"role": "system", "content": "You are Aiko."},
            {"role": "user", "content": "what files can you see?"},
        ]
        runner._maybe_run_tool_pass(messages, stop_requested=None)
        self.assertEqual(
            ollama.chat_with_tools.call_args.kwargs["tool_choice"], "required",
        )


if __name__ == "__main__":
    unittest.main()
