# syntax=docker/dockerfile:1.6

# ==============================================================================
# Production Dockerfile — telegram-bot-template
#
# Multi-stage build:
#   1. builder — installs the project and its dependencies into an isolated
#      prefix (/install) so the runtime image stays slim and auditable.
#   2. runtime — copies the prefix + minimal source layout, runs as the
#      non-root `bot` user, and serves /healthz on :8080.
#
# Reproducibility: the base image is pinned to a specific patch version
# (python:3.11.10-slim-bookworm). Bump the tag intentionally when upgrading.
#
# Build:
#   docker build -t telegram-bot-template:local .
#
# Run (with docker-compose):
#   docker compose --profile dev up -d        # polling, debug-friendly
#   docker compose --profile prod up -d       # webhook, behind reverse proxy
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: builder — install project + dependencies into /install prefix
# ------------------------------------------------------------------------------
FROM python:3.11.10-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build

# build-essential is needed only here to compile any C-extensions in deps
# (e.g. asyncpg, pynacl). It is NOT carried into the runtime stage.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# Copy only what pip needs to resolve and install the project. The actual
# application source is copied separately in the runtime stage so changes to
# app/ do not invalidate the dependency-install cache layer (assuming
# pyproject.toml is unchanged).
COPY pyproject.toml ./
COPY app ./app

# --no-cache-dir is also set via PIP_NO_CACHE_DIR above; passing it on the CLI
# makes the intent explicit and survives changes to env vars.
RUN pip install --no-cache-dir --prefix=/install .

# ------------------------------------------------------------------------------
# Stage 2: runtime — minimal image that runs the bot
# ------------------------------------------------------------------------------
FROM python:3.11.10-slim-bookworm AS runtime

# OCI image metadata (Требование 17.5 / image provenance).
# Override at build time, e.g.:
#   docker build \
#     --build-arg VCS_REF=$(git rev-parse HEAD) \
#     --build-arg BUILD_VERSION=0.1.0 \
#     -t ghcr.io/<owner>/telegram-bot-template:0.1.0 .
ARG VCS_REF=unknown
ARG BUILD_VERSION=0.1.0
LABEL org.opencontainers.image.title="telegram-bot-template" \
      org.opencontainers.image.description="Universal modular Telegram bot template (aiogram 3, SQLAlchemy 2 async, Redis, TON Connect)." \
      org.opencontainers.image.version="${BUILD_VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.source="https://github.com/your-org/telegram-bot-template" \
      org.opencontainers.image.url="https://github.com/your-org/telegram-bot-template" \
      org.opencontainers.image.documentation="https://github.com/your-org/telegram-bot-template#readme" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="docker.io/library/python:3.11.10-slim-bookworm"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/install/bin:$PATH \
    PYTHONPATH=/app:/install/lib/python3.11/site-packages

# Runtime needs:
#   - curl: for the HEALTHCHECK below
#   - tini-style init is provided by docker run --init at compose level; not bundled here
# build-essential is intentionally NOT installed here — compiled artefacts come
# from the builder stage via /install.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system bot \
 && useradd --system --gid bot --home-dir /app --shell /usr/sbin/nologin bot

WORKDIR /app

# Bring in the dependency prefix and the application source. We deliberately
# avoid copying tests/, .git/, .venv/, .ruff_cache/ etc. — see .dockerignore.
COPY --from=builder /install /install
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini

# Drop privileges. /app is owned by root but readable by `bot`; the process
# never writes to /app, only to stdout/stderr (logs) and to Postgres/Redis.
USER bot

EXPOSE 8080

# HEALTHCHECK is the single canonical liveness probe (Требование 17.3).
#   --interval     : check every 30s once the container is up
#   --timeout      : fail the probe if /healthz takes >5s to respond
#                    (the endpoint itself targets <2s; 5s gives headroom)
#   --start-period : grace window during which failures don't count toward
#                    the unhealthy threshold (lets DB/Redis come up first)
#   --retries      : 3 consecutive failures flip the container to `unhealthy`
# `curl -f` returns non-zero on HTTP >=400, which is what we want for /healthz
# (200 = healthy, 503 = degraded). The endpoint contract is in app/core/healthcheck.py.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8080/healthz || exit 1

# Single fixed entrypoint: run the package's __main__. We deliberately do NOT
# set a CMD — the bot has only one supported invocation (long-running process)
# and there are no optional flags. Operational tasks (migrations, one-off
# scripts) are run via `docker compose run --rm migrate ...`, which overrides
# the entrypoint explicitly. Avoiding CMD prevents accidental `docker run image arg`
# from being silently passed as positional arguments to `python -m app`.
ENTRYPOINT ["python", "-m", "app"]
