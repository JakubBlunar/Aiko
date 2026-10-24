"""The standalone playground reads models without loading live install state."""
from pathlib import Path
from unittest.mock import patch

from scripts.avatar_playground import PROJECT_ROOT, build_profile, main


FIXTURE = PROJECT_ROOT / "tests/fixtures/avatar_min"


def test_profile_exposes_model_expressions_without_changing_production_mapping():
    from app.core.persona.avatar_profile import from_disk

    original = from_disk(FIXTURE).to_dict()
    preview = build_profile(FIXTURE, "Test model")
    assert preview["display_name"] == "Test model"
    assert preview["loaded"] is True
    assert len(preview["playground_expressions"]) == len(original["expressions"])
    assert set(preview["playground_expressions"].values()) == {
        expression["name"] for expression in original["expressions"]
    }
    for reaction, expression in original["reaction_mapping"].items():
        assert preview["reaction_mapping"][reaction] == expression
    assert preview["settings"]["root_dir"] == str(FIXTURE)
    assert "nervous" in preview["playground_reactions"]


def test_launcher_owns_manifest_and_starts_only_the_isolated_vite_process(tmp_path):
    captured = {}

    class FakeProcess:
        def __init__(self, command, *, cwd, env):
            captured.update(command=command, cwd=cwd, env=env)
            captured["manifest"] = Path(env["AIKO_PLAYGROUND_PROFILE"])
            assert captured["manifest"].is_file()

        def wait(self):
            return 0

        def poll(self):
            return 0

    with patch("scripts.avatar_playground.subprocess.Popen", FakeProcess):
        assert main(["--model", str(FIXTURE), "--port", "5194"], scratch_root=tmp_path) == 0
    assert captured["cwd"] == PROJECT_ROOT / "web"
    assert "vite.playground.config.ts" in captured["command"]
    assert "5194" in captured["command"]
    assert captured["env"]["AIKO_PLAYGROUND_MODEL"] == str(FIXTURE.resolve())
    assert captured["manifest"].parent.parent == tmp_path
    assert not captured["manifest"].parent.exists()


def test_interruption_stops_child_and_cleans_manifest(tmp_path):
    captured = {}

    class InterruptedProcess:
        def __init__(self, command, *, cwd, env):
            captured["manifest"] = Path(env["AIKO_PLAYGROUND_PROFILE"])
            self.stopped = False

        def wait(self, timeout=None):
            if not self.stopped:
                raise KeyboardInterrupt
            return 0

        def poll(self):
            return 0 if self.stopped else None

        def terminate(self):
            self.stopped = True
            captured["stopped"] = True

    with patch("scripts.avatar_playground.subprocess.Popen", InterruptedProcess):
        assert main(["--model", str(FIXTURE)], scratch_root=tmp_path) == 130
    assert captured["stopped"]
    assert not captured["manifest"].parent.exists()
