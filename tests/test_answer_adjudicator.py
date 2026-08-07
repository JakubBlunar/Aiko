"""L30c: what did that answer say about the belief?

The riskiest component in the hypothesis loop, because a false *confirm*
does not merely record something wrong -- it adds a source, which pushes
the belief through L3's promotion gate and turns a bad guess into
something Aiko states about the user as settled. So most of these tests
are about the confirm side specifically: that it needs positive evidence
from the model, that every failure path lands somewhere else, and that
the opposition guard can only ever weaken it.
"""
from __future__ import annotations

import unittest
from typing import Any

import numpy as np

from app.core.concepts.answer_adjudicator import (
    CONFIRM,
    CORRECT,
    DENY,
    UNCLEAR,
    adjudicate,
    looks_like_an_answer,
)

_BELIEF = "Jacob treats walking as thinking time"


class _FakeOllama:
    """``chat_stream`` stub yielding one canned response per call."""

    def __init__(self, *payloads: str) -> None:
        self._payloads = list(payloads)
        self.calls: list[list[dict[str, Any]]] = []

    def chat_stream(self, messages, **kwargs):
        self.calls.append(messages)
        if not self._payloads:
            raise AssertionError("adjudicator called more times than staged")
        yield self._payloads.pop(0)


class _RaisingOllama:
    def __init__(self) -> None:
        self.calls = 0

    def chat_stream(self, messages, **kwargs):
        self.calls += 1
        raise RuntimeError("model is down")
        yield ""  # pragma: no cover - unreachable, keeps this a generator


def _json(verdict: str, restated: str = "", reason: str = "r") -> str:
    return (
        f'{{"verdict": "{verdict}", "restated": "{restated}", '
        f'"reason": "{reason}"}}'
    )


def _run(payload: str, reply: str = "yeah, pretty much", **kw):
    ollama = _FakeOllama(payload)
    verdict = adjudicate(
        belief=_BELIEF, reply=reply, ollama=ollama, model="m", **kw,
    )
    return verdict, ollama


class VerdictTests(unittest.TestCase):
    def test_confirm(self) -> None:
        verdict, _ = _run(_json("CONFIRM"))
        self.assertEqual(verdict.verdict, CONFIRM)
        self.assertTrue(verdict.used_llm)
        self.assertTrue(verdict.settles)

    def test_correct_carries_the_better_wording(self) -> None:
        verdict, _ = _run(
            _json("CORRECT", restated="he walks to stop thinking"),
            reply="not quite -- it's more that it shuts my head up",
        )
        self.assertEqual(verdict.verdict, CORRECT)
        self.assertEqual(verdict.restated, "he walks to stop thinking")

    def test_deny(self) -> None:
        verdict, _ = _run(_json("DENY"), reply="no, I just like being outside")
        self.assertEqual(verdict.verdict, DENY)
        self.assertTrue(verdict.settles)

    def test_unclear_does_not_settle_anything(self) -> None:
        verdict, _ = _run(_json("UNCLEAR"), reply="ha, maybe? who knows")
        self.assertEqual(verdict.verdict, UNCLEAR)
        self.assertFalse(verdict.settles)

    def test_the_verdict_is_case_insensitive(self) -> None:
        verdict, _ = _run('{"verdict": "confirm", "reason": ""}')
        self.assertEqual(verdict.verdict, CONFIRM)


class FailurePathTests(unittest.TestCase):
    """Every way the call can go wrong has to land on UNCLEAR.

    Not merely "not crash": landing on CONFIRM would cement a belief on
    the strength of a parse error, and landing on DENY would punish one.
    """

    def test_unparseable_output(self) -> None:
        verdict, _ = _run("I think he probably does, yes!")
        self.assertEqual(verdict.verdict, UNCLEAR)
        self.assertEqual(verdict.reason, "unparsed")
        self.assertTrue(verdict.used_llm)

    def test_an_unknown_verdict_word(self) -> None:
        verdict, _ = _run(_json("PROBABLY"))
        self.assertEqual(verdict.verdict, UNCLEAR)

    def test_empty_output(self) -> None:
        verdict, _ = _run("")
        self.assertEqual(verdict.verdict, UNCLEAR)

    def test_the_call_raising(self) -> None:
        ollama = _RaisingOllama()
        verdict = adjudicate(
            belief=_BELIEF, reply="yeah", ollama=ollama, model="m",
        )
        self.assertEqual(verdict.verdict, UNCLEAR)
        self.assertEqual(ollama.calls, 1)

    def test_no_client_at_all(self) -> None:
        verdict = adjudicate(
            belief=_BELIEF, reply="yeah", ollama=None, model="m",
        )
        self.assertEqual(verdict.verdict, UNCLEAR)
        self.assertEqual(verdict.reason, "no_client")
        self.assertFalse(verdict.used_llm)

    def test_an_empty_reply_is_not_an_answer(self) -> None:
        ollama = _FakeOllama(_json("CONFIRM"))
        verdict = adjudicate(
            belief=_BELIEF, reply="   ", ollama=ollama, model="m",
        )
        self.assertEqual(verdict.verdict, UNCLEAR)
        self.assertEqual(ollama.calls, [])


