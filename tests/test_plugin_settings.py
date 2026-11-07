"""Settings are bundle-owned; runtime changes revoke old capability generations."""

from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.core.infra.settings import persist_user_overrides, replace_user_override
from app.core.tasks.workflow.mcp_operation_gate import prepare
from app.core.tasks.workflow.mcp_skills import register_mcp_skills
from app.core.tasks.workflow.skill_registry import WorkflowSkill, WorkflowSkillRegistry
from app.mcp.client.manager import McpToolDescriptor
from app.plugins.loader import discover_plugins
from app.plugins.manager import PluginConflict, PluginManager, PluginValidation
from app.plugins.runtime import ActivatedPlugin, activate_plugin
from app.plugins.settings_contract import PluginSettings
from app.plugins.settings_validation import validate_settings
from app.web.rest.plugin_routes import register


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def bundle(root, pid="p", *, enabled=False, required=False):
    path = root / pid
    write(
        path / "plugin.json",
        {
            "id": pid,
            "name": pid,
            "enabled": enabled,
            "settings": {"schema": "config/schema.json", "ui": "config/ui.json"},
        },
    )
    write(path / "config/default.json", {"value": "default", "roots": []})
    write(
        path / "config/schema.json",
        {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "roots": {
                    "type": "array",
                    "items": {"type": "string", "format": "host-directory", "minLength": 1},
                },
            },
        },
    )
    write(
        path / "config/ui.json",
        {
            "required_for_activation": ["roots"] if required else [],
            "resources": [{"field": "roots", "mode": "read_only"}],
        },
    )
    # Metadata discovery must never import an entry, including a disabled one.
    (path / "entry.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")
    return path


def eventually(predicate):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.01)
    raise AssertionError("Plugin state did not settle")


class Transport:
    def __init__(self, servers=()):
        self.servers = {server.id: server for server in servers}
        self.blocked = set()
        self.replacements = []

    def server_status(self):
        return [{"id": pid, "status": "connected", "tool_count": 1} for pid in list(self.servers)]

    def block_server(self, pid):
        self.blocked.add(pid)

    def replace_server(self, pid, server, **kwargs):
        self.replacements.append(pid)
        if server is None:
            self.servers.pop(pid, None)
        else:
            self.servers[pid] = server
            if kwargs.get("expose", True):
                self.blocked.discard(pid)

    def release_server(self, pid, generation):
        self.blocked.discard(pid)

    def start(self):
        pass

    def set_tools_changed_callback(self, callback):
        self.callback = callback

    def stop(self):
        pass


@pytest.fixture
def runtime(tmp_path):
    bundle(tmp_path, "p")
    bundle(tmp_path, "other", enabled=True)
    entries, enabled, cleanups = {}, [True], []
    transport = Transport()

    def persist(pid, value):
        if pid is None:
            enabled[0] = value["enabled"]
        else:
            entries[pid] = deepcopy(value)

    def activate(stub, **kwargs):
        return ActivatedPlugin(
            id=stub.id,
            name=stub.name,
            root=stub.root,
            enabled=True,
            status="active",
            server=SimpleNamespace(id=stub.id),
            cleanups=[lambda: cleanups.append(stub.id)],
        )

    manager = PluginManager(
        roots=[str(tmp_path)],
        entries=lambda: entries,
        master=lambda: enabled[0],
        persist=persist,
        transport=transport,
        publish=lambda: None,
        activate=activate,
    )
    manager.start()
    eventually(lambda: row(manager, "other")["phase"] == "ready")
    yield manager, entries, transport, cleanups
    manager.close()


def row(manager, pid="p"):
    return next(item for item in manager.snapshot()["plugins"] if item["id"] == pid)


def test_disabled_discovery_is_metadata_only_and_projects_declared_settings(tmp_path):
    path = bundle(tmp_path)
    write(path / "config/user.json", {"unknown_secret": "never expose", "value": "local"})
    (stub,) = discover_plugins([str(tmp_path)])
    assert stub.settings.public_config(stub.config) == {"value": "local", "roots": []}
    assert activate_plugin(stub).status == "disabled"


