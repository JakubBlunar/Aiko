"""A bounded, source-checked reading of a narrated misunderstanding."""
from __future__ import annotations

import json
import logging
import re


log = logging.getLogger(__name__)
STORY_HEADER = "[Narrated viewpoints]"
UNEQUAL_ACCESS = re.compile(
    r"\b(?:only\s+(?:told|showed|informed)|only\s+\w+\s+(?:heard|saw|knew|watched)|"
    r"(?:after|before|while)\s+[^.!?\n]{1,50}\b(?:left|went home|arrived|was away))\b", re.I,
)
_QUESTION = re.compile(
    r"\b(?:why|where|which|who|actual|expect\w*|knows?|knew|thought|thinks?|misunderst\w*|ignor\w*|"
    r"confus\w*|blam\w*|surpris\w*)\b", re.I,
)
_CORRECTED = re.compile(r"\s*(?:wait\b|correction\b|actually\b|I meant\b)", re.I)
_BLAME = re.compile(r"\b(?:ignor(?:e|ed|ing)|on purpose|deliberately|stood me up)\b", re.I)
_CONTRACT = (
    "For this turn return a JSON object. Read the latest user message in the context of their "
    "story. If this is another task or genuinely needs clarification, return {}. Otherwise "
    "give three fields in order: evidence (a short exact quote from the narrator identifying "
    "the decisive event; omit transcript labels), viewpoint (who learned what by the moment asked, "
    "including what "
    "remains uncertain), reply (your natural one- or two-sentence answer to the latest user). "
    "Use only the narrated facts. A promised call is still a plan. An unconfirmed message "
    "leaves receipt open. Repeating the news acknowledges it. A correction supersedes the "
    "old account: quote the correction itself as evidence when it changes the answer. "
    "For a question about the actual state give that state. Someone who followed the last "
    "instruction they received has a reason for their action. Name it gently. A person's "
    "reaction alone can have several explanations. Leave out "
    "offstage messages, motives and guesses about forgetfulness. Put only the spoken "
    "words in reply, without tags."
)


def story_question(text):
    return bool(_QUESTION.search(text or ""))


def refers_to_story(text, source):
    # Topic words and pronouns only decide whether to offer the reading. The
    # reader must still find the relevant evidence and can return an empty object.
    words = set(re.findall(r"\b\w{3,}\b", text.casefold()))
    words -= {"the", "was", "and", "they", "her", "who", "how", "did", "what", "for",
              "where", "would", "which", "that", "are", "with", "have", "there", "you",
              "told", "were", "could", "this", "will", "about", "know", "think"}
    return bool(words.intersection(re.findall(r"\b\w+\b", source.casefold()))
                or re.search(r"\b(?:she|he|they|her|him|them)\b", text, re.I))


def immediate_story_block(text, db, session_id):
    """Cover the first telling and the next question before the observer has run."""
    if not story_question(text):
        return ""
    if UNEQUAL_ACCESS.search(text):
        return STORY_HEADER + "\nUse the narrated events in the current user message."
    if db is None or "?" not in text or not re.search(
        r"\b(?:why|where|who|which|knows?|expect\w*|believ\w*)\b", text, re.I
    ):
        return ""
    rows = list(reversed(db.get_messages(session_id, limit=4)))
    if rows and rows[0].role == "user" and rows[0].content == text:
        rows = rows[1:]
    earlier = next((row.content for row in rows if row.role == "user"), "")
    if not UNEQUAL_ACCESS.search(earlier):
        return ""
    # A question about the reported people can precede the next worker tick.
    # The source remains in ordinary role-labelled history.
    if not refers_to_story(text, earlier):
        return ""
    return STORY_HEADER + "\nUse the narrator's recent account and the latest user message."


def story_contract(current):
    if isinstance(current, str):
        if _CORRECTED.match(current):
            return (
                "Read the newest user message first. It revises a story. Output one JSON object "
                "with person, evidence, status and reply. Person is the person asked about. "
                "Evidence must be copied exactly from the LATEST user message. Use status "
                "'withdrawn' when the narrator takes back or questions their report of telling "
                "this person and the person's expectation is now unknown. Use 'updated' for a "
                "new confirmed fact. Give the natural answer in reply. A different task gets {}."
            )
        elif _BLAME.search(current):
            return (
                "Read the user's story through the end. The disputed fact is receipt of the "
                "change. Output one JSON object with person, evidence, status and reply. Person "
                "is the person being blamed. Evidence is an exact short quote about whom the "
                "change reached. Use status 'missed' if only somebody else was told and no later "
                "handoff is reported, 'received' for an acknowledged update, 'unknown' otherwise. "
                "In reply explain the possible misunderstanding gently in one sentence. Receiving "
                "an update does not establish a motive. A different task gets {}."
            )
    return _CONTRACT


def reading_messages(messages):
    # Preserve the persona, permissions and original message roles.
    head, *rest = messages
    contract = story_contract(messages[-1]["content"])
    if head["role"] != "system":
        return [{"role": "system", "content": contract}, *messages]
    return [{**head, "content": head["content"] + "\n\n" + contract}, *rest]


