"""Settings parsing for the external-MCP-server ``disabled_tools`` deny-list.

The old ``browser_perception`` settings block was removed — perception now
lives entirely in the browser plugin (``plugins/browser/aiko_browser``), so
only the generic MCP-server deny-list parsing remains here.
"""
from __future__ import annotations

import unittest

from app.core.infra.settings import _parse_external_mcp_server


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


if __name__ == "__main__":
    unittest.main()
