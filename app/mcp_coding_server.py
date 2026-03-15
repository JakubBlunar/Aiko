"""MCP server for coding tools: read_file, list_dir, search, apply_patch. Paths restricted to ALLOWED_ROOTS (and optional ALLOWED_FILES). Run with: python -m app.mcp_coding_server"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def _allowed_roots() -> list[Path]:
    raw = os.environ.get("ALLOWED_ROOTS", "").strip()
    if not raw:
        return []
    return [Path(p.strip()).resolve() for p in raw.split("|") if p.strip()]


def _allowed_files() -> list[Path]:
    raw = os.environ.get("ALLOWED_FILES", "").strip()
    if not raw:
        return []
    return [Path(p.strip()).resolve() for p in raw.split("|") if p.strip()]


def _resolve_path(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _is_allowed(path: Path, allow_dirs: bool = True) -> tuple[bool, str]:
    """Return (allowed, error_message)."""
    roots = _allowed_roots()
    files = _allowed_files()
    if not roots and not files:
        return False, "No ALLOWED_ROOTS or ALLOWED_FILES set; server not configured."
    try:
        resolved = path.resolve()
    except Exception as e:
        return False, f"Invalid path: {e}"
    for root in roots:
        try:
            resolved.relative_to(root)
            if resolved.is_dir() and allow_dirs:
                return True, ""
            if resolved.is_file():
                return True, ""
            if allow_dirs and not resolved.exists():
                return True, ""
        except ValueError:
            continue
    for f in files:
        if resolved == f:
            return True, ""
    return False, f"Path not under allowed roots or allowlist: {path}"


def main() -> None:
    roots = _allowed_roots()
    if not roots:
        print("ALLOWED_ROOTS not set; exiting.", file=sys.stderr)
        sys.exit(1)

    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("Install mcp package for coding MCP server: pip install mcp", file=sys.stderr)
        sys.exit(1)

    mcp = FastMCP("coding", description="Read and edit files under allowed workspace roots.")

    @mcp.tool()
    def read_file(path: str) -> str:
        """Read the full contents of a file. Path must be under allowed workspace roots."""
        p = _resolve_path(path)
        allowed, err = _is_allowed(p, allow_dirs=False)
        if not allowed:
            return err
        if not p.is_file():
            return f"Not a file or not found: {path}"
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return f"Error reading file: {e}"

    @mcp.tool()
    def list_dir(path: str) -> str:
        """List directory contents (names only). Path must be under allowed workspace roots."""
        p = _resolve_path(path)
        allowed, err = _is_allowed(p, allow_dirs=True)
        if not allowed:
            return err
        if not p.is_dir():
            return f"Not a directory or not found: {path}"
        try:
            names = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            return "\n".join(n.name for n in names)
        except Exception as e:
            return f"Error listing directory: {e}"

    @mcp.tool()
    def search(directory: str, pattern: str) -> str:
        """Search for files under a directory whose name or content matches a regex. Path must be under allowed roots."""
        dir_p = _resolve_path(directory)
        allowed, err = _is_allowed(dir_p, allow_dirs=True)
        if not allowed:
            return err
        if not dir_p.is_dir():
            return f"Not a directory or not found: {directory}"
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as e:
            return f"Invalid regex: {e}"
        results: list[str] = []
        for root, _dirs, files in os.walk(dir_p):
            root_path = Path(root)
            for name in files:
                if regex.search(name):
                    results.append(str(root_path / name))
                    continue
                try:
                    p = root_path / name
                    if not _is_allowed(p, allow_dirs=False)[0]:
                        continue
                    content = p.read_text(encoding="utf-8", errors="replace")
                    if regex.search(content):
                        results.append(str(p))
                except (ValueError, OSError, UnicodeDecodeError):
                    continue
        return "\n".join(results[:200]) or "No matches found."

    @mcp.tool()
    def apply_patch(file_path: str, content: str) -> str:
        """Write file contents (full replacement). File must be under allowed workspace roots."""
        p = _resolve_path(file_path)
        allowed, err = _is_allowed(p, allow_dirs=False)
        if not allowed:
            return err
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return "File written successfully."
        except Exception as e:
            return f"Error writing file: {e}"

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
