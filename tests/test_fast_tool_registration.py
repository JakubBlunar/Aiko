from types import SimpleNamespace as NS

from app.core.session.memory_facade_mixin import MemoryFacadeMixin
from app.core.session.tool_pass_gate import _TOOL_FAMILY
from app.core.session.tools_registry_mixin import ToolsRegistryMixin


class Host(MemoryFacadeMixin, ToolsRegistryMixin):
    pass


def host():
    value = Host()
    value._settings = NS(
        tools=NS(enabled=True, recall=True, recall_topic=True, recall_concept=True,
                 recall_self_history=True, recall_hypotheses=False, get_time=False,
                 conversation_history=False, calendar=False, reminders=False, weather=False,
                 activity=False, web_search=False, world=False, goals=False, workflow=False),
        memory=NS(enabled=True), agent=NS(belief_tracking_enabled=True))
    value._user_id, value.session_key = "u", "u:s"
    value._memory_store, value._belief_store = NS(), NS()
    value._concept_store, value._concept_learning_store = NS(), NS()
    value._rag_retriever, value._embedder = NS(), NS()
    value._turn_runner = NS(set_tool_registry=lambda registry: None,
                            set_plugin_tool_gate=lambda *args: None)
    return value


def test_readers_register_through_the_real_session_builder_and_have_gate_families():
    value = host()
    value.rebuild_tool_registry()
    names = set(value.available_tool_names())
    assert names == {"recall", "recall_topic", "recall_concept", "recall_self_history",
                     "read_diary", "write_diary", "inspect_memory", "recall_beliefs"}
    assert all(_TOOL_FAMILY[name] == "recall" for name in names - {"write_diary"})


def test_exact_inspection_remains_available_when_only_concept_search_is_enabled():
    value = host()
    value._settings.tools.recall = False
    value._settings.tools.recall_topic = False
    value.rebuild_tool_registry()
    names = value.available_tool_names()
    assert "recall_concept" in names and "inspect_memory" in names
    assert "recall_beliefs" not in names and "write_diary" not in names


def test_disabled_tracking_or_tools_do_not_expose_inactive_readers():
    value = host()
    value._settings.agent.belief_tracking_enabled = False
    value.rebuild_tool_registry()
    assert "recall_beliefs" not in value.available_tool_names()
    value._settings.tools.enabled = False
    value.rebuild_tool_registry()
    assert value.available_tool_names() == []
