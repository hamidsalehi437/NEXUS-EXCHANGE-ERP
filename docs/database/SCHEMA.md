# NEXUS EXCHANGE ERP — Database Schema Policy

| Field | Value |
| --- | --- |
| Document ID | `DB-SCHEMA-001-DOC` |
| Version | 1.0 (Phase 0) |
| Status | **Proposed — pending Phase 0 approval** |
| Engine | PostgreSQL 16 (`nexus_exchange`, UTC) |
| Normative DDL | `docs/database/schema.sql` |

> **خلاصه فارسی** — این سند قواعد اسکیما را تعیین می‌کند: نوع‌داده‌ها (همه مبالغ `NUMERIC(30,10)`، همه زمان‌ها `TIMESTAMPTZ`)، سیاست محدودیت‌ها و ایندکس‌ها، فهرست Trigger/Function، نقش‌ها و سطح دسترسی، انحراف‌های مستند از پرامپت اصلی، و راهبرد Migration با Alembic. همچنین نتیجه راستی‌آزمایی واقعی روی PostgreSQL 16.2 در بخش ۲ ثبت شده است.

---

## 1. Type policy

| Concern | Rule |
| --- | --- |
| Primary keys | `UUID` with `DEFAULT gen_random_uuid()` (core function, no extension) |
| Money / rates / quantities | `NUMERIC(30,10)` only. `real`, `double precision`, `money` are forbidden — a self-check at the end of `schema.sql` raises if any appears |
| Timestamps | `TIMESTAMPTZ` (UTC storage). `timestamp without time zone` is forbidden and detected by the same self-check. Display timezone comes from `branches.timezone` |
| Enumerations | `VARCHAR` + named `CHECK` constraint (PART 6–19 specified `VARCHAR`). Extending a value is a one-line constraint migration; values are never renumbered |
| Short text codes | `VARCHAR(n)` with a format `CHECK` (`branches.code`, `currencies.code`, `users.username`, `permissions.code`) |
| Structured/semi-structured | `JSONB` only for audit snapshots, sync payloads and change-stream payloads |
| Generated columns | `cash_movements.signed_amount`, `journal_lines.foreign_amount` — deterministic, immutable expressions only |
| Booleans | `NOT NULL DEFAULT TRUE/FALSE`; never nullable flags |
| NULLs | Used only where the domain genuinely has "not applicable" (e.g. `customers.branch_id` = shared, `exchange_rates.branch_id` = global quote) |

## 2. Verification evidence (executed, not asserted)

The reference DDL and its invariant suite were executed against a real PostgreSQL **16.2** instance during Phase 0:

| Check | Command | Result |
| --- | --- | --- |
| DDL applies to an empty database | `psql -v ON_ERROR_STOP=1 -f docs/database/schema.sql` | **exit 0**, self-check notice: `30 NUMERIC(30,10) columns, 0 float columns` |
| Invariant suite | `psql -v ON_ERROR_STOP=1 -f tests/invariants/phase0_schema_invariants.sql` | **exit 0**, **27/27 assertions PASS**, `ledger totals: debit = credit = 1770000.0000000000`, `audit chain: valid` |
| Object inventory | `information_schema` / `pg_indexes` | 31 tables, 5 views, 23 functions, 58 triggers, 103 indexes, 413 constraints |

Defects found by the suite and fixed during Phase 0 (recorded for transparency):

| # | Defect | Fix |
| --- | --- | --- |
| 1 | `ck_cash_movements_adjustment_sign` accepted `movement_type = 'ADJUSTMENT'` with `adjustment_sign IS NULL`, because `NULL IN (-1,1)` is NULL and a `CHECK` treats NULL as satisfied | Added explicit `adjustment_sign IS NOT NULL` guard |
| 2 | Reversal design was self-contradictory: the original row holds `status = 'REVERSED'` while the reversing row holds the pointer, so the original could never satisfy a `reversal_of_id IS NOT NULL` requirement | Replaced with `ct_exchange_reversal_bound` (deferred trigger, invariant I-4) + `ck_exchange_transactions_reversal_reason` |
| 3 | `next_document_number` produced `NX-000001`, omitting the period, so numbers could collide across days | Signature changed to `(prefix, scope, period, width)` emitting `NX-20260911-000001` |
| 4 | Sub-query in a `CHECK` constraint (`branches.timezone`) is illegal in PostgreSQL | Replaced with a format `CHECK`; zone validity is verified by the service and by `AT TIME ZONE` at render time |

