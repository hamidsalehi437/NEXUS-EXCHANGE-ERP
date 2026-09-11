"""Token issuing and verification (PART 24, PART 42, API_CONTRACT §2).

Two different kinds of credential, deliberately:

* **Access token** — a short-lived signed JWT (default 15 minutes) that carries
  ``sub`` (user), ``jti`` (token id, used for the revocation denylist), ``sid``
  (session/refresh family), ``did`` (device), ``bid`` (branch), ``roles`` and
  ``perm_hash``. It is verified by signature and expiry only; *authorisation* never
  trusts it (see :mod:`app.api.deps`), which is why a stolen-but-unexpired token
  still cannot outlive a revoked session.
* **Refresh token** — an opaque 256-bit random value. Only its keyed hash is stored
  (``refresh_tokens.token_hash``), so the database never contains anything that can
  be replayed. Rotation is handled by :mod:`app.repositories.sessions`.

Nothing here reads a secret from anywhere but the validated settings object, and no
function in this module logs or returns a raw token except to its immediate caller.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.config import Settings
from app.core.exceptions import TokenExpiredError, TokenInvalidError, TokenRevokedError
from app.core.permissions import permission_hash

# Claim values that pin a token to this system and this API, so a token minted for
# another audience (or a future refresh token) can never be used as an access token.
TOKEN_ISSUER = "nexus-exchange"  # noqa: S105 - JWT issuer, not a secret
TOKEN_AUDIENCE = "nexus-api"  # noqa: S105 - JWT audience, not a secret
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - claim value, not a secret

# Opaque refresh tokens: 32 bytes = 256 bits of entropy, per SECURITY.md §2.
REFRESH_TOKEN_BYTES = 32
REFRESH_TOKEN_PREFIX = "rt_"  # noqa: S105 - visible token prefix, not a secret

# Errors from PyJWT that mean "this token is expired" rather than "this token is bogus".
_EXPIRY_ERRORS = (jwt.ExpiredSignatureError,)


@dataclass(frozen=True, slots=True)
class IssuedAccessToken:
    """An access token and the facts about it that the caller must persist or log."""

    token: str
    jti: str
    expires_at: dt.datetime
    issued_at: dt.datetime
    expires_in_seconds: int


@dataclass(frozen=True, slots=True)
class IssuedRefreshToken:
    """A refresh token in its two representations: raw (client) and hashed (database)."""

    raw: str
    token_hash: str


class AccessTokenClaims(BaseModel):
    """Verified access-token claims, typed and complete.

    ``extra="ignore"`` keeps a token with additional claims (``kid``-based key
    rotation, or a client-supplied hint) usable; every field the API depends on is
    required, so a malformed token fails closed instead of producing a half-filled
    principal.
    """

    model_config = ConfigDict(extra="ignore")

    iss: str
    aud: str
    sub: uuid.UUID
    jti: str
    sid: uuid.UUID = Field(description="Session/refresh-token family id")
    did: uuid.UUID | None = Field(default=None, description="Device id")
    bid: uuid.UUID | None = Field(default=None, description="Branch id")
    roles: tuple[str, ...] = ()
    perm_hash: str = ""
    typ: str = ACCESS_TOKEN_TYPE
    iat: dt.datetime
    exp: dt.datetime

    @property
    def expires_in_seconds(self) -> int:
        return max(0, int((self.exp - dt.datetime.now(tz=dt.UTC)).total_seconds()))


class TokenService:
    """Issue and verify access tokens; mint and hash refresh tokens."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._algorithm = settings.jwt_algorithm
        self._signing_key = settings.jwt_secret
        self._refresh_key = settings.jwt_refresh_secret.encode("utf-8")
        self._access_ttl = dt.timedelta(minutes=settings.access_token_expire_minutes)

    # ---------------------------------------------------------------- access tokens
    @property
    def access_token_ttl_seconds(self) -> int:
        return int(self._access_ttl.total_seconds())

    @property
    def refresh_token_ttl_seconds(self) -> int:
        return int(dt.timedelta(days=self._settings.refresh_token_expire_days).total_seconds())

    def issue_access_token(
        self,
        *,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        device_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        roles: tuple[str, ...] | list[str],
        permissions: frozenset[str] | set[str],
        issued_at: dt.datetime | None = None,
    ) -> IssuedAccessToken:
        """Mint an access token for one authenticated session."""
        now = issued_at or dt.datetime.now(tz=dt.UTC)
        expires_at = now + self._access_ttl
        jti = str(uuid.uuid4())
        payload: dict[str, object] = {
            "iss": TOKEN_ISSUER,
            "aud": TOKEN_AUDIENCE,
            "sub": str(user_id),
            "jti": jti,
            "sid": str(session_id),
            "did": str(device_id) if device_id else None,
            "bid": str(branch_id) if branch_id else None,
            "roles": sorted(str(role) for role in roles),
            "perm_hash": permission_hash({str(p) for p in permissions}),
            "typ": ACCESS_TOKEN_TYPE,
            "iat": int(now.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        token = jwt.encode(
            payload,
            self._signing_key,
            algorithm=self._algorithm,
            headers={"kid": self._key_id},
        )
        return IssuedAccessToken(
            token=token,
            jti=jti,
            expires_at=expires_at,
            issued_at=now,
            expires_in_seconds=int(self._access_ttl.total_seconds()),
        )

    def decode_access_token(self, token: str) -> AccessTokenClaims:
        """Verify an access token, raising the contract's 401 errors on failure.

        Distinct errors matter to the client: ``TOKEN_EXPIRED`` means "refresh and
        retry", everything else means "sign in again". No error message ever echoes the
        token or the underlying library text.
        """
        try:
            payload = jwt.decode(
                token,
                self._signing_key,
                algorithms=[self._algorithm],
                audience=TOKEN_AUDIENCE,
                issuer=TOKEN_ISSUER,
                options={"require": ["exp", "iat", "sub", "jti", "sid"]},
            )
        except _EXPIRY_ERRORS as exc:
            raise TokenExpiredError(
                "The access token has expired.",
                details={"hint": "Exchange the refresh token for a new access token."},
            ) from exc
        except jwt.PyJWTError as exc:
            raise TokenInvalidError("The access token is not valid.") from exc

        if payload.get("typ") != ACCESS_TOKEN_TYPE:
            raise TokenInvalidError("The token is not an access token.")

        try:
            return AccessTokenClaims.model_validate(payload)
        except ValidationError as exc:
            raise TokenInvalidError("The access token is missing required claims.") from exc

    # --------------------------------------------------------------- refresh tokens
    @property
    def _key_id(self) -> str:
        """Key identifier for the ``kid`` header (key rotation, SECURITY.md §5).

        Derived from the signing secret so it changes exactly when the key does —
        but only 12 hex characters of a one-way digest are exposed.
        """
        digest = hashlib.sha256(self._signing_key.encode("utf-8")).hexdigest()
        return f"hs-{self._algorithm.lower()}-{digest[:12]}"

    def mint_refresh_token(self) -> IssuedRefreshToken:
        """Create a fresh opaque refresh token and the hash to persist."""
        raw = REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(REFRESH_TOKEN_BYTES)
        return IssuedRefreshToken(raw=raw, token_hash=self.hash_refresh_token(raw))

    def hash_refresh_token(self, raw: str) -> str:
        """Keyed hash of a refresh token — the only form that ever reaches the database.

        ``refresh_tokens.token_hash`` is documented as "sha256 of the opaque token
        value"; the value is keyed with ``JWT_REFRESH_SECRET`` (HMAC-SHA256, whose
        output is exactly the same 64 hex characters the column expects). A database
        dump alone is therefore not enough to replay a session: the attacker also needs
        the secret, which lives in the runtime secret store.
        """
        return hmac.new(self._refresh_key, raw.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_refresh_token(self, value: str) -> None:
        """Reject syntactically impossible refresh tokens before touching the database.

        A cheap shape check that keeps junk from reaching the query planner and makes
        the failure mode of a truncated token obvious in logs.
        """
        if not value.startswith(REFRESH_TOKEN_PREFIX) or len(value) < 20:
            raise TokenInvalidError("The refresh token is not valid.")


def is_refresh_token_revoked(*, revoked_at: dt.datetime | None) -> bool:
    """Read back the revocation state the database stores for a refresh token."""
    return revoked_at is not None


def token_hash_matches(expected_hash: str, candidate_hash: str) -> bool:
    """Constant-time comparison for token hashes."""
    return hmac.compare_digest(expected_hash, candidate_hash)


# Re-exported so callers do not import two modules for the same job.
__all__ = [
    "ACCESS_TOKEN_TYPE",
    "REFRESH_TOKEN_PREFIX",
    "TOKEN_AUDIENCE",
    "TOKEN_ISSUER",
    "AccessTokenClaims",
    "IssuedAccessToken",
    "IssuedRefreshToken",
    "TokenRevokedError",
    "TokenService",
    "is_refresh_token_revoked",
    "token_hash_matches",
]
