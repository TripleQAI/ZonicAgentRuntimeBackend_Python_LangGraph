# One uvicorn worker per container (design AD-11); scale containers horizontally.
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-group test --no-install-project

COPY src ./src
COPY resources ./resources
# The AWS deployment's overlays (models.dev.json: OpenAI stand-ins for the Bedrock models until
# Bedrock is available); local runs and the tests read resources/ alone.
COPY deploy/aws/resources/ ./resources/
COPY generated ./generated
RUN uv sync --frozen --no-dev --no-group test

RUN useradd --system --uid 10001 runtime
USER runtime

EXPOSE 8000 9000
# The public port 8000 and the internal port 9000 (/metrics) in one process (zonic_runtime.server).
CMD ["/app/.venv/bin/python", "-m", "zonic_runtime.server"]
