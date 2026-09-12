# PHASE 6 REPORT — Cash management (drawers, shifts, movements, variance, reversal)

| Field | Value |
| --- | --- |
| Document ID | `PHASE6-REPORT-001` |
| Phase | 6 — Cash management (roadmap: `docs/architecture/ROADMAP.md`) |
| Status | **READY FOR REVIEW** (only the human reviewer may mark it APPROVED) |
| Starting commit | `9d67582` (Phase 5 finalisation — implementation `40945b1` plus the documentation commit that pins it; the frozen Phase 5 lineage) |
| Implementation commit | *pinned in the finalisation commit of this phase* (visible in `git log`) |
| Branch | `arena/01a090c5-nexus-exchange-erp` |
| Predecessor | [`PHASE5_REPORT.md`](PHASE5_REPORT.md) (exchange engine) and [`PHASE4_REPORT.md`](PHASE4_REPORT.md) (the authoritative posting layer) |
| Successor | Phase 7 (reports) — **not started, not authorised** |

> **خلاصهٔ فارسی** — فاز ۶ مدیریت نقدی را می‌سازد: جلسه‌های صندوق (باز/بسته)، دریافت و
> پرداخت نقدی، اصلاح کسری/اضافه، وارونه‌سازی حرکات، تطبیق شمارش فیزیکی با دفتر، کنترل
> موجودی و قفل‌گذاری، idempotency، ممیزی، مجوزها و جداسازی شعبه — همه با `Decimal`/`NUMERIC`
> و همه از طریق `AccountingService` به‌عنوان تنها مسیر ثبت؛ بدون هیچ تغییری در طرح‌وارهٔ
> منجمدشدهٔ فاز ۰.

---

## 1. Phase name and scope

Phase 6 builds **cash management**: the control layer that owns a drawer across a working shift
and makes every physical movement of money an auditable fact beside the ledger.

In scope, exactly as the phase brief and the roadmap list it:

* the cash service (`app/services/cash_service.py`) — shift lifecycle (open, close), movement
  recording (in, out, adjustment), movement reversal, variance derivation, validation,
  authorization, branch scoping, audit, idempotency and locking;
