# NEXUS EXCHANGE ERP — Development Roadmap (Phases 1–13)

| Field | Value |
| --- | --- |
| Document ID | `ARCH-ROAD-001` |
| Version | 1.1 (Phase 2 status recorded) |
| Status | **Phase 0 approved; Phase 1 delivered; Phase 2 delivered — pending approval** |
| Rule | A phase starts only when the previous phase's exit criteria are green and its report is delivered (PART 59, PART 61) |

> **خلاصه فارسی** — نقشه راه ۱۳ فاز: هر فاز هدف، خروجی‌های واقعی، تست‌های اجباری و شرط پذیرش دارد. هیچ فازی بدون تست سبز بسته نمی‌شود و کد بدون تست «تمام‌شده» محسوب نمی‌شود. بخش ۴ معیارهای MVP (PART 67) را به فازها نگاشت می‌کند و بخش ۳ ریسک‌های هر فاز با راه‌کار آمده است.

---

## 1. How a phase is executed and reported

```text
PLAN → IMPLEMENT → TEST → FIX → VERIFY → DOCUMENT        (PART 59)

Report format (mandatory, after every phase):
  • Files created        (real paths)
  • Tests created        (names + what they prove)
  • Tests passed         (command, exit code, counts, coverage)
  • Known limitations    (explicit, with the phase that resolves them)
  • Next phase           (and what is required to start it)
```

No phase may report "done" on the strength of code that was not executed. A phase that cannot be verified in this environment is reported as **implemented but unverified** with the exact command an operator must run (this applies to Docker/Flutter work in a sandbox without those toolchains).

## 2. Phase overview

| Phase | Name | Primary deliverable | Verified by |
| --- | --- | --- | --- |
| 0 | Architecture | This documentation set + executable schema + invariant suite | ✅ executed (27/27 assertions, PostgreSQL 16.2) |
| 1 | Project foundation | Monorepo skeleton, Docker compose (api/postgres/redis/nginx/worker), Alembic initial revision, seeds, CI, tooling config | `docker compose up -d` healthy; schema-gate + migrate-gate green |
| 2 | Authentication & RBAC | Login/refresh/logout, Argon2id, devices, roles/permissions, rate limits, lockout | §4.1 test catalogue |
| 3 | Core master data | Currencies, branches, customers, accounts, rates (+ audit on rate changes) | Master-data integration tests |
| 4 | Accounting engine | `AccountingService` with the postings of `ACCOUNTING_MODEL.md` | Accounting test catalogue + invariant suite re-run through the service layer |
| 5 | Exchange | Buy/Sell, commission, receipts, cancel, reverse, offline origin hooks | Exchange test catalogue |
| 6 | Cash | Open, in, out, adjustment, close, difference | Cash test catalogue |
| 7 | Reports | Daily, P/L, cash, exchange, customers, transfers, trial balance, general ledger | Report reconciliation tests |
| 8 | Offline | Drift schema, repositories, outbox, sync engine, conflict handling | Sync catalogue (10 scenarios) |
| 9 | Multi-branch | Branch scoping, device policies, branch balances, inter-branch transfers | Scope/permission and branch-report tests |
| 10 | Transfers | Create, approve, pay, cancel, commission, numbering, audit | Transfer lifecycle tests |
| 11 | Printing | A4 PDF, thermal ESC/POS, Bluetooth, receipt templates, calculator shortcuts | Golden-file receipt tests + device smoke tests |
| 12 | Security hardening | OWASP review, dependency audit, RLS/partitioning optional migrations, backup/restore drill, penetration test | `SEC-TEST-001` §5 matrix, restore drill |
| 13 | Release | Windows installer, Android APK/AAB, production package, migration bundle, manuals | Release checklist + signed artifacts |

Sequencing rationale: the ledger (Phase 4) is built **before** any complex UI because every screen depends on its correctness (PART 50). Offline (Phase 8) comes after the server-side flows it must mirror, so the device never diverges from a moving target.

## 3. Phase detail

### Phase 1 — Project foundation

* **Goal:** a booting, linted, tested skeleton with a real database.
* **Deliverables:** `apps/api` package skeleton with `core/{config,security,database,logging,exceptions,money,numbering,permissions}`, `app/main.py`, Dockerfiles, `docker-compose.yml` (+ `.prod`), nginx config, PostgreSQL init scripts (roles, `nexus_exchange`, UTC), Redis config, `alembic/` with the initial revision generated from `schema.sql`, idempotent `seeds/`, `pyproject.toml` (ruff/mypy/pytest), `.github/workflows/ci.yml`, `.env.example`, `scripts/dev_up.sh`, `scripts/migrate.sh`, `scripts/test_all.sh`.
* **Tests:** schema-gate (invariant suite in CI), migrate-gate (`alembic upgrade head` + no drift), config-fail-fast, health endpoints, seed idempotency (running seeds twice changes nothing).
* **Exit criteria:** `docker compose up -d` brings all five services healthy from a clean checkout; `alembic upgrade head` applies cleanly; CI green.
* **Risks:** no Docker in the current sandbox → deliverable is verified by an operator command and reported honestly as unverified here.

