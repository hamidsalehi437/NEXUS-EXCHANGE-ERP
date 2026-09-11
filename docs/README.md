# NEXUS EXCHANGE ERP — Documentation Index

| Field | Value |
| --- | --- |
| Status | **Phase 0 delivered — pending approval** |
| Last updated | 2026-09-11 |
| Rule | Each document has an ID, a version and an owner; documents change in the same PR as the behaviour they describe |

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

Supporting artifacts:

* [`../tests/invariants/phase0_schema_invariants.sql`](../tests/invariants/phase0_schema_invariants.sql) — the executable invariant suite (27 assertions, green).
* `../docs/user-manual/` — operator manuals (authored in Phase 13).
* `openapi.json` — exported by CI from the running application (Phase 1); never hand-edited.

## Phase 0 status in one line

Architecture, ERD, schema, API contract, folder structure, sync design, security architecture, accounting model and roadmap are delivered; the schema and its invariant suite were **executed** against PostgreSQL 16.2 (schema exit 0, 27/27 assertions pass, 31 tables / 5 views / 23 functions / 58 triggers / 413 constraints). No application code, migration or configuration was written — implementation starts only after approval (PART 50, PART 61).

## Conventions used across these documents

* **MUST / must** — a requirement enforced by code, database or CI. **Should** — a strong default that needs a written reason to bend.
* Every claim about money is tied to a database object (`consultant` names are exact: `ct_journal_lines_balanced_*`, `ux_exchange_transactions_reversed_once`, …).
* Deviations from the master prompt are numbered (`D-nn` in `SCHEMA.md`, `ADR-nn` in `ARCHITECTURE.md`) with rationale and trade-off.
* Persian summaries appear at the top of each document; the normative text is English so that code, tests and documents use one vocabulary.
