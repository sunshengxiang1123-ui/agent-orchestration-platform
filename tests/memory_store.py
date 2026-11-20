"""知识库测试用的内存文档表。"""

from typing import Any


class MemoryDocuments:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    async def save_document(self, key: str, value: dict[str, Any]) -> None:
        self.values[key] = value

    async def document(self, key: str) -> dict[str, Any] | None:
        return self.values.get(key)

    async def list_documents(self, prefix: str) -> list[dict[str, Any]]:
        return [value for key, value in self.values.items() if key.startswith(prefix)]

    async def delete_document(self, key: str) -> None:
        self.values.pop(key, None)
