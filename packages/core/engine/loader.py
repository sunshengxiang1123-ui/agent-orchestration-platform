"""工作流 YAML 的安全加载与结构校验。

只允许 ``yaml.safe_load``，并在送入 Pydantic 之前施加大小、深度与别名膨胀三道防护；
随后校验节点必填字段、依赖存在性、id 唯一性与无环性。``improvement_epsilon`` 为负数
表示不按改进幅度提前收敛，只受轮数与预算约束。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from packages.common.errors import PlatformError
from packages.core.models.workflow import NodeDef, WorkflowDef

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 32
MAX_EXPANDED_ITEMS = 20000
RESERVED_IDS = frozenset({"run", "iteration", "inputs"})
REQUIRED_FIELD = {
    "agent": "agent",
    "tool": "tool",
    "subflow": "subflow",
    "verify": "verifier",
    "loop": "loop",
    "router": "condition",
}


class WorkflowLoadError(PlatformError):
    """工作流定义无法安全加载或结构非法。"""


def load_workflow(path: str | Path) -> WorkflowDef:
    """从文件加载工作流，读取时先做字节上限检查。"""
    target = Path(path)
    size = target.stat().st_size
    if size > MAX_BYTES:
        raise WorkflowLoadError(f"工作流文件超过 {MAX_BYTES} 字节上限")
    return load_workflow_text(target.read_text(encoding="utf-8"))


def load_workflow_text(text: str) -> WorkflowDef:
    """从 YAML 文本加载工作流，任何非法输入都转为 WorkflowLoadError。"""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise WorkflowLoadError(f"工作流文本超过 {MAX_BYTES} 字节上限")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowLoadError(f"YAML 解析失败：{exc}") from exc
    if not isinstance(document, dict):
        raise WorkflowLoadError("工作流根节点必须是映射")
    _guard_structure(document)
    _validate_raw_loop_fields(document)
    try:
        workflow = WorkflowDef.model_validate(document)
    except ValidationError as exc:
        raise WorkflowLoadError(f"工作流字段非法：{_summarize(exc)}") from exc
    validate_workflow(workflow)
    return workflow


def validate_workflow(workflow: WorkflowDef) -> None:
    """校验全部作用域：节点字段、依赖存在性、唯一性与无环性。"""
    _validate_scope(workflow.nodes, "工作流")


def _validate_scope(nodes: list[NodeDef], scope: str) -> None:
    if not nodes:
        raise WorkflowLoadError(f"{scope} 至少需要一个节点")
    seen: set[str] = set()
    for node in nodes:
        if node.id in seen:
            raise WorkflowLoadError(f"{scope} 存在重复节点 id：{node.id}")
        if node.id in RESERVED_IDS:
            raise WorkflowLoadError(f"节点 id 不得使用保留字：{node.id}")
        seen.add(node.id)
    for node in nodes:
        _validate_node(node, scope)
        for dependency in node.depends_on:
            if dependency not in seen:
                raise WorkflowLoadError(f"{scope}/{node.id} 依赖不存在的节点：{dependency}")
        if node.on_fail is not None and node.on_fail.goto is not None:
            if node.on_fail.goto not in seen:
                raise WorkflowLoadError(
                    f"{scope}/{node.id} 失败跳转目标不存在：{node.on_fail.goto}"
                )
        if node.loop is not None and node.type != "loop":
            raise WorkflowLoadError(f"{scope}/{node.id} 仅在 loop 类型上允许配置 loop")
    _detect_cycle(nodes, scope)
    for node in nodes:
        if node.type == "loop" and node.loop is not None:
            _validate_scope(node.loop.body, f"{scope}/{node.id}")


def _validate_node(node: NodeDef, scope: str) -> None:
    required = REQUIRED_FIELD.get(node.type)
    if required is not None and getattr(node, required) is None:
        raise WorkflowLoadError(f"{scope}/{node.id} 类型 {node.type} 必须提供 {required}")
    if node.type == "loop" and node.loop is not None and node.loop.max_iterations < 1:
        raise WorkflowLoadError(f"{scope}/{node.id} 的 max_iterations 至少为 1")
    if node.type == "loop" and node.loop is not None and not node.loop.body:
        raise WorkflowLoadError(f"{scope}/{node.id} 的循环体不能为空")
    if node.loop is not None:
        if node.loop.budget_tokens is not None and node.loop.budget_tokens < 0:
            raise WorkflowLoadError(f"{scope}/{node.id} 的 budget_tokens 不能为负数")
    if node.on_fail is not None:
        if node.on_fail.max_retry < 0:
            raise WorkflowLoadError(f"{scope}/{node.id} 的 max_retry 不能为负数")
        if node.on_fail.backoff_seconds < 0:
            raise WorkflowLoadError(f"{scope}/{node.id} 的 backoff_seconds 不能为负数")
    if node.n_candidates is not None and node.n_candidates < 1:
        raise WorkflowLoadError(f"{scope}/{node.id} 的 n_candidates 至少为 1")
    if node.speculative_cancel and node.type != "parallel":
        raise WorkflowLoadError(f"{scope}/{node.id} 的 speculative_cancel 仅适用于 parallel")
    if node.on_fail is not None and node.on_fail.goto is not None and node.on_fail.goto == node.id:
        raise WorkflowLoadError(f"{scope}/{node.id} 的 on_fail.goto 不得指向自身")


def _detect_cycle(nodes: list[NodeDef], scope: str) -> None:
    """Kahn 拓扑排序，剩余节点即环的一部分。"""
    indegree = {node.id: len(node.depends_on) for node in nodes}
    dependents: dict[str, list[str]] = {node.id: [] for node in nodes}
    for node in nodes:
        for dependency in node.depends_on:
            dependents[dependency].append(node.id)
    queue = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for child in dependents[current]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if visited != len(nodes):
        raise WorkflowLoadError(f"{scope} 存在循环依赖")


def _guard_structure(value: Any) -> None:
    """检测递归别名，并限制展开规模，防止 YAML 炸弹。"""
    active: set[int] = set()
    visits = 0

    def walk(item: Any, depth: int) -> int:
        nonlocal visits
        visits += 1
        if visits > MAX_EXPANDED_ITEMS:
            raise WorkflowLoadError("YAML 别名展开规模超过限制")
        if depth > MAX_DEPTH:
            raise WorkflowLoadError(f"YAML 嵌套超过 {MAX_DEPTH} 层")
        if not isinstance(item, (dict, list)):
            return 1
        identity = id(item)
        if identity in active:
            raise WorkflowLoadError("YAML 存在递归别名")
        active.add(identity)
        children = item.values() if isinstance(item, dict) else item
        for child in children:
            walk(child, depth + 1)
        active.discard(identity)
        return visits

    walk(value, 0)


def _validate_raw_loop_fields(document: dict[str, Any]) -> None:
    """在 Pydantic 默认值生效前，确认循环显式声明最大轮数。"""

    def walk_scope(nodes: Any, scope: str) -> None:
        if not isinstance(nodes, list):
            return
        for index, raw_node in enumerate(nodes):
            if not isinstance(raw_node, dict):
                continue
            node_id = raw_node.get("id", f"#{index}")
            if raw_node.get("type") == "loop":
                loop = raw_node.get("loop")
                if not isinstance(loop, dict) or "max_iterations" not in loop:
                    raise WorkflowLoadError(
                        f"{scope}/{node_id} 类型为 loop 时必须显式提供 max_iterations"
                    )
                walk_scope(loop.get("body"), f"{scope}/{node_id}")

    walk_scope(document.get("nodes"), "工作流")


def _summarize(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error["loc"])
        parts.append(f"{location}: {error['msg']}")
    return "；".join(parts)
