"""模型配置写回 .env：只改指定键、删除优先级更高的别名、权限 600。"""

import stat
from pathlib import Path

import pytest

from packages.common.envfile import update_env_file


def test_update_preserves_other_lines(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    original = "# 注释\nPLATFORM_LLM_API_KEY=old\nDEEPSEEK_API_KEY=\nOTHER=1\n"
    path.write_text(original, encoding="utf-8")
    update_env_file(
        path,
        {"DEEPSEEK_API_KEY": "sk-new", "PLATFORM_LLM_MODEL": "m"},
        remove=("PLATFORM_LLM_API_KEY",),
    )
    assert path.read_text(encoding="utf-8") == (
        "# 注释\nDEEPSEEK_API_KEY=sk-new\nOTHER=1\nPLATFORM_LLM_MODEL=m\n"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [item.name for item in tmp_path.iterdir()] == [".env"]


@pytest.mark.parametrize("value", ["a b", "a#b", 'a"b', "a$b"])
def test_unsafe_values_are_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError):
        update_env_file(tmp_path / ".env", {"KEY": value})
