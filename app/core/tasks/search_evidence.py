"""Citable search fields shared by workflow completion and task reporting."""

import json

from app.core.tasks.handler_names import HANDLER_FAST_TOOL, HANDLER_WEB_SEARCH


def returned_hits(row):
    """None denotes a different capability. Malformed search output has no evidence."""
    handler = getattr(row, "handler_name", "")
    data = row.result or {}
    if handler == HANDLER_FAST_TOOL and (row.args or {}).get("tool_name") == "web_search":
        try:
            data = json.loads(data.get("content", ""))
        except (TypeError, ValueError):
            return []
    elif handler != HANDLER_WEB_SEARCH:
        return None
    hits = data.get("results") if isinstance(data, dict) else None
    return hits if isinstance(hits, list) else []


def render_fields(hits, *, inspected_text=None):
    """One complete JSON pair per line, so excerpts retain their original field IDs."""
    lines = []
    for index, hit in enumerate(hits, 1):
        if not isinstance(hit, dict):
            continue
        for key in ("title", "snippet", "url"):
            value = hit.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            if inspected_text is not None and not any(
                json.dumps(value, ensure_ascii=ascii_only)[1:-1] in inspected_text
                for ascii_only in (False, True)
            ):
                continue
            lines.append(json.dumps([f"{index}.{key}", value], ensure_ascii=False,
                                    separators=(",", ":")))
    return "\n".join(lines)


def packed_fields(content):
    """Only complete pairs survive budget trimming and can support a finding."""
    for line in content.split("\n"):
        try:
            pair = json.loads(line)
        except ValueError:
            continue  # A clipped field or host omission marker cannot be cited.
        if (isinstance(pair, list) and len(pair) == 2 and isinstance(pair[0], str)
                and isinstance(pair[1], str)):
            yield pair[0], pair[1]
