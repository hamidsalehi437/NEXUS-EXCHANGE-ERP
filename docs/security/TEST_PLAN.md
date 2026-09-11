# NEXUS EXCHANGE ERP — Test & Verification Plan

| Field | Value |
| --- | --- |
| Document ID | `SEC-TEST-001` |
| Version | 1.0 (Phase 0) |
| Status | **Proposed — pending Phase 0 approval** |
| Scope | Backend (Python), Flutter clients, database invariants, security, performance, restore drills |

> **خلاصه فارسی** — برنامه آزمون: هرم تست (واحد، یکپارچه، E2E، تغییرناپذیرها)، ابزارها (pytest، Hypothesis، Drift in-memory، k6)، آزمون‌های الزامی پرامپت (احراز هویت، ارز، حسابداری، صندوق، همگام‌سازی)، ماتریس امنیتی، دروازه‌های CI و شرط «تست‌ها پاس شده‌اند». نتیجه اجرای واقعی فاز صفر (۲۷ ادعای تغییرناپذیری روی PostgreSQL 16.2) نیز در بخش ۳ ثبت شده است.

---

## 1. Strategy and pyramid

| Level | Share | Tool | Runs |
| --- | --- | --- | --- |
| Unit / property | ~60 % | `pytest`, `hypothesis` | Every commit |
| Integration (DB, API, sync) | ~30 % | `pytest` + a real PostgreSQL 16 container, `httpx.ASGITransport` | Every PR |
| End-to-end (compose stack) | ~10 % | `pytest` against nginx+api+postgres+redis, Flutter `integration_test` | Every PR (nightly for the slow set) |
| Non-functional | Gate-based | `k6`, restore drills, dependency audit | Pre-release, nightly |

Rules: no test asserts on a mock of the thing it is meant to verify (no fake financial calculation); financial tests run against **real PostgreSQL** because the invariants live there; every defect fixed adds a regression test named after the invariant or the incident.

## 2. Environments and fixtures

| Item | Approach |
| --- | --- |
| Database per test session | `pytest-postgresql`/Docker-managed PostgreSQL 16, `schema.sql` applied once, transactions rolled back per test (except sync tests that must commit) |
| Seed fixture | `tests/fixtures`: currencies (AFN base, USD, EUR, PKR), one branch, one device, a cashier, a manager, a customer, the chart of accounts |
| Time control | `freezegun` for business dates and rate effective windows; the database clock is never mocked for invariant tests |
| Flutter local DB | In-memory Drift (`NativeDatabase.memory()`) with the same schema version as production |
| Fake server for offline tests | A `MockAdapter` on Dio plus the real API in a container for the "reconnect" scenario; never a mocked ledger |

## 3. Phase 0 evidence (already executed)

| Artifact | Command | Result |
| --- | --- | --- |
| Normative DDL | `psql -v ON_ERROR_STOP=1 -f docs/database/schema.sql` | exit 0; self-check: 30 `NUMERIC(30,10)` columns, 0 float columns |
| Invariant suite | `psql -v ON_ERROR_STOP=1 -f tests/invariants/phase0_schema_invariants.sql` | exit 0; **27/27 PASS**; Σdebit = Σcredit = 1,770,000.0000000000; audit chain valid |
| Object inventory | `information_schema` | 31 tables, 5 views, 23 functions, 58 triggers, 103 indexes, 413 constraints |

The suite covers: money/type structure (I-8), journal balance/single-sidedness/negatives/duplicate posting (I-1, I-7), cache-vs-ledger equality and rebuild (I-2), append-only enforcement (I-3), reversal mirroring/binding/state machine (I-4), non-negative cash and adjustment signs (I-5), audit chain validity, tamper detection and privilege lockout (I-6), idempotency keys, sync event immutability, allocation arithmetic, transfer lifecycle and version bumps, rate resolution and duplicate-instant rejection, document numbering, change-stream coverage and derived foreign quantity.

This suite becomes part of CI in Phase 1 and must stay green for every subsequent phase.

## 4. Mandatory test catalogue (PART 48)

### 4.1 Authentication (Phase 2)

