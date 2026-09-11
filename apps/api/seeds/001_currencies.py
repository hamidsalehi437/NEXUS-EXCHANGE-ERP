"""Seed currencies (PART 45).

The seven currencies named by the master prompt. The base currency is the one
configured as ``BASE_CURRENCY_CODE`` (default ``AFN``); the database enforces that
exactly one currency carries ``is_base``.

Currencies are never deleted and their ``code`` is immutable (database trigger):
deactivating is the operational path.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings
from app.models.currency import Currency
from seeds.base import SeedContext, SeedCounts, sync_rows

# code, name, symbol, decimal_places, display_order
CURRENCIES: tuple[tuple[str, str, str, int, int], ...] = (
    ("AFN", "Afghan Afghani", "؋", 2, 0),
    ("USD", "US Dollar", "$", 2, 1),
    ("EUR", "Euro", "€", 2, 2),
    ("PKR", "Pakistani Rupee", "₨", 2, 3),
    ("IRR", "Iranian Rial", "﷼", 2, 4),
    ("AED", "UAE Dirham", "د.إ", 2, 5),
    ("SAR", "Saudi Riyal", "﷼", 2, 6),
)


def desired_rows(settings: Settings) -> list[dict[str, Any]]:
    base_code = settings.base_currency_code.upper()
    known_codes = {code for code, *_ in CURRENCIES}
    if base_code not in known_codes:
        raise ValueError(
            f"BASE_CURRENCY_CODE={base_code!r} is not one of the seeded currencies "
            f"({', '.join(sorted(known_codes))})"
        )

    return [
        {
            "code": code,
            "name": name,
            "symbol": symbol,
            "decimal_places": decimal_places,
            "is_base": code == base_code,
            "is_active": True,
            "is_tradable": True,
            "display_order": display_order,
        }
        for code, name, symbol, decimal_places, display_order in CURRENCIES
    ]


def run(context: SeedContext) -> SeedCounts:
    """Idempotently upsert the currency catalogue."""
    settings = context.settings
    counts = sync_rows(
        context,
        Currency,
        natural_key=("code",),
        rows=desired_rows(settings),
        managed_fields=(
            "name",
            "symbol",
            "decimal_places",
            "is_base",
            "display_order",
        ),
    )

    # A currency that was previously the base must not stay the base if the
    # configuration moved: the partial unique index allows only one.
    base_code = settings.base_currency_code.upper()
    for currency in context.session.query(Currency).all():
        if currency.code != base_code and currency.is_base:
            currency.is_base = False
            counts.updated += 1

    if counts.changed:
        context.record_audit(
            action="SEED_CURRENCIES_APPLIED",
            entity_type="currency",
            details={"base_currency_code": base_code, **counts.as_dict()},
        )

    return counts
