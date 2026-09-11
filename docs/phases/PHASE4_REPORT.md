# PHASE 4 REPORT — Accounting engine (double-entry core)

| Field | Value |
| --- | --- |
| Document ID | `PHASE4-REPORT-001` |
| Phase | 4 — Accounting engine / double entry (roadmap: `docs/architecture/ROADMAP.md`) |
| Status | **READY FOR REVIEW** (only the human reviewer may mark it APPROVED) |
| Starting commit | `3a985fd` (Phase 3 finalisation, top of branch before this phase) |
| Implementation commit | *pinned in the finalisation commit of this phase* (visible in `git log`) |
| Branch | `arena/01a090c5-nexus-exchange-erp` |
| Predecessor | [`PHASE3_REPORT.md`](PHASE3_REPORT.md) (master data) |
| Successor | Phase 5 (exchange) — **not started, not authorised** |

> **خلاصه فارسی** — فاز ۴ موتور حسابداری دوطرفه را می‌سازد: تنها نویسندهٔ دفتر کل، اعتبارسنجی تراز، ثبت معاملات ارزی/نقدی/هزینه، ابطال (بدون حذف)، قفل‌گذاری هم‌روندی، idempotency، دامنهٔ شعبه و گزارش تراز آزمایشی — همه با `Decimal`/`NUMERIC` و محافظت سطح پایگاه‌داده.

---

## 1. Phase name and scope

Phase 4 builds the accounting core every later money phase depends on. In scope, exactly as the roadmap lists it:

* `AccountingService` — `create_journal_entry`, `validate_balanced_entry`, `post_exchange`, `post_cash_movement`, `post_expense`, `reverse_transaction` (plus `reverse_journal_entry` and the read side);
* period-agnostic posting (an accounting date, never a period lock — §37 L-4);
* the reversal generator;
* balance and trial-balance queries (`get_account_balance`, `get_trial_balance`) over the immutable table, not the cache;
* a repository layer for the ledger (`repositories/ledger.py`, `repositories/idempotency.py`);
* the journal read API (`GET /journal`, `GET /journal/{id}`) and the trial-balance report (`GET /reports/trial-balance`);
* the accounting test suites and the review of the rate snapshot (§13).

Out of scope and deliberately untouched: the exchange/cash/expense/transfer **routes** (Phases 5, 6, 10), period closing and FX revaluation (§37), and the mobile client.

## 2. Authorisation and constraints under which this phase ran

The phase was executed under the master prompt's rules for a critical financial-integrity phase:

* preserve every approved Phase 0–3 architecture, invariant, security control, migration-discipline rule and testing standard;
* `Decimal`/`NUMERIC` only, never float (PARTs 20, 62);
* only `AccountingService` writes the ledger (PART 46); no business logic in routes (PART 47);
* corrections by reversal, never by mutation (PART 22); history is never deleted (PARTs 18, 25);
* explicitly review whether the rate snapshot/reference needs stronger **database-level** protection, and if a schema change were required, document the reason, write a migration, update the schema reference and add regression tests;
* no placeholders, no TODO/FIXME, no fake accounting logic, no shortcuts around financial invariants;
* run the complete Phase 0–3 regression;
* do not mark the phase APPROVED; do not start Phase 5.

The rate-snapshot review is answered in §13: **no schema change**, with the empirical evidence in §25.

## 3. Starting commit

`3a985fd` — *docs(phase3): pin the implementation commit and record the green CI runs*. At that commit the repository had 905 passing tests, the frozen schema (`docs/database/schema.sql`), the frozen reference DDL checksum, and head revision `0002_runtime_schema_revision`.

## 4. Final commit

The implementation, tests and documentation of this phase are committed as one commit
`feat(accounting): complete phase 4 double-entry engine`; its exact hash is pinned by the
documentation-only finalisation commit that follows it (the same pattern Phase 2 and
Phase 3 used) and is visible at the top of `git log`. The finalisation commit also records
the CI runs of the implementation commit.

## 5. Files created and modified

Production (`app/`, `seeds/`, `scripts/`, `pyproject.toml`): 16 files, **+3 814 / −27** lines.

| File | State | Lines | What it contains |
| --- | --- | --- | --- |
| `apps/api/app/services/accounting_service.py` | new | 2 453 | The single ledger writer: validation, posting, reversal, balance and trial balance, postings per document type, audit, idempotency, scope, locking |
| `apps/api/app/repositories/ledger.py` | new | 461 | Every SQL statement the ledger needs: entry/line inserts, `lock_accounts`, `account_totals`, signed `position()`, `trial_balance`, `ledger_totals`, `balance_cache()` |
| `apps/api/app/core/idempotency.py` | new | 243 | `IdempotencyGuard`, `IdempotencyRequest`, `canonical_request_hash`, `json_safe`, the `ENDPOINT_*` constants |
| `apps/api/app/schemas/journal.py` | new | 133 | `JournalLineView`, `JournalEntryView`, list and trial-balance response models |
| `apps/api/app/api/v1/journal.py` | new | 127 | `GET /journal`, `GET /journal/{id}` |
| `apps/api/app/api/v1/reports.py` | new | 84 | `GET /reports/trial-balance` |
| `apps/api/app/repositories/idempotency.py` | new | 98 | The repository half of the replay guard (self-contained `IdempotencyScope`) |
| `apps/api/app/core/exceptions.py` | modified | +77 | Four Phase 4 domain errors and the shared `sqlstate_of` / `constraint_name_of` / `error_for_sqlstate` helpers |
| `apps/api/app/core/money.py` | modified | +98/−6 | `money_context()`, `quantize_money`, `has_money_scale`, `money_sum`, `money_difference`, `multiply_money`, `divide_money`, `assert_within_money_bounds`, `format_decimal` |
| `apps/api/app/core/permissions.py` | modified | +13 | `GROUP_WIDE_ROLES` and the Phase 4 posting/read permission constants |
| `apps/api/app/core/audit_actions.py` | modified | +11 | `JOURNAL_POSTED`, `JOURNAL_REVERSED`, `LEDGER_POSTING_DENIED` |
| `apps/api/app/core/error_handlers.py` | modified | +5/−21 | Uses the shared SQLSTATE helpers instead of private copies |
| `apps/api/app/services/audit_service.py` | modified | +5 | `ActorContext.branch_id` |
| `apps/api/app/api/deps.py` | modified | +1 | `PrincipalContext.branch_id` |
| `apps/api/app/api/v1/router.py` | modified | +4 | Mounts the two new routers |
| `apps/api/pyproject.toml` | modified | +1 | The `accounting` marker |

Recounted against the Phase 3 baseline `3a985fd` with `git diff HEAD --numstat` on the committed tree: **33 files, +12 033 / −61**, of which 16 production/tooling files (+3 814/−27), 11 test files (+7 333/−17) and 6 documentation files (+886/−17).

Tests: 11 files, **+7 333 / −17** lines (nine new suites plus two harness patches, D4-10 and D4-11).

