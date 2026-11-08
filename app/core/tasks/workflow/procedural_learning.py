"""Evidence-backed procedural notes: learn operation order, never replay old arguments."""

from __future__ import annotations

import json

from app.core.infra import timephrase
from app.core.persona.speech_guide import digest
from app.core.proactive.idle_worker import WorkSignal
from app.core.tasks.workflow.operation_contract import input_schema
from app.core.tasks.workflow.profiles import supports
from app.core.tasks.workflow.discovery import terms

METHOD_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_methods (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, operations TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_methods_owner ON work_methods(user_id);
CREATE TABLE IF NOT EXISTS work_method_sources (
    task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    method_id TEXT REFERENCES work_methods(id) ON DELETE CASCADE,
    request_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    verdict TEXT NOT NULL, revision TEXT NOT NULL
);
"""


def contract_revision(skill):
    return digest([skill.name, input_schema(skill), skill.effect])


class ProceduralLearning:
    name = "work_procedural_learning"
    interval_seconds = 3600

    def __init__(self, db, orchestrator, skills):
        self.db, self.orchestrator, self.skills = db, orchestrator, skills

    def _evidence(self, row, *, allow_failures=False, check_generation=True):
        children = []
        operations = []
        for step in row.state.get("steps", []):
            if not step.get("child_id"):
                continue
            child = self.orchestrator.get(step["child_id"])
            skill = self.skills.get(step.get("skill"))
            if (
                not child
                or child.parent_task_id != row.id
                or child.status
                not in (
                    {"done", "failed", "cancelled", "interrupted"} if allow_failures else {"done"}
                )
                or skill is None
            ):
                return None
            generation = (child.args or {}).get("_config_generation", "")
            if check_generation and generation != skill.generation:
                return None
            operations.append({"name": skill.name, "contract": contract_revision(skill)})
            children.append([child.id, child.result])
        if not (1 if allow_failures else 2) <= len(operations) <= 8:
            return None
        return operations, digest([row.result, children])

    def record(self, row):
        conn = self.db._get_conn()
        supported = (
            row.status == "done"
            and (row.result or {}).get("outcome") == "success"
            and (row.result or {}).get("verification", {}).get("status") == "supported"
        )
        evidence = self._evidence(row, allow_failures=not supported)
        method_id, revision = None, ""
        if supported and evidence:
            operations, revision = evidence
            method_id = digest([row.user_id, operations])
        elif row.state.get("adopted_recipe") and evidence:
            candidate = conn.execute(
                "SELECT operations FROM work_methods WHERE id=? AND user_id=?",
                (row.state["adopted_recipe"], row.user_id),
            ).fetchone()
            if candidate:
                expected = json.loads(candidate[0])
                if evidence[0] == expected[: len(evidence[0])]:
                    method_id, revision = row.state["adopted_recipe"], evidence[1]
        metadata = row.metadata or {}
        request_id = metadata.get("source_message_id")
        request = (
            conn.execute(
                "SELECT role,session_id FROM messages WHERE id=?", (request_id,)
            ).fetchone()
            if type(request_id) is int
            else None
        )
        if not request or request[0] != "user" or request[1] != metadata.get("source_session"):
            request_id = None
        with conn:
            if supported and method_id:
                conn.execute(
                    "INSERT OR IGNORE INTO work_methods VALUES (?,?,?,?)",
                    (
                        method_id,
                        row.user_id,
                        json.dumps(evidence[0]),
                        timephrase.utcnow().isoformat(),
                    ),
                )
            conn.execute(
                "INSERT OR IGNORE INTO work_method_sources VALUES (?,?,?,?,?)",
                (row.id, method_id, request_id, "supported" if supported else "failed", revision),
            )
        return method_id

    def relevant(self, user, profile, *, goal=""):
        rows = (
            self.db._get_conn()
            .execute(
                "SELECT id,operations FROM work_methods WHERE user_id=? "
                "ORDER BY created_at DESC LIMIT 64",
                (user,),
            )
            .fetchall()
        )
        available = []
        for mid, encoded in rows:
            operations = json.loads(encoded)
            if any(
                self.skills.get(item["name"]) is None
                or contract_revision(self.skills.get(item["name"])) != item["contract"]
                or not supports(self.skills.get(item["name"]), profile)
                for item in operations
            ):
                continue
            if goal:
                domain = " ".join(
                    item["name"]
                    + " "
                    + self.skills.get(item["name"]).description
                    + " "
                    + self.skills.get(item["name"]).group
                    for item in operations
                )
                if not set(terms(goal)) & set(terms(domain)):
                    continue  # a valid method is not automatically relevant to this job
            sources = (
                self.db._get_conn()
                .execute(
                    "SELECT task_id,request_id,verdict,revision FROM work_method_sources "
                    "WHERE method_id=? ORDER BY task_id DESC",
                    (mid,),
                )
                .fetchall()
            )
            last_failure = max(
                (source[0] for source in sources if source[2] == "failed"), default=0
            )
            requests, evidence_ids = set(), []
            for tid, request_id, verdict, revision in sources:
                if tid <= last_failure or verdict != "supported" or request_id is None:
                    continue
                row = self.orchestrator.get(tid)
                evidence = (
                    self._evidence(row, check_generation=False)
                    if row and row.user_id == user
                    else None
                )
                if (
                    not evidence
                    or evidence[1] != revision
                    or row.status != "done"
                    or (row.result or {}).get("verification", {}).get("status") != "supported"
                ):
                    continue
                requests.add(request_id)
                evidence_ids.append(tid)
            if len(requests) >= 2:
                available.append(
                    {
                        "id": mid,
                        "operations": [item["name"] for item in operations],
                        "independent_requests": len(requests),
                        "source_jobs": evidence_ids[:3],
                        "instruction": "A previously verified order, not a guarantee. Supply fresh "
                        "arguments/targets, check prerequisites and verify the result.",
                    }
                )
            if len(available) >= 3:
                break
        return available

    def _pending(self):
        return (
            self.db._get_conn()
            .execute(
                "SELECT t.id FROM tasks t LEFT JOIN work_method_sources s ON s.task_id=t.id "
                "WHERE t.parent_task_id IS NULL AND t.handler_name='goal_workflow' "
                "AND t.status IN ('done','failed','cancelled','interrupted') AND s.task_id IS NULL "
                "ORDER BY t.id LIMIT 24"
            )
            .fetchall()
        )

    def is_ready(self, *, now, last_run_at):
        return True

    def demand(self, *, now, last_run_at):
        pending = self._pending()
        return WorkSignal(
            pressure=min(0.3, len(pending) / 80),
            needs_llm=False,
            reason=f"{len(pending)} completed jobs to learn from",
        )

    def run(self):
        count = 0
        for (tid,) in self._pending():
            row = self.orchestrator.get(tid)
            if row:
                self.record(row)
                count += 1
        return {"examined": count}
