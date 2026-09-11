# NEXUS EXCHANGE ERP — Accounting Model

| Field | Value |
| --- | --- |
| Document ID | `ARCH-ACC-001` |
| Version | 1.0 (Phase 0) |
| Status | **Proposed — pending Phase 0 approval** |
| Owner | Accounting domain |
| Last updated | 2026-09-11 |
| Related | `docs/database/schema.sql`, `docs/database/SCHEMA.md`, `docs/api/API_CONTRACT.md` |

> **خلاصه فارسی** — این سند موتور حسابداری را تعریف می‌کند: واحد پول وظیفه‌ای (Functional Currency)، کدینگ حساب‌های پیش‌فرض، قواعد ثبت برای خرید/فروش ارز، کمیشن، صندوق، هزینه، حواله و برگشت معامله (Reversal). قواعد کلیدی: هر ثبت باید `SUM(debit) = SUM(credit)` باشد، اسناد حسابداری Append-Only هستند، اصلاح فقط با برگشت انجام می‌شود، و سود FX از اختلاف «نرخ معامله» و «بهای تمام‌شده میانگین موزون» محاسبه می‌شود. همه این قواعد در سطح PostgreSQL نیز با CHECK/Trigger تضمین شده‌اند.

---

## 1. Purpose and scope

This document is the single authority for **how money moves through the ledger**. Every service in `apps/api/app/services` implements exactly these postings; every report derives from them. If code and this document disagree, the document and the tests (`tests/invariants`, `apps/api/tests/integration`) decide.

## 2. Terminology

| Term | Meaning |
| --- | --- |
| **Functional currency** | The single base currency in which the ledger is balanced and P&L is presented. Exactly one currency row has `is_base = TRUE` (enforced by `ux_currencies_single_base`). Seeded as **AFN**. |
| **Transaction currency** | Any non-base currency involved in a deal (`currency_id` on a line). |
| **Inventory account** | An asset account holding a currency position (e.g. `1020 Cash USD`). |
| **Carrying rate** | Weighted-average functional value per unit of a currency held in an inventory account. |
| **Gross** | `from_amount × exchange_rate` (see §4). |
| **Commission** | Fee retained by the business; always expressed in `to_currency`. |
| **Business date** | Branch-local calendar date derived from `branches.timezone`; used for document numbering and daily reports. Storage is always UTC. |

## 3. Money semantics (the rule that makes the ledger balance)

`journal_lines.debit` / `journal_lines.credit` are **amounts in the functional currency**. `currency_id` records which currency the account holds, and `exchange_rate` records functional units per 1 unit of that currency. Therefore:

* `SUM(debit) = SUM(credit)` is meaningful **inside a single journal entry** (PART 12) — this is enforced at COMMIT by `ct_journal_lines_balanced_*`.
* The foreign quantity of a line is derived, not stored twice: `foreign_amount = (debit + credit) / exchange_rate` (generated column).
* `cash_movements.amount` is the **physical quantity** in its own currency — what a cashier counts. Cash position (`v_cash_position`) is therefore physical, while the ledger is functional. The two are reconciled through `exchange_rate`.

Worked example: a line `Dr Cash USD, debit = 70000.0000000000, currency = USD, exchange_rate = 70` means "we received USD 1,000.0000000000 whose functional value is AFN 70,000".

## 4. Rate convention and direction semantics

**Arithmetic (one rule, always):**

```
gross_to_amount = from_amount × exchange_rate          (exchange_rate = to per 1 from)
net_to_amount   = gross_to_amount − commission         (customer's proceeds when they receive `to`)
```

This matches the PART 29 example (`1000.00 × 70.0 = 70000.00`). `exchange_rate > 0` is enforced on both `exchange_transactions` and `journal_lines`.

**Direction is stated from the business's own books (`transaction_type`):**

| Type | Business receives | Business gives | Applied quoted rate | Counterparty |
| --- | --- | --- | --- | --- |
| `BUY` | `from_amount` of `from_currency` | `net_to_amount` of `to_currency` | `buy_rate` of the resolved pair | Customer sells foreign currency |
| `SELL` | `gross_to_amount` of `to_currency` | `from_amount` of `from_currency` | `sell_rate` of the resolved pair | Customer buys foreign currency |