| File | Tests | Focus |
| --- | --- | --- |
| `apps/api/tests/unit/test_accounting_rules.py` | 57 | Money/Decimal rules, entry validation, fingerprints, no database |
| `apps/api/tests/integration/test_accounting_posting.py` | 60 | Balanced posting, rejections, multi-line, dates, references, authorisation |
| `apps/api/tests/integration/test_accounting_multicurrency.py` | 31 | Functional-currency conversion, `foreign_amount`, positions, carrying rate, FX result |
| `apps/api/tests/integration/test_accounting_immutability_reversal.py` | 29 | Immutability at the database, reversal lifecycle and linkage |
| `apps/api/tests/integration/test_accounting_idempotency.py` | 15 | Replay, mismatch, in-progress, failed reclaim, per-user keys |
| `apps/api/tests/integration/test_accounting_concurrency.py` | 11 | Racing posts, disposals, reversals, duplicate posting, rollback |
| `apps/api/tests/integration/test_accounting_scope.py` | 16 | Branch scoping for posting and reading, group-wide reads, 404-not-403 |
| `apps/api/tests/integration/test_accounting_integrity.py` | 24 | Database-level invariants on a used ledger, reconciliation identities, rate snapshot |
| `apps/api/tests/accounting_helpers.py` | — | The shared harness (`World`, scenarios, races, raw readers) |

Documentation: six files — `docs/phases/PHASE4_REPORT.md` (this file, new), `docs/PROJECT_STATUS.md` and `README.md` (phase status and counts), `docs/architecture/ACCOUNTING_MODEL.md` (§8, §13, §14), `docs/database/SCHEMA.md` (deviation D-22, drift-gate item 2) and `docs/api/API_CONTRACT.md` (error codes, implemented routes).

## 6. Architecture of the accounting engine

```
HTTP route (no business logic — PART 47)
  api/v1/journal.py · api/v1/reports.py
        │  principal, rate limiter, request id
        ▼
AccountingService            ← the only writer (PART 46)
  validate_balanced_entry       pure Decimal arithmetic, no I/O
  _ledger_transaction           one transaction; translates known SQLSTATEs
  _lock_accounts                ordered row locks, before any read they protect
  post_exchange / post_cash_movement / post_expense / create_journal_entry
  reverse_journal_entry / reverse_transaction
  get_journal_entry / list_journal_entries / get_account_balance / get_trial_balance
        │
        ▼
Repositories (SQL, no business rules)
  repositories/ledger.py        entry/line inserts, POSITION, trial balance, cache
  repositories/idempotency.py   claim/complete/replay
        │
        ▼
PostgreSQL 16 (frozen schema)
  deferred ct_journal_lines_balanced_* (NEX02) · ct_cash_movements_non_negative (NEX01)
  append-only triggers (P0001) · NEX06 frozen posting fields
  views v_trial_balance / v_account_balances / v_cash_position / v_currency_position
```

Layering is unchanged from the approved Phase 1 architecture: routes depend on services, services on repositories, repositories on SQLAlchemy Core/ORM. No route contains an accounting rule; no repository contains a business decision.

## 7. Accounting model implemented

The service implements the approved `docs/architecture/ACCOUNTING_MODEL.md` posting rules exactly, and the suites assert the model's worked examples line by line:

| Document | Posting implemented | Reference type |
| --- | --- | --- |
| Opening balance | cash (or counter) account debited, capital/source account credited, rate 1 in the functional currency | `OPENING_BALANCE` |
| Exchange BUY | acquired currency enters at the transaction price; the functional counter-leg leaves at its **carrying rate**; commission is revenue; the difference is the realized FX result | `EXCHANGE_TRANSACTION` |
| Exchange SELL | disposed currency leaves at its carrying rate (read from the ledger, never a cache), counter-leg enters at the transaction price | `EXCHANGE_TRANSACTION` |
| Cash in/out | cash account against the named counter-account, one currency, rate 1 | `CASH_MOVEMENT` |
| Expense | expense account debited, cash account credited | `EXPENSE` |
| Adjustment | signed movement (`adjustment_sign`) against short/over or the counter account | `CASH_MOVEMENT` |
| Reversal | mirror of the original: same accounts, currencies and rates, debit ↔ credit | `REVERSAL` |
| Manual adjustment | arbitrary balanced entry by a holder of `accounts.manage` | `MANUAL_ADJUSTMENT` |
| Transfer (service ready, route in Phase 10) | collection/payout entries | `TRANSFER` |

Line semantics are the frozen schema's, not the service's invention: `foreign_amount` is a `GENERATED ALWAYS` column equal to `(debit + credit) / exchange_rate`, so the quantity on a line can never disagree with the money and the rate that produced it.

## 8. Journal lifecycle

1. **Draft** — exists only inside a service call: a `_PostingPlan` (reference type/id, branch, lines, fingerprint, date, device). Nothing is persisted, so a refused plan leaves no trace to clean up.
2. **Posted** — one `INSERT` into `journal_entries` and one per line, then a single `COMMIT`. The deferred `ct_journal_lines_balanced_*` constraint is what makes "posted" mean "balanced": an unbalanced entry cannot become visible even if the service is bypassed.
3. **Reversed** — a *new* entry with `reference_type = 'REVERSAL'`, `reversal_of_id` pointing at the original, mirrored amounts and its own audit row. The original keeps every column it had.
4. **Never edited, never deleted** — `NEX06` refuses any change to a posting field, `P0001 NEXUS_APPEND_ONLY` refuses line updates/deletes, and the runtime role has no `DELETE` on `journal_entries`/`journal_lines` at all.

There is no "cancelled journal" state, and no ledger status column to change: a journal is posted or reversed, which is the only distinction the books need.

## 9. Posting lifecycle (one transaction)

```
1  authorise                     permission for the reference type → PERMISSION_DENIED (403, audited)
2  assert scope                  actor's branch or group-wide     → FORBIDDEN_SCOPE  (403, audited)
3  lock accounts                 SELECT … FOR UPDATE, ordered by account id, then currency
4  resolve context               base currency, stored position, carrying rate
5  validate money                Decimal, exact scale, within NUMERIC(30,10) bounds
6  validate the entry            ≥2 lines, single-sided, Σdebit = Σcredit (exact)
7  claim idempotency             if the endpoint requires a key
8  insert entry + lines          lines sorted the way the locks were taken
9  audit                          one JOURNAL_POSTED row: numbers + line detail + rate snapshot
10 COMMIT                        NEX02 (balance) and NEX01 (cash) are evaluated here
```

Order is load-bearing, and each step has a test that fails if it moves:

* **Locks before reads (step 3 < step 4).** `_lock_accounts` runs before `position()` and the carrying-rate read. Taking a per-account lock *after* the read it protects is a race, not a lock: two concurrent disposals both read the pre-transaction position and both post. `test_accounting_concurrency.py` reproduces exactly that.
* **One lock order (step 3 == step 8).** `lock_accounts` orders by `(account_id, currency_id)` and the lines are inserted in the same order, so two postings sharing accounts queue instead of deadlocking.
* **Validation before writing (steps 5–6 < step 8).** The database constraints are a *backstop*, never the validator: a refused entry must produce a documented 422 with a field name, not a driver error.
* **Commit-time constraints (step 10).** `NEX01`/`NEX02` are deferred on purpose: an entry and its reversal may pass through an unbalanced state inside one transaction, but never become visible in one.

