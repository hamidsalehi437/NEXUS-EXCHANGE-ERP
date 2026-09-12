"""The RBAC matrix is data, and the data is the specification (PART 41).

These tests pin the role definitions so a later refactor cannot quietly widen a
role's authority — privilege escalation by pull request is still privilege
escalation.
"""

from __future__ import annotations

import re

import pytest

from app.core.permissions import (
    PERMISSION_DESCRIPTIONS,
    ROLE_DESCRIPTIONS,
    ROLE_PERMISSIONS,
    SYSTEM_ROLES,
    Permission,
    RoleName,
)

pytestmark = pytest.mark.unit

_RESOURCE_ACTION = re.compile(r"^[a-z_]+\.[a-z_]+$")

# Permissions that authorise writing to the financial books.
_WRITE_PERMISSIONS = frozenset(
    {
        Permission.EXCHANGE_CREATE,
        Permission.EXCHANGE_CANCEL,
        Permission.EXCHANGE_REVERSE,
        Permission.CASH_CREATE,
        Permission.CASH_CLOSE,
        Permission.CASH_ADJUST,
        Permission.TRANSFERS_CREATE,
        Permission.TRANSFERS_APPROVE,
        Permission.TRANSFERS_PAY,
        Permission.TRANSFERS_CANCEL,
        Permission.EXPENSES_CREATE,
    }
)

# The six roles PART 41 names, and nothing else.
_EXPECTED_ROLES = {
    "SUPER_ADMIN",
    "OWNER",
    "MANAGER",
    "ACCOUNTANT",
    "CASHIER",
    "AUDITOR",
}


class TestPermissionVocabulary:
    @pytest.mark.parametrize("permission", list(Permission))
    def test_permission_is_resource_dot_action(self, permission: Permission) -> None:
        assert _RESOURCE_ACTION.match(permission.value), permission.value

    @pytest.mark.parametrize("permission", list(Permission))
    def test_permission_is_documented(self, permission: Permission) -> None:
        assert PERMISSION_DESCRIPTIONS[permission].strip()

    def test_no_permission_authorises_deleting_history(self) -> None:
        # PART 22/25: financial history is never deleted, so no permission may say so.
        for permission in Permission:
            assert "delete" not in permission.value

    def test_core_permission_names_are_stable(self) -> None:
        # The API contract publishes these strings; they are part of the interface.
        assert Permission.EXCHANGE_CREATE.value == "exchange.create"
        assert Permission.EXCHANGE_CANCEL.value == "exchange.cancel"
        assert Permission.EXCHANGE_REVERSE.value == "exchange.reverse"
        assert Permission.CASH_CREATE.value == "cash.create"
        assert Permission.CASH_CLOSE.value == "cash.close"
        assert Permission.CUSTOMER_CREATE.value == "customer.create"
        assert Permission.CUSTOMER_VIEW.value == "customer.view"
        assert Permission.REPORTS_VIEW.value == "reports.view"
        assert Permission.REPORTS_EXPORT.value == "reports.export"
        assert Permission.RATES_MANAGE.value == "rates.manage"
        assert Permission.USERS_MANAGE.value == "users.manage"
        assert Permission.AUDIT_VIEW.value == "audit.view"
        assert Permission.BRANCH_MANAGE.value == "branch.manage"


class TestRoleDefinitions:
    def test_role_names_match_the_specification(self) -> None:
        assert {role.name for role in RoleName} == _EXPECTED_ROLES

    def test_every_role_has_permissions_and_a_description(self) -> None:
        assert set(ROLE_PERMISSIONS) == set(RoleName)
        assert set(ROLE_DESCRIPTIONS) == set(RoleName)

    @pytest.mark.parametrize("role", list(RoleName))
    def test_grants_are_real_permissions(self, role: RoleName) -> None:
        assert ROLE_PERMISSIONS[role]
        assert ROLE_PERMISSIONS[role] <= set(Permission)

    def test_super_admin_and_owner_hold_every_permission(self) -> None:
        assert ROLE_PERMISSIONS[RoleName.SUPER_ADMIN] == frozenset(Permission)
        assert ROLE_PERMISSIONS[RoleName.OWNER] == frozenset(Permission)

    def test_super_admin_is_a_system_role(self) -> None:
        assert RoleName.SUPER_ADMIN in SYSTEM_ROLES
        assert RoleName.OWNER not in SYSTEM_ROLES  # an owner account is operator-managed

    def test_roles_are_orderable_by_authority(self) -> None:
        # Sanity check that the matrix is not accidentally inverted.
        auditor = ROLE_PERMISSIONS[RoleName.AUDITOR]
        cashier = ROLE_PERMISSIONS[RoleName.CASHIER]
        manager = ROLE_PERMISSIONS[RoleName.MANAGER]
        assert auditor & _WRITE_PERMISSIONS == frozenset()
        assert cashier & _WRITE_PERMISSIONS
        assert manager <= ROLE_PERMISSIONS[RoleName.OWNER]
        assert manager & _WRITE_PERMISSIONS


