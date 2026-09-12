"""Cash-control rules that are decided without a database (Phase 6, unit level).

Three groups:

* the **tables** the module reads its authority from — which permission each operation needs
  (``API_CONTRACT.md`` §8) and which movement type is the mirror of which (§6.4). A test here
  is what keeps a future endpoint from quietly demanding a permission the contract does not
  grant, or reversing an ``IN`` into another ``IN``;
* the **money rules** (``_check_money``): a negative amount, a sub-cent amount, more precision
  than the ledger stores and a value ``NUMERIC(30,10)`` cannot hold are all refused before
  anything is posted — as ``Decimal`` decisions with no float anywhere;
* the **derived strings**: a shift's ``business_date`` in its branch's own timezone, and the
  fixed-point rendering of money in the payloads a client parses.

None of these need PostgreSQL: they are the decisions that must be right *before* a
transaction is opened, and a database-backed test would not make them any truer.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, cast

import pytest

from app.core.exceptions import ValidationError
from app.core.idempotency import canonical_request_hash, json_safe
from app.core.money import MAX_MONEY
from app.core.permissions import Permission
from app.models.currency import Currency
from app.services.cash_service import (
    MIRROR_MOVEMENT,
    OPERATION_PERMISSIONS,
    CashCount,
    CashMovementView,
    CashOpening,
    CashService,
    CashSessionView,
    CloseSessionRequest,
    OpenSessionRequest,
    money_sum_declared,
)

pytestmark = pytest.mark.unit


class _NullAccounting:
    """A stand-in for the ledger: these tests never reach it."""

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - defensive
        raise AssertionError(f"the unit rule under test should not call {name}")


def _service() -> CashService:
    """A cash service with no database, for the rules that are decided before one is opened."""
    return CashService(
        database=cast(Any, None),
        settings=cast(Any, None),
        accounting=cast(Any, _NullAccounting()),
    )


def _currency(*, code: str = "AFN", decimals: int = 2) -> Currency:
    return Currency(
        id=uuid.uuid4(),
        code=code,
        name=f"Test {code}",
        decimal_places=decimals,
        is_base=decimals == 2,
        is_active=True,
        is_tradable=True,
    )


# ------------------------------------------------------------------- the two tables
def test_operation_permissions_match_the_contract() -> None:
    """§8 of the contract, in one table: the door each cash operation needs."""
    assert OPERATION_PERMISSIONS == {
        "open": Permission.CASH_CREATE,
        "in": Permission.CASH_CREATE,
        "out": Permission.CASH_CREATE,
        "adjustment": Permission.CASH_ADJUST,
        "close": Permission.CASH_CLOSE,
        "reverse": Permission.CASH_ADJUST,
        "view": Permission.CASH_VIEW,
    }


def test_the_mirror_of_every_movement_is_an_opposite_movement() -> None:
    """§6.4's directions, and the one type that mirrors itself with a flipped sign."""
    assert MIRROR_MOVEMENT["IN"] == "OUT"
    assert MIRROR_MOVEMENT["OUT"] == "IN"
    assert MIRROR_MOVEMENT["ADJUSTMENT"] == "ADJUSTMENT"
    # An opening is money entering the drawer, so undoing it takes money out — the same
    # direction an expense's correction has, because an expense took money out.
    assert MIRROR_MOVEMENT["OPENING"] == "OUT"
    assert MIRROR_MOVEMENT["EXPENSE"] == "IN"
    assert set(MIRROR_MOVEMENT) == {"IN", "OUT", "ADJUSTMENT", "OPENING", "EXPENSE"}


# -------------------------------------------------------------------- money rules
def _code(refusal: pytest.ExceptionInfo[ValidationError]) -> str:
    """The machine-readable reason the API returns in ``error.details.fields``."""
    return refusal.value.details["fields"][0]["code"]


def test_a_negative_cash_amount_is_refused() -> None:
    """A quantity is never negative: the direction says which way the money went."""
    with pytest.raises(ValidationError) as refusal:
        _service()._check_money(Decimal("-1"), currency=_currency(), field="amount")
    assert _code(refusal) == "negative"
    assert refusal.value.details["fields"][0]["field"] == "amount"


