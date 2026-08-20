"""H31: the extractor mines each turn once, not once per window it lingers in.

``SummaryWorker`` advances a watermark and fires the extractor every
``summary_min_unsummarized_messages`` new messages; the extractor used to
read the trailing ``max_window_messages`` regardless of what it had already
mined, so with the shipped defaults (30 and 6) every turn was offered for
extraction about five times. These cover the watermark that stops that, and
the two cases where getting the advance rule wrong loses turns instead of
duplicating them: a failed call must not advance, and a backlog must drain
from its front rather than its back.
"""
from __future__ import annotations

import json
import re
import unittest

from app.core.infra.chat_database import ChatDatabase, MessageRow
from app.core.memory.memory_extractor import (
    MemoryExtractor,
    _fallback_kind,
    _opens_about_user,
    _opens_first_person,
)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 20
    done_reason = "stop"


class _FakeOllama:
    """Records each prompt and replays a scripted list of responses."""

    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.prompts: list[str] = []

    def chat_json(self, messages, **_kwargs):
        self.prompts.append(messages[-1]["content"])
        nxt = self._responses.pop(0) if self._responses else '{"memories": []}'
        if isinstance(nxt, Exception):
            raise nxt
        return nxt, _Usage()


class _FakeDb:
    """Just the four readers plus the kv pair the watermark uses."""

    def __init__(self, count: int = 0) -> None:
        self.rows: list[MessageRow] = []
        self.kv: dict[str, str] = {}
        for _ in range(count):
            self.append()

    def append(self, role: str | None = None, content: str | None = None) -> MessageRow:
        next_id = len(self.rows) + 1
        row = MessageRow(
            id=next_id,
            session_id="s",
            role=role or ("user" if next_id % 2 else "assistant"),
            content=content or f"line {next_id}",
            token_count=3,
            created_at="2026-08-13T10:00:00+00:00",
        )
        self.rows.append(row)
        return row

    def get_messages(self, session_id, *, limit=None, offset=None):
        rows = list(self.rows)
        return rows[-limit:] if limit else rows

    def get_messages_after(self, session_id, *, after_id, limit):
        return [r for r in self.rows if r.id > after_id][:limit]

    def get_messages_before(self, session_id, *, before_id, limit):
        return [r for r in self.rows if r.id < before_id][-limit:]

    def kv_get(self, key):
        return self.kv.get(key)

    def kv_set(self, key, value):
        self.kv[key] = str(value)


class _FakeStore:
    def __init__(self) -> None:
        self.added: list[dict] = []

    def list_recent(self, limit=50, **_kw):
        return []

    def list_top(self, limit=50, **_kw):
        return []

    def add(self, **kwargs):
        self.added.append(kwargs)
        return object()


class _FakeEmbedder:
    def embed(self, _text):
        return [0.0, 1.0]


def _memories(*contents: str) -> str:
    return json.dumps({
        "memories": [
            {"content": c, "kind": "fact", "salience": 0.5,
             "temporal_type": "durable", "provenance": "stated"}
            for c in contents
        ]
    })


def _build(db, ollama, store=None, **kwargs) -> MemoryExtractor:
    return MemoryExtractor(
        db, store or _FakeStore(), _FakeEmbedder(), ollama,
        model="m",
        **kwargs,
    )


_KEY = "memory.extractor.watermark:s"


class FirstRunTests(unittest.TestCase):
    def test_a_first_run_seeds_from_the_trailing_window(self) -> None:
        # An install with existing history must not mine from message one.
        db = _FakeDb(count=100)
        ollama = _FakeOllama(['{"memories": []}'])
        _build(db, ollama, max_window_messages=30).extract_for_session("s")
        self.assertIn("line 100", ollama.prompts[0])
        self.assertNotIn("line 70\n", ollama.prompts[0])
        self.assertEqual(db.kv[_KEY], "100")

    def test_b_first_run_below_the_floor_does_nothing(self) -> None:
        db = _FakeDb(count=3)
        ollama = _FakeOllama([])
        _build(db, ollama, min_window_messages=4).extract_for_session("s")
        self.assertEqual(ollama.prompts, [])
        self.assertEqual(db.kv, {})