* the cash store (`app/repositories/cash.py`) — session and movement queries, the position
  projections (`v_cash_position` beside the drawer's ledger balance), the reversal linkage read
  and the paginated/filterable listings;
* the wire and response schemas (`app/schemas/cash.py`) and the twelve endpoints of
  `docs/api/API_CONTRACT.md` §9.4;
* the extensions the cash module legitimately needed from the posting layer
  (`resolve_rate` and the ledger-decided movement rate, `guard_inventory` on cash movement
  plans, the movement-id fix in `record_cash_movements`, the public money validators) — additions
  only, no second posting path;
* the test suites: unit rules, shift lifecycle, movements, reversal/accounting integration and
  real-PostgreSQL concurrency.

Out of scope and deliberately untouched: reports and dashboards (Phase 7), expenses,
receivables and payables, the `transfers` product (Phase 8/10 — **drawer-level cash transfers are
in scope here**, see §11), the offline sync engine (Phase 8/12 — this phase contributes hooks
only), printing (Phase 11), Flutter, and any change to the approved Phase 0 schema, its frozen
migrations, `docs/database/schema.sql` or the Phase 4 posting model.

## 2. Authorisation and constraints under which this phase ran

* **One authoritative posting path.** Every financial write of this phase goes through
  `AccountingService`: `post_cash_movement` for a movement's journal entry,
  `reverse_journal_entry` for its undo, `inventory_account` for drawer resolution. The cash
  module writes `cash_movements` as *evidence* — never a balance the ledger does not carry — and
  writes no `journal_entries`, `journal_lines`, account balance or position column itself.
* **The schema is frozen.** No migration was written and none was needed. The existing tables
  (`cash_sessions`, `cash_session_lines`, `cash_movements`), their partial unique indexes, the
  generated `signed_amount` column, the non-negative trigger (`NEX01`) and the position views
  (`v_cash_position`, `v_currency_position`) are used exactly as Phase 0 approved them.
* `Decimal`/`NUMERIC` only; no `float`, no Python `round()` where financial rounding semantics
  apply (PARTs 20, 62).
* Deterministic domain error codes; invalid business semantics never reach accounting, and a
  refusal writes nothing.
* Offline compatibility as contracts and hooks (idempotency, `client_event_id`, deterministic
  references, server-authoritative timestamps), never as a second implementation.
* Full regression of Phases 0–5 before the phase may be declared complete; no test deleted,
  skipped, weakened or xfailed; no filler tests.
* Do not mark the phase APPROVED; do not start Phase 7.

## 3. Starting commit

`9d67582` — the Phase 5 finalisation commit (implementation `40945b1`, plus the documentation
commit that pins it). At that commit the repository had **1 301 passing tests** (548 unit,
753 integration; 0 failed, 0 skipped), `ruff check .` clean, `ruff format --check .` green over
156 files, `mypy app seeds scripts` clean over 97 source files, both schema gates MATCH, the
Phase 0 invariant suite passing on a fresh migrated database, and seeds idempotent
(89 / 88 / 88).

The tree this phase started from also carried the Phase 5 review state: pull request #1 open and
mergeable, Phase 5 **READY FOR REVIEW**, Phase 6 not started. Phase 5 was not modified, and no
file of the frozen phases was deleted, renamed or weakened.

## 4. Final commit

The implementation, tests and documentation of this phase are **one commit** on
`arena/01a090c5-nexus-exchange-erp`; the documentation-only finalisation commit that follows it
pins the exact hash, the diff stat and the CI run identifiers, and `docs/PROJECT_STATUS.md` §1
repeats them. The pattern is the one Phases 2–5 used, and no source file differs between the
implementation commit and that finalisation commit.

| Field | Value |
| --- | --- |
| Commit | *pinned in the finalisation commit of this phase* |
| Parent | `9d67582` — *docs(phase5): pin the implementation commit and record the green CI runs* |
| Contents | implementation + tests + this report + `PROJECT_STATUS.md` + `API_CONTRACT.md` (see §5) |
| Branch | `arena/01a090c5-nexus-exchange-erp` (pushed; pull request #1) |
| CI (push) | recorded in the finalisation commit |
| CI (pull request) | recorded in the finalisation commit |

## 5. Files created and modified

| Kind | File | Lines |
| --- | --- | --- |
| production | `app/services/cash_service.py` (new) | 2 211 |
| production | `app/repositories/cash.py` (new) | 681 |
| production | `app/api/v1/cash.py` (new) | 612 |
| production | `app/schemas/cash.py` (new) | 399 |
| production | `app/services/accounting_service.py` | +110/−4 (3 141 → 3 247) |
| production | `app/core/exceptions.py` | +48 |
| production | `app/core/idempotency.py` | +28/−1 |
| production | `app/api/deps.py` | +23 |
| production | `app/core/audit_actions.py` | +12 |
| production | `app/core/money.py` | +11/−1 |
| production | `app/api/v1/router.py` | +5/−1 |
| tooling | `pyproject.toml` (pytest marker `cash`) | +1 |
| test | `tests/integration/test_cash_movements.py` (new) | 1 316 |
| test | `tests/cash_helpers.py` (new) | 984 |
| test | `tests/integration/test_cash_sessions.py` (new) | 901 |
| test | `tests/integration/test_cash_concurrency.py` (new) | 451 |
| test | `tests/unit/test_cash_rules.py` (new) | 408 |
| test | `tests/integration/test_accounting_multicurrency.py` (amended) | +126/−1 |
| test | `tests/unit/test_money.py` (extended) | +28 |
| docs | `docs/api/API_CONTRACT.md` (§9.4 and the error/permission tables) | +40/−6 |
| docs | `docs/phases/PHASE6_REPORT.md` (new), `docs/PROJECT_STATUS.md` | this report |

Totals before this report and the status update: **20 files, +8 384/−18** — 11 production
files, 1 tooling file, 7 test files and 1 contract document. The phase commit as a whole carries
**22 files, +9 215/−21** (11 production, 1 tooling, 7 test, 3 documentation). No file of
Phases 0–5 was deleted, renamed or weakened; `accounting_service.py` grew by 106 net lines and
nothing was removed from any service.

## 6. Architecture of the cash module

```
HTTP  app/api/v1/cash.py            12 endpoints, permission dependency, rate limit, scope,
      │                             Idempotency-Key (required or optional per contract)
      ▼
rules app/services/cash_service.py  validation · session lifecycle · movement recording ·
      │                             reversal · expected/variance derivation · audit · idempotency
      │                             (the only module that decides what a cash act means)
      ▼
store app/repositories/cash.py      sessions (row/lock/one-open-per-branch/expandable branch
      │                             clause) · movements (row/by-client-event/reversal,
      │                             filtered + paginated) · position rows · drawer ledger read
      ▼
ledger app/services/accounting_service.py   post_cash_movement · reverse_journal_entry ·
      │                                     inventory_account · record_cash_movements · scope
      ▼
DB    cash_movements (evidence, immutable)  journal_lines/journal_entries (the ledger)
      v_cash_position (physical)            account balances (authoritative)
```

The direction of dependency is one-way: the cash module never writes the ledger's tables, and
the ledger never imports the cash module. A cash act is a *document* the cash module owns, plus
*a journal entry* the ledger owns, plus *a movement row* that is the evidence for both — written
in one database transaction, or not at all.

## 7. Business semantics implemented

| Concept | Rule implemented |
| --- | --- |
| Drawer | The branch/currency cash-inventory account the ledger itself resolves (`inventory_account`, chart band 1000–1099). A movement is always recorded against that account; a counter account is a *different* account (`CASH_COUNTER_ACCOUNT_REQUIRED` when it is the drawer itself) |
| Shift (session) | One open shift per branch; the branch row is the mutex, the frozen partial unique index covers device-bound sessions. Opening records the operator, device, branch and the declared count per currency |
| Opening | Declared counts must equal the position the books carry (`CASH_OPENING_MISMATCH`, 409, with both numbers). A drawer the books already carry is *counted, not reposted* — a carried shift posts no movement at all. A genuinely empty till opening a foreign position posts `OPENING_BALANCE` and needs `accounts.manage` |
| Movement types | `OPENING`, `IN`, `OUT`, `ADJUSTMENT`, `EXPENSE`, `CLOSING` (the frozen check constraint). The module records the first four; `CLOSING` is the model's reconciliation snapshot and posts **no** journal entry |
| Expected | `opening_declared + Σ(signed movements of the shift)`, derived from immutable movements, never stored as an independent truth |
| Counted | What the operator physically counted at close, per currency, in the request — stored on `cash_session_lines`, never overwritten afterwards |
| Variance | `difference = counted − expected`, derived; non-zero is posted as an `ADJUSTMENT` to 5090 Cash Short/Over with the operator's reason; an exact count touches the books not at all |
| Business date | The branch-local day (`branch.timezone`), falling back to UTC; every request may carry `transaction_date`, which the server bounds (§19) |
| Reversal | A compensating movement plus the ledger's mirror of the *original* journal entry; the original row and entry stay exactly as posted |

## 8. Session lifecycle: open, expected, close

**Open** (`POST /cash/open`) — validate the caller's branch scope, the openings (each with a
currency, an amount at the currency's scale, an optional positive opening rate; a currency
declared twice is refused), then take the branch row lock, resolve the device, and refuse a
second shift with `409 CASH_SESSION_ALREADY_OPEN` naming the session that holds the branch.
Each opening is compared with the carried position:

* the drawer already carries the currency → the declared amount must equal it exactly
  (`409 CASH_OPENING_MISMATCH`, details carry `declared`, `carried_amount`);
* the drawer carries nothing and the opening is zero → nothing is posted;
* the drawer carries nothing and the opening is non-zero → an `OPENING_BALANCE` movement posts
  the value into the drawer (needs `accounts.manage`), with the client's rate captured and
  audited (`ACCOUNTING_MODEL.md` §6.1).

The opening lines are then written as `cash_session_lines` and the whole thing is one
transaction: journal entries, movements, session, audit.

**Close** (`POST /cash/close`) — load the shift, require it to be `OPEN` (a second close is
`409 CASH_SESSION_NOT_OPEN` with the status and `closed_at`, so the loser of a race is told the
truth), require the caller to be the operator who opened it unless they hold `cash.adjust`
(`403 PERMISSION_DENIED`, `details.reason = NOT_SESSION_OWNER`), and require a count for **every**
currency the shift moved (`422 CASH_RECON_INCOMPLETE`, listing the missing currencies; a currency
the shift never touched may not be counted). Then:

1. `expected` is derived from the shift's movements (see §7);
2. `difference = counted − expected`;
3. a non-zero difference is posted as an `ADJUSTMENT` through 5090 in the direction of the
   difference (`cash.adjust` is required — the count is an assertion about money, and stating an
   assertion in the books is a supervisory act);
4. the session lines are stamped with `expected_amount`, `counted_amount`, `difference` and the
   adjustment's journal entry id; the session becomes `CLOSED` with `closed_by`/`closed_at`;
5. audit records the whole act, including every line's numbers.

Reopening is impossible: there is no endpoint that mutates a closed session, the API exposes no
session write beyond open/close, and a correction after close is a *new* movement (adjustment or
reversal), never an edit.

## 9. Money, Decimal and rounding

Every amount travels as a decimal string and is validated at the edge with the *ledger's own*
rules (the public `validate_money`/`require_positive_money` of `AccountingService`), so a number
the `NUMERIC(30,10)` column cannot store is refused as a bad request, never as a server defect:

* at most 10 decimal places, at most 20 integer digits (`422 VALIDATION_ERROR`,
  `details.fields[].code` naming the field);
* at most the currency's own smallest unit — an amount of 0.001 AFN is refused even if the column
  could hold it;
* positive by definition for movements and openings (`not_positive`/`negative`);
* `float` in a JSON body is rejected, never coerced (a `Decimal`-typed request model with
  `strict` validation);
* the request fingerprint hashes the decimal *string* the caller sent, so `70` and `70.0000000000`
  are different payloads for idempotency purposes even though they are the same number (§16).

Functional valuation is the ledger's job, not the cash module's: a receipt enters at the house's
own published quote for the currency, a disposal leaves at the account's carrying rate, and the
module stores exactly the numbers `post_cash_movement` returns. The database's
`NUMERIC(30,10)`-only rule is asserted by the Phase 0 self-check (30 numeric columns, 0 float
columns).

## 10. Cash-in and cash-out

`POST /cash/in` (money received into the drawer) and `POST /cash/out` (money paid out) share one
implementation. Order of operations:

1. permission (`cash.create`), rate limit, branch scope (`assert_branch_scope`);
2. idempotency claim (the key is **required** on both doors) and `client_event_id` replay;
3. session state — an open shift for the drawer is required (`409 CASH_SESSION_NOT_OPEN`);
4. currency exists and is active (`422 CURRENCY_INACTIVE`);
5. amount validated at the currency's scale and positive;
6. the counter account exists, is in the branch, and is not the drawer
   (`422 CASH_COUNTER_ACCOUNT_REQUIRED`, `same_as_cash_account`);
7. the journal entry posts through `post_cash_movement` with `resolve_rate=True` — the ledger
   values the movement under the account lock (receipts at the house quote, disposals at the
   carrying rate) and refuses a disposal the position cannot cover (see §14);
8. the movement row is recorded against the drawer with the entry's id, the session's id and the
   optional `client_event_id`;
9. audit; then the response is completed in the idempotency record and returned `201`.

The cash-out path is where "a till cannot give value it does not hold" is enforced: the check
happens **after** the account locks are taken, so the second of two concurrent payouts cannot
read a balance that the first one is about to consume (§17). Failure at any step rolls back the
whole transaction — journal, movement, session stamp and audit alike.

## 11. Drawer and account transfers

There is deliberately **no** `/cash/transfer` endpoint in this phase, and this is a design
decision rather than an omission:

* a transfer between a drawer and any account (a second till, a bank account, a person's
  custody account) is a movement *of the caller's drawer* and is expressed by the two doors this
  phase implements — `POST /cash/in` with `source_account_id` (value comes from that account into
  the drawer) or `POST /cash/out` with `target_account_id` (value leaves the drawer to that
  account). The accounting model's `§6.4` entry is exactly this: the drawer leg plus one counter
  leg, in one balanced entry;
* the counter account is validated like any other: it must exist, be in the branch, be a posting
  account, and differ from the drawer — so a self-transfer is impossible by construction
  (`same_as_cash_account`);
* when the counter account is itself a drawer account (a cash account carrying a currency), the
  same position guard applies to it, and *it* may not give value it does not hold (§14). This is
  the rule that makes a transfer between two tills honest: the tills' values are not
  interchangeable at will, they are positions;
* the currency is compatible by construction — the counter leg takes its currency from its own
  account when it carries one, and the ledger values both legs to the same functional amount, so
  the entry balances exactly (§13);
* lock order is deterministic (§17); the movement is atomic, idempotent, audited and reversible
  exactly like any other movement.

The Phase 8/10 `transfers` **product** (customer hawala-style transfers with a PENDING →
APPROVED → PAID lifecycle) is a different feature and is not built here.

## 12. Variance, the 5090 workflow and the count

Variance is never silently applied to cash:

* the count is data. It is stored on `cash_session_lines.counted_amount` and never overwritten —
  not by a later close (a session closes once), not by an adjustment, not by a reversal;
* `expected` and `difference` are **derived** from authoritative movements at close time; nothing
  stores them as an independent truth that could drift;
* a difference is stated in the books only through an explicit, authorised workflow: an
  `ADJUSTMENT` movement against **5090 Cash Short/Over** (seeded in Phase 3), posted by
  `AccountingService`, carrying the operator's reason, the session, the branch, the currency and
  the journal reference, and recorded in the audit trail;
* `cash.adjust` is required to post the difference (a cashier may count and close, but may not
  walk away with the difference as a privilege);
* the same workflow is exposed directly as `POST /cash/adjustment` for corrections outside a
  close, and reversing an adjustment is possible through the reversal architecture (§15);
* an exact count posts nothing at all: `has_variance` is false, the close response carries no
  adjustment ids, and the ledger is untouched.

## 13. Exactly one authoritative posting path

A static reading of the diff shows no direct write to accounting tables outside
`AccountingService`: `cash_service.py` contains no `INSERT INTO journal`, no `JournalEntry(...)`,
no balance update and no `UPDATE accounts`. Its writes are `cash_sessions`,
`cash_session_lines`, `cash_movements` and `audit_log` — the evidence tables of this phase.

The ledger additions this phase needed are strictly *capabilities*, not a second engine:

* `post_cash_movement(..., resolve_rate=True)` — the ledger decides the rate (house quote for a
  receipt, carrying rate for a disposal) inside its own transaction and under its own locks;
* `_resolve_cash_movement_rate` — one function, one place, used by the cash doors;
* `guard_inventory=True` on cash movement plans — the same position guard the generic journal
  door has always run;
* public `validate_money` / `require_positive_money` / `in_cash_band` — the ledger's own
  validators, exported so the edge refuses what the ledger would refuse;
* `record_cash_movements` returns the movement ids *after* the flush (D6-1).

A refusal never writes a partial state: every cash door runs inside
`AccountingService.document_transaction()`, and the tests assert both the empty result (`409`,
no movement, no journal entry, no session change) and the unchanged counts of the affected
tables.

## 14. Position safety and the inventory guard

Two layers protect the branch's value:

1. **Domain**: a disposal is valued by `_carrying_rate`, which reads what the account actually
   holds for that currency and refuses a quantity it cannot cover — `409 INSUFFICIENT_BALANCE`
   with `details.reason = QUANTITY_EXCEEDED` and the exact `shortfall`. When the account holds
   nothing at all the reason is `NO_POSITION` (there is no shortfall to state — the position does
   not exist), with `account_id`, `currency_id`, `foreign_quantity` and `disposing_quantity` in
   the details.
2. **Backstop**: the database's deferred non-negative trigger (`NEX01`) over the generated
   `signed_amount` column, which fires at `COMMIT` if any application path ever let a position go
   negative. It is the safety net, not the interface: the tests assert the domain refusal, and the
   `NEX01` trigger stays as the invariant's last line of defence.

The guard is not limited to the drawer leg. Any **asset account carrying a currency** that gives
value up is guarded, which includes a *counter* leg pointing at another till (§11). That is
intended: it is what makes "a till cannot give value it does not hold" true for both sides of a
transfer. Two Phase 3/4 multicurrency tests had constructed the opposite — crediting an unfunded
drawer and then disposing from it — and were amended to fund the source first; a new regression
test (`test_a_counter_drawer_that_holds_nothing_cannot_give_value_up`) pins the refusal with the
drawer left at zero and no new branch entries (D6-5).

## 15. Reversal and correction

A posted cash movement is never edited and never deleted. `POST /cash/movements/{id}/reverse`
implements the Phase 4/5 architecture:

* the original's journal entry is mirrored by `reverse_journal_entry(original.journal_entry_id)`
  — the ledger writes the compensating entry, and the mirror's `reference_id` is the original
  entry, so the pair is discoverable from both sides;
* a **compensating cash movement** is recorded (`reference_type = REVERSAL`,
  `reference_id` = the original movement), with the mirrored type (`IN`↔`OUT`, `OPENING`→`OUT`,
  `EXPENSE`→`IN`) and the reason in the audit trail;
* the original row keeps its numbers; the reversal is visible from the original
  (`reversed_by_movement_id`) and from the reversal (its reference);
* a second reversal is `409 ALREADY_REVERSED` (the loser of a concurrent double-reverse is told
  the same thing); reversing a reversal is `409 CASH_MOVEMENT_NOT_REVERSIBLE`; a correction dated
  before its original is refused; a reversal the drawer cannot afford is refused **whole**
  (nothing written);
* permission is `cash.adjust` (a correction is a supervisory act), the reason is mandatory and
  audited, and the reversal itself is reversible only through a *new* movement — history is
  never rewritten.

## 16. Idempotency

The existing Phase 2/4/5 store is reused unchanged in mechanism and extended in scope:

* **Endpoint keys** `cash:open`, `cash:in`, `cash:out`, `cash:adjustment`, `cash:close`,
  `cash:reverse` — separate keys per operation, so a client reusing one key across operations is
  answered per operation and never with another operation's result;
* **required** (`400 IDEMPOTENCY_KEY_REQUIRED` when missing or not a UUID) on `in`, `out` and
  `close` — the contract's §8 money doors; **optional** on `open`, `adjustment` and `reverse`
  through the new `OptionalIdempotencyKeyDep`, which still refuses a malformed header;
* canonical request hash (sorted keys, no whitespace, decimal strings preserved exactly), so a
  changed body under the same key is `409 IDEMPOTENCY_KEY_REUSED` while a byte-identical retry
  replays;
* the recorded answer **is** the wire answer: the payload the endpoint returned is stored as the
  response body, and a replay is served from that record (D6-3 — the module now spells UTC
  instants the way the response model does, `…Z`);
* concurrent duplicates produce exactly one financial operation: one movement, one journal entry,
  one `COMPLETED` row, identical replay payloads (measured with four callers on one key, §17);
* the record is completed inside the same transaction as the money, so a crashed request leaves
  `IN_PROGRESS`/`FAILED` (reclaimable) rather than a half-posted movement. A replayed
  `client_event_id` is answered with the movement that event already produced (`DUPLICATE_RESOURCE`
  when the same event id is reused for a different amount or currency — the offline hook).

## 17. Concurrency, locking and rollback

All of the following were measured on **real PostgreSQL 16** with independent per-caller engines
on one event loop (`tests/integration/test_cash_concurrency.py`, 9 tests, no mocks):

| Race | Result |
| --- | --- |
| Two opens of one branch | One shift; the loser gets `409 CASH_SESSION_ALREADY_OPEN` with the session that holds the branch |
| Two payouts (600 + 600) against a 1 000 drawer | Exactly one winner; the loser is refused with `shortfall = 200.0000000000`, and writes no movement and no journal line |
| A receipt (500) and a payout (300) racing on one drawer | Both land; the book equals the till (1 200) whichever commits first — the *order* of the two rows is a commit-order property, so the assertions compare sorted signatures, not sequence |
| Four callers, one `Idempotency-Key` | One movement, one entry, one `COMPLETED` row, identical replay payloads |
| Two closes of one shift | One close; the loser is `409 CASH_SESSION_NOT_OPEN` with `status = CLOSED` and the `closed_at` |
| Two reversals of one movement | One compensating pair; the loser is `409 ALREADY_REVERSED`; the drawer is restored exactly once |
| A storm of mixed directions (6-way) | Books equal the till, no position negative, every movement accounted for |
| Two drawers moving in parallel (AFN and USD) | Per-currency positions stay separate and each reconciles |
| Opposite directions between one drawer and one account | No deadlock: the deterministic lock order holds |

**Lock order** is a single sentence the module can state and the tests can attack: *branch row →
session → accounts, sorted by `(account_id, currency_id)`*. Every cash door takes the locks in
that order, the ledger's `_lock_accounts` sorts its own ids the same way, and the branch row is
the mutex for the "one open shift" question — so two operations that overlap cannot hold the
resources the other needs in opposite order. Every cash act is one transaction; a failure at any
step rolls the whole act back (asserted by counting rows before and after a refusal).

## 18. Authorisation, branch isolation and auditability

* **Permissions** (frozen RBAC, `resource.action`): `cash.create` (open, in, out), `cash.view`
  (balance, movements, sessions), `cash.close` (close — cashiers for *their own* shift),
  `cash.adjust` (adjustment, reversal, and closing somebody else's shift, and closing a shift with
  a non-zero difference). Deny by default: a missing permission is `403 PERMISSION_DENIED` before
  any business logic runs.
* **Branch isolation**: every door asserts the caller's scope (`assert_branch_scope`); a branch
  outside it is `403 FORBIDDEN_SCOPE`; a read of another branch's session is `404` (existence is
  never disclosed), a cross-branch id in a movement or session read is `404` with the requested id
  echoed, and listings default to the branches the caller may see. A confined operator cannot
  open, move or close at another branch (tested).
