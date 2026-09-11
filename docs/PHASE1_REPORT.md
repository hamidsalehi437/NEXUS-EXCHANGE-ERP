# NEXUS EXCHANGE ERP — PHASE 1 VERIFICATION REPORT

**Phase:** 1 — Project Foundation (FastAPI + Pydantic v2 + SQLAlchemy 2.x / Alembic /
five-service Compose stack / seeds / CI / schema gates)
**Status:** Phase 1 code complete and verified as far as this sandbox allows; **Phase 2 not started**.
**Verified state:** `e3beb2f693180df3903d2cbf753e56e0d4519a40` on `arena/01a090c5-nexus-exchange-erp`
(this report file is committed after that revision, so the commit that contains it is one later).
**Verification environment:** Debian bookworm sandbox, Python 3.11.2, PostgreSQL 16.2 (local
installation, TCP 127.0.0.1:5432), Redis 6.2.14 (local, 127.0.0.1:6379), no Docker CLI/daemon,
no GitHub Actions runner.

---

## 1. Phase 1 acceptance criteria — exact verdicts

| # | Acceptance criterion (PART 44 / Phase 1 kickoff) | Verdict | Evidence |
|---|--------------------------------------------------|---------|----------|
| 1 | `docker compose up -d` succeeds | **NOT VERIFIED** | No Docker CLI or daemon in the sandbox (binary download from GitHub releases blocked). Static equivalents only: `tests/unit/test_compose_stack.py` 55 passed; YAML structural script (5 services, 55 interpolated vars documented, only nginx published, healthchecks on all five). |
| 2 | API health check succeeds | **PASS** | Live uvicorn on `0.0.0.0:8010`: `GET /api/v1/health` 200, `GET /api/v1/health/ready` 200 (`postgres` ok, revision `0001_initial_schema`, 1.66 ms; `redis` ok, 0.3 ms), `/api/v1/version` 200, `/api/v1/health/runtime` 200 (Python 3.11.2, PostgreSQL 16.2). |
| 3 | PostgreSQL starts | **PASS (native)** / **NOT VERIFIED (container)** | PostgreSQL 16.2 served every migration, seed, invariant and API test in this report. The `postgres:16-alpine` container from `docker-compose.yml` was never started. |
| 4 | Redis starts where specified | **PASS (native)** / **NOT VERIFIED (container)** | Redis 6.2.14 accepted every connection (API readiness, Celery broker/result backend). The `redis:7.4-alpine` container and its version-pinned configuration were never started. |
| 5 | Alembic migration succeeds on a clean database | **PASS** | `nexus_exchange` dropped and recreated; `sha256sum -c alembic/sql/CHECKSUMS.txt` OK for both artefacts; `alembic upgrade head` → `0001_initial_schema (head)`; result 32 tables / 5 views / 23 routines. |
| 6 | Seeds are idempotent | **PASS** | First run inserts the full reference set (currencies 7, roles + permissions 37, chart of accounts 42, dev admin 1). Every later run is a no-op: `TOTAL: inserted=0 updated=0 unchanged=87 removed=0`, and `python -m seeds --check` (dry run) exits 0. |
| 7 | CI passes | **NOT VERIFIED (GitHub)** / **PASS (local equivalents)** | `.github/workflows/ci.yml` parses (6 jobs, 3 triggers, no `secrets.*`, no floating action tags). Every CI step was reproduced locally with the same commands: lint, format check, mypy, unit, integration, compose-stack, OpenAPI generation. No `push`-triggered run has ever executed on GitHub. |
| 8 | Phase 0 invariant suite stays green after migration | **PASS** | On a freshly migrated database: psql exit 0 and `PHASE 0 SCHEMA INVARIANT SUITE: ALL ASSERTIONS PASSED`, `ledger totals: debit = credit = 1770000.0000000000`, `audit chain: valid`, `self-check passed (30 NUMERIC(30,10), 0 float)`. |
| 9 | Schema / migration gate clean | **PASS** | `scripts/schema_gate.py orm-db --dsn …` → exit 0, 31 tables / 341 columns MATCH. `scripts/schema_gate.py db-db --left … --right …` → exit 0, MATCH. |
| 10 | No secrets committed | **PASS** | `.env` is untracked and ignored (`.gitignore:7`), zero history entries, and no value of `JWT_SECRET`, `JWT_REFRESH_SECRET`, `POSTGRES_PASSWORD`, `REDIS_REQUIRED_PASSWORD` occurs anywhere in the `HEAD` tree. The only env-shaped tracked file is `.env.example` with `change-me` placeholders. |
| 11 | No incorrect financial business logic | **PASS** | Phase 1 contains no money-movement code paths; money enters only as `app/core/money.py` (Decimal/`NUMERIC(30,10)` helpers, 42 unit tests incl. the `MAX_MONEY` bound) and the migrated Phase 0 constraints, which the invariant suite re-verifies.
| 12 | Documentation updated | **PASS** | `README.md`, `docs/README.md`, `docs/DEPLOYMENT.md` (`OPS-DEPLOY-001`), `docs/database/SCHEMA.md` §8 + deviation D-20 all updated for Phase 1; the seven PART 57 documents exist and the Phase 0 accounting/sync/security contracts are unchanged. |

