FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.14-slim

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv

COPY src/ src/

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "-m", "src.main"]
