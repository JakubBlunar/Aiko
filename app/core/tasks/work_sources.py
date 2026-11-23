"""Read the sources of a saved answer, without dispatching tools or inference."""

import re
from urllib.parse import urlsplit

from app.core.tasks.search_evidence import returned_hits
from app.core.tasks.work_evidence import certificate_current, evidence_record, owned_records
from app.core.tasks.work_evidence import result_text
from app.core.tasks.work_outcome import certified_answer


def is_source_question(text):
    return bool(re.fullmatch(
        r"(?:where did you (?:find|get|read) [^?\n]{1,140}|where did that come from|"
        r"what (?:was|is|were|are) (?:your|the) sources?(?: for [^?\n]{1,100})?|"
        r"(?:show|give) me (?:your|the) sources?|sources?)[?!. ]*",
        text.strip(), re.IGNORECASE,
    ))


def _public_link(value):
    if not isinstance(value, str) or len(value) > 2048 or any(c.isspace() for c in value):
        return None
    try:
        url = urlsplit(value)
        if url.scheme in {"https", "http"} and url.hostname and url.username is None:
            return value
    except ValueError:
        pass
    return None


def answer_sources(orch, row, *, passage=None, text=None, claims=None, delivered=None):
    """Return certificate quotations and the locator belonging to that exact result or hit."""
    response = {"task_id": row.id, "status": "unavailable", "sources": []}
    if not certified_answer(row.result):
        return response
    check = row.result["verification"]["certificate"]
    from app.core.tasks.workflow.review_contract import answer_parts

    parts = answer_parts(row.result.get("content", "") if text is None else text)
    if claims is None:
        claims = check.get("claims")
    if not isinstance(claims, list) or not claims:
        return response
    steps = row.result.get("steps") or []
    if not certificate_current(check, owned_records(orch, row.id, steps)):
        return response
    seen = {}
    missing = False
    for claim in claims:
        if not isinstance(claim, dict):
            missing = True
            continue
        index = claim.get("part")
        statement = (parts[index] if type(index) is int and 0 <= index < len(parts)
                     else claim.get("text"))
        if not isinstance(statement, str):
            statement = None
        if delivered is not None and (not statement or statement not in delivered):
            missing = True
            continue
        if passage and (not statement or passage.casefold() not in statement.casefold()):
            continue
        cid, quote = claim.get("task_id"), claim.get("quote")
        if type(cid) is not int or not isinstance(quote, str) or not quote.strip():
            missing = True
            continue
        child = orch.get(cid)
        if (not child or child.parent_task_id != row.id or child.user_id != row.user_id
                or child.status != "done"
                or evidence_record(child)["revision"] != check["sources"].get(str(cid))):
            return {**response, "sources": []}
        field = claim.get("field")
        if field is not None and not isinstance(field, str):
            missing = True
            continue
        item = {"task_id": cid, "quote": quote, "recorded_at": child.completed_at}
        hits = returned_hits(child)
        if hits is not None:
            match = re.fullmatch(r"([1-9][0-9]{0,4})\.(title|snippet|url)", str(field))
            if not match or int(match[1]) > len(hits):
                missing = True
                continue
            hit = hits[int(match[1]) - 1]
            if (not isinstance(hit, dict) or not isinstance(hit.get(match[2]), str)
                    or quote not in hit[match[2]]):
                missing = True
                continue
            item.update(field=field, title=hit.get("title"), url=_public_link(hit.get("url")))
        else:
            text = result_text(child)
            view = "rendered"
            if quote not in text:
                artifacts = getattr(orch, "result_artifacts", None)
                result = child.result or {}
                projection = result.get("planner_context")
                pointers = {
                    "source": result.get("source_artifact"),
                    "projection": projection.get("artifact") if isinstance(projection, dict)
                    else None,
                }
                for view in ("source", "projection"):
                    pointer = pointers[view]
                    if not isinstance(pointer, dict) or not pointer.get("revision"):
                        continue
                    artifact = artifacts.read(row.id, cid, view=view) if artifacts else None
                    if (artifact and artifact["revision"] == pointer["revision"]
                            and quote in artifact["content"]):
                        text = artifact["content"]
                        break
                else:
                    missing = True
                    continue
            item.update(title=child.title, view=view, offset=text.index(quote))
            proof = (child.result or {}).get("postcondition") or {}
            if isinstance(proof, dict) and proof.get("requested_resource") == proof.get("resource"):
                item["url"] = _public_link(proof.get("resource"))
        identity = (cid, field, quote)
        if identity not in seen:
            item["supports"] = []
            response["sources"].append(item)
            seen[identity] = item
        if statement and statement not in seen[identity]["supports"]:
            seen[identity]["supports"].append(statement)
    if response["sources"]:
        response["status"] = "partial" if missing else "ok"
    return response


def source_reply(result):
    if not result["sources"]:
        return "I couldn't trace that answer to a current saved source."
    lines = []
    for source in result["sources"]:
        title = source.get("title") or "Saved result"
        if title == source.get("url"):
            title = "Source page"
        label = f"{title} (result #{source['task_id']})"
        line = f"{label}\n“{source['quote']}”"
        if source.get("url"):
            line += "\n" + source["url"]
        if sum(map(len, lines)) + len(line) > 10500:
            lines.append("More source detail is available in the saved work.")
            break
        lines.append(line)
    if result["status"] == "partial":
        lines.append("I could only trace part of the answer to its saved sources.")
    return "\n\n".join(lines)
