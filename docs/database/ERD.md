# NEXUS EXCHANGE ERP — Entity Relationship Model

| Field | Value |
| --- | --- |
| Document ID | `DB-ERD-001` |
| Version | 1.0 (Phase 0) |
| Status | **Proposed — pending Phase 0 approval** |
| Database | `nexus_exchange` (PostgreSQL 16, UTC) |
| Source of truth | `docs/database/schema.sql` (executable, verified on PostgreSQL 16.2) |

> **خلاصه فارسی** — این سند مدل داده و روابط بین ۳۱ جدول را نشان می‌دهد: هویت و دسترسی، سازمان (شعبه/دستگاه)، داده‌های پایه (ارز/مشتری/حساب)، هسته حسابداری (سند/ردیف)، معاملات (خرید و فروش ارز، حواله، صندوق، هزینه)، ممیزی و همگام‌سازی آفلاین. نکته کلیدی: `journal_lines` منبع حقیقت است و `account_balances` فقط یک کش قابل بازسازی است.

---

## 1. Domain map

```text
┌──────────────────────┐   ┌───────────────────────┐   ┌──────────────────────┐
│  IDENTITY & ACCESS   │   │     ORGANISATION      │   │      MASTER DATA     │
│  users               │──▶│  branches             │◀──│  currencies          │
│  roles               │   │  devices              │   │  customers           │
│  permissions         │   └───────────┬───────────┘   │  accounts            │
│  refresh_tokens      │               │               │  exchange_rates      │
└──────────┬───────────┘               │               └──────────┬───────────┘
           │                           │                          │
           ▼                           ▼                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                            ACCOUNTING CORE                                  │
│   journal_entries ──▶ journal_lines ──▶ accounts / currencies               │
│                             │                                               │
│                             └──▶ account_balances  (rebuildable cache)       │
└──────────┬──────────────────────────────────────────────────────────────────┘
           │ posted from
           ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              TRANSACTIONS                                   │
│  exchange_transactions        transfers        cash_movements + cash_sessions│
│  expenses                                                                   │
└──────────┬──────────────────────────────────────────────────────────────────┘
           │ emitted to
           ▼
┌───────────────────────────┐   ┌────────────────────────────────────────────┐
│         AUDIT             │   │        OFFLINE SYNC & CONTROL              │
│  audit_logs (hash chain)  │   │  sync_events, sync_conflicts, sync_cursors │
│  change_log (pull feed)   │   │  allocation_policies, device_allocations   │
└───────────────────────────┘   │  idempotency_keys, sequences               │
                                └────────────────────────────────────────────┘
```

## 2. Identity and access

```mermaid
erDiagram
    users ||--o{ user_roles : "is granted"
    roles ||--o{ user_roles : "grants"
    roles ||--o{ role_permissions : "grants"
    permissions ||--o{ role_permissions : "is granted by"
    users ||--o{ user_permissions : "explicit grant/deny"
    permissions ||--o{ user_permissions : "applies to"
    users ||--o{ refresh_tokens : "holds"
    devices ||--o{ refresh_tokens : "binds session to"
    users ||--o{ audit_logs : "acts"
    devices ||--o{ audit_logs : "originates"

    users {
        uuid id PK
        varchar username UK
        varchar email
        text password_hash "Argon2id"
        boolean is_active
        smallint failed_login_attempts
        timestamptz locked_until
    }
    roles {
        uuid id PK
        varchar name UK "SUPER_ADMIN..AUDITOR"
        boolean is_system
    }
    permissions {
        varchar code PK "resource.action"
    }
    user_roles {
        uuid user_id PK
        uuid role_id PK
    }
    user_permissions {
        uuid user_id PK
        varchar permission_code PK
        boolean is_granted "false = explicit deny"
        timestamptz expires_at
    }
    refresh_tokens {
        uuid id PK
        char token_hash UK "sha256, never the token"
        uuid family_id "rotation family"
        timestamptz used_at
        timestamptz revoked_at
    }
```

## 3. Organisation and master data

