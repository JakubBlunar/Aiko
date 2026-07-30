<!-- Moved out of AGENTS.md to keep the always-loaded context lean. Paths/links are relative to the repo root. -->

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

Beyond this core set, the tool surface is grouped by domain under `app/mcp/server_tools/*.py` (each a `register(mcp, session)` module). Concept-layer observability (L26), in `server_tools/proactive_task_tools.py`:

| Tool | Args | Returns |
|------|------|---------|
| `get_last_concept_trace` | — | JSON: which concepts entered the LAST turn's prompt — the L5 `concept_block` surfaced set (`concept_id`/`label`/`confidence`/`plasticity`/`hedge`) or a `reason`, and the L4 `coactivation_block` mode + quiet cluster. Tagged with `slice_cache_event` + `aggressive`. |
| `get_concept_graph` | — | JSON: the live concept graph (`session.concepts_snapshot()`) — every concept with status/confidence/plasticity/rationale + resolved evidence edges + counts. Richer than `get_concepts_state`. |
| `get_concept_transitions` | `limit: int = 50` | JSON: recent lifecycle transitions (`promoted`/`dormant`/`retired`/`revived`), newest-first, dropping `discovered` births. |

### Virtual clock (DT1)

`server_tools/debug_clock_tools.py`. **Off unless the process was started with `AIKO_DEBUG_CLOCK=1`** — without it every tool returns a message saying so. Lets you exercise time-gated behaviour (decay, promotion age, anniversaries, gap-return, cue cooldowns) in seconds instead of waiting days.

| Tool | Args | Returns |
|------|------|---------|
| `get_clock_status` | — | JSON: gate state, active offset, real vs virtual now, credited synthetic engagement. |
| `advance_clock` | `days: float = 0`, `hours: float = 0` | Shifts wall-clock now (negatives go back; advances accumulate). |
| `set_clock` | `when: str` | Jumps to an absolute ISO-8601 instant, as an offset so time keeps ticking. |
| `advance_engagement` | `days: float` | Credits synthetic *engaged* days. |
| `reset_clock` | — | Back to real time, and restores the engagement total. |

Two things make this subtler than it looks:

- **Two clocks, and the obvious one is usually wrong.** `advance_clock` moves wall time — anniversaries, cooldowns, candidate TTLs, promotion age. Concept (L3) and memory *decay* run on **engaged** time, which wall-clock advances do not touch at all; those need `advance_engagement`.
- **Decay is catch-up-clamped per sweep** (`concept_decay_max_catchup_days`, default 3). Simulating 60 engaged days means interleaving `advance_engagement(3)` with `force_concept_lifecycle`, not one big advance and one sweep.

Rows written while the clock is shifted keep their virtual timestamps after a reset — nothing can rewrite them — so **run against a copy of `data/chat_sessions.db`**. A live offset is echoed into `get_status` and logged at WARNING on every advance, so it should never be silently in effect.

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

Tools live in domain modules under `app/mcp/server_tools/`, each exposing a `register(mcp, session)` that closes over the live `SessionController`. Add to the module that matches your domain (or create one and register it from `app/mcp/server.py`) — `server.py` itself is just the wiring.

```python
# app/mcp/server_tools/my_tools.py
def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def my_debug_tool(some_arg: str) -> str:
        """Description of what this tool does."""
        # Access any internal state via the `session` reference:
        #   session._settings, session._chat_db, session._memory_store,
        #   session._rag_store, session._tool_registry, etc.
        return "result"
```

The app must be restarted for new tools to take effect.

You are encouraged to add any MCP tool you need to debug a problem. Common examples: inspecting agent message history mid-turn, dumping the system prompt, reading TTS queue state, checking embedding search results, or triggering specific `SessionController` methods. After adding a tool, restart the app and it will appear automatically.

### Architecture Notes

- `app/mcp/server.py` — FastMCP server definition + resources; registers each `server_tools/*.py` module.
- `app/mcp/server_tools/*.py` — the tools themselves, grouped by domain. Add new tools here.
- `app/mcp/runner.py` — Runs uvicorn in a daemon thread; stops on app shutdown.
- `app/core/session/session_controller.py` — Starts the MCP server in `__init__`, stops in `shutdown()`. Message listeners notify the web UI of MCP-triggered messages over WebSocket.
- Config: `config/default.json` key `mcp_server` (`enabled`, `port`).

