FROM python:3.14-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY data ./data
COPY alembic alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
# Web process. The worker fleet runs the same image with:
#   python -m waypoint.worker
CMD ["sh", "-c", "uvicorn waypoint.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
