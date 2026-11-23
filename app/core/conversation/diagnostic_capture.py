"""Anchor explicitly contrasted checks to their original, speaker-owned sentences."""
from dataclasses import asdict, dataclass
import json
import re

from app.core.conversation.conversation_situation import (
    MAX_EVIDENCE_IDS, _extract_json_object, starts_detour,
)
from app.core.infra import timephrase


RETURN_TO = re.compile(r"^(?:back to|returning to|about) (?:the |my )?([\w -]+?)(?:[.,:?!—]|$)",
                       re.I)


def return_subject(notes, text):
    match = RETURN_TO.match(text.strip())
    if not match:
        return "", False
    subject = match[1].strip().casefold()
    if (len(subject.split()) < 2 or subject in {"same issue", "same problem", "earlier issue"}
            or re.search(r"\b(?:forget|drop|stop discussing)\b", text, re.I)):
        return "", False
    return subject, bool(re.search(rf"\b{re.escape(subject)}\b", notes.question.text, re.I))


def comparison_recap(text):
    opening = RETURN_TO.match(text.strip())
    return opening and re.fullmatch(
        r"which (?:cause|explanation|hypothesis) (?:fits|fit)(?: the (?:test|result))? "
        r"(?:best|better)\??", text.strip()[opening.end():].strip(), re.I,
    )


def _note(text, row):
    return {"text": text, "evidence_message_ids": [row.id]}


def _label(text):
    return re.sub(r"^(?:the|an?)\s+", "", text.strip().casefold())


@dataclass
class DiagnosticCapture:
    notes: dict
    ids: set[int]
    instruction: str
    report: dict | None = None
    recap: bool = False

    @property
    def options(self):
        alternatives = ["inconclusive", "unrelated"] if self.report else ["neither"]
        return [h["text"] for h in self.notes["hypotheses"]] + alternatives

    def bind(self, raw):
        """Project only the verdict requested on this call, then use normal validation."""
        payload = _extract_json_object(raw)
        if (not payload or payload.get("operation") not in ("keep", "replace")
                or payload.get("explicit_end") is True):
            return raw
        verdict = payload.get("diagnostic_verdict")
        matches = [i for i, label in enumerate(self.options[:2])
                   if isinstance(verdict, str) and _label(label) == _label(verdict)]
        selected = matches[0] if len(matches) == 1 else None
        if self.recap:
            self.notes["suspended"] = False
        elif self.report:
            if selected is None and verdict != "inconclusive":
                return raw
            # An explicit change to a required test condition takes priority
            # over a model that matches only the surface outcome ("clean").
            check_text = self.notes["next_check"]["text"]
            required = re.findall(r"\b(?:the )?same ([a-z]+)\b", check_text, re.I)
            if any(re.search(rf"\b(?:used|using|from|on|with) (?:a |the )?"
                             rf"(?:different|wrong|other) {re.escape(noun)}[.!]?\s*$",
                             self.report["text"], re.I) for noun in required):
                selected = None
            self.notes["check_result"] = self.report
            self.notes["suspended"] = False
            self.notes["decision_check"].update(
                answer=None if selected is None else (
                    "yes" if selected == self.notes["causal_check"]["expects_yes"] else "no"
                ), fact_index=None,
            )
            self.notes["causal_check"]["result_applies"] = selected is not None
        else:
            if selected is None:
                if verdict == "neither" and isinstance(payload.get("working_set"), dict):
                    payload["working_set"]["causal_check"] = None
                    return json.dumps(payload)
                return raw
            self.notes["causal_check"]["expects_yes"] = selected
            # A newly sourced comparison establishes a new working problem, even
            # when the observer describes the surrounding conversation as continuing.
            payload["operation"] = "replace"
            proposed = payload.get("working_set")
            if isinstance(proposed, dict) and proposed.get("constraints"):
                self.notes["constraints"] = proposed["constraints"]
        evidence = payload.get("evidence_message_ids")
        if not isinstance(evidence, list) or not all(type(mid) is int for mid in evidence):
            return raw
        combined = sorted(self.ids.union(evidence))
        if len(combined) > MAX_EVIDENCE_IDS:
            return raw
        # A retained working set makes a new interest successor ineligible.
        payload.update(working_set=self.notes, evidence_message_ids=combined, successor=None)
        return json.dumps(payload)


