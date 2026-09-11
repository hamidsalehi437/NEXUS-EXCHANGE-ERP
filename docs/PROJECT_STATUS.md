# NEXUS EXCHANGE ERP — Project Status

| Field | Value |
| --- | --- |
| Document ID | `PROJECT-STATUS-001` |
| Version | 1.0 |
| Status | **Living document — updated at the end of every phase** |
| Owner | Project (NEXUS EXCHANGE ERP) |
| Rule | **The repository is the permanent source of truth for project progress.** Chat/session output is never the record; a phase exists only when its report is committed here and this table says so. |

> **خلاصه فارسی** — این سند منبع دائمی حقیقت برای وضعیت پروژه است: جدول فازها، گزارش هر فاز و قانون «هیچ فازی بدون گزارش داخل مخزن بسته نمی‌شود».

---

## 1. Phase table

| Phase | Name | Status | Report |
| --- | --- | --- | --- |
| 0 | Architecture (documentation set, executable schema, invariant suite) | **APPROVED** | `docs/architecture/`, `docs/database/`, `docs/api/API_CONTRACT.md`, `docs/security/`, `docs/architecture/SYNC_DESIGN.md` |
| 1 | Project foundation (API skeleton, initial migration, five-service stack, seeds, CI, tooling) | **APPROVED** | `docs/PHASE1_REPORT.md` |
| 2 | Authentication, users, roles, permissions, devices | **READY FOR REVIEW** | `docs/phases/PHASE2_REPORT.md` |
| 3 | Core master data (currencies, branches, customers, accounts, rates) | NOT STARTED | — |
| 4 | Accounting engine | NOT STARTED | — |
| 5 | Exchange (buy/sell, commission, receipts, cancel, reverse) | NOT STARTED | — |
| 6 | Cash (open, in, out, adjustment, close) | NOT STARTED | — |
| 7 | Reports | NOT STARTED | — |
| 8 | Offline (Drift schema, outbox, sync engine, conflicts) | NOT STARTED | — |
| 9 | Multi-branch (scoping, device policies, branch balances) | NOT STARTED | — |
| 10 | Transfers | NOT STARTED | — |
| 11 | Printing (A4 PDF, thermal ESC/POS, Bluetooth) | NOT STARTED | — |
| 12 | Security hardening (OWASP review, dependency audit, restore drill) | NOT STARTED | — |
| 13 | Release (installers, migration bundle, manuals) | NOT STARTED | — |
| 14 | Reserved — not defined by the approved roadmap | NOT STARTED | — |
| 15 | Reserved — not defined by the approved roadmap | NOT STARTED | — |
| 16 | Reserved — not defined by the approved roadmap | NOT STARTED | — |
| 17 | Reserved — not defined by the approved roadmap | NOT STARTED | — |

Notes:

* Phases 0–13 are the phases defined by the approved roadmap (`docs/architecture/ROADMAP.md`). Rows 14–17 are reserved placeholders; extending the roadmap requires a documentation change and human approval, and this table is updated at the same time.
* Phase 2 is **READY FOR REVIEW**, not approved. Only the human reviewer moves a phase to APPROVED, and the approval is recorded in this table.
* Phase 2 commits: `2c53a78` (start, last Phase 1 commit) → `f3d4bb6` (implementation, 45 files) →
  `9605ea382d3715f96d784699de470b56497680a7` (report, review and CI fixes) → `58eada2` (hash pin) →
  `19468d2` (CI reference-path fix, defect 13) → `0a45154` (CI check-annotation reporter) →
  the commit that carries the defect-14 environment fix and this report's revision.
* Phase 3 has **not** started and must not start before Phase 2 is approved.

## 2. Reporting rule for every phase from Phase 2 onward

Each phase must, before it is declared complete:

1. commit a permanent report at `docs/phases/PHASEn_REPORT.md` containing: phase name and scope; starting commit; final commit; files created/modified; features implemented; database migrations; API endpoints; authentication/security architecture; RBAC architecture; token lifecycle; revocation/session/device management; password hashing; rate limiting; audit logging; security decisions; defects discovered and how each was fixed; complete test commands; exact test results; regression results; Ruff result; MyPy result; ORM/schema parity result; migration/schema verification result; Phase 0 invariant regression result; known limitations (including Docker, GitHub CI and Python-version limitations); remaining security risks; final acceptance checklist; READY FOR REVIEW statement; and an explicit statement that the next phase was not started;
2. update this table (phase status + report link);
3. keep the phase's code, tests, documentation and report in the phase's commit(s) and pull request;
4. put the same acceptance summary and test results in the pull-request description.
5. The report location convention started at Phase 2; the Phase 0 and Phase 1 reports keep their original, already-approved locations (`docs/`).

## 3. Environment limitations that affect every phase in this sandbox

| Limitation | Impact | Status |
| --- | --- | --- |
| No Docker CLI in the development sandbox | `docker compose up -d` and the containerised stack cannot be executed here; the CI compose job runs the real stack (build, health, migrate, seed, login through nginx) and its step conclusions are the evidence | Verified through CI steps |
| GitHub Actions are executed on GitHub, not in the sandbox | CI results are read back through the API (job/step conclusions, check-run annotations). **Job logs are not retrievable here** (`gh run view --log-failed` and the logs API return EOF), so a failing step is diagnosed by local reproduction plus a check annotation emitted by `scripts/ci_exec_report.sh` | Read back per phase |
| Python 3.11.2 (system interpreter) instead of 3.12 | Runtime differs from the target interpreter; dependency set and code target 3.12 | Known; documented per phase |
| No command-line `redis-cli`/`psql` on `PATH` | Verification uses the driver-level scripts (`scripts/schema_gate.py`, `tests/invariants/*.sql` executed through psycopg) | Worked around |

## 4. Change history of this document

| Version | Date | Change |
| --- | --- | --- |
| 1.0 | 2026-09-11 | Created for Phase 2 reporting: permanent phase table, reporting rule, environment limitations |
