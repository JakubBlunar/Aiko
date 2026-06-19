"""Per-skill-group operational playbooks for the workflow planner.

Some skill groups need more than a one-line description to be used well —
they need a short *playbook* of cross-tool know-how (ordering, gotchas,
safety rules). The browser group is the first: the upstream
``real-browser-mcp`` ships a SKILL.md + Cursor rule with the snapshot-first
workflow, the "refs go stale after navigation" rule, the React-dropdown
pattern, and the "never close tabs you didn't create" guardrails. That
guidance belongs to the **planner** (it picks the browser actions), so we
inject it into the planner prompt — but only when the relevant group's
skills are actually in the menu, so it never bloats unrelated workflows.

The filesystem playbook is the second. A filesystem MCP server (e.g.
``@modelcontextprotocol/server-filesystem``) sandboxes every path to a
fixed root and rejects anything outside it ("path outside allowed
directories"). When it runs alongside the built-in file skills (which use
a label like ``Documents:``), the planner tends to cross-contaminate the
two path conventions and hand a label/relative path to an MCP file tool,
which resolves it against the process working directory and gets denied.
The playbook teaches the absolute-path-under-the-sandbox-root discipline.

Pure data + selector functions; no I/O, no settings.
"""
from __future__ import annotations

from typing import Any, Callable, Iterable


# Condensed from real-browser-mcp's agent-config (SKILL.md + the Cursor
# rule). Phrased for our planner and our namespaced skill names
# (``<server_id>__<tool>``); the perception layer already turns a snapshot
# into a ranked element list whose refs are what you pass to click/type.
BROWSER_PLAYBOOK = (
    "Browser skills — how to use them well:\n"
    "- Snapshot FIRST. Call the snapshot skill to get the ranked element "
    "list with refs (e.g. e12); prefer it over a screenshot. Pass those "
    "refs to click/type.\n"
    "- Refs go STALE after any navigation, scroll, or DOM change — "
    "re-snapshot before reusing refs, never reuse old ones.\n"
    "- For a big page, scope the snapshot with a CSS `selector` "
    "(e.g. \"main\") to cut noise.\n"
    "- To act: click/type/press_key/scroll/select using a ref from the "
    "LATEST snapshot. Submit with press_key Enter.\n"
    "- Dropdowns / menus / React portals: click the trigger, wait briefly, "
    "then click the option by visible text (a click-by-text skill if "
    "available). Avoid the evaluate/JavaScript skill for UI — it breaks on "
    "strict-CSP sites and can steal focus.\n"
    "- After an action, re-snapshot (or find) to confirm the result before "
    "the next step.\n"
    "- Safety: NEVER close tabs you didn't open, and don't navigate away "
    "from the page the user is on unless the goal requires it."
)


# A sandboxed filesystem MCP server (any server exposing these tools).
# Phrased generically so it holds for any filesystem-style MCP, and it
# explicitly warns off the built-in ``Documents:`` label cross-contamination
# that triggers "path outside allowed directories".
FILESYSTEM_PLAYBOOK = (
    "Filesystem skills — how to use them well:\n"
    "- These MCP file skills are sandboxed to a fixed root that is NOT the "
    "working directory. FIRST call the list-allowed-directories skill to "
    "learn the exact absolute root, then build every path as an absolute "
    "path UNDER that root (e.g. <root>/notes/file.txt).\n"
    "- NEVER pass a bare, relative, or label-style path (like "
    "\"Documents/foo.txt\" or \"Documents:foo.txt\") to these skills — a "
    "non-absolute path resolves against the process working directory and "
    "is rejected as \"path outside allowed directories\". The built-in "
    "file skills' \"Documents\" label does NOT apply here.\n"
    "- Confirm a path exists with the list-directory or get-file-info skill "
    "before reading/writing when unsure.\n"
    "- There is no copy tool: to COPY a file, read the source then write the "
    "destination (both absolute, under the root). Use the move skill to "
    "move/rename and the create-directory skill to make folders.\n"
    "- If a write/move is denied as outside the root, do NOT retry the same "
    "path — re-read the allowed root and rebuild the path under it, or "
    "finish and report the path problem plainly."
)

