"""Pure exchange rules — no database, no HTTP (PART 49, PART 62, PART 63).

The Phase 5 brief asks for a *document engine* whose arithmetic is decided once and whose
decisions are all decidable from committed data. Everything in this file is one of those
decisions, and none of them needs PostgreSQL:

* the deal arithmetic (``compute_exchange_amounts``) — the formula, the single rounding step,
  the currency's smallest unit, and every way a deal must be refused;
* the branch business date, because a document number rolls over on the counter's day;
* the receipt's movement view, which has to stay a faithful projection of the movements;
* the request schema, which is the last line that keeps a float out of the ledger (PART 62)
  and the client's ``to_amount`` from becoming the stored one (PART 63).

The database half of these rules — the ledger identity, the guards, the races — lives in
``tests/integration/test_exchange_accounting.py`` and the posting/lifecycle suites. These
tests are the first line: a broken rule here is reported in milliseconds.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.exceptions import DataIntegrityError, ValidationError
from app.core.money import (
    MAX_MONEY,
    MONEY_SCALE,
    divide_money,
    format_decimal,
    has_money_scale,
    money_sum,
    multiply_money,
    quantize_money,
)
from app.schemas.exchange import (
    RECEIPT_VERSION,
    ExchangeCreateRequest,
    ExchangeReasonRequest,
)
from app.services.accounting_service import (
    EXCHANGE_TRANSACTION_TYPES,
    ExchangeComputation,
    compute_exchange_amounts,
)
from app.services.exchange_service import _receipt_lines, business_date

# The house's quotes in these tests: it buys USD at 70 AFN and sells USD at 71 AFN.
USD_BUY, USD_SELL = Decimal("70"), Decimal("71")


def refusal_code(error: BaseException) -> str:
    """The field code of a refused exchange rule (``details["fields"][0]["code"]``)."""
    details = getattr(error, "details", None) or {}
    fields = details.get("fields") or []
    assert fields, f"refusal carried no field code: {error!r}"
    return str(fields[0]["code"])


_BUY = {
    "transaction_type": "BUY",
    "from_amount": Decimal("1000"),
    "exchange_rate": USD_BUY,
    "commission": Decimal("500"),
    "from_decimal_places": 2,
    "to_decimal_places": 2,
}


def compute(**overrides: object) -> ExchangeComputation:
    return compute_exchange_amounts(**{**_BUY, **overrides})  # type: ignore[arg-type]


def expect_refusal(**overrides: object) -> str:
    """The field code of a deal the arithmetic must refuse."""
    with pytest.raises(ValidationError) as refusal:
        compute(**overrides)
    return refusal_code(refusal.value)


class TestDealArithmetic:
    """``ACCOUNTING_MODEL.md`` §6.2/§6.3, implemented once in ``compute_exchange_amounts``."""

    def test_a_buy_withholds_the_commission_from_the_payout(self) -> None:
        deal = compute()
        assert deal.gross_amount == Decimal("70000.00")
        assert deal.settlement_amount == Decimal("69500.00")
        # The identity a receipt must be able to show: gross = payout + fee.
        assert deal.settlement_amount + deal.commission == deal.gross_amount

    def test_a_sell_settles_the_full_receipt_and_keeps_the_fee_inside_it(self) -> None:
        deal = compute(transaction_type="SELL", exchange_rate=USD_SELL, commission=Decimal("200"))
        assert deal.gross_amount == Decimal("71000.00")
        # The customer pays the whole receipt; the fee is recognized inside it (§6.3).
        assert deal.settlement_amount == deal.gross_amount
        assert deal.settlement_amount - deal.commission == Decimal("70800.00")

    def test_the_gross_is_the_amount_times_the_rate_at_the_to_currencys_scale(self) -> None:
        deal = compute(
            from_amount=Decimal("12.34"),
            exchange_rate=Decimal("0.5432"),
            commission=Decimal("0"),
        )
        assert deal.gross_amount == multiply_money(Decimal("12.34"), Decimal("0.5432"), scale=2)
        assert deal.gross_amount == Decimal("6.70")
        assert deal.gross_amount.as_tuple().exponent == -2

    def test_a_currency_with_more_places_is_settled_in_its_own_unit(self) -> None:
        deal = compute(
            from_amount=Decimal("100"),
            exchange_rate=Decimal("1.2345678901"),
            commission=Decimal("0.0000000001"),
            to_decimal_places=10,
        )
        assert deal.gross_amount == Decimal("123.4567890100")
        assert deal.settlement_amount == Decimal("123.4567890099")

    def test_the_arithmetic_rounds_once_and_never_twice(self) -> None:
        """``0.4449999`` settles at 2 places as ``0.44``: nothing else is ever rounded.

        Rounding the product to four places first and to two afterwards would report
        ``0.45`` — a phantom half-qiran the ledger would have to invent. The engine
        quantizes the product exactly once, which is why this number is the assertion.
        """
        deal = compute(
            from_amount=Decimal("1"), exchange_rate=Decimal("0.4449999"), commission=Decimal("0")
        )
        assert deal.gross_amount == Decimal("0.44")
        double_rounded = quantize_money(quantize_money(Decimal("0.4449999"), 4), 2)
        assert double_rounded == Decimal("0.45")  # what this rule exists to refuse

    def test_a_half_unit_is_rounded_half_up_at_the_documented_policy(self) -> None:
        deal = compute(
            from_amount=Decimal("1"), exchange_rate=Decimal("0.125"), commission=Decimal("0")
        )
        assert deal.gross_amount == Decimal("0.13")

    def test_a_bigger_rate_never_pays_the_customer_less(self) -> None:
        """Monotonicity: the same deal at a better rate can never settle for less.

        Measured at ten places so that a rate that differs beyond a qiran is visible at
        all — in a two-place currency those two rates legitimately settle for the same
        amount, which is the rounding rule working rather than a defect.
        """
        payouts = [
            compute(exchange_rate=Decimal(rate), to_decimal_places=10).settlement_amount
            for rate in ("69", "69.5", "70", "70.0000000001", "71")
        ]
        assert payouts == sorted(payouts)
        assert len(set(payouts)) == len(payouts)

    def test_the_stated_rate_is_the_one_used(self) -> None:
        """No rate is ever re-derived inside the arithmetic (§6: never mix rates)."""
        deal = compute(exchange_rate=Decimal("70.5"))
        assert deal.exchange_rate == Decimal("70.5")
        assert deal.gross_amount == Decimal("70500.00")

    def test_no_field_of_the_computation_is_a_float(self) -> None:
        deal = compute()
        for value in (
            deal.from_amount,
            deal.exchange_rate,
            deal.commission,
            deal.gross_amount,
            deal.settlement_amount,
        ):
            assert isinstance(value, Decimal)
            assert has_money_scale(value)

    def test_the_payload_is_the_stored_scale_as_strings(self) -> None:
        payload = compute().to_payload()
        assert payload == {
            "transaction_type": "BUY",
            "from_amount": "1000.0000000000",
            "exchange_rate": "70.0000000000",
            "commission": "500.0000000000",
            "gross_amount": "70000.0000000000",
            "settlement_amount": "69500.0000000000",
        }
        assert json.dumps(payload)  # strings, so no consumer can re-parse them as floats


class TestDealRefusals:
    """Every refusal names the field it belongs to, before anything is priced or posted."""

    def test_an_unknown_type_is_not_a_deal_at_all(self) -> None:
        assert expect_refusal(transaction_type="TRANSFER") == "unsupported"
        assert expect_refusal(transaction_type="buy") == "unsupported"

    def test_the_type_vocabulary_is_the_domains(self) -> None:
        assert EXCHANGE_TRANSACTION_TYPES == ("BUY", "SELL")

    def test_a_non_positive_quantity_or_rate_is_refused(self) -> None:
        assert expect_refusal(from_amount=Decimal("0")) == "not_positive"
        assert expect_refusal(from_amount=Decimal("-100")) == "not_positive"
        assert expect_refusal(exchange_rate=Decimal("0")) == "not_positive"
        assert expect_refusal(exchange_rate=Decimal("-70")) == "not_positive"

    def test_a_negative_commission_is_refused(self) -> None:
        assert expect_refusal(commission=Decimal("-5")) == "negative"

    def test_a_commission_that_swallows_the_deal_is_refused(self) -> None:
        assert expect_refusal(commission=Decimal("70000")) == "exceeds_gross"
        assert expect_refusal(commission=Decimal("70001")) == "exceeds_gross"

    def test_an_amount_finer_than_the_smallest_unit_is_refused(self) -> None:
        assert expect_refusal(from_amount=Decimal("0.001")) == "below_smallest_unit"

    def test_a_rate_so_small_it_produces_nothing_is_refused(self) -> None:
        """A 0.0040 payout in a 2-place currency is not a rounding question."""
        assert (
            expect_refusal(from_amount=Decimal("0.01"), exchange_rate=Decimal("0.4"))
            == "zero_result"
        )

    def test_a_float_is_not_a_monetary_value(self) -> None:
        assert expect_refusal(from_amount=1.5) == "not_a_decimal"  # type: ignore[arg-type]
        assert expect_refusal(exchange_rate=70.5) == "not_a_decimal"  # type: ignore[arg-type]

    def test_finer_than_the_stored_scale_is_refused(self) -> None:
        assert expect_refusal(from_amount=Decimal("1.00000000001")) == "not_exact_scale"

    def test_a_value_beyond_the_columns_is_refused_with_its_own_code(self) -> None:
        assert expect_refusal(from_amount=MAX_MONEY * 10) == "over_maximum"


class TestBranchBusinessDate:
    """The counter's day, not UTC's — a document number rolls over at local midnight."""

    def test_kabul_is_four_and_a_half_hours_ahead_of_utc(self) -> None:
        # 19:45 UTC is already the next day at a Kabul counter (00:15 local).
        moment = dt.datetime(2026, 9, 11, 19, 45, tzinfo=dt.UTC)
        assert business_date(moment, timezone_name="Asia/Kabul") == dt.date(2026, 9, 12)
        assert moment.date() == dt.date(2026, 9, 11)  # what a UTC-based number would say

    def test_an_instant_before_local_midnight_keeps_the_operators_day(self) -> None:
        moment = dt.datetime(2026, 9, 11, 19, 29, 59, tzinfo=dt.UTC)  # 23:59:59 local
        assert business_date(moment, timezone_name="Asia/Kabul") == dt.date(2026, 9, 11)

    def test_a_naive_free_branch_falls_back_to_utc(self) -> None:
        moment = dt.datetime(2026, 9, 11, 23, 30, tzinfo=dt.UTC)
        assert business_date(moment, timezone_name=None) == dt.date(2026, 9, 11)
        assert business_date(moment, timezone_name="") == dt.date(2026, 9, 11)

    def test_the_fallback_zone_is_explicit(self) -> None:
        moment = dt.datetime(2026, 9, 11, 23, 30, tzinfo=dt.UTC)
        assert business_date(moment, timezone_name=None, fallback="Asia/Kabul") == dt.date(
            2026, 9, 12
        )

    def test_a_stored_timezone_that_does_not_exist_is_a_defect_not_a_default(self) -> None:
        with pytest.raises(DataIntegrityError) as refusal:
            business_date(dt.datetime(2026, 9, 11, tzinfo=dt.UTC), timezone_name="Mars/Olympus")
        assert refusal.value.details["branch_timezone"] == "Mars/Olympus"

    def test_a_dst_zone_keeps_the_local_calendar_day(self) -> None:
        # New York on 2026-03-08 loses an hour at 02:00 local, so 07:30 UTC is 03:30 local
        # while 06:30 UTC is 01:30 local: the *date* must not slide with the offset.
        assert business_date(
            dt.datetime(2026, 3, 8, 7, 30, tzinfo=dt.UTC), timezone_name="America/New_York"
        ) == dt.date(2026, 3, 8)
        assert business_date(
            dt.datetime(2026, 3, 8, 6, 30, tzinfo=dt.UTC), timezone_name="America/New_York"
        ) == dt.date(2026, 3, 8)


class TestReceiptMovementView:
    """The receipt shows the movements the ledger recorded — in the order they happened."""

    def test_lines_keep_the_movements_order_and_shape(self) -> None:
        movements = [
            {
                "movement_type": "IN",
                "currency_code": "USD",
                "amount": Decimal("1000.0000000000"),
                "account_code": "1001",
            },
            {
                "movement_type": "OUT",
                "currency_code": "AFN",
                "amount": Decimal("69500.0000000000"),
                "account_code": "1000",
            },
        ]
        assert _receipt_lines(movements) == [
            {"movement_type": "IN", "currency_code": "USD", "amount": "1000.0000000000"},
            {"movement_type": "OUT", "currency_code": "AFN", "amount": "69500.0000000000"},
        ]

    def test_a_movement_list_without_movements_renders_nothing(self) -> None:
        assert _receipt_lines([]) == []

    def test_the_receipt_version_is_pinned(self) -> None:
        """A printed receipt can be re-rendered the way it was issued."""
        assert RECEIPT_VERSION == "phase5-1"


class TestRequestSchema:
    """The edge: last line against a float (PART 62), and the client never computes (PART 63)."""

    def body(self, **overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "transaction_type": "BUY",
            "branch_id": str(uuid.uuid4()),
            "from_currency_id": str(uuid.uuid4()),
            "from_amount": "1000.0000000000",
            "to_currency_id": str(uuid.uuid4()),
            "exchange_rate": "70.0000000000",
        }
        return {**body, **overrides}

    def test_amounts_arrive_as_decimal_strings(self) -> None:
        request = ExchangeCreateRequest.model_validate(
            self.body(commission="500", to_amount="69500.0000000000")
        )
        assert request.from_amount == Decimal("1000")
        assert request.commission == Decimal("500")
        assert request.to_amount == Decimal("69500")

    def test_the_commission_defaults_to_zero(self) -> None:
        assert ExchangeCreateRequest.model_validate(self.body()).commission == Decimal(0)

    def test_a_json_float_is_refused_for_every_money_field(self) -> None:
        for field in ("from_amount", "exchange_rate", "commission", "to_amount"):
            with pytest.raises(PydanticValidationError) as refusal:
                ExchangeCreateRequest.model_validate(self.body(**{field: 70.1}))
            assert str(refusal.value).count("decimal") >= 1

    def test_a_negative_commission_is_refused_at_the_edge(self) -> None:
        with pytest.raises(PydanticValidationError):
            ExchangeCreateRequest.model_validate(self.body(commission="-1"))

    def test_an_unknown_transaction_type_is_refused_at_the_edge(self) -> None:
        with pytest.raises(PydanticValidationError):
            ExchangeCreateRequest.model_validate(self.body(transaction_type="SWAP"))

    def test_an_unknown_field_is_refused_rather_than_ignored(self) -> None:
        with pytest.raises(PydanticValidationError):
            ExchangeCreateRequest.model_validate(self.body(profit="1000"))

    def test_a_transaction_date_must_carry_a_timezone(self) -> None:
        with pytest.raises(PydanticValidationError):
            ExchangeCreateRequest.model_validate(self.body(transaction_date="2026-09-11T08:00:00"))

    def test_a_transaction_date_is_stored_in_utc(self) -> None:
        request = ExchangeCreateRequest.model_validate(
            self.body(transaction_date="2026-09-11T12:30:00+04:30")
        )
        assert request.transaction_date == dt.datetime(2026, 9, 11, 8, 0, tzinfo=dt.UTC)

    def test_a_blank_note_is_not_a_note(self) -> None:
        request = ExchangeCreateRequest.model_validate(self.body(description="   "))
        assert request.description is None
        assert (
            ExchangeCreateRequest.model_validate(self.body(description="  rent  ")).description
            == "rent"
        )

    def test_a_value_beyond_the_columns_is_refused_at_the_edge(self) -> None:
        with pytest.raises(PydanticValidationError):
            ExchangeCreateRequest.model_validate(self.body(from_amount=format_decimal(MAX_MONEY)))

    def test_the_reason_of_an_undo_is_required_and_bounded(self) -> None:
        assert ExchangeReasonRequest.model_validate({"reason": "wrong rate"}).reason == "wrong rate"
        with pytest.raises(PydanticValidationError):
            ExchangeReasonRequest.model_validate({"reason": "no"})
        with pytest.raises(PydanticValidationError):
            ExchangeReasonRequest.model_validate({"reason": "x" * 501})
        with pytest.raises(PydanticValidationError):
            ExchangeReasonRequest.model_validate({"reason": "why", "profit": "1"})


class TestMoneyDiscipline:
    """The helpers the engine is allowed to use, at the scales the ledger stores."""

    def test_the_stored_scale_is_ten_places(self) -> None:
        assert MONEY_SCALE == 10
        assert format_decimal(Decimal("70")) == "70.0000000000"

    def test_quantize_uses_the_currencys_places(self) -> None:
        assert quantize_money(Decimal("1.005"), 2) == Decimal("1.01")
        assert quantize_money(Decimal("1.005"), 3) == Decimal("1.005")

    def test_a_mirror_rate_round_trips_within_one_quantum(self) -> None:
        """The reversal's rate is derived, and deriving it must not move money.

        500 USD sold for 35,500 AFN is undone by a document that hands back 35,500 AFN for
        500 USD; its stored rate is ``from / to`` at the ledger's scale, so the product of
        that rate and the mirrored amount returns the original quantity as closely as ten
        places allow.
        """
        rate = divide_money(Decimal("500"), Decimal("35500"))
        assert rate == Decimal("0.0140845070")
        back = multiply_money(Decimal("35500"), rate, scale=2)
        assert back == Decimal("500.00")

    def test_sums_of_legs_stay_exact(self) -> None:
        legs = [Decimal("69500.0000000000"), Decimal("500.0000000000")]
        assert money_sum(legs) == Decimal("70000.0000000000")
