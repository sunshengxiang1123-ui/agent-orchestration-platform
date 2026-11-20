"""CAP-DPS 优先级：关键路径、紧急度、置信度、解锁收益与老化。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class PriorityWeights:
    w1: float = 1.0
    w2: float = 1.0
    w3: float = 1.0
    w4: float = 1.0
    w5: float = 1.0
    w6: float = 0.1


@dataclass
class PriorityNode:
    id: str
    depends_on: list[str] = field(default_factory=list)
    duration: float = 1.0
    urgency: float = 0.0
    confidence: float = 0.0
    spent_penalty: float = 0.0
    unlock: float = 0.0
    queued_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class PriorityError(ValueError):
    """优先级输入图非法。"""


class PriorityCalculator:
    """计算 DAG 关键路径和 CAP-DPS 节点评分。"""

    def __init__(self, weights: PriorityWeights | None = None) -> None:
        self.weights = weights or PriorityWeights()
        self._critical_cache: dict[str, float] = {}

    def critical_paths(self, nodes: Iterable[PriorityNode | Mapping[str, Any]]) -> dict[str, float]:
        normalized = {_node_id(node): _as_node(node) for node in nodes}
        successors: dict[str, list[str]] = {node_id: [] for node_id in normalized}
        for node in normalized.values():
            for dependency in node.depends_on:
                if dependency not in normalized:
                    raise PriorityError(f"依赖不存在：{dependency}")
                successors[dependency].append(node.id)
        visiting: set[str] = set()
        cache: dict[str, float] = {}

        def longest(node_id: str) -> float:
            if node_id in cache:
                return cache[node_id]
            if node_id in visiting:
                raise PriorityError("优先级图存在循环")
            visiting.add(node_id)
            node = normalized[node_id]
            value = max((longest(child) for child in successors[node_id]), default=0.0)
            visiting.remove(node_id)
            cache[node_id] = max(0.0, node.duration) + value
            return cache[node_id]

        for node_id in normalized:
            longest(node_id)
        self._critical_cache = dict(cache)
        return dict(cache)

    def score(
        self,
        node: PriorityNode | Mapping[str, Any],
        *,
        critical_paths: Mapping[str, float] | None = None,
        now: datetime | None = None,
    ) -> float:
        value = _as_node(node)
        critical = (critical_paths or self._critical_cache).get(value.id)
        if critical is None:
            critical = self.critical_paths([value])[value.id]
        current = now or datetime.now(UTC)
        queued_at = _aware(value.queued_at)
        aging = max(0.0, (current - queued_at).total_seconds()) * self.weights.w6
        return (
            self.weights.w1 * critical
            + self.weights.w2 * value.urgency
            + self.weights.w3 * value.confidence
            - self.weights.w4 * value.spent_penalty
            + self.weights.w5 * value.unlock
            + aging
        )

    def rank(
        self,
        nodes: Iterable[PriorityNode | Mapping[str, Any]],
        *,
        now: datetime | None = None,
    ) -> list[tuple[PriorityNode, float]]:
        values = [_as_node(node) for node in nodes]
        paths = self.critical_paths(values)
        return sorted(
            ((node, self.score(node, critical_paths=paths, now=now)) for node in values),
            key=lambda item: (-item[1], item[0].id),
        )


def _node_id(node: PriorityNode | Mapping[str, Any]) -> str:
    return node.id if isinstance(node, PriorityNode) else str(node["id"])


def _as_node(node: PriorityNode | Mapping[str, Any]) -> PriorityNode:
    if isinstance(node, PriorityNode):
        return node
    return PriorityNode(
        id=str(node["id"]),
        depends_on=list(node.get("depends_on", [])),
        duration=float(node.get("duration", 1.0)),
        urgency=float(node.get("urgency", 0.0)),
        confidence=float(node.get("confidence", 0.0)),
        spent_penalty=float(node.get("spent_penalty", 0.0)),
        unlock=float(node.get("unlock", 0.0)),
        queued_at=node.get("queued_at", datetime.now(UTC)),
    )


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
