# Configuration reference

This is the human-facing map of every knob Aiko exposes via
`config/default.json` (shipped) and `config/user.json` (your local
overrides). Drift between this doc and `app/core/infra/settings.py` is
expensive — the
[`config-documentation` rule](../.cursor/rules/config-documentation.mdc)
exists to keep them in lock-step.

> **How to read an entry**
> `- ` `` `key_name` `` *(type, default)* — what it controls. Higher → effect on Aiko. Lower → effect on Aiko.
>
> Section paths reflect the JSON block, e.g. `agent.shared_moments_enabled`
> means the `shared_moments_enabled` field inside the `"agent": { ... }`
> block of `config/default.json`.
>
> Per-section dataclass: `app/core/infra/settings.py`. Each section header below
> names the dataclass it loads into.

> **How to change values**
> `config/default.json` is the shipped baseline; do **not** hand-edit it
> for personal preferences. Drop your overrides in `config/user.json` —
> a deep merge runs at load time so you only need to include the keys you
> want to change. The Settings drawer in the UI rewrites
> `config/user.json` for you.

---

## Cheatsheet — the knobs you'll actually want to turn

| Goal | Knob | Default |
|---|---|---|
| Make Aiko speak faster / slower (global) | `assistant.tts_length_scale` | `1.0` (0.65 fastest – 1.35 slowest) |
| Set / change your name | `assistant.user_display_name` | `""` (forces first-run onboarding) |
| Cap reply length (stop rambling) | `llm.routes.main_chat.max_tokens` | `512` |
| Shrink the KV cache to fit VRAM | `llm.routes.<role>.context_window` | `65536` |
| Keep model warm in VRAM longer | `llm.providers[].keep_alive` | `"30m"` |
| Aiko proactively speaks in **voice** chat after N s silence | `agent.proactive_silence_seconds` | `45` |
| Aiko proactively speaks in **typed** chat after N s silence | `agent.proactive_silence_seconds_typed` | `240` (4 min) |
| Enable typed-mode proactive at all | `agent.proactive_typed_enabled` | `true` |
| Speak typed-mode proactive lines (TTS) | `agent.proactive_typed_tts_enabled` | `false` |
| Forward foreground app name (desktop) | `agent.activity_awareness_enabled` | `false` |
| Share the real-world weather/season | `agent.weather_sync_enabled` | `false` |
| Your weather location (city) | `weather.location_name` | `""` |
| Live2D body-language intensity | `avatar.expressiveness` | `1.0` (0.0–1.5) |
| Live2D outfit override | `avatar.auto_outfit` | `"auto"` |
| Live2D model scale | `avatar.scale_multiplier` | `1.0` |
| Switch the unified grounding line on/off | `agent.grounding_line_mode` | `"off"` (`"replace"` / `"split"` / `"off"`) |
| Closeness ceiling (consent dial: reserved ↔ affectionate) | `agent.intimacy_ceiling` | `0.7` (0.0–1.0) |
| Master switch for Aiko's long-term goals | `agent.goals_enabled` | `true` |
| Hedge old / decayed memories with "(faded)" suffix | `memory.fade_hedge_enabled` | `true` |
| Reinforce "Aiko remembered" beats (callback detector) | `agent.callback_detector_enabled` | `true` |
| Notice when {user_name} double-checks Aiko's claims (calibration) | `agent.calibration_detection_enabled` | `true` |
| Let Aiko occasionally touch the room (sensory anchoring) | `agent.sensory_anchor_enabled` | `true` |
| Pull back when {user} goes quiet (K23 misattunement) | `agent.misattunement_detection_enabled` | `true` |
| Hedge old claims with time-language (K25 confidence decay) | `agent.confidence_time_decay_enabled` | `true` |
| Push back when she has a stance (K29 opinion injection) | `agent.opinion_injection_enabled` | `true` |
| Feel the tension when a turn nears a boundary (L18c boundary clash) | `agent.boundary_clash_enabled` | `true` |
| Don't cave on taste pushback (K46 stance persistence) | `agent.stance_persistence_enabled` | `true` |
| Long-arc callbacks "weeks ago you said…" (K63) | `agent.long_arc_callback_enabled` | `true` |
| Dormant-interest re-opener "we haven't talked about X in ages" (K67) | `agent.dormant_interest_enabled` | `true` |
| Surface "what I've been turning over" between sessions (K28) | `agent.turning_over_enabled` | `true` |
| Wall-clock prefixes on chat history (K-time1) | `agent.history_age_prefix_enabled` | `true` |
| Bridge a new conversation to the previous one (K91) | `agent.continuity_max_messages` | `6` (`0` disables) |
| Cue-register rotation (K51 de-"Heads-up") | `agent.cue_register_rotation_enabled` | `true` |
| Destructive-task approval mode | `agent.task_approval_mode` | `"ask"` (`"ask"` / `"auto"`) |
| Per-capability approval overrides | `agent.task_approval_overrides` | `{}` (e.g. `{"file_write": "auto"}`) |
| Let Aiko write files (workflow skill) | `agent.file_write.enabled` | `false` |
| Let Aiko see images (workflow skill) | `agent.vision.enabled` | `false` |
| Exact-arithmetic tool | bundled `calculator` plugin | enabled |
| Master memory switch | `memory.enabled` | `true` |
| RAG recall depth per turn | `memory.top_k` | `6` |
| Long-term memory cap | `memory.max_memories` | `5000` |
| TTS provider / voice | `tts.provider`, `tts.voice` | `pocket-tts`, `aiko1_refined.safetensors` |
| Voice mode mic on at boot | `audio.enable_microphone` | `true` |
| Enable barge-in (interrupt Aiko while she's talking) | `audio.barge_in_enabled` | `false` |
| Debug log to file | `logging.level`, `logging.file_enabled` | `INFO`, `true` |
| UI-side debug log bridge | `logging.ui_log_enabled` | `false` |

Everything else below is "tune it once when you really need to,
don't touch otherwise."

---

## `assistant` — `AssistantSettings`

Personal identity + the one global TTS knob.

- `assistant.name` *(string, `"Aiko"`)* — the assistant's name. Used in prompts and UI strings. Changing this does **not** rename the persona file; you'd also need to edit `data/persona/aiko_companion.txt`.
- `assistant.remember_history` *(bool, `true`)* — keeps the SQLite chat history. Flip off to make every session ephemeral (history wiped at shutdown).
- `assistant.user_id` *(string, `"default"`)* — scopes memory and beliefs per-user. Change this and Aiko effectively meets a new person (memories are not migrated).
- `assistant.user_display_name` *(string, `""`)* — your name as Aiko addresses you. Empty triggers the first-run onboarding modal in the UI. Single source of truth — `resolve_user_display_name()` reads this everywhere (prompts, transcripts, world-seed, persona templating).
- `assistant.tts_length_scale` *(float, `1.0`)* — global TTS speed multiplier, clamped to `[0.65, 1.35]`. **Higher → slower** speech (more "pacing"); lower → faster. Independent of any per-reaction speed jitter (`agent.tts_runtime_speed_enabled`).

---

## `llm` — `LlmSettings` (the only LLM config block)

Holds the **provider catalogue** (`llm.providers[]`), the **role
assignment table** (`llm.routes{}`) and the **embedding config**
(`llm.embedding`). Everything about which model serves which job lives
here.

> **Upgrading?** The old `ollama` and `chat_llm` blocks were retired. On
> the first boot after upgrading, [`_migrate_legacy_llm`](../app/core/infra/settings.py)
> folds them into `llm` (providers, routes and embedding), writes the
> result to `user.json` and deletes the old keys — a one-shot,
> no-action-required migration. See [llm-providers.md →
> Migrating from the legacy config](llm-providers.md#migrating-from-the-legacy-chat_llm--ollama-config).

### `llm.providers[]` — saved provider catalogue

Each entry is a slotted `LlmProvider`:

- `llm.providers[].id` *(string, required, unique)* — stable identifier used by routes. Example: `"local_ollama"`, `"openai"`, `"openai_team"`.
- `llm.providers[].name` *(string)* — display name for the catalogue list.
- `llm.providers[].kind` *(string, `"ollama"` | `"openai_compatible"`)* — wire protocol family. Anything else falls back to `"ollama"`.
- `llm.providers[].base_url` *(string)* — endpoint URL.
- `llm.providers[].api_key` *(string, `""`)* — bearer token (written via `PUT /api/llm/providers/{id}/credentials`; never round-trips through GET). Stashed in the OS keychain when one is available, leaving `""` on disk.
- `llm.providers[].api_key_env` *(string, `""`)* — env-var fallback (e.g. `"OPENAI_API_KEY"`). Inferred from the host when blank.
- `llm.providers[].extra_headers` *(object, `{}`)* — vendor-specific headers (OpenRouter wants `HTTP-Referer` + `X-Title`).
- `llm.providers[].timeout_seconds` *(int, `300`)* — HTTP timeout, shared by chat + embeddings on this endpoint. Bump if a slow model occasionally times out mid-generation.
- `llm.providers[].keep_alive` *(string, `"30m"`)* — Ollama-only model-resident-in-VRAM duration; silently ignored by remote providers. Accepts any Ollama duration (`"30m"`, `"1h"`, `"-1"` for "forever").
- `llm.providers[].reasoning_effort` *(string, `""`)* — default effort for Responses-API models (GPT-5 / o-series / Grok). Empty = the client's own default; a route can override it.
- `llm.providers[].api_style` *(string, `"auto"` | `"responses"` | `"chat_completions"`)* — OpenAI-compatible surface selector. xAI Grok needs `"responses"` for reasoning + prompt caching.
- `llm.providers[].think_num_predict_headroom` *(int, `2048`)* — Ollama-only: extra `num_predict` budget added automatically on `think=True` worker calls so the reasoning trace doesn't starve the answer.

Two routes pointing at the same provider share one `ChatClient`
instance through the cache in [`app/llm/factory.py`](../app/llm/factory.py).

### `llm.routes{}` — role assignments

Maps a role name (`"main_chat"`, `"worker_default"`, `"workflow"`) to an `LlmRoute`:

- `llm.routes[role].provider_id` *(string, required)* — references `llm.providers[].id`. Server returns 404 when unknown.
- `llm.routes[role].model` *(string, required)* — model name (for `openai_compatible`) or tag (for `ollama`). Free-text combobox in the drawer.
- `llm.routes[role].context_window` *(int | null, `65536` in the shipped defaults)* — explicit budget in tokens, used as the prompt-assembly budget and as Ollama's `num_ctx`. Resolution order is **explicit route value > active client's `get_context_length(model)` > hardcoded 8192 fallback**. **Worth setting explicitly for local models**: auto-detect asks Ollama's `/api/show` for the model's advertised maximum, and recent Qwen tags advertise 256 k — a KV cache that size spills the weights out of VRAM and Ollama silently splits the model across CPU and GPU. For remote providers auto-detect consults a static lookup table of **conservative caps** (gpt-5-mini → 131072, gemini-2.5-* → 131072, claude-3-* → 200000, … see `_CONTEXT_WINDOW_TABLE` in `app/llm/openai_compatible_client.py`); those are deliberately below the model's true max because typical use is under 50 k, bigger budgets make compaction lazy, and staying under 128 k keeps OpenAI requests in the cheaper short-context billing column. `context_window_source` on `get_status` / Diagnostics reports which branch won (`config`, `client`, or `fallback`).
- `llm.routes[role].max_tokens` *(int, `512`)* — hard cap on tokens **per reply** for this role. Without it, models routinely emit 2 k+ tokens of rambling on casual chat. **Higher → longer replies**, more chance of drift; lower → terser, more chance of mid-sentence truncation. `0` / negative disables the cap. Watch `data/app.log` for `ollama response truncated:` / `openai-compat response truncated:` warnings — they fire only when the cap actually clipped a reply.
- `llm.routes[role].temperature` *(float | null, `null`)* — sampling temperature; `null` uses the provider's default.
- `llm.routes[role].reasoning_effort` *(string, `""`)* — per-role override of the provider's value.

Editing any of the three roles takes effect immediately: `update_route`
persists the change and rebuilds the live clients, re-pointing the
`TurnRunner`, the `ProactiveDirector` and every background worker.

Pointing `main_chat` and `worker_default` at the same provider **and**
the same context window makes them share one client — on a single-GPU
box that's what keeps one set of weights resident instead of thrashing
between two.

### `llm.embedding` — the RAG embedder

Not a route: the embedder speaks a different endpoint (`/api/embeddings`)
and is always local.

- `llm.embedding.provider_id` *(string, `"local_ollama"`)* — which catalogue entry hosts it.
- `llm.embedding.model` *(string, `"qwen3-embedding:0.6b"`)* — the embedder used for RAG, beliefs, novelty, conflicts, curiosity seeds, etc. Changing this **invalidates the LanceDB** (existing vectors won't match new ones), which triggers a destructive rebuild on the next boot.
- `llm.embedding.num_ctx` *(int | null, `2048`)* — VRAM lever: `qwen3-embedding` defaults to a 32 k window (~5.8 GB resident) but Aiko only ever embeds short texts.
- `llm.embedding.num_gpu` *(int | null, `0`)* — VRAM lever: `0` forces CPU, freeing the GPU for the chat model. `null` leaves Ollama's placement alone.

---

## `agent` — `AgentSettings`

The big one. Inner-life workers, proactive nudges, summarisation, style trackers, detectors. Most "Aiko feels different lately" knobs live here.

### Proactive — voice mode

- `agent.proactive_silence_seconds` *(float, `45.0`, min `10`)* — seconds of silence in **voice** mode before `ProactiveDirector` is allowed to fire a nudge. Higher → Aiko waits longer before chiming in; lower → she gets nag-y. See `app/core/proactive/proactive_director.py`.
- `agent.proactive_cooldown_seconds` *(float, `120.0`, min `30`)* — minimum gap between two voice-mode proactive nudges. Higher → fewer back-to-back unprompted utterances.

### Proactive — typed mode

Typed-mode runs an independent timer so the cadence can differ (typing sessions tolerate longer silences than mic ones).

- `agent.proactive_typed_enabled` *(bool, `true`)* — master switch for "Aiko speaks first in typed chat." Off → typed sessions are purely user-driven.
- `agent.proactive_silence_seconds_typed` *(float, `240.0`, min `60`)* — silence threshold for typed-mode nudges (default 4 min). Higher → less likely to interrupt a heads-down session.
- `agent.proactive_cooldown_seconds_typed` *(float, `600.0`, min `120`)* — minimum gap between two typed proactive nudges (default 10 min). Higher → quieter.
- `agent.proactive_typed_when_away` *(bool, `false`)* — when `false`, typed proactive respects `_user_present` (browser visibility + Tauri focus); when `true`, Aiko can typed-chime in even when no client window is visible. Voice mode ignores this on purpose.
- `agent.proactive_typed_tts_enabled` *(bool, `false`)* — when `false`, a typed-mode proactive line is **text-only** (bubble, no speech); when `true`, it's also spoken via TTS through the same enqueue the voice path uses. Default off because a typed-silence nudge can land minutes later when you may be away from the speakers. Voice-mode proactive always speaks regardless of this flag.

### Activity awareness (desktop opt-in)

- `agent.activity_awareness_enabled` *(bool, `false`)* — forwards the foreground **app name** (never window titles or URLs) from the Tauri desktop shell so Aiko can naturally reference what you're doing. Off by default; browser shells render the toggle but can't produce a non-null active app. Privacy posture: see `docs/presence-and-activity.md`.

### Weather + season sync (H11, opt-in)

- `agent.weather_sync_enabled` *(bool, `false`)* — master switch for the **passive ambient** weather feed. On (with a resolved `weather.location_name`), a low-frequency worker pulls current conditions into a terse "shared sky" prompt cue, tints the persona-window backdrop, and can nudge the K27 daily colour + seasonal room decor. Coarse city-granularity location only, never GPS. Off by default. The on-demand weather *tools* are gated separately by `tools.weather`. Privacy posture: see `docs/weather-sync.md`.

### Mood-drift narrator (H3)

- `agent.mood_drift_enabled` *(bool, `true`)* — master switch for the slow, read-only awareness of how the user's mood (`valence`) and the four relationship axes have drifted over days/weeks. On → a daily idle-worker samples one point per local day into a small `kv_meta` ring, and a provider surfaces ONE gentle reflective cue per finding (sustained low / recovery / single-axis drift). Off → no sampling, no cue.
- `agent.mood_drift_check_interval_seconds` *(int, `3600`, min `60`)* — sampler cadence. The tick is cheap (a date compare); the sample only lands once per local day.
- `agent.mood_drift_cooldown_days` *(float, `4.0`, min `0`)* — minimum days between two surfaced notes. The per-finding signature watermark already prevents the *same* finding repeating; this guards against two *different* findings firing back-to-back.

### Shared moments + relationship axes (schema v7)

- `agent.shared_moments_enabled` *(bool, `true`)* — master switch for the whole shared-moments subsystem (inline `[[moment:]]` tags, the LLM detector, the Together tab, anniversaries). Off → `[[moment:]]` tags are still stripped from chat but never persisted.
- `agent.shared_moments_llm_enabled` *(bool, `true`)* — toggles only Track 2 (the LLM moment detector). Off → tag-emitted + manually marked moments still work.
- `agent.shared_moments_min_turn_gap` *(int, `5`, min `1`)* — minimum turns between LLM-detected moments. Higher → rarer "we just had a moment" beats.
- `agent.shared_moments_cooldown_seconds` *(float, `300.0`, min `30`)* — wall-clock cooldown between LLM moment detections. Higher → fewer moments per session.
- `agent.anniversary_surfacing_enabled` *(bool, `true`)* — renders an "a year ago today, …" inner-life block on 1mo / 3mo / 6mo / 1yr / Nyr boundaries. Off → no anniversary nudges.
- `agent.relationship_axes_enabled` *(bool, `true`)* — tracks four floats (closeness / humor / trust / comfort) and surfaces them in the prompt when any axis crosses ±0.5. Off → no axes prompt block.

### Summarisation + compaction

- `agent.summary_idle_seconds` *(float, `15.0`, min `2`)* — quiet seconds before the background summary worker runs. Higher → summaries lag further behind the live conversation; lower → CPU thrashes on every breath.
- `agent.summary_min_unsummarized_messages` *(int, `6`, min `2`)* — minimum new messages before the worker triggers. Higher → summaries cover longer chunks but are coarser.
- `agent.summary_target_tokens` *(int, `600`, min `120`)* — token cap on the produced summary. Higher → more detail preserved at the cost of more prompt tokens later.
- `agent.max_prompt_tokens_pct` *(float, `0.8`, clamped `[0.3, 0.95]`)* — when the *next* prompt would exceed this fraction of the context window, schedule an immediate compaction (don't wait for idle). Higher → more aggressive use of context, more risk of overflow; lower → compactions fire earlier, history gets squished sooner.

### Speaking-window scheduler

LLM-driven background workers run during the gap when Aiko is speaking the previous reply, so they feel "free."

- `agent.scheduler_idle_seconds` *(float, `20.0`, min `2`)* — quiet seconds before an idle drain (when no TTS is playing). Higher → workers wait longer to fire on a silent session.
- `agent.scheduler_speaking_window_grace_ms` *(int, `200`, min `0`)* — soft-close grace after TTS finishes during which jobs can still finish.
- `agent.scheduler_max_job_seconds` *(float, `8.0`, min `1`)* — advisory per-job cap. A worker exceeding this gets logged but is not killed mid-flight.

### Inner-life workers (Phase 2c onward)

- `agent.reflection_min_seconds_between` *(float, `8.0`)* — minimum gap between reflection runs. Higher → fewer reflections.
- `agent.reflection_emotional_delta_threshold` *(float, `0.05`)* — minimum |affect change| to trigger a reflection. Higher → only big mood swings reflect; lower → reflects on subtler shifts.
- `agent.user_profile_min_turns` *(int, `6`, min `1`)* — run the user-profile worker every N user turns. Higher → profile updates lag further behind reality.
- `agent.agenda_groom_every_n_turns` *(int, `8`, min `1`)* — agenda groomer cadence in user-turns. Higher → stale items linger.
- `agent.arc_update_every_n_turns` *(int, `1`, min `1`)* — conversation-arc worker cadence. `1` = every turn (it's cheap; arc tag drives expression + TTS speed).
- `agent.prepared_nudge_ttl_seconds` *(float, `600.0`, min `30`)* — how stale a prepared proactive nudge can be before `ProactiveDirector` re-synthesises.

### Filler injection

Avoids dead air on the first token by emitting a short verbal filler.

- `agent.filler_enabled` *(bool, `true`)* — master switch.
- `agent.filler_first_token_ms` *(int, `800`, min `150`)* — emit a filler if the LLM hasn't produced a first delta after this many ms. Lower → fires earlier (filler-heavy); higher → only fires on truly slow first tokens.

### Tool-pass gate (P14)

Skips the forced pre-stream tool-decision LLM pass on turns with no tool-shaped signal, cutting time-to-first-token on banter turns.

- `agent.tool_pass_gate_enabled` *(bool, `true`)* — master switch / kill-switch. `true` → turns with no tool-shaped text and no continuity signal (finished-task block, active task, previous turn used a tool) skip the decision pass entirely. `false` → restore the old always-run behaviour (use this if tool recall ever regresses; see `get_tool_gate_state` over MCP for diagnostics).

### Skills framework — progressive tool disclosure

Narrows which tools the model sees per turn instead of always shipping the whole catalogue. Both routers default off (= today's behaviour). See [skills-framework.md](skills-framework.md).

- `agent.skill_router_enabled` *(bool, `false`)* — brain-lane router. When `true`, a tool-shaped turn exposes only the matched tool families plus the always-on core, instead of every registered tool. The P14 tool families act as the brain skill-groups. Inspect the per-turn active set via `get_tool_gate_state` (`router_enabled` / `core_skills` / `last_active_tools`) over MCP.
- `agent.brain_core_skills` *(list of str, `["time", "recall", "world"]`)* — families always exposed when the brain router narrows. `world` is included so Aiko keeps taking spontaneous room actions (sip tea, shift posture) on turns whose text named no item. An empty/invalid value falls back to the default triple.
- `agent.workflow_skill_router_enabled` *(bool, `false`)* — worker-lane router. When `true`, the goal-workflow planner's skill menu is narrowed to the goal's capability group(s) (`files` / `web` / `vision` / `mcp:<server>`) before each plan, with a full-menu fallback on ambiguity or multi-group goals. Watch the planner `missing_capability` rate as the over-narrowing canary.

### Promise follow-through (K43)

Closes the loop on Aiko's own "I'll look into that" commitments. Assistant-side `kind="promise"` memories carry an `open → surfaced → fulfilled | dropped` lifecycle on metadata; an idle worker arms a one-shot "mention what you found — or own that you haven't yet" cue, and replies / finished background tasks auto-fulfil matching promises.

- `agent.promise_followthrough_enabled` *(bool, `true`)* — master switch for the worker, the cue, and the lifecycle writes.
- `memory.promise_followthrough_interval_seconds` *(int, `1800`, min `30`)* — idle-worker cadence.
- `memory.promise_followthrough_min_age_hours` *(float, `4.0`, min `0`)* — how long a promise must sit open before the cue can arm.
- `memory.promise_followthrough_cooldown_hours` *(float, `6.0`, min `0`)* — wall-clock pacing between consecutive cues.
- `memory.promise_followthrough_drop_after_days` *(float, `14.0`, min `1`)* — promises older than this silently flip to `dropped`.
- `memory.promise_fulfil_min_overlap` *(int, `3`, min `1`)* — content-word overlap a reply / task result must share with the promise body to count as fulfilled.

### Memory consolidation

`MemoryConsolidator` merges near-duplicate memory rows.

- `agent.consolidator_enabled` *(bool, `true`)* — master switch.
- `agent.consolidator_min_hours_between` *(float, `18.0`, min `0.5`)* — minimum hours between consolidation passes. Lower → more aggressive merging.
- `agent.consolidator_chunk_size` *(int, `40`, min `8`)* — max memories scanned per pass (bounds the wall-clock per pass).
- `agent.consolidator_similarity_threshold` *(float, `0.84`, clamped `[0.5, 0.99]`)* — cosine threshold for "these two memories are the same fact." Higher → merges only near-identical rows; lower → merges paraphrases more aggressively (can collapse distinct facts).
- `agent.consolidator_min_cluster_size` *(int, `2`, min `2`)* — minimum cluster size before a merge happens.
- `agent.consolidator_use_llm_merge` *(bool, `true`)* — when `true`, an LLM rewrites the merged content; when `false`, the highest-salience row wins verbatim.

### Relationship pulse (weekly)

- `agent.relationship_pulse_enabled` *(bool, `true`)* — master switch for the once-a-week LLM pass that summarises how the relationship is going as a salience-boosted memory.
- `agent.relationship_pulse_min_hours` *(float, `168.0`, min `24`)* — minimum hours between pulses (default 7 days). Lower → more frequent retrospectives.
- `agent.relationship_pulse_min_turns` *(int, `30`, min `5`)* — minimum turns since the last pulse. Higher → pulse only fires on substantial new history.
- `agent.relationship_pulse_max_tokens` *(int, `256`, min `80`)* — `num_predict` ceiling for the pulse LLM call.

### Cadence / prosody

- `agent.cadence_enabled` *(bool, `true`)* — `ProsodyDispatcher` adds micro prefixes (`"Mm."`, `"Oh,"`) and pause-style punctuation hints. Text-only; engines that ignore punctuation are safe. Off → flat delivery.
- `agent.earcon_auto_sprinkle` *(bool, `true`)* — auto-add `breath` / `soft_sigh` earcons on the first sentence of melancholy / wistful / sad turns. Cooldown-gated. Off → Aiko's inline `[[breath]]` etc. tags still play, but nothing is auto-added.
- `agent.tts_runtime_temp_enabled` *(bool, `false`)* — opt-in: let cadence mutate Pocket-TTS `model.temp` per reaction. **Off by default** because Pocket-TTS is sensitive to temperature excursions (±0.05 can produce pitch artefacts on some voices). Validate on your voice first.
- `agent.tts_runtime_speed_enabled` *(bool, `false`)* — opt-in: let cadence jitter speech speed per reaction. **Off by default** because Pocket-TTS couples speed and pitch (a 10 % faster sentence is also ~1.6 semitones higher), so per-sentence drift gets perceived as "her voice keeps changing." Validate via `tools/tts_speed_ab.py`. The global `assistant.tts_length_scale` is honoured regardless.

### Aiko style-pattern tracker (anti-rut)

Detects when **Aiko's own** recent output has fallen into a rut (same openers, every reply ends in a question, all 50+ word paragraphs). Defaults calibrated to the diagnostic captured against ~120 assistant messages.

- `agent.style_tracker_enabled` *(bool, `true`)* — master switch.
- `agent.style_tracker_window` *(int, `12`, min `2`)* — recent-turn rolling window.
- `agent.style_tracker_warmup` *(int, `6`, min `2`)* — minimum turns before any cue can fire.
- `agent.style_tracker_opener_count_threshold` *(int, `4`, min `2`)* — minimum count of a specific opener within the window before it counts toward concentration.
- `agent.style_tracker_opener_topk_share` *(float, `0.60`, clamped `[0, 1]`)* — share of the window the top-k openers must cover to trip the "you keep starting the same way" cue. Higher → cue fires only on extreme repetition.
- `agent.style_tracker_question_rate_threshold` *(float, `0.75`, clamped `[0, 1]`)* — share of replies ending in `?` that trips the "you're ending everything as a question" cue. Higher → more tolerant.
- `agent.style_tracker_avg_questions_threshold` *(float, `1.5`, min `0`)* — average questions-per-reply that trips the "you're piling on questions" cue.
- `agent.style_tracker_length_avg_threshold` *(float, `50.0`, min `1`)* — average word-count that trips the "all your replies are paragraphs" cue.
- `agent.style_tracker_anaphoric_count_threshold` *(int, `4`, min `2`)* — K88. How many replies in the window must open on a clause that hangs off his sentence ("Then…", "Exactly.", "That makes sense") before the cue fires.
- `agent.style_tracker_anaphoric_rate_threshold` *(float, `0.33`, clamped `[0, 1]`)* — and what share of the window they must be. Both gates must clear, so it stays a rate detector rather than a ban on connectives — the occasional warm one is the point, five in a row is the problem.
- `agent.style_tracker_cue_cooldown_turns` *(int, `5`, min `0`)* — turns to suppress a re-fire of the **same** style cue.

### K49 — casual speech texture

Perfectly clean prose is its own robotic tell, so the persona's **"Speech texture:"** subsection gives Aiko standing permission to use small disfluencies (`"uhm"`, `"mm"`, `"mhm"`, `"wow"`, `"oof"`) *inside* a real thought. The same subsection keeps throat-clearing ("That's a great question", "Let me think about that") banned — a disfluency sits inside a thought, throat-clearing sits in front of one and delays it.

- `agent.speech_texture_enabled` *(bool, `true`)* — gates the guidance. When `false`, the `Speech texture:` subsection is lifted out of the persona at load time, so Aiko loses the permission and reverts to clean prose. Applied to the loaded persona rather than kept as a second copy in code, so the wording stays editable in `data/persona/aiko_companion.txt`. Renaming or deleting the subsection yourself makes the toggle a no-op, which is the intended escape hatch.
- `agent.speech_texture_spoken` *(bool, `true`)* — when `false`, the **non-lexical** fillers (`uhm`, `um`, `uh`, `mm`, `mhm`, `hmm`, `er`, `eh` and their doubled spellings) are stripped from the TTS stream only; the chat transcript keeps them either way. This switch exists because Pocket-TTS has no phoneme control, so a written `"uhm"` is synthesised grapheme-by-grapheme and can land as "uh-em". Ordinary interjections (`wow`, `oh`, `huh`, `yeah`, `oof`) are real words the engine already says correctly and are **never** stripped.

Two details worth knowing about the strip: it only fires when a filler is its own clause (start of text or after clause punctuation, *and* followed by punctuation of its own), so `"it's uhm complicated"` is left alone rather than guessed at; and a reply that is entirely a filler (`"Mhm."`) passes through untouched, because that is a deliberate acknowledgement rather than a stumble.

Both keys are bound at construction (the prompt assembler and the turn runner respectively), so flipping either one needs a restart. Editing the *wording* of the subsection in the persona file does not — the persona is re-read per turn.

If the model over-corrects and sprinkles a filler into every reply, the existing style-pattern tracker's opener-rut band is the backstop — reaction words in the opener slot count as openers like any other word. Verified against a 9B, that over-correction is the likely direction: the disfluency rate settled near 75% of replies rather than the intended minority, and the model wants to put the filler in the opener slot rather than mid-thought. Tests: `tests/test_speech_texture.py`.

### K13 — Jacob-side stylometric mirror

Emits a "How Jacob is writing today: terser than usual, drier than usual" directive so Aiko's register follows his. Five axes: terseness / punctuation / playfulness / slang / question. No embedder, no LLM. **Eligible on every turn** (including aggressive context-mode) because register is the first thing aggressive mode wants to preserve — but it only *renders* when an axis has actually moved, which on the reference corpus is 12% of turns.

Each axis is scored as a deviation from the user's **own rolling baseline**, not against an absolute bar. That is deliberate and it is why there is one knob here instead of five: an absolute bar can only produce a constant on a stable writer. The previous build rendered on 99.7% of 2018 turns, said one of three things, and changed what it said four times in twelve weeks — see `docs/personality-backlog/health.md` H21.

- `agent.style_signal_enabled` *(bool, `true`)* — master switch.
- `agent.style_signal_window` *(int, `30`, min `2`)* — the "lately" window, in recent user turns.
- `agent.style_signal_warmup_min` *(int, `8`, min `2`)* — minimum turns before the window reports at all. Separately, no axis speaks until 60 turns of baseline exist.
- `agent.style_signal_sensitivity` *(float, `3.0`, clamped `[1, 10]`)* — how many baseline standard errors an axis must move before it is named. Higher → quieter and more selective; `3.0` lands near 12% of turns on the reference corpus. Consecutive windows overlap heavily, so this reads stricter than a textbook 3-sigma; going below `2.0` narrates sampling noise as a change in register.

### K14 — implicit engagement signals (latency + length)

Per-turn detector that scores Jacob's reply latency + message length against rolling baselines and routes the signal to **two consumers** depending on mode:

- **Voice mode**: latency + length contribute to a small `closeness_delta` that rides into [`RelationshipAxesUpdater.apply_turn`](../app/core/relationship/relationship_axes.py) on the same turn (snappy replies nudge closeness up; long voice gaps + curt messages nudge it down).
- **Typed mode**: latency is intentionally **NOT** consumed as engagement — typed pauses are thinking time, not disengagement. Instead, a gap landing in the configured band (default 30 min – 4 h) feeds the one-shot **absence-curiosity** inner-life cue on the *next* user turn ("welcome them back warmly without making them feel like they owe you an account of their time"). A label of `"abandoned"` (steep latency *and* curt message) also suppresses the typed proactive nudge.

Latency baseline is voice-only (typed turns never touch the latency window); length baseline is shared with the K13 stylometric mirror via `StyleSignalAnalyzer.recent_word_counts()` (no duplicate buffer).

- `agent.engagement_tracker_enabled` *(bool, `true`)* — master switch. Off → no closeness drift, no absence-curiosity cue, no engagement-based proactive gating.
- `agent.engagement_window` *(int, `12`, min `2`)* — rolling voice-latency window size.
- `agent.engagement_warmup_min` *(int, `6`, min `2`)* — minimum samples before either signal scores (length warms from K13's larger window, latency warms from this one).
- `agent.engagement_latency_z_strong_drop` *(float, `1.5`, min `0.1`)* — z-score at which voice latency contributes the full per-turn cap (its "strong disengagement" threshold). Higher → stricter.
- `agent.engagement_length_z_strong_drop` *(float, `-1.0`, max `-0.1`)* — z-score at which below-baseline message length contributes the full per-turn cap. **Negative by design**; values closer to 0 mean stricter (fewer curt messages trigger).
- `agent.engagement_closeness_delta_max` *(float, `0.04`, clamped `[0, 0.08]`)* — hard cap on the per-turn closeness contribution. Sits inside the existing axes-updater `_MAX_DELTA = 0.08` so reaction-tag + moment-vibe channels still dominate.
- `agent.engagement_absence_curiosity_enabled` *(bool, `true`)* — typed-mode absence-curiosity cue master switch.
- `agent.engagement_absence_curiosity_min_seconds` *(float, `1800.0`, min `60`)* — lower bound on the typed gap (default 30 min). The upper bound is `agent.resume_opener_min_hours` × 3600 (default 4 h) — gaps larger than that route through the existing resume-opener path instead.
- `agent.engagement_proactive_gate` *(bool, `true`)* — when on, an `"abandoned"` engagement label hard-skips the typed silence-break nudge (the absence-curiosity cue handles it on the next user turn instead). Set to `false` to ignore the engagement label on the proactive path.

### G4 — cue outcome accounting

Records one row per *armed* worker cue per turn — armed meaning it had material waiting, not that a worker ran — marking whether it reached the prompt and, when it did not, which mechanism refused it. Read it with the `get_cue_outcomes` MCP tool. Purely a recorder: nothing consumes the ratio to change behaviour (that is G5).

- `agent.cue_accounting_enabled` *(bool, `true`)* — master switch. Off → the `cue_decisions` table stops growing and surfaced cues stop appearing in the L37 ledger; no behavioural change beyond losing the measurement.

### K90 — prompt-block accounting

The wider sibling of G4: one row per prompt block that actually *rendered*, per turn, rather than only the ~15 registered cues and only when armed. It exists so "how often does this steer fire" is answerable for the ~120 blocks that were never cues. Read it with `scripts/lead_follow_report.py` or the Diagnostics panel. Purely a recorder.

- `agent.prompt_block_accounting_enabled` *(bool, `true`)* — master switch. Off → the `turn_prompt_blocks` table stops growing and the report's firing-rate section goes empty; the text metrics are unaffected, since those are computed from the message log.

### K5 — mood shell tilt

Per-turn one-line emotional directive derived from the live [`AffectState`](../app/core/affect/affect_state.py) (valence + arousal) and [`RelationshipAxesState`](../app/core/relationship/relationship_axes.py) (closeness / humor / trust / comfort). Output reads like a stage direction — *"Lean affectionate and unhurried; let warmth show."* / *"Stay playful and quick; the room is laughing."* / *"Slow your tempo; let the words land before pushing forward."* — and colours Aiko's delivery (pacing, sentence length, warmth, word choice) **without** dictating content.

Empty on the common turn — only fires when affect is off-baseline AND/OR a relationship axis crosses `mood_shell_axis_threshold`. Part of the K16 `replace` suppression set (the unified grounding line folds the same surface area); kept active in `split` and `off` modes.

- `agent.mood_shell_enabled` *(bool, `true`)* — master switch. Off → no `Tone shell:` line ever renders.
- `agent.mood_shell_axis_threshold` *(float, `0.5`, clamped `[0, 1]`)* — minimum absolute axis value (closeness / humor / trust / comfort) for an axis to colour the tilt rule selection. Mirrors `relationship_axes._NOTABLE_THRESHOLD` so the "axis is notable" gate is consistent across the relationship-axes line and the mood-shell tilt.

### K17 — clarification-repair detector

Regex classifier that fires when Jacob signals he was misunderstood. Off the hot path; the next turn's inner-life block tells Aiko "you missed his last point — re-read and answer what was actually asked."

- `agent.clarification_repair_enabled` *(bool, `true`)* — master switch. Off → no cue surfaces.

### K8 — affect rupture-and-repair

Fires when Jacob's valence drops sharply between pre- and post-turn affect snapshots **and** Aiko's prior reaction wasn't already empathetic. Next turn renders a "Heads-up: their mood just dipped right after your last reply" cue.

- `agent.rupture_repair_enabled` *(bool, `true`)* — master switch.
- `agent.rupture_valence_drop_threshold` *(float, `0.12`, clamped `[0, 2]`)* — minimum valence drop that counts as a rupture. Higher → fires only on big mood swings; lower → fires on subtler dips. `0.12` sits comfortably above the `AffectUpdater` smoothing-noise floor.

### K45 — mood inertia (instant face, lagging heart)

Fires post-turn when the fresh `[[reaction:X]]` tag's implied affect target strongly outruns the pre-impulse smoothed `AffectState`. The next turn renders a one-shot "your face just jumped to X, but underneath you're still Y — let the words catch up" cue; the Live2D renderer also damps non-mouth expression amplitude proportionally to the same mismatch (mouth params — lipsync ids + the grin overlay — are never damped so talking animation and TTS pauses stay intact).

- `agent.mood_inertia_enabled` *(bool, `true`)* — master switch for the prompt-cue half.
- `memory.mood_inertia_mismatch_threshold` *(float, `0.45`, floor `0.1`)* — effective mismatch (whiplash bonus included) at or above which the cue arms. Higher → only extreme face/feeling gaps fire.
- `memory.mood_inertia_cooldown_turns` *(int, `3`, floor `0`)* — post-turn assessments skipped after a fire so one big swing doesn't nag on consecutive turns.
- `avatar.mood_inertia_damping` *(bool, `true`)* — avatar half: `ExpressionChannel` scales non-mouth expression params by `1 − 0.45·mismatch` (floored at `0.55`). Rides the `avatar_settings_changed` WS payload like `expressiveness`.

### K51 — cue-register rotation

Inner-life cue producers all emit lines opening with the literal `Heads-up:`. At prompt-assembly time the prefix is rotated across four register shapes (`Heads-up:` / `Quiet note:` / `Noticing:` / bare) on a deterministic per-turn seed, so the model never reads the same coach template several times in one prompt. Producers are untouched; the rotation lives entirely in `PromptAssembler`. A shared-prefix lint (`cue-lint:` INFO line when >2 blocks open with the same two words) runs regardless of the switch.

- `agent.cue_register_rotation_enabled` *(bool, `true`)* — master switch. Off → cue blocks land byte-identical to their producer output (literal `Heads-up:`), useful for A/B comparison. No prompt-cache impact either way: the rotated blocks live in the uncached T5/T6 prompt tail.

### Resume opener

- `agent.resume_opener_min_hours` *(float, `4.0`, min `0`)* — when the gap since the last assistant turn exceeds this, schedule a one-shot "welcome back" line. `0` disables.
- `agent.resume_opener_ttl_seconds` *(float, `1800.0`, min `60`)* — TTL applied to the prepared resume nudge (default 30 min) so it survives until you actually start a session.

### Dream worker

Bootstrap-time reflection that fires once per app start when the gap since the last assistant turn is large.

- `agent.dream_worker_enabled` *(bool, `true`)* — master switch.
- `agent.dream_worker_min_hours_since_last` *(float, `6.0`, min `0`)* — minimum offline-gap hours before the dream worker runs at boot.
- `agent.dream_hot_cluster_enabled` *(bool, `true`)* — **K65e.** Add the day's most recently-active K9 clusters to the dream seed ("threads that kept coming up lately: …") so the dream lands on a real recent topic. Off / cold graph → the dream seeds from summary + callbacks + self memories only. Flavour-only: cluster labels never trigger a dream by themselves.
- `agent.dream_hot_cluster_recency_days` *(float, `3.0`, min `0`)* — **K65e.** A cluster counts as part of "the day's" activity only when its newest member is no older than this many days.

### Catchphrase miner

- `agent.catchphrase_miner_enabled` *(bool, `true`)* — promotes 3–7-word phrases recurring N+ times across both user and assistant turns, surfaced via the "running jokes" inner-life block.
- `agent.catchphrase_miner_min_seconds_between` *(float, `600.0`, min `30`)* — minimum wall-clock between miner runs.
- `agent.catchphrase_miner_min_new_user_turns` *(int, `6`, min `1`)* — minimum new user turns since the last run.
- `agent.catchphrase_miner_min_total_count` *(int, `3`, min `2`)* — minimum total occurrences of a phrase before it's promoted to a catchphrase.

### Inside-joke birth (K80)

The miner's fast path. The slow miner above only sees a phrase once it has *recurred* across a window; K80 catches the live moment a bit is born — the user handing one of Aiko's own phrases back to her, laughing — arms a one-shot "that's officially a thing now" cue, and promotes the phrase into the same catchphrase registry.

- `agent.inside_joke_birth_enabled` *(bool, `true`)* — master switch. Off → no detection, no cue, no write.
- `agent.inside_joke_birth_cooldown_hours` *(float, `24.0`, min `0`)* — wall-clock gap between blessings. Rarity is the point: a genuinely funny hour should produce one blessed bit, not a run of them.
- `agent.inside_joke_birth_min_words` *(int, `3`, min `2`)* — shortest echo that can count as a bit. Below three words the "phrase" is usually just shared vocabulary.

### Voice adoption (K26)

The slow counterpart to K13 (register calibration): phrases that started as *his* drift into Aiko's own speech over months. A daily worker reads the catchphrase registry, keeps only rows whose provenance says the user said it first, and — rarely — promotes one into a small prompt block. Defaults are deliberately slow; the beat only works if it is invisible per session and obvious over months.

- `agent.voice_adoption_enabled` *(bool, `true`)* — master switch. Off → the worker never runs and the block stays empty (already-adopted phrases are kept, just not surfaced).
- `agent.voice_adoption_interval_seconds` *(int, `86400`, min `60`)* — sweep cadence.
- `memory.voice_adoption_min_age_days` *(float, `14.0`, min `0`)* — how long a phrase must have been in the catchphrase registry before Aiko can take it on. A phrase from one intense evening is a mood, not a habit.
- `memory.voice_adoption_min_days_between` *(float, `10.0`, min `0`)* — minimum wall-clock between two adoptions. Picking up three phrases in a week is mimicry, not absorption.
- `memory.voice_adoption_max_adopted` *(int, `3`, min `1`)* — ceiling on the active adopted set. Past a handful she stops sounding like herself.
- `memory.voice_adoption_max_rendered` *(int, `2`, min `1`)* — how many of them the prompt block names at once (newest first).

### Phase-4c curiosity worker

One-line follow-up question prep when the recent conversation has gone shallow.

- `agent.curiosity_worker_enabled` *(bool, `true`)* — master switch.
- `agent.curiosity_worker_min_turns_between` *(int, `3`, min `1`)* — minimum turns between candidate emissions.
- `agent.curiosity_worker_min_seconds_between` *(float, `60.0`, min `0`)* — wall-clock cooldown.
- `agent.curiosity_worker_max_user_word_count` *(int, `8`, min `1`)* — only fires when the recent user turns are this short on average (signal that the conversation has gone shallow).
- `agent.curiosity_worker_cluster_anchor_enabled` *(bool, `true`)* — **K65c.** Anchor the follow-up on a known-but-quiet K9 interest (the most-dormant established cluster) instead of echoing the user's literal last words. Falls back to the legacy literal-words prompt when no quiet interest is available (cold / non-persistent graph). Off → pure legacy anchoring.
- `agent.curiosity_worker_quiet_days` *(float, `7.0`, min `0`)* — **K65c.** How many days a cluster's newest member must be old for it to count as "quiet" and eligible as a re-anchor target. Higher → only reach back to long-dormant interests.
- `agent.curiosity_subject_quota` *(float, `0.40`, clamped `[0, 1]`)* — **K87.** Share of what the three curiosity generators draft (`CuriosityWorker`, `CuriositySeedWorker`, `ForwardCuriosityWorker`) that must be about a *subject* rather than about the user. Enforced as a running deficit rather than a coin flip, so the ratio holds across the handful of drafts a day these workers make. `0` restores the pre-K87 behaviour where every note was a question about him waiting to happen; `1` means she never drafts one again, which is its own failure.

### F1 — background fact-checker

- `agent.fact_checker_enabled` *(bool, `true`)* — master switch. Off → the claim queue still persists but the worker never runs.
- `agent.fact_checker_per_hour_cap` *(int, `10`, min `0`)* — hourly cap on web-search queries the worker can issue. Token-bucket persisted to `kv_meta`.
- `agent.fact_checker_per_day_cap` *(int, `50`, min `0`)* — daily cap.

### G2 — schedule learner

- `agent.schedule_learner_enabled` *(bool, `true`)* — master switch for the `usual_hours` profile-field writer.
- `agent.schedule_learner_min_samples` *(int, `5`, min `1`)* — minimum user messages in the window before the worker writes anything. Higher → fresh DBs stay silent longer; lower → claims a schedule from less data.
- `agent.schedule_learner_window_days` *(int, `30`, min `1`)* — rolling window the bucketing scan considers. Higher → smoother but slower to react to a routine change.

### K3 — routine / ritual awareness

Second pass inside `ScheduleLearner` that names recurring slots ("Sunday-morning chats").

- `agent.routine_detection_enabled` *(bool, `true`)* — disable just K3; G2 still writes `usual_hours`.

### G3 — idle curiosity worker

Web-searches `open_question` memories during idle windows.

- `agent.idle_curiosity_enabled` *(bool, `true`)* — master switch.
- `agent.idle_curiosity_per_hour_cap` *(int, `2`, min `0`)* — hourly cap on web searches. Strictly tighter than the fact-checker so a multi-week absence + a backlog of open questions can't dump a wall of "I was reading about" beats on return.
- `agent.idle_curiosity_per_day_cap` *(int, `6`, min `0`)* — daily cap.

### F8 / F9 / K61 — interest-driven knowledge enrichment

F9 is the `idle_knowledge` worker: on an idle tick it reads the K9 topic graph, scores under-researched interest clusters (coverage-weighted, so one big topic can't monopolise it), runs a small worker-LLM **research planner** that judges whether a cluster has an evergreen, impersonal subject worth researching (skipping relationship/feeling/plan-only clusters and advancing to the next candidate in the same tick) and emits up to a few neutral search queries from the cluster's member memories, web-searches one, distils one or two impersonal, evergreen facts (F8 `knowledge` memory kind), and writes them silently. The planner's extra queries are queued so the cluster is mined from fresh angles when it next comes up. F8 boosts those `knowledge` rows in retrieval on informational turns and tags them `(learned)`. K61 is the per-turn inner-life steer that, on question turns, nudges Aiko to commit to the learned specifics instead of survey-hedging. None of this adds an LLM turn to the chat path — F9 runs on the worker model in idle windows, and K61 costs only a local regex + embed + cosine scan.

- `agent.knowledge_enrichment_enabled` *(bool, `true`)* — master switch for the F9 worker. Off → the worker never registers, no web searches, no `knowledge` rows written.
- `agent.knowledge_enrichment_per_hour_cap` *(int, `1`, min `0`)* — hourly cap on F9 web searches (its own `FactCheckRateLimiter` budget keyed `idle_knowledge.rate_state`, separate from F1/G3). Deliberately tight — this is slow, ambient learning, not a research sprint.
- `agent.knowledge_enrichment_per_day_cap` *(int, `4`, min `0`)* — daily cap.
- `agent.knowledge_topic_extraction_enabled` *(bool, `true`)* — master switch for the research planner. Off → the worker falls back to the legacy path (privacy-scrub the cluster summary and search that verbatim), with no researchability judgement and no query queue.
- `agent.knowledge_grounding_enabled` *(bool, `true`)* — master switch for the K61 inner-life block. Off → learned facts still surface through F8 retrieval, but the "commit to specifics, don't hedge" steer is silent.
- `memory.knowledge_enrichment_interval_seconds` *(int, `3600`, min `60`)* — F9 worker cadence.
- `memory.knowledge_cluster_cooldown_hours` *(int, `72`, min `0`)* — per-cluster wall-clock cooldown so the worker rotates across interests instead of grinding one. Stamped on every run (even a no-result / privacy-gated one).
- `memory.knowledge_enrichment_max_per_cluster` *(int, `3`, min `0`)* — a cluster already holding this many `knowledge` rows is skipped (it's researched enough).
- `memory.knowledge_enrichment_max_clusters_per_run` *(int, `3`, min `1`)* — how many ranked candidate clusters a single tick may try before giving up. When the planner judges the top cluster unresearchable it advances to the next rather than wasting the tick.
- `memory.knowledge_research_queries_per_cluster` *(int, `3`, min `1`)* — max impersonal queries the planner may emit per cluster. One is researched per tick; the rest are queued.
- `memory.knowledge_unresearchable_cooldown_hours` *(int, `336`, min `0`)* — long cooldown applied to a cluster the planner deems unresearchable, so a personal-only cluster doesn't re-burn a planner call every few days.
- `agent.knowledge_gap_notice_enabled` *(bool, `true`)* — F10f: master switch for the self-aware **knowledge-gap notice** — the "I keep circling X but never actually dug into it" beat. Independent of F9 `knowledge_enrichment_enabled` (which silently *researches* the same dense, low-`knowledge`-coverage clusters): this one only controls whether Aiko ever *voices* the gap. Off → the `KnowledgeGapNoticeWorker` never registers and the inner-life provider stays empty. The worker (no LLM — a cheap kv pass) drafts a notice for the strongest gap cluster during quiet windows; the T6 provider surfaces it only when the live turn is lexically on that topic, once per topic. The cue is a private prompt hint — Aiko phrases the admission herself, never verbatim.
- `memory.knowledge_gap_notice_interval_seconds` *(int, `3600`, min `60`)* — gap-notice worker cadence.
- `memory.knowledge_gap_notice_min_size` *(int, `5`, min `2`)* — a cluster must have at least this many members to count as a recurring theme worth admitting ignorance about.
- `memory.knowledge_gap_notice_max_knowledge_fraction` *(float, `0.15`, clamped `[0, 1]`)* — upper bound on a cluster's `knowledge`-row fraction for it to still read as a gap. At/below this the topic is "barely researched"; above it Aiko already knows enough that the admit-the-gap beat would be a lie.
- `memory.knowledge_gap_notice_topic_cooldown_hours` *(int, `72`, min `0`)* — per-topic cooldown so a drafted/voiced gap isn't re-raised for a while. Keyed on a stable hash of the cluster label (survives cluster renumbering).
- `memory.knowledge_gap_notice_journal_max` *(int, `6`, min `1`)* — size of the kv journal ring of drafted notices.
- `agent.topic_temperature_enabled` *(bool, `true`)* — F10h: master switch for **topic temperature** (per-cluster affect). When on, a turn that lands on a *charged* topic cluster gets a one-line tonal Heads-up so Aiko meets a **warm** topic (good moments live there) with a little fondness and a **tender** one (vulnerable / patched-up ground) gently instead of flat. Off → the inner-life provider stays empty. Computed **live in the provider** (no worker, no kv, no schema): the cluster's temperature is scored from its `shared_moment` member vibes — the one affect signal cleanly attributable to a cluster. Warm vibes (`warm`/`playful`/`silly`/`proud`/`milestone`/`gift`/`victory`/`creative`) lift warmth; tender vibes (`tender`/`vulnerable`/`comfort`/`repair`) lift tenderness; both saturate. The cue is a private register nudge — Aiko never says "this is tender for us" out loud. (K57 emotion episodes and K32 reactions are deferred — global / not cleanly cluster-attributable.)
- `memory.topic_temperature_min_sim` *(float, `0.45`, clamped `[0, 1]`)* — minimum centroid cosine for the live turn to count as "on" a cluster before its temperature is considered. Keeps the nudge from firing on a loose, incidental brush with a cluster.
- `memory.topic_temperature_threshold` *(float, `0.5`, clamped `[0, 1]`)* — a cluster's dominant pole (warmth or tenderness, both in `[0, 1]`) must reach this for the cue to surface. Higher → only strongly-charged topics nudge tone.
- `memory.topic_temperature_cooldown_turns` *(int, `6`, min `0`)* — global cooldown (in turns) after a temperature cue fires, so a charged topic isn't re-nudged every turn it comes up.
- `agent.topic_mood_origin_enabled` *(bool, `true`)* — H8: rides on top of F10h to give a charged topic an **origin story**. When on, the first time a cluster reads warm / tender the provider stamps the shared moment that *gave* it that feel into the `aiko.topic_mood_origin` kv side-table (keyed by cluster id), and appends an "ever since: …" clause to the tonal cue so Aiko can name the cause once, gently ("this has stayed soft for me ever since you told me about your dad") rather than just the mood. The origin is stable across fires and re-stamps only if the pole flips (warm→tender). Off → the bare warm / tender cue still fires, just without the origin clause.
- `agent.topic_confidence_enabled` *(bool, `true`)* — F10i: master switch for the **per-topic confidence self-model** (a topic-scoped extension of K20 metacognitive calibration). When on, a turn that lands on a *thin* topic cluster nudges Aiko to admit she doesn't know much yet and ask rather than bluff; a *rich* one nudges her to stop over-hedging on what she clearly knows. Off → the inner-life provider stays empty. Computed **live in the provider** (no worker): confidence is a saturating blend of cluster size (conversational familiarity) and learned-fact coverage (`kind` in `knowledge` / `curiosity_finding`). Distinct from F10f (which owns the *dense-but-unresearched* "I keep circling X" beat — those score mid/high here, so they never read as thin) and from K61 knowledge-grounding (which pushes *specific facts* — the familiar band here is an anti-over-hedge register cue only). The cue is a private register nudge, never said aloud.
- `memory.topic_confidence_min_sim` *(float, `0.45`, clamped `[0, 1]`)* — minimum centroid cosine for the live turn to count as "on" a cluster before its confidence is judged (mirrors the temperature gate).
- `memory.topic_confidence_thin_threshold` *(float, `0.25`, clamped `[0, 1]`)* — confidence at/below which the topic reads as *thin* ground (hedge / ask). Genuinely small clusters; F10f owns dense-but-thin.
- `memory.topic_confidence_familiar_threshold` *(float, `0.7`, clamped `[0, 1]`)* — confidence at/above which the topic reads as *familiar* ground (stop over-hedging). Rich clusters with real learned-fact coverage.
- `memory.topic_confidence_cooldown_turns` *(int, `6`, min `0`)* — global cooldown (in turns) after a confidence cue fires.
- `agent.upcoming_horizon_enabled` *(bool, `true`)* — K-time3: master switch for the **upcoming-horizon block**. When on, a cheap forward sweep over `future_plan` memories due within the horizon window renders one terse "coming up" cue with the relative times **already resolved** by `timephrase.humanize_future` ("tomorrow morning 09:00", "on Friday 18:00") so the chat model never recomputes a future date (the thing LLMs reliably get wrong). Off → the inner-life provider stays empty. The cue re-surfaces immediately when the upcoming set changes (a plan appears or passes) and otherwise sits out a per-turn cooldown so an unchanged calendar isn't recited every turn. Computed **live in the provider** (no worker): one mirror scan + a couple of ISO parses. A heads-up, not a calendar readout.
- `memory.upcoming_horizon_days` *(int, `7`, min `1`)* — how far ahead the forward sweep looks for `future_plan` events. Higher → further-out plans surface (but the resolved phrasing gets fuzzier, e.g. "next week"). Lower → only imminent plans surface.
- `memory.upcoming_horizon_max_items` *(int, `3`, min `1`)* — max number of upcoming events listed in the cue, soonest-first. Higher → a fuller list (risks reading like a calendar). Lower → only the very next thing.
- `memory.upcoming_horizon_cooldown_turns` *(int, `6`, min `0`)* — cooldown (in turns) before the *same* set of upcoming plans is re-surfaced; a changed set always re-surfaces immediately. Higher → the heads-up nags less. Lower → it resurfaces more often for an imminent event.
- `agent.session_clock_enabled` *(bool, `true`)* — K-time4: master switch for the **session-clock block** (within-session time awareness, distinct from the cross-session gap family). When on, a cheap derived signal off the recent-message timestamps surfaces two one-shot sub-cues: how long the current *continuous sitting* has run ("we've been at this a while") and a notable *mid-session pause* ("you stepped away a bit and came back"). Off → the inner-life provider stays empty. Computed **live in the provider** (no worker), sharing the recent-history read with the other inner-life walkers. Tonal guard in the rendered cue: observe, never police.
- `agent.session_clock_long_minutes` *(float, `60.0`, min `1`)* — continuous-sitting duration at/above which the elapsed cue reads `long` ("about an hour"). Fires once per band per sitting.
- `agent.session_clock_very_long_minutes` *(float, `150.0`, min `1`)* — duration at/above which the elapsed cue escalates to `very_long` ("a couple of hours"), re-surfacing once even after the `long` cue already fired this sitting.
- `agent.session_clock_break_minutes` *(float, `30.0`, min `1`)* — a gap between consecutive messages longer than this ends the current sitting (a fresh burst starts), so the elapsed clock measures the active sitting rather than wall-clock session age. Re-arms the per-band one-shot.
- `agent.session_clock_gap_min_minutes` *(float, `10.0`, min `0`)* — lower bound on a notable mid-session pause. Pauses shorter than this are ignored.
- `agent.session_clock_gap_max_minutes` *(float, `30.0`, min `0`)* — upper bound on a notable mid-session pause; sits at the K14 absence_curiosity floor so K-time4 never double-fires with the gap-return family that owns everything above it.
- `memory.knowledge_grounding_min_similarity` *(float, `0.45`, clamped `[0, 1]`)* — K61 cosine threshold; a learned fact must be at least this close to the question to surface.
- `memory.knowledge_grounding_max_items` *(int, `2`, min `1`)* — K61 max bullets surfaced per turn.

### F10j — cluster-scoped memory hygiene

- `agent.cluster_scoped_memory_hygiene_enabled` *(bool, `true`)* — F10j: scope the F5 conflict detector **and** the K35 consolidation worker to *within* topic-graph clusters. When on, each worker partitions its candidate snapshot by cluster (`TopicGraph.cluster_id_for`) and runs its all-pairs cosine inside each group instead of across the whole mirror — turning `O(n²)` into `sum(O(k²))` (the P30 scaling win) and keeping only topically-adjacent pairs, where contradictions / near-dupes actually live. Off → both workers fall back to the full all-pairs sweep. No effect until the topic graph is warm / persistent (degrades to the full sweep automatically; the legacy behaviour is byte-identical). **Tradeoff:** a pair whose members landed in different clusters is no longer compared — rare in practice (the clustering floor 0.55 is far looser than the conflict band `[0.80, 0.92)` and the ~0.90 dedupe threshold, so close pairs almost always co-cluster) and eventually-consistent across re-clusters. The per-run `groups` + `cluster_scoped` fields on each worker's result/log line show whether scoping was active.

### F10k — semantic topic tracking for K6 / K18

- `agent.topic_tracking_enabled` *(bool, `true`)* — F10k: when on, the K6 novelty detector maps each measured turn to its best topic-graph cluster (via `best_clusters_for`, reusing the vector it already embeds) and the K6/K18 inner-life cues gain a private, don't-quote context clause: a *return* to a previously-visited cluster reads as "circles back to the X thread — pick it up, not brand-new", a fresh move reads as "shift from X to Y", and K18's lull cue names the looped-on topic. **Additive only** — the centroid band classification is untouched, so K6/K18 fire on the same turns; clusters just enrich the rendered text. Off → the detectors run byte-identically to pre-F10k. Bound at detector construction, so toggling needs a restart.
- `memory.topic_tracking_min_sim` *(float, `0.30`, clamped `[0, 1]`)* — minimum cluster-centroid cosine for a turn to count as confidently "on" a cluster. Below this the turn has no cluster identity and the prior cluster is retained (a transient miss must not read as a topic change).

### F5 — conflicting-memory detector

- `agent.conflict_detector_enabled` *(bool, `true`)* — master switch.
- `agent.conflict_detector_per_hour_cap` *(int, `6`, min `0`)* — hourly cap on LLM verification calls.
- `agent.conflict_detector_per_day_cap` *(int, `30`, min `0`)* — daily cap.

### L9 — concept contradiction detector (living beliefs)

Counter-evidence that lowers an *active* identity concept's confidence and can step it into a revivable `contradicted` status (see [`concept-lifecycle.md`](concept-lifecycle.md)). The L3 lifecycle worker stays the single writer; the detector is a read-only input that reuses the F5 three-tier gate (cosine band → `classify_pair` → LLM YES/NO for borderline). Checks ride L3's rolling batch, so they inherit its round-robin cadence.

- `memory.concept_contradiction_enabled` *(bool, `true`)* — master switch. Off → L3 runs exactly as before (no detector, no `contradicted` transitions).
- `memory.concept_contradiction_batch_size` *(int, `20`, min `1`)* — max **active** concepts contradiction-checked per lifecycle tick; rotates across ticks via `last_lifecycle_at` so a large active set never blocks a tick.
- `memory.concept_contradiction_max_candidates` *(int, `6`, min `1`)* — near memories pulled per concept as counter-evidence candidates.
- `memory.concept_contradiction_similarity_min` / `_max` *(float, `0.6` / `0.95`, clamped `[0, 1]`)* — cosine band a candidate memory must fall in to be considered. Wider than F5's memory↔memory band because the concept side is an abstract label; the band is only a filter — agree-vs-contradict is decided by `classify_pair` / the LLM.
- `memory.concept_contradiction_penalty` *(float, `0.25`, clamped `[0, 1]`)* — confidence dropped per confirmed contradiction, plasticity-damped (a sticky / low-plasticity belief resists disproof).
- `memory.concept_contradicted_confidence_floor` *(float, `0.4`, clamped `[0, 1]`)* — once a contradicted belief's confidence falls below this it flips `active → contradicted` (kept above the dormant floor so "disproven" reads distinctly from "faded"); otherwise it stays active but weakened.
- `agent.concept_contradiction_per_hour_cap` *(int, `6`, min `0`)* — hourly cap on LLM verification calls for *borderline* pairs (definite heuristic hits skip the LLM). Uses its own `FactCheckRateLimiter` (`state_key='concept_contradiction.rate_state'`) so it never shares a budget with F5.
- `agent.concept_contradiction_per_day_cap` *(int, `30`, min `0`)* — daily cap.

### L15 — concept belief revision (concept → supporting-memory re-check)

When L9 flips a belief to `contradicted`, the doubt flows **back down** to the memories that supported it. L3 persists a `concept --contradicts--> memory` edge and hands the concept to the read-mostly [`ConceptBeliefReviser`](../app/core/concepts/concept_belief_reviser.py), which arbitrates — per supporting memory — one of three resolutions: **(a) inaccurate** → lower its confidence; **(b) superseded** → reclassify to `past_event` with a fresh `relevance_until` (confidence untouched); **(c) keep** → no memory write. A cheap `classify_pair` gate keeps the LLM off memories that don't conflict; pinned memories are never touched. L3 stays the single writer of *concept* state — the reviser writes only *memory* state, like F1 / F5. See [`concept-lifecycle.md`](concept-lifecycle.md).

- `memory.concept_belief_revision_enabled` *(bool, `true`)* — master switch. Off → L3 still writes the `contradicts` edge but never re-examines supporting memories.
- `memory.concept_belief_revision_batch_size` *(int, `5`, min `1`)* — max concepts whose supporting memories are re-examined per lifecycle tick (rotates via `last_lifecycle_at`), so a burst of contradictions never blocks a tick.
- `memory.concept_belief_revision_max_evidence` *(int, `6`, min `1`)* — max supporting memories re-examined per concept.
- `memory.concept_belief_revision_confidence_penalty` *(float, `0.2`, clamped `[0, 1]`)* — confidence dropped from a memory judged **inaccurate** (resolution a).
- `memory.concept_belief_revision_confidence_floor` *(float, `0.2`, clamped `[0, 1]`)* — the (a) cut never takes a memory below this floor (a concept never zeroes an observation).
- `memory.concept_belief_revision_superseded_relevance_days` *(float, `7.0`, min `0`)* — grace window for a **superseded** memory (resolution b): `relevance_until = now + this` before the stale-but-true fact slides out of normal RAG.
- `agent.concept_belief_revision_per_hour_cap` *(int, `6`, min `0`)* — hourly cap on the 3-way arbitration LLM calls (the `classify_pair` gate runs first, so only genuine conflicts spend budget). Uses its own `FactCheckRateLimiter` (`state_key='concept_belief_revision.rate_state'`) so it never shares a budget with L9 / F5.
- `agent.concept_belief_revision_per_day_cap` *(int, `30`, min `0`)* — daily cap.

### L16 — concept plasticity (movement governor)

`plasticity` (`[0, 1]`, per concept) is the single learning rate the L3 engine damps *every* confidence move by — decay (`halflife *= 2 - p`), accrual (step `= 0.5 + 0.5*p` of the gap to target), L9 disproof, and the L15 revision cut — so a sticky, low-plasticity core trait resists change in both directions. `p = 1` reproduces the pre-L16 full snap / full penalty, so only sub-1 kinds slow. Plasticity is stamped once, on a concept's first lifecycle eval, from the per-kind `ConceptKind.plasticity_default` band (see [`concept-lifecycle.md`](concept-lifecycle.md)).

- `memory.concept_identity_plasticity` *(float, `0.3`, clamped `[0, 1]`)* — the `identity` kind's default band (low = sticky). Applied on first eval; also damps decay + L9 disproof + L15 revision for identity concepts.
- `memory.concept_default_plasticity` *(float, `0.5`, clamped `[0, 1]`)* — fallback band for any kind that registers no `plasticity_default` on the `ConceptKind` registry.

Three further mechanisms let plasticity *move* rather than stay stamped (all default on, each independently switchable):

- **Relationship modulation** (live, at eval time) — a kind that opts in via its registry `plasticity_modulation` (only `boundary` today) has its *effective* plasticity raised by the live trust + relationship-duration signal, loosening a boundary as the bond deepens; the stored base is never touched. Each modulation records a `signal:relationship_trust --influences--> concept` edge and, on a band cross, emits a `plasticity_shift` event ("never silently").
  - `memory.concept_plasticity_modulation_enabled` *(bool, `true`)* — master switch for the eval-time modulation (and its edge/event bookkeeping).
  - `memory.concept_plasticity_duration_days_full` *(float, `180.0`, min `1`)* — days-known at which the relationship-duration term saturates to `1.0`.
  - `memory.concept_plasticity_shift_event_delta` *(float, `0.1`, clamped `[0, 1]`)* — how far the lift must move from the last recorded `influences` strength before a `plasticity_shift` event fires (bands the event stream).
- **Plasticity-drift** (one-way, persisted) — a settled *active* concept's stored plasticity is nudged **down** toward a floor as its confidence and engaged age grow (stickier with time). Skipped on first eval so the kind band lands first.
  - `memory.concept_plasticity_drift_enabled` *(bool, `true`)* — master switch for the stored-plasticity drift.
  - `memory.concept_plasticity_drift_rate` *(float, `0.05`, clamped `[0, 1]`)* — per-eval drift step scale (higher = firms up faster).
  - `memory.concept_plasticity_drift_floor` *(float, `0.15`, clamped `[0, 1]`)* — the stickiest plasticity drift can reach; it never drops below this.
- **Re-check slowdown** — a sticky (low effective-plasticity) concept is probed for L9 contradictions on a plasticity-scaled stride (`stride = 1 + round(k·(1 − eff_plast))`), skipping intermediate ticks *without* consuming the per-tick contradiction budget, so core beliefs are re-examined less often.
  - `memory.concept_plasticity_recheck_slowdown_enabled` *(bool, `true`)* — master switch; off → every active concept is eligible each tick as before.
  - `memory.concept_plasticity_recheck_stride_k` *(float, `3.0`, min `0`)* — stride steepness; `0` → stride always `1` (no slowdown) even when enabled.

### L17a — concept trajectory (silent-decay sampling)

The event timeline logs *transitions*, so a belief that decays for months without crossing a status floor leaves no trace of the slide. L3 closes that blind spot by appending a `confidence_sample` event when a concept that emitted nothing else this tick has drifted a full band from the confidence at its last recorded event (either direction). The baseline then advances to the sample, so a long fade logs once per band rather than once per tick. Read back with `ConceptEventStore.trajectory(concept_id)` (oldest-first) or `GET /api/concepts/timeline?concept_id=…`. See [`concept-lifecycle.md`](concept-lifecycle.md#reading-one-concepts-trajectory-l17a).

- `memory.concept_confidence_sample_enabled` *(bool, `true`)* — master switch. Off → only transitions reach the timeline, as before.
- `memory.concept_confidence_sample_band` *(float, `0.1`, clamped `[0.01, 1]`)* — how far confidence must move from the last recorded event before a sample fires. Smaller = finer trajectory, more rows.

### L17 — concept evolution (relabelling, learning events, reflections)

L17a records *that* a belief moved; L17 records **how it changed and why**, durably. The off-turn [`ConceptDriftWorker`](../app/core/concepts/concept_drift_worker.py) is the single writer of `label` / `rationale` (L3 keeps `confidence` / `plasticity` / `status`): L2 stages a better wording as a `relabel_proposed` event, the worker gates and adjudicates it, then rewrites the row and appends an immutable `relabeled` event. Salient changes land in the append-only `concept_learning_events` table (v31) with old/new endpoints, a natural-language *because*, and evidence labels snapshotted at detection time; `concept_aliases` keeps a merged-away concept's history reachable. Read it back at Settings → Memory → Evolution, `GET /api/concepts/learning`, `GET /api/concepts/{id}/provenance`, or the `get_concept_*` MCP tools. See [`concept-lifecycle.md`](concept-lifecycle.md#concept-evolution-l17).

Worker cadence and scan bounds:

- `memory.concept_drift_enabled` *(bool, `true`)* — master switch for the whole L17 worker (classification **and** relabelling). Off → labels stay frozen and no learning events accrue; existing history still reads.
- `memory.concept_drift_interval_seconds` *(int, `3600`, min `300`)* — minimum gap between passes. `demand()` only compares a KV watermark against the newest event id, so an idle timeline costs nothing.
- `memory.concept_drift_max_concepts` *(int, `120`, min `1`)* — concepts examined per pass. Bounds the one matrix snapshot and the single matmul that does all succession pairing.
- `memory.concept_drift_trace_anchor` *(int, `20`, min `0`)* / `memory.concept_drift_trace_recent` *(int, `60`, min `1`)* — the two-ended trajectory read: how many oldest (origin) and newest (movement) events per concept. Recent must stay comfortably above the `confidence_sample` rate or a long-lived belief's recent moves get buried.

What counts as a real change:

- `memory.concept_drift_min_salience` *(float, `0.35`, clamped `[0, 1]`)* — floor for persisting a finding as a learning event. Salience is plasticity-weighted, so equal movement in a sticky belief outranks it in a `taste` / `conduct` row.
- `memory.concept_drift_min_age_days` *(float, `3.0`, min `0`)* — a concept must be at least this old before its movement is treated as evolution rather than settling.
- `memory.concept_drift_min_confidence_delta` *(float, `0.15`, clamped `[0, 1]`)* — confidence movement below this is noise, not a story.
- `memory.concept_drift_max_findings` *(int, `12`, min `1`)* — cap on learning events written per pass.

Succession pairing (the primary shape — a new concept forming below the dedupe bar while the old one fades):

- `memory.concept_drift_succession_min_cosine` *(float, `0.55`, clamped `[0, 1]`)* / `memory.concept_drift_succession_max_cosine` *(float, `0.86`, clamped `[0, 1]`)* — the band. The ceiling is the `_DEDUPE_COS` dedupe bar: above it the two rows would have merged, so they cannot be a succession.
- `memory.concept_drift_succession_min_overlap` *(float, `0.25`, clamped `[0, 1]`)* — minimum shared evidence between the fading and rising rows.
- `memory.concept_drift_succession_window_days` *(float, `120.0`, min `1`)* — how far apart the fade and the rise may sit and still be paired.

Relabelling gates (all cheap, all checked before any LLM call):

- `memory.concept_relabel_enabled` *(bool, `true`)* — off → proposals are still staged as events but never applied, so labels stay frozen.
- `memory.concept_relabel_min_cosine` *(float, `0.80`, clamped `[0, 1]`)* — the new wording must stay this close to the current label. Below it, this is a *different belief* and belongs in its own concept, not a rename.
- `memory.concept_relabel_cooldown_days` *(float, `21.0`, min `0`)* — per-concept cooldown between rewrites.
- `memory.concept_relabel_max_per_run` *(int, `3`, min `1`)* — applied relabels per pass; `memory.concept_relabel_scan_limit` *(int, `40`, min `1`)* — pending proposals examined per pass.
- `memory.concept_drift_relabel_min_tokens` *(int, `1`, min `1`)* — minimum token-level difference (after normalising case, punctuation, filler and simple plurals) for a rewording to count as material. Raise it if cosmetic churn gets through.
- `agent.concept_relabel_per_hour_cap` *(int, `3`)* / `agent.concept_relabel_per_day_cap` *(int, `12`)* — shared rate limiter on the adjudication LLM calls.

Note there is no "refuse a previously-held label" knob: that guard reads the `label` snapshots already in `concept_events` and is always on, which is what kills a phrasing ping-pong after one round trip.

Rare learning reflections (the one place this reaches the conversation):

- `agent.concept_learning_reflection_enabled` *(bool, `true`)* — master switch for the T6 `concept_learning_block`. Off → the history stays a debugging surface only.
- `memory.concept_drift_pending_cap` *(int, `3`, min `1`)* — how many pending changes the worker keeps on the KV shelf the turn path reads. Deliberately tiny: prompt assembly must never scan. The shelf holds the most significant **unreported** changes rather than the latest ones — the worker runs daily and the reflection speaks monthly, so overwriting each run meant the change she got to mention was decided by which day the cooldown lifted. New findings compete with what is already shelved, the strongest `cap` survive, and firing the reflection takes that entry off.
- `memory.concept_drift_pending_ttl_days` *(float, `45.0`, min `1`)* — how long a change may sit unsaid before it leaves the shelf. Bounds the squatting the keep-the-strongest rule otherwise allows; re-observing a change does **not** refresh its age, so this measures how long it has gone unmentioned rather than how long it has kept recurring.
- `memory.concept_reflection_min_salience` *(float, `0.6`, clamped `[0, 1]`)* — higher than the persistence floor above, so most recorded changes are never spoken.
- `memory.concept_reflection_min_axes` *(float, `0.3`, clamped `[0, 1]`)* — minimum relationship trust, and warmth, before she'll say her read on him changed.
- `memory.concept_reflection_cooldown_days` *(float, `30.0`, min `0`)* — persisted global cooldown, on top of once-per-conversation and a per-change watermark. A month is deliberate and was re-affirmed after the audit: this is the one place the learning history speaks, and at eleven times a year it stays an event rather than a tic. What the audit did change is *which* change gets the slot — see the shelf note above.

Force one for testing with the `concept_learning_force_next` debug override; it bypasses the trust, relevance and cooldown gates for a single turn.

The cold-start sweep. `concept_drift_max_concepts` bounds each pass, but the event watermark used to advance to the global maximum regardless — so on a store with more concepts than one pass can examine, historical events were skipped and then marked processed. A second cursor pages by concept id, independent of the watermark, and backfills after the forward passes have run. It drains itself on the normal cadence and then writes a done sentinel; `GET /api/concepts/drift` and the MCP `get_concept_drift_state` report its progress. `POST /api/concepts/drift/run` burns through a backlog in minutes.

- `memory.concept_drift_sweep_enabled` *(bool, `true`)* — off → only new events are ever classified. Leave it on until the sweep reports done; after that it costs one query per pass.
- `memory.concept_drift_sweep_page` *(int, `60`, min `1`)* — concepts per sweep page.
- `memory.concept_drift_sweep_max_findings` *(int, `24`, min `1`)* — learning events written per sweep pass. Separate from `concept_drift_max_findings` because that cap (12) would throttle a multi-week backfill to twelve events an hour.

### L17f — the evolution diary

A periodic, browsable "here is how I've changed", composed from the same learning events and kept deliberately separate from the H9 subjective diary. [`EvolutionDiaryWorker`](../app/core/concepts/evolution_diary_worker.py) reads salient events above its watermark and writes **one** short first-person paragraph per period into the `evolution_diary` table (v32), grounded strictly in the stored `because` prose and carrying the concept and event ids it drew on. Read it at Settings → Memory → Evolution (above the feed), `GET /api/concepts/evolution-diary`, or the `get_evolution_diary` / `force_evolution_diary` MCP tools.

- `agent.evolution_diary_enabled` *(bool, `true`)* — off skips the worker; the history it would narrate keeps accruing, so turning it on later resumes from the current watermark.
- `memory.evolution_diary_interval_seconds` *(int, `86400`, min `60`)* — worker cadence. The cooldown below is what actually paces entries.
- `memory.evolution_diary_min_events` *(int, `3`, min `1`)* — the anti-filler floor. Below it the period writes nothing **and leaves its events pending**, so two thin weeks can still add up to one entry worth reading.
- `memory.evolution_diary_min_salience` *(float, `0.45`, clamped `[0, 1]`)* — which changes are worth telling.
- `memory.evolution_diary_cooldown_days` *(float, `7.0`, min `0`)* — minimum gap between entries. Spent even when the model returns nothing, so an unproductive period costs a period rather than looping on the same material.

### L19 — self-history (the autobiography)

Asked "have you changed?", Aiko walks her own record rather than improvising. [`self_history.py`](../app/core/concepts/self_history.py) builds eras of classified change (flipped / faded / revived / born / settled) across every concept including retired ones, and the `recall_self_history` tool narrates them. Inspect the exact payload the model receives at Settings → Memory → **Story**, `GET /api/concepts/self-history?subject=aiko`, or the MCP `get_self_history`.

- `tools.recall_self_history` *(bool, `true`)* — the tool gate. It only registers when the concept and learning stores are both wired.

There is no salience or cadence knob here: this is a pull-side read with no worker. The one field that governs behaviour is `thin_record` in the payload — set by the builder when the trail is too sparse to narrate, which obliges her to say she has no record rather than invent a past. `settled` beliefs do not count toward a record being substantive: having beliefs is not the same as having changed.

### L17d — self-correction rules (learning from her own mistakes)

The step past "this belief changed": when several of Aiko's corrections happen for the *same reason* across *different* beliefs, that is a fact about how she works. [`self_correction.py`](../app/core/concepts/self_correction.py) clusters the `because` clauses and the proposer names the habit as an actionable rule, stored as a `communication_style` concept with `subject="aiko"` and `evidence_model="meta"` — one `("concept", prior_id)` edge per belief it was learned from. Because it is an ordinary concept of an ordinary kind, it steers behaviour through the existing T3 relevant-context path once L3 promotes it.

- `agent.concept_self_correction_enabled` *(bool, `true`)* — off → no rules are proposed; the learning events keep accruing. **Not** the same switch as `agent.self_correction_enabled`, which is K38's in-reply "I got that wrong" cue.
- `memory.concept_self_correction_evidence_floor` *(int, `3`, min `2`)* — how many **distinct beliefs** a pattern must span. Counting beliefs rather than events is what stops one concept she keeps flip-flopping on from minting a rule about her character.
- `memory.concept_self_correction_min_span_days` *(float, `7.0`, min `0`)* — the corrections must be spread over at least this long, so one afternoon's mood cannot read as a tendency.
- `memory.concept_self_correction_min_salience` *(float, `0.5`, clamped `[0, 1]`)* — which corrections are considered at all.
- `memory.concept_self_correction_similarity` *(float, `0.55`, clamped `[0, 1]`)* — single-link cosine threshold over the `because` clauses. Raise it if unrelated corrections get grouped.
- `memory.concept_self_correction_cooldown_days` *(float, `14.0`, min `0`)* — the anti-oscillation lever, and the one to reach for first: it outlasts fresh history, so she cannot rewrite her working strategy weekly however much accrues.
- `memory.concept_self_correction_max_events` *(int, `200`, min `10`)* — corrections read per pass; `memory.concept_self_correction_max_rules` *(int, `2`, min `1`)* — rules proposed per pass. Several at once is not learning, it is a rewrite.

### L13 — affective concepts (topic → durable affect)

The `affective` concept kind (both subjects) captures the durable topic→emotion signature — what energizes/drains the user, and how topics move Aiko — surfaced as tone guidance via the T3 relevance path (never pinned, never said aloud). It is fed by a post-turn per-cluster affect sampler ([`cluster_affect`](../app/core/concepts/cluster_affect.py) EWMA maps, one per subject, keyed by topic `cluster_id`) plus `metadata.affect` stamping on Aiko's `self`/`reflection`/`diary` writes; a `"affect"` synthesis population + two proposers name the pattern. See [`personality-backlog/concepts.md` → L13](personality-backlog/concepts.md). The `affective` kind uses `plasticity_default=0.5` (the fluid band) and `affective_evidence_gate` (floors the `set` gate at ≥2 sources / ≥0.5 days / ≥0.6 confidence).

- `agent.affect_sampler_enabled` *(bool, `true`)* — master switch for the per-turn affect sampler **and** the self-memory affect stamping. Off → no topic→affect signal accrues, so the affective proposers stay silent (existing concepts still surface).
- `memory.concept_synthesis_affect_min_samples` *(int, `3`, min `1`)* — how many affect-bearing turns a cluster (or aiko self-theme) must accrue before it is offered to the affective proposers, so a one-off mood never becomes a durable claim.
- `memory.affect_sampler_min_sim` *(float, `0.4`, clamped `[0, 1]`)* — minimum cosine to the live turn's cluster for the sampler to attribute this turn's affect to it.
- `memory.affect_sampler_top_n` *(int, `1`, min `1`)* — how many top matching clusters a turn's affect is folded into.
- `memory.affect_sampler_learning_rate` *(float, `0.2`, clamped `[0.01, 1]`)* — EWMA alpha for the rolling per-cluster valence/arousal.
- `memory.cluster_affect_map_cap` *(int, `200`, min `1`)* — max clusters kept per subject's affect map (most-recently-updated win).
- `memory.cluster_affect_max_age_days` *(float, `120.0`, min `1`)* — affect-map entries older than this are dropped on write (self-heals across topic-graph refits).

### L7 — relationship rituals (recurring shared moments)

The `ritual` concept kind (`subject=relationship`) names the recurring "thing you two do" — mined from `shared_moment` memories, not the topic graph — and surfaces as warm relationship colour via the T3 relevance path (never pinned, never announced). A pure single-link cosine grouping ([`ritual_grouping`](../app/core/concepts/ritual_grouping.py)) clusters moments into recurring groups (annotated with a dominant vibe + weekday hint); a `"shared_moments"` synthesis population + the [`relationship_ritual`](../app/core/concepts/proposers/relationship_ritual.py) proposer name each group. See [`personality-backlog/concepts.md` → L7](personality-backlog/concepts.md). The `ritual` kind uses `plasticity_default=0.4` and `ritual_evidence_gate` (floors the `set` gate at ≥3 moments / ≥1 day / ≥0.65 confidence).

- `agent.ritual_synthesis_enabled` *(bool, `true`)* — master switch for the ritual synthesis pass. Off → no relationship rituals are mined (the rest of concept synthesis is unaffected; existing rituals still surface).
- `agent.shared_value_synthesis_enabled` *(bool, `true`)* — master switch for the **shared-value** pass, which reads the same moment groups and asks what the pair treats as *mattering* rather than what they repeatedly do ([`value_relationship`](../app/core/concepts/proposers/value_relationship.py), `kind=value` / `subject=relationship`). A new shared value must draw on moments from **two distinct groups**; a principle visible in only one recurring activity is that activity named twice, and that rule is what keeps this pass from restating the one above. Separate switch and separate dirty-tracking watermark from `ritual_synthesis_enabled`, so turning either off leaves the other alone.
- `memory.concept_synthesis_ritual_min_moments` *(int, `6`, min `2`)* — minimum `shared_moment` rows before the ritual pass runs at all (below this there aren't enough moments for a recurring pattern).
- `memory.concept_synthesis_ritual_group_min_size` *(int, `3`, min `2`)* — minimum members a moment cluster needs to be a ritual candidate (a couple of moments isn't a recurring pattern).
- `memory.concept_synthesis_ritual_group_similarity` *(float, `0.45`, clamped `[0, 1]`)* — single-link cosine threshold for joining two shared moments into the same ritual group, **on the mean-centered scale** (see the note below). Was `0.6` on raw vectors, which linked 95% of all pairs and returned the whole corpus as one group. Measured after centering: `0.45` → 7 rituals, `0.40` → 6 but with a 30-member group re-forming, `0.50` → 3.
- `memory.concept_synthesis_max_ritual_groups` *(int, `3`, min `1`)* — cap on ritual groups offered to the proposer per run (bounds the prompt / LLM cost).

**Both shared-moment passes mean-center their vectors before comparing, so their thresholds are not on the same scale as any raw cosine elsewhere in the system.** Every shared moment is about the same two people being affectionate, and that common direction dominates: raw pairwise cosine averaged 0.608 on a real 145-moment corpus, with **95% of all pairs clearing 0.6**. Single-link only needs one chain of edges to merge two groups, so L7 returned the entire corpus as one component and minted exactly one ritual concept. [`ritual_grouping.center_vectors`](../app/core/concepts/ritual_grouping.py) projects the corpus mean out (same corpus: mean −0.006, p90 0.165); L29a's arc grouper calls the same helper. Fixing the `shared_moment` embedding basis was a prerequisite but **not sufficient on its own** — re-embedding alone still left 56% of pairs above 0.6 and one 142-member group. Centering is skipped only when the corpus is within float error of a single direction.

### L8 — narrative arcs (closed causal chains)

The `narrative` concept kind (`subject=user` **and** `aiko`) names a *closed causal arc* — an ordered chain of episodic memories collapsed into one story ("The Great 13900KS Investigation"; for aiko, first-person "the stretch where I learned to hold a gentle stance"). It is the first **`sequence`**-evidence kind: the chain order is stored on `concept_edges.ordinal` (0..n), derived from each candidate cluster's member memories ordered by `event_time` (fallback `created_at`). A `"narrative"` synthesis population + `_run_narrative_pass(subject)` feeds the shared [`propose_narrative`](../app/core/concepts/proposers/base.py) body ([`narrative_user`](../app/core/concepts/proposers/narrative_user.py) third-person / [`narrative_aiko`](../app/core/concepts/proposers/narrative_aiko.py) first-person), which names only **closed** arcs and surfaces them via the T3 relevance path (never pinned, never recited). See [`personality-backlog/concepts.md` → L8](personality-backlog/concepts.md). The `narrative` kind uses `plasticity_default=0.3` and `narrative_evidence_gate` (floors the gate at ≥3 ordered chain steps / ≥1 day / ≥0.6 confidence). A narrative is **not** a rolling "what have we been up to lately" digest (that's the conversation summary's job); relationship + meta narratives are deferred to L29.

- `agent.narrative_synthesis_enabled` *(bool, `true`)* — master switch for the narrative synthesis pass. Off → no arcs are mined (the rest of concept synthesis is unaffected; existing arcs still surface).
- `memory.concept_synthesis_narrative_min_chain` *(int, `3`, min `2`)* — minimum ordered steps a cluster must resolve to before it's offered as a candidate arc (and the proposer's new-arc floor) — a story needs a beginning, middle, and end.
- `memory.concept_synthesis_max_narrative_clusters_per_run` *(int, `3`, min `1`)* — cap on candidate arcs offered to the narrative proposer per run, per subject (bounds the prompt / LLM cost).
- `memory.concept_synthesis_max_narrative_memories` *(int, `40`, min `2`)* — cap on member memories loaded per candidate arc (long-running themes stay bounded; the proposer further elides the middle).

### L29a — episodic shared arcs (the "both of us" narrative)

The third narrative subject: a `narrative` concept with `subject=relationship` naming a *closed joint project* ("the month they rebuilt the memory system"). Same kind, gate, plasticity and relevance-only surfacing as L8, so the only real difference is the source — episodes cut out of the `shared_moment` stream by [`shared_arc_grouping`](../app/core/concepts/shared_arc_grouping.py) rather than sourced from topic clusters. An episode grows while the next moment is within `shared_arc_similarity` of its running centroid **and** within `shared_arc_gap_days` of its last member; it must reach `shared_arc_min_chain` moments and then have been quiet for `shared_arc_quiet_days` before it can be proposed. A `"shared_arc"` synthesis population + `_run_shared_arc_pass` feeds the [`narrative_relationship`](../app/core/concepts/proposers/narrative_relationship.py) proposer (third-person plural voice). See [`personality-backlog/shipped/concepts.md` → L29a](personality-backlog/shipped/concepts.md).

**Shared moments are embedded from their bare summary, not the rendered `"Shared moment (<vibe>): <summary>"` content.** The prefix is identical on every row, so embedding it made the topic graph cluster moments by *vibe word* instead of by topic, which starved arcs, L7 rituals and moment RAG alike. Vibe travels as a structured field. Rows written before this fix need [`scripts/reembed_shared_moments.py`](../scripts/reembed_shared_moments.py) (dry-run by default, `--apply` to write, app stopped), followed by a topic-graph rebuild.

**The arc grouper mean-centers via the same [`center_vectors`](../app/core/concepts/ritual_grouping.py) helper as L7** — see the note under L7 for the measurements. Uncentered, 74% of pairs cleared 0.55 and every threshold from 0.55 to 0.80 produced one snowballing 83-to-132 member episode.

- `agent.shared_arc_synthesis_enabled` *(bool, `true`)* — master switch for the shared-arc pass. Off → no joint arcs are mined; the L8 user/aiko arcs keep running.
- `memory.concept_synthesis_shared_arc_min_chain` *(int, `3`, min `2`)* — minimum moments an episode needs to be offered as an arc (and the proposer's new-arc floor).
- `memory.concept_synthesis_shared_arc_similarity` *(float, `0.45`, clamped `[0, 1]`)* — cosine floor for a moment joining an episode's running centroid, **on the mean-centered scale** (see the shared note under L7). Measured on a 145-moment corpus: `0.45` → 5 readable threads, `0.40` → 8, `0.35` → 14 and chaining starts to return. It matches the ritual default only because both were calibrated on the same corpus — the passes are independent and tuning one does not imply the other.
- `memory.concept_synthesis_shared_arc_gap_days` *(float, `10.0`, min `0.5`)* — how long a thread may go quiet and still count as the same episode. Past this the arc has ended and a resumption starts a fresh one.
- `memory.concept_synthesis_shared_arc_quiet_days` *(float, `3.0`, min `0`)* — how long an episode must have been finished before it is proposed. A project still in motion is not a closed arc.
- `memory.concept_synthesis_max_shared_arc_episodes` *(int, `3`, min `1`)* — cap on episodes offered to the proposer per run (bounds the prompt / LLM cost).

### L14 — aspiration / trajectory concepts (+ momentum callbacks)

The `aspiration` concept kind (`subject=user` **and** `aiko`) is the open-ended sibling of L8 narrative — the **second `sequence`**-evidence kind. It names a *direction* someone is moving in ("building toward a self-hosted life"; for aiko first-person "growing into someone he can rely on") rather than a closed arc, and is distinct from Aiko's concrete K1 goals. It reuses the L8 ordinal chain machinery via the shared [`propose_ordered_concept`](../app/core/concepts/proposers/base.py) body (`gate_flag="directional"` vs narrative's `"closed"`); a `"aspiration"` synthesis population + `_run_aspiration_pass(subject)` shares a `_ordered_candidates` helper with narrative, adding a minimum evidence **span** so a trajectory covers time. `plasticity_default=0.4`, `aspiration_evidence_gate` floors at ≥3 ordered steps / **≥3 days** age (a sustained direction) / ≥0.6 confidence. Surfaces relevance-only through the T3 path **and** via a proactive momentum check-in. See [`personality-backlog/concepts.md` → L14](personality-backlog/concepts.md).

- `agent.aspiration_synthesis_enabled` *(bool, `true`)* — master switch for the aspiration synthesis pass. Off → no trajectories are mined (the rest of concept synthesis is unaffected; existing aspirations still surface).
- `memory.concept_synthesis_aspiration_min_chain` *(int, `3`, min `2`)* — minimum ordered steps a cluster must resolve to before it's offered as a candidate trajectory (and the proposer's new-aspiration floor).
- `memory.concept_synthesis_aspiration_min_span_days` *(float, `14.0`, min `0`)* — minimum number of days the ordered evidence must span before a cluster is offered — a direction has to persist over time, not just accumulate in one sitting.
- `memory.concept_synthesis_max_aspiration_clusters_per_run` *(int, `3`, min `1`)* — cap on candidate trajectories offered to the aspiration proposer per run, per subject.
- `memory.concept_synthesis_max_aspiration_memories` *(int, `40`, min `2`)* — cap on member memories loaded per candidate trajectory.

Proactive momentum callbacks — the `AspirationMomentumWorker` occasionally drafts a private "check in on where they're heading" cue over an active aspiration that has gone stale since it was last reinforced, and `_render_aspiration_momentum_block` surfaces it as a watermark-gated T6 hint the chat model phrases in-context (cue producer, never verbatim):

- `agent.aspiration_momentum_enabled` *(bool, `true`)* — master switch for the momentum worker + its prompt block.
- `memory.aspiration_momentum_interval_seconds` *(float, `21600.0`, min `60`)* — idle cadence for the momentum worker (the natural global spacing between check-ins).
- `memory.aspiration_momentum_cooldown_days` *(float, `10.0`, min `0`)* — per-concept cooldown so a check-in rotates across the active aspirations instead of repeating the strongest one.
- `memory.aspiration_momentum_min_confidence` *(float, `0.6`, min `0`)* — confidence bar an aspiration must clear to be eligible for a check-in.
- `memory.aspiration_momentum_staleness_min_days` *(float, `7.0`, min `0`)* — how long since last reinforcement before an aspiration is worth revisiting.
- `memory.aspiration_momentum_journal_max` *(int, `4`, min `1`)* — cap on the `aiko.aspiration_momentum` cue ring.

### L18 — boundary concepts

The `boundary` concept kind (`subject=user` **and** `aiko`) names a *behaviour-gating* line — soft and guiding, never a refusal ("go gentler about his work when he's stressed"; for aiko first-person "I won't fake agreement just to please him"). It is the first kind mined from a **hybrid of topic clusters + explicit remembered anchors** (`self_tagged` about the user; `self`/`reflection`/`diary` about her). A `"boundary"` synthesis population + `_run_boundary_pass(subject)` feeds two proposers ([`boundary_user`](../app/core/concepts/proposers/boundary_user.py) / [`boundary_aiko`](../app/core/concepts/proposers/boundary_aiko.py)) sharing a `propose_boundary` body whose **composition rule** (`>= 1` anchor OR `>= 2` clusters) lets a **single deliberate anchor** seed a boundary — the L3 `boundary_evidence_gate` correspondingly *overrides* the source floor to 1 (age `0.5d` + confidence `0.65` floors still apply). It joins the always-on core lane (`core_always_on=True`, `core_min_confidence=0.8`) and surfaces via the T3 `relevant_context` path under a soft `_concept_boundary_header`. Surfacing is composite-scored per kind (context + confidence + recency; boundary weights recency higher). See [`personality-backlog/concepts.md` → L18](personality-backlog/concepts.md).

- `agent.boundary_synthesis_enabled` *(bool, `true`)* — master switch for the boundary synthesis pass. Off → no boundaries are mined (the rest of concept synthesis is unaffected; existing boundaries still surface).
- `memory.concept_synthesis_max_boundary_memories` *(int, `24`, min `1`)* — cap on explicit-anchor memories offered to the boundary proposer per run, per subject (topic clusters ride the shared `concept_synthesis_max_clusters_per_run` cap; this bounds only the anchor batch).
- `agent.boundary_evidence_broadening_enabled` *(bool, `true`)* — **L18e.** Fold automatically-extracted `preference` memories into the **user** anchor pool alongside the deliberate `self_tagged` anchors, so a limit the user stated but never had saved can still be noticed. Off → only deliberate anchors are offered, exactly as L18 shipped. The aiko pool is unaffected either way. Note that widening the pool does **not** widen what it takes to mint: since **L46** the composition rule accepts one *deliberate* anchor **or** two sources of any kind, and a `preference` row is not deliberate — granting it the single-source path had taken boundary intake from 46 new rows in a month to 97. Automatic rows are also offered to the proposer under their own "OTHER STATED PREFERENCES" heading rather than under "deliberate anchors", which had been vouching for evidence nobody vouched for.

### L23 follow-on — communication-style concepts

The `communication_style` concept kind (`subject=user` **and** `aiko`) is a **self-authored delivery-style** line — how the conversation should *feel* rather than what it is about (reply detail level, lead vs follow, hedging/confidence, warmth vs terseness), **bound to the context it applies to** ("explain code in depth with examples when we talk programming"). It is the delivery vehicle for progressively lightening the hard-coded persona: mined from the conversation and surfaced through the same T3 `relevant_context` region so Aiko conforms to the user over time. Like `boundary` it is a **hybrid** mined from topic clusters + explicit remembered anchors (`self_tagged` about the user; `self`/`reflection`/`diary` about her), additionally **guided (never grounded)** by a persisted *style-signal digest* — the K13 `style_signal` labels + the distilled `user_profile.communication_style` field (the digest steers *what* style to name; a concept still needs real cluster/memory evidence). A `"comm_style"` synthesis population + `_run_comm_style_pass(subject)` feeds two proposers ([`communication_style_user`](../app/core/concepts/proposers/communication_style_user.py) / [`communication_style_aiko`](../app/core/concepts/proposers/communication_style_aiko.py)) sharing a `propose_communication_style` body whose **composition rule** (`>= 1` anchor OR `>= 2` clusters) lets a **single deliberate anchor** seed a line — the L3 `communication_style_evidence_gate` correspondingly *overrides* the source floor to 1 (age `0.5d` + confidence `0.65` floors still apply). Unlike boundary it is **not** on the always-on core lane — a style line is only relevant when its context is live, so it surfaces purely by relevance + spreading activation (it cites the topic cluster it applies to, so `ConceptView.activated` lights it up when that topic is hot). Rendered under a soft `_concept_communication_style_header`. See [`personality-backlog/concepts.md` → L23](personality-backlog/concepts.md).

- `agent.communication_style_synthesis_enabled` *(bool, `true`)* — master switch for the communication-style synthesis pass. Off → no style concepts are mined (the rest of concept synthesis is unaffected; existing style concepts still surface).
- `memory.concept_synthesis_max_comm_style_memories` *(int, `24`, min `1`)* — cap on explicit-anchor memories offered to the communication-style proposer per run, per subject (topic clusters ride the shared `concept_synthesis_max_clusters_per_run` cap; this bounds only the anchor batch).

### L12 — tension concepts (the first meta kind)

The `tension` concept kind (`subject=user`, `relationship`, **and** `aiko`) is the first **meta** concept — its evidence is two *other* active concepts held in friction (`evidence_model="meta"`, two `("concept", id)` edges), an internal push/pull the person hasn't articulated ("he values rest but rarely takes it"), or, for `relationship`, a user value clashing with an aiko value (never a grievance). A `"tension"` synthesis population + `_run_tension_pass(subject)` runs **last** (the L1 meta dependency-ordering rule) over the small set of active *base* (non-meta) concepts and feeds three proposers ([`tension_user`](../app/core/concepts/proposers/tension_user.py) / [`tension_relationship`](../app/core/concepts/proposers/tension_relationship.py) / [`tension_aiko`](../app/core/concepts/proposers/tension_aiko.py)) sharing a `propose_tension` body whose **composition rule** accepts exactly a pair of distinct concept ids. The L3 `tension_evidence_gate` floors the source count at 2; the lifecycle worker additionally enforces the two store-dependent meta rules — **confidence bounding** (a tension can be no more certain than the shakiest concept it is built on) and **cascade** (a tension whose base leaves `active` is retired to dormant, via `dependents_of`). Tension concepts are deliberately **kept out of the T3 relevant-context block** so a standing friction can never nag; the only surface is the strictly-cooldowned T6 tension cue below (the `concept -> concept` edges still power the spreading-activation + cascade machinery).

- `agent.tension_synthesis_enabled` *(bool, `true`)* — master switch for the tension synthesis pass. Off → no tension concepts are mined (the rest of concept synthesis is unaffected).
- `memory.concept_synthesis_max_tension_concepts` *(int, `24`, min `2`)* — cap on active base concepts offered to the tension proposer per run, per subject (the relationship lens splits it roughly half user / half aiko).

Tension cue — `TensionCueWorker` occasionally drafts a private "a friction worth sitting with" cue over an active tension, and `_render_tension_block` surfaces it as a watermark-gated T6 hint the chat model phrases in-context (cue producer, never verbatim, never a confrontation):

- `agent.tension_cue_enabled` *(bool, `true`)* — master switch for the tension-cue worker + its prompt block. Off → tensions are still mined/tracked but never surface.
- `agent.tension_cue_cooldown_days` *(float, `6.0`, min `0`)* — per-tension cooldown so a cue rotates across the live frictions and stays rare (a tension is "delivered with the most care").
- `memory.tension_cue_interval_seconds` *(float, `28800.0`, min `60`)* — idle cadence for the cue worker.
- `memory.tension_cue_min_confidence` *(float, `0.6`, min `0`)* — confidence bar a tension must clear to be eligible for a cue.
- `memory.tension_cue_journal_max` *(int, `4`, min `1`)* — cap on the `aiko.tension_cue` cue ring.

### L25 — concept edge referential integrity

Concept edges (`evidence` / `contradicts`) point at memory rows that get deleted, pruned, and merged. Most deletes are reconciled synchronously by the `ConceptEdgeReconciler` (registered as a `MemoryStore` delete listener: it drops the memory's edges and recomputes the affected concepts' edge-derived `evidence_count` / `distinct_source_count`). But `MemoryStore.prune` batch-deletes rows **without** firing delete listeners, so an idle sweep worker garbage-collects any orphaned edges it leaves. Destructive merges repoint the victim's edges onto the survivor first (rule b); archived rows keep their edges (rule c). L3 stays the single writer of `confidence` / `plasticity` / `status`. See [`concept-lifecycle.md`](concept-lifecycle.md).

- `memory.concept_edge_integrity_enabled` *(bool, `true`)* — master switch for the idle integrity sweep worker. The synchronous delete-listener reconciliation is always active when the concept layer is wired.
- `memory.concept_edge_integrity_interval_seconds` *(float, `3600.0`, min `1`)* — how often the sweep runs. It's a defence-in-depth safety net (deletes are already handled live), so an hour is plenty.
- `memory.concept_edge_integrity_batch_size` *(int, `200`, min `1`)* — max orphaned edges reconciled per sweep, keeping it a small rolling job.

### L2 / L46 — near-duplicate concept consolidation

Creation-time dedup stops anything at or above the dedup cosine (`0.86`, measured once in [`concept_dedupe.py`](../app/core/concepts/concept_dedupe.py)) from splitting into two rows. Paraphrase twins that land just *below* that bar accumulate with nothing to fix them, so the `ConceptConsolidationWorker` is the retroactive pass: each tick it stacks the active set once and finds every same-`(subject, kind)` pair over `merge_cosine`, then works them **worst (most similar) first**. A maintenance-tier LLM adjudicates whether the two are genuinely one belief — pure cosine cannot tell a paraphrase from a template collision — and only a `same` verdict merges via `ConceptStore.merge_into`. The stronger row always survives, so L3 stays the single writer of `confidence` / `plasticity` / `status`.

Rejections persist in `kv_meta` under `concept_consolidation.verdicts`, keyed on the pair **plus a digest of both labels** with a 30-day TTL — so a stable template collision is paid for once instead of after every restart, while an L17 relabel re-opens the question. Watch one `concept_consolidation run: scanned=… pairs=… auto_merged=… adjudicated=… merged=… rate_limited=…` INFO line per tick; `rate_limited=True` every run means the backlog exceeds the daily budget.

- `memory.concept_consolidation_enabled` *(bool, `true`)* — master switch. Off → twins accumulate with no retroactive fix.
- `memory.concept_consolidation_interval_seconds` *(int, `900`, min `30`)* — tick cadence.
- `memory.concept_consolidation_batch_size` *(int, `40`, min `1`)* — cap on pairs **acted on** per run (not seeds scanned: discovery is a global scan since L46). Worst-first ordering means the cut falls on the least-similar pairs, which are the ones that can wait.
- `memory.concept_consolidation_merge_cosine` *(float, `0.84`, clamped `[0, 1]`)* — the bar for being a *candidate*. Was `0.88`, which admitted only 12 pairs across a month; below ~`0.82` pairs stop being restatements and start being different subjects sharing a sentence template, which the adjudicator would have to reject over and over.
- `memory.concept_consolidation_auto_merge_cosine` *(float, `1.0` = **off**, clamped to `[merge_cosine, 1]`)* — **L46.** The bar above which a pair merges with **no LLM call**. It ships disabled, and the reason is worth knowing before you lower it. The plan was `DEDUPE_COS` (0.86), arguing that the creation guard fuses at that cosine without asking anyone. But the two cases fail in opposite directions: at creation a false positive only reinforces an existing row, whereas here it *destroys* a distinct belief. Hand-reading all 18 above-bar pairs in the live graph found **2 genuine twins and 14 template collisions** — the highest-cosine pair of the whole set (0.900) was a collision. Token overlap doesn't separate them either (twins spanned Jaccard 0.14–0.52, collisions 0.07–0.27), so on templated labels only the adjudicator can tell. Lower it only after dry-running against a copy of your own graph. Floored at `merge_cosine` on load — an auto bar *below* the candidate bar would fuse everything the scan found and silently switch off the judgement this worker exists to apply.
- `agent.concept_consolidation_per_hour_cap` *(int, `6`, min `0`)* / `agent.concept_consolidation_per_day_cap` *(int, `30`, min `0`)* — LLM adjudication budget, on its own `FactCheckRateLimiter` (`state_key="concept_consolidation.rate_state"`) so it never shares with L9 / L15 / F5. Auto-merges cost nothing against it.

### L31 — what a concept may accept as evidence

Creation is gated: a new concept must clear its kind's `min_sources` / `min_chain` / `directional` bars. *Reinforcement* was not gated at all — `resolve_reinforces` checked only that the id the LLM named appeared in the list of 40 it was shown, and every source it cited was then attached with no similarity check, while the creation bars were skipped. Two shapes grew out of that on the live graph, and neither bar below catches the other:

- **Contamination.** An `aspiration/user` row ("deepening emotional and physical intimacy with Aiko…") reached 97 sources including *"Jacob really enjoyed Chainsaw Man's opening song"* and *"organizing the snack stash"* — evidence for something else that happened to be the nearest label on the shown list. The cosine floor refuses it.
- **Accretion.** A `ritual/relationship` row cited **145 of the 158 `shared_moment` memories in the graph, 92%**, and none of it is off-topic: a label that vague really is near everything affectionate, and its *lowest*-cosine evidence still measures 0.385. Only the ceiling bounds that.

Both bars are **forward-only** — they refuse new sources and never remove an edge a concept already holds, so rows that grew before the gate keep their history and simply stop growing. Watch one `evidence admission: admitted=… refused_offtopic=… refused_ceiling=… floor=… ceiling=…` INFO line per synthesis pass, plus a DEBUG line per off-topic refusal carrying the cosine and the concept it was cited for. Design and the full measurement: [`concept_evidence_admission.py`](../app/core/concepts/concept_evidence_admission.py).

- `memory.concept_evidence_admission_cosine` *(float, `0.35`, clamped `[0, 1]`, `0` = off)* — the bar a cited source must clear against the concept's own label embedding. Measured, not chosen: over all 6091 live evidence edges the source-to-label cosine runs p1 `0.324`, p5 `0.384`, p10 `0.424`, p50 `0.574`, p90 `0.756`. `0.35` refuses 2.2% of that stock while catching every piece hand-read as wrong on the contaminated row above (`0.243`, `0.311`, `0.328`) against its genuine evidence at `0.60`–`0.68`. `0.40` refuses 6.7% and `0.45` refuses 15.1%, which is where legitimate spread starts going too. A source whose vector cannot be resolved is **admitted** — failing open risks one loose edge, failing closed would starve every concept the moment an embedding went missing or the embedding model was swapped.
- `memory.concept_evidence_max_sources` *(int, `24`, min `0`, `0` = off)* — the most distinct sources one concept may hold. The 99th percentile of `distinct_source_count` (p50 `4`, p90 `10`, p95 `13`), so it binds on about one concept in a hundred, and deliberately far above where it would *matter*: `confidence_target()` saturates at its `0.97` cap by 8 distinct sources, so everything past the eighth already bought nothing. Nothing can lose confidence or fail a promotion floor by being capped. **A capped concept still bumps `last_reinforced_at`**, which is not a nicety — L3 reads that to decide a belief is still observed and the L46 dormancy TTL retires by wall-clock silence, so a frozen clock would drift the row `active → dormant → retired` while the evidence for it kept arriving. Off-topic refusal is the opposite case and moves nothing, since nothing about the belief was observed (and the proposal gets no say in the wording either — no `relabel_proposed` is staged).

The cosine measured for each arriving source is rolled through a bounded `kv_meta` sample (`concept_synth.evidence_fit`, 500 values) and read by the L45 tuner as `POP_EVIDENCE_FIT` — the one population there measured from *inflow* rather than from the stored graph. Its gate ships **observe-only**.

### K2 — theory-of-mind / belief tracking

- `agent.belief_tracking_enabled` *(bool, `true`)* — master switch for the whole K2 surface (worker + gap detector + tag parser + REST + UI). Off → `[[predict:...]]` self-tags still strip from chat but their payload is dropped.
- `agent.belief_worker_enabled` *(bool, `true`)* — toggle only the background inference worker. With tracking on and worker off, the self-tag fast path still writes beliefs and gaps still surface.
- `agent.belief_interest_bias_enabled` *(bool, `true`)* — **K65b.** Fold the K9 interest map into the belief worker's extraction prompt: prioritise the densest topic clusters and re-check stale active beliefs sitting on them, all in the same LLM call. Off → the worker mines the flat last-N user turns exactly as before. On a cold / unlabelled store the worker is byte-identical to the legacy path regardless. Verify with `force_run("belief_worker")` then grep `belief-worker interest-bias:`.
- `agent.belief_worker_per_hour_cap` *(int, `8`, min `0`)* — hourly cap on LLM extraction calls.
- `agent.belief_worker_per_day_cap` *(int, `40`, min `0`)* — daily cap.

### Promise extraction worker (Phase 3c, reworked)

The sole writer of `kind="promise"` memories. Replaces the retired post-turn regex + speaking-window LLM tracks (which wrote context-free fragments like "Jacob promised: never know"). Runs on the `IdleWorkerScheduler` during quiet windows, reads the last few turns for *context* (both user and assistant lines), and asks the worker LLM for self-contained promises (pronouns/objects resolved). Output is quality-gated (idiom stop-list + pronoun-only rejection) and deduped against existing open promises. The transcript is privacy-gated (a URL/email/address-bearing window is skipped) but otherwise sent to the **local** worker LLM with names intact so pronoun resolution works.

- `agent.promise_worker_enabled` *(bool, `true`)* — master switch. Off → no promises are auto-extracted (the `[[remember:...]]` self-tag path is unaffected).
- `agent.promise_worker_per_hour_cap` *(int, `10`, min `0`)* — hourly cap on LLM extraction calls (the real spend ceiling).
- `agent.promise_worker_per_day_cap` *(int, `60`, min `0`)* — daily cap.
- `memory.promise_worker_interval_seconds` *(int, `600`, min `60`)* — idle-worker cadence; frequent because spend is bounded by the caps, not the interval.
- `memory.promise_worker_lookback_turns` *(int, `12`, min `1`)* — recent turns (both sides) read per run.
- `memory.promise_worker_max_per_run` *(int, `5`, min `1`)* — max promises persisted per run.
- `memory.promise_worker_max_msg_chars` *(int, `2000`, min `200`)* — per-message char cap in the snapshot.
- `memory.promise_worker_max_transcript_chars` *(int, `8000`, min `500`)* — overall transcript char budget.

### K6 — surprise / novelty detector

- `agent.novelty_detection_enabled` *(bool, `true`)* — master switch. Off → the `novelty` inner-life provider is never registered (zero cost on the hot path).

### K18 — topic stagnation detector

Sibling of K6 that fires on the inverse signal: when the rolling distance-to-centroid stays low across a window, Aiko gets a "you've been circling the same topic for a bit" cue.

- `agent.topic_stagnation_enabled` *(bool, `true`)* — master switch. Pure streak counter; no extra embedding cost.

### K9 — topic graph + curiosity seeds

- `agent.topic_graph_enabled` *(bool, `true`)* — master switch for the in-process topic graph wrapper around `MemoryStore._mirror`. Disabling skips both the seed worker's "have we discussed this already?" filter and the Memory-tab cluster panel.
- `agent.topic_graph_persistent_enabled` *(bool, `true`)* — persist the topic graph (clusters + centroids + assignments) to SQLite (schema v20 `topic_clusters` / `memory_topic_assignments`) and maintain it **incrementally**: warm-start from SQLite on boot (no cold rebuild), assign each new memory to the nearest cluster centroid on the fly, and only batch-refit during quiet windows. The batch refit routes through LanceDB ANN above a corpus-size threshold so it scales to a large / uncapped memory store. When `false`, falls back to the legacy in-memory, recompute-on-read (`O(n²)`) behaviour. Debug via MCP `get_topic_graph_persistence_state` / `force_topic_graph_rebuild`; grep `tail_logs(module_contains="topic_graph_rebuild")` for `topic_graph_rebuild:` lines.
- `agent.topic_graph_rebuild_interval_seconds` *(float, `86400`, floor `60`)* — how often the `TopicGraphRebuildWorker` runs a full batch refit (default daily). Corrects incremental drift (orphaned memories, wandering centroids, new topic families that never formed a cluster on their own).
- `agent.topic_graph_refit_pending_threshold` *(int, `25`, min `1`)* — pending-pressure trigger: once this many incrementally-added memories have failed to join any existing cluster, the refit runs on the next idle tick regardless of the interval, so a burst of new topics (e.g. a web-knowledge enrichment run) is folded in promptly.
- `agent.topic_label_enabled` *(bool, `true`)* — F10a: master switch for the `ClusterLabelWorker`, an idle worker that names each topic cluster with a concise worker-LLM phrase ("weekend hiking plans") instead of the heuristic first-sentence-of-the-representative label. Runs entirely off the chat path (no per-turn token cost). Labels are cached in `kv_meta` keyed by the cluster representative (`aiko.topic_label.<rep>`) so a batch refit doesn't force a re-label — the next tick re-applies the cached label for free and only regenerates when the representative is new or the cluster has drifted in size by >50%. The label surfaces as the cluster `summary` in the topic-graph snapshot (Memory drawer) and `GET /api/topic-graph`. Grep `tail_logs(module_contains="topic_label")` for `topic_label run done:` / `topic_label generated:`. Only active in persistent topic-graph mode.
- `agent.topic_label_interval_seconds` *(float, `1800`, floor `60`)* — how often the label worker runs a pass (default 30 min).
- `agent.topic_label_max_per_run` *(int, `4`, min `1`)* — max clusters that get a fresh LLM label per tick (largest-first); bounds worker-LLM spend on a large or churned corpus. The free cache-reapply pass is unbounded.
- `agent.topic_label_max_tokens` *(int, `32`, min `8`)* — token cap for each label generation (a label is a 2-5 word phrase).
- `agent.topic_digest_enabled` *(bool, `true`)* — F10g: master switch for the `TopicDigestWorker`, an idle worker that writes one high-salience `kind="topic_digest"` memory per dense cluster — a worker-LLM one-paragraph "what I know about X" compression of its members — refreshed only when the cluster's size has drifted by >50% since the cached digest (same cache-by-representative trick as F10a, keyed `aiko.topic_digest.<rep>`). The digest **lives in the normal memory pool** (decays, pinnable, shows in the Memory tab) but is **excluded from topic-graph clustering** so it never feeds back into the cluster it summarises. It surfaces through ordinary cosine RAG and, when an anchor cluster has a digest, the F10c expansion path prefers it (see `topic_digest_surface_in_rag`). Refreshes are done in place so the memory id is stable. Runs entirely off the chat path. Grep `tail_logs(module_contains="topic_digest")` for `topic_digest run done:`. MCP `get_topic_digest_state` dumps the live cluster→digest map. Only active in persistent topic-graph mode.
- `agent.topic_digest_interval_seconds` *(float, `3600`, floor `60`)* — how often the digest worker runs a pass (default 1 h).
- `agent.topic_digest_max_per_run` *(int, `3`, min `1`)* — max clusters that get a fresh LLM digest per tick (largest-first); bounds worker-LLM spend. The free cache-reuse pass is unbounded.
- `agent.topic_digest_max_tokens` *(int, `256`, min `32`)* — token cap per digest generation (a 2-4 sentence paragraph).
- `agent.topic_digest_min_cluster_size` *(int, `6`, min `2`)* — a cluster needs at least this many members before it earns a stored digest (small clusters are cheap to read raw).
- `agent.topic_digest_surface_in_rag` *(bool, `true`)* — when on, the F10c expansion path surfaces a cluster's digest as the coarse "What you know about this topic so far:" line (its own section, 600-char truncation) and caps raw sibling enumeration to `rag_digest_sibling_cap`, so a 40-member cluster contributes a gist + a specific instead of N lines. No-op when no digest exists for the anchor cluster (falls back to plain F10c sibling expansion).
- `agent.rag_digest_sibling_cap` *(int, `1`, min `0`)* — how many raw siblings still follow the digest line when a digest is surfaced (`0` = digest only; the gist with no specifics).
- `agent.rag_cluster_diversity_enabled` *(bool, `true`)* — F10b: cluster-aware RAG diversity. When on (and a persistent topic graph is wired), the retriever's final top-k selection caps how many hits may come from a single topic cluster, so one dense cluster (e.g. a big "get to know the user" knot) can't monopolise every slot and crowd out other relevant context. Deterministic MMR-lite: walk the deduped, score-descending candidates and defer a memory hit once its cluster already holds `rag_max_per_cluster` admitted hits, then **backfill** from the deferred overflow in score order — so the re-rank only ever reorders the top-k, never shrinks it. This is about topic *monoculture*, not context bloat (the `top_k` cap already bounds total context regardless of cluster size). No-op on the in-memory / non-persistent topic-graph path. Pure retrieval re-rank, no prompt-shape change.
- `agent.rag_max_per_cluster` *(int, `3`, min `1`)* — max memory hits the retriever takes from one cluster before deferring the rest (applied only while diversity is enabled and the top-k still has room from other clusters). With the default `top_k=6` this leaves at least half the slots for other topics. Message / document hits and unclustered memories are never capped.
- `agent.rag_topic_expansion_enabled` *(bool, `true`)* — F10c: topic multi-hop expansion. When a turn's strongest memory hit (score ≥ `rag_expand_trigger_score`) belongs to a topic cluster, the retriever appends up to `rag_expand_max` sibling members of that cluster — beyond the top-k — whose cosine to the query clears `rag_expand_min_sim`, so Aiko gets the surrounding context, not just the single closest line. Siblings render in a separate "Related notes from the same topic" section so the LLM reads them as associative rather than direct recall. **This changes prompt content**; set `false` (or `rag_expand_max=0`) to revert to pure top-k retrieval. Needs a persistent topic graph + memory store; no-op otherwise.
- `agent.rag_expand_max` *(int, `2`, min `0`)* — max sibling memories topic expansion appends per turn. `0` disables expansion as surely as the flag.
- `agent.rag_expand_trigger_score` *(float, `0.55`)* — the turn's strongest memory hit must score at least this for expansion to fire (avoids rounding out weak/incidental cluster touches). Scores include the small memory prior, so this sits a touch above the bare cosine `score_threshold`.
- `agent.rag_expand_min_sim` *(float, `0.45`)* — minimum cosine (query vs sibling memory) for a cluster member to be pulled in by expansion. Keeps the appended notes genuinely on-topic. (The F10d cluster-scoped recall tool is `tools.recall_topic`, documented in the `tools` section.)
- `agent.rag_direct_recall_enabled` *(bool, `true`)* — K-time2 direct recall. When a query names a clearly retrospective time window ("what did we say yesterday / last Tuesday / back in March?"), the retriever pulls the *actual* messages from that window straight out of SQLite (`ChatDatabase.messages_in_range`) and injects them as `message` hits, so verbatim "what exactly did we say then" recall isn't limited to the semantic top-N. Gated to **guardable** windows only (never fires on chit-chat like "how are you today"); the injected lines also satisfy the empty-window anti-confabulation guard. Injected hits score around `0.55` + the in-window time bonus + per-message recency, so they surface reliably without overpowering a strong semantic memory hit; dedup-by-text collapses overlap with the semantic message hits.
- `agent.rag_direct_recall_max_messages` *(int, `6`, min `0`)* — how many in-window messages the direct-recall path injects per turn. `0` disables it as surely as the flag.
- *(removed)* `agent.interest_map_enabled` / `agent.interest_map_max_clusters` / `agent.interest_map_min_size` — the standalone "interest map" T1 block was **subsumed by the unified context budget**. Topic clusters are now one of the three turn-relevance-scored sources (memories / clusters / concepts) that share the `relevant_context` region at T3; see the `memory.context_budget_*` knobs and [`docs/context-budget.md`](context-budget.md). Leftover keys in `config/user.json` are silently ignored (no migration). The cluster **min-size** quality floor still lives at the topic graph's own `min_cluster_size`.
- `agent.coactivation_block_enabled` *(bool, `true`)* — L4: the co-activation prompt block, a hedged T1 (semi-stable) line naming the topics that keep lighting up together in the same conversations ("you've been circling X / Y / Z together lately") plus a cluster that's gone quiet, so Aiko carries a sense of the user's current "mode". Built from the topic graph's cluster co-activation signal (which clusters co-occur per session); no-op in the non-persistent topic-graph mode, silent while the graph is still immature (L21), dropped under aggressive context pressure.
- `agent.coactivation_block_max_modes` *(int, `4`, min `1`)* — how many co-activation "modes" (groups of clusters that fire together) the signal may surface; only the strongest is rendered, but this bounds the underlying compute. Higher → considers more distinct modes. Lower → focuses on only the most dominant.
- `agent.curiosity_seed_enabled` *(bool, `true`)* — master switch for the curiosity-seed worker.
- `agent.curiosity_seed_max_active` *(int, `6`, min `1`)* — how many unspent seeds the worker keeps on the shelf. Since the seed moved onto the [cue pool](cue-pool.md) this is an *inventory target*, not a cap: the worker reports pressure from the shortfall against it, so a full shelf simply means the idle scheduler does not admit the worker.
- `agent.curiosity_seed_max_per_run` *(int, `2`, min `1`)* — cap on candidates persisted per successful tick.
- `agent.curiosity_seed_min_novelty` *(float, `0.85`, clamped `[0, 1]`)* — cosine floor against existing seeds. Higher → stricter (rejects more "kind of similar" candidates); lower → more eager to write.
- `agent.curiosity_seed_resolve_threshold` *(float, `0.50`, clamped `[0, 1]`)* — cosine match for "the recent turn covered this seed; mark it consumed." Lower than the graph filter on purpose — partial / oblique mentions still count. Now applied as an override on the seed's `CuePolicy.match_threshold`, so a value you already moved keeps working.
- `agent.topic_graph_filter_threshold` *(float, `0.65`, clamped `[0, 1]`)* — cosine threshold for "we've already covered that topic." Higher → filter is stricter (lets more candidates through); lower → seed worker rejects "adjacent but new" candidates as duplicates.

### F2.1 — knowledge-gap resolver

Companion to F1: F1 closes a gap by searching the web; this worker closes it by noticing the answer is **already in memory** (e.g. you answered the question in chat the next session).

- `agent.gap_resolver_enabled` *(bool, `true`)* — master switch.
- `agent.gap_resolver_interval_seconds` *(int, `600`, min `30`)* — cadence in seconds.
- `agent.gap_resolver_threshold` *(float, `0.55`, clamped `[0, 1]`)* — cosine threshold for "this memory answers this gap." Higher → fewer false positives (real gaps stay open longer); lower → more aggressive closing.
- `agent.gap_resolver_per_tick` *(int, `5`, min `1`)* — max gaps the worker resolves per tick.
- `agent.gap_user_answer_resolve_threshold` *(float, `0.50`, clamped `[0, 1]`)* — cosine threshold for the post-turn resolver that closes gaps from the **current** user reply (reuses the user+assistant combined embedding). Lower than the worker threshold because post-turn context is stronger.

### K1 — Aiko's long-term goals

Persistent first-person goals Aiko quietly carries across sessions. Stored as `goal` / `goal_progress` memory rows; surfaced in the prompt as an inner-life block, declared via the `[[goal:summary]]` self-tag, and the four `add_goal` / `update_goal_progress` / `archive_goal` / `list_goals` agent tools. The `GoalWorker` idle worker handles cold-start bootstrap + periodic reflection.

- `agent.goals_enabled` *(bool, `true`)* — master switch for the whole K1 system. Off → no store init, no worker, no prompt block, no self-tag persistence. Existing rows stay in SQLite (safe to toggle). The four agent tools below are independently gated.
- `agent.goal_worker_bootstrap_enabled` *(bool, `true`)* — controls whether the worker's "propose ~3 goals from persona + rolling summary" LLM call runs when the store is empty. Off → seed goals manually via the Memory tab. Reflection path is unaffected. **Note**: as of the first-run onboarding seed (see [`shipped.md` → K1 follow-up](personality-backlog/shipped/patterns-k01-k15.md#k1-long-term-goals-tracker-goal--goal_progress-kinds-goalstore--goalworker)), Aiko's first long-term goal is always a curated, pinned `"Get to know {user_name}"` row inserted at onboarding completion. That row makes `has_any_active()` return `True`, which means the LLM bootstrap path in practice **never fires on a fresh install** — additional goals come from `[[goal:...]]` self-tags during real conversation. Setting this flag false now mostly affects the "user deleted all their goals" recovery path.
- `agent.goal_worker_per_hour_cap` *(int, `3`, min `0`)* — hourly LLM call cap for the `GoalWorker` (bootstrap + reflection combined). `0` disables autonomous calls entirely without unregistering the worker.
- `agent.goal_worker_per_day_cap` *(int, `12`, min `0`)* — daily LLM call cap. With the default `goal_max_active=5`, 12 lets every goal reflect twice a day with headroom for the one-shot bootstrap pass.

### K16 — unified ambient grounding line

Optional fusion of seven "ambient" inner-life signals (circadian, world, activity-awareness, affect/mood, relationship-pulse, user-state, ambient-noise) into a single continuous-awareness paragraph at the top of the system prompt.

- `agent.grounding_line_mode` *(string, `"off"`)* — one of three modes:
  - `"off"` (default, safe rollback) — no fused line; all seven granular blocks render as today.
  - `"replace"` — fused line replaces **all eight** ambient blocks (the seven listed above plus mood_hint). Cleanest test of the companion-feel hypothesis.
  - `"split"` — fused line replaces situational signals (circadian, world, activity, ambient_noise) but **keeps** trend-phrase blocks (affect, mood_hint, relationship, user_state) standalone.

  Verification: `provider_ms.grounding_line` in MCP `get_last_response_detail` is non-zero in `replace`/`split`, missing in `off`. Invalid values clamp to `"off"` with a debug log.

### J12 — intimacy pacing & boundary calibration

Two halves that keep Aiko's forwardness calibrated to the user. **(a)** a learned per-user *pacing signal* — a kv_meta EMA of how forward the user himself is (pet names for Aiko, warm / affectionate messages, affectionate reactions) so Aiko *slightly follows, never leads by much*. **(b)** a plain consent *ceiling* that hard-caps forwardness regardless of relationship stage. The ceiling is the always-on boundary control; only the learned half is gated by the master switch. At the default ceiling (`0.7`, "warm") J12 is behaviour-neutral — the cap only bites for an intimate-stage bond. The cap surfaces three ways: a register cue in the system prompt, a scale factor on the K15 disclosure budget, and a gate on the J9 reciprocal-vulnerability beat.

- `agent.intimacy_ceiling` *(float, `0.7`, clamped `[0, 1]`)* — the consent dial (`reserved` < 0.4 ≤ `warm` < 0.75 ≤ `affectionate`). Lower → Aiko stays warm-but-contained, shares less, and lets the user set the pace on closeness. Higher → removes the cap (stage + learned signal decide where she lands). Always on, independent of the master switch below.
- `agent.intimacy_pacing_enabled` *(bool, `true`)* — master switch for the **learned** half (user-pace EMA + the "follow him, don't lead" cue). Off leaves the consent dial fully functional; only the learned-pacing behaviour stops.
- `agent.intimacy_pacing_learning_rate` *(float, `0.15`, clamped `[0, 1]`)* — EMA blend rate for a new per-message / per-reaction forwardness score. Higher → the estimate tracks recent messages faster; lower → smoother, slower to move.
- `agent.intimacy_pacing_decay_half_life_days` *(float, `14.0`, min `0`)* — half-life of the slow decay of the estimate back toward the neutral `0.5` midpoint. Higher → a forward / cold stretch lingers longer; lower → reverts to neutral faster.
- `agent.intimacy_pacing_follow_strength` *(float, `0.5`, clamped `[0, 1]`)* — how hard Aiko follows the user's own pace within the ceiling. `0` → ignore the learned signal; `1` → match it fully. The "slightly follow, never lead by much" knob.

Verification: MCP `get_intimacy_pacing_state()` dumps the ceiling, band, live user-pace, the per-stage effective forwardness, the K15 disclosure factor, and the cue that would render now; `set_intimacy_ceiling(value)` / `set_user_pace(value)` push known values for end-to-end repro. Tests: `tests/test_intimacy_pacing.py`, `IntimacyPacingProviderSlotTests` in `tests/test_prompt_assembler.py`, `IntimacyPacingSettingsTests` in `tests/test_settings.py`.

### K23 — subtle misattunement detection

Per-turn detector that fires `mild_disengagement` when {user} goes very short or pivots topics right after a substantial Aiko reply. Sits in the gap between K17 (explicit "no that's not what I meant" regex) and K14 (multi-turn engagement aggregate that needs warmup). The cue lands on the **same turn** that's about to reply — pulling back IS the next response.

Two trigger paths, both gated by the cooldown:

1. **Shrink**: `prev_aiko_words >= shrink_min_prev_words` AND `this_user_words <= shrink_max_user_words`. A one-word reply right after a 60-word answer reads as "you went quiet on me".
2. **Pivot**: K6 [`NoveltyDetector`](../app/core/conversation/novelty_detector.py) flagged the current message as `strong_novelty` AND `this_user_words <= pivot_max_user_words`. A short pivot without engaging Aiko's last point.

Either trigger fires the same cue ("pull back, lighter, drop the agenda, no apologies"); strong-vs-mild banding is intentionally not modelled in the MVP — the cooldown gate keeps the cue rare enough that a single voicing is sufficient.

- `agent.misattunement_detection_enabled` *(bool, `true`)* — master switch. Off → provider short-circuits to empty string and the cooldown counter stops moving (the master switch is checked BEFORE the cooldown decrement, so flipping off doesn't quietly drain any pending counter).
- `agent.misattunement_shrink_min_prev_words` *(int, `30`, min `0`)* — minimum word count on Aiko's prior assistant reply to consider it "substantial enough that a short user follow-up reads as drift". Raise to 50+ for a stricter "only after long answers" threshold; lower to 15 for a more sensitive cue that fires after medium replies too. `0` effectively makes the shrink path fire on any user reply that's short enough.
- `agent.misattunement_shrink_max_user_words` *(int, `8`, min `0`)* — maximum word count on the current user message to count as "very short". One-word replies like "ok"/"yeah"/"nice" sit well below this; full short-thoughts ("yeah, that makes sense to me") cross 8 and read as engaged. Lower to 4 for a stricter "literally one-word" gate; raise to 12 to catch slightly longer terse replies.
- `agent.misattunement_pivot_max_user_words` *(int, `8`, min `0`)* — same shape as the shrink-user cap but for the pivot trigger. Mirrored separately so you can tune them independently (e.g. allow longer pivots to count as drift while keeping the shrink cap tight).
- `agent.misattunement_cooldown_turns` *(int, `3`, min `0`)* — turns of cooldown after a fire. Decremented by 1 on every provider call regardless of trigger state; armed back to this value whenever the detector fires. `0` disables the cooldown entirely (every eligible turn fires); higher values keep the cue rare. The conditions for the trigger can persist across consecutive turns when {user} is genuinely busy, so the cooldown is the main protection against the cue stacking.

Verification: enable INFO logging on `app.misattunement_detector` and watch for `misattunement-detector: trigger=… prev_aiko=… this_user=… novelty_band=… cooldown_set=…`. The MCP tools `get_misattunement_state()` and `force_misattunement()` cover end-to-end repro without waiting for an organic trigger. Tests: `tests/test_misattunement_detector.py`, `tests/test_misattunement_provider.py`, `MisattunementProviderTests` in `tests/test_prompt_assembler.py`, `MisattunementSettingsTests` in `tests/test_settings.py`.

### K25 — memory confidence time-decay

Read-side time-decay on memory confidence with a new `(distant)` suffix that's distinct from `(uncertain)` and `(faded)`. No schema change, no decay-writer — each retrieval recomputes `effective_confidence = stored * max(floor, 1 - days_since_created / horizon_days)` and stamps the row with `(distant)` when the result drops below the threshold. Pinned rows bypass.

Three independent suffix predicates layer cleanly:

- `(uncertain)` — **stored** confidence is low (the F1 fact-checker flagged it, or the source was shaky at write time). Persona hedge: "I think", "if I'm remembering right".
- `(distant)` — **raw age** has decayed an otherwise-fine claim. The memory is still active, just old. Persona hedge: "a while back", "don't quote me on the date".
- `(faded)` — **tier + idle** signal: K7 says the row is archived or has decayed in place. Persona hedge: "ages ago", "I might be wrong".

All three can stack on the same row. Order in the rendered prompt: `(uncertain) (distant) (faded)`. The LLM reads source-doubt first, then time-doubt, then cold-history.

Default behaviour at `horizon_days=365, floor=0.3, distant_threshold=0.5`:

| Scenario | When `(distant)` fires |
|---|---|
| Default-confidence claim (0.7) | ~104 days old |
| High-confidence claim (0.9) | ~165 days old |
| Self-tagged claim (0.85) | ~150 days old |
| Pinned row (any confidence) | Never (bypassed) |

- `agent.confidence_time_decay_enabled` *(bool, `true`)* — master switch. Off → no row gets the `(distant)` suffix; the score-side `_confidence_penalty` still reads stored confidence (we're suffix-only, not ranking-side), K7 `(faded)` still fires, `(uncertain)` still fires.
- `memory.confidence_decay_horizon_days` *(int, `365`, min `1`)* — days at which the decay multiplier reaches `floor`. Raise (e.g. `730`) for slower decay — only very old claims hedge; lower (e.g. `90`) for aggressive hedging where even three-month-old claims read as "a while back".
- `memory.confidence_decay_floor` *(float, `0.3`, range `[0, 1]`)* — minimum multiplier the decay can reach. With `floor=0.3`, an old default-confidence (0.7) claim decays to `0.7 * 0.3 = 0.21` and stays there forever. A `floor` of `0` would let very old claims decay to zero (still rendered, just always hedged); a `floor` of `1.0` disables decay entirely (same effect as flipping the master switch off, but the predicate still runs).
- `memory.confidence_decay_distant_threshold` *(float, `0.5`, range `[0, 1]`)* — effective-confidence value below which the `(distant)` suffix fires. Mirrors the existing `0.5` cutoff used for `(uncertain)`. Lower → only very-decayed claims hedge; higher → more hedging across the board.

Verification: call MCP `get_confidence_decay_state(limit=20)` to see which memories would currently render with which suffix. Tweak `user.json`, restart, call again — the row's `effective_confidence` should shift and the `distant` flag should flip predictably. Tests: `tests/test_confidence_decay.py`, `FormatBlockDistantSuffixTests` in `tests/test_rag_retriever_scoring.py`, `ConfidenceDecaySettingsTests` in `tests/test_settings.py`.

### K28 — turning over (what I've been thinking about between sessions)

One-shot inner-life cue on the first user turn after a long typed gap (default `>= 90 min`). Surfaces one recent `kind="reflection"` memory (which covers both `ReflectionWorker` output and `DreamWorker` output — the latter is identified by a `[dream]` content prefix) so Aiko's first reply can fold in "actually, I was thinking about your interview prep last night --" as a casual aside instead of arriving blank. The handling note ("What I've been turning over" in [`data/persona/conditional_handling.txt`](../data/persona/conditional_handling.txt), hoisted into T6 only on the turns the cue fires) carries the anti-announcement discipline (fold it in casually, never lead with "I have something to share", drop silently if it doesn't fit the moment) and the softer dream-variant framing. What she was shown is logged to the `cue_pool` as surfaced, so a reflection she ignored comes back on a later turn rather than being spent on the render — see [`cue-pool.md`](cue-pool.md#cues-chosen-at-render-time-the-surface-time-ledger).

Pairs with K14 absence-curiosity on the 90 min – 4h overlap: K14 frames the welcome-back ("hey, you, back already?"), K28 adds the specific thought ("...and I was thinking about your interview prep"). The two cues stack — they use independent post-turn slots — so a 2h-gap typed turn lands both blocks in the system prompt, in that order. Voice-mode turns never arm K28 (same gating as K14).

Picker (v1, simple-then-iterate):

1. **Age window** — `min_age_hours <= reflection_age <= max_age_hours` (defaults `24h .. 72h`).
2. **Topical match** — candidate embedding scored against the union of active-goal vectors AND the last `recent_msgs_window` user-message vectors from the RAG store. `topical_score = max(over both pools)`. Below `min_topical_similarity` → drop.
3. **Recency tie-break** — among surviving candidates, the youngest wins.

The picker would rather stay silent than surface an off-topic reflection. A weighted picker (`score = recency * w_r + cosine(goals) * w_g + cosine(threads) * w_t`) is documented as a fast-follow in [`shipped.md`](personality-backlog/shipped/patterns-k16-k30.md#k28-what-ive-been-turning-over--between-session-thought-thread) — only worth implementing if the simple picker reads too random.

Settings:

- `agent.turning_over_enabled` *(bool, `true`)* — master switch. Off → no turning-over block ever lands in the prompt and the post-turn arm doesn't stash anything.
- `memory.turning_over_min_gap_minutes` *(float, `90.0`, min `5.0`)* — minimum gap (in minutes) between Aiko's last reply and the current user message that arms K28. Sits inside K14's `[30 min, 4h)` band on purpose so the two cues stack on the 90 min – 4h overlap. Raise (e.g. `240`) to only fire on overnight / multi-day returns; lower (e.g. `60`) to fire on lunch-break-sized gaps.
- `memory.turning_over_min_age_hours` *(float, `24.0`, min `1.0`)* — picker drops reflections younger than this. Prevents a reflection written 5 minutes before the session ended from showing up as "I've been turning this over".
- `memory.turning_over_max_age_hours` *(float, `72.0`, min `min_age_hours + 1`)* — picker drops reflections older than this. Keeps the cue tied to the most recent between-session window. The parser cross-clamps `max >= min + 1` so a hostile config can't produce an empty window.
- `memory.turning_over_min_topical_similarity` *(float, `0.30`, range `[0, 1]`)* — cosine floor for the candidate vs the goal / thread pools. Lower (e.g. `0.20`) → easier topical match (more fires, more "huh, where did that come from"); higher (e.g. `0.45`) → only sharply-on-topic reflections fire.
- `memory.turning_over_recent_msgs_window` *(int, `12`, min `0`)* — how many recent user-message vectors to pull from the RAG store as the "thread" pool. `0` disables the thread pool entirely (picker only matches against active goals).

Verification: enable INFO logging on `app.session` and watch for `turning-over fire: memory_id=… age_h=… topical=… source=… dream=…` on every fire. The MCP tool `get_turning_over_state()` includes a **dry-run picker result** so you can see what *would* surface against the current memory state without waiting for an organic trigger; `force_turning_over()` arms a one-shot bypass on the gap gate so the picker runs on the next message regardless. End-to-end repro: insert a `kind="reflection"` row 30h old aligned with an active goal, call `force_turning_over`, send a relevant message, watch `tail_logs(module_contains="turning_over")` for the fire line and confirm Aiko's reply folds it in as a casual aside. Tests: `tests/test_turning_over_picker.py`, `tests/test_turning_over_provider.py`, `tests/test_post_turn_turning_over.py`, `TurningOverProviderTests` in `tests/test_prompt_assembler.py`, `TurningOverSettingsTests` in `tests/test_settings.py`.

### K29 — opinion injection (push back when she has a stance)

Per-turn detector that fires a one-line cue when {user_name}'s latest message contradicts one of Aiko's stored `kind="self"` stance memories. The whole feature exists to make the persona's "have opinions, disagree when you disagree" claim actually fire against LLM RLHF agreeability — without flipping into contrarianism or moralizing. The persona block ("When you have your own take" in [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt)) teaches Aiko to *share her preference as her own taste*, never to prescribe behaviour for the user, and includes concrete bad/good pairs for the lifestyle (smoking / horror / late-night) failure mode.

Anti-contrarianism is layered (see [`docs/personality-backlog/shipped/patterns-k16-k30.md#k29-opinion-injection--push-back-when-she-has-a-stance`](personality-backlog/shipped/patterns-k16-k30.md#k29-opinion-injection--push-back-when-she-has-a-stance) for the full decision flow):

1. **Predicate filter** — only opinion-shaped stance memories qualify (`I prefer`, `I don't like`, `I love`, `I find ... <adj>`, `I'd rather`, etc.). Biographical facts (`I was born in Tokyo`, `I live in...`) never trigger the loop.
2. **Cosine threshold** — top stance memory's cosine vs the live user message must clear `min_cosine`.
3. **Heuristic gate** — re-uses F5's [`conflict_heuristics.classify_pair`](../app/core/memory/conflict_heuristics.py); `definite` (clear negation-flip or antonym hit on focused phrasing) fires immediately, no LLM call.
4. **LLM YES/NO/UNRELATED gate** — on every non-`definite` path (verbose-stance contradictions that don't clear the heuristic's Jaccard threshold are *exactly* the cases the LLM should catch). Rate-limited via [`FactCheckRateLimiter`](../app/core/memory/fact_check_rate_limiter.py) (`state_key="opinion_injection.rate_state"`). The prompt is explicitly biased toward `NO` / `UNRELATED` when uncertain. Disabling the LLM path entirely (`agent.opinion_injection_require_definite=true`) restricts K29 to the cheap heuristic-only path (Path C); the default Path B uses the LLM as the real arbiter.
5. **Cooldown + per-session cap** — cooldown=5 turns between fires; session cap=3 (silent suppression beyond the cap). Both reset on `switch_session` / `clear_conversation_memory`.

Smoking walkthrough (the canonical lifestyle-stance failure mode the persona block was built around):

1. Aiko has a stored stance memory: "I really don't like smoking, it gives me a headache" (`kind="self"`).
2. {user_name} says: "I like smoking, helps me think."
3. Predicate filter → opinion-shaped ✓. Cosine top match clears 0.55 ✓. `classify_pair` returns `definite` via negation-flip ✓. Cue fires.
4. Aiko's prompt now contains the cue, and the persona block tells her to share her take in her own register ("ugh, that's not my favourite — smoke and I don't really get along") rather than lecturing ("you should quit, it's bad for you").

If {user_name} instead said "I quit smoking last year — it was killing my sleep", the stance aligns with Aiko's, `classify_pair` returns `no`, and the cue stays silent. The cap and cooldown also reset to bound the worst-case (a detector that misfires can't dominate a conversation).

Settings:

- `agent.opinion_injection_enabled` *(bool, `true`)* — master switch. Off → provider short-circuits to empty string and the cooldown counter stops moving (checked BEFORE the decrement so flipping off doesn't quietly drain a pending counter).
- `agent.opinion_injection_require_definite` *(bool, `false`)* — when `true`, drops the LLM gate entirely (Path C: definite-only). Zero LLM cost; only clear negation-flip / antonym hits fire. Useful for slow LLMs or as a temporary measure when the borderline path keeps surfacing false positives.
- `memory.opinion_injection_min_cosine` *(float, `0.55`, range `[0, 1]`)* — top-cosine floor between the live user message and a stance memory's embedding. Higher (e.g. `0.65`) → only near-exact topical brushes count; lower (e.g. `0.45`) → easier topical match (more recall, more noise).
- `memory.opinion_injection_min_user_words` *(int, `4`, min `0`)* — short messages ("ok", "yeah", "lol") never claim a contradiction (they're K23 territory). Set to `0` to disable the length gate.
- `memory.opinion_injection_cooldown_turns` *(int, `5`, min `0`)* — turns of cooldown after a fire. Longer than K23's 3 because a stance disagreement is a heavier beat than a soft-drift cue. `0` disables.
- `memory.opinion_injection_per_session_cap` *(int, `3`, min `0`)* — hard cap on fires per session. Five fires in one conversation almost certainly means the detector is misfiring; the cap silently suppresses the rest. `0` disables the cap (operator override; the cooldown still applies).
- `memory.opinion_injection_per_hour_cap` *(int, `6`, min `0`)* and `memory.opinion_injection_per_day_cap` *(int, `30`, min `0`)* — LLM-gate budgets for the borderline path. Independent from F5's conflict-detector budget (different `state_key`). Setting either to `0` disables the LLM gate (effectively `require_definite=true`).

Verification: enable INFO logging on `app.session` and watch for `opinion-injection fire: trigger=… cosine=… stance_id=… heuristic=… signals=… llm_verdict=… cooldown_set=… session_count=…` on every fire. The MCP tools `get_opinion_injection_state()` and `force_opinion_injection()` cover end-to-end repro without waiting for an organic trigger; the `get_opinion_injection_state` payload includes the rate-limiter snapshot, the last-fire diagnostics, and the live settings snapshot so the tuning loop is "tweak `user.json`, restart, call the tool, see how the rendered cue would change". Tests: `tests/test_opinion_injection_detector.py`, `tests/test_opinion_injection_provider.py`, `OpinionInjectionProviderTests` in `tests/test_prompt_assembler.py`, `OpinionInjectionSettingsTests` in `tests/test_settings.py`.

### L18b/L18c — boundary & communication-style steering

Two related pieces that make the *learned* concept lines (the `communication_style` / `boundary` kinds) actually steer delivery, rather than sitting under the fixed persona.

**L18b — persona lightening + the learned-style steer.** The persona's talk-style rules (`How you talk`, `Conversation rules` incl. `LENGTH:` / `DON'T ALWAYS ASK A QUESTION`, `Leading vs following`) sit at the top of the prompt (T0) and are phrased as *defaults*. A constant, name-aware addendum ([`build_learned_style_addendum`](../app/core/session/prompt_support.py)) is folded in right after the persona (same slot as the speech-grammar addendum, still T0, so it stays in the cache prefix) telling the model that when the context later surfaces a learned communication-style / boundary line, that line is the *live calibration* of the defaults and wins when it fits — "hold them lightly, never as hard rules, and when none surface the defaults simply stand". The addendum is self-gating (inert on turns where nothing surfaces), so there is no per-turn or per-concept branching in T0. Two persona rules (`LENGTH:` sizing and the "1 in 3 turns end on a thought" question cadence) were softened to name that a surfaced learned line recalibrates them. No behaviour-subsystem code gating (K31/K59/K60 stay as-is) — the steer is entirely in the prompt, which keeps it robust across different chat models. This has no dedicated settings knob (it rides the existing `communication_style` / `boundary` concept synthesis switches).

**L18c — boundary-vs-conversation clash cue.** A K29-style per-turn detector ([`boundary_clash_detector`](../app/core/affect/boundary_clash_detector.py)) that fires a soft T6 cue when the live turn is heading *toward* an active `boundary` concept — so Aiko feels the tension in-the-moment instead of only carrying the boundary as background T3 guidance. It reads active boundaries via `ConceptView.relevant` (embedding-nearest, which yields the label-cosine in one call), applies a cosine + word-count gate, and uses `classify_pair` only to *sharpen* the register (a lexical clash makes it "pushing right at" rather than "brushing up against"). Cosine-only, no hot-path LLM. The cue is self-contained (no persona edit needed) and forbids naming the line out loud, refusing, or lecturing.

Settings:

- `agent.boundary_clash_enabled` *(bool, `true`)* — master switch. Off → the provider never runs (no embed, no concept read).
- `memory.boundary_clash_min_cosine` *(float, `0.58`, range `[0, 1]`)* — cosine floor between the live turn and an active boundary's label. Set a touch above the K29 opinion floor (0.55) because a boundary is a broader behavioural line; higher → only near-exact topical brushes fire.
- `memory.boundary_clash_min_user_words` *(int, `4`, min `0`)* — short quips can't credibly approach a boundary. `0` disables the length gate.
- `memory.boundary_clash_cooldown_turns` *(int, `5`, min `0`)* — turns of cooldown after a fire. `0` disables.
- `memory.boundary_clash_per_session_cap` *(int, `3`, min `0`)* — hard cap on fires per session (a standing boundary is background guidance; the sharp cue never nags). `0` disables the cap (cooldown still applies).

Verification: enable INFO logging on `app.session` and watch for `boundary-clash fire: trigger=… cosine=… concept_id=… subject=… heuristic=… cooldown_set=… session_count=…` on every fire. Tests: `tests/test_boundary_clash.py`.

### K46 — stance persistence (don't cave on taste pushback)

Rides on top of K29 + K20 to draw the **taste vs facts** line. After Aiko states a taste (a K29 cue fired), a *mild* pushback from the user ("really?", "you don't like that?") should NOT make her hedge or flip — that's the chatbot-agreeability tell. K46 surfaces a one-line "hold your take" cue AND shields the K20 calibration from a factual-trust hit on that turn (a taste disagreement must not teach Aiko her *facts* are suspect). A *strong* correction ("no, that's wrong", "let me check") is left to K20 untouched — it's a factual signal even mid-taste-talk.

- `agent.stance_persistence_enabled` *(bool, `true`)* — master switch. Off → neither the cue nor the calibration shield run.
- `memory.stance_persistence_window` *(int, `3`, min `0`)* — how many turns a just-stated taste stays "warm". The window is armed (post-turn) whenever a K29 cue fires and decremented once per turn; while it's `> 0` a mild pushback is read as taste disagreement. `0` effectively disables the feature (window can never be positive).

Verification: enable INFO logging on `app.session` and watch for `stance-persistence fire: band=… window=… forced=…` (cue) and `stance-persistence: shielded calibration from taste pushback (band=… window=…)` (write shield). MCP `get_stance_persistence_state()` dumps the switch, the window setting, the live countdown + stance snippet, and the last-fire diagnostic; `force_stance_persistence()` arms a one-shot bypass on the window (a mild-pushback band is still required). Tests: `tests/test_stance_persistence.py`, `StancePersistenceProviderTests` in `tests/test_prompt_assembler.py`, `OpinionInjectionSettingsTests` in `tests/test_settings.py`.

### K81 / K85a — leaning toward something of hers

A rare, lull-gated permission slip to put something of Aiko's own on the table. K81 read only `subject="aiko"` `taste` concepts, of which exactly two have ever been mined, so on the live store the block was silent almost always — not because its gates were tight but because there was nothing behind them. K85a widens the fallback to her `aspiration` / `value` / `identity` concepts, filtered to labels that don't name the user or the bond (about a quarter of the ~104 active rows survive that filter).

- `agent.taste_steer_enabled` *(bool, `true`)* — master switch for the whole block.
- `agent.taste_steer_widen_enabled` *(bool, `true`)* — **K85a.** Off restores the taste-only read. On, the block falls back to aspiration → value → identity when no taste clears the bar, and switches to different copy: a taste is a topic to steer toward, but a value is a position to state, and asking her to steer a conversation onto one produces a lecture.
- `memory.taste_steer_min_confidence` *(float, `0.6`)* — the confidence bar a concept must clear to be leaned on.

L42's concentration / fixation findings suppress the whole block, so a learned lean can't deepen a rut.

### K85 — pursuits (the third subject)

What Aiko keeps returning to **on her own**, with the user out of the picture entirely. Taste is bond-scoped by definition ("topics she enjoys getting into with him"), value and identity are how she reasons, and three quarters of her stored self-concepts name him outright — so when the room went quiet she had nothing of her own to open with. Mined from the `pursuit_note` memories her hobby milestones and substantive away beats now leave behind.

- `agent.pursuit_synthesis_enabled` *(bool, `true`)* — the synthesis pass that turns notes into `pursuit` concepts. Off just skips the pass; existing pursuits keep working.
- `agent.pursuit_seeds_enabled` *(bool, `true`)* — the cold start. Files a handful of authored starter pursuits once per install, as `candidate` rows with **zero evidence**. They cannot steer anything (only `active` concepts surface) and must clear the same gate on the same lived notes a grown pursuit needs; a seed that never comes up accrues no sources and is retired by the L3 candidate TTL after three weeks. Off means she waits for her own notes, which on a fresh install is a fortnight.
- `memory.pursuit_min_notes` *(int, `4`, min `1`)* — how many notes must exist before the pass runs at all. Below the promotion gate's three-source floor nothing could promote, so a cold pool is a pure no-op. Was `6`, which at roughly one note a fortnight meant the pass had still never run months after shipping; `4` keeps a margin over the gate's three, so it lowers the bar for *asking*, not for believing.
- `memory.concept_synthesis_max_pursuit_memories` *(int, `40`, min `1`)* — notes offered per run, taken **chronologically** rather than by salience: recurrence is the signal, and a salience sort would hide exactly the dull repetition that proves it.

The promotion gate is the strictest of the aiko kinds — three distinct notes and a **week** of age, against taste's two and half a day. A pursuit gives her something to open with, so a wrong one is a woman announcing an interest she doesn't have, and the thing that separates a pursuit from an afternoon is that she came back to it.

Once a pursuit is `active` it reaches the conversation two ways:

- `agent.pursuit_lean_enabled` *(bool, `true`)* — the T6 `pursuit_lean_block`, a lull permission slip asking for one small concrete thing about it. It shares the K81 taste lean's pacing gate (a standing K18 lull, warmth earned on a relationship axis, no L42 concentration/fixation finding) **and** its once-per-conversation latch: there is one slip here with two possible sources, and the pursuit runs first because taste is bond-scoped and a pursuit isn't.
- `agent.pursuit_share_wants_enabled` *(bool, `true`)* — feeds her strongest pursuit into the K52 wants ledger as a `share` want, one per tick at a lower starting pressure than the time-sensitive wants, so it surfaces through K53 on an open turn rather than a stalled one. The want retires with its concept: when L3 demotes or decays the pursuit, `_prune_dead_pursuit_wants` drops the want instead of letting its pressure climb toward volunteering an interest she no longer has. Turning the switch off stops new offers but leaves banked wants alone.

### K63 — long-arc callbacks ("weeks ago you said...")

A rare "she actually knows me" beat: occasionally Aiko reaches **weeks or months** back to connect the live turn to something the user told her long ago ("wait — didn't you once mention your dad's workshop, back in spring?"). An *aged retrieval lane* on the RAG retriever (the inverse of the recency boost) finds an old, topically-linked memory; a provider surfaces it as a tentative callback cue, leaning on K25's hedging posture. Rarity is the whole point — paced by a per-session cap, a wall-clock cooldown, a high topical bar, a hard age floor, and a don't-repeat ring.

- `agent.long_arc_callback_enabled` *(bool, `true`)* — master switch. Off → the provider never runs (no embed, no search).
- `memory.long_arc_callback_min_age_days` *(int, `21`, min `1`)* — a callback memory must be at least this old (keeps it "long arc"; K22 covers fresher callbacks).
- `memory.long_arc_callback_min_cosine` *(float, `0.55`, clamped `0..1`)* — topical bar of the live turn vs. the old memory. Higher than the normal RAG `score_threshold` so a callback is a genuine link.
- `memory.long_arc_callback_cooldown_hours` *(float, `6.0`, min `0`)* — wall-clock cooldown between callbacks (persisted in `kv_meta`, survives restarts).
- `memory.long_arc_callback_per_session_cap` *(int, `1`, min `0`)* — at most this many callbacks per session, regardless of cooldown. `0` disables.
- `memory.long_arc_callback_min_user_words` *(int, `5`, min `0`)* — skip turns shorter than this (too little topic to anchor a callback; also avoids a search on trivial replies).

Only memory kinds representing things the *user* told Aiko qualify (`fact` / `preference` / `event` / `relationship` / `shared_moment`); her own self-stances / distilled knowledge never become callbacks. The cue is query-aware and dropped under aggressive mode.

Verification: enable INFO logging on `app.session` and watch for `long-arc-callback fire: mem=… kind=… cosine=… age_days=… forced=…`. MCP `get_long_arc_callback_state()` dumps the switch, all knobs, the live per-session count, the kv cooldown stamp + don't-repeat ring, `cooldown_elapsed`, the force flag, and the last fire; `force_long_arc_callback()` arms a one-shot bypass on the cap + cooldown + min-words gates (the age / cosine / kind gates still apply, so an old topically-matching memory is still required). Repro: seed an old (≥ 21-day) memory on a topic, `force_long_arc_callback()`, send a message on that topic, and the tentative line lands in `get_last_response_detail.system_prompt`. Tests: `tests/test_long_arc_callback.py`, `LongArcCallbackProviderTests` in `tests/test_prompt_assembler.py`, `LongArcCallbackSettingsTests` in `tests/test_settings.py`.

### K67 — dormant-interest re-opener ("we haven't talked about X in ages")

The symmetric sibling of K64b (interest drift) and K34 (future plans): when a topic the user was genuinely into has quietly dropped off, Aiko gently re-opens it on a natural lull ("you used to be all about your band — still playing, or did that fizzle?"). The `DormantInterestWorker` (a cheap, no-LLM idle worker) reads `topic_graph.cluster_activity`, keeps once-high-mass clusters that have gone silent for weeks, and drafts them into the `aiko.dormant_interests` kv journal; a lull-gated provider surfaces one rarely. Unlike the K64b drift cue (which fires when the live turn is *on* the drifting topic), a dormant interest is by definition *not* the live topic, so this one waits for a conversational lull and reaches off-thread.

- `agent.dormant_interest_enabled` *(bool, `true`)* — master switch. Off → the worker never registers and the provider stays empty.
- `memory.dormant_interest_interval_seconds` *(int, `21600`, min `60`)* — how often the worker scans cluster activity (6h default; a dropped interest is a slow signal).
- `memory.dormant_interest_journal_max` *(int, `6`, min `1`)* — size of the kv journal ring.
- `memory.dormant_interest_min_size` *(int, `6`, min `2`)* — a cluster must have at least this many members to count as a genuine past interest (its accumulated members ≈ peak mass).
- `memory.dormant_interest_max_clusters` *(int, `40`, min `1`)* — cap on how many of the largest clusters get scanned per tick.
- `memory.dormant_interest_dormant_days` *(float, `21.0`, min `0`)* — a cluster counts as dormant once its newest member is at least this many days old (~3 weeks).
- `memory.dormant_interest_topic_cooldown_hours` *(int, `336`, min `0`)* — per-topic cooldown so the same dead thread isn't re-drafted (14 days).
- `memory.dormant_interest_surface_cooldown_hours` *(float, `24.0`, min `0`)* — provider-side wall-clock cooldown across ALL topics: at most one re-opener may surface per window, so the beat stays rare even with several queued.

The natural-lull gate reuses the K18 `TopicStagnationDetector.last_mean` standing reading vs. `memory.stagnation_mild_threshold` (the same signal K54 topic-appetite consumes). Each topic surfaces at most once (per-topic `surfaced_keys`). The cue lands in T6 right after `topic_appetite_block`; no-arg, dropped under aggressive mode.

Verification: enable INFO logging and watch for `dormant-interest drafted: …` (`app.dormant_interest_worker`) and `dormant-interest fire: …` (`app.session`). MCP `get_dormant_interest_state()` dumps the switch, registration, journal ring, surfaced keys + clock, topic cooldowns, the live `lull_mean` vs threshold, and the force flag; `force_dormant_interest()` runs the worker once bypassing the caps (a once-big-but-quiet cluster must still exist); `force_dormant_interest_surface()` arms a one-shot bypass on the lull + cooldown + surfaced gates (the ring must be non-empty). Repro: seed a big, weeks-old topic cluster, `force_dormant_interest()`, `force_dormant_interest_surface()`, send a message, and the "we haven't talked about X in ages" line lands in `get_last_response_detail.system_prompt`. Tests: `tests/test_dormant_interest.py`, `DormantInterestProviderTests` in `tests/test_prompt_assembler.py`, `DormantInterestSettingsTests` in `tests/test_settings.py`.

### K-time1 — wall-clock prefixes on chat history

Per-message relative-age tag prepended to every chat-history message sent to the LLM: `[just now] ...`, `[2 min ago] ...`, `[today 13:32] ...`, `[yesterday 18:45] ...`, `[Wednesday 18:45] ...`, `[May 28 18:45] ...`. The current user message Aiko is replying to is appended *after* the history block and never gets a prefix. Default on.

Why: without per-message timestamps the LLM has no clock against the conversation. A user message from 2 minutes ago saying "I'm planning to visit my grandparents in half an hour" pattern-matches as a completed past event, and Aiko asks "did you make it back?". The prefix gives an explicit per-turn clock; the companion persona block in [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt) ("Wall-clock awareness in the conversation") teaches Aiko how to read it and explicitly tells her not to quote the prefix back.

- `agent.history_age_prefix_enabled` *(bool, `true`)* — master switch. Off → the chat-history block is byte-identical to the pre-K-time1 behaviour (raw `{role, content}` pairs with no per-message timestamp). Use the off setting for A/B comparison or if your model interprets the bracketed metadata as part of the dialogue.

Cost: ~4–6 tokens per kept history message. Negligible against the configured `llm.routes.main_chat.context_window` budget.

Verification: enable INFO logging on `app.core.session.prompt_assembler`; the rendered prompt's history messages start with `[…]` brackets. The `_format_age` ladder is unit-tested in `tests/test_prompt_assembler.py::WallClockHistoryPrefixTests`.

### K91 — session-continuity bridge

A "conversation" is a UI affordance: the user starts a new one to get a visual divider in his own sidebar. It is filing, and it says nothing about whether the relationship paused. Aiko's side of it was the opposite — *everything* session-scoped resets at that boundary and nothing crosses it. The transcript is empty, the rolling summary and the K21 thread note are keyed by `session_id` so both come back blank, and every gap cue (J5 reconnection, K14 absence curiosity, K28 turning over, H21 sleep return, K36 away activities, K34 forward curiosity) measures from the previous assistant message *in the same session* and therefore stays silent, because there isn't one.

So the moment she most needs "we were talking about X, about three hours ago" was exactly the moment she knew least: she woke with long-term memory and relationship state intact but no idea a conversation had just ended, and greeted him accordingly.

While the new conversation holds fewer than `agent.continuity_max_messages` messages, a T2 block names how long ago the previous conversation ended and what it was about (its K21 thread note). Two tails, chosen by elapsed time against `CONTINUOUS_WINDOW_SECONDS` (6 h, deliberately under J5's reconnection floor so the two never both speak): under it, "that is close enough to be the same sitting, carry on"; over it, "noticing the gap is natural, but the thread above is where you left off".

- `agent.continuity_max_messages` *(int, `6`)* — how many messages a conversation may hold and still count as "just opened". `0` disables the block.

Deciding "is this a seam?" costs no query: a compacted session is by definition long, and an uncompacted one has all its messages in the history window already loaded, so the common case is settled from values the assembler has in hand. The two extra reads (`latest_other_session`, `get_thread_note`) only happen on the handful of turns that open a conversation.

The elapsed phrase is computed from message timestamps and never read out of the note prose — K21 notes carry their own dates and those are not reliable (the live store has one opening "Jacob fell asleep on June 29, 2026" on a thread whose messages are all from August).

Pure renderer: [`app/core/session/session_continuity.py`](../app/core/session/session_continuity.py). Tests: [`tests/test_session_continuity.py`](../tests/test_session_continuity.py) (both tails and the window boundary, missing note, unparseable timestamp, display-name fallback, elapsed-not-quoted-from-note) and `ContinuitySlotTests` in [`tests/test_prompt_assembler.py`](../tests/test_prompt_assembler.py) (lands on a fresh conversation, quiet once it stands alone, silent on the first conversation ever, `0` disables, `latest_other_session` ordering).

### Brain orchestration — long-running tasks (schema v16)

Phase 1 of the brain-orchestration refactor. Lets Aiko spawn user-initiated long-running work (file search / read for now; web browser + research in later phases) without blocking the conversation. Every input — typed message, voice turn, task completion, scheduler wake — flows through one priority queue (`BrainEventQueue`) drained by a single consumer thread (`BrainLoop`) whose free-to-speak gate guarantees task completions never cut Aiko off mid-sentence. See [`docs/brain-orchestration.md`](brain-orchestration.md) for the full design + data-flow diagram.

- `agent.tasks_enabled` *(bool, `true`)* — master switch for the whole task subsystem. Off → the `start_*` tools are hidden from the LLM, `TaskOrchestrator.start_task` rejects with `reason=disabled`, and the cue / escalation paths stay silent. Existing rows in the `tasks` table are untouched.
- `agent.tasks_per_user_cap` *(int, `8`, min `1`)* — max concurrent `running` + `awaiting_input` rows per user. Higher → more parallel tasks per user (and more memory + WS chatter). Lower → tighter back-pressure on long-running work. Hit a cap → WARNING line `task spawn rejected: reason=per_user_cap`.
- `agent.tasks_resume_on_boot` *(bool, `true`)* — when on, non-terminal task rows surviving a restart get demoted to `interrupted` AND a cue is parked for Aiko's next turn ("the X task stopped — want me to retry?"). Off → rows still demote on boot but Aiko stays silent; user has to ask via REST / UI.
- `agent.tasks_running_block_enabled` *(bool, `true`)* — when on, `InnerLifeProvidersMixin._render_running_tasks_block` renders a T6 prompt block listing live tasks for the active user. Off → block is silent; Aiko has no inner-prompt awareness of her own running work (only the TaskStrip in the UI does).
- `agent.brain_loop_deferred_grace_ms` *(int, `100`, clamped `[10, 5000]`)* — `BrainLoop` poll interval in milliseconds. Smaller → deferred items retry sooner when the free-to-speak gate clears (lower latency on the no-interrupt invariant). Larger → consumer thread wakes less often on idle, at the cost of post-TTS escalation latency. Default `100` ms.
  - **Note (timed-escalation retirement):** the old `agent.task_completion_proactive_after_seconds` (45 s), `agent.task_input_needed_proactive_after_seconds` (20 s), and `agent.task_reply_when_free_seconds` (1 s) windows have been removed. Reporting is now decided by the C6 worker verdict (`surface_now` / `park_for_natural_opening` / `drop`, see below) and floor (user-requested) tasks always surface. An armed cue fires the moment Aiko is free to speak — there is no fixed silence window. `task_input_needed` is UI-only (the TaskStrip surfaces the `awaiting_input` chip; Aiko does not speak the question). The escalation manager's internal retry cadence (poll-until-free) is a constant, not a setting.
- `agent.task_cue_max_age_seconds` *(int, `1800`, clamped `[60, 86400]`)* — wall-clock age above which a parked cue silently drops on the next dequeue / sweep. Protects against awkward stale-context messages ("the YouTube tab I opened 3 hours ago is still going") if the user vanished. Default `1800` = 30 minutes.
- `agent.task_cue_max_aggregated` *(int, `5`, clamped `[1, 20]`)* — hard cap on cues rendered into a single turn's prompt T6 block. Excess cues stay in the DB / WS strip (so the user sees them in the UI), but get dropped from the prompt to keep T6 cheap. The most volatile tier never gets cache hits, so trimming pays off.

Verification: `tail_logs(module_contains="brain_loop")` for dispatch / defer / escalation lines; `tail_logs(module_contains="task_orchestrator")` for spawn / transition / completion / cue lifecycle lines. MCP tools planned for chunk 5+: `list_tasks`, `get_brain_loop_state`, `get_brain_queue_state`. Tests cover settings clamps in `tests/test_settings.py::TaskOrchestrationSettingsTests`, cue-store invariants in `tests/test_task_cue_store.py`, escalation timer behaviour in `tests/test_task_escalation.py`, and the no-interrupt invariant end-to-end in `tests/test_brain_loop_gate.py`.

---

## `memory` — `MemorySettings`

Long-term memory: cross-session vector store of durable facts, plus the tiered (`scratchpad` / `long_term` / `archive`) lifecycle introduced in schema v8.

### Core memory

- `memory.enabled` *(bool, `true`)* — master switch. Off → no RAG, no extraction, no decay. Aiko becomes goldfish.
- `memory.top_k` *(int, `6`, min `0`)* — number of memories retrieved per turn. Higher → richer recall, more prompt tokens; lower → terser, more likely to forget relevant context.
- `memory.score_threshold` *(float, `0.4`, clamped `[0, 1]`)* — minimum cosine for a memory to be eligible for retrieval. Higher → stricter; lower → noisier.
- `memory.max_memories` *(int, `5000`, min `50`)* — cap on the `long_term` tier. Higher → keeps more history (sub-millisecond NumPy + sub-linear LanceDB stay fast).
- `memory.dedupe_threshold` *(float, `0.92`, clamped `[0.5, 0.999]`)* — cosine threshold above which a newly written memory is merged into an existing row. Higher → merges only near-identical rows; lower → can collapse distinct facts.
- `memory.restate_threshold` *(float, `0.85`, clamped `[0.5, 0.999]`)* — the second, narrower dedupe gate: the cosine floor at which a fact **restated shortly after** the first telling is merged instead of given its own row. Deliberately below `dedupe_threshold`, and only safe because three further conditions come with it — see [restatements](memory.md#restatements) for why the pair of thresholds exists.
- `memory.restate_window_hours` *(float, `6.0`, min `0`)* — how far apart two rows may be written and still count as a restatement. `0` disables the narrow gate entirely, leaving only `dedupe_threshold`. This is the condition doing most of the work: same-kind pairs a few hours apart are overwhelmingly rewordings, while similarly-scoring pairs a day or more apart are usually distinct facts sharing a frame.
- `memory.extractor_enabled` *(bool, `true`)* — master switch for the post-summary `MemoryExtractor`. Off → only `[[remember:]]` tags + manual UI adds write memories.
- `memory.self_tagged_salience` *(float, `0.7`, clamped `[0, 1]`)* — default salience for memories written from `[[remember:]]` tags.

### Unified context budget (`relevant_context` region)

One turn-relevance-scored selector fills a shared token budget with a variable mix of **memories + topic clusters + concepts** (the T3 `relevant_context` region), replacing the old three fixed caps (memory `top_k`, interest-map top-N-by-size, concept top-N-by-confidence). The budget is a fraction of the context window (absolute-capped) so it auto-scales from 64k local models up to large cloud windows, and it is **reserved before history is packed** — on overflow history is squished first while surfacing degrades gracefully and last. Full design + sizing math + the degradation ladder live in [`docs/context-budget.md`](context-budget.md).

- `memory.context_budget_enabled` *(bool, `true`)* — master switch. Off → the `relevant_context` region is skipped entirely (no memories / clusters / concepts surfaced).
- `memory.context_budget_fraction` *(float, `0.15`, clamped `[0.0, 0.8]`)* — share of the context window reserved for surfacing. `0.15` → a 64k window reserves ~9.8k (capped below), a 200k window hits the absolute cap. Higher → more remembered context per turn (fewer history tokens); lower → leaner surfacing.
- `memory.context_budget_max_tokens` *(int, `4096`, min `0`)* — absolute ceiling regardless of window size; stops a 200k model from surfacing "200k of memories".
- `memory.context_budget_min_tokens` *(int, `256`, min `0`)* — floor so surfacing never vanishes on small windows (subject to the history floor below).
- `memory.context_budget_history_floor_tokens` *(int, `1024`, min `0`)* — protected history slice: surfacing is clamped so the conversation is never starved below roughly this many tokens.
- `memory.context_budget_memory_pool_k` *(int, `18`, min `0`)* — candidate pool size the selector picks memories from (wider than the surfacing cap so relevance has room to work). This now also widens the retriever's **per-source fan-out** for the candidate pass, so raising it genuinely deepens memory recall instead of being silently capped at the small `retrieve` default. Also the pool size the `RagPrefetcher` warms. (Memories still pass the `memory.score_threshold` gate first — lower that to admit more borderline-relevant hits into the pool.)
- `memory.context_budget_memory_floor` / `_cap` *(int, `1` / `8`, min `0`)* — guaranteed-minimum and hard-maximum memory items in the region.
- `memory.context_budget_memory_weight` *(float, `1.0`, min `0`)* — relevance multiplier biasing memories in the shared greedy fill.
- `memory.context_budget_memory_min_relevance` *(float, `0.0`, clamped `[0, 1]`)* — turn-relevance (cosine) floor below which a memory candidate is dropped.
- `memory.context_budget_cluster_floor` / `_cap` *(int, `0` / `3`, min `0`)* — topic-cluster item floor / cap.
- `memory.context_budget_cluster_weight` *(float, `0.9`, min `0`)* — cluster relevance multiplier.
- `memory.context_budget_cluster_min_relevance` *(float, `0.30`, clamped `[0, 1]`)* — cluster relevance floor. (The cluster **min-size** quality gate still lives at the topic graph's `min_cluster_size`.)
- `memory.context_budget_concept_floor` / `_cap` *(int, `0` / `3`, min `0`)* — concept item floor / cap.
- `memory.context_budget_concept_weight` *(float, `1.1`, min `0`)* — concept relevance multiplier (biased slightly above memories so a strongly-matching learned belief can win a slot).
- `memory.context_budget_concept_min_relevance` *(float, `0.30`, clamped `[0, 1]`)* — concept relevance floor. (The concept **confidence** gate still lives at the candidate layer — only `status="active"` concepts are considered.)
- `memory.context_budget_core_cap` *(int, `2`, min `0`)* — **always-on core lane (L27)**: up to this many high-confidence concepts are *pinned* into the region every turn regardless of turn relevance — this is how "who the user is, what they + Aiko value, how she wants to behave" reaches the prompt even when it doesn't match the topic. Which kinds participate is declared per-kind in the `ConceptKind` registry (`core_always_on`); the picks are **balanced across kinds and subjects** so no one kind (or subject) crowds out the rest, and Aiko's self-model (`subject=aiko`) surfaces alongside the user-model. Pinned concepts bypass the concept `_cap` and `_min_relevance`, so they enrich on top of the relevance picks (they still consume the shared token budget). `0` disables the lane. *(The legacy `context_budget_identity_cap` key still parses into this one.)*
- `memory.context_budget_core_min_confidence` *(float, `0.75`, clamped `[0, 1]`)* — global fallback confidence bar a concept must clear before it is eligible for the pinned lane, so only settled core beliefs are asserted every turn. A kind may set its own `core_min_confidence` in the registry (e.g. a higher bar for behaviour-loaded value/boundary kinds); this setting applies to kinds that don't. *(The legacy `context_budget_identity_min_confidence` key still parses into this one.)*
- `memory.hypothesis_surfacing_enabled` *(bool, `true`)* — **the tentative register (L30a)**: a separate, strongly-hedged group showing what Aiko is *still working out* rather than what she believes. Every other concept lane reads `status="active"` only, which hides a `candidate` instead of hedging it; this lane is the one reader of the candidate pool. Off means candidates stay invisible, exactly as before L30a.
- `memory.context_budget_hypothesis_floor` / `_cap` *(int, `0` / `2`, min `0`)* — open-question floor / cap. The cap was **1** by design — two simultaneous "I'm wondering whether…" lines read as an interview rather than a passing thought. Phase B raised it to **2** *only* because the lane now draws from two origins and offers at most one candidate per origin: a grounded open question and an invented one are different registers, so the pair does not read as an interview the way two grounded questions would. Set it back to `1` and the two origins compete for a single slot, which is a quieter but perfectly coherent configuration.
- `memory.context_budget_hypothesis_weight` *(float, `0.7`, min `0`)* — relevance multiplier, deliberately *below* the concept lane's `1.1` so an equally on-topic open question loses to a belief Aiko has actually earned.
- `memory.context_budget_hypothesis_min_relevance` *(float, `0.35`, clamped `[0, 1]`)* — how on-topic a question must be to be worth raising. Compared against plain cosine, on the same scale as the other sources.
- `memory.hypothesis_min_unsettled` *(float, `0.22`, clamped `[0, 1]`)* — how far from settling a belief must be to count as an open question, where unsettledness blends distinct-source breadth (60%) and conviction (40%) and **deliberately ignores age**. Most candidates are not doubts — they are beliefs waiting out `concept_promote_min_age_days` — and a twice-grounded, fully-confident one scores exactly `0.20`, so the default sits just above that. Lower it and the lane fills with beliefs Aiko is not actually unsure about.
- `memory.hypothesis_min_sources` *(int, `1`, min `0`)* — minimum distinct evidence sources. `0` lets in candidates with no grounding at all, which score *highest* on unsettledness precisely because nothing supports them, so the lane would lead with bare LLM hunches.

#### L30b/L30c — testing a hunch (ask, then learn from the answer)

The lane above lets Aiko *see* an open question. These settings govern the loop that *closes* one: an idle worker queues a `concept_hypothesis` cue for the belief most worth resolving, a dual-mode prompt block raises it when a fitting moment arrives, and a post-turn adjudicator folds the reply back onto that specific concept.

Selection is narrower than the L30a lane on purpose. An answer supplies a **distinct source**, so it can only move a candidate held back on sources or conviction — a row that already clears both and is merely waiting out `concept_promote_min_age_days` is skipped, because asking about it spends a question to change nothing. On the live graph that exclusion takes 42 eligible rows down to 38.

- `agent.concept_hypothesis_ask_enabled` *(bool, `true`)* — master switch for the whole loop: the worker, both surfacing paths, and the post-turn resolver. Off leaves L30a's musing lane fully intact — Aiko still holds her open questions, she just never puts one to the user.
- `memory.concept_hypothesis_interval_seconds` *(int, `1800`, min `60`)* — how often the idle worker looks for a belief worth asking about. Cheap: a `list_by(status="candidate")` read plus per-row gate probes, no LLM.
- `memory.concept_hypothesis_max_per_run` *(int, `1`, min `1`)* — cues drafted per run. One, because the shelf and the surface cooldown already pace the lane and a batch would only queue questions that expire unasked.
- `memory.concept_hypothesis_min_gap_hours` *(float, `4.0`, min `0`)* — how long a typed silence must be before the **gap** path may open with a belief probe. The topic path ignores this entirely: riding a subject the user just raised needs no lull.
- `memory.concept_hypothesis_gap_min_importance` *(float, `0.55`, clamped `[0, 1]`)* — L32 importance a hunch must carry to be worth raising **out of a lull**. A bar the topic path deliberately does not have: on-topic, any open question is fair; out of silence, only one that matters justifies the weight.
- `memory.concept_hypothesis_deny_penalty` *(float, `0.25`, clamped `[0, 1]`)* — confidence penalty applied on a `deny` **or** a `correct`, scaled by the concept's plasticity (the same `apply_contradiction_penalty` L9 uses). Both verdicts cost the same; what differs is that only a deny writes the `contradicts` edge — a near miss stays refinable. Status is never touched here: whether a knocked-down belief stops being carried remains L3's call.
- `memory.concept_hypothesis_answer_threshold` *(float, `0.45`, clamped `[0, 1]`)* — cosine floor for the semantic half of the echo gate that decides whether a reply is even *about* the belief. Only consulted for long replies; anything short goes straight to the adjudicator, since "yeah, kind of" is the archetypal answer to a hunch and shares no words with it. Low on purpose — the gate separates "answering me" from "talking about something else", and raising it silently discards real answers.

Two behaviours are policy rather than settings, in `cue_accounting.py`. `max_asks=1` — an unanswered hunch is dropped, never re-asked, because pressing someone a second time on whether a guess about them is true reads as doubting their first answer. And `surface_cooldown_hours=20.0`, which is "at most one concept-testing question per conversation" expressed as a shelf rule.

#### L30 Phase B — inventing a hypothesis

Everything above resolves a belief that already exists. These settings govern the layer that *creates* one: an idle worker speculates about something not written anywhere, files it to its own `hypotheses` table, and the loop above tests it. A confirmed guess graduates into an ordinary `candidate` concept — see [`hypotheses.md`](hypotheses.md) for the lifecycle, the vocabulary (`credence` is not `confidence`), and the invariants.

- `agent.hypothesis_invention_enabled` *(bool, `true`)* — master switch for the proposer only. Off leaves the ask-and-learn loop working on grounded candidates and stops new inventions; rows already on the shelf still get tested.
- `memory.hypothesis_invention_interval_seconds` *(int, `5400`, min `60`)* — proposer cadence. Slower than the ask worker's because inventing costs a real LLM call and the shelf it stocks is small.
- `memory.hypothesis_invention_max_per_run` *(int, `2`, min `1`)* — rows written per batch, capped further by the remaining room under `hypothesis_max_open`.
- `memory.hypothesis_max_open` *(int, `12`, min `0`)* — hard ceiling on live (`open` or `supported`) rows, checked *before* the LLM call so a full shelf costs nothing. A hard number rather than a soft target because nothing prunes this table by decay: an untested guess is not less plausible next month, only staler. `0` disables invention as effectively as the master switch.
- `memory.hypothesis_min_novelty` *(float, `0.88`, clamped `[0, 1]`)* — cosine at or above which a proposal is rejected as a re-invention of an existing guess, **including a refuted one** (not re-inventing something the user already turned down is the repetition most worth catching). An **expired** row is the one exception and does not block: it aged out unasked, so nothing was learned about the guess, and letting it block would retire that ground permanently over Aiko's own inattention. Sits *above* the concept dedupe bar of `0.86` on purpose: over-rejecting here makes the layer sterile, while letting a near-neighbour through costs one wasted row, so the two errors are not symmetric.
- `memory.hypothesis_concept_novelty` *(float, `0.82`, clamped `[0, 1]`)* — the separate bar against the **concept** graph, of any status. Stricter, because the failure it prevents is worse: "I wonder whether he likes building things" about a belief she has held for a month is not a duplicate wondering, it is Aiko forgetting what she knows out loud. Searched across kinds within the subject, since the proposer's guessed kind carries no authority.
- `memory.hypothesis_ttl_hours` *(float, `336.0`, min `0`)* — how long a guess that was **never asked about** may sit before closing as `expired`. A row that *was* put to the user never ages out — it either has an answer or has one pending, and a clock should not settle either. `0` disables expiry.
- `memory.hypothesis_graduate_min_support` *(int, `2`, min `1`)* — independent confirmations a guess needs before it may become a concept. Two, because it was invented from nothing and one polite "yeah, sure" should not turn a fancy into part of what Aiko knows. A single refutation disqualifies a row outright regardless of how many confirmations sit beside it.
- `memory.hypothesis_graduate_min_credence` *(float, `0.7`, clamped `[0, 1]`)* — credence the row must also carry to graduate.
- `memory.hypothesis_credence_step` *(float, `0.2`, clamped `[0, 1]`)* — how far one answer moves credence. Symmetric between confirm and deny, unlike the concept side: there is no evidence graph underneath to make a confirmation cheaper than a denial. A `correct` costs half a step, because Aiko was close enough that the user bothered to refine her guess rather than reject it.
- `tools.recall_hypotheses` — the read side, documented with the other tool flags below.

The two master switches — `agent.hypothesis_invention_enabled` and `agent.concept_hypothesis_ask_enabled` — are the **only** hypothesis settings a live `PATCH /api/settings` can change, and they arrive in the `companion` block. Both workers re-read `settings.agent` on every tick, so a toggle takes effect on the next one. Everything numeric above (the two cadences, `max_per_run`, `max_open`, both novelty bars, the TTL) is captured when the workers are constructed, so it needs a restart; Settings → Memory → Hypotheses exposes the two toggles and marks the rest read-only rather than offering a control that would appear to work.

#### Cognitive surfacing (L23) — how concepts are *chosen* per turn

The turn-relevant concept lane is scored by a per-kind blend (`ConceptKind.surface_weights`) that models how a mind brings a thought forward: cosine + confidence + recency + a **stability** term (`confidence × plasticity-adjusted`, so a settled/sticky belief ranks on how firmly it's held) + an **emotional/recent-change salience** bump + an additive **spreading-activation** boost, all damped by a repetition-suppression **habituation** multiplier. Defaults are context-only per kind, so an untuned kind ranks exactly as before.

- **Habituation (repetition suppression / anti-nag).** A concept surfaced in the last few turns is damped so surfacing rotates instead of nagging. Turn clock is `relationship.total_turns`; state persists in `kv_meta` under `concept.surfacing_habituation`.
  - `memory.concept_surfacing_habituation_enabled` *(bool, `true`)* — master switch.
  - `memory.concept_surfacing_habituation_window_turns` *(int, `4`, min `0`)* — how many turns the suppression lasts (strongest at "surfaced last turn", recovering to none across the window). `0` disables.
  - `memory.concept_surfacing_habituation_floor` *(float, `0.35`, clamped `[0, 1]`)* — strongest suppression multiplier on the **flex** (turn-relevant) lane; lower = a just-surfaced concept steps aside harder.
  - `memory.concept_surfacing_core_habituation_floor` *(float, `0.8`, clamped `[0, 1]`)* — gentler floor on the **always-on core lane**: the lane over-fetches and *rotates* which core concepts show, but never suppresses the sole qualifier out of contention.
  - `memory.concept_surfacing_state_cap` *(int, `300`, min `0`)* — max concepts kept in the persisted last-surfaced map (pruned to the most recent).
- **Salience (emotional / recent-change intrusion).** A concept with a sharp recent lifecycle event (`contradicted` / `plasticity_shift` / `revived` / `promoted`) gets an intrusion bump on the flex lane, fading over the per-kind `salience_halflife_days`. Only kinds with a non-zero `salience` weight (boundary, affective) are affected.
  - `memory.concept_surfacing_salience_enabled` *(bool, `true`)* — master switch.
  - `memory.concept_surfacing_salience_event_scan` *(int, `120`, min `0`)* — how many recent timeline events are scanned per turn to build the per-concept charge map.
- **Spreading activation (associative priming).** Concepts that share a *hot topic cluster* with the turn's active set (the pinned core) — or, once meta concepts exist, are referenced by them — are pulled into the candidate pool with an additive boost, so a related idea can surface even at low direct cosine. Only kinds with a non-zero `activation` weight (identity/value/affective/boundary) are lifted.
  - `memory.concept_surfacing_activation_enabled` *(bool, `true`)* — master switch.
  - `memory.concept_surfacing_activation_seed_cap` *(int, `4`, min `0`)* — max seed concepts (from the pinned core) whose neighbours are expanded.
  - `memory.concept_surfacing_activation_max` *(int, `4`, min `0`)* — max activated neighbours pulled into the pool per turn.

#### Openness and worker diets (L28) — what she can *reach past*

Ranking concepts by strength converges on the kinds that constrain her: `boundary` carries an importance prior of `0.9` and `value` `0.85`, so the top of any strength-ordered list is a set of rails. These settings are the three places that is corrected — the pinned lane, the per-turn lane, and what background workers read. Background: [`docs/concept-integration.md`](concept-integration.md) (role axis, diets, both brain surfaces).

- `memory.concept_core_openness_slots` *(int, `2`, min `0`)* — **openness reserve** on the always-on core lane. `core_lane_kinds()` returns only `core_always_on` kinds (`identity`, `value`, `boundary`, `generalization` — two anchors and two guides), so before this the lane was *structurally* incapable of pinning an aspiration, taste or pursuit no matter how the weights were tuned. This many slots are reserved for the strongest `generative`-role concepts, drawn from kinds otherwise ineligible for the lane. Cheap against a `context_budget_core_cap` of 15. An unfillable slot falls back to the ordinary lane, so the reserve never wastes a pin. `0` restores the guides-and-anchors-only lane exactly. The slots spread across *kinds* first and subjects second — with a flat draw the two `aspiration` subject buckets took every slot on the live graph and no other generative kind could be reached — and `tension` is excluded outright, since a kind the T3 renderer drops cannot hold a pin.
- `memory.concept_core_openness_min_confidence` *(float, `0.5`, clamped `[0, 1]`)* — the bar a reserved pick must clear. Pinning a half-formed aspiration into *every* turn is worse than pinning nothing. Deliberately below the settledness the guide kinds are held to (`value` 0.85, `boundary` 0.8): the point of the reserve is that something unfinished gets a seat.
- `memory.concept_flex_generative_floor` *(int, `1`, min `0`)* — **generative floor** on the per-turn flex lane, which is tilted rather than closed. Any kind can reach it, but `surface_score` ends in `importance_factor`, which at the default `concept_importance_strength` of `0.4` is ×1.16 for `boundary` against ×0.92 for `taste` — a 26% head start on every comparison, only partly offset by habituation. When the ranked pick contains no generative concept and at least one generative candidate cleared the relevance floor, the weakest selected **guide** is swapped for the strongest generative one. Never an anchor: losing a boundary from one turn is recoverable, losing the identity concept that says who she is talking to is not. A floor rather than a lower `concept_importance_strength` because the tilt is usually right and this fires only in the case where it isn't. `0` disables. Watch the `roles.floor_fired` flag on the concept trace — a floor that fires most turns is itself the finding.
- `memory.concept_diet_token_fraction` *(float, `0.06`, clamped `[0, 0.8]`)* — share of the **worker** context window a diet's concept section may occupy, before `weight`. Clamped like `context_budget_fraction`: a worker whose prompt is four-fifths concepts has no room left for the thing it was asked to reason about.
- `memory.concept_diet_max_tokens` *(int, `600`, min `0`)* — cap on that share, and in practice the knob that actually sizes a diet. On a 64k worker route even 6% is ~3,900 tokens — 200-plus concept lines, likely more than the active store holds — so the fraction alone would never bind and every diet would quietly mean "all of them". The fraction protects a *small* window; the cap sizes a large one. At the defaults a typical diet lands around 30 concepts, a `weight=2.0` reflection pass around 60, a `weight=0.4` cue worker around a dozen.
- `memory.concept_diet_min_tokens` *(int, `150`, min `0`)* — floor after `weight` is applied, so a light diet on a small window still gets a usable handful rather than being silently starved.

Per-consumer appetite is **not** a setting: `kinds`, `subject`, `weight` and the `rationale` for each are declared in [`concept_diets.py`](../app/core/concepts/concept_diets.py), where the invariant that a diet naming a `guide` kind must also name a `generative` one can be enforced (`registry_problems`) and read alongside the reasoning.

To measure any of this against a real store, run [`scripts/concept_openness_report.py`](../scripts/concept_openness_report.py) — a read-only diagnostic that reports the store's role mix, runs the real `core_lane` / `for_consumer` selection against the live rows (so the reserve's fill and each diet's role mix are observed, not inferred), shows how far each generative kind's intake gate is from the value that has to clear it, and totals the concept assertions one turn carries.

#### The T0 profile block's concept lead (L28 / L39)

- `memory.profile_concept_max_lines` *(int, `4`, min `0`)* — how many `subject="user"` identity / value concept bullets lead the profile block, ahead of the structured SQLite fields. `0` disables the lead entirely (pure SQLite profile). This is the one concept surface with **no rotation**: it sits in the T0 cache prefix, so it cannot take a habituation read without becoming a third volatile T0 block and breaking the `_PROMPT_BLOCK_TIERS` prefix ladder. It was `10`, which measured as ~620 tokens of identical always-on assertion every turn (concept labels are full sentences) selected by confidence band from ~170 eligible rows — the same ten leading the block indefinitely. Because this block **claims first** (T3 is built after it, so the turn-relevant lane cannot win the precedence), those lines also pre-empted two thirds of the 15-slot core lane, which *does* rotate and carries the openness reserve. At `4` the released beliefs still reach the prompt, through the lane that rests them.
- `memory.profile_concept_min_confidence` *(float, `0.5`, clamped `[0, 1]`)* — the bar a concept must clear to appear there. Low by design: the cap, not the bar, is what sizes this block, since a settled trait at 0.6 is exactly what the profile is for.

#### Self-tuning concept gates (L45)

Several of the thresholds above are **calibrated automatically** against the live concept distribution, because a constant chosen on one graph is usually in the wrong place on another. A gate declares an *intent* (admit roughly this share of the candidate pool; leave an eligible pool this many times the lane cap; stay under what the population can actually reach) and a daily worker solves for the value that hits it, walking there in small steps between hard floor / ceiling rails. The values in this document remain the **defaults**, used until the graph is old and large enough to have a distribution worth reading.

Resolution order is **default → tuned → user**: anything you set explicitly under `memory` in `config/user.json` always wins, is never overwritten, and no background pass ever edits that file. An overridden gate is still measured, and its *drift* from your value is recorded so you can see what the data would have said.

Learned values live in `data/tuning/concept_gates.json` alongside the statistics behind each one; a per-run snapshot of the concept population goes to `data/tuning/concept_population.jsonl`. Read them with `python scripts/concept_gate_report.py` (a dry run against the live database, writing nothing) or `--trend` for the history; the `get_gate_tuning` / `force_gate_tuning` MCP tools are the live equivalents. Design notes: **L45** in [`docs/personality-backlog/concepts.md`](personality-backlog/concepts.md).

Only **read-side** gates are applied — the ones that decide what enters a single prompt (`context_budget_core_min_confidence`, `concept_core_openness_min_confidence`, `profile_concept_min_confidence`). Gates that *write* to the concept store (promotion, dormancy, retirement, taste synthesis), the thirteen per-kind promotion floors and the three cosine bars are measured and recorded but **never applied**: a bad read value costs one turn, while a bad write value leaves a durable trace and also moves the distribution the next run measures.

- `memory.concept_gate_tuning_enabled` *(bool, `true`)* — master switch. Off means every threshold holds its configured value forever, and nothing is measured or recorded.
- `memory.concept_gate_tuning_heartbeat_seconds` *(int, `21600`, min `600`)* — how often the idle scheduler *considers* the worker, which is deliberately **not** how often it runs. The scheduler admits an over-budget worker only once it is three of its own heartbeats overdue, so a 24-hour heartbeat on a machine that sleeps overnight risks a three-day gap; a six-hour heartbeat with a daily internal cadence gets the same once-a-day work with an 18-hour worst case.
- `memory.concept_gate_tuning_cadence_seconds` *(int, `86400`, min `3600`)* — the real spacing between tuning runs, enforced by a `kv_meta` key. An overdue tuner catches up **once** on the next opportunity rather than backfilling a run per missed day.
- `memory.concept_gate_tuning_cosine_pairs` *(int, `4000`, clamped `[0, 50000]`)* — pairs drawn for the similarity-distribution sample the cosine gates are observed against. Exhaustive comparison is quadratic (~420k pairs at 900 active concepts) while all those gates need is the distribution's *shape*; successive runs draw fresh pairs, so the picture sharpens without any single run paying for it. `0` skips the sample, which is the knob to reach for if the worker's duration becomes a problem.

### Tier lifecycle (schema v8)

- `memory.tiers_enabled` *(bool, `true`)* — master switch for the tiered lifecycle. Off → behaves like the old flat-pool design.
- `memory.decay_rate_scratchpad` *(float, `0.05`)* — salience decay/day for the `scratchpad` tier. Higher → scratchpad rows fade faster.
- `memory.decay_rate_long_term` *(float, `0.02`)* — salience decay/day for `long_term`.
- `memory.decay_rate_archive` *(float, `0.0`)* — salience decay/day for `archive`. `0` keeps cold history frozen.
- `memory.revival_coefficient` *(float, `0.05`)* — per-day salience rebate proportional to `revival_score`. Higher → revived memories regain salience faster.
- `memory.revival_per_hit` *(float, `0.15`)* — bump applied to `revival_score` when Aiko's reply cites enough keywords from a surfaced memory.
- `memory.revival_decay_per_day` *(float, `0.02`)* — daily fade of `revival_score` itself.
- `memory.revival_min_word_overlap` *(int, `3`, min `1`)* — minimum content-word overlap between Aiko's reply and a surfaced memory to count as a citation. Higher → stricter; lower → noisier.
- `memory.semantic_revival_enabled` *(bool, `true`)* — F12. When the keyword test above misses, fall back to cosine between Aiko's reply and the stored memory. Catches paraphrase, which the lexical test structurally cannot: since paraphrasing is the whole reason to hand a memory to an LLM, nearly all of that test's errors are misses. Reuses the reply embedding computed once per post-turn, so there is no extra embed call.
- `memory.semantic_revival_min_cosine` *(float, `0.62`, clamped `[0, 1]`)* — floor for a semantic hit; `0` disables the fallback. **Deliberately high, and still a guess.** Surfaced memories were selected for topical similarity to the turn in the first place and the reply is about that same turn, so cosine here partly measures "was on topic" rather than "she used it". The raw cosine of every comparison — misses included — is recorded in the L37 ledger; run the `get_surfacing_outcomes` MCP tool and read `semantic_floor_replay` to re-derive this from your own history.
- `memory.semantic_revival_per_hit` *(float, `0.05`, clamped `[0, 1]`)* — smaller bump for a semantic hit than for a quote, because it is weaker evidence. Must stay **below** `scratchpad_ttl_min_revival` or a single on-topic coincidence exempts a memory from cleanup forever.
- `memory.scratchpad_ttl_days` *(int, `14`, min `1`)* — scratchpad rows never promoted within this many days are deleted.
- `memory.scratchpad_ttl_min_revival` *(float, `0.10`, clamped `[0, 1]`)* — an unused scratchpad row survives TTL when `revival_score >= this`. Replaced an exact `revival_score == 0.0` test, which was brittle (float equality against a decaying value) and, once semantic revival existed, far too generous. Sits above `semantic_revival_per_hit` and at or below `revival_per_hit`, so a **quoted** memory is spared exactly as before while a merely on-topic one is not; two semantic hits also clear it. Setting this to `0` keeps the original "delete only rows with no revival at all" meaning rather than disabling cleanup.
- `memory.scratchpad_promote_min_age_days` *(int, `7`, min `0`)* — minimum age before scratchpad → long_term promotion is considered.
- `memory.scratchpad_promote_min_use_count` *(int, `3`, min `0`)* — minimum surface count for promotion via use.
- `memory.scratchpad_promote_min_revival` *(float, `0.3`, clamped `[0, 1]`)* — alternate promotion path: `revival_score >= this` AND past `min_age_days` triggers promotion without use-count.
- `memory.archive_demote_idle_days` *(int, `180`, min `1`)* — long_term rows unused for this many days drop to archive.
- `memory.scratchpad_cap` *(int, `1000`, min `50`)* — hard cap on scratchpad rows.
- `memory.archive_cap` *(int, `10000`, min `50`)* — hard cap on archive rows.
- `memory.decay_max_catchup_days` *(float, `30.0`, min `1`)* — safety clamp: even if the app was offline for months, a single decay tick won't apply more than this many days' worth at once.

### K7 — forgetting protocol

Renders a `(faded)` suffix on the RAG memory block for old / decayed rows so the persona reads them as half-remembered instead of as crisp current facts. Fires for archive-tier rows AND for long_term rows that have decayed in place (low salience AND idle for a while). Implementation lives in `_is_faded_memory` inside [`app/core/rag/rag_retriever.py`](../app/core/rag/rag_retriever.py); the persona rule that turns the suffix into a soft hedge lives in [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt).

- `memory.fade_hedge_enabled` *(bool, `true`)* — master switch. Off → no `(faded)` suffix ever, including archive-tier rows. Use when you want Aiko to speak from memory without ever hedging "I think you said this once, ages ago…".
- `memory.faded_salience_threshold` *(float, `0.20`, clamped `[0, 1]`)* — salience floor for a long_term row to register as faded. Higher → more aggressive hedging on lukewarm memories; lower → only very faded rows hedge. Strict `<` semantics — a row sitting exactly on the threshold does NOT fade. Archive-tier rows ignore this and always fade when the master switch is on.
- `memory.faded_idle_days` *(int, `30`, min `1`)* — minimum days since `last_used_at` (or `created_at` if the row has never been touched) before a low-salience long_term row picks up `(faded)`. Strict `>` semantics: a row idle for exactly 30 days does NOT fade. Higher → only very stale rows hedge; lower → more aggressive hedging.

### K22 — callback / inside-joke detector

Post-turn cosine pass between Aiko's reply and older eligible memories. Hits stamp `metadata.callback_count` and bump `salience` + `revival_score` so the retriever's read-side bonus (`_RAG_CALLBACK_BONUS`) prefers memories Aiko has actually managed to weave back into a reply over equally-relevant siblings that have never been cited. The reinforcement is **invisible to the LLM by design** — explicit awareness would lead to meta-narration ("hey, glad I remembered that thing"); the point is for the callback to feel organic. Implementation lives in [`app/core/conversation/callback_detector.py`](../app/core/conversation/callback_detector.py); the RAG read-side bonus lives in [`app/core/rag/rag_retriever.py`](../app/core/rag/rag_retriever.py). The master switch [`agent.callback_detector_enabled`](#k22--callback--inside-joke-detector) only gates the *write* side — once a memory has `callback_count >= 1`, the read-side bonus stays on even if the user later disables the detector.

- `agent.callback_detector_enabled` *(bool, `true`)* — master switch for the post-turn cosine pass. Off → no new callback stamps. Earned weight on already-stamped rows is preserved.
- `memory.callback_age_floor_days` *(int, `3`, min `1`)* — minimum days since `created_at` before a memory is eligible to be counted as a callback target. Lower than this and the row is treated as part of the current thread, not a callback. Higher → only very-old rows qualify.
- `memory.callback_similarity_threshold` *(float, `0.55`, clamped `[0, 1]`)* — cosine similarity floor against the assistant-reply embedding. Same magnitude as K6 `strong_novelty`. Higher → only paraphrases-of-paraphrases trigger; lower → easier (but noisier) callbacks.
- `memory.callback_max_hits_per_turn` *(int, `3`, min `1`)* — maximum rows stamped on a single turn. Prevents a high-similarity sentence from blanket-bumping every near-duplicate row.
- `memory.callback_cooldown_hours` *(int, `24`, min `1`)* — per-row cooldown after a successful callback. A memory called back less than this ago stays silent on subsequent matches.
- `memory.callback_salience_bump` *(float, `0.05`, clamped `[0, 0.5]`)* — salience added to each hit at record time. Store clamps the result to `[0, 1]`. Drives the compounding loop alongside the read-side bonus.
- `memory.callback_revival_bump` *(float, `0.10`, clamped `[0, 1]`)* — revival_score added to each hit. Acts as a tier-promotion signal: a long_term row that keeps getting called back will trend toward salience=1.0 over the promotion worker's sweeps.

### K20 — metacognitive calibration

Post-turn classifier that detects whether `{user_name}` pushed back on / softened / affirmed Aiko's last claim, and adjusts a per-user `CalibrationState` (a global trust scalar in `[0, 1]` plus a bounded ring of topic slots). The state is read by an inner-life provider on the **next** turn — when the global score sits below `calibration_global_low_threshold` or any topic slot is below `calibration_topic_low_threshold`, Aiko sees a one-line "you've been double-checking me lately — hedge the next claim" cue. The state decays exponentially toward `calibration_baseline` so a tense afternoon doesn't sour the whole week. Implementation lives in [`app/core/affect/calibration_detector.py`](../app/core/affect/calibration_detector.py) and [`app/core/affect/calibration_store.py`](../app/core/affect/calibration_store.py); persona guidance is in the **"When {user_name} has been double-checking you"** block of [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt). K20 deliberately does **not** touch RAG retrieval scores — F3 (`memory.confidence` + `(uncertain)` suffix) already owns the per-memory accuracy lane. K20 is the *per-user / per-topic register tilt* on top of it.

- `agent.calibration_detection_enabled` *(bool, `true`)* — master switch for the post-turn classifier AND the inner-life cue. Off → no new state updates AND `_render_calibration_block` returns empty so the cue goes silent. Earned state on disk is preserved.
- `memory.calibration_baseline` *(float, `0.80`, clamped `[0, 1]`)* — score the global + topic slots decay toward in the absence of new signals. `0.80` reads as "neutral-positive" (Aiko speaks confidently by default). Lower → more reflexively hedgy after any pushback; higher → trust recovers more aggressively between sessions.
- `memory.calibration_global_low_threshold` *(float, `0.55`, clamped `[0, 1]`)* — global score floor for the generic cue. The cue fires only when `global_score < threshold`. Lower → cue is rarer (only after sustained pushback); higher → fires more readily on any drop.
- `memory.calibration_topic_low_threshold` *(float, `0.50`, clamped `[0, 1]`)* — per-topic score floor for the topic-specific cue. The topic cue wins over the global cue when both fire because it carries more actionable hedging guidance.
- `memory.calibration_half_life_days` *(float, `5.0`, min `0.1`)* — exponential half-life for the drift toward baseline. After this many days, the gap between current score and baseline halves. Topic slots use a longer half-life internally (`1.6×` global) so a learned topic stance outlives a general bad day. Higher → calibration sticks longer; lower → faster recovery.
- `memory.calibration_topic_merge_threshold` *(float, `0.78`, clamped `[0, 1]`)* — cosine similarity floor between an incoming `assistant_vec` and an existing topic centroid for the slot to absorb the signal (rather than allocate a new slot). Higher → narrower topics, more slots; lower → broader topics, fewer slots.
- `memory.calibration_softening_threshold` *(float, `0.70`, clamped `[0, 1]`)* — cosine floor between `user_vec` and the **prior** turn's `assistant_vec` for the softening detector to fire. Pairs with the hedge-token regex in an AND-gate: both must hold. Lower → looser gate (catches more rephrases at the cost of false positives); higher → only near-paraphrases trigger.
- `memory.calibration_max_topic_slots` *(int, `8`, min `1`)* — hard cap on the topic-slot ring. On overflow the slot whose `abs(score - baseline)` is smallest AND whose `last_signal_at` is oldest is evicted (the weakest signal that hasn't moved recently). Higher → finer topic resolution at the cost of memory / JSON size; lower → coarser, more global behaviour.

### K24 — sensory anchoring layer

Adaptive per-arc cadence that occasionally surfaces a one-line "small physical beat available: the {item} is right here. If a body anchor would land naturally this reply, you could {hint}…" cue so Aiko can substitute a sensory detail for an emotional statement ("pulling the blanket tighter" instead of "I hear you"). The cue **suggests** an `(item, verb-class)` pair; Aiko's voice picks the actual word. State is in-memory on the controller — there is **no DB / no persistence**, worst case after a restart is one extra beat in the first quiet window. Implementation lives in [`app/core/conversation/sensory_anchor.py`](../app/core/conversation/sensory_anchor.py); persona guidance is in the **"Small physical beats"** block of [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt). K24 reads `RoomState.posture` + `WorldStore.list_items()` + the live conversation arc; it intentionally **does not** key off `RoomState.activity` (the redundancy edge cases like "snacking + food cue" are left to the persona rule "use it only if it lands" until we observe enough fired beats to decide whether stricter gating is needed).

The per-arc cadence table is hardcoded in the module (not user-configurable): `support` / `reflection` get the highest probability (0.45) and shortest cooldown (4 turns), `casual_check_in` / `playful` are medium (0.25, 6 turns), `silly` is low (0.10, 8 turns), and `planning` is near-silent (0.05, 12 turns). The four `memory.sensory_anchor_*` knobs below scale that table globally.

- `agent.sensory_anchor_enabled` *(bool, `true`)* — master switch for the entire cadence. Off → `_render_sensory_anchor_block` short-circuits to empty string and no beats are ever offered. Per-arc table + recent-slugs ring on disk are not affected (there's nothing on disk).
- `memory.sensory_anchor_min_turn_gap` *(int, `4`, min `1`)* — global cooldown floor between beats. The per-arc table specifies its own cooldown; the effective cooldown is `max(arc_min, min_turn_gap)`. Raise to make beats rarer overall while keeping the per-arc shape intact; lower to honour the per-arc cooldown verbatim. Setting this to a very high number (e.g. `30`) effectively disables the feature without flipping the master switch — useful for testing.
- `memory.sensory_anchor_probability_scale` *(float, `1.0`, clamped `[0.0, 2.0]`)* — multiplier on the per-arc probability. `1.0` ships as designed; `0.5` halves every band (rarer beats across the board); `2.0` pushes `support`'s 0.45 → 0.90, near "fires whenever cooldown is clear and an item is eligible." Useful for A/B testing whether the body beat reads as presence or performance.
- `memory.sensory_anchor_max_recent_items` *(int, `4`, min `1`)* — no-repeat ring size. After firing on the tea pot, that slug stays out of the candidate pool until `max_recent` other items have fired (or the deque overflows). Higher → more variety required, lower → more repetition tolerance. A ring of `1` allows back-to-back fires on the same item; a ring of `10` in a small room (~5-7 items) means most items will be skipped most of the time.
- `memory.sensory_anchor_max_window_items` *(int, `6`, min `1`)* — hard cap on how many room items the selector considers per tick. The world is small today (~10 items per location), but this protects future "100-item garden" scenarios from a quadratic blow-up in the weighted sample step. Lower → only the first N items the world_store returns are eligible (effectively biased toward low-ID, older items); higher → all items get a fair shot.

The cue is **not** added to the K16 grounding-line suppression matrix: the fused grounding paragraph only ever says "you're sitting at the desk" and never enumerates specific items + verb classes, so K24 is additive on top, not redundant. It **is** dropped under `aggressive=True` (when the prompt-assembler is over-budget): body texture is the first thing to go when context is tight. MCP debug tools `get_sensory_anchor_state` (preview a beat without arming the cooldown) and `force_sensory_anchor` (bypass dice + cooldown, emit one beat) are available for end-to-end testing.

### Memory background workers

Idle LLM workers were retuned to run more often (they no longer block the brain and local-LLM headroom is ample); real spend stays bounded by each worker's `per_hour_cap` / `per_day_cap`.

- `memory.promotion_worker_interval_seconds` *(int, `1800`, min `10`)* — `MemoryPromotionWorker` cadence. Drop to ~60 for active testing.
- `memory.decay_worker_interval_seconds` *(int, `1800`, min `10`)* — `MemoryDecayWorker` cadence. Workers are idempotent; running more often is safe but wastes a little CPU.
- `memory.fact_checker_interval_seconds` *(int, `300`, min `30`)* — F1 `IdleFactChecker` cadence. Defaults to 5 min so newly written memories get verified mid-session.
- `memory.schedule_learner_interval_seconds` *(int, `86400`, min `60`)* — G2 schedule-learner cadence. Once a day is plenty.
- `memory.idle_curiosity_interval_seconds` *(int, `1800`, min `60`)* — G3 idle-curiosity-worker cadence.
- `memory.curiosity_seed_interval_seconds` *(int, `3600`, min `60`)* — K9 curiosity-seed-worker heartbeat. A liveness backstop rather than a cadence: what actually schedules the worker is the deficit against `curiosity_seed_max_active`.
- `memory.conflict_detector_interval_seconds` *(int, `1800`, min `60`)* — F5 conflict-detector cadence.
- `memory.belief_worker_interval_seconds` *(int, `1200`, min `60`)* — K2 belief-inference-worker cadence.
- `memory.promise_worker_interval_seconds` *(int, `600`, min `60`)* — Phase 3c promise-extraction-worker cadence.
- `memory.forward_curiosity_interval_seconds` *(int, `900`, min `30`)* — forward-curiosity-worker cadence.
- `memory.promise_followthrough_interval_seconds` *(int, `900`, min `30`)* — K43 promise-follow-through-worker cadence.
- `memory.goal_reflection_interval_seconds` *(int, `3600`, min `60`)* — K1 `GoalWorker` cadence. Once an hour gives every goal a daily-ish reflection at the default `goal_max_active=5`. Drop to ~60 for an active testing loop; raise for a calmer cadence.

### F5 — conflict detector thresholds

- `memory.conflict_detector_similarity_min` *(float, `0.80`, clamped `[0, 1]`)* — pairs below this are topically too distant to bother checking.
- `memory.conflict_detector_similarity_max` *(float, `0.92`, clamped `[0, 1]`)* — pairs at-or-above this are dedupe-likely (would already have merged at write time).
- `memory.conflict_detector_auto_resolve_delta` *(float, `0.30`, clamped `[0, 1]`)* — when the confidence gap between two halves of a confirmed conflict is at least this big, the worker auto-demotes the loser instead of surfacing to the Conflicts tab. Higher → more cautious (more conflicts surface to UI); lower → more eager auto-resolution.
- `memory.conflict_detector_max_corpus` *(int, `1000`, min `10`)* — cap on the candidate corpus. The all-pairs loop is O(n²); this bounds it.
- `memory.conflict_detector_max_pairs_per_run` *(int, `50`, min `1`)* — cap on heuristic + LLM pairs per tick.

### K3 — routine thresholds

- `memory.routine_min_touches` *(int, `3`, min `1`)* — minimum **distinct ISO weeks** a `(weekday, bucket)` slot must light up. Lower for testing; never below 1.
- `memory.routine_min_share` *(float, `0.30`, clamped `[0, 1]`)* — proportional floor: slot must appear in at least this share of weeks in the rolling window. With a 30-day window that's 2 of ~5 weeks.
- `memory.routine_max_active` *(int, `5`, min `1`)* — cap on named routines written to the `routines` profile field. The 240-char `ProfileEntry` cap is the hard upper bound.

### K2 — belief thresholds

- `memory.belief_worker_lookback_turns` *(int, `12`, min `1`)* — how many recent **user** messages the worker passes to the LLM per extraction. Larger → richer signal at the cost of tokens.
- `memory.belief_worker_interest_top_n` *(int, `5`, min `0`)* — **K65b.** How many of the densest K9 topic clusters (by member count) are folded into the extraction prompt as a "prioritise these interests" hint. `0` disables the interest hint without touching `agent.belief_interest_bias_enabled`.
- `memory.belief_worker_reconsider_max` *(int, `3`, min `0`)* — **K65b.** Max stalest active beliefs sitting on a high-mass interest that get nominated for an in-prompt "still true?" re-check each tick (rides the same LLM call, no extra spend). `0` disables the re-check track.
- `memory.belief_gap_valence_threshold` *(float, `0.30`, clamped `[0, 1]`)* — minimum `|valence_predicted - valence_observed|` for a mood-belief gap. Higher → fewer "am I reading this wrong?" beats.
- `memory.belief_gap_arousal_threshold` *(float, `0.25`, clamped `[0, 1]`)* — same for arousal.
- `memory.belief_recent_window_hours` *(int, `24`, min `1`)* — window for mood-pass predictions. Older mood beliefs age out via the stale sweep instead. Opinion beliefs have no recency window.
- `memory.belief_stale_after_days` *(int, `90`, min `1`)* — active beliefs untouched for this many days flip to `stale`.
- `memory.belief_max_active_per_user` *(int, `200`, min `10`)* — hard ceiling on `active` beliefs. The worker prunes lowest-confidence + oldest down to this cap each tick.

### K1 — long-term goal lifecycle

Caps and per-goal limits for the goal store. Together with the `agent.goal_worker_*` knobs and the `goal_reflection_interval_seconds` cadence above, these bound the size of the active goals block in the prompt and the reflection history kept per goal.

- `memory.goal_max_active` *(int, `5`, min `1`)* — cap on simultaneously-active goals. Adding a new goal past the cap archives the oldest un-pinned active one (history preserved). Higher → richer goals block, more prompt tokens; lower → tighter focus. Pinned goals don't count against the cap.
- `memory.goal_max_progress_per_goal` *(int, `12`, min `1`)* — per-goal cap on retained reflection (`goal_progress`) rows. New entries past the cap evict the oldest. The most recent note is mirrored into the parent goal's metadata so prompt rendering stays cheap. ~12 ≈ two weeks of daily reflections.

### K6 — novelty thresholds

- `memory.novelty_window` *(int, `12`, min `2`)* — size of the rolling centroid ring. Higher → smoother (slower to react to topic pivots); lower → reacts faster but noisier.
- `memory.novelty_warmup_min` *(int, `3`, min `2`)* — minimum ring size before any band is emitted. Prevents cold-start "this is novel" on the first 3 turns of every session.
- `memory.novelty_mild_threshold` *(float, `0.35`, clamped `[0, 2]`)* — distance threshold for a "mild topic shift" band. Higher → only larger shifts trigger it.
- `memory.novelty_strong_threshold` *(float, `0.55`, clamped `[0, 2]`)* — distance threshold for "strong novelty." Setting `strong < mild` falls back to single-threshold behaviour.
- `memory.novelty_cooldown_turns` *(int, `2`, min `0`)* — turns to suppress further novelty signals after a hit. Higher → quieter.

### K18 — stagnation thresholds

- `memory.stagnation_window` *(int, `6`, min `2`)* — distance samples averaged before scoring. Covers ~one conversational beat.
- `memory.stagnation_mild_threshold` *(float, `0.18`, clamped `[0, 1]`)* — mean below this reads as "we've been on this for a bit." Note the inversion vs K6: **lower mean = more stagnant**, so `strong < mild`.
- `memory.stagnation_strong_threshold` *(float, `0.10`, clamped `[0, 1]`)* — mean below this reads as "very on this." Set `strong > mild` to fall back to single-threshold.
- `memory.stagnation_cooldown_turns` *(int, `4`, min `0`)* — post-fire suppression. Longer than K6's because lulls are by nature drawn-out.
- `memory.stagnation_post_novelty_suppression_turns` *(int, `3`, min `0`)* — turns to keep K18 quiet after a K6 hit. Avoids "you just pivoted, but also you've been on this forever" weirdness.

### IdleWorkerScheduler

- `memory.idle_worker_wake_seconds` *(float, `60.0`, min `1`)* — tick cadence. Lower → workers fire sooner after a quiet period starts but increase idle CPU.
- `memory.idle_worker_quiet_threshold_seconds` *(int, `30`, min `0`)* — how long since last user activity before the scheduler considers itself idle.
- `memory.idle_worker_max_per_tick` *(int, `0`, min `0`)* — hard cap on workers per tick. `0` = unlimited (only the time budget matters); positive values clamp tick log volume on heavy backlogs.

#### Demand-driven scheduling (P36)

A worker's `interval_seconds` used to be its cadence. It is now its
**heartbeat** — a liveness backstop. What actually decides whether a
worker runs is its `demand()` probe, a read far cheaper than `run()`
that reports how much work is pending. The scheduler ranks by urgency
(70% pressure, 30% staleness) rather than by age, so a worker with real
backlog runs long before its heartbeat and one with nothing to do costs
a probe instead of a slot.

Workers that have not been migrated have no `demand()` and keep their
old interval behaviour exactly.

The mechanism as a whole — lanes, idle depth, contention grades, the fit
rules, and how to write a `demand()` probe — is documented in
[`idle-workers.md`](idle-workers.md).

**Two lanes.** `idle_worker_tick_budget_ms` was always really a
*contention* limit wearing a *time* limit's clothes: it was sized for
one local Ollama serving both the chat path and the workers, where a
background generation stole the user's next first token. So it now
governs only workers whose run calls the LLM. Everything else — pure
arithmetic, SQL sweeps, mirror scans — draws on
`idle_worker_compute_budget_ms`, which has no GPU to protect. Compute
workers are also drained *first* within a tick, so a cheap worker never
waits out a multi-second generation.

**Contention grades.** The LLM lane is additionally sized by comparing
the `main_chat` and `worker_default` routes:

| Grade | Topology | Effect on the LLM lane |
| --- | --- | --- |
| `none` | Different backends, or either side is not local Ollama | Follows idle depth, same as the compute lane |
| `queueing` | Same local Ollama, same model | Same as `none` today; kept as a distinct grade because it is diagnostically different |
| `swapping` | Same local Ollama, **different** model | Pinned at the base budget through `just_left` and `away`, because Ollama would evict the chat model to load the worker one |

**Idle depth.** The longer the user has been gone, the less a long tick
costs them, so both lanes scale by tier: `just_left` (<5 min) 1x, `away`
(<15 min) 3x, `long_away` (<1 h) 6x, `overnight` 10x. Depth survives a
restart via a wall-clock stamp in `kv_meta`; otherwise a reboot would
make an eight-hour absence look like a fresh one.

- `memory.idle_worker_tick_budget_ms` *(int, `3000`, min `0`)* — base per-tick budget for the **LLM lane**, before depth and contention scaling.
- `memory.idle_worker_compute_budget_ms` *(int, `6000`, min `0`)* — base per-tick budget for the **compute lane** (workers that touch no LLM).
- `memory.idle_worker_pressure_enabled` *(bool, `true`)* — master switch. `false` restores the pre-P36 path exactly: one budget, oldest-first ranking, no probes.
- `memory.idle_worker_urgency_threshold` *(float, `0.35`, clamped `[0, 1]`)* — minimum blended pressure/staleness for admission ahead of the heartbeat. Raise to make Aiko lazier about speculative work.
- `memory.idle_worker_min_interval_ratio` *(float, `0.1`, clamped `[0, 1]`)* — anti-thrash floor as a fraction of each worker's own interval, floored at one tick. One ratio serves intervals spanning three orders of magnitude: at `wake=30`/`ratio=0.1` a 300s worker floors at 30s while an 86400s one floors at 2.4h.
- `memory.idle_worker_depth_max_multiplier` *(float, `10.0`, min `1`)* — caps budget growth with idle depth. `1.0` disables depth scaling entirely.
- `memory.idle_worker_contention_override` *(str, `"auto"`)* — force `none` / `queueing` / `swapping` when the route topology lies, e.g. a "remote" endpoint that is really your own GPU box (which would otherwise read as split backends and quietly remove the protection). Anything unrecognised means auto-detect.

`get_idle_workers_status` reports the live depth tier, contention grade
and both effective lane budgets; `probe_idle_worker_demand` asks every
worker what it thinks it has to do right now, which is the fastest way
to find out why an idle scheduler is idle.

**Removed:** `memory.decay_worker_interval_seconds` is no longer in
`config/default.json` — `MemoryDecayWorker` now reports pressure from
engagement-clock elapsed time, so the interval is only a backstop. The
parser still honours the key if you set it in `user.json`.

---

## `audio` — `AudioSettings`

Server-side audio knobs. The browser / Tauri client owns the mic + speakers; only the parameters the server uses on the audio it **receives** remain here.

- `audio.sample_rate` *(int, `16000`)* — sample rate the STT / VAD pipeline expects (the client resamples to this).
- `audio.channels` *(int, `1`)* — channel count (mono).
- `audio.enable_microphone` *(bool, `true`)* — voice mode allowed at boot. Off → typed-only.
- `audio.vad_level_threshold` *(float, `0.02`)* — RMS energy threshold for "speech detected." Higher → more aggressive silence (drops faint speech); lower → more sensitive (picks up keyboard clicks).
- `audio.vad_silence_seconds` *(float, `1.0`)* — silence duration that closes an utterance.
- `audio.barge_in_enabled` *(bool, `false`)* — let user speech interrupt Aiko's TTS mid-reply. Off → Aiko finishes the sentence; on → her TTS stops and she listens.
- `audio.earcons_enabled` *(bool, `true`)* — play stage-direction earcons (`[[laugh]]`, `[[breath]]`, `[[sigh]]`, …). Off → those tags are silently stripped.

---

## `stt` — `SttSettings`

- `stt.enabled` *(bool, `true`)* — master switch for speech input. `false` means the Whisper recorder is **never** loaded, so the largest single resident cost in the app (weights plus RealtimeSTT's transcription child process, ~0.9 GB together) is never paid; voice input is simply unavailable and text chat is unaffected. Note that `true` does *not* mean "load at boot" — since P27 the model loads lazily, either when voice mode is first switched on (a background prewarm, so the first utterance doesn't wait on it) or on the first audio frame. So this setting is about *never* rather than *when*, and leaving it on costs nothing until you use the mic.
- `stt.model` *(string, `"large-v1"`)* — whisper model identifier. Larger → more accurate / slower / more VRAM.
- `stt.language` *(string | null, `"en"`)* — language hint. `null` = autodetect (slower, less accurate on short clips).
- `stt.device` *(string, `"auto"`, one of `auto` / `cuda` / `cpu`)* — compute device for transcription. `auto` uses the GPU when torch reports a CUDA device and falls back to CPU otherwise, which is what lets the same config boot on a GPU workstation and in the CPU-only Docker image. Pin to `cuda` or `cpu` to skip the probe. Anything unrecognised falls back to `auto`. Note RealtimeSTT's own default is a hard `cuda`, which fails outright without a usable GPU.
- `stt.compute_type` *(string, `"default"`)* — CTranslate2 quantisation. `default` lets faster-whisper choose per device (float16 on GPU, int8 on CPU). `"int8"` → much lighter and faster on CPU at some accuracy cost; `"float16"` → the usual GPU choice.

---

## `tts` — `TtsSettings`

- `tts.provider` *(string, `"pocket-tts"`)* — TTS engine. Currently `"pocket-tts"` is the supported provider.
- `tts.voice` *(string, `"aiko1_refined.safetensors"`)* — voice file used by the active engine.
- `tts.enabled` *(bool, `true`)* — master switch. Off → typed-only output. Since P28 this is also a memory setting: booting with it `false` substitutes a no-op engine, so `app.tts.pocket_tts_service` is never imported and the PyTorch CPU runtime (~0.6-1 GB resident) is never pulled in. Toggling it off at runtime is weaker — it releases the ~100M-param voice weights, but a live process cannot un-import PyTorch. Toggling back on loads the engine on a background thread; the first utterance waits on it.
- `tts.pocket_tts_voice` *(string, `"alba"`)* — Pocket-TTS voice file name (mirrors `tts.voice` for Pocket-TTS specifically). The Settings drawer keeps these in sync.
- `tts.pocket_tts_temp` *(float, `0.6`)* — Pocket-TTS sampling temperature baseline. Pocket-TTS is sensitive here; ±0.05 can produce audible artefacts. Tune on your voice with `tools/tts_speed_ab.py`.
- `tts.pocket_tts_custom_voices_dir` *(string, `""`)* — extra directory of custom Pocket-TTS voices (`.safetensors`). Empty → only the bundled ones.
- `tts.pocket_tts_frames_after_eos` *(int, `1`, clamped `[0, 8]`; `null` / `"default"` → library behaviour)* — how many 80 ms Mimi frames to keep decoding **after** the model has signalled end-of-sequence. Pocket-TTS's own default is a per-utterance guess (1 frame over four words, 3 at or under) plus an unconditional `+2`, so every spoken chunk carried 240 ms of audio — 400 ms for short utterances — generated with nothing left in the text to say. That is where the stray syllable at the end of a chunk came from, and why looking for it in the text pipeline found nothing: the text handed to the synthesiser was always clean. Measured with the RNG pinned (so the two takes are sample-identical up to the tail and the difference *is* the post-EOS segment), that audio runs at **14–42% of the body's RMS** — not decay, audible. Per frame, frame 1 is the genuine phoneme release and frame 2 often matches it in level before frame 3 falls away, hence the default of 1. Lower to `0` if you still hear a tail and don't mind a slightly clipped final consonant; raise it, or set `null`, if word endings sound cut.

---

## `endpointing` — `EndpointingSettings`

Tiered live-mic endpointing. See `app/stt/endpointing.py` for full semantics.

- `endpointing.enabled` *(bool, `true`)* — master switch.
- `endpointing.use_partial_transcript` *(bool, `true`)* — let partial transcripts feed the fast-close branch (closes finished sentences ~0.6 s after the last chunk instead of waiting for the full 3 s turn timeout).
- `endpointing.phrase_silence_seconds` *(float, `1.0`, min `0.2`)* — silence that ends a phrase.
- `endpointing.turn_silence_seconds` *(float, `3.0`, min `0.4`)* — silence that ends a turn (the user's mic input is finalised).
- `endpointing.fast_close_silence_seconds` *(float, `0.6`, min `0.1`)* — silence that fast-closes a clearly-finished sentence (`"…thanks."`). Lower → snappier turnaround; too low → cuts the user off mid-thought.
- `endpointing.hesitation_extend_to_turn` *(bool, `true`)* — when a hesitation marker (`"and uh…"`) is detected, reset the silence counter so the user has a fresh window to find the next word, bounded by `turn_silence_seconds`.
- `endpointing.barge_in_min_speech_seconds` *(float, `0.7`, min `0`)* — minimum speech before barge-in is allowed to interrupt Aiko's TTS (only consulted when `audio.barge_in_enabled` is on). Higher → fewer accidental interrupts from coughs / pets / room noise.
- `endpointing.hesitation_markers` *(list[string], `[]`)* — optional override of the built-in hesitation-marker list (`"um"`, `"uh"`, `"and uh"`, …). Empty falls back to the defaults baked into `app/stt/endpointing.py`. Add domain-specific markers here without touching code.
- `endpointing.sentence_final_markers` *(list[string], `[]`)* — optional override of sentence-final punctuation / words used to identify a clearly-finished utterance (the fast-close branch). Empty → built-in defaults.

---

## `avatar` — `AvatarSettings`

Live2D (Alexia) rendering knobs. The avatar files live at `avatar.root_dir` (gitignored).

- `avatar.root_dir` *(string, `"data/personas/active/Alexia"`)* — avatar bundle directory.
- `avatar.entry_filename` *(string, `"Alexia.model3.json"`)* — model entry file.
- `avatar.scale_multiplier` *(float, `1.0`, clamped `[0.1, 8.0]`)* — global render scale. Higher → bigger Aiko.
- `avatar.auto_outfit` *(string, `"auto"`)* — one of `"auto"` (circadian: pajamas at night when supported), `"day"`, `"pajamas"`, `"pajamas_hooded"`. Anything else clamps to `"auto"`.
- `avatar.expressiveness` *(float, `1.0`, clamped `[0.0, 1.5]`)* — body-language intensity multiplier. `0.0` mutes every mood-driven amplitude (breath sway, body tilts, expression strength, sass bursts); `1.0` is the authored default; `1.5` exaggerates within safe rig limits. See `web/src/live2d/AmbientBodyChannel.ts` + `ExpressionChannel.ts`.
- `avatar.mood_inertia_damping` *(bool, `true`)* — K45: damp non-mouth expression params proportionally to the gap between the fresh reaction tag's implied affect and the smoothed mood. Mouth params (lipsync ids + grin overlay) are never damped. See the K45 section above.
- `avatar.accessory_state` *(object, `{}`)* — persistent accessory toggles. Boolean keys: `lollipop`, `eyeglasses`, `head_sunglasses`, `crossed_arms`. Enum key `eye_color`: `"default"` / `"both_purple"` / `"left_purple"` / `"right_purple"`. Unknown keys are silently dropped at load time so a downgrade can't promote junk into the namespace.

---

## `tools` — `ToolsSettings`

Agent tool registry switches. Each toggles a single tool; `tools.enabled = false` disables the whole registry.

- `tools.enabled` *(bool, `true`)* — master switch for **all** agent tools. Off → Aiko has no tool-calling capability at all (no time lookups, no recall, no web search, no world manipulation).
- `tools.get_time` *(bool, `true`)* — time/date lookup tool.
- `tools.recall` *(bool, `true`)* — explicit memory-recall tool (in addition to automatic RAG).
- `tools.recall_topic` *(bool, `true`)* — F10d cluster-scoped recall. Where `recall` does a global semantic search for the few closest snippets, `recall_topic` matches the query to a whole topic cluster (centroid cosine) and returns that cluster's members ranked by cosine — the "what do I actually know about X?" answer when the user asks Aiko to round up / summarise a subject. No-op (empty result) without a persistent topic graph wired.
- `tools.recall_concept` *(bool, `true`)* — L5 concept recall. Pulls up one higher-order concept Aiko has abstracted about the user (nearest active user-identity concept to the query), bundled in a single call with its supporting evidence memories and topic-cluster labels; an `all_evidence` flag lifts the memory cap for the full picture. No-op (empty result) without the concept store wired into the retriever.
- `tools.recall_hypotheses` *(bool, `true`)* — L30 open-guess recall. Lists what Aiko is still *unsure* about — invented hypotheses from the `hypotheses` table plus the grounded candidate concepts the L30a lane draws from — least settled first, with `origin` marking which is which so an invention is never presented as an observation. Registers on the concept store alone: with no `hypotheses` table the invented half is simply absent rather than the tool being withheld. See [`hypotheses.md`](hypotheses.md).
- `tools.web_search` *(bool, `true`)* — gates whether the background `web_search` workflow skill is offered. The actual search backend (DuckDuckGo vs LangSearch) is configured separately under the `search` block below.
- `tools.world` *(bool, `true`)* — Aiko's room tools (`look_around`, `move_to`, `change_posture`, `inspect_item`, `consume_item`). Off → her room is still alive in the world store but she can't act on it.
- `tools.goals` *(bool, `true`)* — K1 goal tools (`list_goals`, `add_goal`, `update_goal_progress`, `archive_goal`). Off → Aiko's prompt block + worker still surface goals but she can't *act* on them mid-turn. Independent from `agent.goals_enabled`: if the master switch is off the tools are wired but no-op because the store is unset.
- `calculate` is no longer a `tools.*` flag — it moved to the bundled `calculator` plugin (`plugins/calculator/`), a synchronous exact-arithmetic fast tool contributed through the ToolPlugin SDK (`api.register_fast_tool`). It evaluates an expression through an AST whitelist (no `eval`) and returns the result in the same turn so Aiko never guesses a number. Toggle it by enabling/disabling the plugin (`plugin.json` `enabled`, or `plugins.entries.calculator.enabled`). See [`docs/skills-framework.md`](skills-framework.md) for the fast-tool plugin capability.
- `tools.weather` *(bool, `true`)* — H11 synchronous weather tools (`get_weather` / `get_forecast`). Lets Aiko answer "what's the forecast?" for the configured home location or any named city (geocoded at call time). Independent of the passive ambient `agent.weather_sync_enabled` feed — the tools work even with the overlay off. Backend configured under the `weather` block below.

---

## `search` — `SearchSettings`

Web-search backend shared by every search path — the background workers (F1 fact-checker, G3 curiosity, F9 knowledge enrichment) and the goal-workflow `web_search` lane. One pluggable provider is built from this block in `SessionController` and injected into all of them; see [`app/llm/search/providers.py`](../app/llm/search/providers.py).

- `search.provider` *(str, `"duckduckgo"`)* — `"duckduckgo"` (keyless default) or `"langsearch"`. When `"langsearch"` but no API key resolves, it silently falls back to DuckDuckGo.
- `search.api_key` *(str, `""`)* — LangSearch API key. **Write-only via REST**: `GET /api/settings` returns only `has_api_key`, and the value is routed into the OS keychain (blank on disk) when a backend exists. Set it through `PUT /api/settings/search-credentials` or the `LANGSEARCH_API_KEY` env var rather than committing it to `config/user.json`.
- `search.api_key_env` *(str, `"LANGSEARCH_API_KEY"`)* — env var consulted when `api_key` is blank.
- `search.langsearch_summary` *(bool, `true`)* — request LangSearch's long-text summaries (richer context for distillation). Ignored by the DuckDuckGo path.
- `search.langsearch_freshness` *(str, `"noLimit"`)* — time window: `oneDay` / `oneWeek` / `oneMonth` / `oneYear` / `noLimit`.
- `search.langsearch_count` *(int, `10`, clamped `[1, 10]`)* — max results requested per call.
- `search.fallback_to_duckduckgo` *(bool, `true`)* — when LangSearch errors out or its daily quota (free tier = 1000/day) is exhausted, fall back to DuckDuckGo so search still works.
- `search.timeout_seconds` *(float, `12.0`, floor `1.0`)* — LangSearch request timeout.
- `search.langsearch_min_interval_seconds` *(float, `1.1`, floor `0.0`)* — minimum wall-clock spacing kept between consecutive LangSearch requests, enforced **process-wide** (a single class-level gate shared across every background worker — F1 / G3 / F9 — and the brain's `web_search` tool). LangSearch caps at ~1 request/second, so when several queued topics fire in the same window the provider sleeps the remainder before issuing each request rather than tripping the rate limit. `0` disables the throttle. Ignored by the DuckDuckGo path.
- `search.query_reformulation_enabled` *(bool, `true`)* — **F6**: before searching, rewrite a personal claim into a neutral, name-free topic query with the local worker model, post-filtered by the deterministic privacy scrubber (a hallucinated name can never reach the search engine). When off, the workers use the deterministic scrub directly. See [`app/core/memory/query_reformulation.py`](../app/core/memory/query_reformulation.py).

LangSearch's Semantic Rerank API is intentionally not wired (Aiko's RAG is already local cosine and results come back ranked + summarized). LangSearch docs: <https://docs.langsearch.com/>.

---

## `weather` — `WeatherSettings`

H11 real-world co-location. One pluggable backend layer feeds both the passive ambient feed (gated by `agent.weather_sync_enabled`) and the on-demand brain tools (gated by `tools.weather`); see [`app/llm/weather/providers.py`](../app/llm/weather/providers.py). The weather and geocoding backends are deliberately independent so either can be swapped without breaking the other. Privacy posture: coarse city-granularity location only, never GPS — see [`docs/weather-sync.md`](weather-sync.md).

- `weather.provider` *(str, `"open_meteo"`)* — weather backend (keyed purely on lat/lon). Open-Meteo is the keyless default.
- `weather.geocoder` *(str, `"open_meteo"`)* — place-name → coordinate backend, decoupled from `provider`.
- `weather.location_name` *(str, `""`, ≤80 chars)* — your home city (city granularity). Geocoded once to `latitude`/`longitude` when saved via REST. Blank → the ambient feed stays silent.
- `weather.latitude` *(float | null, `null`, clamped `[-90, 90]`)* — cached home latitude. Out-of-range or non-numeric → `null`.
- `weather.longitude` *(float | null, `null`, clamped `[-180, 180]`)* — cached home longitude.
- `weather.units` *(str, `"metric"`)* — `"metric"` (°C / km·h) or `"imperial"` (°F / mph). Anything else falls back to `"metric"`.
- `weather.refresh_interval_minutes` *(int, `30`, floor `15`)* — minutes between ambient fetches. Higher → the shared sky updates less often (less API traffic). Lower → refreshes sooner. The brain tools are on-demand and ignore this.
- `weather.api_key` *(str, `""`)* — reserved for a future keyed backend. **Write-only via REST** (`has_api_key` in `GET /api/settings`).
- `weather.api_key_env` *(str, `"WEATHER_API_KEY"`)* — env var consulted when `api_key` is blank.
- `weather.timeout_seconds` *(float, `10.0`, floor `1.0`)* — per-request HTTP timeout.

---

## Task approvals + `file_write`

Destructive task capabilities (file writes today; shell exec / http post later) are gated by a **reusable** approval layer. The policy is generic; each capability owns a small resource block.

- `agent.builtin_file_skills_enabled` *(bool, `true`)* — when `false`, the built-in workflow file skills (`file_search` / `read_file` / `write_file`) are **not** offered to the planner. Set this off when you handle files exclusively through a filesystem MCP server (e.g. `@modelcontextprotocol/server-filesystem`): it removes the built-in-vs-MCP overlap (two path conventions — the built-in `Documents:` label vs the MCP's absolute-under-sandbox-root) that otherwise makes the planner hand a label/relative path to an MCP file tool and get *"path outside allowed directories"*. With it off, all file work uses one convention; note file ops then depend on the MCP server being up.
- `agent.task_approval_mode` *(str, `"ask"`)* — global default. `"ask"` gates every destructive action behind a TaskStrip approval prompt; `"auto"` performs without asking.
- `agent.task_approval_overrides` *(dict, `{}`)* — per-capability override map, e.g. `{"file_write": "auto"}` to stop asking for writes only. Invalid modes are dropped (never coerced).
- `agent.file_write.enabled` *(bool, `false`)* — master switch for the `write_file` workflow skill + handler. Off → the skill is never offered to the planner. Requires at least one **writable** root (a `agent.task_file_allowed_roots` entry with `read_only: false`).
- `agent.file_write.max_bytes` *(int, `262144`, clamped `[1 KiB, 16 MiB]`)* — cap on the resulting file size.
- `agent.file_write.allowed_extensions` *(list, text-only default)* — case-insensitive write allow-list (empty = allow all).

A session "approve all" click rides on top of both fields in-memory and is never persisted (cleared on restart). Full design + how to add a new destructive capability: [`docs/task-approvals.md`](task-approvals.md).

## Local vision — `agent.vision` (`describe_image`)

The `describe_image` workflow skill lets Aiko *look at* an image inside a configured file root and describe it, using the **single local worker model already loaded** — no second model, no cloud image-token cost. The only requirement is that the worker model is multimodal (e.g. `qwen3.5:27b` / `qwen3.6:27b`); switch `llm.routes.worker_default` + `llm.routes.workflow` to such a model. Read-only → it does NOT touch the approval framework.

- `agent.vision.enabled` *(bool, `false`)* — master switch for the `describe_image` workflow skill + handler. Off → the skill is never offered to the planner. Requires at least one **active** root (`agent.task_file_allowed_roots`).
- `agent.vision.model` *(str, `""`)* — optional model override. Empty (recommended) reuses the effective worker model so there is genuinely one model in VRAM; a non-empty value points the vision call at a different local Ollama model (accepting a load/reload).
- `agent.vision.max_bytes` *(int, `8388608` = 8 MiB, clamped `[1 KiB, 64 MiB]`)* — cap on the image file size that gets base64-encoded and sent to Ollama (refused, never truncated).
- `agent.vision.timeout_seconds` *(int, `180`, floor `5`)* — per-call ceiling hint (a cold model load + a vision pass can be slow).
- `agent.vision.allowed_extensions` *(list, `.png .jpg .jpeg .webp .gif .bmp`)* — case-insensitive image extension allow-list (empty = allow all).
- `agent.vision.default_prompt` *(str)* — instruction sent alongside the image when the caller doesn't supply a question.

MCP debug: `get_vision_state()` (enabled / effective model / worker-client type / active roots / skill registered) and `describe_image_now(path, question="")` (one-shot, bypasses the planner).

### In-chat attachments (D2 Part B)

The chat composer accepts **image + text** attachments (paperclip button, drag-and-drop, or paste). Each file is uploaded to a fixed managed directory `data/attachments/` that is **auto-registered as a read-only sandbox root labelled `Attachments`** — so it resolves through the same file handlers as any other root, with zero per-attachment config.

- Upload: `POST /api/chat/attachments` (multipart `file`) → `{attachment: {id, filename, kind, rel_path, bytes}}`. The image allow-list mirrors `agent.vision.allowed_extensions`; the byte cap rides `agent.vision.max_bytes` (default 8 MiB). Text extensions are a fixed set (`.txt .md .json .csv .py …`).
- Drop an unsent attachment: `DELETE /api/chat/attachments/{stored_name}`.
- Static serving (image thumbnails): `GET /attachment-files/<uuid><ext>`.
- The `chat` WS command carries an optional `attachments: [{rel_path, kind, …}]` array (server-side allow-listed to the `Attachments` root only). The files are persisted onto the user message (`messages.attachments`, schema v18) and surfaced to Aiko as a **per-turn hint** that tells her to route images to `describe_image` and text to `read_file` via `start_workflow` — she acts on the workflow result, never guesses from the filename. No image bytes ever reach the cloud chat model; the **local** worker model reads them.

---

## `mcp_server` — `McpServerSettings`

Embedded MCP (Model Context Protocol) server for development tooling. This is the server the app **exposes** (Cursor / Copilot connect to it).

- `mcp_server.enabled` *(bool, `true`)* — master switch.
- `mcp_server.port` *(int, `6274`, min `1`)* — SSE endpoint. The Cursor MCP config in `.cursor/mcp.json` points here.

---

## `mcp_clients` — `ExternalMcpSettings`

External MCP servers the app **connects out to as a client** (the opposite direction from `mcp_server`). Their tools are discovered at boot and registered **only into the background-worker / goal-workflow lane** — never into the brain's fast tools. See [`docs/mcp-clients.md`](mcp-clients.md) for the architecture, lifecycle, and the filesystem-server proof.

Master switch lives on `agent`:

- `agent.mcp_clients_enabled` *(bool, `true`)* — when off (or `mcp_clients.servers` is empty), the manager never starts and no MCP tools are registered. Only meaningful when `agent.workflow_enabled` is also on (MCP tools are background-lane skills).

`mcp_clients.servers` is a list of `ExternalMcpServer` rows:

- `id` *(string, required)* — stable identifier; the skill names are namespaced `<id>__<tool_name>`. Duplicate ids are dropped.
- `name` *(string)* — human label (defaults to `id`).
- `transport` *(string, `"stdio"`)* — `"stdio"` (launch `command` + `args` as a child process) or `"sse"` (connect to a running server at `url`).
- `command` *(string)* — executable for stdio (e.g. `"npx"`). Required for stdio rows; a stdio row without it is dropped.
- `args` *(string[])* — command arguments (e.g. `["-y", "@modelcontextprotocol/server-filesystem", "/path"]`).
- `env` *(object)* — extra environment for the child. Values support `${ENV:NAME}` indirection, resolved from the process environment at launch, so a token can live in an env var instead of in `config/user.json`.
- `url` *(string)* — endpoint for `sse` rows. Required for sse; an sse row without it is dropped.
- `enabled` *(bool, `true`)* — per-server switch.
- `autostart` *(bool, `true`)* — connect at boot.
- `timeout_seconds` *(float, `30.0`, min `1`)* — per-call read timeout.
- `expose_tools` *(string[], `[]`)* — optional **allow-list** of tool names to register for the planner; empty exposes every tool the server advertises.
- `disabled_tools` *(string[], `[]`)* — optional **deny-list** of tool names to drop even when they pass the allow-list. Applied after `expose_tools`. Convenient for hiding a few unwanted tools (e.g. a browser server's debug group) without enumerating everything you keep.

---

## `plugins` — `PluginsSettings`

SDK-primary MCP ToolPlugins — a small Python package (`plugin.json` stub +
`entry.py` + plugin-local `config/` + optional `SKILL.md`) that registers an MCP
server, planner guidance, and optional tool-result middleware in code. Discovered
from the bundled `plugins/`, the user `data/plugins/`, and any `plugins.paths`, in
that precedence order (first-seen id wins). A disabled plugin's code is never
imported. See [`docs/plugins.md`](plugins.md) for the full model.

- `plugins.enabled` *(bool, `true`)* — master switch for the whole plugin subsystem.
- `plugins.paths` *(string[], `[]`)* — extra discovery roots beyond the two defaults.
- `plugins.entries` *(object)* — per-plugin overrides keyed by plugin id:
  - `entries.<id>.enabled` *(bool | null)* — override the stub's `enabled`.
  - `entries.<id>.config` *(object)* — highest-precedence config override, merged on top of the plugin-local `config/default.json` < `config/user.json`. This central override lives in the app's `config/user.json`; machine-specific paths / tokens usually live in the **gitignored** plugin-local `config/user.json` instead (or are read at runtime via `api.env(...)`).

---

## `browser_perception` — `BrowserPerceptionSettings`

Optional server-agnostic middleware over an MCP browser server's accessibility-snapshot tool: parse → dedup → form-group → heading-context → heuristic rank → diff-vs-previous → compact render for the workflow planner. Off by default. See [`docs/browser-perception.md`](browser-perception.md) for the full design and the "swap the MCP server" runbook.

- `browser_perception.enabled` *(bool, `false`)* — master switch.
- `browser_perception.server_id` *(string, `"browser"`)* — which `mcp_clients.servers` row is the browser server.
- `browser_perception.snapshot_tools` *(string[], `["browser_snapshot"]`)* — tool names whose results get reshaped; every other tool passes through untouched.
- `browser_perception.adapter` *(string, `"real_browser"`)* — snapshot parser: `"real_browser"` (JSON or indented tree) or `"generic"` (indented tree only). Unknown names fall back to `generic`.
- `browser_perception.max_ranked_elements` *(int, `40`, min `1`)* — cap on ranked interactive elements rendered.
- `browser_perception.state_memory_pages` *(int, `8`, min `1`)* — size of the in-process (ephemeral) previous-page-state LRU used for change diffs.
- `browser_perception.weight_role` / `weight_visibility` / `weight_position` / `weight_text` / `weight_context` *(float, `1.0`, min `0`)* — per-signal weights for the heuristic `interaction_likelihood` score.

---

## `web_server` — `WebServerSettings`

FastAPI + WebSocket layer that serves the React UI.

- `web_server.enabled` *(bool, `true`)* — master switch (you almost never want this off).
- `web_server.host` *(string, `"127.0.0.1"`)* — bind address. Set to `"0.0.0.0"` to expose to your LAN.
- `web_server.port` *(int, `6275`, min `1`)* — HTTP / WS port.

---

## `logging` — `LoggingSettings`

Backend log discipline. The companion file `data/app.log` is the source of truth for "what happened during a turn" — see `AGENTS.md` § *Debugging via logs* for the full grep playbook.

- `logging.level` *(string, `"INFO"`)* — global root level. `WARNING` for production quiet, `INFO` for one structured line per turn, `DEBUG` for the firehose.
- `logging.module_levels` *(object, `{}`)* — per-module overrides, e.g. `{"app.core.session.prompt_assembler": "DEBUG"}`. Keep the root at `INFO` and dial up just the suspect module.
- `logging.file_enabled` *(bool, `true`)* — write to the rotating `data/app.log`.
- `logging.file_path` *(string, `"data/app.log"`)* — log file path.
- `logging.file_max_bytes` *(int, `5242880`, min `65 536`)* — rotate at this many bytes (default 5 MB).
- `logging.file_backup_count` *(int, `5`, min `0`)* — number of rotated siblings to keep (`app.log.1` … `.5`).
- `logging.prompt_cache_log_enabled` *(bool, `false`)* — P44 prompt-cache telemetry: one JSONL record per turn describing where the prompt's cacheable prefix broke and how far the token estimate drifted from the provider's real count. Deliberately **not** in `app.log` (a per-turn line would bloat it) — the `app.promptcache` logger sets `propagate = False` and writes its own file. Turn it on for a measuring session, then run `python scripts/prefix_break_report.py`. See [`prompt-caching.md`](prompt-caching.md#measuring-where-the-prefix-breaks-p44).
- `logging.prompt_cache_log_path` *(string, `"data/prompt-cache.jsonl"`)* — where those records go.
- `logging.prompt_cache_log_max_bytes` *(int, `2097152`, min `65 536`)* — rotate at this many bytes. At roughly 300 bytes per turn, 2 MB holds ~7k turns.
- `logging.prompt_cache_log_backup_count` *(int, `2`, min `0`)* — rotated siblings to keep.
- `logging.ui_log_enabled` *(bool, `false`)* — UI debug-log bridge: when on, the browser POSTs structured events (WS dispatch, avatar channel decisions, settings changes) to `/api/logs/ui` which interleaves them into `data/app.log` with a `[ui]` prefix. Flip on via Settings drawer → Diagnostics when reproducing a bug.
- `logging.ui_log_categories` *(list, `["ws", "channel", "settings", "voice"]`)* — allow-list of `source` values the endpoint accepts. Keeps a misbehaving client from spamming arbitrary lines.
- `logging.ui_log_max_batch` *(int, `50`, clamped `[1, 500]`)* — max entries per request.
- `logging.ui_log_max_payload_bytes` *(int, `2048`, clamped `[256, 65 536]`)* — truncates oversized payloads before they hit the rotating log.

---

## Knobs that live **only** in `config/user.json`

Some runtime state belongs in `user.json` because it's hyper-local and never appears in `default.json`. The settings loader doesn't validate these against any dataclass — they're consumed directly by their owners.

- `session.last_active_id` *(string)* — id of the chat session re-opened on boot. Written by `SessionController.shutdown()`, read on next boot. Don't hand-edit unless you know which session id you're picking.
- `desktop.persona_window.width` / `desktop.persona_window.height` *(int)* — geometry of the transparent persona window in the Tauri shell. Also managed by `tauri-plugin-window-state`; this block is a fallback for first-launch sizing.

---

## Adding a new field — checklist

(This is the short-form companion to the
[`config-documentation` rule](../.cursor/rules/config-documentation.mdc).)

1. Add the field to the relevant dataclass in `app/core/infra/settings.py` with a short inline comment explaining what tuning up vs down does.
2. If users should be able to set it from JSON, add the default to `config/default.json` under the right section.
3. Parse it in `load_settings()` with whatever clamp / fallback makes sense.
4. Add a row to the right section of this file using the format `` - `key` *(type, default)* — what it does. Higher → effect. Lower → effect. ``
5. If it's a user-facing knob (i.e. someone might actually want to tune it without reading the source), add a row to the **Cheatsheet** at the top.
6. Grep this file for the new field name to confirm it's there — the rule's validation step. If it's missing, the change is incomplete.
