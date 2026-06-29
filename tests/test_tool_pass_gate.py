"""Tests for the P14 heuristic tool-pass gate
(:mod:`app.core.session.tool_pass_gate`).

Contracts pinned here:

1. **Conservative bias** — every continuity signal (force flag,
   finished-task block, active tasks, previous turn dispatched a tool)
   runs the pass regardless of the user text, and any registered tool
   without a pattern family degrades to always-run.
2. **Per-family signals** — each live tool family's patterns fire on a
   representative tool-shaped message, and only families with
   registered tools are consulted (disabling ``tools.world`` in config
   deactivates the room patterns).
3. **Skip path** — pure banter with no continuity signal skips, with
   ``reason="no_signal"``.
"""
from __future__ import annotations

import unittest

from app.core.session.tool_pass_gate import (
    BRAIN_CORE_FAMILIES,
    GateContext,
    GateDecision,
    families_for_tools,
    select_active_tool_names,
    should_run_tool_pass,
)


_ALL_TOOLS = [
    "get_time", "recall", "web_search",
    "list_file_roots", "start_file_read", "start_file_search",
    "cancel_file_task", "answer_file_task",
    "look_around", "move_to", "change_posture", "inspect_item",
    "consume_item", "water_plant", "plant_seed", "harvest_plant",
    "add_goal", "update_goal_progress", "archive_goal", "list_goals",
    "start_workflow", "check_my_work", "cancel_work",
    "get_weather", "get_forecast",
]

_NO_CONTEXT = GateContext()


def _decide(text: str, tools: list[str] | None = None,
            context: GateContext = _NO_CONTEXT) -> GateDecision:
    return should_run_tool_pass(
        text, tools if tools is not None else _ALL_TOOLS, context=context,
    )


class ContinuityRuleTests(unittest.TestCase):
    """Continuity signals beat the text heuristic in priority order."""

    def test_force_flag_runs_on_pure_banter(self) -> None:
        decision = _decide(
            "hey, how are you?", context=GateContext(force=True),
        )
        self.assertTrue(decision.run)
        self.assertEqual(decision.reason, "force")

    def test_finished_task_block_runs(self) -> None:
        decision = _decide(
            "nice!", context=GateContext(finished_task_block=True),
        )
        self.assertTrue(decision.run)
        self.assertEqual(decision.reason, "finished_task")

    def test_tasks_active_runs(self) -> None:
        decision = _decide(
            "the second one", context=GateContext(tasks_active=True),
        )
        self.assertTrue(decision.run)
        self.assertEqual(decision.reason, "tasks_active")

    def test_last_turn_tool_runs(self) -> None:
        # Follow-up with no tool-shaped token of its own.
        decision = _decide(
            "and the other one?",
            context=GateContext(last_turn_dispatched_tool=True),
        )
        self.assertTrue(decision.run)
        self.assertEqual(decision.reason, "last_turn_tool")

    def test_force_beats_finished_task_in_reason(self) -> None:
        decision = _decide(
            "hi",
            context=GateContext(force=True, finished_task_block=True),
        )
        self.assertEqual(decision.reason, "force")


class UnknownToolTests(unittest.TestCase):
    def test_unknown_registered_tool_always_runs(self) -> None:
        # A future tool with no pattern family must degrade to the
        # status quo (always run) instead of silently never being
        # callable.
        decision = _decide(
            "hey, how are you?", tools=["get_time", "brand_new_tool"],
        )
        self.assertTrue(decision.run)
        self.assertEqual(decision.reason, "unknown_tool")
        self.assertIn("brand_new_tool", decision.matched)

    def test_families_for_tools_splits_known_and_unknown(self) -> None:
        families, unknown = families_for_tools(
            ["get_time", "web_search", "mystery"],
        )
        self.assertEqual(families, {"time", "web"})
        self.assertEqual(unknown, {"mystery"})