@pytest.mark.parametrize("reference", ["../outside.json", "https://example.test/schema.json"])
def test_metadata_paths_cannot_escape_bundle(tmp_path, reference):
    bundle(tmp_path)
    with pytest.raises((ValueError, FileNotFoundError)):
        PluginSettings.from_manifest(tmp_path / "p", {"settings": {"schema": reference}})


def test_explicit_empty_resources_do_not_resurrect_legacy_access():
    spec = PluginSettings(ui={"legacy_single": {"roots": {"from": "root"}}})
    assert spec.normalize({"root": "/old", "roots": []}, set())["roots"] == ["/old"]
    assert spec.normalize({"root": "/old", "roots": []}, {"roots"})["roots"] == []


def test_invalid_metadata_blocks_activation_without_import(tmp_path):
    path = bundle(tmp_path, enabled=True)
    write(path / "config/ui.json", {"resources": "bad"})
    (stub,) = discover_plugins([str(tmp_path)])
    result = activate_plugin(stub)
    assert result.status == "invalid"
    assert "settings metadata" in result.reason
    assert "must not import" not in result.reason


def test_enable_reconfigure_disable_are_selective_and_generations_are_revoked(runtime):
    manager, entries, transport, cleanups = runtime
    other = row(manager, "other")["generation"]
    manager.update("p", expected_revision=row(manager)["revision"], enabled=True)
    eventually(lambda: row(manager)["phase"] == "ready")
    generation = row(manager)["generation"]
    assert manager.guard("p", generation)
    manager.update("p", expected_revision=row(manager)["revision"], config={"value": "changed"})
    assert not manager.guard("p", generation)
    eventually(lambda: row(manager)["phase"] == "ready")
    assert entries["p"]["config"] == {"value": "changed"}
    assert row(manager, "other")["generation"] == other
    assert transport.replacements.count("other") == 2  # boot removal + addition only
    manager.update("p", expected_revision=row(manager)["revision"], enabled=False)
    eventually(lambda: row(manager)["phase"] == "disabled")
    assert "p" not in transport.servers
    assert cleanups.count("p") == 2


def test_conflict_and_invalid_config_do_not_change_persisted_or_active_state(runtime, tmp_path):
    manager, entries, _, _ = runtime
    before = deepcopy(entries)
    with pytest.raises(PluginConflict):
        manager.update("p", expected_revision="stale", enabled=True)
    with pytest.raises(PluginValidation):
        manager.update(
            "p", expected_revision=row(manager)["revision"], config={"roots": ["relative"]}
        )
    with pytest.raises(ValueError):
        manager.update("p", expected_revision=row(manager)["revision"], config={"hidden": "no"})
    assert entries == before
    manager.update(
        "p", expected_revision=row(manager)["revision"], config={"roots": [str(tmp_path)]}
    )
    assert row(manager)["access"] == [{"name": "", "path": str(tmp_path), "mode": "read_only"}]


def test_reset_removes_override_branch_and_master_stops_all_owned_services(runtime):
    manager, entries, transport, _ = runtime
    manager.update("p", expected_revision=row(manager)["revision"], config={"value": "override"})
    manager.update("p", expected_revision=row(manager)["revision"], reset=True)
    assert entries["p"]["config"] == {}
    assert row(manager)["config"]["value"] == "default"
    manager.set_master(False)
    eventually(lambda: not transport.servers)
    assert all(item["phase"] == "blocked" for item in manager.snapshot()["plugins"])
    manager.set_master(True)
    eventually(lambda: row(manager, "other")["phase"] == "ready")


