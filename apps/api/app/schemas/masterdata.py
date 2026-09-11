"""Currency, branch and account schemas (API_CONTRACT sections 9.1 and 9.2).

The immutability rules of the schema are enforced in three places and stated here so a
client can see them without reading the DDL:

* ``currencies.code`` — never editable after creation (the frozen DDL raises ``NEX06``);
  the field is therefore absent from every update model, not merely optional.
* ``branches.code`` — editable only while the branch has no financial history.
* ``accounts.code``/``account_type`` — editable only while the account has no journal
  lines, and ``normal_balance`` is derived from ``account_type``, never supplied.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

# The database enforces the same patterns (``ck_currencies_code_format``,
# ``ck_branches_code_format``). Duplicating them in the schema turns a 500-class
# constraint violation into a 422 with the offending field named.
CURRENCY_CODE_PATTERN = r"^[A-Z]{3,10}$"
BRANCH_CODE_PATTERN = r"^[A-Z0-9][A-Z0-9-]{1,19}$"
ACCOUNT_CODE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{1,49}$"
CUSTOMER_CODE_PATTERN = r"^[A-Z0-9-]{3,50}$"
TIMEZONE_PATTERN = r"^[A-Za-z_]+/[A-Za-z_+-]+$"

ACCOUNT_TYPES = ("ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE")
NORMAL_BALANCES = ("DEBIT", "CREDIT")


def _upper(value: str) -> str:
    return value.strip().upper()


# ------------------------------------------------------------------ currencies
class CurrencyCreateRequest(BaseModel):
    """``POST /api/v1/currencies`` body."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(
        pattern=CURRENCY_CODE_PATTERN, description="ISO-4217-style code, 3-10 letters"
    )
    name: str = Field(min_length=1, max_length=100)
    symbol: str | None = Field(default=None, max_length=20)
    decimal_places: int = Field(default=2, ge=0, le=6)
    is_base: bool = Field(default=False, description="Exactly one currency may be the base")
    is_active: bool = Field(default=True)
    is_tradable: bool = Field(default=True)
    display_order: int = Field(default=0, ge=-32768, le=32767)

    @field_validator("code", mode="before")
    @classmethod
    def _normalise_code(cls, value: object) -> object:
        return _upper(str(value)) if isinstance(value, str) else value


class CurrencyUpdateRequest(BaseModel):
    """``PATCH /api/v1/currencies/{id}`` body.

    ``code`` is deliberately **not** a field of this model: the schema forbids changing
    it, so the API cannot accept it and answer with a confusing 500 from the trigger.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    symbol: str | None = Field(default=None, max_length=20)
    decimal_places: int | None = Field(default=None, ge=0, le=6)
    is_base: bool | None = None
    is_active: bool | None = None
    is_tradable: bool | None = None
    display_order: int | None = Field(default=None, ge=-32768, le=32767)


class CurrencyResponse(BaseModel):
    """One currency."""

    id: uuid.UUID
    code: str
    name: str
    symbol: str | None = None
    decimal_places: int
    is_base: bool
    is_active: bool
    is_tradable: bool
    display_order: int
    created_at: dt.datetime


class CurrencyListResponse(BaseModel):
    """Envelope for currency lists."""

    items: list[CurrencyResponse]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------- branches
class BranchCreateRequest(BaseModel):
    """``POST /api/v1/branches`` body."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=BRANCH_CODE_PATTERN)
    name: str = Field(min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=2000)
    phone: str | None = Field(default=None, max_length=50)
    is_active: bool = True
    timezone: str = Field(default="Asia/Kabul", pattern=TIMEZONE_PATTERN, max_length=64)

    @field_validator("code", mode="before")
    @classmethod
    def _normalise_code(cls, value: object) -> object:
        return _upper(str(value)) if isinstance(value, str) else value


class BranchUpdateRequest(BaseModel):
    """``PATCH /api/v1/branches/{id}`` body — ``code`` only while there is no history."""

    model_config = ConfigDict(extra="forbid")

    code: str | None = Field(default=None, pattern=BRANCH_CODE_PATTERN)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    address: str | None = Field(default=None, max_length=2000)
    phone: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None
    timezone: str | None = Field(default=None, pattern=TIMEZONE_PATTERN, max_length=64)

    @field_validator("code", mode="before")
    @classmethod
    def _normalise_code(cls, value: object) -> object:
        return _upper(str(value)) if isinstance(value, str) else value


class BranchResponse(BaseModel):
    """One branch."""

    id: uuid.UUID
    code: str
    name: str
    address: str | None = None
    phone: str | None = None
    is_active: bool
    timezone: str
    created_at: dt.datetime


class BranchListResponse(BaseModel):
    """Envelope for branch lists."""

    items: list[BranchResponse]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------- chart of accounts
class AccountCreateRequest(BaseModel):
    """``POST /api/v1/accounts`` body — ``normal_balance`` is derived, never supplied."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=ACCOUNT_CODE_PATTERN, max_length=50)
    name: str = Field(min_length=1, max_length=200)
    account_type: str = Field(description=f"One of {', '.join(ACCOUNT_TYPES)}")
    currency_id: uuid.UUID | None = None
    branch_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    is_active: bool = True
    is_postable: bool = Field(
        default=True, description="False for header accounts that only group a subtree"
    )

    @field_validator("account_type", mode="before")
    @classmethod
    def _type(cls, value: object) -> object:
        return _upper(str(value)) if isinstance(value, str) else value

    @field_validator("account_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in ACCOUNT_TYPES:
            raise ValueError(f"account_type must be one of {', '.join(ACCOUNT_TYPES)}")
        return value


class AccountUpdateRequest(BaseModel):
    """``PATCH /api/v1/accounts/{id}`` body.

    ``code`` and ``account_type`` stay editable only until the account carries journal
    lines; the service refuses the change afterwards (``IMMUTABLE_FIELD``) rather than
    letting the ledger's meaning drift under history.
    """

    model_config = ConfigDict(extra="forbid")

    code: str | None = Field(default=None, pattern=ACCOUNT_CODE_PATTERN, max_length=50)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_id: uuid.UUID | None = None
    account_type: str | None = None
    currency_id: uuid.UUID | None = None
    is_active: bool | None = None
    is_postable: bool | None = None

    @field_validator("account_type", mode="before")
    @classmethod
    def _type(cls, value: object) -> object:
        if value is None:
            return None
        upper = _upper(str(value))
        if upper not in ACCOUNT_TYPES:
            raise ValueError(f"account_type must be one of {', '.join(ACCOUNT_TYPES)}")
        return upper


class AccountResponse(BaseModel):
    """One ledger account."""

    id: uuid.UUID
    code: str
    name: str
    account_type: str
    normal_balance: str | None = Field(
        default=None, description="Derived from account_type: ASSET/EXPENSE → DEBIT, others CREDIT"
    )
    currency_id: uuid.UUID | None = None
    currency_code: str | None = None
    branch_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    is_active: bool
    is_postable: bool
    has_children: bool = False
    created_at: dt.datetime
    created_by: uuid.UUID | None = None


class AccountListResponse(BaseModel):
    """Envelope for chart-of-accounts queries."""

    items: list[AccountResponse]
    total: int
    limit: int
    offset: int