The service resolves `buy_rate`/`sell_rate` with `resolve_exchange_rate(from_currency, to_currency, branch, now)` (branch quote wins over the global quote; newest `effective_at ≤ now` wins). The applied rate is stored on the transaction and must fall within the configured tolerance of the resolved quote, otherwise the request is rejected with `RATE_OUT_OF_TOLERANCE` (fat-finger protection).

## 5. Chart of accounts (created by the Phase 1 seed)

| Code | Name | Type | Normal | Currency scope |
| --- | --- | --- | --- | --- |
| 1000 | Cash AFN | ASSET | DEBIT | AFN (base) |
| 1001…1099 | Cash — one inventory account per active currency | ASSET | DEBIT | per currency |
| 1100 | Cash in Transit / Partner Receivable | ASSET | DEBIT | base |
| 1200 | Customer Receivable | ASSET | DEBIT | base |
| 2000 | Customer Advance (unearned) | LIABILITY | CREDIT | base |
| 2100 | Transfer Payable | LIABILITY | CREDIT | per currency |
| 3000 | Owner Capital | EQUITY | CREDIT | base |
| 3100 | Owner Drawings | EQUITY | DEBIT | base |
| 3200 | Retained Earnings | EQUITY | CREDIT | base |
| 4000 | FX Gain / Loss | REVENUE | CREDIT | base |
| 4010 | Commission Income — Exchange | REVENUE | CREDIT | base |
| 4020 | Transfer Fee Income | REVENUE | CREDIT | base |
| 4030 | Other Income | REVENUE | CREDIT | base |
| 5000…5090 | Operating expenses (salaries, rent, utilities, communication, consumables, bank charges, cash short/over) | EXPENSE | DEBIT | base |
| 5100 | Other Expenses | EXPENSE | DEBIT | base |
| 6000 | Opening Balance Offset | EQUITY | CREDIT | base |

Inventory accounts are branch-scoped when the branch keeps its own cash (`accounts.branch_id`); control and P&L accounts are branch-scoped as well so that inter-branch results are separable. Parent accounts (`parent_id`) carry `is_postable = FALSE` and exist only for grouping.

## 6. Posting rules

Notation: `qty` = quantity in a currency, `func` = functional (AFN) value. Every entry below is created by `AccountingService` **inside the same database transaction** as the business document (PART 20).

### 6.1 Opening balances (`reference_type = 'OPENING_BALANCE'`)

```
Dr  Cash-<CUR>            func = qty × opening_rate      (currency = CUR, rate = opening_rate)
Cr  6000 Opening Offset   func = qty × opening_rate      (currency = AFN, rate = 1)
```
plus a `cash_movements` row of type `OPENING` with `amount = qty`. Opening rates are captured in the request, audited, and may not be edited afterwards.

### 6.2 Exchange — BUY (business acquires foreign currency)

Inputs: `F` (from, foreign), `T` (to, base or other), `from_amount`, `rate`, `commission` (in `T`).
`gross = from_amount × rate`; `paid = gross − commission`.

```
Dr  Cash-<F>                func = gross      (currency = F, rate = rate)
Cr  Cash-<T>                func = paid       (currency = T, rate = 1 for base)
Cr  4010 Commission Income  func = commission (currency = T)
```
Cash movements: `IN from_amount` in `F`; `OUT paid` in `T`. Acquired currency is measured at its transaction price (`rate`), which is the correct initial measurement; the commission is recognized immediately as revenue.

**Worked example** — buy 1,000.00 USD at 70.00, commission 500.00 AFN:
`gross = 70,000.00`, `paid = 69,500.00`.
`Dr Cash USD 70,000.00` / `Cr Cash AFN 69,500.00` / `Cr 4010 500.00`. Position: USD +1,000.00, AFN −69,500.00.

