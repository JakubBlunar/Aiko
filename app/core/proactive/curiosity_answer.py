"""Evidence for settling an explicit curiosity question from a volunteered answer."""
import re

from app.core.memory.echo_detector import detect, tokens
from app.core.conversation.question_balance import yields_curiosity


_UNRESOLVED = re.compile(
    r"\b(?:don['’]?t know|do not know|no idea|not sure|unsure|unknown|"
    r"(?:can['’]?t|cannot|couldn['’]?t) (?:tell|remember|say)|"
    r"wonder(?:ing)?|maybe|might|probably|haven['’]?t checked|not (?:made|lined|padded)|"
    r"not (?:yet )?(?:known|sure)|no clue|if|suppose|imagine|i hope|would be)\b", re.I,
)
_MATERIAL = re.compile(r"\b(material|made (?:of|from)|fabric|lining)\b", re.I)
_MATERIAL_WORD = (
    r"cotton|linen|silk|satin|velvet|wool|steel|aluminum|aluminium|plastic|wood|"
    r"leather|fleece|foam|paper|brass|canvas|rubber|glass|nylon|polyester"
)
_DESCRIBED = re.compile(
    r"\b(?:made (?:of|from)|(?:lined|padded|woven|covered|filled|built) with)"
    r"\s+(?:an? |the )?(?!something\b|some material\b)[\w-]+|"
    rf"\b(?:{_MATERIAL_WORD}) (?:lining|interior)\b|"
    rf"\blining (?:is|was) (?:{_MATERIAL_WORD})\b", re.I,
)
_GENERIC = {"what", "which", "when", "where", "does", "made", "from", "material",
            "about", "would", "your", "their", "could", "lining", "fabric", "thing"}
_TIME_QUESTION = re.compile(r"\b(?:when|what (?:time|day|date))\b", re.I)
_TIME = re.compile(
    r"\b(?:\d{1,2}(?::\d{2}|\s*(?:am|pm))|(?:at|on) \d{1,2}|noon|midnight|"
    r"tomorrow|yesterday|tonight|(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|"
    r"december) \d{1,2})\b", re.I,
)
_COLOR = re.compile(r"\b(?:black|white|red|orange|yellow|green|blue|violet|purple|"
                    r"brown|grey|gray|pink|teal|navy|maroon|turquoise|magenta|beige)\b", re.I)
_COUNT = re.compile(r"\b(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|none)\b", re.I)
_REQUESTS = (
    ("color", re.compile(r"\b(?:colou?r|hue)\b", re.I)),
    ("count", re.compile(r"\b(?:how many|number of)\b", re.I)),
    ("time", _TIME_QUESTION),
    ("place", re.compile(r"\bwhere\b", re.I)),
    ("reason", re.compile(r"\bwhy\b", re.I)),
    ("material", _MATERIAL),
)


def _property(text):
    return next((kind for kind, pattern in _REQUESTS if pattern.search(text)), None)


def specific_ask(question, reply, *, subject=""):
    from app.core.concepts.hypothesis_expression import question_span

    span = question_span(question, reply)
    target = tokens(subject.split(" of ")[-1]) - _GENERIC
    # A related question about another object cannot authorize an elliptical reply.
    if target and not (target & tokens(question)) <= tokens(span):
        return ""
    requested = _property(question)
    if requested and _property(span) != requested:
        return ""
    return span


