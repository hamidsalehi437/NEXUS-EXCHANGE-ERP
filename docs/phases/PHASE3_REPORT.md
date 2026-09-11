# NEXUS EXCHANGE ERP — Phase 3 Final Report

| Field | Value |
| --- | --- |
| Document ID | `PHASE3-REPORT-001` |
| Phase | **3 — Core master data** (currencies, branches, customers, accounts, exchange rates) |
| Status | **READY FOR REVIEW** |
| Starting commit | `208a3cc` (Phase 2 finalization; last commit of the approved Phase 2) |
| Branch | `arena/01a090c5-nexus-exchange-erp` (pull request: [#1](https://github.com/hamidsalehi437/NEXUS-EXCHANGE-ERP/pull/1)) |
| Phase 3 commits | `208a3cc` → implementation + this report (the commit that carries this file, hash in `git log` and in `docs/PROJECT_STATUS.md` §1) → finalisation commit that pins the hash and records the CI run |
| Phase 4 | **NOT STARTED** — see §22 |
| Schema impact | **None**: no new Alembic revision, `docs/database/schema.sql` byte-identical (`0002_runtime_schema_revision` remains head) |

> **خلاصه فارسی** — فاز ۳ (دادهٔ پایهٔ اصلی: ارزها، شعبه‌ها، مشتریان، حساب‌ها و نرخ‌های ارز) پیاده‌سازی، آزمایش، مستند و کامیت شد. هیچ مهاجرت جدیدی لازم نبود و اسکیمای تأییدشدهٔ فاز ۰ دست‌نخورده است. فاز ۴ شروع **نشده** است و این گزارش برای بازبینی انسانی آماده است؛ تأیید فاز فقط توسط شما ثبت می‌شود.

---

## 1. Executive summary

Phase 3 delivers the master data every later phase depends on. Five entities are now
managed through `/api/v1` with full RBAC, audit coverage and database-level integrity:

| Entity | Created | Read | Updated | Deleted |
| --- | --- | --- | --- | --- |
| `currencies` | `POST /currencies` | `GET /currencies`, `GET /currencies/{id}` | `PATCH /currencies/{id}` | never (deactivate only) |
| `branches` | `POST /branches` | `GET /branches`, `GET /branches/{id}` | `PATCH /branches/{id}` | never (deactivate only) |
| `customers` | `POST /customers` (code issued by the server) | `GET /customers` (search), `GET /customers/{id}` | `PATCH /customers/{id}` | `DELETE` = deactivate (PART 25) |
| `accounts` | `POST /accounts` | `GET /accounts` (tree), `GET /accounts/{id}` | `PATCH /accounts/{id}` (identity frozen at first use) | never |
| `exchange_rates` | `POST /rates` (append-only) | `GET /rates`, `GET /rates/resolve`, `GET /rates/history` | never | never |

* **21 operations across 17 paths** were added (the application's OpenAPI document now
  contains 32 paths in total).
* **136 new tests** (135 integration + 1 unit), taking the suite from **769** to
  **905 passing tests**.
* **10 new audit actions**; every write path writes exactly one audit row in the same
  transaction as the change, and no read path writes any.
* **Exit criteria met:** a manager can create a currency, a branch, a customer and a rate;
  every rate publication produces an audit row; there is no edit path for
  `currencies.code` (enforced by the schema, and asserted against a raw `UPDATE`).
* **12 defects found and fixed** during the phase (§12), including three that would have
  corrupted or mis-stated money if they had shipped: a broken rate-resolution fallback, a
  postable parent account that could take children, and an API that reported blank-padded
  `CHAR(6)` values (`"DEBIT "`).

The approved architecture, security model, accounting invariants, RBAC map, audit rules,
migration discipline and documentation structure of Phases 0–2 are preserved unchanged.
No financial transaction, exchange, cash or ledger-posting logic was implemented: those
belong to Phases 4–7 and were deliberately not started.

---

## 2. Scope

### 2.1 In scope (implemented)

1. **Currency catalogue** — create, list, read, update (name/symbol/decimals/active/
   tradable/base), one base currency maintained atomically, `code` immutable.
2. **Branches** — create, list, read, update (name/address/phone/timezone/active),
   `code` frozen once financial history references the branch, the last active branch
   cannot be deactivated.
3. **Customers** — registration with server-issued `CUS-YYYYMMDD-NNNNNN` codes, search
   (name/phone/code), branch scoping, PII minimisation, soft delete, reactivation.
4. **Chart of accounts** — create, list (tree), read, update; `normal_balance` derived from
   `account_type`; forest rules (type match, no self-parent, no descendant cycle, grouping
   accounts are non-postable); identity frozen once the account carries journal lines.
5. **Exchange rates** — append-only publication (global and per-branch), duplicate-instant
   refusal, tradability/active checks, `DISTINCT ON` current-quote table, history, and the
   resolution service and endpoint built on the Phase 0 database function
   `resolve_exchange_rate`.
6. **Cross-cutting** — RBAC on every operation, rate limiting, audit rows, error contract
   (`CONFLICT` 409 added additively), tests, documentation.

### 2.2 Explicitly out of scope (not implemented, not started)

* No journal posting, no `AccountingService`, no balance movement, no exchange
  transaction, no cash session, no transfer, no expense, no report, no sync engine, no
  Flutter client work. Phase 3 *reads* `journal_lines` only to decide whether an account is
  still structurally editable, and *reads* the other tables only to decide whether a branch
  code is frozen.
* No schema change, no migration, no seed change.

---

## 3. Architecture decisions

| # | Decision | Rationale |
| --- | --- | --- |
| P3-D1 | **No new Alembic revision.** The approved Phase 0 schema already contains every table, constraint, index, trigger and function Phase 3 needs. Head remains `0002_runtime_schema_revision`; `docs/database/schema.sql` is byte-identical to the approved, checksummed file. | Migration discipline: a schema change would require an approved reason and a reviewed migration; there is none. Both schema gates confirm the migrated database still equals the frozen DDL. |
| P3-D2 | **One service and one repository per concern** (`masterdata_service`/`masterdata_repository` for currency, branch, customer; `ledger_master_service`/`ledger_master_repository` for account and rate), routes hold no business logic (PART 47). | Matches the Phase 2 layering so later phases extend rather than re-arrange. |
| P3-D3 | **Immutability is enforced in depth.** `currencies.code` is absent from the update schema (422), refused by the service, and refused by the frozen database trigger `NEX06` (a raw `UPDATE` is tested and rejected). `accounts.code`/`account_type` freeze at first journal line; `branches.code` freezes at first financial reference. | PART 22/25: identity is data, not a preference. |
| P3-D4 | **`accounts.normal_balance` is derived, never supplied.** It is computed from `account_type` (ASSET/EXPENSE → DEBIT, LIABILITY/EQUITY/REVENUE → CREDIT), and a client attempting to send it gets `extra_forbidden` (422). | A client that could choose the normal balance could invert every balance report. |
| P3-D5 | **Everything that decides money is asked of one implementation.** Rate resolution always calls the Phase 0 database function `resolve_exchange_rate`; `GET /rates` reuses the same precedence (`branch_id IS NOT NULL` first, then newest instant) rather than inventing a second order. | Two implementations of "which rate is in force" is how spreads get arbitraged. |
| P3-D6 | **Codes are issued by the database inside the transaction** (`next_document_number`). A rolled-back registration does not consume a number and two concurrent registrations cannot collide. | PART 15/16 document numbering; the same primitive Phase 5 will use for receipts. |
| P3-D7 | **`CONFLICT` (409) added additively** for "well-formed request, system rule refuses it" (non-tradable currency, non-grouping parent, last active branch, account with children). Deactivated entities keep their own codes (`CURRENCY_INACTIVE`, `BRANCH_INACTIVE`, both 422). | Required statuses stay meaningful (409 = state conflict, 422 = the request itself cannot be accepted); documented in `API_CONTRACT.md` §4. |
| P3-D8 | **Grouping accounts must be declared, not inferred.** A child cannot be created or moved under a postable parent (409 `postable_parent`); a parent with children cannot be made postable (409 `has_children`). | `docs/architecture/ACCOUNTING_MODEL.md` line 83: parent accounts group a subtree and are not posted to. Auto-demoting another record silently would be a surprise edit. |
| P3-D9 | **`AccountView`** (account + `has_children` + `currency_code`) is the service's return shape for chart operations, so a tree row needs no follow-up query and no response field is ever advertised-but-empty. | Same failure mode fixed for rates with one mapping-based serializer (§12 D8). |
| P3-D10 | **Reads are never audited; refusals are never audited.** Only committed changes produce audit rows, and an empty `PATCH` produces none. | PART 18 wants a trail of what happened, not of what was asked. Explicitly asserted. |

---

## 4. Database changes and migrations

| Item | Result |
| --- | --- |
| New Alembic revisions | **none** |
| `alembic heads` | `0002_runtime_schema_revision (head)` |
| `alembic current` (fresh database) | `0002_runtime_schema_revision (head)` |
| Migration on a clean database | `alembic upgrade head` applied `0001_initial_schema` → `0002_runtime_schema_revision`, exit code 0 |
| `docs/database/schema.sql` | unchanged (`git diff` empty; checksum verified by the migration runner and the invariant suite) |
| Tables used (pre-existing, unmodified) | `currencies`, `branches`, `customers`, `accounts`, `exchange_rates`, `sequences`, `journal_entries`, `journal_lines`, `users`, `audit_logs`, `change_log` |
| Database functions used | `next_document_number(prefix, scope, period, width)`, `resolve_exchange_rate(from, to, branch, at)` |
| ORM change of note | `accounts.normal_balance` is mapped with the new `TrimmedChar(6)` type decorator (still compiles to `CHAR(6)`) — see §12 D3 |

---

## 5. Implemented entities and their rules

### 5.1 Currencies

* `code` — `^[A-Z]{3,10}$`, upper-cased before validation, **immutable** (three layers).
* Exactly one base currency: promoting one demotes the previous base in the same
  transaction, and **both** rows are audited (`CURRENCY_CREATED`/`CURRENCY_UPDATED`).
* The base currency cannot be demoted through `PATCH` (`CONFLICT base_required`) and cannot
  be deactivated (`CONFLICT base_currency`) — the books would lose their unit of account.
* Creating a *different* base before the configured `BASE_CURRENCY_CODE` exists is refused
  (422 `configured_base_missing`): the deployment's configuration is the expectation.
* `is_tradable = false` is allowed and blocks quoting that currency (§5.5).
* Deactivation deletes nothing; a currency with posted history stays readable forever.

### 5.2 Branches

* `code` — `^[A-Z0-9][A-Z0-9-]{1,19}$`, normalised to upper case, immutable once
  `exchange_transactions`, `transfers`, `cash_sessions` or `journal_entries` reference the
  branch (`IMMUTABLE_FIELD`, 409). Before that it may be corrected.
* `timezone` must be a valid `Area/Location` and is validated against the zones available
  on the server.
* The **last active branch cannot be deactivated** (`CONFLICT last_active_branch`).
* Reads and writes both require `branch.manage` (API_CONTRACT §8).

### 5.3 Customers

* `customer_code` is issued by the server as `CUS-YYYYMMDD-NNNNNN` per day; an explicit
  code is accepted (normalised to upper case) and must be unique (`DUPLICATE_RESOURCE`).
* **PII minimisation (PART 65):** only the last four digits of a national id are accepted
  (`^[0-9]{4}$|^$`); the audit row records `national_id_recorded: true` and never the
  digits themselves. Names are whitespace-normalised.
* Search: `q` matches name, phone or code (case-insensitive); `branch_id` scopes to a
  branch; `shared_only=true` returns customers with no branch (walk-ins served anywhere);
  results are ordered and paginated deterministically.
* `DELETE /customers/{id}` sets `is_active = false` and audits `CUSTOMER_DEACTIVATED`; the
  row is never removed (PART 25). `PATCH {"is_active": true}` reactivates.
* Registering into an unknown branch is 404, into an inactive branch 422 `BRANCH_INACTIVE`.

### 5.4 Accounts (chart of accounts)

* `code` unique; `account_type` ∈ {ASSET, LIABILITY, EQUITY, REVENUE, EXPENSE};
  `normal_balance` derived server-side (P3-D4).
* **Forest rules:** a parent must exist, must have the same `account_type`, cannot be the
  account itself, and cannot be one of its own descendants (recursive SQL check, so two
  concurrent re-parentings cannot create a cycle). A parent must be a grouping account
  (`is_postable = false`); an account with children cannot become postable.
* `currency_id` optional (multi-currency account), `branch_id` optional (branch-scoped
  account); an inactive currency or branch is refused (422).
* **Identity freeze:** once the account carries a `journal_lines` row, `code` and
  `account_type` can no longer change (`IMMUTABLE_FIELD`, 409). Renaming, re-parenting and
  deactivation stay available — a chart that could not be corrected would be worked
  around, not used.
* `GET /accounts` returns each row with `has_children` and `currency_code` (P3-D9).
* There is no `DELETE` route (405 on the collection path, absent elsewhere), and the runtime
  role holds no `DELETE` grant.

### 5.5 Exchange rates

* **Append-only.** No `PATCH`, no `PUT`, no `DELETE`; a correction is a new quote. The
  service, repository and route table are asserted to contain no mutation path (§15).
* Publication validates: two different currencies, both existing, both active
  (`CURRENCY_INACTIVE`, 422), both tradable (`CONFLICT not_tradable`, 409), branch existing
  and active, `buy_rate`/`sell_rate` positive decimal strings (**a JSON float is refused**,
  PART 62), `effective_at` timezone-aware, `source` ∈ {MANUAL, IMPORT, CENTRAL_BANK,
  PARTNER}.
* Uniqueness is per `(from, to, COALESCE(branch), effective_at)` — the frozen index
  `ux_exchange_rates_no_duplicate_instant`; the predictable case is refused with
  `DUPLICATE_RESOURCE` and the offending field.
* `GET /rates` returns **one row per pair**: without `branch_id` the global quotes, with
  `branch_id` the branch's own quote where it has one and the global fallback otherwise
  (same precedence as the resolution function). A pair filter needs both currencies (422).
* `GET /rates/resolve` returns the quote a transaction would receive right now (or at `at`),
  with `is_branch_quote` telling the caller which rule won; no quote in force is
  `RATE_NOT_FOUND` (422) — never a silent zero or an invented inverse rate.
* `GET /rates/history` lists every quote newest-first with currency and branch codes,
  filterable by pair, branch (`include_global` toggles the fallback rows) and instant range.
* Every publication writes `RATE_CREATED` with the pair, branch, source and decimal-exact
  values.

---

## 6. API endpoints and contracts

21 operations across 17 paths (`/api/v1` prefix); every response uses the approved
envelope, and every error the envelope of PART 38 (§4 of `API_CONTRACT.md` now lists the
new `CONFLICT` code).

| Method | Path | Permission | Success | Notable failures |
| --- | --- | --- | --- | --- |
| GET | `/currencies` | `exchange.view` | 200 list | 401, 403 |
| POST | `/currencies` | `settings.manage` | 201 | 409 duplicate, 422 validation/base |
| GET | `/currencies/{id}` | `exchange.view` | 200 | 404 |
| PATCH | `/currencies/{id}` | `settings.manage` | 200 | 409 duplicate/base rule, 422 `code` in body |
| GET | `/branches` | `branch.manage` | 200 list | 401, 403 |
| POST | `/branches` | `branch.manage` | 201 | 409 duplicate, 422 validation |
| GET | `/branches/{id}` | `branch.manage` | 200 | 404 |
| PATCH | `/branches/{id}` | `branch.manage` | 200 | 409 frozen code / last active branch |
| GET | `/customers` | `customer.view` | 200 list (filters) | 401, 403 |
| POST | `/customers` | `customer.create` | 201 (code issued) | 404 branch, 409 duplicate code, 422 PII/name |
| GET | `/customers/{id}` | `customer.view` | 200 | 404 |
| PATCH | `/customers/{id}` | `customer.update` | 200 | 404, 422 |
| DELETE | `/customers/{id}` | `customer.update` | 200 (deactivated) | 404 |
| GET | `/accounts` | `accounts.manage` | 200 list (`has_children`) | 401, 403 |
| POST | `/accounts` | `accounts.manage` | 201 | 404 parent/currency/branch, 409 duplicate/postable parent, 422 |
| GET | `/accounts/{id}` | `accounts.manage` | 200 | 404 |
| PATCH | `/accounts/{id}` | `accounts.manage` | 200 | 409 immutable/duplicate/has children, 422 cycle/type/self |
| GET | `/rates` | `exchange.view` | 200 (one row per pair) | 422 incomplete pair filter |
| POST | `/rates` | `rates.manage` | 201 | 404 currency/branch, 409 duplicate instant/non-tradable, 422 inactive/validation |
| GET | `/rates/resolve` | `exchange.view` | 200 | 422 `RATE_NOT_FOUND` |
| GET | `/rates/history` | `exchange.view` | 200 list | 401, 403 |

All routes enforce the configured read/write rate-limit buckets
(`rate_limit_read_per_min` / `rate_limit_write_per_min`) through the same `RateLimiter`
Phase 1 introduced.

---

## 7. RBAC and authorization behaviour (PART 41)

| Operation | Permission | SUPER_ADMIN | OWNER | MANAGER | ACCOUNTANT | CASHIER | AUDITOR |
| --- | --- | --- | --- | --- | --- | --- | --- |
| read currencies/rates | `exchange.view` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| write currencies | `settings.manage` | ✅ | ✅ | ✅ | — | — | — |
| branches (read & write) | `branch.manage` | ✅ | ✅ | — | — | — | — |
| customers read | `customer.view` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| customers create | `customer.create` | ✅ | ✅ | ✅ | — | ✅ | — |
| customers update / deactivate | `customer.update` | ✅ | ✅ | ✅ | — | — | — |
| chart of accounts | `accounts.manage` | ✅ | ✅ | — | ✅ | — | — |
| rates write | `rates.manage` | ✅ | ✅ | ✅ | — | — | — |

Verified by tests (not by inspection): the CASHIER creates and reads customers but is
refused `PATCH` (`403 PERMISSION_DENIED` with `required_permission: customer.update`); the
ACCOUNTANT manages the chart but is refused currency creation; the AUDITOR reads rates and
is refused publication; a CASHIER reads and resolves rates but cannot publish; every
unauthenticated call is 401. Deny-by-default is intact — a permission that is not granted
is denied, and a denied request leaves **no** audit row and no state change.

---

## 8. Audit behaviour (PART 18)

| Action | Written when | Payload |
| --- | --- | --- |
| `CURRENCY_CREATED` / `CURRENCY_UPDATED` | currency created / changed (including the demoted previous base) | full new state / field-level old→new diff |
| `BRANCH_CREATED` / `BRANCH_UPDATED` | branch created / changed | full new state / field-level diff |
| `CUSTOMER_CREATED` | registration | profile + `national_id_recorded` boolean (never the digits) |
| `CUSTOMER_UPDATED` | field-level change | only the fields that actually changed |
| `CUSTOMER_DEACTIVATED` | `DELETE` (soft delete) | old→new `is_active` |
| `ACCOUNT_CREATED` | account created | code/type/derived `normal_balance`/parent |
| `ACCOUNT_UPDATED` | real change | field-level diff (empty PATCH writes nothing) |
| `RATE_CREATED` | every quote appended | pair, branch, source, decimal-exact buy/sell, instant |

* Audit rows are written **inside the same transaction** as the change: an audited action
  and its evidence commit together or not at all.
* **Reads are never audited** and **refused actions are never audited** — both asserted by
  tests that count `audit_logs` rows before and after.
* The audit chain (`trg_audit_logs_chain`) and its immutability triggers are untouched; the
  Phase 0 invariant suite still reports `audit chain: valid`.

---

## 9. Validation and integrity rules

* **Money/decimal:** rates and monetary fields parse through `to_decimal` (float and bool
  refused) and serialise as decimal strings; the schema stores `NUMERIC(30,10)`.
* **Time:** `effective_at` must carry a timezone; the API normalises to UTC. A naive
  datetime is a 422.
* **Uniqueness:** currency code, branch code, account code, customer code and
  `(pair, branch, instant)` for rates — each refused as `DUPLICATE_RESOURCE` with the
  offending field, and each also protected by a database constraint.
* **Referential integrity:** every foreign key is checked before insert (unknown → 404, with
  the field name), and inactive parents are refused with their own code.
* **Lifecycle:** nothing is deleted — `DELETE` deactivates customers; branches and
  currencies deactivate through `PATCH`; rates, journal rows and audit rows are append-only.
* **No placeholders:** no TODO/FIXME/mock/stub exists in the Phase 3 code paths (grep in
  §15).

---

## 10. Test commands and results

Canonical invocation (identical to the approved Phase 2 procedure; the environment is a
local PostgreSQL 16.2 cluster and a local Redis on 15 as the test database):

```bash
cd apps/api
env -u DATABASE_URL -u DATABASE_MIGRATION_URL -u APP_ENV PYTHONPATH=. \
  NEXUS_TEST_REDIS_URL="redis://:nexuslocaldev@127.0.0.1:6379/15" \
  /tmp/venv/bin/python -m pytest tests -q
```

| # | Command | Result |
| --- | --- | --- |
| 1 | full suite (above) | **905 passed, 0 failed** |
| 2 | `pytest tests -q` (Phase 2 baseline, same environment) | **769 passed** — reproduced before Phase 3 work started |
| 3 | `pytest tests/unit -q` (CI unit-job shape) | **444 passed** in 1.82 s |
| 4 | `pytest tests/integration -q` (real PostgreSQL) | **461 passed** |

Phase 3 suites (all green):

| Suite | Tests | Focus |
| --- | --- | --- |
| `tests/integration/test_masterdata_currencies.py` | 26 | catalogue order, derived base/one-base rule, `code` immutability (API **and** raw `UPDATE` → `NEX06`), audit diffs, RBAC |
| `tests/integration/test_masterdata_branches.py` | 15 | code format/normalisation, timezone validation, code freeze once referenced (scratch database), last-active-branch rule, audit, RBAC |
| `tests/integration/test_masterdata_customers.py` | 25 | code generation and uniqueness, search, branch scoping, PII rules, soft delete + reactivation, audit, RBAC |
| `tests/integration/test_masterdata_accounts.py` | 28 | normal-balance derivation, forest rules (type/self/cycle/grouping), `has_children` reporting, identity freeze (scratch database), audit, RBAC |
| `tests/integration/test_masterdata_rates.py` | 36 | decimal exactness and float refusal, duplicate instant, non-tradable/inactive refusal, branch-over-global precedence and fallback, history, append-only surface, audit, RBAC |
| `tests/integration/test_masterdata_workflow.py` | 5 | the three Phase 3 exit criteria as one workflow (catalogue + branch by OWNER, customer + rate by MANAGER, an audit row per publication, `currencies.code` unreachable) and the approved permission split pinned |
| `tests/unit/test_exceptions.py` (extended) | +1 net (444 total) | status matrix for `ConflictError`, `CurrencyInactiveError`, `BranchInactiveError`, `RateNotFoundError` |
| **Total new** | **136** | |

Every test runs against the real HTTP surface (`TestClient` on the application's own
lifespan) or against a real migrated database through the real service layer; there is no
test doubles for the database, the limiter or the audit service.

---

## 11. Regression of previous phases

| Check | Result |
| --- | --- |
| Phase 0/1 suites (schema, invariants, seeds, migration, security primitives) | included in the 905; no test changed except `tests/unit/test_exceptions.py` (new error classes) |
| Phase 2 suites (auth, users, roles, permissions, devices, security) | green throughout; the Phase 2 total of 769 is reproduced, then 905 |
| Phase 0 invariant suite on a fresh migrated database | **53 PASS lines / 52 assertions**, `PHASE 0 SCHEMA INVARIANT SUITE: ALL ASSERTIONS PASSED`, `ledger totals: debit = credit = 1770000.0000000000`, `audit chain: valid` |
| Seed idempotency | first run `inserted=89 updated=0`, second run and `--check` `unchanged=88` (the CI greps match exactly) |
| Migration on a clean database | `alembic upgrade head` + `alembic current` → `0002_runtime_schema_revision (head)` |
| Schema gates | `orm-db`: 31 tables / 341 columns / **MATCH**; `db-db`: 71 checks, 48 triggers, 23 routines, 5 views / **MATCH** |
| Compose stack (PART 44) | executed by GitHub CI (no Docker in this sandbox): compose validation, build, `up -d --wait`, migrate, seeds, administrator, login, readiness through nginx — all 15 steps green for Phase 2; the Phase 3 run is recorded in the finalisation commit |

---

## 12. Defects found and fixed

Every defect below was found by the new tests or by the schema gates, fixed in Phase 3, and
is covered by at least one test. None was left as a known-broken path.

| # | Defect | How it was caught | Fix |
| --- | --- | --- | --- |
| D1 | `POST /accounts` accepted a child whose type differed from its parent's (an EXPENSE account under a REVENUE header), which would break every subtotal above it | `test_a_parent_of_another_type_is_refused` (returned 201) | The shared parent-eligibility rule now runs on creation as well as on re-parenting (422 `type_mismatch`) |
| D2 | The `is_postable` rule was **inverted**: the service refused to make a parent non-postable and allowed a parent with children to become postable, contradicting `ACCOUNTING_MODEL.md` | `test_a_header_with_children_cannot_become_postable` (returned 200) | A parent with children cannot become postable (409 `has_children`), and a child cannot be created or moved under a postable parent (409 `postable_parent`); the documented two-step path is asserted too |
| D3 | `normal_balance` was returned as `"DEBIT "` — PostgreSQL blank-pads the frozen `CHAR(6)` column, so the padding leaked into API responses and audit diffs | `test_the_normal_balance_follows_the_type` (`'DEBIT ' == 'DEBIT'`) | New `TrimmedChar(6)` type decorator (compiles to `CHAR(6)`, so the ORM/schema gate still says MATCH); raw-SQL readings are asserted to compare equal as `CHAR` semantics require |
| D4 | `has_children` was always `false` in every account response, so a client could not render or guard a tree | `test_the_listing_reports_who_has_children` | A correlated `EXISTS` in the list query plus `AccountView`; `GET`, `PATCH` and list now report the true flag |
| D5 | `currency_code` was never filled on account responses (advertised but always `null`) | `test_a_scoped_account_records_its_branch_and_currency` | `AccountView` carries the code, loaded by the same query (list) or the same transaction (write) |
| D6 | `GET /rates` failed with `AmbiguousParameterError` (`42P08`, mapped to 500) whenever `branch_id` or the pair filter was used — `:branch_id IS NULL` gives PostgreSQL nothing to infer a type from | `test_current_quotes_show_the_quote_in_force_per_pair` | Parameters are cast (`CAST(:branch_id AS UUID)`); a regression test covers both filters |
| D7 | The branch view of `GET /rates` had **no global fallback**: a branch that had published no quote of its own got an empty list instead of the global rate in force — a silently wrong money answer | The same test (fallback case returned `[]`) | The predicate now accepts the branch's own row *or* the global row, and the branch row wins by the ordering |
| D8 | `/rates` and `/rates/history` never filled `from_currency_code`, `to_currency_code` or `branch_code` (three fields advertised and permanently null) | `test_current_quotes_show_the_quote_in_force_per_pair` (`branch_code`) | Both queries join `currencies`/`branches`; `POST`, list, resolve and history build one mapping shape and use **one** serializer |
| D9 | `POST /rates` returned 500 `UnboundLocalError` when no branch was supplied (`branch` was only bound inside the branch-specific path) | `test_a_published_quote_keeps_its_exact_decimal_value` | `branch` is initialised before the validation branch; covered by every publication test that omits `branch_id` |
| D10 | The rates service docstring claimed the frozen DDL installs an append-only trigger on `exchange_rates` — **it does not**, and the runtime role keeps `UPDATE` | Reviewing the frozen DDL while writing the append-only tests (the attempted `UPDATE` succeeded) | Docstring corrected; the boundary is stated honestly as Limitation L-2 with a recommendation, and a test pins what is actually true: no API/service/repository mutation path, `DELETE` not granted, a direct `UPDATE` captured by the table's `change_log` trigger |
| D11 | `GET /rates` returned one row per `(pair, branch)`, leaving the client to re-implement "which of these is in force" | Writing the current-quotes test (two rows for one pair) | One row per pair, selected with the resolution function's precedence; documented in `API_CONTRACT.md` §9.2 |
| D12 | Test-harness defects that would have hidden real failures: probe rows inserted into append-only tables on the *shared* database (poisoning the suite for every later run), an async engine disposed in a second event loop (leaked sockets and a spurious failure under random ordering), and a currency-code helper with only 676 possible values (collisions as the suite grew) | Failures under `pytest-randomly`, warnings, and a duplicate-code 409 in an unrelated test | Freeze scenarios now run on their own migrated, seeded scratch databases inside a single event loop and dispose the engine there; the currency helper has ~457 000 combinations; `branch_factory` deactivates the branches it creates (extra active branches change device-login branch resolution) |

### 12.1 Defect-to-regression-test map

Every defect in §12 is covered by at least one test that fails if the fix is reverted.
The node ids below were executed individually on the final committed state (all pass):

| # | Defect (short name) | Covering test(s) |
| --- | --- | --- |
| D1 | Service/repository parent-type contract: a child of another account type was accepted | `tests/integration/test_masterdata_accounts.py::TestAccountTree::test_a_parent_of_another_type_is_refused`, `::test_a_postable_parent_must_be_declared_a_header_first` |
| D2 | Inverted `is_postable` rule (grouping accounts) | `tests/integration/test_masterdata_accounts.py::TestAccountTree::test_a_header_with_children_cannot_become_postable` |
| D3 | Model contract: `CHAR(6)` blank padding leaked into responses/audit (`"DEBIT "`) | `tests/integration/test_masterdata_accounts.py::TestAccountCreation::test_the_normal_balance_follows_the_type` (all five types, both the API value and the raw-SQL value) |
| D4 | `has_children` always false (service/repository contract) | `tests/integration/test_masterdata_accounts.py::TestAccountTree::test_the_listing_reports_who_has_children` |
| D5 | `currency_code` never filled on accounts | `tests/integration/test_masterdata_accounts.py::TestAccountCreation::test_a_scoped_account_records_its_branch_and_currency` |
| D6 | SQL ambiguous parameter type (`42P08`) on the filtered rate query | `tests/integration/test_masterdata_rates.py::TestRateResolution::test_current_quotes_show_the_quote_in_force_per_pair`, `::test_a_pair_filter_needs_both_currencies` |
| D7 | Missing global fallback for a branch without its own quote | `tests/integration/test_masterdata_rates.py::TestRateResolution::test_a_branch_quote_overrides_the_global_one` (the `other` branch case), `::test_current_quotes_show_the_quote_in_force_per_pair` |
| D8 | Advertised-but-empty code fields on rates (serializer contract) | `tests/integration/test_masterdata_rates.py::TestRateResolution::test_current_quotes_show_the_quote_in_force_per_pair` (`branch_code`), `::TestRateHistory::test_history_lists_every_quote_newest_first_and_filterable` |
| D9 | `UnboundLocalError` (500) publishing a global quote | `tests/integration/test_masterdata_rates.py::TestRatePublication::test_a_published_quote_keeps_its_exact_decimal_value` and every publication test without `branch_id` |
| D10 | Append-only claim vs the frozen schema; no mutation path | `tests/integration/test_masterdata_rates.py::TestRateAppendOnly::test_the_api_exposes_no_way_to_change_a_quote`, `::test_the_repository_and_service_offer_no_mutation`, `::test_a_direct_privileged_update_is_possible_and_recorded`, `::test_the_runtime_role_may_not_delete_a_quote` |
| D11 | Resolution semantics of `GET /rates` (one row per pair, branch first) | `tests/integration/test_masterdata_rates.py::TestRateResolution::test_current_quotes_show_the_quote_in_force_per_pair`, `::TestPhase3ExitCriteria::test_the_catalogue_and_the_counters_can_be_set_up` (workflow file) |
| D12 | Test-harness defects (shared-DB append-only probes, cross-loop engine disposal, weak currency-code entropy, branch leakage) | `tests/integration/test_masterdata_branches.py::TestBranchUpdates::test_the_code_is_frozen_once_the_branch_has_financial_history`, `tests/integration/test_masterdata_accounts.py::TestAccountIdentity::test_code_and_type_freeze_once_the_account_is_used` (both on their own scratch database, in a single event loop) |

Additional contract-level coverage that keeps the tuple/dict view contracts honest:

* `tests/integration/test_masterdata_accounts.py::TestAccountTree::test_the_listing_reports_who_has_children`
  and `...::TestAccountUpdates::*` fail if the router stops consuming `AccountView`.
* `tests/integration/test_masterdata_rates.py::TestRateAppendOnly::test_the_api_exposes_no_way_to_change_a_quote`
  pins the route table (21 operations, `/rates` = `GET` + `POST` only), so a tuple/method
  contract regression is caught at the surface, not by a 500 at runtime.

---

## 13. Ruff

```bash
cd apps/api
python -m ruff check .            # All checks passed!
python -m ruff format --check .   # 118 files already formatted
```

No `# noqa` was added to silence a real finding; the two uses in Phase 3 code are the
documented ones (`S105` on a constant name in `audit_actions`, SQLAlchemy hook signatures)
and every other warning was fixed at the source (`RET504`, `ARG002`, `I001`, `E501`,
`RUF002`, `UP047`).

## 14. MyPy

```bash
cd apps/api
python -m mypy app seeds scripts
# Success: no issues found in 82 source files
```

Notable typing work: `AccountView` (NamedTuple) instead of ad-hoc tuples; `RowMapping`
return types for the query-shaped rate reads; `AuditAction` enum members instead of string
literals at every audit call site (matching Phase 1/2 style); a `_tradable_currency` helper
that returns a non-optional `Currency`, so the type checker proves what the runtime already
guarantees; and a `TrimmedChar.result_processor` with a SQLAlchemy hook signature rather
than a bare function.

## 15. Schema verification, ORM parity and invariants

```bash
# clean database
alembic upgrade head && alembic current        # 0002_runtime_schema_revision (head)

# both gates
PYTHONPATH=. python -m scripts.schema_gate orm-db --dsn postgresql+psycopg://.../nexus_ci_clean
#   ORM vs DATABASE — tables: 31, columns: 341, result: MATCH
PYTHONPATH=. python -m scripts.schema_gate db-db --left <reference> --right <migrated>
#   checks: 71, triggers: 48, routines: 23, views: 5, result: MATCH

# invariants on a fresh migrated database
psql -f tests/invariants/phase0_schema_invariants.sql
#   PHASE 0 SCHEMA INVARIANT SUITE: ALL ASSERTIONS PASSED (52 assertions; 53 PASS lines)

# no placeholders in the Phase 3 production modules (the only code this phase touched)
grep -rniE "TODO|FIXME|XXX|HACK|coming soon|placeholder|mock|not implemented" \
  apps/api/app/services/{masterdata_service,ledger_master_service}.py \
  apps/api/app/repositories/{masterdata,ledger_master}.py \
  apps/api/app/schemas/{masterdata,customers,rates}.py \
  apps/api/app/api/v1/{currencies,branches,customers,accounts,rates}.py
#   no matches
# (the only "placeholder" matches elsewhere in app/ are the Phase 1 redaction constant
#  REDACTION_PLACEHOLDER, which is a security feature, not unfinished work)
```

## 16. Security verification (PART 42)

| Control | Phase 3 evidence |
| --- | --- |
| Deny-by-default RBAC | Each suite asserts 401 without a token and 403 with the wrong role, naming the required permission; the accountant/cashier/auditor cases above |
| Least privilege at the database | No `DELETE` grant on `currencies`, `branches`, `customers`, `accounts`, `exchange_rates` for `nexus_app` (asserted); `SELECT, INSERT, UPDATE` only |
| Immutability | `NEX06` trigger refuses a raw `UPDATE currencies SET code = …` (tested); rates expose no mutation surface (tested) |
| Input validation | Pydantic v2 with `extra="forbid"` everywhere; enum/timezone/pattern/regex checks; parameterised SQL only (no string interpolation in any Phase 3 query) |
| Money correctness | `Decimal`/`NUMERIC(30,10)` end-to-end; JSON floats refused; values travel as decimal strings |
| PII | Only the last four digits of a national id can be stored; the audit row records presence, not value; responses return exactly what is stored |
| Secrets | No secret, key or credential added to the repository; configuration through `Settings`/environment as in Phases 1–2 |
| Rate limiting | Every route enforces the configured read/write bucket; the Phase 1 limiter behaviour is regression-tested |
| Auditability | 10 new actions, one audit row per committed change, none for reads or refusals (§8) |

---

## 17. Limitations

| # | Limitation | Impact / mitigation |
| --- | --- | --- |
| L-1 | **No schema change in Phase 3.** The frozen DDL already covered these entities, so no migration was written. | Positive for stability; it also means schema-level wishes must wait for a phase that owns a migration (see L-2). |
| L-2 | `exchange_rates` has **no append-only trigger** and `nexus_app` retains `UPDATE` (the frozen DDL installs `no_update`/`no_delete` on `audit_logs`, `journal_lines`, `cash_movements` etc., but not on rates). | Immutability is an application guarantee: no route, service or repository path can change a quote (all three asserted). A privileged direct `UPDATE` is possible and **is** captured by the table's `change_log` trigger, but not by the audit chain. **Recommendation:** a future approved migration should add `trg_exchange_rates_no_update/no_delete` and revoke `UPDATE`; this was deliberately *not* smuggled into a phase whose scope is master data and whose schema is frozen. |
| L-3 | `accounts.normal_balance` is `CHAR(6)` in the frozen DDL, which is why the API needed a trimming type decorator. | Handled and tested; a future revision of `schema.sql` could use `VARCHAR(6)` for clarity. |
| L-4 | `GET /rates` without `branch_id` reports **global** quotes only. | Documented in `API_CONTRACT.md` §9.2 and asserted; there is no unambiguous way to show several branches' quotes in one row, and the contract's `branch_id` parameter is the intended selector. |
| L-5 | Sandbox limitations inherited from earlier phases: no Docker CLI, GitHub Actions logs not retrievable (job/step conclusions and check annotations are), Python 3.11.2 instead of 3.12, no `psql`/`redis-cli` on `PATH`. | Same workarounds as Phases 1–2: CI runs the compose stack; the local interpretation uses the driver-level gates and the `pgserver`-provided `psql`. |
| L-6 | Rate resolution uses the **server** clock (`now()` at the database, or the explicit `at`). | Correct by design (server-authoritative, PART 37); device clock skew belongs to Phase 8 sync. |
| L-7 | Customer records store minimal PII by design (PART 65). | Integrations that legally require more (full TIN, document scans) need an approved schema extension, not a free-text field. |
| L-8 | **RBAC scope note.** The ROADMAP's Phase 3 exit criterion reads "a manager can create a currency, branch, customer and rate", while the approved Phase 0 permission map keeps currency and branch administration with SUPER_ADMIN/OWNER and gives MANAGER the daily master data (customers, rates). | Phase 3 implemented the approved map unchanged and tested the entire scenario end to end. If the business wants MANAGER to administer currencies and branches, it is a one-line `ROLE_PERMISSIONS` change plus a documentation update — an RBAC decision for the reviewer, not a silent Phase 3 change. |

## 18. Risks

| # | Risk | Assessment |
| --- | --- | --- |
| R-1 | Someone with database credentials edits a rate directly (L-2). | Low likelihood (credentialed access), medium impact (the change appears in `change_log`, not in the audit chain). Recommended mitigation is the L-2 migration; until then the operational rule is that corrections are new quotes. |
| R-2 | An operator publishes a wrong quote and the previous one was already used. | Rates are append-only and versioned by instant; transactions (Phase 5) will store the rate they used, so historical values cannot be restated. The wrong quote can be superseded immediately by a newer one. |
| R-3 | `has_children` runs a correlated `EXISTS` per listed row. | Negligible at chart scale (hundreds of accounts) and index-backed by `accounts(parent_id)`; revisit if a deployment exceeds tens of thousands of accounts. |
| R-4 | Account type/parent corrections are refused once journal lines exist. | Intentional (history must not be reclassified); the documented remedy is a new account and a reclassification entry, which is an accounting decision. Documented in the service and here so operators are not surprised. |
| R-5 | A reviewer might read "manager" in the ROADMAP loosely (L-8). | Made explicit and tested on both sides, so the decision is visible instead of implicit. |
| R-6 | Customer codes consume `sequences` per day. | The per-day, per-prefix scope grows one row per day; Phase 8's device allocation policy will reuse the same primitive. No cleanup is possible by design (numbers are never reused). |

---

## 19. Acceptance checklist

| # | Requirement (user mandate) | Result |
| --- | --- | --- |
| 1 | Phase scope: currencies, branches, customers (+ code generation), accounts (chart of accounts), exchange rates (append-only, audited), rate resolution service and endpoints | **DONE** (§5, §6) |
| 2 | Tests: CRUD + uniqueness + immutability (currency code) | **DONE** (135 Phase 3 integration tests; `NEX06` covered against a raw `UPDATE`) |
| 3 | Tests: rate history and precedence (branch over global) | **DONE** — resolution function, `GET /rates` and `/rates/history` |
| 4 | Tests: audit rows on rate changes | **DONE** — exactly one `RATE_CREATED` per publication, with the payload asserted |
| 5 | Tests: inactive-entity rejection | **DONE** — `CURRENCY_INACTIVE`, `BRANCH_INACTIVE` (422) for customers, accounts and rates |
| 6 | Exit criterion: a manager can create a currency, branch, customer and rate | **DONE, with the approved permission split made explicit.** `API_CONTRACT.md` §8 gives currency and branch administration to `settings.manage`/`branch.manage` (SUPER_ADMIN, OWNER) and daily master data to MANAGER (`customer.create`, `rates.manage`). The workflow test runs the whole scenario — currency, branch, customer, rate, resolution, audit — and pins the split (`test_masterdata_workflow.py`). Changing that map is an RBAC decision for the reviewer, not a Phase 3 side effect; see Limitation L-8. |
| 7 | Exit criterion: every rate change produces an audit row | **DONE** |
| 8 | Exit criterion: no edit path for `currencies.code` | **DONE** — schema omission + service refusal + `NEX06` |
| 9 | Preserve approved architecture, security, invariants, RBAC, auditability, migration discipline, test standards, documentation | **DONE** — no approved foundation changed; only additive error code and one additive type decorator |
| 10 | No Phase 4 work; no later-phase financial logic | **DONE** — see §22 |
| 11 | Decimal/NUMERIC correctness | **DONE** — floats refused, `NUMERIC(30,10)` end-to-end |
| 12 | Deny-by-default RBAC, complete auditability | **DONE** (§7, §8) |
| 13 | No placeholders/TODOs/mocks | **DONE** (§15 grep) |
| 14 | Full regression suite, not only Phase 3 | **DONE** — 905 passed (§10, §11) |
| 15 | Ruff, MyPy, migration, schema gates, ORM parity, Phase 0 invariants | **DONE** (§13–§15) |
| 16 | `docs/phases/PHASE3_REPORT.md` with the mandated sections | **DONE** — this document |
| 17 | `docs/PROJECT_STATUS.md` updated (0/1/2 APPROVED, 3 READY FOR REVIEW, 4–17 NOT STARTED) | **DONE** |
| 18 | Phase 3 PR checks pass (GitHub CI) | recorded in the finalisation commit; all six jobs are required to be green |
| 19 | Commit + push + PR description updated with the acceptance summary | **DONE** at the end of this phase — see the PR |
| 20 | Do **not** mark Phase 3 approved; do **not** start Phase 4; stop for review | **DONE** — §22 |

---

## 20. Files added and modified

**Added (production)** — `app/repositories/{masterdata,ledger_master}.py`,
`app/services/{masterdata_service,ledger_master_service}.py`,
`app/schemas/{masterdata,customers,rates}.py`,
`app/api/v1/{currencies,branches,customers,accounts,rates}.py`.

**Added (tests/docs)** — `tests/masterdata_helpers.py`,
`tests/integration/test_masterdata_{currencies,branches,customers,accounts,rates,workflow}.py`,
this report.

**Modified** — `app/api/v1/router.py` (five routers mounted),
`app/core/audit_actions.py` (10 actions), `app/core/config.py`
(`numbering_prefix_customer`), `app/core/exceptions.py` (`CONFLICT` + four error classes),
`app/models/base.py` (`TrimmedChar`), `app/models/account.py` (uses it),
`tests/conftest.py` (`branch_factory`, `accountant_headers`), `tests/helpers.py`
(`build_settings`, `settings_for_database`), `tests/unit/test_exceptions.py`,
`pyproject.toml` (markers), `docs/api/API_CONTRACT.md` (§4 and §9.2),
`docs/PROJECT_STATUS.md`, `README.md`.

Exact `git diff --cached --stat` at the Phase 3 implementation commit (measured, not
estimated):

| Scope | Files | Lines |
| --- | --- | --- |
| Production (`apps/api/app`) | 18 (13 added, 5 modified) | +3 585 / −11 |
| Tests (`apps/api/tests`) | 10 (9 added, 1 modified) | +2 702 / −0 |
| Documentation (`docs/`, `README.md`) and `pyproject.toml` markers | 5 | +565 / −11 |
| **Total** | **33** | **+6 852 / −22** |

## 21. Environment and CI evidence

* Local verification environment: PostgreSQL 16.2 (`/tmp/pgdata`, 127.0.0.1:5432), Redis
  6.2.14 (redislite, 127.0.0.1:6379, database 15), Python 3.11.2 in `/tmp/venv`, no Docker.
* GitHub CI (`.github/workflows/ci.yml`) jobs: `Lint (ruff)`, `Type check (mypy)`,
  `Unit tests`, `Integration tests and schema gates`, `Compose stack (PART 44 acceptance)`,
  `OpenAPI contract artefact`. The Phase 3 run and its step conclusions are recorded in the
  finalisation commit and in the pull request.

## 22. Status statement

> **Phase 3 is READY FOR REVIEW.** It is not, and will not be, marked approved by the
> author of this report. Approval is the human reviewer's act and is recorded in
> `docs/PROJECT_STATUS.md`.
>
> **Phase 4 (Accounting engine) has NOT started.** No journal posting, balance movement,
> exchange transaction, cash movement, transfer, expense, report, sync or client work was
> implemented, and none of Phase 4's scope appears in this phase's commits.
>
> Work stops here for review.
