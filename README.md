# NEXUS EXCHANGE ERP

An offline-first, multi-currency, multi-branch ERP and point-of-sale system for **licensed** currency-exchange and money-service businesses.

> **وضعیت فعلی — فاز ۰ تحویل شد و منتظر تأیید است.**
> در این فاز فقط مستندات معماری، ERD، اسکیمای اجرایی، قرارداد API، طراحی همگام‌سازی آفلاین، معماری امنیت، مدل حسابداری و نقشه راه تولید شد. هیچ کد اپلیکیشنی نوشته نشده است؛ اجرای فاز ۱ پس از تأیید آغاز می‌شود.
>
> **Current status — Phase 0 delivered, awaiting approval.** Phase 0 contains architecture and design artifacts only (plus an executable reference schema and its invariant test suite). Implementation starts with Phase 1 after approval.

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

Verification evidence and the test catalogue: [`docs/security/TEST_PLAN.md`](docs/security/TEST_PLAN.md).

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