**Summary: 9 PASS, 2 partially verified (Docker runtime and GitHub CI execution are NOT VERIFIED),
0 FAIL, 0 acceptance criteria waived.**

---

## 2. Verification chain (the 12 requested checks, in order)

| # | Check | Command | Result |
|---|-------|---------|--------|
| 1 | Full test suite | `PYTHONPATH=. python -m pytest tests -q` | **PASS** — `499 passed in 31.82s` (unit 402 in ~0.9 s, integration 97 in ~29 s) |
| 2 | Ruff | `python -m ruff check .` / `python -m ruff format --check .` | **PASS** — `All checks passed!` / `69 files already formatted` |
| 3 | MyPy | `python -m mypy app seeds scripts` | **PASS** — `Success: no issues found in 49 source files` |
| 4 | Clean-database migration | drop/recreate DB → checksum gate → `alembic upgrade head` | **PASS** — `0001_initial_schema (head)`, 32 tables / 5 views / 23 routines |
| 5 | Schema / migration gate | `sha256sum -c alembic/sql/CHECKSUMS.txt` + `scripts/schema_gate.py db-db` | **PASS** — both frozen artefacts unchanged, `db-db` MATCH, exit 0 |
| 6 | ORM ↔ schema parity | `scripts/schema_gate.py orm-db` | **PASS** — 31 tables / 341 columns MATCH, exit 0 |
| 7 | Phase 0 invariant suite | `psql -v ON_ERROR_STOP=1 -f tests/invariants/phase0_schema_invariants.sql` | **PASS** — ALL ASSERTIONS PASSED, debit = credit = 1770000.0000000000, audit chain valid |
| 8 | Seed idempotency | `python -m seeds` twice, then `--check` | **PASS** — second run `inserted=0 updated=0 unchanged=87 removed=0`, `--check` dry run identical and exit 0 |
| 9 | API health / readiness | live uvicorn `:8010` | **PASS** — health/ready/version/runtime all 200; readiness reports PostgreSQL and Redis |
| 10 | PostgreSQL + Redis live integration | live probes + Celery transport proof | **PASS** — PG 16.2 and Redis 6.2.14 both reachable; `python scripts/verify_worker_live.py` → `LIVE WORKER PROOF: ALL TASKS SUCCEEDED` (all four tasks SUCCESS through the real broker; maintenance observed to revoke the probe token and delete the probe idempotency row) |
| 11 | Compose static validation | `pytest tests/unit/test_compose_stack.py` + structural script | **PASS (static)** — 55 tests; 5 services; 55 interpolated variables all documented in `.env.example`; only nginx publishes a port; PostgreSQL/Redis bound to loopback; healthcheck on all five; `name: nexus-exchange`; no obsolete `version:` key. `docker compose config` → **NOT VERIFIED** (no Docker CLI) |
| 12 | CI / static infrastructure | PyYAML parse + job inspection | **PASS (static)** — workflow parses: name `CI`, triggers `push`/`pull_request`/`workflow_dispatch`, jobs `lint`, `typecheck`, `unit`, `integration`, `compose-stack`, `openapi`; no `secrets.*`; `permissions: contents: read`. Execution on GitHub → **NOT VERIFIED** |

