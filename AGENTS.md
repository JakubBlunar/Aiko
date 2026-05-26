# Agent Instructions

## Project Overview

Aiko is a web-based AI companion built around:

- **Python 3.11+** backend (FastAPI + WebSocket) under `app/`. Entry point: `python -m app.web` (or the `aiko-web` console script).
- **React + Vite + PixiJS** frontend under `web/` (Live2D avatar, chat, voice controls, settings drawer, document upload).
- **Ollama** for chat (via `OllamaClient` directly, not LangChain). The `chat_llm` block can route to any OpenAI-compatible endpoint instead.
- **RealtimeSTT** + **Pocket-TTS** for voice in/out.
- **LanceDB** for vector RAG over memories, recent chat messages, and uploaded documents.
- **SQLite** (`data/chat_sessions.db`) as the source of truth for messages, summaries, and memory metadata.

There is no desktop / Qt / LangChain code. The web UI is the only UI.

## Embedded MCP Server

The app exposes an MCP server on `http://localhost:6274/sse` for development tooling. Start the app first, then connect any MCP client.

### Cursor Setup

Already configured in `.cursor/mcp.json`. The tools appear as native MCP tools — call them directly, no wrapper scripts needed.

### VSCode / Copilot Setup

Add to your MCP settings (`.vscode/mcp.json` or user settings):

```json
{
  "servers": {
    "assistant": {
      "type": "sse",
      "url": "http://localhost:6274/sse"
    }
  }
}
```

### Tools

| Tool | Args | Returns |
|------|------|---------|
| `send_message` | `message: str`, `skip_tts: bool = false` | Assistant response text. The web UI updates live as the message streams. |
| `get_status` | — | JSON: model name, context window, TTS engine, agent tool count, recent metrics. |
| `list_agent_tools` | — | JSON array of `{name, description}` for every agent tool currently registered. |
| `get_last_response_detail` | — | JSON timing breakdown for the last turn (`llm_ms`, `tts_ms`, etc.). |
| `clear_history` | — | Clears the active session in `chat_sessions.db`. |

### Resources

| URI | Content |
|-----|---------|
| `assistant://history` | Recent conversation messages (JSON). |
| `assistant://config` | Current settings snapshot (JSON). |

### Debugging Workflow

1. **Confirm connection**: Call `get_status` — verify `model`, `tool_count`, and `tts.engine`.
2. **Test agent**: Call `send_message` with `skip_tts: true` to avoid audio playback during automated testing.
3. **Check timing**: Call `get_last_response_detail` — `llm_ms` is the model time, `tts_ms` is speech synthesis time.
4. **Read logs**: The app console prints tool registry rebuilds, `TurnRunner` two-pass execution, and proactive nudges at INFO level.

### Adding Custom MCP Tools

If the existing tools are not enough for your debugging scenario, add new ones directly in `app/mcp/server.py`. The server has full access to `SessionController` and all internal state.

```python
@mcp.tool()
def my_debug_tool(some_arg: str) -> str:
    """Description of what this tool does."""
    # Access any internal state via the `session` reference:
    #   session._settings, session._chat_db, session._memory_store,
    #   session._rag_store, session._tool_registry, etc.
    # The app must be restarted for new tools to take effect.
    return "result"
```

You are encouraged to add any MCP tool you need to debug a problem. Common examples: inspecting agent message history mid-turn, dumping the system prompt, reading TTS queue state, checking embedding search results, or triggering specific `SessionController` methods. After adding a tool, restart the app and it will appear automatically.

### Architecture Notes

- `app/mcp/server.py` — FastMCP server definition with all tools/resources. Add new tools here.
- `app/mcp/runner.py` — Runs uvicorn in a daemon thread; stops on app shutdown.
- `app/core/session_controller.py` — Starts the MCP server in `__init__`, stops in `shutdown()`. Message listeners notify the web UI of MCP-triggered messages over WebSocket.
- Config: `config/default.json` key `mcp_server` (`enabled`, `port`).

## Code Conventions

