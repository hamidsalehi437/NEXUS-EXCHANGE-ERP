"""Pure accounting rules — no database, no HTTP (PART 49, PART 62).

Everything here is a rule that decides whether money is allowed to move:

* the balance proof (``validate_balanced_entry``) and every way it must refuse;
* the FX gain/loss *sign* derivation (``§16`` of the Phase 4 brief: derived from the
  model, not from intuition);
* the accounting-date rules;
* the idempotency fingerprint and its canonical hash;
* the authority maps, which are the whole authorization rule for the ledger.

The database half of these rules lives in the integration suites; these tests are the
first line and they never touch PostgreSQL, so a broken rule is reported in milliseconds
instead of after a migration.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from app.core.exceptions import (
    JournalUnbalancedError,
    ValidationError,
)
from app.core.idempotency import (
    canonical_request_hash,
    json_safe,
)
from app.core.money import MAX_MONEY, MoneyError, format_decimal
from app.core.permissions import Permission
from app.services.accounting_service import (
    CASH_MOVEMENT_TYPES,
    EXCHANGE_TRANSACTION_TYPES,
    NON_REVERSIBLE_REFERENCE_TYPES,
    POSTING_AUTHORITY,
    REFERENCE_TYPES,
    REFERENCE_TYPES_REQUIRING_DOCUMENT,
    REVERSAL_AUTHORITY,
    AccountingService,
    JournalEntryView,
    PostingLine,
    _accounting_date,
    _adjustment_sign,
    _cash_movement_type,
    _close_with_fx_result,
    _fingerprint,
    _functional_balance,
    _reference_type,
    _transaction_type,
)

pytestmark = pytest.mark.unit

ACCOUNT = uuid.UUID("11111111-1111-1111-1111-111111111111")
OTHER_ACCOUNT = uuid.UUID("22222222-2222-2222-2222-222222222222")
CURRENCY = uuid.UUID("33333333-3333-3333-3333-333333333333")
OTHER_CURRENCY = uuid.UUID("44444444-4444-4444-4444-444444444444")


def line(
    *,
    account: uuid.UUID = ACCOUNT,
    currency: uuid.UUID = CURRENCY,
    debit: str = "0",
    credit: str = "0",
    rate: str = "1",
    description: str | None = None,
) -> PostingLine:
    return PostingLine(
        account_id=account,
        currency_id=currency,
        debit=Decimal(debit),
        credit=Decimal(credit),
        exchange_rate=Decimal(rate),
        description=description,
    )


class TestBalanceProof:
    """``SUM(debit) = SUM(credit)``, proved in Decimal before anything is written."""

    def test_a_balanced_two_line_entry_is_accepted(self) -> None:
        totals = AccountingService.validate_balanced_entry(
            [
                line(debit="100", account=ACCOUNT),
                line(credit="100", account=OTHER_ACCOUNT),
            ]
        )
        assert totals.lines == 2
        assert totals.debit == Decimal("100")
        assert totals.credit == Decimal("100")
        assert totals.difference == 0
        assert totals.is_balanced is True

    def test_a_multi_line_entry_is_accepted_and_summed_exactly(self) -> None:
        lines = [
            line(debit="1000"),
            line(debit="250.25"),
            line(credit="750.25"),
            line(credit="400"),
            line(credit="100"),
        ]
        totals = AccountingService.validate_balanced_entry(lines)
        assert totals.lines == 5
        assert totals.debit == Decimal("1250.25")
        assert totals.credit == Decimal("1250.25")

    def test_a_many_line_entry_is_accepted(self) -> None:
        lines = [line(debit="0.0000000001") for _ in range(50)]
        lines.append(line(credit="0.000000005", account=OTHER_ACCOUNT))
        totals = AccountingService.validate_balanced_entry(lines)
        assert totals.lines == 51
        assert totals.debit == Decimal("0.0000000050")

    def test_ten_decimal_places_survive_the_proof(self) -> None:
        """The stored scale is 10 dp; a proof that lost a digit would not be a proof."""
        totals = AccountingService.validate_balanced_entry(
            [line(debit="0.0000000003"), line(credit="0.0000000003", account=OTHER_ACCOUNT)]
        )
        assert totals.debit == Decimal("0.0000000003")
        assert format_decimal(totals.debit) == "0.0000000003"

    def test_an_unbalanced_entry_is_refused_with_both_totals(self) -> None:
        with pytest.raises(JournalUnbalancedError) as refusal:
            AccountingService.validate_balanced_entry(
                [line(debit="1000"), line(credit="999.99", account=OTHER_ACCOUNT)]
            )
        assert refusal.value.details["total_debit"] == "1000.0000000000"
        assert refusal.value.details["total_credit"] == "999.9900000000"
        assert refusal.value.details["difference"] == "0.0100000000"

    def test_a_debit_only_entry_is_refused(self) -> None:
        with pytest.raises(JournalUnbalancedError):
            AccountingService.validate_balanced_entry(
                [line(debit="500"), line(credit="0", account=OTHER_ACCOUNT)]
            )

    def test_a_credit_only_entry_is_refused(self) -> None:
        with pytest.raises(JournalUnbalancedError):
            AccountingService.validate_balanced_entry(
                [line(credit="500"), line(debit="0", account=OTHER_ACCOUNT)]
            )

    def test_a_single_line_entry_is_refused(self) -> None:
        with pytest.raises(JournalUnbalancedError) as refusal:
            AccountingService.validate_balanced_entry([line(debit="100")])
        assert refusal.value.details == {"lines": 1, "minimum": 2}

    def test_an_empty_entry_is_refused(self) -> None:
        with pytest.raises(JournalUnbalancedError) as refusal:
            AccountingService.validate_balanced_entry([])
        assert refusal.value.details["minimum"] == 2

    def test_a_zero_value_line_is_refused(self) -> None:
        with pytest.raises(JournalUnbalancedError) as refusal:
            AccountingService.validate_balanced_entry(
                [line(debit="0", credit="0"), line(debit="0", credit="0", account=OTHER_ACCOUNT)]
            )
        assert refusal.value.details["reason"] == "ZERO_VALUE"
        assert refusal.value.details["line"] == 0

    def test_a_line_carrying_both_sides_is_refused(self) -> None:
        with pytest.raises(JournalUnbalancedError) as refusal:
            AccountingService.validate_balanced_entry(
                [line(debit="100", credit="100"), line(credit="100", account=OTHER_ACCOUNT)]
            )
        assert refusal.value.details["reason"] == "DEBIT_AND_CREDIT"

    def test_a_negative_amount_is_refused(self) -> None:
        with pytest.raises(JournalUnbalancedError) as refusal:
            AccountingService.validate_balanced_entry(
                [line(debit="-100"), line(credit="-100", account=OTHER_ACCOUNT)]
            )
        assert "negative" in str(refusal.value).lower()

    def test_a_zero_rate_is_refused(self) -> None:
        with pytest.raises(JournalUnbalancedError) as refusal:
            AccountingService.validate_balanced_entry(
                [line(debit="100", rate="0"), line(credit="100", account=OTHER_ACCOUNT)]
            )
        assert refusal.value.details["exchange_rate"] == "0"

    def test_more_than_ten_decimal_places_is_refused(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            AccountingService.validate_balanced_entry(
                [line(debit="0.00000000001"), line(credit="0.00000000001", account=OTHER_ACCOUNT)]
            )
        assert refusal.value.details["fields"][0]["code"] == "not_exact_scale"

    def test_a_value_beyond_numeric_30_10_is_refused(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            AccountingService.validate_balanced_entry(
                [line(debit=str(MAX_MONEY)), line(credit=str(MAX_MONEY), account=OTHER_ACCOUNT)]
            )
        assert refusal.value.details["fields"][0]["code"] == "over_maximum"

    def test_a_value_at_the_numeric_bound_is_accepted(self) -> None:
        # The literal, not ``MAX_MONEY - 1E-10``: that subtraction would be rounded by the
        # *test process's* default decimal context (28 digits) before the engine ever sees
        # it, which is the very class of defect this suite exists to catch.
        largest = Decimal("99999999999999999999.9999999998")
        totals = AccountingService.validate_balanced_entry(
            [line(debit=str(largest)), line(credit=str(largest), account=OTHER_ACCOUNT)]
        )
        assert totals.debit == largest

    def test_a_float_amount_is_refused(self) -> None:
        """PART 62: a binary float can never be a money value, at any layer."""
        with pytest.raises(ValidationError) as refusal:
            AccountingService.validate_balanced_entry(
                [
                    PostingLine(  # type: ignore[arg-type]
                        account_id=ACCOUNT, currency_id=CURRENCY, debit=1.5
                    ),
                    line(credit="1.5", account=OTHER_ACCOUNT),
                ]
            )
        assert refusal.value.details["fields"][0]["code"] == "not_a_decimal"

    def test_very_small_amounts_sum_exactly(self) -> None:
        """Ten additions of 1e-10 are exactly 1e-9 — a float would already be wrong here."""
        lines = [line(debit="0.0000000001") for _ in range(10)]
        lines.append(line(credit="0.000000001", account=OTHER_ACCOUNT))
        totals = AccountingService.validate_balanced_entry(lines)
        assert totals.debit == Decimal("0.000000001")

    def test_no_float_arithmetic_in_the_totals(self) -> None:
        totals = AccountingService.validate_balanced_entry(
            [line(debit="0.1"), line(debit="0.2"), line(credit="0.3", account=OTHER_ACCOUNT)]
        )
        assert totals.debit == Decimal("0.3")
        assert totals.is_balanced is True


class TestFxResultSignConvention:
    """§16: the direction of the FX line is derived, then asserted in both directions.

    The accounting model fixes it: ``4000 FX Gain / Loss`` is a **revenue** account
    (``ACCOUNTING_MODEL.md`` §5), revenue accounts are credit-normal, so a realized *gain*
    is a **credit** to 4000 and a realized *loss* is a **debit**. Equivalently, from the
    balance proof: the line must close whatever side is short, so the side that is short
    is the side the result lands on.
    """

    def test_balanced_legs_need_no_fx_line(self) -> None:
        lines = [line(debit="100"), line(credit="100", account=OTHER_ACCOUNT)]
        assert (
            _close_with_fx_result(
                lines,
                fx_account_id=OTHER_ACCOUNT,
                functional_currency_id=CURRENCY,
                reference="test",
            )
            == lines
        )

    def test_a_gain_is_credited_to_the_fx_account(self) -> None:
        """Debits exceed credits: the credit side is short, so the gain is a credit."""
        lines = [line(debit="105"), line(credit="100", account=OTHER_ACCOUNT)]
        closed = _close_with_fx_result(
            lines, fx_account_id=ACCOUNT, functional_currency_id=CURRENCY, reference="gain"
        )
        assert len(closed) == 3
        plug = closed[-1]
        assert plug.credit == Decimal("5")
        assert plug.debit == Decimal("0")
        assert plug.exchange_rate == Decimal(1)
        assert plug.description == "FX gain on gain"
        assert sum(item.debit for item in closed) == sum(item.credit for item in closed)

    def test_a_loss_is_debited_to_the_fx_account(self) -> None:
        """Credits exceed debits: the debit side is short, so the loss is a debit."""
        lines = [line(debit="100"), line(credit="107.5", account=OTHER_ACCOUNT)]
        closed = _close_with_fx_result(
            lines, fx_account_id=ACCOUNT, functional_currency_id=CURRENCY, reference="loss"
        )
        plug = closed[-1]
        assert plug.debit == Decimal("7.5")
        assert plug.credit == Decimal("0")
        assert plug.description == "FX loss on loss"
        assert sum(item.debit for item in closed) == sum(item.credit for item in closed)

    def test_a_rounding_difference_of_one_ulp_is_closed(self) -> None:
        lines = [line(debit="100.0000000001"), line(credit="100", account=OTHER_ACCOUNT)]
        closed = _close_with_fx_result(
            lines, fx_account_id=ACCOUNT, functional_currency_id=CURRENCY, reference="rounding"
        )
        assert closed[-1].credit == Decimal("0.0000000001")

    def test_a_result_without_an_fx_account_is_refused_not_rounded_away(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            _close_with_fx_result(
                [line(debit="105"), line(credit="100", account=OTHER_ACCOUNT)],
                fx_account_id=None,
                functional_currency_id=CURRENCY,
                reference="unconfigured",
            )
        assert refusal.value.details["fields"] == [{"field": "fx_account_id", "code": "required"}]
        assert refusal.value.details["difference"] == "5.0000000000"

    def test_the_fx_line_is_in_the_functional_currency(self) -> None:
        closed = _close_with_fx_result(
            [line(debit="105", currency=OTHER_CURRENCY), line(credit="100", account=OTHER_ACCOUNT)],
            fx_account_id=ACCOUNT,
            functional_currency_id=CURRENCY,
            reference="functional",
        )
        assert closed[-1].currency_id == CURRENCY


class TestAccountingDate:
    """Dates are server-authoritative: no client may post into the future (PART 34)."""

    def test_a_naive_date_is_refused(self) -> None:
        with pytest.raises(ValidationError) as refusal:
            _accounting_date(dt.datetime(2026, 1, 1, 12, 0), _settings())
        assert refusal.value.details["fields"] == [{"field": "transaction_date", "code": "naive"}]

    def test_a_future_date_is_refused(self) -> None:
        future = dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=1)
        with pytest.raises(ValidationError) as refusal:
            _accounting_date(future, _settings())
        assert refusal.value.details["fields"] == [
            {"field": "transaction_date", "code": "future_date"}
        ]
        assert refusal.value.details["tolerated_skew_minutes"] == 120

    def test_a_date_inside_the_clock_skew_is_tolerated(self) -> None:
        moment = dt.datetime.now(tz=dt.UTC) + dt.timedelta(minutes=5)
        assert _accounting_date(moment, _settings()) == moment

    def test_a_historical_date_is_kept_verbatim_in_utc(self) -> None:
        kabul = dt.timezone(dt.timedelta(hours=4, minutes=30))
        moment = dt.datetime(2024, 3, 15, 9, 30, tzinfo=kabul)
        assert _accounting_date(moment, _settings()) == moment.astimezone(dt.UTC)

    def test_an_omitted_date_becomes_the_server_clock(self) -> None:
        before = dt.datetime.now(tz=dt.UTC)
        moment = _accounting_date(None, _settings())
        assert before <= moment <= dt.datetime.now(tz=dt.UTC) + dt.timedelta(seconds=1)
        assert moment.tzinfo is not None


class TestVocabulary:
    """Casing and enumeration of the ledger's own vocabulary."""

    def test_a_reference_type_is_normalized_and_validated(self) -> None:
        assert _reference_type("  exchange_transaction ") == "EXCHANGE_TRANSACTION"
        with pytest.raises(ValidationError) as refusal:
            _reference_type("SALE")
        assert refusal.value.details["allowed"] == list(REFERENCE_TYPES)

    def test_a_transaction_type_is_normalized_and_validated(self) -> None:
        assert _transaction_type("buy") == "BUY"
        assert _transaction_type("SELL") == "SELL"
        with pytest.raises(ValidationError) as refusal:
            _transaction_type("SWAP")
        assert refusal.value.details["allowed"] == list(EXCHANGE_TRANSACTION_TYPES)

    def test_a_cash_movement_type_is_normalized_and_validated(self) -> None:
        assert _cash_movement_type("opening") == "OPENING"
        with pytest.raises(ValidationError) as refusal:
            _cash_movement_type("REFUND")
        assert refusal.value.details["allowed"] == list(CASH_MOVEMENT_TYPES)

    def test_a_closing_count_is_not_a_journal_entry(self) -> None:
        """A count is a reconciliation snapshot; the difference is posted as an ADJUSTMENT."""
        with pytest.raises(ValidationError) as refusal:
            _cash_movement_type("closing")
        assert refusal.value.details["fields"] == [{"field": "movement_type", "code": "no_journal"}]
        assert "hint" in refusal.value.details

    def test_an_adjustment_needs_a_sign(self) -> None:
        assert _adjustment_sign("ADJUSTMENT", -1) == -1
        assert _adjustment_sign("ADJUSTMENT", 1) == 1
        with pytest.raises(ValidationError):
            _adjustment_sign("ADJUSTMENT", None)
        with pytest.raises(ValidationError):
            _adjustment_sign("ADJUSTMENT", 0)

    def test_a_non_adjustment_carries_no_sign(self) -> None:
        """IN/OUT take their side from the movement type, so the sign field stays 0."""
        assert _adjustment_sign("IN", None) == 0
        assert _adjustment_sign("OUT", None) == 0
        with pytest.raises(ValidationError) as refusal:
            _adjustment_sign("IN", -1)
        assert refusal.value.details["fields"] == [
            {"field": "adjustment_sign", "code": "unexpected"}
        ]


