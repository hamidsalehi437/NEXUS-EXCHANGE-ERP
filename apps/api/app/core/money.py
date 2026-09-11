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

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext

MONEY_PRECISION = 30
MONEY_SCALE = 10
MONEY_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)  # 1E-10

# Working precision for arithmetic on stored values.
#
# ``decimal`` arithmetic is a *context* operation: with the Python default (28 significant
# digits) an operation whose result needs more than 28 digits is silently rounded, or — for
# ``quantize`` — refused with ``InvalidOperation``. ``NUMERIC(30,10)`` holds 30 significant
# digits, and a product of two stored values needs up to 60 before it is rounded once to
# the stored scale. Every operation in this module therefore runs inside
# :func:`money_context`, and the accumulated rounding is decided there instead of by
# whatever context the caller happened to have.
MONEY_CONTEXT_PRECISION = MONEY_PRECISION + 10  # 40 digits: operand room plus the result


@contextmanager
def money_context() -> Iterator[None]:
    """Decimal context with room for every value ``NUMERIC(30,10)`` can hold.

    Half-up rounding is the documented policy at the stored scale, so it is also the
    context default here; individual operations still state their rounding explicitly.
    """
    with localcontext() as context:
        context.prec = MONEY_CONTEXT_PRECISION
        context.rounding = ROUND_HALF_UP
        yield


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


def _quantum(scale: int) -> Decimal:
    """The stored-scale quantum for ``scale`` decimal places, validated."""
    if scale < 0 or scale > MONEY_SCALE:
        raise MoneyError(f"scale must be between 0 and {MONEY_SCALE}")
    return Decimal(1).scaleb(-scale)


def _quantized(value: Decimal, scale: int) -> Decimal:
    """One rounding to the stored scale, in the wide money context."""
    with money_context():
        return value.quantize(_quantum(scale), rounding=ROUND_HALF_UP)


def quantize_money(value: Decimal, decimal_places: int = 2) -> Decimal:
    """Round to the currency's decimal places using ``ROUND_HALF_UP``.

    Only *final* amounts are rounded; intermediate arithmetic between ledger legs
    must stay unrounded so that ``SUM(debit) = SUM(credit)`` holds exactly.
    """
    if decimal_places < 0 or decimal_places > MONEY_SCALE:
        raise MoneyError(f"decimal_places must be between 0 and {MONEY_SCALE}")
    return _quantized(value, decimal_places)


def format_decimal(value: Decimal, scale: int = MONEY_SCALE) -> str:
    """Canonical wire format: fixed-point string with exactly ``scale`` decimals.

    >>> format_decimal(Decimal('70000'))
    '70000.0000000000'
    >>> format_decimal(Decimal('3E-10'))
    '0.0000000003'

    ``format(..., 'f')`` rather than ``str()``: ``str(Decimal('3E-10'))`` is ``3E-10``, and
    the contract promises every monetary field is a plain fixed-point string at the stored
    scale (API_CONTRACT §2). A client that re-serialised the exponent form, or compared it
    as text, would see a different value than the one that was posted.
    """
    return format(_quantized(value, scale), "f")


def has_money_scale(value: Decimal, *, scale: int = MONEY_SCALE) -> bool:
    """Whether ``value`` is already exactly representable at ``scale``.

    ``Decimal.quantize`` refuses (``InvalidOperation``) when the result needs more digits
    than the ambient context allows, so the check runs in the wide context too: the
    question is about the *stored* column, not about the caller's precision.
    """
    with money_context():
        return value == value.quantize(_quantum(scale), rounding=ROUND_HALF_UP)


def money_sum(values: Iterable[Decimal]) -> Decimal:
    """Exact sum of stored values — no rounding, no scale change.

    Addition of 10-decimal values is exact as long as the running total is not rounded, so
    this is the only correct way to add up journal lines: ``sum()`` under the default
    context would round a 30-digit total to 28 digits, which is precisely how a ledger
    stops balancing in the last digit.
    """
    with money_context():
        return sum(values, Decimal(0))


def money_difference(left: Decimal, right: Decimal, *, scale: int = MONEY_SCALE) -> Decimal:
    """``left - right`` rounded once to the stored scale."""
    with money_context():
        return (left - right).quantize(_quantum(scale), rounding=ROUND_HALF_UP)


def multiply_money(left: Decimal, right: Decimal, *, scale: int = MONEY_SCALE) -> Decimal:
    """``left * right`` rounded once to the stored scale, inside the wide context.

    Multiply and round in one call so there is no intermediate rounding step to double up
    with the final one: ``(a * b)`` under the default context is already a rounded value
    before anyone gets to quantize it.
    """
    with money_context():
        return (left * right).quantize(_quantum(scale), rounding=ROUND_HALF_UP)


def divide_money(numerator: Decimal, denominator: Decimal, *, scale: int = MONEY_SCALE) -> Decimal:
    """``numerator / denominator`` rounded once to the stored scale (a rate or a price)."""
    if denominator == 0:
        raise MoneyError("division by zero is not a money operation")
    with money_context():
        return (numerator / denominator).quantize(_quantum(scale), rounding=ROUND_HALF_UP)


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
