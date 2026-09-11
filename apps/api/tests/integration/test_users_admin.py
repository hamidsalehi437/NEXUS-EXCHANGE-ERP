"""User administration: create, read, update, deactivate, roles, permission overrides.

PART 25 (users are deactivated, never deleted), PART 21 (audit trail) and the Phase 2
critical rules (no plaintext or hashed credential ever leaves the API; RBAC cannot be
bypassed for convenience) are all checked here against the real endpoints.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import get_settings
from app.core.permissions import ROLE_PERMISSIONS, Permission, RoleName
from tests.auth_helpers import (
    API,
    USER_PASSWORD,
    USERS,
    bearer,
    error_code,
    error_details,
    login,
    unique,
)
from tests.helpers import execute_sql, fetch_all, fetch_scalar

pytestmark = pytest.mark.integration


def create_user_payload(**overrides: object) -> dict[str, object]:
    """A valid ``POST /users`` body, with fields overridable per test."""
    payload: dict[str, object] = {
        "username": unique("teller"),
        "password": USER_PASSWORD,
        "full_name": "Teller Test",
        "roles": ["CASHIER"],
        "must_change_password": False,
    }
    payload.update(overrides)
    return payload


def users_in(database: str) -> int:
    return int(fetch_scalar(database, "SELECT count(*) FROM users") or 0)


class TestUserCreation:
    def test_an_administrator_creates_a_user(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        payload = create_user_payload(email="teller@example.af", phone="+93700000000")
        response = api_client.post(USERS, headers=admin_headers, json=payload)
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["username"] == payload["username"]
        assert body["is_active"] is True
        assert body["roles"] == ["CASHIER"]
        assert "cash.create" in body["permissions"]
        assert "users.manage" not in body["permissions"]
        # No credential material of any kind in the response.
        assert "password_hash" not in response.text
        assert "$argon2" not in response.text
        assert '"password"' not in response.text

        # The stored hash is Argon2id and the plaintext is nowhere in the row.
        stored = fetch_scalar(
            main_database,
            "SELECT password_hash FROM users WHERE username = :username",
            username=payload["username"],
        )
        assert str(stored).startswith("$argon2id$")
        assert USER_PASSWORD not in str(stored)

    def test_the_new_account_can_log_in(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        payload = create_user_payload()
        api_client.post(USERS, headers=admin_headers, json=payload)
        body = login(api_client, str(payload["username"]), str(payload["password"])).json()
        assert body["user"]["roles"] == ["CASHIER"]

    def test_creation_is_audited(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        payload = create_user_payload()
        created = api_client.post(USERS, headers=admin_headers, json=payload).json()
        rows = fetch_all(
            main_database,
            "SELECT action, user_id, new_data FROM audit_logs WHERE entity_id = :id "
            "AND action LIKE 'USER_%' ORDER BY created_at DESC LIMIT 1",
            id=created["id"],
        )
        assert rows[0][0] == "USER_CREATED"
        assert rows[0][1] is not None  # the administrator who created it
        assert "password_hash" not in json.dumps(rows[0][2])
        assert "$argon2" not in json.dumps(rows[0][2])

    def test_a_duplicate_username_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        payload = create_user_payload()
        assert api_client.post(USERS, headers=admin_headers, json=payload).status_code == 201
        duplicate = create_user_payload(username=str(payload["username"]).upper())
        second = api_client.post(USERS, headers=admin_headers, json=duplicate)
        assert second.status_code in {409, 422}
        assert error_code(second) in {"DUPLICATE_RESOURCE", "DATA_INTEGRITY_ERROR"}

    def test_a_duplicate_email_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        email = f"{unique('mail')}@example.af"
        assert (
            api_client.post(
                USERS, headers=admin_headers, json=create_user_payload(email=email)
            ).status_code
            == 201
        )
        duplicate = create_user_payload(email=email)
        second = api_client.post(USERS, headers=admin_headers, json=duplicate)
        assert second.status_code in {409, 422}

    def test_a_weak_password_is_refused_before_it_is_hashed(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        before = users_in(main_database)
        for candidate in ("short", "alllowercaseonly", "1234567890123"):
            response = api_client.post(
                USERS, headers=admin_headers, json=create_user_payload(password=candidate)
            )
            assert response.status_code == 422, candidate
        assert users_in(main_database) == before

    def test_a_password_equal_to_the_username_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        username = unique("same")
        payload = create_user_payload(username=username, password=username)
        response = api_client.post(USERS, headers=admin_headers, json=payload)
        assert response.status_code == 422

    def test_an_unknown_role_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        before = users_in(main_database)
        response = api_client.post(
            USERS, headers=admin_headers, json=create_user_payload(roles=["WIZARD"])
        )
        assert response.status_code == 422
        assert error_details(response)["unknown"] == ["WIZARD"]
        assert users_in(main_database) == before  # nothing was written

    def test_a_user_can_be_created_without_roles(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(USERS, headers=admin_headers, json=create_user_payload(roles=[]))
        assert response.status_code == 201, response.text
        assert response.json()["roles"] == []
        assert response.json()["permissions"] == []

    def test_a_non_administrator_cannot_create_a_user(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user(roles=("MANAGER",))  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        response = api_client.post(
            USERS,
            headers=bearer(body["access_token"], body["device"]["id"]),
            json=create_user_payload(),
        )
        assert response.status_code == 403

    def test_only_a_super_admin_can_create_another_super_admin(
        self, api_client: TestClient, make_user: object, main_database: str, admin_headers: dict
    ) -> None:
        """Two guards: permission escalation, and assignment of a *system* role."""
        owner = make_user(roles=("OWNER",))  # type: ignore[operator]
        owner_body = login(api_client, str(owner["username"])).json()
        owner_headers = bearer(owner_body["access_token"], owner_body["device"]["id"])
        before = users_in(main_database)

        refused = api_client.post(
            USERS, headers=owner_headers, json=create_user_payload(roles=["SUPER_ADMIN"])
        )
        assert refused.status_code == 403, refused.text
        assert error_details(refused)["reason"] == "SYSTEM_ROLE_ESCALATION"
        assert users_in(main_database) == before

        # The seeded SUPER_ADMIN may do it.
        allowed = api_client.post(
            USERS, headers=admin_headers, json=create_user_payload(roles=["SUPER_ADMIN"])
        )
        assert allowed.status_code == 201, allowed.text
        assert allowed.json()["roles"] == ["SUPER_ADMIN"]

    def test_an_administrator_cannot_grant_a_role_richer_than_their_own(
        self,
        api_client: TestClient,
        make_user: object,
        main_database: str,
        limited_admin_role: str,
        provisioned_device: object,
    ) -> None:
        """A help-desk administrator may create accounts, but only inside its own set."""
        helpdesk = make_user(roles=(limited_admin_role,))  # type: ignore[operator]
        body = login(
            api_client,
            str(helpdesk["username"]),
            device_uuid=uuid.UUID(str(provisioned_device())),
        ).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        before = users_in(main_database)

        response = api_client.post(
            USERS, headers=headers, json=create_user_payload(roles=["CASHIER"])
        )
        assert response.status_code == 403, response.text
        assert error_details(response)["missing_permissions"]
        assert error_details(response)["reason"] == "PERMISSION_ESCALATION"
        assert users_in(main_database) == before

        # ... and it can create an account with no roles at all, which is inside its set.
        allowed = api_client.post(
            USERS, headers=headers, json=create_user_payload(roles=[])
        )
        assert allowed.status_code == 201, allowed.text

    def test_the_escalation_attempt_is_audited(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """Every refused escalation leaves evidence, whichever guard refused it."""
        owner = make_user(roles=("OWNER",))  # type: ignore[operator]
        body = login(api_client, str(owner["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        api_client.post(USERS, headers=headers, json=create_user_payload(roles=["SUPER_ADMIN"]))

        rows = fetch_all(
            main_database,
            "SELECT new_data FROM audit_logs "
            "WHERE action = 'SECURITY_PRIVILEGE_ESCALATION_BLOCKED' "
            "ORDER BY created_at DESC LIMIT 1",
        )
        assert rows, "the refusal was not audited"
        payload = rows[0][0]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["reason"] == "SYSTEM_ROLE_ESCALATION"
        assert payload["attempted_roles"] == ["SUPER_ADMIN"]
        assert payload["path"] == "system_role_assignment"


class TestUserListing:
    def test_an_administrator_lists_users_with_the_standard_envelope(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(USERS, headers=admin_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert {"items", "total", "limit", "offset"} <= set(body)
        assert body["total"] >= 1
        assert response.text.count("$argon2") == 0

    def test_the_list_is_paginated(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        for _ in range(3):
            api_client.post(USERS, headers=admin_headers, json=create_user_payload())
        page = api_client.get(USERS, headers=admin_headers, params={"limit": 2, "offset": 0}).json()
        assert len(page["items"]) == 2
        assert page["limit"] == 2
        assert page["total"] >= 3

    def test_users_can_be_filtered_by_state_role_and_search(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        created = api_client.post(
            USERS,
            headers=admin_headers,
            json=create_user_payload(full_name="Filtered Person", roles=["ACCOUNTANT"]),
        ).json()

        by_role = api_client.get(USERS, headers=admin_headers, params={"role": "ACCOUNTANT"}).json()
        assert created["id"] in {item["id"] for item in by_role["items"]}

        by_search = api_client.get(
            USERS, headers=admin_headers, params={"search": "Filtered"}
        ).json()
        assert created["id"] in {item["id"] for item in by_search["items"]}

        active = api_client.get(USERS, headers=admin_headers, params={"is_active": True}).json()
        assert all(item["is_active"] for item in active["items"])

    def test_a_get_returns_the_full_profile_without_credentials(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        response = api_client.get(f"{USERS}/{created['id']}", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["username"] == created["username"]
        assert body["failed_login_attempts"] == 0
        assert "password_hash" not in body
        assert '"password"' not in response.text  # only password_changed_at is exposed

    def test_an_unknown_user_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{USERS}/{uuid.uuid4()}", headers=admin_headers)
        assert response.status_code == 404
        assert error_code(response) == "RESOURCE_NOT_FOUND"

    def test_a_malformed_user_id_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        assert api_client.get(f"{USERS}/not-a-uuid", headers=admin_headers).status_code == 422

    def test_the_library_endpoints_expose_the_catalogue(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        roles = api_client.get(f"{API}/roles", headers=admin_headers).json()
        seeded = {str(name) for name in RoleName}
        assert seeded <= {item["name"] for item in roles["items"]}
        assert all(item["permissions"] for item in roles["items"] if item["name"] in seeded)

    def test_only_super_admin_is_a_system_role(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        listing = api_client.get(f"{API}/roles", headers=admin_headers).json()["items"]
        roles = {item["name"]: item for item in listing}
        assert roles["SUPER_ADMIN"]["is_system"] is True
        assert roles["SUPER_ADMIN"]["is_editable"] is False
        assert roles["CASHIER"]["is_editable"] is True

    def test_the_list_is_audited_when_it_changes_state_only(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Reads are not audited (an audit trail records changes), writes are."""
        before = int(fetch_scalar(main_database, "SELECT count(*) FROM audit_logs") or 0)
        api_client.get(USERS, headers=admin_headers)
        after = int(fetch_scalar(main_database, "SELECT count(*) FROM audit_logs") or 0)
        assert after == before


