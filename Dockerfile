# One uvicorn worker per container (design AD-11); scale containers horizontally.
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-group test --no-install-project

COPY src ./src
COPY resources ./resources
COPY generated ./generated
RUN uv sync --frozen --no-dev --no-group test

RUN useradd --system --uid 10001 runtime
USER runtime

EXPOSE 8000 9000
CMD ["/app/.venv/bin/uvicorn", "zonic_runtime.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--loop", "uvloop", "--timeout-graceful-shutdown", "60"]
