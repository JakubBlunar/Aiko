# Toolkit dependencies

Which toolkits are enabled is set in `config/tooling.default.json` (and overrides in `config/tooling.user.json`) under `toolkits` (or backward-compat `agno_toolkits`). The tool registry is config-driven; by default no toolkits are loaded. Each toolkit may require extra Python packages and/or environment variables once you add a factory for it.

## Optional install (extras)

The project defines optional extras for common toolkits. Install only the deps for the toolkits you enable:

```powershell
pip install -e ".[agno-tools]"
```

Or per toolkit:

```powershell
pip install -e ".[agno-duckduckgo]"   # DuckDuckGo search
pip install -e ".[agno-youtube]"      # YouTube captions/metadata
pip install -e ".[agno-openweather]"  # Weather (OPENWEATHER_API_KEY)
pip install -e ".[agno-yfinance]"    # Stock/quotes
```

## Adding toolkits

Toolkits are registered in the agent's tool registry. To add a new toolkit: implement a LangChain-compatible tool (or adapter) for the toolkit id, register it in the registry, and add the toolkit id (and optional params) to `toolkits` in config. MCP servers are configured separately under `tools.mcp` and loaded via LangChain MCP adapters.
