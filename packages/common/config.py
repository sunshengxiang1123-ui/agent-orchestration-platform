"""环境配置，导入时不读取凭据、不建立连接；可由“模型配置”页面写回 .env。"""

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_https_url(value: str) -> str:
    url = urlsplit(value.strip())
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
    ):
        raise ValueError("模型地址必须是无账号、查询参数或片段的 HTTPS 地址")
    return value.strip().rstrip("/")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="PLATFORM_",
        extra="ignore",
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    database_url: str = "sqlite+aiosqlite:///:memory:"
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = Field(default="deepseek-flash", min_length=1, max_length=256)
    llm_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("PLATFORM_LLM_API_KEY", "DEEPSEEK_API_KEY"),
    )
    llm_max_tokens: int = Field(default=1024, ge=1, le=8192)
    llm_timeout_seconds: float = Field(default=60.0, gt=0, le=180)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("llm_base_url")
    @classmethod
    def validate_llm_base_url(cls, value: str) -> str:
        return normalize_https_url(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
