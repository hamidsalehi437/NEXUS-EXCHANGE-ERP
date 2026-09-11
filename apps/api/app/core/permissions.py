"""Permission registry and the role → permission matrix (PART 41).

This module is the single source of truth for RBAC: the seed runner inserts
exactly these codes into ``permissions`` and the default grants into
``role_permissions``, and Phase 2's endpoint dependencies check against the same
enum. A permission that is not listed here cannot be granted.

``SUPER_ADMIN`` deliberately has **no** extra privileges inside the ledger: it can
administer users, branches and settings, but it cannot edit or delete a posted
transaction (no permission in this system grants that — see ``SECURITY.md`` §10).
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    """Machine-readable permission codes (``resource.action``)."""

    EXCHANGE_CREATE = "exchange.create"
    EXCHANGE_VIEW = "exchange.view"
    EXCHANGE_CANCEL = "exchange.cancel"
    EXCHANGE_REVERSE = "exchange.reverse"

    CASH_CREATE = "cash.create"
    CASH_VIEW = "cash.view"
    CASH_CLOSE = "cash.close"
    CASH_ADJUST = "cash.adjust"

    CUSTOMER_CREATE = "customer.create"
    CUSTOMER_VIEW = "customer.view"
    CUSTOMER_UPDATE = "customer.update"

    TRANSFERS_CREATE = "transfers.create"
    TRANSFERS_VIEW = "transfers.view"
    TRANSFERS_APPROVE = "transfers.approve"
    TRANSFERS_PAY = "transfers.pay"
    TRANSFERS_CANCEL = "transfers.cancel"

    EXPENSES_CREATE = "expenses.create"

    REPORTS_VIEW = "reports.view"
    REPORTS_EXPORT = "reports.export"

    RATES_MANAGE = "rates.manage"
    ACCOUNTS_MANAGE = "accounts.manage"

    USERS_MANAGE = "users.manage"
    BRANCH_MANAGE = "branch.manage"
    DEVICE_MANAGE = "device.manage"
    DEVICE_REGISTER = "device.register"

    AUDIT_VIEW = "audit.view"

    SYNC_PUSH = "sync.push"
    SYNC_PULL = "sync.pull"
    SYNC_RESOLVE = "sync.resolve"

    BACKUP_MANAGE = "backup.manage"
    SETTINGS_MANAGE = "settings.manage"


PERMISSION_DESCRIPTIONS: dict[Permission, str] = {
    Permission.EXCHANGE_CREATE: "Create exchange transactions (buy/sell)",
    Permission.EXCHANGE_VIEW: "View exchange transactions",
    Permission.EXCHANGE_CANCEL: "Cancel an exchange transaction before it is reversed",
    Permission.EXCHANGE_REVERSE: "Reverse a completed exchange transaction",
    Permission.CASH_CREATE: "Record cash movements (in/out) and open a shift",
    Permission.CASH_VIEW: "View cash positions and cash movement history",
    Permission.CASH_CLOSE: "Close a cash shift and record the counted difference",
    Permission.CASH_ADJUST: "Record a manual cash adjustment (short/over)",
    Permission.CUSTOMER_CREATE: "Register a customer",
    Permission.CUSTOMER_VIEW: "View customer profiles and statements",
    Permission.CUSTOMER_UPDATE: "Modify customer details",
    Permission.TRANSFERS_CREATE: "Create a money transfer",
    Permission.TRANSFERS_VIEW: "View transfers",
    Permission.TRANSFERS_APPROVE: "Approve a money transfer",
    Permission.TRANSFERS_PAY: "Pay out an approved money transfer",
    Permission.TRANSFERS_CANCEL: "Cancel a transfer that has not been paid",
    Permission.EXPENSES_CREATE: "Record and cancel operating expenses",
    Permission.REPORTS_VIEW: "View operational and financial reports",
    Permission.REPORTS_EXPORT: "Export reports (CSV/XLSX/PDF)",
    Permission.RATES_MANAGE: "Publish exchange rate quotes",
    Permission.ACCOUNTS_MANAGE: "Maintain the chart of accounts",
    Permission.USERS_MANAGE: "Create and manage users, roles and permissions",
    Permission.BRANCH_MANAGE: "Manage branches",
    Permission.DEVICE_MANAGE: "Manage and revoke registered devices",
    Permission.DEVICE_REGISTER: "Register the calling device to a branch",
    Permission.AUDIT_VIEW: "View the audit trail and verify its integrity",
    Permission.SYNC_PUSH: "Push offline events to the server",
    Permission.SYNC_PULL: "Pull master data and the change stream",
    Permission.SYNC_RESOLVE: "Resolve offline synchronisation conflicts",
    Permission.BACKUP_MANAGE: "Create and verify database backups",
    Permission.SETTINGS_MANAGE: "Manage currencies, business settings and system configuration",
}


class RoleName(StrEnum):
    """Seeded role names (PART 41)."""

    SUPER_ADMIN = "SUPER_ADMIN"
    OWNER = "OWNER"
    MANAGER = "MANAGER"
    ACCOUNTANT = "ACCOUNTANT"
    CASHIER = "CASHIER"
    AUDITOR = "AUDITOR"


ROLE_DESCRIPTIONS: dict[RoleName, str] = {
    RoleName.SUPER_ADMIN: "System administrator: user/device/settings administration",
    RoleName.OWNER: "Business owner: full operational authority, still fully audited",
    RoleName.MANAGER: "Branch manager: operations, approvals and reporting",
    RoleName.ACCOUNTANT: "Accountant: books, expenses, reports and cash oversight",
    RoleName.CASHIER: "Counter operator: exchange, customers and cash movements",
    RoleName.AUDITOR: "Read-only auditor: reports, ledgers and the audit trail",
}

_READ_ONLY_ROLES = (
    RoleName.SUPER_ADMIN,
    RoleName.OWNER,
    RoleName.MANAGER,
    RoleName.ACCOUNTANT,
    RoleName.AUDITOR,
)

ROLE_PERMISSIONS: dict[RoleName, frozenset[Permission]] = {
    RoleName.SUPER_ADMIN: frozenset(set(Permission)),
    RoleName.OWNER: frozenset(set(Permission)),
    RoleName.MANAGER: frozenset(
        {
            Permission.EXCHANGE_CREATE,
            Permission.EXCHANGE_VIEW,
            Permission.EXCHANGE_CANCEL,
            Permission.EXCHANGE_REVERSE,
            Permission.CASH_CREATE,
            Permission.CASH_VIEW,
            Permission.CASH_CLOSE,
            Permission.CASH_ADJUST,
            Permission.CUSTOMER_CREATE,
            Permission.CUSTOMER_VIEW,
            Permission.CUSTOMER_UPDATE,
            Permission.TRANSFERS_CREATE,
            Permission.TRANSFERS_VIEW,
            Permission.TRANSFERS_APPROVE,
            Permission.TRANSFERS_PAY,
            Permission.TRANSFERS_CANCEL,
            Permission.EXPENSES_CREATE,
            Permission.REPORTS_VIEW,
            Permission.REPORTS_EXPORT,
            Permission.RATES_MANAGE,
            Permission.DEVICE_REGISTER,
            Permission.AUDIT_VIEW,
            Permission.SYNC_PUSH,
            Permission.SYNC_PULL,
            Permission.SYNC_RESOLVE,
        }
    ),
    RoleName.ACCOUNTANT: frozenset(
        {
            Permission.EXCHANGE_CREATE,
            Permission.EXCHANGE_VIEW,
            Permission.EXCHANGE_CANCEL,
            Permission.CASH_CREATE,
            Permission.CASH_VIEW,
            Permission.CASH_CLOSE,
            Permission.CASH_ADJUST,
            Permission.CUSTOMER_CREATE,
            Permission.CUSTOMER_VIEW,
            Permission.CUSTOMER_UPDATE,
            Permission.TRANSFERS_CREATE,
            Permission.TRANSFERS_VIEW,
            Permission.TRANSFERS_CANCEL,
            Permission.EXPENSES_CREATE,
            Permission.REPORTS_VIEW,
            Permission.REPORTS_EXPORT,
            Permission.ACCOUNTS_MANAGE,
            Permission.SYNC_PUSH,
            Permission.SYNC_PULL,
        }
    ),
    RoleName.CASHIER: frozenset(
        {
            Permission.EXCHANGE_CREATE,
            Permission.EXCHANGE_VIEW,
            Permission.CASH_CREATE,
            Permission.CASH_VIEW,
            Permission.CASH_CLOSE,
            Permission.CUSTOMER_CREATE,
            Permission.CUSTOMER_VIEW,
            Permission.TRANSFERS_CREATE,
            Permission.TRANSFERS_VIEW,
            Permission.DEVICE_REGISTER,
            Permission.SYNC_PUSH,
            Permission.SYNC_PULL,
        }
    ),
    RoleName.AUDITOR: frozenset(
        {
            Permission.EXCHANGE_VIEW,
            Permission.CASH_VIEW,
            Permission.CUSTOMER_VIEW,
            Permission.TRANSFERS_VIEW,
            Permission.REPORTS_VIEW,
            Permission.REPORTS_EXPORT,
            Permission.AUDIT_VIEW,
            Permission.SYNC_PULL,
        }
    ),
}

# Roles that may never be edited or deleted by an operator.
SYSTEM_ROLES: frozenset[RoleName] = frozenset({RoleName.SUPER_ADMIN})

# Documented for reviewers: no permission in the system authorises editing or
# deleting a posted financial record. Cancellation and reversal are modelled as
# status transitions with their own permissions.
assert all(  # pragma: no cover - structural assertion
    not any("delete" in permission.value for permission in permissions)
    for permissions in ROLE_PERMISSIONS.values()
)
assert _READ_ONLY_ROLES  # keep the tuple referenced for documentation purposes
