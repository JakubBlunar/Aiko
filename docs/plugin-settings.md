# Plugin settings and runtime management

Open **Settings > Tools > Plugins**. Each discovered plugin owns its settings
descriptor; the app renders the same controls and lifecycle states for every
bundle. Disabling a plugin or applying its settings does not restart Aiko or
unrelated MCP services. Enabling a plugin trusts its Python entry point, as before.

The bundled source-code, filesystem, browser and calculator plugins use this API.
Source workspaces have stable IDs, display names, absolute host folders and
explicit allowed relative paths. File tools accept multiple allowed folders.
Directory fields always refer to the computer running Aiko, including when the
settings UI is opened on a phone. This version uses typed paths and host-side
validation; it does not offer a host folder browser yet.

## Saved versus active

An update persists desired settings before background activation. Status moves
through applying, installing dependencies, connecting and active, or reports
needs configuration / error. Saved settings are not evidence that a service is
connected. Access summaries distinguish configured and active access. File-tool
writes continue to follow the existing task approval policy.

Apply validates the draft and its expected revision. Conflicts preserve the
draft and require review against the current settings. Reset overrides removes
central configuration values, restoring bundle defaults and any existing
plugin-local values. Editing one plugin leaves unrelated settings alone.

Settings clients receive `plugins_updated` WebSocket invalidations. Mounted
plugin panels also poll every two seconds to recover missed connection/status
events; closed panels do not fetch.

All setting changes conservatively rebuild that plugin's registrations and
reconnect its service. A browser reconnect can lose browser state. Code/native
dependency upgrades and discovering newly installed bundles may still require
an application restart; routine toggles and configuration changes do not.

## Authoring a settings descriptor

Reference plugin-owned JSON files from `plugin.json`:

```json
{
  "plugin_api_version": 1,
  "id": "my_files",
  "name": "My files",
  "enabled": false,
  "settings": {
    "schema": "config/schema.json",
    "ui": "config/ui.json"
  }
}
```

Discovery reads JSON only, including for disabled plugins. Metadata files must
stay inside the bundle, be objects and be at most 64 KiB each. Schema references
are local fragments only. Invalid descriptors fail closed before entry import
or dependency installation. Plugins without descriptors retain enable/disable
and status controls; their opaque legacy configuration is not exposed.

Use an object JSON Schema with `properties`. Supported editors are string,
boolean, integer, number, scalar enum, object, and homogeneous arrays of strings
or objects. Use `title`, `description`, `default`, numeric bounds, `required`,
patterns and normal JSON Schema constraints. Backend validation remains the
authority. Example:

```json
{
  "type": "object",
  "properties": {
    "roots": {
      "type": "array",
      "title": "Allowed folders",
      "default": [],
      "uniqueItems": true,
      "x-addLabel": "Add folder",
      "items": {
        "type": "string",
        "title": "Folder",
        "minLength": 1,
        "format": "host-directory"
      }
    }
  }
}
```

`host-directory` validates existing absolute host folders. An array can declare
`x-uniqueBy` to validate unique object IDs, `x-itemTitle` to name collapsed resource
cards from an object field, and `x-addLabel` to label its add button. New object
items get an ID in their `x-uniqueBy` field. These are generic renderer features;
no bundle ID or setting name belongs in core or the renderer.

`config/ui.json` supplies optional presentation and activation metadata:

```json
{
  "description": "Read and write inside these folders; task approval rules apply.",
  "required_for_activation": ["roots"],
  "advanced": [],
  "resources": [{ "field": "roots", "mode": "read_write" }]
}
```

Resource declarations can also specify `name_field`, `path_field` and
`scope_field` for object arrays. Modes are descriptive access summaries, not a
replacement for the plugin's own enforcement. See the source-code bundle's
schema/UI for named workspace examples. `legacy_single` describes an optional
old field to list migration, used by the two filesystem-related bundles. An
explicit empty resource list always wins over that migration.

## SDK and ownership

`api.settings` returns the standardized `PluginSettings` descriptor. `api.config`
contains the effective settings for this activation, copied from host state.
Configuration precedence remains bundle defaults < plugin-local ignored
`config/user.json` < central `plugins.entries.<id>.config`. Plugin-specific
normalization, capability wiring and operational validation belong in the bundle.

Use `api.register_cleanup(callback)` for resources owned by an activation. The
host runs cleanup on replacement, disable, shutdown or a failed definition.
Callbacks should be synchronous, bounded and idempotent. MCP transports remain
host-owned; plugins should not start duplicate servers themselves.

Only declared properties are editable through the settings API. `writeOnly`
values are omitted from reads and validation errors redact their values. Drafts
send edited fields only; omitted secrets inside stable-ID resource rows are
preserved. Environment-backed credentials through `api.env` remain preferable;
this is not a new encrypted credential store. Never put secrets in metadata,
descriptions, defaults or errors.

Runtime orchestration is in `app/plugins/manager.py`; the session mixin and REST
routes are generic adapters. They do not know about browser weights, file roots
or code workspaces. A plugin cannot add host settings branches or its own REST
settings handlers through this API.

Tool-result middleware can also provide a generic `planner_context` projection
for important current state. See [task context budgeting](task-context-budget.md)
for ownership, freshness and prompt sizing; browser formatting stays in its bundle.

The SDK also provides `register_playbook` and `register_procedure` for on-demand
instructions and bounded compound operations. Definitions, configuration and
domain semantics remain plugin-owned; common generation, admission and checkpoint
rules remain in the host. See [local task execution](task-harness-optimizations.md).

## Dispatch and approval boundaries

Changing configuration immediately revokes the old generation. The host retires
its service, guidance, middleware and fast tools before publishing the replacement.
The service remains hidden until its owner registrations are published.

Planner menus, pending steps, child dispatch, approvals and MCP calls bind the
generation as well as the operation schema. An unchanged tool schema cannot
redirect an old operation to a new folder. In-progress calls check generation
again when returning. Session approve-all grants are generation-scoped; explicit
persistent approval policies keep their stable capability IDs. Uncertain writes
retain the existing conservative outcome and are not automatically repeated.

Old per-turn fast-tool objects also reject calls after revocation. Middleware
is captured at dispatch, so an old result is not processed by a new pipeline.
Work interrupted by a configuration change asks for a fresh plan rather than
silently switching resources.

## API

- `GET /api/plugins`: descriptors, public effective config, revisions, lifecycle
  status, access summaries and tool counts.
- `PATCH /api/plugins`: `{ "enabled": true }` for the master switch.
- `PATCH /api/plugins/{id}`: `{ "revision": "...", "config": {...} }`,
  `{ "revision": "...", "enabled": false }`, or
  `{ "revision": "...", "reset": true }`.
- `POST /api/plugins/{id}/retry`: `{ "revision": "..." }`.

Validation returns 422 with field paths; stale revisions return 409. Activation
is asynchronous and its failure does not roll back the saved desired settings.
Retry or edit the plugin after correcting its configuration/dependency problem.