class SignalFamilyTests(unittest.TestCase):
    """One representative phrase per family fires its patterns."""

    def _assert_runs(self, text: str, family: str) -> None:
        decision = _decide(text)
        self.assertTrue(decision.run, f"{text!r} should run the pass")
        self.assertIn(family, decision.matched)

    def test_time_signal(self) -> None:
        self._assert_runs("what time is it over there?", "time")

    def test_time_date_signal(self) -> None:
        self._assert_runs("do you know what day it is?", "time")

    def test_web_signal(self) -> None:
        self._assert_runs("can you search for the latest python release?", "web")

    def test_weather_signal(self) -> None:
        # H11: weather/forecast route to the dedicated weather family,
        # not web (so the fast weather tool wins over a slow DDG round-trip).
        self._assert_runs("how's the weather tomorrow?", "weather")

    def test_forecast_signal(self) -> None:
        self._assert_runs("what's the forecast for the weekend?", "weather")

    def test_weather_phrase_skips_when_weather_tools_disabled(self) -> None:
        # With the weather tools removed, "weather" patterns aren't
        # consulted and the phrase no longer matches web either.
        decision = _decide(
            "is it going to rain later?", tools=["get_time", "web_search"],
        )
        self.assertFalse(decision.run)
        self.assertEqual(decision.reason, "no_signal")

    def test_recall_signal(self) -> None:
        self._assert_runs("do you remember what I told you about mika?", "recall")

    def test_recall_did_i_tell_signal(self) -> None:
        self._assert_runs("did I tell you about the interview?", "recall")

    def test_files_signal(self) -> None:
        self._assert_runs("what files can you see?", "files")

    def test_files_read_signal(self) -> None:
        self._assert_runs("read the notes from yesterday please", "files")

    def test_world_signal(self) -> None:
        self._assert_runs("go sit by the window", "world")

    def test_world_consume_signal(self) -> None:
        self._assert_runs("have a cookie!", "world")

    def test_goals_signal(self) -> None:
        self._assert_runs("how's the progress on your goals?", "goals")

    def test_tasks_signal(self) -> None:
        self._assert_runs("cancel that workflow", "tasks")

    def test_multiple_families_join_in_reason(self) -> None:
        decision = _decide("search the files for my notes")
        self.assertTrue(decision.run)
        # "search" -> web, "files"/"notes" -> files.
        self.assertIn("files", decision.matched)
        self.assertIn("web", decision.matched)
        self.assertTrue(decision.reason.startswith("signal_"))

    def test_generic_request_runs(self) -> None:
        decision = _decide("can you check whether that thing worked?")
        self.assertTrue(decision.run)
        # "check (the|if|whether|what)" lands either via the tasks
        # family or the generic fallback — both are acceptable run
        # verdicts; what matters is it runs.

    def test_show_me_generic_runs(self) -> None:
        decision = _decide("show me what you've got", tools=["get_time"])
        self.assertTrue(decision.run)
        self.assertEqual(decision.reason, "generic_request")


class FamilyGatingTests(unittest.TestCase):
    """Only families with registered tools are consulted."""

    def test_world_phrase_skips_when_world_tools_disabled(self) -> None:
        decision = _decide(
            "go sit by the window", tools=["get_time", "web_search"],
        )
        self.assertFalse(decision.run)
        self.assertEqual(decision.reason, "no_signal")

    def test_time_phrase_still_fires_with_only_get_time(self) -> None:
        decision = _decide("what time is it?", tools=["get_time"])
        self.assertTrue(decision.run)
        self.assertEqual(decision.matched, ("time",))


class SkipPathTests(unittest.TestCase):
    def test_pure_banter_skips(self) -> None:
        for text in (
            "hey, how are you?",
            "lol that's so true",
            "I had a rough morning honestly",
            "you're sweet",
            "good night aiko",
        ):
            decision = _decide(text)
            self.assertFalse(decision.run, f"{text!r} should skip")
            self.assertEqual(decision.reason, "no_signal")

    def test_empty_text_skips(self) -> None:
        decision = _decide("   ")
        self.assertFalse(decision.run)
        self.assertEqual(decision.reason, "empty_text")

    def test_as_event_shapes(self) -> None:
        run = _decide("what time is it?")
        skip = _decide("hey!")
        self.assertEqual(run.as_event(), "run:signal_time")
        self.assertEqual(skip.as_event(), "skip:no_signal")


