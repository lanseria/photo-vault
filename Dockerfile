# ---- 前端：构建镜像时现场编译 ----
# 构建产物（vault/static/）不进 git，镜像自己从源码生成，新克隆也能直接出完整镜像。
FROM node:24-slim AS web
WORKDIR /web
# pnpm 版本与本地锁文件生成工具链保持一致（lockfileVersion 9.0）
RUN npm install -g pnpm@12.16.0

# 先只拷依赖清单：pnpm-lock.yaml 不变时命中缓存层，改前端代码不再重装依赖。
# --allow-build：pnpm 12 的供应链保护默认拦所有依赖的安装脚本，
# esbuild 的 postinstall 只是个二进制自检，显式放行（干净环境里没有本地批准记录）。
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --allow-build=esbuild

COPY frontend/ ./
# 产物落在 /vault/static（vite outDir = ../vault/static）；走 pnpm build，类型检查失败会挡住镜像构建
RUN pnpm build

# ---- 后端 ----
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
# 前端产物来自上面的 web 阶段；本机有没有 vault/static 都不影响镜像
COPY --from=web /vault/static ./vault/static
COPY db ./db
COPY README.md .

# 安装项目自身，得到完整 .venv
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 7878
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:7878/api/stats >/dev/null || exit 1

CMD ["python", "-m", "vault.cli", "serve"]
