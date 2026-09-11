# NEXUS EXCHANGE ERP — Security Architecture

| Field | Value |
| --- | --- |
| Document ID | `SEC-ARCH-001` |
| Version | 1.0 (Phase 0) |
| Status | **Proposed — pending Phase 0 approval** |
| Owner | Security |
| Related | `docs/api/API_CONTRACT.md`, `docs/architecture/SYNC_DESIGN.md`, `docs/security/TEST_PLAN.md` |

> **خلاصه فارسی** — مدل امنیتی: رمز عبور با Argon2id، JWT کوتاه‌مدت + Refresh Rotation با تشخیص استفاده مجدد، اتصال توکن به دستگاه ثبت‌شده، RBAC با «منع صریح» و محدوده شعبه، رمزنگاری در انتقال و در حالت سکون، لاگ ممیزی زنجیره‌ای (hash chain) و جدول کامل «الزام امنیتی → پیاده‌سازی → نحوه راستی‌آزمایی». بخش ۱۰ مرز قانونی پروژه را صریح تعیین می‌کند: هیچ قابلیتی برای پنهان‌سازی، جعل یا حذف سوابق مالی ساخته نمی‌شود.

---

## 1. Assets and threat model

**Assets, in order of sensitivity**

1. The general ledger and financial documents (integrity and completeness matter more than confidentiality).
2. Cash positions and currency inventory (what a branch can pay out).
3. Customer PII (names, phones, addresses) and transaction history.
4. Credentials, tokens, device keys and database backups.
5. Exchange-rate history and commission structure (commercially sensitive).

**Actors and trust boundaries**

| Actor | Trust | Boundary |
| --- | --- | --- |
| Cashier (device) | Low-trust client; may be offline | Mutual: device is authenticated, but its arithmetic is never trusted |
| Manager / accountant | Medium privilege | RBAC + branch scope |
| Owner / super admin | High privilege | Still cannot delete history; actions are audited |
| External auditor | Read-only | `nexus_auditor` DB role, `audit.view` permission |
| Hostile network | Untrusted | TLS only; no internal service exposed |
| Compromised device | Untrusted | Allowance-bounded, revocable, idempotent ingestion, server recomputation |
| Malicious insider with DB access | Partially trusted | Append-only tables, revoked `DELETE`, hash chain provides *evidence* |

**Threat table (selected)**

| Threat | Impact | Control |
| --- | --- | --- |
| Stolen refresh token | Account takeover | Rotation + family reuse detection + device binding + instant revocation |
| Offline device inflating balances | Money creation | Server-granted allocation, number blocks, rate snapshots, server recomputation, non-negative cash trigger |
| Insider editing a past sale | Fraud, false reporting | Money columns immutable, append-only ledger, hash-chained audit, `nexus_app` has no `DELETE`/`UPDATE` on ledgers |
| Deleting an inconvenient transaction | Concealment | Hard delete forbidden by trigger and revoked grant; reversal is the only path |
| SQL injection | Full compromise | SQLAlchemy parameterisation everywhere; no string-built SQL; `ruff`/review gate; injection tests in the suite |
| Credential stuffing | Account takeover | Argon2id, rate limiting, lockout, structured audit of failures |
| Tampered local database | False local totals | Local audit chain heads are sent on sync; server results are authoritative |
| Backup theft | PII/ledger exposure | Encrypted backups, keys held outside the repository, access logged |
| Privilege escalation via role edit | Unauthorized money movement | `SUPER_ADMIN` is a system role; permission changes are audited diffs; explicit denies win |
| Log leakage of PII/secrets | Privacy breach | Structured logging with redaction; tokens/passwords never logged |
| Replay of a financial request | Double posting | `Idempotency-Key` + `event_id`/`client_event_id` uniqueness |

## 2. Authentication

