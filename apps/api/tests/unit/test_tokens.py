"""Access-token and refresh-token primitives (PART 24, PART 42, API_CONTRACT §2).

These tests use the real :class:`~app.core.tokens.TokenService`, so a change to the
claim layout, the signature check or the refresh-token hash is caught here rather than in
production. No database and no network: only Cryptography and the settings model.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import uuid

import jwt
import pytest

from app.core.config import Settings
from app.core.exceptions import TokenExpiredError, TokenInvalidError
from app.core.permissions import Permission, permission_hash
from app.core.tokens import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_PREFIX,
    TOKEN_AUDIENCE,
    TOKEN_ISSUER,
    TokenService,
)
from tests.helpers import database_dsn

pytestmark = pytest.mark.unit


def build_settings(**overrides: object) -> Settings:
    """Settings good enough to sign tokens, with no dependence on a local .env file."""
    base: dict[str, object] = {
        "app_env": "test",
        "database_url": database_dsn("nexus_token_probe", driver="asyncpg"),
        "database_migration_url": database_dsn("nexus_token_probe", driver="asyncpg"),
        "jwt_secret": "a" * 48,
        "jwt_refresh_secret": "b" * 48,
        "redis_url": "redis://127.0.0.1:6379/15",
        "storage_path": "/tmp/nexus-token-test",
        "access_token_expire_minutes": 15,
        "refresh_token_expire_days": 30,
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


@pytest.fixture
def service() -> TokenService:
    return TokenService(build_settings())


PERMISSIONS = {str(Permission.EXCHANGE_CREATE), str(Permission.CUSTOMER_VIEW)}


class TestAccessTokenIssuing:
    def test_an_issued_token_verifies_and_round_trips(self, service: TokenService) -> None:
        user_id, session_id, device_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        issued = service.issue_access_token(
            user_id=user_id,
            session_id=session_id,
            device_id=device_id,
            branch_id=None,
            roles=("CASHIER",),
            permissions=PERMISSIONS,
        )
        claims = service.decode_access_token(issued.token)
        assert claims.sub == user_id
        assert claims.sid == session_id
        assert claims.did == device_id
        assert claims.roles == ("CASHIER",)
        assert claims.typ == ACCESS_TOKEN_TYPE
        assert claims.iss == TOKEN_ISSUER
        assert claims.aud == TOKEN_AUDIENCE
        assert claims.perm_hash == permission_hash(PERMISSIONS)

    def test_every_token_gets_a_unique_identifier(self, service: TokenService) -> None:
        kwargs = {
            "user_id": uuid.uuid4(),
            "session_id": uuid.uuid4(),
            "device_id": uuid.uuid4(),
            "branch_id": None,
            "roles": ("CASHIER",),
            "permissions": PERMISSIONS,
        }
        first = service.issue_access_token(**kwargs)  # type: ignore[arg-type]
        second = service.issue_access_token(**kwargs)  # type: ignore[arg-type]
        assert first.jti != second.jti
        assert first.token != second.token

    def test_the_lifetime_follows_the_setting(self) -> None:
        service = TokenService(build_settings(access_token_expire_minutes=7))
        issued = service.issue_access_token(
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            device_id=None,
            branch_id=None,
            roles=(),
            permissions=frozenset(),
        )
        assert issued.expires_in_seconds == 7 * 60
        assert issued.expires_at - issued.issued_at == dt.timedelta(minutes=7)

    def test_the_token_carries_a_key_identifier_for_rotation(self, service: TokenService) -> None:
        issued = service.issue_access_token(
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            device_id=None,
            branch_id=None,
            roles=(),
            permissions=frozenset(),
        )
        header = jwt.get_unverified_header(issued.token)
        assert str(header["kid"]).startswith("hs-hs256-")
        assert len(str(header["kid"])) < 32

    def test_no_secret_is_embedded_in_the_token(self, service: TokenService) -> None:
        issued = service.issue_access_token(
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            device_id=None,
            branch_id=None,
            roles=(),
            permissions=frozenset(),
        )
        assert "b" * 48 not in issued.token
        # ``_signing_key`` is the derived HMAC key the issuer signs with.
        assert service._signing_key not in issued.token


class TestAccessTokenVerification:
    def test_an_expired_token_is_reported_as_expired(self, service: TokenService) -> None:
        # Issued 2 hours ago with a 15-minute lifetime.
        issued = service.issue_access_token(
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            device_id=None,
            branch_id=None,
            roles=(),
            permissions=frozenset(),
            issued_at=dt.datetime.now(tz=dt.UTC) - dt.timedelta(hours=2),
        )
        with pytest.raises(TokenExpiredError) as error:
            service.decode_access_token(issued.token)
        assert error.value.code == "TOKEN_EXPIRED"

    def test_a_token_signed_with_another_key_is_rejected(self, service: TokenService) -> None:
        other = TokenService(build_settings(jwt_secret="c" * 48))
        issued = other.issue_access_token(
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            device_id=None,
            branch_id=None,
            roles=(),
            permissions=frozenset(),
        )
        with pytest.raises(TokenInvalidError):
            service.decode_access_token(issued.token)

    def test_an_unsigned_token_is_rejected(self, service: TokenService) -> None:
        unsigned = jwt.encode(
            {
                "iss": TOKEN_ISSUER,
                "aud": TOKEN_AUDIENCE,
                "sub": str(uuid.uuid4()),
                "jti": "x",
                "sid": str(uuid.uuid4()),
                "typ": ACCESS_TOKEN_TYPE,
                "iat": int(dt.datetime.now(tz=dt.UTC).timestamp()),
                "exp": int(dt.datetime.now(tz=dt.UTC).timestamp()) + 600,
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(TokenInvalidError):
            service.decode_access_token(unsigned)

    def test_a_token_for_another_audience_is_rejected(self, service: TokenService) -> None:
        forged = jwt.encode(
            {
                "iss": TOKEN_ISSUER,
                "aud": "some-other-service",
                "sub": str(uuid.uuid4()),
                "jti": "x",
                "sid": str(uuid.uuid4()),
                "typ": ACCESS_TOKEN_TYPE,
                "iat": int(dt.datetime.now(tz=dt.UTC).timestamp()),
                "exp": int(dt.datetime.now(tz=dt.UTC).timestamp()) + 600,
            },
            key="a" * 48,
            algorithm="HS256",
        )
        with pytest.raises(TokenInvalidError):
            service.decode_access_token(forged)

    def test_a_non_access_token_type_is_rejected(self, service: TokenService) -> None:
        forged = jwt.encode(
            {
                "iss": TOKEN_ISSUER,
                "aud": TOKEN_AUDIENCE,
                "sub": str(uuid.uuid4()),
                "jti": "x",
                "sid": str(uuid.uuid4()),
                "typ": "refresh",
                "iat": int(dt.datetime.now(tz=dt.UTC).timestamp()),
                "exp": int(dt.datetime.now(tz=dt.UTC).timestamp()) + 600,
            },
            key="a" * 48,
            algorithm="HS256",
        )
        with pytest.raises(TokenInvalidError):
            service.decode_access_token(forged)

    @pytest.mark.parametrize("missing", ["exp", "iat", "sub", "jti", "sid", "aud", "iss"])
    def test_a_token_missing_a_required_claim_is_rejected(
        self, service: TokenService, missing: str
    ) -> None:
        now = int(dt.datetime.now(tz=dt.UTC).timestamp())
        payload: dict[str, object] = {
            "iss": TOKEN_ISSUER,
            "aud": TOKEN_AUDIENCE,
            "sub": str(uuid.uuid4()),
            "jti": "x",
            "sid": str(uuid.uuid4()),
            "typ": ACCESS_TOKEN_TYPE,
            "iat": now,
            "exp": now + 600,
        }
        payload.pop(missing)
        forged = jwt.encode(payload, key="a" * 48, algorithm="HS256")
        with pytest.raises(TokenInvalidError):
            service.decode_access_token(forged)

    def test_garbage_is_rejected_without_leaking_library_detail(
        self, service: TokenService
    ) -> None:
        with pytest.raises(TokenInvalidError) as error:
            service.decode_access_token("not.a.jwt")
        assert "not.a.jwt" not in str(error.value)
        assert "Traceback" not in str(error.value)

    def test_an_rs_token_is_rejected_when_hs_is_configured(self, service: TokenService) -> None:
        """Algorithm confusion: a token whose header claims RS256 must not be accepted."""
        header = {"alg": "RS256", "typ": "JWT"}
        payload = {
            "iss": TOKEN_ISSUER,
            "aud": TOKEN_AUDIENCE,
            "sub": str(uuid.uuid4()),
            "jti": "x",
            "sid": str(uuid.uuid4()),
            "typ": ACCESS_TOKEN_TYPE,
            "iat": int(dt.datetime.now(tz=dt.UTC).timestamp()),
            "exp": int(dt.datetime.now(tz=dt.UTC).timestamp()) + 600,
        }
        encode = lambda value: (  # noqa: E731 - local helper keeps the forgery readable
            base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )
        signing_input = f"{encode(header)}.{encode(payload)}".encode()
        signature = base64.urlsafe_b64encode(
            hmac.new(service._signing_key.encode(), signing_input, hashlib.sha256).digest()
        ).rstrip(b"=")
        forged = f"{signing_input.decode()}.{signature.decode()}"
        with pytest.raises(TokenInvalidError):
            service.decode_access_token(forged)


class TestRefreshTokens:
    def test_a_refresh_token_is_opaque_and_prefixed(self, service: TokenService) -> None:
        minted = service.mint_refresh_token()
        assert minted.raw.startswith(REFRESH_TOKEN_PREFIX)
        # 32 random bytes base64url-encoded are 43 characters; anything shorter would be
        # less entropy than SECURITY.md §2 promises.
        assert len(minted.raw) - len(REFRESH_TOKEN_PREFIX) >= 43

    def test_the_stored_hash_is_not_the_token(self, service: TokenService) -> None:
        minted = service.mint_refresh_token()
        assert minted.token_hash != minted.raw
        assert minted.raw not in minted.token_hash
        assert len(minted.token_hash) == 64  # CHAR(64) in the approved schema
        assert all(character in "0123456789abcdef" for character in minted.token_hash)

    def test_hashing_is_deterministic_and_keyed(self, service: TokenService) -> None:
        minted = service.mint_refresh_token()
        assert service.hash_refresh_token(minted.raw) == minted.token_hash
        other = TokenService(build_settings(jwt_refresh_secret="d" * 48))
        assert other.hash_refresh_token(minted.raw) != minted.token_hash

    def test_tokens_are_unique(self, service: TokenService) -> None:
        raw_values = {service.mint_refresh_token().raw for _ in range(50)}
        assert len(raw_values) == 50

    @pytest.mark.parametrize("bad", ["", "short", "rt_", "x" * 60, "rt " + "a" * 40])
    def test_malformed_refresh_tokens_are_rejected_before_the_database(
        self, service: TokenService, bad: str
    ) -> None:
        with pytest.raises(TokenInvalidError):
            service.verify_refresh_token(bad)

    def test_a_well_formed_token_passes_the_shape_check(self, service: TokenService) -> None:
        service.verify_refresh_token(service.mint_refresh_token().raw)

    def test_the_refresh_lifetime_follows_the_setting(self) -> None:
        service = TokenService(build_settings(refresh_token_expire_days=3))
        assert service.refresh_token_ttl_seconds == 3 * 24 * 3600