class WatermarkTests(unittest.TestCase):
    def test_a_second_run_offers_only_the_new_turns(self) -> None:
        db = _FakeDb(count=20)
        db.kv[_KEY] = "20"
        for _ in range(6):
            db.append()
        ollama = _FakeOllama(['{"memories": []}'])
        _build(db, ollama, context_messages=0).extract_for_session("s")
        prompt = ollama.prompts[0]
        for line in ("line 21", "line 26"):
            self.assertIn(line, prompt)
        self.assertNotIn("line 20", prompt)
        self.assertEqual(db.kv[_KEY], "26")

    def test_b_mined_turns_are_offered_as_context_not_material(self) -> None:
        db = _FakeDb(count=20)
        db.kv[_KEY] = "20"
        for _ in range(6):
            db.append()
        ollama = _FakeOllama(['{"memories": []}'])
        _build(db, ollama, context_messages=4).extract_for_session("s")
        prompt = ollama.prompts[0]
        # Present for reference resolution...
        self.assertIn("line 17", prompt)
        # ...but on the far side of the do-not-extract line.
        self.assertLess(prompt.index("line 17"), prompt.index("line 21"))
        self.assertIn("Do NOT", prompt)
        self.assertIn("Extract ONLY", prompt)

    def test_c_nothing_new_means_no_model_call(self) -> None:
        db = _FakeDb(count=20)
        db.kv[_KEY] = "20"
        ollama = _FakeOllama([])
        _build(db, ollama).extract_for_session("s")
        self.assertEqual(ollama.prompts, [])

    def test_d_too_few_new_turns_waits_rather_than_advancing(self) -> None:
        # An overflow squish drops the summariser's bar to 2. Mining two
        # rows at a time would be the old bug wearing a watermark.
        db = _FakeDb(count=20)
        db.kv[_KEY] = "20"
        db.append()
        db.append()
        ollama = _FakeOllama([])
        _build(db, ollama, min_window_messages=4).extract_for_session("s")
        self.assertEqual(ollama.prompts, [])
        self.assertEqual(db.kv[_KEY], "20")
        # The material is not lost -- it accumulates and goes on the next run.
        for _ in range(2):
            db.append()
        ollama = _FakeOllama(['{"memories": []}'])
        _build(db, ollama, min_window_messages=4, context_messages=0).extract_for_session("s")
        prompt = ollama.prompts[0]
        self.assertIn("line 21", prompt)
        self.assertIn("line 24", prompt)
        self.assertEqual(db.kv[_KEY], "24")

    def test_e_a_turn_is_never_offered_as_material_twice(self) -> None:
        db = _FakeDb(count=6)
        seen: list[set[str]] = []
        for _run in range(4):
            ollama = _FakeOllama(['{"memories": []}'])
            _build(db, ollama, context_messages=0).extract_for_session("s")
            if ollama.prompts:
                body = ollama.prompts[0].split("Extract ONLY")[-1]
                # Match the line *number*, not every digit in the prompt.
                # The loose version collected the ``13`` out of the
                # ``[Aug 13 12:00]`` stamp each transcript line now
                # carries, so it reported a duplicate on every run while
                # the windows were in fact disjoint (1-6, 7-12, 13-18,
                # 19-24) -- a red test guarding a working property, which
                # is worse than no test.
                seen.append(set(re.findall(r"line (\d+)", body)))
            for _ in range(6):
                db.append()
        # Four runs, six new messages each, and no line offered twice.
        flat = [item for s in seen for item in s]
        self.assertEqual(len(flat), len(set(flat)))
        self.assertEqual(len(seen), 4)


