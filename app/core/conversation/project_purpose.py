"""Revisit an unfinished shared project in light of the user's stated reasons for starting."""
import re

from app.core.conversation.perspective_sources import evidence, near, remarks, terms
from app.core.conversation.practical_answer import PracticalAnswer


_ASK = re.compile(r"\b(?:did we (?:give up on|abandon|achieve what we|accomplish what we)|"
                  r"have we (?:given up on|abandoned)|(?:did|has) (?:it|(?:the|our) [\w -]{1,45}) "
                  r"(?:serve|served) its purpose|was (?:it|(?:the|our) [\w -]{1,45}) "
                  r"(?:a waste|worth doing|worth it))\b", re.I)
_ORIGIN = re.compile(
    r"(?:^|[.!?]\s+)we (?:started|began|set up|made|put together|created) "
    r"(?:our |the |a |an )?(?P<name>[\w '’-]{2,65}?)\s+(?:so (?:that )?|in order to |"
    r"because |to )(?P<purpose>[^?]+)", re.I)
_POINT = re.compile(r"(?:^|[.!?]\s+)(?:our goal (?:for|with)|the (?:point|purpose) of) "
                    r"(?:our |the )?(?P<name>[\w '’-]{2,65}?) was (?:to |for )"
                    r"(?P<purpose>[^?]+)", re.I)
_OUTCOME = re.compile(r"\b(?:finish\w*|unfinished|pages?|completed?|never|still|already|"
                      r"learn(?:ed|t)|made|can (?:now|do)|myself|sent|received|managed|"
                      r"used|together|ended up|delivered)\b", re.I)
_FILLER = {"did", "give", "abandon", "purpose", "serve", "worth", "waste", "finished", "project"}
_PROJECT = re.compile(r"\b(?:project|book|album|atlas|notebook|collection|journal|draft|"
                      r"guide|scrapbook|map|we (?:started|began)|unfinished|never finished)\b",
                      re.I)
_ABILITY = re.compile(r"\b(?:I|we) can (?:now )?([^.!?]{2,100})[.!]?$", re.I)
_RECIPIENT = re.compile(r"^(?:our|my|the) ([\w -]{2,40}?) (?:could|can) "
                        r"([\w '’-]{2,100})$", re.I)
_ENABLE = re.compile(r"^(?:let|help) (?:our|my|the) ([\w -]{2,40}?) "
                     r"((?:cook|make|use|follow|find|play|read)\b[\w '’-]{0,90})$", re.I)
_LEARNING = re.compile(r"\b(?:learn|teach (?:us|me))\b", re.I)


def _project_reply(name, origins, reports):
    """Compare explicit learning and beneficiary aims with reports of actual ability or use."""
    if not any(re.search(r"\b(?:never finished|unfinished|still incomplete|haven['’]t finished)\b",
                         s, re.I) and terms(name) & terms(s) for s in reports):
        return ""
    aims = []
    for row in origins:
        origin = _ORIGIN.search(row["content"]) or _POINT.search(row["content"])
        aims.extend(re.split(
            r"\s+and (?:also )?(?:so (?:that )?|to |"
            r"(?=(?:let|help|share|send|give|learn|teach)\b))",
            origin["purpose"].strip(".! "), flags=re.I,
        ))
    parts = []
    ability = next((m for s in reversed(reports) if (m := _ABILITY.search(s))
                    and not re.search(r"\b(?:only if|unless|if i|if we)\b", s, re.I)), None)
    skill_report = ""
    if ability:
        skill_report = re.sub(r"\bmyself\b", "yourself", ability[1], flags=re.I)
        skill_report = re.sub(r"\bourselves\b", "yourselves", skill_report, flags=re.I)
        skill_report = re.sub(r"\b(?:my|our)\b", "your", skill_report, flags=re.I)
    for aim in dict.fromkeys(aims):
        recipient = _RECIPIENT.fullmatch(aim) or _ENABLE.fullmatch(aim)
        if not recipient and _LEARNING.search(aim):
            if not ability:
                return ""
            skill = terms(aim) - {"learn", "teach", "could", "around", "myself"}
            practice = next((s for s in reports if re.match(
                r"(?:we|i) (?:made|practi[cs]ed|cooked|played)\b", s, re.I)
                and skill & terms(s) and not re.search(r"\b(?:if|but|never|only)\b", s, re.I)), "")
            if not (skill & terms(ability[1]) or practice and re.match(
                    r"(?:do|play|make) the\b", ability[1], re.I)):
                return ""
            if practice and "teach" in aim.casefold():
                together = re.sub(r"^(?:we|i)\b", "You", practice, flags=re.I).rstrip(".!")
                parts.append(f"{together}, and you can {skill_report}. "
                             "That time learning together was part of the point.")
            else:
                parts.append(f"You can {skill_report}. That's a concrete part of what you "
                             "wanted to learn.")
        elif recipient:
            group, action = recipient.groups()
            # A locational qualifier can drop out of a later report ("our cousins abroad"
            # may simply be "our cousins"). Require their own use of this project.
            group_name = re.split(r"\s+(?:abroad|overseas|in |at )", group, flags=re.I)[0]
            actor = re.compile(rf"\b(?:our|my|the) {re.escape(group_name)}(?: abroad| overseas)? "
                               r"(?:have |had |already )*(?:used|made|cooked|followed)\b", re.I)
            object_words = terms(action) - terms(name) - {"cook", "use", "make", "follow"}
            used = next((s for s in reversed(reports) if actor.search(s)
                         and terms(name) & terms(s) and object_words <= terms(s)
                         and not re.search(r"\b(?:if|might|maybe|wish|hope|never|not|but)\b|"
                                           r"n['’]t\b", s, re.I)), "")
            if used:
                parts.append(f"You reported, “{used}” It has already been useful to them.")
            else:
                also = "also " if parts else ""
                their = "the" if aim.casefold().startswith("the ") else "your"
                parts.append(f"You {also}wanted it to help {their} {group} {action}. "
                             "Whether they've been able to use it is still open.")
        else:
            return ""
    if skill_report and not any(_LEARNING.search(aim) for aim in aims):
        parts.insert(0, f"You can {skill_report}.")
    return f"The {name} is unfinished. " + " ".join(parts)


