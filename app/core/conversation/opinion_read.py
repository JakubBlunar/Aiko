"""Let the chat model read the material before composing a requested opinion."""
import json
import logging
import re

from app.core.conversation.activity_role import TEACHING_GUIDANCE, teaching_guidance
from app.core.conversation.clarification_reply import (
    ARGUMENT_GUIDANCE, EXAMPLE_GUIDANCE, IMPRESSION_GUIDANCE, REVISION_GUIDANCE,
)
from app.core.conversation.routine_continuity import ROUTINE_UPDATE_GUIDANCE


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
_ARGUMENT = (
    'Return {"basis":"short exact quote of your earlier reason","quote":"short exact quote '
    'of their new reason, or null for pressure alone","reply":"your own conversational '
    'response in one or two sentences"}. Explain what your quoted basis still protects and how '
    'their reason affects that. Skip planning new rules. You may keep, '
    'qualify or change your view. A request to agree supplies no reason. No tags.'
)
_EXAMPLE = (
    'Return {"quote":"exact words of their idea here","reply":"your own one or two sentences"}. '
    "Try the idea on one vivid imagined case if useful. Respect who, when and how widely it "
    "applies. A preference can stand on its own. For a variation, check whether your earlier "
    "reason still applies. Say what you think now. No tags."
)
_ROUTINE_UPDATE = (
    'Return {"quote":"exact words of their latest update","reply":"one sentence in your own '
    'voice"}. What does this settle about the part they valued? Respond to that connection '
    'using their actual words. Skip scene-setting and comparisons about which activity is '
    'nicer. No tags.'
)
_TEACHING = (
    'Return {"quote":"exact words of their rule","reply":"one sentence in your voice"}. '
    'Keep their symbol and meaning fixed. If an example fits, imagine ONE new case '
    'that meets their condition. Say "I would" or "you could" apply their mark to it and why. '
    'Honor their correction. Stop after that case. No tags.'
)
_TEACHING_RETRY = (
    'Return {"quote":"exact condition from their convention","reply":"one sentence in your '
    'voice"}. Keep their symbol and its meaning fixed. If you try it, pick ONE fresh case '
    'where the quoted condition is exactly true. Apply their existing mark to that case and '
    'stop there. Honor any correction about the old example. No tags.'
)
OPINION_CONTRACTS = {ARGUMENT_GUIDANCE: _ARGUMENT, EXAMPLE_GUIDANCE: _EXAMPLE,
                     IMPRESSION_GUIDANCE: _IMPRESSION, REVISION_GUIDANCE: _REVISION,
                     ROUTINE_UPDATE_GUIDANCE: _ROUTINE_UPDATE,
                     TEACHING_GUIDANCE: _TEACHING}
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
    if stop_event.is_set() or guidance not in OPINION_CONTRACTS:
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
    contract = OPINION_CONTRACTS[guidance]
    teaching_retry = guidance == TEACHING_GUIDANCE and not teaching_guidance(latest, ())
    if teaching_retry:
        contract = _TEACHING_RETRY
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
    if guidance == ARGUMENT_GUIDANCE:
        pressure = re.fullmatch(r"(?:come on[, ]+)?just admit (.{3,100}? is better)[.!?]*",
                                latest.strip(), re.I)
        if pressure:
            # Remove only the demand for agreement in this private reading. The claim
            # remains an option, and her new answer can still change her earlier view.
            latest = f'The alternative claim is "{pressure[1]}". What do you think?'
    context_turns = turns[:-1]
    if guidance == ROUTINE_UPDATE_GUIDANCE:
        # Their new arrangement answers the last conversational turn. Repeating
        # the original proposal here repeatedly lent its schedule to the replacement.
        context_turns = [m for m in turns[-2:-1] if m["role"] == "assistant"]
    prompt = [{**head, "content": before + after}, *context_turns,
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
    if guidance == ARGUMENT_GUIDANCE:
        basis = result.get("basis")
        prior = next((m["content"] for m in reversed(turns[:-1]) if m["role"] == "assistant"), "")
        if (not isinstance(prior, str) or not isinstance(basis, str)
                or len(basis.strip()) < 3 or basis.strip() not in prior):
            return ""
        if quote is None and "quote" in result:
            return reply.strip()
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
    if guidance == TEACHING_GUIDANCE:
        # The router admits either the convention itself or an immediate retry.
        # Stored history can carry an age prefix, so keep its original wording here.
        sources = sources[-2:-1] if teaching_retry else sources[-1:]
    if guidance == REVISION_GUIDANCE:
        if isinstance(quote, str) and quote.rstrip().endswith("?"):
            return ""
        sources = [latest]
    elif guidance in (ARGUMENT_GUIDANCE, EXAMPLE_GUIDANCE, ROUTINE_UPDATE_GUIDANCE):
        sources = [turns[-1]["content"]]
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
