"""Settings parsing for the deny-list + browser_perception block."""
from __future__ import annotations

import unittest

from app.core.infra.settings import (
    _parse_browser_perception,
    _parse_external_mcp_server,
)


class DisabledToolsTests(unittest.TestCase):
    def test_disabled_tools_parsed(self) -> None:
        server = _parse_external_mcp_server(
            {
                "id": "browser",
                "command": "npx",
                "args": ["-y", "real-browser-mcp"],
                "disabled_tools": ["browser_console", "browser_evaluate"],
            }
        )
        assert server is not None
        self.assertEqual(
            server.disabled_tools, ("browser_console", "browser_evaluate")
        )

    def test_disabled_tools_default_empty(self) -> None:
        server = _parse_external_mcp_server({"id": "x", "command": "npx"})
        assert server is not None
        self.assertEqual(server.disabled_tools, ())

    def test_disabled_tools_non_list_ignored(self) -> None:
        server = _parse_external_mcp_server(
            {"id": "x", "command": "npx", "disabled_tools": "nope"}
        )
        assert server is not None
        self.assertEqual(server.disabled_tools, ())


class BrowserPerceptionParseTests(unittest.TestCase):
    def test_defaults_when_empty(self) -> None:
        bp = _parse_browser_perception({})
        self.assertFalse(bp.enabled)
        self.assertEqual(bp.server_id, "browser")
        self.assertEqual(bp.snapshot_tools, ("browser_snapshot",))
        self.assertEqual(bp.adapter, "real_browser")

    def test_non_dict_returns_defaults(self) -> None:
        bp = _parse_browser_perception("nope")
        self.assertFalse(bp.enabled)

    def test_overrides_and_clamps(self) -> None:
        bp = _parse_browser_perception(
            {
                "enabled": True,
                "server_id": "chrome",
                "snapshot_tools": ["snap", "  "],
                "adapter": "generic",
                "max_ranked_elements": 0,
                "state_memory_pages": -5,
                "weight_role": -2.0,
            }
        )
        self.assertTrue(bp.enabled)
        self.assertEqual(bp.server_id, "chrome")
        self.assertEqual(bp.snapshot_tools, ("snap",))
        self.assertEqual(bp.adapter, "generic")
        self.assertEqual(bp.max_ranked_elements, 1)  # clamped to floor
        self.assertEqual(bp.state_memory_pages, 1)  # clamped to floor
        self.assertEqual(bp.weight_role, 0.0)  # clamped to >= 0

    def test_empty_snapshot_tools_falls_back(self) -> None:
        bp = _parse_browser_perception({"snapshot_tools": []})
        self.assertEqual(bp.snapshot_tools, ("browser_snapshot",))

    def test_blank_server_id_falls_back(self) -> None:
        bp = _parse_browser_perception({"server_id": "   "})
        self.assertEqual(bp.server_id, "browser")


if __name__ == "__main__":
    unittest.main()