```mermaid
erDiagram
    branches ||--o{ devices : "hosts"
    branches ||--o{ customers : "scopes (optional)"
    branches ||--o{ accounts : "scopes (optional)"
    branches ||--o{ exchange_rates : "quotes (optional)"
    currencies ||--o{ currencies : ""
    currencies ||--o{ accounts : "denominates"
    currencies ||--o{ exchange_rates : "from"
    currencies ||--o{ exchange_rates : "to"
    accounts ||--o{ accounts : "parent of"
    users ||--o{ devices : "registers"

    branches {
        uuid id PK
        varchar code UK
        varchar timezone "display tz"
    }
    devices {
        uuid id PK
        uuid branch_id FK
        uuid device_uuid UK
        varchar platform "ANDROID|WINDOWS|WEB|IOS"
        timestamptz revoked_at
    }
    customers {
        uuid id PK
        varchar customer_code UK
        varchar full_name
        varchar phone
        uuid branch_id FK "NULL = shared"
        boolean is_active
    }
    accounts {
        uuid id PK
        varchar code UK
        varchar account_type "ASSET..EXPENSE"
        uuid currency_id FK
        uuid branch_id FK
        uuid parent_id FK
        char normal_balance "DEBIT|CREDIT"
        boolean is_postable
    }
    currencies {
        uuid id PK
        varchar code UK "ISO-like"
        smallint decimal_places
        boolean is_base "exactly one"
        boolean is_active
    }
    exchange_rates {
        uuid id PK
        uuid from_currency_id FK
        uuid to_currency_id FK
        numeric buy_rate
        numeric sell_rate
        timestamptz effective_at
        uuid branch_id FK "NULL = global quote"
    }
```

## 4. Accounting core

```mermaid
erDiagram
    journal_entries ||--o{ journal_lines : "contains"
    journal_entries ||--o| journal_entries : "reverses"
    accounts ||--o{ journal_lines : "posted to"
    currencies ||--o{ journal_lines : "denominates"
    accounts ||--o{ account_balances : "cached balance"
    currencies ||--o{ account_balances : "per currency"
    branches ||--o{ journal_entries : "books"
    devices ||--o{ journal_entries : "originates"

    journal_entries {
        uuid id PK
        varchar reference_type "EXCHANGE_TRANSACTION.."
        uuid reference_id "business document"
        timestamptz transaction_date
        uuid branch_id FK
        uuid reversal_of_id FK
        uuid created_by FK
    }
    journal_lines {
        uuid id PK
        uuid journal_entry_id FK
        uuid account_id FK
        numeric debit "functional currency"
        numeric credit "functional currency"
        uuid currency_id FK
        numeric exchange_rate
        numeric foreign_amount "GENERATED"
    }
    account_balances {
        uuid id PK
        uuid account_id FK
        uuid currency_id FK
        numeric debit_total
        numeric credit_total
    }
```

Ledger rules: `journal_lines` is append-only; one entry per `(reference_type, reference_id)`; an entry may be reversed at most once; `SUM(debit) = SUM(credit)` is checked at COMMIT. `account_balances` is a cache that `rebuild_account_balances()` reproduces exactly.

## 5. Transactions

```mermaid
erDiagram
    branches ||--o{ exchange_transactions : "books"
    devices ||--o{ exchange_transactions : "records"
    users ||--o{ exchange_transactions : "cashier"
    customers ||--o{ exchange_transactions : "counterparty"
    currencies ||--o{ exchange_transactions : "from/to"
    journal_entries ||--o| exchange_transactions : "posted entry"
    exchange_transactions ||--o| exchange_transactions : "reversal_of"
    cash_sessions ||--o{ exchange_transactions : "shift"
    branches ||--o{ transfers : "books"
    customers ||--o{ transfers : "payer (optional)"
    journal_entries ||--o| transfers : "posted entry"
    branches ||--o{ cash_sessions : "opens"
    devices ||--o{ cash_sessions : "binds"
    cash_sessions ||--o{ cash_session_lines : "reconciles"
    currencies ||--o{ cash_session_lines : "per currency"
    branches ||--o{ cash_movements : "moves"
    accounts ||--o{ cash_movements : "affects"
    cash_sessions ||--o{ cash_movements : "during"
    journal_entries ||--o| cash_movements : "posted entry"
    branches ||--o{ expenses : "incurs"

    exchange_transactions {
        uuid id PK
        varchar transaction_number UK "NX-YYYYMMDD-NNNNNN"
        varchar transaction_type "BUY|SELL"
        uuid from_currency_id FK
        numeric from_amount
        uuid to_currency_id FK
        numeric to_amount
        numeric exchange_rate
        numeric commission
        varchar status "PENDING|COMPLETED|CANCELLED|REVERSED"
        uuid reversal_of_id FK
        uuid client_event_id UK "offline idempotency"
        integer version "optimistic concurrency"
    }
    transfers {
        uuid id PK
        varchar reference_number UK "TR-YYYYMMDD-NNNNNN"
        varchar status "PENDING|APPROVED|PAID|CANCELLED"
        numeric amount
        numeric commission
        uuid approved_by FK
        uuid paid_by FK
        uuid cancelled_by FK
    }
    cash_movements {
        uuid id PK
        varchar movement_type "OPENING|IN|OUT|EXPENSE|ADJUSTMENT|CLOSING"
        numeric amount "physical quantity"
        smallint adjustment_sign
        numeric signed_amount "GENERATED"
        uuid reference_type "polymorphic link"
        uuid reference_id
    }
    cash_sessions {
        uuid id PK
        varchar status "OPEN|CLOSED"
        uuid opened_by FK
        uuid closed_by FK
    }
    cash_session_lines {
        uuid id PK
        numeric opening_declared
        numeric expected_amount
        numeric counted_amount
        numeric difference "counted - expected"
    }
    expenses {
        uuid id PK
        varchar category
        numeric amount
        varchar status "POSTED|CANCELLED"
    }
```

