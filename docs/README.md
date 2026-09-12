# NEXUS EXCHANGE ERP — Documentation Index

| Field | Value |
| --- | --- |
| Status | **Phase 0/1/2 APPROVED · Phase 3 READY FOR REVIEW** — the permanent phase table lives in [`PROJECT_STATUS.md`](PROJECT_STATUS.md) |
| Last updated | 2026-09-12 |
| Rule | Each document has an ID, a version and an owner; documents change in the same PR as the behaviour they describe. Every phase must also commit its report under `docs/phases/` and update `PROJECT_STATUS.md` — the repository, not a chat session, is the record of progress |

## Reading order

| # | Document | ID | What it answers |
| --- | --- | --- | --- |
| 1 | [`architecture/ARCHITECTURE.md`](architecture/ARCHITECTURE.md) | `ARCH-SYS-001` | What the system is, how it is deployed, why these decisions (ADRs) |
| 2 | [`architecture/FOLDER_STRUCTURE.md`](architecture/FOLDER_STRUCTURE.md) | `ARCH-FS-001` | Where every kind of file lives and the dependency rules |
| 3 | [`architecture/ACCOUNTING_MODEL.md`](architecture/ACCOUNTING_MODEL.md) | `ARCH-ACC-001` | Exactly how money is posted, and which invariants are enforced where |
| 4 | [`database/ERD.md`](database/ERD.md) | `DB-ERD-001` | Entities, relationships, index strategy, volume assumptions |
| 5 | [`database/SCHEMA.md`](database/SCHEMA.md) | `DB-SCHEMA-001-DOC` | Type/constraint policy, verification evidence, deviations, migration strategy |
| 6 | [`database/schema.sql`](database/schema.sql) | `DB-SCHEMA-001` | The normative, executable DDL (verified on PostgreSQL 16.2) |
| 7 | [`api/API_CONTRACT.md`](api/API_CONTRACT.md) | `API-CONTRACT-001` | Every endpoint, error code, permission and status semantics |
| 8 | [`architecture/SYNC_DESIGN.md`](architecture/SYNC_DESIGN.md) | `ARCH-SYNC-001` | Offline-first behaviour: allocation, idempotency, conflicts, pull feed |
| 9 | [`security/SECURITY.md`](security/SECURITY.md) | `SEC-ARCH-001` | Threat model, authn/z, encryption, hardening, regulatory boundary |
| 10 | [`security/TEST_PLAN.md`](security/TEST_PLAN.md) | `SEC-TEST-001` | Test catalogue, security matrix, CI gates, Phase 0 evidence |
| 11 | [`architecture/ROADMAP.md`](architecture/ROADMAP.md) | `ARCH-ROAD-001` | Phases 1–13 with exit criteria, risks, MVP mapping |
| 12 | [`DEPLOYMENT.md`](DEPLOYMENT.md) | `OPS-DEPLOY-001` | How to start, migrate, seed, verify and operate the five-service stack |
| 13 | [`PHASE1_REPORT.md`](PHASE1_REPORT.md) | `PHASE1-REPORT-001` | Phase 1 verification report: PASS / FAIL / NOT VERIFIED per acceptance criterion |
| 14 | [`PROJECT_STATUS.md`](PROJECT_STATUS.md) | `PROJECT-STATUS-001` | **Permanent project status**: phase table, reporting rule, environment limitations |
| 15 | [`phases/PHASE2_REPORT.md`](phases/PHASE2_REPORT.md) | `PHASE2-REPORT-001` | Phase 2 report: authentication, RBAC, users, devices — files, tests, evidence, limitations |
| 16 | [`phases/PHASE3_REPORT.md`](phases/PHASE3_REPORT.md) | `PHASE3-REPORT-001` | Phase 3 report: currencies, branches, customers, accounts, exchange rates — scope, RBAC, audit, defects, exact evidence, limitations |

Supporting artifacts:

* [`../tests/invariants/phase0_schema_invariants.sql`](../tests/invariants/phase0_schema_invariants.sql) — the executable invariant suite (52 assertions / 53 PASS lines, green on a freshly migrated database in every phase so far).
* `../docs/user-manual/` — operator manuals (authored in Phase 13).
* `openapi.json` — exported by CI from the running application (Phase 1); never hand-edited.

## Phase status in one line

Phases 0, 1 and 2 are **APPROVED**. Phase 3 (core master data) is **READY FOR REVIEW**:
currencies, branches, customers (with server-issued codes), the chart of accounts and
exchange rates are now managed through `/api/v1` — **21 operations across 17 paths** — with
deny-by-default RBAC, one audit row per write in the same transaction, server-derived
`normal_balance`, `currencies.code` immutable three ways (request schema, service, frozen
`NEX06` trigger), append-only quotes resolved through the Phase 0 `resolve_exchange_rate`
function (branch quote wins, global fallback), and soft deletion everywhere
(`DELETE` = `is_active = false`).
**No migration was needed**: the approved Phase 0 schema already covered every table,
index, constraint, trigger and function these entities use, so `docs/database/schema.sql` and
the head revision `0002_runtime_schema_revision` are unchanged.
`pytest tests -q` → **905 passed** (769 Phase 0–2 regression + 136 new Phase 3 tests) in
123.00 s on the Phase 3 commit `db85e21`, Ruff (`check` + `format --check`) and MyPy clean,
both schema gates MATCH (31 tables / 341 columns), the seed idempotency check unchanged
(89 / 88 / 88) and the Phase 0 invariant suite green (53 PASS lines) on a freshly migrated
database. CI ran twice on that commit — run `34645985626` (push) and run `34645990882`
(pull request) — with **all six jobs green** in both, including the compose acceptance job
that builds the real five-service stack and logs in through nginx.
No financial transaction, exchange, cash or ledger-posting logic exists yet: those belong to
Phases 4–7 and Phase 4 has **NOT started**.
The permanent, reviewable evidence is [`phases/PHASE3_REPORT.md`](phases/PHASE3_REPORT.md)
(Phase 2: [`phases/PHASE2_REPORT.md`](phases/PHASE2_REPORT.md)).
