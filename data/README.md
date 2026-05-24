# Data directory

Runtime data lives here. Most paths are gitignored — they're created on first run.

## Live (v1)

- **`chat_sessions.db`** — SQLite. Chat messages, rolling session summaries, long-term memories. Source of truth for memory metadata.
- **`lancedb/`** — LanceDB vector store. Mirrors `memories`, indexes chat `messages`, and holds chunked uploaded `documents`. Driven by `app/core/rag_store.py`.
- **`documents/`** — Originals of files uploaded through the Documents section in the web Settings drawer. Indexed into `lancedb/documents` by `app/core/document_ingestor.py`.
- **`persona/`** — Text persona prompts (`aiko_companion.txt` is the default loaded by `PromptAssembler`).
- **`personas/`** — Live2D persona models, one per uploaded zip. The active model lives in `personas/active/` plus a `_persona.json` manifest. Managed by `app/core/persona_manager.py`.
- **`crashlog.txt`** — JSONL crash log written by `app/core/crash_logging.py`.

## Manual asset library

- **`../live-2d-models/`** (project root, gitignored) — raw Live2D model trees you've collected. Not read by the runtime; zip a model up and upload it via the web UI to install it into `personas/`.
