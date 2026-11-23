"""Work roles choose context and operations, without requiring user setup."""

from __future__ import annotations

PROFILES = {
    "general": "Coordinate the goal, discover capabilities and choose another profile as useful.",
    "reader": "Inspect permitted files and sources. Preserve evidence, ranges and source identity.",
    "researcher": "Research questions using available sources. Separate findings from guesses.",
    "browser": "Operate the browser. Inspect current state before acting; verify effects.",
    "writer": "Create or edit requested output with granted operations; check the saved result.",
    "reviewer": "Review existing results, test completion criteria and identify missing evidence.",
}


def supports(skill, profile: str) -> bool:
    if profile == "general" or skill.terminal:
        return True
    group = skill.group.lower()
    if profile == "browser":
        return any(word in group for word in ("browser", "playwright", "chrome"))
    if profile == "writer":
        return skill.effect in {"read", "write", "unknown"}
    if profile in {"reader", "reviewer"}:
        return skill.effect == "read"
    if profile == "researcher":
        return skill.effect == "read"
    return False


def route_catalogue(
    registry,
    profile: str,
    *,
    query: str = "",
    page: int = 0,
    budget: int = 18000,
    page_size: int = 12,
    preferred=(),
    browse=False,
    read_hint="",
) -> tuple[list[dict], list[dict]]:
    import json
    from app.core.tasks.workflow.discovery import (
        discovery_page, manifest, rank_with_matches, route_followup_reads,
    )

    rows = [
        row
        for row in registry.describe_for_planner()
        if (skill := registry.get(row["name"])) and supports(skill, profile)
    ]
    ranked, matches = rank_with_matches(rows, query, preferred)
    matching_groups = None
    if matches and not browse and not page and len(rows) > page_size:
        matching_groups = {row.get("group") or "general" for row in ranked[:matches]}
    menu = discovery_page(ranked, matches, max(0, page), page_size, browse=browse, fallback=3)
    if read_hint and not (browse or page or preferred) and len(rows) > page_size:
        menu = route_followup_reads(menu, ranked, query, read_hint, page_size)
        if matching_groups is not None:
            matching_groups.update(row.get("group") or "general" for row in menu)
    groups = manifest(ranked, page=page, matching_groups=matching_groups)
    selected, used = [], 0
    for row in menu:
        cost = len(json.dumps(row, ensure_ascii=False))
        if cost > budget:
            continue  # A schema is omitted whole, never truncated into an invalid contract.
        if used + cost > budget:
            continue
        selected.append(row)
        used += cost
    return selected, groups


def catalogue(registry, profile, **kwargs):
    return route_catalogue(registry, profile, browse=True, **kwargs)[0]
