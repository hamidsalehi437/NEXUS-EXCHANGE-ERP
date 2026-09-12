# PHASE 5 REPORT — Exchange engine (buy/sell, commission, receipts, cancel, reverse)

| Field | Value |
| --- | --- |
| Document ID | `PHASE5-REPORT-001` |
| Phase | 5 — Exchange engine (roadmap: `docs/architecture/ROADMAP.md`) |
| Status | **READY FOR REVIEW** (only the human reviewer may mark it APPROVED) |
| Starting commit | `f0910b5` (Phase 4 finalisation — Gate Review fix `21b6211` plus its documentation commit; the frozen Phase 4 lineage) |
| Implementation commit | *pinned in the finalisation commit of this phase* (visible in `git log`) |
| Branch | `arena/01a090c5-nexus-exchange-erp` |
| Predecessor | [`PHASE4_REPORT.md`](PHASE4_REPORT.md) (accounting engine — the authoritative posting layer) |
| Successor | Phase 6 (cash) — **not started, not authorised** |

> **خلاصهٔ فارسی** — فاز ۵ موتور معاوضهٔ ارز را می‌سازد: چرخهٔ کامل خرید/فروش، اعتبارسنجی، انتخاب و عکس‌برداری نرخ، محاسبهٔ کمیسیون و سود، ثبت از طریق `AccountingService` (تنها مسیر ثبت)، ماشین وضعیت سند، idempotency، قفل‌گذاری و آزمون‌های هم‌روندی روی PostgreSQL واقعی، ممیزی کامل، شماره‌گذاری اسناد با معماری فاز ۰، مجوزها، و قراردادهای آماده برای حالت آفلاین — همه با `Decimal`/`NUMERIC`.

---

## 1. Phase name and scope

Phase 5 builds the exchange engine: the business orchestration layer that turns a counter
operation ("the customer hands over 1 000 USD, the house pays out afghani") into an auditable
financial document, and posts it through the accounting engine of Phase 4 as **one**
transaction.

In scope, exactly as the phase brief and the roadmap list it:

* the exchange service — validation, quote selection, rate snapshotting, gross/commission/net
  settlement, profit/result, journal creation **through** `AccountingService`, document
  references, audit, idempotency, authorization, concurrency protection, reversal compatibility;
* the exchange document store (`repositories/exchange.py`) — numbering through the frozen
  Phase 0 function, explicit row locks for the lifecycle moves, the joined view projection and
  the cash/revenue reads the receipt and the result tests need;
* the wire and response schemas (`app/schemas/exchange.py`) and the six endpoints of
  `docs/api/API_CONTRACT.md` §9.3;
* the `AccountingService` extensions the engine legitimately needed (an arithmetic core shared
  with the ledger, currency/drawer resolution, movement recording, the reversal authority map,
  branch-scope helpers) — additions only, no second posting path;
* the test suites: unit rules, posting, lifecycle, accounting integration, concurrency.

Out of scope and deliberately untouched: cash sessions (Phase 6), reports (Phase 7), the offline
sync engine (Phase 8/12 — this phase contributes contracts and hooks only), printing (Phase 11),
transfers (Phase 10), and any change to the approved Phase 0 schema, its frozen migrations,
`docs/database/schema.sql` or the Phase 4 posting model.

## 2. Authorisation and constraints under which this phase ran

* One authoritative posting path: every financial write of the exchange engine goes through
  `AccountingService` (`post_exchange`, `record_cash_movements`, the generic entry door for the
  reversal legs). No module of this phase writes `journal_entries`, `journal_lines`, account
  balances, cash positions or inventory positions directly, and no accounting formula is
  duplicated outside the accounting engine — the document service stores exactly the numbers
  `compute_exchange_amounts` returns.
* `Decimal`/`NUMERIC` only; no `float`, no Python `round()` where financial rounding semantics
  apply (PARTs 20, 62).
* Existing schema only. **No migration was written and none was needed**; the frozen
  migrations and the reference DDL are byte-for-byte unchanged (checksum gate in §31).
* Deterministic domain error codes; invalid business semantics never reach accounting.
* Offline compatibility as contracts and hooks, never as a second implementation.
* Full regression of Phases 0–4 before the phase may be declared complete.
* Do not mark the phase APPROVED; do not start Phase 6.

## 3. Starting commit

`f0910b5` — the Phase 4 finalisation commit: the Gate Review fix
`21b6211d7918ad8a16b27d3ca1dc54887d22b907` (exchange-direction and inventory-position holes) and
the documentation commit that pins it. At that commit the repository had **1 173 passing tests**
(0 failed, 0 skipped) in 205.42 s, the frozen schema at head revision
`0002_runtime_schema_revision`, both schema gates MATCH, the Phase 0 invariant suite passing on a
fresh migrated database, and seeds idempotent (89 / 88 / 88).

## 4. Final commit

The implementation, tests and this report are **one commit** on
`arena/01a090c5-nexus-exchange-erp`; the documentation-only finalisation commit that follows it
pins the hash and records the CI runs (the same pattern Phases 2–4 used). The exact hash, diff
stat and CI run identifiers are recorded there and in `docs/PROJECT_STATUS.md` §1.

## 5. Files created and modified

