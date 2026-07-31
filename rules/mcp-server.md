<!-- Moved out of AGENTS.md to keep the always-loaded context lean. Paths/links are relative to the repo root. -->

## Embedded MCP Server

The app exposes an MCP server on `http://localhost:6274/sse` for development tooling. Start the app first, then connect any MCP client.

**Against a container**, the default loopback bind is unreachable from the host, so add the debug overlay: `docker compose -f docker-compose-slim.yaml -f docker-compose.debug.yaml up -d --build`. Same URL afterwards. Only one Aiko can own 6274 at a time, so stop the local app first. See [`docs/docker.md`](../docs/docker.md).

**From a shell**, [`scripts/mcp_call.py`](../scripts/mcp_call.py) calls any tool without an editor client in the loop — useful for the container (an editor's client caches the connection failure from whenever the port was last dead, and can't be retargeted mid-session):

```bash
python scripts/mcp_call.py --list
python scripts/mcp_call.py get_status
python scripts/mcp_call.py send_message --arg message="hey, you awake?"
python scripts/mcp_call.py force_day_color --json '{"color": "amber"}'
```

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
| `list_debug_overrides` | `armed_only: bool = false` | JSON: every one-shot override, whether it is armed, its payload, and what it does. |
| `clear_debug_overrides` | — | Disarms everything pending. Same call a session switch makes. |

#### One-shot debug overrides

Every `force_*` tool arms an entry in
[`session.debug_overrides`](../app/core/session/debug_overrides.py) that the
next matching provider consumes and drops. Three things follow:

- **Ask what is pending** with `list_debug_overrides`. An override that never
  fired used to be invisible until it went off in some later turn.
- **They all clear together** on a session switch or a memory wipe. There is no
  per-flag cleanup list to keep in step, which is what used to leak overrides
  between conversations.
- **Names are registered.** Arming one that isn't in `KNOWN_OVERRIDES` raises,
  so a typo fails loudly rather than writing an attribute no provider reads.

Adding one: register the name and a one-line description in `KNOWN_OVERRIDES`,
`take(...)` it in the provider, and `arm(...)` it from your tool.

Beyond this core set, the tool surface is grouped by domain under `app/mcp/server_tools/*.py` (each a `register(mcp, session)` module). Concept-layer observability (L26), in `server_tools/proactive_task_tools.py`:

| Tool | Args | Returns |
|------|------|---------|
| `get_last_concept_trace` | — | JSON: which concepts entered the LAST turn's prompt — the L5 `concept_block` surfaced set (`concept_id`/`label`/`confidence`/`plasticity`/`hedge`) or a `reason`, and the L4 `coactivation_block` mode + quiet cluster. Tagged with `slice_cache_event` + `aggressive`. |
| `get_concept_graph` | — | JSON: the live concept graph (`session.concepts_snapshot()`) — every concept with status/confidence/plasticity/rationale + resolved evidence edges + counts. Richer than `get_concepts_state`. |
| `get_concept_transitions` | `limit: int = 50` | JSON: recent lifecycle transitions (`promoted`/`dormant`/`retired`/`revived`), newest-first, dropping `discovered` births. |

### Performance measurement (P29 / P31a)

Two "where is it actually going" tools. Both are read-only snapshots of the live process, and both exist because the perf backlog kept accumulating items that could only be guessed at.

| Tool | Args | Returns |
|------|------|---------|
| `get_memory_breakdown` | — | JSON: process RSS + every child process with its cmdline (`app/mcp/server_tools/memory_breakdown_tools.py`), then per-subsystem attribution — STT loaded/model/device, TTS engine class + whether the PyTorch runtime was avoided, memory-mirror rows × embedding bytes, LanceDB on-disk size, embedder LRU. |
| `get_prompt_block_costs` | `top: int = 25` | JSON: per-block character + estimated-token cost of the last assembled prompt, joined against the `_PROMPT_BLOCK_TIERS` ladder and ranked by tokens × the tier's cache-miss probability (`app/mcp/server_tools/prompt_cost_tools.py`). |

Reading them:

- **`get_memory_breakdown` counts the process tree, not the model.** The LLM's context window lives in Ollama, which is a different process entirely and does not appear here. `process.tree_rss_mb` is the number Task Manager shows. A large tree with `stt.weights_loaded` true is the usual answer — on Windows the Whisper weights sit in RealtimeSTT's transcription child, so check `process.children` rather than the parent's RSS.
- **`tts.torch_runtime_avoided`** distinguishes "no model loaded" from "the heavy import never happened". Only booting with `tts.enabled=false` gets you the second one; toggling TTS off at runtime frees the voice weights but cannot un-import PyTorch.
- **`get_prompt_block_costs` ranks by tier, not by size.** A large block in T0 is paid once per prompt-cache lifetime; a small block in T6 is paid on every turn. The weighting is what makes the list actionable — and a block that renders every turn at 0 chars is a content-gating candidate, so those are reported rather than omitted.

### Outcome measurement (L37 / G4)

Both answer "did any of this inner life *do* anything", which was unanswerable before them — surfacing left no trace but a habituation timestamp, and a worker cue left none at all.

| Tool | Args | Returns |
|------|------|---------|
| `get_surfacing_outcomes` | `window_days: int = 30`, `min_settled: int = 1`, `top: int = 20` | JSON: per-item leaderboard of surfaced concepts / memories / cues with engaged + echo counts *and* denominators, a per-lane rollup, echo-kind split, and the semantic-floor replay (`app/mcp/server_tools/surfacing_outcome_tools.py`). |
| `get_cue_outcomes` | `window_days: int = 30`, `cue: str = ""` | JSON: per-cue armed-to-surfaced ratio, decline reasons, and the registered cues never armed at all (`app/mcp/server_tools/cue_outcome_tools.py`). |

Reading them:

- **Read the denominators, not the rates.** Every rate is over settled rows only, and a 1-for-1 item shows the same 100% as a 40-of-50 one. `rows_unsettled` is *expected* to hold about one turn per session — the engagement label comes from the user's next message, so the last turn of a session never settles. A number climbing in step with `rows_total` means the settle path has stopped.
- **`armed` is not "a worker ran".** It counts turns the cue had material waiting, so a worker writing ten findings before one gets through is one delivery and nine supersessions, not ten failures.
- **A low `reach_rate` is not automatically a bug.** A topic-gated cue that stays quiet while the conversation is elsewhere is working. Act on a rate near zero over a long window — that is a gate that never matches, and for an LLM-calling worker it is wasted tokens.
- **`never_armed` is the loudest signal**, and the easiest to miss because it is an absence: a registered cue with no rows either never gets written by its worker or is read wrongly by the arming model, and neither shows up as a bad rate.
- **`coarse_arming` cues report a floor, not an estimate.** Those five dedupe by a per-topic key set rather than a watermark, so arming degrades to "the journal is non-empty" and over-counts.

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

