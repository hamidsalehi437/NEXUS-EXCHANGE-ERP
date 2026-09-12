"""Domain exceptions and the SQLSTATE → error-code mapping.

The API contract (``docs/api/API_CONTRACT.md`` §3-§5) promises every failure is
reported as ``{"error": {"code", "message", "details"}}`` with a specific HTTP
status. This module is the single place where that promise is defined:

* :class:`ErrorCode` enumerates the contract's stable machine-readable codes.
* :class:`NexusError` and its subclasses carry the code, status and details.
* :func:`error_for_sqlstate` translates the database's custom SQLSTATEs
  (``docs/database/SCHEMA.md`` §4.1) into the same vocabulary, so a constraint
  violation raised inside PostgreSQL surfaces as a meaningful API error instead
  of an opaque 500.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """Stable error codes from the API contract."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"  # noqa: S105 - an error code, not a credential
    TOKEN_INVALID = "TOKEN_INVALID"  # noqa: S105 - an error code, not a credential
    TOKEN_REVOKED = "TOKEN_REVOKED"  # noqa: S105 - an error code, not a credential
    DEVICE_REVOKED = "DEVICE_REVOKED"
    DEVICE_UNKNOWN = "DEVICE_UNKNOWN"
    DEVICE_MISMATCH = "DEVICE_MISMATCH"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    # Additive v1 code (Phase 2): a deactivated account must be distinguishable from a
    # wrong password so support can answer "why can I not log in?" without guesswork.
    # It is returned only *after* the password verified, so it leaks nothing to an
    # attacker probing usernames.
    ACCOUNT_DISABLED = "ACCOUNT_DISABLED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    # Additive v1 code (Phase 3): the request is well formed and authorised, but the
    # system's own rules forbid the state it asks for — demoting the base currency,
    # deactivating the last active branch. Kept separate from VALIDATION_ERROR (the
    # request is not malformed) and from IMMUTABLE_FIELD (nothing frozen is being
    # edited), which is what makes the response actionable for an operator.
    CONFLICT = "CONFLICT"
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    DUPLICATE_RESOURCE = "DUPLICATE_RESOURCE"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    JOURNAL_UNBALANCED = "JOURNAL_UNBALANCED"
    INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"
    ALREADY_REVERSED = "ALREADY_REVERSED"
    REVERSAL_INVALID = "REVERSAL_INVALID"
    IMMUTABLE_FIELD = "IMMUTABLE_FIELD"
    APPEND_ONLY_VIOLATION = "APPEND_ONLY_VIOLATION"
    CASH_RECON_INCOMPLETE = "CASH_RECON_INCOMPLETE"
    CASH_COUNTER_ACCOUNT_REQUIRED = "CASH_COUNTER_ACCOUNT_REQUIRED"
    CASH_SESSION_NOT_OPEN = "CASH_SESSION_NOT_OPEN"
    CASH_SESSION_ALREADY_OPEN = "CASH_SESSION_ALREADY_OPEN"
    RATE_NOT_FOUND = "RATE_NOT_FOUND"
    # Additive v1 code (Phase 4): an exchange whose currencies cannot describe a deal —
    # delivering the functional currency, or naming one currency twice. Kept apart from
    # VALIDATION_ERROR so a caller can tell "this document's direction is impossible" from
    # "this field is malformed", and apart from INSUFFICIENT_BALANCE so a missing position
    # is never mistaken for an impossible direction (the two were confusable before the
    # Gate Review regression).
    EXCHANGE_DIRECTION_INVALID = "EXCHANGE_DIRECTION_INVALID"
    RATE_OUT_OF_TOLERANCE = "RATE_OUT_OF_TOLERANCE"
    # Additive v1 code (Phase 5): the client's own `to_amount` disagrees with the amount the
    # server computes from the applied rate (API_CONTRACT.md §9.3). The client never dictates
    # a computed total (PART 63), so the disagreement is named instead of being rounded away.
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    # Additive v1 code (Phase 5): a reversal document is the ledger's undo of a deal, not a
    # deal of its own. Undoing it would put the money back while the original stays marked
    # REVERSED, so the document's status and its economics would tell different stories. The
    # remedy is a new deal; the refusal says so.
    REVERSAL_NOT_UNDOABLE = "REVERSAL_NOT_UNDOABLE"
    CURRENCY_INACTIVE = "CURRENCY_INACTIVE"
    BRANCH_INACTIVE = "BRANCH_INACTIVE"
    CUSTOMER_INACTIVE = "CUSTOMER_INACTIVE"
    TRANSFER_STATE_INVALID = "TRANSFER_STATE_INVALID"
    TRANSFER_ALREADY_PAID = "TRANSFER_ALREADY_PAID"
    ALLOCATION_EXCEEDED = "ALLOCATION_EXCEEDED"
    ALLOCATION_EXPIRED = "ALLOCATION_EXPIRED"
    NUMBER_BLOCK_EXHAUSTED = "NUMBER_BLOCK_EXHAUSTED"
    BUSINESS_DATE_SKEW = "BUSINESS_DATE_SKEW"
    CURSOR_EXPIRED = "CURSOR_EXPIRED"
    SYNC_EVENT_REJECTED = "SYNC_EVENT_REJECTED"
    RATE_LIMITED = "RATE_LIMITED"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    DATA_INTEGRITY_ERROR = "DATA_INTEGRITY_ERROR"


