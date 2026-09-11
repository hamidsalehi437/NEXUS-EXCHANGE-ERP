# NEXUS EXCHANGE ERP — Documentation Index

| Field | Value |
| --- | --- |
| Status | **Phase 2 READY FOR REVIEW** — the permanent phase table lives in [`PROJECT_STATUS.md`](PROJECT_STATUS.md) |
| Last updated | 2026-09-11 |
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

Supporting artifacts:

* [`../tests/invariants/phase0_schema_invariants.sql`](../tests/invariants/phase0_schema_invariants.sql) — the executable invariant suite (27 assertions, green).
* `../docs/user-manual/` — operator manuals (authored in Phase 13).
* `openapi.json` — exported by CI from the running application (Phase 1); never hand-edited.

## Phase status in one line

Phase 2 adds the real authentication and authorization layer on top of the Phase 1
foundation: login with Argon2id and device binding, DB-backed lockout, HS256 access tokens
re-validated against the database on every request, refresh rotation with reuse detection,
logout/session/device revocation, users/roles/permissions administration with anti-escalation
guards, audit attribution for every authentication event, and HTTP-surface rate limiting.
**No schema change was required.** `pytest tests` → **759 passed** (500 Phase 0/1 regression +
259 new Phase 2 tests), Ruff (`check` + `format --check`) and MyPy clean, both schema gates
MATCH, and the Phase 0 invariant suite green (52 assertions) on a freshly migrated database.
No business endpoint is implemented yet; master data starts in Phase 3, which has **not** been started.
The permanent, reviewable evidence for Phase 2 is [`phases/PHASE2_REPORT.md`](phases/PHASE2_REPORT.md).
