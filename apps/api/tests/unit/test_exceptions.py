"""The error vocabulary is part of the contract (PART 38, PART 39).

Clients branch on ``error.code``, so codes and HTTP statuses must not drift; and
the SQLSTATE map is what turns a database-level invariant refusal into a
meaningful API answer.
"""

from __future__ import annotations

import re

import pytest

from app.core.exceptions import (
    DUPLICATE_SQLSTATES,
    AlreadyReversedError,
    AppendOnlyViolationError,
    AuthenticationError,
    CashReconciliationIncompleteError,
    DataIntegrityError,
    DuplicateResourceError,
    ErrorCode,
    ImmutableFieldError,
    InsufficientBalanceError,
    InvalidStatusTransitionError,
    JournalUnbalancedError,
    NexusError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ReversalError,
    ServiceUnavailableError,
    ValidationError,
    error_for_sqlstate,
)

pytestmark = pytest.mark.unit

_UPPER_SNAKE = re.compile(r"^[A-Z][A-Z0-9_]*$")

# status code -> the error classes that must carry it
_STATUS_MATRIX = [
    (ValidationError, 422),
    (ResourceNotFoundError, 404),
    (DuplicateResourceError, 409),
    (DataIntegrityError, 422),
    (AuthenticationError, 401),
    (PermissionDeniedError, 403),
    (InsufficientBalanceError, 409),
    (JournalUnbalancedError, 500),
    (InvalidStatusTransitionError, 409),
    (ReversalError, 422),
    (AlreadyReversedError, 409),
    (ImmutableFieldError, 409),
    (AppendOnlyViolationError, 403),
    (CashReconciliationIncompleteError, 422),
    (ServiceUnavailableError, 503),
]

_SQLSTATE_MATRIX = [
    ("NEX01", InsufficientBalanceError),
    ("NEX02", JournalUnbalancedError),
    ("NEX03", InvalidStatusTransitionError),
    ("NEX04", ReversalError),
    ("NEX05", CashReconciliationIncompleteError),
    ("NEX06", ImmutableFieldError),
    ("P0001", AppendOnlyViolationError),
    ("23505", DuplicateResourceError),
    ("23P01", DuplicateResourceError),
    ("23503", DataIntegrityError),
    ("23514", DataIntegrityError),
    ("40001", ServiceUnavailableError),
    ("40P01", ServiceUnavailableError),
    # Unknown codes must degrade to a generic integrity error, never to a 500 or
    # to a leaked database message.
    (None, DataIntegrityError),
    ("ZZZZZ", DataIntegrityError),
]


class TestErrorCodes:
    @pytest.mark.parametrize("code", list(ErrorCode))
    def test_code_is_stable_upper_snake(self, code: ErrorCode) -> None:
        assert _UPPER_SNAKE.match(code.value), code.value

    def test_contract_codes_are_exact(self) -> None:
        assert ErrorCode.VALIDATION_ERROR == "VALIDATION_ERROR"
        assert ErrorCode.PERMISSION_DENIED == "PERMISSION_DENIED"
        assert ErrorCode.RESOURCE_NOT_FOUND == "RESOURCE_NOT_FOUND"
        assert ErrorCode.DUPLICATE_RESOURCE == "DUPLICATE_RESOURCE"
        assert ErrorCode.INSUFFICIENT_BALANCE == "INSUFFICIENT_BALANCE"
        assert ErrorCode.JOURNAL_UNBALANCED == "JOURNAL_UNBALANCED"
        assert ErrorCode.APPEND_ONLY_VIOLATION == "APPEND_ONLY_VIOLATION"
        assert ErrorCode.SERVICE_UNAVAILABLE == "SERVICE_UNAVAILABLE"
        assert ErrorCode.INTERNAL_ERROR == "INTERNAL_ERROR"

    def test_every_invariant_has_its_own_code(self) -> None:
        codes = [code.value for code in ErrorCode]
        assert len(codes) == len(set(codes))


class TestErrorPayload:
    def test_subclasses_carry_the_documented_status(self) -> None:
        for error_class, status in _STATUS_MATRIX:
            assert error_class.http_status == status, error_class.__name__
            assert issubclass(error_class, NexusError)

    def test_default_message_is_used_when_none_is_given(self) -> None:
        error = ValidationError()
        assert error.message == ValidationError.default_message
        assert error.details == {}

    def test_explicit_message_details_and_code_are_kept(self) -> None:
        error = InsufficientBalanceError(
            "AFN balance is too low",
            details={"currency": "AFN", "available": "10.0000000000"},
        )
        assert error.message == "AFN balance is too low"
        assert error.details == {"currency": "AFN", "available": "10.0000000000"}

    def test_status_and_code_can_be_overridden(self) -> None:
        error = NexusError("nope", code=ErrorCode.RATE_LIMITED, http_status=429)
        assert error.http_status == 429
        assert error.code == ErrorCode.RATE_LIMITED

    def test_to_payload_matches_the_api_contract(self) -> None:
        error = ValidationError("amount must be positive", details={"field": "amount"})
        payload = error.to_payload()
        assert payload == {
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "amount must be positive",
                "details": {"field": "amount"},
            }
        }

    def test_request_id_is_added_only_when_present(self) -> None:
        error = ValidationError("bad input")
        assert "request_id" not in error.to_payload()
        with_request_id = error.to_payload("req-123")
        assert with_request_id["request_id"] == "req-123"
        assert with_request_id["error"]["code"] == "VALIDATION_ERROR"

    def test_default_details_are_not_shared_between_instances(self) -> None:
        first, second = ValidationError(), ValidationError()
        first.details["field"] = "amount"
        assert second.details == {}


class TestSqlstateMapping:
    @pytest.mark.parametrize(("sqlstate", "expected"), _SQLSTATE_MATRIX)
    def test_sqlstate_maps_to_the_domain_error(
        self, sqlstate: str | None, expected: type[NexusError]
    ) -> None:
        error = error_for_sqlstate(sqlstate)
        assert type(error) is expected
        assert error.code == expected.code
        assert error.http_status == expected.http_status

    def test_message_and_details_provided_by_the_caller_are_kept(self) -> None:
        error = error_for_sqlstate(
            "NEX01", "cash position would go negative", details={"account": "1100"}
        )
        assert error.message == "cash position would go negative"
        assert error.details == {"account": "1100"}

    def test_sqlstate_strings_are_matched_exactly(self) -> None:
        # PostgreSQL reports the five-character code; a padded or lower-case value
        # must not silently match a different branch.
        assert type(error_for_sqlstate("nex01")) is DataIntegrityError
        assert type(error_for_sqlstate(" NEX01")) is DataIntegrityError

    def test_duplicate_sqlstates_are_the_replay_safe_ones(self) -> None:
        assert frozenset({"23505", "23P01"}) == DUPLICATE_SQLSTATES
        for sqlstate in DUPLICATE_SQLSTATES:
            assert issubclass(type(error_for_sqlstate(sqlstate)), NexusError)
