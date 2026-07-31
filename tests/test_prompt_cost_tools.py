"""P31a -- the ``get_prompt_block_costs`` MCP surface.

The interesting behaviour isn't the JSON shape, it's the ranking: a large
cached T0 block must NOT outrank a small per-turn T6 block, because that
is precisely the mistake the raw sizes invite.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from app.mcp.server_tools import prompt_cost_tools as pct


class _FakeMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn

        return deco


def _session(block_chars: dict[str, int] | None, **extra) -> SimpleNamespace:
    snapshot = {
        "prompt": "irrelevant",
        "system_tokens": 900,
        "mode": "typed",
        "captured_at": 1234.5,
        "block_chars": block_chars,
    }
    snapshot.update(extra)
    return SimpleNamespace(get_last_system_prompt=lambda: dict(snapshot))


class AvailabilityTests(unittest.TestCase):
    def test_registers_and_documents_itself(self) -> None:
        mcp = _FakeMCP()
        pct.register(mcp, _session({}))
        self.assertEqual(set(mcp.tools), {"get_prompt_block_costs"})
        self.assertTrue((mcp.tools["get_prompt_block_costs"].__doc__ or "").strip())

    def test_explains_itself_before_the_first_turn(self) -> None:
        out = pct.build_report(_session(None))
        self.assertFalse(out["available"])
        self.assertIn("send a message", out["reason"])

    def test_returns_json(self) -> None:
        mcp = _FakeMCP()
        pct.register(mcp, _session({"persona": 400}))
        payload = json.loads(mcp.tools["get_prompt_block_costs"]())
        self.assertTrue(payload["available"])


class RankingTests(unittest.TestCase):
    def test_a_volatile_block_outranks_a_larger_cached_one(self) -> None:
        # 20k chars of persona (T0, weight 0.05) vs 4k of a T6 detector
        # (weight 1.0). By raw size the persona wins; by real per-turn
        # cost the detector does, and that's the whole point of the tool.
        out = pct.build_report(_session({
            "persona": 20_000,
            "clarification_block": 4_000,
        }))
        ranked = [row["block"] for row in out["top_by_effective_cost"]]
        self.assertEqual(ranked[0], "clarification_block")
        by_block = {row["block"]: row for row in out["top_by_effective_cost"]}
        self.assertGreater(
            by_block["persona"]["tokens"],
            by_block["clarification_block"]["tokens"],
            "raw token ordering should still be persona-first",
        )

    def test_top_limits_the_list_but_not_the_rollup(self) -> None:
        chars = {f"b{i}": (i + 1) * 100 for i in range(10)}
        out = pct.build_report(_session(chars), top=3)
        self.assertEqual(len(out["top_by_effective_cost"]), 3)
        self.assertEqual(out["blocks_registered"], 10)

    def test_unregistered_blocks_get_full_weight_not_a_crash(self) -> None:
        out = pct.build_report(_session({"not_in_the_ladder": 1000}))
        row = out["top_by_effective_cost"][0]
        self.assertEqual(row["tier"], "unregistered")
        self.assertEqual(row["effective_tokens"], float(row["tokens"]))


class SilentBlockTests(unittest.TestCase):
    def test_empty_blocks_are_listed_separately_not_ranked(self) -> None:
        out = pct.build_report(_session({
            "persona": 500,
            "tension_block": 0,
            "appreciation_block": 0,
        }))
        self.assertEqual(
            out["silent_blocks"], ["appreciation_block", "tension_block"],
        )
        self.assertEqual(out["blocks_rendered"], 1)
        self.assertEqual(
            [r["block"] for r in out["top_by_effective_cost"]], ["persona"],
        )


class TierRollupTests(unittest.TestCase):
    def test_groups_by_tier_with_rendered_counts(self) -> None:
        out = pct.build_report(_session({
            "persona": 1000,
            "petname_block": 0,
            "affect_block": 200,
        }))
        t0 = out["by_tier"]["T0_stable"]
        self.assertEqual(t0["blocks"], 2)
        self.assertEqual(t0["rendered"], 1)
        self.assertIn("T5_affect_style", out["by_tier"])

    def test_every_ladder_tier_has_a_weight(self) -> None:
        from app.core.session.prompt_assembler import _PROMPT_BLOCK_TIERS

        missing = sorted(set(_PROMPT_BLOCK_TIERS) - set(pct._TIER_WEIGHT))
        self.assertEqual(
            missing, [],
            f"new prompt tiers need a cache-miss weight: {missing}",
        )


if __name__ == "__main__":
    unittest.main()
