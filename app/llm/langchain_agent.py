"""LangChain-based agent with Ollama, optional storage and tools."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase
    from app.llm.embedding_service import EmbeddingService

# Voice-friendly instructions: replies are spoken via TTS.
_BASE_INSTRUCTIONS = """\
You are in a voice conversation. Your replies will be read aloud by text-to-speech, so write for listening: use short, clear sentences and natural, conversational phrasing. Avoid long bullet lists, code blocks, or markdown that sounds awkward when spoken; if the user needs detail, give a brief spoken summary.

For greetings (e.g. "Hello!", "Hi"), casual chat, or when the user has already asked a complete question, always reply directly in character—do not invoke any tools or ask for user input. Only use tools when you need to search, calculate, look something up, or perform a specific action the user requested. Do not use emojis or special characters.

At the **start** of every reply, on the first line, write exactly one reaction tag: [[reaction:neutral]] then a blank line, then your reply. Use one of: neutral, cheerful, excited, surprised, sad, angry, calm, serious, friendly, gentle, enthusiastic. Do not repeat the tag at the end of your reply.

When your reply would be long (e.g. after reading a file, listing code, or giving step-by-step details), use two-tier format so the user hears a brief summary and can read the rest in the chat: put a short spoken summary (1–3 sentences, natural for listening) inside [[spoken]]...[[/spoken]], and put longer content (code, long lists, excerpts) in [[detail]]...[[/detail]]. If you do not use these tags, the entire reply is read aloud. You may use markdown in your replies; use it especially in [[detail]] for code (fenced code blocks with language, e.g. ```python), lists, and structure. The chat UI will render it with formatting and code highlighting.
"""


def _default_storage_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "chat_sessions.db"


def detect_context_window(base_url: str, model: str) -> int:
    """Query Ollama /api/show to read the model's context window size."""
    import requests
    try:
        resp = requests.post(
            f"{base_url.rstrip('/')}/api/show",
            json={"name": model},
            timeout=5,
        )
        resp.raise_for_status()
        info = resp.json()
        model_info = info.get("model_info", {})
        for key, val in model_info.items():
            if "context_length" in key:
                return int(val)
        params = info.get("parameters", "")
        for line in params.splitlines():
            if "num_ctx" in line:
                return int(line.split()[-1])
    except Exception:
        pass
    return 8192


def _get_langchain_tools(
    toolkit_entries: list[tuple[str, dict[str, Any]]] | None,
    mcp_tools: list[Any],
) -> list[Any]:
    """Build list of LangChain tools from registry entries and MCP. Registry has no built-in toolkits yet."""
    tools: list[Any] = []
    # Tool registry: no factories registered by default; toolkit_entries are ignored for now.
    if toolkit_entries:
        log = logging.getLogger("app.llm.langchain_agent")
        for tid, _ in toolkit_entries:
            log.debug("Toolkit %r has no LangChain factory yet; skipping.", tid)
    tools.extend(mcp_tools)
    return tools


def _load_mcp_tools(
    root: Path,
    mcp_config: dict[str, Any],
) -> list[Any]:
    """Load MCP tools from config (servers_json_path, servers_user_json_path)."""
    tools: list[Any] = []
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        logging.getLogger("app.llm.langchain_agent").warning(
            "langchain-mcp-adapters not installed; MCP tools disabled."
        )
        return tools

    servers: dict[str, dict[str, Any]] = {}
    for key in ("servers_json_path", "servers_user_json_path"):
        path_raw = str(mcp_config.get(key, "")).strip()
        if not path_raw:
            continue
        path = Path(path_raw) if Path(path_raw).is_absolute() else root / path_raw
        if not path.exists():
            continue
        try:
            payload = __import__("json").loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw = payload.get("mcpServers") if isinstance(payload, dict) else None
        if isinstance(raw, dict):
            for name, cfg in raw.items():
                if isinstance(cfg, dict):
                    servers[name] = dict(cfg)

    if not servers:
        return tools

    try:
        import asyncio

        connections: dict[str, dict[str, Any]] = {}
        for name, cfg in servers.items():
            transport = str(cfg.get("transport", "stdio")).strip().lower()
            if transport == "stdio":
                cmd = cfg.get("command")
                args = cfg.get("args") or []
                if not cmd:
                    continue
                args_list = [str(a) for a in args] if isinstance(args, list) else []
                env = cfg.get("env")
                if not isinstance(env, dict):
                    env = None
                conn: dict[str, Any] = {
                    "transport": "stdio",
                    "command": str(cmd),
                    "args": args_list,
                }
                if env:
                    conn["env"] = env
                connections[name] = conn
        if connections:
            client = MultiServerMCPClient(connections)
            tools = asyncio.run(client.get_tools())
    except Exception as e:
        logging.getLogger("app.llm.langchain_agent").warning("Failed to load MCP tools: %s", e)
    return tools


def _add_history_search_tool(
    tools: list[Any],
    embedding_service: "EmbeddingService",
    chat_db: "ChatDatabase",
) -> list[Any]:
    """Register a search_history LangChain tool the LLM can invoke."""
    try:
        from langchain_core.tools import tool
    except ImportError:
        return tools

    @tool
    def search_history(query: str) -> str:
        """Search past conversation history for relevant messages. Use when the user
        asks about something discussed earlier or references past context."""
        results = embedding_service.search(query, top_k=5)
        if not results:
            return "No relevant past messages found."
        parts: list[str] = []
        for r in results:
            preview = r.content[:500] + ("..." if len(r.content) > 500 else "")
            parts.append(f"[{r.role}] ({r.created_at}): {preview}")
        return "\n\n".join(parts)

    return tools + [search_history]


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
    toolkit_entries: list[tuple[str, dict[str, Any]]] | None = None,
    num_history_runs: int | None = None,
    compress_tool_results: bool = True,
    compress_tool_results_limit: int | None = None,
    compress_token_limit: int | None = None,
    mcp_config: dict[str, Any] | None = None,
    project_root: Path | None = None,
    context_window: int | None = None,
    chat_db: "ChatDatabase | None" = None,
    embedding_service: "EmbeddingService | None" = None,
) -> Any:
    """Create a LangChain agent with Ollama, storage, optional tools and MCP. Create once and reuse."""
    try:
        from langchain_ollama import ChatOllama
    except ImportError:
        try:
            from langchain_community.chat_models.ollama import ChatOllama
        except ImportError:
            try:
                from langchain_community.chat_models import ChatOllama
            except ImportError:
                raise RuntimeError(
                    "langchain-ollama (or langchain-community) not installed; "
                    "pip install langchain-ollama"
                ) from None

    storage_path = storage_path or _default_storage_path()
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    root = project_root or Path(__file__).resolve().parents[2]

    resolved_ctx = context_window or detect_context_window(base_url, chat_model)
    log = logging.getLogger("app.llm.langchain_agent")
    log.info("Context window for %s: %d tokens", chat_model, resolved_ctx)

    llm = ChatOllama(
        model=chat_model,
        base_url=base_url,
        temperature=temperature,
        num_ctx=resolved_ctx,
    )
    instr = (instructions or _BASE_INSTRUCTIONS).strip()
    if (assistant_background or "").strip():
        instr = f"{instr}\n\nAssistant background: {assistant_background.strip()}"

    mcp_tools: list[Any] = []
    if add_mcp and mcp_config:
        mcp_tools = _load_mcp_tools(root, mcp_config)

    tools_list: list[Any] = []
    if add_tools:
        tools_list = _get_langchain_tools(toolkit_entries, mcp_tools)

    if embedding_service and chat_db:
        tools_list = _add_history_search_tool(tools_list, embedding_service, chat_db)

    num_history = num_history_runs if num_history_runs is not None else 10

    use_react = False
    try:
        from langgraph.prebuilt import create_react_agent
        use_react = True
    except ImportError:
        try:
            from langchain.agents import create_react_agent
            use_react = True
        except ImportError:
            create_react_agent = None

    agent = llm
    if create_react_agent is not None and tools_list:
        try:
            agent = create_react_agent(llm, tools_list, prompt=instr)
        except TypeError:
            agent = create_react_agent(llm, tools_list)
    elif tools_list:
        agent = llm.bind_tools(tools_list)

    return _AgentWrapper(
        agent=agent,
        llm=llm,
        tools=tools_list,
        system_message=instr,
        storage_path=storage_path,
        database_provider=database_provider,
        database_url=database_url,
        num_history_runs=num_history,
        use_react_agent=use_react and bool(tools_list),
        context_window=resolved_ctx,
        compress_tool_results=compress_tool_results,
        compress_tool_results_limit=compress_tool_results_limit,
        chat_db=chat_db,
        embedding_service=embedding_service,
    )


class _AgentWrapper:
    """Wraps LangChain/LangGraph agent with session history, token-aware trimming, and run() API."""

    _RESPONSE_TOKEN_RESERVE = 1024
    _TOOL_RESULT_MAX_CHARS = 8000
    _TOOL_RESULT_HEAD = 3000
    _TOOL_RESULT_TAIL = 500

    def __init__(
        self,
        agent: Any,
        llm: Any,
        tools: list[Any],
        system_message: str,
        storage_path: Path,
        database_provider: str,
        database_url: str | None,
        num_history_runs: int,
        use_react_agent: bool = False,
        context_window: int = 8192,
        compress_tool_results: bool = True,
        compress_tool_results_limit: int | None = None,
        chat_db: "ChatDatabase | None" = None,
        embedding_service: "EmbeddingService | None" = None,
    ) -> None:
        self._agent = agent
        self._llm = llm
        self._tools = tools
        self._system_message = system_message
        self._storage_path = storage_path
        self._database_provider = database_provider
        self._database_url = database_url
        self._num_history_runs = num_history_runs
        self._has_react_agent = use_react_agent and hasattr(agent, "invoke")
        self._context_window = context_window
        self._compress_tool_results = compress_tool_results
        self._compress_tool_results_limit = compress_tool_results_limit
        self._chat_db = chat_db
        self._embedding_service = embedding_service
        self._log = logging.getLogger("app.llm.langchain_agent")

    def run(
        self,
        input: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        stream: bool = False,
        stream_events: bool = False,
    ) -> Any:
        """Run the agent. Returns response object or stream iterator."""
        from langchain_core.messages import HumanMessage

        session_key = session_id or "main"
        if user_id:
            session_key = f"{user_id}:{session_key}"

        messages = self._get_history_messages(session_key)
        messages.append(HumanMessage(content=input.strip()))

        if stream or stream_events:
            return self._stream_run(session_key, messages, stream_events)
        return self._invoke_run(session_key, messages)

    def save_turn(self, session_id: str, user_id: str | None, user_text: str, ai_text: str) -> None:
        """Persist a user+AI turn after streaming (which doesn't auto-save)."""
        session_key = session_id or "main"
        if user_id:
            session_key = f"{user_id}:{session_key}"
        self._persist_turn(session_key, user_text, ai_text)

    def _get_history_messages(self, session_key: str) -> list[Any]:
        from langchain_core.messages import HumanMessage, AIMessage
        from app.llm.token_utils import estimate_tokens, estimate_messages_tokens

        if self._chat_db:
            max_rows = (self._num_history_runs * 2) if self._num_history_runs else 40
            rows = self._chat_db.get_messages(session_key, limit=max_rows)
            lang_messages: list[Any] = []
            for row in rows:
                if row.role == "user":
                    lang_messages.append(HumanMessage(content=row.content))
                elif row.role == "assistant":
                    content = self._compress_content(row.content) if self._compress_tool_results else row.content
                    lang_messages.append(AIMessage(content=content))
        else:
            history = self._get_chat_history_legacy(session_key)
            lang_messages = []
            for msg in history:
                if hasattr(msg, "type"):
                    if msg.type == "human":
                        lang_messages.append(HumanMessage(content=msg.content))
                    elif msg.type == "ai":
                        content = self._compress_content(msg.content) if self._compress_tool_results else msg.content
                        lang_messages.append(AIMessage(content=content))
            max_messages = (self._num_history_runs * 2) if self._num_history_runs else 40
            lang_messages = lang_messages[-max_messages:]

        budget = (
            self._context_window
            - estimate_tokens(self._system_message)
            - self._RESPONSE_TOKEN_RESERVE
        )
        summary = self._load_summary(session_key)
        if summary:
            budget -= estimate_tokens(summary)

        while lang_messages and estimate_messages_tokens(lang_messages) > budget:
            evicted = lang_messages.pop(0)
            self._handle_evicted_message(session_key, evicted)

        return lang_messages

    def _compress_content(self, content: str) -> str:
        if len(content) <= self._TOOL_RESULT_MAX_CHARS:
            return content
        head = content[: self._TOOL_RESULT_HEAD]
        tail = content[-self._TOOL_RESULT_TAIL :]
        return f"{head}\n\n... [truncated, {len(content)} chars total] ...\n\n{tail}"

    def _handle_evicted_message(self, session_key: str, message: Any) -> None:
        """Accumulate evicted messages for periodic summarisation."""
        pass

    def _load_summary(self, session_key: str) -> str:
        if not self._chat_db:
            return ""
        row = self._chat_db.get_latest_summary(session_key)
        return row.summary if row else ""

    def _build_system_message(self, session_key: str) -> str:
        summary = self._load_summary(session_key)
        if summary:
            return f"{self._system_message}\n\nPrior conversation summary: {summary}"
        return self._system_message

    def _get_chat_history_legacy(self, session_key: str) -> list[Any]:
        try:
            from langchain_community.chat_message_histories import SQLChatMessageHistory
        except ImportError:
            return []
        conn_str = "sqlite:///" + str(self._storage_path).replace("\\", "/")
        if self._database_provider == "postgres" and self._database_url:
            conn_str = self._database_url
        try:
            history = SQLChatMessageHistory(
                session_id=session_key,
                connection_string=conn_str,
                table_name="message_store",
            )
            return list(history.messages)
        except Exception:
            return []

    def _persist_turn(self, session_key: str, user_text: str, ai_text: str) -> None:
        """Save a user+AI message pair to the chat database and compute embeddings."""
        from app.llm.token_utils import estimate_tokens

        if self._chat_db:
            user_id = self._chat_db.add_message(
                session_key, "user", user_text, estimate_tokens(user_text),
            )
            ai_id = self._chat_db.add_message(
                session_key, "assistant", ai_text, estimate_tokens(ai_text),
            )
            if self._embedding_service:
                try:
                    self._embedding_service.embed_and_store(user_id, session_key, user_text)
                    self._embedding_service.embed_and_store(ai_id, session_key, ai_text)
                except Exception as exc:
                    self._log.debug("Embedding failed for turn: %s", exc)
        else:
            self._save_messages_legacy(session_key, user_text, ai_text)

    def _save_messages_legacy(self, session_key: str, user_text: str, ai_text: str) -> None:
        try:
            from langchain_community.chat_message_histories import SQLChatMessageHistory
        except ImportError:
            return
        conn_str = "sqlite:///" + str(self._storage_path).replace("\\", "/")
        if self._database_provider == "postgres" and self._database_url:
            conn_str = self._database_url
        try:
            history = SQLChatMessageHistory(
                session_id=session_key,
                connection_string=conn_str,
                table_name="message_store",
            )
            history.add_user_message(user_text)
            history.add_ai_message(ai_text)
        except Exception:
            pass

    def summarize_if_needed(self, session_id: str, user_id: str | None = None) -> None:
        """Generate a rolling summary if the conversation has grown significantly since the last one."""
        if not self._chat_db:
            return
        session_key = session_id or "main"
        if user_id:
            session_key = f"{user_id}:{session_key}"

        existing = self._chat_db.get_latest_summary(session_key)
        total = self._chat_db.get_message_count(session_key)
        already_summarized = existing.messages_summarized if existing else 0
        unsummarized = total - already_summarized

        if unsummarized < 10:
            return

        from app.llm.token_utils import estimate_tokens
        rows = self._chat_db.get_messages(session_key)
        old_rows = rows[:already_summarized + unsummarized - 6]
        if not old_rows:
            return

        text_parts: list[str] = []
        if existing:
            text_parts.append(f"Previous summary: {existing.summary}")
        for r in old_rows[-20:]:
            text_parts.append(f"{r.role}: {r.content[:300]}")
        context = "\n".join(text_parts)

        prompt = (
            "Summarize this conversation history in 2-3 concise sentences. "
            "Preserve key facts, decisions, user preferences, and important details. "
            "Be specific -- include names, tools, technical choices, and outcomes.\n\n"
            f"{context}"
        )
        try:
            from langchain_core.messages import HumanMessage
            result = self._llm.invoke([HumanMessage(content=prompt)])
            summary_text = (getattr(result, "content", "") or "").strip()
            if summary_text:
                self._chat_db.save_summary(
                    session_key,
                    summary_text,
                    estimate_tokens(summary_text),
                    total - 6,
                )
                self._log.info("Generated conversation summary (%d messages summarized)", total - 6)
        except Exception as exc:
            self._log.warning("Summary generation failed: %s", exc)

    def _invoke_run(self, session_key: str, messages: list[Any]) -> Any:
        from langchain_core.messages import SystemMessage, AIMessage

        sys_content = self._build_system_message(session_key)
        full: list[Any] = [SystemMessage(content=sys_content)] + messages
        if self._has_react_agent and self._tools:
            result = self._agent.invoke({"messages": full})
            out_messages = result.get("messages", result) if isinstance(result, dict) else getattr(result, "messages", [])
        else:
            out_messages = self._llm.invoke(full)
            if not isinstance(out_messages, list):
                out_messages = [out_messages]

        for m in reversed(out_messages):
            if hasattr(m, "content") and getattr(m, "type", None) == "ai":
                user_text = messages[-1].content if messages else ""
                self._persist_turn(session_key, user_text, m.content or "")
                break
        return _RunOutput(messages=out_messages)

    def _stream_run(self, session_key: str, messages: list[Any], stream_events: bool) -> Any:
        from langchain_core.messages import SystemMessage

        sys_content = self._build_system_message(session_key)
        full: list[Any] = [SystemMessage(content=sys_content)] + messages
        if stream_events and self._has_react_agent and self._tools:
            return self._agent.stream_events({"messages": full}, version="v2")
        return self._llm.stream(full)


class _RunOutput:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.content = ""
        for m in reversed(messages):
            if hasattr(m, "content") and getattr(m, "type", None) == "ai":
                self.content = (m.content or "").strip()
                break


def _content_from_response(response: Any) -> str:
    if response is None:
        return ""
    if hasattr(response, "content") and response.content is not None:
        return str(response.content).strip()
    if isinstance(response, dict):
        return str(response.get("content") or response.get("text") or "").strip()
    return ""


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
    """Run the agent and return the response content. Supports stream and callbacks."""
    if agent is None:
        return ""
    try:
        if stream:
            accumulated: list[str] = []
            run_result = agent.run(
                message.strip(),
                session_id=session_id,
                user_id=user_id,
                stream=True,
                stream_events=True,
            )
            try:
                for event in run_result:
                    if stop_requested and stop_requested():
                        break
                    kind = event.get("event") if isinstance(event, dict) else getattr(event, "event", None)
                    if kind in ("on_chat_model_stream", "on_llm_stream", "on_llm_new_token"):
                        chunk = event.get("data", {}).get("chunk") if isinstance(event, dict) else getattr(event, "data", None)
                        if chunk and hasattr(chunk, "content") and chunk.content:
                            delta = str(chunk.content)
                            accumulated.append(delta)
                            if on_content:
                                try:
                                    on_content(delta)
                                except Exception:
                                    pass
                    if kind and "tool" in str(kind).lower() and on_tool_use:
                        name = event.get("name") if isinstance(event, dict) else getattr(event, "name", None)
                        if name:
                            try:
                                on_tool_use(name, "")
                            except Exception:
                                pass
            except Exception:
                pass
            ai_text = "".join(accumulated)
            if ai_text and hasattr(agent, "save_turn"):
                try:
                    agent.save_turn(session_id, user_id, message.strip(), ai_text)
                except Exception:
                    pass
            return ai_text

        response = agent.run(
            message.strip(),
            session_id=session_id,
            user_id=user_id,
            stream=False,
        )
        content = _content_from_response(response)
        if not content and hasattr(response, "content"):
            content = str(getattr(response, "content", "") or "").strip()
        if not content and hasattr(response, "messages"):
            for m in reversed(getattr(response, "messages", [])):
                if getattr(m, "type", None) == "ai" and hasattr(m, "content"):
                    content = str(m.content or "").strip()
                    break
        return content or ""
    except Exception as exc:
        logging.getLogger("app").warning("langchain run_agent failed: %s", exc, exc_info=True)
        raise
