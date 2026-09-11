# NEXUS EXCHANGE ERP — Monorepo Folder Structure

| Field | Value |
| --- | --- |
| Document ID | `ARCH-FS-001` |
| Version | 1.0 (Phase 0) |
| Status | **Proposed — pending Phase 0 approval** |
| Owner | Architecture |
| Last updated | 2026-09-11 |

> **خلاصه فارسی** — این سند ساختار پوشه‌ها و قواعد وابستگی مونوریپو را تعیین می‌کند: چهار اپلیکیشن (`api`, `mobile`, `desktop`, `web-admin`)، چهار پکیج مشترک (`api-client`, `shared-types`, `design-system`, `localization`)، زیرساخت (`docker`, `nginx`, `postgres`, `redis`)، مستندات، اسکریپت‌ها و تست‌ها. قواعد کلیدی: منطق کسب‌وکار فقط در `apps/api/app/services`، دسترسی مستقیم به دیتابیس از Flutter ممنوع، کد مشترک فقط از طریق `packages/`، هیچ secret یا فایل build در Git.

---

## 1. Principles

| # | Principle | Enforcement |
| --- | --- | --- |
| P1 | Business logic lives only in `apps/api/app/services`; API routes are thin adapters. | Code review + `ruff` import-boundary rule; integration tests call services directly. |
| P2 | SQL is owned by the API. Flutter apps never execute raw SQL against the server. | Dart side depends on `packages/api-client` only; no `http`/`postgres` imports outside that package (custom lint). |
| P3 | Cross-app code is shared only through `packages/`, never by copying files between apps. | Path-dependency discipline; duplicate-code check in CI (`.arb`/`lib` hash comparison). |
| P4 | Infrastructure is code: every runtime dependency is declared in `infrastructure/docker` + `docker-compose.yml`. | CI boots the stack with `docker compose up -d` and runs integration tests against it. |
| P5 | Nothing secret, generated, or machine-specific is committed. | `.gitignore` + `gitleaks` job in CI. |
| P6 | Documentation is a first-class deliverable, versioned with the code it describes. | `docs/**` updated in the same PR that changes behavior. |

## 2. Dependency direction

```text
apps/web-admin ─┐
apps/desktop  ──┼──► packages/api-client ──► packages/shared-types
apps/mobile   ──┘          │
                           ▼
                  packages/design-system
                  packages/localization

apps/* ──(HTTP/JSON, /api/v1)──► apps/api ──► PostgreSQL (source of truth)
                                    │
                                    ├──► Redis (cache, queue, rate limit, revocation)
                                    └──► worker (Celery: backups, exports, notifications)
```

Arrows are **compile-time or network dependencies**. Reverse imports are forbidden; `apps/api` never imports from `apps/*` clients, and no application imports from `tests/`.

## 3. Full tree

```text
nexus-exchange-erp/
│
├── apps/
│   ├── api/                       # FastAPI backend (see §4)
│   ├── desktop/                   # Flutter Windows desktop client (see §6)
│   ├── mobile/                    # Flutter Android client (see §5)
│   └── web-admin/                 # Next-phase admin web console (Phase 13+, placeholder structure only)
│
├── packages/
│   ├── api-client/                # Dart: typed HTTP client generated from OpenAPI + Dio interceptors
│   ├── shared-types/              # Dart: DTOs, enums, money type, sync envelope contracts
│   ├── design-system/             # Dart: Material 3 theme, tokens, shared widgets, RTL/LTR support
│   └── localization/              # Dart: .arb files (en, fa-AF = Dari, ps-AF = Pashto) + codegen config
│
├── infrastructure/
│   ├── docker/                    # Dockerfiles, compose overlays (dev/prod), entrypoints
│   ├── nginx/                     # TLS termination, reverse proxy, security headers, edge rate limits
│   ├── postgres/                  # init scripts, postgresql.conf tuning, WAL archiving config, roles
│   └── redis/                     # redis.conf (appendonly, maxmemory policy, ACLs)
│
├── docs/                          # see §7
├── scripts/                       # operator + developer automation (see §8)
├── tests/                         # cross-cutting suites that are not owned by one app (see §9)
│
├── .env.example
├── .gitignore
├── docker-compose.yml             # development stack (api, postgres, redis, nginx, worker)
├── docker-compose.prod.yml        # production overlay (resource limits, secrets, no source mounts)
├── README.md
└── LICENSE
```

## 4. Backend — `apps/api`

