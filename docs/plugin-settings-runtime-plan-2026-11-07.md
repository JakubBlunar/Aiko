# Plugin settings and runtime lifecycle

Design recorded 2026-11-07. The first implementation is now available; see the
[runtime settings and SDK guide](plugin-settings.md) for shipped behavior and
verification boundaries. The historical diagnosis below describes the earlier
startup-only implementation. This design extends
the existing [plugin platform roadmap](personality-backlog/plugin-system.md)
and [SDK](plugins.md), rather than introducing a second plugin system.

First-pass verification: 739 representative affected Python checks, 232 related
frontend checks, real source-plugin stdio/workflow execution with isolated pinned
parsers, and an isolated browser preview covering editing, conflicts, validation,
toggles, reset, retry, advanced fields and 375px-wide forms. Later focused lifecycle
checks cover publication and cleanup changes. Two existing private-reach budget
failures remain unchanged from committed HEAD (web 1/0, MCP 471/466); ownership
guards pass. Browser/filesystem `npx` services and physical mobile devices were
not exercised against the live install. No live plugin configuration was changed.

## Current implementation and why restarts are needed

- [`PluginStub`](../app/plugins/loader.py) discovers JSON identity and merged
  opaque configuration. It has no settings schema/UI contract.
- [`activate_plugin`](../app/plugins/runtime.py) installs dependencies and calls
  `define_plugin(api)`, producing a server spec, skills, middleware and fast tools.
  Its `active` status means registrations were produced, not that an MCP
  connection has finished starting or is healthy.
- [`_init_external_mcp`](../app/core/session/task_orchestration_mixin.py) collects
  registrations and starts connections at boot. `reload_plugin_guidance` runs
  discovery/activation again but replaces guidance only.
- [`ExternalMcpManager.restart`](../app/mcp/client/manager.py) reconnects a server
  using its existing spec. There is no selective add/remove/replace lifecycle.
- [`register_mcp_skills`](../app/core/tasks/workflow/mcp_skills.py) adds/overwrites
  skills; it does not atomically remove a plugin's obsolete registrations.
- [`ToolsTab`](../web/src/features/settings/ToolsTab.tsx) exposes built-in tools,
  not plugin-owned fields or lifecycle status.

Current operation signatures cover tool/schema/effect, not configuration or
workspace identity. A root change can preserve the exact same schema and relative
path while changing what source the operation means. Runtime configuration must
address this, alongside the settings form.

## Settings experience

Start in **Settings -> Tools -> Plugins** using reusable plugin cards and form
controls, including the existing mobile settings surface. Each card shows:

- Plugin name, purpose and enable switch.
- Actual state: disabled, needs configuration, installing, starting, ready,
  applying or error; include a useful failure reason and retry action.
- Collapsible configuration generated from its declared fields, with defaults,
  field validation and an explicit Apply action for edits.
- The effective access/capability summary: which workspaces/folders Aiko can
  use, whether writes are available and which existing approval policy applies.
- Whether saved values match the active revision. An enabled checkbox alone
  must not imply that tools are connected or available to the conversation.

Allow configuration while disabled. Validate before enabling and before installing
dependencies when required fields are missing. Disabling must remain possible
even when configuration is broken. Explain global blocks such as the plugin
master switch, main tool switch or disabled background workflows.

Changes persist through the central `plugins.entries.<id>` override; do not edit
bundled manifests/default files or require navigating to plugin directories.
Reset-to-default removes the appropriate override rather than saving a flattened
copy of every inherited value. Preserve unknown legacy configuration keys.

Folder fields refer to the **Aiko host's filesystem**. A mobile/web folder chooser
must not silently supply a folder from the phone/browser machine. Begin with
validated absolute paths, then a host-folder browser or native picker where
appropriate. Reuse one path-list/repeated-object editor across plugins.

## Declarative settings contract

Add optional manifest references to `config/schema.json` and `config/ui.json`.
The loader reads these without importing `entry.py` or installing dependencies.
Disabled and gated plugins must still expose enough metadata to configure them.
Resolve metadata paths inside the plugin bundle and reject escaping references.

Use the project's existing JSON Schema validation on the backend. UI metadata
adds labels, help, groups, advanced fields, directory widgets and repeated-object
titles; it must not execute plugin-supplied JavaScript. A reusable renderer needs
booleans, strings, enums, bounded numbers, folder lists and object arrays first.
The server remains the validation authority. Redact any future secret fields and
use the existing credential boundaries rather than exposing saved secrets in GET.

Field metadata can declare an apply category:

- `live`: a supported immutable configuration/middleware update at an operation
  boundary, without touching access, transport identity or resource binding.
- `reconnect`: rebuild only this plugin's service/registrations.
- `access`: change roots/workspaces/tool exposure, revoke the old scope and
  invalidate affected pending operations/approvals before new dispatch.

Unknown/legacy changes default to controlled plugin reconfiguration. The first
version can reconnect more conservatively; avoiding an Aiko restart is the
requirement. Optimizing particular live fields can follow once their contracts
are explicit.

## Host-owned lifecycle

Introduce one plugin manager with an immutable active snapshot per plugin and
owned handles for connections, skills, middleware, fast tools and cleanup hooks.
Keep these responsibilities out of the already large session mixin.

