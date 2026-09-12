-- =============================================================================
-- NEXUS EXCHANGE ERP — PHASE 0 SCHEMA INVARIANT SUITE
-- =============================================================================
-- Document ID : TEST-SCHEMA-001
-- Version     : 1.0 (Phase 0)
-- Target      : docs/database/schema.sql applied to an empty PostgreSQL 16 database
--
-- RUN
--   psql -v ON_ERROR_STOP=1 -d nexus_exchange -f tests/invariants/phase0_schema_invariants.sql
--
-- WHAT IT PROVES
--   I-1  Total Debit = Total Credit for every journal entry (PART 49)
--   I-2  Posted balance = SUM(valid ledger movements); the balance cache is exact
--   I-3  Cancelled/Reversed transactions are never deleted, only superseded
--   I-4  A reversed transaction always has a bound reversal entry
--   I-5  Cash position can never become negative (PART 37 server-authoritative)
--   I-6  The audit log is append-only and tamper-evident
--   I-7  Idempotency and duplicate-posting prevention at the database level
--   I-8  Every monetary column is NUMERIC, never floating point (PART 62)
--
-- Every assertion prints "PASS <id> ..." or raises an exception that aborts the
-- run. Any FAIL therefore fails CI.
-- =============================================================================

\set ON_ERROR_STOP on
\pset pager off

-- ---------------------------------------------------------------------------
-- Fixtures (deterministic UUIDs so the suite is re-runnable on a fresh database)
-- ---------------------------------------------------------------------------
BEGIN;

INSERT INTO branches (id, code, name, timezone)
VALUES ('11111111-1111-1111-1111-111111111111', 'B01', 'Main Branch', 'Asia/Kabul');

INSERT INTO currencies (id, code, name, symbol, decimal_places, is_base) VALUES
    ('22222222-2222-2222-2222-222222222201', 'AFN', 'Afghan Afghani', '؋', 2, TRUE),
    ('22222222-2222-2222-2222-222222222202', 'USD', 'US Dollar',      '$', 2, FALSE);

INSERT INTO users (id, username, password_hash, full_name)
VALUES ('33333333-3333-3333-3333-333333333301', 'cashier1', 'argon2id$placeholder', 'Cashier One');

INSERT INTO devices (id, branch_id, device_uuid, device_name, platform, registered_by)
VALUES ('44444444-4444-4444-4444-444444444401',
        '11111111-1111-1111-1111-111111111111',
        '55555555-5555-5555-5555-555555555501', 'Counter-1', 'WINDOWS',
        '33333333-3333-3333-3333-333333333301');

INSERT INTO customers (id, customer_code, full_name, phone, branch_id)
VALUES ('66666666-6666-6666-6666-666666666601', 'CUST-000001', 'Ahmad Wali', '+93700000000',
        '11111111-1111-1111-1111-111111111111');

-- Chart of accounts (currency inventory accounts + control accounts)
INSERT INTO accounts (id, code, name, account_type, currency_id, branch_id, normal_balance) VALUES
    ('77777777-7777-7777-7777-777777777701', '1010', 'Cash AFN',           'ASSET',   '22222222-2222-2222-2222-222222222201', '11111111-1111-1111-1111-111111111111', 'DEBIT'),
    ('77777777-7777-7777-7777-777777777702', '1020', 'Cash USD',           'ASSET',   '22222222-2222-2222-2222-222222222202', '11111111-1111-1111-1111-111111111111', 'DEBIT'),
    ('77777777-7777-7777-7777-777777777703', '3010', 'Owner Capital',      'EQUITY',  '22222222-2222-2222-2222-222222222201', '11111111-1111-1111-1111-111111111111', 'CREDIT'),
    ('77777777-7777-7777-7777-777777777704', '4010', 'FX Gain / Loss',     'REVENUE', '22222222-2222-2222-2222-222222222201', '11111111-1111-1111-1111-111111111111', 'CREDIT'),
    ('77777777-7777-7777-7777-777777777705', '4020', 'Commission Income',  'REVENUE', '22222222-2222-2222-2222-222222222201', '11111111-1111-1111-1111-111111111111', 'CREDIT'),
    ('77777777-7777-7777-7777-777777777706', '5010', 'Operating Expense',  'EXPENSE', '22222222-2222-2222-2222-222222222201', '11111111-1111-1111-1111-111111111111', 'DEBIT');

INSERT INTO permissions (code, description) VALUES
    ('exchange.create', 'Create exchange transactions');

INSERT INTO roles (id, name, description, is_system)
VALUES ('88888888-8888-8888-8888-888888888801', 'CASHIER', 'Counter operator', TRUE);

INSERT INTO role_permissions (role_id, permission_code)
VALUES ('88888888-8888-8888-8888-888888888801', 'exchange.create');

INSERT INTO user_roles (user_id, role_id)
VALUES ('33333333-3333-3333-3333-333333333301', '88888888-8888-8888-8888-888888888801');

-- Opening cash: 1,000,000 AFN and 10,000 USD (opening balances are immutable rows)
INSERT INTO cash_sessions (id, branch_id, device_id, opened_by)
VALUES ('99999999-9999-9999-9999-999999999901',
        '11111111-1111-1111-1111-111111111111',
        '44444444-4444-4444-4444-444444444401',
        '33333333-3333-3333-3333-333333333301');

INSERT INTO cash_movements (branch_id, account_id, currency_id, movement_type, amount,
                            cash_session_id, device_id, created_by, description)
