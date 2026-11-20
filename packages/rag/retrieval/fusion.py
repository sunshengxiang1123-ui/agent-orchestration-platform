"""四路召回加权融合、去重与可选重排。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from packages.core.models.io import Chunk


class Retriever(Protocol):
    async def search(
        self, query: str, *, top_k: int, filters: dict[str, object] | None = None
    ) -> list[Chunk]: ...


class FusionRetriever:
    def __init__(
        self,
        retrievers: dict[str, Retriever],
        *,
        weights: dict[str, float] | None = None,
        reranker: Callable[[str, list[Chunk]], list[Chunk] | Awaitable[list[Chunk]]] | None = None,
    ) -> None:
        self.retrievers = retrievers
        self.weights = weights or {"traceback": 4.0, "symbol": 3.0, "bm25": 2.0, "vector": 1.0}
        self.reranker = reranker

    async def search(
        self, query: str, *, top_k: int, filters: dict[str, object] | None = None
    ) -> list[Chunk]:
        merged: dict[str, Chunk] = {}
        for name, retriever in self.retrievers.items():
            result = await retriever.search(query, top_k=top_k, filters=filters)
            weight = self.weights.get(name, 1.0)
            for chunk in result:
                score = chunk.score * weight
                existing = merged.get(chunk.id)
                if existing is None or score > existing.score:
                    merged[chunk.id] = chunk.model_copy(update={"score": score})
        values = sorted(merged.values(), key=lambda item: (-item.score, item.id))[:top_k]
        if self.reranker is not None:
            reranked = self.reranker(query, values)
            values = await reranked if hasattr(reranked, "__await__") else reranked
        return values[:top_k]
