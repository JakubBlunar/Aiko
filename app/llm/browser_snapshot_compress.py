"""Deterministic shrinking of Playwright MCP `browser_snapshot` text for LLM context.

Preserves ``[ref=...]`` substrings so browser_click targets stay valid.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_REF_RE = re.compile(r"(\[ref=[^\]]+\])")


@dataclass(frozen=True, slots=True)
class BrowserSnapshotCompressOptions:
    enabled: bool = True
    max_chars: int | None = None
    max_text_run: int | None = None


def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text)


def _truncate_segments_between_refs(line: str, max_run: int) -> str:
    """Shorten long text runs between [ref=...] chunks without touching ref tokens."""
    if max_run <= 0 or "[ref=" not in line:
        return line
    parts = _REF_RE.split(line)
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if _REF_RE.fullmatch(part):
            out.append(part)
            continue
        if len(part) <= max_run:
            out.append(part)
            continue
        head = max_run // 2
        tail = max_run - head
        out.append(
            f"{part[:head]}...[{len(part)} chars omitted]...{part[-tail:]}" if tail > 0 else f"{part[:max_run]}...[{len(part)} chars omitted]"
        )
    return "".join(out)


def _rebuild_with_budget(lines: list[str], max_chars: int) -> str:
    """Prefer lines containing refs, then prefix of remaining lines, until max_chars."""
    max_chars = max(80, max_chars)
    ref_lines = [(i, ln) for i, ln in enumerate(lines) if "[ref=" in ln]
    other_lines = [(i, ln) for i, ln in enumerate(lines) if "[ref=" not in ln]
    chosen: dict[int, str] = {}
    used = 0
    notice = "\n\n... [browser_snapshot truncated for context size] ...\n"
    reserve = len(notice) + 20
    budget = max_chars - reserve

    for i, ln in ref_lines:
        piece = ln + "\n"
        if used + len(piece) > budget:
            continue
        chosen[i] = ln
        used += len(piece)

    for i, ln in other_lines:
        if i in chosen:
            continue
        piece = ln + "\n"
        if used + len(piece) > budget:
            break
        chosen[i] = ln
        used += len(piece)

    if len(chosen) == len(lines):
        return "\n".join(lines[i] for i in range(len(lines)))

    ordered = [chosen[i] for i in sorted(chosen.keys())]
    body = "\n".join(ordered)
    if len(lines) > len(chosen):
        body += notice
    return body


def compress_browser_snapshot(
    text: str,
    *,
    enabled: bool,
    max_chars: int | None,
    max_text_run: int | None,
) -> str:
    if not enabled or not text:
        return text

    max_run = max_text_run if max_text_run is not None else 2000
    max_run = max(80, min(max_run, 50_000))

    current = text
    for _ in range(5):
        lines = current.splitlines()
        lines = [_truncate_segments_between_refs(ln, max_run) for ln in lines]
        joined = "\n".join(lines)
        joined = _collapse_blank_lines(joined)
        if max_chars is None or len(joined) <= max_chars:
            return joined
        max_run = max(80, max_run // 2)
        current = joined

    if max_chars is not None and len(current) > max_chars:
        return _rebuild_with_budget(current.splitlines(), max_chars)
    return current


def compress_browser_snapshot_with_options(text: str, options: BrowserSnapshotCompressOptions) -> str:
    return compress_browser_snapshot(
        text,
        enabled=options.enabled,
        max_chars=options.max_chars,
        max_text_run=options.max_text_run,
    )


def browser_snapshot_options_from_agent_settings(
    *,
    browser_snapshot_compress: bool,
    browser_snapshot_max_chars: int | None,
    browser_snapshot_max_text_run: int | None,
    compress_tool_results_limit: int | None,
    compress_token_limit: int | None,
) -> BrowserSnapshotCompressOptions:
    """Resolve max_chars: explicit snapshot limit, else token/limit-derived heuristic."""
    resolved_max = browser_snapshot_max_chars
    if resolved_max is None:
        if compress_tool_results_limit is not None and compress_tool_results_limit > 0:
            resolved_max = compress_tool_results_limit
        elif compress_token_limit is not None and compress_token_limit > 0:
            resolved_max = int(compress_token_limit * 3.5)
        else:
            resolved_max = 8000
    resolved_max = max(80, min(resolved_max, 500_000))
    return BrowserSnapshotCompressOptions(
        enabled=browser_snapshot_compress,
        max_chars=resolved_max,
        max_text_run=browser_snapshot_max_text_run
        if browser_snapshot_max_text_run is not None
        else 2000,
    )