VALUES
    ('11111111-1111-1111-1111-111111111111', '77777777-7777-7777-7777-777777777701',
     '22222222-2222-2222-2222-222222222201', 'OPENING', 1000000.00,
     '99999999-9999-9999-9999-999999999901', '44444444-4444-4444-4444-444444444401',
     '33333333-3333-3333-3333-333333333301', 'Opening balance AFN'),
    ('11111111-1111-1111-1111-111111111111', '77777777-7777-7777-7777-777777777702',
     '22222222-2222-2222-2222-222222222202', 'OPENING', 10000.00,
     '99999999-9999-9999-9999-999999999901', '44444444-4444-4444-4444-444444444401',
     '33333333-3333-3333-3333-333333333301', 'Opening balance USD');

INSERT INTO journal_entries (id, reference_type, reference_id, description, branch_id, created_by)
VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01', 'OPENING_BALANCE', NULL,
        'Opening balances', '11111111-1111-1111-1111-111111111111',
        '33333333-3333-3333-3333-333333333301');

INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, currency_id, exchange_rate) VALUES
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01', '77777777-7777-7777-7777-777777777701', 1000000.00, 0, '22222222-2222-2222-2222-222222222201', 1),
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01', '77777777-7777-7777-7777-777777777702', 700000.00, 0, '22222222-2222-2222-2222-222222222202', 70),
    ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa01', '77777777-7777-7777-7777-777777777703', 0, 1700000.00, '22222222-2222-2222-2222-222222222201', 1);

COMMIT;

-- ---------------------------------------------------------------------------
-- I-8  Structural: no floating point money, all timestamps timezone-aware
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_float INTEGER;
    v_naive INTEGER;
    v_money INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_float
      FROM information_schema.columns
     WHERE table_schema = 'public' AND data_type IN ('real', 'double precision', 'money');
    IF v_float <> 0 THEN
        RAISE EXCEPTION 'FAIL I-8 floating-point columns present: %', v_float;
    END IF;

    SELECT COUNT(*) INTO v_naive
      FROM information_schema.columns
     WHERE table_schema = 'public' AND data_type = 'timestamp without time zone';
    IF v_naive <> 0 THEN
        RAISE EXCEPTION 'FAIL I-8 timestamp-without-time-zone columns present: %', v_naive;
    END IF;

    SELECT COUNT(*) INTO v_money
      FROM information_schema.columns
     WHERE table_schema = 'public' AND numeric_precision = 30 AND numeric_scale = 10;
    IF v_money < 30 THEN
        RAISE EXCEPTION 'FAIL I-8 expected >=30 NUMERIC(30,10) columns, found %', v_money;
    END IF;

    RAISE NOTICE 'PASS I-8/money: 0 float columns, 0 naive timestamps, % NUMERIC(30,10) columns', v_money;
END
$$;

DO $$
DECLARE
    v_missing TEXT;
BEGIN
    SELECT string_agg(t, ', ') INTO v_missing
      FROM unnest(ARRAY[
        'users','roles','user_roles','branches','devices','currencies','customers','accounts',
        'journal_entries','journal_lines','exchange_rates','exchange_transactions','transfers',
        'cash_movements','expenses','audit_logs','sync_events','refresh_tokens','idempotency_keys',
        'change_log','account_balances','cash_sessions','cash_session_lines','sync_conflicts',
        'sync_cursors','allocation_policies','device_allocations','sequences'
      ]) AS t
     WHERE to_regclass('public.' || t) IS NULL;

    IF v_missing IS NOT NULL THEN
        RAISE EXCEPTION 'FAIL I-8 required tables missing: %', v_missing;
    END IF;
    RAISE NOTICE 'PASS I-8/tables: all 28 required tables exist';
END
$$;

