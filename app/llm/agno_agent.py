"""Agno-based agent with Ollama, optional storage and tools."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

RunEvent = None
try:
    from agno.agent import RunEvent
except ImportError:
    pass

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
# Start each reply with [[reaction:X]] so TTS can use mood from the first line when streaming.
_BASE_INSTRUCTIONS = """\
You are in a voice conversation. Your replies will be read aloud by text-to-speech, so write for listening: use short, clear sentences and natural, conversational phrasing. Avoid long bullet lists, code blocks, or markdown that sounds awkward when spoken; if the user needs detail, give a brief spoken summary. Use the available tools when needed for search or lookup; for normal conversation, answer directly without using tools. Do not use emojis or special characters.

At the **start** of every reply, on the first line, write exactly one reaction tag: [[reaction:neutral]] then a blank line, then your reply. Use one of: neutral, cheerful, excited, surprised, sad, angry, calm, serious, friendly, gentle, enthusiastic. At the end of every reply you may also append the same tag for compatibility.
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
    assistant_background: str | None = None,
    storage_path: Path | None = None,
    database_provider: str = "sqlite",
    database_url: str | None = None,
    add_tools: bool = True,
    add_mcp: bool = True,
    learning_enabled: bool = True,
) -> Any:
    """Create an Agno Agent with Ollama, storage/db, optional Learning (user profile + memory), and tools. Create once and reuse."""
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
    if (assistant_background or "").strip():
        instr = f"{instr}\n\nAssistant background: {assistant_background.strip()}"

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
        if learning_enabled:
            try:
                from agno.db.sqlite import SqliteDb
                from agno.learn import LearningMachine
                kwargs["db"] = SqliteDb(db_file=str(storage_path))
                kwargs["add_history_to_messages"] = True
                kwargs["num_history_runs"] = 10
                kwargs["learning"] = LearningMachine(
                    user_profile=True,
                    user_memory=True,
                )
            except ImportError:
                learning_enabled = False
        if not learning_enabled or "db" not in kwargs:
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
    elif learning_enabled:
        try:
            from agno.learn import LearningMachine
            kwargs["learning"] = LearningMachine(
                user_profile=True,
                user_memory=True,
            )
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


def _content_from_response(response: Any) -> str:
    """Extract reply text from Agno run result (RunOutput or similar). Tries content, then messages."""
    if response is None:
        return ""
    # Prefer .content
    if hasattr(response, "content") and response.content is not None:
        raw = str(response.content).strip()
        if raw:
            return raw
    # Fallback: last assistant message in .messages
    messages = getattr(response, "messages", None)
    if messages and isinstance(messages, (list, tuple)) and len(messages) > 0:
        for msg in reversed(messages):
            if not hasattr(msg, "content"):
                continue
            role = getattr(msg, "role", None) or getattr(msg, "type", "")
            if str(role).lower() != "assistant":
                continue
            raw = str(msg.content or "").strip()
            if raw:
                return raw
    # Fallback: .text or string repr (avoid object repr)
    raw = str(getattr(response, "text", None) or "").strip()
    if raw:
        return raw
    raw = str(response).strip()
    if raw and not raw.startswith("<") and "object at 0x" not in raw:
        return raw
    return ""


def _is_run_content_event(event: Any) -> bool:
    if event is None:
        return False
    if RunEvent is not None and hasattr(RunEvent, "run_content"):
        if event == RunEvent.run_content:
            return True
    ev = str(getattr(event, "event", event)).lower()
    return ev in ("run_content", "runcontent", "run_response_content")


def _is_tool_event(event: Any) -> bool:
    if event is None:
        return False
    ev = str(getattr(event, "event", event)).lower()
    return "tool" in ev


def run_agent(
    agent: Any,
    message: str,
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    stream: bool = False,
    on_content: Callable[[str], None] | None = None,
    on_tool_use: Any = None,
    stop_requested: Callable[[], bool] | None = None,
) -> str:
    """Run the agent and return the response content.
    When stream=True, iterates events and calls on_content(delta) for each content delta;
    optionally reports tool use via on_tool_use(name, summary). If stop_requested() returns True
    during streaming, breaks and returns partial accumulated string. Returns accumulated full string.
    Pass user_id so Agno Learning (user profile + memory) can scope and inject context.
    """
    if agent is None:
        return ""
    try:
        run_kwargs: dict[str, Any] = {"input": message.strip(), "stream": stream}
        if session_id:
            run_kwargs["session_id"] = session_id
        if user_id:
            run_kwargs["user_id"] = user_id
        if stream:
            run_kwargs["stream_events"] = True
        response = agent.run(**run_kwargs)

        if stream:
            accumulated: list[str] = []
            try:
                for chunk in response:
                    ev = getattr(chunk, "event", None)
                    if _is_run_content_event(ev):
                        delta = getattr(chunk, "content", None) or ""
                        if isinstance(delta, str) and delta:
                            accumulated.append(delta)
                            if on_content:
                                try:
                                    on_content(delta)
                                except Exception:
                                    pass
                    elif _is_tool_event(ev) and on_tool_use:
                        tool_name = getattr(chunk, "tool", None)
                        name = (
                            getattr(tool_name, "tool_name", None)
                            or getattr(tool_name, "name", None)
                            or str(tool_name or "")
                        )
                        if name:
                            summary = getattr(chunk, "result", None) or getattr(chunk, "summary", None) or ""
                            try:
                                on_tool_use(name, str(summary)[:200] if summary else "")
                            except Exception:
                                pass
                    if stop_requested and stop_requested():
                        break
            except StopIteration:
                pass
            return "".join(accumulated)

        content = _content_from_response(response)
        if not content:
            logging.getLogger("app").warning(
                "agno run_agent: empty content from response type=%s attrs=%s",
                type(response).__name__,
                [a for a in dir(response) if not a.startswith("_")],
            )
        if on_tool_use and getattr(response, "tool_calls", None):
            for tc in response.tool_calls:
                name = getattr(tc, "name", None) or getattr(tc, "tool_name", None) or str(getattr(tc, "tool", tc))
                if isinstance(name, str):
                    summary = getattr(tc, "result", None) or getattr(tc, "summary", None) or ""
                    try:
                        on_tool_use(name, str(summary)[:200] if summary else "")
                    except Exception:
                        pass
        return content
    except Exception as exc:
        logging.getLogger("app").warning("agno run_agent failed: %s", exc, exc_info=True)
        raise