The settings facade/REST layer should support listing descriptors and actual
state, validating/updating config, enabling/disabling and retrying apply. An
apply operation runs outside the chat/event-loop/request lane. Dependency install
and process startup have progress/error state; they cannot block a conversation.
Publish lifecycle updates to settings clients through the existing WebSocket.

An update follows this sequence:

1. Validate against the plugin descriptor and an expected configuration revision.
   A stale editor receives a conflict and retains its draft.
2. Persist desired overrides and publish the applying state. Keep desired and
   applied revisions separate; saved is not synonymous with active.
3. Block affected new invocations, and immediately revoke removed access. Retire
   the old owned registrations/service as needed; changing one plugin must not
   restart all MCP servers.
4. Build a fresh registration set, connect the service, discover tools and
   atomically replace this owner's catalogue, guidance, middleware and fast tools.
5. Publish ready or an honest failure. An access reduction must not roll back
   to an old process with broader permissions merely because reconnect failed.

Do not implement Apply by repeatedly calling `activate_all` and appending its
registrations. Add per-server lifecycle operations to `ExternalMcpManager` and
owner-scoped replacement/removal to the skill and fast-tool registration paths.
Registration ownership must also cover plugins such as the calculator, which
have fast tools rather than a server.

Disable removes future tool visibility and dispatch permission immediately,
then quiesces owned resources. An operation already sent may finish; cancellation
cannot undo its external effect. Late outputs retain their original generation
and cannot become fresh success under a replacement configuration. Unknown
mutations retain the harness's existing uncertain-effect behavior.

Changing installed code/native dependencies is a separate upgrade lifecycle.
Routine configuration/toggles should not require an app restart, but this should
not promise safe hot replacement of arbitrary in-process Python modules.

## Bind jobs and approvals to the right configuration

Carry plugin ownership and configuration/access generation through discovered
MCP descriptors, workflow skills, pending steps and approval contracts. Recheck
them in the manager loop immediately before RPC dispatch, as well as at planning
and admission time. An old turn's fast-tool wrapper needs the same owner guard.

On disable or access change, paused jobs preserve their evidence and budget but
wait/replan with an explicit reason. They must not resolve an old relative path
under a newly selected folder. A schema-only signature is insufficient. Pending
approval cannot authorize a different root/account/resource after reconfiguration.
Unrelated plugins and their jobs stay available.

Saved historical results remain history; disabling is not implicit data deletion.
Existing jobs without a provable resource/configuration binding need a fresh plan
when access changes, rather than a guessed migration.

## Bundled plugin adoption

**Source code:** replace the single-root UI with workspace cards containing a
stable id, display name, root and include/exclude paths. Add workspace discovery
and explicit `workspace_id` on source operations; the UI's selected workspace
must not silently retarget a running job. Adapt legacy `root/include_paths` to a
stable default workspace where the binding is known. Retain hashes, path policy
and the read-only boundary.

**Filesystem:** expose a list of allowed roots and update only that server when
roots change. Migrate legacy `root` to a one-item list. Show the actual read/write
capabilities and current approval policy. Do not add a cosmetic per-root
"read-only" switch unless the dispatch/server boundary enforces it.

**Browser:** expose adapter, snapshot/detail limits and memory-page settings,
with ranking weights under Advanced. Replace/reconfigure its owned middleware;
reconnect only when transport/exposure settings require it. Reconnection must
invalidate cached page/element references. Optional executable browser tools
need meaningful descriptions when exposure is changed.

**Calculator/legacy plugins:** runtime enable/disable must remove/add their fast
tools and gate metadata without duplicate registrations. Plugins without schemas
still get lifecycle controls and a clear legacy-config status; optional advanced
JSON editing can use conservative plugin reconfiguration.

## Verification gates and implementation slices

1. **Lifecycle and ownership foundation:** selective server management, atomic
   contribution replacement, dispatch generations, cleanup and persistence.
2. **One complete settings flow:** JSON descriptors, public settings facade and
   REST/WS status, shared form renderer and runtime toggle/apply. Exercise source
   scope and calculator fast-tool cases so the framework covers both lanes.
3. **Bundle migration:** source workspaces, filesystem roots and browser fields,
   with legacy config adapters and clear effective-access copy.

Offline tests should cover disabled-schema discovery without imports/pip; missing
config before dependency installation; invalid edits; persistence/restart;
selective reconnect without disturbing another plugin; stale editor conflicts;
repeated toggles without leaked/duplicate tools; and apply/cleanup failures.

Also prove disable during planning prevents dispatch, and an old approved step
cannot read/write the same relative path in a replacement workspace even when
the tool schema is unchanged. Revocation during an in-flight mutation must not
trigger a blind retry or fresh-success report. Test both MCP and fast-tool lanes.

UI checks should cover add/edit/remove workspace/root cards, validation errors,
saved-versus-active state, progress/retry, keyboard access, mobile layout and
host-folder semantics. Use disposable settings/repos/services, preserving the
existing live-install test guards. Ordinary-use testing can then assess whether
the resulting control surface feels clear and sufficiently immediate.
