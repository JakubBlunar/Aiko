"""Execute an opted-in fast tool using its current registry and privacy boundary."""

from app.core.tasks.handler_names import HANDLER_FAST_TOOL
from app.core.tasks.task_handler import TaskCompleted, TaskFailed


class FastToolHandler:
    name = HANDLER_FAST_TOOL

    def __init__(self, registry_provider):
        self._registry = registry_provider

    def start(self, args, emit):
        registry = self._registry(args.get("user_id"), args.get("source_session"))
        if registry is None:
            emit(TaskFailed(error="The conversation owning this tool is no longer active."))
            return {}
        generation = args.get("generation")
        if not isinstance(generation, str) or not generation:
            emit(TaskFailed(error="Missing fast-tool contract revision."))
            return {}
        result = registry.dispatch(
            args.get("tool_name"), args.get("tool_args", {}), workflow_generation=generation,
        )
        current = self._registry(args.get("user_id"), args.get("source_session"))
        if current is None or current.workflow_revision(args.get("tool_name")) != generation:
            emit(TaskFailed(error="Tool settings or conversation changed during the operation."))
        elif not result.ok:
            emit(TaskFailed(error=result.content))
        else:
            emit(TaskCompleted(result={"content": result.content, "summary": result.content[:280]}))
        return {}

    def resume(self, state, emit):
        emit(TaskFailed(error="Fast-tool call interrupted; plan a fresh read."))
        return state

    def on_input(self, state, answer, emit):
        emit(TaskFailed(error="Fast tools do not accept input."))
        return state

    def cancel(self, state):
        pass
