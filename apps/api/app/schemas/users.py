"""User, role and permission schemas (API_CONTRACT §9.1).

An account's hash never appears in an output model — the column is not modelled here at
all, so no future refactor can leak it by accident. ``must_change_password`` is exposed
because the client has to force the change; the credential itself never is.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.schemas.auth import USERNAME_PATTERN

# Deliberately permissive: the only authoritative test of an address is delivering
# mail to it, and the database stores it as plain VARCHAR(255). A stricter regex would
# reject valid addresses (plus-addressing, new TLDs) without adding security. Chosen over
# pydantic's EmailStr to avoid adding a runtime dependency the approved stack does not list.
EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$"
ROLE_NAME_PATTERN = r"^[A-Z_]{3,100}$"
PERMISSION_CODE_PATTERN = r"^[a-z_]+\.[a-z_]+$"


class UserCreateRequest(BaseModel):
    """``POST /api/v1/users`` body."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=100, pattern=USERNAME_PATTERN)
    password: SecretStr = Field(min_length=12, max_length=128)
    full_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=255, pattern=EMAIL_PATTERN)
    phone: str | None = Field(default=None, max_length=50)
    roles: list[str] = Field(
        default_factory=list,
        description=(
            "Role names; an empty list creates an account with no authority (deny by default)"
        ),
    )
    must_change_password: bool = Field(
        default=True, description="Force a password change at first login"
    )


class UserUpdateRequest(BaseModel):
    """``PATCH /api/v1/users/{id}`` body — every field optional."""

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=255, pattern=EMAIL_PATTERN)
    phone: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None
    must_change_password: bool | None = None
    roles: list[str] | None = Field(default=None, description="Replaces the role set when present")


class PermissionOverrideRequest(BaseModel):
    """One explicit grant or deny in ``PUT /users/{id}/permissions``."""

    model_config = ConfigDict(extra="forbid")

    permission_code: str = Field(pattern=PERMISSION_CODE_PATTERN, max_length=100)
    is_granted: bool = Field(description="false = explicit deny, which wins over any role grant")
    expires_at: dt.datetime | None = None
    reason: str | None = Field(default=None, max_length=500)


class PermissionOverridesRequest(BaseModel):
    """``PUT /api/v1/users/{id}/permissions`` body — replaces the whole override set."""

    model_config = ConfigDict(extra="forbid")

    overrides: list[PermissionOverrideRequest] = Field(default_factory=list, max_length=200)


class UserPermissionState(BaseModel):
    """One override as stored."""

    permission_code: str
    is_granted: bool
    expires_at: dt.datetime | None = None
    reason: str | None = None


class UserResponse(BaseModel):
    """A user as returned by the API (never includes the password hash)."""

    id: uuid.UUID
    username: str
    full_name: str
    email: str | None = None
    phone: str | None = None
    is_active: bool
    must_change_password: bool
    failed_login_attempts: int
    locked_until: dt.datetime | None = None
    last_login_at: dt.datetime | None = None
    password_changed_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    roles: list[str]
    permissions: list[str]
    overrides: list[UserPermissionState] = Field(default_factory=list)


class UserListResponse(BaseModel):
    """``GET /api/v1/users`` response (standard list envelope)."""

    items: list[UserResponse]
    total: int
    limit: int
    offset: int


class PermissionResponse(BaseModel):
    """A permission catalogue entry."""

    code: str
    description: str


class RoleResponse(BaseModel):
    """A role with its current grants."""

    id: uuid.UUID
    name: str
    description: str | None = None
    is_system: bool
    is_editable: bool = Field(description="False for system roles such as SUPER_ADMIN")
    permissions: list[str]


class RolePermissionUpdateRequest(BaseModel):
    """``POST /api/v1/roles/{id}/permissions`` body — replaces the role's grants."""

    model_config = ConfigDict(extra="forbid")

    permissions: list[str] = Field(default_factory=list, max_length=200)


class RoleListResponse(BaseModel):
    """``GET /api/v1/roles`` response."""

    items: list[RoleResponse]
    total: int


class PermissionListResponse(BaseModel):
    """``GET /api/v1/permissions`` response."""

    items: list[PermissionResponse]
    total: int
