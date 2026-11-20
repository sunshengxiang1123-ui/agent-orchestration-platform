"""跨模块调用契约：模型客户端与 Agent 历史表现存储。"""

from __future__ import annotations

from random import Random
from typing import Any, Protocol, runtime_checkable

from packages.core.models.io import LLMResponse


@runtime_checkable
class LLMClient(Protocol):
    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        seed: int | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse: ...


@runtime_checkable
class AgentProfileStore(Protocol):
    def utility(self, agent_id: str, task_type: str, repo: str) -> float: ...

    def sample_success_rate(
        self, agent_id: str, task_type: str, repo: str, rng: Random
    ) -> float: ...

    async def update(
        self,
        agent_id: str,
        task_type: str,
        repo: str,
        *,
        success: bool,
        latency: float,
        cost: int,
    ) -> None: ...
