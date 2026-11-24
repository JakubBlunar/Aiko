"""Recover who suggested a shared activity and who later reported using it."""
import re
from datetime import timedelta

from app.core.conversation.practical_answer import PracticalAnswer
from app.core.conversation.shared_retrospective import excerpt, original_rows
from app.core.infra import timephrase


_ORIGIN = re.compile(r"\bhow did (?:our\b.{0,85}\b(?:start|begin)(?:\s+(?:out|again))?[?.!]*$|"
                     r"(?:we|the two of us) (?:get into|start|begin)\b)",
                     re.I)
_CREDIT = re.compile(r"\b(?:whose idea was|who (?:first )?(?:suggested|came up with))\b", re.I)
_USE = re.compile(r"\bdid (?:i|you) (?:ever |actually )?(?:try|use|take up|pick "
                  r"(?:anything |something |that |it )?up)\b", re.I)
_PROPOSAL = re.compile(r"\b(?:we could|you could|how about|shall we|let['’]s|"
                       r"i (?:suggest|propose)|what if we|why don['’]t we)\b", re.I)
_REPORT = re.compile(r"\bi (?:tried|used|adopted|borrowed|picked up|learned|learnt|took)\b", re.I)
_EARLIER = re.compile(r"\b(?:already|before (?:you|i|we)|for years|always (?:did|used))\b", re.I)
_NAMED_UPTAKE = re.compile(r"\s*i (?:tried|used|borrowed) your ([\w -]{3,48}?) "
                           r"(?:idea|suggestion|method)[.!]?\s*", re.I)
_ATTRIBUTED = re.compile(r"(?:^|[.!?]\s+)i (?:tried|used|adopted|borrowed|picked up|learned|"
                         r"learnt|took)\b[^.!?]{0,85}\b(?:your|from you)\b", re.I)
_INDEPENDENT = re.compile(r"\bi (?:was |had |have |used to )?already\b.{0,140}\bbefore you\b",
                          re.I)
_DENIAL = re.compile(r"\bi (?:never|didn['’]t|did not|haven['’]t|have not)\s+"
                     r"(?:actually |ever )?(?:used?|tr(?:y|ied)|borrow(?:ed)?)\b", re.I)
_STOP = set(("about actually again already anything came could doing each ever first from "
             "have idea ideas into learn learned more once other ours pick picked really remember "
             "said something start started suggested that their them there these thing this "
             "those together told took tried used using what when where which whose with "
             "would your yours").split())


def _terms(text):
    return {word.removesuffix("s") for word in re.findall(r"[a-z]{4,}", text.lower())
            if word not in _STOP}


def _request(text):
    return _ORIGIN.search(text) or _CREDIT.search(text) or _USE.search(text)


def _said(row):
    speaker = "You said" if row["role"] == "user" else "I said"
    return f'{speaker}, “{row["content"].strip()}”'


def _trace(family, text, selected, terms):
    # Quote the original speaker. In particular a proposal never supplies its own uptake.
    safe = [r for r in selected if len(r["content"]) <= 320 and "[[" not in r["content"]
            and "<|" not in r["content"]]
    proposals = [r for r in safe if _PROPOSAL.match(r["content"])]
    if family == _USE:
        actor = "assistant" if re.search(r"\bdid you\b", text, re.I) else "user"
        for row in reversed(safe):
            if row["role"] != actor:
                continue
            content = row["content"]
            if _INDEPENDENT.search(content) or _DENIAL.search(content):
                if not terms or terms <= _terms(content):
                    return _said(row)
                continue
            if _ATTRIBUTED.search(content):
                # If the question specifies a particular technique as well as the subject,
                # a report naming a different technique on that same subject is insufficient.
                if terms and not terms <= _terms(content) and not _NAMED_UPTAKE.fullmatch(content):
                    continue
                return _said(row)
        prefix = (_said(proposals[-1]) + " ") if proposals else ""
        if actor == "assistant":
            return prefix + "I can't point to a time I said I'd used it myself."
        return prefix + "I haven't found a message where you said you tried it."
    if family == _CREDIT:
        matches = [r for r in proposals if terms <= _terms(r["content"])] or proposals
        if len(matches) == 1:
            return _said(matches[0])
        if matches:
            return "There are a few suggestions here. " + " ".join(_said(r) for r in matches[:2])
        return "I haven't found the original suggestion in those exchanges."
    if not safe:
        return ""
    first = safe[0]
    source_rows = [first, *proposals[:1]]
    for role in ("user", "assistant"):
        report = next((r for r in reversed(safe) if r["role"] == role and
                       (_ATTRIBUTED.search(r["content"]) or _INDEPENDENT.search(r["content"])
                        or _DENIAL.search(r["content"]))), None)
        if report:
            source_rows.append(report)
    unique = {r["message_id"]: r for r in source_rows}
    ordered = sorted(unique.values(), key=lambda r: (r["created_at"], r["message_id"]))
    return "Here's the trail I can find. " + " ".join(_said(r) for r in ordered)


