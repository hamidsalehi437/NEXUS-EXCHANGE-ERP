#!/usr/bin/env bash
# =============================================================================
# NEXUS EXCHANGE ERP — the full local verification chain (PART 48, PART 59)
# =============================================================================
# Lint, type-check, unit tests, integration tests and the two schema gates. This
# is the same sequence CI runs, so a green local run means a green pull request.
#
#   scripts/test_all.sh                     # everything below
#   scripts/test_all.sh --unit              # unit tests only (fast)
#   scripts/test_all.sh --integration       # integration tests only
#
# Integration tests create and drop their own databases (nexus_test_*) and need
# NEXUS_TEST_REDIS_URL to point at a Redis instance (db 15 is used).
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
API_DIR="${ROOT_DIR}/apps/api"
cd "${API_DIR}"

PYTHON="${PYTHON:-python3}"
export PYTHONPATH="${API_DIR}"
export NEXUS_TEST_REDIS_URL="${NEXUS_TEST_REDIS_URL:-redis://:nexuslocaldev@127.0.0.1:6379/15}"

STEP=0
run() {
  STEP=$((STEP + 1))
  echo
  echo "==> [${STEP}] $*"
  "$@"
}

SELECTION="all"
for argument in "$@"; do
  case "${argument}" in
    --unit) SELECTION="unit" ;;
    --integration) SELECTION="integration" ;;
    -h|--help) sed -n '2,13p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "error: unknown argument '${argument}'" >&2; exit 2 ;;
  esac
done

if [[ "${SELECTION}" != "integration" ]]; then
  run "${PYTHON}" -m ruff check .
  run "${PYTHON}" -m ruff format --check .
  run "${PYTHON}" -m mypy app seeds scripts
  run "${PYTHON}" -m pytest tests/unit -q
fi

if [[ "${SELECTION}" != "unit" ]]; then
  run "${PYTHON}" -m pytest tests/integration -q
  run "${PYTHON}" -m scripts.schema_gate orm-db
  run "${PYTHON}" -m scripts.schema_gate db-db \
    --left "${REFERENCE_DSN:-postgresql+psycopg://postgres@127.0.0.1:5432/nexus_reference}" \
    --right "${TARGET_DSN:-postgresql+psycopg://postgres@127.0.0.1:5432/nexus_exchange}"
fi

echo
echo "all requested checks passed"
