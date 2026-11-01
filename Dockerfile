FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    TZ=Asia/Shanghai

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata curl \
 && rm -rf /var/lib/apt/lists/*

# 官方 uv 镜像；次版本与本地生成 uv.lock 的工具链保持一致
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/

WORKDIR /app

# 先只拷依赖清单：uv.lock 不变时命中缓存层，改代码不再重装依赖
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

COPY vault ./vault
COPY db ./db
COPY README.md .

# 安装项目自身，得到完整 .venv
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 7878
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:7878/api/stats >/dev/null || exit 1

CMD ["python", "-m", "vault.cli", "serve"]
