"""Deterministic lexical discovery; no auxiliary inference or hidden permissions."""

import math
import re
from collections import Counter

_STOP = frozenset("a an the and or to of in on for from with this that i my me please".split())


def terms(text):
    words = re.findall(r"[a-z0-9]+", str(text).lower().replace("_", " "))
    return [
        word[:-1] if len(word) > 4 and word.endswith("s") else word
        for word in words
        if word not in _STOP
    ]


def rank_with_matches(rows, query, preferred=()):
    query_terms = set(terms(query))
    documents = []
    for row in rows:
        parameters = (row.get("input_schema") or {}).get("properties", {})
        documents.append(
            terms(
                " ".join(
                    [
                        row["name"],
                        row["name"],
                        row.get("group", ""),
                        row.get("description", ""),
                        *parameters,
                    ]
                )
            )
        )
    frequency = Counter(term for document in documents for term in set(document))
    average = sum(map(len, documents)) / max(1, len(documents))
    scores = {}
    for row, document in zip(rows, documents, strict=True):
        counts, score = Counter(document), 0.0
        for term, count in counts.items():
            if term in query_terms:
                inverse = math.log(
                    1 + (len(rows) - frequency[term] + 0.5) / (frequency[term] + 0.5)
                )
                score += (
                    inverse
                    * count
                    * 2.2
                    / (count + 1.2 * (0.25 + 0.75 * len(document) / max(1, average)))
                )
        scores[row["name"]] = score
    pins = {name: index for index, name in enumerate(dict.fromkeys(preferred))}
    ranked = sorted(
        rows, key=lambda row: (pins.get(row["name"], len(pins)), -scores[row["name"]], row["name"])
    )
    return ranked, sum(bool(scores[r["name"]] or r["name"] in pins) for r in ranked)


def rank(rows, query, preferred=()):
    return rank_with_matches(rows, query, preferred)[0]


def discovery_page(ranked, matches, page, size, *, browse, fallback=0):
    if not browse and not page and len(ranked) > size:
        ranked = ranked[:matches or fallback]
    return ranked[page * size:(page + 1) * size]


def route_followup_reads(menu, ranked, query, observation, size):
    """Use new words in an observation to surface lookups, never to dispatch them."""
    novel = {t for t in terms(observation[:1200]) if len(t) >= 3 and not t.isdigit()}
    novel.difference_update(terms(query))
    if not novel:
        return menu
    # Match registered identifiers, not arbitrary examples in tool descriptions.
    # Ties retain the goal ranking. Results can only nominate declared reads.
    candidates = []
    for row in ranked:
        if row.get("effect") != "read" or row.get("terminal"):
            continue
        identifiers = terms(row["name"]) + terms(row.get("group", ""))
        overlap = len(novel.intersection(identifiers))
        if overlap:
            candidates.append((overlap, row))
    candidates.sort(key=lambda pair: -pair[0])
    suggestions = [row for _, row in candidates[:min(3, size // 2)]]
    suggested = {row["name"] for row in suggestions}
    visible = {row["name"] for row in menu}
    extra = [row for row in suggestions if row["name"] not in visible]
    if not extra:
        return menu
    goal_rows = [row for row in menu if row["name"] not in suggested]
    keep = suggested.union(row["name"] for row in goal_rows[:size - len(suggestions)])
    return [row for row in menu if row["name"] in keep] + extra


def latest_read_hint(state, current_observation=None):
    steps = state.get("steps", [])
    boundary = state.get("routing_after_step", 0)
    index = len(steps) - 1
    # Inspecting a saved result is part of the same read, even across several pages.
    while index >= boundary and steps[index].get("skill") == "inspect_result":
        index -= 1
    if index < boundary:
        return ""
    last = steps[index]
    if (last.get("status") == "done" and last.get("effect") == "read"
            and type(last.get("child_id")) is int):
        inspections = state.get("inspections", [])
        if index < len(steps) - 1 and inspections:
            inspected = inspections[-1]
            if inspected.get("task_id") == last["child_id"] and inspected.get("content"):
                return inspected["content"]
        if (current_observation and current_observation.get("status") == "current"
                and current_observation.get("source_task_id") == last["child_id"]
                and isinstance(current_observation.get("content"), str)):
            return current_observation["content"] or last.get("observation", "")
        return last.get("observation", "")
    return ""


def routing_query(state):
    if state.get("query"):
        return state["query"]
    guidance = state.get("guidance") or []
    goal = state.get("goal", "")
    if guidance:
        return (goal + " " + guidance[-1].get("content", "")).strip()
    return goal


def manifest(rows, *, page=0, limit=6, matching_groups=None):
    groups = {}
    for row in rows:
        if row.get("terminal"):
            continue
        group = row.get("group") or "general"
        if matching_groups is not None and group not in matching_groups:
            continue
        item = groups.setdefault(group, {"group": group, "count": 0, "examples": []})
        item["count"] += 1
        if len(item["examples"]) < 2:
            item["examples"].append(row["name"])
    return list(groups.values())[page * limit:(page + 1) * limit]
