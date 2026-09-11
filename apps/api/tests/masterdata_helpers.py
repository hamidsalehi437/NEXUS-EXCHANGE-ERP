"""Shared helpers for the Phase 3 master-data suites.

Everything goes through the real HTTP surface (``TestClient`` against the application's
own lifespan) or through the real database, so a passing test means the deployed process
would behave that way. The factories register their creations for cleanup by *deactivating*
them (never deleting: the schema forbids deleting master data, and the tests should be
unable to do what production cannot).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient

API = "/api/v1"
CURRENCIES = f"{API}/currencies"
BRANCHES = f"{API}/branches"
CUSTOMERS = f"{API}/customers"
ACCOUNTS = f"{API}/accounts"
RATES = f"{API}/rates"


# A currency code the seeded catalogue does not contain, so tests never depend on the
# seed's exact contents. The database pattern is ^[A-Z]{3,10}$; four random letters after
# the leading "T" give 456 976 combinations per run, which is why the suites never collide
# with each other or with the seven seeded codes.
def unique_currency_code() -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    drawn = [letters[(uuid.uuid4().int >> (5 * index)) % 26] for index in range(4)]
    return "T" + "".join(drawn)


def unique_branch_code() -> str:
    """Branch codes are ^[A-Z0-9][A-Z0-9-]{1,19}$ — no lowercase, no underscore."""
    return f"T{uuid.uuid4().hex[:8].upper()}"


def unique_account_code() -> str:
    return f"T{uuid.uuid4().hex[:8].upper()}"


def create_currency(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    code: str | None = None,
    name: str | None = None,
    decimal_places: int = 2,
    is_base: bool = False,
    is_tradable: bool = True,
    expect: int | None = 201,
) -> Any:
    """POST /currencies and (by default) assert success."""
    body: dict[str, Any] = {
        "code": code or unique_currency_code(),
        "name": name or f"Test currency {uuid.uuid4().hex[:6]}",
        "decimal_places": decimal_places,
        "is_base": is_base,
        "is_tradable": is_tradable,
    }
    response = client.post(CURRENCIES, json=body, headers=dict(headers))
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def create_branch(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    code: str | None = None,
    name: str | None = None,
    timezone: str = "Asia/Kabul",
    expect: int | None = 201,
) -> Any:
    """POST /branches and (by default) assert success."""
    body: dict[str, Any] = {
        "code": code or unique_branch_code(),
        "name": name or f"Test branch {uuid.uuid4().hex[:6]}",
        "timezone": timezone,
    }
    response = client.post(BRANCHES, json=body, headers=dict(headers))
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def create_customer(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    full_name: str | None = None,
    phone: str | None = None,
    branch_id: str | None = None,
    customer_code: str | None = None,
    national_id_last4: str | None = None,
    expect: int | None = 201,
) -> Any:
    """POST /customers and (by default) assert success."""
    body: dict[str, Any] = {"full_name": full_name or f"Customer {uuid.uuid4().hex[:8]}"}
    if phone is not None:
        body["phone"] = phone
    if branch_id is not None:
        body["branch_id"] = branch_id
    if customer_code is not None:
        body["customer_code"] = customer_code
    if national_id_last4 is not None:
        body["national_id_last4"] = national_id_last4
    response = client.post(CUSTOMERS, json=body, headers=dict(headers))
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def create_account(
    client: TestClient,
    headers: dict[str, str],
    *,
    code: str | None = None,
    name: str = "Test account",
    account_type: str = "ASSET",
    currency_id: str | None = None,
    branch_id: str | None = None,
    parent_id: str | None = None,
    is_active: bool = True,
    is_postable: bool = True,
    expect: int | None = 201,
):
    body: dict[str, object] = {
        "code": code or unique_account_code(),
        "name": name,
        "account_type": account_type,
        "is_active": is_active,
        "is_postable": is_postable,
    }
    if currency_id is not None:
        body["currency_id"] = currency_id
    if branch_id is not None:
        body["branch_id"] = branch_id
    if parent_id is not None:
        body["parent_id"] = parent_id
    response = client.post(ACCOUNTS, json=body, headers=headers)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def create_header(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str = "Header",
    account_type: str = "ASSET",
    **kwargs: object,
):
    """Create an account that is meant to group a subtree (``is_postable=false``).

    ACCOUNTING_MODEL.md: a parent account groups, it is not posted to. The API enforces
    that by refusing a child under a postable parent, so headers are created explicitly.
    """
    return create_account(
        client,
        headers,
        name=name,
        account_type=account_type,
        is_postable=False,
        **kwargs,  # type: ignore[arg-type]
    )


def publish_rate(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    from_currency_id: str,
    to_currency_id: str,
    buy_rate: str = "70.5000000000",
    sell_rate: str = "71.2500000000",
    branch_id: str | None = None,
    effective_at: str | None = None,
    source: str = "MANUAL",
    expect: int | None = 201,
) -> Any:
    """POST /rates and (by default) assert success."""
    body: dict[str, Any] = {
        "from_currency_id": from_currency_id,
        "to_currency_id": to_currency_id,
        "buy_rate": buy_rate,
        "sell_rate": sell_rate,
        "source": source,
    }
    if branch_id is not None:
        body["branch_id"] = branch_id
    if effective_at is not None:
        body["effective_at"] = effective_at
    response = client.post(RATES, json=body, headers=dict(headers))
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def currency_ids(client: TestClient, headers: Mapping[str, str], codes: Sequence[str]) -> list[str]:
    """Resolve seeded currency codes (AFN, USD, …) to their identifiers."""
    listing = client.get(CURRENCIES, params={"limit": 200}, headers=dict(headers))
    assert listing.status_code == 200, listing.text
    by_code = {row["code"]: row["id"] for row in listing.json()["items"]}
    missing = [code for code in codes if code not in by_code]
    assert not missing, f"seeded currencies missing: {missing}"
    return [by_code[code] for code in codes]


def as_decimal(value: object) -> Decimal:
    """Parse a wire value with :class:`Decimal` — the tests never use ``float`` either."""
    return Decimal(str(value))
