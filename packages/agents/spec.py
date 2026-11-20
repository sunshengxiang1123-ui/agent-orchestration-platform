"""通用 Agent 团队定义的结构校验。

``AgentSpec`` 描述完整角色卡（职责、技能、工具、提示词、容量与采样参数），由
``packages/agents/loader.py`` 从 YAML 安全加载，再经
``packages.application.agents.definition_from_spec`` 转换后注册到调度器。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{0,39}$")
_TAG = re.compile(r"^[\w\u4e00-\u9fff.-]{1,30}$")


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,39}$")
    version: str = Field(default="1.0.0", pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")
    name: str = Field(min_length=1, max_length=60)
    role: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    description: str = Field(min_length=10, max_length=500)
    skills: list[str] = Field(min_length=1, max_length=12)
    tools: list[str] = Field(default_factory=list, max_length=16)
    memory: dict[str, list[str]] = Field(default_factory=dict)
    prompt: str = Field(min_length=20, max_length=4000)
    capacity: int = Field(default=1, ge=1, le=8)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_output_tokens: int = Field(default=1024, ge=64, le=8192)
    tags: list[str] = Field(default_factory=list, max_length=10)

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    @field_validator("skills", "tools")
    @classmethod
    def _identifier_list(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        for item in cleaned:
            if not _IDENTIFIER.fullmatch(item):
                raise ValueError(f"名称不合法：{item}")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("技能或工具不得重复")
        return cleaned

    @field_validator("tags")
    @classmethod
    def _tag_list(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        for item in cleaned:
            if not _TAG.fullmatch(item):
                raise ValueError(f"标签不合法：{item}")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("标签不得重复")
        return cleaned

    @field_validator("memory")
    @classmethod
    def _memory_slots(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for name, slots in value.items():
            if not _IDENTIFIER.fullmatch(name):
                raise ValueError(f"记忆区名称不合法：{name}")
            if len(slots) > 12:
                raise ValueError("单个记忆区最多 12 个槽位")
            for slot in slots:
                if not _IDENTIFIER.fullmatch(slot):
                    raise ValueError(f"记忆槽位不合法：{slot}")
        return value