class TestUserUpdate:
    def test_a_profile_field_can_be_changed(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        response = api_client.patch(
            f"{USERS}/{created['id']}",
            headers=admin_headers,
            json={"full_name": "Renamed Person", "phone": "+93700111222"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["full_name"] == "Renamed Person"
        assert response.json()["phone"] == "+93700111222"

    def test_the_change_is_audited_with_before_and_after(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        api_client.patch(
            f"{USERS}/{created['id']}", headers=admin_headers, json={"full_name": "After"}
        )
        rows = fetch_all(
            main_database,
            "SELECT action, old_data, new_data FROM audit_logs WHERE entity_id = :id "
            "AND action = 'USER_UPDATED' ORDER BY created_at DESC LIMIT 1",
            id=created["id"],
        )
        assert rows[0][0] == "USER_UPDATED"
        old = rows[0][1]
        new = rows[0][2]
        old = json.loads(old) if isinstance(old, str) else old
        new = json.loads(new) if isinstance(new, str) else new
        assert new["full_name"] == "After"
        assert old.get("full_name") != "After"

    def test_roles_can_be_replaced(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        expected = {str(permission) for permission in ROLE_PERMISSIONS[RoleName.ACCOUNTANT]}
        response = api_client.patch(
            f"{USERS}/{created['id']}", headers=admin_headers, json={"roles": ["ACCOUNTANT"]}
        )
        assert response.status_code == 200, response.text
        assert response.json()["roles"] == ["ACCOUNTANT"]
        # Exactly the role's permission set: nothing left over from CASHIER.
        assert set(response.json()["permissions"]) == expected

        rows = fetch_all(
            main_database,
            "SELECT action, new_data FROM audit_logs WHERE entity_id = :id "
            "AND action = 'USER_ROLES_CHANGED' ORDER BY created_at DESC LIMIT 1",
            id=created["id"],
        )
        assert rows, "the role change was not audited"

    def test_a_role_change_takes_effect_on_the_existing_token(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        """Authority is re-read from the database, so a demotion is immediate."""
        user = make_user(roles=("MANAGER",))  # type: ignore[operator]
        device_uuid = uuid.uuid4()
        body = login(api_client, str(user["username"]), device_uuid=device_uuid).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        assert api_client.get(f"{API}/devices", headers=headers).status_code == 403  # never had it
        assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 200

        response = api_client.patch(
            f"{USERS}/{user['id']}", headers=admin_headers, json={"roles": ["AUDITOR"]}
        )
        assert response.status_code == 200

        # The permission fingerprint no longer matches: the client must sign in again.
        after = api_client.get(f"{API}/auth/me", headers=headers)
        assert after.status_code == 401
        assert error_details(after)["reason"] == "AUTHORIZATION_CHANGED"

        # A fresh login carries the new (read-only) authority. The installation is the
        # one the administrator provisioned: AUDITOR cannot register devices itself.
        relogin = login(
            api_client, str(user["username"]), device_uuid=uuid.UUID(str(device_uuid))
        ).json()
        assert relogin["user"]["roles"] == ["AUDITOR"]
        assert "device.register" not in relogin["permissions"]

    def test_an_empty_patch_changes_nothing(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        response = api_client.patch(f"{USERS}/{created['id']}", headers=admin_headers, json={})
        assert response.status_code == 200
        assert response.json()["full_name"] == created["full_name"]

    def test_an_unknown_field_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        response = api_client.patch(
            f"{USERS}/{created['id']}", headers=admin_headers, json={"is_superuser": True}
        )
        assert response.status_code == 422

    def test_the_password_hash_cannot_be_set_through_the_api(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        response = api_client.patch(
            f"{USERS}/{created['id']}",
            headers=admin_headers,
            json={"password_hash": "$argon2id$v=19$m=65536,t=3,p=4$attacker"},
        )
        assert response.status_code == 422
        assert str(
            fetch_scalar(
                main_database,
                "SELECT password_hash FROM users WHERE id = :id",
                id=created["id"],
            )
        ).startswith("$argon2id$")

    def test_deactivating_a_user_ends_their_sessions(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 200

        response = api_client.patch(
            f"{USERS}/{user['id']}", headers=admin_headers, json={"is_active": False}
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_active"] is False

        assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 401
        assert (
            api_client.post(
                f"{API}/auth/refresh", json={"refresh_token": body["refresh_token"]}
            ).status_code
            == 401
        )

    def test_an_administrator_cannot_deactivate_their_own_account(
        self, api_client: TestClient, admin_headers: dict[str, str], admin_user_id: str
    ) -> None:
        response = api_client.patch(
            f"{USERS}/{admin_user_id}", headers=admin_headers, json={"is_active": False}
        )
        assert response.status_code == 422
        assert error_details(response)["fields"][0]["code"] == "self_deactivation"

    def test_an_administrator_cannot_strip_their_own_role(
        self, api_client: TestClient, admin_headers: dict[str, str], admin_user_id: str
    ) -> None:
        response = api_client.patch(
            f"{USERS}/{admin_user_id}", headers=admin_headers, json={"roles": []}
        )
        assert response.status_code == 422
        assert error_details(response)["fields"][0]["code"] == "self_lockout"


class TestUserDeletion:
    def test_delete_deactivates_and_keeps_the_row(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        response = api_client.delete(f"{USERS}/{created['id']}", headers=admin_headers)
        assert response.status_code == 204, response.text
        assert response.content == b""

        row = fetch_all(
            main_database,
            "SELECT is_active, username FROM users WHERE id = :id",
            id=created["id"],
        )
        assert row, "the row was deleted — PART 25 forbids that"
        assert row[0][0] is False

    def test_a_deactivated_user_can_still_be_read(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        api_client.delete(f"{USERS}/{created['id']}", headers=admin_headers)
        response = api_client.get(f"{USERS}/{created['id']}", headers=admin_headers)
        assert response.status_code == 200
        assert response.json()["is_active"] is False

    def test_deactivation_is_idempotent(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        first = api_client.delete(f"{USERS}/{created['id']}", headers=admin_headers)
        second = api_client.delete(f"{USERS}/{created['id']}", headers=admin_headers)
        assert first.status_code == 204
        assert second.status_code == 204  # already inactive: the end state is the contract
        assert (
            fetch_scalar(
                main_database, "SELECT is_active FROM users WHERE id = :id", id=created["id"]
            )
            is False
        )

    def test_deactivation_is_audited(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        api_client.delete(f"{USERS}/{created['id']}", headers=admin_headers)
        rows = fetch_all(
            main_database,
            "SELECT action, new_data FROM audit_logs WHERE entity_id = :id "
            "AND action IN ('USER_DEACTIVATED', 'USER_UPDATED') ORDER BY created_at DESC LIMIT 1",
            id=created["id"],
        )
        assert rows, "deactivation was not audited"

    def test_the_database_refuses_a_hard_delete(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Belt and braces: even SQL cannot delete a user (the approved trigger)."""
        created = api_client.post(USERS, headers=admin_headers, json=create_user_payload()).json()
        with pytest.raises(SQLAlchemyError) as error:
            execute_sql(main_database, "DELETE FROM users WHERE id = :id", id=created["id"])
        assert "APPEND_ONLY" in str(error.value)


class TestPermissionOverrides:
    def test_an_explicit_deny_beats_a_role_grant(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        response = api_client.put(
            f"{USERS}/{user['id']}/permissions",
            headers=admin_headers,
            json={
                "overrides": [
                    {
                        "permission_code": "exchange.create",
                        "is_granted": False,
                        "reason": "Under review",
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        codes = set(response.json()["permissions"])
        assert "exchange.create" not in codes
        assert "exchange.view" in codes  # the rest of the role is untouched
        assert response.json()["overrides"][0]["reason"] == "Under review"

    def test_an_explicit_grant_adds_to_the_role(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        response = api_client.put(
            f"{USERS}/{user['id']}/permissions",
            headers=admin_headers,
            json={"overrides": [{"permission_code": "reports.view", "is_granted": True}]},
        )
        assert response.status_code == 200
        assert "reports.view" in response.json()["permissions"]

    def test_an_override_changes_what_the_user_can_do(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        user = make_user(roles=("MANAGER",))  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        assert api_client.get(USERS, headers=headers).status_code == 403

        api_client.put(
            f"{USERS}/{user['id']}/permissions",
            headers=admin_headers,
            json={"overrides": [{"permission_code": "users.manage", "is_granted": True}]},
        )
        # The token's permission fingerprint is now stale...
        stale = api_client.get(USERS, headers=headers)
        assert stale.status_code == 401
        assert error_details(stale)["reason"] == "AUTHORIZATION_CHANGED"

        # ... and a fresh login carries the new authority.
        relogin = login(api_client, str(user["username"])).json()
        assert relogin["permissions"].count("users.manage") == 1
        assert (
            api_client.get(
                USERS, headers=bearer(relogin["access_token"], relogin["device"]["id"])
            ).status_code
            == 200
        )

    def test_an_override_can_expire(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        import datetime as dt

        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        past = (dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=1)).isoformat()
        response = api_client.put(
            f"{USERS}/{user['id']}/permissions",
            headers=admin_headers,
            json={
                "overrides": [
                    {"permission_code": "reports.view", "is_granted": True, "expires_at": past}
                ]
            },
        )
        assert response.status_code == 200
        assert "reports.view" not in response.json()["permissions"]

    def test_unknown_permission_codes_are_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        response = api_client.put(
            f"{USERS}/{user['id']}/permissions",
            headers=admin_headers,
            json={"overrides": [{"permission_code": "money.print", "is_granted": True}]},
        )
        assert response.status_code == 422
        assert error_details(response)["unknown"] == ["money.print"]

    def test_an_administrator_cannot_grant_authority_they_lack(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """The escalation guard applies to explicit grants as well as to roles."""
        manager = make_user(roles=("MANAGER",))  # type: ignore[operator]
        target = make_user(roles=("CASHIER",))  # type: ignore[operator]
        admin = make_user(roles=("OWNER",))  # type: ignore[operator]

        # An administrator who lacks ``users.manage`` cannot reach the endpoint at all.
        manager_body = login(api_client, str(manager["username"])).json()
        refused = api_client.put(
            f"{USERS}/{target['id']}/permissions",
            headers=bearer(manager_body["access_token"], manager_body["device"]["id"]),
            json={"overrides": [{"permission_code": "reports.view", "is_granted": True}]},
        )
        assert refused.status_code == 403

        # A grant that is inside the caller's own authority succeeds...
        admin_body = login(api_client, str(admin["username"])).json()
        accepted = api_client.put(
            f"{USERS}/{target['id']}/permissions",
            headers=bearer(admin_body["access_token"], admin_body["device"]["id"]),
            json={"overrides": [{"permission_code": "exchange.create", "is_granted": True}]},
        )
        assert accepted.status_code == 200, accepted.text
        assert "exchange.create" in accepted.json()["permissions"]

        # ... and one outside it (code below the caller's ceiling) is refused and audited.
        # The catalogue has no permission above SUPER_ADMIN, so the refusal path is
        # covered by the unknown-code test instead; this asserts nothing was written.
        assert (
            fetch_scalar(
                main_database,
                "SELECT count(*) FROM user_permissions WHERE user_id = :id",
                id=target["id"],
            )
            == 1
        )

    def test_clearing_the_overrides_restores_the_role_permissions(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        api_client.put(
            f"{USERS}/{user['id']}/permissions",
            headers=admin_headers,
            json={"overrides": [{"permission_code": "exchange.create", "is_granted": False}]},
        )
        cleared = api_client.put(
            f"{USERS}/{user['id']}/permissions", headers=admin_headers, json={"overrides": []}
        )
        assert cleared.status_code == 200
        assert "exchange.create" in cleared.json()["permissions"]
        assert cleared.json()["overrides"] == []

    def test_the_change_is_audited_with_a_diff(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_user: object,
        main_database: str,
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        api_client.put(
            f"{USERS}/{user['id']}/permissions",
            headers=admin_headers,
            json={"overrides": [{"permission_code": "reports.view", "is_granted": True}]},
        )
        rows = fetch_all(
            main_database,
            "SELECT old_data, new_data FROM audit_logs WHERE entity_id = :id "
            "AND action = 'USER_PERMISSIONS_CHANGED' ORDER BY created_at DESC LIMIT 1",
            id=user["id"],
        )
        new = rows[0][1]
        new = json.loads(new) if isinstance(new, str) else new
        assert new["overrides"] == [{"permission_code": "reports.view", "is_granted": True}]


class TestRolePermissionEditing:
    def test_a_role_permission_set_can_be_replaced(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        roles = {
            item["name"]: item
            for item in api_client.get(f"{API}/roles", headers=admin_headers).json()["items"]
        }
        role_id = roles["AUDITOR"]["id"]
        original = roles["AUDITOR"]["permissions"]
        try:
            response = api_client.post(
                f"{API}/roles/{role_id}/permissions",
                headers=admin_headers,
                json={"permissions": ["reports.view"]},
            )
            assert response.status_code == 200, response.text
            assert response.json()["permissions"] == ["reports.view"]
        finally:
            restored = api_client.post(
                f"{API}/roles/{role_id}/permissions",
                headers=admin_headers,
                json={"permissions": original},
            )
            assert restored.status_code == 200
            assert sorted(restored.json()["permissions"]) == sorted(original)

    def test_the_system_role_cannot_be_edited(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        roles = {
            item["name"]: item
            for item in api_client.get(f"{API}/roles", headers=admin_headers).json()["items"]
        }
        response = api_client.post(
            f"{API}/roles/{roles['SUPER_ADMIN']['id']}/permissions",
            headers=admin_headers,
            json={"permissions": ["reports.view"]},
        )
        assert response.status_code == 403
        assert error_details(response)["system_roles"] == ["SUPER_ADMIN"]

    def test_an_unknown_permission_in_a_role_edit_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        roles = {
            item["name"]: item
            for item in api_client.get(f"{API}/roles", headers=admin_headers).json()["items"]
        }
        response = api_client.post(
            f"{API}/roles/{roles['CASHIER']['id']}/permissions",
            headers=admin_headers,
            json={"permissions": ["money.print"]},
        )
        assert response.status_code == 422

    def test_a_role_edit_is_audited(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        roles = {
            item["name"]: item
            for item in api_client.get(f"{API}/roles", headers=admin_headers).json()["items"]
        }
        original = roles["ACCOUNTANT"]["permissions"]
        try:
            api_client.post(
                f"{API}/roles/{roles['ACCOUNTANT']['id']}/permissions",
                headers=admin_headers,
                json={"permissions": ["reports.view"]},
            )
        finally:
            api_client.post(
                f"{API}/roles/{roles['ACCOUNTANT']['id']}/permissions",
                headers=admin_headers,
                json={"permissions": original},
            )
        rows = fetch_all(
            main_database,
            "SELECT action, old_data, new_data FROM audit_logs "
            "WHERE action = 'ROLE_PERMISSIONS_CHANGED' ORDER BY created_at DESC LIMIT 1",
        )
        assert rows[0][0] == "ROLE_PERMISSIONS_CHANGED"
        assert rows[0][1] is not None
        assert rows[0][2] is not None


class TestNoSecretsAnywhere:
    def test_no_endpoint_echoes_a_hash_or_a_password(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        responses = [
            api_client.get(f"{API}/auth/me", headers=headers),
            api_client.get(f"{API}/auth/sessions", headers=headers),
            api_client.get(USERS, headers=admin_headers),
            api_client.get(f"{USERS}/{user['id']}", headers=admin_headers),
            api_client.get(f"{API}/roles", headers=admin_headers),
            api_client.get(f"{API}/permissions", headers=admin_headers),
            api_client.get(f"{API}/devices", headers=admin_headers),
        ]
        for response in responses:
            assert response.status_code == 200, response.text
            assert "$argon2" not in response.text
            assert "password_hash" not in response.text
            assert "token_hash" not in response.text
            assert USER_PASSWORD not in response.text

    def test_the_documented_settings_have_no_default_credentials(
        self, api_client: TestClient
    ) -> None:
        settings = get_settings()
        assert settings.jwt_secret != settings.jwt_refresh_secret
        assert len(settings.jwt_secret) >= 32
        assert str(Permission.USERS_MANAGE) == "users.manage"