### 6.3 Exchange — SELL (business disposes foreign currency)

Inputs: `F` (from, foreign), `T` (to, base or other), `from_amount`, `rate`, `commission` (in `T`).
`gross = from_amount × rate`; `net_fx = gross − commission`; `cost = from_amount × carrying_rate(F)`.

```
Dr  Cash-<T>                func = gross                 (currency = T, rate = 1 for base)
Cr  Cash-<F>                func = cost                  (currency = F, rate = carrying_rate)
Dr/Cr 4000 FX Gain / Loss   func = |net_fx − cost|       (Cr when net_fx > cost, Dr when < cost)
Cr  4010 Commission Income  func = commission            (currency = T)
```
Cash movements: `IN gross` in `T` (the customer pays the full gross; the commission stays in the drawer as revenue) and `OUT from_amount` in `F`. Physical flow must always equal ledger flow — the BUY case withholds the commission from the payout (`OUT paid`), the SELL case collects it inside the receipt (`IN gross`).

`carrying_rate(F)` = functional balance of the branch's `Cash-<F>` account ÷ its foreign quantity, taken from the ledger (not from a cache) at posting time. When the position is zero the entry is rejected (`INSUFFICIENT_BALANCE`) — the physical cash constraint `ct_cash_movements_non_negative` (SQLSTATE `NEX01`) independently guarantees a sale can never dispense currency the branch does not hold.

**Worked example** — sell 1,000.00 USD at 70.00 with commission 500.00 AFN, carrying rate 70.00:
`gross = 70,000.00`, `net_fx = 69,500.00`, `cost = 70,000.00` → FX loss 500.00.
`Dr Cash AFN 70,000.00` / `Cr Cash USD 70,000.00` / `Dr 4000 500.00` / `Cr 4010 500.00` → balanced at 70,500.00 on both sides.

### 6.4 Cash in / cash out (`reference_type = 'CASH_MOVEMENT'`)

| Movement | Entry |
| --- | --- |
| `IN` (deposit, capital injection, collection) | `Dr Cash-<CUR> func = amount × rate` / `Cr <source account> func = amount × rate` |
| `OUT` (withdrawal, supplier payment, owner drawing) | `Dr <target account>` / `Cr Cash-<CUR>` |
| `ADJUSTMENT` (+1) | `Dr Cash-<CUR>` / `Cr 5090 Cash Short / Over` |
| `ADJUSTMENT` (−1) | `Dr 5090 Cash Short / Over` / `Cr Cash-<CUR>` |
| `OPENING` | §6.1 |
| `CLOSING` | **No journal entry** — a reconciliation snapshot written to `cash_session_lines`; any discrepancy is posted as `ADJUSTMENT` |

`IN`/`OUT` require an explicit counter-account (`source_account_id` / `target_account_id`); the API rejects a cash movement without one (`CASH_COUNTER_ACCOUNT_REQUIRED`), so cash can never appear from nowhere.

### 6.5 Expense (`reference_type = 'EXPENSE'`)

```
Dr  <expense account for category>   func = amount × rate   (currency = expense currency)
Cr  Cash-<CUR>            (paid immediately)  func = amount × rate
Cr  2000 Customer Advance / payable accounts  (accrued expense)
```
Expenses are posted once (`ux_journal_entries_one_per_reference`); cancellation posts a mirror reversal, never a deletion.

### 6.6 Transfers (`reference_type = 'TRANSFER'`)

Two-step lifecycle with explicit liability recognition:

**On approve (collection from sender)** — `amount` + `commission` collected (cash movements: `IN amount`, `IN commission`):
```
Dr  Cash-<CUR>              func = amount × rate
Dr  Cash-<CUR>              func = commission × rate
Cr  2100 Transfer Payable   func = amount × rate
Cr  4020 Transfer Fee Income func = commission × rate
```

**On pay (payout to receiver)** — same currency:
```
Dr  2100 Transfer Payable   func = amount × rate
Cr  Cash-<CUR>              func = payout_amount × rate
```
Cross-currency payout adds `Dr/Cr 4000 FX Gain / Loss` for the difference between `amount × rate` (liability derecognized) and `payout_amount × rate_payout` (cash out) — the FX result of the payout.

