"""旅游领域的 MCP 工具服务：目录、用户约束、知识库检索与确定性行程校验。

这些工具注册在服务端 ToolRegistry 中，经 ``packages.mcp.MCPServer`` 以 MCP 协议
暴露；平台内的 Agent 与工作流节点只通过 MCP 客户端调用它们。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from packages.application.travel import (
    CATALOG,
    MEAL_COST,
    SOURCE_NOTE,
    TRANSFER_COST,
    TRANSFER_MINUTES,
    Itinerary,
    TravelRequest,
    verify_plan,
)
from packages.core.models.io import ToolContext, ToolResult
from packages.tools.registry import ToolRegistry, ToolSpec

if TYPE_CHECKING:
    from packages.application.runtime import PlatformRuntime

SERVER_NAME = "travel-tools"
INSTRUCTIONS = (
    "旅游规划工具：catalog 返回固定景点目录；constraints 返回某次运行的用户约束；"
    "knowledge_search 检索平台知识库；verify_plan 按目录确定性核算行程。"
)

_RUN_ID = {"type": "string", "description": "平台运行 ID"}


def _request_for(runtime: PlatformRuntime, args: dict[str, Any]) -> TravelRequest:
    """优先用显式传入的 request（外部客户端），否则按 run_id 取运行中的请求。"""
    if isinstance(args.get("request"), dict):
        return TravelRequest.model_validate({**args["request"], "confirm_paid": False})
    entry = runtime.entries.get(str(args.get("run_id", "")))
    if entry is None:
        raise ValueError("运行不存在或已不在当前进程；外部调用请直接传 request")
    return entry.request


def build_travel_registry(runtime: PlatformRuntime) -> ToolRegistry:
    registry = ToolRegistry()

    def catalog(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return ToolResult(
            name="catalog", success=True, content={"items": CATALOG, "source_note": SOURCE_NOTE}
        )

    def constraints(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        request = _request_for(runtime, args)
        return ToolResult(
            name="constraints",
            success=True,
            content={
                **request.model_dump(exclude={"confirm_paid"}),
                "meal_cost": MEAL_COST,
                "transfer_cost": TRANSFER_COST,
                "transfer_minutes": TRANSFER_MINUTES,
            },
        )

    async def knowledge_search(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        top_k = int(args.get("top_k", 3))
        if not 1 <= top_k <= 10:
            raise ValueError("top_k 必须在 1 到 10 之间")
        query = str(args["query"]).strip()[:500]
        hits = await runtime.knowledge.search(query, top_k=top_k)
        return ToolResult(
            name="knowledge_search",
            success=True,
            content={
                "query": query,
                "hits": [
                    {
                        "title": hit.title,
                        "chunk_id": hit.chunk_id,
                        "content": hit.content[:600],
                        "score": hit.score,
                        "tags": hit.tags,
                    }
                    for hit in hits
                ],
            },
        )

    def check_plan(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        plan = Itinerary.model_validate(args["plan"])
        return ToolResult(
            name="verify_plan",
            success=True,
            content=verify_plan(plan, _request_for(runtime, args)),
        )

    specs = [
        ToolSpec(
            name="catalog",
            description="返回固定景点目录（票价、游玩时长、营业时间、标签）。只读。",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=catalog,
        ),
        ToolSpec(
            name="constraints",
            description="返回某次运行的用户约束及固定餐饮、交通成本。传 run_id 或 request。",
            input_schema={
                "type": "object",
                "properties": {"run_id": _RUN_ID, "request": {"type": "object"}},
                "additionalProperties": False,
            },
            cacheable=False,
            handler=constraints,
        ),
        ToolSpec(
            name="knowledge_search",
            description="在平台知识库中检索（BM25 + 词形哈希向量融合），返回最相关片段。",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索语句"},
                    "top_k": {"type": "integer", "description": "返回条数，1-10"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            cacheable=False,
            handler=knowledge_search,
        ),
        ToolSpec(
            name="verify_plan",
            description="按目录确定性核算行程的费用、时间、兴趣与必去景点，返回错误列表。",
            input_schema={
                "type": "object",
                "properties": {
                    "plan": {"type": "object", "description": "{stops:[...], rationale}"},
                    "run_id": _RUN_ID,
                    "request": {"type": "object"},
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
            cacheable=False,
            handler=check_plan,
        ),
    ]
    for spec in specs:
        registry.register(spec)
    return registry
