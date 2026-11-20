"""显式授权一次真实闭环验收；默认不联网，不修改持久的模型授权开关。"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from packages.application.runtime import PlatformRuntime
from packages.application.travel import CALLS_PER_ITERATION, TravelRequest
from packages.common.config import Settings
from packages.llm.deepseek import ModelConfigurationError, validate_deepseek_settings

ROOT = Path(__file__).resolve().parents[1]


async def check_workflow(settings: Settings, iterations: int) -> dict[str, Any]:
    reports = ROOT / "reports"
    await asyncio.to_thread(reports.mkdir, parents=True, exist_ok=True)
    runtime = PlatformRuntime(
        settings, database_url="sqlite+aiosqlite:///" + (reports / "platform.db").as_posix()
    )
    await runtime.start()
    try:
        submitted = await runtime.submit(
            TravelRequest(
                budget=150, max_iterations=iterations, human_review=True, confirm_paid=True
            )
        )
        run_id = submitted["run_id"]
        async with asyncio.timeout(330):
            while True:
                trace = await runtime.trace(run_id)
                if (
                    trace
                    and trace["status"] not in {"queued", "running"}
                    and any(event["kind"] == "finished" for event in trace["events"])
                ):
                    break
                await asyncio.sleep(0.2)
        target = reports / f"live-workflow-{run_id}.json"
        await asyncio.to_thread(
            target.write_text,
            json.dumps(trace, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return {
            "run_id": run_id,
            "status": trace["status"],
            "calls": trace["calls"],
            "spent_tokens": trace["spent_tokens"],
            "evidence": str(target),
        }
    finally:
        await runtime.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="一次受控的真实工作流验收")
    parser.add_argument("--live", action="store_true", help="确认本次真实模型请求可能计费")
    parser.add_argument("--max-iterations", type=int, choices=[1, 2, 3], default=3)
    args = parser.parse_args(argv)
    if not args.live:
        print(
            f"需要确认后使用 --live，最多 {CALLS_PER_ITERATION} × 迭代数 次请求，"
            "每次输出最多 1024 token。"
        )
        print("这不是平台已验收的证据；真实运行可能失败，且须单独人工审批。")
        return 0
    try:
        settings = Settings(_env_file=ROOT / ".env")
        validate_deepseek_settings(settings)
        limit = CALLS_PER_ITERATION * args.max_iterations
        print(f"已明确授权本次运行：最多 {limit} 次模型请求，不修改 .env。")
        summary = asyncio.run(check_workflow(settings, args.max_iterations))
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["status"] in {"awaiting_human", "succeeded"} else 1
    except (ValidationError, ModelConfigurationError):
        print("模型配置未就绪；请本地检查 .env，不要在聊天或日志里输出密钥。")
        return 2
    except TimeoutError:
        print("验收超时，已停止任务；已发送请求可能计费，请检查本地审计和服务商账单。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
