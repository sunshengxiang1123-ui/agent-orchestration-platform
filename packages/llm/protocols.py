"""LLM 审计存储契约。"""

from typing import Any, Protocol


class CallStore(Protocol):
    async def append(self, record: dict[str, Any]) -> None: ...
