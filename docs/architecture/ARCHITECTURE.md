# NEXUS EXCHANGE ERP — System Architecture

| Field | Value |
| --- | --- |
| Document ID | `ARCH-SYS-001` |
| Version | 1.0 (Phase 0) |
| Status | **Proposed — pending Phase 0 approval** |
| Owner | Architecture |
| Last updated | 2026-09-11 |

> **خلاصه فارسی** — معماری سامانه: کلاینت‌های Flutter (موبایل/دسکتاپ) از طریق Nginx با REST/JSON به FastAPI متصل می‌شوند؛ PostgreSQL منبع حقیقت است، Redis فقط کش/صف/محدودسازی نرخ. منطق کسب‌وکار فقط در لایه `services` است، مسیرهای API نازک‌اند، و هر عملیات مالی در یک تراکنش دیتابیس شامل سند، دفتر، صندوق و ممیزی ثبت می‌شود. تصمیم‌های کلیدی معماری (ADR) با دلیل و هزینه‌شان فهرست شده‌اند.

---

## 1. Scope

NEXUS EXCHANGE ERP is an offline-first, multi-currency, multi-branch back-office and point-of-sale system for **licensed** currency-exchange and money-service businesses. This document describes the runtime architecture, the layering rules that keep financial logic correct, and the quality attributes that drive the design.

**Explicit non-goals for the MVP** (no stubs are created for them): FX revaluation, hard accounting-period locks, KYC/AML workflow automation, card/PSP acquiring, payroll, tax filing, and any capability listed in PART 66 (money laundering, concealment, record forgery or deletion, KYC/AML circumvention, unlicensed activity). Regulatory features are added only against a specific jurisdiction's official rules.

## 2. Quality attributes (from the non-negotiables)

| Principle | Concrete architectural consequence |
| --- | --- |
| Financial Accuracy > Convenience | Only `AccountingService` writes the ledger; double-entry is enforced at COMMIT by PostgreSQL (`ct_journal_lines_balanced_*`); money is `NUMERIC(30,10)` end to end; no derived balance is ever treated as a source of truth |
| Data Integrity > Speed | Constraints, deferred triggers and state machines live in the database, so integrity holds even for a script or a buggy service; the balance cache is rebuildable and verified nightly |
| Security > Appearance | Argon2id, rotating device-bound refresh tokens, RBAC with explicit denies, device revocation checked on every sync, hash-chained audit log, least-privilege DB roles, secrets never in Git |
| Auditability > Deletion | Ledger, cash movements and audit rows are append-only; corrections are reversals; `nexus_app` holds no `DELETE` on those tables; the audit chain is verifiable (`verify_audit_chain()`) |
| Offline Reliability > Internet Dependency | The device can operate inside a server-granted envelope (allocation, number block, rate snapshot) and syncs idempotently; the server remains authoritative for all balances (`SYNC_DESIGN.md`) |

## 3. System context

```text
        Cashier (Android/Windows)        Manager / Accountant (Windows)      External auditor
                 │                                   │                              │
                 ▼                                   ▼                              ▼
        ┌───────────────────────────────────────────────────────┐          ┌──────────────────┐
        │            NEXUS EXCHANGE ERP clients                 │          │ Read-only export │
        │  Flutter (Riverpod, Drift + SQLCipher, Dio)           │          │ (PDF/XLSX, later)│
        └──────────────────────────┬────────────────────────────┘          └────────┬─────────┘
                                   │ HTTPS/JSON                                    │ SQL read-only
                                   ▼                                               ▼
                     ┌───────────────────────────┐                        ┌──────────────────┐
                     │     Nginx (TLS, proxy)    │                        │  nexus_reader    │
                     └──────────────┬────────────┘                        └──────────────────┘
                                    │
                     ┌──────────────▼────────────┐        ┌────────────────────────────┐
                     │  FastAPI (api service)    │◀──────▶│ Redis (cache, queue,       │
                     │  /api/v1, OpenAPI         │        │ rate limits, revocation)   │
                     └──────────────┬────────────┘        └─────────────┬──────────────┘
                                    │                                   │ broker
                     ┌──────────────▼────────────┐        ┌─────────────▼──────────────┐
                     │ PostgreSQL 16 (SOLE       │◀──────▶│ Celery worker (backups,    │
                     │ source of truth for money)│        │ reports, reconciliation)   │
                     └───────────────────────────┘        └────────────────────────────┘
```

