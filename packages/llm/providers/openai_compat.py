"""OpenAI 兼容传输，只有适配层能调用；测试可注入 httpx 传输。"""

from __future__ import annotations

from typing import Any

import httpx

from packages.common.errors import PlatformError
from packages.core.models.io import LLMResponse


class ProviderError(PlatformError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenAICompatClient:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        base_url: str,
        api_key: str = "",
        max_tokens: int | None = None,
        thinking_enabled: bool | None = None,
    ) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        if max_tokens is not None and max_tokens < 1:
            raise ValueError("输出 token 上限必须为正整数")
        self.max_tokens = max_tokens
        self.thinking_enabled = thinking_enabled

    async def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        seed: int | None = None,
        tool_choice: str | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature}
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens
        if self.thinking_enabled is not None:
            payload["thinking"] = {"type": "enabled" if self.thinking_enabled else "disabled"}
        if tools:
            payload["tools"] = tools
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
        if seed is not None:
            payload["seed"] = seed
        try:
            response = await self.client.post(
                self.base_url + "/chat/completions",
                json=payload,
                follow_redirects=False,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            )
        except httpx.HTTPError as exc:
            raise ProviderError("LLM 传输失败") from exc
        if not 200 <= response.status_code < 300:
            raise ProviderError("LLM 提供方返回错误", status_code=response.status_code)
        try:
            body = response.json()
            message = body["choices"][0]["message"]
            usage = body.get("usage") or {}
            content = message.get("content")
            tool_calls = message.get("tool_calls") or []
            if not (isinstance(content, str) and content.strip()) and not tool_calls:
                raise ProviderError("LLM 未返回有效文本或工具调用")
            return LLMResponse(
                model=body.get("model", model),
                content=content or "",
                tool_calls=tool_calls,
                tokens_in=usage.get("prompt_tokens", 0),
                tokens_out=usage.get("completion_tokens", 0),
                raw=body,
            )
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise ProviderError("LLM 响应格式非法") from exc
