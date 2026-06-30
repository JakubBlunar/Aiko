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
| Cap reply length (stop rambling) | `chat_llm.max_tokens` | `512` |
| Keep model warm in VRAM longer | `chat_llm.keep_alive` | `"30m"` |
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
| Don't cave on taste pushback (K46 stance persistence) | `agent.stance_persistence_enabled` | `true` |
| Surface "what I've been turning over" between sessions (K28) | `agent.turning_over_enabled` | `true` |
| Wall-clock prefixes on chat history (K-time1) | `agent.history_age_prefix_enabled` | `true` |
| Cue-register rotation (K51 de-"Heads-up") | `agent.cue_register_rotation_enabled` | `true` |
| Destructive-task approval mode | `agent.task_approval_mode` | `"ask"` (`"ask"` / `"auto"`) |
| Per-capability approval overrides | `agent.task_approval_overrides` | `{}` (e.g. `{"file_write": "auto"}`) |
| Let Aiko write files (workflow skill) | `agent.file_write.enabled` | `false` |
| Let Aiko see images (workflow skill) | `agent.vision.enabled` | `false` |
| Exact-arithmetic tool | `tools.calculate` | `true` |
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

## `ollama` — `OllamaSettings` (legacy, mirror of `local_ollama` provider)

The local Ollama runtime that hosts the chat + embedding models. Sits **behind** `chat_llm` (which can route to a different provider). The `embedding_*` fields are still authoritative (the embedder is not catalogued); the rest is mirror-written to the `local_ollama` entry in `llm.providers` on every reconfigure.

