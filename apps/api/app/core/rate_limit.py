"""Redis-backed rate limiting (PART 42, API_CONTRACT §7).

A fixed-window counter per bucket: the first request in a window creates the key
with a TTL, later requests increment it, and the key expires on its own. Fixed
windows are used deliberately — they are predictable ("10 logins per 5 minutes"),
cheap (one round trip) and the burst at a window edge is bounded, which is the
right trade-off for a counter in an office, not a public API.

Two failure modes matter and both are explicit:

* **Redis unavailable** — by default the limiter *allows* the request and logs a
  warning. The database-backed account lockout (``users.locked_until``) is
  unaffected, so brute-force protection does not disappear with Redis. A deployment
  that prefers availability-last can set ``RATE_LIMIT_FAIL_CLOSED=true`` and every
  rate-limited endpoint then answers ``503`` while Redis is down.
* **Bucket exhausted** — ``429 RATE_LIMITED`` with ``Retry-After`` and
  ``X-RateLimit-*`` headers, so a client can back off correctly instead of hammering.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.exceptions import RateLimitedError, ServiceUnavailableError
from app.core.logging import get_logger
from app.core.redis import RedisManager

logger = get_logger(__name__)

# Namespace under the shared ``nexus:`` prefix, e.g. ``nexus:rl:login:127.0.0.1:admin``.
RATE_LIMIT_NAMESPACE = "rl"


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    """Outcome of a bucket check."""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    window_seconds: int
    key: str

    def headers(self) -> dict[str, str]:
        """Standard limit headers; ``Retry-After`` only when the request was refused."""
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(self.remaining, 0)),
            "X-RateLimit-Window": str(self.window_seconds),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(self.retry_after_seconds, 1))
        return headers


class RateLimiter:
    """Fixed-window limiter shared by the API's security-sensitive endpoints."""

    def __init__(self, redis: RedisManager, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.rate_limit_enabled

    def _redis_key(self, bucket: str) -> str:
        return RedisManager.key(RATE_LIMIT_NAMESPACE, bucket)

    async def check(self, bucket: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        """Count one hit against ``bucket`` and report whether it is allowed.

        ``limit <= 0`` disables the bucket (operators may switch a limit off without
        touching code), and a disabled limiter short-circuits before Redis is touched.
        """
        key = self._redis_key(bucket)
        if limit <= 0 or not self.enabled:
            return RateLimitResult(
                allowed=True,
                limit=limit,
                remaining=limit,
                retry_after_seconds=0,
                window_seconds=window_seconds,
                key=key,
            )

        try:
            client = await self._redis.client()
            count = await client.incr(key)
            if count == 1:
                # First hit in this window: start the clock. ``expire`` on a fresh key is
                # race-free because the key was just created by this INCR.
                await client.expire(key, window_seconds)
            ttl = await client.ttl(key)
        except RedisError as exc:
            return self._on_redis_failure(
                bucket=bucket, limit=limit, window_seconds=window_seconds, exc=exc
            )

        effective_ttl = ttl if isinstance(ttl, int) and ttl > 0 else window_seconds
        allowed = int(count) <= limit
        return RateLimitResult(
            allowed=allowed,
            limit=limit,
            remaining=limit - int(count),
            retry_after_seconds=0 if allowed else effective_ttl,
            window_seconds=window_seconds,
            key=key,
        )

    def _on_redis_failure(
        self, *, bucket: str, limit: int, window_seconds: int, exc: RedisError
    ) -> RateLimitResult:
        """Decide what a Redis outage means for a limited endpoint."""
        if self._settings.rate_limit_fail_closed:
            logger.error("rate_limit_unavailable", bucket=bucket, error=str(exc))
            raise ServiceUnavailableError(
                "Rate limiting is unavailable; the request was refused (fail-closed mode).",
                details={"component": "redis"},
            )
        logger.warning("rate_limit_degraded", bucket=bucket, error=str(exc))
        return RateLimitResult(
            allowed=True,
            limit=limit,
            remaining=limit,
            retry_after_seconds=0,
            window_seconds=window_seconds,
            key=self._redis_key(bucket),
        )

    async def enforce(self, bucket: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        """Check the bucket and raise ``429 RATE_LIMITED`` when it is exhausted."""
        result = await self.check(bucket, limit=limit, window_seconds=window_seconds)
        if not result.allowed:
            logger.warning(
                "rate_limited",
                bucket=bucket,
                limit=result.limit,
                window_seconds=result.window_seconds,
            )
            raise RateLimitedError(
                "Too many requests for this operation. Retry later.",
                details={
                    "scope": bucket.split(":", 1)[0],
                    "retry_after_seconds": max(result.retry_after_seconds, 1),
                },
                headers=result.headers(),
            )
        return result


def retry_after_from(result: RateLimitResult) -> int:
    """Convenience for tests and the 429 handler."""
    return max(result.retry_after_seconds, 1)


def current_window_start(window_seconds: int, *, now: float | None = None) -> int:
    """Start of the window containing ``now`` (used by tests to reason about TTLs)."""
    moment = time.time() if now is None else now
    return int(moment) // window_seconds * window_seconds
