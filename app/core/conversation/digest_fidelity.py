"""Source-cited summary clauses with explicit time and representation scope."""
import json
import re
from datetime import timedelta

from app.core.infra import timephrase
from app.core.memory.memory_origin import memory_origin


RULE = """
Return {"claims":[{"text":"one complete short sentence", "source_ids":[ID],
 "scope":"history|current_state|intention|reported_pattern|interpretation|world_knowledge",
 "subject":"user|aiko|relationship|world"}]} with 0-4 clauses.
Every clause cites only shown source IDs. Distinguish historical episodes, a
fresh explicit ongoing state, unresolved intentions and explicitly reported
patterns. Many notes or promises do not establish recurrence. Do not describe
old symptoms as an ongoing condition or ignore a later report of resolution.
Recording span is not symptom duration. Aiko's reflections remain hers, virtual
events remain virtual, and public advice remains attributed world knowledge.
Keep qualifications and exceptions. Use dated historical wording when current
applicability is unknown. No hidden extra digest text: only these clauses are stored.
"""


def parse_claims(raw, rows, now):
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        value = json.loads(raw[start:end])
        claims = value.get("claims")
        if not isinstance(claims, list) or not 1 <= len(claims) <= 4:
            return None
        offered = {int(row.id): row for row in rows}
        result, validity = [], []
        for claim in claims:
            if not isinstance(claim, dict):
                return None
            text, ids = claim.get("text"), claim.get("source_ids")
            scope, subject = claim.get("scope"), claim.get("subject")
            if (not isinstance(text, str) or not 8 <= len(text) <= 600
                    or timephrase.has_relative_deictic(text)
                    or not isinstance(ids, list) or not 1 <= len(ids) <= 4
                    or any(type(mid) is not int or mid not in offered for mid in ids)
                    or not isinstance(scope, str) or scope not in {
                        "history", "current_state", "intention", "reported_pattern",
                        "interpretation", "world_knowledge"}
                    or not isinstance(subject, str) or subject not in {
                        "user", "aiko", "relationship", "world"}):
                return None
            sources = [offered[mid] for mid in ids]
            if scope != "current_state" and re.search(
                r"\b(?:ongoing state|is experiencing|continues to|still suffers|"
                r"persistent cycle)\b",
                text, re.I,
            ):
                return None
            if scope != "reported_pattern" and re.search(
                r"\b(?:often|always|usually|regularly|repeatedly)\b", text, re.I,
            ):
                return None
            origins = [memory_origin(row) for row in sources]
            allowed = {subject, "relationship"} if subject in {"user", "aiko"} else {subject}
            if subject == "relationship":
                allowed = {"user", "aiko", "relationship"}
            if any(origin.subject not in allowed for origin in origins):
                return None
            intentions = any(origin.mode == "intention"
                             or getattr(row, "temporal_type", None) == "future_plan"
                             for origin, row in zip(origins, sources, strict=True))
            if intentions and scope != "intention":
                return None
            if scope == "intention" and not intentions:
                return None
            if scope in {"history", "reported_pattern", "current_state"} and any(
                origin.mode not in {"observation", "declaration", "virtual_event"}
                for origin in origins
            ):
                return None
            if scope == "world_knowledge" and any(o.mode != "world_fact" for o in origins):
                return None
            if scope == "reported_pattern" and not any(re.search(
                r"\b(?:often|usually|regularly|repeatedly|always|every day)\b", row.content, re.I,
            ) for row in sources):
                return None
            if scope == "current_state":
                if any(getattr(row, "temporal_type", None) != "ongoing" or not _fresh(row, now)
                       for row in sources):
                    return None
                if _later_resolution(sources, rows):
                    return None
                validity.extend((timephrase.parse_iso(row.created_at) + timedelta(hours=24))
                                .isoformat() for row in sources)
            result.append({"text": text.strip(), "source_ids": sorted(set(ids)),
                           "scope": scope, "subject": subject})
        return {"version": 1, "claims": result, "valid_until": min(validity) if validity else None,
                "text": " ".join(item["text"] for item in result)}
    except (ValueError, TypeError, AttributeError, KeyError):
        return None


def _fresh(row, now):
    # Source recording is only an upper freshness bound, never an invented event time.
    recorded = timephrase.parse_iso(row.created_at)
    until = timephrase.parse_iso(getattr(row, "relevance_until", None))
    return bool(recorded and timedelta(0) <= now - recorded <= timedelta(hours=24)
                and (until is None or until >= now))


def _later_resolution(sources, rows):
    from app.core.memory.learned_strategies import words

    for source in sources:
        keys = words(source.content)
        # Exclude the actor name at the start of a conventional memory sentence.
        keys -= words(source.content.split()[0])
        recorded = timephrase.parse_iso(source.created_at)
        for row in rows:
            later = timephrase.parse_iso(row.created_at)
            origin = memory_origin(row)
            if (origin.subject != memory_origin(source).subject
                    or origin.mode not in {"observation", "declaration"}
                    or getattr(row, "temporal_type", None) == "future_plan"
                    or re.search(r"\b(?:not|hasn't|haven't|didn't|would|could|might|will|if)\b",
                                 row.content, re.I)):
                continue
            if (recorded and later and later > recorded and keys & words(row.content)
                    and re.search(r"\b(?:went away|resolved|recovered|no longer|ended)\b",
                                  row.content, re.I)):
                return True
    return False
