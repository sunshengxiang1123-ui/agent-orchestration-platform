"""运行内黑板：多 Agent 共享结构化中间产物，按声明的键读写。"""

from __future__ import annotations

import copy
from typing import Any, Protocol

from packages.common.errors import PlatformError


class BlackboardKeyError(PlatformError):
    """读取未声明或未写入的黑板键。"""


class Blackboard(Protocol):
    """黑板契约；实现不得把内部引用直接暴露给调用方。"""

    def get(self, key: str) -> Any: ...

    def set(self, key: str, value: Any) -> None: ...

    def snapshot(self) -> dict[str, Any]: ...


class MemoryBlackboard:
    """进程内实现，读写均做深拷贝，供离线测试与单进程运行使用。"""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}

    def get(self, key: str) -> Any:
        if key not in self._values:
            raise BlackboardKeyError(f"黑板缺少键：{key}")
        return copy.deepcopy(self._values[key])

    def set(self, key: str, value: Any) -> None:
        self._values[key] = copy.deepcopy(value)

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._values)
