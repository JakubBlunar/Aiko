"""Let the chat model read the material before composing a requested opinion."""
import json
import logging
import re

from app.core.conversation.clarification_reply import IMPRESSION_GUIDANCE, REVISION_GUIDANCE


log = logging.getLogger(__name__)
_IMPRESSION = (
    'Answer in JSON: {"quote": "words FROM THE WORK or null if absent", "reply": "your own '
    'choice and why, in one sentence"}. An opinion about a draft is not the draft. '
    "With no material to read, ask to see it. No tags."
)
_REVISION = (
    'Return {"quote":"exact words of the new detail","reply":"your own short answer"}. '
    "Did the new condition apply to only one option? Then you saw the outcome, but cannot "
    "isolate its cause. If both shared it, it cannot explain the difference. A personal "
    "favorite only tells you about that person. Keep observed results; never invent "
    "audience motives. No tags."
)
_ONLY_CONDITION = re.compile(r"\bonly (?:the|one)\b[^.!?]{1,65}?\b"
                             r"((?:had|was|were|got|offered|received)\s+[^.!?]{1,80})[.!?]", re.I)
_QUOTED_WORK = re.compile(r'“([^”\n]+)”|"([^"\n]+)"')
_ORDINAL_VOTE = re.compile(r"\s*I (?:like|prefer) the (?:first|second)(?: one)?[.!]\s*"
                           r"(?=Which\b)", re.I)
_VOTE_ONLY = re.compile(r"(?:I (?:like|prefer) )?(?:the )?(?:draft |version )?"
                        r"(?:first|second|one|two|1|2)(?: one)?[.!]?", re.I)
_BARE_PICK = re.compile(r"which (?:one )?(?:do|would) you (?:personally )?"
                        r"(?:keep|pick|like|prefer)(?:,? and why)?\?", re.I)


def read_opinion(client, messages, guidance, *, model, options, stop_event):
    """Use the ordinary persona and roles. Fall back to normal chat on an incomplete reading."""
    if stop_event.is_set() or guidance not in (IMPRESSION_GUIDANCE, REVISION_GUIDANCE):
        return ""
    if not messages:
        return ""
    head, *turns = messages
    if head["role"] != "system" or not turns or turns[-1]["role"] != "user":
        return ""
    # Put the response contract next to the material. It occupies the space already
    # budgeted for this turn's T6 instruction; all earlier cache breakpoints survive.
    before, found, after = head["content"].rpartition(guidance)
    latest = turns[-1]["content"]
    if not isinstance(latest, str) or turns[-1].get("images"):
        return ""
    contract = _IMPRESSION if guidance == IMPRESSION_GUIDANCE else _REVISION
    condition = _ONLY_CONDITION.search(latest.partition("?")[0])
    if guidance == REVISION_GUIDANCE and condition:
        predicate = json.dumps(condition[1].strip(), ensure_ascii=False)
        contract = (
            'Return {"quote":"exact clause starting with only","weight":"weaker, same, or '
            'unclear","reply":"your short answer"}. Weigh the earlier explanation. Does '
            f"{predicate} offer another explanation for the observed result? Weaker means "
            "the observation can no longer distinguish the two. Same means the new detail "
            "does not bear on the old reason. No tags."
        )
    if not found or len(contract) + 2 > len(found):
        return ""
    works = list(_QUOTED_WORK.finditer(latest)) if guidance == IMPRESSION_GUIDANCE else []
    if len(works) == 2:
        # Only hide a standalone ordinal vote in this reading. Reasons, goals and
        # constraints still matter. The recorded exchange always keeps the original.
        end = works[-1].end()
        vote = _ORDINAL_VOTE.match(latest, end)
        if vote and _BARE_PICK.fullmatch(latest[vote.end():].strip()):
            latest = latest[:end] + " " + latest[vote.end():]
    prompt = [{**head, "content": before + after}, *turns[:-1],
              {**turns[-1], "content": latest + "\n\n" + contract}]
    try:
        raw = "".join(client.chat_stream(prompt, model=model, options=options,
                                         stop_event=stop_event, format_json=True,
                                         surface="opinion_read"))
        if stop_event.is_set() or client.last_usage.done_reason != "stop":
            return ""
        result = json.loads(raw)
    except (ValueError, RuntimeError, OSError) as exc:
        log.debug("Opinion reading unavailable: %s", type(exc).__name__)
        return ""
    if not isinstance(result, dict):
        return ""
    quote, reply = result.get("quote"), result.get("reply")
    if not isinstance(reply, str) or not 1 <= len(reply.strip()) <= 700 or "[[" in reply:
        return ""
    if quote is None:
        if (guidance == IMPRESSION_GUIDANCE and not works and "quote" in result
                and reply.strip().endswith("?")
                and re.search(r"\b(?:see|read|hear|send|share|paste|show)\b", reply, re.I)):
            return reply.strip()
        return ""
    if (guidance == IMPRESSION_GUIDANCE and not works and isinstance(quote, str)
            and _VOTE_ONLY.fullmatch(quote.strip())):
        if (len(turns) == 1 and quote.strip() in latest
                and re.search(r"\b(?:drafts?|openings?)\b", latest, re.I)):
            return "Can I read both before I pick?"
        return ""
    sources = [m["content"] for m in turns if m["role"] == "user"]
    if guidance == REVISION_GUIDANCE:
        if isinstance(quote, str) and quote.rstrip().endswith("?"):
            return ""
        sources = [latest]
    elif len(works) == 2:
        sources = [m[1] or m[2] for m in works]
    if not isinstance(quote, str) or not 3 <= len(quote.strip()) <= 300 or not any(
        isinstance(s, str) and quote.strip() in s for s in sources
    ):
        return ""
    if (guidance == REVISION_GUIDANCE and condition and result.get("weight") == "weaker"
            and condition[1].strip() in quote and "[[" not in condition[0]):
        # The model judged the evidence weaker. Keep its explanation tied to the actual
        # unequal condition: "free" has repeatedly turned into invented locked doors.
        clause = condition[0][0].lower() + condition[0][1:].rstrip(".!?")
        return (f"Knowing that {clause} makes me less sure. I'd want to see how the two "
                "compare on equal terms before putting it down to my first explanation.")
    return reply.strip()
