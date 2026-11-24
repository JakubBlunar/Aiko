"""Short original remarks used to answer explicit questions about shared history."""
from datetime import timedelta
import re

from app.core.conversation.shared_retrospective import excerpt, original_rows
from app.core.infra import timephrase


_STOP = frozenset(("and any are but can did for get had has her him his how its may new nor not "
                   "now one our out say she the too two use was who why you "
                   "about actually after again another anything back been being better both "
                   "changed choose could different does enjoy ever favorite favourite feel felt "
                   "first from going have into just kind know like liked more most much myself "
                   "only other over personally pick prefer really still taste than that their "
                   "them then there these they thing think this those time together want were "
                   "what when which will with would your yours").split())
OWN_REASON = re.compile(
    r"(?:^|[.!?]\s+)(?:honestly,? |actually,? |for me,? )?i(?:['’]d rather|['’]ve "
    r"(?:come to|always)| (?:still |usually |especially |really |always |only |particularly |"
    r"used to |didn['’]t |don['’]t )?(?:prefer\w*|loved?|lik(?:e|ed)|enjoy\w*|"
    r"want(?:ed)?|miss(?:ed)?|cared?|found|find|keep coming back))\b", re.I)


def terms(text):
    words = re.findall(r"[^\W\d_]{3,}", text.casefold())
    return {w.removesuffix("s") for w in words if w not in _STOP}


def remarks(db, owner, key, current_id, *, start=None, end=None):
    now = timephrase.now()
    rows = original_rows(db, owner, key, current_id, start or now - timedelta(days=180), end or now)
    # Preserve qualifiers at the end of a remark. Skip a long or control-bearing message
    # instead of silently cutting its meaning in half when formatting evidence.
    return [r for r in rows if len(r["content"]) <= 380 and "[[" not in r["content"]
            and "<|" not in r["content"]]


def near(left, right):
    return (left["session_id"] == right["session_id"]
            and abs(timephrase.parse_iso(left["created_at"]) - timephrase.parse_iso(
                right["created_at"])) <= timedelta(minutes=15))


def evidence(rows):
    zone = timephrase.now().tzinfo
    return "\n".join(excerpt(r, zone) for r in rows) or "(no matching original exchange)"
