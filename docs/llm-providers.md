# LLM providers

Aiko's chat path can talk to any of the following providers. Switch
between them at runtime from **Settings → Chat → Chat provider** — no
restart required for the user-visible chat path. Background workers
have an opt-in fallback to local Ollama (on by default for remote
providers) so a Gemini free-tier quota survives a long conversation.

## Curated presets

| Preset | Provider | Endpoint | Recommended models | Free tier |
|---|---|---|---|---|
| **Local Ollama** | `ollama` | `http://127.0.0.1:11434` | `llama3.1:8b`, `qwen2.5:7b`, `jaahas/qwen3.5-uncensored:9b` | Unlimited (runs on your machine) |
| **Ollama Cloud** | `ollama` | `https://ollama.com` | `llama3.1:70b`, `qwen2.5:72b` | Paid plan required |
| **Google Gemini** | `openai_compatible` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash-lite`, `gemini-2.5-flash`, `gemini-2.5-pro` | ~15 req/min, ~1500 req/day |
| **OpenAI** | `openai_compatible` | `https://api.openai.com/v1` | `gpt-4o-mini`, `gpt-4o`, `gpt-4.1-mini` | Paid (no free tier) |
| **Groq** | `openai_compatible` | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile`, `llama-3.1-8b-instant` | 30 req/min |
| **OpenRouter** | `openai_compatible` | `https://openrouter.ai/api/v1` | `anthropic/claude-3.5-sonnet`, `openai/gpt-4o-mini`, `google/gemini-2.5-flash` | Pay-per-token (some free) |
| **Custom** | either | (bring your own) | (free-text) | — |

The catalogue is served verbatim by `GET /api/llm/presets`; the React
drawer renders one card per row. Picking a card pre-fills the endpoint
URL, the recommended model, and the workers-use-local toggle so the
common path is "click → paste API key → Save".

## Gemini walkthrough (free-tier)