class TestAuthorityMaps:
    """The maps are the entire authorization rule for the ledger, so they are pinned."""

    def test_every_reference_type_has_a_posting_authority(self) -> None:
        assert set(POSTING_AUTHORITY) == set(REVERSAL_AUTHORITY)
        assert set(POSTING_AUTHORITY) == set(REFERENCE_TYPES)

    def test_the_maps_match_the_api_contract(self) -> None:
        assert POSTING_AUTHORITY["EXCHANGE_TRANSACTION"] is Permission.EXCHANGE_CREATE
        assert POSTING_AUTHORITY["CASH_MOVEMENT"] is Permission.CASH_CREATE
        assert POSTING_AUTHORITY["EXPENSE"] is Permission.EXPENSES_CREATE
        assert POSTING_AUTHORITY["TRANSFER"] is Permission.TRANSFERS_CREATE
        assert POSTING_AUTHORITY["MANUAL_ADJUSTMENT"] is Permission.ACCOUNTS_MANAGE
        assert POSTING_AUTHORITY["OPENING_BALANCE"] is Permission.ACCOUNTS_MANAGE
        assert REVERSAL_AUTHORITY["EXCHANGE_TRANSACTION"] is Permission.EXCHANGE_REVERSE
        assert REVERSAL_AUTHORITY["CASH_MOVEMENT"] is Permission.CASH_ADJUST
        assert REVERSAL_AUTHORITY["TRANSFER"] is Permission.TRANSFERS_CANCEL
        assert REVERSAL_AUTHORITY["EXPENSE"] is Permission.EXPENSES_CREATE

    def test_undoing_is_a_different_authority_from_doing(self) -> None:
        """Segregation of duties: creating and reversing must be separately grantable."""
        assert (
            REVERSAL_AUTHORITY["EXCHANGE_TRANSACTION"]
            is not POSTING_AUTHORITY["EXCHANGE_TRANSACTION"]
        )
        assert REVERSAL_AUTHORITY["CASH_MOVEMENT"] is not POSTING_AUTHORITY["CASH_MOVEMENT"]
        assert REVERSAL_AUTHORITY["TRANSFER"] is not POSTING_AUTHORITY["TRANSFER"]

    def test_only_a_manual_adjustment_may_omit_its_document(self) -> None:
        assert "MANUAL_ADJUSTMENT" in REFERENCE_TYPES
        assert "MANUAL_ADJUSTMENT" not in REFERENCE_TYPES_REQUIRING_DOCUMENT
        assert set(REFERENCE_TYPES_REQUIRING_DOCUMENT) == set(REFERENCE_TYPES) - {
            "MANUAL_ADJUSTMENT"
        }

    def test_a_reversal_is_never_a_reversible_target(self) -> None:
        assert NON_REVERSIBLE_REFERENCE_TYPES == ("REVERSAL",)