class ConfirmRequiresTheModelTests(unittest.TestCase):
    """The mutation check the plan asks for.

    If someone later "optimises" the confirm path into a heuristic --
    a lexical yes-word match, or reading ``classify_pair``'s ``no`` as
    agreement -- these fail. A confirm must trace back to the model
    saying so.
    """

    def test_a_bare_yes_without_the_model_is_not_a_confirm(self) -> None:
        verdict = adjudicate(
            belief=_BELIEF, reply="yes", ollama=None, model="m",
        )
        self.assertNotEqual(verdict.verdict, CONFIRM)

    def test_no_confirm_is_reachable_without_an_llm_call(self) -> None:
        for reply in ("yes", "yeah totally", "mm-hm", "correct", "true"):
            with self.subTest(reply=reply):
                verdict = adjudicate(
                    belief=_BELIEF, reply=reply, ollama=None, model="",
                )
                self.assertFalse(verdict.used_llm)
                self.assertEqual(verdict.verdict, UNCLEAR)

    def test_the_belief_and_reply_both_reach_the_prompt(self) -> None:
        # A guard against a refactor that drops one of the two halves and
        # leaves the model guessing from context it does not have.
        _, ollama = _run(_json("CONFIRM"), reply="yeah, every morning")
        content = ollama.calls[0][-1]["content"]
        self.assertIn(_BELIEF, content)
        self.assertIn("yeah, every morning", content)


class OppositionGuardTests(unittest.TestCase):
    """``classify_pair`` is a one-way veto, never a confirm signal."""

    def test_a_confirm_is_downgraded_on_definite_opposition(self) -> None:
        # "never" against "always" is a negation flip, which the F5
        # heuristics call ``definite``. Whatever the model said, this
        # pairing is not an agreement.
        ollama = _FakeOllama(_json("CONFIRM"))
        verdict = adjudicate(
            belief="Jacob always walks to think",
            reply="Jacob never walks to think",
            ollama=ollama,
            model="m",
        )
        self.assertEqual(verdict.verdict, UNCLEAR)
        self.assertEqual(verdict.reason, "opposed_confirm")

    def test_the_guard_does_not_touch_a_deny(self) -> None:
        ollama = _FakeOllama(_json("DENY"))
        verdict = adjudicate(
            belief="Jacob always walks to think",
            reply="Jacob never walks to think",
            ollama=ollama,
            model="m",
        )
        self.assertEqual(verdict.verdict, DENY)

    def test_absence_of_opposition_never_creates_a_confirm(self) -> None:
        # ``classify_pair`` returns ``no`` here -- meaning "found no
        # opposition", which is not agreement. The model's UNCLEAR must
        # survive it.
        ollama = _FakeOllama(_json("UNCLEAR"))
        verdict = adjudicate(
            belief=_BELIEF,
            reply="anyway, did you see the game last night",
            ollama=ollama,
            model="m",
        )
        self.assertEqual(verdict.verdict, UNCLEAR)


class EchoGateTests(unittest.TestCase):
    def test_a_short_reply_always_reaches_the_model(self) -> None:
        # The whole point: "yeah, kind of" shares no content words with
        # the belief, so a lexical gate would drop exactly the answers
        # this loop exists to read.
        self.assertTrue(looks_like_an_answer(_BELIEF, "yeah, kind of"))

    def test_a_long_unrelated_reply_is_gated_out(self) -> None:
        essay = (
            "So the deployment finally went through last night after we "
            "rebuilt the container image, although the migration needed "
            "another pass before the staging database would accept it."
        )
        self.assertFalse(looks_like_an_answer(_BELIEF, essay))

    def test_a_long_on_subject_reply_passes(self) -> None:
        reply = (
            "Honestly the walking thing is real -- I do most of my actual "
            "thinking on the way to the shops rather than at my desk, "
            "which took me embarrassingly long to notice about myself."
        )
        self.assertTrue(looks_like_an_answer(_BELIEF, reply))

    def test_the_gate_saves_the_call(self) -> None:
        ollama = _FakeOllama(_json("CONFIRM"))
        verdict = adjudicate(
            belief=_BELIEF,
            reply=(
                "So the deployment finally went through last night after "
                "we rebuilt the container image and re-ran the migration."
            ),
            ollama=ollama,
            model="m",
        )
        self.assertEqual(verdict.verdict, UNCLEAR)
        self.assertEqual(verdict.reason, "off_subject")
        self.assertEqual(ollama.calls, [])

    def test_cosine_rescues_a_long_paraphrase(self) -> None:
        # Same vector, no shared words: the semantic half of the gate is
        # what stops a well-paraphrased answer being discarded.
        vec = np.asarray([1.0, 0.0], dtype=np.float32)
        essay = (
            "Completely -- the pavement is where anything resembling a "
            "coherent plan tends to assemble itself, unhelpfully far from "
            "any means of writing it down before it evaporates again."
        )
        self.assertFalse(looks_like_an_answer(_BELIEF, essay))
        self.assertTrue(
            looks_like_an_answer(
                _BELIEF, essay, belief_vec=vec, reply_vec=vec,
            )
        )

    def test_an_empty_belief_settles_nothing(self) -> None:
        # The gate itself passes a short reply before it ever looks at
        # the belief; ``adjudicate`` is where a target with no text is
        # refused, since there is nothing for a verdict to be *about*.
        ollama = _FakeOllama(_json("CONFIRM"))
        verdict = adjudicate(
            belief="  ", reply="yes", ollama=ollama, model="m",
        )
        self.assertEqual(verdict.verdict, UNCLEAR)
        self.assertEqual(ollama.calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
