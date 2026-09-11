"""Login, device binding, account state and lockout (PART 24/25/42, API_CONTRACT §2).

Every case drives the real HTTP endpoint against the real database, so what is asserted
is the behaviour an operator (or an attacker) would observe.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.security import build_password_hasher
from tests.auth_helpers import (
    API,
    DEVICE_ID_HEADER,
    USER_PASSWORD,
    error_code,
    error_details,
    login,
    unique,
)
from tests.helpers import execute_sql, fetch_all, fetch_scalar

pytestmark = pytest.mark.integration


def contains_credential_material(payload: object) -> list[str]:
    """Every path in a JSON document that exposes a hash or a password field.

    The login response legitimately carries the *token* the caller just earned; it must
    never carry the stored credential itself.
    """
    found: list[str] = []

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                lowered = str(key).lower()
                is_secret_name = "hash" in lowered or (
                    "password" in lowered and lowered != "must_change_password"
                )
                if is_secret_name:
                    found.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")
        elif isinstance(node, str) and "$argon2" in node:
            found.append(path)

    walk(payload, "$")
    return found


def audit_actions(database: str, *, entity_id: object | None = None) -> list[str]:
    """Audit action names, newest first, optionally for one entity."""
    if entity_id is None:
        rows = fetch_all(
            database,
            "SELECT action FROM audit_logs ORDER BY created_at DESC, id DESC LIMIT 50",
        )
    else:
        rows = fetch_all(
            database,
            "SELECT action FROM audit_logs WHERE entity_id = :entity_id "
            "ORDER BY created_at DESC, id DESC LIMIT 50",
            entity_id=entity_id,
        )
    return [str(row[0]) for row in rows]


class TestValidLogin:
    def test_login_returns_a_token_pair_and_the_identity(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        device_uuid = uuid.uuid4()
        response = login(api_client, str(user["username"]), device_uuid=device_uuid)
        body = response.json()

        assert body["token_type"] == "bearer"
        assert body["access_token"] and body["refresh_token"].startswith("rt_")
        assert body["expires_in"] == get_settings().access_token_expire_minutes * 60
        assert body["refresh_expires_in"] == get_settings().refresh_token_expire_days * 86400
        assert uuid.UUID(body["session_id"])
        assert body["user"]["username"] == user["username"]
        assert body["user"]["roles"] == ["CASHIER"]
        assert "exchange.create" in body["user"]["permissions"]
        assert "users.manage" not in body["user"]["permissions"]  # least privilege
        assert body["roles"] == body["user"]["roles"]
        assert body["device"]["device_uuid"] == str(device_uuid)
        assert body["device"]["is_new_registration"] is True

    def test_login_registers_the_device_for_the_branch(
        self, api_client: TestClient, make_user: object, main_database: str, branch_id: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        device_uuid = uuid.uuid4()
        body = login(api_client, str(user["username"]), device_uuid=device_uuid).json()

        row = fetch_all(
            main_database,
            "SELECT branch_id, is_active, registered_by, last_seen_at FROM devices "
            "WHERE device_uuid = :uuid",
            uuid=device_uuid,
        )
        assert len(row) == 1
        assert str(row[0][0]) == branch_id
        assert row[0][1] is True
        assert str(row[0][2]) == user["id"]
        assert row[0][3] is not None
        assert body["device"]["branch_id"] == branch_id

    def test_the_second_login_reuses_the_device(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        device_uuid = uuid.uuid4()
        login(api_client, str(user["username"]), device_uuid=device_uuid)
        second = login(
            api_client, str(user["username"]), device_uuid=device_uuid, device_name="Renamed"
        ).json()

        assert second["device"]["is_new_registration"] is False
        devices = fetch_scalar(
            main_database,
            "SELECT count(*) FROM devices WHERE device_uuid = :uuid",
            uuid=device_uuid,
        )
        assert devices == 1

    def test_a_successful_login_updates_the_account_counters(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        login(api_client, str(user["username"]))
        row = fetch_all(
            main_database,
            "SELECT last_login_at, failed_login_attempts, locked_until FROM users WHERE id = :id",
            id=user["id"],
        )
        assert row[0][0] is not None
        assert row[0][1] == 0
        assert row[0][2] is None

    def test_the_refresh_token_is_stored_only_as_a_keyed_hash(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        raw = body["refresh_token"]

        rows = fetch_all(
            main_database,
            "SELECT token_hash FROM refresh_tokens WHERE family_id = :family",
            family=body["session_id"],
        )
        assert len(rows) == 1
        stored = str(rows[0][0])
        assert stored != raw
        assert raw not in stored
        assert len(stored) == 64

    def test_the_password_hash_never_leaves_the_server(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        response = login(api_client, str(user["username"]))
        assert contains_credential_material(response.json()) == []
        assert USER_PASSWORD not in response.text
        # and it really is hashed, with Argon2id, in the database
        stored = fetch_scalar(
            main_database, "SELECT password_hash FROM users WHERE id = :id", id=user["id"]
        )
        assert str(stored).startswith("$argon2id$")
        assert str(stored) != USER_PASSWORD

    def test_an_unknown_username_leaves_no_trace_in_the_response(
        self, api_client: TestClient
    ) -> None:
        response = login(api_client, unique("ghost"), expect=401)
        assert error_code(response) == "INVALID_CREDENTIALS"
        assert "ghost" not in json.dumps(response.json()).lower()

    def test_the_identity_endpoint_matches_the_login_response(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user(roles=("MANAGER",))  # type: ignore[operator]
        token = login(api_client, str(user["username"])).json()
        me = api_client.get(
            f"{API}/auth/me",
            headers={
                "Authorization": f"Bearer {token['access_token']}",
                DEVICE_ID_HEADER: str(token["device"]["id"]),
            },
        )
        assert me.status_code == 200, me.text
        assert me.json()["user"]["username"] == user["username"]
        assert me.json()["session_id"] == token["session_id"]

    def test_ping_confirms_a_usable_token(self, api_client: TestClient, make_user: object) -> None:
        user = make_user()  # type: ignore[operator]
        token = login(api_client, str(user["username"])).json()
        response = api_client.get(
            f"{API}/auth/ping",
            headers={
                "Authorization": f"Bearer {token['access_token']}",
                DEVICE_ID_HEADER: str(token["device"]["id"]),
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert response.headers["cache-control"] == "no-store"


class TestInvalidCredentials:
    def test_a_wrong_password_is_refused(
        self, api_client: TestClient, make_user: object, clear_rate_limits: None
    ) -> None:
        user = make_user()  # type: ignore[operator]
        response = login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
        assert error_code(response) == "INVALID_CREDENTIALS"
        # The message is generic and never echoes the attempted credential.
        assert "Wrong-Passw0rd-2026!" not in response.text
        assert response.json()["error"]["message"]

    def test_a_wrong_password_counts_against_the_account(
        self, api_client: TestClient, make_user: object, main_database: str, clear_rate_limits: None
    ) -> None:
        user = make_user()  # type: ignore[operator]
        login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
        row = fetch_all(
            main_database,
            "SELECT failed_login_attempts, last_login_at FROM users WHERE id = :id",
            id=user["id"],
        )
        assert row[0][0] == 1
        assert row[0][1] is None

    def test_a_wrong_password_opens_no_session(
        self, api_client: TestClient, make_user: object, main_database: str, clear_rate_limits: None
    ) -> None:
        user = make_user()  # type: ignore[operator]
        login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
        sessions = fetch_scalar(
            main_database,
            "SELECT count(*) FROM refresh_tokens WHERE user_id = :id",
            id=user["id"],
        )
        assert sessions == 0

    def test_the_failure_is_audited_with_the_attempt_count(
        self, api_client: TestClient, make_user: object, main_database: str, clear_rate_limits: None
    ) -> None:
        user = make_user()  # type: ignore[operator]
        login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
        rows = fetch_all(
            main_database,
            "SELECT action, new_data FROM audit_logs WHERE entity_id = :id "
            "ORDER BY created_at DESC LIMIT 1",
            id=user["id"],
        )
        assert rows[0][0] == "AUTH_LOGIN_FAILED"
        payload = rows[0][1]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["reason"] == "INVALID_PASSWORD"
        assert payload["failed_attempts"] == 1
        assert payload["remaining_attempts"] == get_settings().login_max_failed_attempts - 1

    def test_an_unknown_user_is_audited_without_an_actor(
        self, api_client: TestClient, main_database: str, clear_rate_limits: None
    ) -> None:
        username = unique("ghost")
        login(api_client, username, expect=401)
        rows = fetch_all(
            main_database,
            "SELECT action, user_id, entity_id, new_data FROM audit_logs "
            "ORDER BY created_at DESC LIMIT 1",
        )
        assert rows[0][0] == "AUTH_LOGIN_FAILED"
        assert rows[0][1] is None
        assert rows[0][2] is None
        payload = rows[0][3]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["username"] == username
        assert payload["reason"] == "UNKNOWN_USER"

    def test_a_failed_login_does_not_touch_any_other_account(
        self, api_client: TestClient, make_user: object, main_database: str, clear_rate_limits: None
    ) -> None:
        """User isolation: the attempt belongs to one account and moves only its counters."""
        attacker_target = make_user()  # type: ignore[operator]
        bystander = make_user()  # type: ignore[operator]
        before = fetch_all(
            main_database,
            "SELECT failed_login_attempts, locked_until, last_login_at, is_active, "
            "password_changed_at, must_change_password FROM users WHERE id = :id",
            id=bystander["id"],
        )

        login(api_client, str(attacker_target["username"]), "Wrong-Passw0rd-2026!", expect=401)

        after = fetch_all(
            main_database,
            "SELECT failed_login_attempts, locked_until, last_login_at, is_active, "
            "password_changed_at, must_change_password FROM users WHERE id = :id",
            id=bystander["id"],
        )
        assert before == after
        assert (
            fetch_scalar(
                main_database,
                "SELECT failed_login_attempts FROM users WHERE id = :id",
                id=attacker_target["id"],
            )
            == 1
        )


class TestAccountState:
    def test_an_inactive_account_cannot_log_in(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user(is_active=False)  # type: ignore[operator]
        # 401, not 403: a deactivated account is "not authenticated" and the answer must
        # not tell an attacker that the username exists but is disabled.
        response = login(api_client, str(user["username"]), expect=401)
        assert error_code(response) == "ACCOUNT_DISABLED"
        assert "AUTH_LOGIN_DENIED" in audit_actions(main_database, entity_id=user["id"])

    def test_a_deactivated_account_cannot_log_in(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """The documented soft delete (DELETE /users/{id}) also stops future logins.

        The deactivation is applied directly here (the endpoint itself is covered by
        ``test_users_admin.py``) so this test stays focused on the login path.
        """
        user = make_user()  # type: ignore[operator]
        execute_sql(
            main_database, "UPDATE users SET is_active = false WHERE id = :id", id=user["id"]
        )
        response = login(api_client, str(user["username"]), expect=401)
        assert error_code(response) == "ACCOUNT_DISABLED"

    def test_five_wrong_passwords_lock_the_account(
        self, api_client: TestClient, make_user: object, main_database: str, clear_rate_limits: None
    ) -> None:
        user = make_user()  # type: ignore[operator]
        max_attempts = get_settings().login_max_failed_attempts
        for _ in range(max_attempts):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)

        row = fetch_all(
            main_database,
            "SELECT failed_login_attempts, locked_until FROM users WHERE id = :id",
            id=user["id"],
        )
        assert row[0][0] == max_attempts
        assert row[0][1] is not None
        assert "AUTH_LOCKOUT" in audit_actions(main_database, entity_id=user["id"])

    def test_a_correct_password_while_locked_reports_423(
        self, api_client: TestClient, make_user: object, main_database: str, clear_rate_limits: None
    ) -> None:
        user = make_user()  # type: ignore[operator]
        for _ in range(get_settings().login_max_failed_attempts):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)

        response = login(api_client, str(user["username"]), expect=423)
        assert error_code(response) == "ACCOUNT_LOCKED"
        assert error_details(response)["locked_until"]
        assert "AUTH_LOGIN_DENIED" in audit_actions(main_database, entity_id=user["id"])

    def test_a_wrong_password_while_locked_still_says_only_invalid_credentials(
        self, api_client: TestClient, make_user: object, clear_rate_limits: None
    ) -> None:
        """The lockout must not become a username oracle for an attacker."""
        user = make_user()  # type: ignore[operator]
        for _ in range(get_settings().login_max_failed_attempts):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)
        response = login(api_client, str(user["username"]), "Another-Wrong-2026!", expect=401)
        assert error_code(response) == "INVALID_CREDENTIALS"

    def test_a_successful_login_clears_an_expired_lock(
        self, api_client: TestClient, make_user: object, main_database: str, clear_rate_limits: None
    ) -> None:
        past = (dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=1)).isoformat()
        user = make_user(locked_until=past)  # type: ignore[operator]
        login(api_client, str(user["username"]))
        row = fetch_all(
            main_database,
            "SELECT failed_login_attempts, locked_until FROM users WHERE id = :id",
            id=user["id"],
        )
        assert row[0][0] == 0
        assert row[0][1] is None

    def test_a_preconfigured_lock_now_blocks_login(
        self, api_client: TestClient, make_user: object, clear_rate_limits: None
    ) -> None:
        future = (dt.datetime.now(tz=dt.UTC) + dt.timedelta(minutes=10)).isoformat()
        user = make_user(locked_until=future)  # type: ignore[operator]
        response = login(api_client, str(user["username"]), expect=423)
        assert error_code(response) == "ACCOUNT_LOCKED"


class TestDevicePolicy:
    def test_a_revoked_device_cannot_log_in(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        device_uuid = uuid.uuid4()
        device_id = login(api_client, str(user["username"]), device_uuid=device_uuid).json()[
            "device"
        ]["id"]
        execute_sql(
            main_database,
            "UPDATE devices SET is_active = false, "
            "revoked_at = now(), revoke_reason = 'lost' WHERE device_uuid = :uuid",
            uuid=device_uuid,
        )
        response = login(api_client, str(user["username"]), device_uuid=device_uuid, expect=401)
        assert error_code(response) == "DEVICE_REVOKED"
        # The refusal is recorded against the device (entity_id), naming the user in the
        # payload: the device is what an administrator will look up.
        rows = fetch_all(
            main_database,
            "SELECT action, user_id, new_data FROM audit_logs WHERE entity_id = :id "
            "ORDER BY created_at DESC LIMIT 1",
            id=device_id,
        )
        assert rows[0][0] == "AUTH_LOGIN_DENIED"
        # No user_id: a login attempt is unauthenticated, so the row names the account in
        # its payload instead (AUTH_LOGIN_FAILED is the only action allowed a NULL actor).
        assert rows[0][1] is None
        payload = rows[0][2]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["reason"] == "DEVICE_REVOKED"
        assert payload["username"] == user["username"]

    def test_an_unknown_device_is_refused_for_a_user_without_device_register(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """AUDITOR has no ``device.register``: unknown device = refused, and audited."""
        user = make_user(roles=("AUDITOR",))  # type: ignore[operator]
        response = login(api_client, str(user["username"]), device_uuid=uuid.uuid4(), expect=401)
        assert error_code(response) == "DEVICE_UNKNOWN"
        assert error_details(response)["hint"]
        assert "DEVICE_REGISTRATION_DENIED" in audit_actions(main_database)

    def test_a_user_with_device_register_self_registers_a_new_device(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user(roles=("MANAGER",))  # type: ignore[operator]
        body = login(api_client, str(user["username"]), device_uuid=uuid.uuid4()).json()
        assert body["device"]["is_new_registration"] is True

    def test_another_account_reuses_the_same_physical_device(
        self, api_client: TestClient, make_user: object
    ) -> None:
        """A shared counter PC: both operators log in on the same installation."""
        device_uuid = uuid.uuid4()
        first = make_user(roles=("CASHIER",))  # type: ignore[operator]
        second = make_user(roles=("CASHIER",))  # type: ignore[operator]
        body_one = login(api_client, str(first["username"]), device_uuid=device_uuid).json()
        body_two = login(api_client, str(second["username"]), device_uuid=device_uuid).json()
        assert body_one["device"]["id"] == body_two["device"]["id"]
        assert body_two["device"]["is_new_registration"] is False
        assert body_one["session_id"] != body_two["session_id"]

    def test_a_new_device_needs_a_branch_when_the_branch_is_unknown(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        response = login(
            api_client,
            str(user["username"]),
            device_uuid=uuid.uuid4(),
            branch_id=uuid.uuid4(),
            expect=422,
        )
        assert error_code(response) == "VALIDATION_ERROR"

    def test_the_app_version_is_recorded(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"]), app_version="2.4.1").json()
        assert (
            fetch_scalar(
                main_database,
                "SELECT app_version FROM devices WHERE id = :id",
                id=body["device"]["id"],
            )
            == "2.4.1"
        )


class TestRequestValidation:
    @pytest.mark.parametrize(
        "field,value",
        [
            ("username", "ab"),  # too short
            ("username", "has space"),
            ("password", ""),
            ("device_uuid", "not-a-uuid"),
            ("platform", "SOLARIS"),
            ("device_name", ""),
        ],
    )
    def test_bad_input_is_rejected_as_422_not_500(
        self, api_client: TestClient, field: str, value: str
    ) -> None:
        body = {
            "username": "user-abcdef",
            "password": "Cashier-Passw0rd-2026!",
            "device_uuid": str(uuid.uuid4()),
            "device_name": "Counter-1",
            "platform": "WINDOWS",
        }
        body[field] = value
        response = api_client.post(f"{API}/auth/login", json=body)
        assert response.status_code == 422, response.text
        assert "error" in response.json()

    def test_an_unknown_field_is_refused(self, api_client: TestClient) -> None:
        response = api_client.post(
            f"{API}/auth/login",
            json={
                "username": "user-abcdef",
                "password": "Cashier-Passw0rd-2026!",
                "device_uuid": str(uuid.uuid4()),
                "device_name": "Counter-1",
                "platform": "WINDOWS",
                "is_admin": True,
            },
        )
        assert response.status_code == 422

    def test_the_platform_is_normalised(self, api_client: TestClient, make_user: object) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"]), platform="android").json()
        assert body["device"]["platform"] == "ANDROID"

    def test_no_request_body_reaches_the_database_when_validation_fails(
        self, api_client: TestClient, main_database: str
    ) -> None:
        before = fetch_scalar(main_database, "SELECT count(*) FROM audit_logs")
        api_client.post(f"{API}/auth/login", json={"username": "x"})
        assert fetch_scalar(main_database, "SELECT count(*) FROM audit_logs") == before

    def test_an_oversized_password_is_refused_before_hashing(self, api_client: TestClient) -> None:
        response = api_client.post(
            f"{API}/auth/login",
            json={
                "username": "user-abcdef",
                "password": "x" * 200,
                "device_uuid": str(uuid.uuid4()),
                "device_name": "Counter-1",
                "platform": "WINDOWS",
            },
        )
        assert response.status_code == 422


class TestStoredCredentialPolicy:
    def test_the_stored_hash_verifies_against_the_password(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        stored = str(
            fetch_scalar(
                main_database, "SELECT password_hash FROM users WHERE id = :id", id=user["id"]
            )
        )
        hasher = build_password_hasher(
            time_cost=get_settings().argon2_time_cost,
            memory_cost=get_settings().argon2_memory_cost,
            parallelism=get_settings().argon2_parallelism,
        )
        assert hasher.verify(USER_PASSWORD, stored).is_valid
        assert hasher.verify("something else entirely", stored).is_valid is False
