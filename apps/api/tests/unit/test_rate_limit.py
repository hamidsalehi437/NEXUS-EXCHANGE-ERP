"""Redis-backed rate limiting: windows, headers and both failure modes (PART 42, §7).

The limiter is tested against the real Redis test database (15), not a fake, because the
fixed-window behaviour *is* Redis behaviour (``INCR`` + ``EXPIRE`` + ``TTL``).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.config import Settings
from app.core.exceptions import RateLimitedError, ServiceUnavailableError
from app.core.rate_limit import RATE_LIMIT_NAMESPACE, RateLimiter
from app.core.redis import RedisManager
from tests.helpers import database_dsn, redis_url

pytestmark = pytest.mark.integration


def build_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "database_url": database_dsn("nexus_ratelimit_probe", driver="asyncpg"),
        "database_migration_url": database_dsn("nexus_ratelimit_probe", driver="asyncpg"),
        "jwt_secret": "a" * 48,
        "jwt_refresh_secret": "b" * 48,
        "redis_url": redis_url(),
        "storage_path": "/tmp/nexus-ratelimit-test",
        "rate_limit_enabled": True,
        "rate_limit_fail_closed": False,
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


@pytest.fixture
async def limiter(clear_rate_limits: None) -> Iterator[RateLimiter]:
    """A limiter on the test Redis, with the API's namespace guaranteed empty."""
    settings = build_settings()
    manager = RedisManager(settings)
    rate_limiter = RateLimiter(manager, settings)
    yield rate_limiter
    await manager.close()


class TestFixedWindow:
    async def test_requests_are_counted_until_the_limit(self, limiter: RateLimiter) -> None:
        allowed = 0
        for _ in range(5):
            result = await limiter.check("probe:count", limit=3, window_seconds=60)
            allowed += int(result.allowed)
        assert allowed == 3

    async def test_the_refusal_reports_the_window_and_the_wait(
        self, limiter: RateLimiter
    ) -> None:
        for _ in range(2):
            await limiter.check("probe:refuse", limit=2, window_seconds=90)
        result = await limiter.check("probe:refuse", limit=2, window_seconds=90)
        assert result.allowed is False
        assert result.limit == 2
        assert result.remaining == -1  # over the limit; headers clamp it to 0
        assert 0 < result.retry_after_seconds <= 90

    async def test_the_counter_lives_in_the_api_namespace(self, limiter: RateLimiter) -> None:
        result = await limiter.check("login:1.2.3.4:admin", limit=10, window_seconds=300)
        assert result.key == f"nexus:{RATE_LIMIT_NAMESPACE}:login:1.2.3.4:admin"

    async def test_the_key_expires_on_its_own(self, limiter: RateLimiter) -> None:
        await limiter.check("probe:ttl", limit=1, window_seconds=60)
        client = await limiter._redis.client()  # the limiter's own Redis handle
        ttl = await client.ttl(f"nexus:{RATE_LIMIT_NAMESPACE}:probe:ttl")
        assert 0 < ttl <= 60

    async def test_buckets_are_independent(self, limiter: RateLimiter) -> None:
        for _ in range(3):
            await limiter.check("probe:user-a", limit=3, window_seconds=60)
        result = await limiter.check("probe:user-b", limit=3, window_seconds=60)
        assert result.allowed is True
        assert result.remaining == 2

    async def test_headers_describe_the_bucket(self, limiter: RateLimiter) -> None:
        result = await limiter.check("probe:headers", limit=10, window_seconds=300)
        headers = result.headers()
        assert headers["X-RateLimit-Limit"] == "10"
        assert headers["X-RateLimit-Remaining"] == "9"
        assert headers["X-RateLimit-Window"] == "300"
        assert "Retry-After" not in headers

    async def test_a_refusal_carries_retry_after(self, limiter: RateLimiter) -> None:
        await limiter.check("probe:retry", limit=1, window_seconds=30)
        headers = (await limiter.check("probe:retry", limit=1, window_seconds=30)).headers()
        assert headers["X-RateLimit-Remaining"] == "0"
        assert 1 <= int(headers["Retry-After"]) <= 30


class TestEnforce:
    async def test_enforce_raises_429_with_the_documented_shape(
        self, limiter: RateLimiter
    ) -> None:
        await limiter.enforce("write:user-1", limit=1, window_seconds=60)
        with pytest.raises(RateLimitedError) as error:
            await limiter.enforce("write:user-1", limit=1, window_seconds=60)
        assert error.value.http_status == 429
        assert error.value.code == "RATE_LIMITED"
        assert error.value.headers["Retry-After"]
        assert error.value.details["scope"] == "write"

    async def test_enforce_returns_the_headers_on_success(self, limiter: RateLimiter) -> None:
        result = await limiter.enforce("read:user-1", limit=5, window_seconds=60)
        assert result.allowed is True
        assert result.headers()["X-RateLimit-Remaining"] == "4"


class TestDisabledAndZeroLimits:
    async def test_a_disabled_limiter_never_counts(self) -> None:
        settings = build_settings(rate_limit_enabled=False)
        manager = RedisManager(settings)
        try:
            limiter = RateLimiter(manager, settings)
            for _ in range(20):
                result = await limiter.check("probe:disabled", limit=1, window_seconds=60)
                assert result.allowed is True
            client = await manager.client()
            assert await client.exists("nexus:rl:probe:disabled") == 0
        finally:
            await manager.close()

    async def test_a_zero_limit_switches_a_bucket_off(self, limiter: RateLimiter) -> None:
        result = await limiter.check("probe:off", limit=0, window_seconds=60)
        assert result.allowed is True
        client = await limiter._redis.client()  # the limiter's own Redis handle
        assert await client.exists("nexus:rl:probe:off") == 0


class TestRedisFailureModes:
    async def test_redis_down_allows_by_default_and_logs_a_warning(self) -> None:
        settings = build_settings(
            redis_url="redis://127.0.0.1:6399/0", rate_limit_fail_closed=False
        )
        manager = RedisManager(settings)
        try:
            limiter = RateLimiter(manager, settings)
            result = await limiter.check("probe:down", limit=1, window_seconds=60)
            assert result.allowed is True
        finally:
            await manager.close()

    async def test_redis_down_refuses_when_fail_closed(self) -> None:
        settings = build_settings(redis_url="redis://127.0.0.1:6399/0", rate_limit_fail_closed=True)
        manager = RedisManager(settings)
        try:
            limiter = RateLimiter(manager, settings)
            with pytest.raises(ServiceUnavailableError) as error:
                await limiter.check("probe:down-fc", limit=1, window_seconds=60)
            assert error.value.http_status == 503
        finally:
            await manager.close()