- Python 3.11+, dataclasses + slots for settings, FastAPI for the HTTP/WebSocket layer.
- LLM calls go through `app/llm/ollama_client.py` (`chat`, `chat_stream`, `chat_with_tools`). Tool dispatch happens in `app/core/turn_runner.py` via a pre-stream `chat_with_tools` pass; the streaming reply runs immediately after.
- The agent's available tools are built once per turn from `config.tools` by `SessionController.rebuild_tool_registry()` — see `app/llm/tools/builtins.py` for the live set (`get_time`, `recall`, `web_search`).
- TTS text processing (`prepare_tts_text` in `app/core/session_text_utils.py`) applies only to the spoken stream, not the chat transcript.
- SQLite (`data/chat_sessions.db`) is the source of truth; LanceDB (`data/lancedb/`) mirrors `memories`, asynchronously indexes `messages`, and holds chunked uploaded `documents`.
- Inline tags Aiko emits: `[[reaction:...]]` (mood, drives Live2D expression and TTS prosody) and `[[remember:...]]` / `[[remember:self:...]]` (writes a long-term memory). Both are stripped from the spoken/chat output.
- Persona file at [`data/persona/aiko_companion.txt`](data/persona/aiko_companion.txt) is user-editable; the "Reading Jacob" section instructs Aiko to mirror the user's register from the live `vocal_tone` and `user_state` prompt cues. The same nudge is mirrored in `_SPEECH_GRAMMAR_ADDENDUM` ([`app/core/prompt_assembler.py`](app/core/prompt_assembler.py)) so a deleted/rewritten persona file doesn't lose the behaviour.
- Live2D rig (Alexia) parameter quirks — outfit / hood / synonyms — are documented in [`docs/alexia-model-notes.md`](docs/alexia-model-notes.md). **Read it before changing `app/core/avatar_profile.py` or the live2d engine.**
- Live2D rendering lives in [`web/src/live2d/`](web/src/live2d/) — `AvatarEngine` plus eight single-purpose channels (`MotionChannel`, `OutfitChannel`, `OverlayChannel`, `LipsyncChannel`, `ExpressionChannel`, `GestureChannel`, `GazeChannel`, `AmbientBodyChannel`). Every per-frame parameter write is in a channel; `Live2DAvatar.tsx` only does Pixi setup + dispose. Channels are pure TS and tested with Vitest in Node — `cd web && npm test` runs the whole channel suite in <1s. **Read [`web/src/live2d/README.md`](web/src/live2d/README.md) before adding behaviour, and never re-introduce `useEffect`-based parameter writes in the component.**
- The `avatar.expressiveness` knob (Settings drawer "Body language intensity" slider, range `0.0-1.5`, default `1.0`) scales every continuous body-language amplitude — `AmbientBodyChannel` breath / valence-tilt / lean-in / slump / sass, plus `ExpressionChannel`'s arousal-scaled expression-amplitude override. Continuous overrides land in the new `tickPreModel` hook (the engine's `beforeModelUpdate` fan-out), which is the **last writable point** in pixi-live2d-display's update order — any new continuous-override channel that needs to win against `expressionManager` (Add blend), `focusController`, or the `breath` driver must hook `tickPreModel` and not `tickTier3`. Capability-gate every override (`has_breath`, `has_body_angle_y`, `expression_params[name]`) so minimal rigs without the parameter pay nothing.
- Frontend state lives in `web/src/store.ts` (Zustand). The WebSocket hook (`web/src/hooks/useAssistantSocket.ts`) is the single point that mutates store state from server events.
- Tauri 2 desktop shell at [`web/src-tauri/`](web/src-tauri/) wraps the same React bundle in two windows: the full chat UI at `/`, and a transparent + frameless persona window at `#/persona` (avatar + drag handle + mic toggle + one-line composer). The Python backend stays external (`python -m app.web`); the shell connects as a client. **All new fetch / WS / asset URLs must go through `backendBase()` in [`web/src/desktop/runtime.ts`](web/src/desktop/runtime.ts)** — root-relative URLs reach the FastAPI proxy only in the browser, not in a Tauri webview. Persona-window geometry lives at `desktop.persona_window` in `config/default.json`, persists through `SessionController.update_desktop_settings`, and round-trips via `PATCH /api/desktop/persona-window` + a `desktop_settings_changed` WS broadcast that resizes the OS frame on the fly. See [`docs/tauri-shell.md`](docs/tauri-shell.md) for the dev loop and architecture notes.
- Mouse input for the gaze channel goes through `AvatarEngine`'s `deps.mouseSource` (the `MouseSource` interface in [`web/src/live2d/AvatarEngine.ts`](web/src/live2d/AvatarEngine.ts)). [`Live2DAvatar.tsx`](web/src/components/Live2DAvatar.tsx) picks the implementation: `WindowMouseSource` (DOM `pointermove`) in the browser, `GlobalMouseSource` ([`web/src/live2d/GlobalMouseSource.ts`](web/src/live2d/GlobalMouseSource.ts), polls Tauri's `cursorPosition()` per RAF) inside the desktop shell so Aiko's eyes track the OS cursor across monitors. **Never read mouse state directly in a channel** — extend `MouseSource` / `MouseSnapshot` if you need a new field, otherwise the desktop and browser builds drift apart. Cross-monitor cursors land as negative or out-of-viewport offsets; `GazeChannel`'s existing clamps handle the saturation, no special-casing required.
- The Settings drawer has a dedicated **Memory** tab ([`web/src/components/SettingsDrawer.tsx`](web/src/components/SettingsDrawer.tsx)) for inspecting + editing long-term memories. It paginates server-side via `GET /api/memories?limit=&offset=&kind=` (page size 50, response includes `total` + `cap`), with edit-in-place, manual create (`POST /api/memories`, dedupes via the existing cosine-collapse path and toasts "merged into memory #N"), salience editing, kind filter / sort selectors, and a **pin** toggle (`POST /api/memories/{id}/pin`). Pinned rows are **immune to `MemoryStore.decay()` and never selected as `prune()` victims**, and `RagRetriever` adds a `+0.05` score bonus for pinned hits. Pinning lives in SQLite (`memories.pinned`, schema v5) only — the LanceDB mirror is intentionally not aware of it, to avoid a destructive vector-store rebuild on existing user databases. Frontend Zustand state is `memoryView: { items, total, cap, page, pageSize, kindFilter, order }` plus `applyMemoryAdded` / `applyMemoryUpdated` / `applyMemoryDeleted` reducers; `memory_added` only prepends to the visible page when on page 0 with `order=recent` and a matching kind filter (otherwise just bumps `total`). New WS event: `memory_updated`. The default `memory.max_memories` cap was bumped from 500 to 5000 — search remains a sub-millisecond NumPy pass at that size and the LanceDB-backed retrieval is sub-linear, so the headroom is essentially free.

