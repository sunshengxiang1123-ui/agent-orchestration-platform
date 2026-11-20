"""工作流定义的稳定数据契约。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from packages.core.engine.expr import compile_expression

NodeType = Literal["agent", "tool", "router", "parallel", "loop", "subflow", "verify", "human_gate"]


class RetryPolicy(BaseModel):
    max_retry: int = 0
    backoff_seconds: float = 1.0
    goto: str | None = None


class LoopConfig(BaseModel):
    max_iterations: int = 5
    budget_tokens: int | None = None
    improvement_epsilon: float = 0.05
    body: list[NodeDef] = Field(default_factory=list)


class NodeDef(BaseModel):
    id: str
    type: NodeType
    depends_on: list[str] = Field(default_factory=list)
    condition: str | None = None
    agent: str | None = None
    tool: str | None = None
    verifier: str | None = None
    subflow: str | None = None
    n_candidates: int | None = None
    speculative_cancel: bool = False
    loop: LoopConfig | None = None
    on_fail: RetryPolicy | None = None
    timeout_seconds: float | None = None

    @field_validator("condition")
    @classmethod
    def validate_condition(cls, value: str | None) -> str | None:
        if value is not None:
            compile_expression(value)
        return value


class WorkflowDef(BaseModel):
    id: str
    version: str = "1.0.0"
    inputs: dict[str, str] = Field(default_factory=dict)
    nodes: list[NodeDef]


LoopConfig.model_rebuild()
