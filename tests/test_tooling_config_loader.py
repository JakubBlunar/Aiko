from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.core.tooling.config_loader import load_tooling_config


class ToolingConfigLoaderTests(unittest.TestCase):
    def test_merge_precedence_default_user_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            default_path = root / "tooling.default.yaml"
            user_path = root / "tooling.user.yaml"

            default_path.write_text(
                """
enabled_tools:
  - ocr.extract_elements
policies:
  full_auto: false
  max_tool_calls_per_turn: 4
""".strip(),
                encoding="utf-8",
            )
            user_path.write_text(
                """
policies:
  full_auto: true
""".strip(),
                encoding="utf-8",
            )

            config = load_tooling_config(
                default_path=default_path,
                user_path=user_path,
                runtime_overrides={"policies": {"max_tool_calls_per_turn": 9}},
            )

            self.assertEqual(config.enabled_tools, ["ocr.extract_elements"])
            self.assertTrue(config.policies.full_auto)
            self.assertEqual(config.policies.max_tool_calls_per_turn, 9)


if __name__ == "__main__":
    unittest.main()
