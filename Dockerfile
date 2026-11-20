# 单容器部署：数据库与 .env 都放在 /data，挂载卷后重建容器不丢数据。
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    PLATFORM_HOST=0.0.0.0 \
    PLATFORM_PORT=8000 \
    PLATFORM_DATA_DIR=/data \
    PLATFORM_ENV_FILE=/data/.env

WORKDIR /app

# 先只装依赖，代码改动时可复用这一层缓存。
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev

COPY apps ./apps
COPY packages ./packages
COPY scripts ./scripts
COPY migrations ./migrations
COPY alembic.ini README.md ./

# 以 root 运行：Linux 主机上 docker 自动创建的 ./data 属于 root，非 root 用户写不进去。
RUN mkdir -p /data

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

CMD ["python", "-m", "scripts.serve_platform"]