Additional checks performed beyond the requested chain:

* **Least-privilege role split** (`nexus_role_split` database): migrations run as `nexus_migrator`;
  the API role can `SELECT currencies` and `UPDATE refresh_tokens`, and is **denied** `UPDATE audit_logs`
  and `DELETE` on `journal_lines`, `accounts`, `audit_logs`, `exchange_transactions` (trigger
  `NEXUS_APPEND_ONLY`), and denied `CREATE TABLE`.
* **Security headers / host validation** live: `X-Content-Type-Options`, `X-Frame-Options: DENY`,
  `Referrer-Policy`, `Permissions-Policy`, `Cache-Control: no-store`; `Host: evil.example` → 400;
  unknown path → 404 in the `{error:{code,message,details}}` envelope with a `request_id`.
* **`scripts/gen_env.sh` end-to-end probe** in a scratch directory: `.env` created with mode `600`,
  six distinct secrets, no placeholders left, 64-character JWT secrets; second run refuses to
  overwrite without `--force` (exit 0).

---

## 3. Test counts

`499 passed` total, zero failures/skips/xfails:

| Suite | File | Tests |
|-------|------|-------|
| unit | `tests/unit/test_permissions.py` | 83 |
| unit | `tests/unit/test_exceptions.py` | 71 |
| unit | `tests/unit/test_config_validation.py` | 60 |
| unit | `tests/unit/test_password_security.py` | 56 |
| unit | `tests/unit/test_compose_stack.py` | 55 |
| unit | `tests/unit/test_money.py` | 42 |
| unit | `tests/unit/test_worker_configuration.py` | 20 |
| unit | `tests/unit/test_logging.py` | 15 |
| **unit subtotal** | | **402** |
| integration | `tests/integration/test_schema_gate.py` | 19 |
| integration | `tests/integration/test_seeds.py` | 19 |
| integration | `tests/integration/test_health.py` | 18 |
| integration | `tests/integration/test_migration.py` | 15 |
| integration | `tests/integration/test_invariants.py` | 15 |
| integration | `tests/integration/test_worker_tasks.py` | 11 |
| **integration subtotal** | | **97** |

---

## 4. Files created and changed in Phase 1

100 files changed versus the approved Phase 0 revision `a1894d9`: **91 added, 6 modified,
3 renamed** (`+13406 / −17`). Grouped by area: `apps/api/app` 40 files, `apps/api/tests` 19,
`apps/api/seeds` 7, `infrastructure` 8, `scripts` 7, `apps/api/alembic` 5, `apps/api/scripts` 2,
`.github` 1, plus `docker-compose.yml`, `.env.example`, `.gitignore` and documentation.

**Added (91)**

