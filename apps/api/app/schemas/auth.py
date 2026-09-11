"""Authentication request/response schemas (API_CONTRACT §2).

Rules applied to every model here:

* request models reject unknown fields (``extra="forbid"``) so a typo in a client
  payload is a 422 instead of a silently ignored security field;
* password fields are ``SecretStr`` — a validation error or a debug dump of the model
  prints ``**********`` rather than the credential, and the value is only unwrapped at
  the moment it is hashed or verified;
* response models contain no hash, no token hash and no internal column that is not part
  of the published contract.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

# Mirrors ck_users_username_format and ck_devices_platform so the API rejects what the
# database would reject, with a field-level message instead of a 422 from PostgreSQL.
USERNAME_PATTERN = r"^[A-Za-z0-9._-]{3,100}$"
DEVICE_PLATFORMS = ("ANDROID", "WINDOWS", "WEB", "IOS")


class LoginRequest(BaseModel):
    """``POST /api/v1/auth/login`` body."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=100, pattern=USERNAME_PATTERN)
    password: SecretStr = Field(min_length=1, max_length=128)
    device_uuid: uuid.UUID = Field(description="Stable installation identifier")
    device_name: str = Field(min_length=1, max_length=200)
    platform: str = Field(description="One of ANDROID, WINDOWS, WEB, IOS")
    app_version: str | None = Field(default=None, max_length=50)
    branch_id: uuid.UUID | None = Field(
        default=None,
        description="Required only when the device is new and several branches exist",
    )

    @field_validator("platform")
    @classmethod
    def _validate_platform(cls, value: str) -> str:
        upper = value.strip().upper()
        if upper not in DEVICE_PLATFORMS:
            raise ValueError(f"platform must be one of {', '.join(DEVICE_PLATFORMS)}")
        return upper


class RefreshRequest(BaseModel):
    """``POST /api/v1/auth/refresh`` body."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: SecretStr = Field(min_length=20, max_length=200)
    device_uuid: uuid.UUID | None = Field(
        default=None, description="Optional; when present it must match the session's device"
    )


class LogoutRequest(BaseModel):
    """``POST /api/v1/auth/logout`` body."""

    model_config = ConfigDict(extra="forbid")

    all_devices: bool = Field(
        default=False, description="Revoke every session of this account, not only this one"
    )


class PasswordChangeRequest(BaseModel):
    """``POST /api/v1/auth/password`` body."""

    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=128)
    new_password: SecretStr = Field(min_length=12, max_length=128)


class SessionUser(BaseModel):
    """The ``user`` object of a login response."""

    id: uuid.UUID
    username: str
    full_name: str
    email: str | None = None
    roles: list[str]
    permissions: list[str]
    must_change_password: bool = False


class SessionDevice(BaseModel):
    """The ``device`` object of a login response."""

    id: uuid.UUID
    device_uuid: uuid.UUID
    device_name: str
    platform: str
    branch_id: uuid.UUID
    is_new_registration: bool = False


class TokenPairResponse(BaseModel):
    """Response of ``POST /auth/login`` and ``POST /auth/refresh`` (API_CONTRACT §2.1)."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - a scheme name, not a credential
    expires_in: int = Field(description="Access-token lifetime in seconds")
    refresh_expires_in: int = Field(description="Refresh-token lifetime in seconds")
    session_id: uuid.UUID = Field(description="Refresh-token family id (this session)")
    user: SessionUser
    device: SessionDevice
    roles: list[str]
    permissions: list[str]
    must_change_password: bool = False


class SessionInfo(BaseModel):
    """One entry of ``GET /auth/sessions``."""

    session_id: uuid.UUID
    device_id: uuid.UUID | None = None
    device_name: str | None = None
    platform: str | None = None
    branch_id: uuid.UUID | None = None
    branch_code: str | None = None
    issued_at: dt.datetime
    expires_at: dt.datetime
    last_used_at: dt.datetime | None = None
    ip_address: str | None = None
    is_current: bool = False


class SessionListResponse(BaseModel):
    """``GET /auth/sessions`` response."""

    items: list[SessionInfo]
    total: int


class LogoutResponse(BaseModel):
    """``POST /auth/logout`` response."""

    scope: str
    revoked_sessions: int
    session_id: uuid.UUID


class PasswordChangeResponse(BaseModel):
    """``POST /auth/password`` response."""

    password_changed_at: dt.datetime
    revoked_sessions: int


class IdentityResponse(BaseModel):
    """``GET /auth/me`` response: who am I, through which device, with what authority."""

    user: SessionUser
    device: SessionDevice | None = None
    session_id: uuid.UUID
    session_expires_at: dt.datetime
    issued_at: dt.datetime
