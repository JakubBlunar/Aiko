"""Tests for the post-turn promise extractor (Phase 3c)."""
from __future__ import annotations

import unittest

import numpy as np

from app.core.promise_extractor import (
    Promise,
    PromiseExtractor,
    _parse_llm_payload,
    extract_regex,
)


class _FakeMemory:
    def __init__(self, mid: int, content: str, kind: str) -> None:
        self.id = mid
        self.content = content
        self.kind = kind


class _FakeMemoryStore:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._next_id = 1
        self.fail = False
        self.return_none = False

    def add(
        self,
        *,
        content,
        kind,
        embedding,
        salience,
        source_session=None,
        source_message_id=None,
        tier=None,
        confidence=None,
    ):
        self.calls.append({
            "content": content,
            "kind": kind,
            "salience": salience,
            "tier": tier,
            "confidence": confidence,
        })
        if self.fail:
            return None
        if self.return_none:
            return None
        mem = _FakeMemory(self._next_id, content, kind)
        self._next_id += 1
        return mem


class _FakeEmbedder:
    def embed(self, text: str):
        return np.zeros(8, dtype=np.float32)


class _FakeOllama:
    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls: list[dict] = []
        self.fail = False

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "options": options})
        if self.fail:
            raise RuntimeError("simulated llm failure")
        return self.response


class ExtractRegexTests(unittest.TestCase):
    def test_user_will_pattern(self):
        ps = extract_regex(user_text="I'll call my mom tomorrow", assistant_text="")
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0].who, "user")
        self.assertIn("call my mom tomorrow", ps[0].text)

    def test_user_need_to_pattern(self):
        ps = extract_regex(user_text="I really need to fix the deploy script", assistant_text="")
        self.assertEqual(ps[0].who, "user")
        self.assertIn("fix the deploy script", ps[0].text)

    def test_remind_me_pattern(self):
        ps = extract_regex(user_text="hey, remind me to update the changelog", assistant_text="")
        self.assertTrue(any("update the changelog" in p.text for p in ps))

    def test_assistant_promise(self):
        ps = extract_regex(user_text="", assistant_text="Sure, I'll remind you tomorrow morning")
        self.assertEqual(len(ps), 1)
        self.assertEqual(ps[0].who, "assistant")

    def test_assistant_let_me_know(self):
        ps = extract_regex(
            user_text="",
            assistant_text="Let me know how the interview goes!",
        )
        self.assertTrue(any("the interview" in p.text for p in ps))

    def test_no_false_positives(self):
        ps = extract_regex(
            user_text="The weather is nice today.",
            assistant_text="That sounds lovely.",
        )
        self.assertEqual(ps, [])

    def test_dedupes_repeats(self):
        ps = extract_regex(
            user_text="I'll do it. Also I'll do it. Yes, I'll do it.",
            assistant_text="",
        )
        # The promise body is the same -> should dedupe.
        self.assertEqual(len(ps), 1)

    def test_to_memory_content(self):
        p = Promise(who="user", text="call my mom tomorrow")
        self.assertIn("Jacob promised", p.to_memory_content())
        p2 = Promise(who="assistant", text="check on the deploy")
        self.assertIn("Aiko promised", p2.to_memory_content())


class ParseLlmPayloadTests(unittest.TestCase):
    def test_basic_object(self):
        raw = (
            '{"promises":[{"who":"user","what":"start running","deadline":"this weekend"}]}'
        )
        out = _parse_llm_payload(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].who, "user")
        self.assertIn("start running", out[0].text)
        self.assertIn("by this weekend", out[0].text)

    def test_drops_short_what(self):
        raw = '{"promises":[{"who":"user","what":"do"}]}'
        self.assertEqual(_parse_llm_payload(raw), [])

    def test_caps_to_three(self):
        raw = (
            '{"promises":['
            '{"who":"user","what":"first thing"},'
            '{"who":"user","what":"second thing"},'
            '{"who":"user","what":"third thing"},'
            '{"who":"user","what":"fourth thing"}'
            ']}'
        )
        self.assertEqual(len(_parse_llm_payload(raw)), 3)

    def test_handles_garbage(self):
        self.assertEqual(_parse_llm_payload("not json"), [])

    def test_handles_fences(self):
        raw = '```json\n{"promises":[{"who":"assistant","what":"send the recap"}]}\n```'
        self.assertEqual(len(_parse_llm_payload(raw)), 1)


class PromiseExtractorTests(unittest.TestCase):
    def _make(self, llm_response: str = "{}", **overrides):
        ollama = _FakeOllama(llm_response)
        store = _FakeMemoryStore()
        embedder = _FakeEmbedder()
        kwargs = {
            "ollama": ollama,
            "memory_store": store,
            "embedder": embedder,
            "model": "m",
            "llm_min_user_turns": 2,
        }
        kwargs.update(overrides)
        ext = PromiseExtractor(**kwargs)
        return ext, ollama, store

    def test_extract_post_turn_persists_regex_matches(self):
        ext, _ollama, store = self._make()
        promises = ext.extract_post_turn(
            user_text="I'll mail the package on Friday",
            assistant_text="",
            session_key="s",
        )
        self.assertEqual(len(promises), 1)
        self.assertEqual(len(store.calls), 1)
        self.assertEqual(store.calls[0]["kind"], "promise")
        self.assertGreaterEqual(ext.stats()["regex_persisted"], 1)

    def test_extract_post_turn_no_promises_no_writes(self):
        ext, _ollama, store = self._make()
        promises = ext.extract_post_turn(
            user_text="nice weather huh",
            assistant_text="indeed",
            session_key="s",
        )
        self.assertEqual(promises, [])
        self.assertEqual(store.calls, [])

    def test_llm_throttled_until_min_turns(self):
        ext, ollama, _store = self._make(
            llm_response='{"promises":[]}',
            llm_min_user_turns=3,
        )
        ext.notify_user_turn()
        result = ext.maybe_run_llm(session_key="s", history_provider=lambda: [])
        self.assertIsNone(result)
        self.assertEqual(ollama.calls, [])

    def test_llm_runs_after_min_turns_persists(self):
        ext, ollama, store = self._make(
            llm_response=(
                '{"promises":[{"who":"user","what":"start writing daily","deadline":null}]}'
            ),
            llm_min_user_turns=2,
        )
        for _ in range(2):
            ext.notify_user_turn()

        result = ext.maybe_run_llm(
            session_key="s",
            history_provider=lambda: [
                ("user", "I keep meaning to start writing daily"),
                ("assistant", "what's stopping you?"),
            ],
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(len(result), 1)
        self.assertEqual(store.calls[-1]["kind"], "promise")
        self.assertGreaterEqual(ext.stats()["llm_persisted"], 1)

    def test_llm_failure_does_not_raise(self):
        ext, ollama, _store = self._make(llm_response="", llm_min_user_turns=1)
        ollama.fail = True
        ext.notify_user_turn()
        result = ext.maybe_run_llm(
            session_key="s",
            history_provider=lambda: [("user", "hi"), ("assistant", "hi")],
        )
        self.assertIsNone(result)
        self.assertEqual(ext.stats()["llm_failed"], 1)


if __name__ == "__main__":
    unittest.main()
