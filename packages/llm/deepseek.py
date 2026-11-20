"""OpenAI 兼容模型接入（默认 DeepSeek）：配置校验、真实会话与免费连通测试。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx

from packages.common.config import Settings
from packages.common.errors import PlatformError
from packages.llm.adapter import LLMAdapter
from packages.llm.protocols import CallStore
from packages.llm.providers.openai_compat import OpenAICompatClient, ProviderError

PLACEHOLDER_KEYS = {"your_api_key", "your_deepseek_api_key", "<deepseek api key>", "sk-xxx"}


class ModelConfigurationError(PlatformError):
    """模型配置不满足调用前提；错误中不包含凭据。"""


def validate_deepseek_settings(settings: Settings) -> None:
    """只检查配置，不构造网络连接，不输出配置对象或密钥。"""
    key = settings.llm_api_key.get_secret_value()
    if not key or not key.strip():
        raise ModelConfigurationError("尚未配置 API Key，请在“模型配置”页面填写")
    if any(character.isspace() for character in key) or not key.isascii():
        raise ModelConfigurationError("API Key 格式错误：请检查空格、换行或中文字符")
    if key.lower() in PLACEHOLDER_KEYS:
        raise ModelConfigurationError("API Key 仍是占位文字，请填写实际密钥")
    url = urlsplit(settings.llm_base_url)
    try:
        valid_port = url.port is None or 0 < url.port < 65536
    except ValueError:
        valid_port = False
    if (
        url.scheme != "https"
        or not url.hostname
        or not valid_port
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        raise ModelConfigurationError("模型地址必须是不含账号、查询参数或片段的 HTTPS 地址")
    if not settings.llm_model.strip() or any(c.isspace() for c in settings.llm_model):
        raise ModelConfigurationError("模型名不能为空或包含空白字符")


def is_deepseek(settings: Settings) -> bool:
    return urlsplit(settings.llm_base_url).hostname == "api.deepseek.com"


def _client(
    settings: Settings, transport: httpx.AsyncBaseTransport | None, timeout: float
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=10),
        follow_redirects=False,
        transport=transport,
    )


@asynccontextmanager
async def deepseek_session(
    settings: Settings,
    *,
    store: CallStore,
    run_id: str | None = None,
    node_execution_id: str | None = None,
    max_attempts: int = 1,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[LLMAdapter]:
    """每次会话都发起真实请求；所有尝试写入审计表。"""
    validate_deepseek_settings(settings)
    async with _client(settings, transport, settings.llm_timeout_seconds) as client:
        provider = OpenAICompatClient(
            client=client,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),
            max_tokens=settings.llm_max_tokens,
            # thinking 是 DeepSeek 扩展参数，其他兼容服务可能拒绝未知字段。
            thinking_enabled=False if is_deepseek(settings) else None,
        )
        yield LLMAdapter(
            provider=provider,
            store=store,
            run_id=run_id,
            node_execution_id=node_execution_id,
            max_attempts=max_attempts,
        )


async def check_connection(
    settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    """请求 ``GET /models`` 验证地址与密钥；不调用对话接口，不消耗 token。"""
    validate_deepseek_settings(settings)
    async with _client(settings, transport, min(settings.llm_timeout_seconds, 15.0)) as client:
        try:
            response = await client.get(
                settings.llm_base_url + "/models",
                headers={"Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}"},
            )
        except httpx.HTTPError as exc:
            raise ProviderError("无法连接模型服务") from exc
    if not 200 <= response.status_code < 300:
        raise ProviderError("模型服务拒绝请求", status_code=response.status_code)
    try:
        items = response.json().get("data") or []
        models = [str(item["id"]) for item in items if isinstance(item, dict) and "id" in item]
    except (ValueError, AttributeError, TypeError):
        models = []
    return {
        "ok": True,
        "models": models[:50],
        "model_listed": settings.llm_model in models if models else None,
    }
