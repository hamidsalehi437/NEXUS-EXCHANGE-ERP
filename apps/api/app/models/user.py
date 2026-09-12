"""User mapping and per-user grants (PART 6, PART 25, PART 41).

Tables: ``users``, ``user_roles``, ``user_permissions``.

``users`` is never hard-deleted (PART 25): deactivation is ``is_active = FALSE``.
The database enforces this with an append-only trigger and by withholding
``DELETE`` from the application role.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, SmallInteger, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, Base):
    """Operator account. Only the Argon2id hash is stored, never the password."""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    failed_login_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<User username={self.username!r} active={self.is_active}>"


class UserRole(Base):
    """User → role assignment."""

    __tablename__ = "user_roles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )


class UserPermission(Base):
    """Explicit per-user grant or deny. An explicit deny wins over role grants."""

    __tablename__ = "user_permissions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    permission_code: Mapped[str] = mapped_column(
        ForeignKey("permissions.code", ondelete="RESTRICT"), primary_key=True
    )
    is_granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    granted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    granted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
