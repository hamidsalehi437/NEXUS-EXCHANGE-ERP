-- =============================================================================
-- NEXUS EXCHANGE ERP — NORMATIVE REFERENCE SCHEMA (Phase 0)
-- =============================================================================
-- Document ID : DB-SCHEMA-001
-- Version     : 1.0 (Phase 0 — proposed, pending approval)
-- Target      : PostgreSQL 16
-- Database    : nexus_exchange
-- Timezone    : UTC (server); display timezone handled by the application layer
--
-- PURPOSE
--   This file is the normative, executable data definition for the system.
--   Phase 1 converts it into the initial Alembic revision. The SQLAlchemy models
--   MUST match this file; CI compares Alembic autogenerate output against the
--   live schema and fails on drift.
--
-- MONEY RULE (PART 62)
--   Every monetary/rate value is NUMERIC(30,10). FLOAT/REAL/DOUBLE PRECISION
--   appear nowhere in this schema. Literal checks at the bottom of the file
--   assert this at runtime.
--
-- COLUMN FIDELITY
--   Columns defined by the master prompt (PART 6–19) keep their exact names and
--   types. Columns added for integrity, multi-branch operation, security or
--   offline sync are explicitly tagged "(additive)" in a comment.
--
-- ADDITIVE EXTENSIONS (all documented in docs/database/SCHEMA.md §Decisions)
--   A1 users: lockout + last-login + password-age columns
--   A2 devices: revocation columns
--   A3 currencies: single-base-currency enforcement, decimal_places bounds
--   A4 customers: is_active, branch scoping, audit stamps
--   A5 journal_entries: branch_id (multi-branch reporting)
--   A6 exchange_rates: branch_id (branch-specific quotes) + no-duplicate-period
--   A7 exchange_transactions: reversal_of_id, reversal_reason, posted journal link
--   A8 transfers: lifecycle actors (approved/paid/cancelled) + customer link
--   A9 cash_movements: adjustment_sign, session link, generated signed_amount
--   A10 expenses: status + journal link
--   A11 audit_logs: seq + prev_hash + chain_hash (tamper-evident hash chain)
--   A12 sync_events: result payload, attempt counter, processing timestamps
--   A13 new tables: refresh_tokens, idempotency_keys, sequences, change_log,
--       account_balances, cash_sessions, cash_session_lines, sync_conflicts,
--       sync_cursors, allocation_policies, device_allocations
--
-- EXECUTION
--   psql -v ON_ERROR_STOP=1 -d nexus_exchange -f docs/database/schema.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 0. Extensions, schema, roles
-- ---------------------------------------------------------------------------
-- No PostgreSQL extensions are required. The schema uses only built-ins so it
-- applies to any PostgreSQL 16 build (managed services included):
--   * gen_random_uuid()          — core since PG 13
--   * sha256() / encode()        — core since PG 11 (audit hash chain)
--   * equality uniqueness        — a UNIQUE index replaces the EXCLUDE/btree_gist
--                                  constraint for rate periods (see §7)
-- Optional contrib-backed hardening (pg_trgm GIN search index) is described in
-- docs/database/SCHEMA.md §Optional hardening and is added by a separate Phase 1
-- migration for deployments that ship postgresql-contrib.

