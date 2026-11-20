"""模型配置接口：读取（不含密钥）、保存到本地 .env、免费连通测试。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from apps.api.deps import runtime_of
from packages.common.config import normalize_https_url
from packages.llm.deepseek import ModelConfigurationError, validate_deepseek_settings
from packages.llm.providers.openai_compat import ProviderError

router = APIRouter(prefix="/api/settings", tags=["settings"])

PROVIDER_ERRORS = {
    400: "请求参数不被接受",
    401: "API Key 无效或已失效",
    402: "账户余额不足",
    403: "账号无权访问该接口",
    404: "地址不正确：服务端没有 /models 接口",
    429: "请求过于频繁或配额受限",
}


class ModelSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # 留空表示沿用已保存的密钥；clear_key 为真时清除。
    api_key: str = Field(default="", max_length=512)
    clear_key: bool = False
    base_url: str = Field(min_length=8, max_length=300)
    model: str = Field(min_length=1, max_length=256)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    timeout_seconds: float = Field(default=60.0, gt=0, le=180)

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        return normalize_https_url(value)

    def update(self) -> dict[str, Any]:
        values: dict[str, Any] = {
            "llm_base_url": self.base_url,
            "llm_model": self.model,
            "llm_max_tokens": self.max_tokens,
            "llm_timeout_seconds": self.timeout_seconds,
        }
        if self.clear_key:
            values["llm_api_key"] = SecretStr("")
        elif self.api_key:
            values["llm_api_key"] = SecretStr(self.api_key)
        return values


@router.get("/model")
async def model_settings(request: Request) -> dict[str, Any]:
    return runtime_of(request).model_settings()


@router.put("/model")
async def save_model_settings(value: ModelSettingsInput, request: Request) -> dict[str, Any]:
    runtime = runtime_of(request)
    settings = runtime.preview_settings(value.update())
    if settings.llm_api_key.get_secret_value():
        try:
            validate_deepseek_settings(settings)
        except ModelConfigurationError as exc:
            raise HTTPException(400, str(exc)) from None
    try:
        return await runtime.save_model_settings(settings)
    except (OSError, ValueError) as exc:
        raise HTTPException(500, f"写入 .env 失败：{exc}") from None


@router.post("/model/test")
async def test_model_settings(value: ModelSettingsInput, request: Request) -> dict[str, Any]:
    """用表单当前的值测试（未保存也可以）；只请求 /models，不消耗 token。"""
    runtime = runtime_of(request)
    settings = runtime.preview_settings(value.update())
    try:
        result = await runtime.test_model_settings(settings)
    except ModelConfigurationError as exc:
        raise HTTPException(400, str(exc)) from None
    except ProviderError as exc:
        detail = PROVIDER_ERRORS.get(exc.status_code or 0, str(exc))
        status = f"HTTP {exc.status_code}" if exc.status_code else "网络错误"
        raise HTTPException(502, f"连接失败（{status}）：{detail}") from None
    return result