def diagnostic_capture(rows, previous):
    if not rows:
        return None
    prior = previous.working_set if previous else None
    if prior and prior.causal_check and prior.decision_check and prior.next_check:
        users = [r for r in rows if r.id > previous.source_message_id and r.role == "user"]
        text = users[0].content.strip() if len(users) == 1 else ""
        named = bool(text and return_subject(prior, text)[1])
        if named and comparison_recap(text) and prior.check_result:
            return DiagnosticCapture(asdict(prior), set(previous.evidence_message_ids),
                                     "", recap=True)
        correction = named and re.search(r"\b(?:correction|actually|wrong|mixed up)\b", text, re.I)
        report_text = text
        if correction:
            report_text = re.sub(r"\s+Which (?:cause|explanation|hypothesis) fits[^?]*\?$", "",
                                 text[RETURN_TO.match(text).end():].strip(), flags=re.I)
        if (text and (not prior.suspended or correction) and not starts_detour(text)
                and 1 <= len(report_text) <= 160 and "?" not in report_text
                and not timephrase.has_relative_deictic(report_text)):
            report = _note(report_text, users[0])
            yes = prior.causal_check.expects_yes
            return DiagnosticCapture(
                asdict(prior), {*previous.evidence_message_ids, users[0].id},
                f"Classify the newest USER observation in message {users[0].id} against this "
                f"specific test question: {prior.next_check.text} The yes prediction was "
                f"{prior.hypotheses[yes].text}: {prior.causal_check.assumptions[yes]}. The no "
                f"prediction was {prior.hypotheses[1-yes].text}: "
                f"{prior.causal_check.assumptions[1-yes]}. A correction supersedes any earlier "
                "observation. Into diagnostic_verdict copy the NAME of the hypothesis whose "
                "prediction matches the latest observed outcome under the stated conditions, "
                '"inconclusive" for a performed check with unclear or changed conditions, '
                'or "unrelated" for a plan, guess, social turn or another problem. '
                "Keep the original predictions fixed.", report,
            )
    # Post-turn observers usually see Aiko's reply after a user-proposed check.
    # Read the source turn itself; never treat that subsequent reply as a test result.
    if (len(rows) >= 2 and rows[-1].role == "assistant" and rows[-2].role == "user"
            and "if yes," in rows[-2].content.lower() and "if no," in rows[-2].content.lower()):
        rows = rows[:-1]
    last = rows[-1]
    if previous and last.id <= previous.source_message_id:
        return None
    if last.role not in {"user", "assistant"} or len(last.content) > 1000:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", last.content.strip())
    predictions = [m for s in sentences if (m := re.fullmatch(
        r"([A-Za-z][\w '-]{2,70}?) would (.{3,110})[.]", s, re.I))]
    steps = {m[1].lower(): m[2] for s in sentences if (m := re.fullmatch(
        r"If (yes|no),\s*(.{3,110})[.]", s, re.I))}
    if len(predictions) != 2 or steps.keys() != {"yes", "no"}:
        return None
    premises = [m[0].rstrip(".") for m in predictions]
    if any(len(premise) > 120 for premise in premises):
        return None
    if len(sentences) != 4 + sum(s.endswith("?") for s in sentences):
        # A mixed turn may already report the outcome. Never manufacture a
        # pre-test prediction from an explanation offered after the result.
        return None
    questions = []
    for row in reversed(rows[-4:]):
        if row.id != last.id and row.role == "user" and not re.fullmatch(
            r"[^.!?]+\?", row.content.strip(),
        ):
            return None
        for s in reversed(re.split(r"(?<=[.!?])\s+", row.content.strip())):
            if not s.endswith("?") or len(s) > 160:
                continue
            if re.match(r"(?:are|is|do|does|did|can|will)\b", s, re.I):
                if not questions or questions[0]["evidence_message_ids"] == [row.id]:
                    questions.append(_note(s, row))
            elif row.role == "user" and re.match(r"(?:why|what causes|what makes)\b", s, re.I):
                if len(questions) != 1 or row.content.strip() != s:
                    return None
                check = questions[0]
                notes = {"question": _note(s, row), "next_check": check,
                         "hypotheses": [_note(m[1], last) for m in predictions],
                         "decision_check": {"if_yes": steps["yes"], "if_no": steps["no"]},
                         "causal_check": {"assumptions": premises,
                                          "result_applies": False}}
                return DiagnosticCapture(
                    notes, {row.id, last.id, *check["evidence_message_ids"]},
                    f"The user goal is in message {row.id}. The proposed test in message "
                    f"{check['evidence_message_ids'][0]} asks: {check['text']} Imagine the "
                    f"answer is YES. The suggested yes step was: {steps['yes']}. Which cause "
                    "specifically predicts that YES observation while the other predicts NO? "
                    "Copy its name exactly into diagnostic_verdict. You are recording a "
                    "prediction made BEFORE the test. An unknown cause or an unanswered "
                    "question is expected. Use neither only if the stated predictions do "
                    "not distinguish these two explanations for this check.",
                )
    return None