| Kind | File | Lines |
| --- | --- | --- |
| production | `app/services/exchange_service.py` (new) | 1 607 |
| production | `app/services/accounting_service.py` (extended) | +560/−27 (2 608 → 3 141) |
| production | `app/repositories/exchange.py` (new) | 398 |
| production | `app/schemas/exchange.py` (new) | 330 |
| production | `app/api/v1/exchange.py` (new) | 320 |
| production | `app/core/exceptions.py` | +68 |
| production | `app/api/deps.py` | +31/−1 |
| production | `app/core/audit_actions.py` | +16 |
| production | `app/api/v1/router.py` | +7/−5 |
| production | `app/core/idempotency.py` | +7 |
| tooling | `pyproject.toml` (pytest marker `exchange`) | +1 |
| test | `tests/exchange_helpers.py` (new) | 1 196 |
| test | `tests/integration/test_exchange_posting.py` (new) | 1 153 |
| test | `tests/integration/test_exchange_lifecycle.py` (new) | 1 002 |
| test | `tests/integration/test_exchange_accounting.py` (new) | 595 |
| test | `tests/integration/test_exchange_concurrency.py` (new) | 570 |
| test | `tests/unit/test_exchange_rules.py` (new) | 403 |
| test | `tests/conftest.py` | +64/−2 |
| test | `tests/accounting_helpers.py` | +26 |
| test | `tests/integration/test_accounting_integrity.py` | +7 |
| test | `tests/integration/test_accounting_multicurrency.py` | +9 |
| docs | `docs/phases/PHASE5_REPORT.md` (new), `docs/PROJECT_STATUS.md` | this report |

Total: **21 files, +8 370/−35** before this report and the status update — 10 production files,
1 tooling, 9 test files, then the documentation. No file of Phases 0–4 was deleted, renamed or
weakened; `accounting_service.py` grew by 533 net lines and nothing was removed from it.

## 6. Architecture of the exchange engine

```
POST /api/v1/exchange ─┐
                       ▼
        app/api/v1/exchange.py            routes: auth dependency, Idempotency-Key,
                       │                  response envelope, status codes (201/200)
                       ▼
        app/services/exchange_service.py  ONE business transaction:
                       │                    validate → resolve device/branch/customer
                       │                    → resolve currencies → deterministic rate snapshot
                       │                    → compute amounts → allocate document number
                       │                    → insert document → post through AccountingService
                       │                    → record cash movements → audit → commit
                       ├────────────► app/repositories/exchange.py   document rows, locks,
                       │                                            numbering, view, reads
                       ▼
        app/services/accounting_service.py  THE authoritative posting layer:
                       │                    post_exchange (BUY/SELL journal),
                       │                    record_cash_movements (physical side),
                       │                    generic entry door (reversal legs),
                       │                    inventory/position guards, account locking
                       ▼
                     PostgreSQL         immutability triggers, deferred balance and
                                        non-negative constraints, NEX03/NEX04/NEX06
```

The document is the *business* record; the journal entry and the movements are the *financial*
record. They are written in one database transaction, and the document row is inserted before
the entry is posted precisely so a failure leaves neither a `PENDING` document, nor a movement,
nor a consumed document number (§17, §26).

## 7. Business semantics implemented (§5 of the brief)

Both document types take a **foreign** `from_currency` (the functional currency is never the
foreign leg) and a `to_currency` that is the functional currency or another foreign one. The two
types differ in which side of the deal the house is on, and therefore in which leg is disposed
of and at what value it leaves the books:

| | BUY | SELL |
| --- | --- | --- |
| House acquires / delivers | acquires `from_currency` at the transaction price | delivers `from_currency` out of its drawer position |
| House pays / receives | pays out `to_currency` at that drawer's carrying rate | receives `to_currency` at the house's functional rate for it |
| Gross | `from_amount × exchange_rate` (quantized at `to_currency` scale) | `from_amount × exchange_rate` (same) |
| Settlement posted as `to_amount` | `gross − commission` | `gross` |
| Commission | income in `to_currency`, kept out of the payout | income in `to_currency`, recognized inside the receipt |
| Position guard | none on the acquired side | the `from_currency` drawer must cover `from_amount` |

Refusals that must happen *before* any rate is applied or any entry is built:

* `transaction_type` that is not `BUY`/`SELL` → 422 `VALIDATION_ERROR`, `fields[{transaction_type,
  unsupported}]`;
* the same currency on both sides → 422 `EXCHANGE_DIRECTION_INVALID`, `SAME_CURRENCY`;
* the functional currency on the foreign leg → 422 `EXCHANGE_DIRECTION_INVALID`,
  `FUNCTIONAL_CURRENCY_NOT_DELIVERABLE` (a customer buying foreign currency is a **SELL** of that
  currency, not a BUY of the afghani);
* an amount or rate that is not positive, an amount below the currency's smallest unit, a rate so
  small it produces nothing, and a commission `≥` gross → 422 `VALIDATION_ERROR` with
  `not_positive` / `negative` / `below_smallest_unit` / `zero_result` / `exceeds_gross` and the
  gross in `details`;
* an inactive currency, customer or branch → the corresponding domain refusal, never a silent
  posting.

## 8. Deterministic rate snapshot (§6 of the brief)

`_resolve_quote` (in the service) and `_functional_rate` (in the ledger) resolve in exactly this
order: **branch context → currency pair → direction → applicable quote → validity → snapshot →
compute → pass the snapshot into accounting**. Consequences the tests pin:

