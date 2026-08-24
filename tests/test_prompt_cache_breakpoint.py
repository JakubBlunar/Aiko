"""Explicit prompt-cache breakpoints (GPT-5.6+).

Why this exists: GPT-5.6 matches the prompt cache *only* at breakpoints
and dropped the best-effort "longest matching unmarked prefix" fallback
every earlier model had. Aiko's prompt is a ~44k-char byte-stable head
followed by per-turn blocks, so under the default implicit breakpoint the
whole thing was re-written at the 1.25x cache-write rate on every single
turn and read back never -- measured 6 hits in 1,078 turns. These tests
pin the two halves of the fix: the assembler marking where its stable
head ends, and the client turning that offset into a real breakpoint
without tripping the documented "explicit mode with no breakpoint
disables caching" footgun.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.core.infra.settings import load_settings
from app.core.session.prompt_assembler import (
    _cache_breakpoints,
    _stable_prefix_offset,
)
from app.llm.chat_client import CACHE_BREAKPOINT_KEY
from app.llm.openai_compatible_client import (
    _MAX_CACHE_BREAKPOINTS,
    OpenAICompatibleClient,
    _cache_breakpoint_offsets,
    _messages_to_responses_input,
    _supports_explicit_cache_breakpoints,
)


def _client(model: str) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        load_settings().ollama,
        base_url="https://api.openai.com/v1",
        model=model,
        api_key="sk-test",
    )


class StablePrefixOffsetTests(unittest.TestCase):
    """The assembler's half: where does the reusable prefix end?"""

    def test_offset_lands_on_a_real_join_boundary(self) -> None:
        # Mirrors assemble_with_budget: same separator, same empty filter.
        head = ["A" * 4000, "B" * 3000]
        parts = [*head, "volatile"]
        offset = _stable_prefix_offset(parts, len(head))
        system_prompt = "\n\n---\n\n".join(p for p in parts if p)
        self.assertEqual(
            system_prompt[:offset], "\n\n---\n\n".join(head),
        )
        # And the tail is exactly the volatile remainder.
        self.assertEqual(system_prompt[offset:], "\n\n---\n\nvolatile")

    def test_empty_parts_are_filtered_consistently(self) -> None:
        parts = ["A" * 4000, "", "B" * 3000, "volatile"]
        offset = _stable_prefix_offset(parts, 3)
        system_prompt = "\n\n---\n\n".join(p for p in parts if p)
        self.assertEqual(
            system_prompt[:offset], "A" * 4000 + "\n\n---\n\n" + "B" * 3000,
        )

    def test_short_head_is_refused(self) -> None:
        # Below the 1,024-token floor a breakpoint is worse than none: it
        # would suppress the implicit one and cache nothing.
        self.assertEqual(_stable_prefix_offset(["tiny persona"], 1), 0)

    def test_no_head_is_refused(self) -> None:
        self.assertEqual(_stable_prefix_offset(["A" * 9000], 0), 0)


class BreakpointSelectionTests(unittest.TestCase):
    """Which boundaries get marked, given the tier layout."""

    # The head has to clear the token floor on its own; the later tiers
    # are small on purpose, which is what they measure as in practice.
    #             head   T0 tail  T1       T2       T3       volatile
    PARTS = ["A" * 9000, "b" * 9, "c" * 9, "d" * 9, "e" * 9, "volatile"]

    def test_all_four_tier_boundaries_are_marked(self) -> None:
        offsets = _cache_breakpoints(
            self.PARTS, head=1, t0_end=2, t2_end=4, t3_end=5,
        )
        self.assertEqual(offsets, (
            _stable_prefix_offset(self.PARTS, 1),
            _stable_prefix_offset(self.PARTS, 2),
            _stable_prefix_offset(self.PARTS, 4),
            _stable_prefix_offset(self.PARTS, 5),
        ))

    def test_offsets_are_ascending(self) -> None:
        offsets = _cache_breakpoints(
            self.PARTS, head=1, t0_end=2, t2_end=4, t3_end=5,
        )
        self.assertEqual(sorted(offsets), list(offsets))

    def test_never_more_than_the_service_limit(self) -> None:
        # Over four markers the whole request 400s, so the assembler must
        # not be able to ask for a fifth.
        offsets = _cache_breakpoints(
            self.PARTS, head=1, t0_end=2, t2_end=4, t3_end=5,
        )
        self.assertLessEqual(len(offsets), _MAX_CACHE_BREAKPOINTS)

    def test_empty_tiers_collapse_instead_of_duplicating(self) -> None:
        # T0 tail, T1, T2 and T3 all absent: every later boundary lands
        # on the same character as the head, and duplicates would emit
        # empty content blocks.
        parts = ["A" * 9000, "", "", "", "", "volatile"]
        self.assertEqual(
            _cache_breakpoints(parts, head=1, t0_end=2, t2_end=4, t3_end=5),
            (_stable_prefix_offset(parts, 1),),
        )

    def test_missing_t3_does_not_duplicate_t2(self) -> None:
        # relevant_context absent -> the assembler passes t3_end == t2_end.
        offsets = _cache_breakpoints(
            self.PARTS, head=1, t0_end=2, t2_end=4, t3_end=4,
        )
        self.assertEqual(len(set(offsets)), len(offsets))
        self.assertEqual(len(offsets), 3)

    def test_no_stable_head_means_no_breakpoints_at_all(self) -> None:
        # Without a cacheable head there is nothing to anchor on, and a
        # lone later mark would suppress the implicit breakpoint while
        # matching far less often.
        self.assertEqual(
            _cache_breakpoints(
                ["A" * 9000], head=0, t0_end=1, t2_end=1, t3_end=1,
            ),
            (),
        )


