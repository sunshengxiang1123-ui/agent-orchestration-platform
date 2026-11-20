"""执行快照与断点续跑。

checkpoint 记录每个已完成节点的最小快照；resume 据此跳过已完成部分，
rollback 则显式作废指定节点的记录，使其重新进入执行。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.common.orm import node_executions
from packages.core.models.run import NodeExecution

CompletionKey = tuple[str, int, int | None]


def completion_key(record: NodeExecution) -> CompletionKey:
    return (record.node_id, record.iteration, record.candidate_index)


@runtime_checkable
class CheckpointStore(Protocol):
    async def record(self, execution: NodeExecution) -> None: ...

    async def completed(self, run_id: str) -> dict[CompletionKey, NodeExecution]: ...

    async def invalidate(self, run_id: str, node_ids: set[str]) -> int: ...


class MemoryCheckpointStore:
    """进程内实现，供离线测试与单进程运行使用。"""

    def __init__(self) -> None:
        self._rows: dict[str, list[NodeExecution]] = {}

    async def record(self, execution: NodeExecution) -> None:
        rows = self._rows.setdefault(execution.run_id, [])
        value = execution.model_copy(deep=True)
        for index, existing in enumerate(rows):
            if existing.id == value.id:
                rows[index] = value
                break
        else:
            rows.append(value)

    async def completed(self, run_id: str) -> dict[CompletionKey, NodeExecution]:
        result: dict[CompletionKey, NodeExecution] = {}
        for value in self._rows.get(run_id, []):
            if value.status == "succeeded":
                result[completion_key(value)] = value.model_copy(deep=True)
        return result

    async def all(self, run_id: str) -> list[NodeExecution]:
        """返回运行的全部快照，主要供离线诊断和测试使用。"""
        return [value.model_copy(deep=True) for value in self._rows.get(run_id, [])]

    async def invalidate(self, run_id: str, node_ids: set[str]) -> int:
        rows = self._rows.get(run_id, [])
        kept = [value for value in rows if value.node_id not in node_ids]
        self._rows[run_id] = kept
        return len(rows) - len(kept)


class SqlCheckpointStore:
    """落库实现：写 node_executions，按 run_id 读回已完成节点。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def record(self, execution: NodeExecution) -> None:
        payload: dict[str, Any] = execution.model_dump()
        async with self._factory() as session, session.begin():
            existing = await session.execute(
                select(node_executions.c.id).where(node_executions.c.id == execution.id)
            )
            if existing.first() is None:
                await session.execute(node_executions.insert().values(**payload))
            else:
                await session.execute(
                    node_executions.update()
                    .where(node_executions.c.id == execution.id)
                    .values(**payload)
                )

    async def completed(self, run_id: str) -> dict[CompletionKey, NodeExecution]:
        statement = (
            select(node_executions)
            .where(node_executions.c.run_id == run_id, node_executions.c.status == "succeeded")
            .order_by(node_executions.c.finished_at, node_executions.c.id)
        )
        async with self._factory() as session:
            rows = (await session.execute(statement)).mappings().all()
        result: dict[CompletionKey, NodeExecution] = {}
        for row in rows:
            execution = NodeExecution.model_validate(dict(row))
            result[completion_key(execution)] = execution
        return result

    async def invalidate(self, run_id: str, node_ids: set[str]) -> int:
        if not node_ids:
            return 0
        statement = (
            node_executions.delete()
            .where(node_executions.c.run_id == run_id)
            .where(node_executions.c.node_id.in_(node_ids))
        )
        async with self._factory() as session, session.begin():
            result = await session.execute(statement)
        rowcount = getattr(result, "rowcount", 0)
        return int(rowcount or 0)
