"""F6 wiring: the three search workers delegate to the reformulator.

Each worker has a heavy ``__init__``; these tests bypass it via
``__new__`` and set only the attributes the scrub method reads, so we
verify the *delegation* (reformulator used when present, deterministic
scrub when absent) without standing up a full SessionController.
"""
from __future__ import annotations

import unittest

from app.core.memory.idle_fact_checker import IdleFactChecker
from app.core.proactive.idle_curiosity_worker import IdleCuriosityWorker
from app.core.proactive.idle_knowledge_worker import IdleKnowledgeWorker


def _wire(worker, reformulator) -> None:
    worker._user_names_provider = lambda: ["Jacob"]
    worker._assistant_name_provider = lambda: "Aiko"
    worker._query_reformulator = reformulator


class KnowledgeWorkerScrubTests(unittest.TestCase):
    def test_uses_reformulator_when_present(self) -> None:
        w = IdleKnowledgeWorker.__new__(IdleKnowledgeWorker)
        _wire(w, lambda _t: "neutral topic query")
        self.assertEqual(w._scrub("Jacob loves jazz"), "neutral topic query")

    def test_falls_back_to_deterministic_when_absent(self) -> None:
        w = IdleKnowledgeWorker.__new__(IdleKnowledgeWorker)
        _wire(w, None)
        # Name stripped by the deterministic scrub.
        out = w._scrub("history of bonsai cultivation")
        self.assertEqual(out, "history of bonsai cultivation")


class CuriosityWorkerScrubTests(unittest.TestCase):
    def test_uses_reformulator_when_present(self) -> None:
        w = IdleCuriosityWorker.__new__(IdleCuriosityWorker)
        _wire(w, lambda _t: "neutral topic query")
        self.assertEqual(w._scrub("what does Jacob like?"), "neutral topic query")

    def test_falls_back_when_absent(self) -> None:
        w = IdleCuriosityWorker.__new__(IdleCuriosityWorker)
        _wire(w, None)
        out = w._scrub("the rules of go")
        self.assertEqual(out, "the rules of go")


class _Claim:
    def __init__(self, text: str) -> None:
        self.claim_text = text


class FactCheckerScrubTests(unittest.TestCase):
    def test_uses_reformulator_when_present(self) -> None:
        w = IdleFactChecker.__new__(IdleFactChecker)
        _wire(w, lambda _t: "neutral topic query")
        self.assertEqual(
            w._scrub_claim(_Claim("Jacob said the earth is flat")),
            "neutral topic query",
        )

    def test_falls_back_when_absent(self) -> None:
        w = IdleFactChecker.__new__(IdleFactChecker)
        _wire(w, None)
        out = w._scrub_claim(_Claim("speed of light in vacuum"))
        self.assertEqual(out, "speed of light in vacuum")


if __name__ == "__main__":
    unittest.main()
