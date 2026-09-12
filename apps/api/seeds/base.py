"""Seed framework: idempotent, auditable, dry-runnable.

Every seed module declares rows keyed by a **natural key** (currency code, role
name, account code, permission code). :func:`sync_rows` compares each desired row
with the database and reports exactly what it did:

* ``inserted`` — the row did not exist;
* ``updated``  — the row existed but a managed attribute differed;
* ``unchanged`` — the row already matched (this is what makes a second run a no-op);
* ``removed``  — an existing row is no longer part of the seed (only used where the
  seed is documented as authoritative, e.g. the role → permission matrix).

Running the seeds is therefore safe at every deployment: it converges the database
to the desired state and never duplicates, never deletes financial history and
never touches an operator's business data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import CHAR, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.logging import get_logger
from app.models.audit import AuditLog
from app.models.base import Base

logger = get_logger(__name__)


@dataclass
class SeedCounts:
    """What a seed module changed."""

    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0

    @property
    def changed(self) -> int:
        return self.inserted + self.updated + self.removed

    def merge(self, other: SeedCounts) -> None:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.removed += other.removed

    def as_dict(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "removed": self.removed,
        }


@dataclass
class SeedContext:
    """Everything a seed module needs.

    ``dry_run`` reports the run mode to the modules and to the log, but it does **not**
    change what they do: a dry run performs the same writes as a real run and the
    caller rolls the transaction back. Skipping writes made ``--check`` diverge from a
    real run — later modules could not resolve references to rows an earlier module
    would have created, so a check on a fresh database failed instead of reporting
    what a real run would insert.
    """

    session: Session
    settings: Settings
    dry_run: bool = False
    counts: SeedCounts = field(default_factory=SeedCounts)

    def record_audit(
        self,
        *,
        action: str,
        entity_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Write one audit row for a seed run that changed something."""
        self.session.add(
            AuditLog(
                action=action,
                entity_type=entity_type,
                entity_id=None,
                new_data=details or {},
                old_data=None,
                request_id="seed",
            )
        )


def _normalise(current: Any, wanted: Any, *, fixed_width: bool) -> tuple[Any, Any]:
    """Make a stored value comparable with the desired value.

    PostgreSQL pads ``CHAR(n)`` values on storage (``'DEBIT'`` is returned as
    ``'DEBIT '``). That is a storage artifact of the fixed-width type, not a data
    difference, so trailing blanks are trimmed before comparing those columns —
    without which a re-seed would "update" every ``normal_balance`` row on every run
    and idempotency would be lost.
    """
    if fixed_width:
        if isinstance(current, str):
            current = current.rstrip()
        if isinstance(wanted, str):
            wanted = wanted.rstrip()
    return current, wanted


def sync_rows(
    context: SeedContext,
    model: type[Base],
    *,
    natural_key: Sequence[str],
    rows: Iterable[dict[str, Any]],
    managed_fields: Sequence[str],
) -> SeedCounts:
    """Insert or update rows so the database matches ``rows`` exactly.

    ``natural_key`` identifies an existing row; ``managed_fields`` are the
    attributes the seed owns (a difference in any of them counts as an update).
    Attributes outside ``managed_fields`` are never touched, so operator edits to
    other columns survive a re-seed.
    """
    counts = SeedCounts()
    table = model.__table__

    for desired in rows:
        key_filter = {field_name: desired[field_name] for field_name in natural_key}
        existing = context.session.execute(
            select(model).filter_by(**key_filter)
        ).scalar_one_or_none()

        if existing is None:
            counts.inserted += 1
            context.session.add(model(**desired))
            continue

        differences = {}
        for field_name in managed_fields:
            column = table.columns.get(field_name)
            fixed_width = column is not None and isinstance(column.type, CHAR)
            current, wanted = _normalise(
                getattr(existing, field_name), desired[field_name], fixed_width=fixed_width
            )
            if current != wanted:
                differences[field_name] = desired[field_name]
        if differences:
            counts.updated += 1
            for field_name, value in differences.items():
                setattr(existing, field_name, value)
        else:
            counts.unchanged += 1

    context.counts.merge(counts)
    return counts


def rows_summary(counts: SeedCounts, label: str) -> str:
    """One-line human summary, used by the CLI and by tests."""
    return (
        f"{label}: inserted={counts.inserted} updated={counts.updated} "
        f"unchanged={counts.unchanged} removed={counts.removed}"
    )
