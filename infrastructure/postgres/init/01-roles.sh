#!/usr/bin/env bash
# =============================================================================
# NEXUS EXCHANGE ERP — PostgreSQL container bootstrap (PART 42, PART 44)
# =============================================================================
# Runs once, on first initialisation of the data directory, as the container's
# superuser (POSTGRES_USER) on the database POSTGRES_DB.
#
# It creates the role split the schema expects (docs/database/SCHEMA.md §8):
#
#   group roles (NOLOGIN, no password)   created here so the migration's own
#                                        idempotent `CREATE ROLE` block is a
#                                        no-op and needs no CREATEROLE right:
#     nexus_owner    owns the schema, runs migrations and seeds
#     nexus_app      application runtime: DML only, no DELETE on ledgers
#     nexus_reader   read-only reporting/BI
#     nexus_auditor  read-only on audit + ledgers
#
#   login roles (password from the environment, never from this file):
#     nexus_migrator  IN ROLE nexus_owner  — alembic + seeds (DATABASE_MIGRATION_URL)
#     nexus_api       IN ROLE nexus_app    — uvicorn + celery   (DATABASE_URL)
#
# Secrets are read with psql's `\getenv`, so the passwords reach PostgreSQL as
# query parameters. They are never passed as command-line arguments (which would
# be visible in `ps`) and never written to disk by this script.
# =============================================================================
set -euo pipefail

if [[ -z "${NEXUS_API_PASSWORD:-}" || -z "${NEXUS_MIGRATOR_PASSWORD:-}" ]]; then
    echo "error: NEXUS_API_PASSWORD and NEXUS_MIGRATOR_PASSWORD must be set" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 \
     --username "${POSTGRES_USER}" \
     --dbname "${POSTGRES_DB}" <<'SQL'
\getenv api_password NEXUS_API_PASSWORD
\getenv migrator_password NEXUS_MIGRATOR_PASSWORD

-- Group roles: idempotent, exactly as docs/database/schema.sql §0 declares them.
SELECT format('CREATE ROLE %I NOLOGIN', role_name)
  FROM (VALUES ('nexus_owner'), ('nexus_app'), ('nexus_reader'), ('nexus_auditor')) AS g(role_name)
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name)
\gexec

-- The migration role needs the group's identity plus CREATEROLE, because the
-- frozen reference DDL contains an idempotent role-provisioning block that must
-- remain runnable on a database where those group roles do not exist yet.
SELECT format('CREATE ROLE %I LOGIN IN ROLE nexus_owner CREATEROLE', 'nexus_migrator')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_migrator')
\gexec

SELECT format('CREATE ROLE %I LOGIN IN ROLE nexus_app', 'nexus_api')
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_api')
\gexec

-- Keep the stored passwords in step with the environment: rotating a value in
-- .env and re-running this script must change the credential actually used.
-- Membership is granted separately: ALTER ROLE has no IN ROLE clause, and
-- re-granting an existing membership is a no-op.
-- format(%L) quotes the literal, and `\getenv` means it was never in argv.
SELECT format('ALTER ROLE %I WITH LOGIN CREATEROLE PASSWORD %L',
              'nexus_migrator', :'migrator_password')
\gexec

SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', 'nexus_api', :'api_password')
\gexec

GRANT nexus_owner TO nexus_migrator;
GRANT nexus_app TO nexus_api;

-- The schema owner runs the migration as the *login* role nexus_migrator.
-- Schema privileges are not inherited the way table privileges are: the public
-- schema belongs to pg_database_owner, and a member of that group still needs an
-- explicit grant to create objects in it. WITH GRANT OPTION is required because
-- the reference DDL itself grants USAGE on this schema to the application roles.
GRANT USAGE, CREATE ON SCHEMA public TO nexus_owner WITH GRANT OPTION;
GRANT USAGE, CREATE ON SCHEMA public TO nexus_migrator WITH GRANT OPTION;

-- PUBLIC keeps no rights on the schema: only the owning roles create objects,
-- the application roles only read and write them (PG15+ already defaults to
-- this; stated explicitly so the rule is not lost to a future upgrade).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

REVOKE CREATE ON SCHEMA public FROM nexus_app, nexus_reader, nexus_auditor;

-- A connection-level safety net for the application roles: a runaway statement
-- or an abandoned transaction cannot pin resources indefinitely. The API sets
-- its own timeouts too (DB_STATEMENT_TIMEOUT_MS); this is the floor.
SELECT format('ALTER ROLE %I SET statement_timeout = %L', 'nexus_api', '15s')
\gexec
SELECT format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', 'nexus_api', '30s')
\gexec
SELECT format('ALTER ROLE %I SET lock_timeout = %L', 'nexus_api', '5s')
\gexec
SELECT format('ALTER ROLE %I SET timezone = %L', 'nexus_api', 'UTC')
\gexec
SELECT format('ALTER ROLE %I SET timezone = %L', 'nexus_migrator', 'UTC')
\gexec

\echo 'nexus roles ready: nexus_migrator (owner), nexus_api (runtime), nexus_reader, nexus_auditor'
SQL
