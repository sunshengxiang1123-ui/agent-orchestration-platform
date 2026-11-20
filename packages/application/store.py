"""本地原型持久化；独立事件表不改变既有核心数据库契约。"""

from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    Integer,
    MetaData,
    String,
    Table,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from packages.common.orm import (
    agent_versions,
    agents,
    llm_calls,
    metadata,
    node_executions,
    runs,
    workflows,
)
from packages.core.models.run import Run
from packages.core.models.workflow import WorkflowDef

runtime_metadata = MetaData()
documents = Table(
    "platform_documents",
    runtime_metadata,
    Column("id", String(180), primary_key=True),
    Column("value", JSON, nullable=False),
)
events = Table(
    "platform_events",
    runtime_metadata,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(128), nullable=False, index=True),
    Column("value", JSON, nullable=False),
)


class RuntimeStore:
    def __init__(self, engine: AsyncEngine, factory: async_sessionmaker[AsyncSession]) -> None:
        self.engine, self.factory = engine, factory

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
            await connection.run_sync(runtime_metadata.create_all)

    async def save_document(self, key: str, value: dict[str, Any]) -> None:
        async with self.factory() as session, session.begin():
            exists = (
                await session.execute(select(documents.c.id).where(documents.c.id == key))
            ).first()
            if exists:
                await session.execute(
                    update(documents).where(documents.c.id == key).values(value=value)
                )
            else:
                await session.execute(insert(documents).values(id=key, value=value))

    async def document(self, key: str) -> dict[str, Any] | None:
        async with self.factory() as session:
            result = (
                await session.execute(select(documents.c.value).where(documents.c.id == key))
            ).scalar_one_or_none()
            return dict(result) if result is not None else None

    async def list_documents(self, prefix: str) -> list[dict[str, Any]]:
        async with self.factory() as session:
            values = (
                (
                    await session.execute(
                        select(documents.c.value).where(documents.c.id.startswith(prefix))
                    )
                )
                .scalars()
                .all()
            )
            return [dict(value) for value in values]

    async def delete_document(self, key: str) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(delete(documents).where(documents.c.id == key))

    async def add_event(self, run_id: str, value: dict[str, Any]) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(insert(events).values(run_id=run_id, value=value))

    async def event_list(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        async with self.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(events)
                        .where(events.c.run_id == run_id, events.c.seq > after)
                        .order_by(events.c.seq)
                        .limit(500)
                    )
                )
                .mappings()
                .all()
            )
            return [{"seq": row["seq"], **row["value"]} for row in rows]

    async def save_run(self, run: Run, *, create: bool = False) -> None:
        async with self.factory() as session, session.begin():
            if create:
                await session.execute(insert(runs).values(**run.model_dump()))
            else:
                await session.execute(
                    update(runs).where(runs.c.id == run.id).values(**run.model_dump())
                )

    async def run(self, run_id: str) -> Run | None:
        async with self.factory() as session:
            row = (
                (await session.execute(select(runs).where(runs.c.id == run_id))).mappings().first()
            )
            return Run.model_validate(dict(row)) if row is not None else None

    async def save_workflow(self, workflow: WorkflowDef) -> None:
        async with self.factory() as session, session.begin():
            exists = (
                await session.execute(
                    select(workflows.c.id).where(
                        workflows.c.id == workflow.id, workflows.c.version == workflow.version
                    )
                )
            ).first()
            if exists is None:
                await session.execute(
                    insert(workflows).values(
                        id=workflow.id, version=workflow.version, definition=workflow.model_dump()
                    )
                )

    async def add_agent(self, value: dict[str, Any]) -> None:
        async with self.factory() as session, session.begin():
            exists = (
                await session.execute(select(agents.c.id).where(agents.c.id == value["id"]))
            ).first()
            if exists is None:
                await session.execute(insert(agents).values(id=value["id"], name=value["name"]))
            await session.execute(
                insert(agent_versions).values(
                    id=f"{value['id']}@{value['version']}",
                    agent_id=value["id"],
                    version=value["version"],
                    definition=value,
                )
            )

    async def agents(self) -> list[dict[str, Any]]:
        async with self.factory() as session:
            rows = (await session.execute(select(agent_versions.c.definition))).scalars().all()
            return [dict(row) for row in rows]

    async def nodes(self, run_id: str) -> list[dict[str, Any]]:
        async with self.factory() as session:
            rows = (
                (
                    await session.execute(
                        select(node_executions)
                        .where(node_executions.c.run_id == run_id)
                        .order_by(node_executions.c.finished_at)
                    )
                )
                .mappings()
                .all()
            )
            return [dict(row) for row in rows]

    async def token_total(self, run_id: str) -> int:
        async with self.factory() as session:
            value = (
                await session.execute(
                    select(func.sum(llm_calls.c.tokens_in + llm_calls.c.tokens_out)).where(
                        llm_calls.c.run_id == run_id
                    )
                )
            ).scalar_one_or_none()
            return int(value or 0)

    async def change_status(self, run_id: str, status: str) -> None:
        """审批或重启只改变状态，保留原始预算、种子和运行配置。"""
        async with self.factory() as session, session.begin():
            await session.execute(update(runs).where(runs.c.id == run_id).values(status=status))