**On cancel** — before payout, the collection entry is reversed (§7). After payout, cancellation is not permitted (`TRANSFER_ALREADY_PAID`, HTTP 409); the correction path is a new offsetting transfer.

### 6.7 Reversal (never a deletion)

```
Reversal journal entry  reference_type = 'REVERSAL', reversal_of_id = original entry,
                        one line per original line with debit/credit swapped
Reversal cash movements same movement_type family, opposite direction, reference_type = 'REVERSAL'
Original transaction    status → REVERSED (reversed_at, reversed_by, reversal_reason required)
```
The reversing `exchange_transactions` row mirrors currencies and amounts (`nexus_validate_exchange_reversal`, SQLSTATE `NEX04`) and the original may be reversed at most once (`ux_exchange_transactions_reversed_once`). Reversal of an already-cancelled or already-reversed document is rejected (`409 ALREADY_REVERSED` / `406`-class domain error mapped per `API_CONTRACT.md`).

## 7. Invariants and where they are enforced

| # | Invariant (PART 49) | Database enforcement | Test |
| --- | --- | --- | --- |
| I-1 | `SUM(debit) = SUM(credit)` per entry, ≥ 2 lines, single-sided lines, non-negative amounts | `ct_journal_lines_balanced_insert/update/delete` (deferred), `ck_journal_lines_single_sided`, `ck_journal_lines_debit_non_negative`, `ck_journal_lines_credit_non_negative` | `phase0_schema_invariants.sql` §I-1 (6 assertions) |
| I-2 | Posted balance = Σ ledger movements; the balance cache never drifts | `rebuild_account_balances()`, statement trigger `trg_journal_lines_balance_cache` | §I-2 |
| I-3 | Cancelled/Reversed ≠ deleted | `nexus_forbid_mutation` triggers on `journal_lines`, `cash_movements`, `exchange_transactions`, `transfers`, `expenses`, `audit_logs`; no `DELETE` grant for `nexus_app` | §§I-3, I-4 |
| I-4 | A `REVERSED` transaction has a bound reversal row | `ct_exchange_reversal_bound` (deferred), `nexus_validate_exchange_reversal`, `ux_exchange_transactions_reversed_once` | §I-4 |
| I-5 | Cash position never negative | `ct_cash_movements_non_negative` (deferred, SQLSTATE `NEX01`) | §I-5 |
| I-6 | Audit log is append-only and tamper-evident | `trg_audit_logs_chain`, `nexus_forbid_mutation`, privilege revokes, `verify_audit_chain()` | §I-6 |
| I-7 | No duplicate posting / duplicate offline event | `ux_journal_entries_one_per_reference`, `ux_exchange_transactions_client_event`, `ux_idempotency_keys_scope` | §§I-7 |
| I-8 | Money is always `NUMERIC`, never float | 30 × `NUMERIC(30,10)`; schema self-check DO block | §I-8 |

**Ledger integrity over presentation (I-2):** `account_balances` is a *rebuildable cache*. Trial balance and any legal report read `journal_lines` (`v_trial_balance`), never the cache.

## 8. Reconciliation identities (asserted by the accounting test suite in Phase 4)

```
Σ debit = Σ credit                                    (per entry and ledger-wide)
cash position (per branch, currency)  = v_cash_position
ledger balance (per account, currency) = v_account_balances after rebuild_account_balances()
net income = Δ(cash) + Δ(inventory at carrying value) − Δ(capital contributions) − Δ(liabilities)
net income = Σ REVENUE − Σ EXPENSE          (from v_trial_balance)
```
The last two identities are asserted numerically in `apps/api/tests/integration/test_accounting_integrity.py` (Phase 4, implemented) against a realistic day built by `apps/api/tests/accounting_helpers.py` — openings in three currencies, two exchanges (one gaining, one losing, one with commission), an expense and an adjustment — so the identities are checked on a ledger that has been *used*, not on an empty one.

