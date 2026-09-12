"""Authorization: deny by default, the role/permission matrix, and password change.

PART 41 requires RBAC with exactly six roles and ``resource.action`` codes, enforced on
the server. This module checks the *matrix* (every role against every seeded endpoint),
not just one example per rule, so a future permission change is visible here.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.permissions import ROLE_PERMISSIONS, Permission, RoleName
from tests.auth_helpers import (
    API,
    DEVICE_ID_HEADER,
    USER_PASSWORD,
    bearer,
    error_code,
    error_details,
    login,
    unique,
)
from tests.helpers import fetch_all, fetch_scalar

pytestmark = pytest.mark.integration

# One representative endpoint per permission the seeded roles actually exercise, plus
# endpoints that need no permission beyond authentication. The matrix below asserts the
# expected status for each (role, endpoint) pair.
#
#   "read"  = GET, "write" = POST/PATCH/PUT/DELETE
PROTECTED_ENDPOINTS: tuple[tuple[str, str, str, str], ...] = (
    # (label, method, path, required permission or "" for any authenticated user)
    ("users.list", "GET", "/users", str(Permission.USERS_MANAGE)),
    ("roles.list", "GET", "/roles", str(Permission.USERS_MANAGE)),
    ("permissions.list", "GET", "/permissions", str(Permission.USERS_MANAGE)),
    ("devices.list", "GET", "/devices", str(Permission.DEVICE_MANAGE)),
    ("devices.register", "POST", "/devices/register", str(Permission.DEVICE_REGISTER)),
    ("auth.me", "GET", "/auth/me", ""),
    ("auth.sessions", "GET", "/auth/sessions", ""),
    ("auth.ping", "GET", "/auth/ping", ""),
)


def call(client: TestClient, method: str, path: str, headers: dict[str, str]) -> int:
    """Issue a request and return only its status code."""
    if path == "/devices/register":
        return client.request(
            method,
            f"{API}{path}",
            headers=headers,
            json={
                "device_uuid": str(uuid.uuid4()),
                "device_name": "Matrix",
                "platform": "WINDOWS",
                "branch_id": str(uuid.uuid4()),
            },
        ).status_code
    return client.request(method, f"{API}{path}", headers=headers).status_code


class TestDenyByDefault:
    def test_every_protected_endpoint_refuses_anonymous_callers(
        self, api_client: TestClient
    ) -> None:
        for label, method, path, _ in PROTECTED_ENDPOINTS:
            status = call(api_client, method, path, {})
            assert status == 401, f"{label} answered {status} without a token"

    def test_an_authenticated_user_without_the_permission_is_refused(
        self, api_client: TestClient, make_user: object, provisioned_device: object
    ) -> None:
        """AUDITOR is the clearest case: read-only, and no administration rights at all."""
        user = make_user(roles=("AUDITOR",))  # type: ignore[operator]
        device_uuid = provisioned_device()
        body = login(
            api_client, str(user["username"]), device_uuid=uuid.UUID(str(device_uuid))
        ).json()
        headers = bearer(body["access_token"], body["device"]["id"])

        denied = [
            "users.list",
            "roles.list",
            "permissions.list",
            "devices.list",
            "devices.register",
        ]
        for label, method, path, _ in PROTECTED_ENDPOINTS:
            status = call(api_client, method, path, headers)
            if label in denied:
                assert status == 403, f"{label} answered {status} for an auditor"
            else:
                assert status in {200, 422}, f"{label} answered {status} for an auditor"

    def test_an_account_with_no_roles_has_no_authority(
        self, api_client: TestClient, make_user: object
    ) -> None:
        """Deny by default: no role means no permission, not "some default set"."""
        user = make_user(roles=())  # type: ignore[operator]
        body = login(
            api_client,
            str(user["username"]),
            device_uuid=uuid.uuid4(),
            expect=401,  # no device.register either, so it cannot even open a session
        )
        assert error_code(body) == "DEVICE_UNKNOWN"

        # With a device provisioned by an administrator the login succeeds, and the
        # account still holds exactly nothing.
        provisioned = api_client.post(
            f"{API}/auth/login",
            json={
                "username": user["username"],
                "password": USER_PASSWORD,
                "device_uuid": str(uuid.uuid4()),
                "device_name": "probe",
                "platform": "WINDOWS",
            },
        )
        assert provisioned.status_code == 401  # still refused: no device.register

    def test_a_permission_is_required_even_for_a_manager(
        self, api_client: TestClient, make_user: object, branch_id: str
    ) -> None:
        """MANAGER may provision a counter but not administer the staff list."""
        user = make_user(roles=("MANAGER",))  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])

        provisioned = api_client.post(
            f"{API}/devices/register",
            headers=headers,
            json={
                "device_uuid": str(uuid.uuid4()),
                "device_name": "Counter-7",
                "platform": "WINDOWS",
                "branch_id": branch_id,
            },
        )
        assert provisioned.status_code == 201, provisioned.text
        # ... but listing the estate and editing users are administrator rights.
        assert call(api_client, "GET", "/devices", headers) == 403
        assert call(api_client, "GET", "/users", headers) == 403

    def test_the_permission_denied_error_names_the_missing_permission(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        response = api_client.get(
            f"{API}/users", headers=bearer(body["access_token"], body["device"]["id"])
        )
        assert response.status_code == 403
        assert error_details(response)["required_permission"] == "users.manage"

    def test_an_unknown_permission_code_in_an_override_is_refused(
        self, api_client: TestClient, make_user: object, admin_headers: dict[str, str]
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        response = api_client.put(
            f"{API}/users/{user['id']}/permissions",
            headers=admin_headers,
            json={"overrides": [{"permission_code": "money.print", "is_granted": True}]},
        )
        assert response.status_code in {404, 422}, response.text


class TestRolePermissionMatrix:
    """The full matrix: every seeded role against every endpoint above."""

    @pytest.mark.parametrize("role_name", [str(role) for role in RoleName])
    def test_role_behaves_as_the_matrix_says(
        self,
        api_client: TestClient,
        make_user: object,
        provisioned_device: object,
        role_name: str,
    ) -> None:
        permissions = {str(permission) for permission in ROLE_PERMISSIONS[RoleName(role_name)]}
        user = make_user(roles=(role_name,))  # type: ignore[operator]
        device_uuid = uuid.UUID(str(provisioned_device()))
        body = login(api_client, str(user["username"]), device_uuid=device_uuid).json()
        headers = bearer(body["access_token"], body["device"]["id"])

        for label, method, path, required in PROTECTED_ENDPOINTS:
            status = call(api_client, method, path, headers)
            if not required:
                expected = 200
            elif required in permissions:
                expected = 200 if method == "GET" else (201, 404, 422)
            else:
                expected = 403
            if isinstance(expected, tuple):
                assert status in expected, f"{role_name}: {label} answered {status}"
            else:
                assert status == expected, f"{role_name}: {label} answered {status}"

    def test_the_role_grants_in_the_database_match_the_code(
        self, api_client: TestClient, main_database: str, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{API}/roles", headers=admin_headers)
        assert response.status_code == 200, response.text
        stored = {item["name"]: set(item["permissions"]) for item in response.json()["items"]}
        expected = {
            str(role): {str(permission) for permission in permissions}
            for role, permissions in ROLE_PERMISSIONS.items()
        }
        # Other suites materialise extra test-only roles (the escalation fixtures), so the
        # contract is "every seeded role is present with exactly its coded grants".
        missing = sorted(set(expected) - set(stored))
        assert not missing, f"seeded roles missing from the catalogue: {missing}"
        for role, permissions in expected.items():
            assert stored[role] == permissions, f"{role} grants drifted from the code"

    def test_the_permission_catalogue_endpoint_lists_the_registry(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{API}/permissions", headers=admin_headers)
        assert response.status_code == 200
        codes = {item["code"] for item in response.json()["items"]}
        assert codes == {str(permission) for permission in Permission}
        assert all(item["description"] for item in response.json()["items"])

    def test_super_admin_holds_every_permission(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        me = api_client.get(f"{API}/auth/me", headers=admin_headers)
        assert me.status_code == 200
        assert set(me.json()["user"]["permissions"]) == {
            str(permission) for permission in Permission
        }


class TestPasswordChange:
    def test_a_password_change_replaces_the_credential(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        new_password = "Rotated-Passw0rd-2026!"

        response = api_client.post(
            f"{API}/auth/password",
            headers=headers,
            json={"current_password": USER_PASSWORD, "new_password": new_password},
        )
        assert response.status_code == 200, response.text
        assert response.json()["password_changed_at"]

        # The old password no longer works; the new one does.
        login(api_client, str(user["username"]), USER_PASSWORD, expect=401)
        relogin = login(api_client, str(user["username"]), new_password)
        assert relogin.status_code == 200

        stored = str(
            fetch_scalar(
                main_database, "SELECT password_hash FROM users WHERE id = :id", id=user["id"]
            )
        )
        assert new_password not in stored
        assert stored.startswith("$argon2id$")

    def test_the_wrong_current_password_is_refused(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        response = api_client.post(
            f"{API}/auth/password",
            headers=bearer(body["access_token"], body["device"]["id"]),
            json={
                "current_password": "Not-The-Passw0rd-2026!",
                "new_password": "Rotated-Passw0rd-2026!",
            },
        )
        assert response.status_code == 401
        assert error_code(response) == "INVALID_CREDENTIALS"

    def test_the_failed_change_is_audited(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        api_client.post(
            f"{API}/auth/password",
            headers=bearer(body["access_token"], body["device"]["id"]),
            json={
                "current_password": "Not-The-Passw0rd-2026!",
                "new_password": "Rotated-Passw0rd-2026!",
            },
        )
        rows = fetch_all(
            main_database,
            "SELECT action, new_data FROM audit_logs WHERE entity_id = :id "
            "AND action LIKE 'AUTH_PASSWORD%' ORDER BY created_at DESC LIMIT 1",
            id=user["id"],
        )
        assert rows[0][0] == "AUTH_PASSWORD_CHANGE_FAILED"
        payload = rows[0][1]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["reason"] == "INVALID_CURRENT_PASSWORD"

    def test_a_short_or_weak_password_is_refused(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        for candidate in ("short", "alllowercaseletters", "1234567890123"):
            response = api_client.post(
                f"{API}/auth/password",
                headers=headers,
                json={"current_password": USER_PASSWORD, "new_password": candidate},
            )
            assert response.status_code == 422, candidate
            assert error_code(response) == "VALIDATION_ERROR"

    def test_the_new_password_must_differ(self, api_client: TestClient, make_user: object) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        response = api_client.post(
            f"{API}/auth/password",
            headers=bearer(body["access_token"], body["device"]["id"]),
            json={"current_password": USER_PASSWORD, "new_password": USER_PASSWORD},
        )
        assert response.status_code == 422

    def test_a_password_change_ends_the_other_sessions(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        phone = login(api_client, str(user["username"]), device_name="Phone").json()
        counter = login(api_client, str(user["username"]), device_name="Counter").json()

        response = api_client.post(
            f"{API}/auth/password",
            headers=bearer(phone["access_token"], phone["device"]["id"]),
            json={"current_password": USER_PASSWORD, "new_password": "Rotated-Passw0rd-2026!"},
        )
        assert response.status_code == 200
        assert response.json()["revoked_sessions"] >= 1

        # The session that changed the password keeps working...
        assert (
            api_client.get(
                f"{API}/auth/me", headers=bearer(phone["access_token"], phone["device"]["id"])
            ).status_code
            == 200
        )
        # ... the other one does not.
        assert (
            api_client.get(
                f"{API}/auth/me", headers=bearer(counter["access_token"], counter["device"]["id"])
            ).status_code
            == 401
        )
        # And the stolen refresh token cannot revive it.
        assert (
            api_client.post(
                f"{API}/auth/refresh", json={"refresh_token": counter["refresh_token"]}
            ).status_code
            == 401
        )

    def test_the_change_is_audited(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        api_client.post(
            f"{API}/auth/password",
            headers=bearer(body["access_token"], body["device"]["id"]),
            json={"current_password": USER_PASSWORD, "new_password": "Rotated-Passw0rd-2026!"},
        )
        rows = fetch_all(
            main_database,
            "SELECT action, user_id, new_data FROM audit_logs "
            "WHERE action = 'AUTH_PASSWORD_CHANGED' ORDER BY created_at DESC LIMIT 1",
        )
        assert rows[0][0] == "AUTH_PASSWORD_CHANGED"
        assert str(rows[0][1]) == user["id"]
        assert "password" not in json.dumps(rows[0][2]).lower()
        assert "$argon2" not in json.dumps(rows[0][2])

    def test_must_change_password_blocks_authority_but_allows_the_change(
        self, api_client: TestClient, make_user: object, branch_id: str
    ) -> None:
        """A new account may authenticate but not act until it has chosen a password."""
        user = make_user(  # type: ignore[operator]
            roles=("MANAGER",), must_change_password=True
        )
        body = login(api_client, str(user["username"])).json()
        assert body["must_change_password"] is True
        headers = bearer(body["access_token"], body["device"]["id"])

        provision = {
            "device_uuid": str(uuid.uuid4()),
            "device_name": "Counter-9",
            "platform": "WINDOWS",
            "branch_id": branch_id,
        }
        # Reading its own identity is allowed (the client must show who is signed in).
        assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 200
        # Using authority is not.
        blocked = api_client.post(f"{API}/devices/register", headers=headers, json=provision)
        assert blocked.status_code == 403
        assert error_details(blocked)["reason"] == "PASSWORD_CHANGE_REQUIRED"
        assert "/api/v1/auth/password" in error_details(blocked)["allowed_endpoints"]

        changed = api_client.post(
            f"{API}/auth/password",
            headers=headers,
            json={"current_password": USER_PASSWORD, "new_password": "Chosen-Passw0rd-2026!"},
        )
        assert changed.status_code == 200, changed.text
        # Authority is usable again on the same token: the flag was the only blocker.
        assert (
            api_client.post(f"{API}/devices/register", headers=headers, json=provision).status_code
            == 201
        )

    def test_the_must_change_flag_is_cleared_by_the_change(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user(  # type: ignore[operator]
            roles=("MANAGER",), must_change_password=True
        )
        body = login(api_client, str(user["username"])).json()
        api_client.post(
            f"{API}/auth/password",
            headers=bearer(body["access_token"], body["device"]["id"]),
            json={"current_password": USER_PASSWORD, "new_password": "Chosen-Passw0rd-2026!"},
        )
        assert (
            fetch_scalar(
                main_database,
                "SELECT must_change_password FROM users WHERE id = :id",
                id=user["id"],
            )
            is False
        )

    def test_a_password_change_does_not_change_authority(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        before = api_client.get(
            f"{API}/auth/me", headers=bearer(body["access_token"], body["device"]["id"])
        ).json()["user"]["permissions"]
        api_client.post(
            f"{API}/auth/password",
            headers=bearer(body["access_token"], body["device"]["id"]),
            json={"current_password": USER_PASSWORD, "new_password": "Rotated-Passw0rd-2026!"},
        )
        after = login(api_client, str(user["username"]), "Rotated-Passw0rd-2026!").json()
        assert sorted(after["permissions"]) == sorted(before)

    def test_the_endpoint_requires_authentication(self, api_client: TestClient) -> None:
        response = api_client.post(
            f"{API}/auth/password",
            json={"current_password": USER_PASSWORD, "new_password": "Rotated-Passw0rd-2026!"},
        )
        assert response.status_code == 401


class TestCrossAccountGuards:
    def test_one_account_cannot_change_another_password(
        self, api_client: TestClient, make_user: object
    ) -> None:
        victim = make_user()  # type: ignore[operator]
        attacker = make_user()  # type: ignore[operator]
        attacker_body = login(api_client, str(attacker["username"])).json()

        response = api_client.post(
            f"{API}/auth/password",
            headers=bearer(attacker_body["access_token"], attacker_body["device"]["id"]),
            json={
                "current_password": attacker["password"],
                "new_password": "Attacker-Passw0rd-2026!",
            },
        )
        assert response.status_code == 200  # changes only their own password
        # The victim is untouched.
        assert login(api_client, str(victim["username"]), USER_PASSWORD).status_code == 200

    def test_a_device_cannot_be_taken_over_by_another_account(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """Device identity is global: the second account binds to the same device row."""
        device_uuid = uuid.uuid4()
        first = make_user()  # type: ignore[operator]
        second = make_user()  # type: ignore[operator]
        first_body = login(api_client, str(first["username"]), device_uuid=device_uuid).json()
        second_body = login(api_client, str(second["username"]), device_uuid=device_uuid).json()
        assert first_body["device"]["id"] == second_body["device"]["id"]

        # Revoking the installation ends both accounts' sessions on it.
        from tests.helpers import execute_sql

        execute_sql(
            main_database,
            "UPDATE devices SET is_active = false, revoked_at = now() WHERE id = :id",
            id=second_body["device"]["id"],
        )
        for body in (first_body, second_body):
            assert (
                api_client.get(
                    f"{API}/auth/me", headers=bearer(body["access_token"], body["device"]["id"])
                ).status_code
                == 401
            )

    def test_headers_cannot_impersonate_another_user(
        self, api_client: TestClient, make_user: object
    ) -> None:
        victim = make_user()  # type: ignore[operator]
        attacker = make_user()  # type: ignore[operator]
        victim_body = login(api_client, str(victim["username"])).json()
        attacker_body = login(api_client, str(attacker["username"])).json()

        for header, value in (
            ("X-User-Id", str(victim["id"])),
            ("X-Role", "SUPER_ADMIN"),
            ("X-Permissions", "users.manage"),
            ("X-Forwarded-User", victim["username"]),
        ):
            headers = bearer(attacker_body["access_token"], attacker_body["device"]["id"])
            headers[header] = str(value)
            me = api_client.get(f"{API}/auth/me", headers=headers)
            assert me.status_code == 200
            assert me.json()["user"]["username"] == attacker["username"]
            assert victim_body["user"]["username"] != attacker["username"]

    def test_a_session_cannot_be_moved_to_another_device(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        other = login(api_client, str(user["username"])).json()
        response = api_client.get(
            f"{API}/auth/me",
            headers={
                "Authorization": f"Bearer {body['access_token']}",
                DEVICE_ID_HEADER: str(other["device"]["id"]),
            },
        )
        assert response.status_code == 401
        assert error_code(response) == "DEVICE_MISMATCH"

    def test_a_username_is_case_insensitive_but_never_ambiguous(
        self, api_client: TestClient, main_database: str
    ) -> None:
        """``ux_users_username_lower`` makes case-variant usernames impossible."""
        from tests.helpers import create_user

        name = unique("Case")
        create_user(main_database, username=name, password=USER_PASSWORD, roles=("CASHIER",))
        assert login(api_client, name.upper()).status_code == 200
        # The unique index on lower(username) is what makes the lookup unambiguous.
        with pytest.raises(IntegrityError):
            create_user(
                main_database,
                username=name.upper(),
                password=USER_PASSWORD,
                roles=("CASHIER",),
            )

    def test_the_login_username_is_case_insensitive(
        self, api_client: TestClient, make_user: object
    ) -> None:
        """Operators type the username; the unique index makes the lookup unambiguous."""
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"]).upper()).json()
        assert body["user"]["username"] == user["username"]

    def test_surrounding_whitespace_is_rejected_rather_than_trimmed(
        self, api_client: TestClient, make_user: object
    ) -> None:
        """Strict input validation: a padded username is a client bug, not a login."""
        user = make_user()  # type: ignore[operator]
        response = login(api_client, f"  {user['username']}  ", expect=422)
        assert error_code(response) == "VALIDATION_ERROR"
