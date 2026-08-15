# Agent Instructions

Aiko is a local-first, web-based AI companion. This file is the **lean entry
point** — a short overview plus the hard rules that always apply. Detailed
references live in [`rules/`](rules/); read the relevant file **on demand**
instead of loading everything up front (see the index at the bottom).

## Project Overview

Aiko is built around:

- **Python 3.11+** backend (FastAPI + WebSocket) under `app/`. Entry point: `python -m app.web` (or the `aiko-web` console script).
- **React + Vite + PixiJS** frontend under `web/` (Live2D avatar, chat, voice controls, settings drawer, document upload).
- **Ollama** for chat (via `OllamaClient` directly, not LangChain). The `llm` block is a catalogue of providers plus a role → provider/model route table, so any role can point at an OpenAI-compatible endpoint instead.
- **RealtimeSTT** + **Pocket-TTS** for voice in/out, with **client-owned audio I/O**: the browser / Tauri shell captures the microphone (48 kHz Int16 mono, browser DSP) and plays back TTS, streaming raw PCM frames over the existing WebSocket. See [`docs/voice-mode.md`](docs/voice-mode.md) for the binary frame protocol and the voice-ownership lock used when multiple windows are open.
- **LanceDB** for vector RAG over memories, recent chat messages, and uploaded documents.
- **SQLite** (`data/chat_sessions.db`) as the source of truth for messages, summaries, and memory metadata.

There is no desktop / Qt / LangChain code. The web UI is the only UI.

## Core rules (always apply)

