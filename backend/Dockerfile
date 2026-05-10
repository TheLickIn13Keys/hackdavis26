# SafePath backend — FastAPI service.
#
# Build context expected: the repo root (so we can also copy ../demo/ for
# the standalone agent-walk UI served at /demo). Build with:
#   docker build -f backend/Dockerfile -t safebike-backend .
# or via docker-compose at the repo root.

# ---------- builder: install deps with uv into a venv -----------------------
FROM python:3.11-slim AS builder

# uv = fast Python package manager. Pinned to a specific image to keep
# rebuilds reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.5.18 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Cache wheel install separately from app code so editing app/ doesn't bust
# the heavy osmnx/geopandas layer.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY backend/app ./app
RUN uv sync --frozen --no-dev

# ---------- runtime: slim image with the venv copied in ---------------------
FROM python:3.11-slim AS runtime

# tini gives proper signal handling for uvicorn under docker stop.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Drop privileges. Numeric uid:gid is friendlier to bind-mounted volumes
# than referring to a name docker can't resolve in this layer.
RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid 1001 --no-create-home app

COPY --from=builder /app/.venv /app/.venv
COPY backend/app ./app
# main.py resolves Path(__file__).parents[2] / "demo" to find the static
# demo UI; in the container that lands at /demo.
COPY demo /demo

# Persisted artifacts (SQLite DB, cached Street View JPEGs). Bind a host
# directory or named volume here in compose to keep them across rebuilds.
RUN mkdir -p /app/data/images /app/cache \
 && chown -R app:app /app /demo

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
