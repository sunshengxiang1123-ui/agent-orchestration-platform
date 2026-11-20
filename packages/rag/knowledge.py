"""知识库文档、切块、索引与管理服务。

持久化复用 ``platform_documents`` 表（键前缀 ``kb:doc:``），不新增数据库迁移；
索引在内存中重建，规模按单机原型设计，不宣称分布式检索。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from packages.core.models.io import Chunk
from packages.rag.retrieval.bm25 import BM25Retriever
from packages.rag.retrieval.fusion import FusionRetriever
from packages.rag.retrieval.hashing import HashingEmbedder
from packages.rag.retrieval.vector import VectorRetriever
from packages.rag.tokenization import lexical_tokens

logger = logging.getLogger(__name__)

MAX_CONTENT = 200_000
MAX_CHUNKS = 2_000
DEFAULT_MAX_CHARS = 600
DEFAULT_OVERLAP = 80

INDEX_NOTE = "BM25 + 离线词形哈希向量；不调用外部 embedding，不保证语义召回"


class KnowledgeStore(Protocol):
    """知识库只需要文档表的四个操作，便于测试注入内存实现。"""

    async def save_document(self, key: str, value: dict[str, Any]) -> None: ...

    async def document(self, key: str) -> dict[str, Any] | None: ...

    async def list_documents(self, prefix: str) -> list[dict[str, Any]]: ...

    async def delete_document(self, key: str) -> None: ...


class KnowledgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=120)
    content: str = Field(min_length=1, max_length=MAX_CONTENT)
    tags: list[str] = Field(default_factory=list, max_length=10)
    source: str = Field(default="manual", min_length=1, max_length=200)

    @field_validator("tags")
    @classmethod
    def _normalize_tags(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in value:
            tag = item.strip()
            if not tag or len(tag) > 30:
                raise ValueError("标签长度须为 1–30 个字符")
            cleaned.append(tag)
        return list(dict.fromkeys(cleaned))


class KnowledgeChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    ordinal: int
    content: str
    start: int
    end: int


class KnowledgeDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    content: str
    tags: list[str]
    source: str
    created_at: str
    updated_at: str
    sha256: str
    chunks: list[KnowledgeChunk]


class KnowledgeSummary(BaseModel):
    id: str
    title: str
    tags: list[str]
    source: str
    char_count: int
    chunk_count: int
    created_at: str
    updated_at: str
    preview: str


class KnowledgeHit(BaseModel):
    document_id: str
    chunk_id: str
    title: str
    content: str
    score: float
    ordinal: int
    tags: list[str]


class KnowledgeStats(BaseModel):
    documents: int
    chunks: int
    characters: int
    indexes: list[str]
    note: str


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[tuple[int, int, str]]:
    """按段落/句末标点切块，返回 ``(start, end, content)`` 三元组。

    偏移量对应规范化换行后的原文，便于前端回溯。超长段落才使用滑动窗口，
    重叠只用于避免句子被完全切断，不代表语义切分。
    """
    if max_chars < 80:
        raise ValueError("max_chars 至少为 80")
    if not 0 <= overlap < max_chars:
        raise ValueError("overlap 必须大于等于 0 且小于 max_chars")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return []
    pieces: list[tuple[int, int, str]] = []
    start = 0
    total = len(normalized)
    while start < total:
        if total - start <= max_chars:
            end = total
        else:
            end = start + max_chars
            lower = start + max_chars * 3 // 5
            cut = max(
                normalized.rfind("\n", lower, end),
                normalized.rfind("。", lower, end),
                normalized.rfind("！", lower, end),
                normalized.rfind("？", lower, end),
                normalized.rfind("；", lower, end),
                normalized.rfind(". ", lower, end),
            )
            if cut >= lower:
                end = cut + 1
        piece = normalized[start:end]
        if piece.strip():
            pieces.append((start, end, piece))
            if len(pieces) > MAX_CHUNKS:
                raise ValueError(f"切块数量超过 {MAX_CHUNKS} 上限")
        if end >= total:
            break
        next_start = end - overlap
        start = next_start if next_start > start else end
    return pieces


class KnowledgeBase:
    """单进程知识库：写入即重建内存索引，读取不加锁。"""

    PREFIX = "kb:doc:"

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        overlap: int = DEFAULT_OVERLAP,
    ) -> None:
        self.store = store
        self.max_chars = max_chars
        self.overlap = overlap
        self._documents: dict[str, KnowledgeDocument] = {}
        self._chunks: list[Chunk] = []
        self._fusion: FusionRetriever | None = None
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        async with self._lock:
            documents: dict[str, KnowledgeDocument] = {}
            for value in await self.store.list_documents(self.PREFIX):
                try:
                    document = KnowledgeDocument.model_validate(value)
                except ValidationError:
                    logger.warning("跳过无法解析的知识库记录")
                    continue
                documents[document.id] = document
            self._documents = documents
            self._rebuild()

    async def add(self, payload: KnowledgeInput) -> KnowledgeDocument:
        async with self._lock:
            digest = hashlib.sha256(payload.content.encode("utf-8")).hexdigest()
            for existing in self._documents.values():
                if existing.sha256 == digest:
                    raise ValueError(f"相同内容已存在：{existing.id}")
            document_id = "kb-" + uuid4().hex[:10]
            spans = chunk_text(payload.content, max_chars=self.max_chars, overlap=self.overlap)
            if not spans:
                raise ValueError("知识内容不能为空")
            now = _now()
            chunks = [
                KnowledgeChunk(
                    id=f"{document_id}#{ordinal}",
                    ordinal=ordinal,
                    content=content,
                    start=start,
                    end=end,
                )
                for ordinal, (start, end, content) in enumerate(spans)
            ]
            document = KnowledgeDocument(
                id=document_id,
                title=payload.title,
                content=payload.content,
                tags=payload.tags,
                source=payload.source,
                created_at=now,
                updated_at=now,
                sha256=digest,
                chunks=chunks,
            )
            await self.store.save_document(self.PREFIX + document_id, document.model_dump())
            self._documents[document_id] = document
            self._rebuild()
            return document

    async def delete(self, document_id: str) -> bool:
        async with self._lock:
            if document_id not in self._documents:
                return False
            await self.store.delete_document(self.PREFIX + document_id)
            del self._documents[document_id]
            self._rebuild()
            return True

    def get(self, document_id: str) -> KnowledgeDocument | None:
        return self._documents.get(document_id)

    def list_documents(self) -> list[KnowledgeSummary]:
        items = [self._summary(document) for document in self._documents.values()]
        return sorted(items, key=lambda item: (item.updated_at, item.id), reverse=True)

    async def search(self, query: str, *, top_k: int = 5) -> list[KnowledgeHit]:
        normalized = query.strip()
        if not normalized or self._fusion is None:
            return []
        results = await self._fusion.search(normalized, top_k=max(1, min(top_k, 20)))
        hits: list[KnowledgeHit] = []
        for chunk in results:
            document_id = str(chunk.metadata.get("document_id", ""))
            document = self._documents.get(document_id)
            if document is None:
                continue
            hits.append(
                KnowledgeHit(
                    document_id=document.id,
                    chunk_id=chunk.id,
                    title=document.title,
                    content=chunk.content,
                    score=round(float(chunk.score), 6),
                    ordinal=int(chunk.metadata.get("ordinal", 0)),
                    tags=document.tags,
                )
            )
        return hits

    def stats(self) -> KnowledgeStats:
        return KnowledgeStats(
            documents=len(self._documents),
            chunks=len(self._chunks),
            characters=sum(len(document.content) for document in self._documents.values()),
            indexes=["bm25", "hashing-vector"],
            note=INDEX_NOTE,
        )

    def _rebuild(self) -> None:
        self._chunks = [
            Chunk(
                id=chunk.id,
                path=f"kb/{document.id}",
                content=chunk.content,
                metadata={
                    "document_id": document.id,
                    "title": document.title,
                    "ordinal": chunk.ordinal,
                },
            )
            for document in sorted(self._documents.values(), key=lambda item: item.id)
            for chunk in document.chunks
        ]
        if not self._chunks:
            self._fusion = None
            return
        self._fusion = FusionRetriever(
            {
                "bm25": BM25Retriever(self._chunks, tokenizer=lexical_tokens),
                "vector": VectorRetriever(self._chunks, embedder=HashingEmbedder()),
            },
            weights={"bm25": 2.0, "vector": 1.0},
        )

    @staticmethod
    def _summary(document: KnowledgeDocument) -> KnowledgeSummary:
        preview = document.content.replace("\n", " ").strip()[:160]
        return KnowledgeSummary(
            id=document.id,
            title=document.title,
            tags=document.tags,
            source=document.source,
            char_count=len(document.content),
            chunk_count=len(document.chunks),
            created_at=document.created_at,
            updated_at=document.updated_at,
            preview=preview,
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
