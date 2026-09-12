# NEXUS EXCHANGE ERP — Deployment and Operations Runbook

| Field | Value |
| --- | --- |
| Document ID | `OPS-DEPLOY-001` |
| Version | 1.0 (Phase 1) |
| Status | **Delivered — Phase 1 verification in progress** |
| Owner | Operations |
| Related | `docs/architecture/ARCHITECTURE.md` §4, `docs/database/SCHEMA.md` §8, `docs/security/SECURITY.md` |
| Scope | The five-service Docker Compose stack (PART 44) and the native (no-container) developer path |

> **خلاصه فارسی** — این سند نحوهٔ راه‌اندازی، مهاجرت، بذرکاری، راستی‌آزمایی و بهره‌برداری از سامانه را روی پشتهٔ پنج‌سرویسی (api، postgres، redis، nginx، worker) توضیح می‌دهد. هیچ رمزی در مخزن یا در فایل `docker-compose.yml` نوشته نمی‌شود؛ همهٔ رازها از `.env` با شکل «الزامی» خوانده می‌شوند تا اگر مقداری نباشد، اجرا به‌جای بالا آمدن با رمز خالی شکست بخورد. پایگاه داده با دو نقش جدا کار می‌کند: `nexus_api` (فقط DML، بدون حذف از دفاتر) برای سرویس‌ها و `nexus_migrator` (مالک اسکیما) برای Alembic و بذرها.

---

## 1. What the stack contains

| Service | Image / build | Published | Purpose |
| --- | --- | --- | --- |
| `nginx` | `nginx:1.27-alpine` | `${NGINX_HTTP_PORT:-8080}` → 80 | The only public entry point; proxy, rate limiting, security headers |
| `api` | `apps/api/Dockerfile` (python:3.12-slim) | internal only (`expose: 8000`) | FastAPI application (ASGI, uvicorn) |
| `worker` | same image as `api` | internal only | Celery worker + beat: integrity checks, token sweep, idempotency retention |
| `postgres` | `postgres:16-bookworm` | `127.0.0.1:${POSTGRES_HOST_PORT:-5433}` | Source of truth (PART 64); `scram-sha-256`, page checksums, UTC |
| `redis` | `redis:7.4-alpine` | `127.0.0.1:${REDIS_HOST_PORT:-6380}` | Celery broker (db 1), result backend (db 2), runtime cache (db 0) |

Volumes: `nexus-postgres-data`, `nexus-redis-data`, `nexus-api-storage` (generated files: exports, backups).
Network: `nexus-backend` (bridge, fixed subnet `172.28.0.0/24` — the API trusts this CIDR for `X-Forwarded-*`).

The API container is **not** published. All access is through nginx, which passes the public `Host` header through so that the application's `TRUSTED_HOSTS` allow-list is evaluated against the real host.

## 2. Requirements

| Requirement | Minimum | Notes |
| --- | --- | --- |
| Docker Engine | 24+ | Compose v2.20+ (`docker compose up --wait`) |
| Docker Compose | v2.20+ | `--wait`, `config -q` |
| RAM | 4 GB | PostgreSQL `shared_buffers=256MB`, Argon2id 64 MiB per hash |
| Disk | 10 GB | Database volume grows with the ledger; plan backups separately |
| Host ports | 8080, 5433, 6380 | Change with `NGINX_HTTP_PORT`, `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT` |
| OS | Linux (production), macOS/Windows (development) | PART 58 targets Linux; the same stack runs elsewhere |

## 3. First start

```bash
git clone <repository> nexus-exchange && cd nexus-exchange
scripts/gen_env.sh                  # writes .env (mode 600) with fresh random secrets
scripts/dev_up.sh                   # build → up --wait → alembic upgrade head → seeds → readiness
```

`scripts/dev_up.sh` is safe to re-run. Manually, the same sequence is:

```bash
docker compose up -d --build --wait                    # all five services healthy
docker compose exec -T api alembic upgrade head        # schema (frozen DDL, checksum-verified)
docker compose exec -T api python -m seeds             # currencies, roles, permissions, chart of accounts
curl -fsS http://127.0.0.1:8080/api/v1/health/ready    # {"status":"ready", ...}
```

Expected readiness payload (trimmed):

```json
{"status": "ready", "environment": "development",
 "components": [{"name": "postgresql", "status": "ok", "detail": "schema revision 0002_runtime_schema_revision"},
                {"name": "redis", "status": "ok"}],
 "schema_revision": "0002_runtime_schema_revision"}
```

The developer administrator (`DEV_ADMIN_USERNAME`) is created **only** when `APP_ENV=development` **and** `DEV_ADMIN_PASSWORD` is non-empty; the seed refuses to run in production, and `Settings` refuses to load a production configuration that carries a development password. This applies to Phase 2 endpoints.

### Secrets

`.env` is generated locally and never committed (`.gitignore`, `.dockerignore`). Every secret is read by Compose as a required variable (`${VAR:?}`), so a missing value stops the stack instead of starting it with an empty password. Rotation:

