from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path

from app.core.tooling.config_loader import load_tooling_config


class ToolingConfigLoaderTests(unittest.TestCase):
    def test_merge_precedence_default_user_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            default_path = root / "tooling.default.json"
            user_path = root / "tooling.user.json"

            default_path.write_text(
                json.dumps(
                    {
                        "enabled_tools": ["ocr.extract_elements"],
                        "policies": {
                            "full_auto": False,
                            "max_tool_calls_per_turn": 4,
                        },
                        "tools": {
                            "mcp": {
                                "enabled": False,
                                "command": "uvx",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            user_path.write_text(
                json.dumps(
                    {
                        "policies": {
                            "full_auto": True,
                        },
                        "tools": {
                            "mcp": {
                                "enabled": True,
                                "args": ["windows-mcp"],
                            }
                        },
                    }
                ),
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
            mcp = config.tool_settings("mcp")
            self.assertTrue(mcp.get("enabled"))
            self.assertEqual(mcp.get("command"), "uvx")


if __name__ == "__main__":
    unittest.main()
