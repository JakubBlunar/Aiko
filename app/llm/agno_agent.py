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
You are in a voice conversation. Your replies will be read aloud by text-to-speech, so write for listening: use short, clear sentences and natural, conversational phrasing. Avoid long bullet lists, code blocks, or markdown that sounds awkward when spoken; if the user needs detail, give a brief spoken summary.

For greetings (e.g. "Hello!", "Hi"), casual chat, or when the user has already asked a complete question, always reply directly in character—do not invoke any tools or ask for user input. Only use tools when you need to search, calculate, look something up, or perform a specific action the user requested. Do not use emojis or special characters.

At the **start** of every reply, on the first line, write exactly one reaction tag: [[reaction:neutral]] then a blank line, then your reply. Use one of: neutral, cheerful, excited, surprised, sad, angry, calm, serious, friendly, gentle, enthusiastic. Do not repeat the tag at the end of your reply.

When your reply would be long (e.g. after reading a file, listing code, or giving step-by-step details), use two-tier format so the user hears a brief summary and can read the rest in the chat: put a short spoken summary (1–3 sentences, natural for listening) inside [[spoken]]...[[/spoken]], and put longer content (code, long lists, excerpts) in [[detail]]...[[/detail]]. If you do not use these tags, the entire reply is read aloud. You may use markdown in your replies; use it especially in [[detail]] for code (fenced code blocks with language, e.g. ```python), lists, and structure. The chat UI will render it with formatting and code highlighting.
"""


def _default_storage_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "agno_sessions.db"


# Registry: toolkit_id -> (factory(params: dict | None) -> Any, pip_packages: list[str], env_vars: list[str])
_ToolkitDescriptor = tuple[
    Callable[[dict[str, Any] | None], Any],
    list[str],
    list[str],
]


def _make_calculator_factory() -> Callable[[dict[str, Any] | None], Any]:
    def factory(params: dict[str, Any] | None) -> Any:
        from agno.tools.calculator import CalculatorTools
        if params:
            return CalculatorTools(**params)
        return CalculatorTools()
    return factory


def _make_duckduckgo_factory() -> Callable[[dict[str, Any] | None], Any]:
    def factory(params: dict[str, Any] | None) -> Any:
        from agno.tools.duckduckgo import DuckDuckGoTools
        if params:
            return DuckDuckGoTools(**params)
        return DuckDuckGoTools()
    return factory


def _make_wikipedia_factory() -> Callable[[dict[str, Any] | None], Any]:
    def factory(params: dict[str, Any] | None) -> Any:
        from agno.tools.wikipedia import WikipediaTools
        if params:
            return WikipediaTools(**params)
        return WikipediaTools()
    return factory


def _make_arxiv_factory() -> Callable[[dict[str, Any] | None], Any]:
    def factory(params: dict[str, Any] | None) -> Any:
        from agno.tools.arxiv import ArxivTools
        if params:
            return ArxivTools(**params)
        return ArxivTools()
    return factory


def _make_googlesearch_factory() -> Callable[[dict[str, Any] | None], Any]:
    def factory(params: dict[str, Any] | None) -> Any:
        from agno.tools.googlesearch import GoogleSearchTools
        if params:
            return GoogleSearchTools(**params)
        return GoogleSearchTools()
    return factory


def _make_youtube_factory() -> Callable[[dict[str, Any] | None], Any]:
    def factory(params: dict[str, Any] | None) -> Any:
        from agno.tools.youtube import YouTubeTools
        if params:
            return YouTubeTools(**params)
        return YouTubeTools()
    return factory


def _make_openweather_factory() -> Callable[[dict[str, Any] | None], Any]:
    def factory(params: dict[str, Any] | None) -> Any:
        from agno.tools.openweather import OpenWeatherTools
        if params:
            return OpenWeatherTools(**params)
        return OpenWeatherTools()
    return factory


def _make_yfinance_factory() -> Callable[[dict[str, Any] | None], Any]:
    def factory(params: dict[str, Any] | None) -> Any:
        from agno.tools.yfinance import YFinanceTools
        if params:
            return YFinanceTools(**params)
        return YFinanceTools()
    return factory


def _make_todoist_factory() -> Callable[[dict[str, Any] | None], Any]:
    def factory(params: dict[str, Any] | None) -> Any:
        from agno.tools.todoist import TodoistTools
        if params:
            return TodoistTools(**params)
        return TodoistTools()
    return factory


def _make_user_control_flow_factory() -> Callable[[dict[str, Any] | None], Any]:
    def factory(params: dict[str, Any] | None) -> Any:
        from agno.tools.user_control_flow import UserControlFlowTools
        if params:
            return UserControlFlowTools(**params)
        return UserControlFlowTools()
    return factory


TOOLKIT_REGISTRY: dict[str, _ToolkitDescriptor] = {
    "calculator": (_make_calculator_factory(), [], []),
    "duckduckgo": (_make_duckduckgo_factory(), ["ddgs"], []),
    "wikipedia": (_make_wikipedia_factory(), [], []),
    "arxiv": (_make_arxiv_factory(), [], []),
    "googlesearch": (_make_googlesearch_factory(), [], []),
    "youtube": (_make_youtube_factory(), ["youtube_transcript_api"], []),
    "openweather": (_make_openweather_factory(), ["requests"], ["OPENWEATHER_API_KEY"]),
    "yfinance": (_make_yfinance_factory(), ["yfinance"], []),
    "todoist": (_make_todoist_factory(), [], ["TODOIST_API_KEY"]),
    "user_control_flow": (_make_user_control_flow_factory(), [], []),
}


def _build_tools(toolkit_entries: list[tuple[str, dict[str, Any]]] | None = None) -> list[Any]:
    """Build Agno toolkit instances from config-driven entries. Each entry is (toolkit_id, params_dict)."""
    if not toolkit_entries:
        toolkit_entries = [
            ("calculator", {}),
            ("duckduckgo", {}),
            ("wikipedia", {}),
            ("arxiv", {}),
        ]
    tools: list[Any] = []
    failed: list[tuple[str, str]] = []  # (id, reason)
    log = logging.getLogger("app.llm.agno_agent")
    for toolkit_id, params in toolkit_entries:
        if not toolkit_id or toolkit_id not in TOOLKIT_REGISTRY:
            continue
        factory, pip_packages, env_vars = TOOLKIT_REGISTRY[toolkit_id]
        try:
            instance = factory(params if params else None)
            tools.append(instance)
        except ImportError as e:
            pip_hint = f"pip install {' '.join(pip_packages)}" if pip_packages else "install required deps"
            env_hint = f"; set {', '.join(env_vars)}" if env_vars else ""
            failed.append((toolkit_id, f"ImportError: {pip_hint}{env_hint}"))
            log.warning("To enable toolkit %r, install: %s%s", toolkit_id, pip_hint, env_hint)
        except (TypeError, ValueError) as e:
            failed.append((toolkit_id, f"invalid params: {e}"))
            log.warning("Toolkit %r failed (params): %s", toolkit_id, e)
    if failed:
        loaded_ids = [tid for tid, _ in toolkit_entries if tid in TOOLKIT_REGISTRY and tid not in {f[0] for f in failed}]
        log.info("Agno toolkits loaded: %s; failed: %s", loaded_ids, failed)
    return tools


def _build_mcp_tools() -> list[Any]:
    """Windows-MCP via Agno MCPTools (stdio). Call connect_agent_mcp() after create_agent to connect."""
    try:
        from agno.tools.mcp import MCPTools
        return [MCPTools(command="uvx windows-mcp")]
    except ImportError:
        return []


def _build_coding_mcp_tools(
    allowed_roots: list[str],
    allowed_files: list[str] | None = None,
) -> list[Any]:
    """Coding MCP server (stdio) with path guardrails. Requires app.mcp_coding_server runnable."""
    if not allowed_roots:
        return []
    try:
        import sys
        from agno.tools.mcp import MCPTools
        roots_str = "|".join(allowed_roots)
        env = {"ALLOWED_ROOTS": roots_str}
        if allowed_files:
            env["ALLOWED_FILES"] = "|".join(allowed_files)
        cmd = [sys.executable, "-m", "app.mcp_coding_server"]
        return [
            MCPTools(
                command=" ".join(cmd),
                env=env,
                tool_name_prefix="coding_",
            )
        ]
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
    agno_toolkit_entries: list[tuple[str, dict[str, Any]]] | None = None,
    num_history_runs: int | None = None,
    compress_tool_results: bool = True,
    compress_tool_results_limit: int | None = None,
    compress_token_limit: int | None = None,
    coding_enabled: bool = False,
    coding_allowed_roots: list[str] | None = None,
    coding_allowed_files: list[str] | None = None,
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

    history_runs = num_history_runs if num_history_runs is not None else 10
    provider = (database_provider or "sqlite").strip().lower()
    if provider == "postgres" and database_url:
        try:
            from agno.db.postgres import PostgresDb
            db = PostgresDb(db_url=database_url)
            kwargs["db"] = db
            kwargs["add_history_to_messages"] = True
            kwargs["num_history_runs"] = history_runs
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
                kwargs["num_history_runs"] = history_runs
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
                kwargs["num_history_runs"] = history_runs
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

    if compress_tool_results:
        kwargs["compress_tool_results"] = True
        if compress_tool_results_limit is not None or compress_token_limit is not None:
            try:
                from agno.compression.manager import CompressionManager
                kwargs["compression_manager"] = CompressionManager(
                    compress_tool_results=True,
                    compress_tool_results_limit=compress_tool_results_limit,
                    compress_token_limit=compress_token_limit,
                )
            except ImportError:
                pass

    tools_list: list[Any] = []
    if add_tools:
        tools_list.extend(_build_tools(agno_toolkit_entries))
    if add_mcp:
        tools_list.extend(_build_mcp_tools())
    if coding_enabled and coding_allowed_roots:
        tools_list.extend(
            _build_coding_mcp_tools(
                list(coding_allowed_roots),
                coding_allowed_files or None,
            )
        )
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
    # Dict-like (e.g. Ollama-style or wrapped)
    if isinstance(response, dict):
        raw = str(response.get("content") or response.get("text") or "").strip()
        if raw:
            return raw
        msg = response.get("message")
        if isinstance(msg, dict):
            raw = str(msg.get("content") or msg.get("text") or "").strip()
            if raw:
                return raw
        for m in reversed(response.get("messages") or []):
            if not isinstance(m, dict):
                continue
            if str(m.get("role") or m.get("type") or "").lower() != "assistant":
                continue
            raw = str(m.get("content") or m.get("text") or "").strip()
            if raw:
                return raw
    # Fallback: last assistant message in .messages (object form)
    messages = getattr(response, "messages", None)
    if messages and isinstance(messages, (list, tuple)) and len(messages) > 0:
        for msg in reversed(messages):
            if hasattr(msg, "content"):
                raw = str(msg.content or "").strip()
            elif isinstance(msg, dict):
                raw = str(msg.get("content") or msg.get("text") or "").strip()
            else:
                continue
            if not raw:
                continue
            role = getattr(msg, "role", None) or getattr(msg, "type", None) if not isinstance(msg, dict) else msg.get("role") or msg.get("type")
            if str(role or "").lower() != "assistant":
                continue
            return raw
    # Fallback: .output, .response, .text
    for attr in ("output", "response", "text"):
        raw = str(getattr(response, attr, None) or "").strip()
        if raw and "object at 0x" not in raw:
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
    if ev in ("run_content", "runcontent", "run_response_content"):
        return True
    if "content" in ev and "run" in ev:
        return True
    return False


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
            # Log enough to debug: type, public attrs, and short repr of content if present
            attrs = [a for a in dir(response) if not a.startswith("_")]
            content_preview = ""
            if hasattr(response, "content") and response.content is not None:
                content_preview = str(response.content)[:300]
            logging.getLogger("app").warning(
                "agno run_agent: empty content from response type=%s attrs=%s content_preview=%s",
                type(response).__name__,
                attrs,
                content_preview or "(none)",
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