A caller may bring its own session (`session=…`); then the *caller* owns the boundary and the ledger joins its transaction. That is how a later phase writes a business document, its journal entry, its cash movement and its audit rows atomically (PART 20) without the ledger deciding when the work becomes visible.

## 10. Reversal lifecycle

* `reverse_journal_entry(journal_entry_id, reason, …)` — reverses one posted entry.
* `reverse_transaction(reference_type, reference_id, reason, …)` — resolves a business document to its journal entry and reverses it, refusing when there is no journal (`RESOURCE_NOT_FOUND`) or when it is already reversed.
* Everything else about the reversal is enforced, not merely implemented: the mirror must be line-for-line (accounts, currencies, rates; debit ↔ credit), the amount must match, the date may not precede the original, `reversal_of_id` may not point at itself, and exactly one reversal per original is possible (`ux_journal_entries_reversed_once`). A reversal may not be reversed.
* The reversal is audited as `JOURNAL_REVERSED` with the reason and the link, and the original stays visible in reads with its reversal chain (`test_the_journal_api_shows_the_reversal_chain`).

## 11. Database changes and migrations

**None.** Phase 4 adds no table, column, constraint, index, trigger, function or view, and no Alembic revision: head stays `0002_runtime_schema_revision` and the frozen reference file `docs/database/schema.sql` keeps its checksum (`EXPECTED_SHA256` in `alembic/versions/0001_initial_schema.py`, verified by `tests/integration/test_migration.py`).

That is a deliberate outcome of §13, not an oversight: every requirement of this phase — balanced posting, immutability, reversal linkage, append-only history, per-branch scoping, money type policy — was already expressible with the approved schema. Writing a revision for the sake of a revision would have invalidated a frozen artefact for no gain.

## 12. Constraints, triggers and functions the engine relies on

| Mechanism | Name | Behaviour the engine depends on |
| --- | --- | --- |
| Deferred balance assertion | `ct_journal_lines_balanced_insert/update/delete` → `nexus_assert_journal_balanced()` | Σdebit = Σcredit exactly, ≥ 2 lines, single-sided, non-zero, at COMMIT (SQLSTATE `NEX02`) |
| Non-negative cash | `ct_cash_movements_non_negative` → `nexus_assert_non_negative_cash()` | a drawer may never go negative (SQLSTATE `NEX01`), evaluated at COMMIT |
| Append-only | `nexus_forbid_mutation()` on `journal_lines`, `cash_movements`, `audit_logs`, … | any `UPDATE`/`DELETE` raises `P0001 NEXUS_APPEND_ONLY` |
| Frozen posting fields | `nexus_journal_entries_immutable()` | changing reference/date/author/branch/reversal link raises `NEX06 NEXUS_IMMUTABLE_FIELD`; the narrative may be annotated |
| One entry per document | `ux_journal_entries_one_per_reference` | a retried document cannot post twice |
| One reversal per original | `ux_journal_entries_reversed_once` | a second reversal is impossible even under concurrency |
| Derived quantity | `journal_lines.foreign_amount` (`GENERATED ALWAYS`) | the foreign quantity always follows `(debit+credit)/exchange_rate` |
| Balance cache | `trg_journal_lines_balance_cache` + `rebuild_account_balances()` | O(1) balance reads, deterministically rebuildable, never authoritative |
| Views | `v_trial_balance`, `v_account_balances`, `v_cash_position`, `v_currency_position` | reports read history, not a cache |
| Audit chain | `nexus_audit_chain()` + `verify_audit_chain()` | the audit trail is tamper-evident |
| Least privilege | `REVOKE UPDATE/DELETE … FROM nexus_app` | even a compromised application role cannot rewrite history |

## 13. Rate snapshot — the explicit review (decision D-4-1)

**Question (from the phase brief).** Does accounting posting require stronger
*database-level* protection of the rate snapshot/reference used by posted journals, given
that Phase 3 documented append-only protection for exchange rates at the application
level only?

**Finding.** The posted rate is not a reference at all. `journal_lines.exchange_rate` is a
plain `NUMERIC(30,10)` column on an append-only table, holding the number that priced the
entry; `foreign_amount` is generated from it. **No table in the schema references
`exchange_rates`** — `journal_lines` has no rate id, no foreign key and no dependency of
any kind. The quote's *identity* is recorded where provenance belongs: the audit row's
`rate_snapshot` (`rate`, `exchange_rate_id`, `from_currency_id`, `to_currency_id`,
`branch_id`, `effective_at`, `source`) plus the per-line `exchange_rate` in `line_detail`,
in a hash-chained append-only table.

**Decision: no schema change.**

| Option | Consequence |
| --- | --- |
| Keep the copied number (chosen) | History is immutable by construction: `P0001`/`NEX06` plus `REVOKE UPDATE, DELETE` protect the line; nothing external can re-price it; a quote stays replaceable (it is a market observation, not money) |
| Add `journal_lines.exchange_rate_id` FK | The quote row gains veto power over financial history (it could no longer be superseded or pruned without touching the ledger), and the schema would imply the referenced row is authoritative for an amount that is in fact frozen in the line. It would also change a frozen artefact for no gain |

**Evidence (§25).** `TestTheRateSnapshotIsProtected` re-prices *every* quote for the pair a
posted entry used (`UPDATE exchange_rates … buy_rate = 99`) and asserts the posted lines are
byte-identical afterwards; it asserts `UPDATE`/`DELETE` on a posted line is refused with
`P0001 NEXUS_APPEND_ONLY`; and it asserts the audit row pins both the rate and the quote id
(`exchange_rate_id`), matching the immutable lines exactly.

**Residual risk (documented, not hidden).** When a caller posts a rate without naming the
quote it came from, `rate_snapshot.exchange_rate_id` is `NULL`: the *number* is always
recorded, the *provenance* only when the caller supplies it. Phase 5's exchange route
resolves the quote through the Phase 3 rate service and passes its id — the parameter,
its validation and its audit rendering exist and are tested today.

## 14. API contracts

| Method | Path | Permission | Behaviour |
| --- | --- | --- | --- |
| `GET` | `/api/v1/journal` | `reports.view` | Newest-first entry list; filters `reference_type`, `reference_id`, `from`, `to`, `branch_id`; `limit` (1–200, default 50) / `offset`; standard list envelope with `total` |
| `GET` | `/api/v1/journal/{id}` | `reports.view` | Entry with its lines, totals and reversal linkage; out of scope → `404 RESOURCE_NOT_FOUND` |
| `GET` | `/api/v1/reports/trial-balance` | `reports.view` | Flat `{source, generated_at, filters, total_debit, total_credit, difference, is_balanced, rows[]}`, each row with `net_debit` for the account's normal-balance direction; out-of-scope `branch_id` → `403 FORBIDDEN_SCOPE` |

Every response names its `source` (`journal_lines` for all three), so a reader can always
tell which immutable table produced a number. The contract document
(`docs/api/API_CONTRACT.md` §9) marks these three as implemented in Phase 4 and adds the
one error code the phase introduced (`IDEMPOTENCY_KEY_REQUIRED`, 400) to the catalogue.

Posting itself is not exposed as a route in this phase: the routes for `/exchange`,
`/cash`, `/expenses` and `/transfers` belong to Phases 5, 6 and 10, and they call the
service surface implemented here. There is no "post anything" endpoint — the ledger is
written by services that know what the money means.

