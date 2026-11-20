"""Thompson 采样 Agent 选择器。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from random import Random

from packages.core.protocols import AgentProfileStore


@dataclass(frozen=True)
class AgentCandidate:
    agent_id: str
    quality: float = 1.0
    cost: float = 1.0
    latency: float = 1.0


@dataclass(frozen=True)
class AgentScore:
    agent_id: str
    sampled_success: float
    utility: float


class ThompsonSelector:
    """使用注入 Random 的 Beta 后验样本选择 Agent。"""

    def __init__(self, *, latency_weight: float = 1.0, cost_weight: float = 1.0) -> None:
        if latency_weight < 0 or cost_weight < 0:
            raise ValueError("成本和延迟权重不能为负数")
        self.latency_weight = latency_weight
        self.cost_weight = cost_weight

    def rank(
        self,
        candidates: Iterable[AgentCandidate],
        *,
        task_type: str,
        repo: str,
        profiles: AgentProfileStore,
        rng: Random,
    ) -> list[AgentScore]:
        values = list(candidates)
        if not values:
            raise ValueError("至少需要一个候选 Agent")
        max_cost = max(max(0.0, candidate.cost) for candidate in values) or 1.0
        max_latency = max(max(0.0, candidate.latency) for candidate in values) or 1.0
        scores: list[AgentScore] = []
        for candidate in values:
            sampled = min(
                1.0,
                max(0.0, profiles.sample_success_rate(candidate.agent_id, task_type, repo, rng)),
            )
            quality = max(0.0, candidate.quality)
            denominator = (
                self.cost_weight * max(0.0, candidate.cost) / max_cost
                + self.latency_weight * max(0.0, candidate.latency) / max_latency
            )
            utility = sampled * quality / max(denominator, 1e-9)
            scores.append(AgentScore(candidate.agent_id, sampled, utility))
        return sorted(scores, key=lambda item: (-item.utility, item.agent_id))

    def select(
        self,
        candidates: Iterable[AgentCandidate],
        *,
        task_type: str,
        repo: str,
        profiles: AgentProfileStore,
        rng: Random,
    ) -> AgentCandidate:
        values = list(candidates)
        by_id = {candidate.agent_id: candidate for candidate in values}
        score = self.rank(
            values,
            task_type=task_type,
            repo=repo,
            profiles=profiles,
            rng=rng,
        )[0]
        return by_id[score.agent_id]
