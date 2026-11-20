"""稳定请求摘要与数据库审计存储。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.common.orm import llm_calls


def cache_key(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None,
    temperature: float,
    seed: int | None,
) -> str:
    """请求摘要，用于审计记录中定位同一请求；规范 JSON 避免分隔符碰撞。"""
    payload = [model, messages, tools, float(temperature), seed]
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


class SQLCallStore:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    async def append(self, record: dict[str, Any]) -> None:
        async with self.factory() as session, session.begin():
            await session.execute(insert(llm_calls).values(**record))
