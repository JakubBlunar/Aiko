---
name: filesystem
description: Sandboxed file read/write under a fixed absolute root
---
Filesystem skills — how to use them well:
- These MCP file skills are sandboxed to a fixed root that is NOT the working directory. FIRST call the list-allowed-directories skill to learn the exact absolute root, then build every path as an absolute path UNDER that root (e.g. <root>/notes/file.txt).
- NEVER pass a bare, relative, or label-style path (like "Documents/foo.txt" or "Documents:foo.txt") to these skills — a non-absolute path resolves against the process working directory and is rejected as "path outside allowed directories". The built-in file skills' "Documents" label does NOT apply here.
- Confirm a path exists with the list-directory or get-file-info skill before reading/writing when unsure.
- There is no copy tool: to COPY a file, read the source then write the destination (both absolute, under the root). Use the move skill to move/rename and the create-directory skill to make folders.
- If a write/move is denied as outside the root, do NOT retry the same path — re-read the allowed root and rebuild the path under it, or finish and report the path problem plainly.
