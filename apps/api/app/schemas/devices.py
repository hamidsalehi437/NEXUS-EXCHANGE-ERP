"""Device schemas (API_CONTRACT §9.1)."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.auth import DEVICE_PLATFORMS


class DeviceRegisterRequest(BaseModel):
    """``POST /api/v1/devices/register`` body."""

    model_config = ConfigDict(extra="forbid")

    device_uuid: uuid.UUID = Field(description="Stable installation identifier")
    device_name: str = Field(min_length=1, max_length=200)
    platform: str = Field(description=f"One of {', '.join(DEVICE_PLATFORMS)}")
    branch_id: uuid.UUID
    app_version: str | None = Field(default=None, max_length=50)


class DeviceRevokeRequest(BaseModel):
    """``POST /api/v1/devices/{id}/revoke`` body."""

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)


class DeviceResponse(BaseModel):
    """A registered device."""

    id: uuid.UUID
    device_uuid: uuid.UUID
    device_name: str
    platform: str
    branch_id: uuid.UUID
    is_active: bool
    app_version: str | None = None
    registered_by: uuid.UUID | None = None
    last_seen_at: dt.datetime | None = None
    last_sync_at: dt.datetime | None = None
    created_at: dt.datetime
    revoked_at: dt.datetime | None = None
    revoked_by: uuid.UUID | None = None
    revoke_reason: str | None = None


class DeviceListResponse(BaseModel):
    """``GET /api/v1/devices`` response."""

    items: list[DeviceResponse]
    total: int
    limit: int
    offset: int


class DeviceRevokeResponse(BaseModel):
    """Outcome of a device revocation."""

    device: DeviceResponse
    revoked_sessions: int
    already_revoked: bool
