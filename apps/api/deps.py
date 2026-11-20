"""API 依赖：只从应用状态取运行时，不在导入时初始化外部服务。"""

from __future__ import annotations

from fastapi import HTTPException, Request

from packages.application.runtime import PlatformRuntime


def runtime_of(request: Request) -> PlatformRuntime:
    instance = getattr(request.app.state, "runtime", None)
    if not isinstance(instance, PlatformRuntime):
        raise HTTPException(503, "运行时尚未启动")
    return instance