* the quote used is the one in force for the pair at the branch at the business moment, published
  through the Phase 3 rate book (no inverse fallback is invented: a pair with no quote is
  `RATE_NOT_FOUND`, with the `as_of` moment in `details`);
* a client-supplied rate outside the house's published quote by more than the accepted tolerance
  is refused `RATE_OUT_OF_TOLERANCE` (409), and a client-side amount that differs from the
  computed settlement by more than one minor unit is refused `AMOUNT_MISMATCH` (422);
* the applied rate travels into the ledger as an explicit `RateSnapshot` (rate, currencies,
  branch) and onto the entry, so a posted document is reproducible after the rate book changes;
* re-pricing a quote afterwards cannot move posted history (test:
  `test_an_issued_quote_moved_afterwards_cannot_change_what_a_posted_deal_means`);
* nothing in this phase updates a historical rate; the rate book stays append-only (§Phase 3).

## 9. Money, Decimal and rounding (§7 of the brief)

* All amounts are `Decimal` end to end; `app/core/money.py` is the only arithmetic layer
  (`multiply_money`, `money_difference`, `money_sum`, `quantize_money`, `divide_money`,
  `format_decimal`, `assert_within_money_bounds`, and the `money_context()` guard that makes an
  accidental `float`/`round()` inside a computation an error rather than a silent rounding).
* One rounding step per leg: `gross = from_amount × rate` quantized once at the `to_currency`
  scale; the settlement is derived from the quantized gross; a stored amount is refused if it
  cannot be expressed at the currency's scale (`below_smallest_unit`) — the engine never stores a
  value it would have to round on the way to the database.
* Functional valuations (`debit`/`credit` on every line) are quantized at the functional
  currency's scale, and the FX result line is the *difference of the two already-quantized legs*,
  so the entry balances exactly and no double rounding can open a gap
  (`_close_with_fx_result`).
* The wire format is a string in both directions; the request schema refuses JSON floats for a
  money field, and the response prints the stored scale (for example `69500.0000000000`).

## 10. Commission engine (§8 of the brief)

* The commission is a first-class field of the document, the computation
  (`ExchangeComputation.commission`) and the entry; it is never folded into the principal and
  never posted as a negative.
* Zero commission is valid and produces no `4010` line at all (not a zero line), which the
  accounting suite asserts for every deal in its matrix; a positive commission is refused when it
  is not smaller than the gross (`exceeds_gross`), so a fee can never swallow a deal.
* The commission is denominated in `to_currency` and posted to the chart's commission income
  account (4010 in the seeded chart) — with the field's own currency and rate, so the entry's
  balance and its audit trail are both readable.
* The accounting representation is asserted directly: the commission line's functional credit
  equals the commission valued at the deal's rate, and `fee == commission` for every deal in the
  six-deal sweep.

## 11. Profit / result (§9 of the brief)

The result of a deal is derived from its own economics and is reproducible from the persisted
document plus its journal lines:

* **BUY** — the acquired leg enters at the transaction price, the paid leg leaves at the drawing
  account's carrying rate; the difference is the realized result. Measured: a fee-only buy
  (`1000 USD @ 70`, commission `500`) yields `0 + 500` (no FX result, the fee is the profit).
* **SELL** — the delivered leg leaves at its carrying rate and the received leg enters at the
  house's functional rate for it. Measured: selling `500 USD @ 71` against a position carried at
  `70` yields `300 + 200` (300 realized FX on the spread, 200 commission).
* A deal struck at the position's own carrying rate yields a **zero** result and posts no income
  line at all (`no 4000/4010 lines`), which is the honest representation: no economic event, no
  entry to invent.
* No manual override of the result exists anywhere in the API: it is not a request field, not a
  document column, and not something the service accepts. Profit is what the ledger computes from
  the persisted quantities, rates and rates of carriage — the tests recompute it from the entry
  lines and require equality (`fx + fee == gained`).

## 12. Exactly one authoritative posting path (§10 of the brief)

* `exchange_service.create_exchange` composes the ledger call: it resolves the branch, device,
  customer, currencies and the two cash accounts (through
  `AccountingService.resolve_exchange_currencies` / `inventory_account`), builds the rate
  snapshot, and posts with **complete explicit posting information** — transaction type,
  reference id, branch, both currencies, both cash accounts, FX account, commission account,
  amounts, rate, snapshot, business date, device, actor.
* The service never touches the account/cash tables: the only rows it writes are the exchange
  document (its own table) and, through the accounting engine, the entry, its lines, the
  movements and the audits.
* Position guards, account locking, branch authorization, idempotency and balance enforcement all
  stay inside the accounting engine. The document service deliberately cannot bypass them: it
  calls the same public methods a Phase 4 caller would.
* The reversal path posts its reversing entry through the same door, and the cancel path posts
  its own reversing entry through `AccountingService.reverse_journal_entry`.

## 13. Inventory and position safety (§11 of the brief)

* Accounts are locked **before** the position is read: `_lock_accounts` runs first, sorted by
  `(account_id, currency_id)`, so two concurrent sellers cannot both read "enough left".
* The disposal guard reads the drawer's functional value per unit under that lock
  (`_carrying_rate`), refuses an empty or short drawer with `INSUFFICIENT_BALANCE` and the reason
  (`NO_POSITION` / `QUANTITY_EXCEEDED`, plus `account_id`, `foreign_quantity`, `shortfall`), and
  never lets the position go negative — with `ct_cash_movements_non_negative` (`NEX01`, deferred
  to COMMIT) as the database's independent backstop.
