#!/usr/bin/env bash
# =============================================================================
# NEXUS EXCHANGE ERP — load/refresh reference data (PART 43, PART 58)
# =============================================================================
#   scripts/seed.sh            # apply the seeds (idempotent upsert by natural key)
#   scripts/seed.sh --check    # report what would change, exit 1 if anything would
#
# Currencies, roles/permissions and the chart of accounts are reference data:
# without them no transaction can be posted. The runner never deletes a row and
# never overwrites an existing balance — `removed` must stay 0.
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

if [[ "${1:-}" == "--check" ]]; then
  docker compose exec -T api python -m seeds --check
else
  docker compose exec -T api python -m seeds
fi
