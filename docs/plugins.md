# ToolPlugins (SDK-primary MCP plugins)

A **plugin** is a self-contained folder that teaches Aiko's background worker
lane a new capability with **no core code change**. A plugin is a small Python
package whose `entry.py` registers, in code:

1. an **MCP server** (how to start / connect to the tool server),
2. its **`SKILL.md` operational guidance** for the workflow planner, and
3. optional **tool-result middleware** that reshapes a tool's raw output before
   the planner sees it (the piece a declarative manifest could not express).

Drop a plugin folder in, enable it, and its tools appear to the background
planner, its guidance is injected into the planner prompt, and its middleware
runs — automatically.

> **Lane rule (hard).** Plugin MCP tools and their `SKILL.md` guidance reach
> the **background workflow planner only** — never the brain's fast
> `ToolRegistry`. This matches the existing external-MCP contract.

## Trust model (no sandbox)

A plugin's `entry.py` runs **in-process** — there is no sandbox. The model is
"you installed it, you trust it" (same as a `pip` package). The safety guarantee
is that the two lifecycle phases are strictly separated, with the enable gate
between them:

1. **Discovery** — [`loader.py`](../app/plugins/loader.py) reads **only**
   `plugin.json` + plugin-local `config/` (pure JSON). It never imports
   `entry.py`, never touches `sys.path`, never runs `pip`.
2. **Activation** — [`runtime.py`](../app/plugins/runtime.py) runs code, and
   **only** for stubs that already resolved to `enabled=true` from JSON alone.

So a **disabled plugin is completely inert**: its code is never imported and its
dependencies are never installed. A forward-incompatible `plugin_api_version` is
skipped the same way.

## Bundle layout

```
plugins/filesystem/
  plugin.json          # STUB: {id, name, plugin_api_version, enabled}
  entry.py             # def define_plugin(api): ...
  config/
    default.json       # committed defaults
    user.json          # GITIGNORED machine-specific / secret values
  skills/SKILL.md      # optional planner guidance
  requirements.txt     # optional Python deps (auto-installed, isolated)
```

Plugins are discovered from three roots, in precedence order (first-seen id
wins, so a bundled plugin shadows a same-id user plugin):

1. `plugins/` in the repo (bundled),
2. `data/plugins/` (user, gitignored),
3. any extra dirs in `plugins.paths` (config).

## `plugin.json` (stub only)

```json
{
  "plugin_api_version": 1,
  "id": "filesystem",
  "name": "Filesystem",
  "enabled": false
}
```

That's the whole manifest. It exists so the app can list / enable plugins
without importing code. An optional `"python_dependencies": ["pkg==1.0"]` key is
also read here (merged with `requirements.txt`). Everything else — the MCP
server, skills, middleware — is registered in `entry.py`. The stub is parsed
leniently (comments / trailing commas tolerated).

## `entry.py` and the `PluginApi`

```python
def define_plugin(api):
    api.require_binary("npx")            # gate out cleanly if npx is missing
    root = api.require_config("root")    # gate out if config.root is unset
    api.register_mcp_server(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", root],
        timeout_seconds=30,
    )
    api.register_skills("skills")        # load skills/SKILL.md as guidance
```

The runtime constructs a [`PluginApi`](../app/plugins/sdk.py), calls
`define_plugin(api)`, and reads back what was registered. Surface:

- `api.config` — the merged plugin config dict (see below).
- `api.plugin_id`, `api.plugin_root`, `api.logger`, `api.env(name, default)`.
- **Gating** (raise `PluginGatedError`, caught → plugin marked `gated_out`):
  `api.require_binary(name)`, `api.require_config(key)`, `api.require_env(name)`
  (each returns the resolved value).
- **Registration**:
  - `api.register_mcp_server(*, transport="stdio", command, args, env, url,
    autostart, timeout_seconds, expose_tools, disabled_tools, name)` — the MCP
    server (id = plugin id).
  - `api.register_skills(*dirs)` (default `"skills"`) /
    `api.register_skill(name, description, body)` — planner guidance.
  - `api.register_tool_result_middleware(mw, *, server_id=None, tool_names=None)`
    — see below.
  - `api.register_fast_tool(*, name, description, parameters, handler,
    family=None, gate_patterns=None)` — a **brain-lane fast tool** (see
    below). Call it any number of times to ship a family of tools.

