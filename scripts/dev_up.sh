#!/usr/bin/env bash
# =============================================================================
# NEXUS EXCHANGE ERP — bring the whole stack up, migrated and seeded
# =============================================================================
# One command for a developer or an operator on a staging host:
#
#   scripts/dev_up.sh              # build, start, wait for health, migrate, seed
#   scripts/dev_up.sh --no-seed    # same, but skip the reference data
#
# Safe to re-run: the migration is idempotent (Alembic records the revision) and
# the seed runner upserts by natural key, reporting unchanged rows as unchanged.
# =============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

SEED=1
for argument in "$@"; do
  case "${argument}" in
    --no-seed) SEED=0 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "error: unknown argument '${argument}'" >&2; exit 2 ;;
  esac
done

require() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' is required (PART 58: Docker + Compose v2)" >&2; exit 1; }
}
require docker

if [[ ! -f .env ]]; then
  echo "no .env found — generating one from .env.example"
  scripts/gen_env.sh
fi

echo "==> starting the stack (api, postgres, redis, nginx, worker)"
docker compose up -d --build --wait

echo "==> applying migrations"
docker compose exec -T api alembic upgrade head
docker compose exec -T api alembic current

if [[ "${SEED}" -eq 1 ]]; then
  echo "==> seeding reference data (idempotent)"
  docker compose exec -T api python -m seeds
fi

echo "==> readiness"
docker compose exec -T api python - <<'PY'
import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health/ready", timeout=10) as response:
    payload = json.load(response)
print(json.dumps(payload, indent=2))
raise SystemExit(0 if payload["status"] == "ready" else 1)
PY

PORT="$(grep -E '^NGINX_HTTP_PORT=' .env | cut -d= -f2 || true)"
echo
echo "stack is up:"
echo "  API through nginx : http://127.0.0.1:${PORT:-8080}/api/v1/health"
echo "  readiness         : http://127.0.0.1:${PORT:-8080}/api/v1/health/ready"
echo "  logs              : docker compose logs -f api worker"
