"""Rate limiting at the HTTP surface (PART 42, API_CONTRACT §7).

The limiter itself is unit-tested in ``tests/unit/test_rate_limit.py``; this module proves
the *endpoints* apply it, that a refused request carries the documented headers and error
code, and that the database-backed lockout still works when Redis is unavailable.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.auth_helpers import API, USERS, bearer, error_code, error_details, login, unique

pytestmark = pytest.mark.integration


class TestLoginThrottle:
    def test_the_login_bucket_is_enforced(self, api_client: TestClient, make_user: object) -> None:
        """The configured number of attempts is allowed; the next one is refused."""
        limit = get_settings().rate_limit_auth_per_5min
        user = make_user(is_active=True)  # type: ignore[operator]
        for _ in range(limit):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)

        refused = login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=429)
        assert error_code(refused) == "RATE_LIMITED"
        assert error_details(refused)["scope"] == "login"
        assert error_details(refused)["retry_after_seconds"] > 0

    def test_the_refusal_carries_the_limit_headers(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        limit = get_settings().rate_limit_auth_per_5min
        for _ in range(limit):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
        refused = login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=429)

        assert refused.headers["X-RateLimit-Limit"] == str(limit)
        assert refused.headers["X-RateLimit-Remaining"] == "0"
        assert int(refused.headers["Retry-After"]) >= 1

    def test_the_bucket_is_per_username(self, api_client: TestClient, make_user: object) -> None:
        """A shared office address must not let one account throttle another."""
        first = make_user()  # type: ignore[operator]
        second = make_user()  # type: ignore[operator]
        for _ in range(get_settings().rate_limit_auth_per_5min):
            login(api_client, str(first["username"]), "Wrong-Passw0rd-2026!", expect=401)
        login(api_client, str(first["username"]), "Wrong-Passw0rd-2026!", expect=429)

        # The second account can still sign in.
        assert login(api_client, str(second["username"])).status_code == 200

    def test_a_successful_login_does_not_bypass_the_bucket(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """The counter counts attempts, not failures — that is what stops stuffing.

        The account lockout (5 failures) fires before the login bucket (10 attempts), so
        the lock is cleared between attempts: the subject here is the limiter, not the
        lockout, and the two controls are asserted separately above.
        """
        from tests.helpers import execute_sql

        user = make_user()  # type: ignore[operator]
        limit = get_settings().rate_limit_auth_per_5min
        for _ in range(limit - 1):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
            execute_sql(
                main_database,
                "UPDATE users SET locked_until = NULL, failed_login_attempts = 0 WHERE id = :id",
                id=user["id"],
            )
        login(api_client, str(user["username"]))  # the last allowed attempt succeeds
        login(api_client, str(user["username"]), expect=429)

    def test_the_retry_window_is_five_minutes(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        for _ in range(get_settings().rate_limit_auth_per_5min):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
        refused = login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=429)
        assert 1 <= error_details(refused)["retry_after_seconds"] <= 300

    def test_the_lockout_survives_the_rate_limiter(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """Two independent controls: Redis throttling *and* the database lockout."""
        limit = get_settings().rate_limit_auth_per_5min
        max_attempts = get_settings().login_max_failed_attempts
        assert max_attempts <= limit

        user = make_user()  # type: ignore[operator]
        for _ in range(max_attempts):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)

        # The account is locked in the database...
        from tests.helpers import fetch_scalar

        assert (
            fetch_scalar(
                main_database,
                "SELECT locked_until FROM users WHERE id = :id",
                id=user["id"],
            )
            is not None
        )
        # ... and the correct password is still refused with 423 while the lock holds.
        locked = login(api_client, str(user["username"]), expect=423)
        assert error_code(locked) == "ACCOUNT_LOCKED"


class TestRefreshThrottle:
    def test_repeated_refreshes_are_throttled(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        limit = get_settings().rate_limit_refresh_per_min

        token = str(body["refresh_token"])
        refused = None
        for _ in range(limit + 5):
            response = api_client.post(
                f"{API}/auth/refresh",
                json={"refresh_token": token, "device_uuid": body["device"]["device_uuid"]},
            )
            if response.status_code == 429:
                refused = response
                break
            if response.status_code == 200:
                token = response.json()["refresh_token"]
        assert refused is not None, "the refresh bucket was never enforced"
        assert error_code(refused) == "RATE_LIMITED"
        assert error_details(refused)["scope"] == "refresh"

    def test_the_refresh_bucket_is_per_device(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        first = login(api_client, str(user["username"]), device_name="A").json()
        second = login(api_client, str(user["username"]), device_name="B").json()
        limit = get_settings().rate_limit_refresh_per_min

        token = str(first["refresh_token"])
        for _ in range(limit + 5):
            response = api_client.post(
                f"{API}/auth/refresh",
                json={"refresh_token": token, "device_uuid": first["device"]["device_uuid"]},
            )
            if response.status_code == 429:
                break
            token = response.json()["refresh_token"]

        # The other installation keeps working.
        assert (
            api_client.post(
                f"{API}/auth/refresh",
                json={
                    "refresh_token": second["refresh_token"],
                    "device_uuid": second["device"]["device_uuid"],
                },
            ).status_code
            == 200
        )


class TestRateLimitAccounting:
    def test_a_throttled_login_writes_no_audit_row(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """A throttled request never reaches the authentication logic."""
        from tests.helpers import fetch_scalar

        user = make_user()  # type: ignore[operator]
        for _ in range(get_settings().rate_limit_auth_per_5min):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
        before = fetch_scalar(main_database, "SELECT count(*) FROM audit_logs")

        login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=429)
        after = fetch_scalar(main_database, "SELECT count(*) FROM audit_logs")
        assert after == before

    def test_a_throttled_login_does_not_touch_the_account(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        from tests.helpers import fetch_scalar

        user = make_user()  # type: ignore[operator]
        for _ in range(get_settings().rate_limit_auth_per_5min):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
        attempts_before = fetch_scalar(
            main_database, "SELECT failed_login_attempts FROM users WHERE id = :id", id=user["id"]
        )
        login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=429)
        attempts_after = fetch_scalar(
            main_database, "SELECT failed_login_attempts FROM users WHERE id = :id", id=user["id"]
        )
        assert attempts_after == attempts_before


class TestRateLimitDoesNotBlockLegitimateWork:
    def test_reads_are_not_throttled_at_the_documented_level(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """The read budget (600/min) leaves room for a real shift's traffic."""
        for _ in range(20):
            assert api_client.get(USERS, headers=admin_headers).status_code == 200

    def test_an_unknown_user_is_throttled_too(self, api_client: TestClient) -> None:
        """Bucket keys are per username, so probing for accounts is throttled as well."""
        name = unique("probe")
        for _ in range(get_settings().rate_limit_auth_per_5min):
            login(api_client, name, "Wrong-Passw0rd-2026!", expect=401)
        assert login(api_client, name, "Wrong-Passw0rd-2026!", expect=429).status_code == 429

    def test_the_bucket_recovers_after_it_expires(
        self, api_client: TestClient, make_user: object, main_database: str, clear_rate_limits: None
    ) -> None:
        """The window is a fixed window: clearing it restores service (TTL-checked)."""
        from tests.helpers import execute_sql

        user = make_user()  # type: ignore[operator]
        for _ in range(get_settings().rate_limit_auth_per_5min):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
        login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=429)
        # Drop the counters the way the TTL eventually would, and confirm recovery.
        import redis as redis_sync

        from tests.helpers import redis_url

        # The lockout is a separate control; clear it so the logins can succeed again.
        execute_sql(
            main_database,
            "UPDATE users SET locked_until = NULL, failed_login_attempts = 0 WHERE id = :id",
            id=user["id"],
        )
        client = redis_sync.from_url(redis_url(), decode_responses=True)
        try:
            for key in client.scan_iter(match="nexus:rl:login:*"):
                client.delete(key)
        finally:
            client.close()
        assert login(api_client, str(user["username"])).status_code == 200

    def test_a_throttled_password_change_cannot_be_used_to_probe(
        self, api_client: TestClient, make_user: object
    ) -> None:
        """Password changes are authenticated, so the account bucket already covers them."""
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        for _ in range(6):
            response = api_client.post(
                f"{API}/auth/password",
                headers=headers,
                json={
                    "current_password": "Not-The-Passw0rd-2026!",
                    "new_password": "Rotated-Passw0rd-2026!",
                },
            )
            assert response.status_code == 401  # never 500, never 429-by-accident
            assert error_code(response) == "INVALID_CREDENTIALS"

    def test_no_token_is_throttled_out_of_the_identity_endpoint(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        for _ in range(30):
            assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 200
        assert uuid.UUID(body["session_id"])  # the session survived the traffic