class AssemblerMarksSystemMessageTests(unittest.TestCase):
    """The offset has to survive onto the real system message."""

    def test_marked_prefix_is_persona_plus_addenda_and_nothing_after(
        self,
    ) -> None:
        from tests.test_prompt_assembler import (  # noqa: PLC0415
            _TempDb,
            _make_assembler,
        )

        # A persona long enough to clear the 1,024-token floor, with a
        # sentinel at its very end so we can prove where the cut landed.
        persona = ("Aiko is warm and direct. " * 400) + "PERSONA-TAIL"
        with _TempDb() as db:
            assembler = _make_assembler(db, persona)
            messages, _telemetry = assembler.assemble_with_budget(
                "s1", "hello", context_window=32768, response_budget=512,
            )
        system = messages[0]
        self.assertEqual(system["role"], "system")
        offsets = system[CACHE_BREAKPOINT_KEY]
        content = system["content"]
        head = content[:offsets[0]]
        # The persona is inside the cached prefix -- that is the point.
        self.assertIn("PERSONA-TAIL", head)
        # And so are the constant grammar addenda that follow it.
        self.assertIn("[[laugh]]", head)
        # The head is a strict prefix ending on a block boundary, never
        # mid-block, so it cannot slice a sentence in half.
        self.assertTrue(content.startswith(head))
        for offset in offsets:
            self.assertTrue(
                offset == len(content)
                or content[offset:].startswith("\n\n---\n\n"),
                "breakpoint must land on a join boundary",
            )


class ModelGateTests(unittest.TestCase):
    """Breakpoints are a 400 on models that predate them."""

    def test_five_six_and_later_supported(self) -> None:
        for model in (
            "gpt-5.6", "gpt-5.6-luna", "gpt-5.7-mini",
            "openai/gpt-5.6", "gpt-6.0", "gpt-10.1",
        ):
            with self.subTest(model=model):
                self.assertTrue(_supports_explicit_cache_breakpoints(model))

    def test_earlier_models_and_other_vendors_refused(self) -> None:
        for model in (
            "gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.1", "gpt-5",
            "gpt-4o", "grok-4.3", "o3-mini", "llama3.1", "",
        ):
            with self.subTest(model=model):
                self.assertFalse(_supports_explicit_cache_breakpoints(model))

    def test_non_string_is_refused(self) -> None:
        self.assertFalse(_supports_explicit_cache_breakpoints(None))


