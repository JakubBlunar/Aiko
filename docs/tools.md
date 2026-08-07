# Tools

Aiko can call tools through the LLM's native function-calling. Each turn runs a
**pre-stream `chat_with_tools` pass**; if a tool call appears it executes, the
result is appended, and then the streaming reply pass runs — so a tool result is
folded into the same turn rather than a separate round-trip.

Tools are grouped into **families**. Toggle each family (or the whole registry)
from the web Settings drawer or under the `tools` block in config — see the
[`tools` section of the configuration reference](configuration.md#tools--toolssettings)
for the exact flags and their no-op conditions. With `agent.skill_router_enabled`
on, only the families relevant to a turn are shown to the model (progressive tool
disclosure — see [`skills-framework.md`](skills-framework.md)).

## Fact tools

For things she can't just know:

| Tool | Returns |
|---|---|
| `get_time` | Current ISO date/time. |
| `recall` | Semantic search across memories, recent messages, and uploaded documents (LanceDB). |
| `recall_topic` | Rounds up everything she remembers about a whole topic/theme (a cluster), not just the single closest line. No-op without a persistent topic graph. |
| `recall_concept` | Explains *why* she thinks/believes something — a higher-order concept with its rationale, supporting memories, topic areas, and related concepts. No-op without the concept store wired in. |
| `recall_self_history` | Walks how a belief *changed* — eras of formed / replaced / faded / revived / held-all-along, each with the reason recorded at the time. Returns `thin_record` rather than improvising when the trail is too sparse. |
| `recall_hypotheses` | Lists what she is still **unsure** about: open guesses with a credence, and an `origin` marking whether she derived each from something she noticed (`grounded`) or invented it outright (`invented`). See [`hypotheses.md`](hypotheses.md). |
| `web_search` | Web results. The backend (LangSearch or DuckDuckGo) is configured under the `search` block; LangSearch falls back to DuckDuckGo. |

## Weather tools

Real-world weather (`config.weather`; independent of the passive ambient
`agent.weather_sync_enabled` feed). Coarse city-granularity location only, never
GPS — see [`weather-sync.md`](weather-sync.md).

| Tool | Returns |
|---|---|
| `get_weather` | Current conditions (temperature, conditions, humidity, wind) for her configured home location or any named city. |
| `get_forecast` | Multi-day forecast for a location. |

## Room tools

For actually inhabiting her room (`WorldStore`) — see [`aiko-room.md`](aiko-room.md).

| Tool | Returns |
|---|---|
| `look_around` | Fresh snapshot of her current spot, posture, and nearby items. |
| `move_to` | Relocate her to a different spot (bed, desk, window seat, ...). |
| `change_posture` | Update posture (sitting / curled_up / ...) + activity. |
| `inspect_item` | Detailed read of one item (description, state, quantity). |
| `consume_item` | Decrement a consumable (cookies, tea); refuses non-consumables. |

The read-only room tools (`look_around`, `inspect_item`) are intentionally
infrequent because the prompt already carries a passive room summary — the
mutative tools (`move_to`, `change_posture`, `consume_item`) are the ones that
actually change visible state.

## Goal tools

Aiko's own longer-term goals (K1). Independent of `agent.goals_enabled` (the
prompt block + worker), these let her *act* on goals mid-turn:

| Tool | Returns |
|---|---|
| `add_goal` | Declare a new longer-term goal. |
| `update_goal_progress` | Log progress against an existing goal. |
| `archive_goal` | Retire a goal she's done with. |
| `list_goals` | Review her current goals. |

## Beyond the built-ins

- **Bundled plugins.** The `calculator` plugin contributes a synchronous
  exact-arithmetic fast tool (AST whitelist, no `eval`) so Aiko never guesses a
  number. Plugins register their own tools through the ToolPlugin SDK — see
  [`plugins.md`](plugins.md) and [`skills-framework.md`](skills-framework.md).
- **External MCP servers.** Point `mcp_clients.servers` at external MCP servers
  to expose their tools to Aiko as first-class tools — see
  [`mcp-clients.md`](mcp-clients.md).