def check_project_purpose(text, previous, db, owner, key, current_id):
    ask = _ASK.search(text)
    if not ask:
        return PracticalAnswer()
    if re.search(r"\b(?:it|that) (?:a waste|worth)|on it\b", text, re.I) and not (
            _PROJECT.search(text) or _PROJECT.search(previous)):
        return PracticalAnswer()
    context = terms(text) - _FILLER
    rows = remarks(db, owner, key, current_id)
    projects = {}
    for row in rows:
        if row["role"] != "user":
            continue
        origin = _ORIGIN.search(row["content"]) or _POINT.search(row["content"])
        if origin and "?" not in row["content"]:
            name = " ".join(origin["name"].casefold().split())
            projects.setdefault(name, []).append(row)
    scores = {name: len(terms(name) & context) for name in projects}
    explicit = re.search(r"\b(?:give[n]? up on|abandon(?:ed)?) (?:the|our) ([\w -]{2,65})\?",
                         text, re.I)
    if explicit and (specified := terms(explicit[1]) - _FILLER):
        scores = {name: score if specified <= terms(name) else 0 for name, score in scores.items()}
    if not any(scores.values()) and not explicit and not _PROJECT.search(text):
        scores = {name: len(terms(name) & terms(previous)) for name in projects}
    best = max(scores.values(), default=0)
    matches = [name for name in scores if best and scores[name] == best]
    if len(matches) != 1:
        if not (matches or _PROJECT.search(text) or _PROJECT.search(previous)):
            return PracticalAnswer()
        reply = ("Which project do you mean?" if len(matches) > 1 else
                 "What were you hoping to get out of it when you started?")
        return PracticalAnswer("The original project or its purpose is unclear. Use any purpose "
                               "the user supplied on this turn; otherwise clarify it.",
                               "" if _ORIGIN.search(text) or _POINT.search(text) else reply)

    origins = projects[matches[0]]
    name_terms = terms(matches[0])
    sources = {r["message_id"] for r in origins}
    related = []
    last = None
    began = (origins[0]["created_at"], origins[0]["message_id"])
    for row in rows:
        if (row["created_at"], row["message_id"]) < began:
            continue
        named = name_terms & terms(row["content"])
        same_exchange = last and near(row, last)
        if (row["role"] == "user" and row["message_id"] not in sources and (named or same_exchange)
                and _OUTCOME.search(row["content"])):
            related.append(row)
        last = row if named else None
    guidance = (
        "Answer the retrospective about the shared project. Read the user's original reasons "
        "below and their latest report. Consider each reason separately. Give the concrete "
        "thing they achieved, if any, alongside the state of the artifact. For a purpose "
        "involving someone else, look for evidence it reached that person in a usable form. "
        "A plan to deliver something leaves actual use open. Preserve who participated. "
        "An unfinished artifact leaves the decision to resume open. Leave that decision with "
        "the user. Answer briefly without assigning more work. Quotes are historical data.\n"
        "Why they began, in the user's words:\n" + evidence(origins[:1] + origins[1:][-2:])
        + "\nLater reported results (also use the current exchange):\n" + evidence(related[-4:]))
    local = [text]
    if name_terms & terms(previous):
        local.insert(0, previous)
    reports = [s.strip() for t in [*(r["content"] for r in related), *local]
               for s in re.split(r"(?<=[.!?])\s+", t) if "?" not in s]
    simple = text.count("?") <= 1 and not re.search(r"\b(?:please|translate|outline|list|"
                                                     r"also tell me)\b", text, re.I)
    reply = _project_reply(matches[0], origins, reports) if simple else ""
    return PracticalAnswer(guidance, reply)