class OffsetValidationTests(unittest.TestCase):
    """A bad offset must degrade to "no breakpoint", never raise or misslice."""

    def test_absent_key(self) -> None:
        self.assertEqual(
            _cache_breakpoint_offsets({"content": "abc"}), (),
        )

    def test_past_end_of_content(self) -> None:
        msg = {"content": "abc", CACHE_BREAKPOINT_KEY: 99}
        self.assertEqual(_cache_breakpoint_offsets(msg), ())

    def test_zero_and_negative(self) -> None:
        for bad in (0, -5):
            msg = {"content": "abcdef", CACHE_BREAKPOINT_KEY: bad}
            self.assertEqual(_cache_breakpoint_offsets(msg), ())

    def test_garbage_value(self) -> None:
        msg = {"content": "abcdef", CACHE_BREAKPOINT_KEY: "nope"}
        self.assertEqual(_cache_breakpoint_offsets(msg), ())

    def test_exactly_at_end_is_valid(self) -> None:
        msg = {"content": "abcdef", CACHE_BREAKPOINT_KEY: 6}
        self.assertEqual(_cache_breakpoint_offsets(msg), (6,))

    def test_bare_int_still_accepted(self) -> None:
        msg = {"content": "abcdef", CACHE_BREAKPOINT_KEY: 3}
        self.assertEqual(_cache_breakpoint_offsets(msg), (3,))

    def test_sequence_is_sorted_and_deduplicated(self) -> None:
        # An unsorted pair would mark a later prefix first, and a repeat
        # would emit an empty content block.
        msg = {"content": "abcdef", CACHE_BREAKPOINT_KEY: [4, 2, 4]}
        self.assertEqual(_cache_breakpoint_offsets(msg), (2, 4))

    def test_one_bad_member_does_not_sink_the_good_ones(self) -> None:
        msg = {"content": "abcdef", CACHE_BREAKPOINT_KEY: (2, 999, "x", 5)}
        self.assertEqual(_cache_breakpoint_offsets(msg), (2, 5))

    def test_capped_at_the_service_limit(self) -> None:
        msg = {
            "content": "a" * 20,
            CACHE_BREAKPOINT_KEY: [2, 4, 6, 8, 10, 12],
        }
        offsets = _cache_breakpoint_offsets(msg)
        self.assertEqual(len(offsets), _MAX_CACHE_BREAKPOINTS)
        # The earliest ones are kept: they are the prefixes most likely
        # to still match.
        self.assertEqual(offsets, (2, 4, 6, 8))


class ResponsesInputShapeTests(unittest.TestCase):
    """The wire shape of the two-block split."""

    def test_split_into_two_marked_blocks(self) -> None:
        messages = [
            {
                "role": "system",
                "content": "STABLE|volatile",
                CACHE_BREAKPOINT_KEY: 6,
            },
            {"role": "user", "content": "hi"},
        ]
        out = _messages_to_responses_input(
            messages, mark_cache_breakpoint=True,
        )
        self.assertEqual(out[0], {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": "STABLE",
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {"type": "input_text", "text": "|volatile"},
            ],
        })
        # The user turn stays a plain string.
        self.assertEqual(out[1], {"role": "user", "content": "hi"})

    def test_two_offsets_give_three_blocks_two_marked(self) -> None:
        out = _messages_to_responses_input(
            [{
                "role": "system",
                "content": "HEAD|MID|volatile",
                CACHE_BREAKPOINT_KEY: (4, 8),
            }],
            mark_cache_breakpoint=True,
        )
        self.assertEqual(out[0]["content"], [
            {
                "type": "input_text",
                "text": "HEAD",
                "prompt_cache_breakpoint": {"mode": "explicit"},
            },
            {
                "type": "input_text",
                "text": "|MID",
                "prompt_cache_breakpoint": {"mode": "explicit"},
            },
            {"type": "input_text", "text": "|volatile"},
        ])

    def test_no_content_is_lost_or_reordered(self) -> None:
        content = "STABLE|volatile"
        for offsets in (6, (4, 8), (3, 6, 9)):
            with self.subTest(offsets=offsets):
                out = _messages_to_responses_input(
                    [{
                        "role": "system",
                        "content": content,
                        CACHE_BREAKPOINT_KEY: offsets,
                    }],
                    mark_cache_breakpoint=True,
                )
                rejoined = "".join(b["text"] for b in out[0]["content"])
                self.assertEqual(rejoined, content)

    def test_no_block_is_ever_empty(self) -> None:
        # An empty ``input_text`` is a wasted block at best and a 400 at
        # worst, and it is what an unvalidated duplicate offset produces.
        out = _messages_to_responses_input(
            [{
                "role": "system",
                "content": "abcdef",
                CACHE_BREAKPOINT_KEY: (3, 3, 6),
            }],
            mark_cache_breakpoint=True,
        )
        for block in out[0]["content"]:
            self.assertTrue(block["text"])

    def test_tail_block_omitted_when_split_at_end(self) -> None:
        out = _messages_to_responses_input(
            [{"role": "system", "content": "ALL", CACHE_BREAKPOINT_KEY: 3}],
            mark_cache_breakpoint=True,
        )
        self.assertEqual(len(out[0]["content"]), 1)
        self.assertIn("prompt_cache_breakpoint", out[0]["content"][0])

    def test_flag_off_leaves_a_plain_string(self) -> None:
        messages = [
            {"role": "system", "content": "abcdef", CACHE_BREAKPOINT_KEY: 3},
        ]
        out = _messages_to_responses_input(messages)
        self.assertEqual(out, [{"role": "system", "content": "abcdef"}])

    def test_private_key_never_reaches_the_input_item(self) -> None:
        out = _messages_to_responses_input(
            [{"role": "system", "content": "abcdef", CACHE_BREAKPOINT_KEY: 3}],
            mark_cache_breakpoint=True,
        )
        self.assertNotIn(CACHE_BREAKPOINT_KEY, out[0])

    def test_caller_list_is_not_mutated(self) -> None:
        msg = {"role": "system", "content": "abcdef", CACHE_BREAKPOINT_KEY: 3}
        _messages_to_responses_input([msg], mark_cache_breakpoint=True)
        self.assertEqual(msg["content"], "abcdef")
        self.assertEqual(msg[CACHE_BREAKPOINT_KEY], 3)