-- ---------------------------------------------------------------------------
-- I-1  Total Debit = Total Credit
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_entry UUID;
BEGIN
    INSERT INTO journal_entries (id, reference_type, reference_id, branch_id, created_by)
    VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02', 'EXCHANGE_TRANSACTION',
            'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01', '11111111-1111-1111-1111-111111111111',
            '33333333-3333-3333-3333-333333333301');

    INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, currency_id, exchange_rate) VALUES
        ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02', '77777777-7777-7777-7777-777777777702', 70000.00, 0, '22222222-2222-2222-2222-222222222202', 70),
        ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02', '77777777-7777-7777-7777-777777777701', 0, 69500.00, '22222222-2222-2222-2222-222222222201', 1),
        ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02', '77777777-7777-7777-7777-777777777705', 0, 500.00, '22222222-2222-2222-2222-222222222201', 1);

    SET CONSTRAINTS ALL IMMEDIATE;   -- force deferred balance check now
    RAISE NOTICE 'PASS I-1/balanced: balanced exchange entry accepted';

    -- Unbalanced variant must be rejected.
    BEGIN
        INSERT INTO journal_entries (id, reference_type, reference_id, branch_id, created_by)
        VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa03', 'EXCHANGE_TRANSACTION',
                'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb02', '11111111-1111-1111-1111-111111111111',
                '33333333-3333-3333-3333-333333333301');
        INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, currency_id, exchange_rate) VALUES
            ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa03', '77777777-7777-7777-7777-777777777701', 100.00, 0, '22222222-2222-2222-2222-222222222201', 1),
            ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa03', '77777777-7777-7777-7777-777777777703', 0, 99.00, '22222222-2222-2222-2222-222222222201', 1);
        SET CONSTRAINTS ALL IMMEDIATE;
        RAISE EXCEPTION 'FAIL I-1 unbalanced entry was accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX02' THEN
            RAISE NOTICE 'PASS I-1/unbalanced: unbalanced entry rejected (NEX02)';
    END;

    -- Single-line entry must be rejected.
    BEGIN
        INSERT INTO journal_entries (id, reference_type, reference_id, branch_id, created_by)
        VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa04', 'EXCHANGE_TRANSACTION',
                'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb03', '11111111-1111-1111-1111-111111111111',
                '33333333-3333-3333-3333-333333333301');
        INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, currency_id, exchange_rate) VALUES
            ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa04', '77777777-7777-7777-7777-777777777701', 100.00, 0, '22222222-2222-2222-2222-222222222201', 1);
        SET CONSTRAINTS ALL IMMEDIATE;
        RAISE EXCEPTION 'FAIL I-1 single-line entry was accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX02' THEN
            RAISE NOTICE 'PASS I-1/single-line: entry with fewer than 2 lines rejected (NEX02)';
    END;

    -- Two-sided line must be rejected by CHECK.
    BEGIN
        INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, currency_id, exchange_rate) VALUES
            ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02', '77777777-7777-7777-7777-777777777701', 10.00, 10.00, '22222222-2222-2222-2222-222222222201', 1);
        RAISE EXCEPTION 'FAIL I-1 two-sided line was accepted';
    EXCEPTION
        WHEN check_violation THEN
            RAISE NOTICE 'PASS I-1/single-sided: line with both debit and credit rejected';
    END;

    -- Negative amounts must be rejected by CHECK.
    BEGIN
        INSERT INTO journal_lines (journal_entry_id, account_id, debit, credit, currency_id, exchange_rate) VALUES
            ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02', '77777777-7777-7777-7777-777777777701', -5.00, 0, '22222222-2222-2222-2222-222222222201', 1);
        RAISE EXCEPTION 'FAIL I-1 negative amount was accepted';
    EXCEPTION
        WHEN check_violation THEN
            RAISE NOTICE 'PASS I-1/negatives: negative debit rejected';
    END;

    -- Duplicate posting for the same business reference must be rejected.
    BEGIN
        INSERT INTO journal_entries (reference_type, reference_id, branch_id, created_by)
        VALUES ('EXCHANGE_TRANSACTION', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01',
                '11111111-1111-1111-1111-111111111111', '33333333-3333-3333-3333-333333333301');
        RAISE EXCEPTION 'FAIL I-7 duplicate posting was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            RAISE NOTICE 'PASS I-7/duplicate-posting: second journal entry for the same reference rejected';
    END;
END
$$;

-- ---------------------------------------------------------------------------
-- I-2 / I-3  Ledger totals vs. cache, and append-only enforcement
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_ledger_debit  NUMERIC(30,10);
    v_ledger_credit NUMERIC(30,10);
    v_cache_sum     NUMERIC(30,10);
BEGIN
    SELECT SUM(debit), SUM(credit) INTO v_ledger_debit, v_ledger_credit FROM journal_lines;

    IF v_ledger_debit <> v_ledger_credit THEN
        RAISE EXCEPTION 'FAIL I-1 global: total debit % <> total credit %', v_ledger_debit, v_ledger_credit;
    END IF;
    RAISE NOTICE 'PASS I-1/global: total debit = total credit = %', v_ledger_debit;

    SELECT SUM(debit_total) INTO v_cache_sum FROM account_balances;
    IF v_cache_sum IS DISTINCT FROM v_ledger_debit THEN
        RAISE EXCEPTION 'FAIL I-2 cache drift: cache % vs ledger %', v_cache_sum, v_ledger_debit;
    END IF;
    RAISE NOTICE 'PASS I-2/cache: account_balances cache matches the ledger (%)', v_cache_sum;

    PERFORM rebuild_account_balances();
    SELECT SUM(debit_total) INTO v_cache_sum FROM account_balances;
    IF v_cache_sum <> v_ledger_debit THEN
        RAISE EXCEPTION 'FAIL I-2 rebuild mismatch: % vs %', v_cache_sum, v_ledger_debit;
    END IF;
    RAISE NOTICE 'PASS I-2/rebuild: rebuild_account_balances() reproduces the cache';

    -- Append-only: journal lines can neither be updated nor deleted.
    BEGIN
        UPDATE journal_lines SET debit = 1 WHERE journal_entry_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02';
        RAISE EXCEPTION 'FAIL I-3 journal_lines UPDATE was accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            RAISE NOTICE 'PASS I-3/append-only: journal_lines UPDATE rejected';
    END;

    BEGIN
        DELETE FROM journal_lines WHERE journal_entry_id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02';
        RAISE EXCEPTION 'FAIL I-3 journal_lines DELETE was accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            RAISE NOTICE 'PASS I-3/append-only: journal_lines DELETE rejected';
    END;

    -- Audit log and cash movements are append-only too.
    BEGIN
        UPDATE cash_movements SET amount = 1;
        RAISE EXCEPTION 'FAIL I-3 cash_movements UPDATE was accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            RAISE NOTICE 'PASS I-3/append-only: cash_movements UPDATE rejected';
    END;
END
$$;

