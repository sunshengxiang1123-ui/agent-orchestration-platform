"""有限语法的递归下降解析器，不执行任意 Python 表达式。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias


class ExpressionError(ValueError):
    """条件表达式存在非法语法。"""


class ExpressionTypeError(ExpressionError):
    """比较操作数类型不兼容。"""


class ExpressionPathError(ExpressionError):
    """运行上下文缺少所需路径。"""


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    offset: int


@dataclass(frozen=True)
class PathExpr:
    parts: tuple[str, ...]


@dataclass(frozen=True)
class CompareExpr:
    path: PathExpr
    operator: str
    value: str | int | float | bool | None


@dataclass(frozen=True)
class NotExpr:
    operand: Expr


@dataclass(frozen=True)
class BooleanExpr:
    operator: Literal["and", "or"]
    left: Expr
    right: Expr


Expr: TypeAlias = PathExpr | CompareExpr | NotExpr | BooleanExpr
_TOKEN = re.compile(
    r"(?P<space>\s+)|(?P<op>==|!=|<=|>=|<|>)|(?P<dot>\.)"
    r"|(?P<number>-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
    r"""|(?P<string>"(?:[^"\\\r\n]|\\.)*"|'(?:[^'\\\r\n]|\\.)*')"""
    r"|(?P<identifier>[^\W\d]\w*)",
    re.UNICODE,
)
_RESERVED = {"and", "or", "not", "true", "false", "null"}


def _tokens(source: str) -> list[Token]:
    result: list[Token] = []
    position = 0
    while position < len(source):
        match = _TOKEN.match(source, position)
        if match is None:
            raise ExpressionError(f"位置 {position} 存在非法字符")
        kind = match.lastgroup
        assert kind is not None
        if kind != "space":
            value = match.group()
            result.append(Token(value if value in _RESERVED else kind, value, position))
        position = match.end()
    result.append(Token("eof", "", len(source)))
    return result


def _string_literal(token: Token) -> str:
    text = token.value
    if text.startswith("'"):
        # 单引号字符串只改变引号转义，其他转义仍由 JSON 解码器严格验证。
        inner = text[1:-1]
        escaped: list[str] = []
        position = 0
        while position < len(inner):
            char = inner[position]
            if char == "\\" and position + 1 < len(inner):
                next_char = inner[position + 1]
                escaped.append("'" if next_char == "'" else "\\" + next_char)
                position += 2
            else:
                escaped.append('\\"' if char == '"' else char)
                position += 1
        text = '"' + "".join(escaped) + '"'
    try:
        value: object = json.loads(text)
    except (ValueError, UnicodeError) as exc:
        raise ExpressionError(f"位置 {token.offset} 字符串转义非法") from exc
    assert isinstance(value, str)
    return value


class _Parser:
    def __init__(self, source: str) -> None:
        self.tokens = _tokens(source)
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def take(self, kind: str) -> Token:
        token = self.current
        if token.kind != kind:
            raise ExpressionError(f"位置 {token.offset} 应为 {kind}，实际为 {token.kind}")
        self.index += 1
        return token

    def parse(self) -> Expr:
        expression = self.parse_or()
        self.take("eof")
        return expression

    def parse_or(self) -> Expr:
        left = self.parse_and()
        while self.current.kind == "or":
            self.take("or")
            left = BooleanExpr("or", left, self.parse_and())
        return left

    def parse_and(self) -> Expr:
        left = self.parse_not()
        while self.current.kind == "and":
            self.take("and")
            left = BooleanExpr("and", left, self.parse_not())
        return left

    def parse_not(self) -> Expr:
        if self.current.kind == "not":
            self.take("not")
            return NotExpr(self.parse_not())
        parts = [self.take("identifier").value]
        while self.current.kind == "dot":
            self.take("dot")
            parts.append(self.take("identifier").value)
        path = PathExpr(tuple(parts))
        if self.current.kind != "op":
            return path
        operator = self.take("op").value
        return CompareExpr(path, operator, self.parse_literal())

    def parse_literal(self) -> str | int | float | bool | None:
        token = self.current
        if token.kind == "string":
            self.take("string")
            return _string_literal(token)
        if token.kind == "number":
            self.take("number")
            if any(char in token.value for char in ".eE"):
                number = float(token.value)
                if not math.isfinite(number):
                    raise ExpressionError("数字必须有限")
                return number
            return int(token.value)
        constants = {"true": True, "false": False, "null": None}
        if token.kind in constants:
            self.index += 1
            return constants[token.kind]
        raise ExpressionError(f"位置 {token.offset} 需要数字、字符串或布尔/空值字面量")


def compile_expression(source: str) -> Expr:
    """加载时编译，非法语法立即报错。"""
    if len(source) > 8192:
        raise ExpressionError("表达式超过长度限制")
    try:
        return _Parser(source).parse()
    except RecursionError as exc:
        raise ExpressionError("表达式嵌套过深") from exc


def _resolve(path: PathExpr, context: Mapping[str, Any]) -> Any:
    value: Any = context
    for part in path.parts:
        if not isinstance(value, Mapping) or part not in value:
            raise ExpressionPathError(f"上下文缺少路径：{'.'.join(path.parts)}")
        value = value[part]
    return value


def evaluate(expression: Expr, context: Mapping[str, Any]) -> bool:
    """显式求值，and/or 按从左到右短路；有序比较拒绝混合类型。"""
    if isinstance(expression, PathExpr):
        return bool(_resolve(expression, context))
    if isinstance(expression, NotExpr):
        return not evaluate(expression.operand, context)
    if isinstance(expression, BooleanExpr):
        left = evaluate(expression.left, context)
        if expression.operator == "and":
            return left and evaluate(expression.right, context)
        return left or evaluate(expression.right, context)
    left = _resolve(expression.path, context)
    right = expression.value
    numeric = type(left) in (int, float) and type(right) in (int, float)
    compatible = type(left) is type(right) or numeric
    if expression.operator == "==":
        return compatible and bool(left == right)
    if expression.operator == "!=":
        return not compatible or bool(left != right)
    if not compatible or not (numeric or isinstance(left, str)):
        raise ExpressionTypeError(f"不能对 {type(left).__name__} 与 {type(right).__name__} 排序")
    if expression.operator == "<":
        return bool(left < right)
    if expression.operator == "<=":
        return bool(left <= right)
    if expression.operator == ">":
        return bool(left > right)
    return bool(left >= right)


def evaluate_expression(source: str, context: Mapping[str, Any]) -> bool:
    return evaluate(compile_expression(source), context)
