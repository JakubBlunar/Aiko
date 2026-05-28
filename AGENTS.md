# Agent Instructions

## Project Overview

Aiko is a web-based AI companion built around:

- **Python 3.11+** backend (FastAPI + WebSocket) under `app/`. Entry point: `python -m app.web` (or the `aiko-web` console script).
- **React + Vite + PixiJS** frontend under `web/` (Live2D avatar, chat, voice controls, settings drawer, document upload).
- **Ollama** for chat (via `OllamaClient` directly, not LangChain). The `chat_llm` block can route to any OpenAI-compatible endpoint instead.
- **RealtimeSTT** + **Pocket-TTS** for voice in/out, with **client-owned audio I/O**: the browser / Tauri shell captures the microphone (48 kHz Int16 mono, browser DSP) and plays back TTS, streaming raw PCM frames over the existing WebSocket. See [`docs/voice-mode.md`](docs/voice-mode.md) for the binary frame protocol and the voice-ownership lock used when multiple windows are open.
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
- Persona file at [`data/persona/aiko_companion.txt`](data/persona/aiko_companion.txt) is user-editable; the "Reading {user_name}" section instructs Aiko to mirror the user's register from the live `vocal_tone` and `user_state` prompt cues. The same nudge is mirrored in `_SPEECH_GRAMMAR_ADDENDUM` ([`app/core/prompt_assembler.py`](app/core/prompt_assembler.py)) so a deleted/rewritten persona file doesn't lose the behaviour. **Every "Jacob" in the persona has been templated** as the literal placeholder `{user_name}` (Phase 4d). `PromptAssembler._load_persona()` calls `.format(user_name=resolve_user_display_name(...))` once per load; new persona text must keep all braces as `{user_name}` (any other `{...}` token crashes `.format()` and falls back to the raw text via the try/except guard). The legacy backup file `aiko_companion_backup.txt` was deleted — never restore it.
- Live2D rig (Alexia) parameter quirks — outfit / hood / synonyms — are documented in [`docs/alexia-model-notes.md`](docs/alexia-model-notes.md). **Read it before changing `app/core/avatar_profile.py` or the live2d engine.**
- Live2D rendering lives in [`web/src/live2d/`](web/src/live2d/) — `AvatarEngine` plus eight single-purpose channels (`MotionChannel`, `OutfitChannel`, `OverlayChannel`, `LipsyncChannel`, `ExpressionChannel`, `GestureChannel`, `GazeChannel`, `AmbientBodyChannel`). Every per-frame parameter write is in a channel; `Live2DAvatar.tsx` only does Pixi setup + dispose. Channels are pure TS and tested with Vitest in Node — `cd web && npm test` runs the whole channel suite in <1s. **Read [`web/src/live2d/README.md`](web/src/live2d/README.md) before adding behaviour, and never re-introduce `useEffect`-based parameter writes in the component.**
- The `avatar.expressiveness` knob (Settings drawer "Body language intensity" slider, range `0.0-1.5`, default `1.0`) scales every continuous body-language amplitude — `AmbientBodyChannel` breath / valence-tilt / lean-in / slump / sass, plus `ExpressionChannel`'s arousal-scaled expression-amplitude override. Continuous overrides land in the new `tickPreModel` hook (the engine's `beforeModelUpdate` fan-out), which is the **last writable point** in pixi-live2d-display's update order — any new continuous-override channel that needs to win against `expressionManager` (Add blend), `focusController`, or the `breath` driver must hook `tickPreModel` and not `tickTier3`. Capability-gate every override (`has_breath`, `has_body_angle_y`, `expression_params[name]`) so minimal rigs without the parameter pay nothing.
- Frontend state lives in `web/src/store.ts` (Zustand). The WebSocket hook (`web/src/hooks/useAssistantSocket.ts`) is the single point that mutates store state from server events.
- Tauri 2 desktop shell at [`web/src-tauri/`](web/src-tauri/) wraps the same React bundle in two windows: the full chat UI at `/`, and a transparent + frameless persona window at `#/persona` (avatar + drag handle + mic toggle + one-line composer). The Python backend stays external (`python -m app.web`); the shell connects as a client. **All new fetch / WS / asset URLs must go through `backendBase()` in [`web/src/desktop/runtime.ts`](web/src/desktop/runtime.ts)** — root-relative URLs reach the FastAPI proxy only in the browser, not in a Tauri webview. Persona-window geometry lives at `desktop.persona_window` in `config/default.json`, persists through `SessionController.update_desktop_settings`, and round-trips via `PATCH /api/desktop/persona-window` + a `desktop_settings_changed` WS broadcast that resizes the OS frame on the fly. See [`docs/tauri-shell.md`](docs/tauri-shell.md) for the dev loop and architecture notes.
- Mouse input for the gaze channel goes through `AvatarEngine`'s `deps.mouseSource` (the `MouseSource` interface in [`web/src/live2d/AvatarEngine.ts`](web/src/live2d/AvatarEngine.ts)). [`Live2DAvatar.tsx`](web/src/components/Live2DAvatar.tsx) picks the implementation: `WindowMouseSource` (DOM `pointermove`) in the browser, `GlobalMouseSource` ([`web/src/live2d/GlobalMouseSource.ts`](web/src/live2d/GlobalMouseSource.ts), polls Tauri's `cursorPosition()` per RAF) inside the desktop shell so Aiko's eyes track the OS cursor across monitors. **Never read mouse state directly in a channel** — extend `MouseSource` / `MouseSnapshot` if you need a new field, otherwise the desktop and browser builds drift apart. Cross-monitor cursors land as negative or out-of-viewport offsets; `GazeChannel`'s existing clamps handle the saturation, no special-casing required.
- The Settings drawer has a dedicated **Memory** tab ([`web/src/components/SettingsDrawer.tsx`](web/src/components/SettingsDrawer.tsx)) for inspecting + editing long-term memories. It paginates server-side via `GET /api/memories?limit=&offset=&kind=` (page size 50, response includes `total` + `cap`), with edit-in-place, manual create (`POST /api/memories`, dedupes via the existing cosine-collapse path and toasts "merged into memory #N"), salience editing, kind filter / sort selectors, and a **pin** toggle (`POST /api/memories/{id}/pin`). Pinned rows are **immune to `MemoryStore.decay()` and never selected as `prune()` victims**, and `RagRetriever` adds a `+0.05` score bonus for pinned hits. Pinning lives in SQLite (`memories.pinned`, schema v5) only — the LanceDB mirror is intentionally not aware of it, to avoid a destructive vector-store rebuild on existing user databases. Frontend Zustand state is `memoryView: { items, total, cap, page, pageSize, kindFilter, order }` plus `applyMemoryAdded` / `applyMemoryUpdated` / `applyMemoryDeleted` reducers; `memory_added` only prepends to the visible page when on page 0 with `order=recent` and a matching kind filter (otherwise just bumps `total`). New WS event: `memory_updated`. The default `memory.max_memories` cap was bumped from 500 to 5000 — search remains a sub-millisecond NumPy pass at that size and the LanceDB-backed retrieval is sub-linear, so the headroom is essentially free.
- **Aiko's room** — the [`WorldStore`](app/core/world_store.py) is a small persistent virtual space owned by `SessionController` (one row per location / item, plus a singleton `world_state` row holding posture / activity / current location). Schema v6 (`chat_database.py`) adds the three `world_*` tables; on first boot a "rich default" room is seeded (desk, bed, bookshelf, kitchenette, window seat, beanbag, mirror corner) with matching items (cookies, tea pot, plush blanket, photo of Jacob, monitors, retro keyboard, …). The room reaches the LLM via three seams: (1) the `world` inner-life prompt provider in `prompt_assembler.py` renders a 3-5 line ambient block ending in an explicit "acknowledge your surroundings only when it feels natural — never force a room mention" tonal nudge; (2) five new agent tools (`look_around` / `move_to` / `change_posture` / `inspect_item` / `consume_item` in [`app/llm/tools/world.py`](app/llm/tools/world.py)) gated by `tools.world` in config; (3) REST under `/api/world` (`GET`, `PATCH /state`, location + item CRUD, `POST /items/{id}/consume`, `POST /seed?force=true`) plus a single `world_updated` WS event carrying typed surgical patches (`{state}` | `{location}` | `{item}` | `{deleted_*_id}` | `{snapshot}`). The Zustand reducer `applyWorldPatch` merges them in place so the new **World** tab in `SettingsDrawer.tsx` stays live. **"Give Aiko a cookie" is intentionally silent** — items appear in her room with `given_by="user"` and Aiko notices them on her next turn through the prompt block, no proactive message is fired. Consumable items (food) decrement on `consume_item` and the row is deleted at quantity zero; non-consumables clamp at zero so the lamp stays in the room.
- **Configurable user display name + macOS distribution** — As of the macOS-publishable build, every "Jacob" hardcode has been replaced by a configurable `assistant.user_display_name` ([`app/core/settings.py`](app/core/settings.py)). `resolve_user_display_name(settings)` returns the trimmed value or the fallback `"friend"`; `is_onboarding_needed(settings)` flips true when the field is blank. The **only** read site for runtime code is the `SessionController.user_display_name` property — everything else (prompt block renderers, worker LLM system prompts via `_build_*_prompt(name)` factories, transcript speaker labels via `app.core.session_text_utils.speaker_label`, the persona templating, the world-store seed, and the frontend `SettingsDrawer` gift presets) threads the name through a `user_display_name_provider` lambda or an explicit keyword. New REST surface: `GET/PUT /api/settings/identity`. New WS hello field: `identity: { user_display_name, needs_onboarding }`. New WS broadcast: `identity_changed`. Frontend: a non-dismissible `FirstRunOnboarding` modal that gates the rest of the UI when `needs_onboarding`, plus a "Change name" affordance in `SettingsDrawer` → Chat tab. The Live2D bundle now lives at `data/personas/active/Alexia/` (legacy `live-2d-models/Alexia/` still honoured if overridden). The macOS hybrid installer ships as `Aiko.app` + `Aiko Setup.command` (`scripts/setup-macos.sh`); the Tauri shell auto-spawns the Python sidecar through the new `ensure_backend_running` Rust command before the React app dials the WS. The `npm run tauri:build:macos` script runs `scripts/check-clean-build.py` first to fail the build on any developer artefact (dev DB present, "Jacob" left in the persona, missing avatar bundle, non-blank default `user_display_name`). End-user walkthrough: [`docs/install-macos.md`](docs/install-macos.md). Tests: `tests/test_user_display_name.py`, `tests/test_persona_templating.py`, `tests/test_world_seed_renaming.py`.
- **Memory tiers + revival drift + IdleWorker framework (schema v8)** — Long-term memory is no longer a flat pool. Every row in [`memories`](app/core/memory_store.py) now carries a `tier` (`scratchpad` / `long_term` / `archive`) and a `revival_score` in `[0, 1]`. Auto-extracted observations (`MemoryExtractor`, `ReflectionWorker`, `DreamWorker`) land in `scratchpad`; verified anchors (`PromiseExtractor`, `CatchphraseMiner`, `SharedMoments`, `RelationshipPulse`, `[[remember:...]]` tags via `TurnRunner`, manual REST/UI adds, milestone memories) write directly to `long_term`; pinned rows are always coerced to `long_term`. `MemoryStore.decay` is now **wall-clock-driven** (proportional to elapsed time since `memory.last_decay_run_at` in the new `kv_meta` key-value table, clamped by `decay_max_catchup_days`) with per-tier rates and a revival rebate (`salience += revival_coefficient * elapsed * revival_score`). `prune()` enforces per-tier caps independently. **Revival detection** (`SessionController._mark_revived_memories`) runs post-turn: it reads the surfaced-IDs snapshot off `RagRetriever.last_surfaced_memory_ids` and bumps `revival_score` on memories whose content shares ≥ `revival_min_word_overlap` (default 3) content words with Aiko's reply. A new **`IdleWorkerScheduler`** (`app/core/idle_worker_scheduler.py`) runs background workers during quiet windows (gated by `SessionController._is_user_idle`: no Live mode, no turn in flight, no recent user activity); it caps one worker per tick to avoid stacking and persists `last_run_at` per worker to `kv_meta`. First two workers: [`MemoryPromotionWorker`](app/core/memory_promotion_worker.py) (promotes scratchpad rows on age + use_count OR revival ≥ threshold, demotes idle long_term rows after 180 days, deletes dead scratchpad, coerces pinned, then re-runs prune) and [`MemoryDecayWorker`](app/core/memory_decay_worker.py) (thin wrapper around `MemoryStore.decay`). Both default to hourly cadence (configurable down to seconds for testing). **New REST endpoints**: `GET /api/memories/counts`, `tier=` query param on `GET /api/memories`, `tier` on PATCH/POST. **Frontend**: Memory tab grew a tier pill, tier filter, per-tier counts header, and a revival % readout when `revival_score > 0.05`. **All new background jobs should register with the `IdleWorkerScheduler` rather than spinning up their own threads** — F1 (fact-checker), G2 (schedule learner), G3 (curiosity worker), and any other "run me when nobody's talking" task fits the same protocol. See [`docs/memory-tiers.md`](docs/memory-tiers.md) for the full design + producer rules.

