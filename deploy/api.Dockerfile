# syntax=docker/dockerfile:1

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/

WORKDIR /app

# 依赖单独一层,源码改动不触发重装
FROM base AS deps
COPY apps/api/pyproject.toml apps/api/uv.lock* ./
RUN uv sync --no-install-project

# 开发镜像:源码由 compose 挂载,venv 在 /opt/venv 不受挂载影响
FROM deps AS dev
RUN uv sync --no-install-project --group dev
COPY apps/api/ ./
RUN uv sync --group dev
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "guardrail_api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# 生产镜像
FROM deps AS prod
COPY apps/api/ ./
RUN uv sync --no-dev
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "guardrail_api.main:app", "--host", "0.0.0.0", "--port", "8000"]

