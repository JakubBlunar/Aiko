"""Bind selected earlier user passages to the conversation at workflow handoff."""
import re

from app.core.infra import timephrase
from app.core.tasks.work_outcome import digest


def bind_references(db, session_key, anchor, origin, quotes):
    if (not isinstance(quotes, list) or len(quotes) > 4
            or any(not isinstance(q, str) or not 1 <= len(q.strip()) <= 600 for q in quotes)):
        raise ValueError("Use up to four short, exact identifying passages as reference_quotes.")
    count = re.search(r"\b(both|the two|the three|the four)\b[^.!?\n]{1,90}"
                      r"\bI (?:mentioned|listed)\b", origin, re.I)
    expected = {"both": 2, "the two": 2, "the three": 3, "the four": 4}
    if not quotes and not count:
        return []
    if not anchor:
        raise ValueError("I cannot bind those references to this request. Ask the user to "
                         "include the items in the current message.")
    history = db.get_messages_before(session_key, before_id=anchor.id, limit=40)
    references = []
    seen = set()
    spans = []
    for raw in quotes:
        quote = raw.strip()
        if len(quote) > 1 and (quote[0], quote[-1]) in {('"', '"'), ('“', '”')}:
            quote = quote[1:-1].strip()
        if quote == origin or quote in seen:
            continue
        matches = [r for r in history if r.role == "user" and quote and quote in r.content]
        if len(matches) != 1 or matches[0].content.count(quote) != 1:
            raise ValueError("A reference did not uniquely match an earlier user passage in "
                             "this chat. Use a more distinctive passage or ask which item.")
        row = matches[0]
        start = row.content.index(quote)
        if any(mid == row.id and start < end and begin < start + len(quote)
               for mid, begin, end in spans):
            raise ValueError("Use a separate identifying passage for each referenced item.")
        spans.append((row.id, start, start + len(quote)))
        excerpt = len(row.content) > 1000
        left = max(0, min(start - (1000 - len(quote)) // 2, len(row.content) - 1000))
        references.append({
            "message_id": row.id, "created_at": row.created_at,
            "message_revision": digest(row.content),
            "selected": quote, "excerpt": excerpt,
            "at_handoff": timephrase.format_transcript([{
                "role": row.role, "created_at": row.created_at,
                "content": row.content[left:left + 1000],
            }]),
        })
        seen.add(quote)
    if count and len(references) != expected[count[1].lower()]:
        raise ValueError("Include one distinct earlier passage for each requested item, "
                         "or ask the user which items they mean.")
    if not references:
        raise ValueError("The current request is already saved. Identify its earlier items "
                         "with distinctive passages or ask which ones were intended.")
    return references