```bash
scripts/gen_env.sh --force            # regenerate all secrets
docker compose up -d --force-recreate # api/worker pick up the new values
```

For a database credential change, the login roles are also re-synchronised by `infrastructure/postgres/init/01-roles.sh` (it is idempotent; run it only through the documented bootstrap path, since it is an `initdb` script in the container).

### Database roles (least privilege)

`infrastructure/postgres/init/01-roles.sh` creates, on first initialisation:

| Role | Kind | Used by | Rights |
| --- | --- | --- | --- |
| `nexus_owner` | group (NOLOGIN) | — | owns the schema |
| `nexus_app` | group (NOLOGIN) | — | `SELECT/INSERT/UPDATE`; no `DELETE` on ledgers, documents, master data; no `UPDATE` on `audit_logs`, `journal_lines`, `cash_movements`; `DELETE` on `idempotency_keys` only (documented retention) |
| `nexus_reader`, `nexus_auditor` | group (NOLOGIN) | reporting / auditors | `SELECT` only |
| `nexus_migrator` | **login** | `DATABASE_MIGRATION_URL` (alembic, seeds) | inherits `nexus_owner`; the only role with DDL rights |
| `nexus_api` | **login** | `DATABASE_URL` (uvicorn, celery) | inherits `nexus_app` |

The suite proves the split: the runtime role is denied `DELETE`/`UPDATE` on the ledger and `DELETE` on master data even when the application is bypassed (`tests/invariants/phase0_schema_invariants.sql`, I-6 privileges).

## 4. Running without Docker

Useful for a developer machine or a host where containers are not allowed.

```bash
# 1. PostgreSQL 16 and Redis 7 must be running locally.
createdb nexus_exchange

# 2. Create the roles exactly as the container bootstrap does. The script is plain
#    psql: group roles, the two login roles, and the schema grants the migration
#    role needs. It is idempotent and never writes a password to a file.
export PGHOST=127.0.0.1 POSTGRES_USER=<superuser> POSTGRES_DB=nexus_exchange
export NEXUS_API_PASSWORD=... NEXUS_MIGRATOR_PASSWORD=...
bash infrastructure/postgres/init/01-roles.sh

# 3. Point .env at 127.0.0.1 (host, port, role) and install dependencies:
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r apps/api/requirements.txt

# 4. Migrate, seed, run (migrations/seeds use DATABASE_MIGRATION_URL, the services DATABASE_URL):
cd apps/api
alembic upgrade head
python -m seeds
uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
celery -A app.worker.celery_app:celery_app worker --beat --queues=integrity,maintenance
```

Both DSNs in `.env` use the **async** scheme (`postgresql+asyncpg://`): the settings model validates them uniformly and derives the synchronous psycopg DSN for Alembic and the seeds itself.

## 5. Everyday operations

| Task | Command |
| --- | --- |
| Status | `docker compose ps` |
| Logs (json) | `docker compose logs -f api worker` |
| Health | `curl -fsS http://127.0.0.1:8080/api/v1/health` |
| Readiness (dependencies) | `curl -fsS http://127.0.0.1:8080/api/v1/health/ready` |
| Build metadata | `curl -fsS http://127.0.0.1:8080/api/v1/version` |
| Apply migrations | `scripts/migrate.sh` (or `docker compose exec -T api alembic upgrade head`) |
| See SQL a migration would run | `scripts/migrate.sh --dry-run` |
| Seed status | `scripts/seed.sh --check` (exit 1 when something would change) |
| Re-run seeds | `scripts/seed.sh` |
| Integrity check on demand | `docker compose exec -T worker celery -A app.worker.celery_app:celery_app call app.worker.integrity.verify_ledger_integrity` |
| Verify the audit chain | `docker compose exec -T worker celery -A app.worker.celery_app:celery_app call app.worker.integrity.verify_audit_chain` |
| Stop | `scripts/dev_down.sh` (add `--volumes` to delete data — development only) |
| Upgrade | backup → `docker compose pull` → `docker compose up -d --build --wait` → `scripts/migrate.sh` |

Scheduled work (in the `worker` container, `beat`):

| Task | Default schedule | What it does |
| --- | --- | --- |
| `verify_audit_chain` | every `AUDIT_CHAIN_CHECK_MINUTES` (15) | fails loudly if an audit row was altered or removed |
| `verify_ledger_integrity` | every `LEDGER_CHECK_EVERY_HOURS` (6) | debits = credits, per-entry balance, balance-cache drift |
| `sweep_expired_refresh_tokens` | hourly at `TOKEN_SWEEP_MINUTE_OF_HOUR` | revokes tokens past expiry that were never rotated |
| `prune_idempotency_keys` | daily at `IDEMPOTENCY_PRUNE_HOUR` | removes COMPLETED/FAILED idempotency records older than `IDEMPOTENCY_RETENTION_DAYS` |

A detected integrity break returns `ok: false` and emits an `integrity_violation` log event; it is **not** retried, because retrying cannot repair tampering. Treat it as an incident and follow `docs/security/SECURITY.md` §9.

