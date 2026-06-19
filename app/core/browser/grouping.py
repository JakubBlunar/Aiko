"""Dedup, heading-context injection, and form grouping over A11yNodes.

All pure functions over the normalized schema — no MCP, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.browser.accessibility import A11yNode


@dataclass(frozen=True, slots=True)
class FormGroup:
    """A logical form: a run of inputs under one heading context, plus an
    optional submit/primary button."""

    context: str
    inputs: tuple[A11yNode, ...]
    submit: A11yNode | None = None


def dedup_nodes(nodes: list[A11yNode]) -> list[A11yNode]:
    """Collapse exact (role, normalized-name) duplicate *interactive*
    elements, keeping the first occurrence.

    Headings and inputs are always preserved (repeated inputs are real —
    think two "quantity" fields), as are interactive elements with an
    empty name (can't safely tell them apart). A repeated named button or
    link (common in nav/footer/list rows) collapses to its first instance.
    """
    seen: set[tuple[str, str]] = set()
    out: list[A11yNode] = []
    for node in nodes:
        if node.is_interactive and not node.is_input and node.name.strip():
            key = node.dedup_key()
            if key in seen:
                continue
            seen.add(key)
        out.append(node)
    return out


def heading_context(nodes: list[A11yNode]) -> dict[int, str]:
    """Map each node's ``dom_order`` to its nearest heading chain.

    Walks nodes in document order maintaining a heading-by-level stack;
    every node is tagged with the breadcrumb of headings above it
    (e.g. ``"Checkout > Payment"``).
    """
    context: dict[int, str] = {}
    headings_by_level: dict[int, str] = {}
    for node in nodes:
        if node.is_heading and node.name.strip():
            level = node.level or 1
            headings_by_level[level] = node.name.strip()
            for deeper in [lvl for lvl in headings_by_level if lvl > level]:
                headings_by_level.pop(deeper, None)
        chain = " > ".join(
            headings_by_level[lvl] for lvl in sorted(headings_by_level)
        )
        context[node.dom_order] = chain
    return context


def group_forms(
    nodes: list[A11yNode], context_map: dict[int, str]
) -> list[FormGroup]:
    """Group consecutive input fields (sharing a heading context) into
    logical forms, attaching the next submit-like button as the submit.

    A run breaks when a non-input, non-submit interactive element appears
    or the heading context changes.
    """
    groups: list[FormGroup] = []
    current: list[A11yNode] = []
    current_ctx = ""

    def _flush(submit: A11yNode | None) -> None:
        nonlocal current, current_ctx
        if current:
            groups.append(
                FormGroup(
                    context=current_ctx,
                    inputs=tuple(current),
                    submit=submit,
                )
            )
        current = []
        current_ctx = ""

    for node in nodes:
        ctx = context_map.get(node.dom_order, "")
        if node.is_input:
            if current and ctx != current_ctx:
                _flush(None)
            if not current:
                current_ctx = ctx
            current.append(node)
        elif node.is_submit and current:
            _flush(node)
        elif node.is_interactive and current:
            # A non-form interactive element ends the run.
            _flush(None)
    _flush(None)
    return [g for g in groups if g.inputs]


__all__ = ["FormGroup", "dedup_nodes", "heading_context", "group_forms"]
