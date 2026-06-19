"""Per-server snapshot adapters: raw tool text -> ``list[A11yNode]``.

This is the ONLY format-specific layer. Switching MCP browser servers
means selecting (or adding) an adapter here; the downstream perception
pipeline is untouched.

Contract: ``parse(raw_text)`` returns a list of nodes, or ``None`` when
the text doesn't look like a snapshot this adapter understands (the
caller then falls back to raw passthrough so browsing never breaks).
An empty page that genuinely has no nodes returns ``[]`` (not ``None``).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol, runtime_checkable

from app.core.browser.accessibility import A11yNode


log = logging.getLogger("app.browser.adapters")


@runtime_checkable
class BrowserSnapshotAdapter(Protocol):
    """Parses one server's snapshot format into normalized nodes."""

    name: str

    def parse(self, raw_text: str) -> list[A11yNode] | None: ...


# ── indented-tree text parsing ───────────────────────────────────────

# One snapshot line: optional indent, optional "- " bullet, a role token,
# an optional quoted accessible name, then any number of [..] attributes.
_LINE_RE = re.compile(
    r"""^(?P<indent>[ \t]*)
        (?:-\s+)?
        (?P<role>[A-Za-z][\w-]*)
        (?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?
        (?P<attrs>(?:\s*\[[^\]]*\])*)
        \s*:?\s*$
    """,
    re.VERBOSE,
)
_ATTR_RE = re.compile(r"\[([^\]]*)\]")
_REF_VALUE_RE = re.compile(r"^[\"']?([\w:-]+)[\"']?$")