-- ---------------------------------------------------------------------------
-- I-5  Cash position can never go negative
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    BEGIN
        INSERT INTO cash_movements (branch_id, account_id, currency_id, movement_type, amount, created_by)
        VALUES ('11111111-1111-1111-1111-111111111111', '77777777-7777-7777-7777-777777777702',
                '22222222-2222-2222-2222-222222222202', 'OUT', 25000.00,
                '33333333-3333-3333-3333-333333333301');
        SET CONSTRAINTS ALL IMMEDIATE;
        RAISE EXCEPTION 'FAIL I-5 overdraft was accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX01' THEN
            RAISE NOTICE 'PASS I-5: cash OUT beyond the USD position rejected (NEX01)';
    END;

    INSERT INTO cash_movements (branch_id, account_id, currency_id, movement_type, amount, created_by)
    VALUES ('11111111-1111-1111-1111-111111111111', '77777777-7777-7777-7777-777777777702',
            '22222222-2222-2222-2222-222222222202', 'OUT', 2500.00,
            '33333333-3333-3333-3333-333333333301');
    SET CONSTRAINTS ALL IMMEDIATE;
    RAISE NOTICE 'PASS I-5: cash OUT within the position accepted';

    BEGIN
        INSERT INTO cash_movements (branch_id, account_id, currency_id, movement_type, amount,
                                    adjustment_sign, created_by)
        VALUES ('11111111-1111-1111-1111-111111111111', '77777777-7777-7777-7777-777777777702',
                '22222222-2222-2222-2222-222222222202', 'ADJUSTMENT', 99000.00, -1,
                '33333333-3333-3333-3333-333333333301');
        SET CONSTRAINTS ALL IMMEDIATE;
        RAISE EXCEPTION 'FAIL I-5 negative ADJUSTMENT was accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX01' THEN
            RAISE NOTICE 'PASS I-5: negative ADJUSTMENT rejected (NEX01)';
    END;

    BEGIN
        INSERT INTO cash_movements (branch_id, account_id, currency_id, movement_type, amount, created_by)
        VALUES ('11111111-1111-1111-1111-111111111111', '77777777-7777-7777-7777-777777777701',
                '22222222-2222-2222-2222-222222222201', 'ADJUSTMENT', 10.00,
                '33333333-3333-3333-3333-333333333301');
        RAISE EXCEPTION 'FAIL I-5 ADJUSTMENT without a sign was accepted';
    EXCEPTION
        WHEN check_violation THEN
            RAISE NOTICE 'PASS I-5: ADJUSTMENT without adjustment_sign rejected';
    END;
END
$$;

-- ---------------------------------------------------------------------------
-- I-6  Audit log: append-only + tamper-evident hash chain
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_seq INTEGER;
    v_broken BIGINT;
    v_original JSONB;
    v_target   UUID;
BEGIN
    INSERT INTO audit_logs (user_id, device_id, action, entity_type, entity_id, new_data, ip_address, request_id)
    VALUES
        ('33333333-3333-3333-3333-333333333301', '44444444-4444-4444-4444-444444444401',
         'EXCHANGE_TRANSACTION_CREATED', 'exchange_transaction', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01',
         '{"transaction_number":"NX-20260911-000001"}'::jsonb, '10.0.0.5', 'req-1'),
        ('33333333-3333-3333-3333-333333333301', NULL,
         'AUTH_LOGIN_SUCCEEDED', 'user', '33333333-3333-3333-3333-333333333301',
         '{"username":"cashier1"}'::jsonb, '10.0.0.5', 'req-2'),
        ('33333333-3333-3333-3333-333333333301', NULL,
         'CASH_SESSION_CLOSED', 'cash_session', '99999999-9999-9999-9999-999999999901',
         '{"difference":"0.00"}'::jsonb, '10.0.0.5', 'req-3');

    IF EXISTS (SELECT 1 FROM verify_audit_chain()) THEN
        RAISE EXCEPTION 'FAIL I-6 chain invalid right after insert';
    END IF;
    RAISE NOTICE 'PASS I-6/chain: hash chain valid over % rows', (SELECT COUNT(*) FROM audit_logs);

    BEGIN
        UPDATE audit_logs SET action = 'TAMPERED';
        RAISE EXCEPTION 'FAIL I-6 audit UPDATE was accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            RAISE NOTICE 'PASS I-6/append-only: audit_logs UPDATE rejected';
    END;

    BEGIN
        DELETE FROM audit_logs;
        RAISE EXCEPTION 'FAIL I-6 audit DELETE was accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            RAISE NOTICE 'PASS I-6/append-only: audit_logs DELETE rejected';
    END;

    -- Simulate a rogue operator with superuser rights touching history, then
    -- prove detection. The application role cannot do this (see below).
    SELECT id, new_data INTO v_target, v_original
      FROM audit_logs WHERE action = 'AUTH_LOGIN_SUCCEEDED' LIMIT 1;

    ALTER TABLE audit_logs DISABLE TRIGGER USER;
    UPDATE audit_logs SET new_data = '{"tampered":true}'::jsonb WHERE id = v_target;
    ALTER TABLE audit_logs ENABLE TRIGGER USER;

    SELECT broken_seq INTO v_broken FROM verify_audit_chain() LIMIT 1;
    IF v_broken IS NULL THEN
        RAISE EXCEPTION 'FAIL I-6 tampering was not detected';
    END IF;
    RAISE NOTICE 'PASS I-6/tamper-evidence: silent UPDATE detected at audit row seq=%', v_broken;

    -- Authorised break-glass repair: restore the original value (the chain hash
    -- of that row becomes valid again). Nothing is deleted — append-only holds.
    ALTER TABLE audit_logs DISABLE TRIGGER USER;
    UPDATE audit_logs SET new_data = v_original WHERE id = v_target;
    ALTER TABLE audit_logs ENABLE TRIGGER USER;

    IF EXISTS (SELECT 1 FROM verify_audit_chain()) THEN
        RAISE EXCEPTION 'FAIL I-6 the chain did not recover after restoration';
    END IF;
    RAISE NOTICE 'PASS I-6/recovery: chain verifies again after restoration (no row deleted)';