* **Drawer ownership**: a cashier closes only the shift they opened (`NOT_SESSION_OWNER`
  otherwise); `GET /cash/sessions/current` is scoped to the caller's branch.
* **Audit**: six actions (`CASH_SESSION_OPENED`, `CASH_SESSION_CLOSED`, `CASH_MOVEMENT_RECORDED`,
  `CASH_ADJUSTMENT_RECORDED`, `CASH_MOVEMENT_REVERSED`, `CASH_OPERATION_DENIED`) record who
  (user, device, IP, request id), what (type, amount, currency, drawer account), when, where
  (branch, session), the references (`reference_id`, `journal_entry_id`, `counter_account_id`,
  movement/session ids), the before/after numbers of a close (expected, counted, difference per
  currency), the reversal linkage and the operator's reason. A *refused* cash act records its own
  audit row (`CASH_OPERATION_DENIED`), so a failed attempt to take money out is as visible as a
  successful one. Audit rows are append-only (`P0001` on update/delete) and the chain is verified
  by the existing audit suites.
* **Mass assignment**: request models are closed (`extra="forbid"`); a client cannot set
  `status`, `signed_amount`, `created_at`, `created_by`, journal ids or session ids the API does
  not accept, and unknown fields are a `422`.

## 19. Business date, timestamps and the period-lock limitation