## 9. Rounding, precision and presentation

| Concern | Policy |
| --- | --- |
| Storage | `NUMERIC(30,10)` for every amount, rate, quantity |
| Intermediate computation | Python `Decimal` at full precision; no rounding between legs |
| Final amounts | Quantized with `ROUND_HALF_UP` to `currencies.decimal_places` (2 for all seeded currencies; the column exists for 0/3-dp currencies) |
| Rates | Stored to 10 dp; displayed with the business's configured rate precision (default 4) |
| Report totals | Summed in `Decimal` from stored rows, quantized to the reporting currency's decimal places at render time |
| Never | `float`, `double`, JavaScript `Number` as a source of truth (PART 62) — a Dart `Money` value type carries a `Decimal`-typed amount across the wire as a **string** |

## 10. Multi-branch accounting

Each branch posts its own journal entries (`journal_entries.branch_id`). Branch inventory accounts are distinct rows (`accounts.branch_id`), so a branch can never spend another branch's cash. Inter-branch transfers (Phase 9) post mirrored entries in both branches plus a 1100 *Cash in Transit* clearing entry, keeping the group trial balance balanced at every instant.

## 11. Periods and closing

* There is no hard period lock in the MVP. Daily closing (`POST /api/v1/cash/close`) produces an auditable reconciliation snapshot per branch and currency.
* Month-end and year-end closing, plus a hard `financial_periods` lock (rejecting postings with `transaction_date` inside a closed period) are scheduled for Phase 12 as a *documented, migration-backed* feature — not implemented as a stub earlier.
* FX revaluation of open currency positions (mark-to-market at period end) is deliberately **out of scope for the MVP**: the business is a flow business whose inventory turns over within the day; the policy must be chosen by the customer's accountant before it is implemented, and inventing it unilaterally would produce numbers nobody can defend.

## 12. Traceability

| Master prompt | Section |
| --- | --- |
| PART 12 | §3, §7 |
| PART 20, PART 21 | §6 |
| PART 22 | §6.7, §13.4 |
| PART 40 | §13.3 |
| PART 46 | §6, §7 (only `AccountingService` writes the ledger) |
| PART 49 | §7, §8 |
| PART 62 | §9 |
| PART 63 | §6 (services are the only ledger writers) |

## 13. Phase 4 implementation notes

This section records how the model above is implemented in
`apps/api/app/services/accounting_service.py`. It is documentation of *deployed*
behaviour; every claim here is pinned by a test named in
[`../phases/PHASE4_REPORT.md`](../phases/PHASE4_REPORT.md) §15.

### 13.1 The single writer

`AccountingService` is the only code that inserts into `journal_entries` /
`journal_lines` (PART 46). Its public surface is exactly the roadmap's:
`create_journal_entry`, `validate_balanced_entry`, `post_exchange`,
`post_cash_movement`, `post_expense`, `reverse_journal_entry` / `reverse_transaction`,
`get_journal_entry`, `list_journal_entries`, `get_account_balance`,
`get_trial_balance`. Every posting path funnels through one private `_post`, so
validation, locking, insertion, audit and idempotency cannot diverge between document
types.

### 13.2 Posting lifecycle (one database transaction)

```
authorise (permission for the reference type)      → PERMISSION_DENIED (audited)
assert branch scope (actor's branch, or group-wide) → FORBIDDEN_SCOPE   (audited)
lock every touched account row, in (account, currency) order
resolve context (base currency, stored positions, carrying rate)
validate the exchange direction (document paths)   → EXCHANGE_DIRECTION_INVALID
guard the inventory positions (generic door)       → INSUFFICIENT_BALANCE
validate money (Decimal, exact 10-dp scale, within NUMERIC(30,10) bounds)
validate the entry (≥ 2 lines, single-sided, debit = credit exactly)
claim the idempotency key (if the endpoint requires one)
insert entry + lines (lines sorted the same way the lock was taken)
write one audit row (JOURNAL_POSTED) carrying the numbers and the rate snapshot
COMMIT — the deferred balance constraint (NEX02) and the non-negative cash
         constraint (NEX01) are evaluated here, not earlier
```