# Distinctive tool names that mark a group as a filesystem MCP server.
# Matching by capability (not server id) keeps detection server-agnostic.
_FILESYSTEM_TOOL_MARKERS = frozenset(
    {
        "list_allowed_directories",
        "directory_tree",
        "create_directory",
        "move_file",
        "list_directory",
        "get_file_info",
    }
)


def _tool_part(skill_name: str) -> str:
    """The ``<tool>`` half of a namespaced ``<server_id>__<tool>`` name."""
    name = str(skill_name or "")
    return name.split("__")[-1] if "__" in name else name


def filesystem_group_for_skills(skills: Iterable[dict[str, Any]]) -> str:
    """Return the ``mcp:<id>`` group of a filesystem-style server, or "".

    Detection is by capability: any skill in an ``mcp:*`` group whose tool
    name is a known filesystem marker tags that whole group as filesystem.
    Server-agnostic, so a swapped filesystem MCP is covered automatically.
    """
    for skill in skills:
        group = str(skill.get("group", "") or "")
        if not group.startswith("mcp:"):
            continue
        if _tool_part(skill.get("name", "")) in _FILESYSTEM_TOOL_MARKERS:
            return group
    return ""


def filesystem_playbook(roots: Iterable[str] | None = None) -> str:
    """The filesystem playbook, with the EXACT allowed root(s) inlined.

    Local planner models are unreliable at the "call list_allowed_directories
    to discover the root, then use it" pattern — they tend to invent a root
    (e.g. ``F:\\allowed\\…``). When the caller can supply the configured
    sandbox root(s), we inline them verbatim so the planner builds paths
    under a real directory it never has to guess. ``roots`` empty / None
    falls back to the base playbook (discover-via-tool)."""
    clean = [str(r).strip() for r in (roots or []) if str(r).strip()]
    if not clean:
        return FILESYSTEM_PLAYBOOK
    listed = "; ".join(clean)
    root_line = (
        "\n- The allowed sandbox root(s) for these skills are EXACTLY: "
        f"{listed}. Build every path under one of these (e.g. "
        f"{clean[0]}\\subdir\\file.txt). Do NOT invent any other root."
    )
    return FILESYSTEM_PLAYBOOK + root_line


def guidance_for_groups(
    present_groups: Iterable[str], *, browser_group: str = ""
) -> str:
    """Concatenated playbook(s) for the groups currently in the menu.

    ``present_groups`` is the set of ``group`` labels on the skills the
    planner will see this turn (post-router-narrowing). ``browser_group``
    is the configured browser skill group (``mcp:<server_id>``); when it's
    in ``present_groups`` the browser playbook is included. Returns an
    empty string when no group has a playbook — the planner prompt then
    has no GUIDANCE block at all.
    """
    groups = {str(g) for g in present_groups if str(g)}
    blocks: list[str] = []
    if browser_group and browser_group in groups:
        blocks.append(BROWSER_PLAYBOOK)
    return "\n\n".join(blocks)


def guidance_for_skills(
    skills: Iterable[dict[str, Any]],
    *,
    browser_group: str = "",
    root_lookup: "Callable[[str], list[str]] | None" = None,
) -> str:
    """Compose every applicable playbook for the planner's current menu.

    Takes the full skill list (each ``{name, group, ...}``) so it can both
    match the configured ``browser_group`` and auto-detect a filesystem
    group by tool capability. ``root_lookup`` maps an MCP ``server_id`` to
    its configured sandbox root path(s); when the filesystem group is
    found, the resolved roots are inlined into the playbook verbatim so the
    planner never has to guess the root. Returns the playbooks joined by
    blank lines, or "" when none apply.
    """
    skill_list = list(skills)
    present = {str(s.get("group", "") or "") for s in skill_list}
    blocks: list[str] = []
    if browser_group and browser_group in present:
        blocks.append(BROWSER_PLAYBOOK)
    fs_group = filesystem_group_for_skills(skill_list)
    if fs_group:
        roots: list[str] = []
        if root_lookup is not None:
            server_id = fs_group[len("mcp:"):]
            try:
                roots = list(root_lookup(server_id) or [])
            except Exception:
                roots = []
        blocks.append(filesystem_playbook(roots))
    return "\n\n".join(blocks)


__all__ = [
    "BROWSER_PLAYBOOK",
    "FILESYSTEM_PLAYBOOK",
    "filesystem_group_for_skills",
    "filesystem_playbook",
    "guidance_for_groups",
    "guidance_for_skills",
]
