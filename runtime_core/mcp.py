from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from threading import Lock
from typing import Any, Callable

from .tools import ToolRegistry


MAX_MCP_RESPONSE_BYTES = 1 * 1024 * 1024  # 1 MB hard cap per framed message
_MAX_MCP_DESCRIPTION_LENGTH = 512


def _sanitize_mcp_schema(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"type": "object", "properties": {}}
    result: dict[str, Any] = {}
    if isinstance(raw.get("type"), str):
        result["type"] = raw["type"]
    if isinstance(raw.get("properties"), dict):
        result["properties"] = raw["properties"]
    if isinstance(raw.get("required"), list):
        result["required"] = raw["required"]
    return result


@dataclass
class MCPServerConfig:
    name: str
    command: str
    args: list[str]
    env: dict[str, str]


class MCPServerSession:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._request_id = 0
        self._lock = Lock()

    def start(self) -> None:
        if self._process is not None:
            return
        env = dict(os.environ)
        env.update(self.config.env)
        self._process = subprocess.Popen(
            [self.config.command, *self.config.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=0,
            env=env,
        )
        try:
            self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "runtime-runtime", "version": "0.1.0"},
                },
            )
            self._notify("notifications/initialized", {})
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._process is None:
            return
        process = self._process
        self._process = None
        try:
            process.terminate()
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def list_tools(self) -> list[dict[str, Any]]:
        self.start()
        response = self._rpc("tools/list", {})
        return list(response.get("tools", []))

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.start()
        return self._rpc("tools/call", {"name": name, "arguments": arguments})

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("MCP process is not running")
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        self._send_message(payload)

    def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._process is None or self._process.stdin is None or self._process.stdout is None:
                raise RuntimeError("MCP process is not running")
            self._request_id += 1
            request_id = self._request_id
            payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            self._send_message(payload)

            while True:
                message = self._read_message(method)
                if "id" not in message:
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(f"MCP error from {self.config.name}: {message['error']}")
                return message.get("result", {})

    def _send_message(self, payload: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("MCP process is not running")
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self._process.stdin.write(header)
        self._process.stdin.write(body)
        self._process.stdin.flush()

    def _read_message(self, method: str) -> dict[str, Any]:
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("MCP process is not running")

        headers: dict[str, str] = {}
        while True:
            line = self._process.stdout.readline()
            if not line:
                stderr = self._read_stderr()
                raise RuntimeError(f"MCP server closed while waiting for {method}: {stderr}")
            if line in {b"\r\n", b"\n"}:
                break
            try:
                key, value = line.decode("ascii").split(":", 1)
            except ValueError as exc:
                raise RuntimeError(f"Invalid MCP header from {self.config.name}: {line!r}") from exc
            headers[key.strip().lower()] = value.strip()

        content_length = headers.get("content-length")
        if content_length is None:
            raise RuntimeError(f"Missing Content-Length header from {self.config.name}.")

        try:
            length = int(content_length)
        except ValueError as exc:
            raise RuntimeError(f"Invalid Content-Length header from {self.config.name}: {content_length!r}") from exc

        if length > MAX_MCP_RESPONSE_BYTES:
            raise RuntimeError(
                f"MCP response from {self.config.name} exceeds {MAX_MCP_RESPONSE_BYTES}-byte limit ({length} bytes)."
            )

        body = self._process.stdout.read(length)
        if len(body) != length:
            stderr = self._read_stderr()
            raise RuntimeError(f"MCP server closed while reading body for {method}: {stderr}")
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"MCP server {self.config.name} sent malformed JSON: {exc}") from exc

    def _read_stderr(self) -> str:
        if self._process is None or self._process.stderr is None:
            return ""
        try:
            raw = self._process.stderr.read()
        except Exception:
            return ""
        return raw.decode("utf-8", errors="replace")


class MCPBridge:
    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.sessions: dict[str, MCPServerSession] = {}

    def load(self) -> None:
        if not self.config_path.exists():
            return
        payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        for raw in payload.get("servers", []):
            config = MCPServerConfig(
                name=raw["name"],
                command=raw["command"],
                args=list(raw.get("args", [])),
                env=dict(raw.get("env", {})),
            )
            self.sessions[config.name] = MCPServerSession(config)

    def register_tools(self, registry: ToolRegistry, emit: Callable[..., None] | None = None) -> list[str]:
        registered: list[str] = []
        for server_name, session in self.sessions.items():
            try:
                tool_specs = session.list_tools()
            except Exception as exc:
                session.close()
                if emit is not None:
                    emit(
                        "mcp.registration_failed",
                        "mcp",
                        f"Failed to register MCP tools from {server_name}.",
                        level="warning",
                        details={"server": server_name, "error": str(exc)},
                    )
                continue
            for tool in tool_specs:
                raw_name = tool.get("name", "")
                tool_name = str(raw_name).strip() if raw_name is not None else ""
                if not tool_name:
                    if emit is not None:
                        emit(
                            "mcp.invalid_tool_spec",
                            "mcp",
                            f"Skipped MCP tool from {server_name}: missing or empty name.",
                            level="warning",
                            details={"server": server_name, "raw_name": repr(raw_name)},
                        )
                    continue

                final_name = f"{server_name}__{tool_name}"
                if registry.get(final_name) is not None:
                    if emit is not None:
                        emit(
                            "mcp.tool_name_collision",
                            "mcp",
                            f"Skipped MCP tool '{final_name}': name already registered.",
                            level="warning",
                            details={"server": server_name, "tool": tool_name, "final_name": final_name},
                        )
                    continue

                raw_description = tool.get("description", f"MCP tool from {server_name}")
                description = str(raw_description)[:_MAX_MCP_DESCRIPTION_LENGTH]
                schema = _sanitize_mcp_schema(tool.get("inputSchema", {}))

                def handler(_session: MCPServerSession = session, _tool_name: str = tool_name, **kwargs: Any) -> dict[str, Any]:
                    return _session.call_tool(_tool_name, kwargs)

                registry.register(
                    name=final_name,
                    description=description,
                    schema=schema,
                    handler=handler,
                    source=f"mcp:{server_name}",
                    scope="configured_mcp",
                )
                registered.append(final_name)
        return registered

    def close(self) -> None:
        for session in self.sessions.values():
            session.close()
