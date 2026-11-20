"""性能评估：批量运行一组约束场景，统计通过率、迭代轮数、调用次数、token 与耗时。

两种模式：
- ``--mock``（默认，免费）：用脚本化替身模型，第一版计划刻意随机，收到反馈后改用
  最优解。它衡量的是平台本身（调度、MCP 工具链、自我循环、验证）的行为与开销，
  不代表大模型的规划能力。
- ``--live``：调用真实模型，会产生费用；默认只跑前 5 个场景。

每个场景都先用穷举求解器算出“是否有解 / 最低费用”，用来区分“约束本身无解”和
“Agent 没找到解”，并计算模型方案与最低费用的差距。

    uv run python -m scripts.benchmark                 # mock，全部 17 个场景
    uv run python -m scripts.benchmark --live --limit 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from packages.application.runtime import PlatformRuntime
from packages.application.travel import CATALOG, TravelRequest, solve
from packages.common.config import Settings

ROOT = Path(__file__).resolve().parents[1]

SCENARIOS: list[dict[str, Any]] = [
    {"name": "默认一日游", "budget": 200},
    {"name": "低预算", "budget": 80},
    {"name": "极低预算（无解）", "budget": 55},
    {"name": "四景点", "budget": 220, "min_stops": 4},
    {"name": "四景点低预算（无解）", "budget": 85, "min_stops": 4},
    {"name": "只看文化", "budget": 200, "interests": ["culture"]},
    {"name": "必去观景塔", "budget": 230, "must_visit": ["tower"]},
    {"name": "必去观景塔低预算（无解）", "budget": 170, "must_visit": ["tower"]},
    {"name": "周一必去博物馆（无解）", "budget": 200, "weekday": 1, "must_visit": ["museum"]},
    {"name": "周二必去艺术馆（无解）", "budget": 200, "weekday": 2, "must_visit": ["gallery"]},
    {"name": "雨天", "budget": 200, "weather": "rainy"},
    {"name": "雨天低预算（无解）", "budget": 100, "weather": "rainy"},
    {"name": "雨天四景点", "budget": 300, "min_stops": 4, "weather": "rainy"},
    {"name": "下午出发", "budget": 200, "day_start": 780, "day_end": 1140},
    {"name": "上午半天", "budget": 200, "min_stops": 2, "day_start": 540, "day_end": 780},
    {"name": "五景点全覆盖", "budget": 400, "min_stops": 5, "day_end": 1260},
    {"name": "高优先级默认", "budget": 200, "priority": 9},
]


@dataclass
class Outcome:
    name: str
    feasible: bool
    optimal_cost: int | None
    status: str
    passed: bool
    first_pass: bool
    iterations: int
    calls: int
    tokens: int
    seconds: float
    tool_calls: dict[str, int]
    knowledge_hits: int
    plan_cost: int | None
    error: str | None


class ScriptedModel:
    """mock 模式的替身：第一版随机，收到反馈后返回最优解；审核员总是通过。"""

    def __init__(self, seed: int = 7, latency: float = 0.0) -> None:
        self.rng = random.Random(seed)
        # 模拟每次模型请求的耗时，让并发调度与吞吐数字有意义。
        self.latency = latency

    def transport(self) -> httpx.MockTransport:
        async def handle(request: httpx.Request) -> httpx.Response:
            if self.latency:
                await asyncio.sleep(self.latency)
            body = json.loads(request.content)
            messages = body["messages"]
            if body.get("tools") and not any(m["role"] == "tool" for m in messages):
                offered = [item["function"]["name"] for item in body["tools"]]
                wanted = [n for n in ("catalog", "constraints", "knowledge_search") if n in offered]
                return self._reply(body, {"content": "", "tool_calls": self._calls(wanted)})
            if "审核当前计划" in messages[0]["content"]:
                return self._reply(body, {"content": '{"approved": true, "feedback": []}'})
            return self._reply(body, {"content": json.dumps(self._plan(messages))})

        return httpx.MockTransport(handle)

    @staticmethod
    def _calls(names: list[str]) -> list[dict[str, Any]]:
        return [
            {
                "id": f"c{index}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(
                        {"query": "预算 行程"} if name == "knowledge_search" else {}
                    ),
                },
            }
            for index, name in enumerate(names)
        ]

    def _plan(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        context = json.loads(messages[1]["content"])
        request = self._request(messages)
        if context.get("feedback") and request is not None:
            best = solve(request)
            if best is not None:
                return best[0].model_dump()
        spots = self.rng.sample([str(item["id"]) for item in CATALOG], 3)
        start = 540
        stops = []
        for spot in spots:
            stops.append({"spot_id": spot, "start_minute": start})
            start += self.rng.choice([90, 110, 130])
        return {"stops": stops, "rationale": "替身模型的随机初稿"}

    @staticmethod
    def _request(messages: list[dict[str, Any]]) -> TravelRequest | None:
        for message in messages:
            if message["role"] != "tool":
                continue
            data = json.loads(message["content"])
            if "min_stops" in data:
                keys = TravelRequest.model_fields.keys() - {"confirm_paid"}
                return TravelRequest(**{key: data[key] for key in keys if key in data})
        return None

    @staticmethod
    def _reply(body: dict[str, Any], message: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": message}],
                "usage": {"prompt_tokens": 600, "completion_tokens": 120},
            },
        )


async def _wait(runtime: PlatformRuntime, run_id: str, wait_seconds: float) -> dict[str, Any]:
    async with asyncio.timeout(wait_seconds):
        while True:
            trace = await runtime.trace(run_id)
            if trace and any(event["kind"] == "finished" for event in trace["events"]):
                return trace
            await asyncio.sleep(0.05)


def _measure(name: str, request: TravelRequest, trace: dict[str, Any]) -> Outcome:
    best = solve(request)
    events = trace["events"]
    verifications = [e["data"] for e in events if e["kind"] == "verification"]
    tools: dict[str, int] = {}
    for event in events:
        if event["kind"] == "tool_result" and event["data"].get("decided_by") == "model":
            tools[event["data"]["name"]] = tools.get(event["data"]["name"], 0) + 1
    hits = sum(len(e["data"].get("hits", [])) for e in events if e["kind"] == "knowledge_retrieved")
    started = datetime.fromisoformat(trace["created_at"])
    finished = datetime.fromisoformat(events[-1]["at"])
    passed = trace["status"] == "succeeded"
    return Outcome(
        name=name,
        feasible=best is not None,
        optimal_cost=best[1] if best else None,
        status=trace["status"],
        passed=passed,
        first_pass=bool(verifications) and verifications[0]["passed"] and passed,
        iterations=len(verifications),
        calls=trace["calls"],
        tokens=trace["spent_tokens"],
        seconds=(finished - started).total_seconds(),
        tool_calls=tools,
        knowledge_hits=hits,
        plan_cost=verifications[-1].get("total_cost") if verifications else None,
        error=trace["error"],
    )


async def run_benchmark(
    settings: Settings,
    scenarios: list[dict[str, Any]],
    *,
    database: Path,
    transport: httpx.AsyncBaseTransport | None,
    wait_seconds: float = 330,
) -> tuple[list[Outcome], float]:
    runtime = PlatformRuntime(
        settings, database_url="sqlite+aiosqlite:///" + database.as_posix(), transport=transport
    )
    await runtime.start()
    try:
        started = time.monotonic()
        submitted = []
        for scenario in scenarios:
            values = {k: v for k, v in scenario.items() if k != "name"}
            request = TravelRequest(human_review=False, confirm_paid=True, **values)
            run = await runtime.submit(request)
            submitted.append((scenario["name"], request, run["run_id"]))
        outcomes = []
        for name, request, run_id in submitted:
            trace = await _wait(runtime, run_id, wait_seconds)
            outcomes.append(_measure(name, request, trace))
        return outcomes, time.monotonic() - started
    finally:
        await runtime.close()


def summarize(outcomes: list[Outcome], wall: float, mode: str) -> dict[str, Any]:
    feasible = [item for item in outcomes if item.feasible]
    solved = [item for item in feasible if item.passed]
    gaps = [
        (item.plan_cost - item.optimal_cost) / item.optimal_cost
        for item in solved
        if item.plan_cost is not None and item.optimal_cost
    ]
    durations = sorted(item.seconds for item in outcomes)
    tool_totals: dict[str, int] = {}
    for item in outcomes:
        for name, count in item.tool_calls.items():
            tool_totals[name] = tool_totals.get(name, 0) + count

    def mean(values: list[float]) -> float:
        return round(statistics.fmean(values), 3) if values else 0.0

    return {
        "mode": mode,
        "scenarios": len(outcomes),
        "feasible": len(feasible),
        "pass_rate_on_feasible": round(len(solved) / len(feasible), 3) if feasible else None,
        "first_pass_rate_on_feasible": (
            round(sum(item.first_pass for item in feasible) / len(feasible), 3)
            if feasible
            else None
        ),
        "repaired_by_self_loop": sum(item.passed and not item.first_pass for item in feasible),
        "infeasible_correctly_rejected": sum(
            not item.feasible and not item.passed for item in outcomes
        ),
        "avg_iterations": mean([item.iterations for item in outcomes]),
        "avg_calls": mean([item.calls for item in outcomes]),
        "avg_tokens": mean([item.tokens for item in outcomes]),
        "avg_cost_gap_vs_optimal": mean(gaps),
        "latency_p50_s": round(durations[len(durations) // 2], 3) if durations else 0,
        "latency_max_s": round(durations[-1], 3) if durations else 0,
        "wall_time_s": round(wall, 3),
        "throughput_runs_per_min": round(len(outcomes) / wall * 60, 2) if wall else None,
        "model_tool_calls": tool_totals,
        "avg_knowledge_hits": mean([item.knowledge_hits for item in outcomes]),
    }


def to_markdown(summary: dict[str, Any], outcomes: list[Outcome]) -> str:
    lines = [
        f"# 性能评估（{summary['mode']}）",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        *[f"| {key} | {value} |" for key, value in summary.items() if key != "mode"],
        "",
        "| 场景 | 有解 | 最低费用 | 状态 | 一次通过 | 迭代 | 调用 | token | 耗时/s | 方案费用 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in outcomes:
        lines.append(
            f"| {item.name} | {'是' if item.feasible else '否'} | {item.optimal_cost or '-'} "
            f"| {item.status} | {'是' if item.first_pass else '否'} | {item.iterations} "
            f"| {item.calls} | {item.tokens} | {item.seconds:.2f} | {item.plan_cost or '-'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量场景性能评估")
    parser.add_argument("--live", action="store_true", help="调用真实模型（会产生费用）")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 个场景")
    parser.add_argument("--iterations", type=int, choices=[1, 2, 3], default=3)
    parser.add_argument(
        "--latency", type=float, default=0.2, help="mock 模式下每次模型请求的模拟耗时（秒）"
    )
    args = parser.parse_args(argv)
    limit = args.limit if args.limit is not None else (5 if args.live else len(SCENARIOS))
    scenarios = [{**scenario, "max_iterations": args.iterations} for scenario in SCENARIOS[:limit]]
    reports = ROOT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    mode = "live" if args.live else "mock"
    transport: httpx.AsyncBaseTransport | None
    if args.live:
        settings = Settings(_env_file=ROOT / ".env")
        transport = None
        print(f"真实模型评估：{len(scenarios)} 个场景，可能产生费用。")
    else:
        settings = Settings(_env_file=None, llm_api_key="sk-benchmark-mock-0000")
        transport = ScriptedModel(latency=args.latency).transport()
        print(f"mock 评估：{len(scenarios)} 个场景，模拟每次模型请求耗时 {args.latency}s。")
    database = reports / f"benchmark-{mode}-{stamp}.db"
    outcomes, wall = asyncio.run(
        run_benchmark(settings, scenarios, database=database, transport=transport)
    )
    summary = summarize(outcomes, wall, mode)
    payload = {"summary": summary, "runs": [item.__dict__ for item in outcomes]}
    (reports / f"benchmark-{mode}-{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = to_markdown(summary, outcomes)
    (reports / f"benchmark-{mode}-{stamp}.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    print(f"报告已保存到 reports/benchmark-{mode}-{stamp}.md / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
