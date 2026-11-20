"""通用 DAG 执行器：并行、自我循环直到验证通过、条件人工关卡、预算与取消。"""

import asyncio

import pytest

from packages.core.engine.checkpoint import MemoryCheckpointStore
from packages.core.engine.executor import Executor, NodeOutcome, NodeRequest
from packages.core.models.run import Run
from packages.core.models.workflow import LoopConfig, NodeDef, WorkflowDef


class Scripted:
    """第 ``passes_on`` 次规划后验证才通过。"""

    def __init__(self, passes_on: int = 2, tokens: int = 0) -> None:
        self.passes_on, self.tokens = passes_on, tokens
        self.plans = 0
        self.calls: list[tuple[str, int, int | None]] = []

    async def execute(self, request: NodeRequest) -> NodeOutcome:
        self.calls.append((request.node.id, request.iteration, request.candidate_index))
        if request.node.id == "plan":
            self.plans += 1
            return NodeOutcome(output={"plan": self.plans}, tokens_in=self.tokens)
        if request.node.id == "check":
            ok = self.plans >= self.passes_on
            return NodeOutcome(passed=ok, score=float(ok), error=None if ok else "未通过")
        if request.node.id == "gate":
            return NodeOutcome(awaiting_human=True)
        if request.node.id == "slow":
            await asyncio.sleep(10)
        return NodeOutcome()


def workflow(iterations: int = 3) -> WorkflowDef:
    return WorkflowDef(
        id="demo",
        nodes=[
            NodeDef(id="collect", type="parallel", n_candidates=2),
            NodeDef(
                id="refine",
                type="loop",
                depends_on=["collect"],
                loop=LoopConfig(
                    max_iterations=iterations,
                    improvement_epsilon=-1,
                    body=[
                        NodeDef(id="plan", type="agent", agent="planner"),
                        NodeDef(id="check", type="verify", depends_on=["plan"]),
                    ],
                ),
            ),
            NodeDef(
                id="gate",
                type="human_gate",
                depends_on=["refine"],
                condition="inputs.review == true",
            ),
        ],
    )


def run(review: bool = True, budget: int | None = None) -> Run:
    return Run(
        id="run-1",
        workflow_id="demo",
        workflow_version="1",
        status="running",
        inputs={"review": review},
        seed=1,
        budget_tokens=budget,
    )


async def test_self_loop_until_verified_then_human_gate() -> None:
    handler = Scripted(passes_on=2)
    current = run()
    await Executor(handler, checkpoints=MemoryCheckpointStore()).run(workflow(), current)
    assert current.status == "awaiting_human"
    assert handler.plans == 2
    assert [call for call in handler.calls if call[0] == "collect"] == [
        ("collect", 0, 0),
        ("collect", 0, 1),
    ] or {call[2] for call in handler.calls if call[0] == "collect"} == {0, 1}
    assert ("plan", 1, None) in handler.calls  # 第二轮迭代


async def test_condition_skips_human_gate() -> None:
    current = run(review=False)
    await Executor(Scripted(passes_on=1), checkpoints=MemoryCheckpointStore()).run(
        workflow(), current
    )
    assert current.status == "succeeded"


async def test_loop_stops_at_iteration_limit() -> None:
    handler = Scripted(passes_on=99)
    current = run()
    await Executor(handler, checkpoints=MemoryCheckpointStore()).run(workflow(2), current)
    assert current.status == "failed"
    assert handler.plans == 2


async def test_token_budget_stops_the_loop() -> None:
    handler = Scripted(passes_on=99, tokens=100)
    current = run(budget=150)
    await Executor(handler, checkpoints=MemoryCheckpointStore()).run(workflow(3), current)
    assert current.status in {"failed", "cancelled"}
    assert handler.plans == 2


async def test_cancellation_marks_run_cancelled() -> None:
    flow = WorkflowDef(id="slow", nodes=[NodeDef(id="slow", type="tool")])
    current = run()
    task = asyncio.create_task(
        Executor(Scripted(), checkpoints=MemoryCheckpointStore()).run(flow, current)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert current.status == "cancelled"