```text
apps/api/
├── app/
│   ├── main.py                    # App factory, lifespan (DB/Redis pools), router mount, exception handlers
│   │
│   ├── core/
│   │   ├── config.py              # Pydantic Settings (env), no defaults for secrets
│   │   ├── security.py            # Argon2id, JWT issue/verify, refresh rotation, permission checks
│   │   ├── database.py            # Async engine, session factory, transaction manager, unit of work
│   │   ├── logging.py             # structlog JSON logging, correlation ID, PII redaction
│   │   ├── exceptions.py          # Domain exceptions → error envelope (PART 38) mapping
│   │   ├── money.py               # Decimal helpers, quantize/rounding policy, no-float guard
│   │   ├── numbering.py           # Document numbering service (NX-YYYYMMDD-NNNNNN), sequences
│   │   ├── idempotency.py         # Idempotency-Key store/read (Redis + DB), replay semantics
│   │   ├── permissions.py         # Permission registry (PART 41) + role→permission defaults
│   │   ├── ratelimit.py           # Redis token bucket / sliding window dependency
│   │   ├── cache.py               # Cache-aside helpers with explicit key namespaces
│   │   └── redis.py               # Redis client factory, key builders, revocation set
│   │
│   ├── models/                    # SQLAlchemy 2.x ORM (1 file per aggregate)
│   │   ├── base.py                # DeclarativeBase, UUID PK mixin, timestamps, versioning mixin
│   │   ├── user.py  role.py  branch.py  device.py  customer.py  currency.py
│   │   ├── exchange_rate.py  exchange_transaction.py  transfer.py
│   │   ├── account.py  journal.py  cash.py  expense.py  audit.py  sync.py
│   │   ├── security.py            # refresh_tokens, device_sessions, idempotency_keys
│   │   └── change_log.py          # server-side change stream for sync pull
│   │
│   ├── schemas/                   # Pydantic v2 request/response models (1 module per resource)
│   │   ├── money.py               # DecimalMoney type: JSON string ⇄ Decimal(30,10)
│   │   ├── common.py              # Page, CursorPage, ErrorEnvelope, IdempotencyMeta
│   │   └── <resource>.py          # auth, user, role, branch, device, customer, currency, rate,
│   │                              # exchange, transfer, account, journal, cash, expense,
│   │                              # report, audit, sync, backup
│   │
│   ├── repositories/              # Data access only (no business rules, no commits)
│   │   ├── base.py                # Generic CRUD, cursor pagination, tenant/branch scoping helpers
│   │   └── <aggregate>_repository.py
│   │
│   ├── services/                  # ALL business rules + transaction boundaries
│   │   ├── exchange_service.py
│   │   ├── accounting_service.py
│   │   ├── cash_service.py
│   │   ├── transfer_service.py
│   │   ├── customer_service.py
│   │   ├── sync_service.py
│   │   ├── audit_service.py
│   │   ├── rate_service.py
│   │   ├── report_service.py
│   │   ├── auth_service.py
│   │   ├── user_service.py
│   │   ├── branch_service.py
│   │   ├── device_service.py
│   │   ├── expense_service.py
│   │   ├── backup_service.py
│   │   └── receipt_service.py     # Deterministic receipt payload (used by PDF/thermal printers)
│   │
│   ├── api/
│   │   ├── deps.py                # Shared FastAPI dependencies (current_user, db, permissions, pagination)
│   │   └── v1/
│   │       ├── router.py          # Aggregates all v1 routers under /api/v1
│   │       ├── auth.py  users.py  roles.py  branches.py  devices.py  customers.py
│   │       ├── currencies.py  rates.py  exchange.py  transfers.py  accounts.py
│   │       ├── journal.py  cash.py  expenses.py  reports.py  audit.py
│   │       ├── sync.py  backups.py  health.py
│   │
│   ├── workers/                   # Celery app + tasks (no business rules; they call services)
│   │   ├── celery_app.py
│   │   ├── tasks_backup.py  tasks_reports.py  tasks_maintenance.py
│   │
│   └── utils/                     # Pure helpers (dates, ids, pagination cursors, validators)
│
├── alembic/
│   ├── env.py
│   └── versions/
├── seeds/                         # Idempotent seed data (currencies, roles, permissions, chart of accounts)
│   ├── 001_currencies.py  002_roles_permissions.py  003_chart_of_accounts.py  004_dev_admin.py
├── tests/                         # see §9
├── pyproject.toml                 # ruff, mypy, pytest, coverage config
├── requirements.in / requirements.txt   # pip-compile locked runtime deps (PART 58 compatible)
├── requirements-dev.txt
└── Dockerfile
```

**Rules specific to the API**

1. `api/v1/*.py` contains routing, authentication/permission dependencies, request/response mapping and **nothing else** (PART 47). Target ≤ 40 lines of logic per handler.
2. Only `services/` may open/commit a transaction; `repositories/` receive a session and never commit.
3. Only `services/accounting_service.py` may write `journal_entries`/`journal_lines` (PART 46).
4. Every monetary value crossing the boundary is `Decimal`; `float` is banned by lint rule `TID251`/custom ruff check and by a unit test that scans for `float(` in money modules.
5. Every mutating service method writes exactly one audit event before returning (PART 20 step 5).