## 4. Container/view of deployment

| Container | Image / technology | Responsibility | Stateless? |
| --- | --- | --- | --- |
| `nginx` | `nginx:1.27-alpine` | TLS termination, HTTP/2, security headers, request size limits, edge rate limiting, static OpenAPI docs | yes |
| `api` | Python 3.12 + FastAPI (uvicorn/gunicorn) | REST API, auth, domain services, transactional writes | yes |
| `worker` | Same image, Celery | Backups, report exports, nightly reconciliation, change-log pruning, notifications | yes |
| `postgres` | PostgreSQL 16 | Source of truth: ledger, documents, audit, sync state | no (stateful, volume) |
| `redis` | Redis 7 | Cache, Celery broker/result backend, rate-limit counters, token revocation set, allocation hot reads | no (rebuildable by design) |

Development runs the same five services via `docker compose up -d`. Production adds resource limits, no source mounts, secret injection and backups (`docker-compose.prod.yml`).

## 5. Logical layering (enforced in code review and by tests)

```text
HTTP          api/v1/*.py            routing, authn/z, DTO mapping, status codes — no business rules
              deps.py                current_user, permission dependency, pagination, idempotency, session
Application   services/*.py          transaction boundary, validation, orchestration, audit emission
Domain        services + core/money  invariants and arithmetic (Decimal-only), numbering, permissions
Data access   repositories/*.py      SQLAlchemy queries; never commits, never contains rules
Persistence   models/*.py            ORM mapping that matches docs/database/schema.sql
```

Rules with teeth:

1. A route handler may not import `models` directly, may not open a transaction and may not contain an `if` that decides money. Target ≤ 40 lines per handler.
2. Only services call `session.commit()`. One API request = one transaction (or one explicitly nested `SAVEPOINT` for batch semantics such as sync push).
3. Only `AccountingService` writes `journal_entries`/`journal_lines`.
4. Repositories take a session and return domain objects/rows; they never call other repositories to make decisions.
5. `core/money.py` is the only place that rounds; `float(` is forbidden in money paths and covered by a unit test that scans the codebase.

## 6. Request lifecycle for a financial operation

```text
POST /api/v1/exchange  (Idempotency-Key: …)
  1. nginx: TLS, size limit, per-IP rate limit
  2. FastAPI: JWT validation (signature, exp, jti revocation) → CurrentUser
  3. deps: permission check (exchange.create) → 403 with error envelope if absent
  4. deps: idempotency lookup → replay stored response if the key was used with the same body hash
  5. schema: Pydantic v2 validation, Decimal parsing from strings → 422 on failure
  6. ExchangeService.execute():
        a. resolve rate (resolve_exchange_rate) + tolerance check
        b. branch/device/permission/customer checks
        c. compute gross, net, commission, carrying rate — Decimal only
        d. INSERT exchange_transactions (client_event_id for offline origin)
        e. AccountingService.post_exchange(): journal entry + lines
        f. CashService.record_movements(): cash_movements rows
        g. AuditService.record(): audit row (hash chained)
        h. single COMMIT  ← deferred triggers verify balance, cash non-negativity, reversal binding
  7. response: transaction number, amounts, status 201
  8. receipt payload rendered (PDF/thermal) from stored data — never recomputed from inputs
```

Any failure inside 6 rolls back everything: no document without its ledger, no ledger without its audit trail (PART 20).

## 7. Cross-cutting concerns

