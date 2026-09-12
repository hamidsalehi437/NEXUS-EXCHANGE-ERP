"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Created: ${create_date}

Schema changes are reviewed SQL (docs/database/SCHEMA.md §10). Describe the change,
state which invariant it protects or affects, and note whether it is additive or
destructive. Never delete financial or audit history (PART 22).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "raise NotImplementedError('Write the reviewed SQL here.')"}


def downgrade() -> None:
    ${downgrades if downgrades else "raise NotImplementedError('Write the reviewed SQL here.')"}
