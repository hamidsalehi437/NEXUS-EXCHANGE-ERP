# NEXUS EXCHANGE ERP — Project Status

| Field | Value |
| --- | --- |
| Document ID | `PROJECT-STATUS-001` |
| Version | 1.7 |
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
| 4 | Accounting engine | **APPROVED** | `docs/phases/PHASE4_REPORT.md` |
| 5 | Exchange (buy/sell, commission, receipts, cancel, reverse) | **READY FOR REVIEW** | `docs/phases/PHASE5_REPORT.md` |
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
* Phase 4 is **APPROVED** (the reviewer's decision, recorded here) with the implementation `21b6211` and its finalisation `f0910b5`. Phase 5 is **READY FOR REVIEW**, not approved. Only the human reviewer moves a phase to APPROVED, and the approval is recorded in this table.
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
* **Gate Review round.** The independent review of `da0c9ff` found two financial boundary holes:
  an invalid exchange *direction* could reach financial posting (a SELL delivering the functional
  currency posted a balanced entry for a deal that cannot exist), and the generic
  `create_journal_entry` door bypassed the inventory-position guard (a manual credit drove an
  inventory account to `-7.1428571429` units). Both are fixed — new `EXCHANGE_DIRECTION_INVALID`
  (422) refusal with `SAME_CURRENCY` / `FUNCTIONAL_CURRENCY_NOT_DELIVERABLE`, and
  `_assert_inventory_positions` run by the generic door under the production account lock — and
  pinned by two new suites (13 + 11 tests) plus three repaired pre-existing tests. Test count
  **1148 → 1173** (24 new tests + one parametrised case for the new error code). The fix commit
  is **`21b6211d7918ad8a16b27d3ca1dc54887d22b907`** (`21b6211`; 11 files, +1 986/−20 — 2
  production, 4 test, 5 documentation), pushed to branch
  `arena/01a090c5-nexus-exchange-erp`; its CI runs are push `34659645307` and pull request
  `34659648417`, both `completed`/**`success`** with all six jobs green, and the finalisation
  commit (documentation only) pins the hash at the top of the branch.
* Gate Review re-verification: `pytest tests -q` → **1173 passed** (0 failed, 0 skipped) in
  180.64 s and again in 172.22 s (two independent runs); `pytest -m accounting -q` → 210 passed;
  `ruff check .` clean, `ruff format --check .` 137 files, `mypy app seeds scripts` 89 files;
  fresh-database migration to head, ORM/schema gate MATCH (31 tables / 341 columns),
  reference/schema gate MATCH (72 indexes / 71 checks / 48 triggers / 23 routines / 5 views),
  reference DDL checksum unchanged; Phase 0 invariant suite ALL ASSERTIONS PASSED (53 PASS lines);
  seeds idempotent (89 / 88 / 88). No migration, no schema change, no constraint weakened, no
  test deleted, disabled or marked `xfail`. Phase 4 remains **READY FOR REVIEW** and Phase 5–17
  remain **NOT STARTED**.
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
* Phase 5 commits: `f0910b5` (start, last Phase 4 commit — the Phase 4 finalisation that pins the
  Gate Review fix `21b6211`) → **`40945b1`**
  (`40945b19039054e79147d93ff5ac46c846bb7070`, *feat(exchange): complete phase 5 exchange engine*;
  **24 files, +9 119/−46** — 10 production, 1 tooling, 10 test and 3 documentation files including
  `docs/phases/PHASE5_REPORT.md`), pushed to branch `arena/01a090c5-nexus-exchange-erp` → the
  **finalisation commit** (documentation only) that pins this hash and records the CI runs, at the
  top of the branch and visible in `git log`.
* Phase 5 verification on that tree: `pytest tests -q` → **1 301 passed** (0 failed, 0 skipped) in
  241.92 s; the five exchange suites together → **126 passed** in 49.43 s (unit rules 44, posting
  31, lifecycle 29, accounting integration 9, concurrency 13 — the concurrency suite also run three
  times in a row green); `ruff check .` clean and `ruff format --check app tests seeds scripts` →
  146 files already formatted; `mypy app seeds scripts` → no issues in **93** source files;
  fresh-database migration (`0001` → `0002_runtime_schema_revision`, head), ORM/schema gate MATCH
  (31 tables, 341 columns) and reference/schema gate MATCH (71 checks, 48 triggers, 23 routines,
  5 views); Phase 0 invariant suite → ALL ASSERTIONS PASSED (debit = credit = 1 770 000.0000000000,
  audit chain valid); seeds idempotent (89 / 88 / 88). Baseline before the phase: 1 173 passed in
  205.42 s.
* Phase 5 CI on `40945b1`: push run `34667533980` and pull-request run `34667536528`, both
  `completed`/**`success`** with all six jobs green (*Lint (ruff)*, *Type check (mypy)*,
  *Unit tests*, *OpenAPI document*, *Integration tests and schema gates*, *Compose stack (PART 44
  acceptance)*), no failing step in either run.
* Phase 5 needed **no migration**: the approved Phase 0 schema already carries the exchange table,
  its state-machine, immutability and reversal triggers, `next_document_number`, the cash-movement
  constraints and the position views, so `docs/database/schema.sql` and head revision
  `0002_runtime_schema_revision` are unchanged and no frozen migration was touched.
* Phase 5 defects: `D5-1` … `D5-6` in `docs/phases/PHASE5_REPORT.md` §23 — a validation-ordering
  defect in the new service, three harness defects (a shared-branch HTTP world that made drawer
  resolution ambiguous, a test that read the shared database instead of the engine, and
  under-funded legacy tests), a misread reverse contract, and a concurrency test that had encoded
  one race winner's shape (both shapes verified by a throwaway probe, then the suite run three
  times green). No assertion was weakened, no test deleted, skipped or xfailed.

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
| GitHub Actions are executed on GitHub, not in the sandbox | CI results are read back through the API (job/step conclusions, check-run annotations). **Job logs are not retrievable here** (`gh run view --log-failed` and the logs API return EOF), so a failing step is diagnosed by local reproduction plus a check annotation emitted by `scripts/ci_exec_report.sh` | **Green for Phase 2** — run `34628225139` (push) and `34628229827` (PR) on `19e0b0f`. **Green for Phase 3** — run `34645985626` (push) and `34645990882` (PR) on `db85e21`. **Green for Phase 4** — run `34656339711` (push) and `34656343155` (PR) on `da0c9ff`, and the Gate Review fix commit `21b6211` with run `34659645307` (push) and `34659648417` (PR): all `completed`/`success`, six jobs `success` in each run, every *Integration tests and schema gates* step `success` (the only non-success step is `Container logs on failure`, `skipped` by design). **Phase 5** — run `34667533980` (push) and `34667536528` (pull request) on `40945b1`: all `completed`/`success`, six jobs `success` in each run |
| Python 3.11.2 (system interpreter) instead of 3.12 | Runtime differs from the target interpreter; dependency set and code target 3.12 | Known; documented per phase |
| No command-line `redis-cli`/`psql` on `PATH` | Verification uses the driver-level scripts (`scripts/schema_gate.py`, `tests/invariants/*.sql` executed through psycopg) | Worked around |

## 4. Change history of this document

| Version | Date | Change |
| --- | --- | --- |
| 1.0 | 2026-09-11 | Created for Phase 2 reporting: permanent phase table, reporting rule, environment limitations |
| 1.1 | 2026-09-12 | Phase 2 marked **APPROVED**; Phase 3 marked **READY FOR REVIEW** with `docs/phases/PHASE3_REPORT.md`; Phase 3 commit chain and the "no migration needed" note added; Phase 4–17 remain NOT STARTED |
| 1.2 | 2026-09-12 | Phase 3 finalisation: the implementation commit pinned as `db85e21` with its exact diff stat and the full verification results (905 passed / ruff / mypy / migration / gates / Phase 0 invariants / seeds); the Phase 3 CI runs `34645985626` and `34645990882` recorded in the environment table. Phase 3 stays **READY FOR REVIEW** (not approved) and Phase 4–17 stay **NOT STARTED** |
| 1.5 | 2026-09-12 | Phase 4 **Gate Review round**: the two financial boundary holes found by the independent review of `da0c9ff` fixed (exchange direction, generic-journal inventory guard), two dedicated regression suites added (24 tests), three pre-existing tests repaired, the whole phase re-verified (1173 passed three times independently; every schema, invariant and seed gate re-run). Phase 4 stays **READY FOR REVIEW** and Phase 5–17 stay **NOT STARTED** |
| 1.8 | 2026-09-12 | Phase 5 finalisation: the implementation commit pinned as **`40945b1`** with its exact diff stat (24 files, +9 119/−46; 10 production, 1 tooling, 10 test, 3 documentation) and its two green CI runs (`34667533980` push, `34667536528` pull request). Phase 5 stays **READY FOR REVIEW** (not approved) and Phase 6–17 stay **NOT STARTED** |
| 1.7 | 2026-09-12 | Phase 5 implementation + report: Phase 5 marked **READY FOR REVIEW** with `docs/phases/PHASE5_REPORT.md`; Phase 4 marked **APPROVED** (the reviewer's decision). Phase 5 commit chain, exact diff stat, full verification results (1 301 passed / 126 exchange tests / ruff / mypy 93 files / migration / both schema gates / Phase 0 invariants / seeds), the no-migration note and the six phase defects recorded. Phase 5 stays **READY FOR REVIEW** (not approved) and Phase 6–17 stay **NOT STARTED** |
| 1.6 | 2026-09-12 | Gate Review finalisation: the fix commit pinned as **`21b6211`** with its exact diff stat and its two green CI runs (`34659645307` push, `34659648417` pull request). Phase 4 stays **READY FOR REVIEW** (not approved) and Phase 5–17 stay **NOT STARTED** |
| 1.4 | 2026-09-12 | Phase 4 finalisation: the implementation commit pinned as **`da0c9ff`** with its exact diff stat (33 files, +12 033/−61) and its two green CI runs (`34656339711` push, `34656343155` pull request) recorded in the environment table. Phase 4 stays **READY FOR REVIEW** (not approved) and Phase 5–17 stay **NOT STARTED** |
| 1.3 | 2026-09-12 | Phase 3 marked **APPROVED**; Phase 4 marked **READY FOR REVIEW** with `docs/phases/PHASE4_REPORT.md`; Phase 4 commit chain, exact diff stat, full verification results (1148 passed / ruff / mypy / migration / both schema gates / Phase 0 invariants / seeds) and the no-migration + rate-snapshot decision recorded. Phase 4 stays **READY FOR REVIEW** (not approved) and Phase 5–17 stay **NOT STARTED** |
