"""Expand compact model proposals into the receipts required by the pattern validators."""
from __future__ import annotations

import re


def extraction_schema(names, strategies):
    """Constrain specialized receipts while keeping ordinary memory fields extensible."""
    if not (names or strategies):
        return None
    string = {"type": "string"}
    integer = {"type": "integer"}

    def record(fields):
        return {"type": "object", "properties": fields, "required": list(fields)}

    def array(items):
        return {"type": "array", "items": items}

    fields = {}
    if names:
        name = record({"term": string, "meaning": string,
                       "definition_id": integer, "adoption_id": integer})
        fields["shared_names"] = array(name)
    if strategies:
        span = record({"message_id": integer, "text": string})
        fields["approaches"] = array(record({
            "goal": span, "constraints": {**array(span), "minItems": 1},
            "exceptions": array(span), "approach": span, "outcome": span,
            "success": {"type": "boolean"},
        }))
    fields["memories"] = array({"type": "object", "additionalProperties": True})
    return record(fields)


def compact_pattern(entry, messages):
    """Message ids address only the transcript actually offered in this extraction."""
    kind = entry.get("kind")
    if kind == "shared_term" and "definition_id" in entry and entry.get("shorthand") is None:
        definition_id = entry["definition_id"]
        definition = messages.get(definition_id) if type(definition_id) is int else None
        adopter_id = entry.get("adoption_id")
        adopter = messages.get(adopter_id) if type(adopter_id) is int else None
        if definition and adopter:
            entry["shorthand"] = {
                "term": entry.get("term"), "meaning": entry.get("meaning"),
                "definition": {"message_id": definition.id, "quote": definition.content},
                "adoption": {"message_id": adopter.id, "quote": adopter.content},
                **{k: entry[k] for k in ("scope", "scope_quote", "expires_on", "revises_memory_id")
                   if k in entry},
            }
    if kind != "strategy" or entry.get("strategy") is not None or "goal" not in entry:
        return

    def span(ref):
        if not isinstance(ref, dict) or type(ref.get("message_id")) is not int:
            raise ValueError
        row = messages.get(ref["message_id"])
        literal = ref.get("text")
        if row is None or not isinstance(literal, str) or not literal:
            raise ValueError
        if literal not in row.content:
            literal = literal.strip().rstrip(".,;: ")
        if not literal or not re.search(r"\w", literal):
            raise ValueError
        if literal not in row.content:
            # A sentence-initial capital is often lowercased in the proposed field.
            initial = literal[:1].upper() + literal[1:]
            if not row.content.startswith(initial):
                raise ValueError
            literal = initial
        return {"text": literal, "quote": row.content, "message_id": row.id}

    try:
        entry["strategy"] = {
            "goal": span(entry["goal"]), "approach": span(entry["approach"]),
            "constraints": [span(ref) for ref in entry["constraints"]],
            "exceptions": [span(ref) for ref in entry.get("exceptions", [])],
            "outcome": {**span(entry["outcome"]), "success": entry["success"]},
        }
    except (KeyError, ValueError, TypeError):
        return


_NAME = re.compile(r"\b(?:let['’]s (?:always )?call|we(?:['’]ll| will) call|"
                   r"by .{1,60} (?:I|we) mean|let['’]s name|we call it)\b", re.I)
_PLAN = re.compile(r"\b(?:schedul\w*|book(?:ed|ing)?|appointment|plan(?:ned|ning)? to|"
                   r"agreed to (?:do|hold|attend|meet|start))\b", re.I)
_DECLINE_NAME = re.compile(
    r"(?:no[, ]+)?(?:i['’]d rather |let['’]s |please )?(?:leave|keep) "
    r"(?:it|this|that) (?:unnamed|without a name)[.!]?", re.I,
)


def one_off_naming_refusal(candidate, messages):
    if candidate.get("kind") != "preference":
        return False
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    for item in evidence:
        if not isinstance(item, dict) or type(item.get("message_id")) is not int:
            return False
        row = messages.get(item["message_id"])
        if row is None or row.role != "user" or not _DECLINE_NAME.fullmatch(row.content.strip()):
            return False
    return True


def naming_only_plan(candidate, messages, adopted_names=()):
    """A naming utterance on its own supplies no undertaking to do the named activity."""
    named = [term.casefold() for term in adopted_names]
    content = candidate["content"]
    reports_naming = named and re.search(r"\b(?:call|name|naming|term|label)\b", content, re.I)
    if (candidate.get("temporal_type") != "future_plan" and not _PLAN.search(content)
            and not reports_naming):
        return False
    evidence = candidate.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return False
    for item in evidence:
        if not isinstance(item, dict) or type(item.get("message_id")) is not int:
            return False
        row = messages.get(item["message_id"])
        if row is None:
            return False
        clauses = re.split(r"[.!?;\n]+|\s+and\s+", row.content, flags=re.I)
        if any(c.strip() and not (_NAME.search(c) or re.fullmatch(
            r"\s*(?:yes|agreed|yes,? let['’]s use that name)\s*", c, re.I,
        )) for c in clauses):
            return False
    # "Let's call the dentist" is itself a plan. Require a known shared name
    # or a quoted label that appears in the naming utterance.
    terms = named + re.findall(r"""["'“‘]([^"'”’]{2,60})["'”’]""", content.casefold())
    return any(_NAME.search(row.content) and any(t in row.content.casefold() for t in terms)
               for item in evidence if (row := messages.get(item["message_id"])))