| Concern | Mechanism | Notes |
| --- | --- | --- |
| Configuration | Pydantic Settings from environment; **no defaults for secrets**; startup fails fast when a required secret is missing | `.env.example` documents every key |
| Logging | `structlog` JSON, correlation/request id propagated from nginx, PII redaction (phone/name masked, never passwords or tokens) | Log to stdout; collected by the platform |
| Errors | Domain exceptions → `{error:{code,message,details}}` with the HTTP status mapping in `API_CONTRACT.md` §4; unhandled exceptions return `500` with a correlation id and no internals | SQLSTATE → domain error mapping table in `SCHEMA.md` §4.1 |
| Idempotency | `idempotency_keys` row (`user_id`, `endpoint`, key, `request_hash`, stored response) inside the same transaction as the effect | Same key + different body → `409 IDEMPOTENCY_KEY_REUSED` |
| Money | `NUMERIC(30,10)` in PostgreSQL, `Decimal` in Python, decimal **strings** in JSON, `Money` value type in Dart | Arithmetic and rounding policy in `ACCOUNTING_MODEL.md` §9 |
| RBAC | Permission registry (`core/permissions.py`) mirroring the `permissions` table; role defaults seeded; explicit per-user deny wins | 403, never 401, for an authenticated user lacking a permission |
| Device trust | Every request carries the device id; revoked/unknown devices are rejected; refresh tokens are device-bound | Applies to API and sync |
| Rate limiting | Redis token bucket per (user, IP, device) + stricter buckets for auth endpoints; `429` with `Retry-After` | Degrades to in-process counters if Redis is unavailable |
| Caching | Cache-aside with explicit namespaces; only rates, permissions and master data are cached; **never** balances in a way that can be stale for a decision | TTLs are short (≤ 60 s) and invalidated on write |
| Background jobs | Celery on Redis; idempotent tasks; `acks_late` with a visibility timeout; schedule in the worker container | Backups, exports, reconciliation, pruning |
| Audit | Every mutating service method emits exactly one audit event with actor, device, ip, before/after JSON | Hash chain + append-only triggers |

## 8. Data flows

| Flow | Path |
| --- | --- |
| Exchange (online) | Device → API → ExchangeService → ledger + cash + audit → receipt payload |
| Exchange (offline) | Device local TX (business row + local journal + local audit + outbox) → sync push → server validation → server ledger + audit → per-event result (`SYNC_DESIGN.md` §5) |
| Daily close | Cashier counts cash → `POST /cash/close` → `cash_session_lines` snapshot → ADJUSTMENT + journal for any variance → audit |
| Reporting | Ledger/movement tables → views (`v_trial_balance`, `v_cash_position`, `v_exchange_daily`) → `ReportService` → API/CSV/PDF |
| Backup | Celery → `pg_dump`/`pg_basebackup` → encrypted artifact to `STORAGE_PATH`; verification with `verify_audit_chain()` after any restore |

## 9. Failure modes and degradation policy

| Failure | Behaviour | Why acceptable |
| --- | --- | --- |
| PostgreSQL unavailable | API returns `503` with `Retry-After`; devices switch to governed offline mode | Money is never written anywhere except the source of truth |
| Redis unavailable | Rate limiting falls back to per-process counters; cache reads miss to the database; Celery jobs queue delayed | Redis holds no money state; allocation reads fall back to the database (the authoritative table) |
| nginx unavailable | Clients cannot reach the API → offline mode (devices keep operating within their allowance) | Offline-first design |
| Worker down | Backups/exports/reconciliation delayed → alert; API unaffected | Financial integrity is enforced at write time, not by jobs |
| Device clock drift | Events accepted and flagged; server timestamps authoritative | Reporting correctness is preserved |
| Duplicate delivery | `event_id`/`client_event_id`/`Idempotency-Key` make replays no-ops | PART 34/40 |
| Partial batch failure | Per-event results; successful events stay committed, failures are isolated | One bad event must not discard good sales |
| Disk full on the server | Writes fail loudly (`500`), reads continue; monitoring alerts on volume thresholds | Predictable behaviour, no silent corruption |

## 10. Performance and SLO targets (MVP)

| Operation | Target (p95) | Basis |
| --- | --- | --- |
| `POST /exchange` (online, including ledger writes) | ≤ 300 ms | Counter needs instant confirmation |
| `POST /auth/login` (Argon2id verify) | ≤ 400 ms | Argon2id parameters tuned to ~150 ms + overhead |
| Exchange rate resolution | ≤ 20 ms | `ix_exchange_rates_pair_effective` |
| Cashier screen data load (rates + customer lookup) | ≤ 200 ms | Cached master data, trigram/btree search |
| Daily/branch report (30 days) | ≤ 1.5 s | Ledger aggregation with branch/date indexes |
| Trial balance (90 days, single branch) | ≤ 2.5 s | `ix_journal_entries_branch_date` |
| Sync push (50 events) | ≤ 2 s | Batched, single transaction per event |
| Availability (business hours) | 99.5 % | Devices degrade to offline rather than fail |

Capacity baseline: 20 branches × 300 transactions/day ≈ 6,000 transactions/day, ≈ 25,000 ledger lines/day — three orders of magnitude below where the chosen indexes need partitioning.

