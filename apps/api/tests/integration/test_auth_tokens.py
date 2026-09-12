"""Access/refresh token lifecycle: expiry, rotation, reuse detection, revocation.

PART 24 and PART 42 require refresh rotation with reuse detection, immediate revocation
and session management. These tests drive the endpoints and then inspect the database, so
they prove both the API contract and the state it leaves behind.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.auth_helpers import (
    API,
    DEVICE_ID_HEADER,
    USER_PASSWORD,
    bearer,
    error_code,
    error_details,
    forge_access_token,
    login,
    refresh,
    unique,
)
from tests.helpers import execute_sql, fetch_all, fetch_scalar


def fresh_session(
    client: TestClient, user: dict[str, object], **kwargs: object
) -> tuple[dict[str, object], dict[str, object], uuid.UUID]:
    """Log in and return ``(tokens, headers, device_uuid)``."""
    device_uuid = kwargs.pop("device_uuid", None) or uuid.uuid4()
    body = login(client, str(user["username"]), device_uuid=device_uuid, **kwargs).json()  # type: ignore[arg-type]
    headers = bearer(body["access_token"], body["device"]["id"])
    return body, headers, device_uuid


pytestmark = pytest.mark.integration


class TestAccessTokenUse:
    def test_the_token_authorises_a_protected_endpoint(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user(roles=("MANAGER",))  # type: ignore[operator]
        _, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        response = api_client.get(f"{API}/auth/me", headers=headers)
        assert response.status_code == 200, response.text

    def test_no_token_is_401_with_the_error_envelope(self, api_client: TestClient) -> None:
        response = api_client.get(f"{API}/auth/me")
        assert response.status_code == 401
        # A missing credential is a token problem in this contract (API_CONTRACT §4):
        # there is no "authenticate first" code to confuse a client's retry logic.
        assert error_code(response) == "TOKEN_INVALID"
        assert response.json()["error"]["message"]

    def test_a_token_without_the_required_permission_is_403(
        self, api_client: TestClient, make_user: object, provisioned_device: object
    ) -> None:
        # AUDITOR has no device.register: an administrator provisions its installation.
        user = make_user(roles=("AUDITOR",))  # type: ignore[operator]
        _, headers, _ = fresh_session(  # type: ignore[arg-type]
            api_client, user, device_uuid=uuid.UUID(str(provisioned_device()))
        )
        response = api_client.get(f"{API}/users", headers=headers)
        assert response.status_code == 403
        assert error_code(response) == "PERMISSION_DENIED"

    def test_a_malformed_authorization_header_is_401(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        _, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        for value in ("Token abc", "Bearer", "Bearer  ", "Bearer not-a-jwt"):
            headers_copy = dict(headers)
            headers_copy["Authorization"] = value
            response = api_client.get(f"{API}/auth/me", headers=headers_copy)
            assert response.status_code == 401, value
            assert error_code(response) in {"TOKEN_INVALID", "AUTHENTICATION_REQUIRED"}

    def test_an_expired_access_token_is_refused(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        tokens, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        expired = forge_access_token(
            user_id=str(user["id"]),
            session_id=str(tokens["session_id"]),
            device_id=str(tokens["device"]["id"]),
            expires_in_seconds=-1,
            issued_seconds_ago=3600,
        )
        response = api_client.get(
            f"{API}/auth/me",
            headers={
                "Authorization": f"Bearer {expired}",
                DEVICE_ID_HEADER: headers[DEVICE_ID_HEADER],
            },
        )
        assert response.status_code == 401
        assert error_code(response) == "TOKEN_EXPIRED"

    def test_a_token_signed_with_a_foreign_key_is_refused(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        tokens, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        forged = forge_access_token(
            user_id=str(user["id"]),
            session_id=str(tokens["session_id"]),
            secret="not-the-real-signing-key-but-long-enough-for-hs256",
        )
        response = api_client.get(
            f"{API}/auth/me",
            headers={
                "Authorization": f"Bearer {forged}",
                DEVICE_ID_HEADER: headers[DEVICE_ID_HEADER],
            },
        )
        assert response.status_code == 401
        assert error_code(response) == "TOKEN_INVALID"

    def test_a_token_for_a_deleted_session_is_refused(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """A signed, unexpired token whose session row is gone must not be trusted."""
        user = make_user()  # type: ignore[operator]
        tokens, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        execute_sql(
            main_database,
            "DELETE FROM refresh_tokens WHERE family_id = :family",
            family=tokens["session_id"],
        )
        response = api_client.get(f"{API}/auth/me", headers=headers)
        assert response.status_code == 401
        assert error_code(response) in {"TOKEN_REVOKED", "SESSION_NOT_FOUND"}

    def test_the_device_header_must_match_the_bound_device(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        _, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        assert headers[DEVICE_ID_HEADER]
        mismatch = api_client.get(
            f"{API}/auth/me",
            headers={
                "Authorization": headers["Authorization"],
                DEVICE_ID_HEADER: str(uuid.uuid4()),
            },
        )
        assert mismatch.status_code == 401
        assert error_code(mismatch) == "DEVICE_MISMATCH"

    def test_a_changed_permission_set_invalidates_the_access_token(
        self,
        api_client: TestClient,
        make_user: object,
        admin_headers: dict[str, str],
    ) -> None:
        """``perm_hash`` mismatch: the client must sign in again, not run on stale rights.

        The change is an explicit deny override set through the API by an administrator —
        the same database path a real revocation takes — and it is removed again so the
        user's authority is exactly what it was before the test.
        """
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        _, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 200

        override_url = f"{API}/users/{user['id']}/permissions"
        denied = api_client.put(
            override_url,
            headers=admin_headers,
            json={
                "overrides": [
                    {
                        "permission_code": "exchange.create",
                        "is_granted": False,
                        "reason": "Phase 2 test: revoke a grant mid-session",
                    }
                ]
            },
        )
        try:
            assert denied.status_code == 200, denied.text
            response = api_client.get(f"{API}/auth/me", headers=headers)
            assert response.status_code == 401
            assert error_code(response) == "TOKEN_INVALID"
            assert error_details(response)["reason"] == "AUTHORIZATION_CHANGED"
        finally:
            cleaned = api_client.put(override_url, headers=admin_headers, json={"overrides": []})
            assert cleaned.status_code == 200, cleaned.text

        # With the override gone the client signs in again and is back to normal.
        relogin = login(api_client, str(user["username"])).json()
        assert (
            api_client.get(
                f"{API}/auth/me",
                headers=bearer(str(relogin["access_token"]), str(relogin["device"]["id"])),
            ).status_code
            == 200
        )

    def test_a_revoked_device_invalidates_its_live_tokens(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        tokens, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        execute_sql(
            main_database,
            "UPDATE devices SET is_active = false, revoked_at = now() WHERE id = :id",
            id=tokens["device"]["id"],
        )
        response = api_client.get(f"{API}/auth/me", headers=headers)
        assert response.status_code == 401
        assert error_code(response) == "DEVICE_REVOKED"


class TestRefreshRotation:
    def test_refresh_returns_a_new_pair_and_keeps_the_session(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        first, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        second = refresh(api_client, str(first["refresh_token"])).json()

        assert second["access_token"] != first["access_token"]
        assert second["refresh_token"] != first["refresh_token"]
        assert second["session_id"] == first["session_id"]  # same family
        assert second["device"]["id"] == first["device"]["id"]
        assert second["user"]["username"] == user["username"]

    def test_the_old_refresh_token_is_stamped_as_used(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        first, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        refresh(api_client, str(first["refresh_token"]))

        rows = fetch_all(
            main_database,
            "SELECT used_at, revoked_at, replaced_by_id FROM refresh_tokens "
            "WHERE family_id = :family ORDER BY issued_at",
            family=first["session_id"],
        )
        assert rows[0][0] is not None  # the consumed token
        assert rows[0][1] is not None  # rotation retires it, it is not reused
        assert rows[0][2] is not None  # and it points at its successor
        assert rows[1][0] is None  # the live one
        assert rows[1][1] is None  # and it is still usable

    def test_the_rotated_token_hash_is_stored_not_the_token(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        first, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        second = refresh(api_client, str(first["refresh_token"])).json()
        hashes = {
            str(row[0])
            for row in fetch_all(
                main_database,
                "SELECT token_hash FROM refresh_tokens WHERE family_id = :family",
                family=first["session_id"],
            )
        }
        assert str(second["refresh_token"]) not in hashes
        assert len(hashes) == 2  # one per rotation

    def test_a_chain_of_rotations_stays_in_one_family(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        current, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        for _ in range(3):
            current = refresh(api_client, str(current["refresh_token"])).json()
        rows = fetch_scalar(
            main_database,
            "SELECT count(*) FROM refresh_tokens WHERE family_id = :family",
            family=current["session_id"],
        )
        assert rows == 4  # initial + three rotations
        # and the newest token still works through the API
        assert (
            api_client.get(
                f"{API}/auth/me",
                headers=bearer(str(current["access_token"]), str(current["device"]["id"])),
            ).status_code
            == 200
        )

    def test_refresh_is_audited(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        first, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        refresh(api_client, str(first["refresh_token"]))
        rows = fetch_all(
            main_database,
            "SELECT action, user_id, entity_type, new_data FROM audit_logs "
            "ORDER BY created_at DESC LIMIT 1",
        )
        assert rows[0][0] == "AUTH_REFRESH_ROTATED"
        # The refresh endpoint is unauthenticated (the token is the credential), so the
        # row carries no user_id; the session and device identify the actor.
        assert rows[0][1] is None
        assert rows[0][2] == "refresh_token"
        payload = rows[0][3]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["session_id"] == first["session_id"]
        assert payload["successor_token_id"]

    def test_a_refresh_token_cannot_be_used_as_an_access_token(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        tokens, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        response = api_client.get(
            f"{API}/auth/me",
            headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
        )
        assert response.status_code == 401
        assert error_code(response) == "TOKEN_INVALID"

    def test_an_unknown_refresh_token_is_refused(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        fresh_session(api_client, user)  # type: ignore[arg-type]
        response = refresh(api_client, "rt_" + "A" * 43, expect=401)
        assert error_code(response) == "TOKEN_INVALID"

    def test_a_malformed_refresh_token_is_refused_before_any_lookup(
        self, api_client: TestClient
    ) -> None:
        response = refresh(api_client, "rt_short", expect=422)
        assert error_code(response) == "VALIDATION_ERROR"

    def test_the_refresh_token_must_belong_to_the_stated_device(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        tokens, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        response = refresh(
            api_client, str(tokens["refresh_token"]), device_uuid=uuid.uuid4(), expect=401
        )
        assert error_code(response) == "DEVICE_MISMATCH"


class TestReuseDetection:
    def test_reusing_a_consumed_refresh_token_revokes_the_whole_family(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        first, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        second = refresh(api_client, str(first["refresh_token"])).json()

        reused = refresh(api_client, str(first["refresh_token"]), expect=401)
        assert error_code(reused) == "TOKEN_REVOKED"

        # The rotated (legitimate) token is dead too: the family was revoked.
        after = refresh(api_client, str(second["refresh_token"]), expect=401)
        assert error_code(after) == "TOKEN_REVOKED"

        revoked = fetch_scalar(
            main_database,
            "SELECT count(*) FROM refresh_tokens "
            "WHERE family_id = :family AND revoked_at IS NOT NULL",
            family=first["session_id"],
        )
        assert revoked == 2

    def test_two_concurrent_uses_of_one_token_are_serialised(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """A race on one refresh token cannot mint two sessions (concurrency case).

        Two threads present the same token at the same moment. The row lock makes the
        rotation serial: exactly one caller wins, the other is handled as a reuse
        attempt, and reuse detection consequently revokes the winning token too.
        """
        from concurrent.futures import ThreadPoolExecutor

        user = make_user()  # type: ignore[operator]
        session, _, device_uuid = fresh_session(api_client, user)  # type: ignore[arg-type]
        token = str(session["refresh_token"])

        def present() -> Any:
            return refresh(api_client, token, device_uuid=device_uuid, expect=None)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(present), pool.submit(present)
            responses = [first.result(), second.result()]

        assert sorted(response.status_code for response in responses) == [200, 401], [
            response.text for response in responses
        ]
        refused = next(response for response in responses if response.status_code == 401)
        assert error_code(refused) == "TOKEN_REVOKED"

        # The rotation that won is dead as well: the family was revoked by the reuse.
        rotated = next(response for response in responses if response.status_code == 200).json()[
            "refresh_token"
        ]
        assert error_code(refresh(api_client, rotated, expect=401)) == "TOKEN_REVOKED"
        assert (
            fetch_scalar(
                main_database,
                "SELECT count(*) FROM refresh_tokens WHERE family_id = :family "
                "AND revoked_reason = 'REUSE_DETECTED'",
                family=session["session_id"],
            )
            >= 1
        )

    def test_reuse_is_audited_as_a_security_event(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        first, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        refresh(api_client, str(first["refresh_token"]))
        refresh(api_client, str(first["refresh_token"]), expect=401)

        rows = fetch_all(
            main_database,
            "SELECT action, entity_id, new_data FROM audit_logs "
            "WHERE action = 'SECURITY_REFRESH_REUSE_DETECTED' "
            "ORDER BY created_at DESC LIMIT 1",
        )
        assert rows[0][0] == "SECURITY_REFRESH_REUSE_DETECTED"
        assert rows[0][1] is not None
        payload = rows[0][2]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["session_id"] == first["session_id"]
        assert payload["response"] == "SESSION_REVOKED"

    def test_reuse_detection_records_the_reason_on_every_token(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        first, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        refresh(api_client, str(first["refresh_token"]))
        refresh(api_client, str(first["refresh_token"]), expect=401)
        reasons = {
            str(row[0])
            for row in fetch_all(
                main_database,
                "SELECT DISTINCT revoked_reason FROM refresh_tokens WHERE family_id = :family",
                family=first["session_id"],
            )
        }
        # The rotated token says ROTATED, and the reuse sweep rewrites the whole family to
        # REUSE_DETECTED: an operator reading the table sees why each token is dead.
        assert "REUSE_DETECTED" in reasons

    def test_a_revoked_access_token_from_the_family_stops_working(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        first, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        refresh(api_client, str(first["refresh_token"]))
        refresh(api_client, str(first["refresh_token"]), expect=401)  # reuse → revoke family

        response = api_client.get(f"{API}/auth/me", headers=headers)
        assert response.status_code == 401
        assert error_code(response) == "TOKEN_REVOKED"

    def test_reuse_after_logout_is_still_refused(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        first, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        assert api_client.post(f"{API}/auth/logout", json={}, headers=headers).status_code == 200
        response = refresh(api_client, str(first["refresh_token"]), expect=401)
        assert error_code(response) in {"TOKEN_REVOKED", "TOKEN_INVALID"}


class TestLogout:
    def test_logout_ends_the_current_session(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        tokens, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        response = api_client.post(f"{API}/auth/logout", json={}, headers=headers)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["scope"] == "current"
        assert body["session_id"] == tokens["session_id"]
        assert body["revoked_sessions"] >= 1

        # Access token and refresh token are both dead.
        assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 401
        assert refresh(api_client, str(tokens["refresh_token"]), expect=401).status_code == 401

        assert (
            fetch_scalar(
                main_database,
                "SELECT count(*) FROM refresh_tokens "
                "WHERE family_id = :family AND revoked_at IS NOT NULL",
                family=tokens["session_id"],
            )
            >= 1
        )

    def test_logout_is_audited(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        _, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        api_client.post(f"{API}/auth/logout", json={}, headers=headers)
        assert "AUTH_LOGOUT" in {
            str(row[0])
            for row in fetch_all(
                main_database,
                "SELECT action FROM audit_logs WHERE entity_id = :id",
                id=user["id"],
            )
        }

    def test_logout_all_devices_ends_every_session(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        phone, phone_headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        counter, counter_headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        assert phone["session_id"] != counter["session_id"]

        response = api_client.post(
            f"{API}/auth/logout", json={"all_devices": True}, headers=phone_headers
        )
        assert counter_headers
        assert response.status_code == 200
        assert response.json()["scope"] == "all_devices"

        for headers in (phone_headers, counter_headers):
            assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 401
        for tokens in (phone, counter):
            assert refresh(api_client, str(tokens["refresh_token"]), expect=401).status_code == 401

        live = fetch_scalar(
            main_database,
            "SELECT count(*) FROM refresh_tokens WHERE user_id = :id AND revoked_at IS NULL",
            id=user["id"],
        )
        assert live == 0

    def test_logout_requires_a_token(self, api_client: TestClient) -> None:
        assert api_client.post(f"{API}/auth/logout", json={}).status_code == 401

    def test_logout_is_idempotent_for_the_server(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        _, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        first = api_client.post(f"{API}/auth/logout", json={}, headers=headers)
        second = api_client.post(f"{API}/auth/logout", json={}, headers=headers)
        assert first.status_code == 200
        assert second.status_code == 401  # the session no longer exists


class TestSessionManagement:
    def test_sessions_are_listed_for_the_account(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        phone, phone_headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        counter, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]

        response = api_client.get(f"{API}/auth/sessions", headers=phone_headers)
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert len(items) == 2
        current = [item for item in items if item["is_current"]]
        assert len(current) == 1
        assert current[0]["session_id"] == phone["session_id"]
        session_ids = {item["session_id"] for item in items}
        assert session_ids == {phone["session_id"], counter["session_id"]}

    def test_the_session_list_never_exposes_a_token(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        tokens, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        text = api_client.get(f"{API}/auth/sessions", headers=headers).text
        assert str(tokens["refresh_token"]) not in text
        assert str(tokens["access_token"]) not in text
        assert "token_hash" not in text

    def test_another_account_session_is_not_visible(
        self, api_client: TestClient, make_user: object
    ) -> None:
        first = make_user()  # type: ignore[operator]
        second = make_user()  # type: ignore[operator]
        first_tokens, first_headers, _ = fresh_session(api_client, first)  # type: ignore[arg-type]
        second_tokens, _, _ = fresh_session(api_client, second)  # type: ignore[arg-type]

        items = api_client.get(f"{API}/auth/sessions", headers=first_headers).json()["items"]
        assert {item["session_id"] for item in items} == {first_tokens["session_id"]}
        assert second_tokens["session_id"] not in {item["session_id"] for item in items}

    def test_a_session_can_be_revoked_from_another_device(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        phone, phone_headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        _, counter_headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]

        response = api_client.post(
            f"{API}/auth/sessions/{phone['session_id']}/revoke", headers=counter_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["scope"] == "session"

        assert api_client.get(f"{API}/auth/me", headers=phone_headers).status_code == 401
        assert counter_headers
        assert api_client.get(f"{API}/auth/me", headers=counter_headers).status_code == 200

    def test_revoking_another_account_session_is_not_possible(
        self, api_client: TestClient, make_user: object
    ) -> None:
        victim = make_user()  # type: ignore[operator]
        attacker = make_user()  # type: ignore[operator]
        victim_tokens, victim_headers, _ = fresh_session(api_client, victim)  # type: ignore[arg-type]
        _, attacker_headers, _ = fresh_session(api_client, attacker)  # type: ignore[arg-type]

        response = api_client.post(
            f"{API}/auth/sessions/{victim_tokens['session_id']}/revoke", headers=attacker_headers
        )
        assert response.status_code == 404  # not 403: existence is not disclosed
        assert error_code(response) == "RESOURCE_NOT_FOUND"
        assert api_client.get(f"{API}/auth/me", headers=victim_headers).status_code == 200

    def test_session_revocation_is_audited(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        user = make_user()  # type: ignore[operator]
        phone, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        _, counter_headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        api_client.post(
            f"{API}/auth/sessions/{phone['session_id']}/revoke", headers=counter_headers
        )
        rows = fetch_all(
            main_database,
            "SELECT new_data FROM audit_logs WHERE action = 'SECURITY_SESSION_REVOKED' "
            "ORDER BY created_at DESC LIMIT 1",
        )
        assert rows, "the revocation was not audited"
        payload = rows[0][0]
        payload = json.loads(payload) if isinstance(payload, str) else payload
        assert payload["session_id"] == phone["session_id"]
        assert payload["was_current"] is False  # revoked from the other device

    def test_an_unknown_session_id_is_a_404(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        _, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        response = api_client.post(f"{API}/auth/sessions/{uuid.uuid4()}/revoke", headers=headers)
        assert response.status_code == 404

    def test_a_revoked_session_disappears_from_the_list(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        phone, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        _, counter_headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        api_client.post(
            f"{API}/auth/sessions/{phone['session_id']}/revoke", headers=counter_headers
        )
        items = api_client.get(f"{API}/auth/sessions", headers=counter_headers).json()["items"]
        assert phone["session_id"] not in {item["session_id"] for item in items}


class TestSessionEdgeCases:
    def test_many_sessions_on_one_device_all_work_independently(
        self, api_client: TestClient, make_user: object
    ) -> None:
        """A cashier reopening the app repeatedly must not invalidate earlier sessions."""
        user = make_user()  # type: ignore[operator]
        device_uuid = uuid.uuid4()
        sessions = [
            login(api_client, str(user["username"]), device_uuid=device_uuid).json()
            for _ in range(5)
        ]
        assert len({session["session_id"] for session in sessions}) == 5
        rotated = [
            refresh(api_client, str(session["refresh_token"])).json() for session in sessions
        ]
        assert all(item["device"]["id"] == sessions[0]["device"]["id"] for item in rotated)

    def test_a_login_lockout_does_not_revoke_existing_sessions(
        self, api_client: TestClient, make_user: object, clear_rate_limits: None
    ) -> None:
        """Brute-forcing the password must not log the real operator out (or in)."""
        user = make_user()  # type: ignore[operator]
        _, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        for _ in range(get_settings().login_max_failed_attempts):
            login(api_client, str(user["username"]), "Wrong-Passw0rd-2026!", expect=401)

        assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 200

    def test_the_denylist_survives_a_second_request_for_the_same_jti(
        self, api_client: TestClient, make_user: object
    ) -> None:
        user = make_user()  # type: ignore[operator]
        _, headers, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        api_client.post(f"{API}/auth/logout", json={}, headers=headers)
        for _ in range(3):
            assert api_client.get(f"{API}/auth/me", headers=headers).status_code == 401

    def test_a_session_survives_short_access_token_lifetimes(
        self, api_client: TestClient, make_user: object, main_database: str
    ) -> None:
        """Rotation is bounded by the refresh lifetime, not by the access lifetime."""
        user = make_user()  # type: ignore[operator]
        tokens, _, _ = fresh_session(api_client, user)  # type: ignore[arg-type]
        expires_at = fetch_scalar(
            main_database,
            "SELECT expires_at FROM refresh_tokens WHERE family_id = :family",
            family=tokens["session_id"],
        )
        assert isinstance(expires_at, dt.datetime)
        expected = dt.datetime.now(tz=dt.UTC) + dt.timedelta(
            days=get_settings().refresh_token_expire_days
        )
        assert abs((expires_at - expected).total_seconds()) < 120

    def test_two_users_on_one_installation_do_not_share_sessions(
        self, api_client: TestClient, make_user: object
    ) -> None:
        device_uuid = uuid.uuid4()
        first = make_user()  # type: ignore[operator]
        second = make_user()  # type: ignore[operator]
        first_tokens, first_headers, _ = fresh_session(  # type: ignore[arg-type]
            api_client, first, device_uuid=device_uuid
        )
        second_tokens, second_headers, _ = fresh_session(  # type: ignore[arg-type]
            api_client, second, device_uuid=device_uuid
        )
        assert first_tokens["session_id"] != second_tokens["session_id"]

        api_client.post(f"{API}/auth/logout", json={}, headers=first_headers)
        assert api_client.get(f"{API}/auth/me", headers=second_headers).status_code == 200
        assert api_client.get(f"{API}/auth/me", headers=first_headers).status_code == 401

    def test_a_unique_username_is_not_enough_to_log_in(self, api_client: TestClient) -> None:
        """No session, no tokens: only the password check opens a session."""
        with_unknown_name = login(api_client, unique("probe"), USER_PASSWORD, expect=401)
        assert error_code(with_unknown_name) == "INVALID_CREDENTIALS"
