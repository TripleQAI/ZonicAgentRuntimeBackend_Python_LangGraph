# The load container (plan P5-02): the runtime's image plus the test code, run at the target
# task's size: docker run --cpus 1 --memory 2g (docs/load/p5-02-report.md).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project
COPY src ./src
COPY resources ./resources
COPY generated ./generated
COPY tests ./tests
RUN uv sync --frozen

EXPOSE 8000 9000
CMD ["/app/.venv/bin/python", "-m", "tests.load.server"]