## 6. Audit, offline sync and control

```mermaid
erDiagram
    devices ||--o{ sync_events : "pushes"
    sync_events ||--o{ sync_conflicts : "may raise"
    devices ||--o{ sync_conflicts : "owns"
    devices ||--|| sync_cursors : "pull position"
    devices ||--o{ device_allocations : "granted"
    allocation_policies ||--o{ device_allocations : "instantiated as"
    users ||--o{ idempotency_keys : "replays"
    devices ||--o{ idempotency_keys : "originates"
    users ||--o{ sync_conflicts : "resolves"

    audit_logs {
        uuid id PK
        bigint seq UK "chain order"
        char prev_hash
        char chain_hash "sha256 link"
        varchar action "RESOURCE_VERB"
        varchar entity_type
        uuid entity_id
        jsonb old_data
        jsonb new_data
        inet ip_address
    }
    change_log {
        bigint seq PK "pull cursor"
        varchar entity_type
        uuid entity_id
        varchar operation "CREATE|UPDATE|CANCEL|REVERSE"
        jsonb payload
    }
    sync_events {
        uuid id PK
        uuid device_id FK
        uuid event_id UK "client UUID"
        varchar operation
        jsonb payload
        timestamptz client_timestamp
        varchar status "PENDING|APPLIED|DUPLICATE|REJECTED|CONFLICT|FAILED"
        jsonb result "replay answer"
    }
    sync_conflicts {
        uuid id PK
        varchar conflict_type
        varchar resolution "PENDING|SERVER_WINS|..."
    }
    device_allocations {
        uuid id PK
        numeric granted_amount
        numeric consumed_amount
        numeric released_amount
        varchar status "ACTIVE|EXPIRED|REVOKED|EXHAUSTED"
    }
    allocation_policies {
        uuid id PK
        numeric max_amount
        integer max_offline_minutes
    }
    idempotency_keys {
        uuid id PK
        uuid key
        varchar endpoint
        char request_hash
        jsonb response_body "stored replay answer"
        varchar status "IN_PROGRESS|COMPLETED|FAILED"
    }
```

## 7. Relationship catalogue

| Child | Parent | Cardinality | On delete | Rationale |
| --- | --- | --- | --- | --- |
| `user_roles.user_id` | `users.id` | N:1 | CASCADE | Pure join row |
| `user_roles.role_id` | `roles.id` | N:1 | CASCADE | Pure join row |
| `role_permissions.role_id` | `roles.id` | N:1 | CASCADE | Permission catalogue is code-owned |
| `role_permissions.permission_code` | `permissions.code` | N:1 | RESTRICT | A permission in use cannot vanish |
| `user_permissions.user_id` | `users.id` | N:1 | CASCADE | Grant/deny row |
| `refresh_tokens.user_id` | `users.id` | N:1 | CASCADE | Sessions die with the user row |
| `refresh_tokens.device_id` | `devices.id` | N:1 | (none) | Device revocation is enforced in the service, not by cascading |
| `devices.branch_id` | `branches.id` | N:1 | RESTRICT | A branch with devices cannot be removed |
| `devices.registered_by` / `revoked_by` | `users.id` | N:1 | RESTRICT | Attribution must survive |
| `customers.branch_id` | `branches.id` | N:1 | RESTRICT | NULL = shared customer |
| `accounts.currency_id` | `currencies.id` | N:1 | RESTRICT | Currency of the account |
| `accounts.branch_id` | `branches.id` | N:1 | RESTRICT | NULL = group-level account |
| `accounts.parent_id` | `accounts.id` | N:1 | RESTRICT | Chart tree |
| `exchange_rates.from_currency_id` / `to_currency_id` | `currencies.id` | N:1 | RESTRICT | Quotes reference live currencies |
| `journal_entries.reference_id` | business document | polymorphic | (none) | Soft link; integrity by `reference_type` CHECK + service |
| `journal_entries.reversal_of_id` | `journal_entries.id` | N:1 unique | (none) | At most one reversal per entry |
| `journal_lines.journal_entry_id` | `journal_entries.id` | N:1 | RESTRICT | Ledger rows are never orphaned |
| `journal_lines.account_id` | `accounts.id` | N:1 | RESTRICT | Posting target |
| `journal_lines.currency_id` | `currencies.id` | N:1 | RESTRICT | Mandatory currency context (deviation D-09) |
| `account_balances.account_id` + `currency_id` | `accounts.id`, `currencies.id` | 1:1 pair | RESTRICT | Cache key |
| `exchange_transactions.branch_id`, `cashier_id`, `device_id`, `customer_id` | branches/users/devices/customers | N:1 | RESTRICT | Attribution of a financial event |
| `exchange_transactions.cash_session_id` | `cash_sessions.id` | N:1 | (none) | Shift grouping |
| `exchange_transactions.reversal_of_id` | `exchange_transactions.id` | N:1 unique | (none) | Single reversal |
| `transfers.*` lifecycle actors | `users.id` | N:1 | RESTRICT | Who approved/paid/cancelled |
| `cash_sessions.branch_id`, `opened_by`, `closed_by` | branches/users | N:1 | RESTRICT | Shift attribution |
| `cash_session_lines.cash_session_id` | `cash_sessions.id` | N:1 | CASCADE | A draft shift's lines vanish with it (only while OPEN) |
| `cash_movements.*` | branches/accounts/currencies/sessions | N:1 | RESTRICT | Immutable movement rows |
| `expenses.branch_id`, `currency_id`, `created_by` | branches/currencies/users | N:1 | RESTRICT | Expense attribution |
| `audit_logs.user_id`, `device_id` | users/devices | N:1 | RESTRICT | An audit row may never lose its actor |
| `sync_events.device_id` | `devices.id` | N:1 | RESTRICT | Events belong to a device |
| `sync_conflicts.device_id`, `sync_event_id`, `resolved_by` | devices/sync_events/users | N:1 | RESTRICT | Conflict provenance |
| `sync_cursors.device_id` | `devices.id` | 1:1 | CASCADE | Cursor is device state |
| `device_allocations.device_id`, `policy_id` | devices/allocation_policies | N:1 | RESTRICT | Granted allowance provenance |
| `idempotency_keys.user_id`, `device_id` | users/devices | N:1 | RESTRICT | Replay scoping |

