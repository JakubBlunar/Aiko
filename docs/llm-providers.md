# LLM providers

Aiko's LLM layer is **catalogue-based**: you save N providers (each
with its own URL + key + headers) into `llm.providers`, then map
roles like `main_chat` and `worker_default` to them through
`llm.routes`. Two routes pointing at the same provider share a
single underlying connection through the shared client cache.

Switch between providers at runtime from **Settings → Chat** — no
restart required. The chat tab has two sections:

1. **Role assignments** — the role → provider/model table. One row
   per active role; pick a provider, a model, a context window, a
   max-tokens budget. Saving rebuilds the live clients immediately.
2. **Saved providers** — the catalogue list: add from a template,
   edit, test credentials, delete entries.

Each role targets a provider independently, so a Gemini free-tier
quota survives a long conversation by keeping `worker_default` on
`local_ollama` while `main_chat` runs on Gemini.

## Curated presets

| Preset | Provider | Endpoint | Recommended models | Free tier |
|---|---|---|---|---|
| **Local Ollama** | `ollama` | `http://127.0.0.1:11434` | `qwen3.5:9b`, `jaahas/qwen3.5-uncensored:9b`, `qwen3.6:27b` | Unlimited (runs on your machine) |
| **Ollama Cloud** | `ollama` | `https://ollama.com` | `llama3.1:70b`, `qwen2.5:72b` | Paid plan required |
| **Google Gemini** | `openai_compatible` | `https://generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.5-flash-lite`, `gemini-2.5-flash`, `gemini-2.5-pro` | ~15 req/min, ~1500 req/day |
| **OpenAI** | `openai_compatible` | `https://api.openai.com/v1` | `gpt-4o-mini`, `gpt-4o`, `gpt-4.1-mini` | Paid (no free tier) |
| **Groq** | `openai_compatible` | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile`, `llama-3.1-8b-instant` | 30 req/min |
| **OpenRouter** | `openai_compatible` | `https://openrouter.ai/api/v1` | `anthropic/claude-3.5-sonnet`, `openai/gpt-4o-mini`, `google/gemini-2.5-flash` | Pay-per-token (some free) |
| **Custom** | either | (bring your own) | (free-text) | — |

The catalogue is served verbatim by `GET /api/llm/presets`; the React
drawer renders one card per template under **Add provider**. Picking a
card pre-fills the endpoint URL and links out to wherever that provider
hands out API keys, so the common path is "click → paste API key →
Save → assign it to a role".

The Local Ollama preset pins `default_context_window` to 65 536 rather
than leaving it on auto-detect. Auto-detect asks Ollama for the model's
advertised maximum, and recent Qwen tags advertise 256 k — a KV cache
that size doesn't fit on a consumer GPU alongside the weights.

## Gemini walkthrough (free-tier)

