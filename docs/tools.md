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
| `web_search` | Web results, fetched **synchronously inside the turn** so she can answer from them in the same reply. See [Synchronous web search](#synchronous-web-search-d3) below. |

## Synchronous web search (D3)

`web_search` is the one tool that blocks the turn on a network call, so it
works differently from the rest.

**When it fires.** The P14 gate has to see a search-shaped signal
(a release date, a new season, an announcement, "look it up", a year, …
— see the `web` family in [`tool_pass_gate.py`](../app/core/session/tool_pass_gate.py)),
*and* the model then has to pick the tool over `respond_directly`. On a
sample of 800 real user turns the `web` family opened zero additional
decision passes beyond those already opening for other families, so a
chatty conversation pays nothing for having it registered.

**What it costs when it does fire.** Measured against LangSearch: ~2.7s
for the round-trip, on top of the ~3s decision pass, on top of the usual
~1.5s to first token — so roughly 7s to her first word instead of 1.5s.
Aiko says a short "hang on, let me check" line through TTS before the
dispatch so the pause reads as a lookup rather than a hang; the chat UI
shows the same thing as a tool-activity chip. That line is spoken only —
it never enters the transcript or the persisted message.

**Context cost.** Three results at 400 characters, about 450 tokens, and
only on the turn that searched: the tool result lives in that turn's
message list and is never persisted into history, so it doesn't compound.

**Privacy.** The query is written by the chat model, which composes it
with the persona, retrieved memories and the transcript in view — so it
is not trusted. Every outbound query passes through the same scrubber the
background fact-checker uses: names and first-person tokens are dropped,
and a query carrying hard identifiers (a URL, an email, an address) is
refused outright with a `ToolError` telling her to rephrase. A refusal
never reaches the search engine.

**No fallback on this lane.** The brain lane builds its own provider with
`search.brain_timeout_seconds` (6s) and *without* the DuckDuckGo
fallback, so a LangSearch outage surfaces as "I couldn't reach the web"
rather than silently reinstating the slow HTML scrape that got this tool
removed from the conversational lane in the first place.

**What she keeps.** After the turn, a speaking-window job hands the hits
to the F9 knowledge worker's distiller, which writes at most two
evergreen, impersonal, source-cited `knowledge` memories (deduped
semantically against what she already knows). Raw snippets are never
stored. So the second conversation about the same show doesn't need the
web. Requires `agent.knowledge_enrichment_enabled`; without it the search
still works, she just doesn't retain it.

Switches: `tools.web_search` (the family, shared with the background
workflow skill) and `search.brain_tool_enabled` (this lane alone — turn
it off to get the latency back and keep the background lanes).

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