| Case | Expected |
| --- | --- |
| Valid login | 200, access + refresh, device bound, `AUTH_LOGIN_SUCCEEDED` audited |
| Invalid password | 401 `INVALID_CREDENTIALS`, `failed_login_attempts` incremented, audited |
| Unknown user | 401 with the same message and timing profile as a wrong password (no user enumeration) |
| Expired access token | 401 `TOKEN_EXPIRED` |
| Refresh rotation | New token issued, old marked used; replaying the old token → 401 `TOKEN_REVOKED` + family revoked |
| Revoked device | 401 `DEVICE_REVOKED` on API **and** sync |
| Token from another device | 401 `DEVICE_MISMATCH` |
| Lockout | 5 failures → 423 `ACCOUNT_LOCKED`, unlock after the window |
| Permission denied | Authenticated cashier calling `POST /exchange/{id}/reverse` → 403 `PERMISSION_DENIED` |
| Branch scope | Manager of branch A reading branch B data → 403 `FORBIDDEN_SCOPE` / 404 |
| Password change | Other sessions revoked, audit event written |

### 4.2 Exchange (Phase 5)

| Case | Expected |
| --- | --- |
| Valid buy | 201, correct `to_amount`, journal balanced, cash movements recorded, receipt payload |
| Valid sell | 201, FX result computed against the carrying rate, cash movements mirrored |
| Commission path | Both types: commission goes to 4010, cash legs match physical flow |
| Invalid currency (inactive/unknown) | 422 `CURRENCY_INACTIVE` / `RESOURCE_NOT_FOUND` |
| Invalid amount (0, negative, > 10 dp, non-numeric) | 422 `VALIDATION_ERROR` |
| Insufficient balance | 409 `INSUFFICIENT_BALANCE` (both the service check and the DB trigger) |
| Rate not found / out of tolerance | 422 `RATE_NOT_FOUND` / 409 `RATE_OUT_OF_TOLERANCE` |
| Client-computed `to_amount` mismatch | 422 `AMOUNT_MISMATCH` |
| Cancellation | `COMPLETED → CANCELLED`, journal reversed, no deletion |
| Reversal | Mirror transaction + reversal journal + audit; original `REVERSED` |
| Double reversal | 409 `ALREADY_REVERSED` |
| Idempotent replay | Same `Idempotency-Key` + body → identical response, exactly one ledger posting |
| Idempotency key reuse with a different body | 409 `IDEMPOTENCY_KEY_REUSED` |

### 4.3 Accounting (Phase 4)

| Case | Expected |
| --- | --- |
| Balanced entry accepted | Commit succeeds |
| Unbalanced entry rejected | SQLSTATE `NEX02` surfaced as `JOURNAL_UNBALANCED` (500 defect path, tested at the service level too) |
| Entry with < 2 lines rejected | `NEX02` |
| Line with both debit and credit rejected | CHECK violation |
| Duplicate posting for one reference | Unique violation → 409 `DUPLICATE_RESOURCE`/defect path asserted |
| Cache equals ledger after operations | `account_balances` = Σ lines; `rebuild_account_balances()` reproduces it |
| Money never float | Type scan over models/schemas + round-trip of a 10-dp amount through API and Dart |
| Rounding policy | `ROUND_HALF_UP` at currency decimals for final amounts; intermediates exact |
| Reversal entries mirror originals | Line-by-line debit/credit swap with equal totals |

### 4.4 Cash (Phase 6)

| Case | Expected |
| --- | --- |
| Open session | One open session per device; second attempt → 409 `CASH_SESSION_ALREADY_OPEN` |
| Opening balance | `OPENING` movement + journal entry |
| Cash in / out | Journal + movement; missing counter-account → 422 `CASH_COUNTER_ACCOUNT_REQUIRED` |
| Overdraft | 409 `INSUFFICIENT_BALANCE` (service and trigger) |
| Close with exact count | Difference 0, session `CLOSED`, no adjustment |
| Close with difference | Difference recorded, `ADJUSTMENT` + journal to 5090, audited |
| Close without counted amounts | 422 `CASH_RECON_INCOMPLETE` |
| Movement history filters | Only the requested branch/currency/type/date range returned |

### 4.5 Sync (Phase 8)

See `SYNC_DESIGN.md` §15 for the ten scenarios (offline create, reconnect, duplicate event, conflict, allocation exhaustion, rate mismatch, retry, failed sync, business-date skew, cursor expiry). Additional invariants: repeated pushes of the same batch produce exactly one ledger posting per event; conflicting events never partially apply; a revoked device cannot post at all.

### 4.6 Reports (Phase 7)

