"""Procedures require independent verified jobs and retain source/correction gates."""

from types import SimpleNamespace

from app.core.infra.chat_database import ChatDatabase
from app.core.tasks.task_store import TaskStore
from app.core.tasks.workflow.procedural_learning import ProceduralLearning
from app.core.tasks.workflow.skill_registry import WorkflowSkillRegistry, WorkflowSkill


def test_methods_need_two_independent_requests_and_never_store_old_arguments(tmp_path):
    db = ChatDatabase(tmp_path / "methods.db")
    store, skills = TaskStore(db), WorkflowSkillRegistry()
    for name in ("read", "verify"):
        skills.register(
            WorkflowSkill(
                name=name,
                description=name,
                group="files",
                effect="read",
                input_schema={"type": "object"},
            )
        )
    learner = ProceduralLearning(db, SimpleNamespace(get=store.get), skills)

    def job(request=None, recipe=None, success=True):
        if request is None:
            request = db.add_message("u:s", "user", "Inspect source")
        tid = store.create(
            user_id="u",
            handler_name="goal_workflow",
            title="Inspect",
            metadata={"source_message_id": request, "source_session": "u:s"},
        )
        steps = []
        for name in ("read", "verify"):
            cid = store.create(
                user_id="u",
                handler_name="test",
                title=name,
                parent_task_id=tid,
                args={"ref": "DO_NOT_LEARN_THIS_REF", "password": "SECRET"},
            )
            store.mark_done(cid, result={"content": "Evidence"})
            steps.append({"skill": name, "child_id": cid, "status": "done"})
        store.update_state(tid, {"steps": steps, "adopted_recipe": recipe})
        store.mark_done(
            tid,
            result={
                "outcome": "success" if success else "partial",
                "verification": {"status": "supported" if success else "unsupported"},
            },
        )
        mid = learner.record(store.get(tid))
        return tid, request, mid

    first, request, method = job()
    assert method and learner.relevant("u", "reader") == []
    job(request=request)  # two tasks from one request are not independent evidence
    assert learner.relevant("u", "reader") == []
    second, _, _ = job()
    available = learner.relevant("u", "reader")
    assert available[0]["independent_requests"] == 2
    assert available[0]["operations"] == ["read", "verify"]
    assert learner.relevant("other", "reader") == []
    encoded = str(db._get_conn().execute("SELECT * FROM work_methods").fetchall())
    assert "SECRET" not in encoded and "DO_NOT_LEARN" not in encoded
    job(recipe=method, success=False)
    assert learner.relevant("u", "reader") == []
    job()
    assert learner.relevant("u", "reader") == []
    last, _, _ = job()
    assert learner.relevant("u", "reader")
    store.delete(last)
    assert learner.relevant("u", "reader") == []
    db._get_conn().close()