class SelectActiveToolNamesTests(unittest.TestCase):
    """Brain-lane progressive disclosure (the SkillRouter)."""

    def _decide(self, text: str, tools: list[str] | None = None) -> GateDecision:
        return should_run_tool_pass(
            text, tools if tools is not None else _ALL_TOOLS, context=_NO_CONTEXT,
        )

    def test_router_disabled_returns_none(self) -> None:
        decision = self._decide("what time is it?")
        self.assertIsNone(
            select_active_tool_names(
                decision, _ALL_TOOLS, router_enabled=False,
            )
        )

    def test_widen_reasons_return_none(self) -> None:
        # Every continuity / fallback reason widens to the full toolset.
        for ctx, _reason in (
            (GateContext(force=True), "force"),
            (GateContext(finished_task_block=True), "finished_task"),
            (GateContext(tasks_active=True), "tasks_active"),
            (GateContext(last_turn_dispatched_tool=True), "last_turn_tool"),
        ):
            decision = should_run_tool_pass("hi", _ALL_TOOLS, context=ctx)
            self.assertIsNone(
                select_active_tool_names(
                    decision, _ALL_TOOLS, router_enabled=True,
                ),
                f"reason={decision.reason} should widen",
            )

    def test_unknown_tool_widens(self) -> None:
        decision = should_run_tool_pass(
            "hey", ["get_time", "brand_new_tool"], context=_NO_CONTEXT,
        )
        self.assertEqual(decision.reason, "unknown_tool")
        self.assertIsNone(
            select_active_tool_names(
                decision, ["get_time", "brand_new_tool"], router_enabled=True,
            )
        )

    def test_generic_request_widens(self) -> None:
        decision = should_run_tool_pass(
            "show me what you've got", ["get_time"], context=_NO_CONTEXT,
        )
        self.assertEqual(decision.reason, "generic_request")
        self.assertIsNone(
            select_active_tool_names(
                decision, ["get_time"], router_enabled=True,
            )
        )

    def test_time_signal_narrows_to_core_only(self) -> None:
        decision = self._decide("what time is it?")
        allow = select_active_tool_names(
            decision, _ALL_TOOLS, router_enabled=True,
        )
        assert allow is not None
        # Core = time/recall/world. No files/goals/tasks/web tools.
        self.assertIn("get_time", allow)
        self.assertIn("recall", allow)
        self.assertIn("consume_item", allow)  # world is core
        self.assertNotIn("list_file_roots", allow)
        self.assertNotIn("add_goal", allow)
        self.assertNotIn("start_workflow", allow)

    def test_files_signal_includes_files_plus_core(self) -> None:
        decision = self._decide("read the notes from yesterday please")
        allow = select_active_tool_names(
            decision, _ALL_TOOLS, router_enabled=True,
        )
        assert allow is not None
        self.assertIn("start_file_read", allow)  # files family matched
        # World is always-on core even though this is a files turn.
        self.assertIn("consume_item", allow)
        self.assertIn("recall", allow)
        # Goals are not relevant -> excluded.
        self.assertNotIn("add_goal", allow)

    def test_world_always_present_via_core(self) -> None:
        # A goals-only turn still exposes world so Aiko can act in her room.
        decision = self._decide("how's the progress on your goals?")
        allow = select_active_tool_names(
            decision, _ALL_TOOLS, router_enabled=True,
        )
        assert allow is not None
        self.assertIn("look_around", allow)
        self.assertIn("add_goal", allow)  # matched family

    def test_custom_core_families_respected(self) -> None:
        decision = self._decide("what time is it?")
        allow = select_active_tool_names(
            decision, _ALL_TOOLS, core_families={"time"}, router_enabled=True,
        )
        assert allow is not None
        self.assertIn("get_time", allow)
        # world dropped from core -> not present on a time-only turn.
        self.assertNotIn("consume_item", allow)

    def test_default_core_is_time_recall_world(self) -> None:
        self.assertEqual(BRAIN_CORE_FAMILIES, frozenset({"time", "recall", "world"}))


if __name__ == "__main__":
    unittest.main()
