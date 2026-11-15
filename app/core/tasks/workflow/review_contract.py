"""Fail-closed review admission: full answer coverage and exact source provenance."""
import json
import re


def normalized(text):
    return " ".join(str(text).split())


def answer_parts(text):
    """Index complete visible sentences/lines so the reviewer need not rewrite them."""
    return [part.strip() for part in re.split(r"(?<=[.!?;])\s+|\n+", text) if part.strip()]


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
    if all(type(claim.get("part")) is int for claim in claims):
        if {claim["part"] for claim in claims} != set(range(len(answer_parts(findings)))):
            return False, "Completion review must cover every indexed answer part."
    elif (any(not isinstance(claim.get("text"), str) or not claim["text"].strip()
              for claim in claims)
          or normalized(" ".join(claim["text"] for claim in claims)) != normalized(findings)):
        # Older in-flight reviewers may still return exact text coverage.
        return False, "Completion review must cover every clause of the proposed answer."
    sources = json.loads(messages[1]["content"])["evidence"]
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
        if not any(source.get("task_id") == cid and source.get("status", "done") == "done"
                   and quote in str(source.get("content", source.get("result", "")))
                   for source in sources):
            return False, "A completion quotation is absent from the reviewed owned source."
    return True, ""