## 11. Architecture decision records

| ADR | Decision | Rationale | Trade-off accepted |
| --- | --- | --- | --- |
| ADR-001 | FastAPI + async SQLAlchemy 2.x + Pydantic v2, Python 3.12 | Typed async stack with automatic OpenAPI, matching the mandated toolchain | Async complexity; mitigated by keeping transactions in services |
| ADR-002 | PostgreSQL is the only source of truth for money; Redis is never authoritative | Balances and ledgers must survive any cache loss | Slightly higher read latency; mitigated by a rebuildable balance cache |
| ADR-003 | Financial invariants enforced *in the database* (constraints, deferred triggers, state machines), not only in Python | A bug, a script or an operator with SQL access must not be able to corrupt the books | Some errors surface as SQLSTATEs; mapping table keeps them user-friendly |
| ADR-004 | Money as `NUMERIC(30,10)` + `Decimal`, decimal strings on the wire | Exact arithmetic across currencies and rates; PART 62 | Slightly more verbose frontend code (a `Money` type is provided) |
| ADR-005 | Append-only ledger, reversals instead of edits/deletes, DB-level `DELETE` revocation for the app role | Auditability (PART 18/22/49) | Corrections require two documents; acceptable and legally preferable |
| ADR-006 | Offline operation bounded by server-granted allocation + number block + rate snapshot | PART 37: two devices must not independently inflate a shared balance | Unused allowance is wasted (released nightly); device UX must show remaining allowance |
| ADR-007 | Idempotency for every financial endpoint, persisted with the response | Retries must never double-post; mobile networks drop responses | Table growth (pruned after 30 days) |
| ADR-008 | Refresh-token rotation with family reuse detection, device-bound | Stolen refresh tokens become detectable and revocable | A detected reuse logs the device out — intended behaviour |
| ADR-009 | Argon2id password hashing (memory-hard) | PART 42 | CPU/memory cost per login; tuned and benchmarked |
| ADR-010 | Audit hash chain with a transaction-scoped advisory lock | Tamper *evidence*, not just tamper prevention | Serialises audit inserts; acceptable at this write volume |
| ADR-011 | Zero-extension portable schema (no `pgcrypto`/`btree_gist`/`pg_trgm` in the baseline) | Managed PostgreSQL parity; verified to apply on a minimal build | Optional trigram search needs a separate migration |
| ADR-012 | Business-date document numbering via a `sequences` table + `next_document_number()` | Per-day, per-family, auditable numbering without native sequence gaps semantics | One contended row per (family, day); acceptable |
| ADR-013 | Reports read the ledger, never the cache | Legal/managerial numbers must be reproducible from immutable rows | Slower reports; mitigated by indexes and materialised exports |
| ADR-014 | Flutter feature-first architecture shared between mobile and desktop with repository abstraction | One team, two platforms, offline identical | Desktop-specific seams are explicit and documented |
| ADR-015 | Localisation: Dari/Pashto (RTL) and English (LTR) first-class, in the design system | PART 52 requires RTL correctness, not an afterthought | Extra work in every widget and formatter |
| ADR-016 | No capability that conceals, deletes or fabricates financial history | PART 66 | Some "convenience" features (silent edits, hard deletes) are refused by design |

## 12. Scaling path

| Stage | Trigger | Action |
| --- | --- | --- |
| Single server (MVP) | — | The compose stack above |
| Vertical + read replica | Reports slow down | Add a streaming replica for `nexus_reader`; point reports at it |
| Partitioning | `audit_logs`/`change_log` > 50 M rows | Monthly partitions + retention jobs |
| Horizontal API | > 500 req/s or HA requirement | Multiple `api` replicas behind nginx; move Celery workers out |
| Managed/HA PostgreSQL | Multi-branch growth | Streaming replication, automated backups, PITR, connection pooling (PgBouncer) |
| Kubernetes | Multi-region or per-tenant isolation | The compose topology maps 1:1 onto deployments/services/statefulsets |

## 13. Traceability

| Master prompt | Section |
| --- | --- |
| PART 1, PART 43, PART 44 | §4 |
| PART 20–22 | §6, §9 |
| PART 46, PART 47 | §5 |
| PART 50 (Phase 0 output #1) | this document |
| PART 62, PART 63, PART 64 | §5, §6, §7 |
| PART 66 | §1 |
