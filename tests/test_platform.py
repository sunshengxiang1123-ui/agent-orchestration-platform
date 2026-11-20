"""端到端：真实运行时 + SQLite + MCP 工具 + 知识库，模型服务用 MockTransport 替身。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from packages.application.runtime import PlatformRuntime
from packages.application.travel import TravelRequest
from packages.common.config import Settings
from packages.core.models.io import ToolContext
from packages.llm.deepseek import ModelConfigurationError
from tests.helpers import (
    BAD_PLAN,
    GOOD_PLAN,
    TEST_KEY,
    FakeModel,
    is_reviewer,
    offered_tools,
)


def settings() -> Settings:
    return Settings(_env_file=None, llm_api_key=SecretStr(TEST_KEY))


Starter = Callable[[FakeModel], Awaitable[PlatformRuntime]]


@pytest.fixture
async def platform(tmp_path: Path) -> AsyncIterator[Starter]:
    created: list[PlatformRuntime] = []

    async def start(model: FakeModel) -> PlatformRuntime:
        runtime = PlatformRuntime(
            settings(),
            database_url="sqlite+aiosqlite:///" + (tmp_path / "platform.db").as_posix(),
            transport=model.transport(),
            env_path=tmp_path / ".env",
        )
        await runtime.start()
        created.append(runtime)
        return runtime

    yield start
    for runtime in created:
        await runtime.close()


async def finish(runtime: PlatformRuntime, run_id: str) -> dict[str, Any]:
    async with asyncio.timeout(20):
        while True:
            trace = await runtime.trace(run_id)
            assert trace is not None
            ended = [event["kind"] for event in trace["events"]]
            # 续跑后会再产生一次 finished，需比 resumed 多才算本次结束
            if trace["status"] not in {"queued", "running"} and ended.count(
                "finished"
            ) > ended.count("resumed"):
                return trace
            await asyncio.sleep(0.05)


def kinds(trace: dict[str, Any]) -> list[str]:
    return [event["kind"] for event in trace["events"]]


async def submit(runtime: PlatformRuntime, **values: Any) -> dict[str, Any]:
    run = await runtime.submit(TravelRequest(confirm_paid=True, **values))
    return await finish(runtime, run["run_id"])


async def search(runtime: PlatformRuntime, query: str) -> list[str]:
    """按 Agent 的方式经 MCP 检索知识库，返回命中文档标题。"""
    name = runtime.mcp_client.tool_name("knowledge_search")
    result = await runtime.tools.call(
        name,
        {"query": query, "top_k": 3},
        ctx=ToolContext(run_id="t", agent_id="tester", allowed_tools=[name], permissions=["read"]),
    )
    assert result.success, result.error
    return [hit["title"] for hit in result.content["hits"]]


async def test_knowledge_base_is_seeded_by_default(platform: Starter) -> None:
    runtime = await platform(FakeModel([GOOD_PLAN]))
    assert runtime.knowledge.stats().documents >= 9
    assert "雨天与高温天的一日旅行安排" in await search(runtime, "下雨天 室内 景点")
    assert "费用与交通核算规则" in await search(runtime, "交通费 怎么算")


async def test_emptied_knowledge_base_is_reseeded_on_restart(platform: Starter) -> None:
    runtime = await platform(FakeModel([GOOD_PLAN]))
    for item in runtime.knowledge.list_documents():
        await runtime.knowledge.delete(item.id)
    assert await search(runtime, "预算") == []
    await runtime.close()
    restarted = await platform(FakeModel([GOOD_PLAN]))
    assert restarted.knowledge.stats().documents >= 9


async def test_happy_path_reaches_human_approval(platform: Starter) -> None:
    model = FakeModel([GOOD_PLAN])
    runtime = await platform(model)
    trace = await submit(runtime)
    assert trace["status"] == "awaiting_human", trace["error"]
    assert trace["validation"]["passed"] and trace["review"]["approved"]
    # 规划、审核各一步，每步先调用工具再给出答案：共 4 次模型请求
    assert model.counts == {"planner": 1, "reviewer": 1, "tool_turns": 2, "models": 0}
    assert trace["calls"] == len(model.requests) == 4
    assert trace["spent_tokens"] == 120 * len(model.requests)
    expected = [
        "scheduled",
        "agent_selected",
        "tool_decision",
        "tool_result",
        "knowledge_retrieved",
    ]
    assert set(expected) <= set(kinds(trace))
    tool_events = [e for e in trace["events"] if e["kind"] == "tool_result"]
    assert all(e["data"]["via"].startswith("MCP tools/call") for e in tool_events)
    chosen = {e["data"]["name"] for e in tool_events if e["data"]["decided_by"] == "model"}
    assert chosen == {"catalog", "constraints", "knowledge_search"}
    retrieved = next(e for e in trace["events"] if e["kind"] == "knowledge_retrieved")
    assert retrieved["data"]["hits"], "种子知识库应当被检索到"
    # 工具结果（含知识片段）以 tool 消息交回模型
    final = model.final_requests("planner")[0]["messages"]
    assert any(m["role"] == "tool" and '"hits"' in m["content"] for m in final)

    decided = await runtime.resolve(trace["run_id"], True, "ok")
    assert decided["status"] == "succeeded"


async def test_self_loop_repairs_plan_from_feedback(platform: Starter) -> None:
    model = FakeModel([BAD_PLAN, GOOD_PLAN])
    runtime = await platform(model)
    trace = await submit(runtime)
    assert trace["status"] == "awaiting_human", trace["error"]
    assert model.counts["planner"] == 2 and model.counts["reviewer"] == 1
    assert "feedback" in kinds(trace)
    # 第二次规划请求带上了上一轮的失败原因
    repair = model.final_requests("planner")[1]["messages"][1]["content"]
    assert "超过预算" in repair


async def test_stops_after_iteration_limit(platform: Starter) -> None:
    model = FakeModel([BAD_PLAN])
    runtime = await platform(model)
    trace = await submit(runtime, max_iterations=2)
    assert trace["status"] == "failed"
    assert model.counts["planner"] == 2 and model.counts["reviewer"] == 0
    assert trace["error"]


async def test_reviewer_rejection_feeds_back(platform: Starter) -> None:
    model = FakeModel([GOOD_PLAN], approve=False)
    runtime = await platform(model)
    trace = await submit(runtime, max_iterations=2)
    assert trace["status"] == "failed"
    assert model.counts["planner"] == 2 and model.counts["reviewer"] == 2


async def test_without_human_review_run_succeeds(platform: Starter) -> None:
    runtime = await platform(FakeModel([GOOD_PLAN]))
    trace = await submit(runtime, human_review=False)
    assert trace["status"] == "succeeded", trace["error"]


async def test_provider_error_is_not_retried(platform: Starter) -> None:
    model = FakeModel([GOOD_PLAN], status_code=401)
    runtime = await platform(model)
    trace = await submit(runtime)
    assert trace["status"] == "failed"
    assert trace["calls"] == 1
    assert any("HTTP 401" in item for item in trace["feedback"])


async def test_submit_requires_cost_confirmation_and_key(platform: Starter) -> None:
    runtime = await platform(FakeModel([GOOD_PLAN]))
    with pytest.raises(ValueError):
        await runtime.submit(TravelRequest(confirm_paid=False))
    runtime.settings = runtime.settings.model_copy(update={"llm_api_key": SecretStr("")})
    with pytest.raises(ModelConfigurationError):
        await runtime.submit(TravelRequest(confirm_paid=True))


async def test_concurrent_runs_are_all_scheduled(platform: Starter) -> None:
    model = FakeModel([GOOD_PLAN])
    runtime = await platform(model)
    runs = [
        await runtime.submit(TravelRequest(confirm_paid=True, priority=priority))
        for priority in (1, 9, 5)
    ]
    traces = [await finish(runtime, run["run_id"]) for run in runs]
    assert {trace["status"] for trace in traces} == {"awaiting_human"}
    orders = {
        trace["inputs"]["priority"]: next(
            e["data"]["dispatch_order"] for e in trace["events"] if e["kind"] == "scheduled"
        )
        for trace in traces
    }
    assert sorted(orders.values()) == [1, 2, 3]


async def test_offered_tools_follow_agent_whitelist(platform: Starter) -> None:
    model = FakeModel([GOOD_PLAN])
    runtime = await platform(model)
    await submit(runtime)
    planner = offered_tools(model.final_requests("planner")[0])
    reviewer = offered_tools(model.final_requests("reviewer")[0])
    assert {"catalog", "constraints", "knowledge_search"} <= set(planner)
    assert "catalog" not in reviewer and "verify_plan" not in reviewer
    for body in model.requests:
        for item in body.get("tools") or []:
            properties = item["function"]["parameters"].get("properties", {})
            assert "run_id" not in properties and "request" not in properties


async def test_final_turn_forces_an_answer(platform: Starter) -> None:
    model = FakeModel([GOOD_PLAN], greedy_tools=True)
    runtime = await platform(model)
    trace = await submit(runtime)
    assert trace["status"] == "awaiting_human", trace["error"]
    planner = [body for body in model.requests if not is_reviewer(body)]
    # 每个 Agent 步骤最多 3 次往返，最后一次 tool_choice=none 强制给出最终结果
    assert [body.get("tool_choice") for body in planner] == ["auto", "auto", "none"]


async def test_model_may_answer_without_tools(platform: Starter) -> None:
    model = FakeModel([GOOD_PLAN], use_tools=False)
    runtime = await platform(model)
    trace = await submit(runtime)
    assert trace["status"] == "awaiting_human", trace["error"]
    assert trace["calls"] == 2 and "tool_decision" not in kinds(trace)


async def test_falls_back_when_tools_unsupported(platform: Starter) -> None:
    model = FakeModel([GOOD_PLAN], tools_supported=False)
    runtime = await platform(model)
    trace = await submit(runtime)
    assert trace["status"] == "awaiting_human", trace["error"]
    assert "tool_calling_fallback" in kinds(trace)
    # 只有第一次请求带了 tools；降级后平台预取，知识库检索仍然发生
    assert [bool(body.get("tools")) for body in model.requests].count(True) == 1
    assert "knowledge_retrieved" in kinds(trace)


async def test_interrupted_run_resumes_from_checkpoint(platform: Starter) -> None:
    first = FakeModel([GOOD_PLAN], block_reviewer=True)
    runtime = await platform(first)
    run = await runtime.submit(TravelRequest(confirm_paid=True))
    async with asyncio.timeout(10):
        await first.reviewer_started.wait()
    await runtime.close()  # 模拟服务在审核途中停止

    second = FakeModel([GOOD_PLAN])
    restarted = await platform(second)
    interrupted = await restarted.trace(run["run_id"])
    assert interrupted is not None
    assert interrupted["status"] == "cancelled" and interrupted["resumable"]

    await restarted.resume(run["run_id"])
    trace = await finish(restarted, run["run_id"])
    assert trace["status"] == "awaiting_human", trace["error"]
    assert "resumed" in kinds(trace)
    # 规划与校验已在 checkpoint 中成功，续跑只重新执行审核
    assert second.counts["planner"] == 0 and second.counts["reviewer"] == 1
    with pytest.raises(ValueError):
        await restarted.resume(run["run_id"])


async def test_user_cancelled_run_cannot_resume(platform: Starter) -> None:
    model = FakeModel([GOOD_PLAN], block_reviewer=True)
    runtime = await platform(model)
    run = await runtime.submit(TravelRequest(confirm_paid=True))
    async with asyncio.timeout(10):
        await model.reviewer_started.wait()
    await runtime.cancel(run["run_id"])
    trace = await finish(runtime, run["run_id"])
    assert trace["status"] == "cancelled" and not trace["resumable"]
    with pytest.raises(ValueError):
        await runtime.resume(run["run_id"])
