"""版本化角色定义与真实调度：技能过滤、负载约束、Beta 后验效用。"""

from collections import deque
from random import Random
from typing import Any, Literal

from pydantic import Field, ValidationError

from packages.agents.loader import AgentLoadError, load_agent_catalog
from packages.agents.profiles import BetaProfileStore
from packages.agents.spec import AgentSpec
from packages.application.travel import StrictModel
from packages.core.scheduler.selector import AgentCandidate, ThompsonSelector

ToolName = Literal["catalog", "constraints", "knowledge", "verify"]


def _tools() -> list[ToolName]:
    return ["catalog", "constraints"]


class AgentDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,39}$")
    version: str = Field(default="1.0.0", pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
    name: str = Field(min_length=1, max_length=60)
    role: Literal["planner", "reviewer"]
    description: str = Field(default="", max_length=500)
    skills: list[Literal["budget", "schedule", "culture", "nature"]] = Field(
        min_length=1, max_length=4
    )
    prompt: str = Field(min_length=1, max_length=4000)
    concurrency: int = Field(default=1, ge=1, le=2)
    tools: list[ToolName] = Field(default_factory=_tools, max_length=4)
    temperature: float = Field(default=0.2, ge=0.0, le=1.5)
    max_output_tokens: int = Field(default=1024, ge=64, le=1024)
    memory: Literal["feedback_and_performance"] = "feedback_and_performance"

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"


def definition_from_spec(spec: AgentSpec) -> AgentDefinition:
    """把 YAML 角色卡转换为运行时调度使用的定义；不匹配本领域时拒绝启动。"""
    try:
        return AgentDefinition(
            id=spec.id,
            version=spec.version,
            name=spec.name,
            role=spec.role,
            description=spec.description,
            skills=spec.skills,
            prompt=spec.prompt,
            concurrency=spec.capacity,
            tools=spec.tools,
            temperature=spec.temperature,
            max_output_tokens=spec.max_output_tokens,
        )
    except ValidationError as exc:
        raise AgentLoadError(f"{spec.id}.yaml 不能用于旅游调度：{exc.errors()[0]['msg']}") from exc


def default_agents(specs: list[AgentSpec] | None = None) -> list[AgentDefinition]:
    """内置团队来自 packages/agents/defs/*.yaml，改 YAML 并递增版本即可上线新角色。"""
    return [definition_from_spec(spec) for spec in (specs or load_agent_catalog())]


class AgentScheduler:
    def __init__(self) -> None:
        self.agents: dict[str, AgentDefinition] = {}
        self.loads: dict[str, int] = {}
        self.profiles = BetaProfileStore(min_samples=0)
        self.selector = ThompsonSelector()

    def candidates(self, role: str) -> list[AgentDefinition]:
        latest: dict[str, AgentDefinition] = {}
        for agent in self.agents.values():
            current = latest.get(agent.id)
            version = tuple(map(int, agent.version.split(".")))
            if current is None or version > tuple(map(int, current.version.split("."))):
                latest[agent.id] = agent
        return [agent for agent in latest.values() if agent.role == role]

    def reserve(
        self, role: str, skills: set[str], rng: Random
    ) -> tuple[AgentDefinition, list[dict[str, Any]]]:
        candidates = self.candidates(role)
        available = [
            agent for agent in candidates if self.loads.get(agent.key, 0) < agent.concurrency
        ]
        if not available:
            raise RuntimeError("没有空闲的匹配角色")
        values = []
        for agent in available:
            profile = self.profiles.get(agent.key, role, "travel")
            values.append(
                AgentCandidate(
                    agent_id=agent.key,
                    quality=1 + len(skills.intersection(agent.skills)),
                    cost=max(1, profile.mean_cost),
                    latency=max(0.01, profile.mean_latency) * (1 + self.loads.get(agent.key, 0)),
                )
            )
        ranked = self.selector.rank(
            values, task_type=role, repo="travel", profiles=self.profiles, rng=rng
        )
        scores = {score.agent_id: score for score in ranked}
        trace = [
            {
                "agent": agent.key,
                "load": self.loads.get(agent.key, 0),
                "capacity": agent.concurrency,
                "available": agent.key in scores,
                "utility": scores[agent.key].utility if agent.key in scores else None,
                "sampled_success": scores[agent.key].sampled_success
                if agent.key in scores
                else None,
                "matching_skills": sorted(skills.intersection(agent.skills)),
            }
            for agent in candidates
        ]
        chosen = self.agents[ranked[0].agent_id]
        self.loads[chosen.key] = self.loads.get(chosen.key, 0) + 1
        return chosen, trace

    def release(self, key: str) -> None:
        self.loads[key] = max(0, self.loads.get(key, 0) - 1)

    def export_profiles(self) -> dict[str, Any]:
        return {
            "items": [
                {
                    "key": list(key),
                    "alpha": value.alpha,
                    "beta": value.beta,
                    "latencies": list(value.latencies),
                    "costs": list(value.costs),
                }
                for key, value in self.profiles.snapshot().items()
            ]
        }

    def restore_profiles(self, value: dict[str, Any]) -> None:
        for item in value.get("items", []):
            profile = self.profiles.get(*item["key"])
            profile.alpha, profile.beta = item["alpha"], item["beta"]
            profile.latencies = deque(item["latencies"], maxlen=20)
            profile.costs = deque(item["costs"], maxlen=20)