## 5. Flutter client — `apps/mobile`

Feature-first layout, **identical** in `apps/desktop` so screens and repositories are portable.

```text
apps/mobile/
├── lib/
│   ├── main.dart                  # Bootstrap: DI container, secure storage, DB open, router, localizations
│   │
│   ├── core/
│   │   ├── database/              # Drift schema, migrations, DAOs, SQLCipher key handling
│   │   │   ├── app_database.dart  # Tables: users_cache, customers, currencies, exchange_rates,
│   │   │   │                      # exchange_transactions, cash_movements, transfers, journal_cache,
│   │   │   │                      # sync_queue, sync_conflicts, settings, number_allocations
│   │   │   ├── daos/              # One DAO per local table
│   │   │   └── migrations/        # Versioned local schema steps (never destructive without backup)
│   │   ├── network/               # Dio factory, auth interceptor, retry/backoff, idempotency keys,
│   │   │                          # connectivity probe, offline detector
│   │   ├── auth/                  # Token storage (secure storage), session/device registration, lock screen
│   │   ├── sync/                  # Sync engine: outbox drain, pull cursor, conflict queue, scheduler
│   │   ├── printer/               # Printing service: thermal (ESC/POS over Bluetooth/USB), A4 PDF
│   │   ├── localization/          # Locale controller (en/fa-AF/ps-AF), RTL handling
│   │   └── security/              # SQLCipher key derivation, remote-wipe, screenshot policy, secrets
│   │
│   ├── features/
│   │   ├── auth/  dashboard/  exchange/  customers/  transfers/  cash/
│   │   ├── accounts/  reports/  settings/  sync/
│   │   └── <feature>/
│   │       ├── data/              # Repository impl (local Drift + remote api-client switch)
│   │       ├── domain/            # Entities + use cases (pure Dart, unit-testable)
│   │       └── presentation/      # Riverpod providers, screens, widgets
│   │
│   ├── shared/
│   │   ├── widgets/  dialogs/  tables/  formatters/   # formatters: money (Decimal), date, RTL numerals
│   │   └── models/                # UI-level models shared across features
│   │
│   └── router/                    # go_router config + guards (auth, permission, sync state)
│
├── test/
│   ├── unit/                      # Use cases, formatters, repository logic with in-memory Drift
│   ├── widget/                    # Screen tests incl. RTL snapshots
│   └── sync/                      # Offline→online simulation harness (fake API, conflict scenarios)
├── integration_test/
└── pubspec.yaml
```

`apps/desktop/lib` mirrors `apps/mobile/lib` exactly. The only permitted divergence:

| Area | mobile | desktop |
| --- | --- | --- |
| `core/printer` | ESC/POS over Bluetooth | ESC/POS over USB/serial + A4 PDF via system printer |
| `core/security` | Android Keystore + SQLCipher | Windows DPAPI/TPM + SQLCipher |
| `features/dashboard` | compact, touch-first | multi-column, keyboard shortcuts, calculator dock |
| `features/settings` | camera/scanner settings | cash-drawer kick, barcode gun, keyboard mapping |

Cross-app reuse: mobile/desktop depend on `packages/design-system`, `packages/localization`, `packages/api-client`, `packages/shared-types` via path dependencies. Screen code that must be shared lives in `packages/design-system/lib/screens/` only if it is fully stateless/generic; otherwise duplicate-by-design is forbidden and the shared seam is the `repo`+`provider` contract.

## 6. Shared packages

| Package | Language | Contains | Must not contain |
| --- | --- | --- | --- |
| `packages/shared-types` | Dart | DTOs (json_serializable), enums (`MovementType`, `SyncStatus`…), `Money` value type, sync envelope, error codes mirror | UI, Dio, DB |
| `packages/api-client` | Dart | OpenAPI-generated client + Dio interceptors (auth refresh, `Idempotency-Key`, retry, error mapping) | Business rules |
| `packages/design-system` | Dart | Material 3 theme, color/typography tokens, RTL-aware widgets, money/date tables, calculator widget | Data access |
| `packages/localization` | Dart | `en.arb`, `fa-AF.arb` (Dari), `ps-AF.arb` (Pashto), l10n codegen config; date/number patterns per locale | Business logic |

Generation policy: `packages/api-client` and `packages/shared-types` are **generated from `docs/api/openapi.json`** (produced by FastAPI in CI). Generated files carry a header and are excluded from manual edits; CI fails if regeneration produces a diff (contract drift gate).

## 7. Documentation — `docs/`

