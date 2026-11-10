"""Share explicitly read-only brain tools as owned, inspectable workflow children."""

from app.core.tasks.handler_names import HANDLER_FAST_TOOL
from app.core.tasks.task_handler import INITIATED_BY_BACKGROUND
from app.core.tasks.workflow.skill_registry import WorkflowSkill


def shared_fast_skills(registry):
    if registry is None:
        return []
    skills = []
    for tool in registry.workflow_tools():
        def spawn(args, ctx, tool=tool):
            parent = ctx.orchestrator.get(ctx.parent_task_id)
            return ctx.orchestrator.start_task(
                user_id=ctx.user_id,
                handler_name=HANDLER_FAST_TOOL,
                args={"tool_name": tool["name"], "tool_args": args,
                      "generation": tool["generation"], "user_id": ctx.user_id,
                      "source_session": (parent.metadata or {}).get("source_session")},
                title=f"workflow: {tool['name']}",
                initiated_by=INITIATED_BY_BACKGROUND,
                notify_aiko=False,
                visible_to_user=True,
                parent_task_id=ctx.parent_task_id,
            )

        skills.append(WorkflowSkill(
            name=tool["name"], description=tool["description"],
            input_schema=tool["input_schema"], effect="read", group="fast",
            generation=tool["generation"], spawn=spawn,
        ))
    return skills
