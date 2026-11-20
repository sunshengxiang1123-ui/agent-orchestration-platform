"""本地 .env 的最小改写：只替换指定键，保留其他行与注释，原子写入并收紧权限。"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_UNSAFE = re.compile(r"[\s\"'#\\$`]")


def update_env_file(path: Path, values: Mapping[str, str], *, remove: tuple[str, ...] = ()) -> None:
    """把 ``values`` 写入 ``path``；``remove`` 中的键整行删除。值不允许含引号或空白。"""
    for key, value in values.items():
        if _UNSAFE.search(value):
            raise ValueError(f"{key} 含有空白、引号、# 或 $ 等字符，不能写入 .env")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = dict(values)
    output: list[str] = []
    for line in lines:
        match = _LINE.match(line)
        name = match.group(1) if match else None
        if name in remove:
            continue
        if name is not None and name in pending:
            output.append(f"{name}={pending.pop(name)}")
            continue
        output.append(line)
    output.extend(f"{name}={value}" for name, value in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(output) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
