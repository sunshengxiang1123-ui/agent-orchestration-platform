"""Agent 团队定义查询接口：展示从 YAML 加载、并实际注册到调度器的角色卡。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from apps.api.deps import runtime_of

router = APIRouter(prefix="/api/agent-catalog", tags=["agents"])


@router.get("")
async def agent_catalog(request: Request) -> dict[str, object]:
    runtime = runtime_of(request)
    items = [spec.model_dump() for spec in runtime.agent_catalog.values()]
    return {
        "items": items,
        "count": len(items),
        "source": "packages/agents/defs",
        "note": "启动时由这些 YAML 注册内置团队并参与调度；修改后递增 version 即上线新版本",
    }


@router.get("/{spec_id}")
async def agent_spec(spec_id: str, request: Request) -> dict[str, object]:
    for spec in runtime_of(request).agent_catalog.values():
        if spec.id == spec_id:
            return spec.model_dump()
    raise HTTPException(404, "Agent 定义不存在")