- `ollama.base_url` *(string, `"http://127.0.0.1:11434"`)* — where the local Ollama daemon listens.
- `ollama.embedding_base_url` *(string, `""`)* — separate URL for the embedding model if you split it onto another box; empty falls back to `base_url`.
- `ollama.chat_model` *(string, `"jaahas/qwen3.5-uncensored:27b"`)* — model name Aiko uses for chat. Larger → smarter / slower; smaller → snappier / drifts more often. Must already be `ollama pull`-ed.
- `ollama.temperature` *(float, `0.6`)* — sampling temperature. Higher → more creative / unhinged; lower → more deterministic / dry. Inherited by `chat_llm.temperature` when unset there.
- `ollama.context_window` *(int | null, `null`)* — context-window override. `null` auto-detects via the Ollama API. Set explicitly only if auto-detect picks wrong.
- `ollama.embedding_model` *(string, `"qwen3-embedding:0.6b"`)* — the embedder used for RAG, beliefs, novelty, conflicts, curiosity seeds, etc. Changing this **invalidates the LanceDB** (existing vectors won't match new vectors).
- `ollama.timeout` *(int, `300`)* — HTTP timeout in seconds, shared by every Ollama client (chat + embeddings). Bump if a slow model occasionally times out mid-generation.

---

## `llm` — `LlmSettings` (catalogue + role mapping)

The canonical LLM configuration. Holds the **provider catalogue**
(`llm.providers[]`) and the **role assignment table** (`llm.routes{}`).
On first boot, [`_migrate_legacy_llm`](../app/core/infra/settings.py)
synthesises this block from the legacy `chat_llm` + `ollama` blocks
when `llm.providers` is empty — see [llm-providers.md →
Migrating from the legacy config](llm-providers.md#migrating-from-the-legacy-chat_llm--ollama-config).

### `llm.providers[]` — saved provider catalogue

Each entry is a slotted `LlmProvider`:

- `llm.providers[].id` *(string, required, unique)* — stable identifier used by routes. Example: `"local_ollama"`, `"openai"`, `"openai_team"`.
- `llm.providers[].name` *(string)* — display name for the catalogue list.
- `llm.providers[].kind` *(string, `"ollama"` | `"openai_compatible"`)* — wire protocol family.
- `llm.providers[].base_url` *(string)* — endpoint URL.
- `llm.providers[].api_key` *(string, `""`)* — bearer token (written via `PUT /api/llm/providers/{id}/credentials`; never round-trips through GET).
- `llm.providers[].api_key_env` *(string, `""`)* — env-var fallback (e.g. `"OPENAI_API_KEY"`).
- `llm.providers[].extra_headers` *(object, `{}`)* — vendor-specific headers (OpenRouter wants `HTTP-Referer` + `X-Title`).
- `llm.providers[].timeout_seconds` *(int, `300`)* — HTTP timeout.
- `llm.providers[].keep_alive` *(string, `"30m"`)* — Ollama-only model-resident-in-VRAM duration; silently ignored by remote providers.

Two routes pointing at the same provider share one `ChatClient`
instance through the cache in [`app/llm/factory.py`](../app/llm/factory.py).

### `llm.routes{}` — role assignments

Maps a role name (canonical: `"main_chat"`, `"worker_default"`; future: `"heavy_workers"`, …) to an `LlmRoute`:

- `llm.routes[role].provider_id` *(string, required)* — references `llm.providers[].id`. Server returns 404 when unknown.
- `llm.routes[role].model` *(string, required)* — model name (for `openai_compatible`) or tag (for `ollama`). Free-text combobox in the drawer.
- `llm.routes[role].context_window` *(int | null, `null`)* — explicit budget. `null` / `0` falls through to the per-model auto-detect (see `chat_llm.context_window` below for the resolution order).
- `llm.routes[role].max_tokens` *(int, `512`)* — hard cap per assistant reply for this role.
- `llm.routes[role].temperature` *(float | null, `null`)* — sampling temperature; `null` inherits from the legacy block.

`main_chat` updates cascade through `reconfigure_chat_llm` so the
live chat client + `TurnRunner` + `ProactiveDirector` rebuild
immediately. `worker_default` updates are persisted; workers pick
up the new config on next restart.

---

## `chat_llm` — `ChatLlmSettings` (legacy, mirror of `llm.routes.main_chat`)

Provider-routing layer in front of `ollama`. Lets you run chat on Ollama Cloud, OpenAI, Grok, Groq, OpenRouter, DeepSeek, Together, Mistral — anything OpenAI-compatible.

**Status**: legacy. The catalogue (`llm.providers` + `llm.routes`) is the new source of truth; the controller mirror-writes both directions so external scripts that still read `chat_llm.*` keep working. New code should target `llm.routes.main_chat` instead.

- `chat_llm.provider` *(string, `"ollama"`)* — `"ollama"` (local or Ollama Cloud) or `"openai_compatible"` (anything that speaks the OpenAI API: Gemini, OpenAI, Groq, OpenRouter, DeepSeek, …).
- `chat_llm.provider_preset` *(string, `""`)* — UI hint emitted by the curated picker. One of `""` / `"ollama"` / `"ollama_cloud"` / `"openai"` / `"gemini"` / `"groq"` / `"openrouter"`. Controller ignores it; only the React drawer reads it to highlight the active preset card.
- `chat_llm.model` *(string, `""`)* — model name override. Empty → falls back to `ollama.chat_model`. For `openai_compatible` this is **required** (e.g. `"gemini-2.5-flash-lite"`, `"gpt-4o-mini"`).
- `chat_llm.base_url` *(string, `""`)* — endpoint URL. Empty → `ollama.base_url` (when provider is `ollama`).
- `chat_llm.api_key` *(string, `""`)* — bearer token. Empty → looked up via `api_key_env` or inferred from the host. Always written via `PUT /api/settings/llm-credentials` from the UI so the key never round-trips through `GET /api/settings`.
- `chat_llm.api_key_env` *(string, `""`)* — explicit env var holding the key (e.g. `"OPENAI_API_KEY"`).
- `chat_llm.context_window` *(int | null, `null`)* — explicit context-window override (tokens) used as the prompt-assembly budget. Resolution order is **explicit override > active client's `get_context_length(model)` > hardcoded 8192 fallback**. Set to `null` / `0` / unset to use auto-detect: Ollama hits `/api/show` per model; the `OpenAICompatibleClient` consults a static lookup table that maps known cloud model ids to **conservative caps** (gpt-5-mini → 131072, gpt-4.1-mini → 131072, gemini-2.5-* → 131072, claude-3-* → 200000, etc. — see `_CONTEXT_WINDOW_TABLE` in `app/llm/openai_compatible_client.py`). Cap is intentionally below the model's true max: gpt-4.1-mini's 1 M and gemini-2.5-pro's 2 M are clamped to 128 k because (a) typical use is <50 k, (b) bigger budgets make compaction lazy, and (c) for OpenAI's long-context billing tier, staying under 128 k keeps requests in the cheaper short-context column. Editable from the drawer's **Settings → Chat → Advanced → Context window** input. The `context_window_source` field on `get_status` / Diagnostics reports which branch won (`config`, `client`, or `fallback`).
- `chat_llm.temperature` *(float | null, `null`)* — overrides `ollama.temperature` when set.
- `chat_llm.extra_headers` *(object, `{}`)* — extra HTTP headers (vendor-specific knobs; OpenRouter wants `HTTP-Referer` + `X-Title`).
- `chat_llm.max_tokens` *(int, `512`)* — hard cap on tokens **per assistant reply**. Without this, models routinely emit 2 k+ tokens of rambling on casual chat. **Higher → longer replies**, more chance the LLM drifts off-topic; lower → terser, more chance of mid-sentence truncation. `0` / negative disables the cap. Watch `data/app.log` for `ollama response truncated:` / `openai-compat response truncated:` warnings — they fire only when the cap actually clipped a reply.
- `chat_llm.keep_alive` *(string, `"30m"`)* — how long Ollama keeps the chat model resident in VRAM after a request. Ollama-only (silently ignored by remote providers). Accepts any Ollama duration (`"30m"`, `"1h"`, `"-1"` for "forever").
- `chat_llm.workers_use_local` *(bool, `true`)* — when the chat provider is **not** `"ollama"` AND this is `true`, the ~24 background workers keep talking to a local Ollama instance. Defaults to `true` because Gemini's 1500-req/day free-tier would drain in well under an hour otherwise. Set to `false` to opt workers into the same remote provider (burns quota; useful when there's no local Ollama at all). See [`docs/llm-providers.md`](llm-providers.md) for the rationale.

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
- `agent.self_image_pulse_enabled` *(bool, `true`)* — daily self-image worker. Off → Aiko never re-introspects how she feels about herself.
- `agent.self_image_max_tokens` *(int, `320`, min `120`)* — `num_predict` ceiling on the self-image LLM call. Bump if you see `surface=self_image_worker` truncation warnings.
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
- `agent.style_tracker_cue_cooldown_turns` *(int, `5`, min `0`)* — turns to suppress a re-fire of the **same** style cue.

### K13 — Jacob-side stylometric mirror

Tracks Jacob's writing style across recent user turns and emits a "How Jacob writes lately: terse, casual, asks back often" directive so Aiko's register stays calibrated. Five axes: terseness / formality / emoji / slang / question rate. No embedder, no LLM. **Always rendered** (including aggressive context-mode) because register is the first thing aggressive mode wants to preserve.

- `agent.style_signal_enabled` *(bool, `true`)* — master switch.
- `agent.style_signal_window` *(int, `30`, min `2`)* — recent-user-turn rolling window.
- `agent.style_signal_warmup_min` *(int, `8`, min `2`)* — minimum turns before any axis renders.
- `agent.style_signal_terse_threshold` *(float, `0.55`, clamped `[0, 1]`)* — share of short messages required for "terse" to render. Higher → cue is stricter.
- `agent.style_signal_formal_threshold` *(float, `0.55`, clamped `[0, 1]`)* — share of formal markers required for "formal."
- `agent.style_signal_emoji_threshold` *(float, `0.05`, clamped `[0, 1]`)* — share of messages containing emoji required for "emoji-heavy."
- `agent.style_signal_slang_threshold` *(float, `0.15`, clamped `[0, 1]`)* — share of slang-flagged messages required for "slangy."
- `agent.style_signal_question_threshold` *(float, `0.40`, clamped `[0, 1]`)* — share of user messages ending in `?` required for "asks back often."

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

### Catchphrase miner

- `agent.catchphrase_miner_enabled` *(bool, `true`)* — promotes 3–7-word phrases recurring N+ times across both user and assistant turns, surfaced via the "running jokes" inner-life block.
- `agent.catchphrase_miner_min_seconds_between` *(float, `600.0`, min `30`)* — minimum wall-clock between miner runs.
- `agent.catchphrase_miner_min_new_user_turns` *(int, `6`, min `1`)* — minimum new user turns since the last run.
- `agent.catchphrase_miner_min_total_count` *(int, `3`, min `2`)* — minimum total occurrences of a phrase before it's promoted to a catchphrase.

### Phase-4c curiosity worker

One-line follow-up question prep when the recent conversation has gone shallow.

- `agent.curiosity_worker_enabled` *(bool, `true`)* — master switch.
- `agent.curiosity_worker_min_turns_between` *(int, `3`, min `1`)* — minimum turns between candidate emissions.
- `agent.curiosity_worker_min_seconds_between` *(float, `60.0`, min `0`)* — wall-clock cooldown.
- `agent.curiosity_worker_max_user_word_count` *(int, `8`, min `1`)* — only fires when the recent user turns are this short on average (signal that the conversation has gone shallow).

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

### K2 — theory-of-mind / belief tracking

- `agent.belief_tracking_enabled` *(bool, `true`)* — master switch for the whole K2 surface (worker + gap detector + tag parser + REST + UI). Off → `[[predict:...]]` self-tags still strip from chat but their payload is dropped.
- `agent.belief_worker_enabled` *(bool, `true`)* — toggle only the background inference worker. With tracking on and worker off, the self-tag fast path still writes beliefs and gaps still surface.
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
- `agent.interest_map_enabled` *(bool, `true`)* — F10e: the "interest map" prompt block, a terse T1 (semi-stable) inner-life line listing Aiko's top few topic clusters by size ("Topics you and {user} keep coming back to: …") so she carries a sense of her recurring threads with no per-turn LLM cost. Built from the topic graph's live cluster map (label + member count only — no join back to the memory mirror), so it's cheap on the hot path. Each topic renders its F10a clean label when the label worker has named it, falling back to the heuristic representative summary otherwise. Lands in T1 right after the goals block; dropped under aggressive context pressure; no-op in the non-persistent topic-graph mode.
- `agent.interest_map_max_clusters` *(int, `5`, min `1`)* — how many topic clusters the interest-map line lists (largest first).
- `agent.interest_map_min_size` *(int, `4`, min `1`)* — minimum cluster size for a topic to count as a recurring "interest" worth surfacing. Raised to the topic graph's own `min_cluster_size` if set lower.
- `agent.curiosity_seed_enabled` *(bool, `true`)* — master switch for the curiosity-seed worker.
- `agent.curiosity_seed_max_active` *(int, `6`, min `1`)* — cap on un-consumed seeds the worker keeps alive. Higher → a fast-talking session can pile up many never-mentioned seeds.
- `agent.curiosity_seed_max_per_run` *(int, `2`, min `1`)* — cap on candidates persisted per successful tick.
- `agent.curiosity_seed_min_novelty` *(float, `0.85`, clamped `[0, 1]`)* — cosine floor against existing seeds. Higher → stricter (rejects more "kind of similar" candidates); lower → more eager to write.
- `agent.curiosity_seed_resolve_threshold` *(float, `0.50`, clamped `[0, 1]`)* — cosine match for "the recent turn covered this seed; mark it consumed." Lower than the graph filter on purpose — partial / oblique mentions still count.
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

One-shot inner-life cue on the first user turn after a long typed gap (default `>= 90 min`). Surfaces one recent `kind="reflection"` memory (which covers both `ReflectionWorker` output and `DreamWorker` output — the latter is identified by a `[dream]` content prefix) so Aiko's first reply can fold in "actually, I was thinking about your interview prep last night --" as a casual aside instead of arriving blank. The persona block ("What I've been turning over" in [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt)) carries the anti-announcement discipline (fold it in casually, never lead with "I have something to share", drop silently if it doesn't fit the moment) and the softer dream-variant framing.

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

### K46 — stance persistence (don't cave on taste pushback)

Rides on top of K29 + K20 to draw the **taste vs facts** line. After Aiko states a taste (a K29 cue fired), a *mild* pushback from the user ("really?", "you don't like that?") should NOT make her hedge or flip — that's the chatbot-agreeability tell. K46 surfaces a one-line "hold your take" cue AND shields the K20 calibration from a factual-trust hit on that turn (a taste disagreement must not teach Aiko her *facts* are suspect). A *strong* correction ("no, that's wrong", "let me check") is left to K20 untouched — it's a factual signal even mid-taste-talk.

- `agent.stance_persistence_enabled` *(bool, `true`)* — master switch. Off → neither the cue nor the calibration shield run.
- `memory.stance_persistence_window` *(int, `3`, min `0`)* — how many turns a just-stated taste stays "warm". The window is armed (post-turn) whenever a K29 cue fires and decremented once per turn; while it's `> 0` a mild pushback is read as taste disagreement. `0` effectively disables the feature (window can never be positive).

Verification: enable INFO logging on `app.session` and watch for `stance-persistence fire: band=… window=… forced=…` (cue) and `stance-persistence: shielded calibration from taste pushback (band=… window=…)` (write shield). MCP `get_stance_persistence_state()` dumps the switch, the window setting, the live countdown + stance snippet, and the last-fire diagnostic; `force_stance_persistence()` arms a one-shot bypass on the window (a mild-pushback band is still required). Tests: `tests/test_stance_persistence.py`, `StancePersistenceProviderTests` in `tests/test_prompt_assembler.py`, `OpinionInjectionSettingsTests` in `tests/test_settings.py`.

### K-time1 — wall-clock prefixes on chat history

Per-message relative-age tag prepended to every chat-history message sent to the LLM: `[just now] ...`, `[2 min ago] ...`, `[today 13:32] ...`, `[yesterday 18:45] ...`, `[Wednesday 18:45] ...`, `[May 28 18:45] ...`. The current user message Aiko is replying to is appended *after* the history block and never gets a prefix. Default on.

Why: without per-message timestamps the LLM has no clock against the conversation. A user message from 2 minutes ago saying "I'm planning to visit my grandparents in half an hour" pattern-matches as a completed past event, and Aiko asks "did you make it back?". The prefix gives an explicit per-turn clock; the companion persona block in [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt) ("Wall-clock awareness in the conversation") teaches Aiko how to read it and explicitly tells her not to quote the prefix back.

- `agent.history_age_prefix_enabled` *(bool, `true`)* — master switch. Off → the chat-history block is byte-identical to the pre-K-time1 behaviour (raw `{role, content}` pairs with no per-message timestamp). Use the off setting for A/B comparison or if your model interprets the bracketed metadata as part of the dialogue.

Cost: ~4–6 tokens per kept history message. Negligible against the configured `ollama.context_window` budget.

Verification: enable INFO logging on `app.core.session.prompt_assembler`; the rendered prompt's history messages start with `[…]` brackets. The `_format_age` ladder is unit-tested in `tests/test_prompt_assembler.py::WallClockHistoryPrefixTests`.

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
- `memory.extractor_enabled` *(bool, `true`)* — master switch for the post-summary `MemoryExtractor`. Off → only `[[remember:]]` tags + manual UI adds write memories.
- `memory.self_tagged_salience` *(float, `0.7`, clamped `[0, 1]`)* — default salience for memories written from `[[remember:]]` tags.

### Tier lifecycle (schema v8)

- `memory.tiers_enabled` *(bool, `true`)* — master switch for the tiered lifecycle. Off → behaves like the old flat-pool design.
- `memory.decay_rate_scratchpad` *(float, `0.05`)* — salience decay/day for the `scratchpad` tier. Higher → scratchpad rows fade faster.
- `memory.decay_rate_long_term` *(float, `0.02`)* — salience decay/day for `long_term`.
- `memory.decay_rate_archive` *(float, `0.0`)* — salience decay/day for `archive`. `0` keeps cold history frozen.
- `memory.revival_coefficient` *(float, `0.05`)* — per-day salience rebate proportional to `revival_score`. Higher → revived memories regain salience faster.
- `memory.revival_per_hit` *(float, `0.15`)* — bump applied to `revival_score` when Aiko's reply cites enough keywords from a surfaced memory.
- `memory.revival_decay_per_day` *(float, `0.02`)* — daily fade of `revival_score` itself.
- `memory.revival_min_word_overlap` *(int, `3`, min `1`)* — minimum content-word overlap between Aiko's reply and a surfaced memory to count as a citation. Higher → stricter; lower → noisier.
- `memory.scratchpad_ttl_days` *(int, `14`, min `1`)* — scratchpad rows never promoted within this many days are deleted.
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
- `memory.curiosity_seed_interval_seconds` *(int, `3600`, min `60`)* — K9 curiosity-seed-worker cadence (a ceiling, not a floor — it short-circuits at `curiosity_seed_max_active`).
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
- `memory.idle_worker_tick_budget_ms` *(int, `3000`, min `0`)* — per-tick wall-time budget. The scheduler runs as many due workers as fit. Set to a small value (e.g. `500`) to approximate the old one-per-tick behaviour. Anti-starvation always lets the most-overdue worker fire even if its EMA estimate exceeds the remaining budget.
- `memory.idle_worker_max_per_tick` *(int, `0`, min `0`)* — hard cap on workers per tick. `0` = unlimited (only the time budget matters); positive values clamp tick log volume on heavy backlogs.

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

- `stt.model` *(string, `"large-v1"`)* — whisper model identifier. Larger → more accurate / slower / more VRAM.
- `stt.language` *(string | null, `"en"`)* — language hint. `null` = autodetect (slower, less accurate on short clips).

---

## `tts` — `TtsSettings`

- `tts.provider` *(string, `"pocket-tts"`)* — TTS engine. Currently `"pocket-tts"` is the supported provider.
- `tts.voice` *(string, `"aiko1_refined.safetensors"`)* — voice file used by the active engine.
- `tts.enabled` *(bool, `true`)* — master switch. Off → typed-only output.
- `tts.pocket_tts_voice` *(string, `"alba"`)* — Pocket-TTS voice file name (mirrors `tts.voice` for Pocket-TTS specifically). The Settings drawer keeps these in sync.
- `tts.pocket_tts_temp` *(float, `0.6`)* — Pocket-TTS sampling temperature baseline. Pocket-TTS is sensitive here; ±0.05 can produce audible artefacts. Tune on your voice with `tools/tts_speed_ab.py`.
- `tts.pocket_tts_custom_voices_dir` *(string, `""`)* — extra directory of custom Pocket-TTS voices (`.safetensors`). Empty → only the bundled ones.

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
- `tools.web_search` *(bool, `true`)* — gates whether the background `web_search` workflow skill is offered. The actual search backend (DuckDuckGo vs LangSearch) is configured separately under the `search` block below.
- `tools.world` *(bool, `true`)* — Aiko's room tools (`look_around`, `move_to`, `change_posture`, `inspect_item`, `consume_item`). Off → her room is still alive in the world store but she can't act on it.
- `tools.goals` *(bool, `true`)* — K1 goal tools (`list_goals`, `add_goal`, `update_goal_progress`, `archive_goal`). Off → Aiko's prompt block + worker still surface goals but she can't *act* on them mid-turn. Independent from `agent.goals_enabled`: if the master switch is off the tools are wired but no-op because the store is unset.
- `tools.calculate` *(bool, `true`)* — synchronous exact-arithmetic tool. Evaluates an expression through an AST whitelist (no `eval`) and returns the result in the same turn so Aiko never guesses a number. See [`docs/task-approvals.md`](task-approvals.md) for the broader task/skill picture.
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