- **Run `npm run lint` before calling a change done.** `lint:py` is ruff (`F`, `E`, `W`, `B` at 100 columns) over `app/ tests/ scripts/`; `lint:web` is `tsc -b`. `npm run lint:py:fix` applies the autofixable subset. There is no CI and there are no git hooks, so this instruction is the enforcement path — the rule selection and what was deliberately left out are argued in [`pyproject.toml`](pyproject.toml).
- **Every tracked text file is LF**, in the blob and in the working tree, enforced by [`.gitattributes`](.gitattributes). If a file you barely touched shows up as a whole-file rewrite, your editor wrote CRLF; convert it back rather than committing the flip.
- **No LangChain / LangGraph anywhere in `app/`.** Every chat path is direct HTTP via `requests`: Ollama through `OllamaClient`, and remote providers through the hand-rolled [`OpenAICompatibleClient`](app/llm/openai_compatible_client.py) — no vendor SDK, deliberately (it explains why in its module docstring).
- **No PySide6 / Qt.** The web UI is the only UI.
- **Don't use f-strings** for print/log lines that have no interpolated variables (ruff `F541`).
- **Don't add emojis to source files** unless the user explicitly asks.
- **TTS text processing** (`prepare_tts_text`) applies to the spoken stream only, never the chat transcript.
- **Long-term memory writes go through `MemoryStore.add(...)`** (SQLite is the source of truth); the LanceDB mirror is handled by `MemoryStore` itself.
- **Any worker prompt that feeds a transcript or memory rows to an LLM must age-tag them** via `timephrase.format_transcript()` / `format_memory_block()`, and must include `timephrase.today_anchor()`. Text destined for storage (`memories.content`, `cue_pool.text`, summaries, thread notes) must not contain bare relative deictics — "today", "tonight", "currently" go stale the moment they are written, so paste `timephrase.STORED_TEXT_TIME_RULE` into the prompt. `MemoryStore.add()` enforces a backstop by reclassifying such rows to `past_event`, but the prompt is where it should be prevented.
- **Inline tags Aiko emits** — `[[reaction:…]]`, `[[remember:…]]` / `[[remember:self:…]]`, `[[prosody:…]]`, `[[arc:…]]`, `[[goal:…]]`, `[[predict:…]]`, `[[touch:…]]`, `[[conflict:…]]`, stage-direction earcons, … — are stripped from the spoken/transcript output before TTS / persistence.
- **Narrative time reads go through the `timephrase` seam** — `timephrase.utcnow()` (stored stamps, elapsed math) or `timephrase.now()` (local, relative phrasing), not `datetime.now(...)` directly. That seam is what the DT1 debug clock shifts. Runtime *timing* — `time.monotonic()` / `time.time()` for latency, audio, timeouts, tick budgets — must stay on the real clock.
- **The tool registry is built per-turn** from settings; never instantiate tools inside loops. When adding a tool, put its "what/when/sync-vs-async" description in its `schema()` (not the persona) and add its name to `_TOOL_FAMILY` in `app/core/session/tool_pass_gate.py`.
- **When adding a prompt block**, pick the tier matching its lifetime, append it inside that tier's cluster, and add its name to `_PROMPT_BLOCK_TIERS` (`app/core/session/prompt_assembler.py`) — the T0→T6 prefix-stability ladder protects the OpenAI prompt cache. If the block is a *steer* (it nudges Aiko to bring something up), also add it to `_OFFERS` in `app/core/conversation/stance.py` or decide out loud that it offers no stance — an absent name there is invisible to the K92 arbiter rather than an error.
- **A cue that names a specific subject belongs in the cue pool**, not in a `kv_meta` ring: add a `CuePolicy` to `CUE_POLICIES` (`app/core/proactive/cue_accounting.py`), publish through `CueProducer`, and surface via `take_pool_cue`. That is what gets it real consumption tracking, deficit-driven scheduling instead of a daily cap, and its handling text hoisted out of the persona. See [`docs/cue-pool.md`](docs/cue-pool.md).
- **One-shot debug overrides live in `session.debug_overrides`**, never as a `_force_*` attribute. Register the name in `KNOWN_OVERRIDES` (`app/core/session/debug_overrides.py`), `take(...)` it in the provider, `arm(...)` it from the MCP tool. They are cleared as a set on a session switch, so there is no cleanup list to update.
- **File size**: keep Python files below ~1,500 lines and React/TS components below ~1,000; split via feature mixins (`app/core/<area>/*_mixin.py`) or feature folders (`web/src/components/<feature>/`) before a file passes ~2,500 lines.
- **Running tests — don't sit through the whole suite.** It's ~9,000 tests and ~11 minutes serially. While iterating, run only what your change can reach: `python scripts/affected_tests.py --run` (import-graph selection; add `--explain` to see why each file was picked). For a full run, `python -m pytest -n auto --dist loadfile` finishes in ~2.5 minutes. Frontend: `cd web && npx vitest related --run <changed files>`. Do a full run before calling work done — the selector reads imports, so it can't see a link made by a subprocess or a data file. See [`rules/code-conventions.md`](rules/code-conventions.md) for the details and the `timing` marker.
- **Persona** (`data/persona/aiko_companion.txt`) is user-editable and is always-on prompt text; every user-name reference must stay the literal `{user_name}` placeholder (any other `{…}` token crashes `.format()`). Conditional handling notes — anything shaped "when your context says X, do Y" — do **not** go here: they live in `data/persona/conditional_handling.txt`, are hoisted into T6 only on the turns their prompt block renders, and their headers must match a registered header byte-for-byte (`CuePolicy.handling_section` for a pooled cue, `HANDLING_SECTIONS` in `app/core/session/prompt_support.py` otherwise). A mismatch, an unregistered block name, or a block that never becomes a local in `assemble_with_budget` all fail silently. See [`docs/cue-pool.md`](docs/cue-pool.md#the-persona-hoist).

For the *why* behind any of these — and for anything not listed here — read the
matching reference file below.

## Reference index (`rules/`)

Read on demand; don't load all of it up front:

- [`rules/mcp-server.md`](rules/mcp-server.md) — the embedded **MCP debug server** (`http://localhost:6274/sse`): how to connect, the core tools, and adding your own. **First stop for interacting with / debugging the live app.**
- [`rules/code-conventions.md`](rules/code-conventions.md) — the **subsystem reference catalogue**: architecture conventions plus per-feature design notes (LLM providers & prompt cache, memory tiers & RAG, the K-series personality/affect/relationship features, avatar / Live2D, Tauri shell, tasks / brain orchestration, external MCP clients, …). Grep it for the area you're about to change.
- [`rules/debugging.md`](rules/debugging.md) — the **log stream**: where to look, line shape + canonical fields, the symptom → grep-target table, level cheat sheet, and workflow.

Deeper design docs live under [`docs/`](docs/) (linked from the files above).
The Cursor-specific short ruleset is in [`.cursorrules`](.cursorrules).
