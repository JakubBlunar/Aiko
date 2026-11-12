# Aiko documentation

`docs/` describes the current checked-in behavior, configuration and contracts.
Start with the guide for the subsystem you need. Runtime activation and natural
conversation quality require their own evidence; a source description does not
establish what an already-running backend has loaded.

Reviews, audits, proposals, rollout records and future acceptance checks live in
[`backlog/`](../backlog/README.md). Update the owning guide when behavior changes;
keep the investigation and implementation history in the backlog.

## Agentic work and tools

| Guide | Owns |
| --- | --- |
| [Agent harness](agent-harness.md) | End-to-end workflow behavior: starting, steering, checkpoints, results, reporting, procedures and learned methods |
| [Brain orchestration](brain-orchestration.md) | Event ordering, task lifecycle, persistence, recovery and transport |
| [Task context and scheduling](task-context-budget.md) | Prompt packing, model admission, inference allowances and observation freshness |
| [Task approvals](task-approvals.md) | Concrete-operation policy, approval answers and dispatch checks |
| [Skills framework](skills-framework.md) | Brain-family routing and workflow capability disclosure |
| [Tools](tools.md) | Conversation-facing tools and their behavior |
| [Plugin SDK](plugins.md) | Capability registration and plugin extension points |
| [Plugin settings](plugin-settings.md) | Settings descriptors, runtime apply, ownership and configuration generations |
| [External MCP clients](mcp-clients.md) | Server connections, tool bridging and diagnostics |
| [Browser perception](browser-perception.md) | Plugin-owned page observations and compact rendering |

## Other subsystem entry points

- Setup and operation: [project README](../README.md), [configuration](configuration.md),
  [LLM providers](llm-providers.md), [Docker](docker.md), [macOS](install-macos.md),
  [desktop shell](tauri-shell.md), [voice](voice-mode.md),
  [TTS engines](tts-engines.md).
- Conversation and memory: [prompt surfacing](prompt-surfacing.md),
  [prompt caching](prompt-caching.md), [context budget](context-budget.md),
  [memory tiers](memory-tiers.md), [memory time and quality](memory-time-quality.md),
  [archive](conversation-archive.md), [diary](diary-journal.md).
- Understanding and initiative: [concept integration](concept-integration.md),
  [concept lifecycle](concept-lifecycle.md), [hypotheses](hypotheses.md),
  [belief interpretations](belief-interpretations.md), [cue pool](cue-pool.md),
  [idle workers](idle-workers.md), [room](aiko-room.md).
- Contributor rules and live debugging: [AGENTS.md](../AGENTS.md),
  [subsystem conventions](../rules/code-conventions.md),
  [MCP debug server](../rules/mcp-server.md), [logs](../rules/debugging.md).
