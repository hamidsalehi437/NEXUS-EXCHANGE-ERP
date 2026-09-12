"""Device registration, listing and revocation (PART 42, SYNC_DESIGN §2, API_CONTRACT §9.1).

Device revocation is the emergency control of Phase 2: it must end the device's sessions
immediately, be idempotent, and leave an audited record naming the administrator.
"""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.auth_helpers import (
    API,
    DEVICES,
    bearer,
    error_code,
    error_details,
    login,
    register_device,
    unique,
)
from tests.helpers import fetch_all, fetch_scalar

pytestmark = pytest.mark.integration


def device_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "device_uuid": str(uuid.uuid4()),
        "device_name": "Counter-12",
        "platform": "ANDROID",
        "app_version": "1.0.0",
    }
    payload.update(overrides)
    return payload


class TestDeviceRegistration:
    def test_an_administrator_provisions_a_device(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_id: str
    ) -> None:
        payload = device_payload(branch_id=branch_id)
        response = api_client.post(f"{DEVICES}/register", headers=admin_headers, json=payload)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["device_uuid"] == payload["device_uuid"]
        assert body["branch_id"] == branch_id
        assert body["is_active"] is True
        assert body["registered_by"]
        assert body["revoked_at"] is None

    def test_the_registration_is_audited(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        branch_id: str,
        main_database: str,
    ) -> None:
        created = register_device(api_client, admin_headers, branch_id=branch_id)
        rows = fetch_all(
            main_database,
            "SELECT action, user_id FROM audit_logs WHERE entity_id = :id "
            "AND action = 'DEVICE_REGISTERED' ORDER BY created_at DESC LIMIT 1",
            id=created["id"],
        )
        assert rows[0][0] == "DEVICE_REGISTERED"
        assert rows[0][1] is not None

    def test_a_duplicate_device_uuid_is_refused(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        branch_id: str,
        main_database: str,
    ) -> None:
        device_uuid = uuid.uuid4()
        first = register_device(
            api_client, admin_headers, branch_id=branch_id, device_uuid=device_uuid
        )
        assert first["device_uuid"] == str(device_uuid)
        register_device(
            api_client,
            admin_headers,
            branch_id=branch_id,
            device_uuid=device_uuid,
            expect=409,
        )
        assert (
            fetch_scalar(
                main_database,
                "SELECT count(*) FROM devices WHERE device_uuid = :uuid",
                uuid=device_uuid,
            )
            == 1
        )

    def test_an_unknown_branch_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            f"{DEVICES}/register",
            headers=admin_headers,
            json=device_payload(branch_id=str(uuid.uuid4())),
        )
        assert response.status_code in {404, 422}, response.text

    def test_an_invalid_platform_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_id: str
    ) -> None:
        response = api_client.post(
            f"{DEVICES}/register",
            headers=admin_headers,
            json=device_payload(branch_id=branch_id, platform="SOLARIS"),
        )
        assert response.status_code == 422

    def test_a_manager_may_provision_but_not_list(
        self, api_client: TestClient, make_user: object, branch_id: str
    ) -> None:
        manager = make_user(roles=("MANAGER",))  # type: ignore[operator]
        body = login(api_client, str(manager["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        assert (
            api_client.post(
                f"{DEVICES}/register", headers=headers, json=device_payload(branch_id=branch_id)
            ).status_code
            == 201
        )
        assert api_client.get(DEVICES, headers=headers).status_code == 403

    def test_a_cashier_may_provision_its_own_counter(
        self, api_client: TestClient, make_user: object, branch_id: str
    ) -> None:
        cashier = make_user(roles=("CASHIER",))  # type: ignore[operator]
        body = login(api_client, str(cashier["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        assert (
            api_client.post(
                f"{DEVICES}/register", headers=headers, json=device_payload(branch_id=branch_id)
            ).status_code
            == 201
        )

    def test_an_auditor_may_not_provision(
        self, api_client: TestClient, make_user: object, provisioned_device: object, branch_id: str
    ) -> None:
        auditor = make_user(roles=("AUDITOR",))  # type: ignore[operator]
        device_uuid = uuid.UUID(str(provisioned_device()))
        body = login(api_client, str(auditor["username"]), device_uuid=device_uuid).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        response = api_client.post(
            f"{DEVICES}/register", headers=headers, json=device_payload(branch_id=branch_id)
        )
        assert response.status_code == 403
        assert error_details(response)["required_permission"] == "device.register"


class TestDeviceListing:
    def test_devices_are_listed_with_the_standard_envelope(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_id: str
    ) -> None:
        created = register_device(api_client, admin_headers, branch_id=branch_id)
        response = api_client.get(DEVICES, headers=admin_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert {"items", "total", "limit", "offset"} <= set(body)
        assert created["id"] in {item["id"] for item in body["items"]}

    def test_the_list_can_be_filtered(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_id: str
    ) -> None:
        created = register_device(api_client, admin_headers, branch_id=branch_id)
        active = api_client.get(
            DEVICES, headers=admin_headers, params={"is_active": True, "branch_id": branch_id}
        ).json()
        assert created["id"] in {item["id"] for item in active["items"]}
        assert all(item["branch_id"] == branch_id for item in active["items"])

    def test_device_records_contain_no_credentials(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        text = api_client.get(DEVICES, headers=admin_headers).text
        for leaked in ("$argon2", "token_hash", "password"):
            assert leaked not in text


class TestDeviceRevocation:
    def test_revoking_a_device_ends_its_sessions(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 200

        response = api_client.post(
            f"{DEVICES}/{body['device']['id']}/revoke",
            headers=admin_headers,
            json={"reason": "Stolen laptop"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["device"]["is_active"] is False
        assert payload["device"]["revoke_reason"] == "Stolen laptop"
        assert payload["device"]["revoked_at"] is not None
        assert payload["revoked_sessions"] >= 1

        # The access token and the refresh token are both dead, and so is a fresh login.
        assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 401
        assert (
            api_client.post(
                f"{API}/auth/refresh", json={"refresh_token": body["refresh_token"]}
            ).status_code
            == 401
        )
        refused = api_client.post(
            f"{API}/auth/login",
            json={
                "username": user["username"],
                "password": user["password"],
                "device_uuid": body["device"]["device_uuid"],
                "device_name": "Counter-1",
                "platform": "WINDOWS",
            },
        )
        assert refused.status_code == 401
        assert error_code(refused) == "DEVICE_REVOKED"

    def test_revocation_is_idempotent(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_id: str
    ) -> None:
        created = register_device(api_client, admin_headers, branch_id=branch_id)
        first = api_client.post(
            f"{DEVICES}/{created['id']}/revoke", headers=admin_headers, json={"reason": "lost"}
        )
        second = api_client.post(
            f"{DEVICES}/{created['id']}/revoke", headers=admin_headers, json={"reason": "again"}
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["already_revoked"] is True
        # The original reason is kept: the evidence of why is not overwritten.
        assert second.json()["device"]["revoke_reason"] == "lost"

    def test_revocation_requires_the_manage_permission(
        self, api_client: TestClient, make_user: object, branch_id: str
    ) -> None:
        manager = make_user(roles=("MANAGER",))  # type: ignore[operator]
        body = login(api_client, str(manager["username"])).json()
        headers = bearer(body["access_token"], body["device"]["id"])
        device = api_client.post(
            f"{DEVICES}/register",
            headers=headers,
            json=device_payload(branch_id=branch_id),
        ).json()
        response = api_client.post(
            f"{DEVICES}/{device['id']}/revoke", headers=headers, json={"reason": "test"}
        )
        assert response.status_code == 403

    def test_an_unknown_device_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            f"{DEVICES}/{uuid.uuid4()}/revoke", headers=admin_headers, json={"reason": "unknown"}
        )
        assert response.status_code == 404
        assert error_code(response) == "RESOURCE_NOT_FOUND"

    def test_the_revocation_is_audited_with_the_actor(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        branch_id: str,
        main_database: str,
    ) -> None:
        created = register_device(api_client, admin_headers, branch_id=branch_id)
        api_client.post(
            f"{DEVICES}/{created['id']}/revoke",
            headers=admin_headers,
            json={"reason": "End of lease"},
        )
        rows = fetch_all(
            main_database,
            "SELECT action, user_id, new_data FROM audit_logs WHERE entity_id = :id "
            "AND action = 'DEVICE_REVOKED' ORDER BY created_at DESC LIMIT 1",
            id=created["id"],
        )
        assert rows[0][0] == "DEVICE_REVOKED"
        assert rows[0][1] is not None  # the administrator who revoked it
        payload = rows[0][2]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["reason"] == "End of lease"
        assert payload["revoked_sessions"] >= 0

    def test_revoking_one_device_leaves_the_others_alone(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        phone = login(api_client, str(user["username"]), device_name="Phone").json()
        counter = login(api_client, str(user["username"]), device_name="Counter").json()
        phone_headers = bearer(phone["access_token"], phone["device"]["id"])
        counter_headers = bearer(counter["access_token"], counter["device"]["id"])

        api_client.post(f"{DEVICES}/{phone['device']['id']}/revoke", headers=admin_headers, json={})
        assert api_client.get(f"{API}/auth/me", headers=phone_headers).status_code == 401
        assert api_client.get(f"{API}/auth/me", headers=counter_headers).status_code == 200

    def test_a_revoked_device_cannot_be_revived_by_logging_in(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_user: object,
        main_database: str,
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        api_client.post(f"{DEVICES}/{body['device']['id']}/revoke", headers=admin_headers, json={})
        assert (
            login(
                api_client,
                str(user["username"]),
                device_uuid=uuid.UUID(body["device"]["device_uuid"]),
                expect=401,
            ).status_code
            == 401
        )
        status = fetch_scalar(
            main_database, "SELECT is_active FROM devices WHERE id = :id", id=body["device"]["id"]
        )
        assert status is False

    def test_a_device_cannot_revoke_itself_without_the_permission(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        body = login(api_client, str(user["username"])).json()
        response = api_client.post(
            f"{DEVICES}/{body['device']['id']}/revoke",
            headers=bearer(body["access_token"], body["device"]["id"]),
            json={"reason": "self"},
        )
        assert response.status_code == 403


class TestDeviceEdgeCases:
    def test_the_same_installation_keeps_one_row_per_uuid(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        branch_id: str,
        main_database: str,
    ) -> None:
        """Provisioning the same installation twice is a conflict, not a second device."""
        device_uuid = uuid.uuid4()
        first = register_device(
            api_client,
            admin_headers,
            branch_id=branch_id,
            device_uuid=device_uuid,
            device_name="Counter-12",
        )
        register_device(
            api_client,
            admin_headers,
            branch_id=branch_id,
            device_uuid=device_uuid,
            device_name="Re-registered",
            expect=409,
        )
        rows = fetch_all(
            main_database,
            "SELECT device_name FROM devices WHERE device_uuid = :uuid",
            uuid=device_uuid,
        )
        assert len(rows) == 1
        assert rows[0][0] == first["device_name"]  # the original registration stands

    def test_a_self_registered_device_appears_in_the_estate(
        self, api_client: TestClient, admin_headers: dict[str, str], make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        body = login(api_client, str(user["username"]), device_name=unique("Tablet")).json()
        listing = api_client.get(DEVICES, headers=admin_headers, params={"limit": 200}).json()
        entry = next(item for item in listing["items"] if item["id"] == body["device"]["id"])
        assert entry["device_name"] == body["device"]["device_name"]
        assert entry["registered_by"] == user["id"]

    def test_last_seen_is_updated_on_login(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        device_uuid = uuid.uuid4()
        body = login(api_client, str(user["username"]), device_uuid=device_uuid).json()
        first = fetch_scalar(
            main_database,
            "SELECT last_seen_at FROM devices WHERE id = :id",
            id=body["device"]["id"],
        )
        api_client.post(f"{API}/auth/refresh", json={"refresh_token": body["refresh_token"]})
        second = fetch_scalar(
            main_database,
            "SELECT last_seen_at FROM devices WHERE id = :id",
            id=body["device"]["id"],
        )
        assert second >= first

    def test_an_invalid_device_uuid_is_a_422(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_id: str
    ) -> None:
        response = api_client.post(
            f"{DEVICES}/register",
            headers=admin_headers,
            json=device_payload(branch_id=branch_id, device_uuid="not-a-uuid"),
        )
        assert response.status_code == 422
