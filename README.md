# NEXUS EXCHANGE ERP

An offline-first, multi-currency, multi-branch ERP and point-of-sale system for **licensed** currency-exchange and money-service businesses.

> **وضعیت فعلی — فاز ۰ و فاز ۱ تحویل شد؛ منتظر تأیید فاز ۱.**
> فاز ۰: مستندات معماری، ERD، اسکیمای اجرایی، قرارداد API، طراحی همگام‌سازی، معماری امنیت، مدل حسابداری و نقشه راه.
> فاز ۱: پایهٔ واقعی `apps/api` (FastAPI + Pydantic v2 + SQLAlchemy 2.x)، مهاجرت Alembic از اسکیمای تأییدشده، پشتهٔ پنج‌سرویسی Docker Compose، بذرکاری idempotent، بررسی سلامت/آمادگی، worker، CI و مجموعهٔ تست. هیچ endpoint کسب‌وکاری هنوز پیاده‌سازی نشده است؛ آن کار فاز ۲ است.
>
> **Current status — Phase 0 and Phase 1 delivered; Phase 1 awaits approval.** Phase 1 contains the API foundation, the initial Alembic revision built from the approved schema, the five-service compose stack, seeds, health/readiness, the worker and CI. No business endpoint exists yet — that is Phase 2.

## The five non-negotiables

| Principle | How it shows up in the system |
| --- | --- |
| **Financial Accuracy > Convenience** | Double-entry enforced at COMMIT by PostgreSQL; only `AccountingService` writes the ledger; `NUMERIC(30,10)`/`Decimal` everywhere, never float |
| **Data Integrity > Speed** | Constraints, deferred triggers and state machines live in the database, so a bug or a script cannot corrupt the books |
| **Security > Appearance** | Argon2id, rotating device-bound refresh tokens, RBAC with explicit denies, device revocation, hash-chained audit log, least-privilege DB roles |
| **Auditability > Deletion** | Ledger, cash and audit rows are append-only; corrections are reversals; the application role has no `DELETE` on history |
| **Offline Reliability > Internet Dependency** | Devices trade inside a server-granted envelope (allowance, number block, rate snapshot) and sync exactly once — the server stays authoritative for balances |

## Repository layout

```text
apps/            api (FastAPI) · mobile · desktop (Flutter) · web-admin (later)
packages/        shared Dart packages (api-client, shared-types, design-system, localization)
infrastructure/  docker · nginx · postgres · redis
docs/            architecture · database · api · security · user-manual
scripts/         operator and developer automation
tests/           cross-cutting suites (invariants, e2e, load, fixtures)
```

Details and dependency rules: [`docs/architecture/FOLDER_STRUCTURE.md`](docs/architecture/FOLDER_STRUCTURE.md).

## Phase 0 deliverables

| # | Deliverable | Location |
| --- | --- | --- |
| 1 | Architecture (+ ADRs, SLOs, failure modes) | [`docs/architecture/ARCHITECTURE.md`](docs/architecture/ARCHITECTURE.md) |
| 2 | ERD and relationship/index catalogue | [`docs/database/ERD.md`](docs/database/ERD.md) |
| 3 | Database schema (executable, normative) | [`docs/database/schema.sql`](docs/database/schema.sql) + [`docs/database/SCHEMA.md`](docs/database/SCHEMA.md) |
| 4 | API contract (endpoints, errors, permissions) | [`docs/api/API_CONTRACT.md`](docs/api/API_CONTRACT.md) |
| 5 | Folder structure and layering rules | [`docs/architecture/FOLDER_STRUCTURE.md`](docs/architecture/FOLDER_STRUCTURE.md) |
| 6 | Offline / sync design | [`docs/architecture/SYNC_DESIGN.md`](docs/architecture/SYNC_DESIGN.md) |
| 7 | Security architecture | [`docs/security/SECURITY.md`](docs/security/SECURITY.md) |
| 8 | Accounting model | [`docs/architecture/ACCOUNTING_MODEL.md`](docs/architecture/ACCOUNTING_MODEL.md) |
| 9 | Development roadmap (Phases 1–13) | [`docs/architecture/ROADMAP.md`](docs/architecture/ROADMAP.md) |

## Phase 1 deliverables