| Control | Implementation |
| --- | --- |
| Password hashing | **Argon2id** (`time_cost = 3`, `memory_cost = 64 MiB`, `parallelism = 4`, 16-byte salt, 32-byte output), tuned to ≈150 ms on the reference server; parameters are re-verified on login and the hash is transparently upgraded when policy changes |
| Password policy | Minimum 12 characters, checked against a common-password blocklist, no forced periodic rotation (NIST SP 800-63B), rotation forced on suspected compromise |
| Access token | JWT, 15-minute lifetime, claims `sub`, `jti`, `did`, `bid`, `roles`, `iat`, `exp`; `kid` header enables key rotation; `jti` revocation set kept in Redis for logout/kill |
| Refresh token | 256-bit opaque random value; only its SHA-256 is persisted; lifetime 30 days; bound to a device row |
| Rotation & reuse detection | Every refresh consumes the presented token and issues a new one in the same family; a reused token revokes the entire family, raises `SECURITY_REFRESH_REUSE_DETECTED`, and forces re-authentication |
| Device binding | Login registers/binds a device (`device_uuid`); tokens carry `did`; a token used from another device is rejected (`DEVICE_MISMATCH`) |
| Login throttling | Per (IP, username) rate limit (10 / 5 min) and per-account lockout after 5 failures (`locked_until`) |
| MFA | Not in the MVP; the schema and login flow reserve `must_change_password`/future `mfa_secret` handling so it can be added without breaking the contract. Recorded as a known limitation (`ROADMAP.md` §Risks) |
| Session lifecycle | Logout revokes the refresh family; password change revokes all other sessions; device revocation kills all of that device's sessions immediately |

## 3. Authorization

| Layer | Mechanism |
| --- | --- |
| Role-based | Roles `SUPER_ADMIN`, `OWNER`, `MANAGER`, `ACCOUNTANT`, `CASHIER`, `AUDITOR` → permission sets (`role_permissions`) |
| Explicit per-user | `user_permissions.is_granted = FALSE` (deny) always wins over any role grant; used for suspension and least-privilege scoping |
| Endpoint enforcement | FastAPI dependency per route (`require("exchange.create")`); **401** for unauthenticated, **403** for authenticated-but-forbidden |
| Object-level | Every query is scoped by branch (`FORBIDDEN_SCOPE`, 403) unless the actor holds a group-wide permission; `RESOURCE_NOT_FOUND` is returned instead of leaking cross-branch existence |
| State-level | Reversal requires `exchange.reverse`, cancellation `exchange.cancel`, approvals `transfers.approve` — separate permissions so one person can be prevented from both creating and reversing |
| Segregation of duties | Recommended production configuration: the cashier cannot reverse; the accountant cannot create cash movements; the owner's own actions are still audited |
| Sync path | Offline-created events are re-authorized server-side against the same permission set — being offline never grants extra rights |

## 4. Data protection

| State | Control |
| --- | --- |
| In transit (client ↔ server) | TLS 1.2+ (1.3 preferred), HSTS 1 year, certificate pinning optional in the client, no plaintext fallback |
| In transit (app ↔ PostgreSQL/Redis) | Same-host container network in the MVP; TLS enforced when databases are remote; Redis requires `requirepass` + ACL user, never exposed publicly |
| At rest (server) | Full-disk/volume encryption on the host; encrypted backups; secrets injected at runtime |
| At rest (device) | SQLCipher with a per-installation key; Android Keystore / Windows DPAPI protects the key; the key never syncs |
| Backups | Encrypted with a recipient key held outside the repository; checksum recorded; restore requires both the artifact and the key |
| PII minimisation | Only what a receipt and the law require: name, phone, address, notes, `national_id_last4`. Full KYC documents are **not** stored in the MVP |
| Logging | Structured logs redact passwords, tokens, hashes, full PII; customer names are truncated in logs; audit rows store business data deliberately (they are the evidence trail) and are access-controlled |
| Retention | Ledger/audit rows are retained per legal requirement (default 10 years); customer data retention policy configurable; deletion is *anonymisation* of PII fields where the law allows, never deletion of ledger rows |
| Data subject requests | The system can export a customer's data and anonymise PII while preserving financial history (legal obligation); both actions are audited |

## 5. Transport, network and platform hardening

