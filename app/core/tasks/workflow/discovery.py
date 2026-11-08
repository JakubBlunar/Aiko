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


def rank(rows, query, preferred=()):
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
        for term in query_terms:
            count = counts[term]
            if count:
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
    pins = {name: index for index, name in enumerate(preferred)}
    return sorted(
        rows, key=lambda row: (pins.get(row["name"], len(pins)), -scores[row["name"]], row["name"])
    )


def manifest(registry):
    groups = {}
    for row in registry.describe_for_planner():
        if row.get("terminal"):
            continue
        group = row.get("group") or "general"
        item = groups.setdefault(group, {"group": group, "count": 0, "examples": []})
        item["count"] += 1
        if len(item["examples"]) < 4:
            item["examples"].append(row["name"])
    return [groups[name] for name in sorted(groups)][:40]