## 6. TLS termination

In production nginx terminates TLS (`FORCE_HTTPS=true`, HSTS on). Add a TLS server block (for example `infrastructure/nginx/conf.d/nexus-tls.conf`, mounted read-only like the others) and mount the certificates:

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name api.nexus.example;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    ssl_session_cache   shared:SSL:10m;
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

    location /api/ {
        limit_req zone=api_general burst=60 nodelay;
        proxy_pass http://nexus_api;
        include /etc/nginx/conf.d/proxy_headers.inc;
    }
}
```

Set `TRUSTED_HOSTS` to the real hostname(s) — never `*` — and `CORS_ORIGINS` to the real client origins. TLS certificates and keys are mounted from the host (or a secret manager) and are never copied into an image.

## 7. Production hardening checklist

- [ ] `APP_ENV=production`, `FORCE_HTTPS=true` (startup fails otherwise), `SECURE_HEADERS_ENABLED=true`.
- [ ] `TRUSTED_HOSTS` lists the API hostname(s); `CORS_ORIGINS` lists explicit client origins (wildcards are refused in production).
- [ ] `DEV_ADMIN_PASSWORD` is empty (startup fails otherwise). The first administrator is created through the documented bootstrap procedure, not by a seed.
- [ ] `BACKUP_ENABLED=true` with `BACKUP_ENCRYPTION_RECIPIENT` set (startup fails otherwise) — see `docs/BACKUP.md` (Phase 12).
- [ ] Secrets delivered by Docker secrets / a vault, not by a `.env` file on disk, where the host supports it; `.env` is mode 600 and git-ignored.
- [ ] Certificates mounted read-only; TLS 1.2+ only; HSTS enabled.
- [ ] Redis and PostgreSQL published on loopback only (already the default in `docker-compose.yml`).
- [ ] Resource limits: add `mem_limit`/`cpus` under a `docker-compose.prod.yml` overlay for the target host.
- [ ] `docker compose logs` shipped to the monitoring stack; alerts on `integrity_violation`, HTTP 5xx and readiness failures.
- [ ] Migration procedure: verified backup → `scripts/migrate.sh` → readiness check → ledger/audit integrity check.

## 8. Verification commands (what "verified" means here)

```bash
# Static infrastructure checks (compose, nginx, redis, Dockerfile, scripts):
cd apps/api && PYTHONPATH=. python -m pytest tests/unit/test_compose_stack.py -q

# Compose interpolation and required secrets:
docker compose config -q && docker compose config --services

# Migration on a clean database + both schema gates:
docker compose exec -T api alembic upgrade head
PYTHONPATH=. python -m scripts.schema_gate orm-db --dsn "$DATABASE_MIGRATION_URL_PSYCOPG"
PYTHONPATH=. python -m scripts.schema_gate db-db --left <reference-dsn> --right <migrated-dsn>

# The Phase 0 invariant suite, on a freshly migrated database:
psql -v ON_ERROR_STOP=1 -d nexus_phase0 -f tests/invariants/phase0_schema_invariants.sql

# Seed idempotency:
docker compose exec -T api python -m seeds --check   # exit 0 when nothing would change

# Celery transport proof: publishes every task to the real broker and waits for
# the worker, then checks the maintenance tasks cleaned up the probe rows:
docker compose exec -T api python scripts/verify_worker_live.py
```

## 9. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Compose refuses to start: `variable X is not set` | A required secret is missing from `.env` | Run `scripts/gen_env.sh` (or fill the value) |
| API restarts repeatedly | Settings validation failed on boot | `docker compose logs api` — the message names the field to fix |
| `/api/v1/health` is 200 but `/health/ready` is 503 | PostgreSQL or Redis unreachable, or no migration applied | `docker compose ps`, then `scripts/migrate.sh` |
| nginx answers 404 for every path | Request path is not under `/api/` (the edge publishes nothing else) | Use `/api/v1/...`; interactive docs are intentionally not exposed |
| `permission denied for table ...` as `nexus_api` | A code path needs a grant the runtime role must not have (or a missing grant for a documented operation) | Check `docs/database/SCHEMA.md` §8; fix the role/grant deliberately, never by running as the owner |
| Seed exits 1 with `changes are required` | `--check` found drift between reference data and the database | Run `scripts/seed.sh` (never delete rows to silence it) |
| `alembic_version` empty after `alembic upgrade head` | The migration aborted on the checksum gate | Verify `sha256sum -c alembic/sql/CHECKSUMS.txt`; restore the file from Git |
| The compose subnet `172.28.0.0/24` collides with an existing route or VPN | The fixed subnet is what makes `FORWARDED_ALLOW_IPS` exact rather than a wildcard | Change `networks.nexus_backend.ipam.config.subnet` **and** the `FORWARDED_ALLOW_IPS` value in `docker-compose.yml` together, then `docker compose up -d --force-recreate` |
| Worker tasks never run | Broker unreachable or beat not running | `docker compose exec worker celery -A app.worker.celery_app:celery_app inspect registered` |