Zero-import basic plugins work because `api` is injected — a plugin needs no
imports to register a server / skills. The SDK's public surface is deliberately
dependency-light so it can later ship as a standalone `aiko-plugin-sdk` for
third-party authors (the `ExternalMcpServer` binding lives in the runtime, not
the SDK).

## Tool-result middleware

A middleware reshapes a claimed tool result before the planner sees it. Any
object matching the [`ToolResultMiddleware`](../app/plugins/sdk.py) shape
works (duck-typed — no import required):

```python
def claims(self, server_id, tool_name) -> bool: ...
def transform(self, server_id, tool_name, raw_text, tool_args=None): ...
    # returns a MiddlewareResult(content, summary, element_count) or None
```

`None` = pass the raw result through unchanged. Registered middlewares form a
chain in [`McpToolHandler`](../app/core/tasks/handlers/mcp_tool.py): for each
tool result, the **first** middleware that `claims()` AND returns a non-`None`
`transform()` wins; a middleware that raises is skipped (the tool never breaks).
This is exactly how the bundled **browser** plugin registers `BrowserPerception`
to reshape `browser_snapshot` output — the parity piece a declarative manifest
could not express.

## Fast tools (brain lane)

Where MCP servers + skills + middleware live on the **background/worker** lane
(the workflow planner spawns tasks), a **fast tool** runs **inline on the
conversational turn** — the chat model calls it like a builtin and reads the
result back in the same reply. Register one with:

```python
def define_plugin(api):
    api.register_fast_tool(
        name="calculate",
        description="Evaluate an arithmetic expression and return the EXACT result. ...",
        parameters={"type": "object",
                    "properties": {"expression": {"type": "string"}},
                    "required": ["expression"]},
        handler=_calculate,          # (args: dict) -> str, runs SYNCHRONOUSLY
        family="math",               # optional: P14 gate / skill-router family
        gate_patterns=[r"calculate", r"percent", r"\d+\s*[-+*/x^]\s*\d+"],
    )
```

Key points:

- **The schema is the skill.** On the brain lane there is no separate
  `SKILL.md`; the `description` (and `parameters`) is what tells Aiko how and
  when to call the tool. Write the usage guidance there.
- **Synchronous + fast.** `handler(args)` runs on the turn thread and must
  return a `str` (or raise) — it blocks the reply, so keep it quick. Raising
  surfaces a clean error string to the model.
- **Gating (optional but recommended).** `family` + `gate_patterns` wire the
  tool into the P14 tool-pass gate / brain skill router
  ([`tool_pass_gate`](../app/core/session/tool_pass_gate.py)): the gate can
  then skip the decision pass on turns with no matching signal, and (with
  `agent.skill_router_enabled`) narrow the disclosed schema set to matching
  families. A tool with **no** family still works but forces the gate to
  always run (`reason="unknown_tool"`).
- **Multiple tools per plugin.** Call `register_fast_tool` as many times as you
  like; each becomes its own tool in the brain `ToolRegistry`.

The bundled **calculator** plugin (`plugins/calculator/`) is the reference: it
moves the old core `calculate` builtin into a plugin, shipping its evaluator in
a plugin-local `aiko_calc` package and its family patterns via `gate_patterns`.

## Plugin-local config

Config lives next to the plugin, mirroring the app's own `config/` layering, and
is merged in precedence order (later wins):

1. `config/default.json` (committed defaults),
2. `config/user.json` (**gitignored** — machine-specific paths, tokens),
3. central `plugins.entries.<id>.config` (an override in the app's
   `config/user.json`).

The merged dict is `api.config`. Secrets can also be read at runtime via
`api.env("MY_TOKEN")`.

## `SKILL.md`

AgentSkills-style: a single-line YAML frontmatter block, then the markdown body
that is the planner playbook.

```markdown
---
name: filesystem
description: Sandboxed file read/write under a fixed absolute root
---
- These file tools are sandboxed to a fixed absolute root …
```

