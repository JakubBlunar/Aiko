"""Tests for the SDK-primary plugin loader (pure stub discovery + config)."""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.plugins.loader import (
    SUPPORTED_PLUGIN_API_VERSION,
    discover_plugins,
    parse_manifest,
    parse_skill_md,
    resolve_plugin_config,
)


def _write_plugin(
    root: Path,
    plugin_id: str,
    manifest: dict,
    *,
    skill_md: str | None = None,
    default_config: dict | None = None,
    user_config: dict | None = None,
    requirements: str | None = None,
    entry_py: str | None = None,
) -> Path:
    pdir = root / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    if skill_md is not None:
        sdir = pdir / "skills"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if default_config is not None or user_config is not None:
        cdir = pdir / "config"
        cdir.mkdir(parents=True, exist_ok=True)
        if default_config is not None:
            (cdir / "default.json").write_text(
                json.dumps(default_config), encoding="utf-8"
            )
        if user_config is not None:
            (cdir / "user.json").write_text(
                json.dumps(user_config), encoding="utf-8"
            )
    if requirements is not None:
        (pdir / "requirements.txt").write_text(requirements, encoding="utf-8")
    if entry_py is not None:
        (pdir / "entry.py").write_text(entry_py, encoding="utf-8")
    return pdir


_STUB = {
    "plugin_api_version": 1,
    "id": "filesystem",
    "name": "Filesystem",
    "enabled": True,
}

_FS_SKILL = (
    "---\n"
    "name: filesystem\n"
    "description: Sandboxed file read/write\n"
    "---\n"
    "Use absolute paths under the sandbox root.\n"
)


class ParseSkillMdTests(unittest.TestCase):
    def test_frontmatter_and_body(self) -> None:
        skill = parse_skill_md(_FS_SKILL, source_path="x/SKILL.md")
        self.assertEqual(skill.name, "filesystem")
        self.assertEqual(skill.description, "Sandboxed file read/write")
        self.assertIn("absolute paths", skill.body)
        self.assertEqual(skill.source_path, "x/SKILL.md")

    def test_no_frontmatter_is_all_body(self) -> None:
        skill = parse_skill_md("just a body, no frontmatter")
        self.assertEqual(skill.name, "")
        self.assertEqual(skill.body, "just a body, no frontmatter")


class ParseManifestTests(unittest.TestCase):
    def test_strict_json(self) -> None:
        self.assertEqual(parse_manifest('{"id": "a"}')["id"], "a")

    def test_tolerates_comments_and_trailing_commas(self) -> None:
        text = (
            "{\n"
            "  // a line comment\n"
            '  "id": "a",\n'
            "  /* block */\n"
            '  "name": "A",\n'
            "}\n"
        )
        parsed = parse_manifest(text)
        self.assertEqual(parsed["id"], "a")
        self.assertEqual(parsed["name"], "A")

    def test_non_object_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_manifest("[1, 2, 3]")


class ResolvePluginConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_precedence_default_lt_user_lt_entry(self) -> None:
        pdir = _write_plugin(
            self.root, "p", _STUB,
            default_config={"a": 1, "b": 1, "c": 1},
            user_config={"b": 2, "c": 2},
        )
        merged = resolve_plugin_config(pdir, {"c": 3})
        self.assertEqual(merged, {"a": 1, "b": 2, "c": 3})

    def test_deep_merge_nested(self) -> None:
        pdir = _write_plugin(
            self.root, "p", _STUB,
            default_config={"weights": {"role": 1.0, "text": 1.0}},
            user_config={"weights": {"text": 2.0}},
        )
        merged = resolve_plugin_config(pdir, None)
        self.assertEqual(merged["weights"], {"role": 1.0, "text": 2.0})

    def test_missing_files_empty(self) -> None:
        pdir = _write_plugin(self.root, "p", _STUB)
        self.assertEqual(resolve_plugin_config(pdir, None), {})


class DiscoverPluginsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_reads_stub_identity_and_config(self) -> None:
        _write_plugin(
            self.root, "filesystem", _STUB,
            default_config={"root": "/base"},
            user_config={"root": "/override"},
        )
        stubs = discover_plugins([str(self.root)])
        self.assertEqual(len(stubs), 1)
        s = stubs[0]
        self.assertEqual(s.id, "filesystem")
        self.assertEqual(s.name, "Filesystem")
        self.assertTrue(s.enabled)
        self.assertFalse(s.unsupported)
        self.assertEqual(s.config["root"], "/override")

    def test_entry_config_wins_last(self) -> None:
        _write_plugin(
            self.root, "filesystem", _STUB,
            default_config={"root": "/base"},
        )
        stubs = discover_plugins(
            [str(self.root)],
            entries={"filesystem": {"config": {"root": "/central"}}},
        )
        self.assertEqual(stubs[0].config["root"], "/central")

    def test_entry_enabled_override(self) -> None:
        _write_plugin(self.root, "filesystem", {**_STUB, "enabled": True})
        stubs = discover_plugins(
            [str(self.root)], entries={"filesystem": {"enabled": False}}
        )
        self.assertFalse(stubs[0].enabled)

    def test_disabled_manifest(self) -> None:
        _write_plugin(self.root, "filesystem", {**_STUB, "enabled": False})
        self.assertFalse(discover_plugins([str(self.root)])[0].enabled)

    def test_unsupported_api_version_flagged(self) -> None:
        _write_plugin(
            self.root, "filesystem",
            {**_STUB, "plugin_api_version": SUPPORTED_PLUGIN_API_VERSION + 1},
        )
        s = discover_plugins([str(self.root)])[0]
        self.assertTrue(s.unsupported)

    def test_python_dependencies_from_manifest_and_requirements(self) -> None:
        _write_plugin(
            self.root, "p",
            {**_STUB, "id": "p", "python_dependencies": ["foo==1.0"]},
            requirements="bar>=2\n# comment\n\nbaz\n",
        )
        s = discover_plugins([str(self.root)])[0]
        self.assertEqual(s.python_dependencies, ["foo==1.0", "bar>=2", "baz"])

    def test_no_id_dropped(self) -> None:
        _write_plugin(self.root, "broken", {"name": "x"})
        self.assertEqual(discover_plugins([str(self.root)]), [])

    def test_precedence_first_root_wins(self) -> None:
        root_a = self.root / "a"
        root_b = self.root / "b"
        _write_plugin(root_a, "filesystem", {**_STUB, "name": "FromA"})
        _write_plugin(root_b, "filesystem", {**_STUB, "name": "FromB"})
        stubs = discover_plugins([str(root_a), str(root_b)])
        self.assertEqual(len(stubs), 1)
        self.assertEqual(stubs[0].name, "FromA")


if __name__ == "__main__":
    unittest.main()
