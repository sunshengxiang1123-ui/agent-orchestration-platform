"""LLM 令牌、容器池和 CPU 并发三类独立准入约束。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


class AdmissionRejected(RuntimeError):
    """资源暂时不足，任务需要背压或稍后重试。"""


@dataclass
class AdmissionLease:
    controller: AdmissionController
    key: str
    cpu: int
    released: bool = False

    async def release(self) -> None:
        if not self.released:
            self.released = True
            await self.controller._release(self.key, self.cpu)

    async def __aenter__(self) -> AdmissionLease:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.release()


class AdmissionController:
    """以独立计数器约束容器池和 CPU，并委托 LLM 令牌桶。"""

    def __init__(
        self,
        *,
        cpu_limit: int,
        container_limits: dict[str, int] | None = None,
        default_container_limit: int = 1,
        llm_limiter: Any | None = None,
    ) -> None:
        if cpu_limit < 1 or default_container_limit < 1:
            raise ValueError("CPU 和容器并发上限必须为正数")
        self.cpu_limit = cpu_limit
        self.container_limits = container_limits or {}
        self.default_container_limit = default_container_limit
        self.llm_limiter = llm_limiter
        self._cpu_in_use = 0
        self._containers: dict[str, int] = {}
        self._condition = asyncio.Condition()

    async def acquire(
        self,
        *,
        repo: str,
        image: str,
        cpu: int = 1,
        tokens: int = 0,
        wait: bool = True,
    ) -> AdmissionLease:
        if cpu < 1 or tokens < 0:
            raise ValueError("cpu 必须为正数，tokens 不能为负数")
        if cpu > self.cpu_limit:
            raise AdmissionRejected("单任务 CPU 需求超过上限")
        key = f"{repo}:{image}"
        async with self._condition:
            while not self._can_reserve(key, cpu):
                if not wait:
                    raise AdmissionRejected("容器池或 CPU 并发达到上限")
                await self._condition.wait()
            self._cpu_in_use += cpu
            self._containers[key] = self._containers.get(key, 0) + 1
        try:
            await self._acquire_llm(tokens, wait=wait)
        except Exception:
            await self._release(key, cpu)
            raise
        return AdmissionLease(self, key, cpu)

    def _can_reserve(self, key: str, cpu: int) -> bool:
        limit = self.container_limits.get(key, self.default_container_limit)
        return self._cpu_in_use + cpu <= self.cpu_limit and self._containers.get(key, 0) < limit

    async def _acquire_llm(self, tokens: int, *, wait: bool) -> None:
        if self.llm_limiter is None or tokens == 0:
            return
        if wait:
            await self.llm_limiter.acquire(tokens)
            return
        try_nowait = getattr(self.llm_limiter, "try_acquire_nowait", None)
        if try_nowait is None or not bool(try_nowait(tokens)):
            raise AdmissionRejected("LLM 令牌桶达到上限")

    async def _release(self, key: str, cpu: int) -> None:
        async with self._condition:
            self._cpu_in_use = max(0, self._cpu_in_use - cpu)
            active = self._containers.get(key, 0) - 1
            if active <= 0:
                self._containers.pop(key, None)
            else:
                self._containers[key] = active
            self._condition.notify_all()

    @property
    def cpu_in_use(self) -> int:
        return self._cpu_in_use

    def container_in_use(self, repo: str, image: str) -> int:
        return self._containers.get(f"{repo}:{image}", 0)
