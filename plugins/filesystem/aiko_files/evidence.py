"""Exact text-note persistence proof for the filesystem bundle's existing tools."""
from __future__ import annotations

import hashlib
import ntpath

from app.plugins.sdk import MiddlewareResult, OperationPostcondition


class TextFileEvidence:
    def __init__(self, server_id):
        self.server_id = server_id

    def claims(self, server_id, tool_name):
        return server_id == self.server_id and tool_name in {
            "write_file", "read_file", "read_text_file",
        }

    def transform(self, server_id, tool_name, raw_text, tool_args=None):
        args = tool_args or {}
        path = args.get("path")
        if not isinstance(path, str) or not path:
            return None
        # Preserve POSIX case; Windows file paths are case-insensitive.
        path = ntpath.normcase(ntpath.normpath(path)) if ntpath.splitdrive(path)[0] else path
        resource = f"{server_id}:text:{path}"
        proof = None
        if tool_name == "write_file" and isinstance(args.get("content"), str):
            proof = OperationPostcondition(resource, expected={
                "sha256": hashlib.sha256(args["content"].encode()).hexdigest(),
            }, requested_resource=resource)
        elif tool_name != "write_file" and not args.get("head") and not args.get("tail"):
            proof = OperationPostcondition(resource, observed={
                "sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
            }, requested_resource=resource)
        return MiddlewareResult(raw_text, raw_text[:200], postcondition=proof)
