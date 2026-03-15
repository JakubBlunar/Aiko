# Agno toolkit dependencies

Which toolkits are enabled is set in `config/tooling.default.json` (and overrides in `config/tooling.user.json`) under `agno_toolkits`. Each toolkit may require extra Python packages and/or environment variables. Install only the deps for the toolkits you enable.

## Optional install (extras)

Install all optional Agno toolkit deps in one go:

```powershell
pip install -e ".[agno-tools]"
```

Or install per toolkit:

```powershell
pip install -e ".[agno-duckduckgo]"   # DuckDuckGo search
pip install -e ".[agno-youtube]"      # YouTube captions/metadata
pip install -e ".[agno-openweather]"   # Weather (also needs OPENWEATHER_API_KEY)
pip install -e ".[agno-yfinance]"      # Stock/quotes
```

## Toolkit id → pip packages and env vars

| Toolkit id     | Pip packages              | Env vars (optional/required) |
|----------------|---------------------------|------------------------------|
| calculator     | (none)                    | —                            |
| duckduckgo     | `ddgs`                    | —                            |
| wikipedia      | (none)                    | —                            |
| arxiv          | (none)                    | —                            |
| googlesearch   | (none)                    | Depends on Agno/backend      |
| youtube        | `youtube_transcript_api`  | —                            |
| openweather    | `requests`                | `OPENWEATHER_API_KEY`        |
| yfinance       | `yfinance`                | —                            |
| todoist        | (none)                    | `TODOIST_API_KEY`            |
| user_control_flow | (none)                 | —                            |

**Coding tools** (read/edit files) use a separate MCP server and the `coding-mcp` extra: `pip install -e ".[coding-mcp]"`. Enable and choose folders in Settings → Coding.

If a toolkit fails to load, the app logs a copy-pastable install line, e.g. `To enable toolkit 'duckduckgo', install: pip install ddgs`.
