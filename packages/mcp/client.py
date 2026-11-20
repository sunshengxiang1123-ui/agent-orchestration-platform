"""MCP 客户端：握手、发现工具，并把远端工具注册进本地 ToolRegistry。

注册后的工具名为 ``mcp:<server>:<tool>``，Agent 是否能调用由 ToolContext 白名单
决定；结果不在客户端缓存，保证知识库等动态工具每次都拿到最新数据。
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from packages.core.models.io import ToolContext, ToolResult
from packages.mcp.server import PROTOCOL_VERSION, MCPError, MCPServer
from packages.tools.registry import ToolRegistry, ToolSpec


class MCPTransport(Protocol):
    async def send(self, message: dict[str, Any]) -> dict[str, Any] | None: ...


class InProcessTransport:
    """进程内传输：消息经 JSON 序列化往返，与跨进程传输的边界一致。"""

    def __init__(self, server: MCPServer) -> None:
        self.server = server

    async def send(self, message: dict[str, Any]) -> dict[str, Any] | None:
        wire = json.loads(json.dumps(message, ensure_ascii=False))
        response = await self.server.handle_message(wire)
        return None if response is None else json.loads(json.dumps(response, ensure_ascii=False))


class MCPClient:
    def __init__(self, server: str, transport: MCPTransport) -> None:
        self.server = server
        self.transport = transport
        self.server_info: dict[str, Any] = {}
        self._ids = itertools.count(1)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        message_id = next(self._ids)
        response = await self.transport.send(
            {"jsonrpc": "2.0", "id": message_id, "method": method, "params": params}
        )
        if response is None or response.get("id") != message_id:
            raise MCPError(-32603, f"MCP 服务端未正确响应 {method}")
        if "error" in response:
            error = response["error"]
            raise MCPError(int(error.get("code", -32603)), str(error.get("message", "")))
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def initialize(self) -> dict[str, Any]:
        self.server_info = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-platform", "version": "0.2.0"},
            },
        )
        await self.transport.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.server_info

    async def list_tools(self) -> list[dict[str, Any]]:
        tools = (await self.request("tools/list", {})).get("tools", [])
        return list(tools) if isinstance(tools, list) else []

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, meta: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if meta:
            params["_meta"] = meta
        return await self.request("tools/call", params)

    def tool_name(self, name: str) -> str:
        return f"mcp:{self.server}:{name}"

    async def register_tools(self, registry: ToolRegistry) -> list[str]:
        names: list[str] = []
        for tool in await self.list_tools():
            name = str(tool["name"])
            registry.register(
                ToolSpec(
                    name=self.tool_name(name),
                    description=str(tool.get("description", "MCP 远端工具")),
                    input_schema=dict(tool.get("inputSchema") or {"type": "object"}),
                    permission="read",
                    cacheable=False,
                    handler=self._handler(name),
                )
            )
            names.append(self.tool_name(name))
        return names

    def _handler(
        self, remote_name: str
    ) -> Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]:
        async def call(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            result = await self.call_tool(
                remote_name, args, meta={"run_id": ctx.run_id, "agent_id": ctx.agent_id}
            )
            texts = [
                str(item.get("text", ""))
                for item in result.get("content", [])
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            if result.get("isError"):
                return ToolResult(
                    name=self.tool_name(remote_name),
                    success=False,
                    error="；".join(texts) or "MCP 工具执行失败",
                )
            content = result.get("structuredContent")
            if not isinstance(content, dict):
                try:
                    parsed = json.loads(texts[0]) if texts else {}
                except ValueError:
                    parsed = {"text": "\n".join(texts)}
                content = parsed if isinstance(parsed, dict) else {"value": parsed}
            return ToolResult(name=self.tool_name(remote_name), success=True, content=content)

        return call