class AdvanceRuleTests(unittest.TestCase):
    def test_a_a_failed_call_leaves_the_watermark_alone(self) -> None:
        db = _FakeDb(count=10)
        db.kv[_KEY] = "10"
        for _ in range(6):
            db.append()
        ollama = _FakeOllama([RuntimeError("ollama down")])
        _build(db, ollama).extract_for_session("s")
        self.assertEqual(db.kv[_KEY], "10")

    def test_b_an_unreadable_answer_leaves_the_watermark_alone(self) -> None:
        db = _FakeDb(count=10)
        db.kv[_KEY] = "10"
        for _ in range(6):
            db.append()
        ollama = _FakeOllama(["I'm afraid I can't do that"])
        _build(db, ollama).extract_for_session("s")
        self.assertEqual(db.kv[_KEY], "10")

    def test_c_an_empty_answer_is_a_verdict_and_advances(self) -> None:
        # "Nothing durable in these turns" is a real answer. Re-offering
        # them would ask the same question until the model changed its mind.
        db = _FakeDb(count=10)
        db.kv[_KEY] = "10"
        for _ in range(6):
            db.append()
        ollama = _FakeOllama(['{"memories": []}'])
        _build(db, ollama).extract_for_session("s")
        self.assertEqual(db.kv[_KEY], "16")

    def test_d_a_salvaged_answer_advances(self) -> None:
        db = _FakeDb(count=10)
        db.kv[_KEY] = "10"
        for _ in range(6):
            db.append()
        truncated = (
            '{"memories": [{"content": "Jacob lives in Prague", '
            '"kind": "fact", "salience": 0.6}, {"content": "cut off'
        )
        ollama = _FakeOllama([truncated])
        store = _FakeStore()
        _build(db, ollama, store=store).extract_for_session("s")
        self.assertEqual(db.kv[_KEY], "16")
        self.assertEqual(len(store.added), 1)

    def test_e_a_backlog_drains_from_its_front(self) -> None:
        # Taking the newest rows of an oversized backlog would advance the
        # watermark past the middle and abandon everything stepped over.
        db = _FakeDb(count=50)
        db.kv[_KEY] = "10"
        ollama = _FakeOllama(['{"memories": []}'])
        _build(db, ollama, max_window_messages=10, context_messages=0).extract_for_session("s")
        prompt = ollama.prompts[0]
        self.assertIn("line 11", prompt)
        self.assertIn("line 20", prompt)
        self.assertNotIn("line 21", prompt)
        self.assertEqual(db.kv[_KEY], "20")


class KindFallbackTests(unittest.TestCase):
    """A missing label must not default to 'this is about the user'."""

    def test_a_first_person_openers(self) -> None:
        self.assertTrue(_opens_first_person("I have taken up collecting caps."))
        self.assertTrue(_opens_first_person("My favourite is the worn one."))
        self.assertFalse(_opens_first_person("Jacob collects bottle caps."))
        self.assertFalse(_opens_first_person(""))

    def test_b_fallback_reads_the_sentence(self) -> None:
        self.assertEqual(
            _fallback_kind("I have taken up collecting bottle caps."), "self",
        )
        self.assertEqual(
            _fallback_kind("Jacob has taken up collecting caps."), "fact",
        )

    def test_c_unknown_kind_on_a_self_note_stays_a_self_note(self) -> None:
        ext = _build(_FakeDb(), _FakeOllama([]))
        out = ext._validate_entries([
            {"content": "I have started collecting bottle caps.",
             "kind": "aiko_note"},
        ])
        self.assertEqual(out[0]["kind"], "self")

    def test_d_a_self_label_on_a_sentence_about_the_user_is_corrected(self) -> None:
        ext = _build(
            _FakeDb(), _FakeOllama([]),
            user_display_name_provider=lambda: "Jacob",
        )
        out = ext._validate_entries([
            {"content": "Jacob prefers double-checking logs.", "kind": "self"},
        ])
        self.assertEqual(out[0]["kind"], "fact")

    def test_e_a_known_kind_is_left_alone(self) -> None:
        ext = _build(
            _FakeDb(), _FakeOllama([]),
            user_display_name_provider=lambda: "Jacob",
        )
        out = ext._validate_entries([
            {"content": "Jacob likes tea.", "kind": "preference"},
        ])
        self.assertEqual(out[0]["kind"], "preference")

    def test_f_the_generic_fallback_name_never_matches(self) -> None:
        self.assertFalse(
            _opens_about_user("The garden is my favourite place.", "the user"),
        )
        self.assertTrue(_opens_about_user("Jacob likes tea.", "Jacob"))


