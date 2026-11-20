"""Alembic 入口：--sql 时只生成 SQL，否则连接 PLATFORM_DATABASE_URL 执行迁移。"""

import asyncio

from alembic import context
from sqlalchemy import Connection

from packages.common.config import get_settings
from packages.common.db import create_engine
from packages.common.orm import metadata

config = context.config


def run_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def migrate(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_online() -> None:
    settings = get_settings()
    engine = create_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    asyncio.run(run_online())