def _parse_attrs(blob: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in _ATTR_RE.findall(blob or ""):
        token = raw.strip()
        if not token:
            continue
        if "=" in token:
            key, _, val = token.partition("=")
            out[key.strip().lower()] = val.strip().strip("\"'")
        else:
            out[token.lower()] = "true"
    return out


def _bbox_from_attrs(attrs: dict[str, str]) -> tuple[int, int, int, int] | None:
    raw = attrs.get("bbox") or attrs.get("rect") or attrs.get("box")
    if not raw:
        return None
    parts = re.split(r"[ ,;]+", raw.strip())
    nums: list[int] = []
    for p in parts:
        try:
            nums.append(int(float(p)))
        except (TypeError, ValueError):
            return None
    if len(nums) != 4:
        return None
    return (nums[0], nums[1], nums[2], nums[3])


def _node_from_line(line_match: re.Match[str], depth: int, order: int) -> A11yNode:
    role = line_match.group("role").strip().lower()
    name = line_match.group("name") or ""
    name = name.replace('\\"', '"').strip()
    attrs = _parse_attrs(line_match.group("attrs") or "")
    ref_raw = attrs.get("ref", "")
    ref_match = _REF_VALUE_RE.match(ref_raw) if ref_raw else None
    ref = ref_match.group(1) if ref_match else ref_raw
    disabled = attrs.get("disabled", "false") in {"true", "", "1"}
    if "disabled" not in attrs:
        disabled = False
    hidden = attrs.get("hidden", "false") in {"true", "1"} or attrs.get(
        "visible", "true"
    ) in {"false", "0"}
    try:
        level = int(attrs.get("level", "0") or "0")
    except (TypeError, ValueError):
        level = 0
    return A11yNode(
        ref=ref,
        role=role,
        name=name,
        value=attrs.get("value", ""),
        depth=depth,
        dom_order=order,
        visible=not hidden,
        disabled=disabled,
        level=level,
        bbox=_bbox_from_attrs(attrs),
        attrs=attrs,
    )


def parse_indented_tree(raw_text: str) -> list[A11yNode] | None:
    """Parse an indented YAML-ish accessibility tree into nodes.

    Depth is derived from a running indentation stack so 2-space, 4-space,
    or tab indentation all resolve to true nesting depth.
    """
    text = (raw_text or "").strip()
    if not text:
        return []
    nodes: list[A11yNode] = []
    indent_stack: list[int] = []
    order = 0
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        m = _LINE_RE.match(raw_line)
        if not m:
            continue
        indent = len(m.group("indent").expandtabs(2))
        while indent_stack and indent < indent_stack[-1]:
            indent_stack.pop()
        if not indent_stack or indent > indent_stack[-1]:
            indent_stack.append(indent)
        depth = len(indent_stack) - 1
        nodes.append(_node_from_line(m, depth, order))
        order += 1
    if not nodes:
        return None
    return nodes


# ── JSON tree parsing ────────────────────────────────────────────────

_JSON_CONTAINER_KEYS = ("nodes", "tree", "root", "elements", "snapshot", "children")
_NAME_KEYS = ("name", "label", "text", "accessibleName", "title")
_REF_KEYS = ("ref", "id", "elementRef", "nodeId", "backendNodeId")
_CHILDREN_KEYS = ("children", "nodes", "items")


def _walk_json_node(
    raw: Any, depth: int, counter: list[int], out: list[A11yNode]
) -> None:
    if isinstance(raw, list):
        for item in raw:
            _walk_json_node(item, depth, counter, out)
        return
    if not isinstance(raw, dict):
        return
    role = str(raw.get("role", "") or "").strip().lower()
    if role:
        name = ""
        for key in _NAME_KEYS:
            if raw.get(key):
                name = str(raw.get(key))
                break
        ref = ""
        for key in _REF_KEYS:
            if raw.get(key) not in (None, ""):
                ref = str(raw.get(key))
                break
        hidden = bool(raw.get("hidden", False)) or raw.get("visible", True) is False
        bbox = None
        box = raw.get("bbox") or raw.get("rect") or raw.get("box")
        if isinstance(box, (list, tuple)) and len(box) == 4:
            try:
                bbox = tuple(int(float(v)) for v in box)  # type: ignore[assignment]
            except (TypeError, ValueError):
                bbox = None
        try:
            level = int(raw.get("level", 0) or 0)
        except (TypeError, ValueError):
            level = 0
        out.append(
            A11yNode(
                ref=ref,
                role=role,
                name=name.strip(),
                value=str(raw.get("value", "") or ""),
                depth=depth,
                dom_order=counter[0],
                visible=not hidden,
                disabled=bool(raw.get("disabled", False)),
                level=level,
                bbox=bbox,  # type: ignore[arg-type]
            )
        )
        counter[0] += 1
    child_depth = depth + 1 if role else depth
    for key in _CHILDREN_KEYS:
        children = raw.get(key)
        if isinstance(children, list):
            for child in children:
                _walk_json_node(child, child_depth, counter, out)


def parse_json_tree(raw_text: str) -> list[A11yNode] | None:
    """Parse a JSON accessibility payload into nodes, or None if not JSON."""
    text = (raw_text or "").strip()
    if not text or text[0] not in "[{":
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    out: list[A11yNode] = []
    counter = [0]
    root: Any = payload
    if isinstance(payload, dict):
        for key in _JSON_CONTAINER_KEYS:
            if key in payload:
                root = payload[key]
                break
    _walk_json_node(root, 0, counter, out)
    return out


# ── adapters ─────────────────────────────────────────────────────────

class GenericIndentedTreeAdapter:
    """Server-agnostic fallback: indented accessibility tree only."""

    name = "generic"

    def parse(self, raw_text: str) -> list[A11yNode] | None:
        try:
            return parse_indented_tree(raw_text)
        except Exception:
            log.debug("generic adapter parse failed", exc_info=True)
            return None


class RealBrowserAdapter:
    """Adapter for ``real-browser-mcp`` ``browser_snapshot`` output.

    Tries JSON first (some builds emit structured payloads), then falls
    back to the indented accessibility tree. Robust to either shape.
    """

    name = "real_browser"

    def parse(self, raw_text: str) -> list[A11yNode] | None:
        try:
            nodes = parse_json_tree(raw_text)
            if nodes:
                return nodes
            return parse_indented_tree(raw_text)
        except Exception:
            log.debug("real_browser adapter parse failed", exc_info=True)
            return None


_ADAPTERS: dict[str, BrowserSnapshotAdapter] = {
    GenericIndentedTreeAdapter.name: GenericIndentedTreeAdapter(),
    RealBrowserAdapter.name: RealBrowserAdapter(),
}


def get_adapter(name: str) -> BrowserSnapshotAdapter:
    """Return the named adapter, falling back to the generic one."""
    adapter = _ADAPTERS.get((name or "").strip().lower())
    if adapter is None:
        log.warning("unknown browser adapter %r, using generic", name)
        return _ADAPTERS[GenericIndentedTreeAdapter.name]
    return adapter


__all__ = [
    "BrowserSnapshotAdapter",
    "GenericIndentedTreeAdapter",
    "RealBrowserAdapter",
    "get_adapter",
    "parse_indented_tree",
    "parse_json_tree",
]