### Phase 2 — Authentication and RBAC

> **Status: delivered (pending approval).** Implemented and verified in `feat(api): Phase 2`; the
> phase report is `docs/phases/PHASE2_REPORT.md`, the endpoint/error additions are in
> `docs/api/API_CONTRACT.md` §2/§2.2/§4/§7, and the control-level description is in
> `docs/security/SECURITY.md` §2/§3. No schema change was required.

* **Deliverables:** `auth_service`, `user_service`, `device_service`, JWT issue/verify with `jti` revocation, Argon2id hashing + parameter upgrade, refresh rotation families with reuse detection, rate limiting, lockout, device registration/revocation, permission registry and role seeding, `api/v1/{auth,users,roles,devices}.py`.
* **Tests:** the full §4.1 catalogue, plus the authorization matrix for every role.
* **Exit criteria:** a user without a permission cannot call the protected endpoint (PART 50 Phase 2 acceptance), token reuse is detected, revoked devices are denied.

### Phase 3 — Core master data

* **Deliverables:** currencies, branches, customers (+ code generation), accounts (chart of accounts seed), exchange rates (append-only, audited), rate resolution service and endpoints.
* **Tests:** CRUD + uniqueness + immutability (currency code), rate history and precedence (branch over global), audit rows on rate changes, inactive-entity rejection.
* **Exit criteria:** a manager can create a currency, branch, customer and rate; every rate change produces an audit row; no edit path exists for `currencies.code`.

### Phase 4 — Accounting engine

* **Deliverables:** `accounting_service` (`create_journal_entry`, `validate_balanced_entry`, `post_exchange`, `post_cash_movement`, `post_expense`, `reverse_transaction`, `get_account_balance`, `get_trial_balance`), period-agnostic posting, reversal generator, balance/trial-balance queries, `repository` layer for the ledger.
* **Tests:** the §4.3 catalogue against real PostgreSQL, plus service-level reproduction of the four Phase 0 defects' regression tests.
* **Exit criteria:** BUY, SELL, CASH IN, CASH OUT, EXPENSE and REVERSAL each produce balanced, audited, reversible entries (PART 50 Phase 4).

### Phase 5 — Exchange

* **Deliverables:** `exchange_service` (rate resolution, tolerance, computation, numbering via `next_document_number`, cash movements, posting, audit), endpoints per `API_CONTRACT.md` §9.3, receipt payload, cancel/reverse.
* **Tests:** §4.2 catalogue including idempotency and offline-origin payloads.
* **Exit criteria:** a real buy and sell post correct journals, print-ready receipts and appear in reports; reversals preserve history.

### Phase 6 — Cash

* **Deliverables:** `cash_service` (open/close sessions, in/out, adjustment, movement history, position queries), `cash_sessions`/`cash_session_lines` endpoints.
* **Tests:** §4.4 catalogue.
* **Exit criteria:** a shift can be opened, operated and closed with an audited difference; the position can never go negative.

### Phase 7 — Reports

* **Deliverables:** `report_service` + endpoints of `API_CONTRACT.md` §9.6, CSV/PDF export path, report caching policy.
* **Tests:** §4.6 catalogue (trial balance identity, P/L reconciliation, filter correctness, reversed-document handling).
* **Exit criteria:** every report is reproducible from immutable tables and the reconciliation identities of `ACCOUNTING_MODEL.md` §8 hold numerically.

### Phase 8 — Offline

* **Deliverables:** Drift schema (PART 35 + `sync_queue`, `number_allocations`, `allocations`), SQLCipher key management, repositories with local/remote implementations, outbox, sync engine (push/pull, backoff, conflict queue, remote wipe), `sync_service` + endpoints, device-side reconciliation view.
* **Tests:** the ten sync scenarios of `SYNC_DESIGN.md` §15, plus local transaction atomicity (crash simulation).
* **Exit criteria:** a device with no connectivity can complete a sale inside its allowance; after reconnect the server applies it exactly once; conflicts are visible and resolvable.

### Phase 9 — Multi-branch

* **Deliverables:** branch scoping across services/queries, branch-scoped inventory accounts, device policies per branch, branch reports, inter-branch transfer with clearing account.
* **Tests:** scope/permission matrix, branch isolation of data, group trial balance still balanced after inter-branch movements.
* **Exit criteria:** one branch cannot spend or read another's cash; group reports reconcile.

### Phase 10 — Transfers

* **Deliverables:** `transfer_service` (create, approve, pay, cancel, commission recognition, payout FX handling), reference numbering (`TR-YYYYMMDD-NNNNNN`), endpoints, lifecycle audit.
* **Tests:** lifecycle state machine, permission split (create/approve/pay/cancel), cancellation before/after payout, cross-currency payout FX result.
* **Exit criteria:** a transfer can be created, approved, paid and cancelled with correct journals at each step and no hard deletes.

### Phase 11 — Printing