* `created_at`, `opened_at` and `closed_at` are written by the server (`now()`/the server clock);
  a client cannot dictate them.
* A movement may carry `transaction_date`; the service bounds it: a *future* instant is refused
  (`422 VALIDATION_ERROR`, `details.fields[].code = "future_date"`, with `latest_allowed`), and a
  past instant is accepted as the accounting date.
* The **business date** of a shift is the branch-local day derived from the branch's timezone
  (`branch.timezone`), with an unknown timezone falling back to UTC — asserted by unit tests over
  fixed clocks and by an integration test against the branch row.
* **Period lock is a documented limitation, not a half-built feature.** The approved accounting
  model (`ACCOUNTING_MODEL.md` §11) states that the MVP has no period lock: any authorised
  operator may date a correction into an earlier period, and the books remain correct because
  history is append-only and corrections are compensating entries. Inventing a partial
  period-lock here (for example refusing backdating only in cash) would be a second, inconsistent
  rule. The limitation is recorded in §32 and in the contract's §9.4 notes on `transaction_date`.

## 20. Offline hooks

This phase builds hooks, not the sync engine (Phase 8/12 is not authorised):

* **Idempotency** on every money door, with the recorded answer replayable byte-for-byte — the
  foundation a device needs to re-send an operation it never saw acknowledged;
* **`client_event_id`** on movements: a device that recorded a receipt while disconnected sends
  its own event id, and the server answers with the movement that event already produced, or
  refuses when the same event id claims a different amount;
* **deterministic references**: a movement's document id is generated server-side and returned,
  so a client can store it and recognise it later; the reference never depends on wall-clock
  ordering;