* Real PostgreSQL races (no mocks, real connections, real commits) prove the outcomes by
  delta-asserting the financial state before and after:
  * two simultaneous sales against one till → exactly one wins, the loser is refused, and
    `cash(USD) == 0` with the winner's afghani in the drawer;
  * a sale larger than the position → refused, nothing observable changed;
  * a partial fit → the remainder is countable (measured: 8 000 units left);
  * simultaneous **BUY and SELL of the same currency at the same branch** → both post, net
    afghani `+1 000`, USD flat, no negative position at any point;
  * simultaneous BUYs from different branches/customers → independent, each branch's position its
    own.

## 14. Customer balance safety (§12 of the brief)

The exchange document names a customer when one is given; no customer *balance* account
participates in these postings (there is no customer-ledger seat in the approved Phase 0 schema
for exchange, and this phase did not invent one). What the engine does enforce is the
customer-record side: the customer must exist, be active and belong to the acting branch's scope
(`_resolve_customer`), the document stores the reference immutably, and no cross-branch customer
can be attached to a document at another branch. The counters that *do* move money
(drawers, cash movements, ledger lines) are the branch-scoped accounts of §13, written with the
branch on every row, so a document can never move another branch's cash.

## 15. Document state machine (§13 of the brief)

`POSTED` → `REVERSED` (mirror document) and `POSTED` → `CANCELLED` (pre-settlement correction),
each with its own journal entry, audit rows and immutable links. The database carries the
authority (`NEX03` status machine, `NEX04` reversal mirroring, the deferred
`ct_exchange_reversal_bound`, `NEX06` money-column immutability, `P0001` on DELETE); the engine
mirrors it in the domain:

* no illegal transition — a second cancel or reverse of the same document is `409
  INVALID_STATUS_TRANSITION`; reversing an already-reversed document is `409 ALREADY_REVERSED`;
* a `POSTED` document is not editable (UPDATE of a money column is refused by `NEX06`, and the
  API exposes no edit route);
* a `REVERSED` document is not re-postable; a mirror document cannot itself be undone
  (`409 REVERSAL_NOT_UNDOABLE`);
* no partial state after a failure: a refusal leaves neither a document row, nor a movement, nor
  a consumed number (§28 of the test catalogue).

## 16. Idempotency (§14 of the brief)

* Every financial write of the phase is claimed through `app/core/idempotency.py` under a key
  scoped to `(user, endpoint, key)` with the three endpoints of the phase
  (`exchange:create`, `exchange:cancel`, `exchange:reverse`), inside a savepoint, so a duplicate
  request cannot write a second document or a second entry.
* Same key **and** same payload → the recorded answer is returned verbatim (status and body
  byte-identical: same document id, same number, same entry); the tests compare every field of the
  stored answer with the replay over the union of their keys.
* Same key with a **different** payload → the claim's fingerprint refuses the request; the
  fingerprint covers every financial field, so a changed amount can never be answered with the
  earlier document.
* Concurrent duplicates: four simultaneous `POST /exchange` calls with one key produce exactly one
  document, one entry and two movements; the loser's contract is a deterministic
  `DUPLICATE_RESOURCE` refusal (not a second document, not a 500).
* Failed-then-retry: a refused attempt does **not** become a stored answer — nothing is claimed,
  the key stays free, and the retry with the same key posts normally (the key is not
  "poisoned"). A rollback (insufficient position) also consumes no number and stores no key.
* Offline-origin duplicates: a re-delivered `client_event_id` with a payload that contradicts the
  stored document is refused and audited as `EXCHANGE_EVENT_CONFLICT` — Last-Write-Wins is
  forbidden for financial records (PART 34). This is the *hook* the offline phase will use, not
  an offline implementation.

## 17. Concurrency, locking and rollback (§15 of the brief)

Analysed and pinned for: branch inventory (drawer positions), cash accounts, the rate snapshot,
the document number, the idempotency claim, and the creation of the document row.

* **Deterministic lock ordering.** Every path that can hold two exclusive locks takes them in the
  same order: accounts sorted by `(account_id, currency_id)` first, then the document row
  (`_lock_document`, `FOR UPDATE`), then the number sequence inside the posting transaction. Two
  documents that share drawers therefore queue instead of deadlocking.
* **The document number is allocated inside the transaction** through the frozen
  `next_document_number(prefix, scope, period)`, so a rollback returns the number: measured by
  reading `sequences.current_value` before and after a refused deal and then asserting the next
  accepted deal takes exactly `counter + 1`.
* **Real races.** `test_exchange_concurrency.py` runs real concurrent database transactions
  (independent sessions/connections, real commits) for: simultaneous sales against limited
  inventory; simultaneous BUY and SELL of the same currency at the same branch; same currency,
  different branches; same branch, different customers; concurrent idempotent duplicates;
  cancel racing reverse (exactly one undo happens, the money comes back exactly once). No mock,
  no fake clock, no fake session.
* **Nothing negative is observable after a commit**: the suites assert the ledger position and
  the drawer position per currency after every race, and the deferred database constraints are
  the backstop that would refuse otherwise.

## 18. Authorization, branch isolation and auditability (§16–§18 of the brief)

