import json
from types import SimpleNamespace as NS

from app.core.memory.pattern_accounting import count_intake, intake_snapshot
from app.core.memory.memory_extractor import MemoryExtractor
from app.core.memory.shared_shorthand import source_receipt
from app.core.session.concept_patterns_mixin import ConceptPatternsMixin
from app.mcp.server_tools.surfacing_outcome_tools import register


def test_aggregate_diagnostics_expose_counts_and_ids_without_private_content():
    source = NS(id=1, role="user", session_id="s", content="Private adopted definition")
    value = {"owner": "u", "sources": [source_receipt(source, source.content)]}
    row = NS(metadata={"shared_shorthand": value})
    kv = {}
    reader = NS(get_message_row=lambda _: source, kv_get=kv.get,
                kv_set=lambda k, v: kv.update({k: v}))

    class Host(ConceptPatternsMixin):
        _memory_store = NS(list_recent=lambda **k: [row] if k["kind"] == "shared_term" else [])
        _chat_db = reader
        _user_id = "u"

        def get_last_metrics(self):
            return {"concepts_surfaced": {"patterns": [{"memory_id": "7", "included": True}]}}

    count_intake(reader, "u", shorthand_proposed=2, shorthand_admitted=1, shorthand_rejected=1)
    report = Host().pattern_learning_snapshot()
    assert report["intake"]["shorthand_proposed"] == 2
    assert report["by_kind"]["shared_term"]["source_valid"] == 1
    assert report["last_turn_patterns"][0]["memory_id"] == "7"
    assert source.content not in json.dumps(report)
    assert intake_snapshot(reader, "other") == {}


def test_debug_tools_use_the_public_facade():
    callbacks = {}

    def tool():
        def bind(fn):
            callbacks[fn.__name__] = fn
            return fn
        return bind

    host = NS(pattern_learning_snapshot=lambda: {"intake": {}},
              understanding_arcs=lambda **k: {"subjects": k["subject"], "arcs": []})
    register(NS(tool=tool), host)
    assert json.loads(callbacks["get_pattern_learning_state"]()) == {"intake": {}}
    report = json.loads(callbacks["get_understanding_arcs"]("relationship"))
    assert report["subjects"] == "relationship"


def test_extraction_context_omits_another_owners_structured_definitions():
    own = NS(id=1, kind="shared_term", content="Own shared meaning", created_at="2026-10-01",
             metadata={"shared_shorthand": {"owner": "u"}})
    other = NS(id=2, kind="shared_term", content="PRIVATE OTHER MEANING", created_at="2026-10-01",
               metadata={"shared_shorthand": {"owner": "other"}})
    extractor = object.__new__(MemoryExtractor)
    extractor._store = NS(list_recent=lambda **k: [own, other], list_top=lambda **k: [])
    extractor._user_id_provider = lambda: "u"
    text = extractor._format_existing(owner="u")
    assert own.content in text
    assert other.content not in text
