"""K96 -- the post-reply think pass.

Three things are worth pinning here, in rising order of subtlety.

The parser has to treat "no second thought this turn" as the *designed*
answer rather than a failure, because that is what most turns should
produce -- and it has to keep that case distinguishable from a model
answering in a shape it cannot read, since one is health and the other is
a bug.

The gates have to decide against spending a call without spending one.

And the request has to reuse the turn's system prompt byte-for-byte.
That last one is the whole economic argument for the feature: the prefix
is ~74k characters, the turn just cached it, and rebuilding or trimming
the string here would silently turn a cache read back into a full send
while every test still passed.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from app.core.proactive.second_thought_worker import (
    SecondThoughtWorker,
    build_instruction,
    parse_second_thought,
    render_cue_text,
)
from app.core.session.second_thought_debug_mixin import SecondThoughtDebugMixin
from app.llm.chat_client import CACHE_BREAKPOINT_KEY


class _FakeClient:
    def __init__(self, response: str = "") -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []
        self.fail = False

    def chat(self, messages, options=None, model=None, **kwargs):
        self.calls.append({
            "messages": messages,
            "options": options,
            "model": model,
            "surface": kwargs.get("surface"),
        })
        if self.fail:
            raise RuntimeError("simulated LLM failure")
        return self.response


def _settings(**over):
    agent = {
        "second_thought_enabled": True,
        "second_thought_max_tokens": 160,
        "second_thought_min_gap_seconds": 0,
        "second_thought_min_user_chars": 0,
        "second_thought_min_reply_chars": 0,
    }
    agent.update(over)
    return SimpleNamespace(agent=SimpleNamespace(**agent))


def _worker(
    client: _FakeClient,
    *,
    queued: list | None = None,
    pending: int = 0,
    inventory_target: int = 2,
    **over,
):
    sink = queued if queued is not None else []

    def _queue(subject, text, payload):
        sink.append((subject, text, payload))
        return True

    return SecondThoughtWorker(
        ollama=client,
        model="gpt-5.6-luna",
        queue_cue=_queue,
        pending_count=lambda: pending,
        settings_provider=lambda: _settings(**over),
        user_display_name_provider=lambda: "Jacob",
        inventory_target=inventory_target,
    )


_GOOD = (
    "SUBJECT: his manager conversation\n"
    "THOUGHT: I answered the logistics and skipped that he sounded "
    "resigned about it."
)

_LONG_USER = "I had a long day and the thing with my manager is still bothering me a lot."
_LONG_REPLY = (
    "That sounds draining. Did you get any sense of whether the deadline "
    "is actually going to move, or is it just being restated at you?"
)


# ── 1. parsing ────────────────────────────────────────────────────────


class ParseTests(unittest.TestCase):
    def test_a_well_formed_pair_parses(self) -> None:
        got = parse_second_thought(_GOOD)
        self.assertEqual(got.subject, "his manager conversation")
        self.assertIn("resigned", got.thought)
        self.assertFalse(got.is_empty())

    def test_the_decline_word_is_empty_not_an_error(self) -> None:
        self.assertTrue(parse_second_thought("NONE").is_empty())

    def test_a_decline_that_explains_itself_is_still_a_decline(self) -> None:
        """The majority path must not be salvaged into a bogus thought.

        A model that declines sometimes adds a line of reasoning. Parsing
        that as content would put slop in the pool on exactly the turns the
        pass correctly judged as needing nothing.
        """
        raw = "NONE -- the reply already covered it.\nTHOUGHT: nothing here"
        self.assertTrue(parse_second_thought(raw).is_empty())

    def test_a_missing_field_is_empty(self) -> None:
        self.assertTrue(
            parse_second_thought("SUBJECT: his manager").is_empty(),
        )
        self.assertTrue(
            parse_second_thought("THOUGHT: I missed something").is_empty(),
        )

    def test_a_degenerate_subject_is_refused(self) -> None:
        """A one-word subject would match almost any reply.

        Consumption is lexical against the subject, so "work" scores as
        used the moment she says the word -- which is a cue that retires
        itself without ever being acted on.
        """
        raw = "SUBJECT: work\nTHOUGHT: something about his job"
        self.assertTrue(parse_second_thought(raw).is_empty())

    def test_tags_are_case_insensitive_and_whitespace_tolerant(self) -> None:
        raw = "  subject :  the deadline thing \n  thought :  I brushed it off. "
        got = parse_second_thought(raw)
        self.assertEqual(got.subject, "the deadline thing")
        self.assertEqual(got.thought, "I brushed it off.")

    def test_junk_is_empty_rather_than_an_exception(self) -> None:
        for raw in ("", "   ", "I think maybe we should talk about it?"):
            with self.subTest(raw=raw):
                self.assertTrue(parse_second_thought(raw).is_empty())

    def test_the_subject_is_bounded(self) -> None:
        raw = f"SUBJECT: {'x' * 500}\nTHOUGHT: {'y' * 900}"
        got = parse_second_thought(raw)
        self.assertLessEqual(len(got.subject), 80)
        self.assertLessEqual(len(got.thought), 400)


class InstructionTests(unittest.TestCase):
    def test_the_instruction_offers_the_declining_path(self) -> None:
        text = build_instruction("Jacob")
        self.assertIn("NONE", text)
        self.assertIn("Jacob", text)

    def test_the_instruction_carries_the_stored_text_time_rule(self) -> None:
        """The cue text outlives the turn, so deictics must be resolved.

        A pooled cue can surface 44 hours after it was drafted (see
        docs/cue-pool.md), by which point a thought written about "tonight"
        is a claim that is simply false.
        """
        from app.core.infra import timephrase

        text = build_instruction("Jacob")
        self.assertIn(timephrase.STORED_TEXT_TIME_RULE, text)
        self.assertIn("Today is", text)

    def test_no_placeholder_survives_rendering(self) -> None:
        self.assertNotIn("{", build_instruction("Jacob"))


# ── 2. the gates ──────────────────────────────────────────────────────


class GateTests(unittest.TestCase):
    def test_disabled_spends_nothing(self) -> None:
        client = _FakeClient(_GOOD)
        worker = _worker(client, second_thought_enabled=False)
        self.assertIsNone(worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
        ))
        self.assertEqual(client.calls, [])
        self.assertEqual(worker.stats()["skipped_disabled"], 1)

    def test_a_thin_turn_spends_nothing(self) -> None:
        client = _FakeClient(_GOOD)
        worker = _worker(
            client,
            second_thought_min_user_chars=80,
            second_thought_min_reply_chars=120,
        )
        self.assertIsNone(worker.maybe_run(
            system_prompt="P", user_text="ok", assistant_text="sure",
        ))
        self.assertEqual(client.calls, [])
        self.assertEqual(worker.stats()["skipped_thin"], 1)

    def test_the_clock_holds_the_second_call(self) -> None:
        client = _FakeClient(_GOOD)
        worker = _worker(client, second_thought_min_gap_seconds=3600)
        worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
        )
        worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(worker.stats()["skipped_recent"], 1)

    def test_a_stocked_shelf_stops_drafting(self) -> None:
        """Turn-triggered production fills far faster than an idle worker.

        Without this the shelf empties itself over consecutive turns, which
        is how a target of 2 becomes a shelf of 14.
        """
        client = _FakeClient(_GOOD)
        worker = _worker(client, pending=2, inventory_target=2)
        self.assertIsNone(worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
        ))
        self.assertEqual(client.calls, [])
        self.assertEqual(worker.stats()["skipped_stocked"], 1)

    def test_an_empty_reply_spends_nothing(self) -> None:
        client = _FakeClient(_GOOD)
        worker = _worker(client)
        self.assertIsNone(worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER, assistant_text="",
        ))
        self.assertEqual(client.calls, [])

    def test_force_bypasses_the_cheap_gates(self) -> None:
        client = _FakeClient(_GOOD)
        worker = _worker(
            client,
            pending=9,
            second_thought_min_gap_seconds=3600,
            second_thought_min_user_chars=500,
            second_thought_min_reply_chars=500,
        )
        got = worker.maybe_run(
            system_prompt="P", user_text="hi", assistant_text="hey",
            force=True,
        )
        self.assertIsNotNone(got)
        self.assertEqual(len(client.calls), 1)

    def test_force_does_not_bypass_the_master_switch(self) -> None:
        """A kill switch a debug tool can talk past is not a kill switch."""
        client = _FakeClient(_GOOD)
        worker = _worker(client, second_thought_enabled=False)
        self.assertIsNone(worker.maybe_run(
            system_prompt="P", user_text="hi", assistant_text="hey",
            force=True,
        ))
        self.assertEqual(client.calls, [])

    def test_a_failing_model_still_burns_its_slot(self) -> None:
        """Otherwise a broken provider is retried on every single turn."""
        client = _FakeClient(_GOOD)
        client.fail = True
        worker = _worker(client, second_thought_min_gap_seconds=3600)
        worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
        )
        worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
        )
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(worker.stats()["failed"], 1)


# ── 3. the cached prefix (the reason this is affordable) ───────────────


class PrefixReuseTests(unittest.TestCase):
    _SYS = "PERSONA AND CONTEXT, VERBATIM" * 40

    def _call(self, breakpoints=(11, 29)):
        client = _FakeClient(_GOOD)
        worker = _worker(client)
        worker.maybe_run(
            system_prompt=self._SYS,
            user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
            cache_breakpoints=breakpoints,
            session_key="default:abc",
        )
        return client.calls[0]

    def test_the_system_prompt_is_reused_byte_for_byte(self) -> None:
        messages = self._call()["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[0]["content"], self._SYS)

    def test_the_breakpoints_are_reattached(self) -> None:
        """Every marked prefix lies inside the system message.

        So the service can still read from the longest entry the turn
        wrote, no matter what follows it here.
        """
        messages = self._call()["messages"]
        self.assertEqual(messages[0][CACHE_BREAKPOINT_KEY], (11, 29))

    def test_no_breakpoint_key_when_the_turn_had_none(self) -> None:
        messages = self._call(breakpoints=())["messages"]
        self.assertNotIn(CACHE_BREAKPOINT_KEY, messages[0])

    def test_the_exchange_then_the_private_instruction_follow(self) -> None:
        messages = self._call()["messages"]
        self.assertEqual(
            [m["role"] for m in messages],
            ["system", "user", "assistant", "user"],
        )
        self.assertEqual(messages[2]["content"], _LONG_REPLY)
        self.assertIn("Private note to yourself", messages[3]["content"])

    def test_it_shares_the_turns_cache_affinity_key(self) -> None:
        options = self._call()["options"]
        self.assertEqual(options["prompt_cache_key"], "default:abc")

    def test_the_output_is_capped_and_the_surface_is_named(self) -> None:
        call = self._call()
        self.assertEqual(call["options"]["num_predict"], 160)
        self.assertEqual(call["surface"], "second_thought")

    def test_it_runs_on_the_chat_model(self) -> None:
        """Not the worker model: the cached prefix only exists there."""
        self.assertEqual(self._call()["model"], "gpt-5.6-luna")


# ── 4. what reaches the pool ──────────────────────────────────────────


class QueueTests(unittest.TestCase):
    def test_a_parsed_thought_is_queued_with_its_subject(self) -> None:
        queued: list = []
        worker = _worker(_FakeClient(_GOOD), queued=queued)
        worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
        )
        self.assertEqual(len(queued), 1)
        subject, text, payload = queued[0]
        self.assertEqual(subject, "his manager conversation")
        self.assertIn("resigned", text)
        self.assertEqual(payload["subject"], subject)
        self.assertIn("drafted_at", payload)
        self.assertEqual(worker.stats()["queued"], 1)

    def test_a_decline_queues_nothing_and_is_counted_apart(self) -> None:
        queued: list = []
        worker = _worker(_FakeClient("NONE"), queued=queued)
        self.assertIsNone(worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
        ))
        self.assertEqual(queued, [])
        stats = worker.stats()
        self.assertEqual(stats["declined"], 1)
        self.assertEqual(stats["unparsed"], 0)

    def test_an_unreadable_answer_is_counted_as_a_bug_signal(self) -> None:
        """Separate from ``declined`` on purpose.

        Merged, a diagnostic could not tell a pass behaving exactly as
        designed from one whose output shape has drifted.
        """
        worker = _worker(_FakeClient("sure, I think he's fine"))
        self.assertIsNone(worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
        ))
        stats = worker.stats()
        self.assertEqual(stats["unparsed"], 1)
        self.assertEqual(stats["declined"], 0)

    def test_the_cue_line_does_not_restate_the_handling_note(self) -> None:
        """The note is hoisted per-surfacing; duplicating it pays twice."""
        text = render_cue_text("I skipped how resigned he sounded.")
        self.assertLess(len(text), 200)
        self.assertIn("resigned", text)

    def test_a_refused_queue_is_a_failure_not_a_silent_success(self) -> None:
        worker = SecondThoughtWorker(
            ollama=_FakeClient(_GOOD),
            model="m",
            queue_cue=lambda *_a: False,
            settings_provider=lambda: _settings(),
        )
        self.assertIsNone(worker.maybe_run(
            system_prompt="P", user_text=_LONG_USER,
            assistant_text=_LONG_REPLY,
        ))
        self.assertEqual(worker.stats()["failed"], 1)


# ── 5. the policy it inherits ─────────────────────────────────────────


class PolicyTests(unittest.TestCase):
    def _policy(self):
        from app.core.proactive.cue_accounting import policy_for

        policy = policy_for("second_thought")
        assert policy is not None
        return policy

    def test_it_is_a_stocked_type_not_an_event_armed_one(self) -> None:
        self.assertGreater(self._policy().inventory_target, 0)

    def test_saying_it_is_what_counts_as_using_it(self) -> None:
        from app.core.proactive.cue_accounting import FULFILMENT_SPOKEN

        self.assertEqual(self._policy().fulfilment, FULFILMENT_SPOKEN)

    def test_the_pacing_gate_is_set(self) -> None:
        """Produced every turn, so the shelf cannot also pace the saying."""
        self.assertGreater(self._policy().surface_cooldown_hours, 0.0)

    def test_the_subject_needs_more_than_one_shared_word(self) -> None:
        self.assertGreaterEqual(self._policy().min_overlap, 2)

    def test_it_expires_inside_a_couple_of_days(self) -> None:
        """A loose end from this conversation is not a Friday topic."""
        self.assertLessEqual(self._policy().ttl_hours, 48.0)


# ── 6. the debug facade ───────────────────────────────────────────────


class _Row:
    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


class _Db:
    def __init__(self, rows: list[_Row]) -> None:
        self.rows = rows

    def get_messages(self, session_key, limit=8):
        return self.rows[-limit:]


class _Host(SecondThoughtDebugMixin):
    """The controller surface the facade is allowed to assume.

    Deliberately minimal: everything here is either public on
    ``SessionController`` or owned by an init mixin, so a method that
    starts reaching somewhere new fails to construct rather than passing
    against a mock that answers any attribute at all.
    """

    session_key = "main"

    def __init__(self, worker, rows=(), prompt="SYSTEM") -> None:
        self._second_thought_worker = worker
        self._settings = _settings()
        self._chat_db = _Db(list(rows))
        self._prompt = prompt
        self.debug_overrides = SimpleNamespace(peek=lambda name, default: False)

    def get_last_system_prompt(self):
        return {"prompt": self._prompt}

    def cue_pool_cadence(self, cue_type):
        return {"blocked": False, "cue_type": cue_type}

    def list_cue_pool(self, *, cue_type=None, limit=50):
        return {"cues": [{"cue_type": cue_type}]}


class DebugFacadeTests(unittest.TestCase):
    """The MCP tools hold a bound method, not the worker.

    The point of the facade is that ``_second_thought_worker`` can be
    renamed without a debug tool quietly reporting ``worker_registered:
    false`` forever -- so what is pinned here is that the state read and
    the forced draft both work through the controller.
    """

    def _rows(self):
        return [_Row("user", _LONG_USER), _Row("assistant", _LONG_REPLY)]

    def test_state_reports_the_switch_and_the_worker_funnel(self) -> None:
        client = _FakeClient(_GOOD)
        host = _Host(_worker(client))
        state = host.second_thought_state()
        self.assertTrue(state["enabled"])
        self.assertTrue(state["worker_registered"])
        self.assertEqual(state["model"], "gpt-5.6-luna")
        self.assertIn("declined", state["stats"])
        self.assertEqual(state["cadence"]["cue_type"], "second_thought")
        self.assertEqual(state["pool"], [{"cue_type": "second_thought"}])

    def test_state_survives_a_missing_worker(self) -> None:
        """Off by default means the usual reading has no worker at all."""
        state = _Host(None).second_thought_state()
        self.assertFalse(state["worker_registered"])
        self.assertEqual(state["stats"], {})

    def test_forced_draft_replays_the_last_exchange(self) -> None:
        client = _FakeClient(_GOOD)
        host = _Host(_worker(client), rows=self._rows())
        out = host.force_second_thought_draft()
        self.assertTrue(out["drafted"])
        self.assertEqual(out["subject"], "his manager conversation")
        sent = client.calls[0]["messages"]
        self.assertEqual(sent[0]["content"], "SYSTEM")
        self.assertEqual(sent[1]["content"], _LONG_USER)
        self.assertEqual(sent[2]["content"], _LONG_REPLY)

    def test_forced_draft_still_obeys_the_master_switch(self) -> None:
        """A switch a debug tool can talk past is not a switch."""
        client = _FakeClient(_GOOD)
        host = _Host(
            _worker(client, second_thought_enabled=False), rows=self._rows(),
        )
        out = host.force_second_thought_draft()
        self.assertFalse(out["drafted"])
        self.assertEqual(client.calls, [])

    def test_no_turn_yet_is_an_answer_not_an_exception(self) -> None:
        client = _FakeClient(_GOOD)
        self.assertIn(
            "error", _Host(_worker(client), prompt="").force_second_thought_draft(),
        )
        self.assertIn(
            "error",
            _Host(_worker(client), rows=[_Row("user", "hi")])
            .force_second_thought_draft(),
        )


if __name__ == "__main__":
    unittest.main()