- **Shared moments + relationship axes (schema v7)** — `memories.metadata` is now a nullable JSON column carrying structured `(when, what, vibe, source_message_ids, last_anniversaried_at)` for the new `shared_moment` memory kind. Detection runs in three tracks: inline `[[moment:vibe:summary]]` tags emitted by Aiko (cheapest), a speaking-window `MomentDetector` LLM job gated on reaction/milestone/promise/gift signals + per-turn cadence + wall-clock cooldown, and a manual "Mark as moment" UI action that auto-pins. Alongside the new memory kind, a `relationship_axes` table stores four floats in `[-1, 1]` (closeness/humor/trust/comfort) with ~30-day exponential decay and ±0.08-per-turn drift caps, surfaced as a terse prompt block only when at least one axis crosses ±0.5. Anniversary surfacing (`pick_anniversary` in `app/core/anniversary.py`) matches 1mo/3mo/6mo/1yr/yearly windows ±1 day with a 6h per-moment rate limit, renders an inner-life block, and nudges `RagRetriever` with a +0.05 score bonus for the matched row. UI: new "Together" tab in `SettingsDrawer` with header / milestones / axes bars / anniversary card / paginated timeline, plus a per-bubble "mark as moment (vibe ▾)" hover action in `ChatView.tsx`. WebSocket: `shared_moment_updated` (CRUD) and `relationship_axes_updated` (debounced) broadcasts via `add_shared_moment_listener` / `add_relationship_axes_listener` on `SessionController`. REST: `GET /api/together`, `/api/shared-moments` CRUD, and `POST /api/chat/messages/{id}/mark-moment`. Disabling switches under `AgentSettings`: `shared_moments_enabled`, `shared_moments_llm_enabled`, `shared_moments_min_turn_gap`, `shared_moments_cooldown_seconds`, `anniversary_surfacing_enabled`, `relationship_axes_enabled`. See `docs/shared-moments-and-relationship.md` for the full design + privacy posture.
- **Typed-mode proactive + activity awareness** — `ProactiveDirector` has two parallel paths now: voice mode (existing, fires from `LiveSession._maybe_proactive` on a 45 s threshold) and **typed mode** (`notify_typed_silence` + `_run_typed`, fires from a `threading.Timer` armed at the end of every typed turn in `SessionController.chat_once_streaming`). The typed path uses an independent cooldown clock (`_last_typed_run_monotonic`), a separate prompt hint (`_PROACTIVE_HINT_TYPED` — explicitly avoids "Jacob has been quiet for a moment" so a 4 min silence never reads as abandonment-anxiety), and skips TTS entirely (text-only by design). It's gated by a 4-input eligibility predicate (`_is_typed_proactive_eligible`): settings toggle ON, voice mode OFF, no turn in flight, AND `_user_present` (the AND-fold of browser visibility + Tauri window focus, sent over the new `presence` WS command from `usePresenceReporter`). Voice mode continues to ignore `_user_present` because a mic-wearing user may be away from the screen but still very much in the conversation. **Activity awareness** is a separate desktop-only opt-in feature riding the same WS plumbing: when `activity.awareness_enabled` is on AND we're in the Tauri shell, `useActivityReporter` polls the Rust `get_active_app` command every 5 s and forwards the foreground app *name* (never window titles, never URLs — that's enforced at the Rust boundary) over a `user_activity` WS frame. `SessionController.set_user_active_app` server-side-gates on the same toggle so a buggy client can't leak data. The captured app surfaces through the new `activity` inner-life provider as "Jacob is currently working in \<App\>" with a tonal nudge to mention only when natural. Off by default; live "Currently sees: \<App\>" readout in Settings → Activity awareness for transparency. Privacy posture details in [`docs/presence-and-activity.md`](docs/presence-and-activity.md). Settings knobs: `agent.proactive_typed_enabled` / `proactive_silence_seconds_typed` (4 min) / `proactive_cooldown_seconds_typed` (10 min) / `activity_awareness_enabled` (off by default).

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
| **Wrong expression / cry-cascade / lip-sync glitch** | Enable **Settings drawer → Chat → Diagnostics → Debug logging**, reproduce, then grep `data/app.log` for `[ui]` lines. The browser pushes WS events + avatar-channel decisions (`channel.expression applyReaction reaction=… expression=…`) into the same file as the backend timestamps so cause + effect sit side-by-side. See [B6 in personality-backlog](docs/personality-backlog.md#b6-ui-debug-logging-bridge). |

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
