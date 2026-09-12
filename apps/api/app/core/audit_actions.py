"""Canonical audit action names (PART 18).

An audit action is a contract with the auditors: renaming one later means the history
stops being queryable with a single filter. They therefore live in one enum, are used
by services instead of string literals, and are asserted by tests so a typo cannot
silently create a second, almost-identical action name.
"""

from __future__ import annotations

from enum import StrEnum


class AuditAction(StrEnum):
    """Every action the API records today (feature phases add their own)."""

    # --- authentication ------------------------------------------------------
    AUTH_LOGIN_SUCCEEDED = "AUTH_LOGIN_SUCCEEDED"
    AUTH_LOGIN_FAILED = "AUTH_LOGIN_FAILED"
    AUTH_LOGIN_DENIED = "AUTH_LOGIN_DENIED"
    AUTH_LOCKOUT = "AUTH_LOCKOUT"
    AUTH_LOGOUT = "AUTH_LOGOUT"
    AUTH_REFRESH_ROTATED = "AUTH_REFRESH_ROTATED"
    AUTH_REFRESH_FAILED = "AUTH_REFRESH_FAILED"
    AUTH_PASSWORD_CHANGED = "AUTH_PASSWORD_CHANGED"  # noqa: S105
    AUTH_PASSWORD_CHANGE_FAILED = "AUTH_PASSWORD_CHANGE_FAILED"  # noqa: S105
    AUTH_CREDENTIAL_UPGRADED = "AUTH_CREDENTIAL_UPGRADED"

    # --- session and device security ----------------------------------------
    SECURITY_REFRESH_REUSE_DETECTED = "SECURITY_REFRESH_REUSE_DETECTED"
    SECURITY_SESSION_REVOKED = "SECURITY_SESSION_REVOKED"
    SECURITY_SESSION_REVOKED_BY_ADMIN = "SECURITY_SESSION_REVOKED_BY_ADMIN"
    DEVICE_REGISTERED = "DEVICE_REGISTERED"
    DEVICE_REGISTRATION_DENIED = "DEVICE_REGISTRATION_DENIED"
    DEVICE_REVOKED = "DEVICE_REVOKED"

    # --- user, role and permission administration ---------------------------
    USER_CREATED = "USER_CREATED"
    USER_UPDATED = "USER_UPDATED"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    USER_ROLES_CHANGED = "USER_ROLES_CHANGED"
    USER_PERMISSIONS_CHANGED = "USER_PERMISSIONS_CHANGED"
    ROLE_PERMISSIONS_CHANGED = "ROLE_PERMISSIONS_CHANGED"

    # --- administrative acts refused before they happen ---------------------
    SECURITY_PRIVILEGE_ESCALATION_BLOCKED = "SECURITY_PRIVILEGE_ESCALATION_BLOCKED"

    # --- core master data (Phase 3) -----------------------------------------
    CURRENCY_CREATED = "CURRENCY_CREATED"
    CURRENCY_UPDATED = "CURRENCY_UPDATED"
    BRANCH_CREATED = "BRANCH_CREATED"
    BRANCH_UPDATED = "BRANCH_UPDATED"
    CUSTOMER_CREATED = "CUSTOMER_CREATED"
    CUSTOMER_UPDATED = "CUSTOMER_UPDATED"
    CUSTOMER_DEACTIVATED = "CUSTOMER_DEACTIVATED"
    ACCOUNT_CREATED = "ACCOUNT_CREATED"
    ACCOUNT_UPDATED = "ACCOUNT_UPDATED"
    RATE_CREATED = "RATE_CREATED"

    # --- accounting engine (Phase 4) ----------------------------------------
    # A posted journal is the record of a movement of money; its audit row is what makes
    # the movement attributable, so posting and reversal are the only two ledger actions
    # that exist. There is deliberately no "JOURNAL_UPDATED" or "JOURNAL_DELETED".
    JOURNAL_POSTED = "JOURNAL_POSTED"
    JOURNAL_REVERSED = "JOURNAL_REVERSED"
    # A posting attempt refused before anything was written (missing authority, wrong
    # branch). The refusal is persisted in its own transaction, so it survives the
    # rollback of the request that caused it.
    LEDGER_POSTING_DENIED = "LEDGER_POSTING_DENIED"

    # --- exchange documents (Phase 5) ---------------------------------------
    # One row per state change of a business document. The journal rows that a document
    # produces carry their own JOURNAL_POSTED / JOURNAL_REVERSED actions, so an auditor can
    # follow "document posted -> entry posted" and back without a second ledger.
    EXCHANGE_CREATED = "EXCHANGE_CREATED"
    EXCHANGE_CANCELLED = "EXCHANGE_CANCELLED"
    EXCHANGE_REVERSED = "EXCHANGE_REVERSED"
    # An offline-origin event was re-delivered with a payload that contradicts the document
    # already stored under that ``client_event_id``: last-write-wins is forbidden for
    # financial records (PART 34), so the contradiction is recorded and refused.
    EXCHANGE_EVENT_CONFLICT = "EXCHANGE_EVENT_CONFLICT"
    # A document operation refused before anything was written (missing permission). Kept
    # separate from the ledger's own denial action so an auditor can tell a refusal to post
    # from a refusal to record the document that would have posted.
    EXCHANGE_ACCESS_DENIED = "EXCHANGE_ACCESS_DENIED"

    # --- cash control (Phase 6) ---------------------------------------------
    # A shift and the movements it contains are separate facts: opening and closing are
    # lifecycle acts on the session, recording a movement is the money moving, and a
    # reversal names the movement it undoes. A refused cash act carries its own action so
    # an operator's failed attempt to take money out is as visible as a successful one.
    CASH_SESSION_OPENED = "CASH_SESSION_OPENED"
    CASH_SESSION_CLOSED = "CASH_SESSION_CLOSED"
    CASH_MOVEMENT_RECORDED = "CASH_MOVEMENT_RECORDED"
    CASH_ADJUSTMENT_RECORDED = "CASH_ADJUSTMENT_RECORDED"
    CASH_MOVEMENT_REVERSED = "CASH_MOVEMENT_REVERSED"
    CASH_OPERATION_DENIED = "CASH_OPERATION_DENIED"


# Actions that must never be attributed to "nobody": a failed login for an unknown
# username has no user row, so the actor is null — every other entry names a user.
ACTIONS_ALLOWING_NULL_ACTOR = frozenset({AuditAction.AUTH_LOGIN_FAILED})