* **server-authoritative** balances, session state and timestamps: nothing in the request can
  assert a balance, a status or a time the server has not decided. No LWW anywhere for cash;
* **replay-safe refusals**: a refusal writes nothing, so a device retrying a refused act cannot
  corrupt a position.

## 21. API contracts (`docs/api/API_CONTRACT.md` §9.4)

§9.4 was rewritten to describe the twelve implemented endpoints exactly (methods, paths,
permissions, which doors require `Idempotency-Key`, status codes and the specific error codes),
plus the shift rules (one open shift per branch, the device index), expected/variance derivation,
the counter-account rule, the projection of the close response, and the statement that money is a
decimal string (a float is refused, never coerced). The error table gained
`CASH_OPENING_MISMATCH` and `CASH_MOVEMENT_NOT_REVERSIBLE`, `CASH_SESSION_ALREADY_OPEN` was
corrected to describe the branch scope, and the permission matrix gained `cash.view`. The
endpoint count in the contract and the count in the router are equal by test
(`test_operation_permissions_match_the_contract` and the API tests).

## 22. Database changes and migrations

**No schema change and no migration.** The phase uses the frozen Phase 0 objects exactly as
approved:

* `cash_sessions` (+ `ux_cash_sessions_one_open_per_device`, the status/closed-stamp checks),
* `cash_session_lines` (opening/expected/counted/difference, the adjustment entry reference),
* `cash_movements` (generated `signed_amount`, `ck_cash_movements_reference`,
  `ck_cash_movements_adjustment_sign`, `ux_cash_movements_client_event`,
  `ix_cash_movements_branch_currency`, `ix_cash_movements_session`),
* the non-negative cash trigger (`NEX01`) and the views `v_cash_position` / `v_currency_position`,
* `journal_entries`/`journal_lines` through `AccountingService` only.

The frozen migrations and `docs/database/schema.sql` are byte-for-byte unchanged; the fresh-database
migration and both schema gates are green (§28). The one database-behaviour fix of this phase is
in the application: `record_cash_movements` reads the generated ids after the flush (D6-1).

## 23. Defects discovered during this phase and how they were fixed

The brief's standing rule is that a latent financial, accounting, concurrency, security,
validation or data-integrity defect found in earlier phases is a real defect: it is fixed here and
pinned by a regression test. Seven defects were found (four in production code, three in tests);
none of them is left open, and no fix weakened an existing test.

| ID | Where | Defect | Fix (and the test that pins it) |
| --- | --- | --- | --- |
| **D6-1** | `app/services/accounting_service.py` — `record_cash_movements` | The method collected `row.id` **before** `flush()`. `id` is generated by the database (`gen_random_uuid()`), so it was `None`: a caller that needed the id of the movement it had just recorded received an unusable value and would fail on `str(None)`/404 paths. Phase 5 never read the return value, which is why the defect survived | Collect the rows, `flush()`, then return `[row.id for row in inserted]`. Pinned by the movement suite (every cash-in/out asserts the returned id resolves through `GET /cash/movements/{id}`) |
| **D6-2** | `app/services/cash_service.py` — movement reversal | Reversing a cash movement wrote a compensating movement but did not undo the original **journal entry**, and could post the compensating entry from the wrong document. The measured state was cash 1 000 / ledger 600 / drawer 600 — the till and the books disagreed by the original amount | One reversal now: link the evidence (`reference_type = REVERSAL`, `reference_id` = original movement), mirror the ledger with `reverse_journal_entry(original.journal_entry_id)`, then record the compensating movement. Verified by a temporary measurement probe (cash == ledger == drawer == 1 000) and pinned by `test_reversing_an_out_puts_the_money_back_exactly_once` and the reversal race |
| **D6-3** | `app/services/cash_service.py` `_iso` + `app/core/idempotency.py` | The idempotency record stored a UTC instant as `…+00:00` while the endpoint's response model renders it `…Z`: the stored answer was not the answer the caller received, and a replay (or an auditor comparing the two) had to normalise the spellings by hand | `_wire_moment()` in `json_safe` renders UTC as `Z`, `cash_service._iso` does the same, and a unit test asserts the stored body equals the live body exactly (`TestTheIdempotencyRecordIsTheWireAnswer`) |
| **D6-4** | `app/core/money.py` — `has_money_scale` | A value wider than the money context (e.g. `1e25`) made `Decimal.quantize` raise `InvalidOperation`, which escaped the validator as a **server defect** (500) instead of the `422` the request deserves — an unhandled-input hole at the money boundary | The check catches `InvalidOperation` and answers `False` ("not at the stored scale"), which the caller reports as the bad request it is. Pinned by `TestScaleCheck::test_a_value_wider_than_the_money_context_is_refused_not_raised` and by the API test that sends `1e25` and expects `422` with "maximum representable amount" |
| **D6-5** | `accounting_service.post_cash_movement` plans | A drawer disposal could reach the database's deferred `NEX01` at `COMMIT` — a *correct* refusal, but a database error where the contract promises a domain error with the shortfall, and a check taken on a stale read | `guard_inventory=True` on cash movement plans: the same position guard the generic journal door runs, inside the ledger transaction under the account locks. Pinned by `test_a_payout_the_drawer_cannot_cover_is_refused_before_anything_is_written` (asserts the code, the shortfall and zero rows written) |
| **D6-6** | `app/repositories/cash.py` — `entry_lines` | The journal lines of a movement were read without a deterministic order, so two rows of the same entry could come back in either order and make an accounting assertion flaky | `ORDER BY l.account_id, l.currency_id, l.debit DESC, l.credit DESC` — an order that is stable for any entry and meaningful to a reader |
| **D6-7** | `tests/integration/test_cash_concurrency.py` | (Test-side) two assertions compared the *sequence* of movement rows produced by a race, but the row order is a commit-order property: under full-suite load `OUT` can commit before `IN`, so the test failed while the money was correct | The assertions compare **sorted** signatures/multisets (`["OPENING", "OUT"]`, and the multiset `[("IN", 500), ("OPENING", 1 000), ("OUT", −300)]`). Sequence assertions that are genuinely sequential elsewhere were left as they are — no assertion was weakened in substance |
| **D6-8** | typing (`app/api/v1/cash.py`, `app/repositories/cash.py`) | Four MyPy errors (a `dict`-typed constant the query expands, unparameterised `Mapping` returns, a `model_validate` on a mapping) | `_EXPANDING_BRANCHES: Any`, `Mapping[str, Any]` parameters, `CashBalanceRowResponse.model_validate(row.to_payload())`. `mypy app seeds scripts` is clean again |

Two further findings were **behaviour confirmations rather than defects**, and are recorded so a
later reader does not re-open them:

* **The counter-leg guard is intended (D6-5, second half).** `post_cash_movement` guards every
  *asset account carrying a currency* that gives value up, counter legs included. The two failing
  Phase 3/4 multicurrency tests had built an impossible state — crediting an unfunded drawer and
  then disposing from it. They were amended to fund the source first (the AFN test funds the
  counter drawer with 10 000 and asserts the remaining 3 000; the USD test funds the second
  drawer with 400 through `capital` and asserts 28 000 / 0), and a new regression test,
  `test_a_counter_drawer_that_holds_nothing_cannot_give_value_up`, pins the refusal
  (`reason = NO_POSITION`, no `shortfall`, no new journal entries, the empty drawer still zero).
  **No production rule was scoped back.**