* **RBAC.** Four permission codes drive the phase: `exchange.create`, `exchange.view`,
  `exchange.cancel`, `exchange.reverse` (the last two deliberately split — the Phase 4 Gate
  Review established that reversing is a supervising act). The actor must be authenticated, the
  device must be registered, active and assigned to the branch it acts for, and a refusal is
  written as `EXCHANGE_ACCESS_DENIED` so a denial is as auditable as a success. Measured
  matrix on a counter device at the document's own branch: MANAGER 201/200/200,
  ACCOUNTANT 201/200/403 (no `exchange.reverse`), CASHIER 201/403/403, AUDITOR 403/403/403.
* **Branch isolation.** A branch-scoped actor cannot post for, list, read, cancel or reverse
  another branch's document: the group-wide roles see the group, a branch actor sees its own
  documents only, a cross-branch `branch_id` is `ForbiddenScopeError`, and reading another
  branch's document by id is `RESOURCE_NOT_FOUND` (the existence of a document is itself branch
  information). Cross-branch attack tests are in the posting and lifecycle suites.
* **Auditability.** Every state change writes exactly one business audit row
  (`EXCHANGE_CREATED`, `EXCHANGE_CANCELLED`, `EXCHANGE_REVERSED`, plus `EXCHANGE_EVENT_CONFLICT`
  and `EXCHANGE_ACCESS_DENIED` for the refusals), and the journal rows carry their own
  `JOURNAL_POSTED` / `JOURNAL_REVERSED` actions — so an auditor can walk document → entry →
  movement and back. The rows carry who (actor + role), what (document, type, currencies,
  amounts, rate, commission, reference), when (UTC + branch business date), where (branch,
  device) and the reversal relation (`reversal_transaction_id`, `reverses_transaction_number`,
  `reversal_reason`). No sensitive data beyond the operational fields (no credentials, no tokens,
  no raw client payload) is stored or logged.

## 19. Document numbering (§18 of the brief)

The engine uses the Phase 0 architecture as it is — `next_document_number(prefix, scope, period)`
over `sequences(name = scope || ':' || period)` — with `NX` as the prefix, the branch's business
date as the period and the document scope as the series name. Properties proved by the tests:

* one series per `(scope, period)`, shared by branches, deliberately (it is a document series,
  not a per-branch counter);
* numbers are unique and contiguous for committed documents (`NX-20260912-000094`), never reused;
* a refused operation rolls the counter back, so the series has no holes from failures;
* the number is unique at the database level (`exchange_transactions.transaction_number`), so a
  duplicate commit is impossible even if two processes raced the sequence;
* a document is never posted without a valid number: allocation happens before the insert, in the
  same transaction.

## 20. Offline hooks (§19 of the brief)

Contracts only — the offline system itself is Phase 8/12 and was **not** built:

* a client-supplied `client_event_id` (UUID) travels with the document, is stored, is unique per
  branch at the database level, and is the idempotency anchor for a replayed offline event;
* the `device_id` and `origin` of every document are recorded, so the counter that produced it is
  known;
* re-delivery with a contradictory payload is detected and refused
  (`EXCHANGE_EVENT_CONFLICT`), never silently applied;
* the posted document's result is immutable and replayable: the same key returns the recorded
  answer byte-for-byte, which is what a client needs to reconcile a queue;
* deterministic retry is safe: a client can retry a timed-out request with the same key forever
  and can never double-post.

## 21. API contracts (§20 of the brief) — `docs/api/API_CONTRACT.md` §9.3

| Method | Path | Purpose | Idempotency-Key |
| --- | --- | --- | --- |
| POST | `/api/v1/exchange` | create (BUY/SELL) and post | **required** |
| GET | `/api/v1/exchange` | page the book (filters, scope, newest first) | — |
| GET | `/api/v1/exchange/{id}` | one document | — |
| POST | `/api/v1/exchange/{id}/cancel` | cancel a posted document | **required** |
| POST | `/api/v1/exchange/{id}/reverse` | reverse a posted document | **required** |
| GET | `/api/v1/exchange/{id}/receipt` | deterministic receipt | — |

* Status codes: 201 for a created document, 200 for reads and for a replayed answer; the error
  envelope is `{error:{code,message,details}}` (PART 38).
* A missing `Idempotency-Key` on a write is `400 IDEMPOTENCY_KEY_REQUIRED`; the key must be a
  UUID (the dependency validates the shape before the service runs).
* The reverse endpoint returns the **original** document (`ExchangeResult(transaction_id =
  original.id)`); the mirror document's id is a payload field (`reversal_transaction_id`), which
  is how the lifecycle suite addresses it.
* `GET /receipt` is deterministic and versioned (`RECEIPT_VERSION = "phase5-1"`): `issued_at` is
  the document's `created_at` and never the request time, repeated calls are byte-identical, and
  cancelling the document changes its `status` while leaving `issued_at`, `settlement` and
  `exchange_rate` untouched. `format=pdf` is refused with 422 — the printed receipt is Phase 11.
  `settlement` lists the document's own movements in a fixed order; `reversal_settlement` lists
  the reversing movements, or `[]` when the document has none.

## 22. Database changes and migrations (§11 of the brief)

