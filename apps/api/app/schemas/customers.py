"""Customer schemas (API_CONTRACT §9.2, PART 10, PART 65).

PII is deliberately minimal: name, phone, address, notes and **the last four digits** of
a national id. Nothing else about identity is stored, and no field here is optional in a
way that would let a customer exist without a name.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.masterdata import CUSTOMER_CODE_PATTERN


def _strip(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class CustomerCreateRequest(BaseModel):
    """``POST /api/v1/customers`` body — ``customer_code`` is issued when omitted."""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)
    national_id_last4: str | None = Field(
        default=None, pattern=r"^[0-9]{4}$", description="Last four digits only (PII minimisation)"
    )
    branch_id: uuid.UUID | None = Field(
        default=None, description="Branch the customer belongs to; omit for a shared customer"
    )
    customer_code: str | None = Field(
        default=None,
        pattern=CUSTOMER_CODE_PATTERN,
        description="Issued automatically as PREFIX-YYYYMMDD-NNNNNN when omitted",
    )
    is_active: bool = True

    @field_validator("full_name", mode="before")
    @classmethod
    def _name(cls, value: object) -> object:
        return " ".join(str(value).split()) if isinstance(value, str) else value

    @field_validator("phone", "address", "notes", "national_id_last4", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        return _strip(value) if isinstance(value, str) else value

    @field_validator("customer_code", mode="before")
    @classmethod
    def _clean_code(cls, value: object) -> object:
        """Normalise case *before* the pattern runs: ``cus-2026…`` and ``CUS-2026…`` are
        the same code, and the stored form is always uppercase (the database agrees)."""
        stripped = _strip(value) if isinstance(value, str) else value
        return stripped.upper() if isinstance(stripped, str) else stripped


class CustomerUpdateRequest(BaseModel):
    """``PATCH /api/v1/customers/{id}`` body — every field optional."""

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=2, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    address: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)
    national_id_last4: str | None = Field(default=None, pattern=r"^[0-9]{4}$")
    branch_id: uuid.UUID | None = None
    is_active: bool | None = Field(
        default=None, description="false deactivates the customer; the row is never deleted"
    )

    @field_validator("full_name", mode="before")
    @classmethod
    def _name(cls, value: object) -> object:
        if value is None:
            return None
        return " ".join(str(value).split()) if isinstance(value, str) else value

    @field_validator("phone", "address", "notes", "national_id_last4", mode="before")
    @classmethod
    def _clean(cls, value: object) -> object:
        return _strip(value) if isinstance(value, str) else value


class CustomerResponse(BaseModel):
    """One customer."""

    id: uuid.UUID
    customer_code: str
    full_name: str
    phone: str | None = None
    address: str | None = None
    notes: str | None = None
    national_id_last4: str | None = None
    branch_id: uuid.UUID | None = None
    is_active: bool
    created_at: dt.datetime
    updated_at: dt.datetime
    created_by: uuid.UUID | None = None
    updated_by: uuid.UUID | None = None


class CustomerListResponse(BaseModel):
    """Envelope for customer search."""

    items: list[CustomerResponse]
    total: int
    limit: int
    offset: int