* **The exchange module's own timestamp spelling.** `exchange_service._iso` renders UTC as
  `+00:00` inside *string* fields of its own payload, so for exchange endpoints the stored
  idempotency record and the wire answer are the same string (self-consistent). The cash module
  renders `Z` because its response model serialises datetimes and the wire answer is `Z`. Both
  spellings are RFC 3339 and both are internally consistent; unifying them is a cosmetic change
  that would touch the frozen Phase 5 module for no behavioural gain, so it is recorded here and
  left for a phase that touches both (see §32).

## 24. Test catalogue

New tests of this phase: **73** (68 cash + 4 money-boundary + 1 counter-drawer regression). The
suite grew from **1 301** to **1 376** collected tests (the extra 2 are the existing parametrised
error-code suite picking up `CASH_OPENING_MISMATCH` and `CASH_MOVEMENT_NOT_REVERSIBLE`).

| File | Tests | Coverage |
| --- | --- | --- |
| `tests/unit/test_cash_rules.py` | 23 | pure rules: expected = opening + net; variance detection; mirror map for every movement type; amount/scale/currency-unit refusals; duplicate currencies at open/close; opening-rate positivity; business date and timezone fallback; fingerprint order-insensitivity and close fingerprint completeness; permissions vs the contract; and `TestTheIdempotencyRecordIsTheWireAnswer` (4) |
| `tests/integration/test_cash_sessions.py` | 15 | shift lifecycle end to end: open posts the counted opening; a carried drawer is counted, not reposted; opening mismatch refused; one shift at a time; exact count closes without touching the books; shortage and overage post to 5090 the right way round; the count is kept; close records who/why; no second close; incomplete count refused; difference without `cash.adjust` refused; balance view keeps currencies apart and reconciles; business date from the branch clock; current-shift scoping; session list filtering/pagination/order |
| `tests/integration/test_cash_movements.py` | 21 | the two money doors and the adjustment: entry + movement + audit in one act; service-level refusals (no counter account, payout beyond the position); the counter account must exist and differ; multi-currency valuation (foreign receipt at the house quote, disposal at the carrying rate); adjustment to 5090 both directions with the permission; a money door requires the key; retry replays and a changed body is refused; `client_event_id` replay; filters/pagination; inactive currency; bad amount leaves no side effect; server-bounded accounting date; a cashier cannot reverse; a confined operator cannot move another branch's cash; reversal restores the money exactly once; double reversal and reversing a reversal refused; an unaffordable reversal refused whole; a document's own movement is not reversed through this door |
| `tests/integration/test_cash_concurrency.py` | 9 | real-PostgreSQL races (§17): two opens; two payouts for one drawer; a receipt and a payout netting exactly; four duplicates on one key; two closes; two reversals; a 6-way mixed storm; two drawers in parallel; opposite directions drawer↔account (no deadlock) |
| `tests/unit/test_money.py` | 4 new | the money boundary: at-scale accepted, beyond-scale refused, wider-than-context refused not raised, answer independent of the ambient decimal context |
| `tests/integration/test_accounting_multicurrency.py` | 1 new (+2 amended) | the counter-drawer regression of D6-5 and the funded-source amendments |
| existing parametrised suites | +2 | the error-code enumeration picks up the two additive codes |

No test was deleted, skipped, weakened or marked xfail; no test asserts a mocked ledger — every
financial assertion is made against PostgreSQL rows.

## 25. Exact test commands and results

All commands run from `apps/api` with the sandbox stack (PostgreSQL 16 on `127.0.0.1:5432`,
Redis 7 on `127.0.0.1:6379`) and the CI environment removed from the shell
(`env -u DATABASE_URL -u DATABASE_MIGRATION_URL -u APP_ENV`), exactly as the CI job does:

| # | Command | Result |
| --- | --- | --- |
| 1 | `ruff check .` | *All checks passed!* |
| 2 | `ruff format --check .` | *156 files already formatted* |
| 3 | `mypy app seeds scripts` | *Success: no issues found in 97 source files* |
| 4 | `pytest tests/unit -q` | **577 passed** in 2.16 s |
| 5 | `pytest tests/integration -q` | **799 passed** in 240.48 s (run 1) / 237.40 s (run 2) |
| 6 | `pytest tests/integration/test_cash_concurrency.py -q` | 9 passed (5.96 / 6.19 / 6.31 / 6.31 s across four runs) |
| 7 | `pytest tests/integration/test_cash_movements.py tests/integration/test_cash_sessions.py tests/integration/test_cash_concurrency.py -q` | 45 passed in 25.18 s |
| 8 | `pytest tests/integration/test_accounting_multicurrency.py -q` | 32 passed in 14.13 s |
| 9 | `alembic upgrade head` on a fresh `nexus_ci_clean` | revision `0002_runtime_schema_revision`, no error |
| 10 | `scripts/schema_gate orm-db --dsn …/nexus_ci_clean` | **MATCH** (Phase 0 self-check: 30 numeric columns, 0 float) |
| 11 | `scripts/schema_gate db-db --left …/nexus_ci_reference --right …/nexus_ci_clean` | **MATCH** |
| 12 | `python -m seeds` (first run) | `TOTAL: inserted=89 updated=0 unchanged=0 removed=0` |
| 13 | `python -m seeds` (second run) | `TOTAL: inserted=0 updated=0 unchanged=88 removed=0` |
| 14 | `python -m seeds --check` | `TOTAL: inserted=0 updated=0 unchanged=88 removed=0` |
| 15 | `psql -f tests/invariants/phase0_schema_invariants.sql` on a fresh migrated `nexus_ci_phase0` | **PHASE 0 SCHEMA INVARIANT SUITE: ALL ASSERTIONS PASSED** |
| 16 | `/tmp/phase6_gates.sh` (all of the above in CI order, `set -euo pipefail`) | **PHASE 6 GATES: ALL PASSED**, exit 0 — run **twice consecutively**, whole sweep green both times |

The complete gate log of both sweeps is reproduced in the shell history of the session and
summarised in the table above; the decisive lines are quoted verbatim:
`577 passed`, `799 passed`, `result: MATCH` (twice), `TOTAL: inserted=89` / `unchanged=88`,
`phase 0: ALL ASSERTIONS PASSED`, `PHASE 6 GATES: ALL PASSED`.

## 26. Regression of Phases 0–5

The full suite *is* the regression: `pytest tests` = the Phase 0 invariant suite's unit
counterparts, the Phase 1 compose/config/logging/worker tests, the Phase 2 auth/user/device
suites, the Phase 3 master-data suites, the Phase 4 accounting engine suites (posting, reversal,
idempotency, scope, reports) and the Phase 5 exchange suites (posting, lifecycle, accounting
integration, concurrency), all green in both consecutive sweeps.

Two Phase 3/4 tests were amended (D6-5) — funded before they dispose — and one new regression was
added; the file that contains them went from 31 to **32 passing tests**. No Phase 0–5 assertion
was weakened: the amendments *strengthen* the tests by removing an impossible premise, and the new
test states the rule they had been violating. The Phase 4 and Phase 5 financial invariants were
re-verified by their own suites in the same runs:
`SUM(debit) == SUM(credit)` per entry, posted balances equal the sum of valid movements, cancelled
documents are not deleted, and reversed documents have a reversal entry.

