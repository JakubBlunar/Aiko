"""Tools stay discoverable while full schemas and optional instructions stay bounded."""

from pathlib import Path
import json

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
    inventory = manifest(rank(rows, "page"))
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


def test_workflow_prompt_stays_bounded_as_unrelated_skills_are_registered():
    from app.core.tasks.workflow.context_budget import TaskBudget, pack_planner
    from app.core.tasks.workflow.discovery import routing_query
    from app.core.tasks.workflow.profiles import route_catalogue
    from app.core.tasks.workflow.skill_registry import WorkflowSkill, WorkflowSkillRegistry
    from app.core.tasks.workflow.workflow_planner import PlannerInput

    sizes = []
    for count in (32, 512):
        reg = WorkflowSkillRegistry()
        for i in range(count):
            reg.register(WorkflowSkill(name=f"library_{i:03}", group=f"group_{i:03}",
                                       description="Access an archive", effect="read"))
        reg.register(WorkflowSkill(name="z_lunar_phase", group="astronomy", effect="read",
                                   description="Look up the lunar phase"))
        reg.register(WorkflowSkill(name="z_lunar_write", group="astronomy", effect="write",
                                   description="Change the lunar phase"))
        state = {"goal": "Help me prepare", "guidance": [{"content": "Check the lunar phase"}]}
        skills, groups = route_catalogue(reg, "reader", query=routing_query(state), page_size=6)
        assert [r["name"] for r in skills] == ["z_lunar_phase"]
        assert groups == [{"group": "astronomy", "count": 1, "examples": ["z_lunar_phase"]}]
        ctx = PlannerInput(goal=state["goal"], skills=skills, work={"capability_groups": groups})
        messages, _, _ = pack_planner(ctx, TaskBudget.resolve(65536))
        sizes.append(len(json.dumps(messages)))
        fallback, _ = route_catalogue(reg, "reader", query="unmatched", page_size=6)
        assert len(fallback) <= 3
        page, _ = route_catalogue(reg, "reader", query="archive", page=1, page_size=6, browse=True)
        assert [r["name"] for r in page] == [f"library_{i:03}" for i in range(6, 12)]
        pinned, _ = route_catalogue(reg, "reader", query="lunar", preferred=["library_021"])
        assert pinned[0]["name"] == "library_021"
    assert sizes[0] == sizes[1]


def test_workflow_extensions_route_beyond_the_first_installed_page():
    host = WorkflowExtensionsMixin()
    rows = [{"name": f"topic_{n:02}", "description": "Archived procedure"} for n in range(20)]
    rows.append({"name": "z_orchard", "description": "Inspect orchard records"})
    host._extensions_provider = lambda: {
        "playbooks": {r["name"]: {**r, "body": "Private until loaded"} for r in rows},
        "procedures": {r["name"]: {**r, "input_schema": {}, "steps": []} for r in rows},
    }
    state = {"goal": "Inspect orchard records"}
    page = host._extension_context(state)
    assert [r["name"] for r in page["procedures"]] == ["z_orchard"]
    assert [r["name"] for r in page["playbook_index"]] == ["z_orchard"]
    assert "Private until loaded" not in str(page)
    page = host._extension_context({**state, "query": "orchard"})
    later = host._extension_context({**state, "query": "orchard", "catalogue_page": 1})
    assert len(later["procedures"]) == 4 and len(later["playbook_index"]) == 6
    for key in ("procedures", "playbook_index"):
        assert not ({r["name"] for r in later[key]} & {r["name"] for r in page[key]})
