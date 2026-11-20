"""工具声明、权限校验、参数白名单与幂等调用注册表。"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from packages.core.models.io import ToolContext, ToolPermission, ToolResult

ToolFunction = Callable[[dict[str, Any], ToolContext], ToolResult | Awaitable[ToolResult]]


class ToolRegistryError(ValueError):
    """工具声明或调用不符合平台契约。"""


class ToolNotFoundError(ToolRegistryError):
    """调用了未注册工具。"""


class ToolPermissionError(ToolRegistryError):
    """工具不在白名单或当前上下文没有所需权限。"""


class ToolSchemaError(ToolRegistryError):
    """工具参数不符合声明的 JSON Schema 子集。"""


@dataclass(frozen=True)
class ToolSpec:
    """一个工具的稳定声明。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    permission: ToolPermission = "read"
    idempotency_key: Callable[[dict[str, Any]], str] | None = None
    # 结果随外部状态变化（如知识库检索、按运行查询）时必须关闭缓存。
    cacheable: bool = True
    handler: ToolFunction | None = field(default=None, compare=False, repr=False)

    def key_for(self, args: dict[str, Any]) -> str:
        if self.idempotency_key is not None:
            return self.idempotency_key(args)
        payload = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()


class ToolRegistry:
    """进程内工具注册表；策略拒绝以失败 ToolResult 返回，不执行 handler。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._results: dict[tuple[str, str], ToolResult] = {}

    def register(self, spec: ToolSpec) -> None:
        if not spec.name or ":" in spec.name and spec.name.startswith("mcp:") is False:
            raise ToolRegistryError("工具名称不能为空且不能包含非法命名空间")
        if spec.name in self._specs:
            raise ToolRegistryError(f"工具已注册：{spec.name}")
        if spec.handler is None:
            raise ToolRegistryError(f"工具缺少 handler：{spec.name}")
        _validate_schema_declaration(spec.input_schema)
        self._specs[spec.name] = spec

    def unregister(self, name: str) -> None:
        self._specs.pop(name, None)
        for key in list(self._results):
            if key[0] == name:
                del self._results[key]

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"工具未注册：{name}") from exc

    def list_specs(self) -> list[ToolSpec]:
        return [self._specs[name] for name in sorted(self._specs)]

    def schema(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
                "permission": spec.permission,
            }
            for spec in self.list_specs()
        ]

    async def call(self, name: str, args: dict[str, Any], *, ctx: ToolContext) -> ToolResult:
        try:
            spec = self.get(name)
            self._authorize(spec, args, ctx)
            _validate_args(spec.input_schema, args)
        except ToolRegistryError as exc:
            return ToolResult(name=name, success=False, error=str(exc))

        key = (spec.name, spec.key_for(args))
        if spec.cacheable and key in self._results:
            return self._results[key].model_copy(deep=True)
        assert spec.handler is not None
        try:
            result = spec.handler(args, ctx)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, ToolResult):
                raise ToolRegistryError("工具 handler 必须返回 ToolResult")
        except Exception as exc:
            result = ToolResult(name=name, success=False, error=f"{type(exc).__name__}: {exc}")
        if spec.cacheable and result.success:
            self._results[key] = result.model_copy(deep=True)
        return result

    def _authorize(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext) -> None:
        if spec.name not in ctx.allowed_tools:
            raise ToolPermissionError(f"工具不在 Agent 白名单：{spec.name}")
        if spec.permission not in ctx.permissions:
            raise ToolPermissionError(f"当前上下文没有 {spec.permission} 权限：{spec.name}")


def _validate_schema_declaration(schema: Mapping[str, Any]) -> None:
    if schema.get("type") != "object":
        raise ToolSchemaError("工具参数 schema 必须是 object")
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        raise ToolSchemaError("schema.properties 必须是映射")
    required = schema.get("required", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ToolSchemaError("schema.required 必须是字符串列表")
    if any(name not in properties for name in required):
        raise ToolSchemaError("schema.required 包含未声明参数")


def _validate_args(schema: Mapping[str, Any], args: Mapping[str, Any]) -> None:
    if not isinstance(args, dict):
        raise ToolSchemaError("工具参数必须是对象")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    missing = [name for name in required if name not in args]
    if missing:
        raise ToolSchemaError(f"缺少必填参数：{','.join(missing)}")
    if schema.get("additionalProperties", True) is False:
        unknown = sorted(set(args) - set(properties))
        if unknown:
            raise ToolSchemaError(f"存在未声明参数：{','.join(unknown)}")
    for name, value in args.items():
        declaration = properties.get(name)
        if declaration is None:
            continue
        _validate_value(name, value, declaration)


def _validate_value(name: str, value: Any, declaration: Mapping[str, Any]) -> None:
    expected = declaration.get("type")
    valid = {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
    }
    if expected in valid and not valid[expected]:
        raise ToolSchemaError(f"参数 {name} 应为 {expected}")
