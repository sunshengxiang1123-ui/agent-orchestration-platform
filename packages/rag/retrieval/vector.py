"""可选向量召回；没有离线权重时返回空结果并允许三路降级。"""

from __future__ import annotations

import warnings
from collections.abc import Callable

from packages.core.models.io import Chunk


class VectorRetriever:
    def __init__(
        self,
        chunks: list[Chunk],
        *,
        embedder: Callable[[str], list[float]] | None = None,
    ) -> None:
        self.chunks = chunks
        self.embedder = embedder
        if embedder is None:
            warnings.warn("Embedding 权重不可用，向量召回降级为空", RuntimeWarning, stacklevel=2)

    async def search(
        self, query: str, *, top_k: int, filters: dict[str, object] | None = None
    ) -> list[Chunk]:
        if self.embedder is None:
            return []
        target = self.embedder(query)
        scored = []
        for chunk in self.chunks:
            vector = self.embedder(chunk.content)
            score = _cosine(target, vector)
            scored.append(chunk.model_copy(update={"score": score}))
        return sorted(scored, key=lambda item: (-item.score, item.id))[:top_k]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    return numerator / (left_norm * right_norm) if left_norm and right_norm else 0.0
