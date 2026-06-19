"""Tests for the tool-decision pass on
:class:`app.core.session.turn_runner.TurnRunner`.

Three contracts are pinned here:

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

3. **P14 heuristic gate** — pure-banter turns skip the decision pass
   entirely (``chat_with_tools`` never fires); tool-shaped turns,
   continuity signals (previous turn dispatched a tool, active tasks,
   force flag), and the ``agent.tool_pass_gate_enabled=false``
   kill-switch all run the pass exactly as before.
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


def _build_full_runner(
    *,
    tool_calls: list[Any] | None = None,
    gate_enabled: bool = True,
    tasks_active: bool | None = None,
) -> tuple[TurnRunner, MagicMock, MagicMock]:
    """Construct a TurnRunner whose ``run()`` works end-to-end.

    Unlike :func:`_build_runner` (which only exercises
    ``_maybe_run_tool_pass`` directly), this stubs the streaming pass
    and the prompt assembler too so the P14 gate path inside
    ``_run_inner`` is covered. The stub registry exposes ``names()``
    + ``__len__`` (both consulted by the gate) and one real tool,
    ``get_time``.
    """
    from app.core.session.prompt_assembler import PromptTelemetry

    ollama = MagicMock()
    response = MagicMock()
    response.content = ""
    response.tool_calls = tool_calls or []
    ollama.chat_with_tools = MagicMock(return_value=response)
    # Fresh iterator per call so multi-turn tests don't share a stream.
    ollama.chat_stream = MagicMock(
        side_effect=lambda *a, **k: iter(["[[reaction:neutral]] hi there."]),
    )
    ollama.last_usage = OllamaUsage()

    db = MagicMock()
    db.add_message = MagicMock(return_value=1)

    prompt = MagicMock()
    prompt.assemble_with_budget = MagicMock(
        side_effect=lambda *a, **k: ([], PromptTelemetry()),
    )

    registry = MagicMock()
    registry.__len__ = MagicMock(return_value=1)
    registry.names = MagicMock(return_value=["get_time"])
    registry.to_ollama_tools = MagicMock(
        return_value=[{
            "type": "function",
            "function": {
                "name": "get_time",
                "description": "Current time.",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    )
    registry.dispatch = MagicMock(
        return_value=types.SimpleNamespace(
            name="get_time", content="12:00", ok=True,
        ),
    )

    runner = TurnRunner(
        ollama=ollama,
        db=db,
        prompt_assembler=prompt,
        model="test-model",
        context_window=8192,
        max_tokens=512,
        temperature=0.7,
        filler_enabled=False,
        tool_registry=registry,
        tool_pass_gate_enabled=gate_enabled,
        tasks_active_provider=(
            (lambda: tasks_active) if tasks_active is not None else None
        ),
    )
    return runner, ollama, registry


class ToolPassGateTests(unittest.TestCase):
    """P14: the heuristic gate decides whether the decision pass runs."""

    def test_banter_skips_tool_pass_entirely(self) -> None:
        runner, ollama, _ = _build_full_runner()
        result = runner.run(
            session_key="default:main", user_text="hey, how are you?",
        )
        ollama.chat_with_tools.assert_not_called()
        # Stream still ran and produced the reply.
        ollama.chat_stream.assert_called_once()
        self.assertIn("hi there", result.text)
        assert result.telemetry is not None
        self.assertEqual(result.telemetry.tool_gate_event, "skip:no_signal")
        self.assertEqual(result.telemetry.tool_pass_ms, 0.0)

    def test_tool_shaped_text_runs_pass(self) -> None:
        runner, ollama, _ = _build_full_runner()
        result = runner.run(
            session_key="default:main", user_text="what time is it?",
        )
        ollama.chat_with_tools.assert_called()
        assert result.telemetry is not None
        self.assertEqual(
            result.telemetry.tool_gate_event, "run:signal_time",
        )

    def test_kill_switch_restores_always_run(self) -> None:
        runner, ollama, _ = _build_full_runner(gate_enabled=False)
        result = runner.run(
            session_key="default:main", user_text="hey, how are you?",
        )
        ollama.chat_with_tools.assert_called()
        assert result.telemetry is not None
        self.assertEqual(result.telemetry.tool_gate_event, "run:disabled")

    def test_tasks_active_runs_pass_on_banter(self) -> None:
        runner, ollama, _ = _build_full_runner(tasks_active=True)
        result = runner.run(
            session_key="default:main", user_text="the second one",
        )
        ollama.chat_with_tools.assert_called()
        assert result.telemetry is not None
        self.assertEqual(
            result.telemetry.tool_gate_event, "run:tasks_active",
        )

    def test_force_flag_is_one_shot(self) -> None:
        runner, ollama, _ = _build_full_runner()
        runner._tool_gate_force_next = True
        result = runner.run(
            session_key="default:main", user_text="hey!",
        )
        ollama.chat_with_tools.assert_called()
        assert result.telemetry is not None
        self.assertEqual(result.telemetry.tool_gate_event, "run:force")
        self.assertFalse(runner._tool_gate_force_next)
        # Second banter turn: flag consumed -> back to skipping.
        ollama.chat_with_tools.reset_mock()
        result2 = runner.run(
            session_key="default:main", user_text="hey again!",
        )
        ollama.chat_with_tools.assert_not_called()
        assert result2.telemetry is not None
        self.assertEqual(
            result2.telemetry.tool_gate_event, "skip:no_signal",
        )

    def test_last_turn_tool_continuity(self) -> None:
        # Turn 1 dispatches a real tool -> turn 2's banter follow-up
        # still runs the pass via the continuity flag. Turn 2 picks
        # the escape tool (no dispatch) -> turn 3 banter skips again.
        runner, ollama, _ = _build_full_runner(
            tool_calls=[_tool_call("get_time", call_id="c1")],
        )
        runner.run(session_key="default:main", user_text="what time is it?")
        self.assertTrue(runner._last_turn_dispatched_tool)

        # Turn 2: model now picks only the escape tool.
        escape_response = MagicMock()
        escape_response.content = ""
        escape_response.tool_calls = [
            _tool_call(_RESPOND_DIRECTLY_TOOL, call_id="c2"),
        ]
        ollama.chat_with_tools = MagicMock(return_value=escape_response)
        result2 = runner.run(
            session_key="default:main", user_text="and the other one?",
        )
        ollama.chat_with_tools.assert_called()
        assert result2.telemetry is not None
        self.assertEqual(
            result2.telemetry.tool_gate_event, "run:last_turn_tool",
        )
        # Escape-only pick clears the continuity flag...
        self.assertFalse(runner._last_turn_dispatched_tool)

        # ...so turn 3's banter skips.
        ollama.chat_with_tools.reset_mock()
        result3 = runner.run(
            session_key="default:main", user_text="haha nice",
        )
        ollama.chat_with_tools.assert_not_called()
        assert result3.telemetry is not None
        self.assertEqual(
            result3.telemetry.tool_gate_event, "skip:no_signal",
        )

    def test_gate_skip_clears_continuity_flag(self) -> None:
        runner, _, _ = _build_full_runner()
        runner._last_turn_dispatched_tool = False
        runner.run(session_key="default:main", user_text="hello!")
        self.assertFalse(runner._last_turn_dispatched_tool)

    def test_gate_state_snapshot_counts(self) -> None:
        runner, _, _ = _build_full_runner()
        runner.run(session_key="default:main", user_text="hey!")
        runner.run(session_key="default:main", user_text="what time is it?")
        state = runner.get_tool_gate_state()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["turns_gated"], 2)
        self.assertEqual(state["passes_skipped"], 1)
        self.assertEqual(state["passes_run"], 1)
        self.assertEqual(state["last_decision"]["reason"], "signal_time")
        self.assertGreaterEqual(state["avg_pass_ms"], 0.0)

    def test_turn_done_log_includes_gate_fields(self) -> None:
        runner, _, _ = _build_full_runner()
        import logging  # noqa: F401  (assertLogs needs the logger name only)
        with self.assertLogs("app.turn_runner", level="INFO") as cm:
            runner.run(session_key="default:main", user_text="hey!")
        haystack = "\n".join(cm.output)
        self.assertIn("tool_gate=skip:no_signal", haystack)
        self.assertIn("tool_pass_ms=", haystack)


class SkillRouterPassTests(unittest.TestCase):
    """Brain-lane progressive disclosure threads an allow-set into the
    tool pass and never strips every tool."""

    def test_allow_forwarded_to_registry(self) -> None:
        runner, _ollama, registry = _build_runner()
        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "what files can you see?"}],
            stop_requested=None,
            allow={"list_file_roots"},
        )
        # The registry is asked for the narrowed subset.
        self.assertEqual(
            registry.to_ollama_tools.call_args.kwargs.get("allow"),
            {"list_file_roots"},
        )

    def test_empty_filter_falls_back_to_full_set(self) -> None:
        runner, ollama, registry = _build_runner()
        real_tool = {
            "type": "function",
            "function": {
                "name": "list_file_roots",
                "description": "List configured file roots.",
                "parameters": {"type": "object", "properties": {}},
            },
        }

        def _to(allow=None):
            # Narrowed subset is empty; full set has the real tool.
            return [] if allow is not None else [real_tool]

        registry.to_ollama_tools = MagicMock(side_effect=_to)
        runner._maybe_run_tool_pass(
            [{"role": "user", "content": "hi"}],
            stop_requested=None,
            allow={"nonexistent"},
            max_rounds=1,
        )
        # Safety fallback: the pass still ran with the full toolset.
        ollama.chat_with_tools.assert_called_once()
        names = [
            t["function"]["name"]
            for t in ollama.chat_with_tools.call_args.kwargs["tools"]
        ]
        self.assertIn("list_file_roots", names)

    def test_router_off_sends_allow_none(self) -> None:
        # Default runner has the router disabled -> run() passes allow=None.
        runner, _ollama, registry = _build_full_runner()
        runner.run(session_key="default:main", user_text="what time is it?")
        self.assertIsNone(
            registry.to_ollama_tools.call_args.kwargs.get("allow"),
        )


class SkillRouterNarrowingTests(unittest.TestCase):
    """End-to-end: with the router on, a tool-shaped turn exposes only the
    matched family + always-on core (time/recall/world)."""

    def _build(self) -> tuple[TurnRunner, MagicMock, MagicMock]:
        from app.core.session.prompt_assembler import PromptTelemetry

        ollama = MagicMock()
        response = MagicMock()
        response.content = ""
        response.tool_calls = []
        ollama.chat_with_tools = MagicMock(return_value=response)
        ollama.chat_stream = MagicMock(
            side_effect=lambda *a, **k: iter(["[[reaction:neutral]] hi."]),
        )
        ollama.last_usage = OllamaUsage()

        db = MagicMock()
        db.add_message = MagicMock(return_value=1)
        prompt = MagicMock()
        prompt.assemble_with_budget = MagicMock(
            side_effect=lambda *a, **k: ([], PromptTelemetry()),
        )

        schemas = {
            name: {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in (
                "get_time", "recall", "list_file_roots",
                "add_goal", "consume_item",
            )
        }
        registry = MagicMock()
        registry.__len__ = MagicMock(return_value=len(schemas))
        registry.names = MagicMock(return_value=sorted(schemas))

        def _to(allow=None):
            names = sorted(schemas)
            if allow is not None:
                names = [n for n in names if n in allow]
            return [schemas[n] for n in names]

        registry.to_ollama_tools = MagicMock(side_effect=_to)
        registry.dispatch = MagicMock(
            return_value=types.SimpleNamespace(
                name="get_time", content="12:00", ok=True,
            ),
        )

        runner = TurnRunner(
            ollama=ollama,
            db=db,
            prompt_assembler=prompt,
            model="test-model",
            context_window=8192,
            max_tokens=512,
            temperature=0.7,
            filler_enabled=False,
            tool_registry=registry,
            skill_router_enabled=True,
        )
        return runner, ollama, registry

    def test_time_turn_exposes_core_only(self) -> None:
        runner, ollama, _ = self._build()
        runner.run(session_key="default:main", user_text="what time is it?")
        names = [
            t["function"]["name"]
            for t in ollama.chat_with_tools.call_args.kwargs["tools"]
        ]
        # Matched family (time) + core (recall, world=consume_item).
        self.assertIn("get_time", names)
        self.assertIn("recall", names)
        self.assertIn("consume_item", names)  # world is always-on core
        # Irrelevant families dropped.
        self.assertNotIn("list_file_roots", names)
        self.assertNotIn("add_goal", names)
        # Escape tool always appended.
        self.assertIn(_RESPOND_DIRECTLY_TOOL, names)

    def test_gate_state_reports_router(self) -> None:
        runner, _ollama, _ = self._build()
        runner.run(session_key="default:main", user_text="what time is it?")
        state = runner.get_tool_gate_state()
        self.assertTrue(state["router_enabled"])
        self.assertEqual(state["core_skills"], ["recall", "time", "world"])
        self.assertIsNotNone(state["last_active_tools"])
        self.assertIn("consume_item", state["last_active_tools"])


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
                    "using the result below. Do NOT start the task again; "
                    "the answer is right here.\n  file read: notes.md:\n    hi"
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

    def test_persona_explanation_of_cue_does_not_relax(self) -> None:
        """The persona TEACHES Aiko what the finished-task cue looks like,
        quoting "...reply now using the result below". That explanation is
        in the system prompt every turn — it must NOT trip the detector and
        relax forced-choice (the bug that made her narrate "doing it now"
        instead of calling start_workflow)."""
        runner, ollama, _ = _build_runner()
        messages = [
            {
                "role": "system",
                "content": (
                    "You are Aiko.\n- Never start the same task twice. If a "
                    "finished task's result is sitting in your context "
                    '("you just finished what the user asked for -- reply '
                    'now using the result below"), that IS the answer.'
                ),
            },
            {"role": "user", "content": "create a file and write a note in it"},
        ]
        runner._maybe_run_tool_pass(messages, stop_requested=None)
        self.assertEqual(
            ollama.chat_with_tools.call_args.kwargs["tool_choice"], "required",
        )


if __name__ == "__main__":
    unittest.main()
