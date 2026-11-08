"""Tools stay discoverable while full schemas and optional instructions stay bounded."""

from pathlib import Path
from types import SimpleNamespace

from app.core.tasks.workflow.discovery import rank, manifest
from app.core.tasks.workflow.extensions_mixin import WorkflowExtensionsMixin
from app.core.tasks.workflow.workflow_planner import parse_planner_response
from app.plugins.sdk import PluginApi


def test_discovery_indexes_parameter_names_and_preserves_exact_pins():
    rows = [
        {
            "name": "files__search",
            "description": "Search source content",
            "group": "mcp:files",
            "input_schema": {"properties": {"file_glob": {}}},
        },
        {"name": "browser__search", "description": "Search page", "group": "mcp:browser"},
    ]
    assert rank(rows, "file_glob")[0]["name"] == "files__search"
    assert rank(rows, "page", ["files__search"])[0]["name"] == "files__search"
    assert rank(rows, "nonexistent") == sorted(rows, key=lambda row: row["name"])
    inventory = manifest(SimpleNamespace(describe_for_planner=lambda: rows))
    assert inventory[0]["count"] == 1
    assert "browser__search" in inventory[0]["examples"]


def test_optional_playbooks_are_copied_and_revoked_with_plugin_generation():
    api = PluginApi(plugin_id="browser", plugin_root=Path("."), config={})
    api.register_playbook("menus", "Handle menus", "Obtain a fresh snapshot after opening a menu.")
    returned = api.playbooks
    returned[0]["body"] = "mutated"
    assert api.playbooks[0]["body"] != "mutated"
    data = {
        "playbooks": {
            "browser:menus": {**api.playbooks[0], "name": "browser:menus", "generation": "one"}
        }
    }
    host = WorkflowExtensionsMixin()
    host._extensions_provider = lambda: data
    state = {}
    assert host._load_playbook(state, {"name": "browser:menus"})
    assert host._extension_context(state)["playbooks"][0]["body"] == api.playbooks[0]["body"]
    data["playbooks"]["browser:menus"]["generation"] = "two"
    assert host._extension_context(state)["playbooks"] == []
    data["playbooks"].clear()
    assert not host._load_playbook(state, {"name": "browser:menus"})
    assert (
        parse_planner_response(
            '{"action":"read_playbook","args":{"name":"browser:menus"}}', valid_skill_names=set()
        ).action
        == "read_playbook"
    )
