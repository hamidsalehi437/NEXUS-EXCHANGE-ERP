"""Let the runtime role read the applied schema revision.

Revision ID: 0002_runtime_schema_revision
Revises: 0001_initial_schema
Created: 2026-09-11 16:00:00 UTC

Why this migration exists
-------------------------

``GET /api/v1/health/ready`` and ``GET /api/v1/version`` report the applied schema
revision by executing ``SELECT version_num FROM alembic_version``. Alembic creates
``alembic_version`` itself, *before* it runs a migration script, so the schema-wide
``ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO
nexus_app`` in the frozen DDL (§20 of ``docs/database/schema.sql``) does not cover it:
default privileges only apply to objects created *after* the statement, and the table
already existed. It is not in that file's explicit ``GRANT`` list either, because the
list enumerates the schema's own tables.

Consequence in a deployment that uses the documented two-role model — the compose
stack and any production install — the runtime role ``nexus_api`` (inheriting
``nexus_app``) could not read the revision, so the readiness probe answered ``503``
with ``postgresql: unavailable (ProgrammingError)``. Any orchestrator or load balancer
that gates traffic on readiness would keep the API out of service while the database was
perfectly healthy. Single-role databases (everything connecting as the superuser, which
is how the test suite runs) could never show it; the compose acceptance job's
"Readiness through nginx" step is the first check that reached this path, and it found
the defect.

The fix is one grant on one table: no structural change, nothing added to the frozen
Phase 0 file, which stays byte-for-byte what was approved (checksummed and verified by
the ``0001`` migration).

Scope decision
--------------

Only ``nexus_app`` — the role the API actually runs as — receives the privilege.
``nexus_reader`` and ``nexus_auditor`` are reporting/BI identities: they read the
business data, and no requirement makes them need the migration revision of the
application, so they are deliberately left without it (least privilege).
"""

from __future__ import annotations

from alembic import op

revision = "0002_runtime_schema_revision"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None

# The runtime *group* role: nexus_api (the login role) inherits it, and so does the
# Celery worker, which runs with the same DATABASE_URL.
_RUNTIME_ROLE = "nexus_app"


def upgrade() -> None:
    """Grant the runtime role read access to the applied migration revision."""
    op.execute(f"GRANT SELECT ON alembic_version TO {_RUNTIME_ROLE}")


def downgrade() -> None:
    """Remove the grant (readiness then reports the database as unavailable again)."""
    op.execute(f"REVOKE SELECT ON alembic_version FROM {_RUNTIME_ROLE}")
