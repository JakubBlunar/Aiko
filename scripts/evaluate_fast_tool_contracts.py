"""Offline contract probes for brain tools; no model, network or install writes.

Results identify observable gaps, not a live-model success rate. Findings are
reported as data so the same probes can be rerun after implementation changes.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def evaluate() -> dict:
    from app.core.memory.memory_origin import MemoryOrigin
    from app.core.rag.rag_retriever import RagRetriever
    from app.core.session.tool_pass_gate import (
        GateContext, _TOOL_FAMILY, select_active_tool_names, should_run_tool_pass,
    )
    from app.llm.tools.base import ToolRegistry
    from app.llm.tools.builtins import GetTimeTool, RecallConceptTool, RecallTool, RecallTopicTool
    from app.llm.tools.diary import ReadDiaryTool
    from app.llm.tools.workflow_tools import CheckMyWorkTool
    from app.llm.tools.world import LookAroundTool

    checks = []

    def record(name, passed, observed):
        checks.append({"name": name, "status": "pass" if passed else "finding",
                       "observed": observed})

    qualification = "Only in an imagined scene; this never happened physically."
    content = "An imagined account with several details. " * 15 + qualification
    row = NS(id=77, content=content, kind="event", salience=.7,
             source_session="synthetic:one", source_message_id=9,
             created_at="2026-10-01T12:00:00+00:00", last_used_at=None, use_count=0,
             metadata={"memory_origin": asdict(MemoryOrigin(
                 "aiko", "virtual_event", "probe", "virtual"))},
             provenance="inferred", temporal_type="past_event", event_time=None,
             confidence=.8, tier="long_term", embedding=np.array([1., 0.]))
    hit = RagRetriever._memory_to_hit(row, score=.8)
    rag = NS(retrieve=lambda query: [hit], recall_topic=lambda topic, **kw: ("A scene", [hit]))
    for tool, arguments in ((RecallTool(rag), {"query": "a scene"}),
                            (RecallTopicTool(rag), {"topic": "a scene"})):
        payload = json.loads(tool.run(arguments))
        item = payload["hits"][0]
        record(tool.schema().name + ":whole_qualified_source",
               qualification in item["text"] and "subject" in item and "created_at" in item,
               {"keys": sorted(item), "end_qualification_retained": qualification in item["text"],
                "input_characters": len(content), "output_characters": len(item["text"])})

    graph = NS(best_clusters_for=lambda *a, **kw: [(77, "A scene", 1.)],
               cluster_member_ids=lambda cid: [77])
    retriever = RagRetriever(NS(), NS(embed=lambda text: np.array([1., 0.])),
                             memory_store=NS(get=lambda mid: row), topic_graph=graph)
    payload = json.loads(RecallTopicTool(retriever).run({"topic": "a scene", "limit": 1}))
    record("recall_topic:coverage_and_continuation",
           "coverage" in payload and any(key in payload for key in ("total", "has_more")),
           {"keys": sorted(payload)})

    row.metadata.update(superseded_by=78, belief_revision="inaccurate")
    payload = json.loads(RecallTopicTool(retriever).run({"topic": "a scene"}))
    items = payload["hits"]
    record("recall_topic:withdrawn_source_status",
           not items or any("superseded" in json.dumps(item) for item in items),
           {"returned_withdrawn_rows": len(items), "keys": sorted(items[0]) if items else []})

    def unavailable(text):
        raise RuntimeError("synthetic unavailable embedder")
    broken = RagRetriever(NS(), NS(embed=unavailable), memory_store=NS(), topic_graph=graph)
    broken._concept_store = NS()
    for tool, arguments in ((RecallTopicTool(broken), {"topic": "a scene"}),
                            (RecallConceptTool(broken), {"query": "a scene"})):
        registry = ToolRegistry()
        registry.register(tool)
        result = registry.dispatch(tool.schema().name, arguments)
        record(tool.schema().name + ":lookup_failure_differs_from_no_match", not result.ok,
               {"ok": result.ok, "payload": json.loads(result.content) if result.ok
                else {"error": result.content}})

    shifted = datetime(2031, 2, 3, 12, tzinfo=timezone.utc)
    with patch("app.core.infra.timephrase.now", return_value=shifted), patch(
        "app.core.infra.timephrase.utcnow", return_value=shifted,
    ):
        local = json.loads(GetTimeTool().run({}))
        explicit = json.loads(GetTimeTool().run({"timezone": "UTC"}))
    record("get_time:timezone_uses_narrative_clock",
           datetime.fromisoformat(local["iso"]) == datetime.fromisoformat(explicit["iso"]),
           {"default_iso": local["iso"], "explicit_timezone_iso": explicit["iso"]})

    def status_result(fail=False):
        def read(**kw):
            if fail:
                raise RuntimeError("synthetic unavailable task store")
            return []
        registry = ToolRegistry()
        registry.register(CheckMyWorkTool(NS(_user_id="synthetic",
            _task_orchestrator=NS(list_running=read))))
        return registry.dispatch("check_my_work", {})

    healthy, failed = status_result(), status_result(True)
    record("check_my_work:failure_differs_from_empty",
           not failed.ok or healthy.content != failed.content,
           {"failure_ok": failed.ok, "same_as_healthy_empty": failed.content == healthy.content})
    properties = CheckMyWorkTool(NS()).schema().parameters.get("properties", {})
    record("check_my_work:completed_task_drilldown",
           "task_id" in properties or "status" in properties,
           {"accepted_arguments": sorted(properties), "reads_active_rows_only": "task_id"
            not in properties})

    names = list(_TOOL_FAMILY)
    for text in ("Why do you think that about me?", "What have you figured out about me?",
                 "What are you up to?", "Can you write a file with those notes?",
                 "Do you remember what I said?", "What's the weather?"):
        decision = should_run_tool_pass(text, names, context=GateContext())
        record("gate:" + text, decision.run,
               {"run": decision.run, "reason": decision.reason, "matched": decision.matched})
    decision = should_run_tool_pass("Those lenses", names, context=GateContext(recall_need=True))
    allowed = select_active_tool_names(decision, names, router_enabled=False)
    record("gate:premise_stays_read_only", allowed == {
        name for name in names if _TOOL_FAMILY[name] == "recall"},
        {"reason": decision.reason, "allowed": sorted(allowed or [])})

    stale_hint = "list_file_roots" in LookAroundTool(NS()).schema().description
    record("look_around:all_named_tools_exist", not stale_hint or "list_file_roots" in names,
           {"mentions_list_file_roots": stale_hint,
            "target_registered": "list_file_roots" in names})
    # A fact source is not accessible through the diary's entry-id escape hatch.
    try:
        ReadDiaryTool(NS(memory_store=NS(get=lambda mid: NS(kind="fact")))).run(
            {"action": "get", "entry_id": 77})
        fact_readable = True
    except Exception as exc:
        from app.llm.tools.base import ToolError
        if not isinstance(exc, ToolError):
            raise
        fact_readable = False
    dedicated = any(name in names for name in ("read_memory", "inspect_memory"))
    record("memory:exact_fact_inspection_available", fact_readable or dedicated,
           {"diary_accepts_fact_id": fact_readable, "dedicated_memory_reader": dedicated})
    return {"scope": "synthetic offline contracts; no live-model calls or state writes",
            "checks": checks,
            "summary": {status: sum(c["status"] == status for c in checks)
                        for status in ("pass", "finding")}}


if __name__ == "__main__":
    print(json.dumps(evaluate(), ensure_ascii=False, indent=2))
