"""Per-skill-group operational guidance for the workflow planner.

Some skill groups need more than a one-line description to be used well —
they need a short *playbook* of cross-tool know-how (ordering, gotchas,
safety rules). That guidance now travels **with the plugin** that provides
the tools: a plugin's ``SKILL.md`` (or a connected server's runtime-captured
instructions) is surfaced to the planner as ``group_guidance`` keyed by the
``mcp:<server_id>`` group. There are no longer any hardcoded browser /
filesystem playbooks baked into the core — a plugin owns the guidance for
its own tools.

Pure data + selector functions; no I/O, no settings.
"""
from __future__ import annotations

from typing import Any, Iterable


def guidance_for_skills(
    skills: Iterable[dict[str, Any]],
    *,
    group_guidance: "dict[str, str] | None" = None,
) -> str:
    """Compose the plugin / captured guidance for the planner's menu.

    Takes the full skill list (each ``{name, group, ...}``) and, for every
    ``mcp:<server_id>`` group present in the menu, includes that group's
    guidance text from ``group_guidance`` (a plugin ``SKILL.md`` or a
    connected server's runtime-captured instructions). Groups without an
    entry contribute nothing. Returns the blocks joined by blank lines, or
    "" when none apply — the planner prompt then has no GUIDANCE block.
    """
    skill_list = list(skills)
    present = {str(s.get("group", "") or "") for s in skill_list}
    gg = group_guidance or {}
    blocks: list[str] = []
    for group in sorted(g for g in present if g.startswith("mcp:")):
        text = gg.get(group, "")
        if text.strip():
            blocks.append(text.strip())
    return "\n\n".join(blocks)


__all__ = [
    "guidance_for_skills",
]
