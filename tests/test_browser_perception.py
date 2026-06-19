"""Tests for the browser perception layer (pure modules + orchestrator)."""
from __future__ import annotations

import json
import unittest

from app.core.browser.accessibility import A11yNode
from app.core.browser.adapters import (
    RealBrowserAdapter,
    get_adapter,
    parse_indented_tree,
    parse_json_tree,
)
from app.core.browser.grouping import dedup_nodes, group_forms, heading_context
from app.core.browser.page_state import PageStateMemory
from app.core.browser.perception import BrowserPerception
from app.core.browser.ranking import RankingWeights, rank_elements
from app.core.browser.rendering import render_page


_SAMPLE_TREE = """
- heading "Checkout" [level=1] [ref=e1]
- textbox "Email" [ref=e2]
- textbox "Card number" [ref=e3]
- button "Place order" [ref=e4]
- link "Home" [ref=e5]
- link "Home" [ref=e6]
""".strip()


def _perception(**overrides) -> BrowserPerception:
    defaults = dict(
        enabled=True,
        server_id="browser",
        snapshot_tools=("browser_snapshot",),
        adapter="real_browser",
        max_ranked_elements=40,
        weights=RankingWeights(),
        state_memory_pages=8,
    )
    defaults.update(overrides)
    return BrowserPerception(**defaults)


class IndentedTreeParseTests(unittest.TestCase):
    def test_parses_roles_names_refs(self) -> None:
        nodes = parse_indented_tree(_SAMPLE_TREE)
        assert nodes is not None
        self.assertEqual(len(nodes), 6)
        self.assertEqual(nodes[0].role, "heading")
        self.assertEqual(nodes[0].name, "Checkout")
        self.assertEqual(nodes[0].level, 1)
        self.assertEqual(nodes[1].role, "textbox")
        self.assertEqual(nodes[1].ref, "e2")
        self.assertTrue(nodes[3].is_submit)

    def test_disabled_and_hidden_flags(self) -> None:
        nodes = parse_indented_tree(
            '- button "Go" [ref=e1] [disabled]\n- link "X" [ref=e2] [hidden]'
        )
        assert nodes is not None
        self.assertTrue(nodes[0].disabled)
        self.assertFalse(nodes[1].visible)

    def test_unparseable_returns_none(self) -> None:
        self.assertIsNone(parse_indented_tree("???? %%%% ----"))

    def test_empty_returns_empty_list(self) -> None:
        self.assertEqual(parse_indented_tree("   "), [])


class JsonTreeParseTests(unittest.TestCase):
    def test_parses_nodes_container(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {"role": "button", "name": "OK", "ref": "e1"},
                    {"role": "textbox", "label": "Name", "id": "e2"},
                ]
            }
        )
        nodes = parse_json_tree(payload)
        assert nodes is not None
        self.assertEqual([n.role for n in nodes], ["button", "textbox"])
        self.assertEqual(nodes[0].name, "OK")
        self.assertEqual(nodes[1].name, "Name")

    def test_non_json_returns_none(self) -> None:
        self.assertIsNone(parse_json_tree("- button \"x\" [ref=e1]"))


class AdapterTests(unittest.TestCase):
    def test_real_browser_falls_back_to_tree(self) -> None:
        nodes = RealBrowserAdapter().parse(_SAMPLE_TREE)
        assert nodes is not None
        self.assertEqual(len(nodes), 6)

    def test_unknown_adapter_falls_back_to_generic(self) -> None:
        self.assertEqual(get_adapter("does-not-exist").name, "generic")


class GroupingTests(unittest.TestCase):
    def test_dedup_collapses_repeated_links(self) -> None:
        nodes = parse_indented_tree(_SAMPLE_TREE)
        assert nodes is not None
        deduped = dedup_nodes(nodes)
        homes = [n for n in deduped if n.name == "Home"]
        self.assertEqual(len(homes), 1)

    def test_dedup_keeps_inputs(self) -> None:
        nodes = [
            A11yNode(ref="a", role="textbox", name="Qty", dom_order=0),
            A11yNode(ref="b", role="textbox", name="Qty", dom_order=1),
        ]
        self.assertEqual(len(dedup_nodes(nodes)), 2)

    def test_heading_context_breadcrumb(self) -> None:
        nodes = parse_indented_tree(_SAMPLE_TREE)
        assert nodes is not None
        ctx = heading_context(nodes)
        # The email textbox sits under the Checkout heading.
        self.assertEqual(ctx[1], "Checkout")

    def test_group_forms_collects_inputs_and_submit(self) -> None:
        nodes = parse_indented_tree(_SAMPLE_TREE)
        assert nodes is not None
        ctx = heading_context(nodes)
        forms = group_forms(nodes, ctx)
        self.assertEqual(len(forms), 1)
        self.assertEqual(len(forms[0].inputs), 2)
        self.assertIsNotNone(forms[0].submit)
        self.assertEqual(forms[0].submit.name, "Place order")


