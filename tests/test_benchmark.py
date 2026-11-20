"""性能评估脚本：mock 模式下能跑完、统计正确，并区分有解与无解场景。"""

from pathlib import Path

from pydantic import SecretStr

from packages.common.config import Settings
from scripts.benchmark import SCENARIOS, ScriptedModel, run_benchmark, summarize, to_markdown


async def test_mock_benchmark_summarizes_runs(tmp_path: Path) -> None:
    names = {"默认一日游", "低预算", "四景点低预算（无解）"}
    picked = [s for s in SCENARIOS if s["name"] in names]
    outcomes, wall = await run_benchmark(
        Settings(_env_file=None, llm_api_key=SecretStr("sk-benchmark-mock-0000")),
        picked,
        database=tmp_path / "bench.db",
        transport=ScriptedModel().transport(),
        wait_seconds=30,
    )
    summary = summarize(outcomes, wall, "mock")
    assert summary["scenarios"] == 3 and summary["feasible"] == 2
    # 替身模型收到反馈后会给出最优解，有解场景应全部通过
    assert summary["pass_rate_on_feasible"] == 1.0
    assert summary["infeasible_correctly_rejected"] == 1
    assert summary["avg_cost_gap_vs_optimal"] >= 0
    assert summary["model_tool_calls"].get("catalog", 0) >= 2
    assert "| 默认一日游 |" in to_markdown(summary, outcomes)