- `.github/workflows/ci.yml`
- `apps/api/.dockerignore`
- `apps/api/Dockerfile`
- `apps/api/alembic.ini`
- `apps/api/alembic/env.py`
- `apps/api/alembic/script.py.mako`
- `apps/api/alembic/sql/0001_initial_schema.sql`
- `apps/api/alembic/sql/CHECKSUMS.txt`
- `apps/api/alembic/versions/20260911_1400_0001_initial_schema.py`
- `apps/api/app/__init__.py`
- `apps/api/app/api/deps.py`
- `apps/api/app/api/v1/health.py`
- `apps/api/app/api/v1/router.py`
- `apps/api/app/core/__init__.py`
- `apps/api/app/core/config.py`
- `apps/api/app/core/database.py`
- `apps/api/app/core/error_handlers.py`
- `apps/api/app/core/exceptions.py`
- `apps/api/app/core/logging.py`
- `apps/api/app/core/money.py`
- `apps/api/app/core/permissions.py`
- `apps/api/app/core/redis.py`
- `apps/api/app/core/security.py`
- `apps/api/app/main.py`
- `apps/api/app/models/__init__.py`
- `apps/api/app/models/account.py`
- `apps/api/app/models/audit.py`
- `apps/api/app/models/base.py`
- `apps/api/app/models/branch.py`
- `apps/api/app/models/cash.py`
- `apps/api/app/models/change_log.py`
- `apps/api/app/models/currency.py`
- `apps/api/app/models/customer.py`
- `apps/api/app/models/device.py`
- `apps/api/app/models/exchange_rate.py`
- `apps/api/app/models/exchange_transaction.py`
- `apps/api/app/models/expense.py`
- `apps/api/app/models/journal.py`
- `apps/api/app/models/role.py`
- `apps/api/app/models/security.py`
- `apps/api/app/models/sequence.py`
- `apps/api/app/models/sync.py`
- `apps/api/app/models/transfer.py`
- `apps/api/app/models/user.py`
- `apps/api/app/schemas/common.py`
- `apps/api/app/worker/__init__.py`
- `apps/api/app/worker/celery_app.py`
- `apps/api/app/worker/integrity.py`
- `apps/api/app/worker/maintenance.py`
- `apps/api/pyproject.toml`
- `apps/api/requirements-dev.txt`
- `apps/api/requirements.txt`
- `apps/api/scripts/schema_gate.py`
- `apps/api/scripts/verify_worker_live.py`
- `apps/api/seeds/001_currencies.py`
- `apps/api/seeds/002_roles_permissions.py`
- `apps/api/seeds/003_chart_of_accounts.py`
- `apps/api/seeds/004_dev_admin.py`
- `apps/api/seeds/__init__.py`
- `apps/api/seeds/__main__.py`
- `apps/api/seeds/base.py`
- `apps/api/tests/conftest.py`
- `apps/api/tests/helpers.py`
- `apps/api/tests/integration/test_health.py`
- `apps/api/tests/integration/test_invariants.py`
- `apps/api/tests/integration/test_migration.py`
- `apps/api/tests/integration/test_schema_gate.py`
- `apps/api/tests/integration/test_seeds.py`
- `apps/api/tests/integration/test_worker_tasks.py`
- `apps/api/tests/unit/test_compose_stack.py`
- `apps/api/tests/unit/test_config_validation.py`
- `apps/api/tests/unit/test_exceptions.py`
- `apps/api/tests/unit/test_logging.py`
- `apps/api/tests/unit/test_money.py`
- `apps/api/tests/unit/test_password_security.py`
- `apps/api/tests/unit/test_permissions.py`
- `apps/api/tests/unit/test_worker_configuration.py`
- `docker-compose.yml`
- `docs/DEPLOYMENT.md`
- `infrastructure/nginx/conf.d/nexus.conf`
- `infrastructure/nginx/conf.d/proxy_headers.inc`
- `infrastructure/nginx/nginx.conf`
- `infrastructure/postgres/init/01-roles.sh`
- `infrastructure/redis/nexus.conf`
- `scripts/dev_down.sh`
- `scripts/dev_up.sh`
- `scripts/gen_env.sh`
- `scripts/gen_openapi.sh`
- `scripts/migrate.sh`
- `scripts/seed.sh`
- `scripts/test_all.sh`

**Modified (6)**

- `.env.example`
- `.gitignore`
- `README.md`
- `docs/README.md`
- `docs/database/SCHEMA.md`
- `docs/database/schema.sql`

**Renamed (3)**

- `infrastructure/nginx/.gitkeep -> apps/api/tests/__init__.py`
- `infrastructure/postgres/.gitkeep -> apps/api/tests/integration/__init__.py`
- `infrastructure/redis/.gitkeep -> apps/api/tests/unit/__init__.py`

---

## 5. Security findings

1. **Fixed during Phase 1:** the proxy trust list defaulted to `"*"`. `Settings.forwarded_allow_ips`
   now defaults to `"127.0.0.1"`, `.env.example` documents it, and the compose file sets
   `FORWARDED_ALLOW_IPS: 172.28.0.0/24` (the exact compose subnet) with a unit test asserting the
   loopback default. Failure mode prevented: an attacker reaching the API directly could spoof
   `X-Forwarded-For` and defeat per-IP rate limiting.
2. **No secrets in the repository** — verified by value-scan of the `HEAD` tree (see criterion 10).
   `.env` is ignored, generated by `scripts/gen_env.sh` with mode `600` and 64-character secrets.
