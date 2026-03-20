"""LangChain-based agent with Ollama, optional storage and tools."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from app.llm.prompt_builder import VOICE_INSTRUCTIONS

if TYPE_CHECKING:
    from app.core.chat_database import ChatDatabase
    from app.llm.embedding_service import EmbeddingService


def _extract_text(content: Any) -> str:
    """Extract plain text from an AI message content field.

    Handles both string content and list-of-blocks format
    (e.g. [{"type": "text", "text": "..."}]).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text", "")))
        return " ".join(p for p in parts if p)
    return str(content) if content else ""


_BASE_INSTRUCTIONS = VOICE_INSTRUCTIONS


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


class _McpManager:
    """Keeps MCP server sessions alive on a background event loop.

    Persistent sessions are created for each server so the Playwright
    browser process (and its state) survives across multiple tool calls.
    """

    def __init__(self, connections: dict[str, dict[str, Any]]) -> None:
        import asyncio
        import concurrent.futures
        import threading

        self._loop = asyncio.new_event_loop()
        self._tools: list[Any] = []
        self._stop_event: asyncio.Future | None = None  # type: ignore[type-arg]
        ready: concurrent.futures.Future[list[Any]] = concurrent.futures.Future()

        def _run_loop() -> None:
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._serve(connections, ready))
            except (Exception, BaseExceptionGroup):
                logging.getLogger("app.llm.langchain_agent").exception(
                    "MCP event loop failed — tools from this MCP session may be unavailable"
                )

        self._thread = threading.Thread(target=_run_loop, daemon=True, name="mcp-loop")
        self._thread.start()
        self._tools = ready.result(timeout=60)

    async def _serve(
        self,
        connections: dict[str, dict[str, Any]],
        ready: Any,
    ) -> None:
        import contextlib
        from langchain_mcp_adapters.client import create_session
        from langchain_mcp_adapters.tools import load_mcp_tools

        all_tools: list[Any] = []
        async with contextlib.AsyncExitStack() as stack:
            for name, conn in connections.items():
                session = await stack.enter_async_context(create_session(conn))
                await session.initialize()
                tools = await load_mcp_tools(session, server_name=name)
                all_tools.extend(tools)
            ready.set_result(all_tools)
            self._stop_event = self._loop.create_future()
            await self._stop_event

    @property
    def tools(self) -> list[Any]:
        return list(self._tools)

    def run_coro(self, coro: Any) -> Any:
        """Submit a coroutine to the persistent loop and block for the result."""
        import asyncio

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=120)

    def shutdown(self) -> None:
        if self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set_result, None)
            except Exception:
                pass
        self._thread.join(timeout=5)
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except Exception:
            pass


def _make_sync_tool(tool: Any, manager: _McpManager) -> Any:
    """Wrap an async-only StructuredTool so it runs on the persistent MCP event loop."""
    import inspect

    if getattr(tool, "func", None) is not None:
        return tool
    coro_fn = getattr(tool, "coroutine", None)
    if coro_fn is None or not inspect.iscoroutinefunction(coro_fn):
        return tool

    def _sync_wrapper(**kwargs: Any) -> Any:
        return manager.run_coro(coro_fn(**kwargs))

    tool.func = _sync_wrapper
    if hasattr(tool, "handle_tool_error"):
        tool.handle_tool_error = True
    return tool


