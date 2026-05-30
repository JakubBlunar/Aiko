# Configuration reference

This is the human-facing map of every knob Aiko exposes via
`config/default.json` (shipped) and `config/user.json` (your local
overrides). Drift between this doc and `app/core/settings.py` is
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
> Per-section dataclass: `app/core/settings.py`. Each section header below
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
| Forward foreground app name (desktop) | `agent.activity_awareness_enabled` | `false` |
| Live2D body-language intensity | `avatar.expressiveness` | `1.0` (0.0–1.5) |
| Live2D outfit override | `avatar.auto_outfit` | `"auto"` |
| Live2D model scale | `avatar.scale_multiplier` | `1.0` |
| Switch the unified grounding line on/off | `agent.grounding_line_mode` | `"off"` (`"replace"` / `"split"` / `"off"`) |
| Master switch for Aiko's long-term goals | `agent.goals_enabled` | `true` |
| Hedge old / decayed memories with "(faded)" suffix | `memory.fade_hedge_enabled` | `true` |
| Reinforce "Aiko remembered" beats (callback detector) | `agent.callback_detector_enabled` | `true` |
| Notice when {user_name} double-checks Aiko's claims (calibration) | `agent.calibration_detection_enabled` | `true` |
| Let Aiko occasionally touch the room (sensory anchoring) | `agent.sensory_anchor_enabled` | `true` |
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

## `ollama` — `OllamaSettings`

The local Ollama runtime that hosts the chat + embedding models. Sits **behind** `chat_llm` (which can route to a different provider).

