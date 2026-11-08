"""On-demand plugin instruction loading and checkpointed compound execution."""

from copy import deepcopy
from types import SimpleNamespace

from app.core.tasks.workflow.operation_contract import validate_arguments
from app.core.tasks.workflow.profiles import supports


class WorkflowExtensionsMixin:
    def _capability_manifest(self):
        from app.core.tasks.workflow.discovery import manifest

        return manifest(self._skills)

    def _extension_context(self, state):
        extensions = self._extensions_provider()
        playbooks = extensions.get("playbooks", {})
        loaded = []
        for name, generation in state.get("loaded_playbooks", {}).items():
            row = playbooks.get(name)
            if row and row["generation"] == generation:
                loaded.append({"name": name, "body": row["body"]})
        procedures = []
        for row in extensions.get("procedures", {}).values():
            if all(self._skills.get(f"{row['owner']}__{step['tool']}") for step in row["steps"]):
                procedures.append(
                    {k: row[k] for k in ("name", "description", "input_schema", "steps")}
                )
        methods = getattr(self, "_methods_provider", lambda: None)()
        return {
            "recipes": (
                methods.relevant(
                    state.get("contract", {}).get("requested_by", ""),
                    state.get("profile", "general"),
                    goal=state.get("goal", ""),
                )
                if methods
                else []
            ),
            "procedures": procedures[:4],
            "playbook_index": [
                {k: row[k] for k in ("name", "description")} for row in playbooks.values()
            ],
            "playbooks": loaded,
        }

    def _load_playbook(self, state, args):
        playbook = self._extensions_provider().get("playbooks", {}).get(args.get("name"))
        if playbook is None:
            return False
        loaded = state.setdefault("loaded_playbooks", {})
        loaded[playbook["name"]] = playbook["generation"]
        while len(loaded) > 2:
            del loaded[next(iter(loaded))]
        return True

    def _start_procedure(self, task_id, state, args, offered):
        from app.core.tasks.workflow.harness_mixin import _signature

        name, inputs = args.get("name"), args.get("inputs", {})
        row = self._extensions_provider().get("procedures", {}).get(name)
        if row is None or name not in offered:
            return "Procedure is not in the current menu."
        errors = validate_arguments(SimpleNamespace(input_schema=row["input_schema"]), inputs)
        if errors:
            return "; ".join(errors)
        if row.get("requires_observation"):
            current = self._current_context(state) or {}
            if (
                current.get("status") != "current"
                or current.get("server_id") != row["owner"]
                or current.get("generation") != row["generation"]
            ):
                return "Obtain a fresh observation before this procedure."

        def bind(value):
            if isinstance(value, dict):
                if set(value) == {"$input"}:
                    return deepcopy(inputs[value["$input"]])
                return {key: bind(item) for key, item in value.items()}
            if isinstance(value, list):
                return [bind(item) for item in value]
            return value

        steps = []
        try:
            for step in row["steps"]:
                name = f"{row['owner']}__{step['tool']}"
                skill = self._skills.get(name)
                if (
                    skill is None
                    or skill.generation != row["generation"]
                    or not supports(skill, state["profile"])
                ):
                    return "Procedure capabilities changed or need another profile."
                arguments = bind(step.get("arguments", {}))
                errors = validate_arguments(skill, arguments)
                if errors:
                    return "; ".join(errors)
                steps.append(
                    {
                        "skill": name,
                        "args": arguments,
                        "effect": skill.effect,
                        "schema_revision": _signature(skill),
                    }
                )
        except (KeyError, TypeError):
            return "Procedure input binding is unavailable."
        state["procedure"] = {
            "name": row["name"],
            "generation": row["generation"],
            "steps": steps,
            "index": 0,
            "cursor": state["message_cursor"],
        }
        return None

    def _advance_procedure(self, task_id, state):
        from app.core.tasks.workflow.harness_mixin import _signature

        procedure = state["procedure"]
        live = self._extensions_provider().get("procedures", {}).get(procedure["name"])
        changed = self._guidance(task_id, state)
        if changed or live is None or live["generation"] != procedure["generation"]:
            state.pop("procedure", None)
            state["notes"] = (
                "Procedure stopped before its next call: guidance or capabilities changed."
            )
            return self._save(task_id, state)
        index = procedure["index"]
        if index == 0 and live.get("requires_observation"):
            current = self._current_context(state) or {}
            if current.get("status") != "current" or current.get("server_id") != live["owner"]:
                state.pop("procedure", None)
                state["notes"] = "Procedure needs a fresh observation after resuming."
                return self._save(task_id, state)
        if index >= len(procedure["steps"]):
            state.pop("procedure", None)
            return self._save(task_id, state)
        step = procedure["steps"][index]
        skill = self._skills.get(step["skill"])
        if skill is None or _signature(skill) != step["schema_revision"]:
            state.pop("procedure", None)
            state["notes"] = "Procedure operation changed; plan from the saved results."
            return self._save(task_id, state)
        state["pending"] = {
            **step,
            "step_id": f"{task_id}:{state['iteration']}:p{index}",
            "child_id": None,
            "attempt": 0,
            "reserved": False,
            "profile": state["profile"],
            "procedure": True,
        }
        return self._save(task_id, state)
