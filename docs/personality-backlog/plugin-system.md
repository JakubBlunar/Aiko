# Plugin system — full vision (P1 + P2 shipped, P3–P4 deferred)

The **SDK-primary ToolPlugin** system is shipped — see
[`docs/plugins.md`](../plugins.md) for the shipped shape. A plugin is a small
Python package (`plugin.json` stub + `entry.py` + plugin-local `config/`) whose
`define_plugin(api)` registers an MCP server, `SKILL.md` guidance, and optional
tool-result middleware. This doc captures the *rest* of the vision (P3 hooks, P4
providers), none of which is built yet.

## History: declarative-first, then SDK-primary

The first cut (P1) was **declarative-only**: a `plugin.json` with `mcp` /
`contracts` / `requires` blocks and `${CONFIG}`/`${ENV}` placeholder
substitution, no code entrypoint — chosen to sidestep the trust decision of
running third-party Python in the host process. It shipped and worked, but hit a
wall: reshaping a tool's *result* (e.g. the browser accessibility snapshot →
ranked render) is real code that a manifest cannot express. Rather than bolt a
second mechanism onto the manifest, P2 replaced the declarative path with an
**SDK** — the manifest became a bare stub and all capabilities moved to
`entry.py`.

The trust boundary is handled structurally instead of by an allow-list: the
**loader is pure JSON** (never imports code) and **activation only runs for
plugins already resolved `enabled=true` from JSON alone**, so a disabled
plugin's code is never imported. "Enabled == trusted", same as a `pip` package.
Isolated per-plugin dependency install (`pip --target data/plugins-deps/<id>/`)
keeps a plugin's deps from polluting the app env.

## Forward-compatible manifest (already shipped)

Every bundle carries:

- `plugin_api_version` *(int)* — the loader routes by version. Phase 1
  supports v1; a higher version is loaded-but-inactive (`status:invalid`),
  never guessed at.
- `contracts` *(map)* — declared capabilities, routed by name:
  - **Implemented now:** `mcp` (synthesise an `ExternalMcpServer`),
    `skills` (load `SKILL.md` guidance).
  - **Reserved now** (recognised, validated, skipped-with-a-log — a bundle
    may declare them today without erroring): `workflow_tools`, `hooks`.
  - **Not reserved:** `channels` (no messaging surface in Aiko),
    `llm_providers` (the LLM catalogue stays config-owned for now). These
    are treated as unknown contracts (warned + ignored).

Because the loader already recognises the reserved names, a phase-2 bundle
can ship `contracts.workflow_tools` and older builds will log-skip it
rather than crash.

## Reserved contracts → existing seams

The two reserved contracts map cleanly onto seams that already exist, so
lighting them up is additive, not a rearchitecture:

| Contract | Maps onto | What a code plugin would register |
|----------|-----------|-----------------------------------|
| `workflow_tools` | [`WorkflowSkillRegistry.register`](../../app/core/tasks/workflow/skill_registry.py) | A code-defined background skill (a `WorkflowSkill` with its own `spawn`) — for capabilities that aren't a plain MCP tool call. |
| `hooks` | inner-life prompt-block providers + [`post_turn_mixin`](../../app/core/session/post_turn_mixin.py) | A prompt-block provider (adds a cue to the system prompt) and/or a post-turn observer (reacts to each finished turn). |
| *(future)* `llm_providers` | the `llm.providers` / `llm.routes` catalogue | A saved provider + default route. Only if we ever want plugin-shipped providers; low priority. |

## The SDK (shipped)

The [`PluginApi`](../../app/plugins/sdk.py) a plugin's `entry.py` codes
against:

```python
# plugins/foo/entry.py  (SHIPPED)
def define_plugin(api) -> None:
    api.require_binary("npx")                       # gating
    api.register_mcp_server(command="npx", args=[...])
    api.register_skills("skills")                   # SKILL.md guidance
    api.register_tool_result_middleware(MyMw())     # reshape tool output
```

Shipped surface: `register_mcp_server` / `register_skills` / `register_skill` /
`register_tool_result_middleware`, gating `require_binary` / `require_config` /
`require_env`, and `config` / `env` / `plugin_root` / `logger` accessors. The
next SDK additions (P3) slot in as more `register_*` methods without changing the
lifecycle.

## Phased roadmap

- **P1 — declarative MCP + skills (SHIPPED, then RETIRED).** Manifest + `SKILL.md`
  + `requires` gating + `${CONFIG}`/`${ENV}` placeholders. Folded into P2.
- **P2 — SDK-primary + tool-result middleware (SHIPPED).** `plugin.json` stub +
  `entry.py` + `PluginApi`; MCP server + skills + tool-result middleware; gating
  helpers; plugin-local config layering; isolated dependency auto-install; the
  pure-loader / activation trust split. Bundled filesystem + browser plugins
  migrated. [`docs/plugins.md`](../plugins.md).
- **P3 — `workflow_tools` + `hooks` on the SDK.** `api.register_workflow_tool(...)`
  (a code-defined `WorkflowSkill` for capabilities that aren't a plain MCP tool
  call) and `api.on_post_turn(...)` / `api.add_prompt_block(...)` (prompt-block
  providers + post-turn observers threaded through the inner-life sweep and
  [`post_turn_mixin`](../../app/core/session/post_turn_mixin.py)).
- **P4 (maybe) — provider contracts.** Plugin-shipped `llm_providers`. Low
  priority; the config catalogue already covers this.

## Extracting the SDK as a package

[`sdk.py`](../../app/plugins/sdk.py) is deliberately stdlib-only (no `app.*`
imports — the `ExternalMcpServer` binding lives in the runtime), so it can later
ship as a standalone `aiko-plugin-sdk` wheel that third-party authors depend on
to type-check their `entry.py` against `PluginApi` / `ToolResultMiddleware` /
`MiddlewareResult`, with zero behaviour change in-tree.

## Explicitly out of scope

- **`channels`** — Aiko has no pluggable messaging surface; there's
  nothing for a channel contract to plug into.