class NexusError(Exception):
    """Base class for every expected (non-defect) application error."""

    code: ErrorCode = ErrorCode.INTERNAL_ERROR
    http_status: int = 500
    default_message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: ErrorCode | None = None,
        http_status: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.details: dict[str, Any] = details or {}
        # Response headers the error demands (``Retry-After`` on 429/503). The envelope
        # handler applies them, so every rejection carries the same shape *and* the same
        # protocol hints a client needs to back off correctly.
        self.headers: dict[str, str] = headers or {}
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        super().__init__(self.message)

    def to_payload(self, request_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "error": {
                "code": str(self.code),
                "message": self.message,
                "details": self.details,
            }
        }
        if request_id:
            payload["request_id"] = request_id
        return payload


# --- Validation and input ----------------------------------------------------
class ValidationError(NexusError):
    code = ErrorCode.VALIDATION_ERROR
    http_status = 422
    default_message = "The request could not be validated."


class ResourceNotFoundError(NexusError):
    code = ErrorCode.RESOURCE_NOT_FOUND
    http_status = 404
    default_message = "The requested resource does not exist."


class DuplicateResourceError(NexusError):
    code = ErrorCode.DUPLICATE_RESOURCE
    http_status = 409
    default_message = "The resource already exists."


class ConflictError(NexusError):
    """The request is valid but violates a system rule (409, additive v1 code)."""

    code = ErrorCode.CONFLICT
    http_status = 409
    default_message = "The request conflicts with the current state of the system."


class CurrencyInactiveError(NexusError):
    """A deactivated currency was referenced (API_CONTRACT §4: 422)."""

    code = ErrorCode.CURRENCY_INACTIVE
    http_status = 422
    default_message = "That currency is not active."


class BranchInactiveError(NexusError):
    """A deactivated branch was referenced (API_CONTRACT §4: 422)."""

    code = ErrorCode.BRANCH_INACTIVE
    http_status = 422
    default_message = "That branch is not active."


class CustomerInactiveError(NexusError):
    """A deactivated customer was named on a document (API_CONTRACT §9.3: 422).

    Deactivation is how a record leaves service (PART 25), so a trade that names one would
    be reopening a closed relationship. The document is refused with its own code rather
    than a generic validation error, because the counter can act on it: pick the right
    customer, or ask a manager to reactivate the record.
    """

    code = ErrorCode.CUSTOMER_INACTIVE
    http_status = 422
    default_message = "That customer is not active."