class TestLeastPrivilege:
    def test_auditor_is_read_only(self) -> None:
        auditor = ROLE_PERMISSIONS[RoleName.AUDITOR]
        assert auditor & _WRITE_PERMISSIONS == frozenset()
        assert {
            Permission.REPORTS_VIEW,
            Permission.REPORTS_EXPORT,
            Permission.AUDIT_VIEW,
            Permission.EXCHANGE_VIEW,
            Permission.CASH_VIEW,
            Permission.CUSTOMER_VIEW,
            Permission.TRANSFERS_VIEW,
        } <= auditor

    def test_auditor_cannot_manage_users_devices_or_rates(self) -> None:
        auditor = ROLE_PERMISSIONS[RoleName.AUDITOR]
        assert (
            auditor
            & {
                Permission.USERS_MANAGE,
                Permission.DEVICE_MANAGE,
                Permission.RATES_MANAGE,
                Permission.BRANCH_MANAGE,
                Permission.SETTINGS_MANAGE,
                Permission.ACCOUNTS_MANAGE,
                Permission.BACKUP_MANAGE,
            }
            == frozenset()
        )

    def test_cashier_is_a_counter_operator(self) -> None:
        cashier = ROLE_PERMISSIONS[RoleName.CASHIER]
        assert {
            Permission.EXCHANGE_CREATE,
            Permission.CASH_CREATE,
            Permission.CUSTOMER_CREATE,
            Permission.TRANSFERS_CREATE,
            Permission.DEVICE_REGISTER,
        } <= cashier
        assert (
            cashier
            & {
                Permission.RATES_MANAGE,
                Permission.USERS_MANAGE,
                Permission.AUDIT_VIEW,
                Permission.BRANCH_MANAGE,
                Permission.ACCOUNTS_MANAGE,
                Permission.BACKUP_MANAGE,
                Permission.SETTINGS_MANAGE,
                Permission.CASH_ADJUST,
                Permission.EXCHANGE_REVERSE,
                Permission.EXPENSES_CREATE,
            }
            == frozenset()
        )

    def test_accountant_keeps_the_books_but_not_the_users(self) -> None:
        accountant = ROLE_PERMISSIONS[RoleName.ACCOUNTANT]
        assert {
            Permission.ACCOUNTS_MANAGE,
            Permission.EXPENSES_CREATE,
            Permission.REPORTS_EXPORT,
        } <= accountant
        # Reversal moves money, so it stays with a manager/owner.
        assert (
            accountant
            & {
                Permission.EXCHANGE_REVERSE,
                Permission.USERS_MANAGE,
                Permission.BRANCH_MANAGE,
                Permission.RATES_MANAGE,
                Permission.BACKUP_MANAGE,
            }
            == frozenset()
        )

    def test_only_managers_and_above_can_reverse(self) -> None:
        for role in (RoleName.CASHIER, RoleName.ACCOUNTANT, RoleName.AUDITOR):
            assert Permission.EXCHANGE_REVERSE not in ROLE_PERMISSIONS[role]

    def test_manager_controls_rates_devices_and_audit(self) -> None:
        manager = ROLE_PERMISSIONS[RoleName.MANAGER]
        assert {
            Permission.RATES_MANAGE,
            Permission.DEVICE_REGISTER,
            Permission.AUDIT_VIEW,
            Permission.EXCHANGE_REVERSE,
            Permission.TRANSFERS_APPROVE,
            Permission.TRANSFERS_PAY,
        } <= manager
        # Administrative authority still belongs to the owner/super admin.
        assert (
            manager
            & {
                Permission.USERS_MANAGE,
                Permission.BRANCH_MANAGE,
                Permission.SETTINGS_MANAGE,
                Permission.BACKUP_MANAGE,
                Permission.DEVICE_MANAGE,
            }
            == frozenset()
        )

    def test_only_owner_level_roles_manage_users_and_settings(self) -> None:
        for role in (
            RoleName.MANAGER,
            RoleName.ACCOUNTANT,
            RoleName.CASHIER,
            RoleName.AUDITOR,
        ):
            assert Permission.USERS_MANAGE not in ROLE_PERMISSIONS[role]
            assert Permission.SETTINGS_MANAGE not in ROLE_PERMISSIONS[role]
            assert Permission.BACKUP_MANAGE not in ROLE_PERMISSIONS[role]

    def test_offline_capable_roles_can_sync(self) -> None:
        for role in (
            RoleName.MANAGER,
            RoleName.ACCOUNTANT,
            RoleName.CASHIER,
            RoleName.AUDITOR,
        ):
            assert Permission.SYNC_PULL in ROLE_PERMISSIONS[role]
