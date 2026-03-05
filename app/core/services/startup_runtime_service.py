from __future__ import annotations

from collections.abc import Callable


class StartupRuntimeService:
    def __init__(
        self,
        *,
        persona_snapshot: Callable[[int], dict],
        history_messages: Callable[[int], list[dict[str, str]]],
        history_summary: Callable[[int, int], str],
        remember_history: Callable[[], bool],
        startup_context_prewarm_enabled: Callable[[], bool],
        startup_history_limit: Callable[[], int],
        history_summary_enabled: Callable[[], bool],
        history_summary_limit: Callable[[], int],
        history_summary_max_chars: Callable[[], int],
        ollama_list_models: Callable[[], list[str]],
        ollama_chat: Callable[[list[dict[str, str]]], str],
        thinking_chat: Callable[[], tuple[Callable[[list[dict[str, str]]], str] | None, str | None]],
        chat_model: Callable[[], str],
        tts_getter: Callable[[], object],
        tts_status: Callable[[], tuple[str, str]],
        mcp_runtime_status: Callable[[], dict[str, object]],
        trace: Callable[[str, str], None],
    ) -> None:
        self._persona_snapshot = persona_snapshot
        self._history_messages = history_messages
        self._history_summary = history_summary
        self._remember_history = remember_history
        self._startup_context_prewarm_enabled = startup_context_prewarm_enabled
        self._startup_history_limit = startup_history_limit
        self._history_summary_enabled = history_summary_enabled
        self._history_summary_limit = history_summary_limit
        self._history_summary_max_chars = history_summary_max_chars
        self._ollama_list_models = ollama_list_models
        self._ollama_chat = ollama_chat
        self._thinking_chat = thinking_chat
        self._chat_model = chat_model
        self._tts_getter = tts_getter
        self._tts_status = tts_status
        self._mcp_runtime_status = mcp_runtime_status
        self._trace = trace

    def build_startup_greeting(self) -> str:
        snapshot = self._persona_snapshot(6)
        notes = list(snapshot.get("user_notes", []))
        has_history = bool(self._remember_history() and self._history_messages(1))

        if notes and has_history:
            return "Welcome back. I loaded your profile and recent conversation context."
        if notes:
            return "Welcome back. I loaded your profile."
        if has_history:
            return "Welcome back. I loaded recent conversation context."
        return "Welcome back. Audio is ready."

    def prewarm_tts(self) -> None:
        tts = self._tts_getter()
        warmup_sync = getattr(tts, "warmup_sync", None)
        if callable(warmup_sync):
            try:
                ok = bool(warmup_sync())
                if not ok:
                    state, details = self._tts_status()
                    self._trace("tts.error", f"TTS warmup failed ({state}): {details}")
            except Exception as exc:
                self._trace("tts.error", f"TTS warmup failed: {exc}")
            return

        warmup_async = getattr(tts, "warmup_async", None)
        if callable(warmup_async):
            try:
                warmup_async()
            except Exception as exc:
                self._trace("tts.error", f"TTS warmup async failed: {exc}")

    def prewarm_runtime(self, on_status: Callable[[str], None] | None = None) -> None:
        def report(message: str) -> None:
            if on_status:
                on_status(message)

        report("Checking Ollama availability...")
        try:
            models = self._ollama_list_models()
        except Exception as exc:
            raise RuntimeError(f"Failed to reach Ollama server: {exc}") from exc

        chat_model = self._chat_model()
        if chat_model not in models:
            raise RuntimeError(
                f"Configured chat model not found in Ollama: {chat_model}. Pull it first."
            )

        report(f"Warming response model: {chat_model}")
        self._ollama_chat(self._build_startup_prewarm_messages())

        thinking_chat, thinking_model = self._thinking_chat()
        if thinking_chat is not None and thinking_model:
            report(f"Warming thinking model: {thinking_model}")
            thinking_chat([
                {"role": "user", "content": "Reply with OK."},
            ])

        report("Warming TTS models...")
        tts = self._tts_getter()
        warmup_sync = getattr(tts, "warmup_sync", None)
        if callable(warmup_sync):
            success = bool(warmup_sync())
            if not success:
                state, details = self._tts_status()
                raise RuntimeError(f"TTS warmup failed ({state}): {details}")
        else:
            self.prewarm_tts()

        self._check_mcp_runtime(report)

        report("Warmup complete")

    def _check_mcp_runtime(self, report: Callable[[str], None]) -> None:
        try:
            status = self._mcp_runtime_status()
        except Exception as exc:
            self._trace("mcp.error", f"Failed to read MCP runtime status during preload: {exc}")
            raise RuntimeError(f"Failed to check MCP runtime: {exc}") from exc

        if not isinstance(status, dict):
            self._trace("mcp.error", "Invalid MCP runtime status payload during preload.")
            raise RuntimeError("Invalid MCP runtime status payload.")

        enabled = bool(status.get("enabled", False))
        if not enabled:
            report("MCP disabled; skipping MCP preload checks")
            return

        server_count = int(status.get("server_count", 0) or 0)
        connected_count = int(status.get("connected_count", 0) or 0)
        report(f"Checking MCP servers: {connected_count}/{server_count} running")

        if server_count <= 0:
            raise RuntimeError("MCP is enabled but no servers are configured or running.")
        if connected_count <= 0:
            raise RuntimeError("MCP is enabled but no MCP servers are running.")
        if connected_count < server_count:
            self._trace(
                "mcp.warn",
                f"MCP preload check partial readiness: {connected_count}/{server_count} server(s) running.",
            )

    def _build_startup_prewarm_messages(self) -> list[dict[str, str]]:
        if not self._startup_context_prewarm_enabled():
            return [{"role": "user", "content": "Reply with OK."}]

        persona_snapshot = self._persona_snapshot(6)
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "You are preparing startup context for an assistant session. "
                    "Read the profile and history context and reply with only OK."
                ),
            }
        ]

        background = str(persona_snapshot.get("assistant_background", "")).strip()
        user_notes = [
            str(item).strip() for item in list(persona_snapshot.get("user_notes", [])) if str(item).strip()
        ]
        if background or user_notes:
            profile_lines: list[str] = []
            if background:
                profile_lines.append(f"Assistant background: {background}")
            if user_notes:
                profile_lines.append("User notes:")
                for note in user_notes[-6:]:
                    profile_lines.append(f"- {note}")
            messages.append({"role": "user", "content": "\n".join(profile_lines)})

        if self._history_summary_enabled() and self._remember_history():
            summary = self._history_summary(self._history_summary_limit(), self._history_summary_max_chars())
            if summary:
                messages.append({"role": "user", "content": f"Conversation summary:\n{summary}"})

        if self._remember_history():
            for item in self._history_messages(self._startup_history_limit()):
                role = str(item.get("role", "")).strip().lower()
                content = str(item.get("content", "")).strip()
                if role in {"user", "assistant"} and content:
                    messages.append({"role": role, "content": content})

        messages.append({"role": "user", "content": "Reply with OK."})
        return messages