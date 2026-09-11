"""Money rules are executable (PART 20, PART 62).

These tests are the guard rail against the single most dangerous shortcut in a
financial system: letting a binary float touch an amount.
"""

from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from app.core.money import (
    MAX_MONEY,
    MONEY_PRECISION,
    MONEY_QUANTUM,
    MONEY_SCALE,
    MoneyError,
    assert_within_money_bounds,
    format_decimal,
    quantize_money,
    require_non_negative,
    require_positive,
    to_decimal,
)

pytestmark = pytest.mark.unit


class TestFloatRejection:
    """A float must never become a monetary value, however harmless it looks."""

    def test_float_is_rejected(self) -> None:
        with pytest.raises(MoneyError, match="not float"):
            to_decimal(1.5)  # type: ignore[arg-type]

    def test_float_that_looks_exact_is_still_rejected(self) -> None:
        # 0.1 is not exactly representable in binary; the API must never accept it.
        with pytest.raises(MoneyError, match="not float"):
            to_decimal(0.1)  # type: ignore[arg-type]

    def test_bool_is_rejected(self) -> None:
        with pytest.raises(MoneyError, match="not bool"):
            to_decimal(True)  # type: ignore[arg-type]

    def test_field_name_appears_in_the_message(self) -> None:
        with pytest.raises(MoneyError, match="exchange_rate"):
            to_decimal(2.5, field="exchange_rate")  # type: ignore[arg-type]


class TestDecimalParsing:
    def test_decimal_string_is_parsed_exactly(self) -> None:
        assert to_decimal("1000.0000000001") == Decimal("1000.0000000001")

    def test_integer_is_accepted(self) -> None:
        assert to_decimal(1000) == Decimal(1000)

    def test_decimal_passes_through(self) -> None:
        value = Decimal("0.0000000001")
        assert to_decimal(value) is value

    def test_surrounding_whitespace_is_trimmed(self) -> None:
        assert to_decimal("  42.5  ") == Decimal("42.5")

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_string_is_rejected(self, value: str) -> None:
        with pytest.raises(MoneyError, match="must not be empty"):
            to_decimal(value)

    @pytest.mark.parametrize("value", ["abc", "10,50", "1.2.3", "--5"])
    def test_malformed_string_is_rejected(self, value: str) -> None:
        with pytest.raises(MoneyError, match="not a valid decimal"):
            to_decimal(value)

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
    def test_non_finite_string_is_rejected(self, value: str) -> None:
        with pytest.raises(MoneyError, match="finite"):
            to_decimal(value)


class TestSignGuards:
    def test_positive_accepts_positive(self) -> None:
        assert require_positive(Decimal("0.0000000001")) == Decimal("0.0000000001")

    @pytest.mark.parametrize("value", ["0", "-0.0000000001"])
    def test_positive_rejects_zero_and_negative(self, value: str) -> None:
        with pytest.raises(MoneyError, match="greater than zero"):
            require_positive(Decimal(value))

    def test_non_negative_accepts_zero(self) -> None:
        assert require_non_negative(Decimal("0")) == Decimal("0")

    def test_non_negative_rejects_negative(self) -> None:
        with pytest.raises(MoneyError, match="must not be negative"):
            require_non_negative(Decimal("-1"))


class TestRounding:
    """Final amounts are rounded half-up at the currency's decimal places."""

    @pytest.mark.parametrize(
        ("value", "places", "expected"),
        [
            ("0.005", 2, "0.01"),
            ("0.004", 2, "0.00"),
            ("2.675", 2, "2.68"),
            ("0.1449", 2, "0.14"),
            ("1.005", 2, "1.01"),
            ("1000", 0, "1000"),
            ("0.00000000005", MONEY_SCALE, "0.0000000001"),
        ],
    )
    def test_half_up_rounding(self, value: str, places: int, expected: str) -> None:
        assert quantize_money(Decimal(value), places) == Decimal(expected)

    def test_rounding_does_not_lose_the_sign(self) -> None:
        assert quantize_money(Decimal("-2.675"), 2) == Decimal("-2.68")

    @pytest.mark.parametrize("places", [-1, MONEY_SCALE + 1])
    def test_out_of_range_scale_is_rejected(self, places: int) -> None:
        with pytest.raises(MoneyError, match="decimal_places"):
            quantize_money(Decimal("1"), places)


class TestWireFormat:
    def test_amount_is_formatted_with_ten_decimals(self) -> None:
        assert format_decimal(Decimal("70000")) == "70000.0000000000"

    def test_format_is_round_trippable(self) -> None:
        original = Decimal("123456.7890123456")
        assert to_decimal(format_decimal(original)) == original.quantize(
            Decimal(1).scaleb(-MONEY_SCALE)
        )

    def test_scale_validation(self) -> None:
        with pytest.raises(MoneyError, match="scale"):
            format_decimal(Decimal("1"), MONEY_SCALE + 1)


class TestBounds:
    """NUMERIC(30,10) is 20 integer digits and 10 decimals; the guard must match it."""

    NEAR_MAX = Decimal("99999999999999999999.9999999998")
    ABOVE_MAX = Decimal("100000000000000000000.0000000000")

    def test_value_inside_numeric_30_10_is_accepted(self) -> None:
        assert assert_within_money_bounds(self.NEAR_MAX) == self.NEAR_MAX

    def test_maximum_value_itself_is_rejected(self) -> None:
        # The guard is exclusive: MAX_MONEY is not representable in NUMERIC(30,10).
        with pytest.raises(MoneyError, match="maximum representable"):
            assert_within_money_bounds(MAX_MONEY)

    @pytest.mark.parametrize("value", ["ABOVE_MAX", "NEGATIVE_ABOVE_MAX"])
    def test_value_outside_numeric_30_10_is_rejected(self, value: str) -> None:
        candidate = self.ABOVE_MAX if value == "ABOVE_MAX" else -self.ABOVE_MAX
        with pytest.raises(MoneyError, match="maximum representable"):
            assert_within_money_bounds(candidate)

    def test_max_money_matches_numeric_30_10(self) -> None:
        # 10**20 - 1E-10 needs 30 significant digits; the default decimal context (28)
        # would silently round it up to 1E20 — the bug this constant now avoids.
        with localcontext() as context:
            context.prec = MONEY_PRECISION * 2
            expected = Decimal(10) ** (MONEY_PRECISION - MONEY_SCALE) - MONEY_QUANTUM
        assert expected == MAX_MONEY
        assert Decimal("99999999999999999999.9999999999") == MAX_MONEY

    def test_max_representable_value_is_accepted(self) -> None:
        assert assert_within_money_bounds(self.NEAR_MAX) == self.NEAR_MAX

    def test_guard_does_not_round_the_value_it_checks(self) -> None:
        # abs() would round NEAR_MAX to 1E20 under the default context and reject it.
        assert abs(self.NEAR_MAX) >= MAX_MONEY  # documents the trap
        assert assert_within_money_bounds(self.NEAR_MAX.copy_abs()) == self.NEAR_MAX