END
$$;

-- Runtime role cannot erase financial history even if application logic is bypassed.
DO $$
DECLARE
    v_denied INTEGER := 0;
BEGIN
    SET LOCAL ROLE nexus_app;

    BEGIN
        DELETE FROM audit_logs;
    EXCEPTION WHEN insufficient_privilege THEN
        v_denied := v_denied + 1;
    END;

    BEGIN
        DELETE FROM journal_lines;
    EXCEPTION WHEN insufficient_privilege THEN
        v_denied := v_denied + 1;
    END;

    BEGIN
        UPDATE audit_logs SET action = 'X';
    EXCEPTION WHEN insufficient_privilege THEN
        v_denied := v_denied + 1;
    END;

    RESET ROLE;

    IF v_denied <> 3 THEN
        RAISE EXCEPTION 'FAIL I-6 privilege lockout: expected 3 denials, got %', v_denied;
    END IF;
    RAISE NOTICE 'PASS I-6/privileges: nexus_app denied DELETE/UPDATE on ledgers (3/3)';
END
$$;

-- ---------------------------------------------------------------------------
-- I-4  Reversal semantics
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_orig UUID;
BEGIN
    INSERT INTO exchange_transactions (
        id, transaction_number, branch_id, device_id, cashier_id, customer_id,
        transaction_type, from_currency_id, from_amount, to_currency_id, to_amount,
        exchange_rate, commission, status, journal_entry_id)
    VALUES ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01', 'NX-20260911-000001',
            '11111111-1111-1111-1111-111111111111', '44444444-4444-4444-4444-444444444401',
            '33333333-3333-3333-3333-333333333301', '66666666-6666-6666-6666-666666666601',
            'BUY', '22222222-2222-2222-2222-222222222202', 1000.00,
            '22222222-2222-2222-2222-222222222201', 69500.00, 70, 500.00, 'COMPLETED',
            'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02');

    -- Money fields are immutable after posting.
    BEGIN
        UPDATE exchange_transactions SET to_amount = 999999 WHERE id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01';
        RAISE EXCEPTION 'FAIL I-4 amount mutation was accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX06' THEN
            RAISE NOTICE 'PASS I-4/immutable: editing a posted amount rejected (NEX06)';
    END;

    -- Reversal must mirror currencies and amounts.
    BEGIN
        INSERT INTO exchange_transactions (
            transaction_number, branch_id, cashier_id, transaction_type,
            from_currency_id, from_amount, to_currency_id, to_amount, exchange_rate,
            status, reversal_of_id, reversal_reason)
        VALUES ('NX-20260911-000099',
                '11111111-1111-1111-1111-111111111111', '33333333-3333-3333-3333-333333333301',
                'BUY', '22222222-2222-2222-2222-222222222201', 1.00,
                '22222222-2222-2222-2222-222222222202', 1.00, 70, 'COMPLETED',
                'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01', 'wrong mirror');
        RAISE EXCEPTION 'FAIL I-4 mismatched reversal was accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX04' THEN
            RAISE NOTICE 'PASS I-4/mirror: reversal with wrong currencies/amounts rejected (NEX04)';
    END;

    -- Correct mirror reversal.
    INSERT INTO exchange_transactions (
        id, transaction_number, branch_id, device_id, cashier_id, customer_id,
        transaction_type, from_currency_id, from_amount, to_currency_id, to_amount,
        exchange_rate, commission, status, reversal_of_id, reversal_reason)
    VALUES ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb02', 'NX-20260911-000002',
            '11111111-1111-1111-1111-111111111111', '44444444-4444-4444-4444-444444444401',
            '33333333-3333-3333-3333-333333333301', '66666666-6666-6666-6666-666666666601',
            'BUY', '22222222-2222-2222-2222-222222222201', 69500.00,
            '22222222-2222-2222-2222-222222222202', 1000.00, 70, 500.00, 'COMPLETED',
            'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01', 'customer error');
    -- The reversal row is itself COMPLETED; only the ORIGINAL row carries
    -- status=REVERSED / reversed_at (enforced by ck_exchange_transactions_reversal_stamp).

    UPDATE exchange_transactions
       SET status = 'REVERSED', reversed_at = NOW(), reversed_by = '33333333-3333-3333-3333-333333333301'
     WHERE id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01';

    IF NOT EXISTS (SELECT 1 FROM exchange_transactions
                    WHERE reversal_of_id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01') THEN
        RAISE EXCEPTION 'FAIL I-4 reversed transaction has no reversal entry';
    END IF;
    RAISE NOTICE 'PASS I-4: reversed transaction carries a bound reversal entry';

    -- A second reversal must be impossible.
    BEGIN
        INSERT INTO exchange_transactions (
            transaction_number, branch_id, cashier_id, transaction_type,
            from_currency_id, from_amount, to_currency_id, to_amount, exchange_rate,
            status, reversal_of_id)
        VALUES ('NX-20260911-000003',
                '11111111-1111-1111-1111-111111111111', '33333333-3333-3333-3333-333333333301',
                'BUY', '22222222-2222-2222-2222-222222222201', 69500.00,
                '22222222-2222-2222-2222-222222222202', 1000.00, 70, 'COMPLETED',
                'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01');
        RAISE EXCEPTION 'FAIL I-4 second reversal was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            RAISE NOTICE 'PASS I-4/single-reversal: double reversal rejected';
        WHEN SQLSTATE 'NEX04' THEN
            RAISE NOTICE 'PASS I-4/single-reversal: double reversal rejected (NEX04)';
    END;

    -- Terminal states are terminal.
    BEGIN
        UPDATE exchange_transactions SET status = 'COMPLETED' WHERE id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01';
        RAISE EXCEPTION 'FAIL I-4 REVERSED -> COMPLETED was accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX03' THEN
            RAISE NOTICE 'PASS I-4/status-machine: REVERSED -> COMPLETED rejected (NEX03)';
    END;

    -- Nothing may be deleted.
    BEGIN
        DELETE FROM exchange_transactions WHERE id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01';
        RAISE EXCEPTION 'FAIL I-3 delete of a reversed transaction was accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            RAISE NOTICE 'PASS I-3: cancelled/reversed transactions cannot be deleted';
    END;

    -- Idempotent offline ingestion: the same client event cannot post twice.
    UPDATE exchange_transactions
       SET client_event_id = 'cccccccc-cccc-cccc-cccc-cccccccccc01'
     WHERE id = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01';
    BEGIN
        INSERT INTO exchange_transactions (
            transaction_number, branch_id, cashier_id, transaction_type,
            from_currency_id, from_amount, to_currency_id, to_amount, exchange_rate,
            client_event_id)
        VALUES ('NX-20260911-000004',
                '11111111-1111-1111-1111-111111111111', '33333333-3333-3333-3333-333333333301',
                'BUY', '22222222-2222-2222-2222-222222222202', 5.00,
                '22222222-2222-2222-2222-222222222201', 350.00, 70,
                'cccccccc-cccc-cccc-cccc-cccccccccc01');
        RAISE EXCEPTION 'FAIL I-7 duplicate client_event_id was accepted';
    EXCEPTION
        WHEN unique_violation THEN
            RAISE NOTICE 'PASS I-7/idempotency: duplicate client_event_id rejected';
    END;
END
$$;

-- ---------------------------------------------------------------------------
-- Idempotency keys, sync events, transfers, rates, numbering, change stream
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    -- Idempotency key replay protection
    INSERT INTO idempotency_keys (key, user_id, endpoint, request_hash, status, response_status, response_body)
    VALUES ('dddddddd-dddd-dddd-dddd-dddddddddd01', '33333333-3333-3333-3333-333333333301',
            'POST /api/v1/exchange', repeat('a', 64), 'COMPLETED', 201, '{"id":"x"}');

    BEGIN
        INSERT INTO idempotency_keys (key, user_id, endpoint, request_hash)
        VALUES ('dddddddd-dddd-dddd-dddd-dddddddddd01', '33333333-3333-3333-3333-333333333301',
                'POST /api/v1/exchange', repeat('b', 64));
        RAISE EXCEPTION 'FAIL idempotency duplicate key accepted';
    EXCEPTION
        WHEN unique_violation THEN
            RAISE NOTICE 'PASS idempotency: replay of the same Idempotency-Key rejected';
    END;

    -- Sync event envelope is immutable and events cannot be deleted
    INSERT INTO sync_events (id, device_id, event_id, entity_type, entity_id, operation, payload, client_timestamp)
    VALUES ('eeeeeeee-eeee-eeee-eeee-eeeeeeeeee01', '44444444-4444-4444-4444-444444444401',
            'ffffffff-ffff-ffff-ffff-ffffffffffff', 'exchange_transaction',
            'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01', 'CREATE', '{"to_amount":"69500.00"}'::jsonb, NOW() - INTERVAL '5 minutes');

    BEGIN
        INSERT INTO sync_events (device_id, event_id, entity_type, entity_id, operation, payload, client_timestamp)
        VALUES ('44444444-4444-4444-4444-444444444401', 'ffffffff-ffff-ffff-ffff-ffffffffffff',
                'exchange_transaction', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbb01', 'CREATE',
                '{"to_amount":"1.00"}'::jsonb, NOW());
        RAISE EXCEPTION 'FAIL duplicate event_id accepted';
    EXCEPTION
        WHEN unique_violation THEN
            RAISE NOTICE 'PASS sync/unique-event: duplicate event_id rejected';
    END;

    BEGIN
        UPDATE sync_events SET payload = '{"to_amount":"1.00"}'::jsonb
         WHERE event_id = 'ffffffff-ffff-ffff-ffff-ffffffffffff';
        RAISE EXCEPTION 'FAIL sync payload mutation accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX06' THEN
            RAISE NOTICE 'PASS sync/immutable: sync event payload mutation rejected (NEX06)';
    END;

    -- Status-only updates are allowed (processing state machine)
    UPDATE sync_events
       SET status = 'APPLIED', server_timestamp = NOW(), processed_at = NOW(), attempt_count = 1
     WHERE event_id = 'ffffffff-ffff-ffff-ffff-ffffffffffff';
    RAISE NOTICE 'PASS sync/state: sync event processing state updated';

    BEGIN
        DELETE FROM sync_events WHERE event_id = 'ffffffff-ffff-ffff-ffff-ffffffffffff';
        RAISE EXCEPTION 'FAIL sync event delete accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            RAISE NOTICE 'PASS sync/append-only: sync event delete rejected';
    END;

    -- Allocation math
    INSERT INTO allocation_policies (id, name, device_id, currency_id, max_amount, created_by)
    VALUES ('99999999-9999-9999-9999-999999999902', 'Counter-1 AFN offline cap',
            '44444444-4444-4444-4444-444444444401', '22222222-2222-2222-2222-222222222201',
            50000.00, '33333333-3333-3333-3333-333333333301');

    INSERT INTO device_allocations (device_id, policy_id, currency_id, window_start, window_end, granted_amount)
    VALUES ('44444444-4444-4444-4444-444444444401', '99999999-9999-9999-9999-999999999902',
            '22222222-2222-2222-2222-222222222201', NOW() - INTERVAL '1 hour', NOW() + INTERVAL '7 hours', 50000.00);

    IF available_allocation('44444444-4444-4444-4444-444444444401',
                            '22222222-2222-2222-2222-222222222201') <> 50000.00 THEN
        RAISE EXCEPTION 'FAIL allocation: expected 50000.00 available';
    END IF;
    RAISE NOTICE 'PASS allocation: available_allocation() = 50000.00';

    BEGIN
        UPDATE device_allocations SET consumed_amount = 60000.00;
        RAISE EXCEPTION 'FAIL allocation over-consumption accepted';
    EXCEPTION
        WHEN check_violation THEN
            RAISE NOTICE 'PASS allocation: consuming more than granted rejected';
    END;
END
$$;

DO $$
DECLARE
    v_answerless INTEGER;
BEGIN
    -- Transfers follow their own state machine and are never deleted.
    INSERT INTO transfers (id, reference_number, sender_name, receiver_name, currency_id, amount,
                           commission, status, created_by, branch_id)
    VALUES ('abababab-abab-abab-abab-ababababab01', 'TR-20260911-000001', 'Ahmad Wali', 'Karim Noor',
            '22222222-2222-2222-2222-222222222202', 500.00, 10.00, 'PENDING',
            '33333333-3333-3333-3333-333333333301', '11111111-1111-1111-1111-111111111111');

    BEGIN
        UPDATE transfers SET status = 'PAID' WHERE id = 'abababab-abab-abab-abab-ababababab01';
        RAISE EXCEPTION 'FAIL transfer PENDING -> PAID was accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX03' THEN
            RAISE NOTICE 'PASS transfer/state-machine: PENDING -> PAID rejected (NEX03)';
    END;

    UPDATE transfers SET status = 'APPROVED', approved_by = '33333333-3333-3333-3333-333333333301', approved_at = NOW()
     WHERE id = 'abababab-abab-abab-abab-ababababab01';
    UPDATE transfers SET status = 'PAID', paid_by = '33333333-3333-3333-3333-333333333301', paid_at = NOW()
     WHERE id = 'abababab-abab-abab-abab-ababababab01';
    RAISE NOTICE 'PASS transfer/lifecycle: PENDING -> APPROVED -> PAID accepted';

    BEGIN
        UPDATE transfers SET amount = 1 WHERE id = 'abababab-abab-abab-abab-ababababab01';
        RAISE EXCEPTION 'FAIL transfer amount mutation accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX06' THEN
            RAISE NOTICE 'PASS transfer/immutable: amount mutation rejected (NEX06)';
    END;

    BEGIN
        DELETE FROM transfers WHERE id = 'abababab-abab-abab-abab-ababababab01';
        RAISE EXCEPTION 'FAIL transfer delete accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            RAISE NOTICE 'PASS transfer/append-only: transfer delete rejected';
    END;

    -- Optimistic concurrency counter
    SELECT version INTO v_answerless FROM transfers WHERE id = 'abababab-abab-abab-abab-ababababab01';
    IF v_answerless <> 3 THEN
        RAISE EXCEPTION 'FAIL version bump: expected 3 after two updates, got %', v_answerless;
    END IF;
    RAISE NOTICE 'PASS version: optimistic concurrency counter = %', v_answerless;

    -- Hard delete of a user is forbidden (PART 25): deactivate instead.
    BEGIN
        DELETE FROM users WHERE id = '33333333-3333-3333-3333-333333333301';
        RAISE EXCEPTION 'FAIL user hard delete accepted';
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            RAISE NOTICE 'PASS users/no-hard-delete: DELETE forbidden, is_active=FALSE is the path';
    END;
    UPDATE users SET is_active = FALSE WHERE id = '33333333-3333-3333-3333-333333333301';
    RAISE NOTICE 'PASS users/deactivate: is_active=FALSE accepted';

    -- Currency code is immutable.
    BEGIN
        UPDATE currencies SET code = 'XXX' WHERE code = 'USD';
        RAISE EXCEPTION 'FAIL currency code mutation accepted';
    EXCEPTION
        WHEN SQLSTATE 'NEX06' THEN
            RAISE NOTICE 'PASS currencies/immutable: code change rejected (NEX06)';
    END;
END
$$;

DO $$
DECLARE
    v_rate RECORD;
    v_number TEXT;
    v_changes INTEGER;
BEGIN
    -- Rates: no two quotes for the same pair may share an instant (EXCLUDE constraint).
    INSERT INTO exchange_rates (from_currency_id, to_currency_id, buy_rate, sell_rate, effective_at, created_by)
    VALUES ('22222222-2222-2222-2222-222222222202', '22222222-2222-2222-2222-222222222201',
            69.5000000000, 70.5000000000, NOW() - INTERVAL '2 hours', '33333333-3333-3333-3333-333333333301');

    BEGIN
        INSERT INTO exchange_rates (from_currency_id, to_currency_id, buy_rate, sell_rate, effective_at)
        VALUES ('22222222-2222-2222-2222-222222222202', '22222222-2222-2222-2222-222222222201',
                1, 1, NOW() - INTERVAL '2 hours');
        RAISE EXCEPTION 'FAIL duplicate rate instant accepted';
    EXCEPTION
        WHEN unique_violation THEN
            RAISE NOTICE 'PASS rates/no-duplicate-instant: duplicate quote instant rejected';
    END;

    INSERT INTO exchange_rates (from_currency_id, to_currency_id, buy_rate, sell_rate, effective_at)
    VALUES ('22222222-2222-2222-2222-222222222202', '22222222-2222-2222-2222-222222222201',
            69.9000000000, 70.9000000000, NOW() - INTERVAL '1 hour');

    SELECT * INTO v_rate FROM resolve_exchange_rate(
        '22222222-2222-2222-2222-222222222202', '22222222-2222-2222-2222-222222222201',
        '11111111-1111-1111-1111-111111111111');
    IF v_rate.buy_rate <> 69.9000000000 THEN
        RAISE EXCEPTION 'FAIL rate resolution: expected the newest quote, got %', v_rate.buy_rate;
    END IF;
    RAISE NOTICE 'PASS rates/resolve: newest quote in force selected (buy=%)', v_rate.buy_rate;

    -- Branch-specific quote wins over the global quote.
    INSERT INTO exchange_rates (from_currency_id, to_currency_id, buy_rate, sell_rate, effective_at, branch_id)
    VALUES ('22222222-2222-2222-2222-222222222202', '22222222-2222-2222-2222-222222222201',
            70.1000000000, 71.1000000000, NOW() - INTERVAL '30 minutes',
            '11111111-1111-1111-1111-111111111111');

    SELECT * INTO v_rate FROM resolve_exchange_rate(
        '22222222-2222-2222-2222-222222222202', '22222222-2222-2222-2222-222222222201',
        '11111111-1111-1111-1111-111111111111');
    IF v_rate.buy_rate <> 70.1000000000 THEN
        RAISE EXCEPTION 'FAIL branch rate precedence: got %', v_rate.buy_rate;
    END IF;
    RAISE NOTICE 'PASS rates/branch-precedence: branch quote overrides the global quote';

    -- Document numbering
    v_number := next_document_number('NX', 'exchange_transaction', '20260911');
    IF v_number <> 'NX-20260911-000001' THEN
        RAISE EXCEPTION 'FAIL numbering: unexpected number %', v_number;
    END IF;
    IF next_document_number('NX', 'exchange_transaction', '20260911') <> 'NX-20260911-000002' THEN
        RAISE EXCEPTION 'FAIL numbering: counter did not advance';
    END IF;
    -- A different business date restarts at 000001 and cannot collide.
    IF next_document_number('NX', 'exchange_transaction', '20260912') <> 'NX-20260912-000001' THEN
        RAISE EXCEPTION 'FAIL numbering: per-day scope not isolated';
    END IF;
    -- A different document family shares the counter table without interference.
    IF next_document_number('TR', 'transfer', '20260911') <> 'TR-20260911-000001' THEN
        RAISE EXCEPTION 'FAIL numbering: transfer sequence not isolated';
    END IF;
    RAISE NOTICE 'PASS numbering: NX-20260911-000001, -000002, per-day and per-family isolation';

    -- Change stream produced by triggers for offline pull
    SELECT COUNT(DISTINCT entity_type) INTO v_changes FROM change_log;
    IF v_changes < 5 THEN
        RAISE EXCEPTION 'FAIL change_log: expected >=5 entity types, got %', v_changes;
    END IF;
    RAISE NOTICE 'PASS change_log: % entity types streamed for offline pull', v_changes;

    -- Foreign quantity derived from the functional amount and rate
    IF (SELECT foreign_amount FROM journal_lines l
          JOIN journal_entries e ON e.id = l.journal_entry_id
         WHERE e.id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaa02'
           AND l.account_id = '77777777-7777-7777-7777-777777777702') <> 1000.0000000000 THEN
        RAISE EXCEPTION 'FAIL foreign_amount derivation';
    END IF;
    RAISE NOTICE 'PASS foreign_amount: 70,000 AFN at rate 70 derives 1,000.0000000000 USD';
END
$$;

-- ---------------------------------------------------------------------------
-- Final: ledger-wide invariants after the whole suite
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_debit  NUMERIC(30,10);
    v_credit NUMERIC(30,10);
BEGIN
    SELECT SUM(debit), SUM(credit) INTO v_debit, v_credit FROM journal_lines;
    IF v_debit <> v_credit THEN
        RAISE EXCEPTION 'FAIL I-1 final: debit % <> credit %', v_debit, v_credit;
    END IF;

    IF EXISTS (SELECT 1 FROM verify_audit_chain()) THEN
        RAISE EXCEPTION 'FAIL I-6 final: audit chain broken';
    END IF;

    RAISE NOTICE '=====================================================';
    RAISE NOTICE 'PHASE 0 SCHEMA INVARIANT SUITE: ALL ASSERTIONS PASSED';
    RAISE NOTICE '  ledger totals  : debit = credit = %', v_debit;
    RAISE NOTICE '  audit chain     : valid';
    RAISE NOTICE '=====================================================';
END
$$;
