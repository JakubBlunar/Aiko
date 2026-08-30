"""L30c — what did that answer actually say about the belief?

Aiko raised a hunch ("can I ask -- do you walk to think?") and the user
replied. This module decides what the reply *did* to the belief, and
nothing else: it returns a verdict and writes no state, so the same
classifier serves both target shapes the loop can carry (a candidate
concept in Phase A, an invented hypothesis in Phase B).

Four verdicts, because three are not enough
-------------------------------------------
The obvious design is confirm / deny. It is wrong, and expensively so.
The single most valuable reply to a hunch is neither: *"not really --
it's more that I hate being still."* Collapsed into ``deny`` that
answer's content is thrown away and a nearly-correct belief is punished
as if it were false; collapsed into ``confirm`` it cements the wrong
wording. ``CORRECT`` keeps the near-miss alive and refinable, which is
the case a person actually learns the most from.

``UNCLEAR`` is the fourth because the user is under no obligation to
answer. They may deflect, joke, or change the subject, and the honest
outcome then is that Aiko still does not know.

Why the classifier is asymmetric
--------------------------------
The error costs are not symmetric, so the evidence bars should not be
either. A false **confirm** adds a source to a belief, which pushes it
through L3's promotion gate and turns a wrong guess into something Aiko
asserts about the user as settled knowledge. A false **deny** merely
knocks some confidence off a hunch that can be re-earned. So confirming
requires positive evidence from the model, and every failure path --
unparseable output, an exception, a missing client -- lands on
``UNCLEAR`` rather than on a guess.

:func:`~app.core.memory.conflict_heuristics.classify_pair` is used in
exactly one direction for the same reason: a ``definite`` opposition
signal (a negation flip, an antonym) *downgrades* a confirm to unclear,
but a ``no`` result never promotes anything. ``no`` there means "found no
opposition", which is not the same claim as "agreed" -- reading it as
agreement is precisely the false confirm this design is built to avoid.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import-only
    import threading

    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.answer_adjudicator")


#: The belief is true as stated. Adds a source; L3 may then promote it.
CONFIRM = "confirm"
#: Close, but the wording is off ("it's more that ..."). The belief takes
#: a confidence penalty and the clarification is stored so the next
#: synthesis pass can propose a better-worded version. No ``contradicts``
#: edge -- a near miss stays refinable.
CORRECT = "correct"
#: Flatly untrue. Penalty *plus* a ``contradicts`` edge, so L9 and L3 see
#: a real disconfirmation and let the belief fade.
DENY = "deny"
#: No usable answer -- deflection, a joke, a change of subject, or
#: anything the classifier could not read. Writes nothing.
UNCLEAR = "unclear"

VERDICTS = frozenset({CONFIRM, CORRECT, DENY, UNCLEAR})

#: Replies at or under this length skip the echo gate and go straight to
#: the model. "yeah, kind of" is the archetypal answer to a hunch and it
#: shares no content words with the belief, so a lexical gate would drop
#: exactly the replies the loop exists to read. Long messages are the
#: ones worth gating: a paragraph with no relation to the belief is
#: someone who moved on.
_SHORT_REPLY_CHARS = 80

#: Content-word overlap needed for a longer reply to count as on-subject.
_MIN_OVERLAP = 1

#: Cosine floor for the semantic half of the echo gate, used only when
#: the caller supplies both vectors. Deliberately low: this gate only has
#: to separate "answering me" from "talking about something else", and
#: being too strict here silently drops real answers.
_MIN_COSINE = 0.35

_MAX_TOKENS = 160

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)

_SYSTEM_PROMPT = (
    "You judge what a person's reply says about a GUESS someone made "
    "about them. Answer with ONE JSON object on a single line and "
    "nothing else. Schema: {\"verdict\": \"CONFIRM\" | \"CORRECT\" | "
    "\"DENY\" | \"UNCLEAR\", \"restated\": \"<= 140 chars, or empty\", "
    "\"reason\": \"<= 80 chars\"}. "
    "CONFIRM = the reply agrees the guess is true, including a casual "
    "yes. "
    "CORRECT = the reply says the guess is close but not quite, and "
    "gives the better version; put that better version in 'restated'. "
    "DENY = the reply says the guess is simply not true. "
    "UNCLEAR = the reply dodges, jokes, asks something back, is about a "
    "different subject, or does not settle the guess either way. "
    "Judge only what the reply asserts -- never what you think is "
    "likely. When you are unsure, answer UNCLEAR."
)

_USER_TEMPLATE = "GUESS: {belief}\nTHEIR REPLY: {reply}"


@dataclass(frozen=True, slots=True)
class AnswerVerdict:
    """One classified reply.

    ``restated`` is the model's better wording on a ``CORRECT``, kept so
    a caller that owns a rewritable statement can use it without a second
    call. It is advisory -- Phase A ignores it, because a concept's label
    belongs to the L17 drift worker rather than to this path.
    """

    verdict: str = UNCLEAR
    reason: str = ""
    restated: str = ""
    #: False when the echo gate or a missing client short-circuited the
    #: call. Distinguishes "the model said it could not tell" from "we
    #: never asked", which the tests and the debug surfaces both need.
    used_llm: bool = False

    @property
    def settles(self) -> bool:
        """True when the belief learned something from this reply."""
        return self.verdict in (CONFIRM, CORRECT, DENY)


def looks_like_an_answer(
    belief: str,
    reply: str,
    *,
    belief_vec: Any = None,
    reply_vec: Any = None,
    min_cosine: float | None = None,
    question: str = "",
    question_vec: Any = None,
) -> bool:
    """Cheap gate: is this reply plausibly about the belief at all?

    Saves the LLM call on the common case where Aiko asked and the user
    simply carried on with their own thread. Passes anything short (see
    :data:`_SHORT_REPLY_CHARS`) on the reasoning there, and otherwise
    requires the reply to echo the belief lexically or semantically.

    ``question`` / ``question_vec`` are the words she actually asked
    (H7). A paraphrase of *her* wording can miss the stored label and
    still be an answer; either item matching is enough.

    The semantic half only runs when the caller supplies both vectors,
    and it is what rescues a well-paraphrased answer: *"the pavement is
    where anything resembling a plan assembles itself"* shares no content
    word with "walks to think" and would otherwise be discarded.
    """
    body = (reply or "").strip()
    if not body:
        return False
    if len(body) <= _SHORT_REPLY_CHARS:
        return True
    floor = _MIN_COSINE if min_cosine is None else float(min_cosine)
    if _echoes(
        str(belief or ""),
        body,
        item_vec=belief_vec,
        reply_vec=reply_vec,
        min_cosine=floor,
    ):
        return True
    asked = (question or "").strip()
    if asked and _echoes(
        asked,
        body,
        item_vec=question_vec,
        reply_vec=reply_vec,
        min_cosine=floor,
    ):
        return True
    return False


def _echoes(
    item_text: str,
    reply: str,
    *,
    item_vec: Any,
    reply_vec: Any,
    min_cosine: float,
) -> bool:
    """Lexical or cosine hit of ``reply`` against one item."""
    try:
        from app.core.memory.echo_detector import detect, tokens

        verdict = detect(
            reply_tokens=tokens(reply),
            item_text=item_text,
            min_overlap=_MIN_OVERLAP,
            reply_vec=reply_vec,
            item_vec=item_vec,
            min_cosine=min_cosine if reply_vec is not None else None,
        )
    except Exception:
        log.debug("answer echo gate failed", exc_info=True)
        return True
    return bool(verdict.echoed)


def adjudicate(
    *,
    belief: str,
    reply: str,
    ollama: "OllamaClient | None",
    model: str,
    belief_vec: Any = None,
    reply_vec: Any = None,
    min_cosine: float | None = None,
    question: str = "",
    question_vec: Any = None,
    cancel_event: "threading.Event | None" = None,
) -> AnswerVerdict:
    """Classify what ``reply`` says about ``belief``.

    Pure: returns a verdict and touches nothing. Never raises -- every
    failure path resolves to :data:`UNCLEAR`. The session resolver then
    decides whether to hold the cue (echo miss / no client) or expire it
    (an LLM dodge).
    """
    statement = (belief or "").strip()
    body = (reply or "").strip()
    if not statement or not body:
        return AnswerVerdict()
    if not looks_like_an_answer(
        statement,
        body,
        belief_vec=belief_vec,
        reply_vec=reply_vec,
        min_cosine=min_cosine,
        question=question,
        question_vec=question_vec,
    ):
        log.debug("answer adjudicator: off-subject reply, no call")
        return AnswerVerdict(reason="off_subject")
    if ollama is None or not model:
        return AnswerVerdict(reason="no_client")

    parsed = _ask(
        statement, body, ollama=ollama, model=model, cancel_event=cancel_event,
    )
    if parsed is None:
        return AnswerVerdict(reason="unparsed", used_llm=True)

    verdict, restated, reason = parsed
    if verdict == CONFIRM and _definitely_opposed(statement, body):
        # The model read agreement out of a reply that flips a term or
        # negates the belief. One-way guard: this only ever weakens a
        # confirm, never manufactures one.
        log.info(
            "answer adjudicator: confirm downgraded on opposition signal "
            "belief=%r",
            statement[:80],
        )
        return AnswerVerdict(
            verdict=UNCLEAR, reason="opposed_confirm", used_llm=True,
        )
    return AnswerVerdict(
        verdict=verdict, reason=reason, restated=restated, used_llm=True,
    )


def _definitely_opposed(belief: str, reply: str) -> bool:
    """True when the F5 heuristics see an unambiguous contradiction."""
    try:
        from app.core.memory.conflict_heuristics import classify_pair

        return classify_pair(belief, reply).label == "definite"
    except Exception:
        log.debug("classify_pair guard failed", exc_info=True)
        return False


def _ask(
    belief: str,
    reply: str,
    *,
    ollama: "OllamaClient",
    model: str,
    cancel_event: "threading.Event | None",
) -> tuple[str, str, str] | None:
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": _USER_TEMPLATE.format(belief=belief, reply=reply),
        },
    ]
    chunks: list[str] = []
    t0 = time.monotonic()
    try:
        stream = ollama.chat_stream(
            messages,
            options={"num_predict": _MAX_TOKENS},
            model=model,
            stop_event=cancel_event,
            format_json=True,
            think=False,
            surface="answer_adjudicator",
        )
        for chunk in stream:
            chunks.append(chunk)
    except Exception:
        log.warning("answer adjudicator call raised", exc_info=True)
        return None
    raw = "".join(chunks).strip()
    if not raw:
        return None
    if log.isEnabledFor(logging.DEBUG):
        log.debug(
            "answer adjudicator raw: chars=%d elapsed_ms=%.0f preview=%r",
            len(raw),
            (time.monotonic() - t0) * 1000.0,
            raw[:200],
        )
    return _parse(raw)


def _parse(raw: str) -> tuple[str, str, str] | None:
    match = _JSON_OBJECT_RE.search(raw or "")
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        return None
    restated = str(parsed.get("restated", "") or "").strip()[:140]
    reason = str(parsed.get("reason", "") or "").strip()[:120]
    return verdict, restated, reason


__all__ = [
    "CONFIRM",
    "CORRECT",
    "DENY",
    "UNCLEAR",
    "VERDICTS",
    "AnswerVerdict",
    "adjudicate",
    "looks_like_an_answer",
]
