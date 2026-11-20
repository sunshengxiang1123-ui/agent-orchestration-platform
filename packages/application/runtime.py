"""单机平台运行时：优先级队列、持久化、可取消任务和人工最终审批。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import event

from packages.agents.loader import load_agent_catalog
from packages.agents.spec import AgentSpec
from packages.application.agents import AgentDefinition, AgentScheduler, default_agents
from packages.application.checkpoint import RuntimeCheckpointStore
from packages.application.mcp_tools import INSTRUCTIONS, SERVER_NAME, build_travel_registry
from packages.application.store import RuntimeStore
from packages.application.travel import (
    CALLS_PER_ITERATION,
    RUN_TOKEN_BUDGET,
    TravelRequest,
    assurance_workflow,
    travel_workflow,
)
from packages.common.config import Settings
from packages.common.db import create_engine, create_session_factory
from packages.common.envfile import update_env_file
from packages.core.engine.executor import Executor
from packages.core.models.run import Run
from packages.core.scheduler.admission import AdmissionController
from packages.core.scheduler.priority import PriorityCalculator, PriorityNode
from packages.llm.cache import SQLCallStore
from packages.llm.deepseek import (
    ModelConfigurationError,
    check_connection,
    validate_deepseek_settings,
)
from packages.mcp.client import InProcessTransport, MCPClient
from packages.mcp.server import MCPServer
from packages.rag.knowledge import KnowledgeBase
from packages.rag.seeding import seed_knowledge
from packages.tools.registry import ToolRegistry


@dataclass
class LiveRun:
    run: Run
    request: TravelRequest
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    phase: str = "queued"
    plan: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    feedback: list[str] = field(default_factory=list)
    review: dict[str, Any] | None = None
    calls: int = 0
    iteration: int = 0
    decision: dict[str, Any] | None = None
    error: str | None = None
    source: str = "live"
    # 服务停止或崩溃中断的运行可以从 checkpoint 继续；用户主动取消的不可以。
    resumable: bool = False
    resumes: int = 0

    def view(self) -> dict[str, Any]:
        return {
            "run_id": self.run.id,
            "status": self.phase if self.phase == "queued" else self.run.status,
            "mode": self.source,
            "inputs": self.request.model_dump(exclude={"confirm_paid"}),
            "created_at": self.created_at.isoformat(),
            "plan": self.plan,
            "validation": self.validation,
            "review": self.review,
            "feedback": self.feedback,
            "calls": self.calls,
            "call_limit": CALLS_PER_ITERATION * self.request.max_iterations,
            "spent_tokens": self.run.spent_tokens,
            "iteration": self.iteration,
            "decision": self.decision,
            "error": self.error,
            "resumable": self.resumable,
            "resumes": self.resumes,
        }


class PlatformRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        database_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
        env_path: Path | None = None,
    ) -> None:
        self.settings, self.transport = settings, transport
        # 模型配置页面保存时写回的 .env；为 None 时只改内存（测试场景）。
        self.env_path = env_path
        self.engine = create_engine(database_url)
        if self.engine.dialect.name == "sqlite":
            event.listen(self.engine.sync_engine, "connect", self._sqlite_options)
        self.factory = create_session_factory(self.engine)
        self.store = RuntimeStore(self.engine, self.factory)
        self.knowledge = KnowledgeBase(self.store)
        # 工具层：服务端注册表经 MCP 暴露；Agent 侧注册表只含经 MCP 发现的远端工具。
        self.mcp_server = MCPServer(
            build_travel_registry(self),
            name=SERVER_NAME,
            version="1.0.0",
            instructions=INSTRUCTIONS,
        )
        self.mcp_client = MCPClient("travel", InProcessTransport(self.mcp_server))
        self.tools = ToolRegistry()
        # 经 MCP tools/list 发现的工具声明，用来生成发给模型的 function 定义。
        self.mcp_tools: list[dict[str, Any]] = []
        self.call_store = SQLCallStore(self.factory)
        self.checkpoints = RuntimeCheckpointStore(self.factory)
        self.scheduler = AgentScheduler()
        self.agent_catalog: dict[str, AgentSpec] = {}
        self.admission = AdmissionController(cpu_limit=2, default_container_limit=2)
        self.entries: dict[str, LiveRun] = {}
        self.pending: dict[str, PriorityNode] = {}
        self.jobs: dict[str, asyncio.Task[None]] = {}
        self.workers: list[asyncio.Task[None]] = []
        self.wake = asyncio.Event()
        self.agent_changed = asyncio.Event()
        self.mutations = asyncio.Lock()
        self.persistence = asyncio.Lock()
        self.closing = False
        self.dispatch_counter = 0

    @staticmethod
    def _sqlite_options(connection: Any, record: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    async def start(self) -> None:
        await self.store.initialize()
        specs = load_agent_catalog()
        self.agent_catalog = {spec.key: spec for spec in specs}
        await self.knowledge.load()
        # 每次启动把种子文档同步到最新版本（缺失的补上、改过的更新），用户文档不动。
        await seed_knowledge(self.knowledge)
        await self.mcp_client.initialize()
        await self.mcp_client.register_tools(self.tools)
        self.mcp_tools = await self.mcp_client.list_tools()
        for value in await self.store.agents():
            agent = AgentDefinition.model_validate(value)
            self.scheduler.agents[agent.key] = agent
        for agent in default_agents(specs):
            if agent.key not in self.scheduler.agents:
                await self.register(agent)
        saved = await self.store.document("profiles")
        if saved:
            self.scheduler.restore_profiles(saved)
        for previous in await self.store.list_documents("run:"):
            if previous["status"] in {"running", "queued"}:
                previous.update(
                    status="cancelled",
                    error="服务重启中断；不会自动重发收费请求，可手动从断点继续",
                    resumable=True,
                )
                await self.store.save_document("run:" + previous["run_id"], previous)
                await self.store.change_status(previous["run_id"], "cancelled")
        await self.store.save_workflow(travel_workflow())
        await self.store.save_workflow(assurance_workflow())
        self.workers = [asyncio.create_task(self._worker()) for _ in range(2)]

    async def close(self) -> None:
        self.closing = True
        async with self.mutations:
            for run_id in list(self.pending):
                self.pending.pop(run_id)
                entry = self.entries[run_id]
                entry.phase, entry.run.status = "finished", "cancelled"
                entry.error, entry.resumable = "服务停止时仍在排队，可从断点继续", True
                await self.persist(entry)
        for task in list(self.jobs.values()):
            task.cancel()
        self.wake.set()
        await asyncio.gather(*self.workers, return_exceptions=True)
        await self.engine.dispose()

    def model_status(self) -> dict[str, Any]:
        try:
            validate_deepseek_settings(self.settings)
            ready, reason = True, "已配置；启动任务前仍需逐次确认费用"
        except ModelConfigurationError as exc:
            ready, reason = False, str(exc)
        return {
            "ready": ready,
            "reason": reason,
            "model": self.settings.llm_model,
            "base_url": self.settings.llm_base_url,
            "storage": "本地 SQLite；重启保留审计，中断任务不会自动重发",
            "execution": "真实模型" if self.transport is None else "测试注入传输",
        }

    async def register(self, agent: AgentDefinition) -> None:
        async with self.mutations:
            if agent.key in self.scheduler.agents:
                raise ValueError("该 Agent 版本已存在；修改 Prompt 请注册新版本")
            if len(self.scheduler.agents) >= 100:
                raise ValueError("单机原型最多保存 100 个 Agent 版本")
            await self.store.add_agent(agent.model_dump())
            self.scheduler.agents[agent.key] = agent
            self.agent_changed.set()

    async def submit(self, request: TravelRequest) -> dict[str, Any]:
        if not request.confirm_paid:
            raise ValueError("必须明确确认本次真实模型调用费用")
        validate_deepseek_settings(self.settings)
        if any(not self.scheduler.candidates(role) for role in ("planner", "reviewer")):
            raise ValueError("必须注册可用的规划与审核角色后才能运行")
        async with self.mutations:
            if len(self.pending) + len(self.jobs) >= 20:
                raise ValueError("运行队列已满，请稍后重试")
            run = Run(
                id=uuid4().hex,
                workflow_id="travel",
                workflow_version=travel_workflow().version,
                status="running",
                inputs=request.model_dump(exclude={"confirm_paid"}),
                seed=42,
                mode="live",
                budget_tokens=RUN_TOKEN_BUDGET,
            )
            entry = LiveRun(run, request, source="live" if self.transport is None else "test")
            await self.store.save_run(run, create=True)
            self.entries[run.id] = entry
            await self.persist(entry)
            self.pending[run.id] = PriorityNode(
                id=run.id,
                urgency=request.priority,
                duration=request.max_iterations * 2,
                queued_at=entry.created_at,
            )
            await self.emit(entry, "queued", {"priority": request.priority})
            self.wake.set()
            return entry.view()

    async def persist(self, entry: LiveRun) -> None:
        async with self.persistence:
            await self.store.save_run(entry.run)
            await self.store.save_document("run:" + entry.run.id, entry.view())

    async def emit(self, entry: LiveRun, kind: str, data: dict[str, Any]) -> None:
        await self.store.add_event(
            entry.run.id,
            {
                "kind": kind,
                "at": datetime.now(UTC).isoformat(),
                "iteration": entry.iteration,
                "data": data,
            },
        )

    async def trace(self, run_id: str) -> dict[str, Any] | None:
        value = await self.store.document("run:" + run_id)
        if value is not None:
            value["nodes"] = await self.store.nodes(run_id)
            value["events"] = await self.store.event_list(run_id)
        return value

    async def cancel(self, run_id: str) -> None:
        async with self.mutations:
            await self._cancel(run_id)

    async def _cancel(self, run_id: str) -> None:
        entry = self.entries.get(run_id)
        if entry is None:
            raise ValueError("此运行不在当前进程中；已结束或重启中断")
        if entry.run.status not in {"running", "awaiting_human"}:
            raise ValueError("运行已结束")
        task = self.jobs.get(run_id)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if entry.run.status in {"running", "awaiting_human"}:
                entry.phase, entry.run.status = "finished", "cancelled"
                await self.persist(entry)
        else:
            self.pending.pop(run_id, None)
            entry.phase, entry.run.status = "finished", "cancelled"
            await self.emit(entry, "cancelled", {"reason": "用户取消；已发送请求可能已计费"})
            await self.persist(entry)

    async def resume(self, run_id: str) -> dict[str, Any]:
        """从 checkpoint 继续被服务中断的运行：已成功的节点直接恢复，不再调用模型。"""
        validate_deepseek_settings(self.settings)
        async with self.mutations:
            current = self.entries.get(run_id)
            if current is not None and current.phase != "finished":
                raise ValueError("运行仍在进行中")
            value = await self.store.document("run:" + run_id)
            if value is None or not value.get("resumable"):
                raise ValueError("只有被服务停止或重启中断的运行可以从断点继续")
            if len(self.pending) + len(self.jobs) >= 20:
                raise ValueError("运行队列已满，请稍后重试")
            run = await self.store.run(run_id)
            if run is None:
                raise ValueError("运行记录缺失，无法续跑")
            run.status = "running"
            request = TravelRequest.model_validate({**value["inputs"], "confirm_paid": True})
            entry = LiveRun(
                run,
                request,
                created_at=datetime.fromisoformat(value["created_at"]),
                plan=value.get("plan"),
                validation=value.get("validation"),
                feedback=list(value.get("feedback") or []),
                review=value.get("review"),
                calls=int(value.get("calls") or 0),
                iteration=int(value.get("iteration") or 0),
                source=str(value.get("mode") or "live"),
                resumes=int(value.get("resumes") or 0) + 1,
            )
            self.entries[run_id] = entry
            await self.persist(entry)
            completed = await self.checkpoints.completed(run_id)
            self.pending[run_id] = PriorityNode(
                id=run_id,
                urgency=request.priority,
                duration=request.max_iterations * 2,
                queued_at=datetime.now(UTC),
            )
            await self.emit(
                entry,
                "resumed",
                {"resumes": entry.resumes, "completed_nodes": len(completed)},
            )
            self.wake.set()
            return entry.view()

    async def resolve(self, run_id: str, approved: bool, comment: str) -> dict[str, Any]:
        async with self.mutations:
            value = await self.store.document("run:" + run_id)
            if value is None or value["status"] != "awaiting_human":
                raise ValueError("运行不处于待人工审批状态")
            if not (value.get("validation") or {}).get("passed") or not (
                value.get("review") or {}
            ).get("approved"):
                raise ValueError("自动验证未通过，不能人工标为成功")
            status = "succeeded" if approved else "failed"
            decision = {
                "approved": approved,
                "comment": comment,
                "at": datetime.now(UTC).isoformat(),
            }
            value.update(status=status, decision=decision)
            entry = self.entries.get(run_id)
            if entry is not None:
                entry.run.status = "succeeded" if approved else "failed"
                entry.decision = decision
                await self.persist(entry)
            else:
                await self.store.save_document("run:" + run_id, value)
                await self.store.change_status(run_id, status)
            await self.store.add_event(
                run_id, {"kind": "human_review", "at": decision["at"], "data": decision}
            )
            return value

    async def _worker(self) -> None:
        while not self.closing:
            if not self.pending:
                self.wake.clear()
                await self.wake.wait()
                continue
            ranked = PriorityCalculator().rank(list(self.pending.values()))
            node, score = ranked[0]
            self.pending.pop(node.id)
            entry = self.entries[node.id]
            entry.phase = "running"
            self.dispatch_counter += 1
            task = asyncio.create_task(self._execute(entry, score, self.dispatch_counter))
            self.jobs[node.id] = task
            try:
                await task
            except asyncio.CancelledError:
                pass
            finally:
                self.jobs.pop(node.id, None)

    async def _execute(self, entry: LiveRun, score: float, dispatch_order: int) -> None:
        from packages.application.handler import TravelHandler

        try:
            await self.emit(
                entry,
                "scheduled",
                {
                    "priority_score": score,
                    "dispatch_order": dispatch_order,
                    "policy": "关键路径估计+紧急度+等待老化",
                },
            )
            await self.persist(entry)
            handler = TravelHandler(self, entry)
            executor = Executor(handler, checkpoints=self.checkpoints)
            workflow = travel_workflow(entry.request.max_iterations)
            subflows = {"assurance": assurance_workflow()}
            async with asyncio.timeout(300):
                if entry.resumes:
                    # 续跑：成功节点从 checkpoint 恢复，只执行未完成的部分。
                    await executor.resume(workflow, entry.run, subflows=subflows)
                else:
                    await executor.run(workflow, entry.run, subflows=subflows)
            if entry.run.status in {"succeeded", "awaiting_human"} and not (
                (entry.validation or {}).get("passed") and (entry.review or {}).get("approved")
            ):
                entry.run.status = "failed"
                entry.error = "未完成完整验证链，拒绝标记为成功"
            if entry.run.status == "failed" and entry.error is None:
                entry.error = "验证未通过或执行失败，已到停止条件；详见 Trace"
        except asyncio.CancelledError:
            entry.run.status = "cancelled"
            if self.closing:
                entry.error, entry.resumable = "服务停止中断，可从断点继续", True
            else:
                entry.error = "用户取消"
        except TimeoutError:
            entry.run.status, entry.error = "failed", "运行超过 300 秒上限，未自动重试"
        except Exception:
            entry.run.status, entry.error = "failed", "运行内部异常；未将结果标为成功"
        finally:
            entry.phase = "finished"
            entry.run.spent_tokens = await self.store.token_total(entry.run.id)
            await self.persist(entry)
            await self.emit(entry, "finished", {"status": entry.run.status, "error": entry.error})

    async def remember_performance(self) -> None:
        async with self.persistence:
            await self.store.save_document("profiles", self.scheduler.export_profiles())

    def model_settings(self) -> dict[str, Any]:
        """给配置页面的视图；永远不返回密钥本身，只返回首尾几位用于辨认。"""
        key = self.settings.llm_api_key.get_secret_value()
        hint = (key[:3] + "****" + key[-4:]) if len(key) >= 12 else ("已设置" if key else "")
        return {
            "key_configured": bool(key),
            "key_hint": hint,
            "base_url": self.settings.llm_base_url,
            "model": self.settings.llm_model,
            "max_tokens": self.settings.llm_max_tokens,
            "timeout_seconds": self.settings.llm_timeout_seconds,
            "env_file": self.env_path.name if self.env_path else None,
            **self.model_status(),
        }

    def preview_settings(self, update: dict[str, Any]) -> Settings:
        """合并页面提交的字段；空密钥表示沿用当前密钥。"""
        merged: Settings = self.settings.model_copy(update=update)
        return merged

    async def save_model_settings(self, settings: Settings) -> dict[str, Any]:
        async with self.mutations:
            if self.env_path is not None:
                await asyncio.to_thread(
                    update_env_file,
                    self.env_path,
                    {
                        "DEEPSEEK_API_KEY": settings.llm_api_key.get_secret_value(),
                        "PLATFORM_LLM_BASE_URL": settings.llm_base_url,
                        "PLATFORM_LLM_MODEL": settings.llm_model,
                        "PLATFORM_LLM_MAX_TOKENS": str(settings.llm_max_tokens),
                        "PLATFORM_LLM_TIMEOUT_SECONDS": str(settings.llm_timeout_seconds),
                    },
                    # 别名优先级更高，保留旧行会让重启后读到旧密钥。
                    remove=("PLATFORM_LLM_API_KEY",),
                )
            # 已在执行的节点持有自己的设置副本；新的模型调用使用新配置。
            self.settings = settings
        return self.model_settings()

    async def test_model_settings(self, settings: Settings) -> dict[str, Any]:
        return await check_connection(settings, transport=self.transport)