| # | Deliverable | Location |
| --- | --- | --- |
| 1 | API foundation (FastAPI, Pydantic v2, SQLAlchemy 2.x, layered packages) | [`apps/api/app/`](apps/api/app) |
| 2 | ORM models for all 31 tables | [`apps/api/app/models/`](apps/api/app/models) |
| 3 | Initial Alembic revision (applies the approved DDL verbatim, checksum-verified) | [`apps/api/alembic/`](apps/api/alembic) |
| 4 | Seed runner (currencies, roles/permissions, chart of accounts; idempotent, `--check`) | [`apps/api/seeds/`](apps/api/seeds) |
| 5 | Health, readiness and version endpoints | [`apps/api/app/api/v1/health.py`](apps/api/app/api/v1/health.py) |
| 6 | Celery worker (audit-chain and ledger verification, token sweep, idempotency retention) | [`apps/api/app/worker/`](apps/api/app/worker) |
| 7 | Five-service Docker Compose stack + nginx edge | [`docker-compose.yml`](docker-compose.yml), [`infrastructure/`](infrastructure) |
| 8 | Schema gates (ORM↔live DB, reference file↔live DB) | [`apps/api/scripts/schema_gate.py`](apps/api/scripts/schema_gate.py) |
| 9 | Operator scripts (`dev_up`, `migrate`, `seed`, `test_all`, `gen_env`, `gen_openapi`) | [`scripts/`](scripts) |
| 10 | CI (lint, types, unit, integration + gates, compose stack, OpenAPI) | [`.github/workflows/ci.yml`](.github/workflows/ci.yml) |
| 11 | Deployment runbook | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |

Verification evidence and the test catalogue: [`docs/security/TEST_PLAN.md`](docs/security/TEST_PLAN.md).

## Run the stack (Phase 1)

```bash
scripts/gen_env.sh            # writes .env (mode 600) with fresh random secrets
scripts/dev_up.sh             # build → up --wait → alembic upgrade head → seeds → readiness
curl -fsS http://127.0.0.1:8080/api/v1/health/ready
```

Five services: `nginx` (the only public entry point), `api`, `worker`, `postgres` 16 and `redis` 7.
Full runbook, native (no-Docker) path, TLS, hardening checklist and troubleshooting:
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## Verify Phase 1 yourself

```bash
cd apps/api
PYTHONPATH=. python -m pytest tests/unit -q          # unit tests, no database needed
PYTHONPATH=. python -m pytest tests/integration -q   # real PostgreSQL: migration, schema gates, seeds, invariants, worker
python -m ruff check . && python -m mypy app seeds scripts

# The database half needs PostgreSQL 16 (see docs/DEPLOYMENT.md §4):
alembic upgrade head                                 # applies the approved schema, checksum-verified
python -m scripts.schema_gate orm-db                 # ORM metadata ↔ live schema
python -m scripts.schema_gate db-db --left <reference-dsn> --right <migrated-dsn>
python -m seeds && python -m seeds --check           # idempotent seed data
psql -d <fresh-db> -f ../tests/invariants/phase0_schema_invariants.sql
```

## Verify the Phase 0 schema yourself

The reference schema needs **no PostgreSQL extensions** and runs on any PostgreSQL 16 build:

```bash
createdb nexus_exchange
psql -v ON_ERROR_STOP=1 -d nexus_exchange -f docs/database/schema.sql
psql -v ON_ERROR_STOP=1 -d nexus_exchange -f tests/invariants/phase0_schema_invariants.sql
```

Expected: the schema self-check prints `NEXUS schema self-check passed (30 NUMERIC(30,10) columns, 0 float columns)`, and the invariant suite ends with

```text
PHASE 0 SCHEMA INVARIANT SUITE: ALL ASSERTIONS PASSED
  ledger totals  : debit = credit = 1770000.0000000000
  audit chain     : valid
```

That suite is recorded in Phase 0 as 27/27 assertions passing against PostgreSQL 16.2.

## Legal boundary

This system is built for the internal management of **licensed** businesses. It deliberately provides no capability for money laundering, concealment of transactions, document forgery, deletion of financial records, KYC/AML circumvention, or unlicensed financial activity. Regulatory features are implemented only from the official rules of the target jurisdiction. See [`docs/security/SECURITY.md`](docs/security/SECURITY.md) §10.

## License

See [`LICENSE`](LICENSE).