class TestFingerprintAndReplaySafety:
    """The fingerprint decides what "the same request" means for a retry (PART 40)."""

    def test_the_fingerprint_drops_what_the_client_did_not_supply(self) -> None:
        supplied = _fingerprint(a=1, b=None, c="x")
        assert supplied == {"a": 1, "c": "x"}

    def test_the_hash_is_order_independent(self) -> None:
        assert canonical_request_hash({"a": 1, "b": 2}) == canonical_request_hash({"b": 2, "a": 1})

    def test_a_different_amount_is_a_different_request(self) -> None:
        """Decimal('70') and Decimal('70.0000000000') must not collide into one posting."""
        assert canonical_request_hash({"rate": Decimal("70")}) != canonical_request_hash(
            {"rate": Decimal("70.0000000000")}
        )

    def test_uuid_and_datetime_values_hash_deterministically(self) -> None:
        moment = dt.datetime(2026, 5, 1, 10, 0, tzinfo=dt.UTC)
        first = canonical_request_hash({"id": ACCOUNT, "at": moment})
        second = canonical_request_hash({"id": uuid.UUID(str(ACCOUNT)), "at": moment})
        assert first == second
        assert len(first) == 64

    def test_a_float_in_a_payload_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            json_safe({"amount": 10.5})


class TestBalanceConvention:
    """``normal_balance`` decides the reported sign (``v_account_balances``)."""

    def test_a_debit_account_reports_debits_minus_credits(self) -> None:
        assert _functional_balance("DEBIT", Decimal("100"), Decimal("30")) == Decimal("70")

    def test_a_credit_account_reports_credits_minus_debits(self) -> None:
        assert _functional_balance("CREDIT", Decimal("30"), Decimal("100")) == Decimal("70")

    def test_the_blank_padded_char_column_is_stripped(self) -> None:
        """The frozen column is ``CHAR(6)``: PostgreSQL pads it to ``"DEBIT "``."""
        assert _functional_balance("DEBIT ", Decimal("1"), Decimal("0")) == Decimal("1")
        assert _functional_balance("credit", Decimal("0"), Decimal("1")) == Decimal("1")


