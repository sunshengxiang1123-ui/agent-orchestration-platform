"""运行与节点快照的稳定数据契约。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

NodeStatus = Literal[
    "pending", "ready", "running", "succeeded", "failed", "skipped", "cancelled", "awaiting_human"
]


class NodeExecution(BaseModel):
    id: str
    run_id: str
    node_id: str
    iteration: int = 0
    candidate_index: int | None = None
    status: NodeStatus
    agent_id: str | None = None
    agent_version: str | None = None
    prompt_variant: str | None = None
    input_snapshot: dict[str, Any]
    output_snapshot: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    tokens_in: int = 0
    tokens_out: int = 0


class Run(BaseModel):
    id: str
    workflow_id: str
    workflow_version: str
    status: Literal["running", "succeeded", "failed", "cancelled", "awaiting_human"]
    inputs: dict[str, Any]
    seed: int
    mode: Literal["live"] = "live"
    budget_tokens: int | None = None
    spent_tokens: int = 0