## 27. Ruff and MyPy

* `ruff check .` — clean over the whole repository (the new module, service, schemas, helpers and
  tests included).
* `ruff format --check .` — **156 files already formatted**; no reformat was left uncommitted.
* `mypy app seeds scripts` — **Success: no issues found in 97 source files** (97 was the count at
  `9d67582`; the new modules were fully typed from the start and the four errors D6-8 introduced
  by the first draft were fixed rather than silenced with `# type: ignore`).
* No `# type: ignore`, no `# noqa` and no `pragma: no cover` was added by this phase; the diff was
  read for them as a static audit.

## 28. Migration, schema gates, Phase 0 invariants and seeds

Reproduced on a database created from scratch in this sweep (not a reused one):

* `alembic upgrade head` reached the frozen head revision and `alembic current` agrees;
* `schema_gate orm-db` compared the migrated database with the SQLAlchemy metadata: **MATCH**,
  and the SQL self-check reports 30 `NUMERIC(30,10)` columns and 0 float columns;
* `schema_gate db-db` compared the reference DDL (`docs/database/schema.sql`, applied to
  `nexus_ci_reference`) with the migrated database (`nexus_ci_clean`): **MATCH** — so the frozen
  reference schema and the ORM/schema pair still agree byte-for-byte on structure;
* the Phase 0 invariant suite ran against a freshly created and migrated `nexus_ci_phase0`:
  **ALL ASSERTIONS PASSED** (deferred constraints, triggers, append-only rules, the balance
  invariant);
* seeds are idempotent three ways: first run inserts 89, the second run and `--check` report
  88 unchanged rows with 0 inserted/updated/removed.

## 29. CI results

CI runs the same gates on a clean machine plus the OpenAPI document job and the five-service
compose acceptance job the sandbox cannot run. The push and pull-request run identifiers of the
implementation commit, with every job's conclusion, are recorded in the finalisation commit that
follows this one and in `docs/PROJECT_STATUS.md` §1/§3 — the Phase 2–5 pattern. The local sweep
(§25) reproduces every CI step that does not require Docker, twice consecutively, and this phase
adds no CI configuration change.

> The compose job (PART 44 acceptance) builds the real stack (`api`, `postgres`, `redis`,
> `nginx`, `worker`), migrates and seeds it through `docker compose exec`, logs in through nginx
> and registers the worker. It is unchanged by this phase; its result is part of the recorded CI
> runs.

## 30. Security review

The review covered the attack surface this phase adds, with a test or a code fact for each:

| Threat | Finding / control |
| --- | --- |
| **IDOR** (another branch's or another operator's session/movement) | Every identifier is loaded and then scope-checked. A foreign session is `404 RESOURCE_NOT_FOUND` (existence never disclosed), a foreign movement likewise; a cross-branch list or balance is `403 FORBIDDEN_SCOPE`. Tested for reads, closes, movements and reversals |
| **Branch leakage in listings** | Listings are filtered by the caller's scope in SQL (`_branch_clause` expands the scoped branch ids; `TRUE` only when the scope is unrestricted), and pagination counts apply to the filtered set. Tested by a confined operator against another branch's rows |
| **Unauthorised operations** | Every route carries a permission dependency (`cash.create`/`view`/`close`/`adjust`) checked before business logic; a cashier cannot adjust, cannot reverse, cannot close another operator's shift. Tested for each refusal, including the escalation attempt "cashier reverses their own payout" |
| **Replay / duplicate posting** | Idempotency on all six money doors (required on three), canonical payload hashing, `IDEMPOTENCY_KEY_REUSED` on a changed body, `IN_PROGRESS` refusal, exactly-one-operation under four concurrent callers; plus `client_event_id` replay |
| **Race conditions** | Real-PostgreSQL races (§17) for overspend, double close, double reversal, double open and duplicate keys; deterministic lock order; the non-negative trigger as a backstop |
| **Mass assignment** | Request models are closed; a client cannot set `status`, `signed_amount`, ids, audit fields or `created_at`; unknown fields are `422` |
| **Unsafe account / currency ids** | Accounts are resolved and validated in the caller's branch (`inventory_account`, `_account_by_code`), a counter account equal to the drawer is refused, an inactive currency is refused, and an account from another branch fails the scope check |
| **Amount abuse** (negative, zero, extreme, float, wrong scale) | Validated at the edge with the ledger's own rules: `not_positive`, `negative`, more precision than the currency or the column allows, `float` refused, `1e25` refused as a bad request (D6-4) instead of a server error |
| **Reference manipulation** | Document ids are server-generated; `reference_id`/`reference_type` are not client-writable; a reversal points at the original movement the server itself resolved, never at a client-supplied id; the reversal of a reversal is refused |
| **Audit tampering / deletion** | Audit rows are append-only (`P0001` on UPDATE/DELETE, tested), the chain is verified by the audit suites, cash history is never deleted (no DELETE endpoint; the database forbids it), and corrections are compensating entries |
| **Information leakage** | Error envelopes carry a code and the fields that explain the refusal, never internal SQL, stack traces or another branch's data; a refusal is `403`/`404`/`409` where a 500 would leak a defect; the API docs describe the same codes |
| **Session/privilege smuggling** | The effective permission set is recomputed from the database on every request (Phase 2 design), so a stale token cannot perform a cash act after a role change; device-bound calls assert the device |

No security defect was found open at the end of the phase. Two candidate issues were examined and
closed as *not* defects: (a) the counter-leg guard (intended, §14), and (b) a `409` refusal that
writes an audit row plus no financial data (intended: a failed attempt is a fact worth recording).

## 31. Performance review

* **Indexes**: the phase adds no index and needs none beyond the frozen set. Every read path uses
  an existing index: `ix_cash_movements_branch_currency (branch_id, currency_id, created_at DESC)`
  for the balance projection and the movement book, `ix_cash_movements_session` for a shift's
  movements, `ix_cash_movements_reference` for the reversal lookup,
  `ux_cash_movements_client_event` for the offline replay probe, and the partial unique index for
  the one-open-shift claim. Session listings are served by the primary key and the branch/status
  filter, and both listings are keyset-free, `LIMIT`/`OFFSET` paginated with a bounded page size
  (≤ 200).
* **N+1**: sessions are loaded in one row per session with their lines and movements fetched by
  the *set* of session ids (one query per collection, not per row); movement views resolve their
  reversal linkage with one lookup per row against an indexed column, and the adjustment/entry
  references are read with the row itself. No loop issues a query per item.
* **Transaction duration and lock scope**: a cash act is a short transaction — validate, lock the
  session/accounts, write one entry with two lines, one movement, one audit row, commit. Locks are
  held only for that duration, and the long-lived read paths (`balance`, listings) take no locks.
  The one exclusive serialization point is deliberate: the branch row when a shift is opened, and
  the drawer's accounts when money moves.
* **Lock order**: deterministic everywhere (branch → session → accounts sorted by
  `(account_id, currency_id)`), which is what the deadlock test measures rather than assumes.
* **Position, session and audit queries**: the balance projection aggregates over the indexed
  branch/currency prefix; the position of one drawer is a single grouped read; the audit write is
  an append (one insert) inside the same transaction, so auditing costs one row per act and never
  a second round trip after commit.
