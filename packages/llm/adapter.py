"""统一 LLM 入口：每次尝试均写入审计表，只对 429/5xx 做有限退避重试。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from packages.core.models.io import LLMResponse
from packages.core.protocols import LLMClient
from packages.llm.cache import cache_key
from packages.llm.protocols import CallStore
from packages.llm.providers.openai_compat import ProviderError


class LLMAdapter:
    def __init__(
        self,
        *,
        provider: LLMClient,
        store: CallStore,
        run_id: str | None = None,
        node_execution_id: str | None = None,
        max_attempts: int = 4,
        time_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not 1 <= max_attempts <= 4:
            raise ValueError("单次调用尝试次数必须在 1 到 4 之间")
        self.provider, self.store = provider, store
        self.max_attempts = max_attempts
        self.run_id, self.node_execution_id = run_id, node_execution_id
        self.time_fn, self.sleep_fn = time_fn, sleep_fn

    async def _audit(
        self,
        *,
        key: str,
        request: dict[str, Any],
        response: LLMResponse | None,
        duration: float,
        retries: int,
        error: str | None,
    ) -> None:
        await self.store.append(
            {
                "id": str(uuid4()),
                "run_id": self.run_id,
                "node_execution_id": self.node_execution_id,
                "cache_key": key,
                "model": request["model"],
                "mode": "live",
                "request": request,
                "response": response.model_dump(mode="json") if response is not None else None,
                "tokens_in": response.tokens_in if response is not None else 0,
                "tokens_out": response.tokens_out if response is not None else 0,
                "duration": max(0.0, duration),
                "cache_hit": False,
                "retries": retries,
                "error": error,
                "created_at": datetime.now(UTC),
            }
        )

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        seed: int | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        request = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature,
            "seed": seed,
        }
        key = cache_key(
            model=model, messages=messages, tools=tools, temperature=temperature, seed=seed
        )
        for attempt in range(self.max_attempts):
            started = self.time_fn()
            try:
                result = await self.provider.chat(
                    model=model,
                    messages=messages,
                    tools=tools,
                    temperature=temperature,
                    seed=seed,
                    tool_choice=tool_choice,
                )
            except asyncio.CancelledError:
                await self._audit(
                    key=key,
                    request=request,
                    response=None,
                    duration=self.time_fn() - started,
                    retries=attempt,
                    error="调用已取消",
                )
                raise
            except Exception as exc:
                await self._audit(
                    key=key,
                    request=request,
                    response=None,
                    duration=self.time_fn() - started,
                    retries=attempt,
                    error=str(exc),
                )
                transient = isinstance(exc, ProviderError) and (
                    exc.status_code == 429
                    or (exc.status_code is not None and exc.status_code >= 500)
                )
                if not transient or attempt == self.max_attempts - 1:
                    raise
                await self.sleep_fn(float(2**attempt))
                continue
            await self._audit(
                key=key,
                request=request,
                response=result,
                duration=self.time_fn() - started,
                retries=attempt,
                error=None,
            )
            return result
        raise AssertionError("重试循环不应落空")
