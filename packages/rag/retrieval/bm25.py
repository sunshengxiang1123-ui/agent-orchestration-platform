"""轻量 BM25 文本召回，离线替代 PostgreSQL tsvector 的测试实现。"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable

from packages.core.models.io import Chunk

Tokenizer = Callable[[str], list[str]]


class BM25Retriever:
    def __init__(
        self,
        chunks: list[Chunk],
        *,
        k1: float = 1.5,
        b: float = 0.75,
        tokenizer: Tokenizer | None = None,
    ) -> None:
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.tokenizer = tokenizer or _tokenize
        self._tokens = [self.tokenizer(chunk.content) for chunk in chunks]
        self._length = sum(len(tokens) for tokens in self._tokens) / max(len(self._tokens), 1)
        self._df = Counter(token for tokens in self._tokens for token in set(tokens))

    async def search(
        self, query: str, *, top_k: int, filters: dict[str, object] | None = None
    ) -> list[Chunk]:
        query_tokens = self.tokenizer(query)
        scored: list[Chunk] = []
        for chunk, tokens in zip(self.chunks, self._tokens, strict=True):
            counts = Counter(tokens)
            score = 0.0
            for token in query_tokens:
                if token not in counts:
                    continue
                idf = math.log(
                    1 + (len(self.chunks) - self._df[token] + 0.5) / (self._df[token] + 0.5)
                )
                tf = counts[token]
                denominator = tf + self.k1 * (
                    1 - self.b + self.b * len(tokens) / max(self._length, 1e-9)
                )
                score += idf * tf * (self.k1 + 1) / denominator
            if score > 0:
                scored.append(chunk.model_copy(update={"score": score}))
        return sorted(scored, key=lambda item: (-item.score, item.id))[:top_k]


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]+", text.lower())