```text
docs/
├── README.md                      # Index + reading order + owner per document
├── architecture/
│   ├── ARCHITECTURE.md            # System architecture, ADRs, quality attributes
│   ├── FOLDER_STRUCTURE.md        # (this document)
│   ├── ACCOUNTING_MODEL.md        # Chart of accounts, posting rules, invariants
│   ├── SYNC_DESIGN.md             # Offline-first + allocation + conflict model
│   └── ROADMAP.md                 # Phase 1–13 plan, gates, risks
├── api/
│   ├── API_CONTRACT.md            # REST contract, error catalog, conventions
│   └── openapi.json               # Generated in Phase 1 by CI (not hand-written)
├── database/
│   ├── ERD.md                     # Entity relationship diagrams + relationship catalogue
│   ├── SCHEMA.md                  # Type/constraint/index policy, migration strategy
│   └── schema.sql                 # Normative reference DDL (source for Phase 1 Alembic revision)
├── security/
│   ├── SECURITY.md                # Threat model, authN/Z, RBAC matrix, hardening, compliance boundary
│   └── TEST_PLAN.md               # Security test matrix (executed in Phase 12)
└── user-manual/                   # Operational manuals (cashier/manager/accountant), authored in Phase 13
```

## 8. Scripts — `scripts/`

| Script | Purpose |
| --- | --- |
| `dev_up.sh` / `dev_down.sh` | `docker compose up -d` + wait-for-health, optional `alembic upgrade head` + `seeds` |
| `migrate.sh` | `alembic upgrade head` inside the api container with pre-flight backup check |
| `seed.sh` | Idempotent seed for currencies, roles/permissions, chart of accounts |
| `test_all.sh` | Lint + type-check + unit + integration for API, and analyze + test for Flutter |
| `gen_openapi.sh` | Export `docs/api/openapi.json` from the running app |
| `gen_api_client.sh` | Regenerate Dart client + shared types from OpenAPI |
| `backup_db.sh` / `restore_db.sh` | Encrypted `pg_basebackup`+WAL / verified restore (Phase 12 hardening) |
| `release_android.sh` / `release_windows.sh` | Signed AAB/APK / Windows installer build (Phase 13) |
| `smoke_prod.sh` | Post-deploy health + login + read-only exchange rate checks |

All scripts: `set -euo pipefail`, non-interactive, safe to re-run, no secrets on the command line.

## 9. Tests — `tests/`

`tests/` holds suites that span applications. Application-owned tests stay in `apps/<app>/tests` (API) and `apps/<app>/test` (Flutter).

```text
tests/
├── e2e/                           # Cross-service flows against the compose stack (login → exchange → report)
├── invariants/                    # PART 49 financial invariants as executable property tests (Hypothesis)
├── load/                          # k6/Locust scenarios + SLO budgets (Phase 12)
└── fixtures/                      # Shared seeds: currencies, accounts, customers, device bootstrap
```

## 10. Environment configuration

| File | Committed | Purpose |
| --- | --- | --- |
| `.env.example` | ✅ | Documented template (PART 43); no real values |
| `.env` | ❌ git-ignored | Developer/operator local values |
| `.env.test` | ✅ | Deterministic non-secret test values |
| Production secrets | ❌ | Docker secrets / K8s secrets / operator vault; **never** in the repo or images |

## 11. Naming conventions

| Artifact | Convention | Example |
| --- | --- | --- |
| Python module / file | `snake_case` | `exchange_service.py` |
| Python class | `PascalCase` | `ExchangeService` |
| DB table / column | `snake_case`, plural tables | `exchange_transactions.to_amount` |
| API path | plural `kebab-free` nouns, camel-free | `/api/v1/exchange-rates` |
| JSON field | `snake_case` | `from_currency_id` |
| Dart file | `snake_case.dart` | `exchange_repository.dart` |
| Dart class / provider | `PascalCase` / `camelCaseProvider` | `ExchangeRepository`, `exchangeRepositoryProvider` |
| Permission string | `resource.action` | `exchange.reverse` |
| Audit action | `RESOURCE_VERB` upper snake | `EXCHANGE_TRANSACTION_CREATED` |
| Migration revision | `<utc-ts>_<slug>` | `20260915_1200_create_core_tables` |
| Branch code | `^[A-Z0-9][A-Z0-9-]{1,19}$` | `B01`, `KBL-CENTER` |

## 12. Phase 0 scaffolding note

Phase 0 creates only the directory skeleton with `.gitkeep` markers and this documentation set. No source file, migration, schema object, or configuration in this phase — implementation starts in Phase 1 after approval.

## 13. Traceability

| Master prompt | Section here |
| --- | --- |
| PART 2 | §3, §4, §5, §6 |
| PART 3 | §4 |
| PART 4 | §5, §6 |
| PART 43 | §10 |
| PART 46, PART 47 | §4 (rules 1–5), §6 |
| PART 57 | §7 |
| PART 63 | §2 (dependency direction) |