Order matters and is deliberate: the account rows are locked **before** any read that
the posting depends on (`position()`, the carrying rate). A lock taken after the read it
protects would let two concurrent transactions both read a pre-transaction position and
both post a disposal — which is exactly the defect
`test_accounting_concurrency.py::TestConcurrentDisposals` reproduces. Every public
posting method therefore locks the *whole* set of accounts it may touch — including the
FX account a residual may add — at the start of its transaction, and the guard that reads
a position (§13.7) reads it under that lock.

### 13.3 Idempotency

`POST`-shaped money movements accept `Idempotency-Key` (PART 40). The key is claimed
inside the ledger transaction and completed with the serialized response, so:

* the same key with the same request body replays the stored response
  (`Idempotency-Replayed: true`, original status code);
* the same key with a *different* body is refused `409 IDEMPOTENCY_KEY_REUSED` — a key
  never silently swallows a different request;
* a concurrent duplicate gets `409 IDEMPOTENCY_IN_PROGRESS` with `Retry-After`;
* a failed attempt releases the key (`FAILED` is reclaimable), so a client may retry.

The request fingerprint is built from the *plan* (document type, reference, branch,
quantized amounts, rates, dates) — never from a Python object graph — so two requests
that mean the same thing hash the same way regardless of formatting (`70` and `70.00`).

### 13.4 Reversal lifecycle

`reverse_journal_entry` (and `reverse_transaction`, which resolves a document to its
journal) inserts a **mirror** entry: every account, currency and rate is kept and debit
is swapped with credit, so the *quantities* return as well as the functional amounts.
The original is never touched. The reversal carries `reversal_of_id` (a self-FK with
`ck_journal_entries_no_self_reversal`), a `REVERSAL` reference type pointing at the
original entry, its own audit row, and it may not be dated before the entry it reverses.
`ux_journal_entries_reversed_once` makes a second reversal impossible even under
concurrency, and the service refuses to reverse a reversal.

### 13.5 Branch scope and authorisation

Posting authority is per document type (`POSTING_AUTHORITY`): `EXCHANGE_TRANSACTION` →
`exchange.create`, `CASH_MOVEMENT` → `cash.create`, `EXPENSE` → `expenses.create`,
`TRANSFER` → `transfers.create`, `MANUAL_ADJUSTMENT` / `OPENING_BALANCE` →
`accounts.manage`. Authority and object scope are separate checks: a `MANAGER` holds
`cash.create` but is still refused another branch's ledger, and a reader outside the
actor's scope receives **404**, not 403, so the ledger does not leak the existence of
other branches' entries. Every refusal is audited as `LEDGER_POSTING_DENIED` in its own
transaction, so it survives the rollback of the request that caused it.

### 13.6 Reports read the immutable table

`get_trial_balance` and `get_account_balance` read `journal_lines` (through
`v_trial_balance` / `v_account_balances`), never the rebuildable cache, and
`rebuild_account_balances()` is asserted to be a no-op on a ledger whose cache is
current. Every response states its `source` so a reader can tell which immutable table
produced the numbers.

### 13.7 Boundary guards (Gate Review regression)

Two boundary rules were added after the independent Gate Review of this phase; both are
described in full, with their evidence, in
[`../phases/PHASE4_REPORT.md`](../phases/PHASE4_REPORT.md) §40.

**Exchange direction.** §6.2 and §6.3 both describe the *delivered* currency as foreign,
so `post_exchange` refuses a pair that cannot describe a deal before it prices anything:
the same currency on both sides (`SAME_CURRENCY`) or the functional currency as the
delivered side (`FUNCTIONAL_CURRENCY_NOT_DELIVERABLE`), both `422
EXCHANGE_DIRECTION_INVALID` naming the offending field. Without this the SELL case *posted*
a balanced but impossible entry, and the BUY case was refused only accidentally, by the
disposal guard of the *other* drawer reading an empty position (`INSUFFICIENT_BALANCE`).