* **Documented hazards**: (1) `v_cash_position` aggregates per branch+currency and cannot be
  indexed through a view — for a branch with an unbounded movement history a materialized daily
  summary would eventually be needed (a reports-phase concern, not a correctness one);
  (2) `LIMIT/OFFSET` pagination degrades on very deep offsets — acceptable for operator-facing
  histories, and the reports phase (Phase 7) will read aggregates instead;
  (3) the branch row lock serializes shift *opening* at a branch — the intended mutex, but a
  branch that opens many shifts per second would serialize there (not a realistic exchange
  workload: one shift per branch per working day);
  (4) the idempotency record is read on every money door — one indexed lookup by
  `(user, endpoint, key)`.

## 32. Limitations and remaining risks

**Limitations (deliberate, documented, not defects):**

1. **No period lock** (`ACCOUNTING_MODEL.md` §11): an authorised operator may date a correction
   into an earlier period. The books stay correct (append-only + compensating entries), but a
   period cannot be "closed" against backdating. Inventing a partial rule here was explicitly
   avoided; §19 explains why.
2. **No `/cash/transfer` endpoint**: drawer↔account transfers are the two money doors plus a
   counter account (§11). The Phase 8/10 `transfers` product (customer transfers with an approval
   lifecycle) is not this phase's feature.
3. **No cash session for multiple drawers in one shift**: one open shift per branch, as the
   frozen partial unique index and the roadmap's `PART 30 cash/close` state.
4. **`exchange_service._iso` renders UTC as `+00:00`** in its own string fields (self-consistent
   with its wire answer and its idempotency record, but a different spelling from the cash
   module's `Z`). Left untouched to avoid modifying frozen Phase 5 code for a cosmetic reason;
   recorded in §23.
5. **Sandbox cannot run the compose job** (no Docker): the five-service acceptance is verified by
   CI, not locally. Same limitation as Phases 2–5.
6. **`CLOSING` movement type is unused by this phase's writes** — the accounting model defines it
   as a reconciliation snapshot that posts no entry, and the close flow states variance through
   `ADJUSTMENT` instead, exactly as §6.4 requires. The type remains part of the frozen schema and
   the API filter vocabulary.

**Risks (tracked, with the mitigation in place):**

* a future phase that adds *another* cash-band account per (branch, currency) would make
  `physical_balance` (branch-wide) and the drawer's ledger quantity diverge in a way the current
  resolution cannot express — the `reconciled` flag would surface it rather than hide it, and the
  position test would fail;
* the deep-offset pagination hazard above;
* an operator who closes a shift with a difference and no `cash.adjust` holder available cannot
  state the difference: the shift stays open and the count is preserved, which is the safe
  failure mode (the alternative — closing with an unstated difference — would be silent).

## 33. Acceptance checklist

| # | Requirement | Evidence |
| --- | --- | --- |
| 1 | Sessions: open/close, no duplicate open, no transacting when closed, no double close | §8, session suite (15), open/close races |
| 2 | Opening balances immutable after open; opening must match the carried position | §7/§8, `test_an_opening_that_disagrees_with_the_books_is_refused`, `test_a_drawer_the_books_already_carry_is_counted_not_reposted` |
| 3 | Expected derived from authoritative movements; variance derived; count never overwritten | §12, unit rules (expected = opening + net), session suite (count kept) |
| 4 | Close requires a valid count for every moved currency; concurrency-safe; ownership enforced | §8, `CASH_RECON_INCOMPLETE`, `NOT_SESSION_OWNER`, close race |
| 5 | Cash-in/out: authorization → branch → session → currency → amount → account → idempotency → one atomic transaction; no partial state on failure | §10, §13, movement suite (refusal leaves zero rows) |
| 6 | Out enforces the available position with a lock taken **before** the check; real PG race evidence | §14, §17, two-payout race with `shortfall` |
| 7 | Transfers (drawer/account): no self-transfer, currency compatibility, source availability after lock, balanced, atomic, reversible | §11, `same_as_cash_account`, counter-drawer guard, reversal suite |
| 8 | Variance never silently applied; adjustment only through the authorised workflow with 5090; originals preserved; reversible | §12, session suite (shortage/overage), adjustment direction tests, reversal |
| 9 | Multi-currency positions currency-specific, never aggregated; Decimal/NUMERIC only; functional valuation by the ledger | §9, §14, balance-view test, AFN/USD concurrency test, 30-numeric/0-float self-check |
| 10 | Accounting integration: debit == credit, correct accounts/currency/branch/business date, immutable posted journal, reversal compatibility | §13, ledger suites in the same runs, reversal tests |
| 11 | Reversal architecture: compensating entries, double reversal rejected, positions restored, both race outcomes deterministic | §15, §17, reversal suite + reversal race |
| 12 | Idempotency: same key+payload → same result, changed payload → reject, concurrent duplicates → one operation, full side effects | §16, unit wire-answer tests, four-caller race, changed-body refusal |
| 13 | Real-PostgreSQL concurrency tests; deterministic lock order; no mock-only evidence | §17 (9 tests, measured) |
| 14 | RBAC and branch isolation: permissions per door, deny by default, no leakage/IDOR/escalation | §18, §30 |
| 15 | Audit: who/what/when/where/amounts/references/before-after/reason/idempotency; immutable | §18, audit assertions in every suite, `P0001` |
| 16 | API §9.4 endpoints per the existing conventions, pagination, errors, `Idempotency-Key`; contract updated | §21, `docs/api/API_CONTRACT.md` §9.4 + error/permission tables |
| 17 | DB: no migration needed, frozen migrations untouched, fresh-database verification, indexes/constraints reused | §22, §28 |
| 18 | Business date: no arbitrary backdating, server-authoritative timestamps, period-lock limitation documented | §19, §32 |
| 19 | Offline hooks only, no Phase 8/12 work | §20 |
| 20 | Security review with all mutations authorization-protected | §30 |
| 21 | Tests: unit/integration/concurrency/property-invariants, no filler, no weakened test; full suite ≥ 2 consecutive green runs | §24, §25 (two consecutive full sweeps, exit 0) |
| 22 | 16 quality gates recorded with exact commands/results; performance review documented | §25 (statics, tests, migration, both schema gates, seeds ×3, Phase 0, full sweep ×2), §31 |
| 23 | Docs: this report (34 sections) + `PROJECT_STATUS.md`; Phase 6 never marked approved | this document, §34 |
| 24 | Git/PR: clean implementation commit, pushed, PR #1 updated, no merge | §4, `PROJECT_STATUS.md` §1 |
| 25 | Prohibitions respected: no Phase 7+, no expenses/receivables/payables, no `transfers` product, no reports/dashboard, no Flutter, no offline sync, no second ledger, no float, no history mutation, no deleted cash rows, no placeholders | §1, §13, static audit of the diff |

## 34. Statement

**PHASE 6 — READY FOR REVIEW. PHASE 7 — NOT STARTED.**

Cash management is implemented, tested and documented: shifts open, carry, count and close; money
moves in, out and between accounts through the accounting engine as the single posting path;
variance is derived, stated only by an authorised compensating adjustment against 5090, and never
silently applied; posted history is immutable and corrections are reversals; the concurrency
behaviour is measured on real PostgreSQL rather than assumed; the schema is untouched and the
frozen lineage is intact.

This report is a claim of readiness, not a claim of approval. Only the human reviewer moves the
phase to APPROVED, and Phase 7 does not begin until that decision is recorded in
`docs/PROJECT_STATUS.md`.
