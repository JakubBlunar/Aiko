# Agent rules & reference

Detailed guidance for anyone (or any AI assistant — Cursor, Copilot, Claude,
…) working in this repo. These files were split out of the root
[`AGENTS.md`](../AGENTS.md) so the always-loaded context stays small; read the
relevant file **on demand** rather than loading everything up front.

`AGENTS.md` keeps only the project overview + the always-apply hard rules and
points here for the rest.

| File | Read it when you need… |
|------|------------------------|
| [`mcp-server.md`](mcp-server.md) | To interact with or debug the **live app** — the embedded MCP debug server (`http://localhost:6274/sse`): how to connect (Cursor / VSCode / Copilot), the core tools (`send_message`, `get_status`, `get_last_response_detail`, …), and how to add your own debug tools. |
| [`code-conventions.md`](code-conventions.md) | To change **any subsystem** — the reference catalogue of architecture conventions and per-feature design notes: LLM provider routing + prompt-cache tiers, memory tiers + RAG, the K-series personality/affect/relationship features, avatar/Live2D channels, Tauri shell, tasks / brain orchestration, external MCP clients, and more. Grep it for the area you're about to touch. |
| [`debugging.md`](debugging.md) | To diagnose a **runtime problem** — the log stream (where to look, line shape + canonical fields), the symptom → grep-target table, the level cheat sheet, and the practical workflow. |

Deeper design docs live under [`docs/`](../docs/) and are linked from the files
above.
