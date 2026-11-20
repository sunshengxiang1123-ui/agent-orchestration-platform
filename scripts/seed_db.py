"""把 packages/rag/seed_docs 中的种子文档同步到本地平台数据库（补缺、更新、删除已下线的）。

平台每次启动都会自动同步；不想重启平台时也可以手动运行：
    uv run python -m scripts.seed_db
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from packages.application.store import RuntimeStore
from packages.common.db import create_engine, create_session_factory
from packages.rag.knowledge import KnowledgeBase
from packages.rag.seeding import seed_knowledge

ROOT = Path(__file__).resolve().parents[1]


async def seed() -> int:
    reports = ROOT / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    engine = create_engine("sqlite+aiosqlite:///" + (reports / "platform.db").as_posix())
    try:
        store = RuntimeStore(engine, create_session_factory(engine))
        await store.initialize()
        knowledge = KnowledgeBase(store)
        await knowledge.load()
        return await seed_knowledge(knowledge)
    finally:
        await engine.dispose()


def main() -> None:
    added = asyncio.run(seed())
    print(f"已新增或更新 {added} 篇种子文档。如平台正在运行，请重启以重建索引。")


if __name__ == "__main__":
    main()