class ExistingBlockTests(unittest.TestCase):
    def test_a_recent_rows_are_shown_not_just_salient_ones(self) -> None:
        """The rows most at risk of being re-emitted are the newest ones.

        ``list_top`` ranks by salience, and everything the extractor writes
        lands in scratchpad at whatever salience the model guessed -- often
        0.0 -- so a row written four minutes ago was never in the block that
        exists to prevent duplicating it.
        """
        class _Mem:
            def __init__(self, mid, content, salience):
                self.id = mid
                self.content = content
                self.salience = salience
                self.kind = "fact"
                self.created_at = "2026-08-13T10:00:00+00:00"
                self.pinned = False
                self.temporal_type = "durable"
                self.event_time = None

        class _Store(_FakeStore):
            def list_recent(self, limit=50, **_kw):
                return [_Mem(1, "written four minutes ago", 0.0)]

            def list_top(self, limit=50, **_kw):
                return [_Mem(2, "an old salient row", 0.9)]

        db = _FakeDb(count=8)
        ollama = _FakeOllama(['{"memories": []}'])
        _build(db, ollama, store=_Store()).extract_for_session("s")
        prompt = ollama.prompts[0]
        self.assertIn("written four minutes ago", prompt)
        self.assertIn("an old salient row", prompt)

    def test_b_the_two_lists_are_deduplicated(self) -> None:
        class _Mem:
            def __init__(self):
                self.id = 7
                self.content = "the same row from both readers"
                self.salience = 0.9
                self.kind = "fact"
                self.created_at = "2026-08-13T10:00:00+00:00"
                self.pinned = False
                self.temporal_type = "durable"
                self.event_time = None

        class _Store(_FakeStore):
            def list_recent(self, limit=50, **_kw):
                return [_Mem()]

            def list_top(self, limit=50, **_kw):
                return [_Mem()]

        db = _FakeDb(count=8)
        ollama = _FakeOllama(['{"memories": []}'])
        _build(db, ollama, store=_Store()).extract_for_session("s")
        self.assertEqual(
            ollama.prompts[0].count("the same row from both readers"), 1,
        )


class PromptTests(unittest.TestCase):
    def test_a_the_attribution_rule_names_both_sides(self) -> None:
        from app.core.memory.memory_extractor import _build_system_prompt

        prompt = _build_system_prompt("Jacob")
        self.assertIn("Half of this transcript is Aiko talking", prompt)
        self.assertIn("never restate it as a fact about Jacob", prompt)


class DbReaderTests(unittest.TestCase):
    """``get_messages_after`` takes the oldest rows, not the newest."""

    def setUp(self) -> None:
        import tempfile
        import pathlib

        self._dir = tempfile.TemporaryDirectory()
        self.db = ChatDatabase(pathlib.Path(self._dir.name) / "t.db")
        for i in range(20):
            self.db.add_message(
                "s", "user" if i % 2 else "assistant", f"m{i}",
            )

    def tearDown(self) -> None:
        conn = getattr(self.db._local, "conn", None)
        if conn is not None:
            conn.close()
            self.db._local.conn = None
        try:
            self._dir.cleanup()
        except PermissionError:
            pass

    def test_a_returns_rows_after_the_id_oldest_first(self) -> None:
        rows = self.db.get_messages_after("s", after_id=5, limit=4)
        self.assertEqual([r.id for r in rows], [6, 7, 8, 9])

    def test_b_empty_past_the_end(self) -> None:
        self.assertEqual(
            self.db.get_messages_after("s", after_id=999, limit=5), [],
        )

    def test_c_zero_limit_is_empty(self) -> None:
        self.assertEqual(
            self.db.get_messages_after("s", after_id=0, limit=0), [],
        )

    def test_d_scoped_to_the_session(self) -> None:
        self.db.add_message("other", "user", "elsewhere")
        rows = self.db.get_messages_after("other", after_id=0, limit=50)
        self.assertEqual([r.content for r in rows], ["elsewhere"])


if __name__ == "__main__":
    unittest.main()
