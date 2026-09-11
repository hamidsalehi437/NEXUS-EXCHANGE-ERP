#!/usr/bin/env bash
# =============================================================================
# NEXUS EXCHANGE ERP — stop the stack
# =============================================================================
#   scripts/dev_down.sh            # stop containers, keep volumes (data survives)
#   scripts/dev_down.sh --volumes  # stop and DELETE the database/redis volumes
#
# Deleting volumes destroys the ledger and the audit log; it is only ever a
# developer action on a disposable environment (PART 18/PART 22: financial and
# audit history is never deleted by the system itself).
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

VOLUMES=0
for argument in "$@"; do
  case "${argument}" in
    --volumes) VOLUMES=1 ;;
    -h|--help) sed -n '2,8p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "error: unknown argument '${argument}'" >&2; exit 2 ;;
  esac
done

if [[ "${VOLUMES}" -eq 1 ]]; then
  echo "WARNING: this deletes nexus-postgres-data, nexus-redis-data and nexus-api-storage."
  read -r -p "type 'yes' to continue: " confirmation
  [[ "${confirmation}" == "yes" ]] || { echo "aborted"; exit 1; }
  docker compose down --volumes --remove-orphans
else
  docker compose down --remove-orphans
fi
