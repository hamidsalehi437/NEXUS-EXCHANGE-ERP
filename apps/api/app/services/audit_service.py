"""Audit service — one call site for every recorded action (PART 18).

Services never build ``AuditLog`` rows themselves: they call
:meth:`AuditService.record`, which fills in the actor (user, device, IP, request id)
from the request context and scrubs credential-shaped fields (see
:func:`app.repositories.audit.scrub_payload`). That keeps two properties true by
construction:

* every entry names who did it, from where and in which request;
* no password, token or hash ever reaches the permanent evidence trail.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_actions import AuditAction
from app.core.security import normalize_ip_address
from app.repositories.audit import AuditRepository


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Who is acting, and through what — the four fields every audit row carries.

    ``user_id`` is ``None`` only for events that happen before a user is known (a login
    attempt with an unknown username); everything else is attributed.
    """

    user_id: uuid.UUID | None = None
    device_id: uuid.UUID | None = None
    ip_address: str | None = None
    request_id: str | None = None
    # The branch this actor is bound to (the device's branch). Phase 4's ledger uses it
    # for scope decisions: an entry may only be posted to, or read from, the actor's own
    # branch unless the actor holds a global scope role. ``None`` means "not bound to a
    # branch", which is a *restricted* state, never an unrestricted one.
    branch_id: uuid.UUID | None = None
    # The actor's effective permissions, carried so administrative services can refuse to
    # grant authority the actor does not hold (see UserService escalation guards).
    permissions: frozenset[str] = frozenset()
    # ... and the roles those permissions come from, so a *system* role cannot be handed
    # out by an administrator who does not hold it (deny by default).
    roles: tuple[str, ...] = ()

    def as_log_fields(self) -> dict[str, Any]:
        """Non-secret fields suitable for a structured log line."""
        return {
            "actor_user_id": str(self.user_id) if self.user_id else None,
            "actor_device_id": str(self.device_id) if self.device_id else None,
            "ip_address": self.ip_address,
            "request_id": self.request_id,
        }


class AuditService:
    """Append-only audit writes for the current transaction."""

    def __init__(self, session: AsyncSession, *, actor: ActorContext | None = None) -> None:
        self._repo = AuditRepository(session)
        self._actor = actor or ActorContext()

    def record(
        self,
        *,
        action: AuditAction,
        entity_type: str,
        entity_id: uuid.UUID | None = None,
        new_data: Mapping[str, Any] | None = None,
        old_data: Mapping[str, Any] | None = None,
        actor: ActorContext | None = None,
    ) -> None:
        """Write one audit row inside the caller's transaction.

        Recording happens in the same transaction as the change it describes, so an
        audited action and its evidence commit together or not at all.
        """
        effective = actor or self._actor
        self._repo.record(
            action=str(action),
            entity_type=entity_type,
            entity_id=entity_id,
            user_id=effective.user_id,
            device_id=effective.device_id,
            old_data=old_data,
            new_data=new_data,
            # Never let an unparsable peer address (an unusual proxy chain, a UNIX socket)
            # turn an audited operation into a 500: an unknown address is stored as NULL.
            ip_address=normalize_ip_address(effective.ip_address),
            request_id=effective.request_id,
        )