## 8. Index strategy (why each index exists)

| Access pattern | Index |
| --- | --- |
| Latest quote for a pair (exchange screen, every sale) | `ix_exchange_rates_pair_effective (from, to, effective_at DESC)` |
| Duplicate-quote prevention | `ux_exchange_rates_no_duplicate_instant` |
| Cashier shift / daily report by branch | `ix_exchange_transactions_branch_created`, `ix_exchange_transactions_cashier_created` |
| Customer statement | `ix_exchange_transactions_customer`, `ix_transfers_customer`, `ix_customers_phone` |
| Pending offline rows to reconcile | `ix_exchange_transactions_origin_pending` (partial) |
| Ledger drill-down | `ix_journal_lines_entry`, `ix_journal_lines_account`, `ix_journal_entries_date` |
| Trial balance by branch/date | `ix_journal_entries_branch_date` |
| Cash position / movement history | `ix_cash_movements_branch_currency`, `ix_cash_movements_session` |
| Audit investigation | `ix_audit_logs_entity`, `ix_audit_logs_action`, `ix_audit_logs_user` |
| Offline pull | `ix_change_log_occurred`, `ix_change_log_entity`, `ix_sync_cursors` PK |
| Conflicting offline events queue | `ix_sync_conflicts_pending` (partial), `ix_sync_events_device_status` |
| Name search on the counter | `ix_customers_full_name` (Phase 12 adds a `pg_trgm` GIN index where contrib is available) |

## 9. Data volume assumptions (planning baseline)

| Table | Rows/year (single branch, 300 tx/day) | Retention |
| --- | --- | --- |
| `exchange_transactions` | ~90,000 | 10 years |
| `journal_entries` / `journal_lines` | ~90,000 / ~300,000 | 10 years (legal) |
| `cash_movements` | ~200,000 | 10 years |
| `audit_logs` | ~1,000,000 | 10 years, monthly partitions planned (Phase 12) |
| `change_log` | ~1,000,000 | 180 days, then pruned by a maintenance task |
| `sync_events` | ~90,000 | 2 years |

Partitioning (`audit_logs`, `change_log`, `sync_events`, `journal_lines` by month) is a Phase 12 migration; the primary keys and access paths above are already partition-compatible (`seq`/`created_at` predicates).

## 10. Traceability

| Master prompt | Section |
| --- | --- |
| PART 6–19 | §2–§6 (entities) |
| PART 11, PART 12 | §4 |
| PART 18 | §6 (hash chain columns) |
| PART 19, PART 34 | §6 (sync state machine) |
| PART 37 | §6 (`allocation_policies`, `device_allocations`) |
| PART 40 | §6 (`idempotency_keys`) |
