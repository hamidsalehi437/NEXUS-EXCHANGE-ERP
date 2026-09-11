"""Device queries (PART 8, PART 42).

A device row is the anchor of the whole session model: refresh tokens are bound to a
device, revocation is a device operation, and every audit row carries the device that
produced the action.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Branch
from app.models.device import Device


class DeviceRepository:
    """Reads and writes for ``devices``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_device(self, device_id: uuid.UUID, *, for_update: bool = False) -> Device | None:
        statement = select(Device).where(Device.id == device_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_uuid(
        self, device_uuid: uuid.UUID, *, for_update: bool = False
    ) -> Device | None:
        statement = select(Device).where(Device.device_uuid == device_uuid)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_devices(
        self,
        *,
        branch_id: uuid.UUID | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Device], int]:
        statement = select(Device).order_by(Device.created_at.desc())
        if branch_id is not None:
            statement = statement.where(Device.branch_id == branch_id)
        if is_active is not None:
            statement = statement.where(Device.is_active.is_(is_active))
        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.execute(statement.limit(limit).offset(offset))
        return list(rows.scalars().all()), int(total or 0)

    async def branch_of(self, device_id: uuid.UUID) -> Branch | None:
        return (
            await self._session.execute(
                select(Branch)
                .join(Device, Device.branch_id == Branch.id)
                .where(Device.id == device_id)
            )
        ).scalar_one_or_none()

    def add(self, device: Device) -> Device:
        self._session.add(device)
        return device

    async def touch_last_seen(self, device_id: uuid.UUID, *, when: dt.datetime) -> None:
        """Record that the device was seen (used by login/refresh, not by every request)."""
        device = await self.get_device(device_id, for_update=True)
        if device is not None:
            device.last_seen_at = when
