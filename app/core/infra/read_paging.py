"""Revision-bound JSON pages for read-only tool records."""
import hashlib
import json


def json_page(value, *, content_offset=0, revision=None, chars=6000):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    key = hashlib.sha256(text.encode()).hexdigest()
    if (revision is not None and revision != key) or content_offset and not revision:
        raise ValueError("Record changed or revision missing; restart at content_offset 0.")
    if not 0 <= content_offset <= len(text):
        raise ValueError("content_offset is outside this record.")
    end = min(len(text), content_offset + chars)
    complete = content_offset == 0 and end == len(text)
    return {"record": value if complete else None, "revision": key,
            "content_offset": content_offset, "is_fragment": not complete,
            "details_fragment": None if complete else text[content_offset:end],
            "fragment_encoding": "json", "next_offset": end if end < len(text) else None}
