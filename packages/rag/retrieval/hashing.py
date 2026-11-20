"""离线哈希向量：只利用词形，不下载权重、不调用外部 embedding 服务。

它把 :func:`packages.rag.tokenization.lexical_tokens` 的输出哈希到固定维度，
因此只能捕捉字面重合，不能代表语义相似。接口与真实 embedding 一致，后续可
直接替换为带权重的实现。
"""

from __future__ import annotations

import hashlib
import math

from packages.rag.tokenization import lexical_tokens

DEFAULT_DIMENSIONS = 256


class HashingEmbedder:
    """确定性的词形哈希向量；不得用它宣称语义检索效果。"""

    def __init__(self, dimensions: int = DEFAULT_DIMENSIONS) -> None:
        if dimensions < 8:
            raise ValueError("向量维度至少为 8")
        self.dimensions = dimensions

    def __call__(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in lexical_tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            vector[value % self.dimensions] += 1.0 if value & 1 else -1.0
        norm = math.sqrt(sum(item * item for item in vector))
        return [item / norm for item in vector] if norm else vector
