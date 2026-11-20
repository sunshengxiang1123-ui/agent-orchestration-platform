"""知识库种子文档：每次启动把 ``seed_docs/*.md`` 同步到最新版本，保证默认就能检索到内容。

文件格式：开头若干 ``key: value`` 行（title、tags），以 ``---`` 分隔正文。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from packages.rag.knowledge import KnowledgeBase, KnowledgeInput

SEED_DIR = Path(__file__).resolve().parent / "seed_docs"


def parse_seed(text: str, fallback_title: str) -> KnowledgeInput:
    header, separator, body = text.partition("\n---\n")
    if not separator:
        header, body = "", text
    fields: dict[str, str] = {}
    for line in header.splitlines():
        key, _, value = line.partition(":")
        if value:
            fields[key.strip().lower()] = value.strip()
    tags = [item.strip() for item in fields.get("tags", "").split(",") if item.strip()]
    return KnowledgeInput(
        title=fields.get("title", fallback_title),
        content=body.strip(),
        tags=tags,
        source="seed",
    )


def _read_all(root: Path) -> list[tuple[Path, str]]:
    return [(path, path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.md"))]


async def seed_knowledge(knowledge: KnowledgeBase, root: Path = SEED_DIR) -> int:
    """把种子文档同步到最新版本，返回新增或更新的篇数。

    缺失的导入；同标题但内容有变化的替换为新版；已从 seed_docs 移除的旧种子删除。
    来源不是 seed 的文档（用户自己添加的）一律不动。
    """
    seeds = [parse_seed(text, path.stem) for path, text in await asyncio.to_thread(_read_all, root)]
    titles = {payload.title for payload in seeds}
    existing = {item.title: item for item in knowledge.list_documents() if item.source == "seed"}
    for title, item in existing.items():
        if title not in titles:
            await knowledge.delete(item.id)
    changed = 0
    for payload in seeds:
        current = existing.get(payload.title)
        if current is not None:
            document = knowledge.get(current.id)
            if (
                document is not None
                and document.content == payload.content
                and document.tags == payload.tags
            ):
                continue
            await knowledge.delete(current.id)
        try:
            await knowledge.add(payload)
        except ValueError:
            continue  # 与用户自建文档内容完全相同，保留用户那份
        changed += 1
    return changed
