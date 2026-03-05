from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any
from urllib import request
from urllib.error import HTTPError, URLError


class MCPHttpClient:
    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        startup_timeout_seconds: float = 10.0,
        request_timeout_seconds: float = 20.0,
        trace: Callable[[str, str], None] | None = None,
    ) -> None:
        self._url = str(url or "").strip()
        self._headers = {str(k): str(v) for k, v in (headers or {}).items()}
        self._startup_timeout_seconds = max(1.0, float(startup_timeout_seconds))
        self._request_timeout_seconds = max(1.0, float(request_timeout_seconds))
        self._trace = trace or (lambda *_args, **_kwargs: None)

        self._started = False
        self._next_id = 1
        self._initialize_result: dict[str, Any] = {}
        self._tools_cache: list[dict[str, Any]] = []

    @property
    def started(self) -> bool:
        return bool(self._started)

    def start(self) -> bool:
        if self._started:
            return True
        if not self._url:
            self._trace("mcp.error", "MCP HTTP URL is empty.")
            return False

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
                self._trace("mcp.error", "HTTP initialize returned invalid payload.")
                self._started = False
                return False

            self._initialize_result = dict(init_result)
            self._notify("notifications/initialized", {})
            self._tools_cache = self.list_tools(refresh=True)
        except Exception as exc:
            self._trace("mcp.error", f"HTTP MCP initialization failed: {exc}")
            self._started = False
            return False

        self._started = True
        self._trace("mcp.start", f"Connected MCP server via HTTP: {self._url}")
        return True

    def stop(self) -> None:
        self._started = False

    def list_tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        if self._tools_cache and not refresh:
            return [dict(item) for item in self._tools_cache]
        result = self._request("tools/list", {}, timeout_seconds=self._request_timeout_seconds)
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

    def get_runtime_status(self) -> dict[str, Any]:
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
            "connected": bool(self._started),
            "url": self._url,
            "protocol_version": protocol_version,
            "server_name": server_name,
            "server_version": server_version,
            "capability_keys": capability_keys,
            "tool_count": len(self._tools_cache),
            "tool_names": [str(item.get("name", "")).strip() for item in self._tools_cache if isinstance(item, dict)],
        }

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        payload = {
            "jsonrpc": "2.0",
            "method": str(method),
            "params": dict(params),
        }
        self._post_json(payload, timeout_seconds=self._request_timeout_seconds)

    def _request(self, method: str, params: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        req_id = self._next_request_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": str(method),
            "params": dict(params),
        }
        response = self._post_json(payload, timeout_seconds=timeout_seconds)
        if not isinstance(response, dict):
            raise RuntimeError("Invalid MCP HTTP response payload.")

        if "error" in response and isinstance(response["error"], dict):
            err = response["error"]
            code = err.get("code", "unknown")
            msg = err.get("message", "MCP HTTP call failed")
            raise RuntimeError(f"MCP HTTP error {code}: {msg}")

        result = response.get("result", {})
        return result if isinstance(result, dict) else {}

    def _post_json(self, payload: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        req = request.Request(
            self._url,
            data=data,
            headers={
                "Content-Type": "application/json",
                **self._headers,
            },
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=max(0.1, float(timeout_seconds))) as resp:
                raw = resp.read()
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"HTTP {exc.code}: {body or exc.reason}") from exc
        except URLError as exc:
            raise RuntimeError(f"HTTP MCP connection failed: {exc.reason}") from exc

        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as exc:
            raise RuntimeError(f"Invalid JSON from HTTP MCP server: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError("HTTP MCP response must be a JSON object.")
        return parsed

    def _next_request_id(self) -> int:
        current = self._next_id
        self._next_id += 1
        return current