**None.** The Phase 0 schema already carries everything this phase needs:
`exchange_transactions` (with `origin`, `client_event_id`, `device_id`, `cash_session_id`,
`version`, `journal_entry_id`, `reversal_journal_entry_id`, `reversal_of_id`, `reversal_reason`,
`reversed_by`, `reversed_at`, the uniqueness of `transaction_number` and `client_event_id`), the
triggers `NEX03` (status machine), `NEX04` (reversal mirroring), `NEX06` (money-column
immutability), the deferred `ct_exchange_reversal_bound`, `P0001` (no DELETE), the
`next_document_number` routine, `cash_movements` with its generated `signed_amount` and the
deferred `NEX01`, and `v_cash_position`. Head revision stays
`0002_runtime_schema_revision`; the frozen migration files are untouched; `docs/database/schema.sql`
is unchanged and its checksum still matches `CHECKSUMS.txt`.

## 23. Defects discovered during this phase and how they were fixed

No financial defect was found in the Phase 4 posting layer. The defects found were in the new
engine's ordering and in the test harness; each is fixed in code or in the harness, and **no
assertion was weakened, no test deleted, skipped or xfailed**.

* **D5-1 — the document service priced before it validated.** `create_exchange` resolved the
  quote before checking the deal's arithmetic, so a malformed document (a non-positive amount, a
  commission larger than the gross, an amount below the currency's smallest unit) was reported as
  `RATE_NOT_FOUND`, which is the wrong business answer and hides the client's mistake. Fixed by
  computing and validating the amounts *first*, so the deterministic domain refusal names the
  field the operator must fix and the quote is never consulted for an impossible deal.
* **D5-2 — an HTTP-level test worlds shared one branch, making drawer resolution ambiguous.**
  The engine's `inventory_account` resolution (branch-bound → 1000–1099 band → single group-wide)
  correctly refuses to guess when a branch has more than one candidate for a currency. A group of
  tests created HTTP sessions at the seeded `MAIN` branch *and* their own branch-bound drawers, so
  the till could not be resolved and the *engine* was blamed for a *test* setup defect. Fixed in
  the harness: HTTP-level worlds now run on their own branch with a registered device and a login
  on that branch (`http_counter` / `attach_session`), which is also how a real counter is
  provisioned. The resolution order itself is now covered positively as well: a branch with no
  drawer of its own posts out of the group chart band, with the movements carrying that branch.
* **D5-3 — a test that observed the shared database instead of the engine.** The
  retry-after-failure test needed "no rate is in force for this pair", but it used the pair every
  other suite publishes group-wide, so once the whole suite ran in one process the deal was priced
  and the test failed. The engine was right; the test was asserting the state of the database.
  Fixed by moving that test to an isolated pair (`PKR/AFN`, priced only by the test itself and by
  the retry it proves), which keeps the assertion strict.
* **D5-4 — under-funded legacy tests exposed a Phase 4 regression (guard was correct).** Three
  accounting tests from Phase 4 drove a drawer below zero and were refused by the `_carrying_rate`
  guard. Review confirmed the guard is the model: the tests were under-funded, not mis-guarded.
  Fixed in the harness (`fund_drawer`, called inside the scenario transaction), with the guard
  untouched.
* **D5-5 — the reverse endpoint's return value was misunderstood by its own tests.** The lifecycle
  and accounting suites initially asserted that the reverse call returns the *mirror* document;
  the contract is that it returns the **original** (the mirror is a payload field). Fixed in the
  tests; the endpoint was correct and is now documented explicitly (§21) so the contract cannot be
  misread again.
* **D5-6 — a concurrency test had encoded *one* race winner's implementation shape.** The
  cancel-vs-reverse race test required two `REVERSAL` cash movements at the branch. That is the
  shape of a **cancellation** (which reverses in place); when the **reversal** won the race, the
  undo was the mirror document's own movement pair under `EXCHANGE_TRANSACTION`, and the count
  was zero — so the test passed or failed depending on which side of a real race happened to win.
  The engine was right in both cases (the race left one undo, one reversing entry and the positions
  restored either way); the assertion was wrong. Fixed by asserting the *property* instead of one
  branch's shape: exactly one reversing entry, exactly two deal movements, and exactly two undo
  movements — taken from the shape the winner actually produced — whose signature (account,
  currency, signed quantity) is the exact negation of the deal's own pair, with the other shape
  forbidden. Verified by racing eight documents in a throwaway probe (both shapes occurred; every
  one satisfied the property); the probe was deleted and the concurrency suite was then run three
  times green.

## 24. Test catalogue

| Suite | File | Tests | What it proves |
| --- | --- | --- | --- |
| Unit rules | `tests/unit/test_exchange_rules.py` | 44 | arithmetic, refusals, business date, receipt lines, request-schema edges, money helpers |
| Posting | `tests/integration/test_exchange_posting.py` | 31 | creation, quoting, validation, settlement, entry shape, numbering, drawer resolution, idempotency |
| Lifecycle | `tests/integration/test_exchange_lifecycle.py` | 29 | cancel, reverse, state machine, receipt, RBAC matrix, scope, numbering, self-reversal authority |
| Accounting integration | `tests/integration/test_exchange_accounting.py` | 9 | the accounting properties of the whole phase: balance, result identity, parity, undo, re-pricing |
| Concurrency | `tests/integration/test_exchange_concurrency.py` | 13 | real races: inventory, BUY/SELL, branches, customers, duplicates, cancel-vs-reverse |
| — | **total** | **126** | |

Highlights of the accounting-property suite (each one a *property*, not a fixture check):