## Debugging via logs

Aiko writes a single, level-disciplined log stream that lands in three places at once: stderr (live console), `data/app.log` (rotating, 5 MB × 5 files), and an in-process ring buffer (last 1000 lines) exposed over MCP. Every line is shaped the same way — memorise the shape and the canonical fields and you can answer most "what just happened?" questions with one or two MCP calls.

### a. Where to look (priority order)

1. **`tail_logs(n=200, level="DEBUG", module_contains=…)`** — first stop. Instant, scoped, level-filterable. Use `module_contains="prompt"` to focus on `app.core.prompt_assembler`, `module_contains="ollama"` for the LLM client, etc.
2. **`read_log_file(lines=2000, grep="turn=abc12345")`** — when the issue is older than the ring (~1000 lines back) or you want to follow one turn end-to-end. Hits the rotating `data/app.log` (and its `.1`…`.5` siblings).
3. **`data/app.log` directly via Read tool** — for offline / cross-session forensic work, including reading rolled siblings.
4. **`terminals/*.txt`** — last resort for noise outside the `app.*` loggers (uvicorn, fastmcp, library warnings).

`get_log_config` returns the current effective configuration (level, file path, module overrides, ring usage).

### b. Standard line shape

Every record is formatted as:

```
[YYYY-MM-DD HH:MM:SS,mmm] LEVEL [logger.name turn=abc12345] message text key1=val1 key2=val2
```

`turn=…` is an 8-char hex correlation id allocated by `TurnRunner.run()` and propagated via a `contextvars.ContextVar` ([`app/core/log_context.py`](app/core/log_context.py)). When no turn is active you'll see `turn=-`. The canonical structured fields (memorise these for precise greps):

- **Turn lifecycle**: `model=`, `ctx_pct=`, `prompt=`, `completion=`, `first_token_ms=`, `total_ms=`, `eval_ms=`, `tools=`, `compactions=`, `mood=`, `aborted=`, `chars=`, `filler=`.
- **Prompt assembly**: `ctx=`, `budget=`, `est_tokens=`, `sys=`, `hist=`, `user=`, `rag_tokens=`, `history_msgs_in=`, `history_msgs_out=`, `inner_blocks=`, `summary_active=`, `compaction=`, `aggressive=`.
- **Scheduler**: `jobs_run=`, `elapsed_ms=`, `queue_after=`, `idle=`, `names=`, `submitted=`, `ran=`, `cancelled=`, `windows_opened=`.
- **Ollama**: `msgs=`, `tools=`, `stream=`, `prompt_tokens=`, `completion_tokens=`, `tool_calls=`, `format_json=`, `stopped=`.
- **STT**: `chars=`, `duration_ms=`, `model=`, `language=`, `init_ms=`, `device=`, `sample_rate=`, `max_s=`, `silence_s=`.
- **TTS**: `voice=`, `provider=`, `init_ms=`, `chunk_chars=`, `speed=`, `generate_ms=`, `played_ms=`, `queue_depth=`, `drained_chunks=`.
- **Proactive**: `source=` (`prepared`|`llm`).

