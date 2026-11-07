---
name: filesystem
description: File read/write inside explicitly configured absolute folders
---
Filesystem skills — how to use them well:
- These MCP file skills are restricted to the configured allowed folders, which are NOT the working directory. FIRST call list-allowed-directories to learn the absolute folders. Choose the folder matching the user's request; if ambiguous, ask. Build each path as an absolute path inside that allowed folder.
- NEVER pass a bare, relative, or label-style path (like "Documents/foo.txt" or "Documents:foo.txt") to these skills — a non-absolute path resolves against the process working directory and is rejected as "path outside allowed directories". The built-in file skills' "Documents" label does NOT apply here.
- Confirm a path exists with the list-directory or get-file-info skill before reading/writing when unsure.
- There is no copy tool: to COPY a file, read the source then write the destination (both absolute and inside allowed folders). Use the move skill to move/rename and the create-directory skill to make folders.
- If access is denied, do not try a broader spelling or guess a different folder. Re-read the allowed folders and report the path problem if the requested resource is outside them. Changing plugin access invalidates old operations and approvals; start a fresh plan instead of redirecting a queued operation.