class RankingTests(unittest.TestCase):
    def test_submit_outranks_plain_link(self) -> None:
        nodes = [
            A11yNode(ref="l", role="link", name="More", dom_order=0),
            A11yNode(ref="b", role="button", name="Submit", dom_order=1),
        ]
        ranked = rank_elements(nodes, {}, RankingWeights(), 40)
        self.assertEqual(ranked[0].node.ref, "b")

    def test_hidden_sinks(self) -> None:
        nodes = [
            A11yNode(ref="v", role="button", name="Visible", dom_order=0),
            A11yNode(ref="h", role="button", name="Hidden", dom_order=1, visible=False),
        ]
        ranked = rank_elements(nodes, {}, RankingWeights(), 40)
        self.assertEqual(ranked[-1].node.ref, "h")

    def test_cap_limits_results(self) -> None:
        nodes = [
            A11yNode(ref=f"e{i}", role="button", name=f"b{i}", dom_order=i)
            for i in range(10)
        ]
        ranked = rank_elements(nodes, {}, RankingWeights(), 3)
        self.assertEqual(len(ranked), 3)

    def test_non_interactive_excluded(self) -> None:
        nodes = [A11yNode(ref="h", role="heading", name="Title", dom_order=0)]
        self.assertEqual(rank_elements(nodes, {}, RankingWeights(), 40), [])


class PageStateTests(unittest.TestCase):
    def test_first_visit_no_diff(self) -> None:
        mem = PageStateMemory(max_pages=4)
        nodes = [A11yNode(ref="a", role="button", name="OK", dom_order=0)]
        self.assertIsNone(mem.update_and_diff("p1", nodes))

    def test_added_and_removed(self) -> None:
        mem = PageStateMemory(max_pages=4)
        first = [A11yNode(ref="a", role="button", name="OK", dom_order=0)]
        mem.update_and_diff("p1", first)
        second = [A11yNode(ref="b", role="button", name="Cancel", dom_order=0)]
        diff = mem.update_and_diff("p1", second)
        assert diff is not None
        self.assertTrue(any("Cancel" in a for a in diff.added))
        self.assertTrue(any("OK" in r for r in diff.removed))

    def test_lru_eviction(self) -> None:
        mem = PageStateMemory(max_pages=1)
        n = [A11yNode(ref="a", role="button", name="OK", dom_order=0)]
        mem.update_and_diff("p1", n)
        mem.update_and_diff("p2", n)
        self.assertEqual(len(mem), 1)


class RenderingTests(unittest.TestCase):
    def test_render_includes_header_and_summary(self) -> None:
        nodes = parse_indented_tree(_SAMPLE_TREE)
        assert nodes is not None
        ctx = heading_context(nodes)
        ranked = rank_elements(nodes, ctx, RankingWeights(), 40)
        content, summary = render_page(
            "Checkout", ranked, [], None, total_nodes=len(nodes)
        )
        self.assertIn("Page: Checkout", content)
        self.assertIn("Interactive elements", content)
        self.assertIn("top:", summary)


class BrowserPerceptionTests(unittest.TestCase):
    def test_claims_only_matching_server_and_tool(self) -> None:
        p = _perception()
        self.assertTrue(p.claims("browser", "browser_snapshot"))
        self.assertFalse(p.claims("browser", "browser_click"))
        self.assertFalse(p.claims("filesystem", "browser_snapshot"))

    def test_disabled_never_claims(self) -> None:
        self.assertFalse(_perception(enabled=False).claims("browser", "browser_snapshot"))

    def test_transform_reshapes_snapshot(self) -> None:
        p = _perception()
        result = p.transform("browser", "browser_snapshot", _SAMPLE_TREE, {})
        assert result is not None
        self.assertGreater(result.element_count, 0)
        self.assertIn("Place order", result.content)

    def test_transform_unparseable_returns_none(self) -> None:
        p = _perception()
        self.assertIsNone(
            p.transform("browser", "browser_snapshot", "???? %%%%", {})
        )

    def test_transform_non_claim_returns_none(self) -> None:
        p = _perception()
        self.assertIsNone(p.transform("browser", "browser_click", _SAMPLE_TREE, {}))

    def test_second_snapshot_shows_diff(self) -> None:
        p = _perception()
        p.transform("browser", "browser_snapshot", _SAMPLE_TREE, {"url": "u"})
        changed = _SAMPLE_TREE + '\n- button "New thing" [ref=e9]'
        result = p.transform("browser", "browser_snapshot", changed, {"url": "u"})
        assert result is not None
        self.assertIn("Changes since last snapshot", result.content)


if __name__ == "__main__":
    unittest.main()
