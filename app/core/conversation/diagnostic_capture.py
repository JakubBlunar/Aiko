"""Anchor explicitly contrasted checks to their original, speaker-owned sentences."""
from dataclasses import asdict, dataclass, replace
import json
import re

from app.core.conversation.conversation_situation import (
    ACTIVE, MAX_EVIDENCE_IDS, _extract_json_object, starts_detour, state_age_seconds,
)
from app.core.infra import timephrase


RETURN_TO = re.compile(r"^(?:(?:let['’]s (?:get|go) )?back to|returning to|about) "
                       r"(?:the |my )?([\w -]+?)(?:[.,:?!—]|$)", re.I)


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


def starts_separate_task(notes, text):
    """Recognize an independently stated request with a disjoint subject."""
    if not notes or not notes.check_result or not notes.causal_check:
        return False
    request = text.strip()
    if not re.fullmatch(r"(?:(?:why |what (?:causes|makes) )[^.!?]{5,145}\?|"
                        r"(?:please )?(?:suggest|name|write|draft|design|plan) "
                        r"[^.!?]{5,145}[.!?]?)", request, re.I):
        return False
    noise = {"what", "causes", "makes", "does", "that", "this", "with", "have", "would"}
    def terms(s):
        return set(re.findall(r"\b[a-z]{3,}\b", s.lower())) - noise - {"why", "the", "are"}
    subject = terms(request)
    evidence = " ".join(n.text for n in (notes.question, notes.next_check, *notes.hypotheses) if n)
    return len(subject) >= 2 and not subject.intersection(terms(evidence))


def named_problem(store, state, text, *, max_age=21600):
    """Select a unique, current comparison without restoring old world or social state."""
    if not state or not state.parked_comparison or not RETURN_TO.match(text.strip()):
        return state
    parked = state.parked_comparison
    if not return_subject(parked.working_set, text)[1]:
        return state
    candidate = replace(
        state, status=ACTIVE, summary=parked.working_set.question.text,
        working_set=parked.working_set, evidence_message_ids=parked.evidence_message_ids,
        working_source_revision=parked.working_source_revision, updated_at=parked.updated_at,
        shared=False, shared_activity="", place_ref="", aiko_activity="",
        pragmatic_frame=None, repair_target=None, parked_comparison=None, miss_count=0,
    )
    age = state_age_seconds(candidate)
    if age is None or age > max_age or not store.working_sources_current(candidate):
        return state
    if state.working_set and return_subject(state.working_set, text)[1]:
        return None
    return candidate


def _note(text, row):
    return {"text": text, "evidence_message_ids": [row.id]}


def _label(text):
    return re.sub(r"^(?:the|an?)\s+", "", text.strip().casefold())


def changed_test_condition(question, observation):
    required = re.findall(r"\b(?:the )?same ([a-z]+)\b", question, re.I)
    return any(re.search(rf"\b(?:used|using|from|on|with) (?:a |the )?"
                         rf"(?:different|wrong|other) {re.escape(noun)}[.!]?\s*$",
                         observation, re.I) for noun in required)


@dataclass
class ProposedTest:
    hypotheses: list[str]
    predictions: list[str]
    steps: dict[str, str]


def proposed_test(text: str) -> ProposedTest | None:
    """Read an explicit two-way test before either the reply or the observer runs."""
    if len(text) > 1000 or "if yes," not in text.lower() or "if no," not in text.lower():
        return None
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    hypotheses, predictions, steps = [], [], {}
    for sentence in sentences:
        prediction = re.fullmatch(r"([A-Za-z][\w '-]{2,70}?) would (.{3,110})\.", sentence, re.I)
        if prediction and len(sentence) - 1 <= 120:
            hypotheses.append(prediction[1])
            predictions.append(sentence[:-1])
            continue
        step = re.fullmatch(r"If (yes|no),\s*(.{3,110})\.", sentence, re.I)
        if step and step[1].lower() not in steps:
            steps[step[1].lower()] = step[2]
            continue
        if not re.fullmatch(r"(?:are|is|do|does|did|can|will)\b[^.!?]*\?", sentence, re.I):
            # A mixed turn may already report the outcome.
            return None
    if len(predictions) == 2 and steps.keys() == {"yes", "no"}:
        return ProposedTest(hypotheses, predictions, steps)
    return None


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
            if changed_test_condition(self.notes["next_check"]["text"], self.report["text"]):
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
        request = text[RETURN_TO.match(text).end():].strip() if named else ""
        resume = named and re.fullmatch(
            r"(?:where were we|what (?:should (?:I|we) (?:try|check|do) next|was the result))?\??",
            request, re.I,
        )
        if named and (comparison_recap(text) or resume) and prior.check_result:
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
    proposal = proposed_test(last.content) if last.role in {"user", "assistant"} else None
    if proposal is None:
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
                         "hypotheses": [_note(label, last) for label in proposal.hypotheses],
                         "decision_check": {"if_yes": proposal.steps["yes"],
                                            "if_no": proposal.steps["no"]},
                         "causal_check": {"assumptions": proposal.predictions,
                                          "result_applies": False}}
                return DiagnosticCapture(
                    notes, {row.id, last.id, *check["evidence_message_ids"]},
                    f"The user goal is in message {row.id}. The proposed test in message "
                    f"{check['evidence_message_ids'][0]} asks: {check['text']} Imagine the "
                    f"answer is YES. The suggested yes step was: {proposal.steps['yes']}. "
                    "Which cause specifically predicts that YES observation while the other "
                    "predicts NO? "
                    "Copy its name exactly into diagnostic_verdict. You are recording a "
                    "prediction made BEFORE the test. An unknown cause or an unanswered "
                    "question is expected. Use neither only if the stated predictions do "
                    "not distinguish these two explanations for this check.",
                )
    return None
