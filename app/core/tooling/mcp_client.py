from __future__ import annotations

import atexit
from collections.abc import Callable
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from typing import Any


class MCPStdioClient:
    def __init__(
        self,
        *,
        command: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        framing_mode: str = "content-length",
        startup_timeout_seconds: float = 10.0,
        trace: Callable[[str, str], None] | None = None,
    ) -> None:
        self._command = str(command or "").strip()
        self._args = [str(item) for item in (args or [])]
        self._env = {str(k): str(v) for k, v in (env or {}).items()}
        normalized_framing = str(framing_mode or "content-length").strip().lower()
        self._framing_mode = (
            normalized_framing
            if normalized_framing in {"content-length", "newline-json"}
            else "content-length"
        )
        self._startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self._trace = trace or (lambda *_args, **_kwargs: None)

        self._process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._inbound: queue.Queue[dict[str, Any]] = queue.Queue()
        self._send_lock = threading.Lock()
        self._next_id = 1
        self._leftover: dict[int, dict[str, Any]] = {}
        self._started = False
        self._tools_cache: list[dict[str, Any]] = []
        self._initialize_result: dict[str, Any] = {}

        atexit.register(self.stop)

    @property
    def started(self) -> bool:
        process = self._process
        return bool(self._started and process is not None and process.poll() is None)

    def start(self) -> bool:
        process = self._process
        if self._started and process is not None and process.poll() is None:
            return True
        if self._started and (process is None or process.poll() is not None):
            # Process crashed/exited; allow a clean restart on next start call.
            self._started = False
            self._process = None
        if not self._command:
            self._trace("mcp.error", "MCP command is empty.")
            return False

        launch_env = self._build_env()
        resolved_command = self._resolve_command_for_launch(self._command, launch_env)
        cmd = [resolved_command, *self._args]
        try:
            self._process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=launch_env,
            )
        except Exception as exc:
            self._trace("mcp.error", f"Failed to start MCP process: {exc}")
            return False

        self._stop_event.clear()
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._stderr_thread = threading.Thread(target=self._stderr_loop, daemon=True)
        self._reader_thread.start()
        self._stderr_thread.start()

        try:
            init_result = self._request(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "english-speaking-assistant",
                        "version": "0.1.0",
                    },
                },
                timeout_seconds=self._startup_timeout_seconds,
            )
            if not isinstance(init_result, dict):
                self._trace("mcp.error", "Initialize returned invalid payload.")
                self.stop()
                return False

            self._initialize_result = dict(init_result)

            self._notify("notifications/initialized", {})
            self._tools_cache = self.list_tools(refresh=True)
        except Exception as exc:
            self._trace("mcp.error", f"MCP initialization failed: {exc}")
            self.stop()
            return False

        self._started = True
        self._trace("mcp.start", f"Connected MCP server via stdio: {' '.join(cmd)}")
        return True

    @staticmethod
    def _resolve_command_for_launch(command: str, env: dict[str, str]) -> str:
        raw = str(command or "").strip()
        if not raw:
            return raw
        # Absolute/relative paths should be used as provided.
        if any(sep in raw for sep in ("/", "\\")):
            return raw
        resolved = shutil.which(raw, path=str(env.get("PATH", "")))
        return str(resolved).strip() if resolved else raw

    def stop(self) -> None:
        self._started = False
        self._stop_event.set()

        proc = self._process
        self._process = None
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                else:
                    proc.wait(timeout=0.2)
            except Exception:
                pass

    def get_runtime_status(self) -> dict[str, Any]:
        process = self._process
        connected = bool(self._started and process is not None and process.poll() is None)
        server_info = self._initialize_result.get("serverInfo", {})
        capabilities = self._initialize_result.get("capabilities", {})
        protocol_version = str(self._initialize_result.get("protocolVersion", "")).strip()

        server_name = ""
        server_version = ""
        if isinstance(server_info, dict):
            server_name = str(server_info.get("name", "")).strip()
            server_version = str(server_info.get("version", "")).strip()

        capability_keys: list[str] = []
        if isinstance(capabilities, dict):
            capability_keys = sorted(str(key).strip() for key in capabilities.keys() if str(key).strip())

        return {
            "connected": connected,
            "command": self._command,
            "args": list(self._args),
            "framing_mode": self._framing_mode,
            "protocol_version": protocol_version,
            "server_name": server_name,
            "server_version": server_version,
            "capability_keys": capability_keys,
            "tool_count": len(self._tools_cache),
            "tool_names": [str(item.get("name", "")).strip() for item in self._tools_cache if isinstance(item, dict)],
        }

    def list_tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._tools_cache and not refresh:
            return [dict(item) for item in self._tools_cache]
        result = self._request("tools/list", {}, timeout_seconds=self._startup_timeout_seconds)
        tools = result.get("tools", []) if isinstance(result, dict) else []
        parsed: list[dict[str, Any]] = []
        if isinstance(tools, list):
            for item in tools:
                if isinstance(item, dict):
                    parsed.append(dict(item))
        self._tools_cache = parsed
        return [dict(item) for item in self._tools_cache]

    def call_tool(self, *, name: str, args: dict[str, Any], timeout_ms: int = 10000) -> dict[str, Any]:
        timeout_seconds = max(0.1, float(timeout_ms) / 1000.0)
        result = self._request(
            "tools/call",
            {
                "name": str(name),
                "arguments": dict(args),
            },
            timeout_seconds=timeout_seconds,
        )
        return result if isinstance(result, dict) else {}

    def _build_env(self) -> dict[str, str]:
        merged = dict(os.environ)
        merged.update(self._env)

        # PATH resilience: if command is bare 'uvx' and not currently resolvable,
        # prepend common global uv installation location.
        command_name = str(self._command or "").strip().lower()
        if command_name == "uvx":
            resolved = shutil.which("uvx", path=str(merged.get("PATH", "")))
            if not resolved:
                candidate = Path.home() / ".local" / "bin"
                candidate_uvx = candidate / "uvx.exe"
                if candidate_uvx.exists():
                    current_path = str(merged.get("PATH", ""))
                    merged["PATH"] = f"{candidate};{current_path}" if current_path else str(candidate)
        return merged

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": str(method),
            "params": dict(params),
        }
        self._send_message(payload)

    def _request(self, method: str, params: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        req_id = self._next_request_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": str(method),
            "params": dict(params),
        }
        self._send_message(payload)

        if req_id in self._leftover:
            message = self._leftover.pop(req_id)
            return self._extract_result_or_raise(message)

        deadline = time.time() + max(0.1, float(timeout_seconds))
        while time.time() < deadline:
            try:
                message = self._inbound.get(timeout=0.1)
            except queue.Empty:
                if self._process is not None and self._process.poll() is not None:
                    raise RuntimeError("MCP server process exited while waiting for response.")
                continue

            msg_id = message.get("id")
            if isinstance(msg_id, int):
                if msg_id == req_id:
                    return self._extract_result_or_raise(message)
                self._leftover[msg_id] = message
                continue

            # Ignore notifications/requests from server for now.
            continue

        raise TimeoutError(f"Timed out waiting for MCP response to '{method}'.")

    def _extract_result_or_raise(self, message: dict[str, Any]) -> dict[str, Any]:
        if "error" in message and isinstance(message["error"], dict):
            err = message["error"]
            code = err.get("code", "unknown")
            msg = err.get("message", "MCP call failed")
            raise RuntimeError(f"MCP error {code}: {msg}")
        result = message.get("result", {})
        return result if isinstance(result, dict) else {}

    def _next_request_id(self) -> int:
        current = self._next_id
        self._next_id += 1
        return current

    def _send_message(self, payload: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("MCP process is not running.")

        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        if self._framing_mode == "newline-json":
            frame = data + b"\n"
        else:
            frame = b"Content-Length: " + str(len(data)).encode("ascii") + b"\r\n\r\n" + data
        with self._send_lock:
            process.stdin.write(frame)
            process.stdin.flush()

    def _reader_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return

        while not self._stop_event.is_set():
            if self._framing_mode == "newline-json":
                message = self._read_newline_message(process.stdout)
            else:
                message = self._read_framed_message(process.stdout)
            if message is None:
                return
            self._inbound.put(message)

    def _stderr_loop(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return

        while not self._stop_event.is_set():
            line = process.stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if text:
                self._trace("mcp.stderr", text)

    @staticmethod
    def _read_framed_message(stream: Any) -> dict[str, Any] | None:
        headers: dict[str, str] = {}
        while True:
            raw = stream.readline()
            if not raw:
                return None
            line = raw.decode("ascii", errors="ignore").strip()
            if not line:
                break
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        length_raw = headers.get("content-length", "")
        try:
            length = int(length_raw)
        except Exception:
            return None
        if length <= 0:
            return None

        payload = stream.read(length)
        if not payload:
            return None
        try:
            decoded = json.loads(payload.decode("utf-8", errors="replace"))
        except Exception:
            return None
        return decoded if isinstance(decoded, dict) else None

    @staticmethod
    def _read_newline_message(stream: Any) -> dict[str, Any] | None:
        while True:
            raw = stream.readline()
            if not raw:
                return None
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                decoded = json.loads(line)
            except Exception:
                continue
            return decoded if isinstance(decoded, dict) else None