Reproduce with any PostgreSQL 16 (no extensions required):

```bash
createdb nexus_exchange
psql -v ON_ERROR_STOP=1 -d nexus_exchange -f docs/database/schema.sql
psql -v ON_ERROR_STOP=1 -d nexus_exchange -f tests/invariants/phase0_schema_invariants.sql
```

## 3. Zero-extension policy and optional hardening

`schema.sql` requires **no PostgreSQL extension**: `gen_random_uuid()` (core ≥ 13), `sha256()`/`encode()` (core ≥ 11) and an equality `UNIQUE` index replace `pgcrypto`, `btree_gist` and `pg_trgm`. This keeps the schema portable to managed cloud PostgreSQL and to minimal images.

Optional, deployment-gated hardening (added by dedicated Phase 1/12 migrations when `postgresql-contrib` is present — never silently assumed):

| Capability | Migration | Benefit |
| --- | --- | --- |
| Trigram search | `CREATE EXTENSION pg_trgm; CREATE INDEX … USING gin (full_name gin_trgm_ops)` on `customers` | Sub-100 ms partial-name search on the counter |
| Vector search (already available in the verified build) | `vector` extension | Future duplicate-customer detection |
| Partitioning | `audit_logs`, `change_log`, `sync_events` by month | Bounded index size, cheap retention |
| Row-level security | Per-branch policies for `nexus_reader` | Defence in depth for BI/reporting roles |

## 4. Constraint taxonomy

| Prefix | Kind | Example |
| --- | --- | --- |
| `ck_` | `CHECK` | `ck_journal_lines_single_sided` |
| `ux_` | Unique index/constraint | `ux_users_username_lower`, `ux_exchange_transactions_client_event` |
| `ix_` | Non-unique index | `ix_journal_lines_account` |
| `fk_` | Foreign key | `fk_exchange_transactions_cash_session` |
| `ex_` | Exclusion (reserved; none required in v1.0) | — |
| `trg_` | Row/statement trigger | `trg_exchange_transactions_immutable` |
| `ct_` | Constraint (deferred) trigger | `ct_journal_lines_balanced_insert` |

### 4.1 Custom SQLSTATEs

Application code maps these to the API error envelope (`API_CONTRACT.md` §4):

| SQLSTATE | Meaning | API code |
| --- | --- | --- |
| `NEX01` | Insufficient currency/cash balance | `INSUFFICIENT_BALANCE` (409) |
| `NEX02` | Journal entry unbalanced, < 2 lines, or invalid line | `JOURNAL_UNBALANCED` (500 — programming error) |
| `NEX03` | Invalid status transition | `INVALID_STATUS_TRANSITION` (409) |
| `NEX04` | Reversal target invalid / unbound / already reversed | `ALREADY_REVERSED` (409) / `REVERSAL_INVALID` (422) |
| `NEX05` | Cash reconciliation incomplete | `CASH_RECON_INCOMPLETE` (422) |
| `NEX06` | Attempt to modify an immutable field | `IMMUTABLE_FIELD` (409) |
| `P0001` | Append-only table mutation | `APPEND_ONLY_VIOLATION` (403/500) |

## 5. Trigger inventory

| Trigger | Table | Timing | Purpose |
| --- | --- | --- | --- |
| `trg_*_updated_at` | users, customers, exchange_transactions, transfers, device_allocations, sync_cursors | BEFORE UPDATE | Maintain `updated_at` |
| `trg_*_version` | exchange_transactions, transfers | BEFORE UPDATE | Optimistic concurrency counter (`version`) |
| `ct_journal_lines_balanced_insert/update/delete` | journal_lines | AFTER, **deferred** | `SUM(debit) = SUM(credit)`, ≥ 2 lines, single-sided lines |
| `trg_journal_lines_balance_cache` | journal_lines | AFTER INSERT, statement | Maintain the rebuildable `account_balances` cache |
| `ct_cash_movements_non_negative` | cash_movements | AFTER, **deferred** | Cash position can never be negative |
| `ct_exchange_reversal_bound` | exchange_transactions | AFTER, **deferred** | A `REVERSED` row must have a bound reversal row |
| `trg_audit_logs_chain` | audit_logs | BEFORE INSERT | Hash-chain link with advisory-lock serialisation |
| `trg_*_no_update` / `*_no_delete` | audit_logs, journal_lines, journal_entries, cash_movements, expenses, exchange_transactions, transfers, users, branches, customers, currencies, accounts, sync_events | BEFORE UPDATE/DELETE | Append-only / no hard delete |
| `trg_*_immutable` | exchange_transactions, transfers, journal_entries, sync_events, currencies | BEFORE UPDATE | Money, identity and envelope columns freeze after posting |
| `trg_*_status` | exchange_transactions, transfers | BEFORE UPDATE | State machines |
| `trg_exchange_transactions_reversal` | exchange_transactions | BEFORE INSERT | Reversal must mirror currencies and amounts |
| `trg_cash_session_lines_recon` | cash_session_lines | BEFORE INSERT/UPDATE | `difference = counted − expected` or reject incomplete reconciliation |
| `trg_change_log_*` | 8 syncable tables | AFTER INSERT/UPDATE | Feed the offline pull stream |

