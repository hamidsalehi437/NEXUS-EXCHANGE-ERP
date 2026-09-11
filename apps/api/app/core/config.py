"""Application configuration — Pydantic Settings (PART 43).

Rules enforced here (SECURITY.md §5, §8):

* Secrets have **no defaults**. A missing or weak secret stops the process at
  startup instead of degrading silently in production.
* Environment-specific safety rules are validation errors, not warnings:
  ``production`` refuses wildcard CORS, refuses HTTP-only transport and refuses a
  development admin password.
* Nothing in this module reads or logs a secret value.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.logging import REDACTION_PLACEHOLDER

AppEnv = Literal["development", "test", "production"]

# Fields that carry a credential. They are masked by ``repr()``/``str()`` so a
# settings object can never leak a secret into a traceback, a crash report or a
# log line that prints the object (PART 42; the same placeholder the structured
# logging redactor uses).
# ``model_dump()`` is deliberately NOT masked: it is an explicit request for the
# values, and callers that serialize it own the secret-handling decision.
_CREDENTIAL_FIELDS = frozenset(
    {
        "database_url",
        "database_migration_url",
        "jwt_secret",
        "jwt_refresh_secret",
        "redis_url",
        "celery_broker_url",
        "celery_result_backend",
        "sentry_dsn",
    }
)

# Values that have appeared in `.env.example` or in copy-pasted deployment notes.
# Accepting them would be worse than refusing to start.
_FORBIDDEN_SECRET_VALUES = frozenset(
    {
        "",
        "change-me",
        "changeme",
        "secret",
        "password",
        "jwt-secret",
        "test",
        "xxx",
    }
)

_MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    """Typed, validated application settings."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Runtime -------------------------------------------------------------
    app_env: AppEnv = "development"
    app_name: str = "NEXUS EXCHANGE ERP"
    app_version: str = "0.1.0"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    tz: str = "UTC"
    api_port: int = Field(default=8000, ge=1, le=65535)
    # Which peers may set X-Forwarded-* (uvicorn reads the same variable). The default
    # trusts loopback only: a wildcard would let any client forge the address that ends
    # up in audit records and in per-IP rate limiting. Compose sets the exact CIDR.
    forwarded_allow_ips: str = "127.0.0.1"

    # --- Build metadata (exposed by GET /version) ----------------------------
    git_sha: str = "unknown"
    build_time: str = "unknown"

    # --- Database ------------------------------------------------------------
    database_url: str
    database_migration_url: str
    db_pool_size: int = Field(default=10, ge=1, le=200)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_statement_timeout_ms: int = Field(default=15_000, ge=0)
    db_idle_in_transaction_timeout_ms: int = Field(default=30_000, ge=0)
    db_connect_retries: int = Field(default=10, ge=0, le=120)
    db_connect_retry_delay_seconds: float = Field(default=1.5, ge=0.1, le=30.0)

    # --- Redis / Celery ------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    celery_task_always_eager: bool = False
    # Verification tasks are read-only and bounded: a hung check must not hold a
    # worker slot forever, and the soft limit lets the task log before it is killed.
    worker_task_soft_time_limit_seconds: int = Field(default=240, ge=10, le=7_200)
    worker_task_time_limit_seconds: int = Field(default=300, ge=30, le=7_200)
    worker_result_expires_seconds: int = Field(default=3_600, ge=60)
    # Schedule of the read-only integrity checks (see app/worker/celery_app.py).
    audit_chain_check_minutes: int = Field(default=15, ge=1, le=1_440)
    ledger_check_every_hours: int = Field(default=6, ge=1, le=168)
    # Security housekeeping: revoke expired refresh tokens (hourly) and age out
    # completed idempotency records (daily, after IDEMPOTENCY_RETENTION_DAYS).
    token_sweep_minute_of_hour: int = Field(default=5, ge=0, le=59)
    idempotency_prune_hour: int = Field(default=3, ge=0, le=23)
    idempotency_prune_minute_of_hour: int = Field(default=20, ge=0, le=59)
    ledger_check_minute_of_hour: int = Field(default=30, ge=0, le=59)

    # --- Authentication ------------------------------------------------------
    jwt_secret: str
    jwt_refresh_secret: str
    jwt_algorithm: Literal["HS256", "HS384", "HS512", "RS256", "RS384", "RS512"] = "HS256"
    jwt_private_key_path: Path | None = None
    jwt_public_key_path: Path | None = None
    access_token_expire_minutes: int = Field(default=15, ge=1, le=1440)
    refresh_token_expire_days: int = Field(default=30, ge=1, le=365)
    argon2_time_cost: int = Field(default=3, ge=1, le=10)
    argon2_memory_cost: int = Field(default=65_536, ge=8_192, le=1_048_576)
    argon2_parallelism: int = Field(default=4, ge=1, le=16)
    login_max_failed_attempts: int = Field(default=5, ge=1, le=50)
    login_lockout_minutes: int = Field(default=15, ge=1, le=1440)

    # --- HTTP / security -----------------------------------------------------
    cors_origins: str = ""
    trusted_hosts: str = "localhost,127.0.0.1"
    rate_limit_enabled: bool = True
    rate_limit_auth_per_5min: int = Field(default=10, ge=0)
    rate_limit_refresh_per_min: int = Field(default=60, ge=0)
    rate_limit_write_per_min: int = Field(default=120, ge=0)
    rate_limit_read_per_min: int = Field(default=600, ge=0)
    # What a Redis outage means for rate-limited endpoints. Default (false) keeps the
    # counter working: the request is allowed and the outage is logged loudly, while the
    # database-backed account lockout still blocks brute force. Set true to refuse
    # instead (503) when the deployment prefers a hard guarantee over availability.
    rate_limit_fail_closed: bool = False
    secure_headers_enabled: bool = True
    force_https: bool = False

    # --- Domain policy -------------------------------------------------------
    base_currency_code: str = "AFN"
    rate_tolerance_bps: int = Field(default=50, ge=0, le=5_000)
    business_date_skew_minutes: int = Field(default=120, ge=0)
    offline_max_minutes_default: int = Field(default=480, ge=1)
    numbering_prefix_exchange: str = "NX"
    numbering_prefix_transfer: str = "TR"
    numbering_width: int = Field(default=6, ge=4, le=12)
    idempotency_retention_days: int = Field(default=30, ge=1)

    # --- Storage / backups ---------------------------------------------------
    storage_path: Path = Path("/data/storage")
    backup_enabled: bool = True
    backup_schedule_cron: str = "0 2 * * *"
    backup_retention_days: int = Field(default=30, ge=1)
    backup_encryption_recipient: str | None = None
    backup_verify_after_create: bool = True

    # --- Worker --------------------------------------------------------------
    change_log_retention_days: int = Field(default=180, ge=1)
    reconciliation_cron: str = "0 3 * * *"

    # --- Development seed ----------------------------------------------------
    dev_admin_username: str = "admin"
    dev_admin_password: str | None = None
    dev_admin_full_name: str = "System Administrator"

    # --- Observability -------------------------------------------------------
    sentry_dsn: str | None = None
    metrics_enabled: bool = False

    # ------------------------------------------------------------------ utils
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def trusted_hosts_list(self) -> list[str]:
        return [host.strip() for host in self.trusted_hosts.split(",") if host.strip()]

    @property
    def broker_url(self) -> str:
        """Celery broker — defaults to Redis database 1."""
        return self.celery_broker_url or self._redis_db(1)

    @property
    def result_backend_url(self) -> str:
        """Celery result backend — defaults to Redis database 2."""
        return self.celery_result_backend or self._redis_db(2)

    @property
    def migration_dsn_psycopg(self) -> str:
        """Synchronous DSN for Alembic and the seed runner.

        Migrations run as the schema owner over psycopg (simple-query protocol),
        which is what allows the frozen DDL file to be executed as a whole.
        """
        return to_psycopg_dsn(self.database_migration_url)

    def _redis_db(self, index: int) -> str:
        base, _, _ = self.redis_url.rpartition("/")
        return f"{base}/{index}" if base else self.redis_url

    # ------------------------------------------------------------- validators
    @model_validator(mode="after")
    def _validate_worker_limits(self) -> Settings:
        if self.worker_task_soft_time_limit_seconds >= self.worker_task_time_limit_seconds:
            raise ValueError(
                "WORKER_TASK_SOFT_TIME_LIMIT_SECONDS must be lower than "
                "WORKER_TASK_TIME_LIMIT_SECONDS"
            )
        return self

    @field_validator("tz")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - tzdata present
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value

    @field_validator("storage_path")
    @classmethod
    def _validate_storage_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("STORAGE_PATH must be an absolute path")
        return value

    @field_validator("database_url", "database_migration_url")
    @classmethod
    def _validate_database_url(cls, value: str, _info: object) -> str:
        if not value.startswith("postgresql+asyncpg://"):
            raise ValueError(
                "must use the async driver, e.g. "
                "postgresql+asyncpg://user:password@host:5432/nexus_exchange"
            )
        return value

    @field_validator("redis_url")
    @classmethod
    def _validate_redis_url(cls, value: str) -> str:
        if not value.startswith(("redis://", "rediss://", "unix://")):
            raise ValueError("REDIS_URL must start with redis://, rediss:// or unix://")
        return value

    @field_validator("jwt_secret", "jwt_refresh_secret")
    @classmethod
    def _validate_secret(cls, value: str, info: object) -> str:
        field = getattr(info, "field_name", "secret")
        env_field = field.upper()
        if value.strip().lower() in _FORBIDDEN_SECRET_VALUES:
            raise ValueError(
                f"{env_field} must be a strong random value; "
                'generate one with: python -c "import secrets;print(secrets.token_hex(32))"'
            )
        if len(value) < _MIN_SECRET_LENGTH:
            raise ValueError(f"{env_field} must be at least {_MIN_SECRET_LENGTH} characters long")
        return value

    @field_validator("numbering_prefix_exchange", "numbering_prefix_transfer")
    @classmethod
    def _validate_prefix(cls, value: str) -> str:
        if not value.isalpha() or not value.isupper() or len(value) > 6:
            raise ValueError("document number prefixes must be 1-6 uppercase letters")
        return value

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> Settings:
        if self.jwt_secret == self.jwt_refresh_secret:
            raise ValueError("JWT_SECRET and JWT_REFRESH_SECRET must be different values")

        if self.jwt_algorithm.startswith("RS") and not self.jwt_private_key_path:
            raise ValueError("JWT_PRIVATE_KEY_PATH is required when JWT_ALGORITHM uses RSA")

        wildcard = "*" in self.cors_origins_list
        if wildcard and self.is_production:
            raise ValueError("CORS_ORIGINS must not contain '*' in production")
        if wildcard and len(self.cors_origins_list) > 1:
            raise ValueError("CORS_ORIGINS must not mix '*' with explicit origins")

        if self.is_production:
            if not self.force_https:
                raise ValueError("FORCE_HTTPS must be true in production (nginx terminates TLS)")
            if self.dev_admin_password:
                raise ValueError(
                    "DEV_ADMIN_PASSWORD must be empty in production; "
                    "create the first administrator through the documented bootstrap procedure"
                )
            if self.backup_enabled and not self.backup_encryption_recipient:
                raise ValueError(
                    "BACKUP_ENCRYPTION_RECIPIENT is required when backups are enabled "
                    "in production (backups must be encrypted at rest)"
                )
            if not self.secure_headers_enabled:
                raise ValueError("SECURE_HEADERS_ENABLED must be true in production")

        if self.db_pool_size + self.db_max_overflow <= 0:
            raise ValueError("database pool must allow at least one connection")

        return self

    def __repr_args__(self) -> Iterator[tuple[str | None, Any]]:
        """Mask credential-bearing fields whenever the settings object is printed."""
        for name, value in super().__repr_args__():
            if name in _CREDENTIAL_FIELDS and value is not None:
                yield name, REDACTION_PLACEHOLDER
            else:
                yield name, value

    @property
    def safe_summary(self) -> dict[str, object]:
        """Non-secret configuration summary for startup logs and /version."""
        return {
            "app_env": self.app_env,
            "app_version": self.app_version,
            "log_level": self.log_level,
            "database_host": _dsn_host(self.database_url),
            "redis_host": _dsn_host(self.redis_url),
            "base_currency_code": self.base_currency_code,
            "rate_tolerance_bps": self.rate_tolerance_bps,
            "secure_headers_enabled": self.secure_headers_enabled,
            "force_https": self.force_https,
            "backup_enabled": self.backup_enabled,
            "rate_limit_enabled": self.rate_limit_enabled,
        }


def to_psycopg_dsn(async_dsn: str) -> str:
    """Translate an ``asyncpg`` SQLAlchemy DSN into a ``psycopg`` one."""
    for async_scheme, sync_scheme in (
        ("postgresql+asyncpg://", "postgresql+psycopg://"),
        ("postgresql+psycopg://", "postgresql+psycopg://"),
    ):
        if async_dsn.startswith(async_scheme):
            return sync_scheme + async_dsn[len(async_scheme) :]
    return async_dsn


def _dsn_host(dsn: str) -> str:
    """Extract ``host:port/database`` from a DSN without exposing credentials."""
    _, _, remainder = dsn.partition("://")
    _, _, host_part = remainder.rpartition("@")
    host_part = host_part or remainder
    return host_part.split("?")[0]


def load_settings() -> Settings:
    """Load and validate settings, raising a readable error when they are invalid."""
    try:
        return Settings()  # values come from the environment and the .env file
    except ValidationError as exc:  # pragma: no cover - exercised via tests
        raise SystemExit(
            "Invalid NEXUS configuration. Fix the following and restart:\n"
            + "\n".join(
                f"  - {'.'.join(str(loc) for loc in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            )
        ) from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor used by the application and the worker."""
    return load_settings()