| Area | Control |
| --- | --- |
| Public exposure | Only `nginx` publishes ports (80→HTTPS redirect, 443). `api`, `postgres`, `redis` and `worker` stay on the internal compose network |
| TLS | Modern cipher suite, TLS 1.2+; certificate renewal automated (Let's Encrypt/corporate CA); OCSP stapling |
| Security headers (nginx) | `Strict-Transport-Security`, `Content-Security-Policy` (admin UI: `default-src 'self'`), `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy` minimal, `Cache-Control: no-store` on API responses |
| Request limits | Body size caps, timeouts, `client_max_body_size` tuned per endpoint, edge rate limiting |
| Containers | Non-root user, read-only root filesystem where possible, dropped capabilities, no `privileged`, pinned image digests, healthchecks |
| Host | Minimal packages, unattended security updates, SSH keys only, firewall allowing 22/80/443, fail2ban-class protection for SSH, separate non-root deployment user |
| Secrets | Never in Git (`.gitignore` + CI secret scanning); production values via Docker/K8s secrets or a vault; `.env` is development-only and chmod 600 |
| Secret rotation | JWT signing keys rotate with `kid` (old keys verify for one access-token lifetime); DB passwords and API keys rotate at least annually and after any suspected exposure |

## 6. Application security

| Concern | Control |
| --- | --- |
| Input validation | Pydantic v2 models, strict types, `Decimal` from strings, unknown fields rejected, length/regex bounds matching the database `CHECK`s |
| SQL injection | SQLAlchemy 2.x parameterised queries/ORM only; dynamic identifiers come from whitelists (sort fields, report groupings); verified by an injection test suite |
| Output | JSON only; no HTML rendering of user data on the server; the admin UI escapes via its framework; no server-side template injection surface |
| Mass assignment | Separate request schemas; server-computed fields (`to_amount`, `transaction_number`, `status`, balances) are never accepted from clients |
| CORS | Explicit origin allowlist; `*` is never combined with credentials |
| CSRF | Bearer-token API (no cookie auth for the API); if a browser session cookie is ever introduced, `SameSite=Strict` + double-submit tokens are mandatory |
| File uploads | Attachments (receipts/expenses) are limited by type/size, stored outside the web root with generated names, served only through an authenticated endpoint |
| Error leakage | The error envelope never exposes stack traces, SQL, or internal paths; `request_id` correlates with server logs |
| Business-logic abuse | Rate changes, reversals and manual adjustments are rate-limited and permission-gated; anomaly alerts (§9) surface unusual patterns |
| Dependency risk | `pip-audit`/`safety` and `npm audit` in CI; locked dependency versions; images rebuilt monthly |
| Idempotency as a control | Prevents duplicate-value attacks through retries (double crediting a customer's account) |

## 7. Database security

| Control | Implementation |
| --- | --- |
| Least privilege | `nexus_app` has DML on operational tables but **no `DELETE`** on ledgers, business documents and master data, and **no `UPDATE`** on `audit_logs`, `journal_lines`, `cash_movements` (verified by the invariant suite: 3/3 denials) |
| Append-only enforcement | Triggers (`nexus_forbid_mutation`) plus revoked grants — defence in depth against both application bugs and direct SQL |
| Immutability of posted money | Triggers freeze money/identity columns after posting; corrections are reversals (`NEXUS06`) |
| Audit chain | `prev_hash`/`chain_hash` with serialised inserts; `verify_audit_chain()` detects any silent edit or deletion; the route `GET /audit/verify` exposes it |
| Integrity invariants | Balanced journals, non-negative cash, valid state transitions, reversal binding — enforced at COMMIT, not only in code |
| Connection hygiene | Dedicated credentials per environment, `statement_timeout` and `idle_in_transaction_session_timeout` set, connection pooling with bounded size, no superuser for the application |
| Migration safety | Migrations run as `nexus_owner` from a controlled pipeline with a pre-flight backup; no DDL from the app role |
| Backups | Encrypted, access-controlled, and their restoration is itself an audited event with post-restore verification |

## 8. Deployment roles and key handling

| Secret | Where it lives | Who can read it |
| --- | --- | --- |
| `JWT_SECRET` / signing keys | Runtime secret store | API containers only |
| `JWT_REFRESH_SECRET` | Runtime secret store | API containers only |
| Database password | Runtime secret store | API/worker containers |
| Redis password | Runtime secret store | API/worker containers |
| Backup encryption key | Offline/escrow, outside the server | Owner + escrow procedure |
| Device data-encryption keys | Device keystore | Device only |

Rotation procedure, revocation and the emergency key-compromise runbook are executed via `scripts/` (Phase 12 deliverable) and recorded in the operational log.

## 9. Auditing, monitoring and detection

**Audited events (non-exhaustive):** login success/failure/lockout, refresh reuse, logout, device register/revoke, user/role/permission changes, rate creation, exchange create/cancel/reverse, cash open/in/out/adjustment/close, expense create/cancel, transfer create/approve/pay/cancel, expense and manual journal adjustments, sync push conflicts and resolutions, backup create/verify/restore, report exports, settings changes.

Every audit row records actor, device, IP, action, entity, before/after JSON, and joins the hash chain.

**Alerts (worker + monitoring):**

| Signal | Threshold | Why |
| --- | --- | --- |
| Failed logins per account | > 10 / hour | Credential stuffing |
| Refresh reuse detected | any | Token theft |
| Reversals by one user | > 3 / day | Concealment attempt |
| Rate changes outside business hours | any | Manipulation before a manual transaction |
| Manual adjustments (cash short/over) | > threshold / day / branch | Skimming |
| `verify_audit_chain()` invalid | any | Tampering (P1) |
| Balance-cache drift after rebuild | any | Defect or interference |
| Sync conflicts of type `ALLOCATION_EXCEEDED` | repeated | Device or policy problem |
| Backup failure | any | Recovery risk |

## 10. Regulatory boundary and data privacy (PART 65, PART 66)

* The system is a back-office and point-of-sale tool for **licensed** businesses. It intentionally provides **no** capability for money laundering, concealment of transactions, document forgery, deletion of financial records, KYC/AML circumvention, or unlicensed financial activity.
* Concretely refused by design: hard-deleting transactions or audit rows, editing posted amounts, back-dating or fabricating documents, hiding transactions from reports, disabling the audit trail, exporting data in a form designed to evade reporting.
* Regulatory features (AML reporting formats, CTR/STR thresholds, sanctions screening, KYC document storage) are **implemented only from the official rules of the target jurisdiction**, with legal review, as a scoped later phase — never improvised.
* Data minimisation (PART 65): customer data is limited to name, contact details and a short notes field plus `national_id_last4`; full identity documents are not stored in the MVP; customer records are role-restricted (`customer.view`), transmitted over TLS and protected at rest like the rest of the database.
* Retention: financial and audit records are kept for the statutory period (default 10 years); PII that is not required for financial history can be anonymised on a documented, audited request.

## 11. Required security verification (executed in Phase 12)

The complete matrix lives in `docs/security/TEST_PLAN.md`. Minimum before release:

| Area | Evidence required |
| --- | --- |
| Authentication | Valid/invalid login, expired token, rotated-then-reused refresh, revoked device, lockout |
| Authorization | Every permission-denied case from the matrix in `API_CONTRACT.md` §8 |
| Injection | SQLi payload suite against filters, sorts, search and report parameters |
| Rate limits | 429 triggers plus `Retry-After` correctness |
| Idempotency | Replay returns the stored response; key reuse with a different body is rejected |
| Ledger integrity | `tests/invariants/phase0_schema_invariants.sql` (already green) plus API-level equivalent |
| Audit | Hash chain valid; tamper detected; append-only enforced for the app role |
| Backup/restore | Restore into a scratch database, chain + balances verified |
| Dependency audit | Zero unpatched high/critical CVEs at release |
| Offline abuse | Allocation exhaustion, expired window, revoked device, tampered local DB |

## 12. Incident response (summary)

1. **Detect** — alert or report; capture `request_id`s, audit rows and logs.
2. **Contain** — revoke the affected device(s)/sessions (`POST /devices/{id}/revoke`, logout-all), rotate affected secrets.
3. **Preserve evidence** — export audit rows and `verify_audit_chain()` output *before* remediation; never delete.
4. **Eradicate & recover** — restore from a verified backup when data integrity is in doubt; re-verify the chain and balances after restore.
5. **Record** — write the incident report (timeline, root cause, actions, legal notifications) and add regression tests.
6. **Break-glass** — DB-level recovery access uses `nexus_owner` under two-person rule; every action under break-glass is expected to appear in the audit trail (the chain makes silent superuser edits detectable).

## 13. Traceability

| Master prompt | Section |
| --- | --- |
| PART 1 (auth stack) | §2 |
| PART 18 | §7 (audit chain) |
| PART 24, PART 25 | §2, §3 |
| PART 41 | §3 |
| PART 42 | §2–§9 |
| PART 65 | §4, §10 |
| PART 66 | §10 |
| PART 50 (Phase 0 output #7) | this document |
