"""限制 LLM 出口和单元测试网络导入，识别常见导入别名。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_FRAMEWORKS = {"langchain", "langgraph", "autogen", "crewai", "dify"}
NETWORK_MODULES = {
    "requests",
    "aiohttp",
    "urllib",
    "urllib3",
    "socket",
    "http",
    "httpcore",
    "ftplib",
    "smtplib",
    "websockets",
    "grpc",
    "openai",
    "anthropic",
}
LLM_SDKS = {"openai", "anthropic", "litellm", "dashscope", "google.generativeai"}
ENDPOINT_METHODS = {"post", "put", "request", "stream", "send", "urlopen", "fetch"}


def _qualified(node: ast.AST, aliases: dict[str, str]) -> str:
    if isinstance(node, ast.Name):
        return aliases.get(node.id, node.id)
    if isinstance(node, ast.Attribute):
        prefix = _qualified(node.value, aliases)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _qualified(node.func, aliases)
    return ""


def scan_source(source: str, relative: Path) -> list[str]:
    tree = ast.parse(source, filename=str(relative))
    parts = relative.parts
    is_unit = parts[:2] == ("tests", "unit")
    is_llm = parts[:2] == ("packages", "llm")
    is_business = parts[0] in {"packages", "apps"}
    aliases: dict[str, str] = {}
    errors: list[str] = []

    def fail(node: ast.AST, message: str) -> None:
        errors.append(f"{relative.as_posix()}:{getattr(node, 'lineno', 1)}：{message}")

    for node in ast.walk(tree):
        imports: list[tuple[str, str]] = []
        if isinstance(node, ast.Import):
            imports = [(item.asname or item.name.split(".")[0], item.name) for item in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports = [
                (item.asname or item.name, f"{node.module}.{item.name}") for item in node.names
            ]
        for local, module in imports:
            aliases[local] = module
            root = module.split(".")[0]
            if any(root == name or root.startswith(name + "_") for name in FORBIDDEN_FRAMEWORKS):
                fail(node, "禁止引入第三方编排框架")
            if is_unit and root in NETWORK_MODULES:
                fail(node, "单元测试不得导入 httpx 以外的网络库")
            if (
                is_business
                and not is_llm
                and any(module == name or module.startswith(name + ".") for name in LLM_SDKS)
            ):
                fail(node, "LLM SDK 只能在 packages/llm 内使用")

    # 简单赋值别名传播，让 httpx 客户端改名不能绕过边界。
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            name = _qualified(node.value, aliases)
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and name:
                    aliases[target.id] = name

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _qualified(node.func, aliases)
        if is_business and not is_llm:
            root = name.split(".")[0]
            method = name.rsplit(".", 1)[-1]
            if root in NETWORK_MODULES | {"httpx"} and method in ENDPOINT_METHODS:
                fail(node, "业务代码不得直接构造 HTTP 请求；LLM 请求须经适配层")
            if "chat.completions.create" in name or "responses.create" in name:
                fail(node, "LLM 调用必须经 packages/llm")
        if parts[:3] == ("packages", "core", "engine") and name in {"eval", "exec"}:
            fail(node, "引擎禁止动态执行表达式")
    return errors


def main() -> int:
    errors: list[str] = []
    for folder in ("packages", "apps", "tests/unit"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            errors.extend(scan_source(path.read_text(encoding="utf-8"), path.relative_to(ROOT)))
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("LLM 出口与单元测试网络边界检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
