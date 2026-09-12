# NEXUS EXCHANGE ERP — Offline-First & Synchronisation Design

| Field | Value |
| --- | --- |
| Document ID | `ARCH-SYNC-001` |
| Version | 1.0 (Phase 0) |
| Status | **Proposed — pending Phase 0 approval** |
| Principle | Offline Reliability > Internet Dependency, **but** Financial Accuracy > Convenience |
| Related | `docs/database/schema.sql`, `docs/architecture/ACCOUNTING_MODEL.md`, `docs/api/API_CONTRACT.md` |

> **خلاصه فارسی** — طراحی آفلاین-اول: دستگاه می‌تواند بدون اینترنت معامله ثبت کند، اما هرگز اجازه ندارد موجودی مشترک را خودسرانه افزایش دهد. کنترل‌ها: سهمیه آفلاین از پیش تخصیص‌یافته (allocation)، بلوک شماره سند پیش‌تخصیص‌شده، «عکس نرخ» دارای اعتبار زمانی، و تشخیص تعارض به‌جای Last-Write-Wins. سرور تنها مرجع نهایی است: هر رویداد با یک `event_id` یکتا ارسال می‌شود، سرور آن را idempotent پردازش می‌کند و نتیجه اول را برای درخواست‌های تکراری برمی‌گرداند.

---

## 1. Problem statement and non-negotiables

A currency exchange counter must keep working when the link drops — that is a commercial requirement, not a nice-to-have. But two devices that each "add a sale" to the same currency stock can invent money. The design therefore separates two concerns:

| Concern | Authority |
| --- | --- |
| *Operational continuity* (record a sale now, print a receipt) | The device, inside a server-granted envelope |
| *Financial truth* (balances, profit, the ledger) | The server, always |

**Rules that are never bent:**

1. The device may not exceed the allowance the server granted it (`device_allocations`).
2. The device may not invent a document number, a rate, or a customer — all come from server-granted ranges/snapshots.
3. Financial records never use "last write wins". A conflict is *detected and recorded*, never merged.
4. Balances are never combined by addition on the client. The server recomputes every balance from the ledger.
5. Local SQLite is a *temporary operational source*; after sync, server reconciliation is mandatory (PART 64).

## 2. Architecture

```text
┌───────────────────────────── DEVICE (Flutter) ─────────────────────────────┐
│  UI ─▶ Repository ─▶ Local DB (SQLite/SQLCipher, Drift)                    │
│                          │ 1. local validation (allocation, balance, rate) │
│                          │ 2. ONE local transaction:                     │
│                          │    business row + local journal + local audit  │
│                          │    + sync_queue row (same TX, all-or-nothing) │
│                          ▼                                                 │
│                     Sync Engine ──── Exponential backoff, batch, resume   │
└──────────────────────────────────┬─────────────────────────────────────────┘
                                   │ HTTPS  POST /api/v1/sync/push
                                   ▼
┌──────────────────────────── SERVER (FastAPI) ──────────────────────────────┐
│  sync_events (append-only, event_id UNIQUE)                                │
│    ├─ duplicate?  ─▶ return stored `result` (PART 34)                      │
│    ├─ validate: device active, allocation available, rate within snapshot, │
│    │            references exist, version/state valid                      │
│    ├─ apply via domain services (ExchangeService/CashService/…)            │
│    ├─ post journal + cash movement + audit in the SAME DB transaction      │
│    └─ record result (APPLIED | CONFLICT | REJECTED | FAILED)               │
│  change_log (per-entity feed) ──▶ GET /api/v1/sync/pull?since=<seq>        │
└────────────────────────────────────────────────────────────────────────────┘
```

## 3. Local database schema (Drift/SQLite, SQLCipher at rest)

Every synchronised table carries the PART 35 envelope: `id`, `server_id`, `local_version`, `sync_status`, `created_at`, `updated_at`.