1. Visit [ai.google.dev](https://ai.google.dev), grab a free API key.
2. Open **Settings → Chat → Saved providers → + Add provider** and
   click the **Google Gemini** card.
3. Hit **Edit** on the new row and paste the key into **API key** (a
   password input; the key is never echoed back through any GET
   endpoint), then **Save**.
4. Click **Test** on the row. A green toast with the latency in ms
   means everything is wired correctly; a red one shows Gemini's
   verbatim error (most often `unauthorized` for a typo'd key).
5. In **Role assignments**, set `main_chat`'s provider to Gemini. The
   **Model** combobox suggests `/v1/models` plus the preset's curated
   names — `gemini-2.5-flash-lite` is the fastest free-tier option.
6. Click **Save** on the row. The change takes effect on your next
   message — no restart needed.

Leave `worker_default` and `workflow` pointed at `local_ollama`.
Background workers (reflection, dream, memory extraction, belief
inference, ~24 jobs total) fire many times per hour and would drain
Gemini's 1500-req/day budget in well under an hour. Point them at
Gemini too if you'd rather not run a local model at all — the remote
provider's quota is on you in that case.

## Provider catalogue + role mapping

The schema lives in [`app/core/infra/settings.py`](../app/core/infra/settings.py)
as four slotted dataclasses on `AppSettings.llm`:

```python
@dataclass(slots=True)
class LlmProvider:
    id: str                       # stable, unique
    name: str                     # display name
    kind: str                     # "ollama" | "openai_compatible"
    base_url: str
    api_key: str = ""             # explicit (write-only via PUT)
    api_key_env: str = ""         # env-var fallback
    extra_headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: int = 300
    keep_alive: str = "30m"
    reasoning_effort: str = ""    # GPT-5 / o-series / Grok
    api_style: str = "auto"       # auto | responses | chat_completions
    think_num_predict_headroom: int = 2048

@dataclass(slots=True)
class LlmRoute:
    provider_id: str              # references LlmProvider.id
    model: str
    context_window: int | None = None
    max_tokens: int = 512
    temperature: float | None = None
    reasoning_effort: str = ""    # overrides the provider's value

@dataclass(slots=True)
class LlmEmbedding:
    provider_id: str = "local_ollama"
    model: str = "qwen3-embedding:0.6b"
    num_ctx: int | None = None    # VRAM lever
    num_gpu: int | None = None    # 0 forces CPU

@dataclass(slots=True)
class LlmSettings:
    providers: list[LlmProvider] = field(default_factory=list)
    routes: dict[str, LlmRoute] = field(default_factory=dict)
    embedding: LlmEmbedding = field(default_factory=LlmEmbedding)
```

Canonical roles are exported from `settings.py` as
`LLM_ROLE_MAIN_CHAT = "main_chat"`,
`LLM_ROLE_WORKER_DEFAULT = "worker_default"` and
`LLM_ROLE_WORKFLOW = "workflow"`. New roles can be added without a
schema change — the table is keyed by role string.

A typical `user.json`:

```json
"llm": {
  "providers": [
    {
      "id": "local_ollama",
      "name": "Local Ollama",
      "kind": "ollama",
      "base_url": "http://127.0.0.1:11434"
    },
    {
      "id": "openai",
      "name": "OpenAI",
      "kind": "openai_compatible",
      "base_url": "https://api.openai.com/v1",
      "api_key_env": "OPENAI_API_KEY"
    }
  ],
  "routes": {
    "main_chat":      { "provider_id": "openai",       "model": "gpt-5-mini", "context_window": 131072 },
    "worker_default": { "provider_id": "local_ollama", "model": "qwen3.5:9b", "context_window": 65536 },
    "workflow":       { "provider_id": "local_ollama", "model": "qwen3.5:9b", "context_window": 65536 }
  },
  "embedding": {
    "provider_id": "local_ollama",
    "model": "qwen3-embedding:0.6b",
    "num_ctx": 2048,
    "num_gpu": 0
  }
}
```

The shipped defaults put all three roles on `qwen3.5:9b` at 65 536
tokens: ~6.6 GB of weights plus a 64 k KV cache is the largest
combination that fits comfortably in 12 GB of VRAM, and one model
across all three roles means Ollama keeps a single copy resident.

### Shared client cache

[`app/llm/factory.py`](../app/llm/factory.py) exposes a `ClientCache`
keyed by `(kind, base_url, resolved_api_key)`. Two routes pointing at
the same provider share one underlying `ChatClient` instance — no
duplicated HTTP connection pools, no duplicated keep-alive cost.
Edit a provider's credentials and the cache slot is invalidated; the
next `get()` rebuilds. App shutdown drains the cache.

Inspect the live cache state from MCP with `get_client_cache_stats()`:

```json
{
  "entries": 1,
  "providers": 2,
  "keys": [
    {"kind": "ollama", "base_url": "http://127.0.0.1:11434",
     "has_api_key": false, "provider_ids": ["local_ollama"]}
  ]
}
```

`entries=1` with `providers=2` means both providers share one slot
— that's the cache earning its keep.

### How the routing works under the hood

`SessionController` keeps two `ChatClient` references side by side:

- `self._chat_client` — used by `TurnRunner` (the live chat path) and
  `ProactiveDirector` (typed/voice proactive nudges). Built from
  `routes.main_chat` and the matching `LlmProvider`.
- `self._worker_client` — used by every background worker
  (`ReflectionWorker`, `DreamWorker`, `MemoryExtractor`,
  `BeliefInferenceWorker`, `MemoryConflictWorker`,
  `IdleFactChecker`, `CuriosityWorker`, `CuriositySeedWorker`,
  `GoalWorker`, `IdleCuriosityWorker`, `ArcSmootherWorker`,
  `MomentDetector`, `NarrativeWeaver`, `PromiseExtractor`,
  `MemoryConsolidator`, `RelationshipPulseWorker`, `UserProfileWorker`,
  `DialogueActTagger`, `AgendaWorker`,
  `SummaryWorker`, and a few more). Built from `routes.worker_default`.

Both implement the structural [`ChatClient`](../app/llm/chat_client.py)
protocol and are built by `build_client_for_route()` — at boot, and
again on every `update_route(...)` call, which rebuilds the whole
topology in place so the workers already holding a reference follow
along.

When `main_chat` and `worker_default` agree on both provider *and*
context window they resolve to the same client. They need separate
instances otherwise: the client's transport carries the default
`num_ctx`, which is what sizes Ollama's KV cache on first load.

`self._ollama` is a back-compat alias for `self._worker_client` — too
many worker init sites (24+) reference it for a one-shot rename to
buy us anything. New code should use the explicit names.

## Migrating from the legacy `chat_llm` / `ollama` config

Both blocks were retired — `llm` is the only LLM config surface. On
the first boot after upgrading, [`_migrate_legacy_llm`](../app/core/infra/settings.py)
synthesises the new block from whatever the old ones held (it runs
whenever `llm.providers` is empty):

1. **Local Ollama** is always created from `ollama.base_url` /
   `ollama.timeout`. Id: `local_ollama`.
2. If `chat_llm.provider` is anything other than `ollama` (or the
   `base_url` doesn't match the local Ollama), a second provider is
   created from the `chat_llm` block. The id comes from
   `chat_llm.provider_preset` when set (`"openai"`, `"gemini"`, …),
   falling back to `"chat_migrated"`.
3. `main_chat` → the chat provider; `worker_default` and `workflow`
   → `local_ollama`. Model + context_window + max_tokens come from
   `chat_llm`; the worker model from `ollama.chat_model`.
4. `ollama.embedding_*` folds into `llm.embedding`.

The result is written to `user.json` and the `chat_llm` / `ollama`
keys are **deleted** — otherwise they'd keep winning the deep merge
and shadowing the block that replaced them. Later boots parse `llm`
directly. Any API key sitting in the retired keychain account is
re-filed onto the `main_chat` provider's account rather than orphaned.

Nothing is required of you, but be aware the migration reads what was
on disk: if you had hand-edited both blocks into disagreement, check
**Settings → Chat** afterwards. Deleting `llm` from `user.json`
re-runs the migration on the next boot only if the legacy keys are
still there; otherwise you fall back to the shipped defaults.

## API key storage (OS keychain)

API keys are **not** written as plaintext to `config/user.json`. They
live in the operating system's secure credential store — Windows
Credential Manager, macOS Keychain, or the Freedesktop Secret Service
on Linux — via the [`keyring`](https://pypi.org/project/keyring/)
package, wrapped by [`app/core/infra/secret_store.py`](../app/core/infra/secret_store.py).

How it works:

- **Write-through.** Every credential save (UI →
  `update_provider_credentials` / `add_provider`) routes the key
  through `secret_store.store_or_passthrough(account, key)`. When a
  keychain backend is present the secret is stashed there and `""` is
  written to disk. When **no** backend is usable (headless box, locked
  keychain) the key falls back to plaintext in `user.json` so it is
  never silently lost.
- **In-memory only.** The resolved key still lives on the in-memory
  `LlmProvider.api_key` dataclass for the life of the process, so every
  read / cache-key / `has_api_key` masking path is unchanged — only
  *persistence* is redirected.
- **Boot migration + hydration.** `SessionController._init_secret_storage`
  (first thing in `__init__`) moves any leftover plaintext key from
  `user.json` into the keychain and blanks it on disk, and conversely
  hydrates an in-memory key from the keychain when the on-disk value is
  blank. A key left under the retired `chat_llm` account is adopted onto
  the `main_chat` provider's account (`provider:<id>`) and the old
  account deleted, so there is no second, drift-prone copy.
- **Accounts.** Service namespace `aiko-assistant`; one account per
  credential — `provider:<id>` per catalogue row.
- **Inert under pytest** so the test suite never touches the developer's
  real keychain (the historical plaintext-config behaviour is preserved
  in tests).

## API key resolution order

When the chat client is built, the in-memory `api_key` is sourced in
this order (the keychain is consulted at boot to populate it — see
above):

1. **Explicit value** in `provider.api_key` (populated via the UI
   write-through, or hydrated from the keychain at boot).
2. **Environment variable** named in `api_key_env`. Empty `api_key_env`
   falls back to a per-host hint (see `_PROVIDER_ENV_HINTS` in
   `session_controller.py` / `factory.py`):
   - `api.openai.com` → `OPENAI_API_KEY`
   - `api.groq.com` → `GROQ_API_KEY`
   - `openrouter.ai` → `OPENROUTER_API_KEY`
   - `api.x.ai` → `XAI_API_KEY`
   - `generativelanguage.googleapis.com` → `GEMINI_API_KEY`
   - `ollama.com` → `OLLAMA_API_KEY`

Setting the key via an environment variable is still supported and also
keeps it out of `config/user.json`. The drawer treats a populated
env-var value the same as a typed-in key — both surface as
`has_api_key: true` in the masked snapshot.

## REST surface

### Catalogue + role mapping

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/llm/providers` | List the saved provider catalogue with credentials masked. |
| `POST` | `/api/llm/providers` | Add a provider. Body: `{template_id?: str, draft: {...}}`. Returns 409 on id collision. |
| `PATCH` | `/api/llm/providers/{id}` | Edit non-credential fields. `api_key` / `api_key_env` are **stripped** here as a safety net. |
| `PUT` | `/api/llm/providers/{id}/credentials` | Replace `api_key` / `api_key_env` on a saved provider. Rejects whitespace in the key. |
| `DELETE` | `/api/llm/providers/{id}` | Delete a provider. 409 when still referenced by a route — retarget the route first. |
| `POST` | `/api/llm/providers/{id}/test` | One-token probe against the saved credentials. Body (optional): `{model?: str, context_window?: int}`. |
| `GET` | `/api/llm/routes` | List role assignments. |
| `PATCH` | `/api/llm/routes/{role}` | Set `provider_id` / `model` / `context_window` / `max_tokens` / `temperature` / `reasoning_effort` for a role. Unspecified keys keep their current value. Persists, then rebuilds the live clients and cascades to the TurnRunner, the proactive director and every worker. 404 when `provider_id` is unknown. |

### Models

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/llm/presets` | Returns the curated template catalogue (the Add-provider cards). Read-only. |
| `GET` | `/api/models?provider=…` | Model list. `provider` is a catalogue provider **id**; omit it for the active chat provider. Prepends the configured model so the dropdown always has a working selection. |
| `GET` | `/api/models/required` | Which locally-hosted models the current routes need, and whether each one is actually downloaded. Unlike `/api/models` this reports the provider's real inventory, so "configured but never pulled" is visible. Backs the first-run model step. |
| `POST` | `/api/models/pull` | Start downloading a model onto an Ollama provider. Body: `{model, provider_id?}`. Returns **202** immediately — a multi-GB pull outlives any HTTP timeout — and streams `model_pull_progress` WS events until one carries `status: "done"` or `status: "error"` (every other `status` is Ollama's own phase string). 409 on a concurrent pull of the same model, 400 for a hosted provider (nothing to download). |
| `POST` | `/api/llm/test-connection` | Dry-run one-token chat ping against **candidate** creds that haven't been saved yet. Never persists, never touches the shared client cache. Use `/api/llm/providers/{id}/test` for a saved provider. |

The `llm_settings_changed` WebSocket event broadcasts every time the
catalogue or the routes change. Payload carries two top-level keys:
`providers` (masked catalogue) and `routes` (role table).

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

## Picking a model — free-text combobox

The Model field in each **Role assignments** row is a free-text
combobox (`<input list>` + `<datalist>`), not a hard `<select>`. That
means:

- The suggestion list shows the **union** of the matching preset's
  `recommended_models` (e.g. for OpenAI: `gpt-5-mini`, `gpt-5-nano`,
  `gpt-4.1-mini`, `gpt-4.1-nano`) and the live `/v1/models` response
  for the chosen provider — so `gpt-5-mini` stays pickable even when
  OpenAI's API doesn't return it for an unverified org.
- You can also **type any model id** the suggestion list doesn't
  contain — a brand-new release, an experimental preview, an
  OpenRouter-prefixed id like `anthropic/claude-3.5-sonnet`, a
  fine-tuned model id, anything. Save accepts it as-is.
- **Test before Save** when you type a custom id. A typo gets caught
  immediately by the provider's `400` / `404` response surfaced under
  the row. Saving an unrecognised id and *then* finding out at the
  next chat turn is the slow path.

## Tuning the context window

The chat path's prompt-assembly budget is resolved at boot and on
every route change with three-step precedence:

1. **Explicit route value** — `llm.routes[role].context_window`,
   editable from the row's **Context window** number input. Set to
   `0` / blank to fall through to the next step. **Recommended for
   local models**: step 2 asks Ollama for the model's advertised
   maximum, and recent Qwen tags advertise 256 k, which won't fit
   in consumer VRAM alongside the weights.
2. **Client lookup** — the active `ChatClient` answers
   `get_context_length(model)`. `OllamaClient` hits `/api/show`
   per model. `OpenAICompatibleClient` consults a static
   prefix-keyed table (see `_CONTEXT_WINDOW_TABLE` in
   [`app/llm/openai_compatible_client.py`](../app/llm/openai_compatible_client.py))
   that maps known cloud model ids to **conservative caps**:

   | Family | Cap | Reason |
   |---|---|---|
   | `gpt-5*` (mini, nano, pro, 5.1, 5.2, 5.4-*, 5.5-*) | 131 072 | Native 400 k, capped at 128 k |
   | `gpt-4.1*` | 131 072 | Native 1 M, capped at 128 k |
   | `gpt-4o*`, `gpt-4-turbo` | 131 072 | Native 128 k |
   | `gpt-4` | 8 192 | Native 8 k |
   | `gpt-3.5-turbo` | 16 385 | Native 16 k |
   | `o1`, `o3`, `o4-mini` | 200 000 | Native 200 k |
   | `gemini-2.5-*` (`models/gemini-2.5-*` also) | 131 072 | Native 1-2 M, capped at 128 k |
   | `llama-3.3-*`, `llama-3.1-*` | 131 072 | Native 128 k |
   | `claude-3*`, `claude-4` (incl. `anthropic/claude-*`) | 200 000 | Native 200 k |
   | unknown | — | falls through to step 3 |

3. **Hardcoded fallback** — `8192`. Only hit when neither the
   override nor the client lookup produces a value (typically only
   for truly unknown remote model ids).

The current source is reported by
`SessionController.context_window_source` (`config`, `client`, or
`fallback`) and surfaces in `get_status` and the Diagnostics tab.

### Why a cap, not the model's true max?

`gpt-4.1-mini`'s real context is 1 M tokens; `gemini-2.5-pro`'s
is 2 M. We cap both at 128 k because:

- **Real conversational use rarely exceeds 50 k.** A 1 M budget
  just gives compaction a permission slip to be lazy.
- **Memory and CPU.** The prompt assembler builds a token-budget
  estimate per provider for every turn — wider budgets are
  measurably slower.
- **Billing cliff.** OpenAI's `gpt-5.4` / `gpt-5.5` long-context
  tier roughly doubles input price above 200 k tokens. Capping
  at 128 k stays firmly in the cheaper short-context column.

You can bump the override up from 131 072 if you genuinely need
more (very long-form chat, in-app document Q&A, etc.) — but the
default makes the typical case both fast and cheap.

## Responses API (`/v1/responses`) for the dotted GPT-5.x line

The original GPT-5 models (`gpt-5`, `gpt-5-mini`, `gpt-5-nano`,
`gpt-5-pro`) and the o-series work fine on `/v1/chat/completions`.
The **decimal-versioned** siblings (`gpt-5.1`, `gpt-5.4-mini`,
`gpt-5.5-*`, …) moved reasoning behind the **Responses API**: on
`/v1/chat/completions` they return

```
400 — Function tools with reasoning_effort are not supported for
gpt-5.4-mini in /v1/chat/completions. Please use /v1/responses instead.
```

So `OpenAICompatibleClient` auto-routes any model matching the dotted
pattern (`_use_responses_api` →
[`app/llm/openai_compatible_client.py`](../app/llm/openai_compatible_client.py))
through `POST /v1/responses` for **every** call — the tool-decision
pass, the streaming reply, JSON workers, and the test-connection probe.
No config flag is needed; pick the model and it just works.

What the client translates between the two shapes:

| Concept | `/v1/chat/completions` | `/v1/responses` |
|---|---|---|
| Messages | `messages: [...]` | `input: [...]` (tool calls become `function_call` / `function_call_output` items) |
| Reasoning | `reasoning_effort: "low"` (flat) | `reasoning: {effort: "low"}` |
| Function tools | `{type:"function", function:{name,...}}` | `{type:"function", name, ...}` (flattened) |
| Forced tool | `tool_choice:{type:"function",function:{name}}` | `{type:"function",name}` |
| Output budget | `max_completion_tokens` | `max_output_tokens` |
| JSON mode | `response_format:{type:"json_object"}` | `text:{format:{type:"json_object"}}` |
| Visible text | `choices[0].message.content` | `output[]` → `message` → `output_text` parts |
| Tool calls | `choices[0].message.tool_calls` | `output[]` → `function_call` items |
| Usage | `prompt_tokens` / `completion_tokens` | `input_tokens` / `output_tokens` |
| Truncation | `finish_reason="length"` | `status="incomplete"` + `incomplete_details.reason="max_output_tokens"` |

**Reasoning-token reserve.** On the Responses API `max_output_tokens`
caps reasoning **and** visible tokens *combined* — a tight budget lets
the reasoning trace eat the whole allowance and return an empty
`incomplete` response. To preserve the caller's intended *visible*
budget (`llm.routes[role].max_tokens` → `num_predict`), the client adds a
per-effort reserve on top of it (`_RESPONSES_REASONING_RESERVE`:
`minimal=256`, `low=1024`, `medium=4096`, `high=8192`, `xhigh=16384`,
`none=0`). It's a ceiling, not a floor — the model only spends what it
needs, so the reserve costs nothing on easy turns and prevents silent
truncation on hard ones.

Temperature is never sent for these models (the reasoning family locks
it to the default and 400s on any other value). The legacy
chat-completions omit-hack (`_tools_with_reasoning_unsupported`) is kept
as a fallback for the rare case a dotted model is forced onto
chat-completions, but the normal path never reaches it.

## OpenAI prompt caching

OpenAI's API automatically applies a ~90 % discount on cached
**input** tokens — any prefix that exactly matches a previous
request within ~5–10 minutes counts as cached. The cached-input
column in the [official pricing
page](https://openai.com/api/pricing) shows the discounted rate:

| Model | Input | **Cached input** | Output |
|---|---|---|---|
| `gpt-5-mini` | $0.25 / M | $0.025 / M | $2.00 / M |
| `gpt-5-nano` | $0.05 / M | $0.005 / M | $0.40 / M |
| `gpt-4.1-mini` | $0.40 / M | $0.10 / M | $1.60 / M |
| `gpt-4.1-nano` | $0.10 / M | $0.025 / M | $0.40 / M |

Aiko's prompt assembler builds a very stable prefix: the persona
block, RAG memories that aren't churning, the bulk of recent
chat history. So most turns after the first hit OpenAI's cache
on a large fraction of the input.

A realistic per-turn cost at 50 k context + 250 output tokens
(typical for `max_tokens=512`):

| Model | Effective per turn | Per 100 turns |
|---|---|---|
| `gpt-5-nano` | ~$0.0004 | ~$0.04 |
| `gpt-5-mini` | ~$0.0021 | ~$0.21 |
| `gpt-4.1-nano` | ~$0.0015 | ~$0.15 |
| `gpt-4.1-mini` | ~$0.0058 | ~$0.58 |

No code change required — caching is transparent on OpenAI's
side. The prompt assembler is laid out specifically to maximise the
cached prefix, and the per-turn telemetry surfaces the hit-rate so
you can verify it. Three ergonomic notes:

- **Cache TTL is 5–10 min.** A long silence triggers a full-price
  re-warm on the next turn. Aiko's proactive nudges in
  typed-silence mode keep the cache warm "by accident".
- **`prompt_tokens_details.cached_tokens`** is the field the API
  returns to tell you how many input tokens hit the cache. It is
  lifted onto `ChatUsage.cached_tokens` and surfaces in two places:
  the `turn done:` INFO line as `cached=N cached_pct=%.1f` (grep
  `data/app.log` for `cached_pct=` to chart hit-rate over time),
  and the MCP `get_last_response_detail` tool under
  `usage.cached_tokens` / `usage.cached_tokens_pct`.
- **Prefix-stability is enforced in code.** The prompt assembler
  arranges `system_parts` from most-stable (T0) to most-volatile
  (T6); adding a new block in the wrong tier is the single most
  common reason cache-hit-rate stays low. The contract +
  contributor guide live in [`docs/prompt-caching.md`](prompt-caching.md);
  cross-tier invariants are pinned by
  `tests/test_prompt_assembler.py::PromptCachePrefixOrderingTests`.

## See also

- [configuration.md](configuration.md) — cheatsheet for every
  `config/default.json` key.
- [prompt-caching.md](prompt-caching.md) — the prefix-stability
  ladder that drives the cached-input column above, plus the
  contributor guide for adding new prompt blocks without breaking
  the cache.
- [AGENTS.md](../AGENTS.md) — top-level project conventions (the
  "Debugging via logs" section covers the wrong-provider symptom
  and the "Low cache-hit rate on OpenAI" diagnosis flow).