class TestMoneyArithmeticAtTheStoredScale:
    """Regression class for the money-precision defects the Phase 4 suite first found.

    All three were ``Decimal`` *context* effects: the stored column holds 30 significant
    digits, Python arithmetic defaults to 28, and ``quantize`` refuses rather than rounds
    when it cannot fit. The fixes live in ``app.core.money`` (one wide context, one
    rounding step per operation, plain fixed-point wire format); these tests pin them.
    """

    def test_a_tiny_amount_is_never_formatted_in_scientific_notation(self) -> None:
        assert format_decimal(Decimal("3E-10")) == "0.0000000003"
        assert format_decimal(Decimal("0.0000000001")) == "0.0000000001"

    def test_the_largest_storable_amount_can_be_formatted(self) -> None:
        assert format_decimal(MAX_MONEY) == "99999999999999999999.9999999999"

    def test_a_twenty_digit_amount_can_be_rounded(self) -> None:
        """``quantize`` at 20 integer digits needs 30 significant digits, not 28."""
        from app.core.money import quantize_money as quantize

        assert quantize(Decimal("50000000000000000000"), 10) == Decimal(
            "50000000000000000000.0000000000"
        )

    def test_a_product_is_rounded_once_at_the_stored_scale(self) -> None:
        from app.core.money import multiply_money

        product = multiply_money(Decimal("99999999999999999999.9999999999"), Decimal("1.5"))
        assert product == Decimal("149999999999999999999.9999999999")

    def test_a_quotient_is_rounded_half_up_at_the_stored_scale(self) -> None:
        from app.core.money import divide_money

        assert divide_money(Decimal("70000"), Decimal("3")) == Decimal("23333.3333333333")
        with pytest.raises(MoneyError):
            divide_money(Decimal("1"), Decimal("0"))

    def test_a_sum_of_ten_decimal_values_is_exact(self) -> None:
        from app.core.money import money_sum

        values = [Decimal("99999999999999999999.9999999999")] * 3
        assert money_sum(values) == Decimal("299999999999999999999.9999999997")

    def test_scale_membership_is_judged_at_the_stored_scale(self) -> None:
        from app.core.money import has_money_scale

        assert has_money_scale(Decimal("99999999999999999999.9999999999")) is True
        assert has_money_scale(Decimal("1.00000000001")) is False


class TestEntryViewRoundTrip:
    """A replayed answer is rebuilt from the stored payload; the round trip must be exact."""

    def test_a_payload_rebuilds_into_an_equal_view(self) -> None:
        view = JournalEntryView(
            id=ACCOUNT,
            reference_type="EXCHANGE_TRANSACTION",
            reference_id=OTHER_ACCOUNT,
            description="round trip",
            transaction_date=dt.datetime(2026, 4, 1, 8, 0, tzinfo=dt.UTC),
            created_at=dt.datetime(2026, 4, 1, 8, 0, 1, tzinfo=dt.UTC),
            created_by=ACCOUNT,
            created_by_username="tester",
            branch_id=CURRENCY,
            branch_code="MAIN",
            device_id=None,
            reversal_of_id=None,
            reversed_by_entry_id=None,
            line_count=2,
            total_debit=Decimal("700.0000000000"),
            total_credit=Decimal("700.0000000000"),
            lines=(),
        )
        rebuilt = JournalEntryView.from_payload(view.to_payload())
        assert rebuilt == view
        assert rebuilt.is_balanced is True


def _settings() -> object:
    """The real settings object, so the skew and timezone rules come from configuration."""
    from app.core.config import get_settings

    return get_settings()