## 6. Function inventory

| Function | Kind | Purpose |
| --- | --- | --- |
| `next_document_number(prefix, scope, period, width)` | atomic | `NX-20260911-000001` style numbering |
| `resolve_exchange_rate(from, to, branch, at)` | STABLE | Quote in force (branch quote wins, newest wins) |
| `available_allocation(device, currency, at)` | STABLE | Remaining server-granted offline allowance |
| `rebuild_account_balances()` | maintenance | Deterministic rebuild of the balance cache |
| `verify_audit_chain(from_seq)` | audit | Returns the first broken hash-chain link |
| `nexus_assert_journal_balanced()`, `nexus_assert_non_negative_cash()`, `nexus_assert_reversal_bound()`, `nexus_validate_*` | trigger bodies | Invariant enforcement |
| `nexus_log_change()`, `nexus_audit_chain()`, `nexus_maintain_account_balances()`, `nexus_set_updated_at()`, `nexus_bump_version()`, `nexus_forbid_mutation()` | trigger bodies | Cross-cutting maintenance |

## 7. Views

| View | Source of truth | Used by |
| --- | --- | --- |
| `v_trial_balance` | `journal_lines` | `GET /reports/trial-balance` |
| `v_account_balances` | `account_balances` cache joined to `accounts` | Ledger and account screens |
| `v_cash_position` | `cash_movements` | `GET /cash/balance`, cash reports |
| `v_currency_position` | `cash_movements` | Inventory/position display |
| `v_exchange_daily` | `exchange_transactions` | `GET /reports/daily`, `GET /reports/exchange` |

Legal/financial reporting always reads the ledger or the movement tables. The cache is never the basis of a report (invariant I-2).

## 8. Roles and privileges

| Role | NOLOGIN | Purpose | Effective rights |
| --- | --- | --- | --- |
| `nexus_owner` | yes | Owns the schema, runs migrations and seeds | Full DDL/DML |
| `nexus_app` | yes | Application runtime | `SELECT/INSERT/UPDATE` on operational tables; **no `DELETE` on ledgers, business documents or master data**; **no `UPDATE` on `audit_logs`, `journal_lines`, `cash_movements`** |
| `nexus_reader` | yes | Reporting/BI read-only | `SELECT` only |
| `nexus_auditor` | yes | External auditor | `SELECT` only (audit + ledgers) |

Deployments create login roles that inherit from these groups (see `docs/security/SECURITY.md` §8). The verified suite proves `nexus_app` is denied `DELETE`/`UPDATE` on ledgers even if application logic is bypassed (3/3 denials).

`nexus_app` also holds `SELECT` on `alembic_version` (D-21), the one table Alembic creates
outside the frozen DDL: since the reference file is checksummed and frozen, that grant is
delivered by the migration `0002_runtime_schema_revision` rather than by an edit to it.

`nexus_app` holds exactly one `DELETE` privilege: `idempotency_keys` (D-20). It is an operational table — no money, no audit trail, no business document — and PART 40's retention window (`IDEMPOTENCY_RETENTION_DAYS`, default 30 days) requires that records whose request can no longer be replayed are removed by the maintenance worker. Every other financial, document, master-data and audit table keeps the revocations above.

## 9. Documented deviations and additions

"Deviations" are places where the Phase 0 schema is stricter than, or extends, the DDL in PARTS 6–19. Each is deliberate and each is reflected in code, tests and this document.