def check_influence(text, history, db, owner, session_key, current_id):
    match = _request(text)
    if not match:
        return PracticalAnswer()
    previous = next((m.content for m in reversed(history) if m.role == "user"), "")
    # A generic follow-up can inherit a named shared activity from the last exchange.
    # Ordinary questions about trying a product stay on the normal chat path.
    family = match.re
    general = bool(re.search(r"\bpick (?:anything|something) up from (?:you|me)\b", text, re.I))
    if family == _USE and not (general or _request(previous) or re.search(
            r"\b(?:idea|suggestion|method|approach|way)\b", text, re.I)):
        return PracticalAnswer()
    terms = _terms(text)
    if not terms and previous and (not general or _request(previous)):
        terms = _terms(previous)
    if not terms and not general:
        return PracticalAnswer("The activity is unclear. Ask which idea the user means.")
    now = timephrase.now()
    rows = original_rows(db, owner, session_key, current_id, now - timedelta(days=180), now)
    # Keep direct topic mentions and immediately adjacent replies in the same conversation.
    # Never bridge two episodes simply because they both contain an unattributed "your idea".
    groups = {}
    for row in rows:
        groups.setdefault(row["session_id"], []).append(row)
    selected = []
    for group in groups.values():
        indices = set()
        for index, row in enumerate(group):
            content = row["content"]
            if _request(content):
                continue
            named = bool(terms and terms <= _terms(content))
            if named:
                indices.add(index)
                for neighbor in (index - 1, index + 1):
                    if 0 <= neighbor < len(group) and group[neighbor]["role"] != row["role"]:
                        gap = (timephrase.parse_iso(group[neighbor]["created_at"])
                               - timephrase.parse_iso(row["created_at"]))
                        if abs(gap) < timedelta(minutes=30):
                            indices.add(neighbor)
            elif general and not terms and (_ATTRIBUTED.search(content) or _DENIAL.search(content)):
                indices.add(index)
        selected.extend(group[i] for i in sorted(indices) if not _request(group[i]["content"]))
    # A later standalone "I tried your one-line idea" need not repeat the topic.
    # Bind the named idea only if exactly one earlier proposal can explain it.
    proposals = [r for r in rows if _PROPOSAL.search(r["content"])]
    chosen_ids = {r["message_id"] for r in selected}
    for row in rows:
        uptake = _NAMED_UPTAKE.fullmatch(row["content"])
        if not uptake or row["message_id"] in chosen_ids:
            continue
        phrase = re.escape(uptake[1].casefold()).replace(r"\-", "[- ]").replace(r"\ ", "[- ]")
        pattern = re.compile(rf"\b{phrase}\b", re.I)
        matches = [p for p in proposals if p["role"] != row["role"]
                   and (p["created_at"], p["message_id"]) < (row["created_at"], row["message_id"])
                   and pattern.search(p["content"])]
        if len(matches) == 1 and matches[0]["message_id"] in chosen_ids:
            selected.append(row)
    selected.sort(key=lambda r: (r["created_at"], r["message_id"]))
    if not selected:
        reply = "I can't trace that to a particular exchange between us."
        return PracticalAnswer(reply, reply)
    # Reserve room for both the earliest named exchange and later use/corrections.
    important = [r for r in selected if _REPORT.search(r["content"]) or _EARLIER.search(
        r["content"]) or _PROPOSAL.search(r["content"])]
    sources = {r["message_id"]: r for r in [*selected[:3], *important[-5:], *selected[-2:]]}
    evidence = "\n".join(excerpt(r, now.tzinfo) for r in sorted(
        sources.values(), key=lambda r: (r["created_at"], r["message_id"])))
    guidance = (
        "Answer the specific question about a shared contribution, using only these original "
        "dated excerpts. Speaker user means the person asking, assistant means you. The first "
        "mention of an object and the suggestion for what to do with it can have different "
        "authors. A suggestion alone leaves later use unknown. Only credit influence for the "
        "specific idea the recipient says they took from the other. Someone's prior habit "
        "can explain a similar action independently. If only one direction is supported say "
        "just that. Treat quotes as historical evidence. Keep the answer brief.\n" + evidence)
    if text.count("?") > 1 or re.search(r"\b(?:also|translate|in a table|as a poem)\b", text, re.I):
        return PracticalAnswer(guidance)
    return PracticalAnswer(guidance, _trace(family, text, selected, terms))