* a five-deal BUY/SELL × USD/EUR matrix: every document posts exactly one balanced entry and two
  movements, every line carries its own single rate, and the drawer position equals the ledger
  balance for each currency;
* result identity: fee-only buy → `0 + 500`; sell at `71` against carriage `70` → `300 + 200`; a
  deal at the carrying rate → zero result and no income line;
* a six-deal sweep where the ledger's own result equals the branch's actual gain
  (`fx + fee == gained`), `fee == commission`, and `to_amount == compute_exchange_amounts(...)`;
* cancel and reverse return the positions **exactly** as they were, and both documents stay
  visible;
* a later quote cannot re-price a posted document;
* no posted line ever carries a negative quantity.

## 25. Exact test commands and results

Environment: PostgreSQL 16 and Redis 7.4 on the loopback interface, Python 3.11.2 with the
project's development dependencies (`/tmp/venv`), the sandbox's local stack
(`NEXUS_TEST_ADMIN_DSN`, `NEXUS_TEST_REDIS_URL`), no Docker.

```bash
cd apps/api
export PYTHONPATH=. NEXUS_TEST_ADMIN_DSN="postgresql+psycopg://postgres@127.0.0.1:5432/postgres" \
       NEXUS_TEST_REDIS_URL="redis://:nexuslocaldev@127.0.0.1:6379/15"
env -u DATABASE_URL -u DATABASE_MIGRATION_URL -u APP_ENV python -m pytest -q          # whole suite
```

| Run | Command | Result |
| --- | --- | --- |
| Whole suite (Phase 5 tree, final) | `python -m pytest -q` | **1 301 passed, 0 failed, 0 skipped** in 241.92 s |
| Phase 5 exchange suites | `python -m pytest tests/unit/test_exchange_rules.py tests/integration/test_exchange_posting.py tests/integration/test_exchange_lifecycle.py tests/integration/test_exchange_accounting.py tests/integration/test_exchange_concurrency.py -q` | **126 passed** in 49.43 s |
| Concurrency repeated | `python -m pytest tests/integration/test_exchange_concurrency.py -q` (three consecutive runs) | 13 passed in 10.80 / 10.37 / 11.08 s |
| Posting alone | `python -m pytest tests/integration/test_exchange_posting.py -q` | 31 passed in 19.43 s |
| Lifecycle alone | `python -m pytest tests/integration/test_exchange_lifecycle.py -q` | 29 passed in 18.24 s |
| Accounting integration alone | `python -m pytest tests/integration/test_exchange_accounting.py -q` | 9 passed in 9.33 s |
| Unit rules alone | `python -m pytest tests/unit/test_exchange_rules.py -q` | 44 passed in 0.45 s |

Baseline before this phase: **1 173 passed** in 205.42 s (recorded in
`/tmp/phase5_baseline.txt`). The collected suite grew from 1 173 to **1 301** tests (+128), of which
**126** live in the new exchange suites (the remaining two are further parametrised cases on
paths the phase extended). No test was removed, skipped or xfailed.

## 26. Regression of Phases 0–4 (§27)

The whole suite is the regression, and it was run on the phase's tree after the last code change:
**1 301 passed / 0 failed / 0 skipped**, including every Phase 0 invariant test, the Phase 1
foundation tests, the Phase 2 authentication/device/RBAC suites, all Phase 3 master-data suites
and the whole Phase 4 accounting group. The three Phase 4 tests touched by the harness defect
(D5-4) were repaired, not weakened: they now fund the drawer they draw from and assert the same
outcomes as before.

## 27. Ruff and MyPy

| Gate | Command | Result |
| --- | --- | --- |
| Ruff lint | `python -m ruff check .` | **All checks passed** — the only suppressions added are three `S608` annotations on the repository's constant-built SQL (module constants and generated bind names only; every operator value is bound) |
| Ruff format | `python -m ruff format --check app tests seeds scripts` | **146 files already formatted** (3 files were reformatted by this phase and re-verified) |
| MyPy | `python -m mypy app seeds scripts` | **Success: no issues found in 93 source files** |

## 28. Migration and schema gates

* **Fresh migration**: a clean database migrated to `0002_runtime_schema_revision (head)` with the
  Phase 0/1 DDL — no new revision, no frozen file touched.
* **ORM ↔ migrated database** (`python -m scripts.schema_gate orm-db --dsn …nexus_gate_clean`):
  **MATCH** — 31 tables, 341 columns.
* **Reference DDL ↔ migrated database** (`python -m scripts.schema_gate db-db --left …nexus_gate_reference --right …nexus_gate_clean`):
  **MATCH** — 71 checks, 48 triggers, 23 routines, 5 views.
* `docs/database/schema.sql` is unchanged; the reference self-check that the file runs on load
  reported *NEXUS schema self-check passed (30 NUMERIC(30,10) columns, 0 float columns)*.

## 29. Phase 0 invariant suite and seeds

* `tests/invariants/phase0_schema_invariants.sql` on a fresh migrated database:
  **PHASE 0 SCHEMA INVARIANT SUITE: ALL ASSERTIONS PASSED** — ledger totals `debit = credit =
  1 770 000.0000000000`, audit chain valid.
* Seeds on a fresh migrated database: first run `inserted=89`, second run
  `inserted=0 updated=0 unchanged=88`, `--check` identical — idempotent, unchanged by this phase.

