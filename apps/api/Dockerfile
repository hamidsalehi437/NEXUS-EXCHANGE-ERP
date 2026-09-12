# NEXUS EXCHANGE ERP — API image (PART 44, PART 58)
#
# Multi-stage build:
#   * builder  — resolves the exact pinned requirements into a self-contained venv
#   * runtime  — slim Debian with the venv, a non-root user, no compilers
#
# The image runs as an unprivileged user, never bakes secrets in (PART 42: no
# secrets in images), and ships a container health check that hits the liveness
# endpoint only — readiness is a deployment-time concern, not a restart trigger.

# --------------------------------------------------------------------------- builder
FROM python:3.12-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential + libpq-dev are needed to compile wheels for any dependency that
# has no binary wheel for the target platform; they stay out of the runtime image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt requirements-dev.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r requirements.txt

# --------------------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app \
    APP_HOME=/app

# tini reaps zombies and forwards signals so `docker compose stop` is clean.
# curl is used by the health check; libpq is required by psycopg.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl tini libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 nexus \
    && useradd --uid 10001 --gid nexus --home-dir /app --shell /usr/sbin/nologin nexus

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Application code only. tests/ and .env are excluded by .dockerignore.
COPY --chown=nexus:nexus alembic.ini ./
COPY --chown=nexus:nexus alembic ./alembic
COPY --chown=nexus:nexus app ./app
COPY --chown=nexus:nexus seeds ./seeds

# Storage for generated files (backups, exports) is a volume, owned by the app user.
RUN mkdir -p /data/storage && chown -R nexus:nexus /data

USER nexus

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:8000/api/v1/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
# --proxy-headers trusts X-Forwarded-* from nginx (FORWARDED_ALLOW_IPS is set per env).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
