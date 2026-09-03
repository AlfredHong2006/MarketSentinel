# Single-service image for the public read-only MarketSentinel deployment.
#
# One container serves both the React client and the API from one origin, so the browser makes
# same-origin requests and CORS is not involved. The frozen deployment snapshot is baked in, so
# the image is self-contained and needs no database service, no volume, and no API key.
#
# Every setting the public deployment needs is baked in as an ENV default in the runtime stage
# below, so the image runs correctly with no environment file and no secret supplied.
#
# Build:  docker build -t marketsentinel:public .
# Run:    docker run --rm -p 8000:8000 marketsentinel:public

# ---------------------------------------------------------------------------------------------
# Stage 1 -- build the React client.
#
# frontend/.env.production sets VITE_API_BASE_URL to an empty string, which `vite build` picks up
# automatically in production mode. That bakes relative, same-origin request paths into the
# bundle instead of a hardcoded deployment hostname.
# ---------------------------------------------------------------------------------------------
FROM node:20-alpine AS frontend

WORKDIR /build
# Copied before the sources so a dependency layer is only rebuilt when the manifests change.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------------------------
# Stage 2 -- resolve the Python runtime environment.
#
# `uv sync` with no extras installs the public dependency set defined in pyproject.toml: the
# GET-only API and nothing else. torch, transformers and streamlit live in optional extras, so
# this deliberately does NOT install PyTorch or the ~3GB of nvidia-* CUDA wheels it drags in on
# Linux. The public API never imports them -- sentiment scoring only runs behind POST
# /api/v1/analyze, which public mode answers with 404.
# ---------------------------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS dependencies

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app
# pyproject.toml declares both of these as packaging metadata, so the build fails without them.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
# --frozen fails loudly if uv.lock disagrees with pyproject.toml rather than silently resolving
# something different from what was tested. --no-editable installs a real wheel, so the runtime
# stage needs no copy of src/.
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------------------------------------
# Stage 3 -- runtime. Carries no uv, no build tooling, and no Node.
# ---------------------------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# A non-root runtime user. uid 10001 is arbitrary but fixed, so the data directory below can be
# given to it at build time rather than chowned on every container start.
RUN useradd --create-home --uid 10001 marketsentinel

WORKDIR /app

COPY --from=dependencies /app/.venv /app/.venv
COPY --from=frontend /build/dist /app/frontend/dist

# The frozen deployment snapshot, not the live runtime database. Owned by the runtime user
# because SQLite opens the database read-write even for reads: `initialize()` sets
# `PRAGMA journal_mode = WAL` and applies additive column migrations at startup, and WAL needs
# to create -wal/-shm siblings next to the file. The container filesystem is writable and
# ephemeral, which is exactly right here -- public mode writes nothing worth keeping, so every
# restart restores this pristine snapshot.
COPY --chown=marketsentinel:marketsentinel deploy/public-snapshot.db /app/data/marketsentinel.db
# Mandatory, not an optimisation. The read path resolves companies through `resolve_cached`,
# which never reaches the network; without this file every company overview 404s and constituent
# search silently degrades to the 13-company built-in offline subset.
COPY --chown=marketsentinel:marketsentinel deploy/constituents_cache.json /app/data/constituents_cache.json

USER marketsentinel

ENV MARKETSENTINEL_PUBLIC_MODE=true \
    MARKETSENTINEL_DATABASE_PATH=/app/data/marketsentinel.db \
    MARKETSENTINEL_CONSTITUENT_CACHE_PATH=/app/data/constituents_cache.json \
    MARKETSENTINEL_FRONTEND_DIST_PATH=/app/frontend/dist \
    MARKETSENTINEL_ALLOW_DEMO_FALLBACK=false \
    MARKETSENTINEL_CORS_ALLOW_ORIGINS=[] \
    PORT=8000

EXPOSE 8000

# Shell form deliberately, so /bin/sh expands ${PORT} before exec. Every container platform
# assigns the port it will route to through the PORT environment variable, and a runtime variable
# overrides the ENV default above -- so the image binds whatever the host asks for and stays
# vendor-neutral. `EXPOSE 8000` also lets a host that infers the port from image metadata pick
# the same one. Host 0.0.0.0 rather than 127.0.0.1 because the platform routes in from outside
# the container.
#
# One worker deliberately. The price cache is a per-process in-memory TTL, so each extra worker
# multiplies third-party yfinance calls by one more independent cache, and a small free instance's
# memory is better spent on a single warm process than on several cold ones.
CMD uvicorn marketsentinel.api.app:app --host 0.0.0.0 --port "${PORT}" --workers 1
