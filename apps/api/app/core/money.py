"""Money handling — the only place amounts are parsed, validated or rounded.

PART 62 forbids ``float``/``double`` as a source of truth. This module makes that
rule executable:

* :func:`to_decimal` **rejects** ``float``/``bool`` inputs outright, so a stray
  ``1.005`` can never enter an amount, a rate or a quantity.
* :func:`quantize_money` applies the documented rounding policy (``ROUND_HALF_UP``
  at the currency's decimal places); intermediate arithmetic stays exact.
* :func:`format_decimal` produces the canonical wire format (fixed 10 decimal
  places) that the API contract requires for every monetary field.

Everything stored in PostgreSQL is ``NUMERIC(30,10)``; everything in Python is
:class:`decimal.Decimal`; everything on the wire is a string.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_PRECISION = 30
MONEY_SCALE = 10
MONEY_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)  # 1E-10

# Largest value NUMERIC(30,10) can hold: 20 integer digits and all 10 decimals set.
# Written as a literal on purpose. Computing it arithmetically (10**20 - 1E-10) would be
# evaluated under Python's default decimal context (28 significant digits), which rounds
# the result up to exactly 1E20 and makes the bounds guard reject valid amounts.
MAX_MONEY = Decimal("9" * (MONEY_PRECISION - MONEY_SCALE) + "." + "9" * MONEY_SCALE)


class MoneyError(ValueError):
    """Raised when a value cannot be used as a monetary amount."""


def to_decimal(value: str | int | Decimal, *, field: str = "amount") -> Decimal:
    """Parse a monetary value **without ever accepting a binary float**.

    >>> to_decimal("1000.0000000000")
    Decimal('1000.0000000000')
    >>> to_decimal(1000)
    Decimal('1000')
    >>> to_decimal(1000.5)
    Traceback (most recent call last):
    ...
    app.core.money.MoneyError: amount must be a decimal string, not float
    """
    if isinstance(value, bool):  # bool is an int subclass; never a money value
        raise MoneyError(f"{field} must be a decimal string, not bool")
    if isinstance(value, float):
        raise MoneyError(f"{field} must be a decimal string, not float")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise MoneyError(f"{field} must not be empty")
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError(f"{field} is not a valid decimal value: {value!r}") from exc
        if not parsed.is_finite():
            raise MoneyError(f"{field} must be a finite decimal value")
        return parsed
    raise MoneyError(f"{field} must be a decimal string, int or Decimal")


def require_positive(value: Decimal, *, field: str = "amount") -> Decimal:
    """Return ``value`` when it is strictly positive, otherwise raise."""
    if value <= 0:
        raise MoneyError(f"{field} must be greater than zero")
    return value


def require_non_negative(value: Decimal, *, field: str = "amount") -> Decimal:
    """Return ``value`` when it is zero or positive, otherwise raise."""
    if value < 0:
        raise MoneyError(f"{field} must not be negative")
    return value


def quantize_money(value: Decimal, decimal_places: int = 2) -> Decimal:
    """Round to the currency's decimal places using ``ROUND_HALF_UP``.

    Only *final* amounts are rounded; intermediate arithmetic between ledger legs
    must stay unrounded so that ``SUM(debit) = SUM(credit)`` holds exactly.
    """
    if decimal_places < 0 or decimal_places > MONEY_SCALE:
        raise MoneyError(f"decimal_places must be between 0 and {MONEY_SCALE}")
    quantum = Decimal(1).scaleb(-decimal_places)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def format_decimal(value: Decimal, scale: int = MONEY_SCALE) -> str:
    """Canonical wire format: fixed-point string with exactly ``scale`` decimals.

    >>> format_decimal(Decimal('70000'))
    '70000.0000000000'
    """
    if scale < 0 or scale > MONEY_SCALE:
        raise MoneyError(f"scale must be between 0 and {MONEY_SCALE}")
    return str(value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP))


def assert_within_money_bounds(value: Decimal, *, field: str = "amount") -> Decimal:
    """Guard against values that would not fit ``NUMERIC(30,10)``.

    ``copy_abs()`` is used instead of ``abs()`` on purpose: ``abs()`` is a *context*
    operation, so it silently rounds a 30-significant-digit amount to the default
    28-digit precision — which would push a valid amount over the bound and reject it.
    ``copy_abs()`` is exact.
    """
    if value.copy_abs() >= MAX_MONEY:
        raise MoneyError(f"{field} exceeds the maximum representable amount")
    return value
