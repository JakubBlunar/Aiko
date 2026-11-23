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


def _new_read_terms(query, observation):
    words = {t for t in terms(observation[:1200]) if len(t) >= 3 and not t.isdigit()}
    return words.difference(terms(query))


def route_followup_reads(menu, ranked, query, observation, size):
    """Use new words in an observation to surface lookups, never to dispatch them."""
    novel = _new_read_terms(query, observation)
    if not novel:
        return menu
    # Description-only matches need two terms that distinguish reads in this catalogue.
    reads = [row for row in ranked if row.get("effect") == "read" and not row.get("terminal")]
    descriptions = [novel.intersection(terms(row.get("description", ""))) for row in reads]
    frequency = Counter(term for description in descriptions for term in description)
    distinctive = {term for term, count in frequency.items()
                   if len(term) >= 4 and count <= max(1, len(reads) // 2)}
    candidates = []
    for row, description in zip(reads, descriptions, strict=True):
        named = novel.intersection(terms(row["name"]) + terms(row.get("group", "")))
        described = distinctive.intersection(description)
        if named or len(described) >= 2:
            matches = named or described
            candidates.append((bool(named), matches, row))
    # Cover equally supported alternatives before filling the page with editions
    # of one lookup. The goal's ordering settles any remaining ties.
    covered = set()
    suggestions = []
    while candidates and len(suggestions) < min(3, size // 2):
        best = max(candidates, key=lambda item: (item[0], len(item[1]), len(item[1] - covered)))
        candidates.remove(best)
        covered.update(best[1])
        suggestions.append(best[2])
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


def named_read_alternatives(menu, query, observation):
    """Distinct group names introduced by a read and exposed in the current menu."""
    novel = _new_read_terms(query, observation)
    groups = {}
    for row in menu:
        group = row.get("group") or ""
        matched = novel.intersection(terms(group))
        if row.get("effect") == "read" and matched:
            groups[group] = matched
    # A shared server prefix alone is no indication of different referenced subjects.
    return [g for g, hits in groups.items()
            if any(hits - other and other - hits for other in groups.values())]


def routing_query(state):
    if state.get("query"):
        return state["query"]
    if state.get("reference_query"):
        return state["reference_query"]
    goal = state.get("goal", "")
    for turn in reversed(state.get("guidance") or []):
        if turn.get("role", "user") == "user":
            return (goal + " " + turn.get("content", "")).strip()
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