| Case | Expected |
| --- | --- |
| Trial balance balances | Σdebit = Σcredit for any range; ties to `v_trial_balance` |
| Profit/Loss ties out | Net income equals ΣREVENUE − ΣEXPENSE and reconciles with cash+inventory movement |
| Daily report | Matches the seeded scenario totals, per branch/currency/cashier |
| General ledger | Running balance equals the account balance at the end of the range |
| Filters | `from/to/branch_id/currency_id/cashier_id` all honoured; invalid ranges → 422 |
| Reversed documents | Included in history with their reversal, excluded from active totals appropriately |

## 5. Security test matrix (Phase 12)

| Area | Cases |
| --- | --- |
| Authentication | §4.1 in full, plus timing/enumeration checks and JWT tampering (`alg: none`, wrong `kid`, modified claims) |
| Authorization | Every cell of `API_CONTRACT.md` §8 for each role: allowed and denied |
| Injection | SQLi/NoSQL-style payloads in `q`, `sort`, filters, report parameters, sync payloads; assert no error leakage and no data crossing |
| Input handling | Oversized payloads, wrong content types, unknown fields, deeply nested JSON, unicode/RTL edge cases in names |
| Rate limits | Login, refresh, financial writes, sync push; `Retry-After` correctness |
| Idempotency | Replay, key reuse, concurrent duplicates |
| Audit | Chain verification, tamper detection (with and without superuser simulation), append-only denials for `nexus_app` |
| Device lifecycle | Register, revoke mid-session, revoked device sync, re-registration |
| Offline abuse | Allocation exhaustion, expired window, clock tampering, tampered local DB chain head |
| Secrets | CI secret scanning, no secrets in logs/images/artifacts |
| Dependencies | `pip-audit`, `npm audit`, container image scan; zero unpatched high/critical |
| Backup/restore | Encrypted backup, restore into a scratch DB, chain + balances verified |

## 6. CI/CD gates (PART 56)

```text
push / PR
  ├─ backend-lint      ruff check + ruff format --check
  ├─ backend-types     mypy --strict (app + tests typed where practical)
  ├─ schema-gate       apply schema.sql to a fresh PG 16 → run the invariant suite
  │                    (extension-free; no contrib required)
  ├─ migrate-gate      alembic upgrade head on empty DB → `alembic check` shows no drift
  ├─ backend-tests     unit + integration with coverage ≥ 85 % on services/ and core/
  ├─ security-tests    authz matrix, injection suite, idempotency, rate limits
  ├─ flutter-analyze   flutter analyze --fatal-infos
  ├─ flutter-tests     unit + widget + offline sync harness
  ├─ e2e               docker compose up → login → buy → sell → report → reversal
  └─ build             API image, Android AAB, Windows installer  (release job only)
```

**A failing gate blocks the release.** A phase is complete only when the gates for its scope are green and its required tests are present — "code written but untested" is not a completed phase (PART 59).

## 7. Performance and resilience

| Test | Target |
| --- | --- |
| `POST /exchange` load (10 concurrent cashiers) | p95 ≤ 300 ms, zero invariant violations |
| Rate resolution | p95 ≤ 20 ms |
| Daily report, 30 days, 5 branches | ≤ 1.5 s |
| Sync push batch of 50 events | ≤ 2 s, exactly-once application |
| Sustained 100 req/s for 5 min | No connection-pool exhaustion, error rate < 0.1 % |
| PostgreSQL restart during traffic | API returns 503 with `Retry-After`, recovers without data loss |
| Redis loss | API keeps serving (degraded rate limiting), no money impact |
| Backup restore drill | Restore + verify chain/balances within RTO 2 h, RPO ≤ 15 min |

## 8. Definition of "tests pass" for a phase report

1. Every command is recorded verbatim with its exit code.
2. Counts are reported (tests, assertions, coverage) and the *failing* tests, if any, are named — never summarised away.
3. Any defect found is listed with the fix and the added regression test (as done in Phase 0, `SCHEMA.md` §2).
4. Known limitations are stated explicitly rather than hidden behind "coming soon" (which is banned by PART 60).

## 9. Traceability

| Master prompt | Section |
| --- | --- |
| PART 48 | §4 |
| PART 49 | §3, §4.3 |
| PART 56 | §6 |
| PART 59 | §8 |
| PART 60 | §8 (no placeholders in tests either) |
| PART 67 | §6 (MVP acceptance is gated by these suites) |
