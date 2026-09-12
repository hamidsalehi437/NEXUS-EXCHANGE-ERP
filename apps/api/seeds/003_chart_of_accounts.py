"""Seed the chart of accounts (PART 45, ACCOUNTING_MODEL.md §5).

The chart is built from the approved accounting model:

* group/parent accounts (``1``…``6``) with ``is_postable = FALSE`` for reporting
  structure only;
* one **inventory (cash) account per active currency**: the base currency takes
  ``1000`` and other currencies take ``1001``, ``1002``, … in ``display_order``,
  so the code assigned to a currency is stable across re-seeds;
* control accounts (transit, receivable, customer advance, transfer payable per
  currency), equity, revenue and expense accounts.

``normal_balance`` is derived from ``account_type`` (never entered by hand), which
is what the reporting views rely on to present balances with the right sign.

Branch scoping: these are group-level accounts (``branch_id IS NULL``). Per-branch
inventory accounts are created when a branch is created, as documented in
ACCOUNTING_MODEL.md §10.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.models.account import Account
from app.models.currency import Currency
from seeds.base import SeedContext, SeedCounts, sync_rows

# account_type → normal balance (derived, never entered manually)
NORMAL_BALANCE: dict[str, str] = {
    "ASSET": "DEBIT",
    "EXPENSE": "DEBIT",
    "LIABILITY": "CREDIT",
    "EQUITY": "CREDIT",
    "REVENUE": "CREDIT",
}

# code, name, account_type, parent_code, is_postable
STRUCTURAL_ACCOUNTS: tuple[tuple[str, str, str, str | None, bool], ...] = (
    ("1", "ASSETS", "ASSET", None, False),
    ("2", "LIABILITIES", "LIABILITY", None, False),
    ("3", "EQUITY", "EQUITY", None, False),
    ("4", "REVENUE", "REVENUE", None, False),
    ("5", "EXPENSES", "EXPENSE", None, False),
    ("6", "OPENING BALANCES", "EQUITY", None, False),
)

FIXED_ACCOUNTS: tuple[tuple[str, str, str, str], ...] = (
    # code, name, account_type, parent_code
    ("1100", "Cash in Transit / Partner Receivable", "ASSET", "1"),
    ("1200", "Customer Receivable", "ASSET", "1"),
    ("2000", "Customer Advance (unearned)", "LIABILITY", "2"),
    ("3000", "Owner Capital", "EQUITY", "3"),
    ("3100", "Owner Drawings", "EQUITY", "3"),
    ("3200", "Retained Earnings", "EQUITY", "3"),
    ("4000", "FX Gain / Loss", "REVENUE", "4"),
    ("4010", "Commission Income - Exchange", "REVENUE", "4"),
    ("4020", "Transfer Fee Income", "REVENUE", "4"),
    ("4030", "Other Income", "REVENUE", "4"),
    ("5000", "Salaries and Wages", "EXPENSE", "5"),
    ("5010", "Rent", "EXPENSE", "5"),
    ("5020", "Utilities", "EXPENSE", "5"),
    ("5030", "Communication and Internet", "EXPENSE", "5"),
    ("5040", "Office Supplies and Consumables", "EXPENSE", "5"),
    ("5050", "Bank Charges", "EXPENSE", "5"),
    ("5060", "Government Fees and Licences", "EXPENSE", "5"),
    ("5070", "Transport and Fuel", "EXPENSE", "5"),
    ("5080", "Maintenance and Repairs", "EXPENSE", "5"),
    ("5090", "Cash Short / Over", "EXPENSE", "5"),
    ("5100", "Other Expenses", "EXPENSE", "5"),
    ("6000", "Opening Balance Offset", "EQUITY", "6"),
)

BASE_CASH_ACCOUNT_CODE = "1000"
CASH_ACCOUNT_CODE_START = 1001
BASE_TRANSFER_PAYABLE_CODE = "2100"
TRANSFER_PAYABLE_CODE_START = 2101


def _currency_driven_rows(currencies: list[Currency]) -> list[dict[str, Any]]:
    """Cash inventory and transfer-payable accounts, one per currency."""
    rows: list[dict[str, Any]] = []
    ordered = sorted(currencies, key=lambda currency: (currency.display_order, currency.code))

    cash_index = 0
    payable_index = 0
    for currency in ordered:
        if currency.is_base:
            cash_code = BASE_CASH_ACCOUNT_CODE
            payable_code = BASE_TRANSFER_PAYABLE_CODE
        else:
            cash_code = str(CASH_ACCOUNT_CODE_START + cash_index)
            payable_code = str(TRANSFER_PAYABLE_CODE_START + payable_index)
            cash_index += 1
            payable_index += 1

        rows.append(
            {
                "code": cash_code,
                "name": f"Cash {currency.code}",
                "account_type": "ASSET",
                "parent_code": "1",
                "currency_code": currency.code,
            }
        )
        rows.append(
            {
                "code": payable_code,
                "name": f"Transfer Payable {currency.code}",
                "account_type": "LIABILITY",
                "parent_code": "2",
                "currency_code": currency.code,
            }
        )

    return rows


MANAGED_FIELDS: tuple[str, ...] = (
    "name",
    "account_type",
    "normal_balance",
    "is_postable",
    "currency_id",
    "parent_id",
)


def _with_ownership(row: dict[str, Any]) -> dict[str, Any]:
    """Add the columns a freshly inserted row needs but the seed never updates."""
    return {**row, "is_active": True, "created_by": None}


def _resolve(
    rows: list[dict[str, Any]],
    *,
    existing_by_code: dict[str, Account],
    currency_by_code: dict[str, Currency],
) -> list[dict[str, Any]]:
    """Turn ``parent_code``/``currency_code`` references into keys.

    Parents are created earlier in this same run, so they must be present: a missing
    parent means the seed order is broken and is a hard error. ``--check`` is a real
    run that is rolled back, so it resolves parents exactly like a real run.
    """
    resolved: list[dict[str, Any]] = []
    for row in rows:
        parent_code = row["parent_code"]
        currency_code = row["currency_code"]
        parent = existing_by_code.get(parent_code) if parent_code else None
        currency = currency_by_code.get(currency_code) if currency_code else None

        if parent_code and parent is None:
            raise RuntimeError(f"parent account {parent_code} is missing")

        resolved.append(
            _with_ownership(
                {
                    **{key: value for key, value in row.items() if not key.endswith("_code")},
                    "code": row["code"],
                    "parent_id": parent.id if parent else None,
                    "currency_id": currency.id if currency else None,
                }
            )
        )
    return resolved


def run(context: SeedContext) -> SeedCounts:
    """Idempotently upsert the chart of accounts."""
    session = context.session
    currencies = list(session.execute(select(Currency)).scalars().all())
    if not currencies:
        raise RuntimeError(
            "no currencies found: run the currencies seed before the chart of accounts"
        )
    currency_by_code = {currency.code: currency for currency in currencies}

    structural = [
        {
            "code": code,
            "name": name,
            "account_type": account_type,
            "normal_balance": NORMAL_BALANCE[account_type],
            "parent_code": parent_code,
            "currency_code": None,
            "is_postable": is_postable,
        }
        for code, name, account_type, parent_code, is_postable in STRUCTURAL_ACCOUNTS
    ]

    # Parents first: the rest of the chart references them.
    existing_by_code = {
        account.code: account for account in session.execute(select(Account)).scalars().all()
    }
    counts = sync_rows(
        context,
        Account,
        natural_key=("code",),
        rows=_resolve(
            structural,
            existing_by_code=existing_by_code,
            currency_by_code=currency_by_code,
        ),
        managed_fields=MANAGED_FIELDS,
    )
    # Flush so the structural accounts exist before the children reference them.
    session.flush()

    existing_by_code = {
        account.code: account for account in session.execute(select(Account)).scalars().all()
    }
    base_currency_code = _base_currency_code(currencies)
    children = [
        {
            "code": code,
            "name": name,
            "account_type": account_type,
            "normal_balance": NORMAL_BALANCE[account_type],
            "parent_code": parent_code,
            "currency_code": base_currency_code,
            "is_postable": True,
        }
        for code, name, account_type, parent_code in FIXED_ACCOUNTS
    ] + [
        {
            **row,
            "normal_balance": NORMAL_BALANCE[row["account_type"]],
            "is_postable": True,
        }
        for row in _currency_driven_rows(currencies)
    ]
    counts.merge(
        sync_rows(
            context,
            Account,
            natural_key=("code",),
            rows=_resolve(
                children,
                existing_by_code=existing_by_code,
                currency_by_code=currency_by_code,
            ),
            managed_fields=MANAGED_FIELDS,
        )
    )

    changed = counts.changed
    if changed:
        context.record_audit(
            action="SEED_CHART_OF_ACCOUNTS_APPLIED",
            entity_type="account",
            details={
                "accounts": len(structural) + len(children),
                "currencies": len(currencies),
                **counts.as_dict(),
            },
        )

    return counts


def _base_currency_code(currencies: list[Currency]) -> str:
    for currency in currencies:
        if currency.is_base:
            return currency.code
    raise RuntimeError("no base currency is configured; run the currencies seed first")
