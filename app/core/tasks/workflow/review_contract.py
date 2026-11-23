"""Fail-closed review admission: full answer coverage and exact source provenance."""
import json
import re

from app.core.tasks.search_evidence import packed_fields


def normalized(text):
    return " ".join(str(text).split())


def answer_parts(text):
    """Index complete visible sentences/lines so the reviewer need not rewrite them."""
    return [part.strip() for part in re.split(r"(?<=[.!?;])\s+|\n+", text) if part.strip()]


def extractive_parts(parts, value):
    """Indices covered by the complete field, including qualifications in later sentences."""
    sentences = [normalized(part) for part in answer_parts(value)]
    count = len(sentences)
    if not count:
        return set()
    covered = set()
    for start in range(len(parts) - count + 1):
        if parts[start:start + count] == sentences:
            covered.update(range(start, start + count))
    return covered


SEARCH_PROOF_RULE = (
    "For a search hit, reproduce the complete relevant title, snippet or URL verbatim as "
    "a standalone answer line. For synthesis or paraphrase, inspect an underlying source. "
    "If the hits do not answer the question, report the gap."
)


def _price_literals(text):
    # Use only unambiguous written currency markers. Derived amounts can appear
    # in findings without being present on any individual page.
    number = r"\d+(?:[.,]\d+)*"
    marker = r"EUR|GBP|USD|CAD|AUD|CHF|JPY|€|£|\$|¥"
    matches = re.finditer(
        rf"(?<!\w)({marker})\s*({number})(?!\w)|(?<!\w)({number})\s*({marker})(?!\w)",
        text, re.I,
    )
    amounts = set()
    for match in matches:
        currency, value = (match[1], match[2]) if match[1] else (match[4], match[3])
        currency = {"€": "EUR", "£": "GBP"}.get(currency, currency.upper())
        amounts.add((currency, value))
    return amounts


def validate_review(result, usage, findings, contract, messages, *, partial=False):
    if getattr(usage, "done_reason", None) in {"length", "max_tokens"}:
        return False, "Completion review was truncated."
    if not isinstance(result, dict) or result.get("supported") is not True:
        return False, "Completion review did not support the answer."
    if not isinstance(result.get("missing"), list) or result["missing"]:
        return False, "Completion review lists missing proof or omits the missing-proof field."
    criteria = contract.get("criteria") or [contract.get("objective", "")]
    checks = result.get("criteria")
    if (not isinstance(checks, list) or len(checks) != len(criteria)
            or any(not isinstance(item, dict) or type(item.get("index")) is not int
                   or type(item.get("supported")) is not bool
                   or (not partial and item["supported"] is not True) for item in checks)
            or {item["index"] for item in checks} != set(range(len(criteria)))):
        return False, "Completion review must support every requested criterion."
    claims = result.get("claims")
    if (not isinstance(claims, list) or not claims or len(claims) > 48
            or any(not isinstance(claim, dict) for claim in claims)):
        return False, "Completion review must cover every clause of the proposed answer."
    for check in checks:
        if not check["supported"]:
            continue
        indices = check.get("claim_indices")
        if indices is None and len(criteria) == 1:
            # With a single criterion all accepted claims already belong to it.
            continue
        if (not isinstance(indices, list) or not indices
                or any(type(i) is not int or not 0 <= i < len(claims) for i in indices)):
            return False, "Each supported criterion needs references to its supporting claims."
    if all(type(claim.get("part")) is int for claim in claims):
        if {claim["part"] for claim in claims} != set(range(len(answer_parts(findings)))):
            return False, "Completion review must cover every indexed answer part."
    elif (any(not isinstance(claim.get("text"), str) or not claim["text"].strip()
              for claim in claims)
          or normalized(" ".join(claim["text"] for claim in claims)) != normalized(findings)):
        # Older in-flight reviewers may still return exact text coverage.
        return False, "Completion review must cover every clause of the proposed answer."
    sources = json.loads(messages[1]["content"])["evidence"]
    parts = [normalized(part) for part in answer_parts(findings)]
    for claim in claims:
        cid, quote = claim.get("task_id"), claim.get("quote")
        if type(cid) is not int or not isinstance(quote, str) or not quote.strip():
            return False, "Every factual claim needs an owned source quotation."
        if any(source.get("task_id") == cid
               and source.get("search_coverage", {}).get("returned") == 0 for source in sources):
            return False, "An empty search provides no factual source for this answer."
        for source in sources:
            proof = source.get("postcondition") or {}
            if (source.get("task_id") == cid and proof.get("requested_resource")
                    and proof["requested_resource"] != proof.get("resource")):
                return False, "A cited result belongs to a different resource than requested."
        owned = [source for source in sources
                 if source.get("task_id") == cid and source.get("status", "done") == "done"]
        if any("search_coverage" in source for source in owned):
            part = claim.get("part")
            if type(part) is not int:
                return False, "A search quotation needs its answer part index."
            fields = set()
            for source in owned:
                for field_id, value in packed_fields(source.get("content", "")):
                    if quote in value and part in extractive_parts(parts, value):
                        fields.add(field_id)
            field = claim.get("field")
            if field is None and len(fields) == 1:
                claim["field"] = fields.pop()
            elif not isinstance(field, str) or field not in fields:
                return False, SEARCH_PROOF_RULE
        elif not any(quote in str(source.get("content", source.get("result", "")))
                     for source in owned):
            return False, "A completion quotation is absent from the reviewed owned source."
    prices = _price_literals(findings)
    if len(prices) >= 2:
        observed = set().union(*(_price_literals(str(source.get("content", "")))
                                 for source in sources if source.get("status", "done") == "done"))
        quoted = set().union(*(_price_literals(claim["quote"]) for claim in claims))
        if missing_prices := (prices & observed) - quoted:
            absent = ", ".join(f"{currency} {value}" for currency, value in sorted(missing_prices))
            return False, f"Include a source quotation for each listed price. Missing: {absent}."
    return True, ""
