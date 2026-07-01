"""Tests for the plugin activation runtime (imports entry.py, runs deps)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.plugins.loader import PluginStub, discover_plugins
from app.plugins import runtime


def _write(root: Path, plugin_id: str, manifest: dict, *, entry: str = "",
           skill_md: str | None = None, config: dict | None = None) -> None:
    pdir = root / plugin_id
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    if entry:
        (pdir / "entry.py").write_text(entry, encoding="utf-8")
    if skill_md is not None:
        sdir = pdir / "skills"
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if config is not None:
        cdir = pdir / "config"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "default.json").write_text(json.dumps(config), encoding="utf-8")


_STUB = {"plugin_api_version": 1, "id": "p", "name": "P", "enabled": True}


class ActivateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _stub(self) -> PluginStub:
        return discover_plugins([str(self.root)])[0]

    def test_active_server_and_guidance(self) -> None:
        _write(
            self.root, "p", _STUB,
            entry=(
                "def define_plugin(api):\n"
                "    api.register_mcp_server(command='npx', args=['-y','srv'])\n"
                "    api.register_skills('skills')\n"
            ),
            skill_md="---\nname: p\n---\nUse it well.\n",
        )
        result = runtime.activate_plugin(self._stub())
        self.assertEqual(result.status, "active")
        self.assertIsNotNone(result.server)
        self.assertEqual(result.server.command, "npx")
        self.assertIn("mcp:p", result.group_guidance)
        self.assertIn("Use it well", result.group_guidance["mcp:p"])

    def test_middleware_only_active(self) -> None:
        _write(
            self.root, "p", _STUB,
            entry=(
                "from app.plugins.sdk import MiddlewareResult\n"
                "class M:\n"
                "    def claims(self, s, t):\n        return True\n"
                "    def transform(self, s, t, txt, a=None):\n"
                "        return MiddlewareResult('c', 'summ', 0)\n"
                "def define_plugin(api):\n"
                "    api.register_tool_result_middleware(M())\n"
            ),
        )
        result = runtime.activate_plugin(self._stub())
        self.assertEqual(result.status, "active")
        self.assertEqual(len(result.middlewares), 1)
        self.assertIsNone(result.server)

    def test_gated_out_on_require(self) -> None:
        _write(
            self.root, "p", _STUB,
            entry=(
                "def define_plugin(api):\n"
                "    api.require_config('root')\n"
                "    api.register_mcp_server(command='npx')\n"
            ),
        )
        result = runtime.activate_plugin(self._stub())
        self.assertEqual(result.status, "gated_out")
        self.assertIn("root", result.reason)
        self.assertIsNone(result.server)

    def test_gated_out_then_active_with_config(self) -> None:
        _write(
            self.root, "p", _STUB,
            entry=(
                "def define_plugin(api):\n"
                "    root = api.require_config('root')\n"
                "    api.register_mcp_server(command='npx', args=[root])\n"
            ),
            config={"root": "/base"},
        )
        result = runtime.activate_plugin(self._stub())
        self.assertEqual(result.status, "active")
        self.assertIn("/base", result.server.args)

    def test_missing_entry_invalid(self) -> None:
        _write(self.root, "p", _STUB)  # no entry.py
        result = runtime.activate_plugin(self._stub())
        self.assertEqual(result.status, "invalid")
        self.assertIn("entry", result.reason)

    def test_no_define_plugin_invalid(self) -> None:
        _write(self.root, "p", _STUB, entry="x = 1\n")
        result = runtime.activate_plugin(self._stub())
        self.assertEqual(result.status, "invalid")
        self.assertIn("define_plugin", result.reason)

    def test_registers_nothing_invalid(self) -> None:
        _write(self.root, "p", _STUB, entry="def define_plugin(api):\n    pass\n")
        result = runtime.activate_plugin(self._stub())
        self.assertEqual(result.status, "invalid")
        self.assertIn("no capabilities", result.reason)

    def test_disabled_stub_not_imported(self) -> None:
        # An entry that would crash on import must never run for a disabled stub.
        _write(
            self.root, "p", {**_STUB, "enabled": False},
            entry="raise RuntimeError('should never import')\n",
        )
        result = runtime.activate_plugin(self._stub())
        self.assertEqual(result.status, "disabled")

    def test_unsupported_stub_not_imported(self) -> None:
        _write(
            self.root, "p", {**_STUB, "plugin_api_version": 999},
            entry="raise RuntimeError('should never import')\n",
        )
        result = runtime.activate_plugin(self._stub())
        self.assertEqual(result.status, "unsupported")


class DependencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.deps_root = Path(self._tmp.name)

    def _stub(self, deps: list[str]) -> PluginStub:
        return PluginStub(
            id="dep", name="Dep", root=str(self.deps_root / "src"),
            enabled=True, plugin_api_version=1, python_dependencies=deps,
        )

    def test_no_deps_none(self) -> None:
        status, reason = runtime.ensure_dependencies(
            self._stub([]), deps_root=self.deps_root
        )
        self.assertEqual(status, "none")

    def test_install_writes_marker_and_syspath(self) -> None:
        stub = self._stub(["foo==1.0"])
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(runtime.subprocess, "run", return_value=fake) as run:
            status, reason = runtime.ensure_dependencies(
                stub, deps_root=self.deps_root
            )
        self.assertEqual(status, "installed")
        run.assert_called_once()
        target = self.deps_root / "dep"
        self.assertTrue((target / ".installed.json").is_file())
        self.assertIn(str(target), sys.path)

    def test_cached_skips_pip(self) -> None:
        stub = self._stub(["foo==1.0"])
        fake = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(runtime.subprocess, "run", return_value=fake):
            runtime.ensure_dependencies(stub, deps_root=self.deps_root)
        # Second call: marker hash matches -> no pip.
        with mock.patch.object(runtime.subprocess, "run") as run2:
            status, _ = runtime.ensure_dependencies(stub, deps_root=self.deps_root)
        self.assertEqual(status, "cached")
        run2.assert_not_called()

    def test_pip_failure_reports(self) -> None:
        stub = self._stub(["foo==1.0"])
        fake = mock.Mock(returncode=1, stdout="", stderr="no such package")
        with mock.patch.object(runtime.subprocess, "run", return_value=fake):
            status, reason = runtime.ensure_dependencies(
                stub, deps_root=self.deps_root
            )
        self.assertEqual(status, "failed")
        self.assertIn("no such package", reason)


class BrowserParityTests(unittest.TestCase):
    """The bundled browser plugin's middleware reshapes a snapshot identically
    to a directly-constructed BrowserPerception from the plugin-local
    ``aiko_browser`` package (proves the plugin wiring is a pure passthrough)."""

    _SNAPSHOT = (
        '- button "Submit" [ref=e1]\n'
        '- link "Home" [ref=e2]\n'
        '- textbox "Search" [ref=e3]\n'
    )

    def _browser_middleware(self):
        from app.plugins.loader import default_plugin_roots, discover_plugins

        stubs = discover_plugins(
            [default_plugin_roots()[0]], entries={"browser": {"enabled": True}}
        )
        stub = next(s for s in stubs if s.id == "browser")
        result = runtime.activate_plugin(stub)
        return result, stub

    def test_middleware_claims_only_snapshot_tool(self) -> None:
        import shutil

        if not shutil.which("npx"):
            self.skipTest("npx not present -> browser plugin gates on require_binary")
        result, _ = self._browser_middleware()
        self.assertEqual(result.status, "active")
        self.assertEqual(len(result.middlewares), 1)
        mw = result.middlewares[0]
        self.assertTrue(mw.claims("browser", "browser_snapshot"))
        self.assertFalse(mw.claims("browser", "browser_click"))
        self.assertFalse(mw.claims("other", "browser_snapshot"))

    def test_transform_matches_direct_perception(self) -> None:
        import shutil

        if not shutil.which("npx"):
            self.skipTest("npx not present -> browser plugin gates on require_binary")
        # Activation puts the plugin root on sys.path so ``aiko_browser``
        # (the plugin-local perception package) imports cleanly.
        result, _ = self._browser_middleware()
        from aiko_browser.perception import BrowserPerception

        mw = result.middlewares[0]
        direct = BrowserPerception.from_config(
            {"enabled": True}, server_id="browser"
        )
        got = mw.transform("browser", "browser_snapshot", self._SNAPSHOT, {})
        want = direct.transform("browser", "browser_snapshot", self._SNAPSHOT, {})
        self.assertIsNotNone(got)
        self.assertIsNotNone(want)
        self.assertEqual(got.content, want.content)
        self.assertEqual(got.summary, want.summary)


if __name__ == "__main__":
    unittest.main()