3. **Least privilege enforced in the database, not only in the application** — append-only triggers
   reject `DELETE`/`UPDATE` on financial and audit tables for the API role (probe run documented above).
4. **Compose hardening (static):** `read_only` root filesystems with `tmpfs` for the API and worker,
   `cap_drop: ALL`, non-root images/users, PostgreSQL and Redis bound to `127.0.0.1`, only nginx
   published, required (`:?`) secrets with no built-in defaults.
5. **No new exposure introduced by Phase 1:** the only published endpoint is nginx on
   `${NGINX_HTTP_PORT:-8080}`; the API port is not published.

---

## 6. Limitations and explicitly unverified items

1. **Docker runtime — NOT VERIFIED.** There is no Docker CLI or daemon in this sandbox and the
   compose binary could not be downloaded. Consequently `docker compose up -d`, `docker compose
   config`, image builds, container start-up order, container healthcheck transitions,
   the `postgres:16-alpine` / `redis:7.4-alpine` / `nginx:1.27-alpine` images, the nginx config as
   loaded by nginx, the Redis config as loaded by redis-server, and the PostgreSQL bootstrap script
   inside the container are **all unverified at runtime**. They are covered by static validation only
   (`tests/unit/test_compose_stack.py` and the structural script).
2. **GitHub CI — NOT VERIFIED.** The workflow has never executed on GitHub; only the same commands
   locally. No CI run URL exists for this phase.
3. **Python 3.12 execution — NOT VERIFIED.** The sandbox interpreter is Python 3.11.2; no 3.12
   interpreter is obtainable here (python.org and GitHub release assets are blocked). `pyproject.toml`
   declares `requires-python = ">=3.12"`, Ruff and MyPy target 3.12, and the Dockerfile pins
   `python:3.12-slim-bookworm`; the runtime test suite itself therefore ran on 3.11 and 3.12 runtime
   behaviour is asserted statically, not observed.
4. **Redis version — NOT VERIFIED.** Live Redis here is 6.2.14, while compose and CI pin
   `redis:7.4-alpine`; task/broker/result-backend behaviour was proven against 6.2.14.
5. **PostgreSQL 16 container — NOT VERIFIED**; the verified server is PostgreSQL 16.2 installed
   locally, which satisfies the PART 5 floor of 16+.
6. **Invoice/backup/restore/offline-sync paths are out of Phase 1 scope** and remain unimplemented
   (Phase 6+ per the roadmap); nothing in this report claims they work. The MVP acceptance list of
   PART 67 is therefore **not** met yet, by design.
7. The Phase 0 invariant suite is authoritative on a **freshly migrated** database; it is not a
   tamper-proof monitor of a live database (a tampered audit row makes check I-6 fail, as designed).

---

## 7. How to reproduce

```bash
# 1. Environment file (never committed)
./scripts/gen_env.sh                     # writes .env with mode 600

# 2. Full local verification chain
cd apps/api
PYTHONPATH=. python -m pytest tests -q                        # 499 passed
python -m ruff check . && python -m ruff format --check .     # clean
python -m mypy app seeds scripts                              # clean
cd ../..
docker compose config -q && docker compose up -d             # requires Docker (NOT verified here)
docker compose exec -T api alembic upgrade head
docker compose exec -T api python -m seeds --check
docker compose exec -T api python scripts/verify_worker_live.py
psql -v ON_ERROR_STOP=1 -d nexus_exchange -f tests/invariants/phase0_schema_invariants.sql
```

---

## 8. Git history for Phase 1

| Commit | Description |
|--------|-------------|
| `f5ac3b7` | Phase 1: API foundation, initial migration, five-service stack, seeds, CI (100 files, +13304/−17) |
| `93911b6` | Phase 1 verification: compose spec checks and settings-level environment validation |
| `9c92cc9` | chore: remove an internal test-authoring note from the repository |
| `e3beb2f` | test(worker): add `scripts/verify_worker_live.py` transport proof and document it |

All four are pushed to `origin/arena/01a090c5-nexus-exchange-erp`. The verified code state is
`e3beb2f693180df3903d2cbf753e56e0d4519a40`; the working tree is clean.

**Phase 2 has not been started.**
