"""Agent Beta 后验与延迟/成本滑动窗口。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from random import Random
from statistics import fmean


@dataclass
class AgentProfile:
    alpha: float = 1.0
    beta: float = 1.0
    latencies: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    costs: deque[int] = field(default_factory=lambda: deque(maxlen=20))

    @property
    def samples(self) -> int:
        return max(0, round(self.alpha + self.beta - 2))

    @property
    def success_rate(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def mean_latency(self) -> float:
        return fmean(self.latencies) if self.latencies else 1.0

    @property
    def mean_cost(self) -> float:
        return fmean(self.costs) if self.costs else 1.0


class BetaProfileStore:
    """按 ``(agent, task_type, repo)`` 隔离的后验和滑动窗口。"""

    def __init__(self, *, min_samples: int = 5, window_size: int = 20) -> None:
        if min_samples < 0 or window_size < 1:
            raise ValueError("min_samples 不能为负，window_size 必须为正")
        self.min_samples = min_samples
        self.window_size = window_size
        self._profiles: dict[tuple[str, str, str], AgentProfile] = {}

    def get(self, agent_id: str, task_type: str, repo: str) -> AgentProfile:
        key = (agent_id, task_type, repo)
        if key not in self._profiles:
            self._profiles[key] = AgentProfile(
                latencies=deque(maxlen=self.window_size),
                costs=deque(maxlen=self.window_size),
            )
        return self._profiles[key]

    def utility(self, agent_id: str, task_type: str, repo: str) -> float:
        profile = self.get(agent_id, task_type, repo)
        denominator = max(profile.mean_latency, 1e-9) * max(profile.mean_cost, 1.0)
        return profile.success_rate / denominator

    def sample_success_rate(
        self,
        agent_id: str,
        task_type: str,
        repo: str,
        rng: Random,
    ) -> float:
        profile = self.get(agent_id, task_type, repo)
        if profile.samples < self.min_samples:
            # 样本不足时用均匀先验抽样，避免把偶然成功固化为过度利用。
            return rng.betavariate(1.0, 1.0)
        return rng.betavariate(profile.alpha, profile.beta)

    async def update(
        self,
        agent_id: str,
        task_type: str,
        repo: str,
        *,
        success: bool,
        latency: float,
        cost: int,
    ) -> None:
        if latency < 0 or cost < 0:
            raise ValueError("延迟和成本不能为负数")
        profile = self.get(agent_id, task_type, repo)
        if success:
            profile.alpha += 1.0
        else:
            profile.beta += 1.0
        profile.latencies.append(float(latency))
        profile.costs.append(int(cost))

    def snapshot(self) -> dict[tuple[str, str, str], AgentProfile]:
        return {key: value for key, value in self._profiles.items()}
