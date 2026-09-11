#!/usr/bin/env bash
# =============================================================================
# NEXUS EXCHANGE ERP — apply database migrations (PART 55, PART 58)
# =============================================================================
# Runs `alembic upgrade head` inside the api container with the migration role.
#
#   scripts/migrate.sh              # upgrade to head
#   scripts/migrate.sh --current    # show the applied revision only
#   scripts/migrate.sh --dry-run    # print the SQL that would run, change nothing
#
# The revision executes a frozen copy of docs/database/schema.sql and verifies
# its sha256 before touching the database, so a tampered or stale reference file
# aborts the migration instead of producing a different schema.
#
# Pre-flight backups (PART 55) are a deployment requirement: on a database that
# already holds financial data, take a verified backup first (docs/DEPLOYMENT.md
# §6 and docs/BACKUP.md, Phase 12).
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

MODE="upgrade"
for argument in "$@"; do
  case "${argument}" in
    --current) MODE="current" ;;
    --dry-run) MODE="dry-run" ;;
    -h|--help) sed -n '2,16p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "error: unknown argument '${argument}'" >&2; exit 2 ;;
  esac
done

case "${MODE}" in
  upgrade)
    docker compose exec -T api alembic upgrade head
    docker compose exec -T api alembic current
    ;;
  current)
    docker compose exec -T api alembic current
    ;;
  dry-run)
    docker compose exec -T api alembic upgrade head --sql
    ;;
esac