class RateNotFoundError(NexusError):
    """No quote is in force for the requested pair, branch and instant (422)."""

    code = ErrorCode.RATE_NOT_FOUND
    http_status = 422
    default_message = "No exchange rate is in force for that pair."


class DataIntegrityError(NexusError):
    code = ErrorCode.DATA_INTEGRITY_ERROR
    http_status = 422
    default_message = "The request violates a data integrity rule."


# --- Authentication and authorisation ---------------------------------------
class AuthenticationError(NexusError):
    """401 for every credential/token problem, with the specific contract code."""

    code = ErrorCode.TOKEN_INVALID
    http_status = 401
    default_message = "Authentication is required."


class InvalidCredentialsError(AuthenticationError):
    code = ErrorCode.INVALID_CREDENTIALS
    default_message = "The username or password is incorrect."


class TokenInvalidError(AuthenticationError):
    code = ErrorCode.TOKEN_INVALID
    default_message = "The token is not valid."


class TokenExpiredError(AuthenticationError):
    code = ErrorCode.TOKEN_EXPIRED
    default_message = "The token has expired."


class TokenRevokedError(AuthenticationError):
    code = ErrorCode.TOKEN_REVOKED
    default_message = "This session has been revoked. Sign in again."


class DeviceRevokedError(AuthenticationError):
    code = ErrorCode.DEVICE_REVOKED
    default_message = "This device has been revoked."


class DeviceUnknownError(AuthenticationError):
    code = ErrorCode.DEVICE_UNKNOWN
    default_message = "This device is not registered."


class DeviceMismatchError(AuthenticationError):
    code = ErrorCode.DEVICE_MISMATCH
    default_message = "The token does not belong to this device."


class PermissionDeniedError(NexusError):
    code = ErrorCode.PERMISSION_DENIED
    http_status = 403
    default_message = "You do not have permission to perform this action."


class AccountDisabledError(NexusError):
    """The credentials were correct, but the account is deactivated (PART 25)."""

    code = ErrorCode.ACCOUNT_DISABLED
    http_status = 401
    default_message = "This account is deactivated. Contact an administrator."


class AccountLockedError(NexusError):
    """Too many failed logins; the account is temporarily locked."""

    code = ErrorCode.ACCOUNT_LOCKED
    http_status = 423
    default_message = "The account is temporarily locked after repeated failed logins."


class RateLimitedError(NexusError):
    """A rate-limit bucket is exhausted (PART 42)."""

    code = ErrorCode.RATE_LIMITED
    http_status = 429
    default_message = "Too many requests. Retry later."


class ForbiddenScopeError(NexusError):
    code = ErrorCode.FORBIDDEN_SCOPE
    http_status = 403
    default_message = "This resource belongs to another branch."


# --- Financial invariants (mirrors the database SQLSTATEs) -------------------
class RateOutOfToleranceError(NexusError):
    """The supplied rate is too far from the quote in force (``rate_tolerance_bps``).

    A cashier may type a rate the house has not published, but only inside the tolerated
    band: beyond it the transaction would move money at a price nobody approved, which is
    exactly what the tolerance exists to catch. ``STRICT_RATE_TOLERANCE`` (Phase 3 publishes
    quotes; Phase 5 spends them) is what makes the band meaningful rather than cosmetic.
    """

    code = ErrorCode.RATE_OUT_OF_TOLERANCE
    http_status = 409
    default_message = "The supplied rate is outside the tolerance of the published quote."


class AmountMismatchError(NexusError):
    """The amount the client stated is not the amount the server computes.

    The server is authoritative for every computed total (PART 63): ``to_amount`` is derived
    from the applied rate and the commission, never accepted from the client. A client that
    sends its own expectation is telling the server what it *believes* the customer is owed,
    and a disagreement inside one minor unit is tolerated only because a hand-held device may
    round differently at the last decimal place.
    """

    code = ErrorCode.AMOUNT_MISMATCH
    http_status = 422
    default_message = "The stated amount does not match the amount computed from the rate."


