# NEXUS EXCHANGE ERP — Project Status

| Field | Value |
| --- | --- |
| Document ID | `PROJECT-STATUS-001` |
| Version | 1.4 |
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
| 2 | Authentication, users, roles, permissions, devices | **APPROVED** | `docs/phases/PHASE2_REPORT.md` |
| 3 | Core master data (currencies, branches, customers, accounts, rates) | **APPROVED** | `docs/phases/PHASE3_REPORT.md` |
| 4 | Accounting engine | **READY FOR REVIEW** | `docs/phases/PHASE4_REPORT.md` |
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
* Phase 1, Phase 2 and Phase 3 are **APPROVED** (the reviewer's decision, recorded here).
* Phase 4 is **READY FOR REVIEW**, not approved. Only the human reviewer moves a phase to APPROVED, and the approval is recorded in this table.
* Phase 2 commits: `2c53a78` (start, last Phase 1 commit) → `f3d4bb6` (implementation, 45 files) →
  `9605ea382d3715f96d784699de470b56497680a7` (report, review and CI fixes) → `58eada2` (hash pin) →
  `19468d2` (CI reference-path fix, defect 13) → `0a45154` (CI check-annotation reporter) →
  `149bd6e` (environment-comment defect 14) → `2e8896a` (readiness probe, defect 15) →
  `19e0b0f` (**finalization**: defect 16 fix — the grant-only migration
  `0002_runtime_schema_revision` — plus the revision of `docs/phases/PHASE2_REPORT.md` that
  carries the accepted results) → the documentation commit that pins these hashes (top of the
  branch; visible in `git log`).
* Phase 2 adds one grant-only migration (`0002_runtime_schema_revision`, decision `D-21` in
  `docs/database/SCHEMA.md`) and no structural schema change: the approved Phase 0 schema,
  its invariants and the frozen reference DDL are unchanged.
* Phase 3 commits: `208a3cc` (start, last Phase 2 commit) → **`db85e21`**
  (`db85e212d2219d73a08f4ee26b2889e57b076f5f`, implementation + report; 33 files, +6 887/−22 —
  18 production, 10 test, 4 documentation and 1 `pyproject.toml`) → the finalisation commit
  (documentation only) that pins this hash and records the CI runs, at the top of the branch
  and visible in `git log`.
* Phase 3 verification on the pinned commit `db85e21`: `pytest tests -q` → **905 passed**
  (0 failed, 0 skipped) in 123.00 s; `ruff check .` → clean, `ruff format --check .` → 119 files
  already formatted; `mypy app seeds scripts` → no issues in 82 source files; fresh-database
  migration (`0001` → `0002_runtime_schema_revision`, head), ORM/schema and schema/schema gates
  MATCH (31 tables, 341 columns, 72 indexes, 71 checks, 48 triggers, 23 routines, 5 views);
  Phase 0 invariant suite on a fresh migrated database → ALL ASSERTIONS PASSED (53 PASS lines);
  seeds idempotent (89 / 88 / 88).
* Phase 3 needed **no migration**: the approved Phase 0 schema already contained every table,
  constraint, index and function these entities use, so `docs/database/schema.sql` and the head
  revision `0002_runtime_schema_revision` are unchanged.
* Phase 3 exit criteria: a manager can create a currency, branch, customer and rate (with the
  approved RBAC split made explicit in the report, Limitation L-8); every rate publication
  produces an audit row; `currencies.code` has no edit path (schema, service and `NEX06`
  trigger). Full regression: **905 passed**.
* Phase 4 commits: `3a985fd` (start, last Phase 3 commit — *docs(phase3): pin the implementation
  commit and record the green CI runs*) → **`da0c9ff`**
  (`da0c9ff261123fd41b2806052adb1f82ecd21f33`, *feat(accounting): complete phase 4 double-entry engine*; 33 files, +12 033/−61 — 16 production/tooling, 11 test and 6 documentation
  files, including `docs/phases/PHASE4_REPORT.md`), pushed to branch
  `arena/01a090c5-nexus-exchange-erp` → the **finalisation commit** (documentation only) that pins
  this hash and records the CI runs, at the top of the branch and visible in `git log`.
* Phase 4 CI on `da0c9ff`: push run `34656339711` and pull-request run `34656343155`, both
  `completed`/**`success`** with all six jobs green and every step of *Integration tests and
  schema gates* green (`pytest (integration)`, migration on a clean database, both schema gates,
  seed idempotency — `inserted=89` then `unchanged=88` twice — and the Phase 0 invariant suite).
* Phase 4 verification on the implementation commit: `pytest tests -q` → **1148 passed**
  (0 failed, 0 skipped) in 165.04 s and again in 172.38 s on the committed tree; `ruff check .` → clean, `ruff format --check .` → 135 files
  already formatted; `mypy app seeds scripts` → no issues in 89 source files; fresh-database
  migration (`0001` → `0002_runtime_schema_revision`, head), ORM/schema gate MATCH (31 tables,
  341 columns) and schema/schema gate MATCH (31 tables, 72 indexes, 71 checks, 48 triggers,
  23 routines, 5 views); Phase 0 invariant suite on a fresh migrated database → ALL ASSERTIONS
  PASSED (53 PASS lines); seeds idempotent (89 / 88 / 88); `docs/database/schema.sql`
  sha256 `37f7bc3c…` unchanged against `CHECKSUMS.txt`.
* Phase 4 needed **no migration**: every table, constraint, index, grant and function the double-entry
  engine uses already exists in the approved Phase 0 schema, so `docs/database/schema.sql` and head
  revision `0002_runtime_schema_revision` are unchanged. The rate-snapshot question raised by the
  phase brief was reviewed explicitly (decision `D-4-1`, `docs/architecture/ACCOUNTING_MODEL.md`
  §14) and answered **no schema change**, with regression tests proving that re-pricing a quote
  cannot move posted history.
* Phase 4 defects: `D4-1` … `D4-9` (product and test defects found while building the engine),
  plus `D4-10` and `D4-11` (two order-dependent harness defects the full regressions exposed — a
  currency-code collision and a global `?limit=500` page lookup). Each was fixed in code or in the
  harness; no assertion was weakened and no test was skipped or xfailed
  (`docs/phases/PHASE4_REPORT.md` §24). Complete regression of Phases 0–3: **1148 passed, 0 failed**.
* Phase 5 (exchange routes) has **not** started: no Phase 5 code, route, migration or test exists,
  and nothing in Phase 4 presumes it beyond the `AccountingService` surface the roadmap assigns to
  Phase 4.

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
| No Docker CLI in the development sandbox | `docker compose up -d` and the containerised stack cannot be executed here; the CI compose job runs the real stack (build, health, migrate, seeds, development administrator, login and readiness through nginx, worker registration) and its step conclusions are the evidence | **All 15 compose steps green** in run `34628225139` |
| GitHub Actions are executed on GitHub, not in the sandbox | CI results are read back through the API (job/step conclusions, check-run annotations). **Job logs are not retrievable here** (`gh run view --log-failed` and the logs API return EOF), so a failing step is diagnosed by local reproduction plus a check annotation emitted by `scripts/ci_exec_report.sh` | **Green for Phase 2** — run `34628225139` (push) and `34628229827` (PR) on `19e0b0f`. **Green for Phase 3** — run `34645985626` (push) and `34645990882` (PR) on `db85e21`. **Green for Phase 4** — run `34656339711` (push) and `34656343155` (PR) on `da0c9ff`: both `completed`/`success`, all six jobs `success` in each run, every *Integration tests and schema gates* step `success` (the only non-success step is `Container logs on failure`, `skipped` by design) |
| Python 3.11.2 (system interpreter) instead of 3.12 | Runtime differs from the target interpreter; dependency set and code target 3.12 | Known; documented per phase |
| No command-line `redis-cli`/`psql` on `PATH` | Verification uses the driver-level scripts (`scripts/schema_gate.py`, `tests/invariants/*.sql` executed through psycopg) | Worked around |

## 4. Change history of this document

| Version | Date | Change |
| --- | --- | --- |
| 1.0 | 2026-09-11 | Created for Phase 2 reporting: permanent phase table, reporting rule, environment limitations |
| 1.1 | 2026-09-12 | Phase 2 marked **APPROVED**; Phase 3 marked **READY FOR REVIEW** with `docs/phases/PHASE3_REPORT.md`; Phase 3 commit chain and the "no migration needed" note added; Phase 4–17 remain NOT STARTED |
| 1.2 | 2026-09-12 | Phase 3 finalisation: the implementation commit pinned as `db85e21` with its exact diff stat and the full verification results (905 passed / ruff / mypy / migration / gates / Phase 0 invariants / seeds); the Phase 3 CI runs `34645985626` and `34645990882` recorded in the environment table. Phase 3 stays **READY FOR REVIEW** (not approved) and Phase 4–17 stay **NOT STARTED** |
| 1.4 | 2026-09-12 | Phase 4 finalisation: the implementation commit pinned as **`da0c9ff`** with its exact diff stat (33 files, +12 033/−61) and its two green CI runs (`34656339711` push, `34656343155` pull request) recorded in the environment table. Phase 4 stays **READY FOR REVIEW** (not approved) and Phase 5–17 stay **NOT STARTED** |
| 1.3 | 2026-09-12 | Phase 3 marked **APPROVED**; Phase 4 marked **READY FOR REVIEW** with `docs/phases/PHASE4_REPORT.md`; Phase 4 commit chain, exact diff stat, full verification results (1148 passed / ruff / mypy / migration / both schema gates / Phase 0 invariants / seeds) and the no-migration + rate-snapshot decision recorded. Phase 4 stays **READY FOR REVIEW** (not approved) and Phase 5–17 stay **NOT STARTED** |
