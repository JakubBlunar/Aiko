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


def catalogue(
    registry, profile: str, *, query: str = "", page: int = 0, budget: int = 18000
) -> list[dict]:
    import json

    rows = [
        row
        for row in registry.describe_for_planner()
        if supports(registry.get(row["name"]), profile)
    ]
    if query:
        words = query.lower().split()
        rows.sort(
            key=lambda row: (
                -sum(word in (row["name"] + " " + row["description"]).lower() for word in words)
            )
        )
    selected, used = [], 0
    for row in rows[max(0, page) * 12 :]:
        cost = len(json.dumps(row, ensure_ascii=False))
        if cost > budget:
            continue  # A schema is omitted whole, never truncated into an invalid contract.
        if used + cost > budget or len(selected) >= 12:
            break
        selected.append(row)
        used += cost
    return selected