def test_an_amount_with_more_precision_than_the_ledger_stores_is_refused() -> None:
    """``NUMERIC(30,10)`` is the storage contract; silently rounding money is not allowed."""
    with pytest.raises(ValidationError) as refusal:
        _service()._check_money(
            Decimal("1.00000000001"), currency=_currency(decimals=10), field="amount"
        )
    # The ledger's own validator answers first and answers once (``not_exact_scale``); a
    # second, weaker check behind it would be dead code, so there is only one.
    assert _code(refusal) == "not_exact_scale"


def test_an_amount_below_the_currency_smallest_unit_is_refused() -> None:
    """A currency with two decimals cannot hold half a cent."""
    with pytest.raises(ValidationError) as refusal:
        _service()._check_money(Decimal("0.005"), currency=_currency(decimals=2), field="counted")
    assert _code(refusal) == "below_smallest_unit"
    assert refusal.value.details["currency_code"] == "AFN"
    assert refusal.value.details["decimal_places"] == 2


def test_an_amount_the_storage_cannot_hold_is_refused() -> None:
    """``MAX_MONEY`` is not representable in ``NUMERIC(30,10)``: the edge refuses it first."""
    with pytest.raises(ValidationError) as refusal:
        _service()._check_money(MAX_MONEY, currency=_currency(decimals=10), field="amount")
    assert _code(refusal) == "over_maximum"


def test_an_amount_wider_than_the_money_context_is_a_validation_error() -> None:
    """Thirty-one digits cannot be quantized at all — a bad request, never a server defect.

    Phase 6 defect: the scale check raised ``decimal.InvalidOperation`` for such a value, so
    the API reported a 500 instead of the 422 the caller's input deserves.
    """
    with pytest.raises(ValidationError) as refusal:
        _service()._check_money(Decimal("1" * 31), currency=_currency(decimals=10), field="amount")
    assert _code(refusal) == "not_exact_scale"


def test_an_ordinary_amount_passes_through_unchanged() -> None:
    """The happy path is exact: no rounding, no scale change, ``Decimal`` end to end."""
    checked = _service()._check_money(
        Decimal("1500.25"), currency=_currency(decimals=2), field="amount"
    )
    assert checked == Decimal("1500.25")
    assert isinstance(checked, Decimal)


def test_a_currency_without_decimal_places_still_takes_whole_units() -> None:
    """A zero-decimal currency (the seed models IRR that way) takes integers only."""
    assert _service()._check_money(
        Decimal("1000"), currency=_currency(code="IRR", decimals=0), field="amount"
    ) == Decimal("1000")
    with pytest.raises(ValidationError):
        _service()._check_money(
            Decimal("1000.5"), currency=_currency(code="IRR", decimals=0), field="amount"
        )


# ------------------------------------------------------------------ list validation
def test_a_currency_declared_twice_at_open_is_refused() -> None:
    """Two counts of one currency is an ambiguity, not a sum."""
    currency_id = uuid.uuid4()
    with pytest.raises(ValidationError) as refusal:
        _service()._validate_openings(
            [
                CashOpening(currency_id=currency_id, amount=Decimal("100")),
                CashOpening(currency_id=currency_id, amount=Decimal("200")),
            ]
        )
    assert _code(refusal) == "duplicate"


def test_a_currency_counted_twice_at_close_is_refused() -> None:
    currency_id = uuid.uuid4()
    with pytest.raises(ValidationError) as refusal:
        _service()._validate_counts(
            [
                CashCount(currency_id=currency_id, amount=Decimal("100")),
                CashCount(currency_id=currency_id, amount=Decimal("100")),
            ]
        )
    assert _code(refusal) == "duplicate"


def test_an_opening_rate_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        _service()._validate_openings(
            [
                CashOpening(
                    currency_id=uuid.uuid4(), amount=Decimal("10"), exchange_rate=Decimal("-1")
                )
            ]
        )


