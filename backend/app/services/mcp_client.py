"""MCP 客户端：基于 JSON-RPC 2.0 的轻量实现（不依赖 mcp SDK）。

支持传输：
- http：Streamable HTTP / JSON-RPC POST 到 endpoint（最通用）
- sse：HTTP SSE 端点（先 POST，兼容返回 text/event-stream）
- stdio：启动本地命令，经 stdin/stdout 交换 JSON-RPC（容器内需可访问命令）

协议方法：initialize → tools/list → tools/call
"""
from __future__ import annotations

import json
import subprocess
import threading
import uuid
from typing import Any

import httpx

PROTOCOL_VERSION = "2024-11-05"


class MCPClient:
    def __init__(self, transport: str, endpoint: str = "", command: str = "",
                 env: dict | None = None, timeout: int = 20, headers: dict | None = None):
        self.transport = transport
        self.endpoint = endpoint
        self.command = command
        self.env = env or {}
        self.headers = headers or {}
        self.timeout = timeout
        self._id = 0
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.initialized = False

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    # ── 传输层 ──

    def _send_http(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream", **self.headers}
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as c:
            resp = c.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            text = resp.text
            # Streamable HTTP 可能返回 SSE 帧：取第一个 data: 的 JSON
            if text.lstrip().startswith("event:") or "data:" in text[:200]:
                for line in text.splitlines():
                    if line.startswith("data:"):
                        return json.loads(line[5:].strip())
                raise RuntimeError("SSE 响应中无 data 帧")
            return resp.json()

    def _send_stdio(self, payload: dict) -> dict:
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                import os
                env = {**os.environ, **self.env}
                self._proc = subprocess.Popen(
                    self.command, shell=True, stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1, env=env,
                )
            self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            if not line:
                err = self._proc.stderr.read()[:300] if self._proc.stderr else ""
                raise RuntimeError(f"stdio 无响应: {err}")
            return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        payload = {"jsonrpc": "2.0", "id": self._next_id(), "method": method}
        if params is not None:
            payload["params"] = params
        if self.transport == "stdio":
            data = self._send_stdio(payload)
        else:
            data = self._send_http(payload)
        if "error" in data:
            raise RuntimeError(f"MCP {method} 错误: {data['error']}")
        return data.get("result", {})

    def notify(self, method: str, params: dict | None = None) -> None:
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        if self.transport == "stdio":
            self._send_stdio(payload)
        else:
            # notification 无响应体（MCP 规范：服务端不回复），忽略解析
            try:
                with httpx.Client(timeout=self.timeout, follow_redirects=True) as c:
                    c.post(self.endpoint, json=payload,
                           headers={"Content-Type": "application/json", **self.headers})
            except Exception:
                pass

    # ── 协议方法 ──

    def initialize(self) -> dict:
        result = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "agent-work-system", "version": "1.0"},
        })
        self.notify("notifications/initialized", {})
        self.initialized = True
        return result

    def list_tools(self) -> list[dict]:
        result = self.request("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        # MCP 返回 content: [{type:"text", text:"..."}]
        parts = []
        for c in result.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)

    def close(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None


def connect_and_discover(transport: str, endpoint: str = "", command: str = "",
                         env: dict | None = None, timeout: int = 20,
                         headers: dict | None = None) -> dict:
    """连接 MCP Server 并发现其工具与能力信息。"""
    client = MCPClient(transport, endpoint, command, env, timeout, headers)
    try:
        info = client.initialize()
        tools = client.list_tools()
        server_info = info.get("serverInfo", {})
        return {
            "server_name": server_info.get("name", ""),
            "server_version": server_info.get("version", ""),
            "protocol_version": info.get("protocolVersion", ""),
            "tools": [{
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema", {}),
            } for t in tools],
        }
    finally:
        client.close()
