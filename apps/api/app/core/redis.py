"""Redis access — cache, rate-limit counters, Celery broker and health probing.

Redis never holds financial state (ADR-002): it may be flushed at any time without
losing money. Everything here is therefore safe to treat as best-effort except the
health probe, which reports a degraded service honestly instead of hiding it.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

KEY_PREFIX = "nexus"


class RedisManager:
    """Lazy Redis client with namespaced keys and a real readiness probe."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: aioredis.Redis[str] | None = None

    async def client(self) -> aioredis.Redis[str]:
        if self._client is None:
            self._client = aioredis.from_url(
                self._settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=2.0,
                socket_timeout=2.0,
                health_check_interval=30,
                client_name="nexus-api",
            )
        return self._client

    async def ping(self) -> None:
        """Raise :class:`RedisError` when Redis does not answer PING."""
        client = await self.client()
        await client.ping()

    async def close(self) -> None:
        if self._client is not None:
            # redis-py 5.x async API; its bundled stubs still declare close() only.
            await self._client.aclose()  # type: ignore[attr-defined]
            self._client = None

    @staticmethod
    def key(*parts: object) -> str:
        """Build a namespaced cache key, e.g. ``nexus:rate:USD:AFN``."""
        return ":".join([KEY_PREFIX, *(str(part) for part in parts)])

    async def get_json(self, key: str) -> str | None:
        """Best-effort read; a Redis outage returns ``None`` instead of failing."""
        try:
            client = await self.client()
            value = await client.get(key)
            return value if value is None else str(value)
        except RedisError as exc:
            logger.warning("redis_get_failed", key=key, error=str(exc))
            return None

    async def set_json(self, key: str, value: str, *, ttl_seconds: int = 60) -> bool:
        """Best-effort write with a mandatory TTL (no unbounded cache growth)."""
        try:
            client = await self.client()
            await client.set(key, value, ex=ttl_seconds)
            return True
        except RedisError as exc:
            logger.warning("redis_set_failed", key=key, error=str(exc))
            return False

    async def info(self) -> dict[str, Any]:
        """Minimal INFO subset for the readiness endpoint."""
        client = await self.client()
        raw = await client.info(section="server")
        return {"redis_version": raw.get("redis_version"), "mode": raw.get("redis_mode")}
