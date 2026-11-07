# Source code plugin

Opt-in editor-style tools for Aiko's background workflow. All nine MCP
operations advertise read-only annotations and reuse the existing planner,
profiles, task evidence and work drawer. Normal conversation delegates through
`start_workflow`; no new fast-brain catalogue or persona block is added.

## Tools

| Tool | Result |
| --- | --- |
| `workspace_info` | Explicit source scope, limits, supported languages and local checkout HEAD when available |
| `find_files` | Glob filename matches, sizes and modification stamps |
| `search_code` | Literal text matches with path, line, character column and excerpt |
| `read_code` | Bounded source ranges, including continuation within a long line |
| `file_symbols` | File outline of syntax declarations and qualified names |
| `find_symbol` | Declaration candidates by name across a bounded source scope |
| `read_symbol` | Function/class/declaration body; ambiguity is returned rather than guessed |
| `find_references` | Exact identifier candidates, excluding ordinary comments/string text |
| `syntax_problems` | Parser syntax errors in one supported source file |

Python uses the host interpreter's AST and tokenizer. JavaScript/JSX,
TypeScript and TSX use [Tree-sitter](https://github.com/tree-sitter/py-tree-sitter)
with pinned [JavaScript](https://github.com/tree-sitter/tree-sitter-javascript)
and [TypeScript/TSX](https://github.com/tree-sitter/tree-sitter-typescript) grammars.
Parsing stays local. These are syntax tools, not language-server type/binding
resolution: equal names can identify different declarations. Syntax errors are
not compiler/typechecker/linter diagnostics, and reading a test is not running it.

Text read/search supports additional source/configuration/documentation formats;
syntax tools explicitly report unsupported languages. Future LSP and semantic
search adapters can build on these operation/result boundaries. This version
does not maintain an embedding index or background watcher.

## Configure and enable

The bundle is disabled by default. It has no implicit source root or scope.

1. Copy `config/user.example.json` to the ignored `config/user.json`, selecting
   the actual checkout root and relative directories/files Aiko may inspect.
   The example uses `F:/Projects/assistant` with app/frontend/test/script/docs
   source; change it for another installation. `include_paths` is required.
2. Set `plugins.entries.self_code.enabled` to `true` in the app's ignored
   `config/user.json` (and ensure the `plugins.enabled` master switch is on),
   then restart Aiko. Activation installs the pinned parser wheels into the
   existing per-plugin dependency target. Guidance reload alone does not add a
   new MCP server to an already-running app.

The root must exist and be absolute. Include paths may name files or directories;
`.` explicitly admits permitted source throughout that root. Include order sets
scan priority. `exclude_patterns` only narrow access and match case-insensitively.
Search filename globs are case-sensitive and support recursive `**`.

Source contents reach the configured workflow model when used. Select paths
accordingly; enabling this plugin does not change model routing. Do not use a
whole home directory as an incidental source scope.

Example request: "Look at how your completed tasks reach our conversation.
Explain the main path and cite the implementation and tests." Aiko chooses
the reader/researcher/reviewer role herself.

## Source identity and limits

Paths are repository-relative. Lines and character columns are 1-based; columns
use Unicode code points, not UTF-8 bytes or LSP UTF-16 offsets. Reads/search hits
carry SHA-256 and modification stamps. Pass `expected_sha256` when revisiting or
paging a file; query continuations require the prior `revision`, or a pinned
file hash for symbol outlines. Changed sources are rejected instead of mixing
versions. Read pages return `next.start_line/start_column`; preserve the original
end line for a particular range.

Defaults cap each file at 512 KiB, queries at 400 matching source files / 8 MiB,
and traversal at 12,000 entries. Query time is checked between bounded file and
parser operations, with a three-second default. Responses also cap lines,
characters, parser nodes, symbols, matches and serialized page size. Partial
scans expose their limits; zero matches in a partial scope do not prove absence.
Narrow the path pattern before expanding a search.

Checkout HEAD comes from bounded local Git metadata without running Git, hooks,
filters or project commands. Worktree/external Git metadata can report an
unavailable HEAD. Dirty state is not evaluated and running-build match is not
verified. File hashes identify the inspected working contents even when HEAD is
known; they do not establish a clean checkout.

## Access boundary

Each read checks include scope, resolved path, regular-file type, size, identity
and any expected hash. Symlinks, junctions, hardlinked files, traversal,
absolute/drive-relative paths and Windows alternate streams/devices are rejected.
Common private data, logs, attachments, credential/secret files, hidden
directories, dependencies and build output are excluded. Filename rules cannot
detect every secret inside otherwise admitted source; explicit scope matters.

The server uses Python isolated mode and trusted plugin/parser paths. The target
checkout is not added to Python imports. Source/comments are untrusted evidence,
never instructions or permission to read broader paths. The plugin does not
write companion memories or run an LLM of its own.

Installed plugins remain trusted code under the existing SDK. Source checks are
application policy, not an OS sandbox against hostile concurrent filesystem
changes. Editing, terminals, builds/tests, installation, commits, publishing and
restarts need separate capabilities and authorization.

## Verification boundary

Tests use disposable repositories/databases and the real stdio MCP server. The
complete `start_workflow` -> discovered tool -> child evidence -> completion
review path uses a scripted planner/reviewer. Regressions cover access boundaries,
junction/hardlink escapes, Unicode locations, revision changes, paging, ambiguity
and actual Python/JS/TS/TSX parsers.

Parser-dependent tests skip if optional plugin dependencies are absent from the
test interpreter; run them with the pinned wheels when changing language adapters.
These checks do not establish live-model usefulness or that the configured
checkout matches the running Aiko process.
