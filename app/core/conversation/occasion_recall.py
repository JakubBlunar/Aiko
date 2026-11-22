"""Compare two recent user reports when asked how often a named incident happened."""
from datetime import timedelta
import re

from app.core.conversation.occasion_review import compare_accounts, read_account, review_pair
from app.core.infra import timephrase


COUNT = re.compile(
    r"how many (?:separate )?times did (?:the |my )?([a-z]+(?: [a-z]+){0,3}) ([a-z]+)\??", re.I,
)
DAY = r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
DOUBT = re.compile(
    r"\b(?:if|whether|maybe|perhaps|might|would|will|could|never|not|"
    r"no|another|different|twice|always|usually|sometimes|every|except|although|but|and|or|"
    r"unless|instead|thought|supposed|\w+n['’]t)\b|[?;\"“]", re.I,
)


def _relation(subject, verb, first, later):
    stem = re.escape(verb.removesuffix("e"))
    past = rf"(?:{stem}(?:{stem[-1]})?ed|{re.escape(verb)})"
    named = rf"(?:my|the) {re.escape(subject)}"
    intro = rf"(?:on {DAY}[, ]+)?"
    if (DOUBT.search(first) or DOUBT.search(later)
            or not re.fullmatch(rf"{intro}{named} {past}(?: (?:on|at|during|while|when|in) "
                                rf"[\w '’-]+)?[.!]?", first, re.I)
            or re.search(r"\b(?:again|once more|second time)\b", first, re.I)):
        return None
    if re.fullmatch(rf"{intro}{named} {past} (?:again|a second time|once more)"
                    rf"(?: (?:on|at|during|after) [\w '’-]+)?[.!]?", later, re.I):
        return "distinct"
    # An explicit reference can use the object's head noun ("that chain problem").
    # Repairs are allowed only as the main assertion; no new fault is inferred.
    head = re.escape(subject.split()[-1])
    reference = rf"that (?:same )?(?:{re.escape(subject)}|{head}) (?:problem|incident|issue)"
    days = re.findall(rf"\b{DAY}\b", first, re.I)
    dated = [rf"{re.escape(day)}['’]s (?:{re.escape(subject)} )?{re.escape(verb)}"
             for day in days]
    dated.extend(rf"that same {re.escape(day)} {re.escape(verb)} of {named}" for day in days)
    anchor = "(?:" + "|".join([reference, *dated]) + ")"
    if re.fullmatch(rf"{intro}I (?:replaced|fixed|ordered|bought|paid for|got|installed) "
                    rf"[\w '’-]+ (?:for|after|because of) {anchor}[.!]?", later, re.I):
        return "same"
    if re.fullmatch(rf"{anchor} (?:delayed|cost|took) [\w '’-]+[.!]?", later, re.I):
        return "same"
    return None


def occasion_frequency(db, question, session_key):
    match = COUNT.fullmatch(question.strip())
    owner, separator, _ = session_key.partition(":")
    if not match or not owner or not separator or session_key.endswith(":noremember"):
        return None
    subject, verb = (part.casefold() for part in match.groups())
    head = subject.split()[-1]
    end = timephrase.utcnow()
    rows = db.messages_in_range((end - timedelta(days=14)).isoformat(), end.isoformat(),
                                limit=401, user_id=owner)
    # The archive scan is bounded. Never turn an incomplete window into a count.
    mentions = [row for row in reversed(rows)
                if row["role"] == "user" and row["session_id"].partition(":")[0] == owner
                and not row["session_id"].endswith(":noremember")
                and not COUNT.fullmatch(row["content"].strip())
                and re.search(rf"\b{re.escape(head)}\b", row["content"], re.I)]
    if len(mentions) < 2:
        return None
    uncertain = "I can't establish a count for that incident from the recent reports I found."
    if (len(rows) > 400 or len(mentions) != 2
            or mentions[0]["session_id"] == mentions[1]["session_id"]):
        return uncertain, []

    def read(row):
        return read_account(None, row["session_id"], row["message_id"], row["session_id"], db)

    accounts = [read(row) for row in mentions]
    if any(account is None or account.statement != row["content"]
           for account, row in zip(accounts, mentions, strict=True)):
        return uncertain, []
    first, later = accounts
    if first.message_id >= later.message_id:
        return uncertain, []
    relation = _relation(subject, verb, first.statement.strip(), later.statement.strip())
    review = review_pair(first, later, relation) if relation else None
    result = compare_accounts(*(read(row) for row in mentions), review)
    if result["occasion_count"] is None:
        return uncertain, []
    reply = "From the two recent reports I found, " + result["frequency_reply"].lower()
    if relation == "same":
        reply += f" Your later update was “{later.statement.strip()}”"
    return reply, [first.message_id, later.message_id]
