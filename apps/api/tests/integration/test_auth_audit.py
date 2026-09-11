"""Audit evidence produced by the authentication and administration paths.

PART 18 requires an append-only trail of who did what, from where and in which request.
Phase 2 adds its own actions, and this module checks the properties that matter after a
full lifecycle has been exercised: the hash chain still verifies, every action is
attributed, and no credential ever reaches the evidence trail.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.auth_helpers import (
    API,
    DEVICES,
    USER_PASSWORD,
    USERS,
    bearer,
    login,
    refresh,
)
from tests.helpers import fetch_all, fetch_scalar

pytestmark = pytest.mark.integration


def exercise_a_shift(
    api_client: TestClient,
    admin_headers: dict[str, str],
    branch_id: str,
    *,
    username: str,
) -> str:
    """Drive one full authentication/administration lifecycle and return the user id."""
    created = api_client.post(
        USERS,
        headers=admin_headers,
        json={
            "username": username,
            "password": USER_PASSWORD,
            "full_name": "Audit Subject",
            "roles": ["CASHIER"],
            "must_change_password": False,
        },
    ).json()
    user_id = str(created["id"])

    body = login(api_client, username).json()
    refresh(api_client, str(body["refresh_token"]))
    refresh(api_client, str(body["refresh_token"]), expect=401)  # reuse → family revoked

    second = login(api_client, username).json()
    second_headers = bearer(second["access_token"], second["device"]["id"])
    api_client.post(f"{API}/auth/sessions/{body['session_id']}/revoke", headers=second_headers)
    api_client.post(f"{API}/auth/logout", json={}, headers=second_headers)

    login(api_client, username, "Wrong-Passw0rd-2026!", expect=401)
    for _ in range(get_settings().login_max_failed_attempts):
        login(api_client, username, "Wrong-Passw0rd-2026!", expect=401)

    api_client.patch(
        f"{USERS}/{user_id}", headers=admin_headers, json={"full_name": "Audit Subject II"}
    )
    api_client.put(
        f"{USERS}/{user_id}/permissions",
        headers=admin_headers,
        json={"overrides": [{"permission_code": "reports.view", "is_granted": True}]},
    )
    device = api_client.post(
        f"{DEVICES}/register",
        headers=admin_headers,
        json={
            "device_uuid": str(uuid.uuid4()),
            "device_name": "Audit Counter",
            "platform": "WINDOWS",
            "branch_id": branch_id,
        },
    ).json()
    api_client.post(
        f"{DEVICES}/{device['id']}/revoke", headers=admin_headers, json={"reason": "audit"}
    )
    api_client.delete(f"{USERS}/{user_id}", headers=admin_headers)
    return user_id


class TestAuditCompleteness:
    def test_every_phase_two_action_is_recorded(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        branch_id: str,
        main_database: str,
    ) -> None:
        exercise_a_shift(api_client, admin_headers, branch_id, username="audit-subject")
        actions = {
            str(row[0])
            for row in fetch_all(
                main_database,
                "SELECT DISTINCT action FROM audit_logs WHERE action IN ("
                "'AUTH_LOGIN_SUCCEEDED','AUTH_LOGIN_FAILED','AUTH_LOGIN_DENIED',"
                "'AUTH_LOCKOUT','AUTH_LOGOUT','AUTH_REFRESH_ROTATED',"
                "'SECURITY_REFRESH_REUSE_DETECTED','SECURITY_SESSION_REVOKED',"
                "'USER_CREATED','USER_UPDATED','USER_DEACTIVATED',"
                "'USER_PERMISSIONS_CHANGED','DEVICE_REGISTERED','DEVICE_REVOKED')",
            )
        }
        expected = {
            "AUTH_LOGIN_SUCCEEDED",
            "AUTH_LOGIN_FAILED",
            "AUTH_LOCKOUT",
            "AUTH_LOGOUT",
            "AUTH_REFRESH_ROTATED",
            "SECURITY_REFRESH_REUSE_DETECTED",
            "SECURITY_SESSION_REVOKED",
            "USER_CREATED",
            "USER_UPDATED",
            "USER_DEACTIVATED",
            "USER_PERMISSIONS_CHANGED",
            "DEVICE_REGISTERED",
            "DEVICE_REVOKED",
        }
        assert expected <= actions

    def test_the_hash_chain_still_verifies(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        branch_id: str,
        main_database: str,
    ) -> None:
        exercise_a_shift(api_client, admin_headers, branch_id, username="audit-chain")
        assert fetch_scalar(main_database, "SELECT count(*) FROM verify_audit_chain()") == 0

    def test_no_credential_reaches_the_trail(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        branch_id: str,
        main_database: str,
    ) -> None:
        exercise_a_shift(api_client, admin_headers, branch_id, username="audit-secrets")
        rows = fetch_all(
            main_database,
            "SELECT action, COALESCE(old_data::text, '') || COALESCE(new_data::text, '') "
            "FROM audit_logs WHERE action LIKE 'AUTH_%' OR action LIKE 'USER_%' "
            "OR action LIKE 'DEVICE_%' OR action LIKE 'SECURITY_%'",
        )
        for action, payload in rows:
            assert "$argon2" not in payload, action
            assert "rt_" not in payload, action
            assert USER_PASSWORD not in payload, action
            assert "password_hash" not in payload, action

    def test_each_action_names_an_actor_where_one_exists(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        branch_id: str,
        main_database: str,
    ) -> None:
        """Pre-authentication events may have no user; everything else is attributed."""
        exercise_a_shift(api_client, admin_headers, branch_id, username="audit-actor")
        rows = fetch_all(
            main_database,
            "SELECT action, count(*) FILTER (WHERE user_id IS NULL) AS anonymous, count(*) "
            "FROM audit_logs WHERE action LIKE 'AUTH_%' OR action LIKE 'USER_%' "
            "OR action LIKE 'DEVICE_%' OR action LIKE 'SECURITY_%' GROUP BY action",
        )
        allowed_anonymous = {
            "AUTH_LOGIN_FAILED",  # wrong password / unknown user
            "AUTH_LOGIN_DENIED",  # refused before a session exists
            "AUTH_LOCKOUT",  # recorded with the attempt
            "AUTH_REFRESH_ROTATED",  # the refresh token is the credential
            "AUTH_REFRESH_FAILED",
            "SECURITY_REFRESH_REUSE_DETECTED",
            "DEVICE_REGISTRATION_DENIED",
        }
        for action, anonymous, total in rows:
            if action in allowed_anonymous:
                continue
            assert anonymous == 0, f"{action} has {anonymous}/{total} unattributed rows"

    def test_the_request_id_is_recorded(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
    ) -> None:
        created = api_client.post(
            USERS,
            headers=admin_headers,
            json={
                "username": "audit-request-id",
                "password": USER_PASSWORD,
                "full_name": "Request Id",
                "roles": [],
                "must_change_password": False,
            },
        )
        assert created.status_code == 201
        rows = fetch_all(
            main_database,
            "SELECT request_id FROM audit_logs WHERE entity_id = :id AND action = 'USER_CREATED'",
            id=created.json()["id"],
        )
        assert rows[0][0]

    def test_a_non_address_peer_is_stored_as_null(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
    ) -> None:
        """A non-address peer (in-process transport) is stored as NULL, not as garbage."""
        created = api_client.post(
            USERS,
            headers=admin_headers,
            json={
                "username": "audit-ip",
                "password": USER_PASSWORD,
                "full_name": "Ip",
                "roles": [],
                "must_change_password": False,
            },
        )
        assert created.status_code == 201
        rows = fetch_all(
            main_database,
            "SELECT ip_address FROM audit_logs WHERE entity_id = :id",
            id=created.json()["id"],
        )
        assert rows[0][0] is None

    def test_reads_are_not_audited(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """The trail records changes and security events, not every GET."""
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        before = fetch_scalar(main_database, "SELECT count(*) FROM audit_logs")
        for _ in range(5):
            api_client.get(f"{API}/auth/me", headers=headers)
            api_client.get(f"{API}/auth/sessions", headers=headers)
        assert fetch_scalar(main_database, "SELECT count(*) FROM audit_logs") == before

    def test_a_failed_escalation_is_recorded_even_though_the_request_fails(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        owner = make_user(roles=("OWNER",))  # type: ignore[operator]
        body = login(api_client, str(owner["username"])).json()
        response = api_client.post(
            USERS,
            headers=bearer(body["access_token"], body["device"]["id"]),
            json={
                "username": "audit-escalation",
                "password": USER_PASSWORD,
                "full_name": "Escalation",
                "roles": ["SUPER_ADMIN"],
                "must_change_password": False,
            },
        )
        assert response.status_code == 403
        rows = fetch_all(
            main_database,
            "SELECT new_data FROM audit_logs "
            "WHERE action = 'SECURITY_PRIVILEGE_ESCALATION_BLOCKED' "
            "ORDER BY created_at DESC LIMIT 1",
        )
        assert rows, "the refusal left no evidence"
        payload = rows[0][0]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["attempted_roles"] == ["SUPER_ADMIN"]

    def test_the_trail_cannot_be_rewritten(
        self, api_client: TestClient, main_database: str
    ) -> None:
        """Append-only is enforced by the database, not by convention."""
        from sqlalchemy.exc import SQLAlchemyError

        from tests.helpers import execute_sql

        with pytest.raises(SQLAlchemyError) as error:
            execute_sql(main_database, "UPDATE audit_logs SET action = 'FORGED'")
        assert "APPEND_ONLY" in str(error.value)

    def test_the_trail_cannot_be_deleted(self, api_client: TestClient, main_database: str) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        from tests.helpers import execute_sql

        with pytest.raises(SQLAlchemyError) as error:
            execute_sql(main_database, "DELETE FROM audit_logs")
        assert "APPEND_ONLY" in str(error.value)