## 15. Authorisation and RBAC

Authority is per document type (`POSTING_AUTHORITY`), and reading is separate from writing:

| Operation | Permission |
| --- | --- |
| `EXCHANGE_TRANSACTION` | `exchange.create` |
| `CASH_MOVEMENT` | `cash.create` |
| `EXPENSE` | `expenses.create` |
| `TRANSFER` | `transfers.create` |
| `MANUAL_ADJUSTMENT`, `OPENING_BALANCE` | `accounts.manage` |
| Reverse | the authority of the reversed document |
| `GET /journal*`, `GET /reports/trial-balance` | `reports.view` |

Consequences that are asserted, not assumed:

* a `CASHIER` or `MANAGER` holds `cash.create` but **not** `accounts.manage`, so they can
  record cash but cannot invent a manual adjustment;
* an `ACCOUNTANT` holds both, which is the approved split;
* a missing permission is `403 PERMISSION_DENIED`; a *forged* actor (an entry that names a
  user who does not exist, or a caller with no user) is refused before anything is written;
* every refusal is audited as `LEDGER_POSTING_DENIED` with the required permission, the
  actor's roles and the branch context, written in its own transaction so it survives the
  rollback of the request that caused it.

## 16. Branch scoping and isolation

`GROUP_WIDE_ROLES = {SUPER_ADMIN, OWNER}` see and post everywhere; every other actor is
bound to `actor.branch_id` (the device's branch), and an actor bound to no branch sees
nothing — `None` means *restricted*, never *unrestricted*.

* Posting into another branch is `403 FORBIDDEN_SCOPE`, audited with
  `reason = ANOTHER_BRANCH`; an actor with no branch is refused with `ACTOR_HAS_NO_BRANCH`.
* Reading another branch's entry is `404`, not `403`: a manager probing ids learns nothing
  about other branches' journals.
* Filtering the list by a branch outside the scope is `403 FORBIDDEN_SCOPE` (the two mean
  different things to a client: an empty list would silently hide a misconfiguration).
* A group-wide reader sees every branch's entries, and a branch-scoped reader sees exactly
  one — both asserted, the latter with a second branch created by the test.

## 17. Auditability

Three actions are added: `JOURNAL_POSTED`, `JOURNAL_REVERSED`, `LEDGER_POSTING_DENIED`.
Each posted entry writes **exactly one** audit row (asserted, so a retry can never
duplicate the trail), carrying:

* the reference type/id, description, transaction date and branch;
* `line_detail` — account, currency, debit, credit and `exchange_rate` per line;
* `total_debit` / `total_credit` as exact 10-dp strings;
* `rate_snapshot` — where the rate came from (§13);
* the document-specific extras (`movement_type`, `commission`, `reason`, …).

Audit rows are append-only and hash-chained (`verify_audit_chain(0)` returns no broken row
on the accumulated test ledger), so the trail is not merely present but tamper-evident.
`new_data` is the evidence for a dispute: it can be compared against the immutable lines
item by item, which one of the Phase 4 tests does.

## 18. Idempotency and duplicate-posting prevention

Three independent layers, because a retry can arrive three different ways:

1. **The natural key.** `ux_journal_entries_one_per_reference` makes a second entry for the
   same document id impossible; the service turns that into `409 DUPLICATE_RESOURCE` with
   `details["journal_entry_id"]` naming the entry that already exists, so a client can
   reconcile instead of guessing.
2. **The idempotency key (PART 40).** `IdempotencyGuard.claim()` writes an `IN_PROGRESS`
   row in the same transaction as the posting; `complete()` stores the serialized response.
   Same key + same body → the stored response replays (original status, no second posting);
   same key + different body → `409 IDEMPOTENCY_KEY_REUSED`; concurrent duplicate →
   `409 IDEMPOTENCY_IN_PROGRESS` with `Retry-After`; a failed attempt is reclaimable, so a
   crash does not burn a key. The fingerprint is built from the plan's canonical fields
   (amounts quantized, rates normalized), never from Python objects, so `70` and `70.00`
   hash identically — and the key is scoped per user and per endpoint, so one user's key
   cannot replay another's response.
3. **The database.** Even with both layers bypassed, the unique index refuses the second
   row; the deferred constraints still have to be satisfied for anything to commit.

## 19. Decimal, precision and rounding

* Every monetary value in the service is a `Decimal`; a `float` is refused with
  `not_a_decimal` rather than silently converted, and a value carrying more than 10 decimal
  places (`not_exact_scale`) is refused rather than truncated.
* Arithmetic runs inside `money_context()` — a `decimal.Context` of 40 significant digits
  (30 storage digits + 10) with `ROUND_HALF_UP`, so an intermediate result cannot be rounded
  by the process-wide 28-digit default before it is stored.
* `assert_within_money_bounds` refuses anything with `copy_abs() >= MAX_MONEY`; the largest
  postable value is therefore `MAX_MONEY - MONEY_QUANTUM`
  (`99999999999999999999.9999999998`), which posts exactly, while `MAX_MONEY` and
  `MAX_MONEY × 10` are refused with `over_maximum` and write nothing. The rationale is that
  a value with no head-room left cannot be added to without overflowing `NUMERIC(30,10)`.
* Commission and FX results are quantized once, at 10 dp, and the *entry* is balanced in
  those exact terms — `test_a_rounded_commission_never_leaves_the_entry_unbalanced` posts a
  commission whose half-up rounding is non-trivial and asserts Σdebit = Σcredit to the last
  digit.
* 500 tiny values (one quantum each) post in one transaction and survive the balance cache
  without losing a digit.

## 20. Multi-currency behaviour (including the position defect)

* The functional currency is the branch's base currency (Phase 0 rule preserved). A
  foreign-currency leg is valued at `quantity × rate` (converted when the counter-leg is not
  the functional currency); the functional leg is at rate 1.
* A **disposed** currency leaves at its *carrying* rate — the functional value per unit
  actually held, read from the ledger, never from a cache — so a losing sale posts a loss
  and a winning one a gain, and an empty position is refused (`INSUFFICIENT_BALANCE` with
  the quantity guard's `NO_POSITION` / `QUANTITY_EXCEEDED` codes).
* `foreign_amount` on every line equals `(debit + credit) / exchange_rate`, asserted for
  every line of every entry in the integrity suite — the invariant that ties quantities to
  money.
* **Defect found and fixed here (D4-2, §24):** the repository's `position()` summed
  `foreign_amount` unsigned, so a disposal *added* to the quantity instead of removing from
  it. It priced the losing sale at the carrying rate of an inflated position
  (`21000 / 1700 = 12.3529411765`) and let a drawer sell currency it did not hold. The fix
  is a signed sum (`debit > 0 ? foreign_amount : -foreign_amount`); the IN/OUT round trip in
  the multicurrency suite covers it permanently.

## 21. Concurrency, locking and rollback

Eleven tests race real transactions against one database:

* two disposals of the same position → exactly one wins, the loser gets
  `INSUFFICIENT_BALANCE`, and the position never goes negative;
* a reversal racing its original → the outcome is a `DUPLICATE_RESOURCE`/`ALREADY_REVERSED`
  refusal or a clean reversal, never an unbalanced or duplicated ledger; the suite asserts
  the *ledger*, not the exception, because both outcomes are legal;
* concurrent duplicate posting of one document → one entry;
* 20 racing posts on shared accounts → no deadlock (the lock order equals the insert
  order), all posted, cache consistent;
* a failure inside a posting (a zero-value line, a negative cash movement, a forged actor)
  rolls the transaction back completely: no entry, no line, no cash movement, no audit row.

Lock ordering, not lock strength, is what makes this safe: every posting takes the same
ordered set of `FOR UPDATE` locks before it reads a position.

## 22. Immutability and historical-record protection

Four independent guarantees, each asserted at the level where it lives:

| Guarantee | Mechanism | Test |
| --- | --- | --- |
| A posted entry's money fields cannot change | `NEX06` trigger | `test_no_posting_field_of_a_posted_entry_can_change` (every frozen column, one by one) |
| A posted line cannot change or disappear | `P0001 NEXUS_APPEND_ONLY`, no `UPDATE`/`DELETE` grant for `nexus_app` | `test_a_journal_line_cannot_be_updated/deleted`, `test_the_runtime_role_has_no_delete_or_update_on_the_ledger` |
| History is read, not computed twice | the trial balance reads `journal_lines`; the cache is rebuildable and its rebuild is a no-op | `TestTheCacheNeverDriftsFromTheLedger` |
| Nothing financial is ever deleted | no delete path exists in the service or the API; the only way to undo is a reversal | `test_deleting_a_posted_entry_is_refused`, `test_the_reversal_of_a_foreign_position_returns_the_quantity` |

## 23. Database and application agreement (requirement 20)

The service and the schema must not contradict each other, so the mapping is one table, in
one place (`app/core/exceptions.py`), used by both the service (`_ledger_transaction`) and
the HTTP layer (`error_handlers`):

| SQLSTATE | Meaning | Domain error |
| --- | --- | --- |
| `NEX01` | cash position would go negative | `409 INSUFFICIENT_BALANCE` |
| `NEX02` | entry unbalanced / <2 lines / invalid line | `500 JOURNAL_UNBALANCED` (a defect, surfaced loudly) |
| `NEX03` | illegal state transition | `409 INVALID_STATUS_TRANSITION` |
| `NEX04` | reversal target invalid or unbound | `422 REVERSAL_INVALID` |
| `NEX05` | cash reconciliation incomplete | `422 CASH_RECON_INCOMPLETE` |
| `NEX06` | frozen posting field | `409 IMMUTABLE_FIELD` |
| `P0001` | append-only violation | `403 APPEND_ONLY_VIOLATION` |
| `23505`, `23P01` | unique/exclusion violation | `409 DUPLICATE_RESOURCE` |
| `23503`, `23514` | FK/check violation | `422 DATA_INTEGRITY_ERROR` |
| `40001`, `40P01` | serialization failure / deadlock (retryable) | `503 SERVICE_UNAVAILABLE` + `Retry-After` |

`_ledger_transaction` translates **only** the SQLSTATEs in this table and re-raises anything
else untouched: inventing a financial meaning for an unknown driver error would be worse
than surfacing it. The service validates before it writes, so the database's refusals are a
backstop — and the tests deliberately bypass the service (raw SQL) to prove the backstop
works, then assert that the same conditions produce the *documented* domain error when the
service is used.

## 24. Defects discovered and fixed in this phase

| # | Defect | How it was found | Fix | Pinned by |
| --- | --- | --- | --- | --- |
| D4-1 | `position()` summed `foreign_amount` **unsigned**, so a disposal added to the held quantity; the losing sale was priced at `21000 / 1700` and a drawer could sell currency it did not hold | the multicurrency IN/OUT round trip failed; the numbers revealed the sign | signed sum in `repositories/ledger.py` (`debit > 0 ? foreign_amount : -foreign_amount`) | `test_accounting_multicurrency.py` (31) + the concurrency suite (11) |
| D4-2 | Account rows were locked **after** the position read they protect, so two concurrent disposals could both post | a race test produced two successful disposals of one position | `_lock_accounts` is the first statement inside every ledger transaction, ordered `(account_id, currency_id)` — the same order the lines are inserted in | `test_accounting_concurrency.py::TestConcurrentDisposals` |
| D4-3 | The idempotency fingerprint serialized `PostingLine` objects, which `json_safe` refuses, so every keyed posting failed validation | keyed posting returned `422` with `Unsupported value in an idempotent request body` | `_fingerprint_lines` stringifies the canonical fields (amounts quantized, so `70` == `70.00`) | `test_accounting_idempotency.py` (15) |
| D4-4 | The fingerprint quantized money **before** payload validation, so a non-`Decimal` amount raised `AttributeError` instead of a validation error | `test_a_float_amount_cannot_enter_the_ledger` crashed inside the fingerprint | `_fingerprint_money` uses `repr(value)` for non-`Decimal`s and lets the validator refuse them | same suite |
| D4-5 | `accounts.normal_balance` arrives from PostgreSQL as blank-padded `CHAR(6)`, leaking `"DEBIT "` into trial-balance rows | the balance-direction assertions compared padded strings | `_functional_balance` strips and upper-cases the value (the Phase 3 `TrimmedChar` lesson applied at the query boundary) | `test_accounting_integrity.py::TestTheCacheNeverDriftsFromTheLedger` |
| D4-6 | A duplicate posting surfaced as a raw integrity error instead of a reconcilable refusal | the duplicate-posting test received a `DataIntegrityError` naming a unique index | `ux_journal_entries_one_per_reference` is translated to `409 DUPLICATE_RESOURCE` with `details["journal_entry_id"]` | `test_accounting_posting.py::TestDuplicatePosting` |
| D4-7 | The idempotency repository imported `app.core.idempotency`, which imports the repository — a circular import that only appears when the module is imported first | application import order | `IdempotencyScope` is defined in the repository, which is now self-contained | import-time failure on every run |
| D4-8 | Test-harness defect: `busy_world.ids["buy_usd"]` was resolved by "the newest `EXCHANGE_TRANSACTION`", which is the EUR entry (rate 75, not the USD entry at 70) — the rate assertions were measuring the wrong entry | the new rate-snapshot tests failed with `Decimal('75') != Decimal('70')` | the fixture now stores the ids returned by the service, never a re-query | `test_accounting_integrity.py` |
| D4-9 | Test-isolation defect: the journal date-range test relied on a five-day window over the shared branch and the default page size, so other suites' "now" postings pushed its entry off the page | the suite passed alone and failed in a full run | a window only that posting can fall into (`hours_ago=3 … hours_ago=1`) plus `assert total == len(visible)`, which fails loudly if the page is truncated again | `test_accounting_posting.py::TestAccountingDates` |
| D4-10 | **Harness defect:** the currency-code generator drew from only 26⁴ = 456,976 codes. With ~25 codes drawn per session the birthday probability is ~0.07 %, and a full-suite run eventually failed with `409 DUPLICATE_RESOURCE` on a freshly drawn code — a Phase 3 test failing for a reason that had nothing to do with Phase 3 | the first full regression run | `unique_currency_code()` now draws `"T"` + **8** letters (26⁸ ≈ 2.1 × 10¹¹); the generator's own comment records the arithmetic | `tests/integration/test_masterdata_workflow.py` + `test_masterdata_currencies.py` (31 passed) and the final full run |
| D4-11 | **Harness defect:** `TestAccountTree::test_the_listing_reports_who_has_children` looked its two accounts up in a global `?limit=500` page. Phase 4 scaffolds a chart per scenario, so the shared session database outgrew the page and the lookup raised `KeyError` | the second full regression run | the test now asserts on listings **filtered by the parent it created** (children of the root, children of the branch) plus the detail endpoints — deterministic regardless of how many accounts other suites create, and a stronger statement than the page lookup | `tests/integration/test_masterdata_accounts.py` (28 passed) and the final full run |

Each of these is fixed in production code or in the harness — never by weakening an
assertion. Four (`D4-8` … `D4-11`) were defects in the *tests* that would have silently
weakened the evidence or made the suite order-dependent, which is why they are listed here
too: a financial suite that fails for an unrelated reason is a defect in the evidence.

**Final audit, no finding.** The last review pass re-checked the two boundaries a reviewer
would poke at, against the model rather than against intuition:

* a counter account that carries **no currency of its own** (capital `3000`, opening offset
  `6000`, an expense account) records a **functional** leg at rate 1 even when the movement is
  in a foreign currency, exactly as `§6.1` writes it — `test_a_currency_less_counter_account_records_functional_value`
  asserts `AFN / 1.0000000000 / 17500.0000000000`, and `foreign_amount` therefore never claims
  a physical quantity that did not move;
* a disposal **larger than the position it draws from** is refused with
  `INSUFFICIENT_BALANCE`, `details["reason"] = "QUANTITY_EXCEEDED"` and the exact shortfall —
  asserted for the documented case, for a second drawer and for the loser of a race
  (`test_accounting_multicurrency.py`, `test_accounting_concurrency.py`), so a drawer can never
  go short in the books even though the matching physical guard (`NEX01` on `cash_movements`)
  only fires once a cash row exists.

Both behaviours were already implemented and covered; the audit changed no product code. The
final audit also replaced three ad-hoc test password literals with the shared `USER_PASSWORD`
constant so the diff contains no credential-shaped strings of its own making.

---

## 25. Test catalogue

| File | Tests | What it pins |
| --- | --- | --- |
| `tests/unit/test_accounting_rules.py` | 57 | Money rules without a database: balanced/unbalanced/zero/negative/single-sided lines, multi-line entries, scale and bound validation, fingerprint determinism, the authority maps, date rules, payload round-trips |
| `tests/integration/test_accounting_posting.py` | 60 | Posting through the service on a real database: every document type, rejection cases, multi-line journals, accounting-date rules, reference/document rules, duplicate posting, authorisation |
| `tests/integration/test_accounting_multicurrency.py` | 31 | Functional-currency conversion, `foreign_amount` per line, carrying rate, FX result, disposal and empty-position guards, IN/OUT round trips, reversal of a foreign position |
| `tests/integration/test_accounting_immutability_reversal.py` | 29 | Every frozen posting field refused (`NEX06`), line update/delete refused (`P0001`), runtime-role grants, reversal mirror/linkage/uniqueness, reversal audit |
| `tests/integration/test_accounting_idempotency.py` | 15 | Replay of the stored response, mismatch refusal, in-progress refusal, failed-attempt reclaim, per-user and per-endpoint key scope, no second posting |
| `tests/integration/test_accounting_concurrency.py` | 11 | Racing disposals, racing reversals, racing duplicate postings, shared-account posts (no deadlock), rollback on failure |
| `tests/integration/test_accounting_scope.py` | 16 | Posting and reading across branches, group-wide readers, 404-not-403 for other branches, 403 for an explicit out-of-scope filter, auditor reads |
| `tests/integration/test_accounting_integrity.py` | 24 | The ledger's own invariants on an accumulated book: Σdebit = Σcredit per entry and ledger-wide, cache drift and deterministic rebuild, view directions, §8 identities, non-negative cash (`NEX01`), audit chain, reversal linkage, rate snapshot, `NUMERIC(30,10)` edges |

`tests/accounting_helpers.py` (630 lines) provides the shared harness: `World` (branch, chart slice, currencies, actors), scenario and race runners, and raw-SQL readers that let a test assert the database directly rather than trusting the service.

## 26. Exact test commands and results

```bash
cd apps/api
# full regression (the number quoted in every status document of this phase)
env -u DATABASE_URL -u DATABASE_MIGRATION_URL -u APP_ENV PYTHONPATH=. \
    NEXUS_TEST_REDIS_URL="redis://:nexuslocaldev@127.0.0.1:6379/15" \
    python -m pytest tests -q -p no:randomly
#   1148 passed in 165.04s (0:02:45)   EXIT=0
#   1148 passed in 172.38s (0:02:52)   EXIT=0   (a second, independent full run)

# the accounting suites alone
python -m pytest tests -m accounting -q
#   186 passed (186 of the 243 new tests carry the marker; the 57 unit tests are `unit`)

# collection, per file
python -m pytest tests/unit/test_accounting_rules.py -q                       # 57 passed
python -m pytest tests/integration/test_accounting_posting.py -q               # 60 passed
python -m pytest tests/integration/test_accounting_multicurrency.py -q         # 31 passed
python -m pytest tests/integration/test_accounting_immutability_reversal.py -q # 29 passed
python -m pytest tests/integration/test_accounting_idempotency.py -q           # 15 passed
python -m pytest tests/integration/test_accounting_concurrency.py -q           # 11 passed
python -m pytest tests/integration/test_accounting_scope.py -q                 # 16 passed
python -m pytest tests/integration/test_accounting_integrity.py -q             # 24 passed
```

## 27. Regression of Phases 0–3

The Phase 3 baseline was **905 passed** at `3a985fd`. Phase 4 adds **243 tests** (57 unit + 186 integration), so the suite is **1148 tests**:

| Run | Result | Note |
| --- | --- | --- |
| Baseline at `3a985fd` | 905 passed | recorded in `PHASE3_REPORT.md` |
| Full run 1 | 1147 passed, **1 failed** | `test_masterdata_workflow.py::TestPhase3ExitCriteria::test_there_is_no_edit_path_for_a_currency_code` collided on a randomly drawn currency code → fixed as D4-10 |
| Full run 2 | 1147 passed, **1 failed** | `test_masterdata_accounts.py::TestAccountTree::test_the_listing_reports_who_has_children` read a global `?limit=500` page that the accumulated session database had outgrown → fixed as D4-11 |
| Full run 3 | **1148 passed, 0 failed, 0 skipped** in 165.04 s, `EXIT=0` | the run this report quotes |
| Full run 4 (final tree) | **1148 passed, 0 failed, 0 skipped** in 172.38 s, `EXIT=0` | an independent second green full run on the exact tree being committed — two consecutive clean runs is the answer to the two order-dependent flakes above |

Both failures were in *test harness* code, not in the product, and neither was silenced: the currency generator now draws from 26⁸ codes instead of 26⁴, and the account-tree test asserts on listings filtered by the parent it created (a stronger statement than the page lookup it replaced). Every Phase 0–3 suite otherwise passes unchanged, and no assertion was loosened anywhere.

## 28. Ruff

```bash
cd apps/api
python -m ruff check .            # All checks passed!
python -m ruff format --check .   # 135 files already formatted
```

No `# noqa` was added to silence a real finding in Phase 4 code; the only suppressions in the new modules are the documented `S608` on test-only SQL identifiers and SQLAlchemy hook signatures.

## 29. MyPy

```bash
cd apps/api
python -m mypy app seeds scripts
#   Success: no issues found in 89 source files
```

The ledger's row shapes are typed as `RowMapping` at the repository boundary and converted to `JournalLineView`/`JournalEntryView` (frozen dataclasses) before leaving the service, so routes never touch an untyped mapping.

## 30. Migration on a clean database

```bash
psql -h 127.0.0.1 -U postgres -d postgres -c "DROP DATABASE IF EXISTS nexus_ci_clean WITH (FORCE)" \
     -c "CREATE DATABASE nexus_ci_clean"
cd apps/api
DATABASE_URL=postgresql+asyncpg://postgres@127.0.0.1:5432/nexus_ci_clean \
DATABASE_MIGRATION_URL=…  alembic upgrade head
alembic current
#   0002_runtime_schema_revision (head)
```

`docs/database/schema.sql` still hashes to
`37f7bc3cfc523589f934494b910a49db67a4d66cfd1b34b843cf3819102ae455`, matching
`apps/api/alembic/sql/CHECKSUMS.txt`, and `tests/integration/test_migration.py` (part of the
suite) verifies that checksum against `EXPECTED_SHA256` on every run. Phase 4 wrote **no**
revision: head is unchanged from Phase 3.

## 31. Schema gates and ORM/schema parity

```bash
PYTHONPATH=. python -m scripts.schema_gate orm-db \
    --dsn postgresql+psycopg://postgres@127.0.0.1:5432/nexus_ci_clean
#   ORM vs DATABASE — tables: 31, columns: 341, result: MATCH

psql -f docs/database/schema.sql        # onto a second database (nexus_ci_reference)
PYTHONPATH=. python -m scripts.schema_gate db-db \
    --left  postgresql+psycopg://…/nexus_ci_reference \
    --right postgresql+psycopg://…/nexus_ci_clean
#   REFERENCE (schema.sql) vs MIGRATION (alembic upgrade head)
#   tables: 31, indexes: 72, checks: 71, triggers: 48, routines: 23, views: 5, result: MATCH
```

The reference file's own self-check prints `NEXUS schema self-check passed (30
NUMERIC(30,10) columns, 0 float columns)` while it is applied. Both gates are the same ones
CI runs, so "Phase 4 needed no structural change" is verified the same way the schema has
been since Phase 1. Autogenerate (`alembic check`) is not part of the accepted gate set —
see limitation L-10.

## 32. Phase 0 invariant suite on a fresh migrated database

```bash
psql -f tests/invariants/phase0_schema_invariants.sql   # on nexus_ci_phase0, migrated to head
#   PHASE 0 SCHEMA INVARIANT SUITE: ALL ASSERTIONS PASSED
#     ledger totals  : debit = credit = 1770000.0000000000
#     audit chain     : valid
#   53 PASS lines, exit code 0
```

The frozen Phase 0 invariant suite still passes unchanged, on a database built by the
migrations of this branch — the strongest available statement that Phase 4 did not disturb
the approved foundations.

## 33. Seed idempotency

```bash
APP_ENV=development DEV_ADMIN_PASSWORD=… python -m seeds         # inserted=89 updated=0 unchanged=0 removed=0
APP_ENV=development DEV_ADMIN_PASSWORD=… python -m seeds         # inserted=0  updated=0 unchanged=88 removed=0
APP_ENV=development DEV_ADMIN_PASSWORD=… python -m seeds --check # dry_run=true, unchanged=88
```

All three CI grep patterns match exactly.

## 34. Static audit of the diff

| Check | Result |
| --- | --- |
| `TODO`/`FIXME`/`XXX`/`HACK`/`coming soon`/`placeholder`/`mock`/`not implemented` in the Phase 4 production modules | no matches |
| Same scan across `app/`, `seeds/`, `scripts/` | one match, `app/core/config.py`'s list of *forbidden* weak secret values (`"xxx"`) — a security feature, pre-existing, not unfinished work |
| Debug leftovers (`breakpoint()`, `import pdb`, `print(`) in changed files | none |
| Secret-shaped literals added by the diff | none (the only credentials in the diff are the documented test passwords in test files) |
| Unintended files (`.env`, `*.local`, caches, artefacts) | none; `git status` shows only the intended changes |

## 35. Security verification (PART 41, PART 42)

| Control | Phase 4 evidence |
| --- | --- |
| Deny-by-default RBAC | Every ledger route is behind `reports.view`; posting authority is per document type (`POSTING_AUTHORITY`), asserted for each; an actor without the authority is refused *and* audited |
| Object-level scope | Non-group-wide actors are bound to their own branch for both posting and reading; other branches answer 404 for a single entry and 403 for an explicit filter |
| Least privilege at the database | `nexus_app` holds no `UPDATE`/`DELETE` on `journal_entries`, `journal_lines`, `cash_movements`, `audit_logs`; asserted by `test_the_runtime_role_has_no_delete_or_update_on_the_ledger` |
| Immutability | `NEX06` and `P0001` refusals are asserted by reading the driver error, not a wrapper |
| Auditability | One hash-chained audit row per posted/reversed entry and per refused posting; `verify_audit_chain(0)` returns no broken row on the accumulated ledger |
| Money handling | `Decimal` only; floats refused with `not_a_decimal`; scale and bound enforced before writing; `NUMERIC(30,10)` at rest |
| Idempotency | `Idempotency-Key` claim/complete/replay, scoped per user and endpoint, mismatch and in-progress refusals |
| No secrets, no placeholder, no debug code | §34 |

## 36. CI results

The implementation commit is pushed to `arena/01a090c5-nexus-exchange-erp`, which runs the
six CI jobs (`lint`, `typecheck`, `unit`, `integration` — integration suite, migration,
both schema gates, seed idempotency, Phase 0 invariants —, `compose-stack`, `openapi`). The
exact runs and their job/step conclusions are recorded in `docs/PROJECT_STATUS.md` §3 and
in the finalisation commit, by the same read-back procedure Phase 2 and Phase 3 used (job
logs are not retrievable from this sandbox; step conclusions and check annotations are).

## 37. Limitations

| # | Limitation | Why it is acceptable here |
| --- | --- | --- |
| L-1 | **No Docker CLI in the development sandbox**, so `docker compose up -d` cannot be executed locally | The CI `compose-stack` job runs the real five-service stack end to end (build → health → migrate → seed → readiness through nginx) and its step conclusions are the evidence, exactly as in Phase 3 |
| L-2 | **Python 3.11.2** in the sandbox instead of the 3.12 target | Code and dependencies target 3.12; the difference is a runtime property of the sandbox, not of the deliverable |
| L-3 | No `psql`/`redis-cli` on `PATH` | The gate commands above use the PostgreSQL client bundled with the sandbox's server package and the project's own scripts; CI uses the stock clients |
| L-4 | **No period locking and no FX revaluation** | Deliberate and documented since Phase 0 (`ACCOUNTING_MODEL.md` §11): posting is period-agnostic; month-end closing and revaluation are a migration-backed Phase 12 feature, not a stub |
| L-5 | `exchange_rates` is mutable at the database level (no append-only trigger) | A quote is a market observation, not money. Decision D-4-1 (§13) shows the ledger does not depend on it, and the tests prove a re-priced quote cannot move history |
| L-6 | `GROUP_WIDE_ROLES` is a **role set**, not a delegable `branch.scope_all` permission | The approved permission matrix is honored; a finer-grained scope permission would be a capability change and belongs in a later, explicitly approved phase |
| L-7 | `rate_snapshot.exchange_rate_id` is `NULL` when the caller posts a rate without naming a quote | The number is always recorded; the provenance is recorded when supplied. Phase 5's route resolves the quote and passes it — the parameter and its audit rendering are tested today |
| L-8 | `get_account_balance` has no HTTP route yet (`GET /journal`, `GET /journal/{id}`, `GET /reports/trial-balance` do) | Account balance is service-level in Phase 4; the reports phase exposes it. No stub route was added to pretend otherwise |
| L-9 | The `slow` marker covers the integrity suite; no test is skipped anywhere | 1148 passed, 0 skipped — the marker only groups reporting |
| L-10 | `alembic check` (autogenerate drift) is not part of the gate set: it reports SQLAlchemy-declared names for constraints/indexes that the frozen reference DDL creates as literal SQL | Pre-existing since Phase 1; the accepted drift gate is `scripts/schema_gate.py` (`orm-db` + `db-db`, both MATCH). Documented in `docs/database/SCHEMA.md` §10 so the claim and the gate agree |

## 38. Risks

| # | Risk | Mitigation / status |
| --- | --- | --- |
| R-1 | Concurrency safety is demonstrated by 11 targeted race tests, not by a load test | The races are the ones the books depend on (two disposals of one position, a reversal racing its original, duplicate posting of one document, shared-account posts). Production load testing belongs to Phase 12 |
| R-2 | Serialization failures and deadlocks are retryable (`503` + `Retry-After`) but the client must retry | Documented in the error catalogue; the ledger never retries silently, because a silent retry of a posting is exactly what idempotency exists to make unnecessary |
| R-3 | The balance cache is a cache | Reports read `journal_lines`; `rebuild_account_balances()` is asserted to be a no-op on a current ledger, and the worker can rebuild it |
| R-4 | Rate provenance without a quote id (L-7) | Phase 5 passes the resolved quote; until then the number itself is recorded and immutable |
| R-5 | Real-world rates, commissions and rounding policies vary by business | Rounding is centralized in `money.py` and asserted at the edges; a policy change is a code change in one place with tests, not a schema change |
| R-6 | The suite shares one session database, so tests that read *global* pages can be affected by other suites' rows | Two such tests were found and rewritten in this phase (D4-10, D4-11); the remaining global reads are bounded by small catalogues (currencies, branches) and are asserted on filtered listings |

## 39. Acceptance checklist

Requirements of the phase brief:

| # | Requirement | Evidence |
| --- | --- | --- |
| 1 | Double-entry mathematically enforced | `validate_balanced_entry` + deferred `ct_journal_lines_balanced_*`; §25 posting suite |
| 2 | Every posted journal has Σdebit = Σcredit | Asserted per entry and ledger-wide, directly in SQL, in the integrity suite |
| 3 | Decimal/NUMERIC only for money | `money.py` policy; 30 `NUMERIC(30,10)` columns; schema self-check; floats refused (`not_a_decimal`) |
| 4 | No floating-point monetary calculation | No `float(` and no `Decimal(<float literal>)` in the ledger path (audited, §34) |
| 5 | Posted journals and lines immutable | `NEX06` for entry fields, `P0001` for lines, `REVOKE UPDATE/DELETE` for the runtime role |
| 6 | Corrections by reversal/adjustment, never destructive mutation | Reversal lifecycle (§10); no update/delete path exists |
| 7 | Complete auditability | One hash-chained audit row per event, plus a row for every refused posting |
| 8 | Valid account/currency relationships | Account activity/postability, branch binding, currency match and activity are validated before writing |
| 9 | Consistent debit/credit semantics | Single-sided `CHECK`, non-negative amounts, normal-balance direction in reports |
| 10 | Phase 0 functional-currency rules preserved | Functional legs at rate 1; Phase 0 invariant suite still ALL ASSERTIONS PASSED |
| 11 | `foreign_amount` / exchange-rate semantics preserved | Generated column; the per-line relation is asserted on every line of the used ledger |
| 12 | Invalid or unbalanced journals prevented | Validation before writing; database refusals translated to documented domain errors |
| 13 | Duplicate posting prevented through idempotency | Three layers: natural key, idempotency guard, unique index; 15 dedicated tests |
| 14 | Transaction/document references preserved | `reference_type`/`reference_id` on every entry; reversal references the original *entry* id; one entry per document |
| 15 | Accounting-date / business-date rules enforced | Naive dates refused, future dates refused, reversals may not precede their original, ranges filter on the stored instant |
| 16 | Branch and authorisation boundaries respected | §15, §16; posted, read and filtered scope all asserted |
| 17 | Unauthorised users cannot post or manipulate | RBAC tests per document type; refusals audited as `LEDGER_POSTING_DENIED` |
| 18 | Historical financial records cannot be deleted or silently modified | Triggers + revokes + no delete path; asserted by reading driver errors |
| 19 | Reversals fully auditable and balanced | Reversal mirror/link/uniqueness tests and the reversal audit test |
| 20 | Database and application must not contradict each other | One SQLSTATE→error mapping shared by the service and the HTTP layer (§23); both directions tested |

Phase-brief specific items:

| Item | Evidence |
| --- | --- |
| Explicit review of the rate snapshot/reference (with a decision) | §13, decision D-4-1: no schema change, with the reasoning table and the tests in `TestTheRateSnapshotIsProtected` |
| No silent schema change | §11, §30, §31: no revision, reference file checksum unchanged, both gates MATCH |
| Accounting test list (balanced/unbalanced/zero/debit-only/credit-only/multi-line/multi-currency/functional conversion/`foreign_amount`/account-currency validation/branch authorisation/immutability/UPDATE and DELETE rejection/reversal creation, balance and linkage/duplicate and idempotent posting/concurrency/rollback/audit events/unauthorised and forbidden posting/historical protection/accounting dates/precision and rounding/large and small decimals/database-level invariants) | §25 — every item maps to named tests across the eight files |
| Complete Phase 0–3 regression | §27 — 1148 passed, 0 failed |
| Documentation (`PHASE4_REPORT.md`, `PROJECT_STATUS.md`, README, contract, model, schema reference) | this file plus the four updated documents listed in §5 |

## 40. Statement

Phase 4 is **READY FOR REVIEW**. It is *not* approved: only the human reviewer moves a
phase to APPROVED, and that decision is recorded in `docs/PROJECT_STATUS.md`.

Phase 5 (exchange routes: buy/sell documents, receipts, cancel and reverse) has **not been
started**, no Phase 5 code, route, migration or test exists, and nothing in this phase
presumes it beyond the service surface the roadmap assigns to Phase 4.
