"""知识库：种子导入、切块索引、检索相关性与删除后重建。"""

import pytest

from packages.rag.knowledge import KnowledgeBase, KnowledgeInput
from packages.rag.seeding import SEED_DIR, parse_seed, seed_knowledge
from tests.memory_store import MemoryDocuments


async def seeded() -> KnowledgeBase:
    knowledge = KnowledgeBase(MemoryDocuments())
    await knowledge.load()
    await seed_knowledge(knowledge)
    return knowledge


async def test_seed_imports_every_document_once() -> None:
    knowledge = await seeded()
    count = len(list(SEED_DIR.glob("*.md")))
    assert knowledge.stats().documents == count >= 9
    assert await seed_knowledge(knowledge) == 0  # 重复导入自动跳过


def test_seed_header_is_parsed() -> None:
    parsed = parse_seed("title: 标题\ntags: a, b\n---\n正文内容", "fallback")
    assert (parsed.title, parsed.tags, parsed.content, parsed.source) == (
        "标题",
        ["a", "b"],
        "正文内容",
        "seed",
    )


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("观景塔 预算", "景点指南：示例观景塔"),
        ("交通费 每段 10 元", "费用与交通核算规则"),
        ("植物园 闭园 时间", "景点指南：自然类景点"),
        ("科技馆 老街 古寺", "景点指南：文化类景点"),
        ("超过预算 未覆盖全部兴趣类别 修正", "常见校验失败与修正方法"),
        ("下雨 室内 景点", "雨天与高温天的一日旅行安排"),
        ("带老人 孩子 节奏", "带老人或孩子出游的节奏建议"),
        ("必去景点 开门较晚", "必去景点的处理方法"),
    ],
)
async def test_search_returns_relevant_document_first(query: str, expected: str) -> None:
    hits = await (await seeded()).search(query, top_k=3)
    assert hits and expected in [hit.title for hit in hits]


async def test_add_duplicate_and_delete() -> None:
    knowledge = KnowledgeBase(MemoryDocuments())
    await knowledge.load()
    document = await knowledge.add(
        KnowledgeInput(title="雨天", content="雨天优先安排室内的博物馆。")
    )
    with pytest.raises(ValueError):
        await knowledge.add(KnowledgeInput(title="重复", content="雨天优先安排室内的博物馆。"))
    assert (await knowledge.search("雨天 博物馆"))[0].document_id == document.id
    assert await knowledge.delete(document.id)
    assert await knowledge.search("雨天 博物馆") == []


async def test_seed_sync_updates_changed_and_removes_retired() -> None:
    knowledge = KnowledgeBase(MemoryDocuments())
    await knowledge.load()
    await knowledge.add(KnowledgeInput(title="已下线的旧种子", content="旧内容", source="seed"))
    await knowledge.add(
        KnowledgeInput(title="费用与交通核算规则", content="过时规则", source="seed")
    )
    await knowledge.add(KnowledgeInput(title="我的笔记", content="用户自己的文档"))
    assert await seed_knowledge(knowledge) == len(list(SEED_DIR.glob("*.md")))
    titles = {item.title: item for item in knowledge.list_documents()}
    assert "已下线的旧种子" not in titles and "我的笔记" in titles
    rules = knowledge.get(titles["费用与交通核算规则"].id)
    assert rules is not None and "闭馆日" in rules.content
