"""Security primitives — password hashing (Phase 1 scope).

Phase 1 delivers exactly the primitive the foundation needs: Argon2id hashing
with the parameters documented in ``SECURITY.md`` §2, plus the password policy
the seed runner and Phase 2's user endpoints will enforce. Token issuing, refresh
rotation, device binding and permission checks are Phase 2 work and are **not**
stubbed here.

Argon2id is memory-hard: the parameters below are the documented baseline
(``time_cost=3``, ``memory_cost=64 MiB``, ``parallelism=4``) and are re-checked on
every successful verification, so a hash created with weaker parameters is
transparently upgraded on the next login.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128

# A short, transparent blocklist of the passwords that appear in every credential
# dump. Deployments can extend it by listing one password per line in
# ``<STORAGE_PATH>/password_blocklist.txt`` (loaded by the operator, not by code).
COMMON_PASSWORDS: frozenset[str] = frozenset(
    {
        "password",
        "password1",
        "password123",
        "passw0rd",
        "123456",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty",
        "qwerty123",
        "abc123",
        "letmein",
        "welcome",
        "admin",
        "admin123",
        "administrator",
        "changeme",
        "change-me",
        "iloveyou",
        "monkey",
        "dragon",
        "sunshine",
        "princess",
        "football",
        "master",
        "superman",
        "nexus",
        "nexus123",
        "exchange",
        "exchange123",
        "Afghanistan",
        "kabul123",
    }
)


class PasswordPolicyError(ValueError):
    """Raised when a password does not satisfy the policy."""


@dataclass(frozen=True, slots=True)
class PasswordHash:
    """Result of a verification: whether it matched and whether it needs upgrading."""

    is_valid: bool
    needs_rehash: bool
    algorithm: str


class PasswordHasher:
    """Argon2id hasher with explicit, reviewable parameters."""

    algorithm = "argon2id"

    def __init__(
        self,
        *,
        time_cost: int = 3,
        memory_cost: int = 65_536,
        parallelism: int = 4,
        hash_len: int = 32,
        salt_len: int = 16,
    ) -> None:
        self._hasher = Argon2PasswordHasher(
            time_cost=time_cost,
            memory_cost=memory_cost,
            parallelism=parallelism,
            hash_len=hash_len,
            salt_len=salt_len,
            type=Type.ID,
        )
        self._parameters = {
            "time_cost": time_cost,
            "memory_cost": memory_cost,
            "parallelism": parallelism,
        }

    @property
    def parameters(self) -> dict[str, int]:
        """The Argon2id parameters in force (safe to log; contains no secret)."""
        return dict(self._parameters)

    def hash(self, password: str) -> str:
        """Hash a password that has already passed :func:`enforce_password_policy`."""
        return self._hasher.hash(password)

    def verify(self, password: str, password_hash: str) -> PasswordHash:
        """Verify a password without leaking *why* it failed."""
        try:
            self._hasher.verify(password_hash, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return PasswordHash(is_valid=False, needs_rehash=False, algorithm=self.algorithm)

        return PasswordHash(
            is_valid=True,
            needs_rehash=self._hasher.check_needs_rehash(password_hash),
            algorithm=self.algorithm,
        )


def enforce_password_policy(
    password: str,
    *,
    username: str | None = None,
    full_name: str | None = None,
    blocklist: frozenset[str] = COMMON_PASSWORDS,
) -> None:
    """Raise :class:`PasswordPolicyError` when a password is not acceptable.

    Rules (SECURITY.md §2): length bounds, not a known/common password, not equal
    to the username or the full name, and mixed character classes. Periodic
    rotation is deliberately *not* required (NIST SP 800-63B).
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            f"password must be at least {MIN_PASSWORD_LENGTH} characters long"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at most {MAX_PASSWORD_LENGTH} characters long")
    if password.strip() != password:
        raise PasswordPolicyError("password must not start or end with whitespace")

    lowered = password.casefold()
    if lowered in {value.casefold() for value in blocklist}:
        raise PasswordPolicyError("password is among the most common passwords")

    if username and lowered == username.casefold():
        raise PasswordPolicyError("password must not be the same as the username")
    if full_name and lowered == full_name.casefold():
        raise PasswordPolicyError("password must not be the same as the full name")

    classes = sum(
        (
            any(char.islower() for char in password),
            any(char.isupper() for char in password),
            any(char.isdigit() for char in password),
            any(not char.isalnum() for char in password),
        )
    )
    if classes < 3:
        raise PasswordPolicyError(
            "password must contain at least three of: lowercase, uppercase, digit, symbol"
        )


def build_password_hasher(
    *,
    time_cost: int,
    memory_cost: int,
    parallelism: int,
) -> PasswordHasher:
    """Build a hasher from validated settings."""
    return PasswordHasher(
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
    )


def normalize_ip_address(value: str | None) -> str | None:
    """Return a value PostgreSQL's ``INET`` type accepts, or ``None``.

    ``request.client.host`` is an address under uvicorn, but an ASGI server may report
    something else: a placeholder from an unusual proxy chain, a UNIX-socket peer, or the
    in-process test transport's literal ``testclient``. The client address is stored in
    two ``INET`` columns (``audit_logs.ip_address``, ``refresh_tokens.ip_address``), and a
    malformed value from the transport must never be able to fail an otherwise valid
    login — the audit row is dropped to ``NULL`` instead, which is exactly how an
    untraceable address should be recorded.
    """
    if not value:
        return None
    candidate = value.strip()
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate
