"""Tests for Playwright MCP browser_snapshot string compression."""
from __future__ import annotations

import unittest

from app.llm.browser_snapshot_compress import (
    BrowserSnapshotCompressOptions,
    browser_snapshot_options_from_agent_settings,
    compress_browser_snapshot,
    compress_browser_snapshot_with_options,
)


class BrowserSnapshotCompressTests(unittest.TestCase):
    def test_disabled_returns_unchanged(self) -> None:
        raw = "x" * 20_000
        out = compress_browser_snapshot(raw, enabled=False, max_chars=1000, max_text_run=100)
        self.assertEqual(out, raw)

    def test_preserves_ref_tokens(self) -> None:
        line = '- link "ok" [ref=e99] [cursor=pointer]'
        out = compress_browser_snapshot(
            line,
            enabled=True,
            max_chars=None,
            max_text_run=500,
        )
        self.assertIn("[ref=e99]", out)

    def test_truncates_long_segment_between_refs(self) -> None:
        filler = "Z" * 400
        line = f'- generic [ref=e1]: {filler} middle {filler} end [ref=e2]'
        out = compress_browser_snapshot(
            line,
            enabled=True,
            max_chars=None,
            max_text_run=120,
        )
        self.assertIn("[ref=e1]", out)
        self.assertIn("[ref=e2]", out)
        self.assertIn("chars omitted", out)
        self.assertLess(len(out), len(line))

    def test_max_chars_drops_non_ref_lines_first(self) -> None:
        lines = ["x" * 200, "- btn [ref=e1]", "y" * 200, "- link [ref=e2]", "z" * 200]
        text = "\n".join(lines)
        out = compress_browser_snapshot(
            text,
            enabled=True,
            max_chars=180,
            max_text_run=500,
        )
        self.assertIn("[ref=e1]", out)
        self.assertIn("[ref=e2]", out)
        self.assertLess(len(out), len(text))
        self.assertIn("truncated", out.lower())

    def test_collapse_excessive_blank_lines(self) -> None:
        text = "a\n\n\n\nb"
        out = compress_browser_snapshot(
            text,
            enabled=True,
            max_chars=None,
            max_text_run=500,
        )
        self.assertNotIn("\n\n\n", out)

    def test_with_options_dataclass(self) -> None:
        opts = BrowserSnapshotCompressOptions(enabled=True, max_chars=10_000, max_text_run=100)
        out = compress_browser_snapshot_with_options("short [ref=e1]", opts)
        self.assertIn("[ref=e1]", out)

    def test_options_from_agent_settings_fallback(self) -> None:
        o = browser_snapshot_options_from_agent_settings(
            browser_snapshot_compress=True,
            browser_snapshot_max_chars=None,
            browser_snapshot_max_text_run=None,
            compress_tool_results_limit=4000,
            compress_token_limit=None,
        )
        self.assertTrue(o.enabled)
        self.assertEqual(o.max_chars, 4000)

    def test_options_from_agent_settings_token_derived(self) -> None:
        o = browser_snapshot_options_from_agent_settings(
            browser_snapshot_compress=True,
            browser_snapshot_max_chars=None,
            browser_snapshot_max_text_run=None,
            compress_tool_results_limit=None,
            compress_token_limit=1000,
        )
        self.assertEqual(o.max_chars, int(1000 * 3.5))


if __name__ == "__main__":
    unittest.main()
