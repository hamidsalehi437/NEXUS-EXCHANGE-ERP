"""Settings: parsing, validation, derived properties and secret hygiene (PART 5, PART 42, PART 43).

Every case disables the developer's ``.env`` (``_env_file=None``) and passes the values
it depends on explicitly, so the outcome never depends on a checkout's local file.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings, load_settings, to_psycopg_dsn
from tests.helpers import database_dsn

# A valid baseline. Secrets are long, distinct and clearly not placeholders.
_BASE: dict[str, object] = {
    "app_env": "development",
    "database_url": database_dsn("nexus_config_probe", driver="asyncpg"),
    "database_migration_url": database_dsn("nexus_config_probe", driver="asyncpg"),
    "jwt_secret": "1" * 40,
    "jwt_refresh_secret": "2" * 40,
    "redis_url": "redis://:probe-password@127.0.0.1:6379/0",
    "cors_origins": "http://localhost:3000",
    "trusted_hosts": "localhost,127.0.0.1",
    "storage_path": "/tmp/nexus-config-test",
    # Explicit: the environment might carry a development password, and a production
    # configuration must be refused because of it.
    "dev_admin_password": None,
}


def build_settings(**overrides: object) -> Settings:
    """Build Settings from ``_BASE`` plus overrides, ignoring any local ``.env``."""
    return Settings(_env_file=None, **{**_BASE, **overrides})  # type: ignore[arg-type]


def production_settings(**overrides: object) -> Settings:
    """A configuration that satisfies every production-only rule."""
    return build_settings(
        **{
            "app_env": "production",
            "force_https": True,
            "backup_encryption_recipient": "age1probe" + "0" * 51,
            **overrides,
        }
    )


class TestValidConfiguration:
    def test_a_valid_development_configuration_loads(self) -> None:
        settings = build_settings()
        assert settings.app_env == "development"
        assert settings.is_production is False
        assert settings.tz == "UTC"
        assert settings.base_currency_code == "AFN"

    def test_a_valid_production_configuration_loads(self) -> None:
        settings = production_settings()
        assert settings.is_production is True
        assert settings.is_test is False
        assert settings.dev_admin_password is None

    def test_the_test_environment_is_detected(self) -> None:
        settings = build_settings(app_env="test")
        assert settings.is_test is True
        assert settings.is_production is False

    def test_defaults_are_safe(self) -> None:
        settings = build_settings()
        assert settings.rate_limit_enabled is True
        assert settings.secure_headers_enabled is True
        assert settings.argon2_memory_cost >= 19_456  # OWASP minimum for Argon2id
        assert settings.access_token_expire_minutes <= 60
        assert settings.idempotency_retention_days >= 1
        assert settings.backup_verify_after_create is True

    def test_lists_are_parsed_and_trimmed(self) -> None:
        settings = build_settings(
            cors_origins=" http://a.example , http://b.example ,",
            trusted_hosts=" a.example ,, b.example ",
        )
        assert settings.cors_origins_list == ["http://a.example", "http://b.example"]
        assert settings.trusted_hosts_list == ["a.example", "b.example"]

    def test_the_proxy_trust_list_defaults_to_loopback(self) -> None:
        """A wildcard would let any client forge the client IP recorded in the audit log."""
        assert build_settings().forwarded_allow_ips == "127.0.0.1"

    def test_celery_urls_default_to_separate_redis_databases(self) -> None:
        settings = build_settings()
        assert settings.broker_url.endswith("/1")
        assert settings.result_backend_url.endswith("/2")
        # The runtime cache stays on database 0, so a flushed broker cannot lose
        # idempotency state.
        assert settings.redis_url.endswith("/0")

    def test_explicit_celery_urls_win(self) -> None:
        settings = build_settings(
            celery_broker_url="redis://:brokerpw@broker.internal:6379/4",
            celery_result_backend="redis://:resultpw@results.internal:6379/5",
        )
        assert settings.broker_url.endswith("/4")
        assert settings.result_backend_url.endswith("/5")


class TestSecretValidation:
    @pytest.mark.parametrize(
        "placeholder", ["", "change-me", "changeme", "secret", "password", "xxx"]
    )
    def test_placeholder_secrets_are_rejected(self, placeholder: str) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(jwt_secret=placeholder)
        assert "strong random value" in str(error.value)

    def test_a_short_secret_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(jwt_secret="short-but-not-a-placeholder")
        assert "at least 32 characters long" in str(error.value)

    def test_access_and_refresh_secrets_must_differ(self) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(jwt_refresh_secret="1" * 40)
        assert "must be different values" in str(error.value)

    def test_a_hex_secret_of_the_documented_length_is_accepted(self) -> None:
        settings = build_settings(jwt_secret="a1" * 32)
        assert len(settings.jwt_secret) == 64

    def test_rsa_algorithm_requires_a_private_key(self) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(jwt_algorithm="RS256")
        assert "JWT_PRIVATE_KEY_PATH is required" in str(error.value)

    def test_rsa_algorithm_with_a_key_is_accepted(self) -> None:
        settings = build_settings(
            jwt_algorithm="RS256", jwt_private_key_path="/run/secrets/jwt_private.pem"
        )
        assert settings.jwt_algorithm == "RS256"


class TestDatabaseUrlValidation:
    def test_the_async_driver_is_required(self) -> None:
        """Both DSNs are async: the synchronous psycopg DSN is derived, never configured."""
        with pytest.raises(ValidationError) as error:
            build_settings(database_url="postgresql://user:pw@127.0.0.1:5432/nexus_exchange")
        assert "must use the async driver" in str(error.value)

    def test_the_migration_dsn_must_use_the_same_driver(self) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(
                database_migration_url=database_dsn("nexus_config_probe", driver="psycopg")
            )
        assert "must use the async driver" in str(error.value)

    @pytest.mark.parametrize(
        "dsn", ["mysql://user@host/db", "sqlite:///tmp/db.sqlite", "postgresql+psycopg2://u@h/db"]
    )
    def test_non_postgresql_drivers_are_rejected(self, dsn: str) -> None:
        with pytest.raises(ValidationError):
            build_settings(database_url=dsn)

    def test_the_migration_dsn_is_converted_to_psycopg_for_alembic(self) -> None:
        settings = build_settings()
        sync_dsn = settings.migration_dsn_psycopg
        assert sync_dsn.startswith("postgresql+psycopg://")
        assert "asyncpg" not in sync_dsn
        # Credentials and target are preserved; only the driver changes.
        assert settings.database_migration_url.split("://", 1)[1] in sync_dsn

    def test_psycopg_conversion_is_idempotent(self) -> None:
        async_dsn = "postgresql+asyncpg://user:pw@host:5432/db"
        once = to_psycopg_dsn(async_dsn)
        assert once == "postgresql+psycopg://user:pw@host:5432/db"
        assert to_psycopg_dsn(once) == once

    def test_psycopg_conversion_leaves_unknown_schemes_alone(self) -> None:
        """A DSN it does not understand is returned unchanged rather than mangled."""
        assert to_psycopg_dsn("sqlite:///tmp/db.sqlite") == "sqlite:///tmp/db.sqlite"

    def test_redis_scheme_is_validated(self) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(redis_url="http://127.0.0.1:6379/0")
        assert "REDIS_URL must start with" in str(error.value)

    @pytest.mark.parametrize(
        "url",
        ["redis://127.0.0.1:6379/0", "rediss://redis.example:6380/0", "unix:///tmp/redis.sock"],
    )
    def test_supported_redis_schemes_are_accepted(self, url: str) -> None:
        assert build_settings(redis_url=url).redis_url == url


class TestCrossFieldRules:
    def test_relative_storage_path_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(storage_path="storage")
        assert "absolute path" in str(error.value)

    def test_unknown_timezone_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(tz="Mars/Olympus_Mons")
        assert "unknown timezone" in str(error.value)

    def test_worker_soft_limit_must_be_lower_than_the_hard_limit(self) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(
                worker_task_soft_time_limit_seconds=300, worker_task_time_limit_seconds=300
            )
        assert "WORKER_TASK_SOFT_TIME_LIMIT_SECONDS" in str(error.value)

    @pytest.mark.parametrize("prefix", ["nex", "TOOLONGPREFIX", "N1"])
    def test_document_prefixes_are_validated(self, prefix: str) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(numbering_prefix_exchange=prefix)
        assert "uppercase letters" in str(error.value)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"api_port": 0},
            {"api_port": 70_000},
            {"db_pool_size": 0},
            {"db_pool_size": 0, "db_max_overflow": 0},
            {"argon2_time_cost": 0},
            {"numbering_width": 2},
            {"rate_tolerance_bps": 9_999},
            {"login_max_failed_attempts": 0},
        ],
    )
    def test_environment_ranges_are_enforced(self, overrides: dict[str, object]) -> None:
        with pytest.raises(ValidationError):
            build_settings(**overrides)

    def test_pool_must_allow_a_connection(self) -> None:
        """A pool of zero would make the API start and then fail every request."""
        with pytest.raises(ValidationError):
            build_settings(db_pool_size=0, db_max_overflow=0)


class TestProductionRules:
    def test_http_only_production_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as error:
            production_settings(force_https=False)
        assert "FORCE_HTTPS must be true" in str(error.value)

    def test_development_admin_password_is_rejected_in_production(self) -> None:
        """A credential that exists in a repository or a runbook must never work in production."""
        with pytest.raises(ValidationError) as error:
            production_settings(dev_admin_password="Some-Dev-Password-2026!")
        assert "DEV_ADMIN_PASSWORD must be empty" in str(error.value)

    def test_unencrypted_backups_are_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError) as error:
            production_settings(backup_enabled=True, backup_encryption_recipient=None)
        assert "BACKUP_ENCRYPTION_RECIPIENT is required" in str(error.value)

    def test_disabled_security_headers_are_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError) as error:
            production_settings(secure_headers_enabled=False)
        assert "SECURE_HEADERS_ENABLED must be true" in str(error.value)

    def test_wildcard_cors_is_rejected_in_production(self) -> None:
        with pytest.raises(ValidationError) as error:
            production_settings(cors_origins="*")
        assert "must not contain '*'" in str(error.value)

    def test_wildcard_cannot_be_mixed_with_explicit_origins(self) -> None:
        with pytest.raises(ValidationError) as error:
            build_settings(cors_origins="*,http://localhost:3000")
        assert "must not mix" in str(error.value)

    def test_wildcard_cors_alone_is_allowed_in_development(self) -> None:
        """Guarded by TRUSTED_HOSTS and credentials, a wildcard is a development choice."""
        assert build_settings(cors_origins="*").cors_origins_list == ["*"]

    def test_production_may_disable_backups_without_a_recipient(self) -> None:
        settings = production_settings(backup_enabled=False, backup_encryption_recipient=None)
        assert settings.backup_enabled is False


class TestSecretHygiene:
    def test_printing_the_settings_object_masks_credentials(self) -> None:
        settings = build_settings()
        rendered = f"{settings!r} {settings!s}"
        assert "[redacted]" in rendered
        assert settings.jwt_secret not in rendered
        assert settings.jwt_refresh_secret not in rendered
        assert "probe-password" not in rendered

    def test_serialization_is_explicit_and_unmasked(self) -> None:
        """``model_dump()`` is a deliberate request for values; callers own the output."""
        settings = build_settings()
        assert settings.model_dump()["jwt_secret"] == "1" * 40

    def test_safe_summary_contains_no_credentials(self) -> None:
        settings = build_settings(jwt_secret="uniquesecretvalue" + "0" * 20)
        summary = settings.safe_summary
        assert summary["database_host"] == "127.0.0.1:5432/nexus_config_probe"
        assert summary["redis_host"] == "127.0.0.1:6379/0"
        assert "jwt_secret" not in summary
        assert "uniquesecretvalue" not in str(summary)
        assert "probe-password" not in str(summary)


class TestPrecedence:
    def test_explicit_values_win_over_the_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
        monkeypatch.setenv("JWT_SECRET", "b" * 48)
        settings = build_settings(rate_limit_enabled=True)
        assert settings.rate_limit_enabled is True
        assert settings.jwt_secret == "1" * 40


class TestLoadSettings:
    def test_load_settings_reports_an_invalid_environment_readably(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bad deployment fails at startup with a readable message, not a traceback."""
        monkeypatch.setenv("DATABASE_URL", "mysql://user@host/db")
        monkeypatch.setenv("JWT_SECRET", "weak")
        with pytest.raises(SystemExit) as exit_info:
            load_settings()
        message = str(exit_info.value)
        assert "Invalid NEXUS configuration" in message
        assert "database_url" in message
        assert "jwt_secret" in message

    def test_load_settings_returns_a_settings_instance(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key, value in _BASE.items():
            if value is not None:
                monkeypatch.setenv(key.upper(), str(value))
        assert isinstance(load_settings(), Settings)
