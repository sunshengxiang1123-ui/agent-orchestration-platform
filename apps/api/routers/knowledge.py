"""知识库管理接口：新增、列表、详情、删除与检索。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from apps.api.deps import runtime_of
from packages.rag.knowledge import KnowledgeInput

router = APIRouter(prefix="/api/knowledge", tags=["knowledge"])


class KnowledgeSearch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=5, ge=1, le=20)


@router.get("")
async def list_knowledge(request: Request) -> dict[str, object]:
    knowledge = runtime_of(request).knowledge
    return {
        "items": [item.model_dump() for item in knowledge.list_documents()],
        "stats": knowledge.stats().model_dump(),
    }


@router.get("/stats")
async def knowledge_stats(request: Request) -> dict[str, object]:
    return runtime_of(request).knowledge.stats().model_dump()


@router.post("", status_code=201)
async def create_knowledge(value: KnowledgeInput, request: Request) -> dict[str, object]:
    try:
        document = await runtime_of(request).knowledge.add(value)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return document.model_dump()


@router.post("/search")
async def search_knowledge(value: KnowledgeSearch, request: Request) -> dict[str, object]:
    hits = await runtime_of(request).knowledge.search(value.query, top_k=value.top_k)
    return {
        "query": value.query,
        "count": len(hits),
        "items": [hit.model_dump() for hit in hits],
        "note": "BM25 + 离线词形哈希向量；只做字面召回，不保证语义相关",
    }


@router.get("/{document_id}")
async def get_knowledge(document_id: str, request: Request) -> dict[str, object]:
    document = runtime_of(request).knowledge.get(document_id)
    if document is None:
        raise HTTPException(404, "知识文档不存在")
    return document.model_dump()


@router.delete("/{document_id}")
async def delete_knowledge(document_id: str, request: Request) -> dict[str, object]:
    if not await runtime_of(request).knowledge.delete(document_id):
        raise HTTPException(404, "知识文档不存在")
    return {"deleted": True, "id": document_id}
