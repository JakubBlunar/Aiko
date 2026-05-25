# Data directory

Runtime data lives here. Most paths are gitignored — they're created on first run.

## Live (v1)

- **`chat_sessions.db`** — SQLite. Chat messages, rolling session summaries, long-term memories. Source of truth for memory metadata.
- **`lancedb/`** — LanceDB vector store. Mirrors `memories`, indexes chat `messages`, and holds chunked uploaded `documents`. Driven by `app/core/rag_store.py`.
- **`documents/`** — Originals of files uploaded through the Documents section in the web Settings drawer. Indexed into `lancedb/documents` by `app/core/document_ingestor.py`.
- **`persona/`** — Text persona prompts (`aiko_companion.txt` is the default loaded by `PromptAssembler`). Served by FastAPI at `/persona-text/` (renamed from `/persona/` to avoid the singular-vs-plural footgun the avatar pipeline introduced).
- **`crashlog.txt`** — JSONL crash log written by `app/core/crash_logging.py`.

## Avatar bundle (gitignored)

- **`../live-2d-models/Alexia/`** — bundled Live2D avatar files (model3.json, cdi3.json, expressions, motions, textures). Drop them in here on first checkout. The directory is gitignored so each developer ships their own copy. Loaded once at boot by `app/core/avatar_profile.py` and served as static files at `/avatar/`. Replaces the old upload-based persona pipeline.
