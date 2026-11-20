"""Agent 团队 YAML 的安全加载。

与工作流加载器同级的防护：只允许 ``yaml.safe_load``，先做字节、深度与别名
展开上限检查，再交给 Pydantic。文件名必须与 ``id`` 一致，避免目录里出现无法
对应角色的定义。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from packages.agents.spec import AgentSpec
from packages.common.errors import PlatformError

MAX_AGENT_BYTES = 64 * 1024
MAX_DEPTH = 16
MAX_EXPANDED_ITEMS = 5_000
DEFAULT_AGENT_DIR = Path(__file__).resolve().parent / "defs"


class AgentLoadError(PlatformError):
    """Agent 定义无法安全加载或结构非法。"""


def load_agent_catalog(root: str | Path | None = None) -> list[AgentSpec]:
    """加载目录下全部 ``*.yaml``，要求 id 唯一并按 id 排序。"""
    base = Path(root) if root is not None else DEFAULT_AGENT_DIR
    if not base.is_dir():
        raise AgentLoadError(f"Agent 定义目录不存在：{base}")
    specs = [load_agent_file(path) for path in sorted(base.glob("*.yaml"))]
    if not specs:
        raise AgentLoadError(f"Agent 定义目录为空：{base}")
    seen: set[str] = set()
    for spec in specs:
        if spec.id in seen:
            raise AgentLoadError(f"Agent id 重复：{spec.id}")
        seen.add(spec.id)
    return sorted(specs, key=lambda item: item.id)


def load_agent_file(path: str | Path) -> AgentSpec:
    target = Path(path)
    if not target.is_file():
        raise AgentLoadError(f"Agent 定义不存在：{target}")
    if target.stat().st_size > MAX_AGENT_BYTES:
        raise AgentLoadError(f"Agent 定义超过 {MAX_AGENT_BYTES} 字节上限")
    spec = load_agent_text(target.read_text(encoding="utf-8"))
    if target.stem != spec.id:
        raise AgentLoadError(f"文件名 {target.name} 与 Agent id {spec.id} 不一致")
    return spec


def load_agent_text(text: str) -> AgentSpec:
    if len(text.encode("utf-8")) > MAX_AGENT_BYTES:
        raise AgentLoadError(f"Agent 定义超过 {MAX_AGENT_BYTES} 字节上限")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AgentLoadError(f"YAML 解析失败：{exc}") from exc
    if not isinstance(document, dict):
        raise AgentLoadError("Agent 定义根节点必须是映射")
    _guard_structure(document)
    try:
        return AgentSpec.model_validate(document)
    except ValidationError as exc:
        raise AgentLoadError(f"Agent 字段非法：{_summarize(exc)}") from exc


def _guard_structure(value: Any) -> None:
    active: set[int] = set()
    visits = 0

    def walk(item: Any, depth: int) -> None:
        nonlocal visits
        visits += 1
        if visits > MAX_EXPANDED_ITEMS:
            raise AgentLoadError("YAML 别名展开规模超过限制")
        if depth > MAX_DEPTH:
            raise AgentLoadError(f"YAML 嵌套超过 {MAX_DEPTH} 层")
        if not isinstance(item, (dict, list)):
            return
        identity = id(item)
        if identity in active:
            raise AgentLoadError("YAML 存在递归别名")
        active.add(identity)
        children = item.values() if isinstance(item, dict) else item
        for child in children:
            walk(child, depth + 1)
        active.discard(identity)

    walk(value, 0)


def _summarize(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    return "；".join(parts)