def test_disabling_during_activation_cleans_late_resources_without_publishing(tmp_path):
    bundle(tmp_path, enabled=True)
    entered, release, disposed = threading.Event(), threading.Event(), threading.Event()
    entries = {}

    def activate(stub, **kwargs):
        entered.set()
        assert release.wait(4)
        return ActivatedPlugin(
            id="p",
            name="p",
            root=stub.root,
            enabled=True,
            status="active",
            server=SimpleNamespace(id="p"),
            cleanups=[disposed.set],
        )

    manager = PluginManager(
        roots=[str(tmp_path)],
        entries=lambda: entries,
        master=lambda: True,
        persist=lambda pid, entry: entries.update({pid: entry}),
        transport=Transport(),
        publish=lambda: None,
        activate=activate,
    )
    try:
        manager.start()
        assert entered.wait(4)
        manager.update("p", expected_revision=row(manager)["revision"], enabled=False)
        eventually(lambda: row(manager)["phase"] == "disabled")
        release.set()
        assert disposed.wait(4)
        assert manager.active_plugins() == []
    finally:
        release.set()
        manager.close()


def test_required_configuration_is_saved_before_dependencies_or_imports(tmp_path):
    bundle(tmp_path, enabled=True, required=True)
    manager = PluginManager(
        roots=[str(tmp_path)],
        entries=lambda: {},
        master=lambda: True,
        persist=lambda *args: None,
        transport=Transport(),
        publish=lambda: None,
        activate=lambda *args, **kwargs: pytest.fail("must configure before activation"),
    )
    try:
        manager.start()
        eventually(lambda: row(manager)["phase"] == "needs_configuration")
    finally:
        manager.close()


def test_exact_owned_persistence_reset_preserves_other_settings(tmp_path):
    path = tmp_path / "settings.json"
    persist_user_overrides(
        {
            "tts": {"volume": 0.3},
            "plugins": {
                "entries": {
                    "p": {"enabled": True, "config": {"value": "old"}},
                    "other": {"enabled": False},
                }
            },
        },
        path=path,
    )
    replace_user_override(("plugins", "entries", "p"), {"enabled": True, "config": {}}, path=path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["tts"] == {"volume": 0.3}
    assert saved["plugins"]["entries"] == {
        "p": {"enabled": True, "config": {}},
        "other": {"enabled": False},
    }


def test_secrets_are_not_returned_or_revealed_by_validation_and_survive_row_edits():
    spec = PluginSettings(
        schema={
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "x-uniqueBy": "id",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "name": {"type": "string"},
                            "token": {"type": "string", "writeOnly": True, "pattern": "^allowed$"},
                        },
                    },
                }
            },
        }
    )
    config = {"rows": [{"id": "x", "name": "old", "token": "private"}]}
    assert "private" not in json.dumps(spec.public_config(config))
    assert "private" not in json.dumps(validate_settings(spec, config))
    edited = spec.merge_config(config, {"rows": [{"id": "x", "name": "new"}]})
    assert edited["rows"][0]["token"] == "private"
    assert spec.merge_config(config, {"rows": []}) == {"rows": []}


def test_settings_rest_status_validation_conflict_and_disable(runtime):
    manager, _, _, _ = runtime
    app = FastAPI()
    register(app, SimpleNamespace(plugins=manager))
    with TestClient(app) as client:
        assert client.get("/api/plugins").status_code == 200
        assert client.patch("/api/plugins/p", json={"revision": "stale"}).status_code == 409
        revision = row(manager)["revision"]
        assert (
            client.patch(
                "/api/plugins/p", json={"revision": revision, "config": {"roots": ["relative"]}}
            ).status_code
            == 422
        )
        assert (
            client.patch("/api/plugins/p", json={"revision": revision, "enabled": True}).status_code
            == 200
        )
        assert client.patch("/api/plugins", json={"enabled": False}).json()["enabled"] is False
        assert client.patch("/api/plugins", json={"enabled": "false"}).status_code == 422
        assert client.post("/api/plugins/unknown/retry", json={}).status_code == 404


def test_mcp_generation_changes_contract_even_with_identical_schema_and_prunes_skills():
    descriptor = McpToolDescriptor("p", "read", "read", {"type": "object"}, "read", "new")
    manager = SimpleNamespace(list_available_tools=lambda: [descriptor])
    args = {"server_id": "p", "tool_name": "read", "tool_args": {}}
    assert prepare(manager, args)[1]
    assert prepare(manager, {**args, "_config_generation": "old"})[1]
    assert prepare(manager, {**args, "_config_generation": "new"})[1] is None
    registry = WorkflowSkillRegistry()
    registry.register(WorkflowSkill(name="native", description="keep"))
    register_mcp_skills(registry, manager)
    assert registry.get("p__read").generation == "new"
    register_mcp_skills(registry, SimpleNamespace(list_available_tools=lambda: []))
    assert registry.names() == ["native"]