-- Group roles (NOLOGIN). Deployments create login roles that inherit from these.
-- infrastructure/postgres/init/*.sql performs the same steps for containers.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_owner') THEN
        CREATE ROLE nexus_owner NOLOGIN;          -- owns schema, runs migrations
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_app') THEN
        CREATE ROLE nexus_app NOLOGIN;            -- application runtime (DML, no DELETE on ledgers)
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_reader') THEN
        CREATE ROLE nexus_reader NOLOGIN;         -- read-only reporting / BI
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_auditor') THEN
        CREATE ROLE nexus_auditor NOLOGIN;        -- read-only on audit + ledgers
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 1. Enum-like domain constraints
--    The master prompt specifies VARCHAR columns (not native ENUM types) so that
--    values can be extended with a plain CHECK-constraint migration.
--    Every such column carries a named CHECK constraint listed below.
-- ---------------------------------------------------------------------------

-- Account types: ASSET | LIABILITY | EQUITY | REVENUE | EXPENSE
-- Exchange transaction types: BUY | SELL
-- Exchange transaction status: PENDING | COMPLETED | CANCELLED | REVERSED
-- Transfer status: PENDING | APPROVED | PAID | CANCELLED
-- Cash movement types: OPENING | IN | OUT | EXPENSE | ADJUSTMENT | CLOSING
-- Sync operations: CREATE | UPDATE | CANCEL | REVERSE

-- ---------------------------------------------------------------------------
-- 2. Shared trigger functions
-- ---------------------------------------------------------------------------

-- 2.1 updated_at maintenance -------------------------------------------------
CREATE OR REPLACE FUNCTION nexus_set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$;

-- 2.2 Append-only enforcement (PART 18 / PART 22 / PART 49) ------------------
CREATE OR REPLACE FUNCTION nexus_forbid_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'NEXUS_APPEND_ONLY: % is append-only; % is forbidden', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'P0001',
              HINT = 'Financial history is corrected by reversal, never by mutation (PART 22).';
END;
$$;

-- 2.3 Journal balance enforcement -------------------------------------------
-- Fires at COMMIT time (DEFERRABLE INITIALLY DEFERRED) on journal_lines.
-- Guarantees, per journal entry:
--   * at least 2 lines
--   * SUM(debit) = SUM(credit) exactly (NUMERIC comparison, no tolerance)
--   * no line with both debit and credit populated
--   * no line with debit = credit = 0
CREATE OR REPLACE FUNCTION nexus_assert_journal_balanced()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_entry_id   UUID;
    v_lines      INT;
    v_debit      NUMERIC(30,10);
    v_credit     NUMERIC(30,10);
    v_bad_lines  INT;
BEGIN
    -- NEW is unassigned on DELETE: branch on TG_OP instead of touching NEW blindly.
    IF TG_OP = 'DELETE' THEN
        v_entry_id := OLD.journal_entry_id;
    ELSE
        v_entry_id := NEW.journal_entry_id;
    END IF;

    SELECT COUNT(*),
           COALESCE(SUM(debit), 0),
           COALESCE(SUM(credit), 0),
           COUNT(*) FILTER (
               WHERE (debit > 0 AND credit > 0)
                  OR (debit = 0 AND credit = 0)
                  OR debit < 0
                  OR credit < 0
           )
      INTO v_lines, v_debit, v_credit, v_bad_lines
      FROM journal_lines
     WHERE journal_entry_id = v_entry_id;

    -- The entry was deleted as part of the same transaction (cascade cleanup).
    IF v_lines = 0 THEN
        RETURN NULL;
    END IF;

    IF v_bad_lines > 0 THEN
        RAISE EXCEPTION 'NEXUS_JOURNAL_LINE_INVALID: entry % has % invalid line(s)', v_entry_id, v_bad_lines
            USING ERRCODE = 'NEX02',
                  HINT = 'Each line must have exactly one side > 0 and the other = 0; negatives are not allowed.';
    END IF;

    IF v_lines < 2 THEN
        RAISE EXCEPTION 'NEXUS_JOURNAL_UNBALANCED: entry % has % line(s); minimum is 2', v_entry_id, v_lines
            USING ERRCODE = 'NEX02';
    END IF;

    IF v_debit <> v_credit THEN
        RAISE EXCEPTION 'NEXUS_JOURNAL_UNBALANCED: entry % debit=% credit=%', v_entry_id, v_debit, v_credit
            USING ERRCODE = 'NEX02',
            HINT = 'Total Debit must equal Total Credit (PART 49).';
    END IF;

    RETURN NULL;
END;
$$;

-- 2.4 Balance cache maintenance ---------------------------------------------
-- account_balances is a REBUILDABLE CACHE, never the source of truth.
-- Source of truth = journal_lines. rebuild_account_balances() restores it.
-- Statement-level trigger: one upsert batch per posting statement.
CREATE OR REPLACE FUNCTION nexus_maintain_account_balances()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO account_balances (account_id, currency_id, debit_total, credit_total, updated_at)
    SELECT l.account_id,
           l.currency_id,
           SUM(l.debit),
           SUM(l.credit),
           NOW()
      FROM new_journal_lines l
     GROUP BY l.account_id, l.currency_id
    ON CONFLICT (account_id, currency_id) DO UPDATE
       SET debit_total  = account_balances.debit_total  + EXCLUDED.debit_total,
           credit_total = account_balances.credit_total + EXCLUDED.credit_total,
           updated_at   = NOW();
    RETURN NULL;
END;
$$;

-- 2.5 Audit hash chain -------------------------------------------------------
-- Serialises audit inserts with a transaction-scoped advisory lock so that
-- prev_hash always references the immediately preceding row. Any UPDATE or
-- DELETE of a historical row breaks verify_audit_chain().
CREATE OR REPLACE FUNCTION nexus_audit_chain()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_prev_hash CHAR(64);
BEGIN
    PERFORM pg_advisory_xact_lock(918273645);

    SELECT chain_hash INTO v_prev_hash
      FROM audit_logs
     ORDER BY seq DESC
     LIMIT 1;

    IF v_prev_hash IS NULL THEN
        v_prev_hash := repeat('0', 64);
    END IF;

    NEW.prev_hash := v_prev_hash;
    NEW.chain_hash := encode(
        sha256(
            convert_to(v_prev_hash || '|' ||
            NEW.id::text                      || '|' ||
            COALESCE(NEW.user_id::text, '')   || '|' ||
            COALESCE(NEW.device_id::text, '') || '|' ||
            NEW.action                        || '|' ||
            NEW.entity_type                   || '|' ||
            COALESCE(NEW.entity_id::text, '') || '|' ||
            COALESCE(NEW.old_data::text, '')  || '|' ||
            COALESCE(NEW.new_data::text, '')  || '|' ||
            COALESCE(NEW.ip_address::text, '') || '|' ||
            to_char(NEW.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
            'UTF8'
        )),
        'hex'
    );
    RETURN NEW;
END;
$$;

-- Verifies the chain from p_from_seq onwards. Returns the first broken link.
CREATE OR REPLACE FUNCTION verify_audit_chain(p_from_seq BIGINT DEFAULT 0)
RETURNS TABLE (broken_seq BIGINT, expected_hash CHAR(64), stored_hash CHAR(64))
LANGUAGE plpgsql
AS $$
DECLARE
    r            RECORD;
    v_prev_hash  CHAR(64) := repeat('0', 64);
    v_expected   CHAR(64);
BEGIN
    FOR r IN
        SELECT * FROM audit_logs WHERE seq > p_from_seq ORDER BY seq
    LOOP
        IF r.prev_hash <> v_prev_hash THEN
            broken_seq    := r.seq;
            expected_hash := v_prev_hash;
            stored_hash   := r.prev_hash;
            RETURN NEXT;
            RETURN;
        END IF;

        v_expected := encode(
            sha256(
                convert_to(v_prev_hash || '|' ||
                r.id::text                        || '|' ||
                COALESCE(r.user_id::text, '')     || '|' ||
                COALESCE(r.device_id::text, '')   || '|' ||
                r.action                          || '|' ||
                r.entity_type                     || '|' ||
                COALESCE(r.entity_id::text, '')   || '|' ||
                COALESCE(r.old_data::text, '')    || '|' ||
                COALESCE(r.new_data::text, '')    || '|' ||
                COALESCE(r.ip_address::text, '')  || '|' ||
                to_char(r.created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US'),
                'UTF8'
            )),
            'hex'
        );

        IF v_expected <> r.chain_hash THEN
            broken_seq    := r.seq;
            expected_hash := v_expected;
            stored_hash   := r.chain_hash;
            RETURN NEXT;
            RETURN;
        END IF;

        v_prev_hash := r.chain_hash;
    END LOOP;
END;
$$;

-- 2.6 Change stream for offline sync pull (docs/architecture/SYNC_DESIGN.md) --
CREATE OR REPLACE FUNCTION nexus_log_change()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_op         VARCHAR(20);
    v_payload    JSONB;
    v_branch     UUID;
    v_new_status TEXT;
    v_old_status TEXT;
BEGIN
    -- Status is read through jsonb so this function can serve tables that do not
    -- have a status column (branches, currencies, customers, accounts, rates).
    v_new_status := to_jsonb(NEW) ->> 'status';

    IF TG_OP = 'INSERT' THEN
        v_op := 'CREATE';
    ELSIF TG_OP = 'UPDATE' THEN
        v_old_status := to_jsonb(OLD) ->> 'status';
        IF v_new_status = 'CANCELLED' AND v_old_status IS DISTINCT FROM 'CANCELLED' THEN
            v_op := 'CANCEL';
        ELSIF v_new_status = 'REVERSED' AND v_old_status IS DISTINCT FROM 'REVERSED' THEN
            v_op := 'REVERSE';
        ELSE
            v_op := 'UPDATE';
        END IF;
    ELSE
        RAISE EXCEPTION 'NEXUS: nexus_log_change does not support %', TG_OP
            USING ERRCODE = 'P0001';
    END IF;

    v_payload := to_jsonb(NEW);
    -- Never propagate secret material through the change stream.
    v_payload := v_payload - 'password_hash' - 'token_hash' - 'api_key_hash';

    BEGIN
        v_branch := (v_payload ->> 'branch_id')::UUID;
    EXCEPTION WHEN OTHERS THEN
        v_branch := NULL;
    END;

    INSERT INTO change_log (entity_type, entity_id, operation, branch_id, payload)
    VALUES (TG_TABLE_NAME, NEW.id, v_op, v_branch, v_payload);

    RETURN NULL;
END;
$$;

-- 2.7 Document numbering -----------------------------------------------------
-- Atomic, gap-tolerant, concurrency-safe. The period is part of both the counter
-- scope and the emitted number, so numbers can never collide across days.
--   next_document_number('NX', 'exchange_transaction', '20260911')
--     -> NX-20260911-000001
--   next_document_number('TR', 'transfer', '20260911')
--     -> TR-20260911-000001
-- p_period is supplied by the service as the branch BUSINESS date (not the UTC
-- date) so a counter rollover matches the operator's working day.
CREATE OR REPLACE FUNCTION next_document_number(
    p_prefix TEXT,
    p_scope  TEXT,
    p_period TEXT DEFAULT to_char(NOW() AT TIME ZONE 'UTC', 'YYYYMMDD'),
    p_width  INTEGER DEFAULT 6
)
RETURNS TEXT
LANGUAGE plpgsql
AS $$
DECLARE
    v_value BIGINT;
    v_name  TEXT;
BEGIN
    IF p_period !~ '^[0-9]{8}$' THEN
        RAISE EXCEPTION 'NEXUS_BAD_PERIOD: expected YYYYMMDD, got %', p_period
            USING ERRCODE = '22007';
    END IF;

    IF p_width < 4 OR p_width > 12 THEN
        RAISE EXCEPTION 'NEXUS_BAD_WIDTH: expected 4..12, got %', p_width
            USING ERRCODE = '22023';
    END IF;

    v_name := p_scope || ':' || p_period;

    INSERT INTO sequences (name, current_value)
    VALUES (v_name, 1)
    ON CONFLICT (name) DO UPDATE
       SET current_value = sequences.current_value + 1,
           updated_at    = NOW()
    RETURNING current_value INTO v_value;

    RETURN p_prefix || '-' || p_period || '-' || lpad(v_value::text, p_width, '0');
END;
$$;

-- ---------------------------------------------------------------------------
-- 3. Identity & access
-- ---------------------------------------------------------------------------

-- PART 6 — users (+ additive A1)
CREATE TABLE users (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    username               VARCHAR(100) NOT NULL UNIQUE,
    email                  VARCHAR(255),
    password_hash          TEXT NOT NULL,
    full_name              VARCHAR(200) NOT NULL,
    phone                  VARCHAR(50),
    is_active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive A1) authentication policy state
    last_login_at          TIMESTAMPTZ,
    password_changed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    failed_login_attempts  SMALLINT NOT NULL DEFAULT 0,
    locked_until           TIMESTAMPTZ,
    must_change_password   BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT ck_users_username_format CHECK (username ~ '^[A-Za-z0-9._-]{3,100}$'),
    CONSTRAINT ck_users_failed_attempts CHECK (failed_login_attempts >= 0)
);
CREATE UNIQUE INDEX ux_users_username_lower ON users (lower(username));
CREATE UNIQUE INDEX ux_users_email_lower ON users (lower(email)) WHERE email IS NOT NULL;

-- PART 6 — roles
CREATE TABLE roles (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         VARCHAR(100) NOT NULL UNIQUE,
    description  TEXT,
    is_system    BOOLEAN NOT NULL DEFAULT FALSE,   -- (additive) seeded roles are not deletable
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_roles_name_upper CHECK (name = upper(name)),
    CONSTRAINT ck_roles_name_format CHECK (name ~ '^[A-Z_]{3,100}$')
);

-- PART 6 — user_roles
CREATE TABLE user_roles (
    user_id  UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role_id  UUID NOT NULL REFERENCES roles (id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);

-- Permission catalogue (PART 41). Permissions are code-owned, not user-created.
CREATE TABLE permissions (
    code         VARCHAR(100) PRIMARY KEY,
    description  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_permissions_code_format CHECK (code ~ '^[a-z_]+\.[a-z_]+$')
);

CREATE TABLE role_permissions (
    role_id          UUID NOT NULL REFERENCES roles (id) ON DELETE CASCADE,
    permission_code  VARCHAR(100) NOT NULL REFERENCES permissions (code) ON DELETE RESTRICT,
    granted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (role_id, permission_code)
);

-- Explicit per-user grants/denials (break-glass and scope restrictions).
CREATE TABLE user_permissions (
    user_id          UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    permission_code  VARCHAR(100) NOT NULL REFERENCES permissions (code) ON DELETE RESTRICT,
    is_granted       BOOLEAN NOT NULL,           -- FALSE = explicit deny (wins over role grant)
    granted_by       UUID REFERENCES users (id),
    granted_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ,
    reason           TEXT,
    PRIMARY KEY (user_id, permission_code)
);

-- Refresh-token rotation families (PART 24 / PART 42)
CREATE TABLE refresh_tokens (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    device_id        UUID,                        -- FK added after devices exists
    family_id        UUID NOT NULL,
    parent_id        UUID REFERENCES refresh_tokens (id) ON DELETE SET NULL,  -- (additive A13)
    token_hash       CHAR(64) NOT NULL UNIQUE,    -- sha256 of the opaque token value
    issued_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at       TIMESTAMPTZ NOT NULL,
    used_at          TIMESTAMPTZ,
    revoked_at       TIMESTAMPTZ,
    revoked_reason   VARCHAR(100),
    replaced_by_id   UUID REFERENCES refresh_tokens (id) ON DELETE SET NULL,
    ip_address       INET,
    user_agent       TEXT,
    CONSTRAINT ck_refresh_tokens_expiry CHECK (expires_at > issued_at)
);
CREATE INDEX ix_refresh_tokens_user_active ON refresh_tokens (user_id, expires_at) WHERE revoked_at IS NULL;
CREATE INDEX ix_refresh_tokens_family ON refresh_tokens (family_id);

-- ---------------------------------------------------------------------------
-- 4. Organisation
-- ---------------------------------------------------------------------------

-- PART 7 — branches
CREATE TABLE branches (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code        VARCHAR(50) NOT NULL UNIQUE,
    name        VARCHAR(200) NOT NULL,
    address     TEXT,
    phone       VARCHAR(50),
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    timezone    VARCHAR(64) NOT NULL DEFAULT 'Asia/Kabul',   -- (additive) display timezone
    CONSTRAINT ck_branches_code_format CHECK (code ~ '^[A-Z0-9][A-Z0-9-]{1,19}$'),
    CONSTRAINT ck_branches_timezone_format CHECK (timezone ~ '^[A-Za-z_]+/[A-Za-z_+-]+$')
);

-- PART 8 — devices (+ additive A2)
CREATE TABLE devices (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id         UUID NOT NULL REFERENCES branches (id),
    device_uuid       UUID NOT NULL UNIQUE,
    device_name       VARCHAR(200) NOT NULL,
    platform          VARCHAR(50) NOT NULL,
    is_active         BOOLEAN NOT NULL DEFAULT TRUE,
    last_sync_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive A2) registration + revocation + connectivity
    registered_by     UUID REFERENCES users (id),
    app_version       VARCHAR(50),
    last_seen_at      TIMESTAMPTZ,
    revoked_at        TIMESTAMPTZ,
    revoked_by        UUID REFERENCES users (id),
    revoke_reason     TEXT,
    CONSTRAINT ck_devices_platform CHECK (platform IN ('ANDROID', 'WINDOWS', 'WEB', 'IOS')),
    CONSTRAINT ck_devices_revocation CHECK (
        (revoked_at IS NULL AND revoked_by IS NULL)
        OR (revoked_at IS NOT NULL)
    ),
    CONSTRAINT ck_devices_revoked_inactive CHECK (revoked_at IS NULL OR is_active = FALSE)
);
CREATE INDEX ix_devices_branch ON devices (branch_id) WHERE is_active;

-- ---------------------------------------------------------------------------
-- 5. Master data
-- ---------------------------------------------------------------------------

-- PART 9 — currencies (+ additive A3)
CREATE TABLE currencies (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code            VARCHAR(10) NOT NULL UNIQUE,
    name            VARCHAR(100) NOT NULL,
    symbol          VARCHAR(20),
    decimal_places  SMALLINT NOT NULL DEFAULT 2,
    is_base         BOOLEAN NOT NULL DEFAULT FALSE,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive A3)
    is_tradable     BOOLEAN NOT NULL DEFAULT TRUE,
    display_order   SMALLINT NOT NULL DEFAULT 0,
    CONSTRAINT ck_currencies_code_format CHECK (code ~ '^[A-Z]{3,10}$'),
    CONSTRAINT ck_currencies_decimal_places CHECK (decimal_places BETWEEN 0 AND 6)
);
-- Exactly one base currency may exist at any time.
CREATE UNIQUE INDEX ux_currencies_single_base ON currencies ((is_base)) WHERE is_base;

-- PART 10 — customers (+ additive A4)
CREATE TABLE customers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_code   VARCHAR(50) NOT NULL UNIQUE,
    full_name       VARCHAR(200) NOT NULL,
    phone           VARCHAR(50),
    address         TEXT,
    notes           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive A4) lifecycle + scoping + audit stamps
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    branch_id       UUID REFERENCES branches (id),          -- NULL = shared across branches
    created_by      UUID REFERENCES users (id),
    updated_by      UUID REFERENCES users (id),
    national_id_last4 VARCHAR(4),                            -- PII-minimal (PART 65)
    CONSTRAINT ck_customers_code_format CHECK (customer_code ~ '^[A-Z0-9-]{3,50}$'),
    CONSTRAINT ck_customers_full_name_len CHECK (char_length(btrim(full_name)) >= 2)
);
CREATE INDEX ix_customers_full_name ON customers (full_name);
CREATE INDEX ix_customers_phone ON customers (phone);
CREATE INDEX ix_customers_branch ON customers (branch_id) WHERE is_active;

-- ---------------------------------------------------------------------------
-- 6. Accounting core (PART 11 / PART 12)
-- ---------------------------------------------------------------------------

-- Chart of accounts. account_type: ASSET | LIABILITY | EQUITY | REVENUE | EXPENSE
CREATE TABLE accounts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code          VARCHAR(50) NOT NULL UNIQUE,
    name          VARCHAR(200) NOT NULL,
    account_type  VARCHAR(50) NOT NULL,
    currency_id   UUID REFERENCES currencies (id),
    branch_id     UUID REFERENCES branches (id),
    parent_id     UUID REFERENCES accounts (id),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive) posting control + normal balance + audit stamp
    is_postable   BOOLEAN NOT NULL DEFAULT TRUE,
    normal_balance CHAR(6),                       -- 'DEBIT' | 'CREDIT' (derived at seed time)
    created_by    UUID REFERENCES users (id),
    CONSTRAINT ck_accounts_type CHECK (
        account_type IN ('ASSET', 'LIABILITY', 'EQUITY', 'REVENUE', 'EXPENSE')
    ),
    CONSTRAINT ck_accounts_normal_balance CHECK (
        normal_balance IS NULL OR normal_balance IN ('DEBIT', 'CREDIT')
    ),
    CONSTRAINT ck_accounts_no_self_parent CHECK (parent_id IS NULL OR parent_id <> id)
);
CREATE INDEX ix_accounts_parent ON accounts (parent_id);
CREATE INDEX ix_accounts_branch ON accounts (branch_id);
CREATE INDEX ix_accounts_type ON accounts (account_type);

-- PART 12 — journal_entries (+ additive A5)
CREATE TABLE journal_entries (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reference_type    VARCHAR(50) NOT NULL,
    reference_id      UUID,
    description       TEXT,
    transaction_date  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by        UUID REFERENCES users (id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive A5)
    branch_id         UUID REFERENCES branches (id),
    device_id         UUID REFERENCES devices (id),
    reversal_of_id    UUID REFERENCES journal_entries (id),
    CONSTRAINT ck_journal_entries_reference_type CHECK (
        reference_type IN (
            'EXCHANGE_TRANSACTION', 'CASH_MOVEMENT', 'TRANSFER', 'EXPENSE',
            'OPENING_BALANCE', 'MANUAL_ADJUSTMENT', 'REVERSAL'
        )
    ),
    CONSTRAINT ck_journal_entries_no_self_reversal CHECK (reversal_of_id IS NULL OR reversal_of_id <> id)
);
CREATE INDEX ix_journal_entries_reference ON journal_entries (reference_type, reference_id);
CREATE INDEX ix_journal_entries_date ON journal_entries (transaction_date);
CREATE INDEX ix_journal_entries_branch_date ON journal_entries (branch_id, transaction_date);
-- A journal entry may be reversed at most once.
CREATE UNIQUE INDEX ux_journal_entries_reversed_once ON journal_entries (reversal_of_id)
    WHERE reversal_of_id IS NOT NULL;
-- Duplicate posting is impossible: one journal entry per business document.
-- (Reversal entries use reference_type = 'REVERSAL', so they are not blocked.)
CREATE UNIQUE INDEX ux_journal_entries_one_per_reference
    ON journal_entries (reference_type, reference_id)
    WHERE reference_id IS NOT NULL AND reference_type <> 'MANUAL_ADJUSTMENT';

-- PART 12 — journal_lines
-- MONEY SEMANTICS (see docs/architecture/ACCOUNTING_MODEL.md §3)
--   debit / credit : value in the FUNCTIONAL (base) currency. This is what makes
--                    SUM(debit) = SUM(credit) meaningful inside one entry.
--   currency_id    : the currency context of the account being moved (NOT NULL —
--                    deviation D-09 in SCHEMA.md; an unclassifiable line is a bug).
--   exchange_rate  : units of functional currency per 1 unit of currency_id (1 for base).
--   foreign_amount : generated quantity in currency_id = (debit + credit) / exchange_rate.
--                    Meaningful for currency-denominated inventory accounts.
CREATE TABLE journal_lines (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    journal_entry_id  UUID NOT NULL REFERENCES journal_entries (id) ON DELETE RESTRICT,
    account_id        UUID NOT NULL REFERENCES accounts (id) ON DELETE RESTRICT,
    debit             NUMERIC(30,10) NOT NULL DEFAULT 0,
    credit            NUMERIC(30,10) NOT NULL DEFAULT 0,
    currency_id       UUID NOT NULL REFERENCES currencies (id),
    exchange_rate     NUMERIC(30,10) NOT NULL DEFAULT 1,
    description       TEXT,
    foreign_amount    NUMERIC(30,10) GENERATED ALWAYS AS (
        CASE
            WHEN exchange_rate IS NULL OR exchange_rate = 0 THEN NULL
            ELSE (debit + credit) / exchange_rate
        END
    ) STORED,
    CONSTRAINT ck_journal_lines_debit_non_negative  CHECK (debit  >= 0),
    CONSTRAINT ck_journal_lines_credit_non_negative CHECK (credit >= 0),
    CONSTRAINT ck_journal_lines_rate_positive CHECK (exchange_rate IS NULL OR exchange_rate > 0),
    CONSTRAINT ck_journal_lines_single_sided CHECK (
        (debit > 0 AND credit = 0) OR (credit > 0 AND debit = 0)
    )
);
CREATE INDEX ix_journal_lines_entry ON journal_lines (journal_entry_id);
CREATE INDEX ix_journal_lines_account ON journal_lines (account_id);
CREATE INDEX ix_journal_lines_currency ON journal_lines (currency_id);

-- Balances cache (additive A13) — rebuildable from journal_lines only.
CREATE TABLE account_balances (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id    UUID NOT NULL REFERENCES accounts (id) ON DELETE RESTRICT,
    currency_id   UUID NOT NULL REFERENCES currencies (id) ON DELETE RESTRICT,
    debit_total   NUMERIC(30,10) NOT NULL DEFAULT 0,
    credit_total  NUMERIC(30,10) NOT NULL DEFAULT 0,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ux_account_balances_account_currency UNIQUE (account_id, currency_id)
);

-- Deterministic rebuild of the cache (also used by the invariant test suite).
CREATE OR REPLACE FUNCTION rebuild_account_balances()
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    DELETE FROM account_balances;
    INSERT INTO account_balances (account_id, currency_id, debit_total, credit_total, updated_at)
    SELECT l.account_id, l.currency_id, SUM(l.debit), SUM(l.credit), NOW()
      FROM journal_lines l
     WHERE l.currency_id IS NOT NULL
     GROUP BY l.account_id, l.currency_id;
END;
$$;

-- ---------------------------------------------------------------------------
-- 7. Rates (PART 13) (+ additive A6)
-- ---------------------------------------------------------------------------
CREATE TABLE exchange_rates (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_currency_id  UUID NOT NULL REFERENCES currencies (id),
    to_currency_id    UUID NOT NULL REFERENCES currencies (id),
    buy_rate          NUMERIC(30,10) NOT NULL,
    sell_rate         NUMERIC(30,10) NOT NULL,
    effective_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by        UUID REFERENCES users (id),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive A6)
    branch_id         UUID REFERENCES branches (id),   -- NULL = global quote
    source            VARCHAR(50) NOT NULL DEFAULT 'MANUAL',
    CONSTRAINT ck_exchange_rates_distinct_currencies CHECK (from_currency_id <> to_currency_id),
    CONSTRAINT ck_exchange_rates_positive CHECK (buy_rate > 0 AND sell_rate > 0),
    CONSTRAINT ck_exchange_rates_source CHECK (source IN ('MANUAL', 'IMPORT', 'CENTRAL_BANK', 'PARTNER'))
);
CREATE INDEX ix_exchange_rates_pair_effective
    ON exchange_rates (from_currency_id, to_currency_id, effective_at DESC);
CREATE INDEX ix_exchange_rates_branch
    ON exchange_rates (branch_id, effective_at DESC);
-- No two quotes for the same pair (and branch scope) may share an effective_at
-- timestamp. Equality-only exclusion is exactly a unique index, which avoids a
-- btree_gist dependency (the sentinel UUID stands for "global quote").
CREATE UNIQUE INDEX ux_exchange_rates_no_duplicate_instant
    ON exchange_rates (
        from_currency_id,
        to_currency_id,
        COALESCE(branch_id, '00000000-0000-0000-0000-000000000000'::uuid),
        effective_at
    );

-- Resolve the quote in force at a given instant (branch quote wins over global).
CREATE OR REPLACE FUNCTION resolve_exchange_rate(
    p_from_currency UUID,
    p_to_currency   UUID,
    p_branch        UUID,
    p_at            TIMESTAMPTZ DEFAULT NOW()
)
RETURNS TABLE (
    exchange_rate_id UUID,
    buy_rate         NUMERIC(30,10),
    sell_rate        NUMERIC(30,10),
    effective_at     TIMESTAMPTZ,
    branch_id        UUID
)
LANGUAGE sql
STABLE
AS $$
    SELECT r.id, r.buy_rate, r.sell_rate, r.effective_at, r.branch_id
      FROM exchange_rates r
     WHERE r.from_currency_id = p_from_currency
       AND r.to_currency_id   = p_to_currency
       AND r.effective_at    <= p_at
       AND (r.branch_id IS NULL OR r.branch_id = p_branch)
     ORDER BY (r.branch_id IS NOT NULL) DESC, r.effective_at DESC
     LIMIT 1;
$$;

-- ---------------------------------------------------------------------------
-- 8. Exchange transactions (PART 14) (+ additive A7)
-- ---------------------------------------------------------------------------
CREATE TABLE exchange_transactions (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    transaction_number   VARCHAR(50) NOT NULL UNIQUE,
    branch_id            UUID NOT NULL REFERENCES branches (id),
    device_id            UUID REFERENCES devices (id),
    cashier_id           UUID NOT NULL REFERENCES users (id),
    customer_id          UUID REFERENCES customers (id),
    transaction_type     VARCHAR(20) NOT NULL,
    from_currency_id     UUID NOT NULL REFERENCES currencies (id),
    from_amount          NUMERIC(30,10) NOT NULL,
    to_currency_id       UUID NOT NULL REFERENCES currencies (id),
    to_amount            NUMERIC(30,10) NOT NULL,
    exchange_rate        NUMERIC(30,10) NOT NULL,
    commission           NUMERIC(30,10) NOT NULL DEFAULT 0,
    status               VARCHAR(30) NOT NULL DEFAULT 'COMPLETED',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive A7) reversal + posting links + optimistic concurrency + offline origin
    reversal_of_id       UUID REFERENCES exchange_transactions (id),
    reversal_reason      TEXT,
    reversed_by          UUID REFERENCES users (id),
    reversed_at          TIMESTAMPTZ,
    journal_entry_id     UUID REFERENCES journal_entries (id),
    reversal_journal_entry_id UUID REFERENCES journal_entries (id),
    version              INTEGER NOT NULL DEFAULT 1,
    origin               VARCHAR(20) NOT NULL DEFAULT 'ONLINE',
    client_event_id      UUID,
    cash_session_id      UUID,                       -- FK added after cash_sessions exists
    CONSTRAINT ck_exchange_transactions_type CHECK (transaction_type IN ('BUY', 'SELL')),
    CONSTRAINT ck_exchange_transactions_status CHECK (
        status IN ('PENDING', 'COMPLETED', 'CANCELLED', 'REVERSED')
    ),
    CONSTRAINT ck_exchange_transactions_origin CHECK (origin IN ('ONLINE', 'OFFLINE')),
    CONSTRAINT ck_exchange_transactions_distinct_currencies CHECK (from_currency_id <> to_currency_id),
    CONSTRAINT ck_exchange_transactions_amounts CHECK (from_amount > 0 AND to_amount > 0 AND exchange_rate > 0),
    CONSTRAINT ck_exchange_transactions_commission CHECK (commission >= 0),
    -- Link direction: the REVERSING row points at the original through
    -- reversal_of_id. The original keeps reversal_of_id IS NULL and moves to
    -- status = 'REVERSED'; invariant I-4 (a REVERSED row must have a bound
    -- reversal row) is enforced by the deferred trigger ct_exchange_reversal_bound.
    CONSTRAINT ck_exchange_transactions_reversal_reason CHECK (
        reversal_reason IS NULL OR reversal_of_id IS NOT NULL
    ),
    CONSTRAINT ck_exchange_transactions_reversal_stamp CHECK (
        (status = 'REVERSED') = (reversed_at IS NOT NULL)
    ),
    CONSTRAINT ck_exchange_transactions_reversed_by CHECK (
        (reversed_at IS NULL) = (reversed_by IS NULL)
    ),
    CONSTRAINT ck_exchange_transactions_version CHECK (version >= 1)
);
CREATE INDEX ix_exchange_transactions_branch_created
    ON exchange_transactions (branch_id, created_at DESC);
CREATE INDEX ix_exchange_transactions_cashier_created
    ON exchange_transactions (cashier_id, created_at DESC);
CREATE INDEX ix_exchange_transactions_customer
    ON exchange_transactions (customer_id, created_at DESC);
CREATE INDEX ix_exchange_transactions_status ON exchange_transactions (status);
CREATE INDEX ix_exchange_transactions_type ON exchange_transactions (transaction_type);
CREATE INDEX ix_exchange_transactions_origin_pending
    ON exchange_transactions (origin, created_at) WHERE status = 'PENDING';
-- An exchange transaction may be reversed at most once.
CREATE UNIQUE INDEX ux_exchange_transactions_reversed_once
    ON exchange_transactions (reversal_of_id) WHERE reversal_of_id IS NOT NULL;
-- One server row per offline client event (idempotent sync ingestion, PART 34).
CREATE UNIQUE INDEX ux_exchange_transactions_client_event
    ON exchange_transactions (client_event_id) WHERE client_event_id IS NOT NULL;

-- Invariant I-4: a transaction in status REVERSED must have a bound reversal row.
-- Deferred, so the service may insert the reversal and flip the original in any
-- order inside one transaction.
CREATE OR REPLACE FUNCTION nexus_assert_reversal_bound()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_id UUID;
BEGIN
    v_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;

    IF (SELECT status FROM exchange_transactions WHERE id = v_id) = 'REVERSED'
       AND NOT EXISTS (SELECT 1 FROM exchange_transactions WHERE reversal_of_id = v_id) THEN
        RAISE EXCEPTION 'NEXUS_REVERSAL_UNBOUND: transaction % is REVERSED without a reversal row', v_id
            USING ERRCODE = 'NEX04',
                  HINT = 'Insert the reversing transaction in the same database transaction (PART 22).';
    END IF;

    RETURN NULL;
END;
$$;

-- Status machine: PENDING→COMPLETED|CANCELLED, COMPLETED→REVERSED|CANCELLED, terminal otherwise.
CREATE OR REPLACE FUNCTION nexus_validate_exchange_status()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;

    IF (OLD.status = 'PENDING'   AND NEW.status IN ('COMPLETED', 'CANCELLED'))
    OR (OLD.status = 'COMPLETED' AND NEW.status IN ('REVERSED', 'CANCELLED')) THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'NEXUS_INVALID_STATUS_TRANSITION: exchange % % -> %', OLD.id, OLD.status, NEW.status
        USING ERRCODE = 'NEX03';
END;
$$;

-- Reversal integrity: mirrored currencies/amounts, original must be COMPLETED and not yet reversed.
CREATE OR REPLACE FUNCTION nexus_validate_exchange_reversal()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    o RECORD;
BEGIN
    IF NEW.reversal_of_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT * INTO o FROM exchange_transactions WHERE id = NEW.reversal_of_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'NEXUS_REVERSAL_TARGET_MISSING: % not found', NEW.reversal_of_id
            USING ERRCODE = 'NEX04';
    END IF;

    IF o.status <> 'COMPLETED' THEN
        RAISE EXCEPTION 'NEXUS_REVERSAL_TARGET_STATE: original transaction % is % and cannot be reversed',
            o.id, o.status USING ERRCODE = 'NEX04';
    END IF;

    IF NEW.from_currency_id <> o.to_currency_id
       OR NEW.to_currency_id <> o.from_currency_id
       OR NEW.from_amount <> o.to_amount
       OR NEW.to_amount <> o.from_amount
       OR NEW.branch_id <> o.branch_id
       OR NEW.transaction_type <> o.transaction_type THEN
        RAISE EXCEPTION 'NEXUS_REVERSAL_MISMATCH: reversal of % must mirror currencies and amounts', o.id
            USING ERRCODE = 'NEX04';
    END IF;

    RETURN NEW;
END;
$$;

-- ---------------------------------------------------------------------------
-- 9. Transfers (PART 15) (+ additive A8)
-- ---------------------------------------------------------------------------
CREATE TABLE transfers (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reference_number      VARCHAR(100) NOT NULL UNIQUE,
    sender_name           VARCHAR(200) NOT NULL,
    sender_phone          VARCHAR(50),
    receiver_name         VARCHAR(200) NOT NULL,
    receiver_phone        VARCHAR(50),
    source_location       VARCHAR(200),
    destination_location  VARCHAR(200),
    currency_id           UUID NOT NULL REFERENCES currencies (id),
    amount                NUMERIC(30,10) NOT NULL,
    exchange_rate         NUMERIC(30,10),
    commission            NUMERIC(30,10) NOT NULL DEFAULT 0,
    status                VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    created_by            UUID REFERENCES users (id),
    branch_id             UUID REFERENCES branches (id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive A8) lifecycle actors + offline origin + optimistic concurrency
    customer_id           UUID REFERENCES customers (id),
    approved_by           UUID REFERENCES users (id),
    approved_at           TIMESTAMPTZ,
    paid_by               UUID REFERENCES users (id),
    paid_at               TIMESTAMPTZ,
    payout_amount         NUMERIC(30,10),
    payout_currency_id    UUID REFERENCES currencies (id),
    cancelled_by          UUID REFERENCES users (id),
    cancelled_at          TIMESTAMPTZ,
    cancel_reason         TEXT,
    journal_entry_id      UUID REFERENCES journal_entries (id),
    version               INTEGER NOT NULL DEFAULT 1,
    origin                VARCHAR(20) NOT NULL DEFAULT 'ONLINE',
    client_event_id       UUID,
    CONSTRAINT ck_transfers_status CHECK (status IN ('PENDING', 'APPROVED', 'PAID', 'CANCELLED')),
    CONSTRAINT ck_transfers_amount CHECK (amount > 0),
    CONSTRAINT ck_transfers_commission CHECK (commission >= 0),
    CONSTRAINT ck_transfers_rate CHECK (exchange_rate IS NULL OR exchange_rate > 0),
    CONSTRAINT ck_transfers_origin CHECK (origin IN ('ONLINE', 'OFFLINE')),
    CONSTRAINT ck_transfers_paid_stamp CHECK ((status = 'PAID') = (paid_at IS NOT NULL)),
    CONSTRAINT ck_transfers_approved_stamp CHECK (approved_at IS NULL OR status IN ('APPROVED', 'PAID')),
    CONSTRAINT ck_transfers_version CHECK (version >= 1)
);
CREATE INDEX ix_transfers_status_created ON transfers (status, created_at DESC);
CREATE INDEX ix_transfers_branch ON transfers (branch_id, created_at DESC);
CREATE INDEX ix_transfers_created_by ON transfers (created_by, created_at DESC);
CREATE INDEX ix_transfers_customer ON transfers (customer_id, created_at DESC);
CREATE UNIQUE INDEX ux_transfers_client_event
    ON transfers (client_event_id) WHERE client_event_id IS NOT NULL;

CREATE OR REPLACE FUNCTION nexus_validate_transfer_status()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.status = OLD.status THEN
        RETURN NEW;
    END IF;

    IF (OLD.status = 'PENDING'  AND NEW.status IN ('APPROVED', 'CANCELLED'))
    OR (OLD.status = 'APPROVED' AND NEW.status IN ('PAID', 'CANCELLED')) THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'NEXUS_INVALID_STATUS_TRANSITION: transfer % % -> %', OLD.id, OLD.status, NEW.status
        USING ERRCODE = 'NEX03';
END;
$$;

-- ---------------------------------------------------------------------------
-- 10. Cash (PART 16) (+ additive A9, A13)
-- ---------------------------------------------------------------------------

-- Cash sessions: one drawer shift per branch/device/operator (PART 30 cash/close).
CREATE TABLE cash_sessions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id      UUID NOT NULL REFERENCES branches (id),
    device_id      UUID REFERENCES devices (id),
    opened_by      UUID NOT NULL REFERENCES users (id),
    opened_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_by      UUID REFERENCES users (id),
    closed_at      TIMESTAMPTZ,
    status         VARCHAR(20) NOT NULL DEFAULT 'OPEN',
    notes          TEXT,
    CONSTRAINT ck_cash_sessions_status CHECK (status IN ('OPEN', 'CLOSED')),
    CONSTRAINT ck_cash_sessions_closed_stamp CHECK (
        (status = 'CLOSED') = (closed_at IS NOT NULL)
    )
);
-- At most one open session per device (and one per branch when no device is bound).
CREATE UNIQUE INDEX ux_cash_sessions_one_open_per_device
    ON cash_sessions (device_id) WHERE status = 'OPEN' AND device_id IS NOT NULL;

CREATE TABLE cash_session_lines (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cash_session_id   UUID NOT NULL REFERENCES cash_sessions (id) ON DELETE CASCADE,
    currency_id       UUID NOT NULL REFERENCES currencies (id),
    opening_declared  NUMERIC(30,10) NOT NULL DEFAULT 0,
    expected_amount   NUMERIC(30,10),
    counted_amount    NUMERIC(30,10),
    difference        NUMERIC(30,10),
    CONSTRAINT ux_cash_session_lines_currency UNIQUE (cash_session_id, currency_id),
    CONSTRAINT ck_cash_session_lines_opening CHECK (opening_declared >= 0)
);
-- difference must always equal counted - expected when both are present.
CREATE OR REPLACE FUNCTION nexus_validate_cash_session_line()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.counted_amount IS NOT NULL AND NEW.expected_amount IS NOT NULL THEN
        NEW.difference := NEW.counted_amount - NEW.expected_amount;
    ELSIF NEW.counted_amount IS NULL AND NEW.expected_amount IS NULL THEN
        NEW.difference := NULL;
    ELSE
        RAISE EXCEPTION 'NEXUS_CASH_RECON_INCOMPLETE: session line % needs both counted and expected', NEW.id
            USING ERRCODE = 'NEX05';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TABLE cash_movements (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id       UUID NOT NULL REFERENCES branches (id),
    account_id      UUID NOT NULL REFERENCES accounts (id),
    currency_id     UUID NOT NULL REFERENCES currencies (id),
    movement_type   VARCHAR(30) NOT NULL,
    amount          NUMERIC(30,10) NOT NULL,
    reference_type  VARCHAR(50),
    reference_id    UUID,
    description     TEXT,
    created_by      UUID REFERENCES users (id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive A9)
    adjustment_sign SMALLINT,
    cash_session_id UUID REFERENCES cash_sessions (id),
    device_id       UUID REFERENCES devices (id),
    journal_entry_id UUID REFERENCES journal_entries (id),
    client_event_id UUID,
    -- signed_amount is the single canonical sign convention used by the
    -- non-negative cash-position constraint and by reporting.
    signed_amount   NUMERIC(30,10) GENERATED ALWAYS AS (
        CASE
            WHEN movement_type IN ('OPENING', 'IN')  THEN amount
            WHEN movement_type IN ('OUT', 'EXPENSE') THEN -amount
            WHEN movement_type = 'ADJUSTMENT'        THEN amount * COALESCE(adjustment_sign, 0)
            ELSE 0::NUMERIC
        END
    ) STORED,
    CONSTRAINT ck_cash_movements_type CHECK (
        movement_type IN ('OPENING', 'IN', 'OUT', 'EXPENSE', 'ADJUSTMENT', 'CLOSING')
    ),
    CONSTRAINT ck_cash_movements_amount CHECK (amount >= 0),
    -- Three-valued logic guard: "adjustment_sign IN (-1,1)" is NULL (and therefore
    -- SATISFIED by a CHECK constraint) when the sign is absent. The IS NOT NULL
    -- clause is what actually makes the requirement enforceable.
    CONSTRAINT ck_cash_movements_adjustment_sign CHECK (
        (movement_type = 'ADJUSTMENT' AND adjustment_sign IS NOT NULL AND adjustment_sign IN (-1, 1))
        OR (movement_type <> 'ADJUSTMENT' AND adjustment_sign IS NULL)
    ),
    CONSTRAINT ck_cash_movements_reference CHECK (
        (reference_type IS NULL) = (reference_id IS NULL)
    )
);
CREATE INDEX ix_cash_movements_branch_currency
    ON cash_movements (branch_id, currency_id, created_at DESC);
CREATE INDEX ix_cash_movements_reference ON cash_movements (reference_type, reference_id);
CREATE INDEX ix_cash_movements_session ON cash_movements (cash_session_id);
CREATE INDEX ix_cash_movements_type_created ON cash_movements (movement_type, created_at DESC);
CREATE UNIQUE INDEX ux_cash_movements_client_event
    ON cash_movements (client_event_id) WHERE client_event_id IS NOT NULL;

-- Cash position can never go negative for a (branch, currency) pair.
CREATE OR REPLACE FUNCTION nexus_assert_non_negative_cash()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_branch   UUID;
    v_currency UUID;
    v_total    NUMERIC(30,10);
BEGIN
    IF TG_OP = 'DELETE' THEN
        v_branch   := OLD.branch_id;
        v_currency := OLD.currency_id;
    ELSE
        v_branch   := NEW.branch_id;
        v_currency := NEW.currency_id;
    END IF;

    SELECT COALESCE(SUM(signed_amount), 0) INTO v_total
      FROM cash_movements
     WHERE branch_id = v_branch
       AND currency_id = v_currency;

    IF v_total < 0 THEN
        RAISE EXCEPTION 'NEXUS_INSUFFICIENT_BALANCE: branch % currency % would hold %',
            v_branch, v_currency, v_total
            USING ERRCODE = 'NEX01',
                  HINT = 'Cash position may never be negative (server-authoritative balance).';
    END IF;

    RETURN NULL;
END;
$$;

-- ---------------------------------------------------------------------------
-- 11. Expenses (PART 17) (+ additive A10)
-- ---------------------------------------------------------------------------
CREATE TABLE expenses (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    branch_id        UUID REFERENCES branches (id),
    category         VARCHAR(100) NOT NULL,
    amount           NUMERIC(30,10) NOT NULL,
    currency_id      UUID NOT NULL REFERENCES currencies (id),
    description      TEXT,
    expense_date     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by       UUID REFERENCES users (id),
    -- (additive A10)
    status           VARCHAR(20) NOT NULL DEFAULT 'POSTED',
    payee            VARCHAR(200),
    attachment_path  TEXT,
    journal_entry_id UUID REFERENCES journal_entries (id),
    approved_by      UUID REFERENCES users (id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_expenses_amount CHECK (amount > 0),
    CONSTRAINT ck_expenses_status CHECK (status IN ('POSTED', 'CANCELLED'))
);
CREATE INDEX ix_expenses_branch_date ON expenses (branch_id, expense_date DESC);
CREATE INDEX ix_expenses_category ON expenses (category);

-- ---------------------------------------------------------------------------
-- 12. Audit (PART 18) (+ additive A11)
-- ---------------------------------------------------------------------------
CREATE TABLE audit_logs (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID REFERENCES users (id),
    device_id    UUID REFERENCES devices (id),
    action       VARCHAR(100) NOT NULL,
    entity_type  VARCHAR(100) NOT NULL,
    entity_id    UUID,
    old_data     JSONB,
    new_data     JSONB,
    ip_address   INET,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- (additive A11) tamper-evident hash chain
    seq          BIGSERIAL NOT NULL,
    prev_hash    CHAR(64),
    chain_hash   CHAR(64),
    request_id   VARCHAR(100),
    CONSTRAINT ck_audit_logs_action_format CHECK (action = upper(action)),
    CONSTRAINT ux_audit_logs_seq UNIQUE (seq)
);
CREATE INDEX ix_audit_logs_created ON audit_logs (created_at DESC);
CREATE INDEX ix_audit_logs_entity ON audit_logs (entity_type, entity_id, created_at DESC);
CREATE INDEX ix_audit_logs_user ON audit_logs (user_id, created_at DESC);
CREATE INDEX ix_audit_logs_action ON audit_logs (action, created_at DESC);

-- ---------------------------------------------------------------------------
-- 13. Offline synchronisation (PART 19) (+ additive A12, A13)
-- ---------------------------------------------------------------------------
CREATE TABLE sync_events (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id        UUID NOT NULL REFERENCES devices (id),
    event_id         UUID NOT NULL UNIQUE,
    entity_type      VARCHAR(100) NOT NULL,
    entity_id        UUID NOT NULL,
    operation        VARCHAR(20) NOT NULL,
    payload          JSONB NOT NULL,
    client_timestamp TIMESTAMPTZ NOT NULL,
    server_timestamp TIMESTAMPTZ,
    status           VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    error_message    TEXT,
    -- (additive A12)
    result           JSONB,
    attempt_count    SMALLINT NOT NULL DEFAULT 0,
    processed_at     TIMESTAMPTZ,
    batch_id         UUID,
    idempotency_key  UUID,
    CONSTRAINT ck_sync_events_operation CHECK (operation IN ('CREATE', 'UPDATE', 'CANCEL', 'REVERSE')),
    CONSTRAINT ck_sync_events_status CHECK (
        status IN ('PENDING', 'APPLIED', 'DUPLICATE', 'REJECTED', 'CONFLICT', 'FAILED')
    ),
    CONSTRAINT ck_sync_events_attempts CHECK (attempt_count >= 0)
);
CREATE INDEX ix_sync_events_device_status ON sync_events (device_id, status);
CREATE INDEX ix_sync_events_entity ON sync_events (entity_type, entity_id);
CREATE INDEX ix_sync_events_received ON sync_events (client_timestamp);

CREATE TABLE sync_conflicts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id      UUID NOT NULL REFERENCES devices (id),
    sync_event_id  UUID REFERENCES sync_events (id),
    entity_type    VARCHAR(100) NOT NULL,
    entity_id      UUID NOT NULL,
    conflict_type  VARCHAR(50) NOT NULL,
    server_version JSONB,
    client_payload JSONB NOT NULL,
    resolution     VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    resolved_by    UUID REFERENCES users (id),
    resolved_at    TIMESTAMPTZ,
    resolution_note TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_sync_conflicts_type CHECK (conflict_type IN (
        'STALE_VERSION', 'DUPLICATE_EVENT', 'ALLOCATION_EXCEEDED', 'INSUFFICIENT_BALANCE',
        'RATE_MISMATCH', 'REFERENCE_MISSING', 'ALREADY_REVERSED', 'DEVICE_REVOKED'
    )),
    CONSTRAINT ck_sync_conflicts_resolution CHECK (
        resolution IN ('PENDING', 'SERVER_WINS', 'CLIENT_REPOSTED', 'VOIDED', 'MANUAL')
    ),
    CONSTRAINT ck_sync_conflicts_resolved_stamp CHECK (
        (resolution = 'PENDING') = (resolved_at IS NULL)
    )
);
CREATE INDEX ix_sync_conflicts_pending ON sync_conflicts (resolution, created_at) WHERE resolution = 'PENDING';

-- Per-device pull cursor over change_log.
CREATE TABLE sync_cursors (
    device_id     UUID PRIMARY KEY REFERENCES devices (id) ON DELETE CASCADE,
    last_seq      BIGINT NOT NULL DEFAULT 0,
    last_sync_at  TIMESTAMPTZ,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_sync_cursors_seq CHECK (last_seq >= 0)
);

-- Server-side change stream consumed by GET /api/v1/sync/pull.
CREATE TABLE change_log (
    seq          BIGSERIAL PRIMARY KEY,
    entity_type  VARCHAR(100) NOT NULL,
    entity_id    UUID NOT NULL,
    operation    VARCHAR(20) NOT NULL,
    branch_id    UUID REFERENCES branches (id),
    payload      JSONB NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_change_log_operation CHECK (operation IN ('CREATE', 'UPDATE', 'CANCEL', 'REVERSE'))
);
CREATE INDEX ix_change_log_entity ON change_log (entity_type, entity_id);
CREATE INDEX ix_change_log_occurred ON change_log (occurred_at);

-- ---------------------------------------------------------------------------
-- 14. Offline allocation control (PART 37)
-- ---------------------------------------------------------------------------
-- Financial offline operations are bounded by a server-granted allowance.
-- Policies are templates; device_allocations are the concrete grants.
CREATE TABLE allocation_policies (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                  VARCHAR(100) NOT NULL,
    branch_id             UUID REFERENCES branches (id),
    device_id             UUID REFERENCES devices (id),
    currency_id           UUID NOT NULL REFERENCES currencies (id),
    max_amount            NUMERIC(30,10) NOT NULL,
    max_offline_minutes   INTEGER NOT NULL DEFAULT 480,
    allow_buy             BOOLEAN NOT NULL DEFAULT TRUE,
    allow_sell            BOOLEAN NOT NULL DEFAULT TRUE,
    max_commission        NUMERIC(30,10),
    is_active             BOOLEAN NOT NULL DEFAULT TRUE,
    created_by            UUID REFERENCES users (id),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at            TIMESTAMPTZ,
    revoked_by            UUID REFERENCES users (id),
    CONSTRAINT ck_allocation_policies_amount CHECK (max_amount >= 0),
    CONSTRAINT ck_allocation_policies_window CHECK (max_offline_minutes > 0),
    CONSTRAINT ck_allocation_policies_scope CHECK (branch_id IS NOT NULL OR device_id IS NOT NULL)
);

CREATE TABLE device_allocations (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id        UUID NOT NULL REFERENCES devices (id),
    policy_id        UUID REFERENCES allocation_policies (id),
    currency_id      UUID NOT NULL REFERENCES currencies (id),
    window_start     TIMESTAMPTZ NOT NULL,
    window_end       TIMESTAMPTZ NOT NULL,
    granted_amount   NUMERIC(30,10) NOT NULL,
    consumed_amount  NUMERIC(30,10) NOT NULL DEFAULT 0,
    released_amount  NUMERIC(30,10) NOT NULL DEFAULT 0,
    status           VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_device_allocations_amounts CHECK (
        granted_amount >= 0 AND consumed_amount >= 0 AND released_amount >= 0
        AND consumed_amount + released_amount <= granted_amount
    ),
    CONSTRAINT ck_device_allocations_window CHECK (window_end > window_start),
    CONSTRAINT ck_device_allocations_status CHECK (status IN ('ACTIVE', 'EXPIRED', 'REVOKED', 'EXHAUSTED')),
    CONSTRAINT ux_device_allocations_window UNIQUE (device_id, currency_id, window_start)
);
CREATE INDEX ix_device_allocations_active ON device_allocations (device_id, currency_id) WHERE status = 'ACTIVE';

-- Remaining allowance for a device in a currency at a point in time.
CREATE OR REPLACE FUNCTION available_allocation(
    p_device_id   UUID,
    p_currency_id UUID,
    p_at          TIMESTAMPTZ DEFAULT NOW()
)
RETURNS NUMERIC(30,10)
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(SUM(granted_amount - consumed_amount - released_amount), 0)::NUMERIC(30,10)
      FROM device_allocations
     WHERE device_id   = p_device_id
       AND currency_id = p_currency_id
       AND status      = 'ACTIVE'
       AND p_at >= window_start
       AND p_at <  window_end;
$$;

-- ---------------------------------------------------------------------------
-- 15. Idempotency (PART 40)
-- ---------------------------------------------------------------------------
CREATE TABLE idempotency_keys (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    key              UUID NOT NULL,
    user_id          UUID NOT NULL REFERENCES users (id),
    device_id        UUID REFERENCES devices (id),
    endpoint         VARCHAR(200) NOT NULL,
    request_hash     CHAR(64) NOT NULL,
    status           VARCHAR(20) NOT NULL DEFAULT 'IN_PROGRESS',
    response_status  SMALLINT,
    response_body    JSONB,
    resource_type    VARCHAR(100),
    resource_id      UUID,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at     TIMESTAMPTZ,
    CONSTRAINT ux_idempotency_keys_scope UNIQUE (user_id, endpoint, key),
    CONSTRAINT ck_idempotency_keys_status CHECK (status IN ('IN_PROGRESS', 'COMPLETED', 'FAILED'))
);
CREATE INDEX ix_idempotency_keys_created ON idempotency_keys (created_at DESC);

-- ---------------------------------------------------------------------------
-- 16. Document numbering storage
-- ---------------------------------------------------------------------------
CREATE TABLE sequences (
    name           VARCHAR(100) PRIMARY KEY,
    current_value  BIGINT NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ck_sequences_value CHECK (current_value >= 0)
);

-- ---------------------------------------------------------------------------
-- 17. Deferred foreign keys (tables created out of dependency order)
-- ---------------------------------------------------------------------------
ALTER TABLE exchange_transactions
    ADD CONSTRAINT fk_exchange_transactions_cash_session
    FOREIGN KEY (cash_session_id) REFERENCES cash_sessions (id);

ALTER TABLE refresh_tokens
    ADD CONSTRAINT fk_refresh_tokens_device
    FOREIGN KEY (device_id) REFERENCES devices (id);

-- ---------------------------------------------------------------------------
-- 18. Triggers
-- ---------------------------------------------------------------------------

-- updated_at
CREATE TRIGGER trg_users_updated_at BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION nexus_set_updated_at();
CREATE TRIGGER trg_customers_updated_at BEFORE UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION nexus_set_updated_at();
CREATE TRIGGER trg_exchange_transactions_updated_at BEFORE UPDATE ON exchange_transactions
    FOR EACH ROW EXECUTE FUNCTION nexus_set_updated_at();
CREATE TRIGGER trg_transfers_updated_at BEFORE UPDATE ON transfers
    FOR EACH ROW EXECUTE FUNCTION nexus_set_updated_at();
CREATE TRIGGER trg_device_allocations_updated_at BEFORE UPDATE ON device_allocations
    FOR EACH ROW EXECUTE FUNCTION nexus_set_updated_at();
CREATE TRIGGER trg_sync_cursors_updated_at BEFORE UPDATE ON sync_cursors
    FOR EACH ROW EXECUTE FUNCTION nexus_set_updated_at();

-- Optimistic concurrency: every UPDATE bumps version (used by offline conflict detection).
CREATE OR REPLACE FUNCTION nexus_bump_version()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.version := OLD.version + 1;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_exchange_transactions_version BEFORE UPDATE ON exchange_transactions
    FOR EACH ROW EXECUTE FUNCTION nexus_bump_version();
CREATE TRIGGER trg_transfers_version BEFORE UPDATE ON transfers
    FOR EACH ROW EXECUTE FUNCTION nexus_bump_version();

-- Journal balance (deferred to COMMIT — PART 12 / PART 49)
CREATE CONSTRAINT TRIGGER ct_journal_lines_balanced_insert
    AFTER INSERT ON journal_lines
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION nexus_assert_journal_balanced();
CREATE CONSTRAINT TRIGGER ct_journal_lines_balanced_update
    AFTER UPDATE ON journal_lines
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION nexus_assert_journal_balanced();
CREATE CONSTRAINT TRIGGER ct_journal_lines_balanced_delete
    AFTER DELETE ON journal_lines
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION nexus_assert_journal_balanced();

-- Balance cache (statement level, sees the full statement through transition tables)
CREATE TRIGGER trg_journal_lines_balance_cache
    AFTER INSERT ON journal_lines
    REFERENCING NEW TABLE AS new_journal_lines
    FOR EACH STATEMENT EXECUTE FUNCTION nexus_maintain_account_balances();

-- Non-negative cash position (deferred to COMMIT)
CREATE CONSTRAINT TRIGGER ct_cash_movements_non_negative
    AFTER INSERT ON cash_movements
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION nexus_assert_non_negative_cash();

-- Audit hash chain + append-only
CREATE TRIGGER trg_audit_logs_chain BEFORE INSERT ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION nexus_audit_chain();
CREATE TRIGGER trg_audit_logs_no_update BEFORE UPDATE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_audit_logs_no_delete BEFORE DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();

-- Append-only ledgers and history (PART 18 / PART 22 / PART 49)
CREATE TRIGGER trg_journal_lines_no_update BEFORE UPDATE ON journal_lines
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_journal_lines_no_delete BEFORE DELETE ON journal_lines
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_cash_movements_no_update BEFORE UPDATE ON cash_movements
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_cash_movements_no_delete BEFORE DELETE ON cash_movements
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_expenses_no_delete BEFORE DELETE ON expenses
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();

-- Master data and business documents are archived, never hard-deleted (PART 25).
CREATE TRIGGER trg_users_no_delete BEFORE DELETE ON users
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_branches_no_delete BEFORE DELETE ON branches
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_customers_no_delete BEFORE DELETE ON customers
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_currencies_no_delete BEFORE DELETE ON currencies
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_accounts_no_delete BEFORE DELETE ON accounts
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_exchange_transactions_no_delete BEFORE DELETE ON exchange_transactions
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_transfers_no_delete BEFORE DELETE ON transfers
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();
CREATE TRIGGER trg_sync_events_no_delete BEFORE DELETE ON sync_events
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();

-- Currency code is immutable: changing it would silently reinterpret history.
CREATE OR REPLACE FUNCTION nexus_currencies_immutable_code()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.code <> OLD.code THEN
        RAISE EXCEPTION 'NEXUS_IMMUTABLE_FIELD: currencies.code cannot change (% -> %)', OLD.code, NEW.code
            USING ERRCODE = 'NEX06';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_currencies_immutable_code BEFORE UPDATE ON currencies
    FOR EACH ROW EXECUTE FUNCTION nexus_currencies_immutable_code();

-- Journal entry immutability: only description is editable; money columns are frozen.
CREATE OR REPLACE FUNCTION nexus_journal_entries_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.reference_type <> OLD.reference_type
       OR NEW.reference_id IS DISTINCT FROM OLD.reference_id
       OR NEW.transaction_date <> OLD.transaction_date
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.branch_id IS DISTINCT FROM OLD.branch_id
       OR NEW.reversal_of_id IS DISTINCT FROM OLD.reversal_of_id THEN
        RAISE EXCEPTION 'NEXUS_IMMUTABLE_FIELD: journal_entries posting fields cannot change (entry %)', OLD.id
            USING ERRCODE = 'NEX06';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_journal_entries_immutable BEFORE UPDATE ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION nexus_journal_entries_immutable();
CREATE TRIGGER trg_journal_entries_no_delete BEFORE DELETE ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION nexus_forbid_mutation();

-- Exchange transaction: money fields are frozen after posting; only lifecycle moves.
CREATE OR REPLACE FUNCTION nexus_exchange_transactions_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.transaction_number <> OLD.transaction_number
       OR NEW.transaction_type   <> OLD.transaction_type
       OR NEW.branch_id          <> OLD.branch_id
       OR NEW.cashier_id         <> OLD.cashier_id
       OR NEW.customer_id        IS DISTINCT FROM OLD.customer_id
       OR NEW.from_currency_id   <> OLD.from_currency_id
       OR NEW.to_currency_id     <> OLD.to_currency_id
       OR NEW.from_amount        <> OLD.from_amount
       OR NEW.to_amount          <> OLD.to_amount
       OR NEW.exchange_rate      <> OLD.exchange_rate
       OR NEW.commission         <> OLD.commission
       OR NEW.created_at         <> OLD.created_at THEN
        RAISE EXCEPTION 'NEXUS_IMMUTABLE_FIELD: exchange transaction % is posted and immutable', OLD.id
            USING ERRCODE = 'NEX06',
            HINT = 'Issue a reversal (PART 22) instead of editing.';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_exchange_transactions_immutable BEFORE UPDATE ON exchange_transactions
    FOR EACH ROW EXECUTE FUNCTION nexus_exchange_transactions_immutable();

-- Transfer: money/parties frozen; lifecycle columns move.
CREATE OR REPLACE FUNCTION nexus_transfers_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.reference_number <> OLD.reference_number
       OR NEW.sender_name      <> OLD.sender_name
       OR NEW.receiver_name    <> OLD.receiver_name
       OR NEW.currency_id      <> OLD.currency_id
       OR NEW.amount           <> OLD.amount
       OR NEW.created_at       <> OLD.created_at THEN
        RAISE EXCEPTION 'NEXUS_IMMUTABLE_FIELD: transfer % is posted and immutable', OLD.id
            USING ERRCODE = 'NEX06',
            HINT = 'Cancel the transfer and create a new one.';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_transfers_immutable BEFORE UPDATE ON transfers
    FOR EACH ROW EXECUTE FUNCTION nexus_transfers_immutable();

-- Status machines
CREATE TRIGGER trg_exchange_transactions_status BEFORE UPDATE ON exchange_transactions
    FOR EACH ROW EXECUTE FUNCTION nexus_validate_exchange_status();
CREATE CONSTRAINT TRIGGER ct_exchange_reversal_bound
    AFTER INSERT OR UPDATE ON exchange_transactions
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION nexus_assert_reversal_bound();
CREATE TRIGGER trg_transfers_status BEFORE UPDATE ON transfers
    FOR EACH ROW EXECUTE FUNCTION nexus_validate_transfer_status();
CREATE TRIGGER trg_exchange_transactions_reversal BEFORE INSERT ON exchange_transactions
    FOR EACH ROW EXECUTE FUNCTION nexus_validate_exchange_reversal();
CREATE TRIGGER trg_cash_session_lines_recon BEFORE INSERT OR UPDATE ON cash_session_lines
    FOR EACH ROW EXECUTE FUNCTION nexus_validate_cash_session_line();

-- Sync events: identity and payload are immutable; only processing state moves.
CREATE OR REPLACE FUNCTION nexus_sync_events_immutable()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.event_id        <> OLD.event_id
       OR NEW.device_id    <> OLD.device_id
       OR NEW.entity_id    <> OLD.entity_id
       OR NEW.entity_type  <> OLD.entity_type
       OR NEW.operation    <> OLD.operation
       OR NEW.payload      <> OLD.payload
       OR NEW.client_timestamp <> OLD.client_timestamp THEN
        RAISE EXCEPTION 'NEXUS_IMMUTABLE_FIELD: sync event % envelope is immutable', OLD.id
            USING ERRCODE = 'NEX06';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER trg_sync_events_immutable BEFORE UPDATE ON sync_events
    FOR EACH ROW EXECUTE FUNCTION nexus_sync_events_immutable();

-- Change stream producers (offline pull).
CREATE TRIGGER trg_change_log_branches AFTER INSERT OR UPDATE ON branches
    FOR EACH ROW EXECUTE FUNCTION nexus_log_change();
CREATE TRIGGER trg_change_log_currencies AFTER INSERT OR UPDATE ON currencies
    FOR EACH ROW EXECUTE FUNCTION nexus_log_change();
CREATE TRIGGER trg_change_log_customers AFTER INSERT OR UPDATE ON customers
    FOR EACH ROW EXECUTE FUNCTION nexus_log_change();
CREATE TRIGGER trg_change_log_exchange_rates AFTER INSERT OR UPDATE ON exchange_rates
    FOR EACH ROW EXECUTE FUNCTION nexus_log_change();
CREATE TRIGGER trg_change_log_accounts AFTER INSERT OR UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION nexus_log_change();
CREATE TRIGGER trg_change_log_exchange_transactions AFTER INSERT OR UPDATE ON exchange_transactions
    FOR EACH ROW EXECUTE FUNCTION nexus_log_change();
CREATE TRIGGER trg_change_log_transfers AFTER INSERT OR UPDATE ON transfers
    FOR EACH ROW EXECUTE FUNCTION nexus_log_change();
CREATE TRIGGER trg_change_log_cash_movements AFTER INSERT OR UPDATE ON cash_movements
    FOR EACH ROW EXECUTE FUNCTION nexus_log_change();

-- ---------------------------------------------------------------------------
-- 19. Reporting views
-- ---------------------------------------------------------------------------

-- Trial balance directly from the ledger (never from the balance cache).
CREATE VIEW v_trial_balance AS
SELECT a.id            AS account_id,
       a.code          AS account_code,
       a.name          AS account_name,
       a.account_type,
       l.currency_id,
       c.code          AS currency_code,
       SUM(l.debit)    AS total_debit,
       SUM(l.credit)   AS total_credit,
       SUM(l.debit) - SUM(l.credit) AS net_debit
  FROM journal_lines l
  JOIN journal_entries e ON e.id = l.journal_entry_id
  JOIN accounts a        ON a.id = l.account_id
  LEFT JOIN currencies c ON c.id = l.currency_id
 GROUP BY a.id, a.code, a.name, a.account_type, l.currency_id, c.code;

-- Account balances with type-aware sign convention.
CREATE VIEW v_account_balances AS
SELECT ab.account_id,
       a.code        AS account_code,
       a.name        AS account_name,
       a.account_type,
       a.normal_balance,
       ab.currency_id,
       c.code        AS currency_code,
       ab.debit_total,
       ab.credit_total,
       CASE
           WHEN a.normal_balance = 'CREDIT' THEN ab.credit_total - ab.debit_total
           WHEN a.normal_balance = 'DEBIT'  THEN ab.debit_total - ab.credit_total
           ELSE ab.debit_total - ab.credit_total
       END AS balance,
       ab.updated_at
  FROM account_balances ab
  JOIN accounts a        ON a.id = ab.account_id
  LEFT JOIN currencies c ON c.id = ab.currency_id;

-- Branch cash position derived from immutable movement rows.
CREATE VIEW v_cash_position AS
SELECT m.branch_id,
       b.code   AS branch_code,
       m.currency_id,
       c.code   AS currency_code,
       SUM(m.signed_amount) AS balance,
       MAX(m.created_at)    AS last_movement_at
  FROM cash_movements m
  JOIN branches   b ON b.id = m.branch_id
  JOIN currencies c ON c.id = m.currency_id
 GROUP BY m.branch_id, b.code, m.currency_id, c.code;

-- Currency position: physical quantity held (from immutable cash movements).
CREATE VIEW v_currency_position AS
SELECT m.branch_id,
       b.code  AS branch_code,
       m.currency_id,
       c.code  AS currency_code,
       SUM(m.signed_amount) AS quantity,
       MAX(m.created_at)    AS last_movement_at
  FROM cash_movements m
  JOIN branches   b ON b.id = m.branch_id
  JOIN currencies c ON c.id = m.currency_id
 GROUP BY m.branch_id, b.code, m.currency_id, c.code;

-- Daily exchange activity (reports/daily, reports/exchange).
CREATE VIEW v_exchange_daily AS
SELECT t.branch_id,
       date_trunc('day', t.created_at AT TIME ZONE 'UTC')::date AS business_date,
       t.transaction_type,
       t.from_currency_id,
       t.to_currency_id,
       COUNT(*)            AS transaction_count,
       SUM(t.from_amount)  AS total_from_amount,
       SUM(t.to_amount)    AS total_to_amount,
       SUM(t.commission)   AS total_commission
  FROM exchange_transactions t
 WHERE t.status IN ('COMPLETED', 'REVERSED')
 GROUP BY t.branch_id, date_trunc('day', t.created_at AT TIME ZONE 'UTC')::date,
          t.transaction_type, t.from_currency_id, t.to_currency_id;

-- ---------------------------------------------------------------------------
-- 20. Privileges (least privilege for the runtime role)
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO nexus_app, nexus_reader, nexus_auditor;

GRANT SELECT, INSERT, UPDATE ON
    users, roles, user_roles, permissions, role_permissions, user_permissions,
    refresh_tokens, branches, devices, currencies, customers, accounts,
    journal_entries, journal_lines, exchange_rates, exchange_transactions,
    transfers, cash_sessions, cash_session_lines, cash_movements, expenses,
    sync_events, sync_conflicts, sync_cursors, change_log, device_allocations,
    allocation_policies, idempotency_keys, sequences, audit_logs,
    account_balances
TO nexus_app;

GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO nexus_app;

-- The runtime role may not erase financial history, even if application logic
-- were bypassed. Reversal-only correction is enforced at the database level.
REVOKE DELETE ON
    audit_logs, journal_entries, journal_lines, cash_movements, expenses,
    exchange_transactions, transfers, users, branches, customers, currencies,
    accounts, sync_events
FROM nexus_app;
REVOKE UPDATE ON audit_logs, journal_lines, cash_movements FROM nexus_app;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO nexus_reader, nexus_auditor;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE ON TABLES TO nexus_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO nexus_reader, nexus_auditor;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE ON SEQUENCES TO nexus_app;

-- ---------------------------------------------------------------------------
-- 21. Schema self-checks (fail loudly if a rule was violated in this file)
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_float_columns INTEGER;
    v_missing_timezone INTEGER;
    v_money_columns INTEGER;
BEGIN
    -- No floating-point column may ever hold money.
    SELECT COUNT(*) INTO v_float_columns
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND data_type IN ('real', 'double precision', 'money');

    IF v_float_columns > 0 THEN
        RAISE EXCEPTION 'NEXUS_SCHEMA_FLOAT_MONEY: % floating-point column(s) found', v_float_columns;
    END IF;

    -- Every timestamp must be timezone-aware.
    SELECT COUNT(*) INTO v_missing_timezone
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND data_type = 'timestamp without time zone';

    IF v_missing_timezone > 0 THEN
        RAISE EXCEPTION 'NEXUS_SCHEMA_NAIVE_TIMESTAMP: % column(s) without time zone', v_missing_timezone;
    END IF;

    SELECT COUNT(*) INTO v_money_columns
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND numeric_precision = 30
       AND numeric_scale = 10;

    IF v_money_columns = 0 THEN
        RAISE EXCEPTION 'NEXUS_SCHEMA_NO_MONEY_COLUMNS: NUMERIC(30,10) columns are missing';
    END IF;

    RAISE NOTICE 'NEXUS schema self-check passed (% NUMERIC(30,10) columns, 0 float columns).', v_money_columns;
END
$$;

-- =============================================================================
-- END OF NORMATIVE REFERENCE SCHEMA
-- =============================================================================
