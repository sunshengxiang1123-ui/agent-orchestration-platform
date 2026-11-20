"""默认仅检查本地配置；--live 仅授权一次最多 64 输出 token 的连通测试。"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from packages.common.config import Settings
from packages.common.db import create_engine, create_session_factory
from packages.common.orm import metadata
from packages.core.models.io import LLMResponse
from packages.llm.cache import SQLCallStore
from packages.llm.deepseek import (
    ModelConfigurationError,
    deepseek_session,
    validate_deepseek_settings,
)
from packages.llm.providers.openai_compat import ProviderError

ROOT = Path(__file__).resolve().parents[1]


async def probe_model(
    settings: Settings,
    output_dir: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LLMResponse:
    """只发送固定的非敏感短提示，审计落本地 SQLite，不读取项目内容。"""
    validate_deepseek_settings(settings)
    bounded = settings.model_copy(update={"llm_max_tokens": min(settings.llm_max_tokens, 64)})
    await asyncio.to_thread(output_dir.mkdir, parents=True, exist_ok=True)
    database = (await asyncio.to_thread(output_dir.resolve)) / "model-check.db"
    engine = create_engine("sqlite+aiosqlite:///" + database.as_posix())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        store = SQLCallStore(create_session_factory(engine))
        async with deepseek_session(
            bounded,
            store=store,
            max_attempts=1,
            transport=transport,
        ) as adapter:
            async with asyncio.timeout(bounded.llm_timeout_seconds + 5):
                return await adapter.chat(
                    model=bounded.llm_model,
                    messages=[{"role": "user", "content": "Reply with the single word OK."}],
                )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 配置检查及显式授权的单次连通测试")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument(
        "--live", action="store_true", help="授权本次真实付费请求；不重试，最多 64 输出 token"
    )
    args = parser.parse_args(argv)
    try:
        settings = Settings(_env_file=args.env_file)
        validate_deepseek_settings(settings)
    except ValidationError:
        print("配置格式错误，请检查本地 .env 的地址、模型和数值字段；未发起请求。")
        return 2
    except ModelConfigurationError as exc:
        print(f"配置未就绪：{exc}；未发起请求。")
        return 2
    if not args.live:
        print("本地配置检查通过，密钥已设置（不显示）；尚未验证密钥或余额，未发起请求。")
        print("需要你确认后再用 --live：只请求一次，输出上限 64 token，不自动重试。")
        return 0
    print("执行一次真实模型连通测试（可能计费），只发送固定短提示，不上传项目文件。")
    try:
        result = asyncio.run(probe_model(settings, ROOT / "reports"))
    except ProviderError as exc:
        explanations = {
            400: "请求参数或模型名不匹配",
            401: "密钥无效或身份验证失败",
            402: "账户余额不足",
            403: "账号或请求权限不足",
            404: "接口或模型不存在",
            429: "请求频率或配额受限",
        }
        detail = explanations.get(exc.status_code or 0, "传输失败、服务异常或响应格式无效")
        print(f"模型连通测试失败：{detail}（HTTP {exc.status_code or '未知'}）；没有自动重试。")
        return 1
    except TimeoutError:
        print("模型连通测试超时，没有自动重试；服务端可能已处理并计费，请先检查后台。")
        return 1
    except (OSError, SQLAlchemyError):
        print("本地审计存储失败；请检查 reports 目录权限。若请求已发出，不能认定未计费。")
        return 1
    if not result.content.strip():
        print("接口有响应但没有文本回复，不能视为文本推理验证通过。")
        return 1
    print(f"模型返回有效文本；输入 {result.tokens_in} token，输出 {result.tokens_out} token。")
    print("审计保存在 reports/model-check.db；这仅验证模型连通，不代表平台已完成竞赛验收。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