### c. Symptom → grep target

| Symptom | First check |
|---|---|
| **Aiko silent / no reply** | `tail_logs(level="DEBUG", module_contains="turn_runner")` → find the failed `turn=…`, look at `first_token_ms=` and surrounding ERROR lines. Then `read_log_file(grep="turn=<id>")` to grab everything correlated. |
| **Voice mode never picks up** | `set_log_level("app.stt.realtime_stt_service", "DEBUG")`, then `tail_logs(module_contains="stt")`. Look for missing `STT engine ready:` INFO or repeated capture errors. |
| **TTS stutters / drops** | `tail_logs(module_contains="tts")`. Cross-reference `TTS enqueue:` / `TTS play done:` DEBUG with `tts state:` transitions in `app.tts_queue`. |
| **Wrong context retrieved** | `set_log_level("app.core.prompt_assembler", "DEBUG")`, replay the turn, then read the `prompt built:` line for `rag_tokens` / `inner_blocks` / `history_msgs_in/out`. |
| **Memory not recorded** | `tail_logs(module_contains="memory")`, look for `memory:` INFO lines from `turn_runner`, confirm the extractor ran. |
| **Proactive nudge fires wrong / never** | `tail_logs(module_contains="proactive")`. Check `source=prepared|llm` and the skip reasons (`cooldown`, `chat in progress`, `no history`). |
| **Slow first token** | Grep `first_token_ms=` over recent turns. If elevated, `set_log_level("app.llm.ollama_client", "DEBUG")` and watch `ollama chat_stream done:` for the per-call `elapsed_ms`. |
| **Compaction not triggering** | Grep `context overflow projected` and `compactions=`; if the threshold is wrong, look at `est_tokens=` / `budget=` from `prompt_assembler`. |
| **Scheduler jobs queued but not running** | `set_log_level("app.scheduler", "DEBUG")`, watch `scheduler drain:` for `jobs_run=` and `queue_after=`. |
| **Crash / unhandled exception** | Read `data/crashlog.txt` (separate from `app.log`, only fatal traces + faulthandler dumps). |

### d. Level cheat sheet

The level dial is a contract — every new log call must respect it.

- **ERROR** — only real failures (turn lost, engine crashed, non-2xx). Always shown.
- **WARNING** — degraded paths and retries (model warmup failed, voice switch failed, scheduler job raised but loop continued).
- **INFO** (default) — one structured line per turn (`turn done:`), lifecycle moments (boot, `ollama connected:`, `STT engine ready:`, `TTS engine ready:`, `TTS voice switched:`, `STT capture done:`, scheduler init/shutdown, compaction triggered, proactive spoken).
- **DEBUG** — the tweaking firehose (per-Ollama-call timings, scheduler per-drain summaries, prompt-assembler retrieval breakdown, STT capture lifecycle, TTS enqueue/play, RAG cache hits, full transcript previews).

Practical presets:

| Goal | How |
|---|---|
| Default | `level=INFO` in config. Turn-by-turn trace + lifecycle. |
| Investigate one module | Keep global `level=INFO`; bump just the relevant module via `set_log_level("app.core.prompt_assembler", "DEBUG")` (runtime) or `logging.module_levels` in `config/user.json` (persistent). |
| Production / quiet | `level=WARNING` or `level=ERROR` — a healthy session emits zero log lines. |
| Full firehose | `level=DEBUG` — only when actively tweaking; expect ~hundreds of lines per turn. |

### e. Practical workflow

1. **Before reproducing**: `set_log_level("app.core.<suspect>", "DEBUG")` on the module you suspect.
2. **Trigger the issue**: `send_message(skip_tts=true)` (or use the UI / voice).
3. **Read back**: `tail_logs(module_contains="<suspect>")` for the most recent ring entries, then `read_log_file(grep="turn=<id>")` to widen the lens to the whole turn.
4. **If you need a new field**: open the relevant module, add a structured log line at the right level (use `key=value` so a future grep stays cheap), document the new field in §b above, and restart the app.
