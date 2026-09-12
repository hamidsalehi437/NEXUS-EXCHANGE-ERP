"""Access-token revocation list (SECURITY.md §2: ``jti`` denylist in Redis).

The authoritative revocation state for a session is the database (``refresh_tokens``):
a revoked family makes its access token unusable on the next request, because
:meth:`~app.repositories.sessions.SessionRepository.load_session_context` reads the
session on every call. The Redis denylist is a second, independent switch that lets an
operator kill a single access token — defence in depth, never the only control.

Consequence, stated plainly: if Redis is unavailable the API *still* honours database
revocation. The denylist lookup therefore fails open **by design** and logs a warning;
the alternative (refusing every authenticated request while Redis is down) would take
the counters offline for a control the database already provides.
"""

from __future__ import annotations

import uuid

from redis.exceptions import RedisError

from app.core.logging import get_logger
from app.core.redis import RedisManager

logger = get_logger(__name__)

REVOCATION_NAMESPACE = "jti"


class RevocationList:
    """Redis-backed denylist of access-token ids (``jti``)."""

    def __init__(self, redis: RedisManager) -> None:
        self._redis = redis

    @staticmethod
    def key(jti: str | uuid.UUID) -> str:
        return RedisManager.key(REVOCATION_NAMESPACE, str(jti))

    async def revoke(self, jti: str | uuid.UUID, *, expires_in_seconds: int) -> bool:
        """Deny a token id until it would have expired anyway.

        The TTL is the token's remaining lifetime: the list never grows without bound,
        and a revoked token stops being tracked exactly when it stops being accepted.
        """
        ttl = max(int(expires_in_seconds), 1)
        try:
            client = await self._redis.client()
            await client.set(self.key(jti), "1", ex=ttl)
            return True
        except RedisError as exc:
            logger.warning("revocation_list_write_failed", error=str(exc))
            return False

    async def is_revoked(self, jti: str | uuid.UUID) -> bool:
        """True when the token id is denylisted; a Redis outage reports "not revoked"."""
        try:
            client = await self._redis.client()
            return bool(await client.exists(self.key(jti)))
        except RedisError as exc:
            logger.warning("revocation_list_unavailable", error=str(exc))
            return False

    async def revoke_many(self, jtis: list[str], *, expires_in_seconds: int) -> int:
        revoked = 0
        for jti in jtis:
            if await self.revoke(jti, expires_in_seconds=expires_in_seconds):
                revoked += 1
        return revoked