`sync_status ∈ {SYNCED, PENDING, FAILED, CONFLICT}` (PART 35). Additional local columns per table: `row_version` (server `version` we last saw), `deleted_locally` (soft marker only; financial rows are never deleted), `last_error`.

| Local table | Mirrors | Notes |
| --- | --- | --- |
| `users_cache` | `users`(subset) | Never stores `password_hash`; only id, username, full_name, roles, permissions, `is_active` |
| `branches` + `devices_cache` | `branches`, `devices` | Current branch/device identity, `timezone` |
| `customers` | `customers` | Branch-scoped or shared; may be created offline with a provisional code from the granted block |
| `currencies` + `exchange_rates` | `currencies`, `exchange_rates` | Rates are a **snapshot** with `snapshot_id`, `valid_from`, `valid_until`, `tolerance_bps` |
| `accounts` + `journal_cache` | `accounts`, `journal_entries/lines` (local copy) | Local double-entry mirror so the device can show coherent totals offline |
| `exchange_transactions` | `exchange_transactions` | Local row holds the same amounts/rate as sent upstream (`Decimal`-as-string) |
| `cash_movements` | `cash_movements` | Opening/IN/OUT/ADJUSTMENT recorded offline; `CLOSING` only online |
| `transfers` | `transfers` | Offline transfers are restricted to `PENDING` (approval/payout require the server) |
| `expenses` | `expenses` | Offline expenses consume the granted allowance |
| `sync_queue` | — | Outbox: `event_id`, `entity_type`, `entity_id`, `operation`, `payload`, `client_timestamp`, `attempt_count`, `next_attempt_at`, `status`, `last_error` |
| `sync_conflicts` | `sync_conflicts` (server copy) | Conflicts surfaced to the operator with the server's version |
| `number_allocations` | — | Pre-granted document-number blocks (`NX-20260911-000101…000150`) |
| `allocations` | `device_allocations` (device's own slice) | Server-granted offline allowance, consumed locally |
| `settings` | — | Branch, printer, language, calculator defaults |

`sync_queue`, `number_allocations` and `allocations` are **additions** to PART 35, required to make its own rules implementable (`ROADMAP.md` ADR list).

Local integrity: the device writes the business row, the local journal, the local audit entry and the `sync_queue` row inside **one** SQLite transaction (PART 36). A crash either loses the whole sale or keeps all four.

## 4. Event envelope

```json
{
  "event_id": "9f1c2e0a-6a37-4c9c-9c2b-3fd1b7c4a001",
  "device_id": "44444444-4444-4444-4444-444444444401",
  "entity_type": "exchange_transaction",
  "entity_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01",
  "operation": "CREATE",
  "client_timestamp": "2026-09-11T08:14:03.117Z",
  "client_business_date": "2026-09-11",
  "client_sequence": 42,
  "row_version": null,
  "payload": { "...": "full business document, including journal lines and cash movements" }
}
```

| Field | Why it exists |
| --- | --- |
| `event_id` | Idempotency key for the whole event; unique in `sync_events` and in the entity's `client_event_id` |
| `client_sequence` | Per-device monotonic counter; makes gaps and reordering visible during reconciliation |
| `client_timestamp` | Evidence of when the operator acted; never trusted for business-date-critical logic on its own |
| `client_business_date` | Branch-local date used for the number block; if it disagrees with the server's branch date beyond a threshold, the event is flagged (`BUSINESS_DATE_SKEW`) |
| `row_version` | Expected server version, for `UPDATE`/`CANCEL`/`REVERSE` — the basis of `STALE_VERSION` detection |
| `payload` | Complete document (amounts as strings, journal lines, cash movements) so the server can validate rather than trust |

**Money on the wire is always a decimal string** (`"70000.0000000000"`), never a JSON number — JSON numbers are IEEE-754 doubles in most runtimes (PART 62).

## 5. Push protocol

`POST /api/v1/sync/push` accepts a batch (≤ 50 events, ≤ 1 MB) and returns a **per-event** result; one bad event never discards the batch:

```json
{
  "device_id": "…",
  "results": [
    {"event_id": "…", "status": "APPLIED",    "server_entity_id": "…", "server_reference": "NX-20260911-000104", "applied_at": "…"},
    {"event_id": "…", "status": "DUPLICATE",  "server_entity_id": "…", "server_reference": "NX-20260911-000104"},
    {"event_id": "…", "status": "CONFLICT",   "conflict": {"type": "ALLOCATION_EXCEEDED", "details": {"available": "0.00"}}},
    {"event_id": "…", "status": "REJECTED",   "error": {"code": "RATE_OUT_OF_TOLERANCE", "message": "…", "details": {}}}
  ],
  "server_time": "2026-09-11T08:20:41Z",
  "pull_cursor": 918233
}
```

Processing order and guarantees:

1. **Envelope validation** — device exists and is active, `operation` legal for `entity_type`, payload schema valid, `event_id` matches `payload.client_event_id`.
2. **Duplicate short-circuit** — if `event_id` was seen before, the stored `result` is returned verbatim; **nothing is applied again** (PART 34). Recorded as `DUPLICATE`.
3. **Authorisation** — the cashier bound to the device must hold the permission for the operation (offline-created events are still subject to server RBAC).
4. **Authority checks** — allowance, rate snapshot validity, references (customer/currency/account), row version, current state.
5. **Application** — the domain service applies the event inside one database transaction: document + journal + cash movement + audit + `sync_events.result` + `change_log` row.
6. **Failure isolation** — a failing event is marked `REJECTED`/`CONFLICT`/`FAILED` with `error_message`; the batch continues.

## 6. Pull protocol

`GET /api/v1/sync/pull?since=<seq>&limit=500` streams `change_log` rows (ordered by `seq`, cursor stored in `sync_cursors`):

```json
{
  "cursor": 918233,
  "has_more": true,
  "changes": [
    {"seq": 918200, "entity_type": "exchange_rates", "entity_id": "…", "operation": "CREATE",
     "occurred_at": "2026-09-11T08:16:00Z", "payload": {"...": "…"}}
  ],
  "rate_snapshot": {"snapshot_id": "…", "valid_until": "2026-09-11T12:00:00Z", "tolerance_bps": 50, "rates": []},
  "allocation": {"currency_id": "…", "remaining": "50000.0000000000", "window_end": "…"}
}
```

* Master data (currencies, rates, branches, customers, accounts, users/permissions) is **server-authoritative**: the pull is authoritative and local edits to it are rejected.
* Pull is resumable: the client persists `cursor` and re-requests; a server prune of `change_log` older than the retention window returns `410 CURSOR_EXPIRED`, and the client performs a **full resync** of master data before continuing.
* Rates and allowances are refreshed by every pull, so a device that reconnects briefly is fully governed again.

## 7. Conflict taxonomy (never "last write wins")

| Type | Trigger | Server behaviour | Client/operator resolution |
| --- | --- | --- | --- |
| `DUPLICATE_EVENT` | Same `event_id` re-sent | Return stored result, apply nothing | Client marks the row `SYNCED` (replay is safe and expected) |
| `STALE_VERSION` | `row_version` < current server version (e.g. two devices cancelling the same document) | Reject with the server's version attached | Operator sees both; the second action becomes a manual decision |
| `ALLOCATION_EXCEEDED` | Offline consumption exceeds the granted allowance | Reject; no ledger effect | Manager grants a new allocation; the operator may retry if the underlying sale is still valid |
| `INSUFFICIENT_BALANCE` | Ledger or cash position would go negative | Reject (`NEX01`) | Decide with the manager whether to record a correction, or void the local document (voiding is itself recorded) |
| `RATE_MISMATCH` | Applied rate outside the snapshot's `tolerance_bps`, or the snapshot expired | Reject; the server's quoted rate and window are attached | Re-price and re-post as a new event, or a manager approves a documented exception — both audited |
| `REFERENCE_MISSING` | Customer/currency/account unknown to the server | Reject | Sync the master data first, then retry |
| `ALREADY_REVERSED` | Reversal of an already reversed/cancelled document | Reject | None needed; the operator is informed |
| `DEVICE_REVOKED` | Device was revoked while offline | Reject with `403` and an instruction to re-register | Devices must be re-registered and re-authorised; local queue is preserved for audit, not for posting |
| `BUSINESS_DATE_SKEW` | `client_business_date` differs from the server's branch date beyond threshold | Event is **flagged**, then processed under the flag (the money is real) | An alert appears in the sync screen and the audit trail for the manager to review |

Recorded in `sync_conflicts` with `status = PENDING` until an operator resolves it (`SERVER_WINS`, `CLIENT_REPOSTED`, `VOIDED`, `MANUAL`); every resolution writes an audit entry.

## 8. Device clock, business date and time

| Aspect | Rule |
| --- | --- |
| Storage | `client_timestamp` and `server_timestamp` are both stored; the server's clock is authoritative for `created_at`, journal `transaction_date` and all reporting |
| Business date | Derived from `branches.timezone` on the server; the client sends its own business date only for number-block scoping and skew detection |
| Clock skew | If `|client_timestamp − server_timestamp| > 10 min`, the event is accepted but flagged in `sync_events.error_message` and surfaced in the sync screen |
| Offline duration | A device may operate offline until its allowance expires *or* `max_offline_minutes` elapses; after that the app transitions to **read-only offline** (can view history, cannot post new financial documents) — this is the hard boundary of PART 37 |

## 9. Offline financial envelope (PART 37 in concrete terms)

Four server controls make offline trading safe; all four are enforced *and* recorded:

1. **Allocation** — the server grants `device_allocations` per device and currency (e.g. 50,000 AFN between 08:00 and 20:00). The device refuses to post beyond `granted − consumed − released`. Draining is visible in the UI, in advance.
2. **Number blocks** — the server allocates document numbers per device per day (`number_allocations`). Numbering gaps are expected and harmless; duplicates are impossible.
3. **Rate snapshot** — rates are pushed with `valid_from`/`valid_until` and a `tolerance_bps`. Offline trades use the snapshot rate; the server re-validates the applied rate against it.
4. **Position guard** — the device maintains a local projection of its branch/currency position and refuses to dispense currency it believes it lacks. The server independently enforces the same rule at the ledger level (`ct_cash_movements_non_negative`).

Nothing in the device's arithmetic is trusted: the server recomputes amounts, carrying rates, commissions, FX results, journal lines and the resulting balances. If recomputation differs from the device's local journal, the **server's** figures are authoritative; the difference is recorded on the conflict/resolution record and visible to the accountant (never silently absorbed).

## 10. Sync state machines

**Client (`sync_status`)**

```text
PENDING ──push ok──▶ SYNCED
   │  ▲                 ▲
   │  │ retry           │ (server-only change)
   ▼  │                 │
 FAILED ────────────────┘
   │
   └──server conflict──▶ CONFLICT ──operator resolves──▶ SYNCED | VOIDED
```

**Server (`sync_events.status`)**: `PENDING → APPLIED | DUPLICATE | REJECTED | CONFLICT | FAILED`. `PENDING` is the only non-terminal state; an event's terminal state and its `result` are immutable once written.

## 11. Retry, backoff and dead-lettering

| Parameter | Value (default) | Notes |
| --- | --- | --- |
| Batch size | 50 events / 1 MB | Configurable |
| Retry policy | Exponential backoff with jitter: 2 s → 4 s → 8 s … capped at 5 min | Network/5xx only |
| Terminal failures | 4xx domain errors are **not** retried | `REJECTED`/`CONFLICT` go to the operator queue |
| Max attempts | 20, then `FAILED` + alert | Prevents an infinite loop hammering the server |
| Ordering | Events are applied in `client_sequence` order per device | Reordering within a batch is fixed by the server before applying |
| Backpressure | `429` with `Retry-After` respected; batch size halves automatically | Server-side rate limiting per device |

## 12. Reconciliation and detection of drift

| Job | Frequency | Action |
| --- | --- | --- |
| Device reconciliation report | On every successful pull | Server sends per-currency position and today's transaction totals; the device compares with its local projection and raises a visible warning on mismatch |
| Server drift check | Nightly (Celery) | `rebuild_account_balances()` compared against the cached balances; `verify_audit_chain()`; Σ debit = Σ credit; any mismatch raises a P1 alert |
| Stale device report | Nightly | Devices with `last_sync_at` older than their allowance window are listed; their grants are released (`released_amount`) |
| Allocation release | Nightly | Unexpired, unused allowance is released and re-granted for the next window |

## 13. API surface (contract detail in `API_CONTRACT.md` §9)

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/sync/push` | Batched, idempotent event ingestion (`Idempotency-Key` also accepted) |
| `GET` | `/api/v1/sync/pull` | Change feed + rate snapshot + allowance refresh |
| `GET` | `/api/v1/sync/status` | Device state: last sync, pending conflicts, allowance, rate-snapshot validity, retention health |
| `POST` | `/api/v1/sync/resolve-conflict` | Operator resolution of a `PENDING` conflict (permission-gated: `sync.resolve` for managers) |

## 14. Security of the offline path

| Concern | Control |
| --- | --- |
| Data at rest on the device | SQLCipher; key derived per installation (Keystore/DPAPI-backed) and never synced |
| Tokens | Access token short-lived; refresh token bound to the device row; device revocation invalidates both immediately (server-side check on every sync) |
| Tampering with the local DB | Local audit entries are hash-chained like the server's; the chain head is sent with each push so a wiped/edited local DB is detectable at reconciliation |
| Replay | `event_id` uniqueness plus `client_event_id` unique indexes on `exchange_transactions`, `transfers`, `cash_movements` |
| Sensitive data | Local cache holds no password hashes; customer PII is limited to what the receipt needs (PART 65) |
| Lost device | Remote revoke (`is_active = FALSE`, `revoked_at`) + optional remote wipe flag; the queue remains on the server for audit |

## 15. Test plan for the sync engine (PART 48)

| Test | Layer | Expected outcome |
| --- | --- | --- |
| Offline create | Flutter integration + API | Device posts sale offline; after reconnect the server applies it, posts journal/cash/audit, and returns `APPLIED` |
| Reconnect | Flutter integration | Queue drains in `client_sequence` order; local rows become `SYNCED`; pull updates rates/master data |
| Duplicate event | API integration | Same `event_id` twice → second returns `DUPLICATE` with the original reference; ledger has exactly one entry set |
| Conflict (double cancel) | API integration | Second `CANCEL` with a stale `row_version` → `STALE_VERSION`, `sync_conflicts` row, no ledger change |
| Allocation exhaustion | API integration | Offline trade beyond the grant → `ALLOCATION_EXCEEDED`; ledger unchanged; UI shows the remaining grant |
| Rate mismatch | API integration | Rate outside tolerance → `RATE_MISMATCH`, no ledger change until re-priced |
| Retry | API integration | 503 → retried with backoff → applied exactly once |
| Failed sync | API integration | Terminal 4xx → `REJECTED`, queue item parked, operator notified, no partial ledger write |
| Business-date skew | API integration | Device date ≠ branch date → event applied and flagged |
| Cursor expiry | API integration | Pruned cursor → `410 CURSOR_EXPIRED`, client full-resyncs |

## 16. Traceability

| Master prompt | Section |
| --- | --- |
| PART 19, PART 33 | §4, §5, §6, §13 |
| PART 34 | §5 (idempotency), §7 (no LWW) |
| PART 35, PART 36 | §3 |
| PART 37 | §9 |
| PART 40 | §5, §13 |
| PART 63, PART 64 | §1, §6 |
| PART 65 | §14 |