class InsufficientBalanceError(NexusError):
    code = ErrorCode.INSUFFICIENT_BALANCE
    http_status = 409
    default_message = "Insufficient currency balance."


class JournalUnbalancedError(NexusError):
    """A defect in our own posting code; the database refused an unbalanced entry."""

    code = ErrorCode.JOURNAL_UNBALANCED
    http_status = 500
    default_message = "The journal entry is not balanced."


class InvalidStatusTransitionError(NexusError):
    code = ErrorCode.INVALID_STATUS_TRANSITION
    http_status = 409
    default_message = "The requested status change is not allowed."


class ReversalError(NexusError):
    code = ErrorCode.REVERSAL_INVALID
    http_status = 422
    default_message = "The reversal request is not valid."


class CashCounterAccountRequiredError(NexusError):
    """A cash movement without a counter account: cash cannot appear from nowhere.

    ``ACCOUNTING_MODEL.md`` §6.4 requires an explicit ``source_account_id`` /
    ``target_account_id`` for every ``IN``/``OUT`` movement, which is why this refusal has
    its own code instead of a generic validation error: the operator has to be told which
    field is missing.
    """

    code = ErrorCode.CASH_COUNTER_ACCOUNT_REQUIRED
    http_status = 422
    default_message = "A cash movement needs the counter account it moves value from or to."


class AlreadyReversedError(NexusError):
    code = ErrorCode.ALREADY_REVERSED
    http_status = 409
    default_message = "This document has already been reversed or cancelled."


class ReversalNotUndoableError(ConflictError):
    """An attempt to undo a reversal document (409).

    A reversing document exists to undo one deal, and the original's status — REVERSED —
    is the record that it did. Undoing the reversal (by cancelling or reversing it) would
    restore the money while leaving the original marked REVERSED: the status a reader
    trusts would contradict the positions an auditor measures. The domain therefore keeps
    the pair consistent and sends the operator to the honest remedy, a new exchange at the
    current quote, instead of letting a cancellation chain rewrite history.
    """

    code = ErrorCode.REVERSAL_NOT_UNDOABLE
    http_status = 409
    default_message = "A reversal document cannot itself be cancelled or reversed."


class ExchangeDirectionError(NexusError):
    """The currencies of an exchange document cannot express a deal.

    ``ACCOUNTING_MODEL.md`` §6.2/§6.3 define both exchange types with the **delivered**
    currency foreign: a BUY acquires foreign currency and pays the functional one, a SELL
    delivers foreign currency and receives the functional one. So the functional currency
    can never be the delivered side, and a deal cannot name the same currency on both
    sides. Neither case is a missing rate or an empty drawer, and saying so is the point of
    giving them their own code.
    """

    code = ErrorCode.EXCHANGE_DIRECTION_INVALID
    http_status = 422
    default_message = "The currencies of this exchange do not describe a valid deal."


class ImmutableFieldError(NexusError):
    code = ErrorCode.IMMUTABLE_FIELD
    http_status = 409
    default_message = "Posted financial fields cannot be modified."


class AppendOnlyViolationError(NexusError):
    code = ErrorCode.APPEND_ONLY_VIOLATION
    http_status = 403
    default_message = "This record is append-only and cannot be modified or deleted."


# --- Idempotency (PART 40) ----------------------------------------------------
class IdempotencyKeyReusedError(NexusError):
    """The key is already bound to a different request body.

    Answering with the stored response would silently drop the new request, and posting
    it would break the promise the key makes — so the caller is told to use a new key.
    """

    code = ErrorCode.IDEMPOTENCY_KEY_REUSED
    http_status = 409
    default_message = "This Idempotency-Key was already used with a different request."


class IdempotencyInProgressError(NexusError):
    """Another attempt with this key has not finished yet."""

    code = ErrorCode.IDEMPOTENCY_IN_PROGRESS
    http_status = 409
    default_message = "This Idempotency-Key is already being processed."


