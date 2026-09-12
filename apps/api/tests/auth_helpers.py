"""Shared helpers for the Phase 2 authentication/RBAC tests.

Everything here goes through the real HTTP surface (``TestClient`` + the application's
own lifespan), so a passing test means the deployed process would behave that way.
"""

from __future__ import annotations

import datetime as dt
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from fastapi.testclient import TestClient

API = "/api/v1"
USERS = f"{API}/users"
DEVICES = f"{API}/devices"
DEVICE_ID_HEADER = "X-Device-Id"

# Passwords used by the fixtures. They satisfy the policy (>= 12 chars, 3 character
# classes) but are obviously test-only values.
USER_PASSWORD = "Cashier-Passw0rd-2026!"
SECOND_PASSWORD = "Second-Passw0rd-2026!"

# The seeded development administrator (SUPER_ADMIN). Its password is the test-only
# constant in tests/helpers.py; seed 004 refuses to run outside the development guard.
ADMIN_USERNAME = "admin"


def unique(prefix: str) -> str:
    """A collision-free username fragment; usernames must match ^[A-Za-z0-9._-]{3,100}$."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def login(
    client: TestClient,
    username: str,
    password: str = USER_PASSWORD,
    *,
    device_uuid: uuid.UUID | None = None,
    device_name: str = "Counter-1",
    platform: str = "WINDOWS",
    app_version: str = "1.0.0",
    branch_id: uuid.UUID | None = None,
    expect: int | None = 200,
) -> Any:
    """POST /auth/login and (by default) assert it succeeded."""
    body: dict[str, Any] = {
        "username": username,
        "password": password,
        "device_uuid": str(device_uuid or uuid.uuid4()),
        "device_name": device_name,
        "platform": platform,
        "app_version": app_version,
    }
    if branch_id is not None:
        body["branch_id"] = str(branch_id)
    response = client.post(f"{API}/auth/login", json=body)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def bearer(access_token: str, device_id: uuid.UUID | str | None = None) -> dict[str, str]:
    """Authorization headers for an access token, optionally claiming a device."""
    headers = {"Authorization": f"Bearer {access_token}"}
    if device_id is not None:
        headers[DEVICE_ID_HEADER] = str(device_id)
    return headers


def login_headers(
    client: TestClient, username: str, password: str = USER_PASSWORD, **kwargs: Any
) -> tuple[dict[str, str], dict[str, Any]]:
    """Log in and return ``(headers, response_body)``."""
    response = login(client, username, password, **kwargs)
    body = response.json()
    return bearer(body["access_token"], body["device"]["id"]), body


def refresh(
    client: TestClient,
    refresh_token: str,
    *,
    device_uuid: uuid.UUID | str | None = None,
    expect: int | None = 200,
) -> Any:
    """POST /auth/refresh with an optional device claim."""
    body: dict[str, Any] = {"refresh_token": refresh_token}
    if device_uuid is not None:
        body["device_uuid"] = str(device_uuid)
    response = client.post(f"{API}/auth/refresh", json=body)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def register_device(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    branch_id: str,
    device_uuid: uuid.UUID | None = None,
    device_name: str = "Provisioned",
    platform: str = "WINDOWS",
    expect: int | None = 201,
) -> dict[str, Any]:
    """``POST /devices/register`` — the administrator path to provision a counter."""
    response = client.post(
        f"{API}/devices/register",
        headers=dict(headers),
        json={
            "device_uuid": str(device_uuid or uuid.uuid4()),
            "device_name": device_name,
            "platform": platform,
            "branch_id": str(branch_id),
        },
    )
    if expect is not None:
        assert response.status_code == expect, response.text
    return response.json()


def error_code(response: Any) -> str:
    """The ``error.code`` of an error envelope (fails loudly if the body is not one)."""
    payload = response.json()
    assert "error" in payload, payload
    return str(payload["error"]["code"])


def error_details(response: Any) -> Mapping[str, Any]:
    return response.json()["error"].get("details", {})


def forge_access_token(
    *,
    user_id: str,
    session_id: str,
    device_id: str | None = None,
    roles: Sequence[str] = (),
    permissions: Sequence[str] = (),
    expires_in_seconds: int = 900,
    issued_seconds_ago: int = 0,
    secret: str | None = None,
    algorithm: str = "HS256",
    issuer: str = "nexus-exchange",
    audience: str = "nexus-api",
    token_type: str = "access",  # noqa: S107 - claim value, not a credential
) -> str:
    """Mint a token directly, so expiry and claim tampering can be tested precisely.

    The API's own issuer is used to *create* tokens, so a token built here is only
    different in the dimension under test — an expired one really is expired, and a
    tampered one really is signed with the wrong key.
    """
    import jwt

    from app.core.config import get_settings
    from app.core.permissions import permission_hash

    now = int(time.time())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": user_id,
        "jti": str(uuid.uuid4()),
        "sid": session_id,
        "did": device_id,
        "bid": None,
        "roles": list(roles),
        "perm_hash": permission_hash(permissions),
        "typ": token_type,
        "iat": now - issued_seconds_ago,
        "exp": now - issued_seconds_ago + expires_in_seconds,
    }
    key = secret if secret is not None else get_settings().jwt_secret
    return jwt.encode(payload, key, algorithm=algorithm)


def iso(value: dt.datetime) -> str:
    return value.isoformat()
