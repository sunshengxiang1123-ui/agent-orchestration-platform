"""MCP 协议：握手、工具发现、调用、错误码，以及客户端白名单与不缓存。"""

from types import SimpleNamespace
from typing import Any

from packages.application.mcp_tools import SERVER_NAME, build_travel_registry
from packages.core.models.io import ToolContext
from packages.mcp.client import InProcessTransport, MCPClient
from packages.mcp.server import METHOD_NOT_FOUND, PROTOCOL_VERSION, MCPServer
from packages.rag.knowledge import KnowledgeBase, KnowledgeInput
from packages.tools.registry import ToolRegistry
from tests.helpers import GOOD_PLAN
from tests.memory_store import MemoryDocuments

REQUEST = {"budget": 200, "min_stops": 3}


async def make_server() -> tuple[MCPServer, Any]:
    knowledge = KnowledgeBase(MemoryDocuments())
    await knowledge.load()
    runtime = SimpleNamespace(knowledge=knowledge, entries={})
    registry = build_travel_registry(runtime)  # type: ignore[arg-type]
    return MCPServer(registry, name=SERVER_NAME), runtime


async def rpc(server: MCPServer, method: str, params: dict[str, Any] | None = None) -> Any:
    return await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )


async def test_initialize_and_list_tools() -> None:
    server, _ = await make_server()
    init = await rpc(server, "initialize", {"protocolVersion": PROTOCOL_VERSION})
    assert init["result"]["serverInfo"]["name"] == SERVER_NAME
    assert init["result"]["capabilities"] == {"tools": {"listChanged": False}}
    tools = (await rpc(server, "tools/list"))["result"]["tools"]
    assert {tool["name"] for tool in tools} == {
        "catalog",
        "constraints",
        "knowledge_search",
        "verify_plan",
    }
    assert all(tool["inputSchema"]["type"] == "object" for tool in tools)


async def test_protocol_errors() -> None:
    server, _ = await make_server()
    assert (await rpc(server, "resources/list"))["error"]["code"] == METHOD_NOT_FOUND
    invalid = await server.handle_message({"id": 1})
    assert invalid is not None and invalid["error"]["code"] == -32600
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert await server.handle_message(notification) is None


async def test_verify_plan_tool_with_explicit_request() -> None:
    server, _ = await make_server()
    arguments = {"plan": GOOD_PLAN, "request": REQUEST}
    response = await rpc(server, "tools/call", {"name": "verify_plan", "arguments": arguments})
    result = response["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["passed"] is True
    assert result["content"][0]["type"] == "text"


async def test_tool_errors_are_reported_as_is_error() -> None:
    server, _ = await make_server()
    missing = await rpc(server, "tools/call", {"name": "knowledge_search", "arguments": {}})
    assert missing["result"]["isError"] is True
    unknown_run = await rpc(
        server, "tools/call", {"name": "constraints", "arguments": {"run_id": "x"}}
    )
    assert unknown_run["result"]["isError"] is True


async def test_client_enforces_whitelist_and_does_not_cache() -> None:
    server, runtime = await make_server()
    client = MCPClient("travel", InProcessTransport(server))
    await client.initialize()
    registry = ToolRegistry()
    names = await client.register_tools(registry)
    assert "mcp:travel:knowledge_search" in names

    def ctx(*allowed: str) -> ToolContext:
        return ToolContext(
            run_id="r1",
            agent_id="tester",
            allowed_tools=[client.tool_name(name) for name in allowed],
            permissions=["read"],
        )

    denied = await registry.call("mcp:travel:catalog", {}, ctx=ctx("constraints"))
    assert not denied.success and "白名单" in (denied.error or "")

    args = {"query": "雨天 安排", "top_k": 3}
    first = await registry.call("mcp:travel:knowledge_search", args, ctx=ctx("knowledge_search"))
    assert first.success and first.content["hits"] == []
    await runtime.knowledge.add(KnowledgeInput(title="雨天", content="雨天安排室内的博物馆。"))
    second = await registry.call("mcp:travel:knowledge_search", args, ctx=ctx("knowledge_search"))
    assert [hit["title"] for hit in second.content["hits"]] == ["雨天"]
    assert server.calls == 2
