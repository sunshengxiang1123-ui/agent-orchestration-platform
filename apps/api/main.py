"""同源本地平台：真实运行接口、状态监控、角色管理与原生前端。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from apps.api.deps import runtime_of
from apps.api.routers.agents import router as agent_catalog_router
from apps.api.routers.knowledge import router as knowledge_router
from apps.api.routers.settings import router as settings_router
from packages.application.agents import AgentDefinition
from packages.application.runtime import PlatformRuntime
from packages.application.travel import (
    CATALOG,
    SOURCE_NOTE,
    StrictModel,
    TravelRequest,
    assurance_workflow,
    travel_workflow,
    workflow_source,
)
from packages.common.config import Settings
from packages.llm.deepseek import ModelConfigurationError
from packages.mcp.server import INVALID_REQUEST, PARSE_ERROR, PROTOCOL_VERSION, error_response

ROOT = Path(__file__).resolve().parents[2]


class ResumeConfirmation(StrictModel):
    confirm_paid: bool = Field(strict=True)


class HumanDecision(StrictModel):
    approved: bool = Field(strict=True)
    comment: str = Field(default="", max_length=1000)


def create_app(runtime: PlatformRuntime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        instance = runtime
        if instance is None:
            import asyncio
            import os

            # 容器部署时用环境变量把数据库和 .env 放到挂载卷里。
            reports = Path(os.environ.get("PLATFORM_DATA_DIR") or ROOT / "reports")
            env_path = Path(os.environ.get("PLATFORM_ENV_FILE") or ROOT / ".env")
            await asyncio.to_thread(reports.mkdir, parents=True, exist_ok=True)
            instance = PlatformRuntime(
                Settings(_env_file=env_path),
                database_url="sqlite+aiosqlite:///" + (reports / "platform.db").as_posix(),
                env_path=env_path,
            )
        application.state.runtime = instance
        await instance.start()
        try:
            yield
        finally:
            await instance.close()

    application = FastAPI(title="Agent 编排调度平台", version="0.2.0", lifespan=lifespan)
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver", "offline.test"],
    )

    @application.middleware("http")
    async def same_origin(request: Request, call_next: RequestResponseEndpoint) -> Response:
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or parsed.netloc != request.headers.get(
                "host"
            ):
                return JSONResponse({"detail": "本地平台拒绝跨站写入"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/config")
    async def configuration(request: Request) -> dict[str, object]:
        return runtime_of(request).model_status()

    @application.get("/api/tools/catalog")
    async def catalog() -> dict[str, object]:
        return {"items": CATALOG, "source_note": SOURCE_NOTE}

    @application.get("/api/mcp/tools")
    async def mcp_tools(request: Request) -> dict[str, object]:
        instance = runtime_of(request)
        return {
            "endpoint": "/mcp",
            "transport": "Streamable HTTP（JSON 响应）；平台内部为进程内传输",
            "server": instance.mcp_client.server_info,
            "tools": await instance.mcp_client.list_tools(),
            "calls": instance.mcp_server.calls,
        }

    @application.post("/mcp")
    async def mcp_endpoint(request: Request) -> Response:
        """MCP Streamable HTTP 端点（只用 JSON 响应，不开 SSE）；同源中间件已校验 Origin。"""
        try:
            message = await request.json()
        except ValueError:
            return JSONResponse(error_response(None, PARSE_ERROR, "JSON 解析失败"), 400)
        if isinstance(message, list):
            return JSONResponse(error_response(None, INVALID_REQUEST, "不支持批量请求"), 400)
        response = await runtime_of(request).mcp_server.handle_message(message)
        if response is None:
            return Response(status_code=202)
        return JSONResponse(response, headers={"MCP-Protocol-Version": PROTOCOL_VERSION})

    @application.get("/mcp")
    async def mcp_stream() -> Response:
        return Response(status_code=405, headers={"Allow": "POST"})

    @application.get("/api/workflows/travel")
    async def workflow() -> dict[str, object]:
        return {
            "definition": travel_workflow().model_dump(),
            "subflows": {"assurance": assurance_workflow().model_dump()},
            "source": {
                "travel.yaml": workflow_source("travel"),
                "assurance.yaml": workflow_source("assurance"),
            },
        }

    @application.get("/api/agents")
    async def list_agents(
        request: Request, role: Literal["planner", "reviewer"] | None = None
    ) -> list[dict[str, object]]:
        scheduler = runtime_of(request).scheduler
        values = scheduler.candidates(role) if role else list(scheduler.agents.values())
        return [
            {
                **agent.model_dump(),
                "key": agent.key,
                "load": scheduler.loads.get(agent.key, 0),
                "history": {
                    "samples": scheduler.profiles.get(agent.key, agent.role, "travel").samples,
                    "success_rate": scheduler.profiles.get(
                        agent.key, agent.role, "travel"
                    ).success_rate,
                },
            }
            for agent in values
        ]

    @application.post("/api/agents", status_code=201)
    async def register_agent(value: AgentDefinition, request: Request) -> dict[str, object]:
        try:
            await runtime_of(request).register(value)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return value.model_dump()

    @application.get("/api/runs")
    async def list_runs(request: Request) -> list[dict[str, object]]:
        values = await runtime_of(request).store.list_documents("run:")
        return sorted(values, key=lambda item: item["created_at"], reverse=True)[:100]

    @application.post("/api/runs", status_code=202)
    async def submit(value: TravelRequest, request: Request) -> dict[str, object]:
        try:
            return await runtime_of(request).submit(value)
        except ModelConfigurationError as exc:
            raise HTTPException(503, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    @application.get("/api/runs/{run_id}/trace")
    async def trace(run_id: str, request: Request) -> dict[str, object]:
        value = await runtime_of(request).trace(run_id)
        if value is None:
            raise HTTPException(404, "运行不存在")
        return value

    @application.get("/api/runs/{run_id}/events")
    async def events(
        run_id: str, request: Request, after: int = Query(default=0, ge=0)
    ) -> list[dict[str, object]]:
        instance = runtime_of(request)
        if await instance.store.document("run:" + run_id) is None:
            raise HTTPException(404, "运行不存在")
        return await instance.store.event_list(run_id, after)

    @application.post("/api/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str, value: ResumeConfirmation, request: Request) -> dict[str, object]:
        if not value.confirm_paid:
            raise HTTPException(400, "续跑会继续调用真实模型，必须确认费用")
        try:
            return await runtime_of(request).resume(run_id)
        except ModelConfigurationError as exc:
            raise HTTPException(503, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @application.post("/api/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request) -> dict[str, str]:
        try:
            await runtime_of(request).cancel(run_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"status": "cancelled"}

    @application.get("/api/human-tasks")
    async def human_tasks(request: Request) -> list[dict[str, object]]:
        return [
            value
            for value in await runtime_of(request).store.list_documents("run:")
            if value["status"] == "awaiting_human"
        ]

    @application.post("/api/runs/{run_id}/review")
    async def review(run_id: str, value: HumanDecision, request: Request) -> dict[str, object]:
        try:
            return await runtime_of(request).resolve(run_id, value.approved, value.comment)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    application.include_router(knowledge_router)
    application.include_router(agent_catalog_router)
    application.include_router(settings_router)
    application.mount("/assets", StaticFiles(directory=ROOT / "apps/web/src"), name="assets")

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(ROOT / "apps/web/index.html")

    return application


app = create_app()
