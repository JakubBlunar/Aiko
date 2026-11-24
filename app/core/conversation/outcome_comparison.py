"""Turn-local comparisons of reported output and observed opportunities."""
import re
from fractions import Fraction
from dataclasses import dataclass

from app.core.conversation.practical_answer import PracticalAnswer


_WORD_NUMBERS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                 "fourteen fifteen sixteen seventeen eighteen nineteen twenty").split()
_COUNT = r"(?:\d{1,6}|" + "|".join(_WORD_NUMBERS) + ")"
_OPPORTUNITIES = r"(?:sessions?|attempts?|tries|shifts?|runs?|visits?|days?|rounds?)"
_UNIT = re.compile(rf"\b{_OPPORTUNITIES}\b", re.I)
_COMPARE = re.compile(
    r"\b(?:when (?:did|was|were)|which|where (?:did|was)|more likely|higher (?:rate|average)|"
    r"compare|how often|does (?:this|that) (?:prove|mean|show)|did .+ make me)\b", re.I,
)
_METRIC = re.compile(
    r"\b(?:more|average|total|rate|likely|often|faster|better|throughput|chance|chances|"
    r"frequency|probability|success)\b", re.I,
)
_PERIOD = r"(?:mornings|evenings|afternoons|weekends|weekdays)"
_PAIR = re.compile(
    rf"\b(?P<a>{_COUNT})\s+(?P<item>[a-z]+)\s+(?:in|over|across|from)\s+"
    rf"(?:(?P<na>{_COUNT})\s+(?P<first>[a-z]+)\s+(?P<unit_a>{_OPPORTUNITIES})"
    rf"|the (?P<period_a>{_PERIOD}))\s*,?\s*"
    rf"(?:and|versus|vs\.?)\s+(?P<b>{_COUNT})(?:\s+(?P=item))?\s+"
    rf"(?:in|over|across|from)\s+(?:(?P<nb>{_COUNT})\s+(?P<second>[a-z]+)\s+"
    rf"(?P<unit_b>{_OPPORTUNITIES})|the (?P<period_b>{_PERIOD}))\b", re.I,
)
_LABELLED = re.compile(rf"([a-z]+):\s*({_COUNT}) ([a-z]+) (?:in|over|across) "
                       rf"({_COUNT}) ({_OPPORTUNITIES})")
_GUIDE = (
    "Answer each comparison the user actually asked for. For total output compare counts. "
    "For output per opportunity divide each output count by its own opportunity count. "
    "For the chance of finishing at least one, divide opportunities with a finish by all "
    "opportunities for that group. Output counts can serve as successful-opportunity counts "
    "only if at most one output was possible per opportunity. Prefer explicit counts of "
    "successful opportunities when supplied. Preserve ties. A missing denominator is unknown "
    "and zero opportunities gives an undefined rate. Give whatever comparison can be answered "
    "and identify the specific missing count. Keep observed differences separate from causes."
)


def _count(value):
    return int(value) if value.isdecimal() else _WORD_NUMBERS.index(value.lower())


def _singular(word):
    if word == "tries":
        return "try"
    if word.endswith(("ches", "shes", "xes")):
        return word[:-2]
    return word.removesuffix("s")


@dataclass
class Observation:
    label: str
    outputs: int
    opportunities: int | None
    successes: int | None = None


def _observations(pair, conditions):
    a = Observation(pair["first"], _count(pair["a"]),
                    _count(pair["na"]) if pair["na"] else None)
    b = Observation(pair["second"], _count(pair["b"]),
                    _count(pair["nb"]) if pair["nb"] else None)
    if a.label == b.label:
        return None
    item, unit = _singular(pair["item"]), _singular(pair["unit"])
    limit = ""
    for condition in conditions:
        if re.fullmatch(rf"each {unit} (?:produced|had) at most one {item}"
                        rf"|at most one {item} per {unit}", condition):
            if limit == "multiple":
                return None
            limit = "binary"
            continue
        if re.fullmatch(rf"some {pair['unit']} (?:produced|had) (?:several|multiple) "
                        rf"{pair['item']}", condition):
            if limit == "binary":
                return None
            limit = "multiple"
            continue
        if any(g.opportunities is None and condition == f"i didn't count {g.label} {pair['unit']}"
               for g in (a, b)):
            continue
        success = re.fullmatch(rf"in the (\w+) ({_COUNT}) {pair['unit']} had a finish", condition)
        if not success:
            return None
        group = next((g for g in (a, b) if success[1] in (g.label, g.label + "s")), None)
        if group is None or group.successes is not None:
            return None
        group.successes = _count(success[2])
    for group in (a, b):
        if limit == "binary":
            if group.successes not in (None, group.outputs):
                return None
            group.successes = group.outputs
        if group.outputs <= 1 and group.successes is None:
            group.successes = group.outputs
        if group.opportunities == 0 and group.outputs:
            return None
        limit_count = min(group.outputs, group.opportunities
                          if group.opportunities is not None else group.outputs)
        if group.successes is not None and (group.successes > limit_count
                                            or group.successes == 0 < group.outputs):
            return None
    return a, b


def _metric(question, item, unit):
    verb = r"(?:finish|complete|make|produce)"
    if re.fullmatch(rf"(?:when|where) did i {verb} more(?: {item})?(?: in total)?"
                    rf"|which (?:group|period) (?:had|produced) more {item} in total", question):
        return "total"
    if re.fullmatch(rf"(?:when|where) did i {verb} more {item} per {unit}"
                    rf"|which had (?:the )?higher (?:average number of {item} per {unit}"
                    rf"|throughput)|which (?:group|period) averaged more {item} per {unit}",
                    question):
        return "throughput"
    if re.fullmatch(rf"(?:when|where) was i more likely to {verb} (?:one|at least one"
                    rf"|(?:a|one|at least one) {_singular(item)})", question):
        return "likelihood"
    return ""