def supported_reply(raw, messages):
    try:
        reading = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(reading, dict):
        return ""
    quote, reply = reading.get("evidence"), reading.get("reply")
    if not isinstance(quote, str):
        return ""
    quote = re.sub(r"^(?:Earlier narrated events in order \(quoted user account\):\s*)?"
                   r"\[[^\]\n]{1,40}\] User:\s*", "", quote.strip())
    # Some models quote both the original instruction and the later handoff.
    # Verify both separately when the joining word is outside the quotation.
    pair = re.fullmatch(r'["“]([^"”\n]+)["”]\s+and\s+["“]([^"”\n]+)["”]', quote.strip())
    spans = pair.groups() if pair else (quote.strip().strip('"“”').strip(),)
    if any(not 4 <= len(span) <= 500 or "[[" in span for span in spans):
        return ""
    if not isinstance(reply, str) or not 1 <= len(reply.strip()) <= 700 or "[[" in reply:
        return ""
    users = [m["content"] for m in messages[-8:] if m["role"] == "user"
             and isinstance(m.get("content"), str)]
    person = reading.get("person")
    if not isinstance(person, str) or not re.fullmatch(r"[^\W\d_]+(?:[-’'][^\W\d_]+)?", person):
        person = ""
    if (len(spans) == 1 and person and re.match(r"^(?:He|She|They) ", spans[0])
            and not any(spans[0] in content for content in users)):
        # Recover the narrator's actual sentence only when its entire predicate
        # follows the selected person's literal name in the same source.
        named = person + " " + spans[0].split(" ", 1)[1]
        if any(named in content for content in users):
            spans = (named,)
    if not all(any(span in content for content in users) for span in spans):
        return ""
    latest = users[-1]
    quote = next((span for span in reversed(spans) if span in latest), spans[-1])
    # Answer the evidential limit when the narrator explicitly says that receipt
    # is unknown. A later confirmation in the same account takes precedence.
    if (re.search(r"\b(?:sent|texted|emailed)\b", latest, re.I) and re.search(
        r"\b(?:can't|cannot|couldn't) (?:see|tell) (?:if|whether) (?:she|he|they) "
        r"(?:read|saw|opened) (?:it|the message)[.!]\s*does \w+ know\?\s*$", latest, re.I
    ) and not any(re.search(r"\b(?:acknowledged|repeated|confirmed)\b", row, re.I)
                  for row in users[:-1])):
        return "Possibly. You know the update was sent, but you can't tell whether they read it."
    if person and re.search(rf"\b{re.escape(person)}\b", latest, re.I):
        status = reading.get("status")
        name = re.escape(person)
        if _CORRECTED.match(latest):
            # A direct withdrawal followed only by the question supersedes even a
            # literal earlier quote selected by the model.
            withdrawn = re.search(
                rf"\bI (?:don't remember|can't recall) telling {name}\b[^.!?;]*[.!]\s*"
                rf"(?:where|what) (?:would|does|did|is) {name}\b[^.!?]*\?\s*$", latest, re.I)
            if withdrawn:
                return (f"You don't remember telling {person} yourself. From that alone, "
                        "it's unclear what they were expecting.")
            if status == "withdrawn" and quote in latest and person.casefold() in quote.casefold():
                return (f"With that correction, I'm unsure what {person} was expecting. "
                        f'You said, “{quote.rstrip(".")}.”')
        if _BLAME.search(latest):
            acknowledgment = re.search(
                rf"\b{name} (?:later |then )?(?:called and )?"
                r"(?:repeated [^.!?]{1,60} back|confirmed|acknowledged)\b", quote, re.I)
            if (quote in latest and acknowledgment
                    and not re.search(r"\b(?:not|never|if|might|didn't)\b", latest, re.I)):
                return (f'You said, “{quote.rstrip(".")}.” That sounds like {person} got the '
                        "update. What made them act differently is still open.")
            sole = re.search(r"\b(?i:only (?:told|informed|notified)) ([A-Z][a-z]+)"
                             r"\s*[.!?]", latest)
            if (status == "missed" and sole and sole[1].casefold() != person.casefold()
                    and not re.search(r"\b(?:told|heard|sent|called|read|replied|confirmed|"
                                      r"repeated|texted|passed)\b", latest[sole.end():], re.I)):
                return (f"It sounds like {person} may have been following the original plan. "
                        f"You mentioned telling {sole[1]} about the change.")
    if _CORRECTED.match(latest) and quote not in latest:
        return ""
    return reply.strip()


def read_story(client, messages, *, model, options, stop_event):
    """Buffer the short reading so only an admitted reply reaches speech or history."""
    if stop_event.is_set():
        return ""
    try:
        raw = "".join(client.chat_stream(
            reading_messages(messages), model=model, options=options, stop_event=stop_event,
            format_json=True, surface="story_perspective",
        ))
    except (ValueError, RuntimeError, OSError) as exc:
        log.debug("Story reading unavailable: %s", type(exc).__name__)
        return ""
    if stop_event.is_set() or client.last_usage.done_reason != "stop":
        return ""
    return supported_reply(raw, messages)