# ------------------------------------------------------------------ derived strings
def test_expected_amount_is_the_declared_opening_plus_the_shift_net() -> None:
    """The definition of "expected" (§9.4), in ``Decimal``, with the sign intact."""
    assert money_sum_declared(Decimal("1000"), Decimal("250")) == Decimal("1250")
    assert money_sum_declared(Decimal("1000"), Decimal("-250")) == Decimal("750")
    assert money_sum_declared(Decimal("0"), Decimal("0")) == Decimal("0")
    # A shift that only paid out: the expectation follows the money down.
    assert money_sum_declared(Decimal("500"), Decimal("-500")) == Decimal("0")


def test_the_business_date_is_the_branch_local_day() -> None:
    """Kabul runs at +04:30: an evening UTC instant is already tomorrow there."""
    row = {
        "opened_at": dt.datetime(2026, 9, 11, 20, 30, tzinfo=dt.UTC),
        "branch_timezone": "Asia/Kabul",
    }
    payload = CashSessionView(row={**row, **_session_row_defaults(row)}).to_payload()
    assert payload["business_date"] == "2026-09-12"
    # ... and the same instant is still the 11th in UTC, which is why the branch's own zone
    # is what a shift reports.
    utc = CashSessionView(
        row={**row, **_session_row_defaults(row | {"branch_timezone": "UTC"})}
    ).to_payload()
    assert utc["business_date"] == "2026-09-11"


def test_an_unknown_branch_timezone_falls_back_to_utc() -> None:
    """A bad zone is a data problem; it must not make a read fail."""
    row = {
        "opened_at": dt.datetime(2026, 9, 11, 20, 30, tzinfo=dt.UTC),
        "branch_timezone": "Mars/Olympus_Mons",
    }
    payload = CashSessionView(row={**row, **_session_row_defaults(row)}).to_payload()
    assert payload["business_date"] == "2026-09-11"


def test_money_leaves_as_a_fixed_point_string() -> None:
    """The wire format is the stored scale, never an exponent and never a float."""
    view = CashMovementView(
        row={
            "id": uuid.uuid4(),
            "branch_id": uuid.uuid4(),
            "branch_code": "T1",
            "account_id": uuid.uuid4(),
            "account_code": "1000",
            "currency_id": uuid.uuid4(),
            "currency_code": "AFN",
            "movement_type": "IN",
            "amount": Decimal("3E-10"),
            "signed_amount": Decimal("1500"),
            "adjustment_sign": None,
            "reference_type": "CASH_MOVEMENT",
            "reference_id": uuid.uuid4(),
            "description": None,
            "cash_session_id": None,
            "session_status": None,
            "device_id": None,
            "journal_entry_id": None,
            "client_event_id": None,
            "created_by": None,
            "created_by_username": None,
            "created_at": dt.datetime(2026, 9, 12, 8, 0, tzinfo=dt.UTC),
        }
    )
    payload = view.to_payload()
    assert payload["amount"] == "0.0000000003"
    assert payload["signed_amount"] == "1500.0000000000"
    assert isinstance(payload["amount"], str)


def test_has_variance_only_when_a_line_actually_differs() -> None:
    """A zero difference is a reconciled shift, not a variance."""
    line = {
        "id": uuid.uuid4(),
        "currency_id": uuid.uuid4(),
        "currency_code": "AFN",
        "currency_name": "Afghani",
        "currency_decimal_places": 2,
        "opening_declared": Decimal("100"),
        "expected_amount": Decimal("100"),
        "counted_amount": Decimal("100"),
        "difference": Decimal("0"),
    }
    session_row = _session_row_defaults()
    assert CashSessionView(row=session_row, lines=(line,)).to_payload()["has_variance"] is False
    off = {**line, "counted_amount": Decimal("99"), "difference": Decimal("-1")}
    assert CashSessionView(row=session_row, lines=(off,)).to_payload()["has_variance"] is True
    # A line of an *open* shift has no difference yet, which is not a variance either.
    open_line = {**line, "expected_amount": None, "counted_amount": None, "difference": None}
    assert (
        CashSessionView(row=session_row, lines=(open_line,)).to_payload()["has_variance"] is False
    )