def test_session_publishes_sdk_fast_tools_once_and_old_turn_registry_is_revoked(
    tmp_path, monkeypatch
):
    from app.core.infra.settings import PluginsSettings, ExternalMcpSettings
    from app.core.session.plugin_settings_mixin import PluginSettingsMixin

    path = bundle(tmp_path, enabled=True)
    (path / "entry.py").write_text(
        "def define_plugin(api):\n"
        "    assert 'value' in api.settings.schema['properties']\n"
        "    api.register_fast_tool(name='test_value', description='value',\n"
        "        parameters={'type': 'object'}, handler=lambda args: api.config['value'])\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("app.plugins.loader.default_plugin_roots", lambda: [str(tmp_path)])
    monkeypatch.setattr("app.mcp.client.manager.ExternalMcpManager", Transport)
    monkeypatch.setattr("app.core.infra.settings.USER_CONFIG_PATH", tmp_path / "saved.json")

    class Host(PluginSettingsMixin):
        def _collect_plugin_fast_tools(self, specs, *, target=None):
            (target or self)._plugin_fast_tools.extend(specs)

        def rebuild_tool_registry(self):
            pass

    host = Host()
    host._settings = SimpleNamespace(plugins=PluginsSettings(), mcp_clients=ExternalMcpSettings())
    host._turn_runner = object()
    host._start_plugin_runtime(SimpleNamespace(mcp_clients_enabled=True), WorkflowSkillRegistry())
    try:
        eventually(lambda: row(host.plugins)["phase"] == "ready")
        old = host._plugin_fast_tools[0]
        assert old.handler({}) == "default"
        host.plugins.update(
            "p", expected_revision=row(host.plugins)["revision"], config={"value": "new"}
        )
        eventually(lambda: row(host.plugins)["phase"] == "ready")
        assert len(host._plugin_fast_tools) == 1
        assert host._plugin_fast_tools[0].handler({}) == "new"
        with pytest.raises(RuntimeError, match="configuration changed"):
            old.handler({})
        assert host._settings.plugins.entries["p"].config == {"value": "new"}
        saved = json.loads((tmp_path / "saved.json").read_text(encoding="utf-8"))
        assert saved["plugins"]["entries"]["p"]["config"] == {"value": "new"}
        host.plugins.update("p", expected_revision=row(host.plugins)["revision"], enabled=False)
        assert host._plugin_fast_tools == []
    finally:
        host.plugins.close()


def test_sdk_cleans_resources_when_definition_fails(tmp_path, monkeypatch):
    from app.plugins.sdk import PluginApi
    from app.plugins import runtime

    bundle(tmp_path, enabled=True)
    cleaned = []

    def define(api: PluginApi):
        api.register_cleanup(lambda: cleaned.append("disposed"))
        raise RuntimeError("definition failed")

    monkeypatch.setattr(
        runtime, "_import_entry", lambda stub: SimpleNamespace(define_plugin=define)
    )
    (stub,) = discover_plugins([str(tmp_path)])
    assert activate_plugin(stub).status == "invalid"
    assert cleaned == ["disposed"]


def test_session_approval_is_bound_to_generation_but_persistent_policy_uses_stable_id():
    from app.core.session.task_orchestration_mixin import TaskOrchestrationMixin

    host = TaskOrchestrationMixin()
    host._settings = SimpleNamespace(
        agent=SimpleNamespace(task_approval_mode="ask", task_approval_overrides={})
    )
    host._approved_capabilities = {"mcp:p:write@old"}
    assert host._resolve_task_approval("mcp:p:write@old") == "auto"
    assert host._resolve_task_approval("mcp:p:write@new") == "ask"
    host._settings.agent.task_approval_overrides = {"mcp:p:write": "auto"}
    assert host._resolve_task_approval("mcp:p:write@new") == "auto"


def test_stale_approve_all_cannot_dispatch_or_grant_access_to_new_configuration():
    from dataclasses import replace
    from app.core.tasks.approval import APPROVE_ALL
    from app.core.tasks.handlers.mcp_tool import McpToolHandler
    from app.core.tasks.task_handler import TaskFailed

    descriptor = McpToolDescriptor("p", "write", "write", {"type": "object"}, "write", "old")
    calls, grants, events = [], [], []
    manager = SimpleNamespace(
        list_available_tools=lambda: [descriptor],
        call_tool=lambda *args, **kwargs: calls.append(args),
    )
    handler = McpToolHandler(
        manager=manager, resolve_approval=lambda cap: "ask", mark_session_approved=grants.append
    )
    state = handler.start(
        {"server_id": "p", "tool_name": "write", "tool_args": {}, "_config_generation": "old"},
        events.append,
    )
    descriptor = replace(descriptor, generation="new")
    handler.on_input(state, APPROVE_ALL, events.append)
    assert calls == []
    assert grants == ["mcp:p:write@old"]
    assert isinstance(events[-1], TaskFailed)


def test_spawn_cannot_substitute_new_configuration_after_admission():
    from dataclasses import replace
    from app.core.tasks.workflow.skill_registry import SpawnContext

    calls = []
    old = WorkflowSkill(
        name="read",
        description="read",
        generation="old",
        spawn=lambda args, ctx: calls.append("old"),
    )
    registry = WorkflowSkillRegistry()
    registry.register(replace(old, generation="new", spawn=lambda args, ctx: calls.append("new")))
    assert registry.spawn_child("read", {}, SpawnContext(None, "u", 1), expected_skill=old) is None
    assert calls == []


def test_result_uses_middleware_snapshot_from_dispatch():
    from app.core.tasks.handlers.mcp_tool import McpToolHandler

    def middleware(label):
        return SimpleNamespace(
            claims=lambda *args: True,
            transform=lambda *args: SimpleNamespace(content=label, summary=label),
        )

    chain, events = [middleware("old")], []

    def call(*args, **kwargs):
        chain[:] = [middleware("new")]
        return SimpleNamespace(content=[SimpleNamespace(text="raw")], isError=False)

    handler = McpToolHandler(
        manager=SimpleNamespace(call_tool=call), middlewares_provider=lambda: chain
    )
    handler.start({"server_id": "p", "tool_name": "read", "tool_args": {}}, events.append)
    assert events[-1].result["content"] == "old"


def test_cancelled_dependency_install_terminates_owned_process(monkeypatch):
    import sys
    from app.plugins import runtime

    owned = []
    original = runtime.subprocess.Popen

    def popen(*args, **kwargs):
        process = original(*args, **kwargs)
        owned.append(process)
        return process

    monkeypatch.setattr(runtime.subprocess, "Popen", popen)
    result = runtime._install_cancellable(
        [sys.executable, "-c", "import time; time.sleep(30)"], lambda: True
    )
    assert result.returncode != 0
    assert "cancelled" in result.stderr
    assert all(process.poll() is not None for process in owned)


def test_failed_publication_retires_transport_and_cleans_resources(tmp_path):
    bundle(tmp_path, enabled=True)
    transport, cleaned, failed = Transport(), [], []

    def publish():
        if manager.active_plugins() and not failed:
            failed.append(True)
            raise RuntimeError("publication failed")

    manager = PluginManager(
        roots=[str(tmp_path)],
        entries=lambda: {},
        master=lambda: True,
        persist=lambda *args: None,
        transport=transport,
        publish=publish,
        activate=lambda stub, **kwargs: ActivatedPlugin(
            id="p",
            name="p",
            root=stub.root,
            enabled=True,
            status="active",
            server=SimpleNamespace(id="p"),
            cleanups=[lambda: cleaned.append(True)],
        ),
    )
    try:
        manager.start()
        eventually(lambda: row(manager)["phase"] == "error")
        eventually(lambda: not transport.servers)
        assert cleaned == [True]
        assert not manager.active_plugins()
    finally:
        manager.close()
