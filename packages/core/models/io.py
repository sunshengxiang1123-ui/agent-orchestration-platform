"""抽象接口的输入输出类型，禁止在类型构造时产生外部副作用。"""

from typing import Any, Literal

from pydantic import BaseModel, Field

ToolPermission = Literal["read", "compute", "propose", "mutate"]


def _default_permissions() -> list[ToolPermission]:
    return ["read", "compute", "propose"]


class LLMResponse(BaseModel):
    content: str = ""
    model: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    raw: dict[str, Any] = Field(default_factory=dict)


class ToolContext(BaseModel):
    run_id: str
    agent_id: str
    allowed_tools: list[str] = Field(default_factory=list)
    permissions: list[ToolPermission] = Field(default_factory=_default_permissions)
    sandbox_root: str | None = None
    values: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    name: str
    success: bool
    content: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Chunk(BaseModel):
    id: str
    path: str
    content: str
    score: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)
