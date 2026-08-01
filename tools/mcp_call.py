"""Call a tool on Aiko's embedded MCP debug server from the shell.

The IDE's MCP client drops its connection whenever the container is
recreated and cannot be told to reconnect from here, so this is the
out-of-band way in during a rebuild loop.

    python tools/mcp_call.py get_idle_workers_status
    python tools/mcp_call.py probe_idle_worker_demand
    python tools/mcp_call.py send_message '{"text": "hey"}'
    python tools/mcp_call.py --list
"""
from __future__ import annotations

import asyncio
import json
import sys

from mcp import ClientSession
from mcp.client.sse import sse_client

URL = "http://localhost:6274/sse"


async def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    async with sse_client(URL, timeout=30) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if args[0] in ("--list", "-l"):
                tools = await session.list_tools()
                for tool in sorted(tools.tools, key=lambda t: t.name):
                    summary = (tool.description or "").strip().splitlines()
                    print(f"{tool.name}: {summary[0] if summary else ''}")
                return 0
            name = args[0]
            payload = json.loads(args[1]) if len(args) > 1 else {}
            result = await session.call_tool(name, payload)
            for block in result.content:
                print(getattr(block, "text", block))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
