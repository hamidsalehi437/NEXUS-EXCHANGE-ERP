"""Device registration and revocation (PART 8, PART 42).

A device is the unit of trust a counter is bound to: sessions belong to it, sync
pushes are attributed to it, and revoking it must take effect immediately. Revocation
therefore does three things in one transaction — flags the row, ends every live session
of that device, and writes an audit entry — because a "revoked" device that keeps a
working refresh token would be the worst of both worlds.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.audit_actions import AuditAction
from app.core.database import Database
from app.core.exceptions import DuplicateResourceError, ResourceNotFoundError, ValidationError
from app.models.branch import Branch
from app.models.device import Device
from app.repositories.devices import DeviceRepository
from app.repositories.sessions import REASON_DEVICE_REVOKED, SessionRepository
from app.repositories.users import BranchRepository
from app.services.audit_service import ActorContext, AuditService

# Platforms the schema accepts (mirrors ck_devices_platform in the approved schema).
ALLOWED_PLATFORMS = frozenset({"ANDROID", "WINDOWS", "WEB", "IOS"})


@dataclass(frozen=True, slots=True)
class DeviceRegistration:
    """A registered device plus the branch it belongs to."""

    device: Device
    branch: Branch


@dataclass(frozen=True, slots=True)
class DeviceRevocation:
    """Outcome of a revocation: how many sessions the device lost."""

    device: Device
    revoked_sessions: int
    already_revoked: bool


class DeviceService:
    """Register devices to branches and revoke them."""

    def __init__(self, *, database: Database) -> None:
        self._database = database

    @staticmethod
    def _now() -> dt.datetime:
        return dt.datetime.now(tz=dt.UTC)

    async def register_device(
        self,
        *,
        device_uuid: uuid.UUID,
        device_name: str,
        platform: str,
        branch_id: uuid.UUID,
        app_version: str | None,
        actor: ActorContext,
    ) -> DeviceRegistration:
        """Provision a device for a branch (the setup path used by an administrator).

        Distinct from the self-service registration at login: this one is explicit,
        audited as ``registered_by`` the acting administrator, and refuses a device that
        already exists instead of re-binding it to another branch.
        """
        platform_code = platform.strip().upper()
        if platform_code not in ALLOWED_PLATFORMS:
            raise ValidationError(
                "Unsupported platform.",
                details={
                    "fields": [
                        {
                            "field": "platform",
                            "code": "unsupported",
                            "message": f"one of {sorted(ALLOWED_PLATFORMS)}",
                        }
                    ]
                },
            )

        async with self._database.transaction() as session:
            devices = DeviceRepository(session)
            branches = BranchRepository(session)
            audit = AuditService(session)

            existing = await devices.get_by_uuid(device_uuid)
            if existing is not None:
                raise DuplicateResourceError(
                    "A device with this identifier is already registered.",
                    details={
                        "device_id": str(existing.id),
                        "branch_id": str(existing.branch_id),
                        "is_active": existing.is_active,
                    },
                )

            branch = await branches.get_branch(branch_id)
            if branch is None:
                raise ValidationError(
                    "The requested branch does not exist.",
                    details={"fields": [{"field": "branch_id", "code": "not_found"}]},
                )
            if not branch.is_active:
                raise ValidationError(
                    "The requested branch is not active.",
                    details={"fields": [{"field": "branch_id", "code": "inactive"}]},
                )

            device = Device(
                branch_id=branch.id,
                device_uuid=device_uuid,
                device_name=device_name.strip(),
                platform=platform_code,
                app_version=app_version,
                registered_by=actor.user_id,
                last_seen_at=None,
                is_active=True,
            )
            devices.add(device)
            await session.flush()

            audit.record(
                action=AuditAction.DEVICE_REGISTERED,
                entity_type="device",
                entity_id=device.id,
                new_data={
                    "device_uuid": str(device.device_uuid),
                    "device_name": device.device_name,
                    "platform": device.platform,
                    "branch_id": str(branch.id),
                    "branch_code": branch.code,
                    "registration_path": "admin_api",
                },
                actor=actor,
            )
            return DeviceRegistration(device=device, branch=branch)

    async def revoke_device(
        self,
        *,
        device_id: uuid.UUID,
        reason: str | None,
        actor: ActorContext,
    ) -> DeviceRevocation:
        """Revoke a device and end its live sessions immediately."""
        async with self._database.transaction() as session:
            devices = DeviceRepository(session)
            sessions = SessionRepository(session)
            audit = AuditService(session)
            now = self._now()

            device = await devices.get_device(device_id, for_update=True)
            if device is None:
                raise ResourceNotFoundError("No such device.")

            if device.revoked_at is not None:
                # Idempotent: revoking twice is not an error and does not double-audit.
                return DeviceRevocation(device=device, revoked_sessions=0, already_revoked=True)

            device.revoked_at = now
            device.revoked_by = actor.user_id
            device.revoke_reason = reason
            device.is_active = False

            revoked_sessions = await sessions.revoke_all_for_device(
                device.id, reason=REASON_DEVICE_REVOKED, when=now
            )
            audit.record(
                action=AuditAction.DEVICE_REVOKED,
                entity_type="device",
                entity_id=device.id,
                old_data={"is_active": True},
                new_data={
                    "device_uuid": str(device.device_uuid),
                    "device_name": device.device_name,
                    "branch_id": str(device.branch_id),
                    "reason": reason,
                    "revoked_sessions": revoked_sessions,
                },
                actor=actor,
            )
            return DeviceRevocation(
                device=device, revoked_sessions=revoked_sessions, already_revoked=False
            )

    async def list_devices(
        self,
        *,
        branch_id: uuid.UUID | None,
        is_active: bool | None,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[Device], int]:
        async with self._database.session() as session:
            return await DeviceRepository(session).list_devices(
                branch_id=branch_id, is_active=is_active, limit=limit, offset=offset
            )


__all__ = [
    "ALLOWED_PLATFORMS",
    "DeviceRegistration",
    "DeviceRevocation",
    "DeviceService",
]