1. Visit [ai.google.dev](https://ai.google.dev), grab a free API key.
2. Open **Settings → Chat → Chat provider**, click the **Google Gemini**
   card.
3. Paste the key into **API key** (the field is a password input; the
   key is never echoed back through any GET endpoint).
4. The **Model** dropdown auto-populates from `/v1/models`. Pick
   `gemini-2.5-flash-lite` for the fastest free-tier responses, or
   `gemini-2.5-pro` if you don't mind the slower rate-limit.
5. Click **Test connection**. A green check with the latency in ms
   means everything is wired correctly; a red banner shows Gemini's
   verbatim error (most often `unauthorized` for a typo'd key).
6. Click **Save provider**. The change takes effect on your next
   message — no restart needed.

During the conversation:

- **Aiko's chat** (the visible turn) goes to Gemini.
- **Background workers** (reflection, dream, memory extraction,
  belief inference, ~24 jobs total) stay on local Ollama because
  `workers_use_local=true` is the default for non-Ollama providers.
  This is intentional — those workers fire many times per hour and
  would drain Gemini's 1500-req/day budget in well under an hour.

Want to use Gemini for everything? Toggle **Background workers use
local Ollama** off in the same panel. The remote provider's quota is
on you in that case.

## How the routing works under the hood

`SessionController` keeps two `ChatClient` references side by side:

- `self._chat_client` — used by `TurnRunner` (the live chat path) and
  `ProactiveDirector` (typed/voice proactive nudges). This is the
  client the user picks in the drawer.
- `self._worker_client` — used by every background worker
  (`ReflectionWorker`, `DreamWorker`, `MemoryExtractor`,
  `BeliefInferenceWorker`, `MemoryConflictWorker`,
  `IdleFactChecker`, `CuriosityWorker`, `CuriositySeedWorker`,
  `GoalWorker`, `IdleCuriosityWorker`, `ArcSmootherWorker`,
  `MomentDetector`, `NarrativeWeaver`, `PromiseExtractor`,
  `MemoryConsolidator`, `RelationshipPulseWorker`, `UserProfileWorker`,
  `DialogueActTagger`, `AgendaWorker`, `SelfImageWorker`,
  `SummaryWorker`, and a few more). For non-Ollama chat providers,
  defaults to a fresh local `OllamaClient`.

Both implement the structural [`ChatClient`](../app/llm/chat_client.py)
protocol. The chat client is built by `_build_chat_client()` at boot
and on every `reconfigure_chat_llm()` call.

`self._ollama` is a back-compat alias for `self._worker_client` — too
many worker init sites (24+) reference it for a one-shot rename to
buy us anything. New code should use the explicit names.

## API key resolution order

When the chat client is built, the API key is sourced in this order:

1. **Explicit value** in `chat_llm.api_key` (typically populated via
   `PUT /api/settings/llm-credentials` from the UI).
2. **Environment variable** named in `chat_llm.api_key_env`. Empty
   `api_key_env` falls back to a per-host hint (see
   `_PROVIDER_ENV_HINTS` in `session_controller.py`):
   - `api.openai.com` → `OPENAI_API_KEY`
   - `api.groq.com` → `GROQ_API_KEY`
   - `openrouter.ai` → `OPENROUTER_API_KEY`
   - `api.x.ai` → `XAI_API_KEY`
   - `generativelanguage.googleapis.com` → `GEMINI_API_KEY`
   - `ollama.com` → `OLLAMA_API_KEY`

Storing the key in an environment variable keeps it out of
`config/user.json`. The drawer treats a populated env-var value the
same as a typed-in key — both surface as `has_api_key: true` in the
masked snapshot.

## REST surface

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/settings` | Returns the `chat_llm` block with `has_api_key` instead of the raw key. |
| `PATCH` | `/api/settings` | `chat_llm` branch triggers `reconfigure_chat_llm`. The `api_key` field is **stripped** here as a safety net — use `PUT /api/settings/llm-credentials` instead. |
| `PUT` | `/api/settings/llm-credentials` | Write-only path for `api_key` / `api_key_env` / `base_url` / `extra_headers`. Returns the masked snapshot. |
| `GET` | `/api/llm/presets` | Returns the curated catalogue. Read-only. |
| `GET` | `/api/models?provider=…` | Preview model list for a non-active provider (used by the drawer when picking a preset card). |
| `POST` | `/api/llm/test-connection` | Dry-run one-token chat ping against candidate creds. Never persists. Returns `{success, latency_ms, prompt_tokens, completion_tokens, model_resolved, error_code, error_message}`. |

The `llm_settings_changed` WebSocket event broadcasts every time the
chat_llm config changes, so a settings drawer open in another tab
reloads its view.

## Provider quirks

- **Gemini's OpenAI-compat layer** does not accept `system` role on
  every model. The `OpenAICompatibleClient` collapses every `system`
  message into the first `user` message (with a blank line as a
  separator) when the configured model starts with `gemini-` or
  `models/gemini-`. The persona is preserved; the wire shape is
  different.
- **Truncation logging** is unified. Both `OllamaClient` and
  `OpenAICompatibleClient` log a WARNING when the response stops on a
  `length` / `finish_reason: "length"` sentinel, with the same
  `surface=` / `model=` / `completion_tokens=` fields. Grep
  `tail_logs(module_contains="ollama", level="WARNING")` for Ollama
  and `module_contains="openai"` for the remote path.
- **OpenRouter** wants two extra headers for analytics:
  `HTTP-Referer` and `X-Title`. Drop them into **Advanced → Extra
  headers** (JSON object). Empty values are filtered out
  automatically.

## See also

- [configuration.md](configuration.md) — cheatsheet for every
  `config/default.json` key.
- [AGENTS.md](../AGENTS.md) — top-level project conventions (the
  "Debugging via logs" section covers the wrong-provider symptom).