def volunteered_answer(subject, question, text, verdict):
    """Evaluate one speaker at a time so an object's name cannot certify another's answer."""
    if not verdict.echoed or not text:
        return False
    # Scope uncertainty to its sentence. "Maybe, it's lined with silk" is
    # uncertain throughout; a later "I wonder who made it" is independent.
    sentences = []
    for part in re.findall(r"[^.!?]+[.!?]?", text):
        if _UNRESOLVED.search(part) or part.endswith("?"):
            # A bare qualification retracts the preceding assertion.
            if re.fullmatch(r"\s*(?:(?:actually|well)[, ]+)?(?:i['’]m |i am )?"
                            r"(?:not (?:really )?sure|unsure|maybe)(?: though)?[.!]?\s*",
                            part, re.I):
                sentences.clear()
            sentences.append("")  # Prevent anaphora across an unresolved statement.
        else:
            clauses = re.split(r"[,;]|\band (?=(?:my|our|the|their)\b)", part, flags=re.I)
            sentences.extend(clause.strip() for clause in clauses if clause.strip())
    entity = tokens(subject.split(" of ")[-1]) - _GENERIC
    requested = _property(question)
    for index, sentence in enumerate(sentences):
        if not sentence:
            continue
        words = tokens(sentence)
        prior = sentences[index - 1] if index and re.match(
            r"^(?:it|they|its|the inside)\b", sentence, re.I,
        ) else ""
        mentioned = words | tokens(prior)
        if requested == "material":
            if not _DESCRIBED.search(sentence):
                continue
            if entity and entity <= mentioned:
                return True
            # With an anaphoric answer and no literal subject match, the semantic
            # signal can recover a paraphrased referent in the preceding statement.
            if prior and verdict.kind == "semantic" and not prior.endswith("?"):
                return True
        elif entity and entity <= mentioned:
            if requested == "color":
                for color in _COLOR.finditer(sentence):
                    before = sentence[:color.start()].strip()
                    after = sentence[color.end():].strip()
                    if (entity <= tokens(before) and re.search(
                        r"\b(?:is|was|are|were|looks?|in)\s*(?:a\s+)?(?:dark|light|bright)?$",
                        before, re.I,
                    )) or (after and after.split()[0].strip(".,!").lower() in entity):
                        return True
                continue
            if requested == "count":
                for count in _COUNT.finditer(sentence):
                    tail = sentence[count.end():].strip().split()
                    if tail and tail[0].lower().rstrip(".,!") in entity:
                        return True
                continue
            if requested == "reason":
                if re.search(r"\b(?:because|since|so that|reason)\b", sentence, re.I):
                    return True
                continue
            if requested == "time":
                if _TIME.search(sentence):
                    return True
                continue
            if requested == "place":
                if re.search(r"\b(?:at|in|near|inside|outside|beside|behind)\s+\w+",
                             sentence, re.I):
                    return True
                continue
            # For an unclassified question a copula and the subject alone
            # cannot distinguish an answer from an unrelated property.
            focus = tokens(question) - entity - _GENERIC
            if ((focus & words or "the answer is" in sentence.casefold())
                    and words - tokens(question) - tokens(subject)
                    and re.search(r"\b(?:is|was|are|were|have|has|prefer|chose|use|used)\b",
                                  sentence, re.I)):
                return True
    return False


def short_answer(question, text):
    """Allow short elliptical replies only after a verified ask of the same question."""
    # A compact answer can precede an explicit change of subject in the same turn.
    parts = re.split(r"[.!]\s+|[,;]\s*", text.strip(), maxsplit=1)
    if len(parts) == 2 and (yields_curiosity(parts[1]) or re.match(
        r"(?:do you )?want to (?:hear|know|see)\b", parts[1], re.I,
    )):
        text = parts[0]
    if _UNRESOLVED.search(text) or "?" in text:
        return False
    requested = _property(question)
    if requested == "reason":
        return bool(re.fullmatch(r"\s*because\s+[\w'’ -]+[.!]?\s*", text, re.I))
    if not re.fullmatch(r"[\w :'’-]{2,32}[.!]?", text.strip()):
        return False
    value = text.strip().rstrip(".!")
    if requested == "time":
        without_at = re.sub("^at ", "", value, flags=re.I)
        return bool(_TIME.fullmatch(value) or _TIME.fullmatch(without_at))
    if requested == "color":
        return bool(_COLOR.fullmatch(value))
    if requested == "count":
        return bool(_COUNT.fullmatch(value))
    if requested == "material":
        return bool(re.fullmatch(
            r"(?:(?:it['’]s|it is|mostly|pure|solid|soft) )?"
            rf"(?:{_MATERIAL_WORD})[.!]?",
            text.strip(), re.I,
        ))
    return False


def opens_on_question(question, text):
    """The opening addresses the last question and there is more to hear.

    This only selects a listening cue. Overlap cannot settle a question.
    """
    parts = re.split(r"[.!]\s+|\n+", text.strip(), maxsplit=1)
    if not question or len(parts) < 2 or not parts[1].strip():
        return False
    opening = parts[0]
    overlap = (tokens(question) & tokens(opening)) - _GENERIC
    return short_answer(question, opening) or bool(overlap)


def hold_seed_for_turn(row, text):
    """Read-only admission shared with the ledger's expired-but-unasked copies."""
    if not text:
        return False
    if yields_curiosity(text):
        return True
    question = str(row.payload.get("prompt_text") or row.text)
    if "?" not in question:
        return False
    verdict = detect(reply_tokens=tokens(text), item_text=row.subject, min_overlap=1)
    return volunteered_answer(row.subject, question, text, verdict)