All registered `SKILL.md` bodies for a plugin are joined into
`group_guidance["mcp:<id>"]`, injected when that server's tools are in the menu.

## Guidance precedence

For each `mcp:<id>` group present in a workflow's skill menu, the planner picks
guidance in this order:

**plugin `SKILL.md` > runtime-captured server instructions > hardcoded playbook**

- *Plugin `SKILL.md`* is stamped at activation.
- *Runtime-captured* is the server's `initialize()` `instructions` plus a
  best-effort `list_prompts()` snapshot, read live from the manager.
- *Hardcoded* is the built-in `BROWSER_PLAYBOOK` / `FILESYSTEM_PLAYBOOK` in
  [`skill_guidance.py`](../app/core/tasks/workflow/skill_guidance.py).

## Python dependencies (auto-install, isolated)

Declare deps in `requirements.txt` (or the manifest `python_dependencies` key).
When an enabled plugin activates, the runtime installs them into an **isolated
per-plugin dir** (`data/plugins-deps/<id>/`, gitignored) via
`pip install --target`, and prepends that dir to `sys.path`. A `.installed.json`
marker (hash of the dep list) makes it idempotent — reinstall only when the list
changes. If install fails, the plugin is marked `invalid` and its `entry.py` is
not imported. First-boot install can be slow (like an `npx` cold start).

> Caveat: isolated `--target` dirs work well for pure-Python wheels; native /
> compiled packages installed this way can occasionally clash across plugins.

## Config

```json
{
  "plugins": {
    "enabled": true,
    "paths": ["F:/extra/plugins"],
    "entries": {
      "filesystem": { "enabled": true, "config": { "root": "F:/notes" } },
      "browser": { "enabled": true }
    }
  }
}
```

- `plugins.enabled` — master switch for the whole plugin subsystem.
- `plugins.paths` — extra discovery roots.
- `plugins.entries.<id>.enabled` — override the stub's `enabled`.
- `plugins.entries.<id>.config` — highest-precedence config override (merged on
  top of the plugin-local `config/`).

## Bundled plugins

`plugins/filesystem/` and `plugins/browser/` ship as SDK plugins, **disabled by
default** (they would otherwise auto-launch `npx` servers). Enable a bundled
plugin by setting its `plugin.json` `enabled` (or
`plugins.entries.<id>.enabled = true`):

- **filesystem** — pure server + skills. Needs a `root`; put it in
  `plugins/filesystem/config/user.json` (see `config/user.example.json`). Gates
  out via `require_config("root")` until set.
- **browser** — server + skills + the `BrowserPerception` tool-result
  middleware. Tune adapter / ranking weights in `plugins/browser/config/`.

The legacy global `browser_perception` config block still works (back-compat);
when the browser plugin registers its own middleware for the same `server_id`,
the global block is skipped so a snapshot is not reshaped twice.

## Migration from the declarative format

Earlier plugins were pure `plugin.json` manifests (`mcp` / `contracts` /
`requires` blocks + `${CONFIG}`/`${ENV}` placeholder substitution). That path is
retired: the manifest is now a stub and capabilities move to `entry.py`. Replace
a declarative `mcp` block with `api.register_mcp_server(...)`, `requires` with
`api.require_*(...)`, and `${CONFIG:key}` / `${ENV:NAME}` with `api.config[key]`
/ `api.env("NAME")`.

## Debugging (MCP)

- `list_plugins()` — every discovered plugin + status (`active` / `disabled` /
  `gated_out` / `unsupported` / `invalid`), reason, middleware/skill counts,
  deps status, warnings.
- `get_plugin(id)` — full detail incl. the synthesised server + guidance.
- `reload_plugins()` — re-run discovery + activation and refresh planner
  **guidance** live (an edited `SKILL.md` lands on the next workflow).
  Adding/removing a plugin's MCP **server**, changing its middleware, or editing
  `entry.py` **code** still needs an app restart.
- `get_external_mcp_instructions(server_id="")` — the runtime-captured
  instructions/prompts, or the merged `{group: guidance}` the planner reads.

Grep target for load-time tracing:
`tail_logs(module_contains="plugins")` shows `plugin … active:`,
`plugin … gated out:`, `plugin … installing deps:`, and activation-error lines.
