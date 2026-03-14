"""Agno-based agent with Ollama, optional storage and tools."""
from __future__ import annotations

from pathlib import Path
from typing import Any

_agno_import_error: Exception | None = None
Agent = None
Ollama = None
try:
    from agno.agent import Agent
    from agno.models.ollama import Ollama
except ImportError as e:
    _agno_import_error = e
    Agent = None
    Ollama = None

# Voice-friendly instructions: replies are spoken via TTS, so keep them natural to hear.
_BASE_INSTRUCTIONS = """\
You are in a voice conversation. Your replies will be read aloud by text-to-speech, so write for listening: use short, clear sentences and natural, conversational phrasing. Avoid long bullet lists, code blocks, or markdown that sounds awkward when spoken; if the user needs detail, give a brief spoken summary. Use the available tools when needed for search or lookup; for normal conversation, answer directly without using tools. Do not use emojis or special characters. At the end of every reply, append exactly one reaction tag on its own line: [[reaction:neutral]] using one of: neutral, excited, surprised, sad, angry, calm.
"""


def _default_storage_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "agno_sessions.db"


def _build_tools() -> list[Any]:
    tools: list[Any] = []
    try:
        from agno.tools.googlesearch import GoogleSearchTools
        tools.append(GoogleSearchTools())
    except ImportError:
        pass
    try:
        from agno.tools.wikipedia import WikipediaTools
        tools.append(WikipediaTools())
    except ImportError:
        pass
    try:
        from agno.tools.arxiv import ArxivTools
        tools.append(ArxivTools())
    except ImportError:
        pass
    return tools


def _build_mcp_tools() -> list[Any]:
    """Windows-MCP via Agno MCPTools (stdio). Call connect_agent_mcp() after create_agent to connect."""
    try:
        from agno.tools.mcp import MCPTools
        return [MCPTools(command="uvx windows-mcp")]
    except ImportError:
        return []


def create_agent(
    *,
    chat_model: str = "llama3.1:8b",
    base_url: str = "http://127.0.0.1:11434",
    temperature: float = 0.6,
    instructions: str | None = None,
    storage_path: Path | None = None,
    database_provider: str = "sqlite",
    database_url: str | None = None,
    add_tools: bool = True,
    add_mcp: bool = True,
) -> Any:
    """Create an Agno Agent with Ollama, storage (SQLite or Postgres), and tools. Create once and reuse."""
    if Agent is None or Ollama is None:
        msg = "agno package not installed or missing dependency; pip install agno"
        if _agno_import_error is not None:
            msg += f". Original error: {_agno_import_error}"
        raise RuntimeError(msg)

    try:
        model = Ollama(id=chat_model, base_url=base_url)
    except TypeError:
        model = Ollama(id=chat_model)
    instr = (instructions or _BASE_INSTRUCTIONS).strip()

    kwargs: dict[str, Any] = {
        "model": model,
        "instructions": instr,
    }

    provider = (database_provider or "sqlite").strip().lower()
    if provider == "postgres" and database_url:
        try:
            from agno.db.postgres import PostgresDb
            db = PostgresDb(db_url=database_url)
            kwargs["db"] = db
            kwargs["add_history_to_messages"] = True
            kwargs["num_history_runs"] = 10
        except ImportError:
            provider = "sqlite"
    if provider != "postgres" or "db" not in kwargs:
        storage_path = storage_path or _default_storage_path()
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from agno.storage.sqlite import SqliteStorage
            kwargs["storage"] = SqliteStorage(
                table_name="agent_sessions",
                db_file=str(storage_path),
            )
            kwargs["add_history_to_messages"] = True
            kwargs["num_history_runs"] = 10
        except ImportError:
            pass

    tools_list: list[Any] = []
    if add_tools:
        tools_list.extend(_build_tools())
    if add_mcp:
        tools_list.extend(_build_mcp_tools())
    if tools_list:
        kwargs["tools"] = tools_list

    return Agent(**kwargs)


def run_agent(
    agent: Any,
    message: str,
    *,
    session_id: str | None = None,
    stream: bool = False,
    on_tool_use: Any = None,
) -> str:
    """Run the agent and return the response content. Optionally report tool use via on_tool_use(name, summary)."""
    if agent is None:
        return ""
    try:
        run_kwargs: dict[str, Any] = {"message": message.strip(), "stream": stream}
        if session_id:
            run_kwargs["session_id"] = session_id
        response = agent.run(**run_kwargs)
        content = ""
        if hasattr(response, "content") and response.content is not None:
            content = (str(response.content) or "").strip()
            if on_tool_use and getattr(response, "tool_calls", None):
                for tc in response.tool_calls:
                    name = getattr(tc, "name", None) or getattr(tc, "tool_name", None) or str(getattr(tc, "tool", tc))
                    if isinstance(name, str):
                        summary = getattr(tc, "result", None) or getattr(tc, "summary", None) or ""
                        try:
                            on_tool_use(name, str(summary)[:200] if summary else "")
                        except Exception:
                            pass
        if not content:
            content = (str(response or "")).strip()
        return content
    except Exception:
        return ""
