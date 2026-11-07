"""Plugin-owned workspace routing; a relative path never crosses workspace roots."""

from __future__ import annotations

from aiko_code.policy import SourceError
from aiko_code.service import CodeTools


class WorkspaceTools:
    def __init__(self, config):
        self._legacy = "workspaces" not in config
        rows = (
            config.get("workspaces")
            if not self._legacy
            else [
                {
                    "id": "default",
                    "name": "Default workspace",
                    "root": config.get("root"),
                    "include_paths": config.get("include_paths", []),
                    "exclude_patterns": config.get("exclude_patterns", []),
                }
            ]
        )
        if not isinstance(rows, list) or not rows:
            raise SourceError("Add at least one source workspace with allowed relative paths.")
        self._tools = {}
        self._names = {}
        for row in rows:
            wid = row.get("id")
            if not isinstance(wid, str) or not wid or wid in self._tools:
                raise SourceError("Each source workspace needs a unique stable ID.")
            self._tools[wid] = CodeTools({**config, **row})
            self._names[wid] = row.get("name", wid)

    def _select(self, workspace_id):
        if not workspace_id and self._legacy:
            workspace_id = "default"
        if workspace_id not in self._tools:
            raise SourceError("Choose workspace_id from workspace_info before reading source.")
        return self._tools[workspace_id]

    def info(self, workspace_id=""):
        if workspace_id or self._legacy:
            return self._select(workspace_id).workspace_info()
        return {
            "workspaces": [
                {"id": wid, "name": self._names[wid], **tool.workspace_info()}
                for wid, tool in self._tools.items()
            ]
        }

    def call(self, workspace_id, operation, **arguments):
        return getattr(self._select(workspace_id), operation)(**arguments)
