"""The debug server must construct cleanly in a fresh Python process."""

import subprocess
import sys
import textwrap
from pathlib import Path


def test_fresh_server_resolves_settings_before_sources_inspect_lifespan():
    # Other MCP tests can complete the shared Settings model lazily, hiding
    # the first-construction warning. Exercise the real factory in isolation.
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import warnings

            warnings.filterwarnings(
                "error", message="Field 'lifespan' has an incomplete definition.*",
            )
            from app.mcp.server import create_mcp_server

            for _ in range(2):
                server = create_mcp_server(object(), host="127.0.0.1", port=16274)
                assert server.settings.host == "127.0.0.1"
                assert server.settings.port == 16274
                assert server.settings.lifespan is None
                assert server._tool_manager.get_tool("get_status") is not None
        """)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
