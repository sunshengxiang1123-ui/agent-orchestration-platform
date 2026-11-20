"""单进程启动入口。默认只监听本机回环地址；容器内由 PLATFORM_HOST 改为 0.0.0.0。"""

import os
from pathlib import Path

import uvicorn


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    host = os.environ.get("PLATFORM_HOST", "127.0.0.1")
    port = int(os.environ.get("PLATFORM_PORT", "8000"))
    uvicorn.run("apps.api.main:app", host=host, port=port, workers=1, app_dir=str(root))


if __name__ == "__main__":
    main()