**Inventory positions through the generic door.** `create_journal_entry` sets
`guard_inventory` on its posting plan, and `_post` runs `_assert_inventory_positions`
before the first insert: for every **inventory** account (§2: an asset account bound to a
currency) the plan's lines contribute `(debit - credit) / rate` — the expression
PostgreSQL uses to generate `foreign_amount` — and a negative outcome is refused with the
same vocabulary the SELL path uses (`NO_POSITION`, or `QUANTITY_EXCEEDED` with the
shortfall). Invariant I-5 therefore holds for the ledger itself, not only for the physical
`cash_movements` row.

*Scope of the guard.* Document paths keep their own rules, because their physical side is
written by the phase that owns the document and `ct_cash_movements_non_negative`
(`NEX01`) is the authority there: a BUY pays out of a drawer whose cash movement is
written in the same transaction, and a reversal mirrors an entry that already exists.
What the generic door may not do is move a position that no document vouches for — which
is the hole the Gate Review found (a manual entry had driven a drawer to
`-7.1428571429` units with a balanced, immutable, audited journal to prove it).

## 14. Rate snapshot — protection review and decision D-4-1

A posting must answer two different questions after the fact: *what rate priced this
entry?* and *which quote was that rate?* Phase 4 reviewed how each is protected, because
the prompt asks explicitly whether the rate snapshot/reference needs stronger
**database-level** protection than the append-only application rules documented in
Phase 3.

**Decision: no schema change.** The snapshot is already structurally immutable, and the
one change that sounds stronger — a foreign key from the journal line to the quote row —
would be weaker. The reasoning:

| Question | Where the answer lives | Protection |
| --- | --- | --- |
| What rate priced the entry? | `journal_lines.exchange_rate` — the number is **copied** into the line; `foreign_amount` is a `GENERATED ALWAYS` column derived from it | The line is append-only: `trg_journal_lines_no_update/no_delete` → `P0001 NEXUS_APPEND_ONLY`, plus `REVOKE UPDATE, DELETE` from `nexus_app`. Nothing can re-price it, and no other table can either: `journal_lines` has **no** reference to `exchange_rates` at all |
| Which quote was it? | `audit_logs.new_data.rate_snapshot` (`rate`, `exchange_rate_id`, `from_currency_id`, `to_currency_id`, `branch_id`, `effective_at`, `source`) plus the per-line `exchange_rate` in `line_detail` | `audit_logs` is append-only and hash-chained (`nexus_forbid_mutation`, `nexus_audit_chain`, `verify_audit_chain()`), and `nexus_app` has no `UPDATE`/`DELETE` on it |
| Can a later quote change history? | It cannot: `exchange_rates` is deliberately mutable (a quote is a market observation, not money — it carries no append-only trigger), but nothing the ledger stores depends on it | `test_accounting_integrity.py::TestTheRateSnapshotIsProtected::test_re_pricing_every_quote_for_the_pair_cannot_re_price_history` re-prices **every** quote for the pair the entry used and asserts the posted lines are byte-identical |
| Is the recorded reference resolvable? | `exchange_rate_id` names the quote row | Asserted by `...::test_the_audit_trail_records_the_rate_and_the_quote_behind_it`, which also pins the audit's per-line snapshot against the immutable lines |
| Could a stronger constraint hurt? | A FK from `journal_lines` to `exchange_rates` would give the quote row veto power over financial history (blocking its own replacement/cleanup) and would imply that the referenced row is authoritative for the amount — while the amount is the copied number. It would also violate the frozen-schema rule for no benefit | Recorded as decision D-4-1 in `../phases/PHASE4_REPORT.md` §13 |

The residual risk is therefore *provenance*, not arithmetic: if a caller posts a rate
without naming the quote it came from, the audit row records the number but
`exchange_rate_id` is `NULL`. Phase 5's `POST /exchange` resolves the quote through the
Phase 3 `RateService` and passes its id, which the service accepts and the audit row
pins; the parameter exists and is tested today.