* **Deliverables:** receipt templates (A4 PDF and 80 mm thermal), ESC/POS encoder, Bluetooth/USB transports, printer settings screen, in-app calculator with keyboard shortcuts (PART 54), golden-file templates per language.
* **Tests:** golden-file rendering (Dari/Pashto/English, RTL), ESC/POS byte-level tests, printer error handling.
* **Exit criteria:** a completed transaction prints identically from stored data on both paper sizes; reprints are permitted and audited as reprints.

### Phase 12 — Security hardening

* **Deliverables:** OWASP ASVS review and fixes, dependency audit, optional migrations (partitioning, `pg_trgm` search index, RLS for readers), backup/restore automation with verification, alerting rules, penetration-test remediation, rotation runbooks.
* **Tests:** `SEC-TEST-001` §5 in full plus a restore drill with chain verification.
* **Exit criteria:** zero unpatched high/critical findings, restore drill within RTO/RPO, hash chain valid after restore.

### Phase 13 — Release

* **Deliverables:** signed Windows installer, Android APK/AAB, Docker production package, database migration bundle, `docs/user-manual/*`, deployment and upgrade runbooks, release notes.
* **Tests:** install → migrate → seed → smoke (`scripts/smoke_prod.sh`) on a clean production-like host; upgrade path from the previous release.
* **Exit criteria:** PART 67 MVP checklist fully green with evidence attached.

## 4. MVP acceptance mapping (PART 67)

| Acceptance criterion | Phase | Evidence |
| --- | --- | --- |
| Login works | 2 | §4.1 suite |
| RBAC works | 2, 9 | Authorization matrix |
| Currency works | 3 | Master-data tests |
| Customer works | 3 | CRUD + statement tests |
| Buy works | 5 | §4.2 |
| Sell works | 5 | §4.2 |
| Accounting works | 4 | §4.3 + invariant suite |
| Cash works | 6 | §4.4 |
| Reversal works | 4, 5 | §4.3/§4.2 reversal cases |
| Audit works | 2–8 | Audit assertions in every suite + `GET /audit/verify` |
| Reports work | 7 | §4.6 reconciliation |
| Offline works | 8 | Sync catalogue |
| Sync works | 8 | Sync catalogue |
| Backup works | 12 | Backup tests |
| Restore works | 12 | Restore drill with chain verification |
| Tests pass | all | CI gates of `SEC-TEST-001` §6 |

## 5. Cross-phase risks

| # | Risk | Impact | Mitigation | Owner phase |
| --- | --- | --- | --- | --- |
| R1 | Offline devices over-trading before the allowance is enforced end to end | Money creation | Allocation + number blocks + rate snapshot enforced on both sides; server recomputation; allocation tests in Phases 8/12 | 8 |
| R2 | Rounding disagreements between Dart and Python | Reconciled totals differ | Single arithmetic rule (`ACCOUNTING_MODEL.md` §4), decimal strings on the wire, `Money` type in Dart, cross-language round-trip tests | 5, 8 |
| R3 | Carrying-rate computation drifting between ledger and cache | Wrong FX results | Balance derived from the ledger at posting time; cache rebuild verified nightly | 4 |
| R4 | Timezone/business-date errors (Dari calendar display, UTC storage) | Wrong daily reports | UTC storage, branch timezone only for display/business date, `freezegun` tests around midnight | 3, 7 |
| R5 | Flutter/Docker unavailable in the build sandbox | Phases 8–13 unverified here | Report as implemented-but-unverified with exact operator commands; keep pure-Dart logic unit-testable without devices | 1, 8 |
| R6 | Migration lock on large tables | Downtime | Expand/contract + `NOT VALID` constraints, off-peak windows, statement timeouts | 1, 12 |
| R7 | Printer hardware variance (ESC/POS dialects) | Unusable receipts | Byte-level encoder tests + configurable dialect profiles + field validation with the customer's printers | 11 |
| R8 | Regulatory scope creep without legal basis | Non-compliance or refused features | PART 66 boundary, jurisdictional implementation only with legal review | 12, 13 |
| R9 | Single-developer/small-team key-person risk | Delivery stalls | This documentation set (decisions recorded with rationale), tests as executable specs, ADR list in `ARCHITECTURE.md` §11 | all |
| R10 | Scope pressure to add silent edits/hard deletes | Auditability loss | Refused by design; documented in `SECURITY.md` §10 | all |

## 6. Change control

1. Any deviation from the master prompt or from these documents is recorded as a numbered decision (see `SCHEMA.md` §9 "D-nn" and `ARCHITECTURE.md` §11 "ADR-nn") **with its rationale and trade-off**, in the same change that implements it.
2. A change that weakens an invariant is not a refactor; it requires a documented decision and an updated invariant test.
3. Documents and code move together: a PR that changes behaviour without updating the affected document is incomplete.
4. Phase 0 must be approved before Phase 1 begins; approval is recorded in the project log with the approver's name and date.

## 7. Traceability

| Master prompt | Section |
| --- | --- |
| PART 50 | §3 (phases and their acceptance) |
| PART 56, PART 59, PART 61 | §1, §2 |
| PART 67 | §4 |
