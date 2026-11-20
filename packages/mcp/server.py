"""MCP 服务端：把 ToolRegistry 中的工具按 MCP 协议（JSON-RPC 2.0）暴露。

支持 ``initialize`` / ``notifications/initialized`` / ``ping`` / ``tools/list`` /
``tools/call``。传输层与协议解耦：平台内部用进程内传输，外部客户端经
``POST /mcp``（Streamable HTTP 的 JSON 响应模式）接入。
"""

from __future__ import annotations

import json
from typing import Any

from packages.core.models.io import ToolContext
from packages.tools.registry import ToolRegistry

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class MCPError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def error_response(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


class MCPServer:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        name: str = "platform-tools",
        version: str = "1.0.0",
        instructions: str = "",
    ) -> None:
        self.registry = registry
        self.name, self.version, self.instructions = name, version, instructions
        self.calls = 0

    async def handle_message(self, message: Any) -> dict[str, Any] | None:
        """处理一条 JSON-RPC 消息；通知（无 id）返回 None。"""
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
        ):
            return error_response(
                message.get("id") if isinstance(message, dict) else None,
                INVALID_REQUEST,
                "不是合法的 JSON-RPC 2.0 请求",
            )
        method = message["method"]
        params = message.get("params") or {}
        if "id" not in message:
            return None  # 通知：notifications/initialized 等无需响应
        message_id = message["id"]
        if not isinstance(params, dict):
            return error_response(message_id, INVALID_PARAMS, "params 必须是对象")
        try:
            result = await self._dispatch(method, params)
        except MCPError as exc:
            return error_response(message_id, exc.code, str(exc))
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            version = requested if requested in SUPPORTED_VERSIONS else PROTOCOL_VERSION
            result: dict[str, Any] = {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
            if self.instructions:
                result["instructions"] = self.instructions
            return result
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": item["name"],
                        "description": item["description"],
                        "inputSchema": item["input_schema"],
                    }
                    for item in self.registry.schema()
                ]
            }
        if method == "tools/call":
            return await self._call_tool(params)
        raise MCPError(METHOD_NOT_FOUND, f"不支持的 MCP 方法：{method}")

    async def _call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise MCPError(INVALID_PARAMS, "tools/call 需要字符串 name 和对象 arguments")
        raw_meta = params.get("_meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        self.calls += 1
        # 服务端只负责执行与参数校验；“哪个 Agent 能用哪个工具”由客户端注册表的白名单决定。
        result = await self.registry.call(
            name,
            arguments,
            ctx=ToolContext(
                run_id=str(meta.get("run_id", "mcp")),
                agent_id=str(meta.get("agent_id", "mcp-client")),
                allowed_tools=[name],
                permissions=["read", "compute"],
            ),
        )
        if not result.success:
            return {
                "content": [{"type": "text", "text": result.error or "工具执行失败"}],
                "isError": True,
            }
        return {
            "content": [{"type": "text", "text": json.dumps(result.content, ensure_ascii=False)}],
            "structuredContent": result.content,
            "isError": False,
        }