def _load_mcp_tools(
    root: Path,
    mcp_config: dict[str, Any],
) -> tuple[list[Any], _McpManager | None]:
    """Load MCP tools from config. Returns (tools, manager)."""
    log = logging.getLogger("app.llm.langchain_agent")
    tools: list[Any] = []
    manager: _McpManager | None = None
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient  # noqa: F401
    except ImportError:
        log.warning("langchain-mcp-adapters not installed; MCP tools disabled.")
        return tools, None

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
        return tools, None

    try:
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
            manager = _McpManager(connections)
            tools = [_make_sync_tool(t, manager) for t in manager.tools]
            log.info("Loaded %d MCP tools: %s", len(tools), [t.name for t in tools])
    except Exception as e:
        log.warning("Failed to load MCP tools: %s", e)
    return tools, manager


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

    _current_session: list[str | None] = [None]

    @tool
    def search_history(query: str) -> str:
        """Search past conversation history for relevant messages. Use when the user
        asks about something discussed earlier or references past context."""
        results = embedding_service.search(
            query, session_id=_current_session[0], top_k=5,
        )
        if not results:
            return "No relevant past messages found."
        parts: list[str] = []
        for r in results:
            preview = r.content[:500] + ("..." if len(r.content) > 500 else "")
            parts.append(f"[{r.role}] ({r.created_at}): {preview}")
        return "\n\n".join(parts)

    search_history._session_ref = _current_session  # type: ignore[attr-defined]
    return tools + [search_history]


