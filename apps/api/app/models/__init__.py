"""SQLAlchemy 2.x ORM models — one module per aggregate.

Importing this package registers every table in ``Base.metadata``. The mapping
mirrors ``docs/database/schema.sql`` column for column; the gate in
``tests/integration/test_schema_gate.py`` fails when they diverge.

Deliberate scope of the mapping (documented, not an omission):

* **Columns, primary keys, foreign keys and unique constraints** are modelled —
  they are what the application and its repositories rely on.
* **CHECK constraints, triggers, functions, views and indexes** belong to the
  database and are *not* duplicated here. They are protected by the invariant
  suite (``tests/invariants/phase0_schema_invariants.sql``) and by the
  schema-apply-vs-migrate structural gate, so there is exactly one source of
  truth for every financial invariant.
* **ORM relationships** are added in the phase that first needs them, so no
  speculative navigation code ships untested.
"""

from app.models.account import Account, AccountBalance
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.branch import Branch
from app.models.cash import CashMovement, CashSession, CashSessionLine
from app.models.change_log import ChangeLog
from app.models.currency import Currency
from app.models.customer import Customer
from app.models.device import Device
from app.models.exchange_rate import ExchangeRate
from app.models.exchange_transaction import ExchangeTransaction
from app.models.expense import Expense
from app.models.journal import JournalEntry, JournalLine
from app.models.role import Permission, Role, RolePermission
from app.models.security import IdempotencyKey, RefreshToken
from app.models.sequence import DocumentSequence
from app.models.sync import (
    AllocationPolicy,
    DeviceAllocation,
    SyncConflict,
    SyncCursor,
    SyncEvent,
)
from app.models.transfer import Transfer
from app.models.user import User, UserPermission, UserRole

__all__ = [
    "Account",
    "AccountBalance",
    "AllocationPolicy",
    "AuditLog",
    "Base",
    "Branch",
    "CashMovement",
    "CashSession",
    "CashSessionLine",
    "ChangeLog",
    "Currency",
    "Customer",
    "Device",
    "DeviceAllocation",
    "DocumentSequence",
    "ExchangeRate",
    "ExchangeTransaction",
    "Expense",
    "IdempotencyKey",
    "JournalEntry",
    "JournalLine",
    "Permission",
    "RefreshToken",
    "Role",
    "RolePermission",
    "SyncConflict",
    "SyncCursor",
    "SyncEvent",
    "Transfer",
    "User",
    "UserPermission",
    "UserRole",
]