def _comparison(groups, metric, item, unit):
    values, details, unknown, empty = [], [], [], []
    for group in groups:
        if metric == "total":
            values.append(group.outputs)
            details.append(f"{group.outputs} {item} in {group.label} {unit}")
            continue
        n = group.outputs if metric == "throughput" else group.successes
        d = group.opportunities
        if d == 0:
            empty.append(f"There were no {group.label} {unit} to calculate a rate from.")
        elif d is None or n is None:
            if d is None:
                unknown.append(f"the number of {group.label} {unit}")
            if n is None:
                unknown.append(f"the number of {group.label} {unit} with a finish")
        else:
            values.append(Fraction(n, d))
            if metric == "throughput":
                # Preserve exact thirds etc. and use a decimal when it terminates briefly.
                rate = Fraction(n, d)
                amount = (f"{float(rate):.3f}".rstrip("0").rstrip(".")
                          if 1000 % rate.denominator == 0 else str(rate))
                details.append(f"{amount} {item} per {group.label} {_singular(unit)}")
            else:
                details.append(f"{n}/{d} {group.label} {unit} with a finish")
    if unknown or empty:
        known = ["You recorded " + details[0] + "."] if details else []
        if unknown:
            known.append("To calculate both rates, I'd need " + " and ".join(unknown) + ".")
        return " ".join(known + empty)
    subject = {"total": "Your total output", "throughput": f"Your average per {_singular(unit)}",
               "likelihood": f"The share of {unit} with a finish"}[metric]
    if values[0] == values[1]:
        verdict = "was tied"
    else:
        winner = groups[0] if values[0] > values[1] else groups[1]
        verdict = f"was higher for {winner.label} {unit}"
    return f"{subject} {verdict} (" + " versus ".join(details) + ")."


def _read_pair(parts):
    claim = re.sub(r"^i (?:finished|completed|made|produced) ", "", parts[0].rstrip("."))
    match = _PAIR.fullmatch(claim)
    if match:
        pair = match.groupdict()
        units = {u for u in (pair["unit_a"], pair["unit_b"]) if u}
        if len(units) != 1:
            return None, parts
        pair["unit"] = units.pop()
        pair["first"] = pair["first"] or pair["period_a"][:-1]
        pair["second"] = pair["second"] or pair["period_b"][:-1]
        return pair, parts[1:]
    if len(parts) < 2:
        return None, parts
    left, right = (_LABELLED.fullmatch(s.rstrip(".")) for s in parts[:2])
    if not left or not right or left[3] != right[3] or left[5] != right[5]:
        return None, parts
    pair = {"first": left[1], "a": left[2], "item": left[3], "na": left[4], "unit": left[5],
            "second": right[1], "b": right[2], "nb": right[4]}
    return pair, parts[2:]


def check_outcomes(text, previous=""):
    if not text or not _COMPARE.search(text) or not _METRIC.search(text):
        return PracticalAnswer()
    questions = [s for s in re.split(r"(?<=[.!?])\s+|\n+", text.strip()) if s.endswith("?")]
    if questions and not any(re.search(
            r"\b(?:finish|complete|make|produce|produced|total|average|averaged|rates?|"
            r"likely|per|faster|throughput|chance|success|probability)\b", q, re.I)
            for q in questions):
        return PracticalAnswer()
    source = text
    followup = len(text.split()) <= 20
    if followup and not _PAIR.search(text) and previous:
        # Only an actual follow-up about the reported item can borrow the counts.
        prior_parts = re.split(r"(?<=[.!?])\s+", previous.lower().strip())
        prior, _ = _read_pair(prior_parts)
        if prior and _metric(text.lower().strip().rstrip("?"), prior["item"],
                             _singular(prior["unit"])):
            while prior_parts and prior_parts[-1].endswith("?"):
                prior_parts.pop()
            source = " ".join(prior_parts) + "\n" + text
    if not _UNIT.search(source) or len(re.findall(rf"\b{_COUNT}\b", source, re.I)) < 2:
        return PracticalAnswer()
    guide = PracticalAnswer("Comparing observed outcomes. " + _GUIDE)
    parts = re.split(r"(?<=[.!?])\s+|\n+", source.lower().replace("’", "'").strip())
    pair, remaining = _read_pair(parts)
    if not pair or not parts[-1].endswith("?"):
        return guide
    conditions = []
    metrics = []
    for part in remaining:
        if part.endswith("?"):
            metric = _metric(part[:-1], pair["item"], _singular(pair["unit"]))
            if re.fullmatch(
                    rf"does (?:this|that) prove working (?:{pair['first']}|{pair['second']}) "
                    r"made me faster\?", part):
                metric = "cause"
            if not metric:
                return guide
            if metric not in metrics:
                metrics.append(metric)
        elif metrics:
            return guide
        else:
            conditions.append(part.rstrip("."))
    groups = _observations(pair, conditions)
    if not groups:
        return guide
    answers = []
    for metric in metrics:
        if metric == "cause":
            answers.append("These observations alone can't establish what caused the difference. "
                           "Comparing speed would also require time spent.")
            metric = "throughput"
        answers.append(_comparison(groups, metric, pair["item"], pair["unit"]))
    reply = " ".join(answers)
    return PracticalAnswer(guide.guidance, reply)
