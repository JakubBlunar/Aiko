"""Claude-style agent controller.

Replaces LangGraph's ``create_react_agent`` with an explicit
``Triage -> (Plain | Reason-Act-Reflect)`` pipeline so that:

1. Conversational turns NEVER see tools. Plain LLM stream straight to TTS.
2. Action turns get a bounded Reason -> Act -> Reflect loop with a hard
   iteration cap, mirroring how Claude works.
3. The session type can scope which tools are available.

The controller emits the same ``("llm_stream", iter)`` /
``("react_stream", iter)`` shape that ``run_agent`` in
``app/llm/langchain_agent.py`` already knows how to consume, so the public
streaming contract is unchanged.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from app.core.action_intent import has_explicit_tool_request

logger = logging.getLogger("app.llm.agent_controller")


# ---------------------------------------------------------------------------
# Triage
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TriageDecision:
    mode: str  # "conversational" | "tools"
    confidence: float
    reason: str


_SHORT_MESSAGE_WORDS = 6


_TRIAGE_JUDGE_SYSTEM = (
    "You are a JSON-only intent classifier for a voice assistant.\n"
    "You MUST respond with a single JSON object and nothing else. No markdown.\n\n"
    "Decide whether the user's latest message requires the assistant to call "
    "a tool (search the web, browse a URL, click on a page, save a note, "
    "search memory, calculate something, look up the current time) to "
    "respond correctly.\n\n"
    "Conversation, opinions, reactions, agreement words, small talk, jokes, "
    "and questions the assistant can answer from its own knowledge do NOT "
    "need tools.\n\n"
    'Respond with ONLY: {"needs_tools": <true|false>, "reason": "<8 words>"}'
)


class TurnTriage:
    """Decide whether the latest user turn needs tools.

    Three-step hybrid:
      1. Strict regex via :func:`has_explicit_tool_request` -> tools.
      2. Short message fast path (<= 6 words, no action keyword) ->
         conversational.
      3. Optional judge LLM call with a hard timeout (default 0.5 s); on
         timeout or error we default to conversational.
    """

    def __init__(
        self,
        judge_llm: Any | None,
        *,
        judge_enabled: bool = True,
        timeout_seconds: float = 0.5,
    ) -> None:
        self._judge_llm = judge_llm
        self._judge_enabled = bool(judge_enabled)
        self._timeout = max(0.1, float(timeout_seconds))
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="triage-judge"
        )

    def classify(self, user_text: str, *, session_type: str = "chat") -> TriageDecision:
        text = (user_text or "").strip()
        if not text:
            return TriageDecision("conversational", 1.0, "empty_message")

        if has_explicit_tool_request(text):
            return TriageDecision("tools", 0.95, "regex_explicit_tool")

        words = text.split()
        if len(words) <= _SHORT_MESSAGE_WORDS:
            return TriageDecision("conversational", 0.9, "short_message")

        # Agentic sessions are slightly more permissive: the user already
        # opted in to autonomous tool use, so when the judge isn't sure we
        # still default to conversation but the threshold can be lower.
        if not self._judge_enabled or self._judge_llm is None:
            return TriageDecision(
                "conversational", 0.7, "no_judge_default_conversational"
            )

        verdict, reason = self._ask_judge(text)
        if verdict is True:
            return TriageDecision("tools", 0.75, f"judge:{reason or 'yes'}")
        if verdict is False:
            return TriageDecision("conversational", 0.8, f"judge:{reason or 'no'}")
        return TriageDecision("conversational", 0.6, "judge_timeout_default")

    def _ask_judge(self, user_text: str) -> tuple[bool | None, str]:
        # Fast path: talk to Ollama HTTP directly so the request is actually
        # cancelled (socket closed) when our timeout fires. concurrent.futures
        # cannot interrupt a running thread, and ChatOllama.invoke() blocks
        # uninterruptibly inside the ollama python client -- which means a
        # "timed out" judge call would keep generating on the GPU for many
        # seconds AFTER we returned, fighting the chat model for compute.
        # We also cap num_predict to ~64 tokens so the verdict JSON cannot
        # run away even on a slow GPU.
        model = (getattr(self._judge_llm, "model", "") or "").strip()
        base_url = (getattr(self._judge_llm, "base_url", "") or "").strip()
        client_kwargs = getattr(self._judge_llm, "client_kwargs", None) or {}
        headers = dict((client_kwargs.get("headers") or {})) if isinstance(client_kwargs, dict) else {}
        if model and base_url:
            try:
                import requests as _requests
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": _TRIAGE_JUDGE_SYSTEM},
                        {"role": "user", "content": f'User message: "{user_text[:300]}"'},
                    ],
                    "format": "json",
                    "stream": False,
                    "keep_alive": "10m",
                    "options": {
                        "temperature": 0.0,
                        "num_predict": 80,
                        "num_ctx": 2048,
                    },
                }
                # Connect should be sub-millisecond against local Ollama; cap
                # tightly so the worst case is bounded by self._timeout, not by
                # an unreachable-host connect timeout.
                resp = _requests.post(
                    base_url.rstrip("/") + "/api/chat",
                    json=payload,
                    headers=headers or None,
                    timeout=(0.5, max(0.2, self._timeout)),
                )
                resp.raise_for_status()
                data = resp.json()
                raw = ""
                if isinstance(data, dict):
                    msg = data.get("message") or {}
                    if isinstance(msg, dict):
                        raw = str(msg.get("content", "") or "").strip()
                    if not raw:
                        raw = str(data.get("response", "") or "").strip()
                raw = re.sub(r"\[\[reaction:\w+\]\]", "", raw).strip()
                raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                if not raw:
                    return None, "empty"
                parsed = json.loads(raw)
                needs = bool(parsed.get("needs_tools", False))
                reason = str(parsed.get("reason", "") or "").strip()[:60]
                return needs, reason
            except _requests.exceptions.Timeout:
                logger.info("Triage judge timed out after %.2fs (HTTP)", self._timeout)
                return None, "timeout"
            except Exception as exc:
                logger.debug("Triage judge HTTP failed (%s); falling back to LangChain path", exc)
                # fall through

        # Fallback: original LangChain-based path (still uses uninterruptible
        # invoke under the hood). Reached only if we couldn't extract a
        # base_url/model from the judge LLM, or the HTTP call errored.
        from langchain_core.messages import HumanMessage, SystemMessage

        prompt = [
            SystemMessage(content=_TRIAGE_JUDGE_SYSTEM),
            HumanMessage(content=f'User message: "{user_text[:300]}"'),
        ]

        def _run() -> tuple[bool | None, str]:
            try:
                result = self._judge_llm.invoke(prompt)
                raw = str(getattr(result, "content", "") or "").strip()
                raw = re.sub(r"\[\[reaction:\w+\]\]", "", raw).strip()
                raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                parsed = json.loads(raw)
                needs = bool(parsed.get("needs_tools", False))
                reason = str(parsed.get("reason", "") or "").strip()[:60]
                return needs, reason
            except Exception as exc:
                logger.debug("Triage judge failed: %s", exc)
                return None, "judge_error"

        future = self._executor.submit(_run)
        try:
            return future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError:
            logger.info("Triage judge timed out after %.2fs (fallback path leaks GPU until done)", self._timeout)
            future.cancel()
            return None, "timeout"
        except Exception as exc:
            logger.debug("Triage judge unexpected error: %s", exc)
            return None, "error"

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Reason -> Act -> Reflect
# ---------------------------------------------------------------------------


def _short_repr(value: Any, limit: int = 80) -> str:
    s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "..."


class ReasonActReflect:
    """Explicit, bounded multi-step action loop.

    Behaves like a Claude-style controller: one tool decision per iteration,
    explicit reflection after each tool, hard iteration cap, and forced
    summary if the cap is hit.
    """

    _TOOL_RESULT_TRUNCATE = 8000

    def __init__(
        self,
        llm: Any,
        tools: list[Any],
        *,
        iterations_max: int = 3,
    ) -> None:
        self._llm = llm
        self._tools = list(tools)
        self._tools_by_name = {getattr(t, "name", ""): t for t in self._tools if getattr(t, "name", "")}
        self._iterations_max = max(1, int(iterations_max))

    def stream(self, messages: list[Any], *, allowed_names: set[str] | None = None) -> Iterable[dict]:
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        scoped_tools = self._filtered_tools(allowed_names)
        if not scoped_tools:
            logger.info("ReasonActReflect: no tools available; falling back to plain LLM.")
            for chunk in self._llm.stream(messages):
                content = self._chunk_text(chunk)
                if content:
                    yield {"agent": {"messages": [AIMessage(content=content)]}}
            return

        try:
            bound = self._llm.bind_tools(scoped_tools)
        except Exception as exc:
            logger.warning("bind_tools failed (%s); falling back to plain LLM.", exc)
            for chunk in self._llm.stream(messages):
                content = self._chunk_text(chunk)
                if content:
                    yield {"agent": {"messages": [AIMessage(content=content)]}}
            return

        convo: list[Any] = list(messages)
        scoped_by_name = {getattr(t, "name", ""): t for t in scoped_tools if getattr(t, "name", "")}

        for iteration in range(self._iterations_max):
            try:
                ai_msg = bound.invoke(convo)
            except Exception as exc:
                logger.warning("Action turn invoke failed: %s", exc)
                ai_msg = AIMessage(content=f"(tool dispatch error: {exc})")

            tool_calls = list(getattr(ai_msg, "tool_calls", None) or [])
            yield {"agent": {"messages": [ai_msg]}}

            if not tool_calls:
                logger.info(
                    "ReasonActReflect: iteration %d produced text only; done.", iteration + 1,
                )
                return

            convo.append(ai_msg)

            tool_msgs: list[Any] = []
            for call in tool_calls:
                name = str(call.get("name") or "")
                args = call.get("args") or {}
                tool_id = call.get("id") or ""
                tool = scoped_by_name.get(name)
                if tool is None:
                    tool_msgs.append(
                        ToolMessage(
                            content=f"Tool {name!r} is not available in this session.",
                            tool_call_id=tool_id,
                            name=name,
                        )
                    )
                    continue
                try:
                    if hasattr(tool, "invoke"):
                        output = tool.invoke(args)
                    else:
                        func = getattr(tool, "func", None)
                        output = func(**args) if callable(func) else f"Tool {name} has no implementation."
                except Exception as exc:
                    output = f"Tool {name} raised: {exc}"
                content = str(output)
                if len(content) > self._TOOL_RESULT_TRUNCATE:
                    head = content[: int(self._TOOL_RESULT_TRUNCATE * 0.8)]
                    tail = content[-int(self._TOOL_RESULT_TRUNCATE * 0.1):]
                    content = f"{head}\n\n... [truncated, {len(str(output))} chars] ...\n\n{tail}"
                logger.info(
                    "tool %s(%s) -> %s",
                    name,
                    _short_repr(args, 60),
                    _short_repr(output, 80),
                )
                tool_msgs.append(
                    ToolMessage(content=content, tool_call_id=tool_id, name=name)
                )

            yield {"tools": {"messages": tool_msgs}}
            convo.extend(tool_msgs)

            if iteration < self._iterations_max - 1:
                convo.append(
                    HumanMessage(
                        content=(
                            "You just received the tool result above. Either: "
                            "(a) call exactly ONE more tool if the task is genuinely "
                            "unfinished, OR "
                            "(b) reply directly to the user with a short spoken summary "
                            "of what you found. Do NOT call the same tool with the same "
                            "arguments again. Default to (b) unless you need new "
                            "information."
                        )
                    )
                )

        # Cap hit. Force a summary with a tool-free LLM call so we always
        # produce a spoken reply.
        logger.info("ReasonActReflect: hit iteration cap (%d); forcing summary.", self._iterations_max)
        convo.append(
            HumanMessage(
                content=(
                    "Stop calling tools. In 1-2 short spoken sentences, summarise what "
                    "you did and the outcome for the user. Do NOT call any tools. "
                    "Do NOT greet again."
                )
            )
        )
        try:
            for chunk in self._llm.stream(convo):
                content = self._chunk_text(chunk)
                if content:
                    yield {"agent": {"messages": [AIMessage(content=content)]}}
        except Exception as exc:
            logger.warning("Summary stream failed: %s", exc)
            yield {
                "agent": {
                    "messages": [
                        AIMessage(content="I hit the action limit before finishing. Let me know how to proceed.")
                    ]
                }
            }

    def _filtered_tools(self, allowed_names: set[str] | None) -> list[Any]:
        if allowed_names is None:
            return list(self._tools)
        return [t for t in self._tools if getattr(t, "name", "") in allowed_names]

    @staticmethod
    def _chunk_text(chunk: Any) -> str:
        content = getattr(chunk, "content", "")
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict):
                    parts.append(str(block.get("text", "")))
            return "".join(parts)
        return str(content or "")


# ---------------------------------------------------------------------------
# AgentController
# ---------------------------------------------------------------------------


class AgentController:
    """Triage -> Plain / Action dispatcher.

    Exposes :meth:`stream` returning a ``(kind, iter)`` tuple compatible with
    the existing ``_stream_run`` / ``run_agent`` contract:

      * ``kind="llm_stream"`` -- iterator of LangChain chunks (token stream)
      * ``kind="react_stream"`` -- iterator of update dicts
        ``{node: {"messages": [...]}}``

    """

    def __init__(
        self,
        llm: Any,
        tools: list[Any],
        *,
        judge_llm: Any | None = None,
        iterations_max: int = 3,
        triage_judge_enabled: bool = True,
        triage_judge_timeout: float = 0.5,
        session_policy_resolver: Callable[[str], set[str] | None] | None = None,
    ) -> None:
        self._llm = llm
        self._tools = list(tools)
        self._triage = TurnTriage(
            judge_llm,
            judge_enabled=triage_judge_enabled,
            timeout_seconds=triage_judge_timeout,
        )
        self._loop = ReasonActReflect(llm, tools, iterations_max=iterations_max)
        self._session_policy_resolver = session_policy_resolver

    def stream(
        self,
        messages: list[Any],
        *,
        user_text: str = "",
        session_type: str = "chat",
    ) -> tuple[str, Iterable[Any]]:
        decision = self._triage.classify(user_text, session_type=session_type)
        logger.info(
            "Triage: mode=%s session=%s reason=%s (text=%r)",
            decision.mode, session_type, decision.reason, (user_text or "")[:80],
        )

        if decision.mode == "conversational" or not self._tools:
            return "llm_stream", self._llm.stream(messages)

        allowed_names = None
        if self._session_policy_resolver is not None:
            try:
                allowed_names = self._session_policy_resolver(session_type)
            except Exception as exc:
                logger.debug("Session policy resolver failed: %s", exc)

        return "react_stream", self._loop.stream(messages, allowed_names=allowed_names)

    def shutdown(self) -> None:
        self._triage.shutdown()