| # | Change | Type | Rationale | Risk if omitted |
| --- | --- | --- | --- | --- |
| D-01 | `VARCHAR` + named `CHECK` instead of native `ENUM` | Decision | Values change over the product's life; constraint migrations are online and reviewable | `ALTER TYPE … ADD VALUE` cannot run inside a transaction and blocks rolling deploys |
| D-02 | `journal_lines.exchange_rate NOT NULL DEFAULT 1` (was nullable) | Tightening | Every line needs a functional-currency conversion basis to be reportable | Silent NULL rates make historical revaluation and audit impossible |
| D-03 | `journal_lines.foreign_amount` generated column | Addition | Derives the foreign quantity from `(debit+credit)/rate`, so quantity and value can never disagree | Quantity would be recomputed ad hoc and drift |
| D-04 | Money columns immutable after posting (trigger) | Tightening | PART 22: corrections are reversals, never edits | History could be rewritten by an UPDATE |
| D-05 | `ux_journal_entries_one_per_reference` | Addition | Prevents double posting of one business document (PART 48 "duplicate posting rejected") | Retried requests could post twice |
| D-06 | `audit_logs.seq/prev_hash/chain_hash/request_id` | Addition | Tamper-evidence (PART 18's "never deleted" plus detectability) | Deletion or silent edits would be undetectable |
| D-07 | `permissions`, `role_permissions`, `user_permissions` | Addition | PART 41 lists permissions; explicit per-user deny is needed for break-glass and scoping | RBAC would be role-only and unauditable |
| D-08 | `refresh_tokens` | Addition | PART 24/42 require rotation and reuse detection | Rotation without a store cannot detect replay |
| D-09 | `journal_lines.currency_id NOT NULL` (was nullable) | Tightening | Multi-currency correctness; an unclassifiable line is a bug | Balances could be keyed to NULL currency |
| D-10 | `exchange_rates.branch_id` + unique instant | Addition | Branch-specific quotes (PART 9 multi-branch) and duplicate-quote prevention | Two conflicting quotes at the same instant |
| D-11 | `cash_sessions`, `cash_session_lines`, `cash_movements.adjustment_sign`, generated `signed_amount` | Addition | PART 30 `cash/close` and PART 48 "cash difference" need a reconciliation artefact and unambiguous signs | Cash close would be a report-only fiction |
| D-12 | `transfers` lifecycle actors, `payout_amount`, `payout_currency_id`, `version`, `origin`, `client_event_id` | Addition | PART 31 lifecycle and PART 34 offline ingestion | Approvals/payouts would be unattributed |
| D-13 | `change_log`, `sync_cursors`, `sync_conflicts`, `idempotency_keys`, `sequences`, `allocation_policies`, `device_allocations` | Addition | PARTS 19/34/37/40 need a server-side change stream, conflict store, replay guard, numbering and offline allowances | Offline-first would be unimplementable |
| D-14 | `users` (lockout/password age), `devices` (revocation), `customers` (is_active, branch, PII-minimal stamp) | Addition | PART 42 (rate limiting, device revocation), PART 25 (no hard delete), PART 65 (PII minimisation) | Security controls would live only in memory |
| D-15 | `journal_entries.branch_id`, `device_id`, `reversal_of_id` | Addition | Multi-branch trial balance, device attribution, reversal linkage | Branch reporting would require fragile joins |
| D-16 | `account_balances` cache (+ `rebuild_account_balances()`) | Addition | O(1) balance reads for the counter; rebuildable, never authoritative | Balance screens would scan the ledger on every keystroke |
| D-17 | Zero-extension policy | Decision | Portability across managed PostgreSQL; verified on a build without contrib | Deployments could fail on `CREATE EXTENSION` |
| D-18 | Status machines as triggers | Addition | PART 14/15 status sets are meaningless without transition rules | `REVERSED → COMPLETED` would be possible |
| D-19 | Reversal link direction fixed (reversing row points at the original) + deferred I-4 trigger | Decision | Makes "reversed ⇒ reveral exists" checkable without circular constraints | Ambiguity that the Phase 0 suite caught as defect #2 |
| D-20 | `GRANT DELETE ON idempotency_keys TO nexus_app` | Correction | The runtime role is least-privilege (`SELECT/INSERT/UPDATE` only), which left the retention worker unable to delete expired idempotency records — the scheduled task failed with `permission denied`, discovered while verifying the Phase 1 worker against a database owned by a non-superuser role | Idempotency records would grow without bound and the documented retention policy would be unenforceable; the alternative (running the worker as the schema owner) would hand DDL rights to a service process |
| D-21 | `GRANT SELECT ON alembic_version TO nexus_app` (migration `0002_runtime_schema_revision`) | Correction | The runtime role could not read the applied migration revision, so `/api/v1/health/ready` answered `503` with `postgresql: unavailable (ProgrammingError)` in every deployment that uses the documented two-role model. Alembic creates `alembic_version` *before* it runs the migration script, so the frozen DDL's `ALTER DEFAULT PRIVILEGES … TO nexus_app` (which only affects objects created afterwards) never covered it, and the explicit grant list enumerates the schema's own tables. Found by the compose acceptance job's readiness step (Phase 2, defect 16); a single-role test database cannot show it | Orchestrators and load balancers would keep a healthy API out of service; the alternative (readiness ignoring the revision) would hide a genuine permission failure instead of reporting it |

**Not implemented on purpose (no placeholders):** FX revaluation of open positions, hard period locking and month-end closing (see `ACCOUNTING_MODEL.md` §11); KYC/AML modules (regulatory scope, `SECURITY.md` §10); payroll, inventory and tax modules (out of product scope).

## 10. Migration strategy (Alembic)

1. **The reference file is the contract.** `docs/database/schema.sql` and the ORM models must agree. Phase 1 turns the file into the initial revision; afterwards, every structural change is a new revision plus an updated reference file. A revision that changes no structure — `0002_runtime_schema_revision` is a single grant — does not touch the frozen, checksummed file, and `scripts/schema_gate.py db-db` keeps proving that what the migrations build is structurally identical to it.
2. **Drift gate in CI.** `alembic upgrade head` on an empty database → `alembic check` (autogenerate diff) must be empty; a second job applies the generated schema and runs `tests/invariants/phase0_schema_invariants.sql`.
3. **Naming convention** is configured in `alembic.ini` (`%(table_name)s` + prefixes from §4) so autogenerate produces names matching the reference file.
4. **Forward-only by default.** `downgrade()` is implemented where it is safe and omitted with an explanatory exception where data loss is unavoidable (e.g. dropping a ledger column); production rollback is by restore, not by downgrade.
5. **Expand/contract for online changes:** add nullable column → backfill in batches → add constraint `NOT VALID` → validate → flip application → drop old column in a later release.
6. **Money-safety rule:** a migration may never widen money to float, may never drop a ledger column in the same release that stops writing it, and any constraint on ledgers is added as `NOT VALID` then `VALIDATE CONSTRAINT` to avoid a long ACCESS EXCLUSIVE lock.
7. **Pre-flight:** `scripts/migrate.sh` takes a verified backup and records `alembic current` + row counts in the audit log before running.
8. **Seed idempotency:** `seeds/*.py` upsert by natural key (currency code, role name, account code) and log an audit event; `seeds/004_dev_admin.py` refuses to run when `APP_ENV != development`.

## 11. Backup, restore and integrity verification

| Concern | Mechanism |
| --- | --- |
| Logical backup | `pg_dump --format=custom` daily (retained 30 days) plus pre-migration snapshots |
| Physical/PITR | `pg_basebackup` + WAL archiving (production), restore drill in Phase 12 |
| Encryption | Backups are encrypted at rest (`age`/GPG recipient key held outside the repository) |
| Verification | After every restore: `SELECT * FROM verify_audit_chain();` returns **no rows**; `rebuild_account_balances()`; compare `Σ debit = Σ credit`; compare trial balance hash against the pre-restore snapshot |
| Offline client backup | Encrypted SQLite copy (see `SYNC_DESIGN.md` §9) |

A restore that does not pass the three checks above is treated as a failed restore, not a successful one.

## 12. Traceability

| Master prompt | Section |
| --- | --- |
| PART 5, PART 6–19 | §1, §9 (column fidelity and documented additions) |
| PART 40, PART 41, PART 42 | §8, §9 (D-07, D-08, D-13, D-14) |
| PART 46, PART 49 | §4, §5 (invariant enforcement) |
| PART 56 | §10 (CI drift gate) |
| PART 59 | §2 (PLAN → IMPLEMENT → TEST → FIX → VERIFY → DOCUMENT evidence) |
| PART 62 | §1 (money types), self-check in `schema.sql` |
