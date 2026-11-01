# syntax=docker/dockerfile:1.7

# ---- builder: resolve and install dependencies into /app/.venv ----
FROM python:3.11.9-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Only the lockfile and manifest are mounted, so this layer is reused until
# dependencies change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev


# ---- runtime: slim image with just the venv and application code ----
FROM python:3.11.9-slim-bookworm AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app
RUN chown app:app /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app main.py alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app core ./core
COPY --chown=app:app app ./app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=3)"

# Apply migrations, then serve. Single worker: in-flight batch tracking lives in process memory.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn main:app --host 0.0.0.0 --port \"$PORT\" --proxy-headers --forwarded-allow-ips '*'"]
