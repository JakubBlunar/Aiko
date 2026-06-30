# Data directory

Runtime data lives here. Most paths are gitignored — they're created on first run.

## Live (v1)

- **`chat_sessions.db`** — SQLite. Chat messages, rolling session summaries, long-term memories. Source of truth for memory metadata.
- **`lancedb/`** — LanceDB vector store. Mirrors `memories`, indexes chat `messages`, and holds chunked uploaded `documents`. Driven by `app/core/rag/rag_store.py`.
- **`documents/`** — Originals of files uploaded through the Documents section in the web Settings drawer. Indexed into `lancedb/documents` by `app/core/rag/document_ingestor.py`.
- **`persona/`** — Text persona prompts (`aiko_companion.txt` is the default loaded by `PromptAssembler`). Served by FastAPI at `/persona-text/` (renamed from `/persona/` to avoid the singular-vs-plural footgun the avatar pipeline introduced).
- **`crashlog.txt`** — JSONL crash log written by `app/core/infra/crash_logging.py`.

## Avatar bundle (gitignored)

- **`personas/active/Alexia/`** — bundled Live2D avatar files (model3.json, cdi3.json, expressions, motions, textures). Drop them in here on first checkout. The directory is gitignored so each developer ships their own copy. Loaded once at boot by `app/core/persona/avatar_profile.py` and served as static files at `/avatar/`. This is the single source of truth for the bundle — the Tauri/macOS app and the Docker image both seed it from here. Override `avatar.root_dir` in `config/user.json` only if you want a custom path.