# ------------------------------------------------------------------- fingerprints
def test_an_opening_fingerprint_ignores_the_order_of_the_currencies() -> None:
    """A client that lists the same balances differently is asking the same question."""
    first, second = uuid.uuid4(), uuid.uuid4()
    left = OpenSessionRequest(
        branch_id=uuid.uuid4(),
        openings=[
            CashOpening(currency_id=first, amount=Decimal("100")),
            CashOpening(currency_id=second, amount=Decimal("200")),
        ],
    )
    right = OpenSessionRequest(
        branch_id=left.branch_id,
        openings=list(reversed(left.openings)),
    )
    assert canonical_request_hash(left.fingerprint()) == canonical_request_hash(right.fingerprint())
    # ... and a different amount is a different request, down to the last stored digit.
    changed = OpenSessionRequest(
        branch_id=left.branch_id,
        openings=[
            CashOpening(currency_id=first, amount=Decimal("100.0000000001")),
            CashOpening(currency_id=second, amount=Decimal("200")),
        ],
    )
    assert canonical_request_hash(left.fingerprint()) != canonical_request_hash(
        changed.fingerprint()
    )


def test_a_close_fingerprint_covers_every_counted_currency() -> None:
    """Two closes of one shift that counted differently are different requests."""
    session_id = uuid.uuid4()
    first, second = uuid.uuid4(), uuid.uuid4()
    left = CloseSessionRequest(
        session_id=session_id,
        counted=[
            CashCount(currency_id=first, amount=Decimal("10")),
            CashCount(currency_id=second, amount=Decimal("20")),
        ],
    )
    same = CloseSessionRequest(session_id=session_id, counted=list(reversed(left.counted)))
    assert canonical_request_hash(left.fingerprint()) == canonical_request_hash(same.fingerprint())
    other = CloseSessionRequest(
        session_id=session_id,
        counted=[
            CashCount(currency_id=first, amount=Decimal("10")),
            CashCount(currency_id=second, amount=Decimal("20.01")),
        ],
    )
    assert canonical_request_hash(left.fingerprint()) != canonical_request_hash(other.fingerprint())


def _session_row_defaults(row: dict[str, Any] | None = None) -> dict[str, Any]:
    """The session-header columns a payload needs, so a unit test can build one cheaply."""
    opening = dt.datetime(2026, 9, 11, 20, 30, tzinfo=dt.UTC)
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "branch_id": uuid.uuid4(),
        "branch_code": "T1",
        "branch_name": "Test branch",
        "branch_timezone": "UTC",
        "device_id": None,
        "device_uuid": None,
        "device_name": None,
        "status": "OPEN",
        "opened_by": uuid.uuid4(),
        "opened_by_username": "cashier",
        "opened_at": opening,
        "closed_by": None,
        "closed_by_username": None,
        "closed_at": None,
        "notes": None,
    }
    if row is not None:
        defaults.update(row)
    return defaults


class TestTheIdempotencyRecordIsTheWireAnswer:
    """PART 40: the recorded answer is what the caller received, to the character.

    A replay is served *from this record*, so any difference between the record and the wire
    becomes a difference between the first call and its retry — on money endpoints that is
    the one comparison a client is entitled to make.
    """

    def test_a_moment_is_recorded_the_way_the_api_renders_it(self) -> None:
        moment = dt.datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=dt.UTC)
        assert json_safe({"at": moment}) == {"at": "2026-01-02T03:04:05.678901Z"}

    def test_a_non_utc_offset_keeps_its_offset(self) -> None:
        kabul = dt.timezone(dt.timedelta(hours=4, minutes=30))
        moment = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=kabul)
        assert json_safe({"at": moment}) == {"at": "2026-01-02T03:04:05+04:30"}

    def test_money_and_identifiers_stay_exact_strings(self) -> None:
        identifier = uuid.uuid4()
        stored = json_safe({"amount": Decimal("1000.0000000000"), "id": identifier})
        assert stored == {"amount": "1000.0000000000", "id": str(identifier)}

    def test_a_float_is_refused_rather_than_recorded(self) -> None:
        with pytest.raises(ValidationError):
            json_safe({"amount": 1000.5})
