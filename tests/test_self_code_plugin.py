"""Scoped editor-like tools: no project execution, honest evidence and MCP integration."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "self_code"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN))
    return importlib.import_module("aiko_code.service"), importlib.import_module("aiko_code.policy")


@pytest.fixture
def code(tmp_path, modules):
    root = tmp_path / "source"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text(
        "# comments mention orchard\n"
        'MESSAGE = "orchard is a string"\n'
        "class Garden:\n"
        "    def orchard(self, value):\n"
        "        return value + 1\n"
        "\n"
        "def use_it():\n"
        "    return Garden().orchard(2)\n",
        encoding="utf-8",
    )
    (root / "src" / "other.py").write_text(
        'def orchard():\n    return "different subject"\n', encoding="utf-8"
    )
    service, policy = modules
    config = {"root": str(root), "include_paths": ["src"]}
    return root, service.CodeTools(config), policy.SourceError, config


def test_plugin_is_disabled_and_discovery_does_not_import_or_install():
    from app.plugins.loader import discover_plugins
    from app.plugins.runtime import activate_plugin

    stub = discover_plugins([str(PLUGIN.parent)], entries={"self_code": {"enabled": False}})
    stub = next(item for item in stub if item.id == "self_code")
    assert not stub.enabled
    assert len(stub.python_dependencies) == 3
    result = activate_plugin(stub)
    assert result.status == "disabled"
    assert result.server is None


def test_literal_search_source_hash_line_and_revision_paging(code):
    root, tools, error, _ = code
    first = tools.search_code("orchard", limit=1)
    assert first["scope_complete"]
    item = first["items"][0]
    assert item["path"] == "src/main.py"
    assert item["line"] == 1 and item["column"] == 20
    assert item["sha256"] == hashlib.sha256((root / item["path"]).read_bytes()).hexdigest()
    next_page = tools.search_code(
        "orchard", offset=first["next_offset"], revision=first["revision"], limit=1
    )
    assert next_page["items"][0]["line"] == 2
    (root / "src" / "main.py").write_text("changed = True\n", encoding="utf-8")
    with pytest.raises(error, match="revision changed"):
        tools.search_code("orchard", offset=1, revision=first["revision"])
    assert tools.search_code("Garden().*")["items"] == []  # literal, not regex


def test_python_declarations_qualified_names_and_reference_candidates(code):
    _, tools, _, _ = code
    outline = tools.file_symbols("src/main.py")
    symbols = {item["qualified_name"]: item for item in outline["items"]}
    assert set(symbols) == {"MESSAGE", "Garden", "Garden.orchard", "use_it"}
    assert symbols["Garden.orchard"]["line"] == 4
    assert symbols["Garden.orchard"]["column"] == 9
    assert symbols["Garden.orchard"]["end_line"] == 5
    source = tools.read_symbol("src/main.py", "Garden.orchard")
    assert source["lines"][0]["line"] == 4
    assert "return value" in source["lines"][1]["text"]
    declarations = tools.find_symbol("orchard", exact=True)
    assert len(declarations["items"]) == 2
    references = tools.find_references("orchard")
    assert references["resolution"] == "identifier_candidates"
    assert {(r["path"], r["line"]) for r in references["items"]} == {
        ("src/main.py", 4),
        ("src/main.py", 8),
        ("src/other.py", 1),
    }


def test_unicode_columns_use_characters_and_search_case_insensitive_is_not_byte_indexed(code):
    root, tools, _, _ = code
    (root / "src" / "unicode.py").write_text('label = "ßß"; orchard = 2\n', encoding="utf-8")
    found = tools.search_code("ORCHARD", pattern="**/unicode.py")["items"][0]
    assert found["column"] == 15


def test_bounded_reads_empty_files_and_long_line_continuation(code):
    root, tools, error, _ = code
    path = root / "src" / "long.txt"
    path.write_text("a" * 10000 + "\ntail\n", encoding="utf-8")
    first = tools.read_code("src/long.txt")
    assert len(first["lines"][0]["text"]) == 8000
    assert first["next"] == {"start_line": 1, "start_column": 8001}
    rest = tools.read_code("src/long.txt", **first["next"], expected_sha256=first["sha256"])
    assert len(rest["lines"][0]["text"]) == 2000
    assert rest["lines"][1]["text"] == "tail"
    path.write_text("new version\n", encoding="utf-8")
    with pytest.raises(error, match="revision changed"):
        tools.read_code("src/long.txt", expected_sha256=first["sha256"])
    path.write_text("", encoding="utf-8")
    empty = tools.read_code("src/long.txt")
    assert empty["lines"] == [] and empty["next"] is None
    path.write_text("\n".join(str(i) for i in range(250)), encoding="utf-8")
    page = tools.read_code("src/long.txt")
    assert len(page["lines"]) == 200 and page["next"]["start_line"] == 201


@pytest.mark.parametrize(
    "path",
    [
        "../private.py",
        "/etc/passwd",
        "C:/private.py",
        "C:private.py",
        "src/../../private.py",
        "src\\..\\private.py",
        "src/main.py:secret",
        "src/CON.py",
        "src/file.py.",
        "src/file.py ",
        "src/user.json",
        "src/.env",
        "src/data/state.py",
    ],
)
def test_unpermitted_paths_fail_before_source_contents_are_returned(code, path):
    _, tools, error, _ = code
    with pytest.raises((error, OSError)):
        tools.read_code(path)


def test_include_scope_exclusions_and_generated_binary_files(code):
    root, tools, error, config = code
    (root / "private.py").write_text("SECRET_OUTSIDE_SCOPE = 1", encoding="utf-8")
    for name in ("data", "logs", "node_modules", ".git"):
        (root / "src" / name).mkdir()
        (root / "src" / name / "secret.py").write_text("excluded", encoding="utf-8")
    (root / "src" / "user.json").write_text('{"api_key":"private"}', encoding="utf-8")
    (root / "src" / "binary.py").write_bytes(b"code\x00binary")
    found = tools.find_files("**/*.py")
    assert {r["path"] for r in found["items"]} == {"src/main.py", "src/other.py", "src/binary.py"}
    with pytest.raises(error, match="outside.*include_paths"):
        tools.read_code("private.py")
    with pytest.raises(error, match="text source"):
        tools.read_code("src/binary.py")
    narrowed = type(tools)({**config, "exclude_patterns": ["**/other.py"]})
    assert not narrowed.search_code("different subject")["items"]


def test_symlink_junction_and_hardlink_escape_are_denied(code, tmp_path):
    root, tools, error, _ = code
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private.py").write_text("EXTERNAL_PRIVATE = True", encoding="utf-8")
    link = root / "src" / "escape"
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(error, match="symlinks or junctions"):
            tools.read_code("src/escape/private.py")
        assert not tools.search_code("EXTERNAL_PRIVATE")["items"]
    finally:
        link.rmdir() if os.name == "nt" else link.unlink()
    hardlink = root / "src" / "linked.py"
    os.link(outside / "private.py", hardlink)
    with pytest.raises(error, match="unlinked"):
        tools.read_code("src/linked.py")


def test_scan_limits_do_not_turn_partial_absence_into_complete_absence(code):
    _, tools, _, config = code
    bounded = type(tools)({**config, "max_scan_files": 1})
    result = bounded.search_code("not present")
    assert result["items"] == []
    assert not result["scope_complete"] and "file_limit" in result["limited_by"]


def test_syntax_check_never_imports_or_executes_source(code):
    root, tools, _, _ = code
    marker = root / "must-not-be-created.txt"
    (root / "src" / "trap.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    result = tools.syntax_problems("src/trap.py")
    assert result["problems"] == []
    assert result["coverage"] == "syntax_only" and not result["tests_executed"]
    assert not marker.exists()
    (root / "src" / "trap.py").write_text("def broken(:\n", encoding="utf-8")
    assert tools.syntax_problems("src/trap.py")["problems"][0]["line"] == 1


def test_workspace_identity_reads_head_without_git_or_checkout_code_execution(code):
    root, tools, _, _ = code
    git = root / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="ascii")
    (git / "refs" / "heads" / "main").write_text("a" * 40, encoding="ascii")
    info = tools.workspace_info()
    assert info["checkout_head"] == "a" * 40
    assert info["working_tree_state"] == "not_evaluated"
    assert info["running_build_match"] == "not_verified"


def test_recursive_globs_and_source_hash_required_for_continuations(code):
    root, tools, error, _ = code
    nested = root / "src" / "deep" / "nested"
    nested.mkdir(parents=True)
    (nested / "match.py").write_text("VALUE = 1\n", encoding="utf-8")
    found = tools.find_files("src/**/*.py", limit=1)
    assert found["items"][0]["path"] == "src/deep/nested/match.py"
    with pytest.raises(error, match="Continuation requires"):
        tools.find_files("src/**/*.py", offset=1)
    page = tools.file_symbols("src/main.py", limit=1)
    with pytest.raises(error, match="Continuation requires"):
        tools.file_symbols("src/main.py", offset=1)
    rest = tools.file_symbols("src/main.py", offset=1, expected_sha256=page["source"]["sha256"])
    assert rest["items"][0]["name"] == "Garden"


def test_ambiguous_symbols_decorators_and_fstring_identifiers(code):
    root, tools, _, _ = code
    (root / "src" / "scope.py").write_text(
        "class One:\n    @decorator\n    def same(self):\n        return f'{self.value}'\n"
        "class Two:\n    def same(self):\n        return 2\n",
        encoding="utf-8",
    )
    ambiguous = tools.read_symbol("src/scope.py", "same")
    assert ambiguous["status"] == "ambiguous" and len(ambiguous["candidates"]) == 2
    first = tools.read_symbol("src/scope.py", "One.same")
    assert first["lines"][0]["line"] == 2
    refs = tools.find_references("value", pattern="src/scope.py")["items"]
    assert len(refs) == 1 and refs[0]["line"] == 4


def test_parser_errors_unsupported_languages_and_oversized_names_are_honest(code):
    root, tools, _, _ = code
    (root / "src" / "plain.md").write_text("# notes\n", encoding="utf-8")
    assert tools.syntax_problems("src/plain.md")["status"] == "unsupported"
    assert tools.read_symbol("src/plain.md", "notes")["status"] == "unsupported"
    assert tools.file_symbols("src/plain.md")["status"] == "unsupported"
    (root / "src" / "giant.py").write_text(
        "def " + "a" * 10000 + "():\n    return 1\n", encoding="utf-8"
    )
    symbols = tools.file_symbols("src/giant.py")
    assert not symbols["scope_complete"]
    assert not symbols["items"][0]["name_complete"]
    assert len(json.dumps(symbols)) < 5000


def test_byte_and_walk_limits_and_utf8_source_changed_checks(code, modules, monkeypatch):
    root, tools, error, config = code
    (root / "src" / "a.py").write_text("# " + "x" * 1500, encoding="utf-8")
    bounded = type(tools)({**config, "max_scan_bytes": 1024})
    result = bounded.search_code("nothing")
    assert not result["scope_complete"] and "byte_limit" in result["limited_by"]
    bounded = type(tools)({**config, "max_walk_entries": 1})
    assert "entry_limit" in bounded.find_files()["limited_by"]
    (root / "src" / "bad.py").write_bytes(b"\xff\xfe")
    with pytest.raises(error, match="UTF-8"):
        tools.read_code("src/bad.py")
    original = tools.scope.resolve
    calls = 0

    def changed(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            (root / "src" / "main.py").write_text("replacement = True\n", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(tools.scope, "resolve", changed)
    with pytest.raises(error, match="changed while reading"):
        tools.read_code("src/main.py")


@pytest.mark.parametrize(
    "suffix,source,expected",
    [
        (
            ".ts",
            "export interface Options { size: number }\n"
            "export class Garden { water() { return 1; } }\n",
            {"Options", "Options.size", "Garden", "Garden.water"},
        ),
        (
            ".tsx",
            "export const View = (props: {name: string}) => <span>{props.name}</span>;\n",
            {"View"},
        ),
        (".js", "export function orchard(x) { return x + 1; }\n", {"orchard"}),
    ],
)
def test_real_js_ts_tsx_syntax_parsers(code, suffix, source, expected):
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_javascript")
    pytest.importorskip("tree_sitter_typescript")
    root, tools, _, _ = code
    path = "src/example" + suffix
    (root / path).write_text(source, encoding="utf-8")
    result = tools.file_symbols(path)
    assert result["scope_complete"], result
    assert {symbol["qualified_name"] for symbol in result["items"]} == expected
    assert tools.syntax_problems(path)["problems"] == []


def _server_module():
    spec = importlib.util.spec_from_file_location("self_code_server_test", PLUGIN / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.anyio
async def test_mcp_schemas_and_host_workflow_read_effect(code):
    from app.core.tasks.workflow.mcp_skills import _skill_from_descriptor
    from app.mcp.client.manager import McpToolDescriptor

    _, _, _, config = code
    server = _server_module().create_server(config)
    descriptors = await server.list_tools()
    assert len(descriptors) == 9
    assert {d.name for d in descriptors}.isdisjoint({"execute", "edit", "run_tests", "terminal"})
    for descriptor in descriptors:
        assert descriptor.annotations.readOnlyHint is True
        assert descriptor.annotations.openWorldHint is False
        skill = _skill_from_descriptor(
            McpToolDescriptor(
                "self_code", descriptor.name, descriptor.description, descriptor.inputSchema, "read"
            )
        )
        assert skill.effect == "read"
        assert skill.input_schema == descriptor.inputSchema
    result = await server.call_tool(
        "read_code", {"path": "src/main.py", "start_line": 3, "end_line": 5}
    )
    # FastMCP returns text blocks and structured output for dict results.
    blocks = result[0] if isinstance(result, tuple) else result
    assert "Garden" in blocks[0].text
    for name, arguments in (
        ("workspace_info", {}),
        ("find_files", {"pattern": "**/*.py"}),
        ("search_code", {"query": "orchard"}),
        ("file_symbols", {"path": "src/main.py"}),
        ("find_symbol", {"query": "Garden"}),
        ("read_symbol", {"path": "src/main.py", "name": "Garden.orchard"}),
        ("find_references", {"name": "orchard"}),
        ("syntax_problems", {"path": "src/main.py"}),
    ):
        result = await server.call_tool(name, arguments)
        blocks = result[0] if isinstance(result, tuple) else result
        assert blocks[0].text, name


@pytest.mark.anyio
async def test_stdio_plugin_runs_in_isolated_process_and_returns_source_evidence(code):
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_javascript")
    pytest.importorskip("tree_sitter_typescript")
    from app.plugins.sdk import PluginApi
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    _, _, _, config = code
    spec = importlib.util.spec_from_file_location("self_code_entry_test", PLUGIN / "entry.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    api = PluginApi(plugin_id="self_code", plugin_root=PLUGIN, config=config)
    module.define_plugin(api)
    server = api.server_spec
    assert server["command"] == sys.executable and server["args"][0] == "-I"
    params = StdioServerParameters(
        command=server["command"], args=server["args"], env=server["env"]
    )
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert len(listed.tools) == 9
        result = await session.call_tool("find_symbol", {"query": "Garden.orchard", "exact": True})
        assert not result.isError
        payload = json.loads(result.content[0].text)
        assert payload["items"][0]["qualified_name"] == "Garden.orchard"
        assert payload["items"][0]["sha256"]
        denied = await session.call_tool("read_code", {"path": "../private.py"})
        assert denied.isError and "repository-relative" in denied.content[0].text


def test_conversation_tool_to_real_plugin_to_checked_workflow(code, tmp_path):
    """No fake tool execution: use the real manager, stdio server and task child."""
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_javascript")
    pytest.importorskip("tree_sitter_typescript")
    from types import SimpleNamespace
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import ExternalMcpServer
    from app.core.tasks.handlers.mcp_tool import McpToolHandler
    from app.core.tasks.task_events import TaskEventStore
    from app.core.tasks.task_orchestrator import TaskOrchestrator
    from app.core.tasks.task_store import TaskStore
    from app.core.tasks.workflow.goal_workflow_handler import GoalWorkflowHandler
    from app.core.tasks.workflow.mcp_skills import register_mcp_skills
    from app.core.tasks.workflow.skill_registry import WorkflowSkillRegistry
    from app.llm.tools.workflow_tools import StartWorkflowTool
    from app.mcp.client.manager import ExternalMcpManager

    _, _, _, config = code
    parser_roots = sorted(
        {
            str(Path(importlib.util.find_spec(name).origin).parent.parent)
            for name in ("tree_sitter", "tree_sitter_javascript", "tree_sitter_typescript")
        }
    )
    manager = ExternalMcpManager(
        [
            ExternalMcpServer(
                id="self_code",
                name="Source code",
                command=sys.executable,
                args=("-I", str(PLUGIN / "server.py"), "--config", json.dumps(config)),
                env={"AIKO_CODE_DEPENDENCIES": json.dumps(parser_roots)},
            )
        ],
        connect_timeout=8,
    )
    db = ChatDatabase(tmp_path / "jobs.db")
    store = TaskStore(db)
    orchestrator = TaskOrchestrator(
        store=store, event_store=TaskEventStore(db), heartbeat_enabled=False
    )
    try:
        manager.start()
        end = time.monotonic() + 8
        while not manager.list_available_tools() and time.monotonic() < end:
            time.sleep(0.03)
        descriptors = manager.list_available_tools()
        assert len(descriptors) == 9
        assert all(item.effect == "read" for item in descriptors)
        selected = next(item.qualified_name for item in descriptors if item.name == "read_symbol")
        registry = WorkflowSkillRegistry()
        register_mcp_skills(registry, manager)
        orchestrator.register_handler(
            McpToolHandler(
                manager=manager,
                resolve_approval=lambda _: pytest.fail("A read-only tool asked to mutate"),
                can_dispatch=orchestrator.claim_operation_dispatch,
            )
        )

        class Client:
            def chat_json(self, messages, **kwargs):
                if kwargs["surface"] == "workflow_verify":
                    assert "return value + 1" in messages[1]["content"]
                    return json.dumps(
                        {"supported": True, "reason": "The method source supports it."}
                    ), None
                children = store.list_for_user("u", limit=10)
                child = next((row for row in children if row.parent_task_id), None)
                decision = (
                    {
                        "action": selected,
                        "profile": "reader",
                        "args": {"path": "src/main.py", "name": "Garden.orchard"},
                    }
                    if child is None
                    else {
                        "action": "finish",
                        "outcome": "success",
                        "findings": "Garden.orchard returns value + 1 (src/main.py:4-5).",
                        "evidence_ids": [child.id],
                    }
                )
                return json.dumps(decision), None

        orchestrator.register_handler(
            GoalWorkflowHandler(
                orchestrator=orchestrator, skill_registry=registry, worker_client_provider=Client
            )
        )
        session = SimpleNamespace(
            _user_id="u",
            _task_orchestrator=orchestrator,
            _active_turn_user_text="Explain what Garden.orchard does.",
            session_key="conversation",
        )
        started = json.loads(
            StartWorkflowTool(session).run({"goal": "Explain Garden.orchard from source"})
        )
        task_id = started["task_id"]
        assert orchestrator.wait_for_task(task_id, timeout=8) == "done"
        row = store.get(task_id)
        assert row.result["outcome"] == "success", row.result
        assert row.result["verification"]["status"] == "supported"
        assert row.state["profile"] == "reader"
        assert len(store.list_children(task_id)) == 1
    finally:
        orchestrator.shutdown(wait=False)
        manager.stop()
