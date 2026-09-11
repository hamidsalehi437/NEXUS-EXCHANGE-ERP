# Task: write the remaining unit tests for apps/api

Working dir: `apps/api` (repository root is `/home/user/NEXUS-EXCHANGE-ERP`).

* Run tests: `cd /home/user/NEXUS-EXCHANGE-ERP/apps/api && PYTHONPATH=. /tmp/venv311/bin/python -m pytest tests/unit -q`
* Lint: `/tmp/venv311/bin/ruff check tests/unit` (must be clean)
* Style reference: `tests/unit/test_money.py` (already written, 42 passing tests) — follow it.

Rules:

* Unit tests only: no database, no network, no filesystem writes, no sleeps.
* Do **not** modify anything under `app/` or `seeds/`. If you find a real defect, leave it
  alone and report it in your final message.
* Mark every module with `pytestmark = pytest.mark.unit`.
* pytest is configured with `filterwarnings = ["error"]`: a warning fails the test.
* Use `pytest.raises` / `pytest.mark.parametrize`; do not mock domain logic.

## Files to create (all under `tests/unit/`)

### 1. `test_permissions.py` (`app/core/permissions.py`)

* `Permission` is a StrEnum; every value matches `^[a-z_]+\.[a-z_]+$` (resource.action).
* `RoleName` has exactly: SUPER_ADMIN, OWNER, MANAGER, ACCOUNTANT, CASHIER, AUDITOR.
* `ROLE_PERMISSIONS` and `ROLE_DESCRIPTIONS` keys equal `set(RoleName)`.
* SUPER_ADMIN and OWNER hold every permission; assert only what the module data actually says
  (read the file first).
* AUDITOR holds no create/cancel/reverse/close permission; it does hold read-only ones.
* CASHIER does not hold `rates.manage`, `users.manage`, `audit.view`, `branch.manage`.
* Every permission granted to any role is a member of `Permission`.

### 2. `test_password_security.py` (`app/core/security.py`)

* Use a fast hasher in tests: `PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)`
  (or `build_password_hasher` with the same values), otherwise the suite becomes slow.
* `hash()` returns an argon2id string (`"$argon2id$"`); `verify(password, hash)` returns
  `PasswordHash(is_valid=True, needs_rehash=False, algorithm="argon2id")`.
* Wrong password -> `is_valid` False. Hashing the same password twice yields different hashes
  (salt). The plaintext never appears in the hash.
* `verify()` on a malformed hash returns `is_valid=False` instead of raising.
* `.parameters` exposes time_cost/memory_cost/parallelism and no secret material.
* `enforce_password_policy` accepts a strong password and rejects (raising
  `PasswordPolicyError`): shorter than `MIN_PASSWORD_LENGTH`, longer than
  `MAX_PASSWORD_LENGTH`, leading/trailing whitespace, a value from `COMMON_PASSWORDS`,
  password equal to `username`, password equal to `full_name`, and fewer than three
  character classes.

### 3. `test_exceptions.py` (`app/core/exceptions.py`)

* `ErrorCode` members are stable UPPER_SNAKE strings (assert a few exact values found in the
  file, e.g. VALIDATION_ERROR, INTERNAL_ERROR).
* `NexusError` subclass HTTP statuses as defined in the module (ValidationError 422,
  ResourceNotFoundError 404, DuplicateResourceError 409, AuthenticationError 401,
  PermissionDeniedError 403, InsufficientBalanceError 409, ImmutableFieldError 409,
  AppendOnlyViolationError 403, ServiceUnavailableError 503).
* `error_for_sqlstate` mapping: NEX01 -> InsufficientBalanceError, NEX02 ->
  JournalUnbalancedError, NEX03 -> InvalidStatusTransitionError, NEX04 -> ReversalError,
  NEX05 -> CashReconciliationIncompleteError, NEX06 -> ImmutableFieldError, P0001 ->
  AppendOnlyViolationError, 23505 -> DuplicateResourceError, 40001/40P01 ->
  ServiceUnavailableError, 23503/23514 -> DataIntegrityError, None and "ZZZZZ" ->
  DataIntegrityError.
* The returned error keeps the message and details that were passed in.

### 4. `test_config_validation.py` (`app/core/config.py`)

* The test environment already provides required values (`tests/conftest.py` sets
  DATABASE_URL, DATABASE_MIGRATION_URL, REDIS_URL, JWT_SECRET, JWT_REFRESH_SECRET, ...).
  Explicit keyword arguments override the environment — use that to build invalid cases,
  e.g. `Settings(database_url="postgresql://u:p@h/db")`.
* Reject: non-async database URL, non-postgresql URL, REDIS_URL not starting with `redis://`,
  relative STORAGE_PATH, unknown TZ, worker soft limit >= hard limit, identical JWT secrets,
  CORS `*` mixed with explicit origins, production with `force_https=False`, production with
  `DEV_ADMIN_PASSWORD` set, production with backups enabled but no
  `BACKUP_ENCRYPTION_RECIPIENT`.
* Accept: a valid development configuration; CORS `*` alone in development.
* Properties: `is_production`/`is_test`, `trusted_hosts_list`/`cors_origins_list` split on
  commas and strip blanks, `broker_url`/`result_backend_url` derive Redis db 1/2 from
  REDIS_URL when the celery URLs are unset, `migration_dsn_psycopg` converts the asyncpg DSN
  to psycopg.
* `load_settings()` returns a `Settings` instance.

### 5. `test_logging.py` (`app/core/logging.py`)

* `configure_logging(level="INFO", fmt="json")`, then emit an event with
  `get_logger("test")` and assert via `capsys` that secrets are redacted: a DSN password
  (`postgresql://user:s3cret@host:5432/db`), a bearer token (`Bearer abc.def.ghi`) and an
  argon2 hash must not appear in the output, while `[redacted]` does.
* The JSON line still contains the event name and the non-secret fields.
* `get_logger` returns an object with `.info`/`.warning` callables.

### 6. `test_worker_configuration.py` (`app/worker/celery_app.py`)

* Importing `app.worker.celery_app` must not connect anywhere.
* `celery_app.conf`: UTC timezone enabled; `task_acks_late` True;
  `task_reject_on_worker_lost` True; `worker_prefetch_multiplier` 1;
  `task_soft_time_limit` < `task_time_limit`.
* Task registry contains:
  `app.worker.integrity.verify_audit_chain`, `app.worker.integrity.verify_ledger_integrity`,
  `app.worker.maintenance.sweep_expired_refresh_tokens`,
  `app.worker.maintenance.prune_idempotency_keys`.
* `task_routes` sends `app.worker.integrity.*` to the `integrity` queue and
  `app.worker.maintenance.*` to the `maintenance` queue; `task_default_queue` equals
  `DEFAULT_QUEUE` ("maintenance"); `WORKER_QUEUES == ("integrity", "maintenance")`.
* `beat_schedule` keys are exactly `{"verify-audit-chain", "verify-ledger-integrity",
  "sweep-expired-refresh-tokens", "prune-idempotency-keys"}`, each naming a registered task.
* `_redacted("redis://:pw@host:6379/1")` hides the password; a URL without credentials is
  returned unchanged.

## Finish

1. `PYTHONPATH=. /tmp/venv311/bin/python -m pytest tests/unit -q` — everything must pass.
2. `/tmp/venv311/bin/ruff check tests/unit` — must be clean.
3. Report: files created, number of tests, and any app-code defect found (do not fix it).