def create_agent(
    *,
    chat_model: str = "llama3.1:8b",
    base_url: str = "http://127.0.0.1:11434",
    temperature: float = 0.6,
    timeout: int = 300,
    judge_model: str | None = None,
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
    personality_token_budget: int = 300,
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
        client_kwargs={"timeout": float(timeout)},
        keep_alive="10m",
    )

    judge_llm = None
    if judge_model:
        judge_llm = ChatOllama(
            model=judge_model,
            base_url=base_url,
            temperature=0.0,
            num_ctx=4096,
            format="json",
            client_kwargs={"timeout": 120.0},
            keep_alive="10m",
        )
        log.info("Judge model configured: %s", judge_model)

    instr = (instructions or _BASE_INSTRUCTIONS).strip()
    if (assistant_background or "").strip():
        instr = f"{instr}\n\nAssistant background: {assistant_background.strip()}"

    mcp_tools: list[Any] = []
    mcp_mgr: _McpManager | None = None
    if add_mcp and mcp_config:
        mcp_tools, mcp_mgr = _load_mcp_tools(root, mcp_config)

    tools_list: list[Any] = []
    if add_tools:
        tools_list = _get_langchain_tools(toolkit_entries, mcp_tools)

    from app.llm.builtin_tools import _make_tools
    tools_list.extend(_make_tools(chat_db=chat_db))

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
        agent = create_react_agent(llm, tools_list)
    elif tools_list:
        agent = llm.bind_tools(tools_list)

    return _AgentWrapper(
        agent=agent,
        llm=llm,
        judge_llm=judge_llm,
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
        compress_token_limit=compress_token_limit,
        chat_db=chat_db,
        embedding_service=embedding_service,
        personality_token_budget=personality_token_budget,
        mcp_manager=mcp_mgr,
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
        compress_token_limit: int | None = None,
        chat_db: "ChatDatabase | None" = None,
        embedding_service: "EmbeddingService | None" = None,
        judge_llm: Any = None,
        personality_token_budget: int = 300,
        mcp_manager: _McpManager | None = None,
    ) -> None:
        self._agent = agent
        self._llm = llm
        self._judge_llm = judge_llm
        self._tools = tools
        self._system_message = system_message
        self._storage_path = storage_path
        self._database_provider = database_provider
        self._database_url = database_url
        self._num_history_runs = num_history_runs
        self._has_react_agent = use_react_agent and hasattr(agent, "invoke")
        self._context_window = context_window
        self._compress_tool_results = compress_tool_results
        if compress_tool_results_limit is not None and compress_tool_results_limit > 0:
            self._TOOL_RESULT_MAX_CHARS = compress_tool_results_limit
            self._TOOL_RESULT_HEAD = max(500, int(compress_tool_results_limit * 0.75))
            self._TOOL_RESULT_TAIL = max(200, int(compress_tool_results_limit * 0.1))
        if compress_token_limit is not None and compress_token_limit > 0:
            chars = int(compress_token_limit * 3.5)
            self._TOOL_RESULT_MAX_CHARS = chars
            self._TOOL_RESULT_HEAD = max(500, int(chars * 0.75))
            self._TOOL_RESULT_TAIL = max(200, int(chars * 0.1))
        self._chat_db = chat_db
        self._embedding_service = embedding_service
        self._personality_token_budget = personality_token_budget
        self._mcp_manager = mcp_manager
        self._cached_system_message: str | None = None
        self._persist_enabled: bool = True
        self._last_context_tokens: int = 0
        self._log = logging.getLogger("app.llm.langchain_agent")

    @property
    def persist_enabled(self) -> bool:
        return self._persist_enabled

    @persist_enabled.setter
    def persist_enabled(self, value: bool) -> None:
        self._persist_enabled = bool(value)

    @property
    def mcp_manager(self) -> _McpManager | None:
        return self._mcp_manager

    def shutdown_mcp(self) -> None:
        """Stop the persistent MCP event loop owned by this agent."""
        if self._mcp_manager is not None:
            self._mcp_manager.shutdown()
            self._mcp_manager = None

    def run(
        self,
        input: str,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        stream: bool = False,
        stream_events: bool = False,
        force_plain_llm: bool = False,
    ) -> Any:
        """Run the agent. Returns response object or stream iterator."""
        from langchain_core.messages import HumanMessage

        session_key = session_id or "main"
        if user_id:
            session_key = f"{user_id}:{session_key}"

        for t in self._tools:
            ref = getattr(t, "_session_ref", None)
            if ref is not None:
                ref[0] = session_key

        messages = self._get_history_messages(session_key, user_input=input.strip())
        messages.append(HumanMessage(content=input.strip()))

        if stream or stream_events:
            return self._stream_run(session_key, messages, stream_events, force_plain_llm=force_plain_llm)
        return self._invoke_run(session_key, messages)

    def save_turn(self, session_id: str, user_id: str | None, user_text: str, ai_text: str) -> None:
        """Persist a user+AI turn after streaming (which doesn't auto-save)."""
        session_key = session_id or "main"
        if user_id:
            session_key = f"{user_id}:{session_key}"
        self._persist_turn(session_key, user_text, ai_text)

    def _get_history_messages(self, session_key: str, *, user_input: str = "") -> list[Any]:
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

        sys_content = self._build_system_message(session_key, user_input=user_input)
        self._cached_system_message = sys_content
        budget = (
            self._context_window
            - estimate_tokens(sys_content)
            - self._RESPONSE_TOKEN_RESERVE
        )

        running_tokens = estimate_messages_tokens(lang_messages)
        while lang_messages and running_tokens > budget:
            evicted = lang_messages.pop(0)
            running_tokens -= estimate_tokens(getattr(evicted, "content", "") or "") + 4
            self._handle_evicted_message(session_key, evicted)

        sys_tokens = estimate_tokens(sys_content)
        self._last_context_tokens = sys_tokens + running_tokens + self._RESPONSE_TOKEN_RESERVE

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

    def _build_system_message(self, session_key: str, *, user_input: str = "") -> str:
        parts = [self._system_message]

        summary = self._load_summary(session_key)
        if summary:
            parts.append(f"Prior conversation summary: {summary}")

        if self._chat_db:
            from app.llm.token_utils import estimate_tokens
            budget = getattr(self, "_personality_token_budget", 300)
            notes = self._chat_db.get_personality_notes(session_key, min_confidence=0.4)
            if notes:
                from collections import defaultdict
                grouped: dict[str, list[str]] = defaultdict(list)
                used_tokens = 0
                for n in notes:
                    line = n.note
                    line_tokens = estimate_tokens(line)
                    if used_tokens + line_tokens > budget:
                        break
                    cat = (n.category or "general").replace("_", " ").title()
                    grouped[cat].append(line)
                    used_tokens += line_tokens
                if grouped:
                    section_lines = ["What you know about the user (use naturally, don't list them back):"]
                    for cat, items in grouped.items():
                        section_lines.append(f"  {cat}: {'; '.join(items)}")
                    parts.append("\n".join(section_lines))

            topics = self._chat_db.get_recent_topics(session_key, limit=10)
            if topics:
                topic_list = ", ".join(t.topic for t in topics[:8])
                parts.append(f"Avoid repeating these recent topics (find something fresh): {topic_list}")

        if user_input and self._embedding_service:
            try:
                hits = self._embedding_service.search(
                    user_input, session_id=session_key, top_k=3, max_candidates=300,
                )
                relevant = [h for h in hits if h.score >= 0.7]
                if relevant:
                    ctx_lines = ["Relevant past context (referenced for continuity, do not quote back):"]
                    for h in relevant:
                        preview = h.content[:200]
                        ctx_lines.append(f"  [{h.role}]: {preview}")
                    parts.append("\n".join(ctx_lines))
            except Exception:
                pass

        return "\n\n".join(parts)

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
        if not self._persist_enabled:
            return
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
        end_of_old = already_summarized + unsummarized - 6
        fetch_start = max(0, end_of_old - 20)
        old_rows = self._chat_db.get_messages(
            session_key, offset=fetch_start, limit=end_of_old - fetch_start,
        )
        if not old_rows:
            return

        text_parts: list[str] = []
        if existing:
            text_parts.append(f"Previous summary: {existing.summary}")
        for r in old_rows:
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

    _PERSONALITY_JUDGE_PROMPT = (
        "You are a JSON-only analysis tool. You MUST respond with a single JSON object and nothing else. "
        "No commentary, no markdown, no tags, no preamble — only valid JSON.\n\n"
        "Task: Analyze conversations and extract personality observations about the user.\n\n"
        "You will receive:\n"
        "1. Existing personality notes (may be empty)\n"
        "2. Recent conversation messages\n\n"
        "Each note is an object: "
        '{"category": "<cat>", "note": "<observation>", "confidence": <0.0-1.0>}\n\n'
        "Categories: user_preference, relationship, tone, topic_interest, inside_reference\n\n"
        "Rules:\n"
        "- Keep existing notes that are still accurate (same text, same or higher confidence)\n"
        "- Merge related notes into one (e.g. 'likes rock' + evidence of Dragonforce → 'loves power metal, especially Dragonforce')\n"
        "- Update notes that are contradicted by recent conversation\n"
        "- Add new observations from recent messages\n"
        "- Drop notes that are clearly wrong or irrelevant\n"
        "- Max 40 notes total. Be concise — each note should be one short sentence.\n"
        "- Also output recent_topics: a JSON array of 3-5 topic strings discussed recently.\n\n"
        'Respond with ONLY this JSON structure:\n'
        '{"notes": [...], "recent_topics": [...]}'
    )

    def update_personality(
        self,
        session_id: str,
        user_id: str | None = None,
        *,
        decay_rate: float = 0.1,
        prune_threshold: float = 0.15,
        max_notes: int = 40,
    ) -> None:
        """Use the judge model to analyze recent conversation and update personality notes."""
        if not self._chat_db or not self._judge_llm:
            return
        session_key = session_id or "main"
        if user_id:
            session_key = f"{user_id}:{session_key}"

        recent = self._chat_db.get_messages(session_key, limit=20)
        if len(recent) < 6:
            return

        existing = self._chat_db.get_personality_notes(session_key)
        existing_text = ""
        if existing:
            lines = [f"- [{n.category}] {n.note} (confidence: {n.confidence:.1f})" for n in existing]
            existing_text = "Existing notes:\n" + "\n".join(lines)

        conv_lines = [f"{r.role}: {r.content[:200]}" for r in recent[-14:]]
        conv_text = "Recent conversation:\n" + "\n".join(conv_lines)

        from langchain_core.messages import SystemMessage, HumanMessage
        prompt = [
            SystemMessage(content=self._PERSONALITY_JUDGE_PROMPT),
            HumanMessage(content=f"{existing_text}\n\n{conv_text}"),
        ]

        try:
            import json as _json
            import re as _re
            result = self._judge_llm.invoke(prompt)
            raw = _extract_text(getattr(result, "content", "")).strip()
            raw = _re.sub(r"\[\[reaction:\w+\]\]", "", raw).strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            self._log.info("Personality judge raw (%d chars): %s", len(raw), raw[:200])
            parsed = _json.loads(raw)

            notes_data = parsed.get("notes", [])
            if not isinstance(notes_data, list):
                self._log.warning("Personality judge returned invalid notes format")
                return

            note_tuples: list[tuple[str, str, float]] = []
            for item in notes_data[:max_notes]:
                if not isinstance(item, dict):
                    continue
                cat = str(item.get("category", "topic_interest")).strip()
                note = str(item.get("note", "")).strip()
                conf = float(item.get("confidence", 0.7))
                if note and cat:
                    note_tuples.append((cat, note, max(0.0, min(1.0, conf))))

            if note_tuples:
                self._chat_db.replace_personality_notes(session_key, note_tuples)
                self._log.info("Updated %d personality notes", len(note_tuples))

            topics = parsed.get("recent_topics", [])
            if isinstance(topics, list):
                for t in topics[:10]:
                    topic_str = str(t).strip()
                    if topic_str:
                        self._chat_db.add_recent_topic(session_key, topic_str)

        except Exception as exc:
            self._log.warning("Personality update failed: %s", exc)
            self._chat_db.decay_personality_notes(session_key, decay_rate, prune_threshold)

    def _invoke_run(self, session_key: str, messages: list[Any]) -> Any:
        from langchain_core.messages import SystemMessage, AIMessage

        sys_content = getattr(self, "_cached_system_message", None) or self._build_system_message(session_key)
        self._cached_system_message = None
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

    def _stream_run(
        self, session_key: str, messages: list[Any], stream_events: bool,
        *, force_plain_llm: bool = False,
    ) -> Any:
        from langchain_core.messages import SystemMessage

        sys_content = getattr(self, "_cached_system_message", None) or self._build_system_message(session_key)
        self._cached_system_message = None
        full: list[Any] = [SystemMessage(content=sys_content)] + messages
        self._log.info(
            "stream_run: react=%s tools=%d msgs=%d sys_chars=%d force_plain=%s",
            self._has_react_agent, len(self._tools), len(full), len(sys_content), force_plain_llm,
        )
        if not force_plain_llm and self._has_react_agent and self._tools:
            return ("react_stream", self._react_stream_with_retry(full))
        return ("llm_stream", self._llm.stream(full))

    _TOOL_INTENT_PATTERNS = [
        r"let me (navigate|open|search|click|browse|type|take|check|look|find|go to|play)",
        r"i'?ll (navigate|open|search|click|browse|type|take|go to|find|play)",
        r"navigat(e|ing)\s+to",
        r"search(ing)?\s+(for|on|youtube|google)",
        r"open(ing)?\s+(the|a|google|youtube|that|this)",
        r"(find|play|look up)\s+.{3,20}\s+(for you|on youtube|on google|right now)",
    ]

    def _has_tool_intent(self, text: str) -> bool:
        import re
        text_lower = text.lower()
        return any(re.search(p, text_lower) for p in self._TOOL_INTENT_PATTERNS)

    _JUDGE_SYSTEM = (
        "You are a JSON-only quality-control judge. You MUST respond with a single JSON object and nothing else. "
        "No commentary, no markdown, no tags, no preamble — only valid JSON.\n\n"
        "Task: Decide whether an AI assistant's conversation turn is complete.\n\n"
        "Reply with EXACTLY one JSON object:\n"
        '{"verdict": "<VERDICT>", "nudge": "<message or empty>"}\n\n'
        "Possible verdicts:\n"
        '- "complete": The assistant finished the task and gave the user a useful spoken response. nudge must be "".\n'
        '- "needs_summary": Tools were called successfully but the assistant never gave a spoken answer. '
        "nudge = a short instruction telling the assistant to summarize results in speech.\n"
        '- "needs_tools": The assistant said it would use tools but never actually called any. '
        "nudge = a short instruction telling it to actually call the tools.\n"
        '- "needs_continuation": The assistant started calling tools but didn\'t finish the task. '
        "nudge = a short instruction telling it to continue.\n\n"
        "IMPORTANT: The nudge must be a brief, direct instruction (1-2 sentences). "
        "Always include: do NOT repeat any greeting or introduction."
    )

    def _judge_nudge(
        self,
        user_request: str,
        tool_calls: int,
        tool_names: list[str],
        ai_text: str,
        last_ai: str,
    ) -> str:
        """Use the small judge model to decide whether/what nudge is needed.

        Returns empty string if the turn is complete, otherwise the nudge message.
        Falls back to regex-based heuristics if judge model is unavailable.
        """
        if ai_text.strip() and tool_calls == 0 and not self._has_tool_intent(ai_text):
            self._log.info("Judge fast-path: text present, no tools, no tool intent → complete")
            return ""

        if not self._judge_llm:
            return self._judge_nudge_fallback(tool_calls, tool_names, ai_text, last_ai)

        from langchain_core.messages import SystemMessage, HumanMessage as JHuman

        summary_parts = [f"User request: {user_request[:200]}"]
        summary_parts.append(f"Tool calls made: {tool_calls} ({', '.join(tool_names[:10]) or 'none'})")
        if ai_text:
            summary_parts.append(f"Assistant's spoken text: {ai_text[:300]}")
        else:
            summary_parts.append("Assistant's spoken text: (none)")
        if last_ai and last_ai != ai_text:
            summary_parts.append(f"Last AI message: {last_ai[:200]}")

        prompt = [
            SystemMessage(content=self._JUDGE_SYSTEM),
            JHuman(content="\n".join(summary_parts)),
        ]

        try:
            import json as _json
            import re as _re
            result = self._judge_llm.invoke(prompt)
            raw = _extract_text(getattr(result, "content", "")).strip()
            raw = _re.sub(r"\[\[reaction:\w+\]\]", "", raw).strip()
            raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            self._log.info("Judge raw response: %s", raw[:200])
            parsed = _json.loads(raw)
            verdict = parsed.get("verdict", "complete")
            nudge = (parsed.get("nudge") or "").strip()
            if verdict == "complete":
                self._log.info("Judge verdict: complete (no nudge needed)")
                return ""
            if not nudge:
                nudge = self._default_nudge_for_verdict(verdict, tool_calls)
                if not nudge:
                    self._log.info("Judge verdict: %s but no nudge generated, treating as complete", verdict)
                    return ""
            self._log.info("Judge verdict: %s, nudge: %s", verdict, nudge[:100])
            return nudge
        except Exception as exc:
            self._log.warning("Judge model failed (%s), falling back to heuristics", exc)
            return self._judge_nudge_fallback(tool_calls, tool_names, ai_text, last_ai)

    _VERDICT_DEFAULT_NUDGES = {
        "needs_summary": (
            "Good, you called tools but didn't provide a spoken response. "
            "Briefly summarize what you did and what the result is. "
            "Do NOT repeat any greeting or introduction. "
            "Reply with text ONLY, do NOT call any more tools."
        ),
        "needs_tools": (
            "You said you would use tools but didn't call any. "
            "Please actually call the appropriate tool now."
        ),
        "needs_continuation": (
            "You started the task but didn't finish. "
            "Continue calling tools to complete it. "
            "Do NOT greet again or repeat your introduction."
        ),
    }

    def _default_nudge_for_verdict(self, verdict: str, tool_calls: int) -> str:
        """Generate a default nudge when the judge verdict is non-complete but nudge text is empty."""
        if verdict == "needs_summary" and tool_calls == 0:
            return ""
        return self._VERDICT_DEFAULT_NUDGES.get(verdict, "")

    def _judge_nudge_fallback(
        self,
        tool_calls: int,
        tool_names: list[str],
        ai_text: str,
        last_ai: str,
    ) -> str:
        """Regex-based fallback when judge model is unavailable."""
        if tool_calls == 0 and self._has_tool_intent(ai_text):
            return (
                "You said you would use browser tools but didn't call any. "
                "Please actually call the appropriate tool now."
            )
        if tool_calls > 0 and not ai_text:
            return (
                "Good, you called tools but didn't provide a spoken response. "
                "Briefly summarize what you did and what the result is. "
                "Do NOT repeat any greeting or introduction. "
                "Reply with text ONLY, do NOT call any more tools."
            )
        if tool_calls > 0 and self._has_tool_intent(last_ai):
            return (
                "You started the task but didn't finish. "
                "Continue calling tools to complete it. "
                "Do NOT greet again or repeat your introduction."
            )
        return ""

    def _stream_and_collect(
        self, stream_iter: Any, msgs_out: list[Any], stats: dict[str, Any],
    ) -> Any:
        """Yield updates from *stream_iter* while collecting stats into *stats*."""
        tool_count = 0
        tool_names: list[str] = []
        last_ai = ""

        for update in stream_iter:
            for _node, node_data in update.items():
                for m in (node_data.get("messages", []) if isinstance(node_data, dict) else []):
                    msgs_out.append(m)
                    if getattr(m, "type", None) == "ai":
                        tc = getattr(m, "tool_calls", None)
                        if tc:
                            tool_count += len(tc)
                            for call in tc:
                                tool_names.append(call.get("name", "?"))
                        c = _extract_text(getattr(m, "content", ""))
                        if c.strip():
                            last_ai = c
            yield update

        stats["tool_calls"] = tool_count
        stats["tool_names"] = tool_names
        stats["last_ai"] = last_ai
        all_text = " ".join(
            _extract_text(getattr(m, "content", ""))
            for m in msgs_out if getattr(m, "type", None) == "ai"
        ).strip()
        stats["all_ai_text"] = all_text

    def _react_stream_with_retry(self, messages: list[Any]) -> Any:
        """Stream react agent with retry, using judge model to decide nudges.

        After the first pass, the judge model analyzes the turn and decides:
        - complete: no action needed
        - needs_summary / needs_tools / needs_continuation: retry with nudge

        Falls back to regex heuristics if judge model is unavailable.
        If after the retry the model still produced no text, a text-only
        LLM call (no tools) forces a spoken summary.
        """
        from langchain_core.messages import HumanMessage

        user_request = ""
        for m in reversed(messages):
            if getattr(m, "type", None) == "human":
                user_request = _extract_text(getattr(m, "content", ""))
                break

        all_streamed: list[Any] = []
        stats: dict[str, Any] = {}

        yield from self._stream_and_collect(
            self._agent.stream({"messages": messages}, stream_mode="updates"),
            all_streamed, stats,
        )

        tc = stats.get("tool_calls", 0)
        names = stats.get("tool_names", [])
        ai_text = stats.get("all_ai_text", "")
        last_ai = stats.get("last_ai", "")

        self._log.info(
            "Stream pass complete: tool_calls=%d tools=%s ai_text_len=%d last=%r",
            tc, names, len(ai_text), last_ai[:80],
        )

        nudge = self._judge_nudge(user_request, tc, names, ai_text, last_ai)

        if not nudge:
            return

        self._log.info("Nudging agent: %s", nudge)
        retry_msgs = list(messages) + all_streamed
        retry_msgs.append(HumanMessage(content=nudge))

        retry_streamed: list[Any] = []
        retry_stats: dict[str, Any] = {}
        yield from self._stream_and_collect(
            self._agent.stream({"messages": retry_msgs}, stream_mode="updates"),
            retry_streamed, retry_stats,
        )

        retry_ai_text = retry_stats.get("all_ai_text", "")
        self._log.info(
            "Retry pass complete: tool_calls=%d ai_text_len=%d",
            retry_stats.get("tool_calls", 0), len(retry_ai_text),
        )

        if retry_ai_text:
            return

        self._log.info("Retry produced no text; forcing text-only summary via raw LLM")
        summary_msgs = list(messages) + all_streamed + retry_streamed
        summary_msgs.append(HumanMessage(
            content="Now give a brief spoken summary of everything you just did and what the result was. "
            "Do NOT repeat any greeting or introduction. Continue naturally. "
            "Reply with text ONLY. Do NOT call any tools."
        ))
        for chunk in self._llm.stream(summary_msgs):
            content = _extract_text(getattr(chunk, "content", ""))
            if content:
                from langchain_core.messages import AIMessageChunk
                yield {"agent": {"messages": [AIMessageChunk(content=content)]}}


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
            _seen_ai_text: set[str] = set()
            tool_calls_seen: list[str] = []
            run_result = agent.run(
                message.strip(),
                session_id=session_id,
                user_id=user_id,
                stream=True,
                stream_events=True,
            )
            log = logging.getLogger("app.llm.langchain_agent")
            try:
                stream_kind, stream_iter = run_result
                log.info("Stream started: kind=%s", stream_kind)
                if stream_kind == "react_stream":
                    first_narration_done = False
                    for update in stream_iter:
                        if stop_requested and stop_requested():
                            break
                        for node_name, node_data in update.items():
                            msgs = node_data.get("messages", []) if isinstance(node_data, dict) else []
                            for m in msgs:
                                mtype = getattr(m, "type", None)
                                tc = getattr(m, "tool_calls", None)
                                raw_content = getattr(m, "content", "")
                                content = _extract_text(raw_content)
                                log.debug(
                                    "react node=%s type=%s tool_calls=%s content=%r",
                                    node_name, mtype, tc or "none", content[:100],
                                )
                                if mtype == "ai":
                                    if tc:
                                        if not first_narration_done and content.strip():
                                            _key = content.strip()
                                            if _key not in _seen_ai_text:
                                                first_narration_done = True
                                                _seen_ai_text.add(_key)
                                                accumulated.append(content)
                                                if on_content:
                                                    try:
                                                        on_content(content)
                                                    except Exception:
                                                        pass
                                        for call in tc:
                                            name = call.get("name", "tool")
                                            tool_calls_seen.append(name)
                                            if on_tool_use:
                                                try:
                                                    on_tool_use(name, "")
                                                except Exception:
                                                    pass
                                    elif content.strip():
                                        _key = content.strip()
                                        if _key not in _seen_ai_text:
                                            _seen_ai_text.add(_key)
                                            accumulated.append(content)
                                            if on_content:
                                                try:
                                                    on_content(content)
                                                except Exception:
                                                    pass
                else:
                    for chunk in stream_iter:
                        if stop_requested and stop_requested():
                            break
                        if hasattr(chunk, "content") and chunk.content:
                            delta = _extract_text(chunk.content)
                            if delta.strip():
                                accumulated.append(delta)
                                if on_content:
                                    try:
                                        on_content(delta)
                                    except Exception:
                                        pass
            except Exception as exc:
                log.warning("Streaming error: %s", exc, exc_info=True)
                if not accumulated and stream_kind == "react_stream":
                    log.info("React stream failed; retrying with plain LLM (no tools)")
                    try:
                        _, plain_iter = agent.run(
                            message.strip(),
                            session_id=session_id,
                            user_id=user_id,
                            stream=True,
                            stream_events=True,
                            force_plain_llm=True,
                        )
                        for chunk in plain_iter:
                            if stop_requested and stop_requested():
                                break
                            if hasattr(chunk, "content") and chunk.content:
                                delta = _extract_text(chunk.content)
                                if delta.strip():
                                    accumulated.append(delta)
                                    if on_content:
                                        try:
                                            on_content(delta)
                                        except Exception:
                                            pass
                    except Exception as fallback_exc:
                        log.warning("Plain LLM fallback also failed: %s", fallback_exc)
            ai_text = "".join(accumulated)
            if not ai_text.strip() and tool_calls_seen:
                log.info(
                    "No AI text but %d tool calls made (%s); generating fallback",
                    len(tool_calls_seen), tool_calls_seen,
                )
                fallback = f"I used {', '.join(tool_calls_seen)} to help with your request."
                if on_content:
                    try:
                        on_content(fallback)
                    except Exception:
                        pass
                ai_text = fallback
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