class ResponsesPayloadTests(unittest.TestCase):
    """End-to-end payload assembly, including the explicit-mode footgun."""

    def _payload(self, model: str, messages: list[dict]) -> dict:
        client = _client(model)
        return client._build_responses_payload(
            messages=messages,
            model=model,
            options={"prompt_cache_key": "default:abc"},
            tools=None,
            stream=False,
            format_json=False,
            tool_choice=None,
        )

    def _marked(self) -> list[dict]:
        return [
            {
                "role": "system",
                "content": "STABLE|volatile",
                CACHE_BREAKPOINT_KEY: 6,
            },
            {"role": "user", "content": "hi"},
        ]

    def test_explicit_mode_set_when_a_breakpoint_is_emitted(self) -> None:
        payload = self._payload("gpt-5.6-luna", self._marked())
        self.assertEqual(
            payload["prompt_cache_options"], {"mode": "explicit"},
        )
        self.assertIsInstance(payload["input"][0]["content"], list)
        # prompt_cache_key is required for 5.6's reliable matching.
        self.assertEqual(payload["prompt_cache_key"], "default:abc")

    def test_explicit_mode_never_set_without_a_breakpoint(self) -> None:
        # The documented footgun: explicit mode with no breakpoint turns
        # caching OFF entirely rather than falling back to implicit.
        payload = self._payload(
            "gpt-5.6-luna", [{"role": "user", "content": "hi"}],
        )
        self.assertNotIn("prompt_cache_options", payload)

    def test_unusable_offset_falls_back_to_implicit(self) -> None:
        messages = [
            {"role": "system", "content": "abc", CACHE_BREAKPOINT_KEY: 999},
        ]
        payload = self._payload("gpt-5.6-luna", messages)
        self.assertNotIn("prompt_cache_options", payload)
        self.assertEqual(payload["input"][0]["content"], "abc")

    def test_older_responses_model_gets_no_breakpoint(self) -> None:
        # gpt-5.4 routes through /v1/responses but predates breakpoints.
        payload = self._payload("gpt-5.4", self._marked())
        self.assertNotIn("prompt_cache_options", payload)
        self.assertEqual(
            payload["input"][0]["content"], "STABLE|volatile",
        )

    def test_grok_is_left_alone(self) -> None:
        client = OpenAICompatibleClient(
            load_settings().ollama,
            base_url="https://api.x.ai/v1",
            model="grok-4.3",
            api_key="sk-test",
            api_style="responses",
        )
        payload = client._build_responses_payload(
            messages=self._marked(),
            model="grok-4.3",
            options={"prompt_cache_key": "default:abc"},
            tools=None,
            stream=False,
            format_json=False,
            tool_choice=None,
        )
        self.assertNotIn("prompt_cache_options", payload)
        self.assertEqual(payload["input"][0]["content"], "STABLE|volatile")


class ChatCompletionsStripTests(unittest.TestCase):
    """The bare key is an unknown field on /v1/chat/completions."""

    def test_key_stripped_from_the_wire(self) -> None:
        client = _client("gpt-4o-mini")
        messages = [
            {"role": "system", "content": "abcdef", CACHE_BREAKPOINT_KEY: 3},
            {"role": "user", "content": "hi"},
        ]
        from tests.test_openai_compatible_client import (  # noqa: PLC0415
            _fake_chat_response,
        )

        with patch(
            "app.llm.openai_compatible_client.requests.post",
            return_value=_fake_chat_response(content="ok"),
        ) as posted:
            client.chat_with_tools(messages)
        wire = posted.call_args.kwargs["json"]["messages"]
        for m in wire:
            self.assertNotIn(CACHE_BREAKPOINT_KEY, m)
        # Content itself must survive intact.
        self.assertEqual(wire[0]["content"], "abcdef")


if __name__ == "__main__":
    unittest.main()
