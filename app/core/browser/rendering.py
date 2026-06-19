"""Render a perceived page into a compact planner block + one-line summary."""
from __future__ import annotations

from app.core.browser.grouping import FormGroup
from app.core.browser.page_state import PageDiff
from app.core.browser.ranking import RankedElement


def _ref_suffix(ref: str) -> str:
    return f" ref={ref}" if ref else ""


def render_elements(ranked: list[RankedElement]) -> str:
    lines: list[str] = []
    for i, item in enumerate(ranked, start=1):
        node = item.node
        name = " ".join(node.name.split()) or "(unnamed)"
        ctx = f" — ctx: {item.context}" if item.context else ""
        flags = ""
        if node.disabled:
            flags += " [disabled]"
        if not node.visible:
            flags += " [hidden]"
        lines.append(
            f"{i}. [{node.role}] \"{name}\"{_ref_suffix(node.ref)}"
            f"{ctx} (score {item.score:.2f}){flags}"
        )
    return "\n".join(lines)


def render_forms(forms: list[FormGroup]) -> str:
    lines: list[str] = []
    for form in forms:
        input_names = ", ".join(
            (" ".join(n.name.split()) or "(unnamed)") for n in form.inputs
        )
        ctx = f"{form.context}: " if form.context else ""
        submit = ""
        if form.submit is not None:
            sname = " ".join(form.submit.name.split()) or "(unnamed)"
            submit = f" submit=[{sname}{_ref_suffix(form.submit.ref)}]"
        lines.append(f"- {ctx}inputs[{input_names}]{submit}")
    return "\n".join(lines)


def render_diff(diff: PageDiff | None) -> str:
    if diff is None or diff.is_empty:
        return ""
    lines: list[str] = []
    if diff.added:
        lines.append("  + added: " + "; ".join(diff.added))
    if diff.removed:
        lines.append("  - removed: " + "; ".join(diff.removed))
    if diff.changed:
        lines.append("  ~ changed: " + "; ".join(diff.changed))
    return "\n".join(lines)


def render_page(
    title: str,
    ranked: list[RankedElement],
    forms: list[FormGroup],
    diff: PageDiff | None,
    *,
    total_nodes: int,
) -> tuple[str, str]:
    """Build (content, summary) for the planner blackboard."""
    header = f"Page: {title}" if title else "Page"
    sections: list[str] = [header]

    if ranked:
        sections.append(
            f"Interactive elements (ranked, {len(ranked)} shown):\n"
            + render_elements(ranked)
        )
    else:
        sections.append("Interactive elements: none detected.")

    forms_block = render_forms(forms)
    if forms_block:
        sections.append("Forms:\n" + forms_block)

    diff_block = render_diff(diff)
    if diff_block:
        sections.append("Changes since last snapshot:\n" + diff_block)

    content = "\n\n".join(sections)

    if ranked:
        top = ranked[0].node
        top_name = " ".join(top.name.split()) or "(unnamed)"
        summary = (
            f"page '{title}': {len(ranked)} interactive, "
            f"top: [{top.role}] \"{top_name}\""
        )
    else:
        summary = f"page '{title}': no interactive elements ({total_nodes} nodes)"
    return content, summary[:200]


__all__ = ["render_page", "render_elements", "render_forms", "render_diff"]