## 30. CI results

Recorded in the finalisation commit and in `docs/PROJECT_STATUS.md` §1/§3: the push run and the
pull-request run of the implementation commit, all six jobs, with every step of *Integration
tests and schema gates* green (pytest integration, migration on a clean database, both schema
gates, seed idempotency, Phase 0 invariants) and the compose job's acceptance steps green.

## 31. Limitations

* **L5-1 — printing is not part of this phase.** The receipt endpoint returns the deterministic
  JSON contract; PDF/thermal rendering is Phase 11, and `format=pdf` is refused with 422 rather
  than half-implemented.
* **L5-2 — the offline system is not built.** Only the hooks of §20 exist (client event id,
  device/origin, conflict refusal, byte-identical replay). The outbox, the sync engine and the
  conflict UI are Phase 8/12.
* **L5-3 — the sandbox has no Docker CLI**, so `docker compose up -d` and the containerised
  acceptance steps cannot be executed here; they run in CI (the compose job), and this phase adds
  no compose change.
* **L5-4 — local interpreter is Python 3.11.2** while the target is 3.12+; the code is written for
  3.12 and the CI runs it there.
* **L5-5 — no `psql`/`redis-cli` on `PATH`**: the gates here are run through the driver-level
  scripts (`scripts/schema_gate.py`, the invariant SQL through the bundled client), which is the
  same code CI runs.
* **L5-6 — a document's business date is its creation date at the branch** (the approved model:
  `created_at` in the branch timezone). A deliberately backdated deal keeps its date on the entry's
  `transaction_date` and in the document number, while the document view reports the business
  date the branch was working on. Backdating the document itself is not part of the approval.
* **L5-7 — the exchange document carries no customer *balance* account** (the approved schema has
  none for this operation); the customer side is a reference plus scope enforcement (§14).

## 32. Remaining risks

* **R5-1 — drawer resolution is configuration-sensitive.** A branch with two cash accounts for the
  same currency is genuinely ambiguous and is refused (`AMBIGUOUS_CASH_ACCOUNT`, 422) rather than
  guessed. Operations must give a branch one till per currency; the refusal message names the
  branch, the currency and the candidates so the fix is obvious.
* **R5-2 — the shared document series is global per `(scope, period)`.** Numbers are unique and
  contiguous, but a busy group will see interleaved numbers across branches. This is the approved
  Phase 0 architecture; per-branch series would be a schema decision, not a Phase 5 one.
* **R5-3 — quotation quality is a policy risk, not a code risk.** The engine refuses a rate
  outside tolerance and refuses a missing quote; it cannot tell an operator that the *published*
  rate is wrong. Rate governance (who may publish, limits, spreads) stays with Phase 3's RBAC and
  Phase 7's reports.

## 33. Acceptance checklist

| # | Requirement | Evidence |
| --- | --- | --- |
| 1 | BUY/SELL lifecycle implemented | posting + lifecycle suites, §7 |
| 2 | Validation before pricing and posting; deterministic error codes | §7, D5-1, unit rules suite |
| 3 | Quoted rate selection, validity, snapshot, reproducibility | §8, accounting suite |
| 4 | Gross / commission / net settlement computed in one place | §9, `compute_exchange_amounts`, 44 unit rules |
| 5 | Profit/result reproducible from persisted data | §11, six-deal sweep assertion |
| 6 | Multi-currency movement (both sides), functional valuation | §7, §9, EUR/USD cross test (7 455 debit = credit) |
| 7 | Journal creation **only** through `AccountingService` | §12, module docstrings, static audit |
| 8 | Document references, audit rows, reversal compatibility | §18, lifecycle suite |
| 9 | Idempotency (sequential, duplicate, changed payload, concurrent, retry, rollback) | §16, posting + concurrency suites |
| 10 | Authorization and cross-branch attack refusal | §18, role matrix and scope tests |
| 11 | Concurrency protection with real transactions | §17, 13 concurrency tests |
| 12 | Inventory never negative; races measured | §13, `NEX01` backstop, delta assertions |
| 13 | State machine enforced in domain and database | §15, `NEX03`/`NEX04`/`NEX06`/`P0001` tests |
| 14 | Numbering through `next_document_number`, no duplicates, rollback-safe | §19, posting suite |
| 15 | Offline hooks only, no Phase 8 work | §20 |
| 16 | Six API endpoints per contract, correct statuses and envelope | §21 |
| 17 | No schema change, no frozen migration touched, gates MATCH | §22, §28 |
| 18 | Full regression, ruff, mypy, invariants, seeds | §25–§29 |
| 19 | No test deleted, skipped or xfailed; no assertion weakened | §23, §25 |

## 34. Statement

The exchange engine is implemented, tested, documented and verified as one commit on
`arena/01a090c5-nexus-exchange-erp`. The full suite is **1 301 passed / 0 failed / 0 skipped**;
Ruff, Ruff-format and MyPy are clean; both schema gates are MATCH on a freshly migrated database;
the Phase 0 invariant suite passes; the seeds are idempotent; and no migration, schema file or
frozen Phase 0–4 artefact was changed.

**PHASE 5 — READY FOR REVIEW**

**PHASE 6 — NOT STARTED**

Only the human reviewer may mark a phase APPROVED. Phase 5 stops here for independent
Architect/Gatekeeper review; nothing of Phase 6 (cash sessions) has been started.