class IdempotencyKeyRequiredError(NexusError):
    """A money-moving endpoint was called without an ``Idempotency-Key``."""

    code = ErrorCode.IDEMPOTENCY_KEY_REQUIRED
    http_status = 400
    default_message = "This endpoint requires an Idempotency-Key header."


class CashReconciliationIncompleteError(NexusError):
    code = ErrorCode.CASH_RECON_INCOMPLETE
    http_status = 422
    default_message = "A cash reconciliation needs both the expected and the counted amount."


# --- Infrastructure ----------------------------------------------------------
class ServiceUnavailableError(NexusError):
    code = ErrorCode.SERVICE_UNAVAILABLE
    http_status = 503
    default_message = "A required service is temporarily unavailable."


# SQLSTATE → domain error. Custom codes are defined in docs/database/SCHEMA.md §4.1.
_SQLSTATE_MAP: dict[str, type[NexusError]] = {
    "NEX01": InsufficientBalanceError,  # cash/currency position would go negative
    "NEX02": JournalUnbalancedError,  # entry unbalanced, <2 lines, invalid line
    "NEX03": InvalidStatusTransitionError,  # state machine violation
    "NEX04": ReversalError,  # reversal target invalid / unbound
    "NEX05": CashReconciliationIncompleteError,
    "NEX06": ImmutableFieldError,  # posted field is frozen
    "P0001": AppendOnlyViolationError,  # append-only trigger fired
    "23505": DuplicateResourceError,  # unique_violation
    "23503": DataIntegrityError,  # foreign_key_violation
    "23514": DataIntegrityError,  # check_violation
    "23P01": DuplicateResourceError,  # exclusion_violation
    "40001": ServiceUnavailableError,  # serialization_failure (retryable)
    "40P01": ServiceUnavailableError,  # deadlock_detected (retryable)
}

# SQLSTATEs that mean "the same insertion already happened" for offline replay.
DUPLICATE_SQLSTATES = frozenset({"23505", "23P01"})


# The SQLSTATEs this application knows how to name. Callers that must *not* swallow an
# unrecognised database failure (a service translating its own transaction's refusal, for
# example) check membership here instead of guessing at the map's contents.
KNOWN_SQLSTATES: frozenset[str] = frozenset(_SQLSTATE_MAP)


def sqlstate_of(exc: BaseException) -> str | None:
    """The PostgreSQL SQLSTATE carried by a SQLAlchemy exception, when it has one.

    The attribute lives on the driver's exception (``sqlstate`` for asyncpg, ``pgcode``
    for psycopg), which SQLAlchemy exposes as ``orig`` and does not type — so it is
    reached defensively and reported as absent rather than guessed.
    """
    original = getattr(exc, "orig", None)
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    return str(sqlstate) if sqlstate else None


def constraint_name_of(exc: BaseException) -> str | None:
    """The name of the violated constraint, when the driver exposes it.

    asyncpg puts it on the exception itself; psycopg puts it in ``diag``. Both are
    checked because the same service runs under either driver (the API uses asyncpg, the
    Alembic path uses psycopg).
    """
    original = getattr(exc, "orig", None)
    direct = getattr(original, "constraint_name", None)
    if direct:
        return str(direct)
    diagnostic = getattr(original, "diag", None)
    named = getattr(diagnostic, "constraint_name", None)
    return str(named) if named else None


def error_for_sqlstate(
    sqlstate: str | None,
    message: str | None = None,
    *,
    details: dict[str, Any] | None = None,
) -> NexusError:
    """Return the domain error matching a PostgreSQL SQLSTATE.

    Unknown codes produce a :class:`DataIntegrityError` (422) rather than leaking a
    raw database message to the client.
    """
    error_class = _SQLSTATE_MAP.get(sqlstate or "", DataIntegrityError)
    return error_class(message, details=details)
