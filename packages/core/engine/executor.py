"""通用 DAG 执行器：节点调度、checkpoint、续跑、循环与推测式取消。

执行器只理解工作流图和通用节点控制语义，具体的 Agent、工具、验证器或人工界面
行为全部通过 ``NodeHandler`` 注入，因此这里不绑定任何软件工程领域实现。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from packages.core.engine.blackboard import Blackboard, MemoryBlackboard
from packages.core.engine.checkpoint import CheckpointStore, CompletionKey, MemoryCheckpointStore
from packages.core.engine.expr import evaluate_expression
from packages.core.models.run import NodeExecution, NodeStatus, Run
from packages.core.models.workflow import NodeDef, WorkflowDef

PASSED_STATUSES = frozenset({"succeeded", "skipped"})
BLOCKED_STATUSES = frozenset({"failed", "cancelled"})
MAX_GOTO_RESETS = 16


@dataclass
class NodeOutcome:
    """单个节点动作的执行结果，由注入的处理器填充。"""

    passed: bool = True
    score: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    output: dict[str, Any] = field(default_factory=dict)
    action: tuple[str, str, str] | None = None
    awaiting_human: bool = False
    error: str | None = None


@dataclass
class NodeRequest:
    """处理器收到的请求；不暴露执行器内部的拓扑状态机。"""

    run: Run
    node: NodeDef
    iteration: int
    candidate_index: int | None
    inputs: dict[str, Any]
    upstream: dict[str, Any]
    blackboard: Blackboard


@runtime_checkable
class NodeHandler(Protocol):
    """八种节点共用的领域无关处理器契约。"""

    async def execute(self, request: NodeRequest) -> NodeOutcome: ...


@dataclass
class NodeRecord:
    node_id: str
    iteration: int
    candidate_index: int | None
    status: NodeStatus
    outcome: NodeOutcome | None = None
    transient: bool = False


@dataclass
class RunReport:
    run: Run
    records: list[NodeRecord] = field(default_factory=list)
    cancelled: int = 0

    @property
    def succeeded(self) -> bool:
        return self.run.status == "succeeded"

    def record_for(self, node_id: str) -> NodeRecord | None:
        for record in reversed(self.records):
            if record.node_id == node_id:
                return record
        return None


class Executor:
    """按依赖拓扑执行工作流，并用 checkpoint 支持续跑。"""

    def __init__(
        self,
        handler: NodeHandler,
        *,
        checkpoints: CheckpointStore | None = None,
        blackboard: Blackboard | None = None,
    ) -> None:
        self._handler = handler
        self._checkpoints = checkpoints or MemoryCheckpointStore()
        self._blackboard = blackboard or MemoryBlackboard()

    async def run(
        self,
        workflow: WorkflowDef,
        run: Run,
        *,
        resume: bool = False,
        subflows: dict[str, WorkflowDef] | None = None,
    ) -> RunReport:
        completed = await self._checkpoints.completed(run.id) if resume else {}
        report = RunReport(run=run)
        try:
            await self._execute_scope(
                workflow.nodes,
                run,
                report,
                completed,
                _ScopeState(),
                subflows or {},
                iteration_offset=0,
            )
        except asyncio.CancelledError:
            run.status = "cancelled"
            raise
        if run.status == "running":
            run.status = "failed" if _has_failure(report) else "succeeded"
        return report

    async def resume(
        self,
        workflow: WorkflowDef,
        run: Run,
        *,
        subflows: dict[str, WorkflowDef] | None = None,
    ) -> RunReport:
        """续跑运行；成功节点只从 checkpoint 恢复，不再次执行或落库。"""
        run.status = "running"
        return await self.run(workflow, run, resume=True, subflows=subflows)

    async def rollback(self, run: Run | str, node_id: str | set[str]) -> int:
        """删除节点及其历史快照，使下一次续跑重新执行这些节点。"""
        run_id = run.id if isinstance(run, Run) else run
        node_ids = {node_id} if isinstance(node_id, str) else set(node_id)
        return await self._checkpoints.invalidate(run_id, node_ids)

    async def _execute_scope(
        self,
        nodes: list[NodeDef],
        run: Run,
        report: RunReport,
        completed: dict[CompletionKey, NodeExecution],
        state: _ScopeState,
        subflows: dict[str, WorkflowDef],
        *,
        iteration_offset: int,
    ) -> None:
        statuses: dict[str, NodeStatus] = {node.id: "pending" for node in nodes}
        by_id = {node.id: node for node in nodes}
        while True:
            if run.status == "awaiting_human":
                return
            await self._skip_blocked(nodes, statuses, run, report, iteration_offset)
            ready = [
                node
                for node in nodes
                if statuses[node.id] == "pending"
                and all(statuses[dependency] in PASSED_STATUSES for dependency in node.depends_on)
            ]
            if not ready:
                return
            for node in ready:
                statuses[node.id] = "running"
                record = await self._dispatch(
                    node,
                    run,
                    report,
                    completed,
                    state,
                    subflows,
                    iteration_offset=iteration_offset,
                )
                statuses[node.id] = record.status
                report.records.append(record)
                if record.status == "succeeded":
                    state.remember(node.id, record.outcome)
                if record.status == "awaiting_human":
                    run.status = "awaiting_human"
                    return
                if record.status == "failed" and node.on_fail and node.on_fail.goto:
                    target = node.on_fail.goto
                    if target in by_id and state.allow_reset(target, node.id):
                        reset_ids = {target, *_transitive_dependents(nodes, target)}
                        for reset_id in reset_ids:
                            statuses[reset_id] = "pending"
                            state.forget(reset_id)
                            _forget_completed(completed, reset_id)
            if run.budget_tokens is not None and run.spent_tokens >= run.budget_tokens:
                await self._cancel_remaining(nodes, statuses, run, report, iteration_offset)
                return

    async def _skip_blocked(
        self,
        nodes: list[NodeDef],
        statuses: dict[str, NodeStatus],
        run: Run,
        report: RunReport,
        iteration_offset: int,
    ) -> None:
        for node in nodes:
            if statuses[node.id] != "pending":
                continue
            if any(statuses[dependency] in BLOCKED_STATUSES for dependency in node.depends_on):
                statuses[node.id] = "skipped"
                report.records.append(NodeRecord(node.id, iteration_offset, None, "skipped"))
                await self._persist(
                    run,
                    node,
                    iteration_offset,
                    None,
                    "skipped",
                    None,
                    "upstream_blocked",
                    input_snapshot={"reason": "upstream_blocked"},
                )

    async def _cancel_remaining(
        self,
        nodes: list[NodeDef],
        statuses: dict[str, NodeStatus],
        run: Run,
        report: RunReport,
        iteration_offset: int,
    ) -> None:
        run.status = "cancelled"
        for node in nodes:
            if statuses[node.id] != "pending":
                continue
            statuses[node.id] = "cancelled"
            outcome = NodeOutcome(passed=False, error="预算耗尽")
            report.records.append(NodeRecord(node.id, iteration_offset, None, "cancelled", outcome))
            await self._persist(
                run,
                node,
                iteration_offset,
                None,
                "cancelled",
                outcome,
                "budget",
                input_snapshot={"reason": "budget"},
            )

    async def _dispatch(
        self,
        node: NodeDef,
        run: Run,
        report: RunReport,
        completed: dict[CompletionKey, NodeExecution],
        state: _ScopeState,
        subflows: dict[str, WorkflowDef],
        *,
        iteration_offset: int,
    ) -> NodeRecord:
        if node.condition is not None and not evaluate_expression(
            node.condition, state.context(run)
        ):
            await self._persist(
                run,
                node,
                iteration_offset,
                None,
                "skipped",
                None,
                "condition",
                input_snapshot={"reason": "condition"},
            )
            return NodeRecord(node.id, iteration_offset, None, "skipped")

        cached = _find_completed(completed, node.id, iteration_offset, None, node.type == "loop")
        if cached is not None:
            return NodeRecord(
                node.id,
                cached.iteration,
                None,
                "succeeded",
                _outcome_from_execution(cached),
            )

        if node.type == "loop":
            if node.loop is None:
                outcome = NodeOutcome(passed=False, error="循环节点缺少配置")
                await self._persist(
                    run,
                    node,
                    iteration_offset,
                    None,
                    "failed",
                    outcome,
                    "invalid_loop",
                )
                return NodeRecord(node.id, iteration_offset, None, "failed", outcome)
            return await self._run_loop(
                node,
                run,
                report,
                completed,
                state,
                subflows,
                iteration_offset=iteration_offset,
            )

        if node.type == "parallel":
            record, cancelled = await self._run_parallel(node, run, state, iteration_offset)
            report.cancelled += cancelled
            return record

        if node.type == "subflow":
            nested = subflows.get(node.subflow or "")
            if nested is None:
                outcome = NodeOutcome(passed=False, error=f"子工作流未注册：{node.subflow}")
                await self._persist(
                    run,
                    node,
                    iteration_offset,
                    None,
                    "failed",
                    outcome,
                    "subflow",
                )
                return NodeRecord(node.id, iteration_offset, None, "failed", outcome)
            nested_report = RunReport(run=run)
            await self._execute_scope(
                nested.nodes,
                run,
                nested_report,
                completed,
                state,
                subflows,
                iteration_offset=iteration_offset,
            )
            report.records.extend(nested_report.records)
            if run.status == "awaiting_human":
                outcome = NodeOutcome(passed=False, awaiting_human=True, output=state.outputs())
                await self._persist(
                    run,
                    node,
                    iteration_offset,
                    None,
                    "awaiting_human",
                    outcome,
                    "subflow",
                )
                return NodeRecord(node.id, iteration_offset, None, "awaiting_human", outcome)
            passed = not _has_failure(nested_report)
            outcome = NodeOutcome(passed=passed, output=state.outputs())
            status: NodeStatus = "succeeded" if passed else "failed"
            await self._persist(
                run,
                node,
                iteration_offset,
                None,
                status,
                outcome,
                "subflow",
            )
            return NodeRecord(node.id, iteration_offset, None, status, outcome)

        return await self._invoke(node, run, state, iteration_offset, None)

    async def _invoke(
        self,
        node: NodeDef,
        run: Run,
        state: _ScopeState,
        iteration: int,
        candidate: int | None,
        *,
        swallow_cancel: bool = False,
    ) -> NodeRecord:
        retries = node.on_fail.max_retry if node.on_fail else 0
        outcome = NodeOutcome(passed=False, error="未执行")
        status: NodeStatus = "failed"
        for attempt in range(retries + 1):
            request = NodeRequest(
                run=run,
                node=node,
                iteration=iteration,
                candidate_index=candidate,
                inputs=dict(run.inputs),
                upstream=state.outputs(),
                blackboard=self._blackboard,
            )
            try:
                action = self._handler.execute(request)
                if node.timeout_seconds is not None:
                    outcome = await asyncio.wait_for(action, node.timeout_seconds)
                else:
                    outcome = await action
                if not isinstance(outcome, NodeOutcome):
                    raise TypeError("NodeHandler 必须返回 NodeOutcome")
            except asyncio.CancelledError:
                outcome = NodeOutcome(passed=False, error="节点执行被取消")
                await self._persist(
                    run,
                    node,
                    iteration,
                    candidate,
                    "cancelled",
                    outcome,
                    f"cancelled-{attempt}",
                    input_snapshot={"inputs": dict(run.inputs), "upstream": state.outputs()},
                )
                if swallow_cancel:
                    return NodeRecord(node.id, iteration, candidate, "cancelled", outcome)
                raise
            except TimeoutError:
                outcome = NodeOutcome(passed=False, error="节点执行超时")
            except Exception as exc:
                outcome = NodeOutcome(passed=False, error=f"{type(exc).__name__}: {exc}")

            run.spent_tokens += outcome.tokens_in + outcome.tokens_out
            status = _status_for(outcome)
            await self._persist(
                run,
                node,
                iteration,
                candidate,
                status,
                outcome,
                attempt,
                input_snapshot={"inputs": dict(run.inputs), "upstream": state.outputs()},
            )
            if status != "failed":
                break
            if attempt < retries and node.on_fail is not None:
                await asyncio.sleep(node.on_fail.backoff_seconds)
        return NodeRecord(node.id, iteration, candidate, status, outcome)

    async def _run_parallel(
        self,
        node: NodeDef,
        run: Run,
        state: _ScopeState,
        iteration: int,
    ) -> tuple[NodeRecord, int]:
        count = node.n_candidates or 1
        tasks = [
            asyncio.create_task(
                self._invoke(node, run, state, iteration, index, swallow_cancel=True)
            )
            for index in range(count)
        ]
        pending: set[asyncio.Task[NodeRecord]] = set(tasks)
        results: list[NodeRecord] = []
        winner: NodeOutcome | None = None
        cancelled = 0
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                done_records = [task.result() for task in done]
                results.extend(done_records)
                if winner is None:
                    winner_record = next(
                        (record for record in done_records if record.status == "succeeded"),
                        None,
                    )
                    if winner_record is not None:
                        winner = winner_record.outcome
                        if node.speculative_cancel and pending:
                            for task in pending:
                                task.cancel()
                            cancelled_records = await asyncio.gather(
                                *pending, return_exceptions=False
                            )
                            results.extend(cancelled_records)
                            cancelled = sum(
                                record.status == "cancelled" for record in cancelled_records
                            )
                            pending = set()
                            break
            if winner is None:
                winner = next(
                    (record.outcome for record in results if record.status == "succeeded"),
                    None,
                )
        finally:
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        status: NodeStatus = "succeeded" if winner is not None else "failed"
        outcome = winner or NodeOutcome(passed=False, error="并行候选均未成功")
        return NodeRecord(node.id, iteration, None, status, outcome), cancelled

    async def _run_loop(
        self,
        node: NodeDef,
        run: Run,
        report: RunReport,
        completed: dict[CompletionKey, NodeExecution],
        state: _ScopeState,
        subflows: dict[str, WorkflowDef],
        *,
        iteration_offset: int,
    ) -> NodeRecord:
        assert node.loop is not None
        config = node.loop
        signatures: set[str] = set()
        previous_score: float | None = None
        loop_start_tokens = run.spent_tokens
        last_outcome = NodeOutcome(passed=False, error="循环未执行")
        last_iteration = iteration_offset

        for local_iteration in range(config.max_iterations):
            iteration = iteration_offset + local_iteration
            last_iteration = iteration
            if config.budget_tokens is not None and (
                run.spent_tokens - loop_start_tokens >= config.budget_tokens
            ):
                last_outcome = NodeOutcome(passed=False, error="循环 token 预算耗尽")
                break
            if run.budget_tokens is not None and run.spent_tokens >= run.budget_tokens:
                last_outcome = NodeOutcome(passed=False, error="运行 token 预算耗尽")
                break

            inner_report = RunReport(run=run)
            await self._execute_scope(
                config.body,
                run,
                inner_report,
                completed,
                state,
                subflows,
                iteration_offset=iteration,
            )
            body_outcomes = [
                record.outcome for record in inner_report.records if record.outcome is not None
            ]
            body_failed = _has_failure(inner_report) or any(
                record.status in {"cancelled", "awaiting_human"} for record in inner_report.records
            )
            for record in inner_report.records:
                record.transient = True
            report.records.extend(inner_report.records)
            passed = bool(body_outcomes) and not body_failed and run.status != "awaiting_human"
            last_outcome = NodeOutcome(
                passed=passed,
                score=max((item.score for item in body_outcomes), default=0.0),
                tokens_in=sum(item.tokens_in for item in body_outcomes),
                tokens_out=sum(item.tokens_out for item in body_outcomes),
                output=state.outputs(),
                action=next((item.action for item in reversed(body_outcomes) if item.action), None),
                error=next((item.error for item in reversed(body_outcomes) if item.error), None),
            )
            status = _status_for(last_outcome)
            await self._persist(
                run,
                node,
                iteration,
                None,
                status,
                last_outcome,
                f"loop-{local_iteration}",
                input_snapshot={"iteration": iteration},
            )
            if status == "awaiting_human":
                return NodeRecord(node.id, iteration, None, status, last_outcome)
            if passed:
                return NodeRecord(node.id, iteration, None, "succeeded", last_outcome)

            if last_outcome.action is not None:
                signature = _action_signature(last_outcome.action)
                if signature in signatures:
                    last_outcome.error = "动作签名重复，循环收敛"
                    break
                signatures.add(signature)
            if previous_score is not None:
                improvement = last_outcome.score - previous_score
                if improvement < config.improvement_epsilon:
                    last_outcome.error = "改进度低于阈值，循环收敛"
                    break
            previous_score = last_outcome.score

        await self._persist(
            run,
            node,
            last_iteration,
            None,
            "failed",
            last_outcome,
            "loop-final",
            input_snapshot={"iteration": last_iteration},
        )
        return NodeRecord(node.id, last_iteration, None, "failed", last_outcome)

    async def _persist(
        self,
        run: Run,
        node: NodeDef,
        iteration: int,
        candidate: int | None,
        status: NodeStatus,
        outcome: NodeOutcome | None,
        attempt: Any,
        *,
        input_snapshot: dict[str, Any] | None = None,
    ) -> None:
        snapshot = None if outcome is None else _outcome_snapshot(outcome)
        now = datetime.now(UTC)
        candidate_key = "main" if candidate is None else str(candidate)
        attempt_key = str(attempt).replace(":", "_")
        await self._checkpoints.record(
            NodeExecution(
                id=f"{run.id}:{node.id}:{iteration}:{candidate_key}:{attempt_key}",
                run_id=run.id,
                node_id=node.id,
                iteration=iteration,
                candidate_index=candidate,
                status=status,
                agent_id=node.agent,
                input_snapshot=input_snapshot or {"type": node.type},
                output_snapshot=snapshot,
                error=outcome.error if outcome else None,
                started_at=now,
                finished_at=now,
                tokens_in=outcome.tokens_in if outcome else 0,
                tokens_out=outcome.tokens_out if outcome else 0,
            )
        )


class _ScopeState:
    """作用域产物集合与失败跳转重置计数。"""

    def __init__(self) -> None:
        self._outputs: dict[str, dict[str, Any]] = {}
        self._resets: dict[tuple[str, str], int] = {}

    def remember(self, node_id: str, outcome: NodeOutcome | None) -> None:
        if outcome is None:
            return
        self._outputs[node_id] = {
            "passed": outcome.passed,
            "score": outcome.score,
            **outcome.output,
        }

    def forget(self, node_id: str) -> None:
        self._outputs.pop(node_id, None)

    def outputs(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._outputs.items()}

    def context(self, run: Run) -> dict[str, Any]:
        outputs = self.outputs()
        return {**outputs, "nodes": outputs, "inputs": dict(run.inputs)}

    def allow_reset(self, target: str, source: str) -> bool:
        key = (target, source)
        count = self._resets.get(key, 0) + 1
        self._resets[key] = count
        return count <= MAX_GOTO_RESETS


def _status_for(outcome: NodeOutcome) -> NodeStatus:
    if outcome.awaiting_human:
        return "awaiting_human"
    return "succeeded" if outcome.passed else "failed"


def _has_failure(report: RunReport) -> bool:
    return any(record.status == "failed" and not record.transient for record in report.records)


def _transitive_dependents(nodes: list[NodeDef], node_id: str) -> set[str]:
    dependents: dict[str, list[str]] = {node.id: [] for node in nodes}
    for node in nodes:
        for dependency in node.depends_on:
            dependents[dependency].append(node.id)
    found: set[str] = set()
    stack = [node_id]
    while stack:
        current = stack.pop()
        for child in dependents.get(current, []):
            if child not in found:
                found.add(child)
                stack.append(child)
    return found


def _action_signature(action: tuple[str, str, str]) -> str:
    """用规格要求的三元组生成稳定动作签名。"""
    payload = json.dumps(list(action), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _outcome_snapshot(outcome: NodeOutcome) -> dict[str, Any]:
    return {
        "passed": outcome.passed,
        "score": outcome.score,
        "output": outcome.output,
        "action": list(outcome.action) if outcome.action is not None else None,
        "awaiting_human": outcome.awaiting_human,
        "error": outcome.error,
        "tokens_in": outcome.tokens_in,
        "tokens_out": outcome.tokens_out,
    }


def _outcome_from_execution(execution: NodeExecution) -> NodeOutcome:
    snapshot = execution.output_snapshot or {}
    action = snapshot.get("action")
    return NodeOutcome(
        passed=bool(snapshot.get("passed", True)),
        score=float(snapshot.get("score", 0.0)),
        tokens_in=int(snapshot.get("tokens_in", execution.tokens_in)),
        tokens_out=int(snapshot.get("tokens_out", execution.tokens_out)),
        output=dict(snapshot.get("output", {})),
        action=tuple(action) if isinstance(action, list) and len(action) == 3 else None,
        awaiting_human=bool(snapshot.get("awaiting_human", False)),
        error=snapshot.get("error"),
    )


def _find_completed(
    completed: dict[CompletionKey, NodeExecution],
    node_id: str,
    iteration: int,
    candidate: int | None,
    is_loop: bool,
) -> NodeExecution | None:
    exact = completed.get((node_id, iteration, candidate))
    if exact is not None:
        return exact
    if not is_loop:
        return None
    candidates = [
        record
        for (record_node, _record_iteration, record_candidate), record in completed.items()
        if record_node == node_id and record_candidate == candidate
    ]
    return max(candidates, key=lambda record: record.iteration, default=None)


def _forget_completed(completed: dict[CompletionKey, NodeExecution], node_id: str) -> None:
    for key in list(completed):
        if key[0] == node_id:
            del completed[key]