- `ollama.base_url` *(string, `"http://127.0.0.1:11434"`)* — where the local Ollama daemon listens.
- `ollama.embedding_base_url` *(string, `""`)* — separate URL for the embedding model if you split it onto another box; empty falls back to `base_url`.
- `ollama.chat_model` *(string, `"jaahas/qwen3.5-uncensored:27b"`)* — model name Aiko uses for chat. Larger → smarter / slower; smaller → snappier / drifts more often. Must already be `ollama pull`-ed.
- `ollama.temperature` *(float, `0.6`)* — sampling temperature. Higher → more creative / unhinged; lower → more deterministic / dry. Inherited by `chat_llm.temperature` when unset there.
- `ollama.context_window` *(int | null, `null`)* — context-window override. `null` auto-detects via the Ollama API. Set explicitly only if auto-detect picks wrong.
- `ollama.embedding_model` *(string, `"qwen3-embedding:0.6b"`)* — the embedder used for RAG, beliefs, novelty, conflicts, curiosity seeds, etc. Changing this **invalidates the LanceDB** (existing vectors won't match new vectors).
- `ollama.timeout` *(int, `300`)* — HTTP timeout in seconds, shared by every Ollama client (chat + embeddings). Bump if a slow model occasionally times out mid-generation.

---

## `chat_llm` — `ChatLlmSettings`

Provider-routing layer in front of `ollama`. Lets you run chat on Ollama Cloud, OpenAI, Grok, Groq, OpenRouter, DeepSeek, Together, Mistral — anything OpenAI-compatible.

- `chat_llm.provider` *(string, `"ollama"`)* — `"ollama"` (local or Ollama Cloud) or `"openai_compatible"` (anything that speaks the OpenAI API).
- `chat_llm.model` *(string, `""`)* — model name override. Empty → falls back to `ollama.chat_model`.
- `chat_llm.base_url` *(string, `""`)* — endpoint URL. Empty → `ollama.base_url` (when provider is `ollama`).
- `chat_llm.api_key` *(string, `""`)* — bearer token. Empty → looked up via `api_key_env` or inferred from the host.
- `chat_llm.api_key_env` *(string, `""`)* — explicit env var holding the key (e.g. `"OPENAI_API_KEY"`).
- `chat_llm.context_window` *(int | null, `null`)* — context window for the routed model.
- `chat_llm.temperature` *(float | null, `null`)* — overrides `ollama.temperature` when set.
- `chat_llm.extra_headers` *(object, `{}`)* — extra HTTP headers (vendor-specific knobs).
- `chat_llm.max_tokens` *(int, `512`)* — hard cap on tokens **per assistant reply**. Without this, models routinely emit 2 k+ tokens of rambling on casual chat. **Higher → longer replies**, more chance the LLM drifts off-topic; lower → terser, more chance of mid-sentence truncation. `0` / negative disables the cap. Watch `data/app.log` for `ollama response truncated:` warnings — they fire only when the cap actually clipped a reply.
- `chat_llm.keep_alive` *(string, `"30m"`)* — how long Ollama keeps the chat model resident in VRAM after a request. Higher → no model-load latency on the next message; lower → frees GPU for other workloads sooner. Accepts any Ollama duration (`"30m"`, `"1h"`, `"-1"` for "forever").

---

## `agent` — `AgentSettings`

The big one. Inner-life workers, proactive nudges, summarisation, style trackers, detectors. Most "Aiko feels different lately" knobs live here.

### Proactive — voice mode

- `agent.proactive_silence_seconds` *(float, `45.0`, min `10`)* — seconds of silence in **voice** mode before `ProactiveDirector` is allowed to fire a nudge. Higher → Aiko waits longer before chiming in; lower → she gets nag-y. See `app/core/proactive_director.py`.
- `agent.proactive_cooldown_seconds` *(float, `120.0`, min `30`)* — minimum gap between two voice-mode proactive nudges. Higher → fewer back-to-back unprompted utterances.

### Proactive — typed mode

Typed-mode runs an independent timer so the cadence can differ (typing sessions tolerate longer silences than mic ones).

- `agent.proactive_typed_enabled` *(bool, `true`)* — master switch for "Aiko speaks first in typed chat." Off → typed sessions are purely user-driven.
- `agent.proactive_silence_seconds_typed` *(float, `240.0`, min `60`)* — silence threshold for typed-mode nudges (default 4 min). Higher → less likely to interrupt a heads-down session.
- `agent.proactive_cooldown_seconds_typed` *(float, `600.0`, min `120`)* — minimum gap between two typed proactive nudges (default 10 min). Higher → quieter.
- `agent.proactive_typed_when_away` *(bool, `false`)* — when `false`, typed proactive respects `_user_present` (browser visibility + Tauri focus); when `true`, Aiko can typed-chime in even when no client window is visible. Voice mode ignores this on purpose.

### Activity awareness (desktop opt-in)

- `agent.activity_awareness_enabled` *(bool, `false`)* — forwards the foreground **app name** (never window titles or URLs) from the Tauri desktop shell so Aiko can naturally reference what you're doing. Off by default; browser shells render the toggle but can't produce a non-null active app. Privacy posture: see `docs/presence-and-activity.md`.

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

- **Voice mode**: latency + length contribute to a small `closeness_delta` that rides into [`RelationshipAxesUpdater.apply_turn`](../app/core/relationship_axes.py) on the same turn (snappy replies nudge closeness up; long voice gaps + curt messages nudge it down).
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

Per-turn one-line emotional directive derived from the live [`AffectState`](../app/core/affect_state.py) (valence + arousal) and [`RelationshipAxesState`](../app/core/relationship_axes.py) (closeness / humor / trust / comfort). Output reads like a stage direction — *"Lean affectionate and unhurried; let warmth show."* / *"Stay playful and quick; the room is laughing."* / *"Slow your tempo; let the words land before pushing forward."* — and colours Aiko's delivery (pacing, sentence length, warmth, word choice) **without** dictating content.

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

### F5 — conflicting-memory detector

- `agent.conflict_detector_enabled` *(bool, `true`)* — master switch.
- `agent.conflict_detector_per_hour_cap` *(int, `6`, min `0`)* — hourly cap on LLM verification calls.
- `agent.conflict_detector_per_day_cap` *(int, `30`, min `0`)* — daily cap.

### K2 — theory-of-mind / belief tracking

- `agent.belief_tracking_enabled` *(bool, `true`)* — master switch for the whole K2 surface (worker + gap detector + tag parser + REST + UI). Off → `[[predict:...]]` self-tags still strip from chat but their payload is dropped.
- `agent.belief_worker_enabled` *(bool, `true`)* — toggle only the background inference worker. With tracking on and worker off, the self-tag fast path still writes beliefs and gaps still surface.
- `agent.belief_worker_per_hour_cap` *(int, `4`, min `0`)* — hourly cap on LLM extraction calls.
- `agent.belief_worker_per_day_cap` *(int, `20`, min `0`)* — daily cap.

### K6 — surprise / novelty detector

- `agent.novelty_detection_enabled` *(bool, `true`)* — master switch. Off → the `novelty` inner-life provider is never registered (zero cost on the hot path).

### K18 — topic stagnation detector

Sibling of K6 that fires on the inverse signal: when the rolling distance-to-centroid stays low across a window, Aiko gets a "you've been circling the same topic for a bit" cue.

- `agent.topic_stagnation_enabled` *(bool, `true`)* — master switch. Pure streak counter; no extra embedding cost.

### K9 — topic graph + curiosity seeds

- `agent.topic_graph_enabled` *(bool, `true`)* — master switch for the in-process topic graph wrapper around `MemoryStore._mirror`. Disabling skips both the seed worker's "have we discussed this already?" filter and the Memory-tab cluster panel.
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
- `agent.goal_worker_bootstrap_enabled` *(bool, `true`)* — controls whether the worker's "propose ~3 goals from persona + rolling summary" LLM call runs when the store is empty. Off → seed goals manually via the Memory tab. Reflection path is unaffected.
- `agent.goal_worker_per_hour_cap` *(int, `3`, min `0`)* — hourly LLM call cap for the `GoalWorker` (bootstrap + reflection combined). `0` disables autonomous calls entirely without unregistering the worker.
- `agent.goal_worker_per_day_cap` *(int, `12`, min `0`)* — daily LLM call cap. With the default `goal_max_active=5`, 12 lets every goal reflect twice a day with headroom for the one-shot bootstrap pass.

### K16 — unified ambient grounding line

Optional fusion of seven "ambient" inner-life signals (circadian, world, activity-awareness, affect/mood, relationship-pulse, user-state, ambient-noise) into a single continuous-awareness paragraph at the top of the system prompt.

- `agent.grounding_line_mode` *(string, `"off"`)* — one of three modes:
  - `"off"` (default, safe rollback) — no fused line; all seven granular blocks render as today.
  - `"replace"` — fused line replaces **all eight** ambient blocks (the seven listed above plus mood_hint). Cleanest test of the companion-feel hypothesis.
  - `"split"` — fused line replaces situational signals (circadian, world, activity, ambient_noise) but **keeps** trend-phrase blocks (affect, mood_hint, relationship, user_state) standalone.

  Verification: `provider_ms.grounding_line` in MCP `get_last_response_detail` is non-zero in `replace`/`split`, missing in `off`. Invalid values clamp to `"off"` with a debug log.

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

Renders a `(faded)` suffix on the RAG memory block for old / decayed rows so the persona reads them as half-remembered instead of as crisp current facts. Fires for archive-tier rows AND for long_term rows that have decayed in place (low salience AND idle for a while). Implementation lives in `_is_faded_memory` inside [`app/core/rag_retriever.py`](../app/core/rag_retriever.py); the persona rule that turns the suffix into a soft hedge lives in [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt).

- `memory.fade_hedge_enabled` *(bool, `true`)* — master switch. Off → no `(faded)` suffix ever, including archive-tier rows. Use when you want Aiko to speak from memory without ever hedging "I think you said this once, ages ago…".
- `memory.faded_salience_threshold` *(float, `0.20`, clamped `[0, 1]`)* — salience floor for a long_term row to register as faded. Higher → more aggressive hedging on lukewarm memories; lower → only very faded rows hedge. Strict `<` semantics — a row sitting exactly on the threshold does NOT fade. Archive-tier rows ignore this and always fade when the master switch is on.
- `memory.faded_idle_days` *(int, `30`, min `1`)* — minimum days since `last_used_at` (or `created_at` if the row has never been touched) before a low-salience long_term row picks up `(faded)`. Strict `>` semantics: a row idle for exactly 30 days does NOT fade. Higher → only very stale rows hedge; lower → more aggressive hedging.

### K22 — callback / inside-joke detector

Post-turn cosine pass between Aiko's reply and older eligible memories. Hits stamp `metadata.callback_count` and bump `salience` + `revival_score` so the retriever's read-side bonus (`_RAG_CALLBACK_BONUS`) prefers memories Aiko has actually managed to weave back into a reply over equally-relevant siblings that have never been cited. The reinforcement is **invisible to the LLM by design** — explicit awareness would lead to meta-narration ("hey, glad I remembered that thing"); the point is for the callback to feel organic. Implementation lives in [`app/core/callback_detector.py`](../app/core/callback_detector.py); the RAG read-side bonus lives in [`app/core/rag_retriever.py`](../app/core/rag_retriever.py). The master switch [`agent.callback_detector_enabled`](#k22--callback--inside-joke-detector) only gates the *write* side — once a memory has `callback_count >= 1`, the read-side bonus stays on even if the user later disables the detector.

- `agent.callback_detector_enabled` *(bool, `true`)* — master switch for the post-turn cosine pass. Off → no new callback stamps. Earned weight on already-stamped rows is preserved.
- `memory.callback_age_floor_days` *(int, `3`, min `1`)* — minimum days since `created_at` before a memory is eligible to be counted as a callback target. Lower than this and the row is treated as part of the current thread, not a callback. Higher → only very-old rows qualify.
- `memory.callback_similarity_threshold` *(float, `0.55`, clamped `[0, 1]`)* — cosine similarity floor against the assistant-reply embedding. Same magnitude as K6 `strong_novelty`. Higher → only paraphrases-of-paraphrases trigger; lower → easier (but noisier) callbacks.
- `memory.callback_max_hits_per_turn` *(int, `3`, min `1`)* — maximum rows stamped on a single turn. Prevents a high-similarity sentence from blanket-bumping every near-duplicate row.
- `memory.callback_cooldown_hours` *(int, `24`, min `1`)* — per-row cooldown after a successful callback. A memory called back less than this ago stays silent on subsequent matches.
- `memory.callback_salience_bump` *(float, `0.05`, clamped `[0, 0.5]`)* — salience added to each hit at record time. Store clamps the result to `[0, 1]`. Drives the compounding loop alongside the read-side bonus.
- `memory.callback_revival_bump` *(float, `0.10`, clamped `[0, 1]`)* — revival_score added to each hit. Acts as a tier-promotion signal: a long_term row that keeps getting called back will trend toward salience=1.0 over the promotion worker's sweeps.

### K20 — metacognitive calibration

Post-turn classifier that detects whether `{user_name}` pushed back on / softened / affirmed Aiko's last claim, and adjusts a per-user `CalibrationState` (a global trust scalar in `[0, 1]` plus a bounded ring of topic slots). The state is read by an inner-life provider on the **next** turn — when the global score sits below `calibration_global_low_threshold` or any topic slot is below `calibration_topic_low_threshold`, Aiko sees a one-line "you've been double-checking me lately — hedge the next claim" cue. The state decays exponentially toward `calibration_baseline` so a tense afternoon doesn't sour the whole week. Implementation lives in [`app/core/calibration_detector.py`](../app/core/calibration_detector.py) and [`app/core/calibration_store.py`](../app/core/calibration_store.py); persona guidance is in the **"When {user_name} has been double-checking you"** block of [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt). K20 deliberately does **not** touch RAG retrieval scores — F3 (`memory.confidence` + `(uncertain)` suffix) already owns the per-memory accuracy lane. K20 is the *per-user / per-topic register tilt* on top of it.

- `agent.calibration_detection_enabled` *(bool, `true`)* — master switch for the post-turn classifier AND the inner-life cue. Off → no new state updates AND `_render_calibration_block` returns empty so the cue goes silent. Earned state on disk is preserved.
- `memory.calibration_baseline` *(float, `0.80`, clamped `[0, 1]`)* — score the global + topic slots decay toward in the absence of new signals. `0.80` reads as "neutral-positive" (Aiko speaks confidently by default). Lower → more reflexively hedgy after any pushback; higher → trust recovers more aggressively between sessions.
- `memory.calibration_global_low_threshold` *(float, `0.55`, clamped `[0, 1]`)* — global score floor for the generic cue. The cue fires only when `global_score < threshold`. Lower → cue is rarer (only after sustained pushback); higher → fires more readily on any drop.
- `memory.calibration_topic_low_threshold` *(float, `0.50`, clamped `[0, 1]`)* — per-topic score floor for the topic-specific cue. The topic cue wins over the global cue when both fire because it carries more actionable hedging guidance.
- `memory.calibration_half_life_days` *(float, `5.0`, min `0.1`)* — exponential half-life for the drift toward baseline. After this many days, the gap between current score and baseline halves. Topic slots use a longer half-life internally (`1.6×` global) so a learned topic stance outlives a general bad day. Higher → calibration sticks longer; lower → faster recovery.
- `memory.calibration_topic_merge_threshold` *(float, `0.78`, clamped `[0, 1]`)* — cosine similarity floor between an incoming `assistant_vec` and an existing topic centroid for the slot to absorb the signal (rather than allocate a new slot). Higher → narrower topics, more slots; lower → broader topics, fewer slots.
- `memory.calibration_softening_threshold` *(float, `0.70`, clamped `[0, 1]`)* — cosine floor between `user_vec` and the **prior** turn's `assistant_vec` for the softening detector to fire. Pairs with the hedge-token regex in an AND-gate: both must hold. Lower → looser gate (catches more rephrases at the cost of false positives); higher → only near-paraphrases trigger.
- `memory.calibration_max_topic_slots` *(int, `8`, min `1`)* — hard cap on the topic-slot ring. On overflow the slot whose `abs(score - baseline)` is smallest AND whose `last_signal_at` is oldest is evicted (the weakest signal that hasn't moved recently). Higher → finer topic resolution at the cost of memory / JSON size; lower → coarser, more global behaviour.

### K24 — sensory anchoring layer

Adaptive per-arc cadence that occasionally surfaces a one-line "small physical beat available: the {item} is right here. If a body anchor would land naturally this reply, you could {hint}…" cue so Aiko can substitute a sensory detail for an emotional statement ("pulling the blanket tighter" instead of "I hear you"). The cue **suggests** an `(item, verb-class)` pair; Aiko's voice picks the actual word. State is in-memory on the controller — there is **no DB / no persistence**, worst case after a restart is one extra beat in the first quiet window. Implementation lives in [`app/core/sensory_anchor.py`](../app/core/sensory_anchor.py); persona guidance is in the **"Small physical beats"** block of [`data/persona/aiko_companion.txt`](../data/persona/aiko_companion.txt). K24 reads `RoomState.posture` + `WorldStore.list_items()` + the live conversation arc; it intentionally **does not** key off `RoomState.activity` (the redundancy edge cases like "snacking + food cue" are left to the persona rule "use it only if it lands" until we observe enough fired beats to decide whether stricter gating is needed).

The per-arc cadence table is hardcoded in the module (not user-configurable): `support` / `reflection` get the highest probability (0.45) and shortest cooldown (4 turns), `casual_check_in` / `playful` are medium (0.25, 6 turns), `silly` is low (0.10, 8 turns), and `planning` is near-silent (0.05, 12 turns). The four `memory.sensory_anchor_*` knobs below scale that table globally.

- `agent.sensory_anchor_enabled` *(bool, `true`)* — master switch for the entire cadence. Off → `_render_sensory_anchor_block` short-circuits to empty string and no beats are ever offered. Per-arc table + recent-slugs ring on disk are not affected (there's nothing on disk).
- `memory.sensory_anchor_min_turn_gap` *(int, `4`, min `1`)* — global cooldown floor between beats. The per-arc table specifies its own cooldown; the effective cooldown is `max(arc_min, min_turn_gap)`. Raise to make beats rarer overall while keeping the per-arc shape intact; lower to honour the per-arc cooldown verbatim. Setting this to a very high number (e.g. `30`) effectively disables the feature without flipping the master switch — useful for testing.
- `memory.sensory_anchor_probability_scale` *(float, `1.0`, clamped `[0.0, 2.0]`)* — multiplier on the per-arc probability. `1.0` ships as designed; `0.5` halves every band (rarer beats across the board); `2.0` pushes `support`'s 0.45 → 0.90, near "fires whenever cooldown is clear and an item is eligible." Useful for A/B testing whether the body beat reads as presence or performance.
- `memory.sensory_anchor_max_recent_items` *(int, `4`, min `1`)* — no-repeat ring size. After firing on the tea pot, that slug stays out of the candidate pool until `max_recent` other items have fired (or the deque overflows). Higher → more variety required, lower → more repetition tolerance. A ring of `1` allows back-to-back fires on the same item; a ring of `10` in a small room (~5-7 items) means most items will be skipped most of the time.
- `memory.sensory_anchor_max_window_items` *(int, `6`, min `1`)* — hard cap on how many room items the selector considers per tick. The world is small today (~10 items per location), but this protects future "100-item garden" scenarios from a quadratic blow-up in the weighted sample step. Lower → only the first N items the world_store returns are eligible (effectively biased toward low-ID, older items); higher → all items get a fair shot.

The cue is **not** added to the K16 grounding-line suppression matrix: the fused grounding paragraph only ever says "you're sitting at the desk" and never enumerates specific items + verb classes, so K24 is additive on top, not redundant. It **is** dropped under `aggressive=True` (when the prompt-assembler is over-budget): body texture is the first thing to go when context is tight. MCP debug tools `get_sensory_anchor_state` (preview a beat without arming the cooldown) and `force_sensory_anchor` (bypass dice + cooldown, emit one beat) are available for end-to-end testing.

### Memory background workers

- `memory.promotion_worker_interval_seconds` *(int, `3600`, min `10`)* — `MemoryPromotionWorker` cadence. Drop to ~60 for active testing.
- `memory.decay_worker_interval_seconds` *(int, `3600`, min `10`)* — `MemoryDecayWorker` cadence. Workers are idempotent; running more often is safe but wastes a little CPU.
- `memory.fact_checker_interval_seconds` *(int, `300`, min `30`)* — F1 `IdleFactChecker` cadence. Defaults to 5 min so newly written memories get verified mid-session.
- `memory.schedule_learner_interval_seconds` *(int, `86400`, min `60`)* — G2 schedule-learner cadence. Once a day is plenty.
- `memory.idle_curiosity_interval_seconds` *(int, `1800`, min `60`)* — G3 idle-curiosity-worker cadence.
- `memory.curiosity_seed_interval_seconds` *(int, `3600`, min `60`)* — K9 curiosity-seed-worker cadence (a ceiling, not a floor — it short-circuits at `curiosity_seed_max_active`).
- `memory.conflict_detector_interval_seconds` *(int, `3600`, min `60`)* — F5 conflict-detector cadence.
- `memory.belief_worker_interval_seconds` *(int, `3600`, min `60`)* — K2 belief-inference-worker cadence.
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
- `avatar.accessory_state` *(object, `{}`)* — persistent accessory toggles. Boolean keys: `lollipop`, `eyeglasses`, `head_sunglasses`, `crossed_arms`. Enum key `eye_color`: `"default"` / `"both_purple"` / `"left_purple"` / `"right_purple"`. Unknown keys are silently dropped at load time so a downgrade can't promote junk into the namespace.

---

## `tools` — `ToolsSettings`

Agent tool registry switches. Each toggles a single tool; `tools.enabled = false` disables the whole registry.

- `tools.enabled` *(bool, `true`)* — master switch for **all** agent tools. Off → Aiko has no tool-calling capability at all (no time lookups, no recall, no web search, no world manipulation).
- `tools.get_time` *(bool, `true`)* — time/date lookup tool.
- `tools.recall` *(bool, `true`)* — explicit memory-recall tool (in addition to automatic RAG).
- `tools.web_search` *(bool, `true`)* — DuckDuckGo-backed web search tool.
- `tools.world` *(bool, `true`)* — Aiko's room tools (`look_around`, `move_to`, `change_posture`, `inspect_item`, `consume_item`). Off → her room is still alive in the world store but she can't act on it.
- `tools.goals` *(bool, `true`)* — K1 goal tools (`list_goals`, `add_goal`, `update_goal_progress`, `archive_goal`). Off → Aiko's prompt block + worker still surface goals but she can't *act* on them mid-turn. Independent from `agent.goals_enabled`: if the master switch is off the tools are wired but no-op because the store is unset.

---

## `mcp_server` — `McpServerSettings`

Embedded MCP (Model Context Protocol) server for development tooling.

- `mcp_server.enabled` *(bool, `true`)* — master switch.
- `mcp_server.port` *(int, `6274`, min `1`)* — SSE endpoint. The Cursor MCP config in `.cursor/mcp.json` points here.

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
- `logging.module_levels` *(object, `{}`)* — per-module overrides, e.g. `{"app.core.prompt_assembler": "DEBUG"}`. Keep the root at `INFO` and dial up just the suspect module.
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

1. Add the field to the relevant dataclass in `app/core/settings.py` with a short inline comment explaining what tuning up vs down does.
2. If users should be able to set it from JSON, add the default to `config/default.json` under the right section.
3. Parse it in `load_settings()` with whatever clamp / fallback makes sense.
4. Add a row to the right section of this file using the format `` - `key` *(type, default)* — what it does. Higher → effect. Lower → effect. ``
5. If it's a user-facing knob (i.e. someone might actually want to tune it without reading the source), add a row to the **Cheatsheet** at the top.
6. Grep this file for the new field name to confirm it's there — the rule's validation step. If it's missing, the change is incomplete.
