"""Keep echoed history and bare mood labels out of reply prefixes."""

from __future__ import annotations

import re

from app.core.affect.reactions import REACTIONS


_CLOCK = r"(?:[01]\d|2[0-3]):[0-5]\d"
_DAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
_REPLY_LABEL = re.compile(
    r"\[{1,2}[ \t]*(?:(?:just )?now|\d{1,4} (?:min|minutes?|hours?|days?) ago|"
    rf"(?:today|yesterday|{_DAY}|{_MONTH} \d{{2}}) {_CLOCK}"
    r"(?: \(\d{1,2}h(?: \d{1,2}m)? ago\))?)[ \t]*\]{1,2}"
    r"|\[{1,2}[ \t]*(?:" + "|".join(REACTIONS) + r")"
    r"(?:[ \t]*\+[ \t]*(?:" + "|".join(REACTIONS) + r"))*[ \t]*\]{1,2}",
    re.IGNORECASE,
)


def strip_history_age_prefix(text: str, *, streaming: bool = False) -> str:
    """Strip bare clock or known mood labels at the start of a reply.

    Quoted examples, inline mentions, code and Markdown links keep their text.
    During streaming, defer an unresolved leading bracket (at most 80 chars)
    and whitespace after a removed label. Otherwise a split ``[now]``
    leaks before it can be recognized, or changing offsets duplicates speech.
    A closed label at the very end waits for the next character so a following
    ``(url)`` can still establish that it is a Markdown link.
    """
    remaining = text
    while True:
        head = remaining.lstrip()
        match = _REPLY_LABEL.match(head)
        if match is None:
            if streaming and (
                not head
                or (head.startswith("[") and "]" not in head and len(head) <= 80)
            ):
                return ""
            return remaining
        tail = head[match.end():]
        if not tail:
            return ""
        if not tail[0].isspace():
            return remaining
        following = tail.lstrip()
        # A reference link may put whitespace between its two labels. Wait
        # for a split second label before deciding whether this is another
        # echoed age or a link whose visible text must survive.
        if following.startswith("[") and _REPLY_LABEL.match(following) is None:
            if streaming and "]" not in following and len(following) <= 80:
                return ""
            return remaining
        remaining = following
