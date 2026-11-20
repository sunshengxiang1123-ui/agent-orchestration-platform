"""离线检索共用的轻量分词：英文标识符 + 中文二元组。

不做词性标注、不做同义词扩展；中文二元组只是让 BM25 能在没有分词模型的
离线环境里匹配短语，不能据此宣称语义理解。
"""

from __future__ import annotations

import re

_ASCII = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]+")


def lexical_tokens(text: str) -> list[str]:
    """把文本切成可复现的词形单元，供 BM25 与哈希向量共用。"""
    lowered = text.lower()
    tokens = _ASCII.findall(lowered)
    for run in _CJK.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens
