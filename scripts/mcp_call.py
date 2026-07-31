"""Call a tool on Aiko's embedded MCP debug server from a shell.

Exists for the containerised loop. An MCP client inside an editor is the nicer
way to drive a *local* app, but it caches a failed connection from whenever the
port was last dead, and it can't be pointed at a container mid-session. This
talks to the SSE endpoint directly, so it works the moment the port answers.

Against Docker, publish the port first (the default bind is loopback *inside*
the container, which is unreachable from the host)::

    docker compose -f docker-compose-slim.yaml -f docker-compose.debug.yaml up -d

Then::

    python scripts/mcp_call.py --list
    python scripts/mcp_call.py get_status
    python scripts/mcp_call.py send_message --arg message="hey, you awake?"
    python scripts/mcp_call.py force_day_color --json '{"color": "amber"}'

``--timeout`` defaults to 180 s because ``send_message`` runs a full turn: two
LLM passes plus the post-turn inner life, which on a 9B model over CPU is
comfortably slower than any library default.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import timedelta
from typing import Any

DEFAULT_URL = "http://localhost:6274/sse"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Call one tool on Aiko's embedded MCP debug server.",
    )
    parser.add_argument(
        "tool",
        nargs="?",
        help="Tool name. Omit with --list to enumerate what is available.",
    )
    parser.add_argument(
        "--url", default=DEFAULT_URL, help=f"SSE endpoint (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--arg",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="One argument, repeatable. Values are strings unless --json is used.",
    )
    parser.add_argument(
        "--json",
        dest="json_args",
        help="Arguments as a single JSON object. Mutually exclusive with --arg.",
    )
    parser.add_argument(
        "--list", action="store_true", help="List every registered tool and exit.",
    )
    parser.add_argument(
        "--timeout", type=float, default=180.0, help="Read timeout in seconds.",
    )
    return parser.parse_args(argv)


def _build_arguments(args: argparse.Namespace) -> dict[str, Any]:
    if args.json_args and args.arg:
        raise SystemExit("use either --json or --arg, not both")
    if args.json_args:
        parsed = json.loads(args.json_args)
        if not isinstance(parsed, dict):
            raise SystemExit("--json must be a JSON object")
        return parsed
    out: dict[str, Any] = {}
    for pair in args.arg:
        name, sep, value = pair.partition("=")
        if not sep:
            raise SystemExit(f"--arg must look like NAME=VALUE, got {pair!r}")
        out[name.strip()] = value
    return out


async def _run(args: argparse.Namespace) -> int:
    # Imported here so --help works without the mcp package installed.
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    arguments = _build_arguments(args)
    async with sse_client(args.url) as (read, write):
        async with ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=args.timeout),
        ) as session:
            await session.initialize()
            if args.list:
                listing = await session.list_tools()
                for tool in sorted(listing.tools, key=lambda t: t.name):
                    summary = (tool.description or "").strip().splitlines()
                    head = summary[0] if summary else ""
                    print(f"{tool.name:<44} {head}")
                print(f"\n{len(listing.tools)} tools", file=sys.stderr)
                return 0
            if not args.tool:
                raise SystemExit("give a tool name, or --list")
            result = await session.call_tool(args.tool, arguments)
            for block in result.content:
                text = getattr(block, "text", None)
                print(text if text is not None else block)
            return 1 if getattr(result, "isError", False) else 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
