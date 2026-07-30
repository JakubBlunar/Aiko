"""DT1 MCP surface -- the tools that drive the virtual clock.

Uses the lightweight `_FakeMCP` registry pattern (as in
`test_concept_trace.py`) rather than standing up a real FastMCP server:
these tools are thin wrappers, so what matters is that they find the
clock on the session, return parseable JSON, and stay inert when the
env gate is off.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from app.core.infra import timephrase as tp
from app.core.infra.debug_clock import ENV_FLAG, DebugClock
from app.core.infra.engagement_clock import EngagementClock
from app.mcp.server_tools import core_tools, debug_clock_tools


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _engagement() -> EngagementClock:
    store: dict[str, str] = {}
    return EngagementClock(
        kv_get=store.get,
        kv_set=lambda k, v: store.__setitem__(k, v),
        settings=SimpleNamespace(engagement_seconds_per_day=3600.0),
    )


def _tools(clock) -> dict:
    mcp = _FakeMCP()
    debug_clock_tools.register(mcp, SimpleNamespace(_debug_clock=clock))
    return mcp.tools


class ToolRegistrationTests(unittest.TestCase):
    def test_all_five_tools_register(self) -> None:
        tools = _tools(DebugClock(enabled=False))
        self.assertEqual(
            set(tools),
            {
                "get_clock_status",
                "advance_clock",
                "set_clock",
                "advance_engagement",
                "reset_clock",
            },
        )

    def test_every_tool_documents_itself(self) -> None:
        for name, fn in _tools(DebugClock(enabled=False)).items():
            self.assertTrue((fn.__doc__ or "").strip(), msg=name)


class DisabledGateTests(unittest.TestCase):
    """With the env flag off the tools must explain, not fail."""

    def setUp(self) -> None:
        self.tools = _tools(DebugClock(enabled=False))

    def test_mutators_report_the_flag(self) -> None:
        for call in (
            lambda: self.tools["advance_clock"](days=5),
            lambda: self.tools["set_clock"]("2030-01-01T00:00:00Z"),
            lambda: self.tools["advance_engagement"](5),
            lambda: self.tools["reset_clock"](),
        ):
            payload = json.loads(call())
            self.assertFalse(payload["ok"])
            self.assertIn(ENV_FLAG, payload["error"])

    def test_status_still_readable(self) -> None:
        payload = json.loads(self.tools["get_clock_status"]())
        self.assertFalse(payload["enabled"])
        self.assertFalse(payload["active"])


class MissingClockTests(unittest.TestCase):
    """A session whose clock failed to init must not explode."""

    def test_tools_report_absence(self) -> None:
        tools = _tools(None)
        for name in tools:
            result = tools[name]() if name in (
                "get_clock_status", "reset_clock",
            ) else None
            if result is not None:
                self.assertIn("unavailable", result)
        self.assertIn("unavailable", tools["advance_clock"](days=1))
        self.assertIn("unavailable", tools["set_clock"]("2030-01-01T00:00:00Z"))
        self.assertIn("unavailable", tools["advance_engagement"](1))


class EnabledToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = DebugClock(
            enabled=True, engagement_clock=_engagement(),
        )
        self.clock.install()
        self.tools = _tools(self.clock)

    def tearDown(self) -> None:
        self.clock.uninstall()
        tp.set_now_provider(None)

    def test_advance_shifts_and_reports(self) -> None:
        payload = json.loads(self.tools["advance_clock"](days=14))
        self.assertTrue(payload["active"])
        self.assertEqual(payload["offset"], "+14d")
        self.assertAlmostEqual(
            (tp.now() - tp.real_now()).total_seconds(), 14 * 86400, delta=5,
        )

    def test_set_clock_jumps(self) -> None:
        payload = json.loads(self.tools["set_clock"]("2031-06-01T12:00:00Z"))
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["virtual_now"].startswith("2031-06-01"))

    def test_set_clock_rejects_garbage(self) -> None:
        payload = json.loads(self.tools["set_clock"]("tuesday-ish"))
        self.assertFalse(payload["ok"])

    def test_advance_engagement_is_a_separate_lever(self) -> None:
        self.tools["advance_clock"](days=30)
        after_wall = json.loads(self.tools["get_clock_status"]())
        self.assertEqual(after_wall["engagement"]["total_units"], 0.0)

        payload = json.loads(self.tools["advance_engagement"](30))
        self.assertAlmostEqual(
            payload["engagement"]["total_units"], 30 * 3600.0, delta=1,
        )

    def test_reset_clears_both_levers(self) -> None:
        self.tools["advance_clock"](days=10)
        self.tools["advance_engagement"](10)
        payload = json.loads(self.tools["reset_clock"]())
        self.assertFalse(payload["active"])
        self.assertEqual(payload["engagement"]["total_units"], 0.0)

    def test_status_is_json(self) -> None:
        self.tools["advance_clock"](hours=6)
        payload = json.loads(self.tools["get_clock_status"]())
        self.assertIn("real_now", payload)
        self.assertIn("virtual_now", payload)
        self.assertNotEqual(payload["real_now"], payload["virtual_now"])


class GetStatusExposureTests(unittest.TestCase):
    """A live offset must be visible from the first tool anyone calls."""

    def tearDown(self) -> None:
        tp.set_now_provider(None)

    def _status(self, clock) -> dict:
        mcp = _FakeMCP()
        session = SimpleNamespace(
            _debug_clock=clock,
            effective_chat_model="m",
            context_window_size=1,
            tts_provider="p",
            tts_voice="v",
            _settings=SimpleNamespace(tts=SimpleNamespace(enabled=False)),
            session_key="s",
            get_last_metrics=lambda: {},
        )
        core_tools.register(mcp, session)
        return json.loads(mcp.tools["get_status"]())

    def test_absent_on_real_time(self) -> None:
        self.assertNotIn("debug_clock", self._status(DebugClock(enabled=True)))

    def test_absent_when_gate_is_off(self) -> None:
        clock = DebugClock(enabled=False)
        clock.advance(days=5)
        self.assertNotIn("debug_clock", self._status(clock))

    def test_present_once_shifted(self) -> None:
        clock = DebugClock(enabled=True)
        clock.advance(days=5)
        status = self._status(clock)
        self.assertIn("debug_clock", status)
        self.assertEqual(status["debug_clock"]["offset"], "+5d")

    def test_missing_clock_is_tolerated(self) -> None:
        self.assertNotIn("debug_clock", self._status(None))


if __name__ == "__main__":
    unittest.main()
