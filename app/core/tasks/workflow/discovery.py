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
